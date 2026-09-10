# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Unit tests for the harness generators (gen_manifest / gen_wiring / gen_posture /
gen_api_surface) and check_all's flag contract.
test_generated_docs_current.py pins output FRESHNESS; these pin the generators' LOGIC - a
generator that lies consistently sails through a freshness check."""

import ast
import os
import types

import pytest

import check_all
import gen_all
import gen_api_surface
import gen_manifest
import gen_posture
import gen_wiring


# ── gen_wiring: registration call-site attribution ───────────────────────────

class TestToolHandlerMap:
    def _map(self, src):
        return gen_wiring._tool_handler_map(ast.parse(src))

    def test_sibling_tools_sharing_a_stem_keep_their_own_handlers(self):
        src = (
            'tool_a = Tool.create_simple(name="doc_save", description=D)\n'
            'item_a = Item.create_tool_item(tool=tool_a, write="write", handler=save_handler)\n'
            'tool_b = Tool.create_simple(name="doc_save_as", description=D2)\n'
            'item_b = Item.create_tool_item(tool=tool_b, write="write", handler=save_as_handler)\n'
        )
        m = self._map(src)
        assert m == {"doc_save": "save_handler", "doc_save_as": "save_as_handler"}

    def test_chained_builder_calls_still_resolve(self):
        src = (
            'tool = (Tool.create_simple(name="view_screenshot", description=D)\n'
            '        .add_input_property("width", {})\n'
            '        .strict_schema())\n'
            'item = Item.create_tool_item(tool=tool, write="read", handler=handler)\n'
        )
        assert self._map(src) == {"view_screenshot": "handler"}

    def test_create_with_string_input_form_resolves(self):
        src = (
            'tool = Tool.create_with_string_input(name="param_set", description=D,\n'
            '                                     input_param_name="name")\n'
            'item = Item.create_tool_item(tool=tool, write="write", handler=set_handler)\n'
        )
        assert self._map(src) == {"param_set": "set_handler"}

    def test_registration_without_a_name_or_handler_is_absent(self):
        src = (
            'tool = Tool.create_simple(description=D)\n'
            'item = Item.create_tool_item(tool=tool, write="read", handler=handler)\n'
            'other = Item.create_tool_item(tool=unknown_var, write="read", handler=h2)\n'
        )
        assert self._map(src) == {}


# ── gen_manifest: family grouping + CLAUDE.md splice ──────────────────────────

class TestGenManifestFamilies:
    def test_first_matching_prefix_wins_and_leftovers_group_as_other(self):
        tools = [{"name": "model_extrude"}, {"name": "cam_get"}, {"name": "zzz_thing"}]
        fam = gen_manifest.families(tools)
        assert any(t["name"] == "model_extrude" for t in fam.get("model", []))
        assert any(t["name"] == "cam_get" for t in fam.get("cam", []))
        assert any(t["name"] == "zzz_thing" for t in fam.get("other", []))

    def test_catalog_escapes_pipes_in_kind_hints(self):
        data = {"kinds": [{"kind": "UnitField", "hint": "mm | cm | in selector", "summary": ""}],
                "tools": [{"name": "model_extrude"}], "helpers": []}
        out = gen_manifest.render_catalog(data)
        assert "mm \\| cm \\| in selector" in out          # an unescaped pipe would break the table


class TestSplice:
    # _splice is generic (path + begin/end markers); the same seam splices the root families census
    # and the tools/CLAUDE.md catalog. Exercised here on a temp file with the catalog markers.
    def _doc(self, tmp_path, body, newline="\n"):
        # LF by default, as a generated file is: Path.write_text translates to the PLATFORM newline,
        # which would hand every test below a CRLF fixture no generator ever produces.
        p = tmp_path / "DOC.md"
        with open(p, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(body)
        return str(p)

    def test_splice_replaces_only_between_markers(self, tmp_path):
        b, e = gen_manifest._CAT_BEGIN, gen_manifest._CAT_END
        path = self._doc(tmp_path, "before\n" + b + "\nstale\n" + e + "\nafter\n")
        block = b + "\nfresh\n" + e
        assert gen_manifest._splice(path, b, e, block) is False       # it changed something
        text = open(path, encoding="utf-8").read()
        assert "fresh" in text and "stale" not in text
        assert text.startswith("before\n") and text.endswith("after\n")

    def test_check_mode_reports_stale_without_writing(self, tmp_path):
        b, e = gen_manifest._CAT_BEGIN, gen_manifest._CAT_END
        path = self._doc(tmp_path, b + "\nstale\n" + e)
        block = b + "\nfresh\n" + e
        assert gen_manifest._splice(path, b, e, block, check=True) is False
        assert "stale" in open(path, encoding="utf-8").read()          # untouched

    def test_current_content_reports_true(self, tmp_path):
        b, e = gen_manifest._CAT_BEGIN, gen_manifest._CAT_END
        block = b + "\ncurrent\n" + e
        path = self._doc(tmp_path, block)
        assert gen_manifest._splice(path, b, e, block, check=True) is True

    def test_a_current_block_in_a_crlf_file_is_still_rewritten(self, tmp_path):
        # the already-current test is what makes this invisible: read through universal newlines a
        # CRLF file matches its own block, returns early, and keeps its CRs through every run.
        b, e = gen_manifest._CAT_BEGIN, gen_manifest._CAT_END
        block = b + "\ncurrent\n" + e
        path = self._doc(tmp_path, "head\n" + block + "\ntail\n", newline="\r\n")
        assert gen_manifest._splice(path, b, e, block) is False
        assert b"\r" not in open(path, "rb").read()

    def test_missing_markers_raise_systemexit(self, tmp_path):
        path = self._doc(tmp_path, "no markers here\n")
        with pytest.raises(SystemExit):
            gen_manifest._splice(path, gen_manifest._CAT_BEGIN, gen_manifest._CAT_END, "block")


# ── check_all: --live and --offline are refused together ─────────────────────

class TestCheckAllLiveGateFlags:
    """--live runs the live gate and --offline skips it. Accepting both would discard one silently,
    and the script's whole claim is that skipping live verification is a VISIBLE choice."""

    def test_live_and_offline_together_are_refused_naming_both(self, capsys):
        with pytest.raises(SystemExit) as exc:
            check_all.build_parser().parse_args(["--live", "--offline"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "--live" in err and "--offline" in err, err

    def test_each_gate_flag_alone_parses(self):
        assert check_all.build_parser().parse_args(["--live"]).live is True
        assert check_all.build_parser().parse_args(["--offline"]).offline is True

    def test_gate_flags_combine_with_the_scope_flags(self):
        # only the two GATE flags conflict - --fast/--gen still pair with either.
        args = check_all.build_parser().parse_args(["--offline", "--fast"])
        assert (args.offline, args.fast, args.live) == (True, True, False)


# ── gen_all: a generator's RETURN value is a verdict too ─────────────────────

class TestGenAllExitCode:
    """gen_all fronts every generator in one process, and its --check is what
    test_generated_docs_current shells. Reading only SystemExit and discarding main()'s RETURN
    value means a generator that reports failure by returning a code can never fail the aggregate:
    the stale doc it found is aggregated as a pass."""

    def test_a_returned_failure_code_is_a_failure(self):
        assert gen_all.exit_code(1) == 1
        assert gen_all.exit_code(2) == 2

    def test_zero_and_none_are_both_success(self):
        # None is the shape every generator here uses today (it exits by SystemExit, or returns
        # nothing); it must not become a failure.
        assert gen_all.exit_code(0) == 0
        assert gen_all.exit_code(None) == 0

    def test_false_is_a_failure_and_true_is_not(self):
        # bool IS an int subclass, so an unguarded int check reads False as exit code 0 - and False
        # is exactly how a freshness question answers "stale".
        assert gen_all.exit_code(False) == 1
        assert gen_all.exit_code(True) == 0

    def test_a_non_numeric_value_follows_the_systemexit_convention(self):
        assert gen_all.exit_code("tests/api_surface.py is STALE") == 1
        assert gen_all.exit_code("") == 0

    def test_a_generator_that_RETURNS_one_fails_the_aggregate_naming_it(
            self, monkeypatch, tmp_path, capsys):
        # the defect itself: a module whose main() returns 1 and raises nothing.
        (tmp_path / "gen_returns_one.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (tmp_path / "gen_returns_none.py").write_text("def main():\n    return None\n",
                                                      encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(gen_all, "_GENERATORS", ("gen_returns_none", "gen_returns_one"))
        monkeypatch.setattr(gen_all.sys, "argv", ["gen_all.py", "--check"])
        assert gen_all.main() == 1
        err = capsys.readouterr().err
        assert "gen_returns_one" in err and "gen_returns_none" not in err

    def test_every_generator_returning_nothing_still_passes(self, monkeypatch, tmp_path):
        # the boundary beside it: today's shape, where a clean run returns None.
        (tmp_path / "gen_clean_a.py").write_text("def main():\n    return None\n", encoding="utf-8")
        (tmp_path / "gen_clean_b.py").write_text("def main():\n    pass\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(gen_all, "_GENERATORS", ("gen_clean_a", "gen_clean_b"))
        monkeypatch.setattr(gen_all.sys, "argv", ["gen_all.py", "--check"])
        assert gen_all.main() == 0

    def test_a_raised_systemexit_still_fails_the_aggregate(self, monkeypatch, tmp_path, capsys):
        # the path that already worked keeps working, and is now named in the summary too.
        (tmp_path / "gen_raises.py").write_text("def main():\n    raise SystemExit(1)\n",
                                                encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(gen_all, "_GENERATORS", ("gen_raises",))
        monkeypatch.setattr(gen_all.sys, "argv", ["gen_all.py", "--check"])
        assert gen_all.main() == 1
        assert "gen_raises" in capsys.readouterr().err

    def test_a_bare_sys_exit_is_not_a_failure(self, monkeypatch, tmp_path):
        # SystemExit(None) is `sys.exit()` - a clean stop, which must not read as exit code 1.
        (tmp_path / "gen_bare_exit.py").write_text("def main():\n    raise SystemExit()\n",
                                                   encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(gen_all, "_GENERATORS", ("gen_bare_exit",))
        monkeypatch.setattr(gen_all.sys, "argv", ["gen_all.py", "--check"])
        assert gen_all.main() == 0


# ── gen_api_surface: --check cannot pass without the bindings it compares against ──

class TestApiSurfaceCheckNeedsBindings:
    """The surface table is the only thing that catches a misspelled input property. Without the
    installed bindings the table cannot be recomputed, so --check has established nothing - a
    machine with no Fusion must fail, not green over an arbitrarily stale committed file."""

    def test_check_fails_when_the_bindings_are_absent(self, monkeypatch, capsys):
        monkeypatch.setattr(gen_api_surface, "find_bindings", lambda: None)
        assert os.path.isfile(gen_api_surface.OUT_PATH), "the committed table must exist for this "\
            "test to prove its presence is NOT what makes --check pass"
        with pytest.raises(SystemExit) as exc:
            gen_api_surface.main(["--check"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "webdeploy" in err, f"the failure must name the bindings path it needed: {err}"
        assert "api_surface.py" in err

    def test_generate_without_bindings_also_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(gen_api_surface, "find_bindings", lambda: None)
        with pytest.raises(SystemExit) as exc:
            gen_api_surface.main([])
        assert exc.value.code == 1
        assert "webdeploy" in capsys.readouterr().err


    def test_explicit_bindings_dir_wins_and_requires_all_api_modules(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit" / "adsk"
        explicit.mkdir(parents=True)
        for name in gen_api_surface._MODULES:
            (explicit / (name + ".py")).write_text("", encoding="utf-8")
        monkeypatch.setattr(gen_api_surface, "_BINDING_GLOBS", (str(tmp_path / "other"),))
        assert gen_api_surface.find_bindings(str(explicit)) == os.path.abspath(str(explicit))

    @pytest.mark.parametrize("shape", ["missing", "incomplete"])
    def test_explicit_bindings_dir_invalid_fails_visibly(self, tmp_path, shape):
        explicit = tmp_path / "adsk"
        if shape == "incomplete":
            explicit.mkdir()
            (explicit / "core.py").write_text("", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            gen_api_surface.find_bindings(str(explicit))
        assert "Explicit --bindings-dir is invalid" in str(exc.value)

    @pytest.mark.parametrize("pattern", gen_api_surface._BINDING_GLOBS,
                             ids=["windows-production", "windows-preview", "macos-production"])
    def test_build_label_uses_the_hash_for_each_declared_binding_layout(self, pattern):
        path = pattern.replace("*", "probe-build-hash")
        assert gen_api_surface._build_id(path) == "probe-build-hash"
        assert 'BINDINGS_BUILD = "probe-build-hash"' in gen_api_surface._render(
            path, {}, {}, set())

    def test_main_forwards_explicit_bindings_dir(self, tmp_path, monkeypatch):
        seen = []
        expected = str(tmp_path / "adsk")
        monkeypatch.setattr(
            gen_api_surface, "build",
            lambda path=None: (seen.append(path) or ("root", {}, {}, set())))
        monkeypatch.setattr(gen_api_surface, "_render", lambda *args: "")
        out = tmp_path / "surface.py"
        out.write_text("", encoding="utf-8")
        monkeypatch.setattr(gen_api_surface, "OUT_PATH", str(out))
        assert gen_api_surface.main(["--check", "--bindings-dir", expected]) == 0
        assert seen == [expected]

# ── gen_api_surface: a measured runtime return beats a broken binding annotation ──

class TestMeasuredFactoryReturns:
    """A binding's return annotation can name the wrong class (measured live: Arc2D.createByCenter
    is annotated adsk.core.Point2D - its CENTER argument - while the call returns an Arc2D). The
    override substitutes the measured class at generation, and refuses to outlive the defect."""

    def test_a_measured_return_replaces_the_broken_annotation(self):
        got = gen_api_surface.apply_measured_returns(
            {"core.Arc2D.createByCenter": "core.Point2D"},
            {"core.Arc2D.createByCenter": ("core.Arc2D", "Fusion 2705.0.108")})
        assert got == {"core.Arc2D.createByCenter": "core.Arc2D"}

    def test_annotations_with_no_override_row_are_left_alone(self):
        got = gen_api_surface.apply_measured_returns(
            {"core.Arc2D.createByCenter": "core.Point2D", "core.Color.create": "core.Color"},
            {"core.Arc2D.createByCenter": ("core.Arc2D", "Fusion 2705.0.108")})
        assert got["core.Color.create"] == "core.Color"

    def test_the_input_mapping_is_not_mutated(self):
        # the generator keeps using `factories` downstream; an in-place edit would be invisible here
        # and load-bearing there
        src = {"core.Arc2D.createByCenter": "core.Point2D"}
        gen_api_surface.apply_measured_returns(
            src, {"core.Arc2D.createByCenter": ("core.Arc2D", "Fusion 2705.0.108")})
        assert src == {"core.Arc2D.createByCenter": "core.Point2D"}

    def test_a_row_the_bindings_dropped_is_refused(self):
        with pytest.raises(SystemExit) as exc:
            gen_api_surface.apply_measured_returns(
                {"core.Color.create": "core.Color"},
                {"core.Gone.createByCenter": ("core.Gone", "Fusion 2705.0.108")})
        assert "core.Gone.createByCenter" in str(exc.value)

    def test_a_row_the_bindings_have_since_fixed_is_refused(self):
        # the override must not silently become a no-op that hides a repaired binding
        with pytest.raises(SystemExit) as exc:
            gen_api_surface.apply_measured_returns(
                {"core.Arc2D.createByCenter": "core.Arc2D"},
                {"core.Arc2D.createByCenter": ("core.Arc2D", "Fusion 2705.0.108")})
        assert "obsolete" in str(exc.value)

    def test_the_shipped_table_names_only_real_create_factories(self):
        # every shipped row must key a factory the committed surface actually carries
        import api_surface
        for key in gen_api_surface._MEASURED_FACTORY_RETURNS:
            assert key.rsplit(".", 1)[1].startswith("create"), key
            assert key in api_surface.FACTORIES, f"{key} is not in the generated FACTORIES table"


# ── name collisions ACROSS modules (each module looks clean in isolation) ─────

class _FakeItem:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name

    def to_dict(self):
        return {"description": "A fake tool. Second sentence.",
                "annotations": {"readOnlyHint": True},
                "inputSchema": {"properties": {"target": {}}}}


class _FakeRegistry:
    """The seam both walks drive: reset per module, then read back what register_tool() added."""

    def __init__(self):
        self._tools = []

    def reset_registry(self):
        self._tools = []

    def get_tools(self):
        return list(self._tools)

    def add(self, name):
        self._tools.append(_FakeItem(name))


def _fake_modules(reg, spec):
    """{module name -> a stub whose register_tool() registers its tool names into `reg`}."""
    mods = {}
    for mod_name, names in spec.items():
        def _reg(names=names):
            for n in names:
                reg.add(n)
        mods[mod_name] = types.SimpleNamespace(register_tool=_reg)
    return mods


class TestManifestNameCollision:
    def _drive(self, monkeypatch, spec):
        reg = _FakeRegistry()
        mods = _fake_modules(reg, spec)
        monkeypatch.setattr(gen_manifest, "_tool_modules", lambda: sorted(mods))
        monkeypatch.setattr(gen_manifest, "load_tool", lambda n: mods[n])
        monkeypatch.setattr(gen_manifest, "_collect_kinds", lambda: [])
        monkeypatch.setattr(gen_manifest, "_collect_helpers", lambda: [])
        return gen_manifest._collect_unguarded(reg)

    def test_two_modules_registering_one_name_fail_naming_both_files(self, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            self._drive(monkeypatch, {"mod_a": ["doc_save"], "mod_b": ["doc_save"]})
        msg = str(exc.value)
        assert "doc_save" in msg
        assert "tools/mod_a.py" in msg and "tools/mod_b.py" in msg, msg

    def test_distinct_names_across_modules_collect_normally(self, monkeypatch):
        data = self._drive(monkeypatch, {"mod_a": ["doc_save"], "mod_b": ["doc_open"]})
        assert [t["name"] for t in data["tools"]] == ["doc_open", "doc_save"]

    def test_one_module_registering_two_tools_is_not_a_collision(self, monkeypatch):
        data = self._drive(monkeypatch, {"mod_a": ["mesh_export", "save_as_mesh"]})
        assert [t["name"] for t in data["tools"]] == ["mesh_export", "save_as_mesh"]


class TestClaimName:
    def test_reclaiming_by_the_same_module_is_allowed(self):
        owner = {}
        gen_manifest.claim_name(owner, "cam_get", "cam_get")
        gen_manifest.claim_name(owner, "cam_get", "cam_get")
        assert owner == {"cam_get": "cam_get"}

    def test_a_second_module_raises(self):
        owner = {"cam_get": "cam_get"}
        with pytest.raises(SystemExit):
            gen_manifest.claim_name(owner, "cam_get", "cam_read")


# ── gen_wiring: collision + the one-hop note harvest ──────────────────────────

_COLLIDING_MODULE = '''"""Fake tool module."""


def handler(**kwargs):
    return {"note": "a note longer than the twenty-character floor."}


tool = Tool.create_simple(name="doc_save", description=D)
item = Item.create_tool_item(tool=tool, write="write", handler=handler)
'''


class TestWiringNameCollision:
    def test_a_second_module_registering_the_name_fails_instead_of_overwriting(
            self, monkeypatch, tmp_path):
        for mod_name in ("mod_a", "mod_b"):
            (tmp_path / f"{mod_name}.py").write_text(_COLLIDING_MODULE, encoding="utf-8")
        reg = _FakeRegistry()
        mods = _fake_modules(reg, {"mod_a": ["doc_save"], "mod_b": ["doc_save"]})
        monkeypatch.setattr(gen_wiring, "TOOLS_DIR", str(tmp_path))
        monkeypatch.setattr(gen_wiring, "_tool_modules", lambda: sorted(mods))
        monkeypatch.setattr(gen_wiring, "load_tool", lambda n: mods[n])
        with pytest.raises(SystemExit) as exc:
            gen_wiring.collect(registry=reg)
        msg = str(exc.value)
        assert "doc_save" in msg
        assert "tools/mod_a.py" in msg and "tools/mod_b.py" in msg, msg

    def test_distinct_names_record_both_tools(self, monkeypatch, tmp_path):
        for mod_name in ("mod_a", "mod_b"):
            (tmp_path / f"{mod_name}.py").write_text(_COLLIDING_MODULE, encoding="utf-8")
        reg = _FakeRegistry()
        mods = _fake_modules(reg, {"mod_a": ["doc_save"], "mod_b": ["doc_open"]})
        monkeypatch.setattr(gen_wiring, "TOOLS_DIR", str(tmp_path))
        monkeypatch.setattr(gen_wiring, "_tool_modules", lambda: sorted(mods))
        monkeypatch.setattr(gen_wiring, "load_tool", lambda n: mods[n])
        data = gen_wiring.collect(registry=reg)
        assert sorted(data["records"]) == ["doc_open", "doc_save"]
        assert [item.get_name() for item in reg.get_tools()] == ["doc_open"]


@pytest.fixture
def populated_wiring_registry(monkeypatch):
    from mcpServer.mcp_primitives import registry
    from mcpServer.mcp_primitives.item import Item
    from mcpServer.mcp_primitives.tool import Tool

    seeded = registry.Registry()
    sentinel = Item.create_tool_item(
        tool=Tool.create_simple(name="sentinel_get", description="Sentinel tool."),
        write="read", handler=lambda: {})
    seeded.register(sentinel)
    monkeypatch.setattr(registry, "_registry_instance", seeded)
    return registry, seeded, sentinel


class TestWiringRegistryIsolation:
    @pytest.mark.parametrize("failure", [None, "registration", "attribution"])
    def test_implicit_collect_restores_the_populated_real_singleton(
            self, monkeypatch, populated_wiring_registry, failure):
        registry, seeded, sentinel = populated_wiring_registry

        def register():
            registry.get_registry()
            if failure == "registration":
                raise RuntimeError("registration failed")

        monkeypatch.setattr(gen_wiring, "_tool_modules", lambda: ["probe_mod"])
        monkeypatch.setattr(
            gen_wiring, "load_tool",
            lambda _name: types.SimpleNamespace(register_tool=register))
        if failure == "attribution":
            monkeypatch.setattr(registry, "get_tools", lambda: [_FakeItem("probe_get")])
            monkeypatch.setattr(
                gen_wiring, "_attribute",
                lambda *_args: (_ for _ in ()).throw(RuntimeError("attribution failed")))
        if failure:
            with pytest.raises(RuntimeError, match=failure):
                gen_wiring.collect()
        else:
            assert gen_wiring.collect()["records"] == {}
        assert registry._registry_instance is seeded
        assert seeded.get_tools() == [sentinel]

_HOP_MODULE = '''"""Fake rich-read module: the router holds no guidance, the slices hold it all."""


def _slice_geometry(out):
    return {"note": "geometry slice note - call find_geometry for a handle."}


def _deep_helper():
    return {"note": "two hops from the handler and NOT this tool's guidance."}


def _slice_units(out):
    _deep_helper()
    return {"note": "units slice note - lengths are centimetres internally."}


def _unreached_slice(out):
    return {"note": "no call reaches this slice from the handler."}


def handler(**kwargs):
    out = {"note": "router note - pass include= for more."}
    _slice_geometry(out)
    _slice_units(out)
    return out


tool = Tool.create_simple(name="demo_get", description=D)
item = Item.create_tool_item(tool=tool, write="read", handler=handler)
'''

_RECURSIVE_MODULE = '''"""Fake module whose handler calls itself."""


def handler(depth=1, **kwargs):
    if depth:
        handler(depth - 1)
    return {"note": "recursive handler note, long enough to count."}


tool = Tool.create_simple(name="demo_probe", description=D)
item = Item.create_tool_item(tool=tool, write="read", handler=handler)
'''


class TestNoteHarvestFollowsOneCallHop:
    """A rich read's handler is a router: its notes are built in the _slice_* helpers it calls, so
    harvesting only the handler body reports the tool as having no guidance surface at all."""

    def _notes(self, monkeypatch, tmp_path, source, mod_name, tool_name):
        (tmp_path / f"{mod_name}.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(gen_wiring, "TOOLS_DIR", str(tmp_path))
        notes, _ = gen_wiring._attribute(mod_name, tool_name)
        return notes

    def test_helper_notes_one_hop_from_the_handler_are_harvested(self, monkeypatch, tmp_path):
        notes = self._notes(monkeypatch, tmp_path, _HOP_MODULE, "demo_get_mod", "demo_get")
        assert "router note - pass include= for more." in notes
        assert "geometry slice note - call find_geometry for a handle." in notes
        assert "units slice note - lengths are centimetres internally." in notes

    def test_the_hop_stops_at_one_level(self, monkeypatch, tmp_path):
        # the exact depth boundary: _slice_units is followed (hop 1), the _deep_helper it calls is
        # not (hop 2) - otherwise a shared low-level utility's strings land on every tool above it.
        notes = self._notes(monkeypatch, tmp_path, _HOP_MODULE, "demo_get_mod", "demo_get")
        assert "two hops from the handler and NOT this tool's guidance." not in notes

    def test_a_helper_the_handler_never_calls_is_not_attributed(self, monkeypatch, tmp_path):
        notes = self._notes(monkeypatch, tmp_path, _HOP_MODULE, "demo_get_mod", "demo_get")
        assert "no call reaches this slice from the handler." not in notes

    def test_a_recursive_handler_yields_its_note_once(self, monkeypatch, tmp_path):
        notes = self._notes(monkeypatch, tmp_path, _RECURSIVE_MODULE, "demo_probe_mod", "demo_probe")
        assert notes.count("recursive handler note, long enough to count.") == 1


_TWO_TOOL_MODULE = '''"""Fake module registering two tools out of one file."""


def get_handler(**kwargs):
    return {"note": "the read note, long enough to be harvested."}


def edit_handler(**kwargs):
    return {"note": "the edit note, long enough to be harvested."}


tool = Tool.create_simple(name="demo_get", description=D)
item = Item.create_tool_item(tool=tool, write="read", handler=get_handler)
edit_tool = Tool.create_simple(name="demo_edit", description=D)
edit_item = Item.create_tool_item(tool=edit_tool, write="write", handler=edit_handler)
'''


class TestWiringParsesEachModuleOnce:
    """Attribution runs once per registered TOOL and the guard census walks every module again, so
    an unmemoized parse re-reads one file several times over - the cost that made gen_wiring the
    slowest part of the generator check."""

    def _count_parses(self, monkeypatch, work):
        calls = []
        real = ast.parse
        monkeypatch.setattr(gen_wiring.ast, "parse",
                            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        work()
        return len(calls)

    def test_two_tools_in_one_module_parse_the_file_once(self, monkeypatch, tmp_path):
        (tmp_path / "twin_mod.py").write_text(_TWO_TOOL_MODULE, encoding="utf-8")
        monkeypatch.setattr(gen_wiring, "TOOLS_DIR", str(tmp_path))
        gen_wiring._parsed.clear()

        def work():
            gen_wiring._attribute("twin_mod", "demo_get")
            gen_wiring._attribute("twin_mod", "demo_edit")
            gen_wiring._module_functions("twin_mod")     # the guard census's own walk

        assert self._count_parses(monkeypatch, work) == 1

    def test_a_rewritten_module_is_parsed_again(self, monkeypatch, tmp_path):
        # The memo is keyed on the file's own stat, not its module NAME: several tests here write a
        # DIFFERENT module under the same name into their own tmp dir, and a name-keyed memo would
        # attribute one test's notes to the next.
        path = tmp_path / "twin_mod.py"
        path.write_text(_TWO_TOOL_MODULE, encoding="utf-8")
        monkeypatch.setattr(gen_wiring, "TOOLS_DIR", str(tmp_path))
        gen_wiring._parsed.clear()
        first, _ = gen_wiring._attribute("twin_mod", "demo_get")
        assert "the read note, long enough to be harvested." in first
        path.write_text(_TWO_TOOL_MODULE.replace("the read note", "the REWRITTEN note"),
                        encoding="utf-8")
        again, _ = gen_wiring._attribute("twin_mod", "demo_get")
        assert "the REWRITTEN note, long enough to be harvested." in again


# ── gen_posture: a write whose EFFECT leaves the document is not a local model write ──

class TestPostureLeavesDocument:
    """The modeling posture promises auto-allow for LOCAL model writes - work an operator sees in
    the timeline and can undo. A write that puts bytes on disk, into a shared library, or restarts
    the add-in is not that, whatever family it sits in."""

    def _presets(self):
        return gen_posture.build_presets(gen_manifest.collect()["tools"])

    def _bare(self, wire_names):
        p = gen_posture.WIRE_PREFIX
        return {w[len(p):] for w in wire_names if w.startswith(p)}

    def test_every_leaves_document_tool_asks_under_modeling(self):
        modeling = self._presets()["modeling"]
        allow, ask = self._bare(modeling["allow"]), self._bare(modeling["ask"])
        leaked = sorted(n for n in gen_posture.LEAVES_DOCUMENT if n in allow)
        assert not leaked, ("the modeling posture auto-allows writes whose effect leaves the "
                            "document: " + ", ".join(leaked))
        missing = sorted(n for n in gen_posture.LEAVES_DOCUMENT if n not in ask)
        assert not missing, "not routed to ask under modeling: " + ", ".join(missing)

    def test_the_named_outward_writes_ask_under_modeling(self):
        # pinned independently of LEAVES_DOCUMENT: dropping an entry from the classifier must go
        # red here, not quietly satisfy a test that iterates the classifier's own keys.
        ask = self._bare(self._presets()["modeling"]["ask"])
        for name in ("cam_post", "cam_edit_tools", "cam_create_machine", "cam_save_template",
                     "cam_generate_setup_sheet", "design_export", "mesh_export", "drawing_export",
                     "sys_reload_addin"):
            assert name in ask, f"{name} writes outside the document and must ask under modeling"

    def test_local_model_writes_stay_auto_allowed(self):
        allow = self._bare(self._presets()["modeling"]["allow"])
        for name in ("model_extrude", "sketch_create", "joint_create", "cam_generate",
                     "view_screenshot", "save_as_mesh"):
            assert name in allow, f"{name} is a local write and must stay auto-allowed under modeling"

    def test_every_named_tool_is_a_registered_write_kind_tool(self):
        # a renamed or read-kind entry would be a silent no-op in the classifier.
        tools = gen_manifest.collect()["tools"]
        kinds = {t["name"]: t["write"] for t in tools}
        wrong = sorted(f"{n} ({kinds.get(n, 'not registered')})"
                       for n in gen_posture.LEAVES_DOCUMENT if kinds.get(n) != "write")
        assert not wrong, ("LEAVES_DOCUMENT names something that is not a registered write-kind "
                           "tool: " + ", ".join(wrong))

    def test_every_named_tool_carries_a_reason(self):
        blank = sorted(n for n, why in gen_posture.LEAVES_DOCUMENT.items() if not (why or "").strip())
        assert not blank, "LEAVES_DOCUMENT entries need the effect stated: " + ", ".join(blank)

    def test_leaves_document_covers_family_and_named_effects(self):
        assert gen_posture.leaves_document("doc_save") is True       # cloud/lifecycle family
        assert gen_posture.leaves_document("cam_post") is True        # named effect
        assert gen_posture.leaves_document("cam_generate") is False   # local toolpath compute
        assert gen_posture.leaves_document("model_extrude") is False

    def test_the_conservative_preset_is_unchanged_by_the_effect_class(self):
        # conservative asks for EVERY write; the effect class only splits the modeling posture.
        tools = gen_manifest.collect()["tools"]
        conservative = gen_posture.build_presets(tools)["conservative"]
        assert self._bare(conservative["ask"]) == {t["name"] for t in tools if t["write"] == "write"}
