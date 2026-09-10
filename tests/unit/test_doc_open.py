"""Unit tests for ``doc_open.py`` identifier parsing.

``_b64url_decode`` and ``_urn_candidates`` turn whatever the user pastes — a
bare URN, a URN with a ``?version=`` suffix, or a full Fusion web URL with the
lineage URN base64url-encoded into a path segment — into the list of URN
candidates to try. This is pure string/base64 work and a rich bug surface
(padding restoration, the - / _ alphabet, picking the urn:adsk segment out of a
URL), so it gets thorough coverage. No live Fusion needed.

The encoded URN below is real: base64url(urn:adsk.wipprod:dm.lineage:abc123XYZ).
"""

from conftest import (FakeApplication, FakeDataFile, FakeDocuments, FakeFusionDocument,
                      load_tool)

od = load_tool("doc_open")
w = load_tool('_write_guard')

class _EqualDocument(FakeFusionDocument):
    def __init__(self, handle, **kwargs):
        super().__init__(**kwargs)
        self._handle = handle

    def __eq__(self, other):
        return isinstance(other, _EqualDocument) and self._handle == other._handle


class _RaisingEqualityDocument(FakeFusionDocument):
    def __eq__(self, other):
        raise RuntimeError("active document comparison unavailable")

URN = "urn:adsk.wipprod:dm.lineage:abc123XYZ"
URN_B64URL = "dXJuOmFkc2sud2lwcHJvZDpkbS5saW5lYWdlOmFiYzEyM1hZWg"
WEB_URL = f"https://myhub.autodesk360.com/g/projects/proj/data/{URN_B64URL}/"


# ── _b64url_decode ─────────────────────────────────────────────────────────

class TestB64UrlDecode:
    def test_decodes_real_urn_segment(self):
        assert od._b64url_decode(URN_B64URL) == URN

    def test_restores_missing_padding(self):
        # The segment above has no '=' padding; decode must still succeed.
        assert od._b64url_decode(URN_B64URL).startswith("urn:adsk")

    def test_invalid_base64_returns_none(self):
        assert od._b64url_decode("!!!not base64!!!") is None


# ── _urn_candidates ────────────────────────────────────────────────────────

class TestUrnCandidates:
    def test_bare_urn_is_first_candidate(self):
        out = od._urn_candidates(URN)
        assert out[0] == URN

    def test_extracts_urn_from_web_url(self):
        out = od._urn_candidates(WEB_URL)
        assert URN in out

    def test_urn_with_version_suffix_kept_as_is(self):
        raw = f"{URN}?version=3"
        out = od._urn_candidates(raw)
        # The raw value (with suffix) is always tried first...
        assert out[0] == raw
        # ...and the clean inline urn is also surfaced as a candidate.
        assert URN in out

    def test_candidates_are_deduped(self):
        out = od._urn_candidates(URN)
        assert len(out) == len(set(out))

    def test_plain_garbage_yields_only_itself(self):
        out = od._urn_candidates("just-some-text")
        assert out == ["just-some-text"]


# ── CAM-template open guard + declare-intent default (refuse the silent crash) ──────────────────
#
# A freshly-copied multi-reference CAM doc cannot be opened OR even reference-inspected via the API
# without crashing Fusion. CAM-ness CANNOT be auto-detected (inspecting the file IS the crash), so
# the safe-vs-unsafe call MUST come from the caller: doc_open REFUSES any open that hasn't DECLARED
# INTENT - either is_cam_template=true (UI open) or force_api_open=true (explicit API open). A bare
# call resolves/opens NOTHING. These pin that contract.

import json


class TestCamTemplateGuard:
    def test_refuses_api_open_and_does_not_resolve_or_open(self, monkeypatch):
        # The guard must short-circuit BEFORE _resolve_data_file or _open_document — touching the
        # file is itself the crash. So both must remain uncalled.
        touched = {"resolve": False, "open": False}
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: touched.__setitem__("resolve", True) or (object(), raw, [raw]))
        monkeypatch.setattr(od, "_open_document",
                            lambda d: touched.__setitem__("open", True) or (object(), "x", None))
        res = od.handler(file_id="urn:cam", is_cam_template=True)
        payload = json.loads(res["content"][0]["text"])
        assert res["isError"] is False
        assert payload["refused_api_open"] is True
        assert payload["opened"] is False
        assert payload["file_id"] == "urn:cam"
        assert "UI" in payload["note"] or "Data Panel" in payload["note"]
        assert touched["resolve"] is False        # did NOT resolve the DataFile (would crash)
        assert touched["open"] is False           # did NOT attempt the open

    def test_wrapped_cam_refusal_does_not_stamp_active_document(self, monkeypatch):
        active = FakeFusionDocument(name="Active")
        fake_app = FakeApplication(active_document=active)
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        out = json.loads(w.wrap(od.handler)(file_id="urn:cam", is_cam_template=True)
                         ["content"][0]["text"])
        assert out["opened"] is False and out["acted_on"] is None
    def test_normal_open_requires_force_api_open(self, monkeypatch):
        # A non-CAM doc opens normally — but ONLY when the caller declares force_api_open=true.
        opened = FakeFusionDocument(name="Plain")
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", FakeApplication(active_document=opened))
        res = od.handler(file_id="urn:plain", force_api_open=True)
        payload = json.loads(res["content"][0]["text"])
        assert payload["opened"] is True
        assert payload["document_name"] == "Plain"

    def test_bare_open_refuses_without_declaring_intent(self, monkeypatch):
        # The crash guard: a bare doc_open (no is_cam_template, no force_api_open) must NOT
        # silently take the API path. It refuses and resolves/opens NOTHING.
        touched = {"resolve": False, "open": False}
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: touched.__setitem__("resolve", True) or (object(), raw, [raw]))
        monkeypatch.setattr(od, "_open_document",
                            lambda d: touched.__setitem__("open", True) or (object(), "x", None))
        res = od.handler(file_id="urn:something")
        assert res["isError"] is True
        # the refusal must name BOTH ways to declare intent (error text is in 'message')
        assert "is_cam_template" in res["message"]
        assert "force_api_open" in res["message"]
        assert touched["resolve"] is False        # never touched the file (could be a CAM crash)
        assert touched["open"] is False

    def test_cam_flag_wins_over_force(self, monkeypatch):
        # If a caller sets BOTH, the safe path wins: declaring it a CAM template refuses the API
        # open regardless of force_api_open (you can't force-crash through the CAM guard).
        touched = {"resolve": False}
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: touched.__setitem__("resolve", True) or (object(), raw, [raw]))
        res = od.handler(file_id="urn:cam", is_cam_template=True, force_api_open=True)
        payload = json.loads(res["content"][0]["text"])
        assert payload["refused_api_open"] is True
        assert touched["resolve"] is False


# ── the async open hands confirmation to a read tool, and says so only when it must ──────────────


class TestAsyncLoadHandoff:
    """Opening a cloud document is asynchronous: the call can return before the design has loaded
    and become active, and this handler deliberately does not block Fusion's main thread waiting
    for it. So the payload publishes the state it READ and hands confirmation to a named read
    tool - and that handoff sentence is itself a reading, not a fixture: an open that already
    reads active carries none."""

    def test_the_documents_this_one_REFERENCES_are_counted(self, monkeypatch):
        # MEASURED: an assembly whose documentReferences read 9 had loaded 27 documents, so this
        # is the document's own DIRECT reference count, not what the open walked - the payload key
        # matches workspace_orient's, and doc_get's open_count is the loaded total.
        opened = FakeFusionDocument(name="Plain", references=[object(), object(), object()])
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", FakeApplication(active_document=opened))
        payload = json.loads(od.handler(file_id="urn:plain",
                                        force_api_open=True)["content"][0]["text"])
        assert payload["referenced_documents"] == 3

    def _open(self, monkeypatch, active):
        opened = FakeFusionDocument(name="Plain")
        elsewhere = FakeFusionDocument(name="Other")
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        fake_app = FakeApplication(active_document=opened if active else elsewhere)
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        res = od.handler(file_id="urn:plain", force_api_open=True)
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def test_wrapped_pending_open_does_not_stamp_previous_active_document(self, monkeypatch):
        opened = FakeFusionDocument(name="Plain")
        elsewhere = FakeFusionDocument(name="Other")
        fake_app = FakeApplication(active_document=elsewhere)
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain", file_id="urn:plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        out = json.loads(w.wrap(od.handler)(file_id="urn:plain", force_api_open=True)
                         ["content"][0]["text"])
        assert out["is_active"] is False and out["acted_on"] is None
        assert out["document_handle"].startswith("session:")

    def test_wrapped_active_open_names_the_opened_document(self, monkeypatch):
        opened = FakeFusionDocument(name="Plain", data_file=FakeDataFile("Plain", file_id="urn:plain"))
        fake_app = FakeApplication(active_document=opened)
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain", file_id="urn:plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        out = json.loads(w.wrap(od.handler)(file_id="urn:plain", force_api_open=True)
                         ["content"][0]["text"])
        assert out["is_active"] is True
        assert out["acted_on"]["name"] == "Plain"
        assert out["acted_on"]["document_id"] == "urn:plain"

    def test_unreadable_active_comparison_keeps_activation_unknown(self, monkeypatch):
        opened = FakeFusionDocument(name="Plain", data_file=FakeDataFile("Plain", file_id="urn:plain"))
        fake_app = FakeApplication(active_document=_RaisingEqualityDocument(name="Other"))
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain", file_id="urn:plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        out = json.loads(od.handler(file_id="urn:plain", force_api_open=True)
                         ["content"][0]["text"])
        assert out["is_active"] is None and out["acted_on"] is None
        assert "unreadable" in out["note"] and "doc_get" in out["note"]

    def test_distinct_wrappers_with_native_equality_name_opened_document(self, monkeypatch):
        opened = _EqualDocument("plain", name="Plain", data_file=FakeDataFile("Plain", file_id="urn:plain"))
        active_wrapper = _EqualDocument("plain", name="Plain", data_file=FakeDataFile("Plain", file_id="urn:plain"))
        fake_app = FakeApplication(active_document=active_wrapper)
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Plain", file_id="urn:plain"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", fake_app)
        monkeypatch.setattr(w, "app", fake_app)
        out = json.loads(w.wrap(od.handler)(file_id="urn:plain", force_api_open=True)
                         ["content"][0]["text"])
        assert out["is_active"] is True and out["acted_on"]["document_id"] == "urn:plain"

    def test_a_document_not_yet_active_claims_no_load_and_names_the_poller(self, monkeypatch):
        pending = self._open(monkeypatch, active=False)
        assert pending["opened"] is True             # the call landed...
        assert pending["is_active"] is False         # ...and the read says the load has not
        assert "asynchronous" in pending["note"]
        assert "workspace_orient" in pending["note"]  # where completion IS confirmed
        # the twin: once the document reads active there is nothing to hand off, so a note that
        # shipped either way would be prose rather than the state this call measured.
        landed = self._open(monkeypatch, active=True)
        assert landed["is_active"] is True and landed["note"] is None


class TestTheOpenReportsItsOwnCost:
    """A large assembly's open pulls its whole reference family into the session and can run past
    the server's call timeout - which is why this tool is exempt from it. The payload is what makes
    that wait legible: the seconds the open ran, and how many documents the session GAINED, which
    is a different number from the document's own direct reference count."""

    def _open(self, monkeypatch, family):
        opened = FakeFusionDocument(name="Assembly", references=[object()])
        app = FakeApplication(active_document=opened,
                              documents=FakeDocuments(documents=[FakeFusionDocument(name="Home")]))

        def _load(_data_file):
            app.documents._items.extend(FakeFusionDocument(name=f"Ref{i}") for i in range(family))
            app.documents._items.append(opened)
            return opened, "openUsingContext", None

        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Assembly"), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", _load)
        monkeypatch.setattr(od, "app", app)
        res = od.handler(file_id="urn:asm", force_api_open=True)
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def test_the_documents_the_open_LOADED_are_counted_and_named(self, monkeypatch):
        out = self._open(monkeypatch, family=26)
        # 26 referenced documents plus the assembly itself - the session census DIFFERENCE, which
        # is not documentReferences (1 here, and 9 on the assembly that loaded 27 live).
        assert out["documents_loaded"] == 27 and out["referenced_documents"] == 1
        assert isinstance(out["open_seconds"], float)
        assert "27 documents" in out["note"] and "open_count" in out["note"]

    def test_an_open_that_loaded_only_itself_makes_no_cost_claim(self, monkeypatch):
        # The exact boundary: one document loaded is an ordinary open, and the cost sentence exists
        # to explain a slow one - shipping it either way would make it prose, not a reading.
        out = self._open(monkeypatch, family=0)
        assert out["documents_loaded"] == 1 and out["note"] is None

    def test_the_open_is_exempt_from_the_servers_call_timeout(self):
        # The open COMMITS whether or not the server is still waiting on it, so a timeout could
        # only replace the payload above with a false failure for a document that IS open.
        assert od.item.enforce_timeout is False


class TestConfiguredDesign:
    """A Configured Design opens at ONE of its configurations, and which one is not this call's to
    choose - so the payload publishes the flag it read off the DataFile and, only then, the note
    naming where the configurations are listed and switched. A plain design carries neither."""

    def _open(self, monkeypatch, **data_file):
        opened = FakeFusionDocument(name="Bracket")
        monkeypatch.setattr(od, "_resolve_data_file",
                            lambda raw: (FakeDataFile("Bracket", **data_file), raw, [raw]))
        monkeypatch.setattr(od, "_open_document", lambda d: (opened, "openUsingContext", None))
        monkeypatch.setattr(od, "app", FakeApplication(active_document=opened))
        res = od.handler(file_id="urn:bracket", force_api_open=True)
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def test_a_configured_design_publishes_the_configuration_note(self, monkeypatch):
        out = self._open(monkeypatch, is_configured_design=True)
        assert out["is_configured_design"] is True
        note = out["configured_design_note"]
        assert "design_get" in note and "include=['configurations']" in note
        assert "design_configure" in note and "action='activate'" in note
        assert "configurationTopTable" not in note and "ConfigurationRow" not in note
        # the twin: the note rides on the flag the file answered, so a plain design gets no claim
        # about configurations it does not have.
        plain = self._open(monkeypatch, is_configured_design=False)
        assert plain["is_configured_design"] is False
        assert "configured_design_note" not in plain
