# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The MCP Resource surface: the packaged guidance served over resources/*.

Every request runs through the REAL SimpleMCPServer.handle_request over the catalog the production
builder hands start_server, so what is asserted here is what a client that speaks resources
receives - the capability advertised only when the content actually loaded, one listing row without
the body in it, the read, and the two refusal codes.

The parity class holds the served Markdown against the committed skill minus its frontmatter: one
authored render with two consumers, rather than two renders that happen to agree today.
"""

import asyncio
import json
import os
import re
import sys

import pytest

import gen_guidance
from conftest import COMMANDS_DIR, load_mcp_server

if COMMANDS_DIR not in sys.path:                   # the flat package root the add-in code lives in
    sys.path.insert(0, COMMANDS_DIR)
from mcpServer.guidance import loader, render, resources  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKILL_PATH = os.path.join(_REPO, ".claude", "skills", "parametric-cad-design", "SKILL.md")

# The addresses this server publishes: the whole document, then one per non-kernel section (the
# kernel is IN every reading of the document, so it has no address of its own). Written out here
# rather than read back from the catalog: a test that took the URI from the thing under test would
# pass whatever it became.
URI = "fusion-essentials://guidance/parametric-cad-design"
SECTION_URIS = [f"{URI}/{s}" for s in loader.SECTION_IDS if s != loader.KERNEL]
URIS = [URI] + SECTION_URIS


@pytest.fixture(scope="module")
def mcp():
    return load_mcp_server()


@pytest.fixture
def served(mcp):
    """The server as entry.py starts it: the real packaged catalog, handed in."""
    return mcp.SimpleMCPServer(resources=resources.catalog())


@pytest.fixture
def bare(mcp):
    """The same server with no catalog - what a guidance document that did not load leaves."""
    return mcp.SimpleMCPServer()


# The id every request below travels with. A client matches a response to its call by this alone,
# so it is distinctive rather than 1: an envelope that dropped it (or hardcoded one) is only
# visible against an id nothing else in the exchange could produce.
_REQUEST_ID = "g3-resource-42"


def _request(server, method, params=None, request_id=_REQUEST_ID):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return asyncio.run(server.handle_request(body))


def _result(response, request_id=_REQUEST_ID):
    assert "error" not in response, response
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == request_id, "the response must echo the id its request carried"
    return response["result"]


def _error(response, request_id=_REQUEST_ID):
    assert "result" not in response, response
    assert response["id"] == request_id, "an error must echo the id its request carried"
    return response["error"]


def _body():
    """The Markdown the packaged document renders to, read independently of the server."""
    doc, _sha = loader.load()
    return render.body(doc)


def _section_body(section_id):
    """One section's Markdown, read the same way."""
    doc, _sha = loader.load()
    return render.section_text(doc, section_id)


def _rows(server):
    return _result(_request(server, "resources/list"))["resources"]


# ── initialize: the capability is advertised only when there is something to serve ──

class TestInitializeAdvertisesResourcesWhenTheCatalogLoaded:
    def test_a_loaded_catalog_advertises_the_resources_capability(self, served):
        capabilities = _result(_request(served, "initialize", {}))["capabilities"]
        assert capabilities["resources"] == {}

    def test_an_empty_catalog_advertises_no_resources_capability(self, bare):
        # a document that did not load must leave the capability UNADVERTISED - a client that never
        # hears of resources never asks for one that would fail.
        capabilities = _result(_request(bare, "initialize", {}))["capabilities"]
        assert "resources" not in capabilities
        assert capabilities["tools"] == {}, "the tool surface is unaffected either way"

    def test_neither_subscribe_nor_list_changed_is_claimed(self, served):
        # the catalog is fixed for the server's lifetime: there is nothing to subscribe to and no
        # change to notify, so the advertisement carries no flag it cannot honor.
        capabilities = _result(_request(served, "initialize", {}))["capabilities"]
        assert list(capabilities["resources"].keys()) == []

    def test_the_protocol_revision_is_unchanged(self, mcp, served):
        # resources already exist in 2025-03-26; a bump would claim unrelated semantics.
        assert mcp.SUPPORTED_PROTOCOL_VERSIONS == ("2025-03-26",)
        result = _result(_request(served, "initialize", {"protocolVersion": "2025-03-26"}))
        assert result["protocolVersion"] == "2025-03-26"


# ── resources/list ──────────────────────────────────────────────────────────

class TestResourcesList:
    def test_the_listing_names_the_document_and_each_of_its_sections(self, served):
        rows = _rows(served)
        assert [row["uri"] for row in rows] == URIS
        assert rows[0]["name"] == "parametric-cad-design"
        assert rows[1]["name"] == "parametric-cad-design/plan"

    def test_a_section_row_offers_its_own_use_when_as_the_description(self, served):
        # what a client chooses BY: the whole document's description says when to read the
        # document, and a section's says when to read that section.
        doc, _sha = loader.load()
        plan = loader.find_section(doc, "plan")
        row = {r["uri"]: r for r in _rows(served)}[f"{URI}/plan"]
        assert row["title"] == plan["title"]
        assert row["description"] == plan["use_when"]

    def test_the_row_carries_the_display_fields_a_client_shows(self, served):
        row = _rows(served)[0]
        assert row["title"] == "Designing in Fusion"
        assert row["mimeType"] == "text/markdown"
        assert row["description"].startswith("Use when designing or modelling in Fusion")

    def test_the_advertised_size_is_the_byte_length_of_what_a_read_returns(self, served):
        assert _rows(served)[0]["size"] == len(_body().encode("utf-8"))

    def test_the_listing_never_carries_the_body(self, served):
        # a listing that shipped the document would cost every client the whole read it did not ask
        # for - the body belongs to resources/read.
        row = _rows(served)[0]
        assert "text" not in row
        assert set(row) <= {"uri", "name", "title", "description", "mimeType", "size"}

    def test_the_listing_issues_no_cursor(self, served):
        assert "nextCursor" not in _result(_request(served, "resources/list"))

    def test_an_empty_catalog_lists_nothing(self, bare):
        assert _rows(bare) == []

    def test_a_cursor_this_server_never_issued_is_refused(self, served):
        # the whole catalog comes back in one page; answering a page for a cursor would tell a
        # paginating client it had resumed something.
        error = _error(_request(served, "resources/list", {"cursor": "page-2"}))
        assert error["code"] == -32602
        assert "cursor" in error["message"]

    def test_an_explicitly_null_cursor_is_not_a_refusal(self, served):
        # absent and null both mean "no cursor" - only a VALUE this server never issued is refused.
        assert (len(_result(_request(served, "resources/list", {"cursor": None}))["resources"])
                == len(URIS))

    def test_params_that_are_not_an_object_are_refused(self, served):
        assert _error(_request(served, "resources/list", ["cursor"]))["code"] == -32602

    def test_an_explicitly_null_params_member_reads_as_no_arguments(self, served):
        # JSON-RPC lets a client send "params": null, which means "no arguments" - not a malformed
        # call. Sent as the raw body, since a helper that omits the key would not exercise it.
        response = asyncio.run(served.handle_request(
            {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": "resources/list", "params": None}))
        assert [row["uri"] for row in _result(response)["resources"]] == URIS


# ── resources/read ──────────────────────────────────────────────────────────

class TestResourcesRead:
    def test_reading_the_published_uri_returns_the_rendered_markdown(self, served):
        contents = _result(_request(served, "resources/read", {"uri": URI}))["contents"]
        assert len(contents) == 1
        assert contents[0]["text"] == _body()

    def test_reading_one_section_returns_that_section_alone(self, served):
        contents = _result(_request(served, "resources/read",
                                    {"uri": f"{URI}/assemble"}))["contents"]
        assert contents[0]["text"] == _section_body("assemble")
        assert "## Validate" not in contents[0]["text"]

    def test_the_content_row_names_its_uri_and_its_markdown_type(self, served):
        content = _result(_request(served, "resources/read", {"uri": URI}))["contents"][0]
        assert content["uri"] == URI
        assert content["mimeType"] == "text/markdown"

    def test_an_unknown_uri_is_refused_naming_it_and_what_is_published(self, served):
        error = _error(_request(served, "resources/read",
                                {"uri": "fusion-essentials://guidance/cam-strategy"}))
        assert error["code"] == -32002
        assert "cam-strategy" in error["message"]
        assert URI in error["message"], "the client needs the address that does resolve"

    def test_a_near_miss_uri_does_not_resolve(self, served):
        # the address is matched exactly: a trailing slash is a different resource, not this one.
        assert _error(_request(served, "resources/read", {"uri": URI + "/"}))["code"] == -32002

    def test_a_missing_uri_parameter_is_refused_as_invalid_params(self, served):
        error = _error(_request(served, "resources/read", {}))
        assert error["code"] == -32602
        assert "uri" in error["message"]

    @pytest.mark.parametrize("uri", [42, ["a"], {"uri": "a"}, None, "", "   "])
    def test_a_uri_that_is_not_an_address_is_refused_as_invalid_params(self, served, uri):
        assert _error(_request(served, "resources/read", {"uri": uri}))["code"] == -32602

    def test_params_that_are_not_an_object_are_refused(self, served):
        assert _error(_request(served, "resources/read", "the-uri"))["code"] == -32602

    def test_an_explicitly_null_params_member_is_a_read_with_no_uri(self, served):
        response = asyncio.run(served.handle_request(
            {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": "resources/read", "params": None}))
        assert _error(response)["code"] == -32602

    def test_reading_from_an_empty_catalog_refuses_rather_than_serving_nothing(self, bare):
        error = _error(_request(bare, "resources/read", {"uri": URI}))
        assert error["code"] == -32002
        assert "no resources" in error["message"]


# ── resources/templates/list ────────────────────────────────────────────────

class TestResourceTemplatesList:
    def test_the_template_listing_is_exactly_empty(self, served):
        assert _result(_request(served, "resources/templates/list")) == {"resourceTemplates": []}

    def test_a_bare_probe_carrying_an_empty_params_object_still_gets_the_empty_list(self, served):
        # every published resource is a fixed address, so there is no template to expand: the empty
        # list is the true answer to a well-formed probe, not an error to special-case.
        assert _result(_request(served, "resources/templates/list", {})) == {"resourceTemplates": []}

    def test_an_empty_catalog_answers_the_same(self, bare):
        assert _result(_request(bare, "resources/templates/list")) == {"resourceTemplates": []}

    def test_a_cursor_this_server_never_issued_is_refused_here_too(self, served):
        # one server, one answer to one bad call: this listing issues no cursor either, so
        # succeeding on one would tell a paginating client it had resumed something.
        error = _error(_request(served, "resources/templates/list", {"cursor": "page-2"}))
        assert error["code"] == -32602
        assert "cursor" in error["message"]
        assert "resources/templates/list" in error["message"], "the refusal names the method called"

    def test_params_that_are_not_an_object_are_refused(self, served):
        assert _error(_request(served, "resources/templates/list", ["cursor"]))["code"] == -32602

    def test_an_explicitly_null_cursor_is_not_a_refusal(self, served):
        assert _result(_request(served, "resources/templates/list",
                                {"cursor": None})) == {"resourceTemplates": []}

    def test_an_explicitly_null_params_member_reads_as_no_arguments(self, served):
        response = asyncio.run(served.handle_request(
            {"jsonrpc": "2.0", "id": _REQUEST_ID, "method": "resources/templates/list",
             "params": None}))
        assert _result(response) == {"resourceTemplates": []}

    def test_the_two_listings_refuse_the_same_bad_call_the_same_way(self, served):
        # the consistency itself: a client cannot code against a server that answers one malformed
        # call two ways, so the pair is asserted together rather than one handler at a time.
        for params in ({"cursor": "page-2"}, ["cursor"], "not-an-object", 7):
            codes = {method: _error(_request(served, method, params))["code"]
                     for method in ("resources/list", "resources/templates/list")}
            assert set(codes.values()) == {-32602}, (params, codes)


# ── what this server does NOT implement ─────────────────────────────────────

class TestUnimplementedMethodsAreMethodNotFound:
    @pytest.mark.parametrize("method", [
        "prompts/list",             # the Prompt surface is deliberately not implemented
        "prompts/get",
        "resources/subscribe",      # not advertised, so not implemented
        "resources/unsubscribe",
        "resources/read/all",       # a near-miss on a method that exists
    ])
    def test_an_unimplemented_method_is_refused_as_method_not_found(self, served, method):
        error = _error(_request(served, method, {}))
        assert error["code"] == -32601
        assert method in error["message"]


# ── a catalog entry that cannot be served is never advertised ───────────────

class TestUnservableEntriesAreDropped:
    @pytest.mark.parametrize("entry", [
        {"uri": URI, "name": "guidance"},                 # nothing to serve
        {"name": "guidance", "text": "# body"},           # no address to serve it at
        {"uri": "", "name": "guidance", "text": "# body"},  # an empty address is no address
        {"uri": URI, "name": "guidance", "text": None},
        {"uri": URI, "text": "# body"},                   # nameless: the row would name nothing
        {"uri": URI, "name": None, "text": "# body"},     # a present-but-null name reaches the wire
        {"uri": URI, "name": "", "text": "# body"},       # an empty name is no name
        {"uri": URI, "name": 7, "text": "# body"},
        "not-a-resource",
    ])
    def test_an_entry_missing_an_address_a_name_or_a_body_is_not_published(self, mcp, entry):
        server = mcp.SimpleMCPServer(resources=[entry])
        assert _rows(server) == []
        assert "resources" not in _result(_request(server, "initialize", {}))["capabilities"]

    def test_a_nameless_entry_is_dropped_rather_than_listed_as_one_field(self, mcp):
        # the projection copies whatever fields an entry carries, so a nameless entry that got in
        # would be advertised as a row with a uri and nothing else - and a null name would cross the
        # wire as `"name": null`, which is worse than not offering the resource at all.
        server = mcp.SimpleMCPServer(resources=[{"uri": "x://nameless", "text": "# body"},
                                                {"uri": "x://null-name", "name": None,
                                                 "text": "# body"}])
        assert server.resources == []
        assert _error(_request(server, "resources/read",
                               {"uri": "x://nameless"}))["code"] == -32002

    def test_a_servable_entry_beside_an_unservable_one_is_still_served(self, mcp):
        server = mcp.SimpleMCPServer(resources=[{"uri": "x://broken", "name": "broken"},
                                                {"uri": "x://ok", "name": "ok",
                                                 "text": "# body"}])
        assert [row["uri"] for row in _rows(server)] == ["x://ok"]
        assert _rows(server)[0]["name"] == "ok"
        assert _result(_request(server, "resources/read",
                                {"uri": "x://ok"}))["contents"][0]["text"] == "# body"

    def test_every_published_row_carries_the_two_fields_the_spec_requires(self, served):
        # uri + name are what a client addresses and shows; admission is what guarantees both,
        # since the listing projection only copies the fields an entry happens to carry.
        for row in _rows(served):
            assert isinstance(row["uri"], str) and row["uri"]
            assert isinstance(row["name"], str) and row["name"]


# ── the last hop: start_server hands the catalog to the server it builds ────
#
# entry.py builds the catalog and start_server is what turns it into a serving instance, so the
# argument can be dropped at that one call and every handler test above still passes. The real
# function runs here with its two side effects stubbed - no socket is bound and no thread serves.

class _NoSocketHTTPServer:
    """ThreadedHTTPServer's surface, minus the bind: the constructor and the loop start_server
    hands its thread."""

    def __init__(self, address, handler_cls):
        self.address = address
        self.handler_cls = handler_cls

    def serve_forever(self):
        raise AssertionError("the serving loop must not run in a unit test")


class _NoThread:
    """threading.Thread's constructor + start(), running nothing."""

    def __init__(self, target=None, daemon=None, name=None):
        self.target, self.daemon, self.name = target, daemon, name
        self.started = False

    def start(self):
        self.started = True


@pytest.fixture
def no_transport(mcp, monkeypatch):
    """start_server with its bind and its serving thread replaced - everything else is the real
    function, including the SimpleMCPServer it constructs."""
    monkeypatch.setattr(mcp, "ThreadedHTTPServer", _NoSocketHTTPServer)
    monkeypatch.setattr(mcp.threading, "Thread", _NoThread)
    monkeypatch.setattr(mcp.drawing_jobs, "_DEFAULT_STORE", mcp.drawing_jobs._DEFAULT_STORE)
    return mcp


class TestStartServerPublishesTheCatalogItWasHanded:
    def _start(self, mcp, resources_arg):
        result = mcp.start_server(
            "127.0.0.1", 27182, items=[], resources=resources_arg, job_store=object())
        assert result["status"] == mcp.START_OK, result
        assert result["thread"].started, "the serving thread must still be started"
        return result["mcp"]

    def test_the_catalog_reaches_the_server_start_server_builds(self, no_transport):
        server = self._start(no_transport, resources.catalog())
        assert [r["uri"] for r in server.resources] == URIS
        assert server.resources[0]["text"] == _body()

    def test_that_server_advertises_and_serves_the_resource(self, no_transport):
        # the whole point of the hop: the instance the transport actually runs is the one that has
        # to advertise the capability and answer the read.
        server = self._start(no_transport, resources.catalog())
        assert _result(_request(server, "initialize", {}))["capabilities"]["resources"] == {}
        assert _result(_request(server, "resources/read",
                                {"uri": URI}))["contents"][0]["text"] == _body()

    def test_no_catalog_leaves_that_server_advertising_none(self, no_transport):
        server = self._start(no_transport, [])
        assert server.resources == []
        assert "resources" not in _result(_request(server, "initialize", {}))["capabilities"]


# ── the catalog builder itself ──────────────────────────────────────────────

class TestTheCatalogBuilder:
    def test_it_publishes_the_whole_document_then_one_entry_per_non_kernel_section(self):
        catalog = resources.catalog()
        assert [e["uri"] for e in catalog] == URIS
        assert catalog[0]["text"] == _body()
        assert catalog[1]["text"] == _section_body("plan")

    def test_the_kernel_gets_no_address_of_its_own(self):
        # it is carried by every reading of the document (and by the skill map), so a separate
        # address for it would publish the same text twice.
        assert f"{URI}/{loader.KERNEL}" not in [e["uri"] for e in resources.catalog()]

    def test_the_address_is_built_from_the_documents_own_id(self):
        doc, _sha = loader.load()
        assert resources.catalog()[0]["uri"] == resources.uri_for(doc["guidance_id"]) == URI

    def test_the_size_counts_bytes_rather_than_characters(self, tmp_path):
        # the field is documented in bytes, which only a non-ASCII document can tell apart from a
        # character count. The shipped document is pure ASCII, so the distinction is probed here.
        path = tmp_path / "wide.json"
        path.write_text(json.dumps({
            "guidance_id": "probe", "name": "probe", "title": "Probe \u00b5", "summary": "s",
            "sections": [{"id": i, "title": i, "rules": []} for i in loader.SECTION_IDS],
        }), encoding="utf-8")
        entry = resources.catalog(str(path))[0]
        assert entry["size"] == len(entry["text"].encode("utf-8")) > len(entry["text"])

    def test_it_builds_from_any_working_directory(self, monkeypatch, tmp_path):
        # the add-in's process CWD is Fusion's, not the repo: the packaged file is found beside the
        # code or not at all.
        monkeypatch.chdir(tmp_path)
        assert resources.catalog()[0]["text"] == _body()

    def test_a_missing_packaged_document_is_reported_by_name(self, tmp_path):
        missing = str(tmp_path / "gone.json")
        with pytest.raises(loader.GuidanceUnavailable) as exc:
            resources.catalog(missing)
        assert "gone.json" in str(exc.value)


# ── parity: the resource body IS the committed skill's body ─────────────────

class TestTheServedTextIsTheCommittedPackage:
    def _committed_body(self):
        with open(_SKILL_PATH, encoding="utf-8") as fh:
            committed = fh.read()
        match = re.match(r"^---\n.*?\n---\n+", committed, re.S)
        assert match, "the committed skill lost its frontmatter block"
        return committed[match.end():]

    def test_each_served_section_equals_the_committed_playbook_file(self, served):
        # the two channels an agent can reach a playbook through - the resource read and the file
        # on disk - are one render, not two that agree today.
        for section_id in loader.SECTION_IDS:
            if section_id == loader.KERNEL:
                continue
            text = _result(_request(served, "resources/read",
                                    {"uri": f"{URI}/{section_id}"}))["contents"][0]["text"]
            with open(gen_guidance.playbook_path(section_id), encoding="utf-8") as fh:
                assert fh.read() == text, section_id

    def test_the_committed_skill_is_the_map_while_the_resource_serves_the_document(self, served):
        # the SKILL.md an agent carries is a MAP: the kernel plus where to fetch the rest. The
        # resource channel is where the whole document lives, so they are NOT the same text.
        doc, _sha = loader.load()
        assert self._committed_body() == render.map_text(doc)
        served_text = _result(_request(served, "resources/read", {"uri": URI}))["contents"][0]["text"]
        assert served_text == _body() != self._committed_body()

    def test_the_frontmatter_a_skill_loader_needs_is_not_served(self, served):
        text = _result(_request(served, "resources/read", {"uri": URI}))["contents"][0]["text"]
        assert text.startswith("# Designing in Fusion")
        assert "name: parametric-cad-design" not in text

    def test_the_generator_renders_through_the_shipped_functions(self):
        # the consolidation itself: one set of render functions, imported by the generator, not a
        # second copy that agrees today. A re-rolled copy in the generator fails here.
        assert gen_guidance.body is render.body
        assert gen_guidance.section_text is render.section_text
        assert gen_guidance.map_text is render.map_text
        assert gen_guidance.recipe_text is render.recipe_text
        assert gen_guidance.SECTION_IDS is loader.SECTION_IDS
