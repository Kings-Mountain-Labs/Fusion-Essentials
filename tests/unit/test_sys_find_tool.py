"""Unit tests for ``sys_find_tool`` — search the server's own tools + input-kinds by keyword.

No adsk.* (pure introspection). We patch the registry's get_tools to a known set of fake tools and
assert: matching across name/description/inputs, name matches outranking description matches, the
input-kind search against the real _inputs module, and the guards (empty query).
"""

import json

from conftest import load_tool

ft = load_tool("sys_find_tool")

# the handler filters registry items by isinstance(prim, Tool) — so build REAL Tool primitives
_Tool = ft.Tool


def _tool(name, description="", inputs=()):
    t = _Tool.create_simple(name=name, description=description)
    for k in inputs:
        t.add_input_property(k, {"type": "string"})
    return t


def _item(prim):
    return type("Item", (), {"primitive": prim})()


def _install(tools):
    # the handler imports get_tools at module load; patch the name it bound
    ft.get_tools = lambda: [_item(t) for t in tools]


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_empty_query_errors(self):
        _install([])
        res = ft.handler(query="")
        assert res["isError"] is True and "query" in res["message"].lower()


# ── tool search ────────────────────────────────────────────────────────────

class TestToolSearch:
    def test_matches_name(self):
        _install([_tool("cam_select_geometry", "select machining geometry"),
                  _tool("model_extrude", "extrude a profile")])
        out = _payload(ft.handler(query="extrude", include_kinds=False))
        names = [t["tool"] for t in out["tools"]]
        assert "model_extrude" in names and "cam_select_geometry" not in names

    def test_matches_description_and_inputs(self):
        _install([_tool("model_revolve", "spin a sketch profile about an axis",
                        inputs=["sketch_name", "profile_index", "axis"])])
        out = _payload(ft.handler(query="profile", include_kinds=False))
        assert out["tool_count"] == 1 and out["tools"][0]["tool"] == "model_revolve"

    def test_name_match_outranks_description_only(self):
        # put the description-only tool FIRST so a tie would let IT win — only the 2x name weight
        # promotes the name-match above it. (Guards against the weight being dropped to 1x.)
        _install([_tool("doc_save", "export the active document to a file somewhere"),  # 'export' desc only
                  _tool("design_export", "write the design out")])                      # 'export' in name
        out = _payload(ft.handler(query="export", include_kinds=False))
        assert out["tools"][0]["tool"] == "design_export"   # name hit (score 2) beats desc hit (score 1)

    def test_summary_is_first_sentence(self):
        _install([_tool("t", "First sentence. Second sentence with more detail.")])
        out = _payload(ft.handler(query="first", include_kinds=False))
        assert out["tools"][0]["summary"].startswith("First sentence")
        assert "Second sentence" not in out["tools"][0]["summary"]

    def test_no_match_reports(self):
        _install([_tool("model_extrude", "extrude a profile")])
        out = _payload(ft.handler(query="zzzznotathing", include_kinds=False))
        assert out["tool_count"] == 0 and "note" in out


# ── input-kind search (the anti-blindness half, against the REAL _inputs) ───

class TestKindSearch:
    def test_profile_query_surfaces_ProfileRef(self):
        _install([])
        out = _payload(ft.handler(query="profile"))
        kind_names = [k["kind"] for k in out["kinds"]]
        assert "ProfileRef" in kind_names

    def test_body_query_surfaces_BodyRef(self):
        _install([])
        out = _payload(ft.handler(query="body reference"))
        kind_names = [k["kind"] for k in out["kinds"]]
        assert "BodyRef" in kind_names

    def test_include_kinds_false_omits_them(self):
        _install([])
        out = _payload(ft.handler(query="profile", include_kinds=False))
        assert "kinds" not in out

    def test_kinds_note_points_to_convention(self):
        _install([])
        out = _payload(ft.handler(query="profile"))
        assert out.get("kinds") and "hand-roll" in out["note"].lower()
