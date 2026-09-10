"""Unit tests for ``design_export.py`` - export a body/component/whole-design to a neutral CAD file.

Covers the format dispatch (step/iges/sat/smt/usd/f3d/stl/3mf/obj), target resolution (handle / body
name / component name / whole design), path defaulting + extension handling, the invisible-content
and per-format option knobs (STL binary/units, DXF construction/points/projected/units), and that the
right ExportManager.create*Options call is used per format. No live Fusion - fakes mimic ExportManager.
"""

import json

import pytest

from conftest import (BRepBody, BRepEdge, BRepFace, FakeExportManager, FakeOccurrence, MakeComp,
                      MakeDesign, Sketch, SketchCurves, _ExportOptions, _NamedCollection, install,
                      load_tool)

dx = load_tool("design_export")


# ── fakes ────────────────────────────────────────────────────────────────────

def _occ(name, full_path=None):
    """One occurrence. A real Occurrence always answers `component`; a read that RAISES is the
    unresolved-external-reference signal the shared occurrence census filters on."""
    return FakeOccurrence(path=full_path or name, component=MakeComp(name.split(":")[0]))


def _comp(name, bodies=(), occurrences=()):
    """A component holding `bodies` and `occurrences` - the two collections a target resolves in."""
    return MakeComp(name=name, bodies=list(bodies), occurrences=list(occurrences))


def _install(monkeypatch, bodies=None, comp_name="Root", occurrences=(), components=None):
    bodies = bodies if bodies is not None else [BRepBody("Body1")]
    comp = _comp(comp_name, bodies, occurrences)
    em = FakeExportManager()
    design = MakeDesign(comp=comp)
    design.exportManager = em
    if components is not None:
        # only when a test needs the design-wide component walk: without it the design answers just
        # the root, as most tests want.
        design._all_components = [_comp(n) for n in components]
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
    install(dx, design)
    return design, em, comp


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


# ── format dispatch ──────────────────────────────────────────────────────────

class TestFormatDispatch:
    def test_step_uses_step_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "step"
        assert em._executed is not None

    def test_iges_uses_iges_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        _payload(dx.handler(format="iges", file_path=str(tmp_path / "p.igs")))
        assert em._calls[-1]["kind"] == "iges"

    def test_sat_uses_sat_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        _payload(dx.handler(format="sat", file_path=str(tmp_path / "p.sat")))
        assert em._calls[-1]["kind"] == "sat"

    def test_stl_uses_stl_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["kind"] == "stl"

    def test_smt_uses_smt_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="smt", file_path=str(tmp_path / "p.smt")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "smt"

    def test_usd_uses_usd_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="usd", file_path=str(tmp_path / "p.usdz")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "usd"

    def test_f3d_uses_fusion_archive_options(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="f3d", file_path=str(tmp_path / "p.f3d")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "f3d"

    def test_3mf_uses_c3mf_options_geom_first(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="3mf", file_path=str(tmp_path / "p.3mf")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "3mf"
        # 3MF is a mesh-style format - geometry-first arg order like STL
        assert em._calls[-1]["geom"] is not None

    def test_obj_uses_obj_options_geom_first(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="obj", file_path=str(tmp_path / "p.obj")))
        assert out["exported"] is True
        assert em._calls[-1]["kind"] == "obj"

    def test_unknown_format_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        res = dx.handler(format="dwg", file_path=str(tmp_path / "p.dwg"))
        assert res["isError"] is True and "format" in res["message"]


# ── target resolution ────────────────────────────────────────────────────────

class TestTargetResolution:
    def test_whole_design_when_no_target(self, tmp_path, monkeypatch):
        _, em, comp = _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step")))
        # whole-design export passes the root component as the geometry
        assert em._calls[-1]["geom"] is comp
        assert "design" in out["target"].lower() or "root" in out["target"].lower()

    # A BODY and an OCCURRENCE reach the mesh factories only (see TestComponentOnlyFormats), so the
    # resolution tests below drive format=stl - the target vocabulary is what they pin.
    def test_body_by_name(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch, bodies=[BRepBody("Widget")])
        out = _payload(dx.handler(format="stl", target="Widget", file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["geom"].name == "Widget"
        assert "Widget" in out["target"]

    def test_body_by_handle(self, tmp_path, monkeypatch):
        design, em, _ = _install(monkeypatch, bodies=[BRepBody("Body1")])
        h = "/v" + "X" * 70
        design._tokens[h] = BRepBody("FromHandle")
        out = _payload(dx.handler(format="stl", target=h, file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["geom"].name == "FromHandle"

    def test_long_body_name_not_mistaken_for_handle(self, tmp_path, monkeypatch):
        # A long body NAME is not a handle: _resolve_token_entity returns None for a non-token, so
        # resolution falls through to the name lookup and a long body name exports by NAME.
        long_name = "Left-Outrigger-Pivot-Bracket-Weldment-Subassembly-Body-Number-Seven"
        assert len(long_name) > 60
        _, em, _ = _install(monkeypatch, bodies=[BRepBody(long_name)])
        out = _payload(dx.handler(format="stl", target=long_name, file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["geom"].name == long_name
        assert long_name in out["target"]

    def test_occurrence_by_name(self, tmp_path, monkeypatch):
        # occurrence resolution goes through the shared _resolve_occurrence (exact name/fullPathName)
        occ = _occ("Gear:1")
        _, em, _ = _install(monkeypatch, occurrences=[occ])
        out = _payload(dx.handler(format="stl", target="Gear:1", file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["geom"] is occ
        assert "Gear:1" in out["target"]

    def test_a_step_export_of_an_occurrence_is_refused_naming_the_routes_that_work(self,
                                                                                   tmp_path,
                                                                                   monkeypatch):
        # MEASURED on one occurrence carrying a body plus two sketches: createSTEPExportOptions
        # RAISES "3 : invlid argument geometry" on an Occurrence and on a BRepBody, while the same
        # component exported 8,905 bytes - so the refusal comes BEFORE the factory, naming a route.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Gear:1")])
        res = dx.handler(format="step", target="Gear:1", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True
        assert "Gear:1" in res["message"] and "COMPONENT" in res["message"]
        assert "stl" in res["message"]                      # the format that DOES take it
        assert em._executed is None                         # nothing was written

    def test_a_mesh_export_of_that_same_occurrence_is_allowed(self, tmp_path, monkeypatch):
        # The exact boundary: the refusal is per-FORMAT, not per-target - stl took all three live.
        occ = _occ("Gear:1")
        _, em, _ = _install(monkeypatch, occurrences=[occ])
        out = _payload(dx.handler(format="stl", target="Gear:1", file_path=str(tmp_path / "p.stl")))
        assert em._calls[-1]["geom"] is occ and out["exported"] is True

    def test_usd_and_f3d_refuse_an_occurrence_too(self, tmp_path, monkeypatch):
        # MEASURED alongside step/iges/sat/smt: usd and f3d RAISE on an occurrence and on a body
        # while writing 3,096 / 76,158 bytes from the same component - they are not mesh formats.
        for fmt in ("usd", "f3d"):
            _, em, _ = _install(monkeypatch, occurrences=[_occ("Gear:1")])
            res = dx.handler(format=fmt, target="Gear:1", file_path=str(tmp_path / f"p.{fmt}"))
            assert res["isError"] is True, fmt
            assert "COMPONENT" in res["message"] and em._executed is None, fmt

    def test_3mf_and_obj_take_an_occurrence(self, tmp_path, monkeypatch):
        # The boundary the remedy sentence rests on: all three formats it names were measured
        # landing a file from an occurrence, so naming them sends the caller somewhere that works.
        for fmt in ("stl", "obj", "3mf"):
            occ = _occ("Gear:1")
            _, em, _ = _install(monkeypatch, occurrences=[occ])
            out = _payload(dx.handler(format=fmt, target="Gear:1",
                                      file_path=str(tmp_path / f"q.{fmt}")))
            assert em._calls[-1]["geom"] is occ and out["exported"] is True, fmt

    def test_a_step_export_of_a_COMPONENT_is_allowed(self, tmp_path, monkeypatch):
        # The other side of the same boundary: a component is what the BRep factories take.
        _, em, _ = _install(monkeypatch, components=["Frame"])
        out = _payload(dx.handler(format="step", target="Frame",
                                  file_path=str(tmp_path / "p.step")))
        assert em._calls[-1]["geom"].name == "Frame" and out["exported"] is True

    def test_missing_named_target_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch, bodies=[BRepBody("Body1")])
        res = dx.handler(format="step", target="Nope", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True and "Nope" in res["message"]

    def test_ambiguous_name_refused_not_first_instance(self, tmp_path, monkeypatch):
        # two instances share the local name "Bolt:1" under different sub-assemblies - the real
        # shared resolver (_inputs._resolve_occurrence) must REFUSE the bare substring, naming both
        # fullPathNames, never export the first (wrong) instance.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Bolt:1", "Sub-A:1+Bolt:1"),
                                                      _occ("Bolt:1", "Sub-B:1+Bolt:1")])
        res = dx.handler(format="step", target="Bolt", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "Sub-A:1+Bolt:1" in res["message"] and "Sub-B:1+Bolt:1" in res["message"]
        assert em._executed is None                        # nothing was exported

    def test_component_name_resolves_when_one_component_carries_it(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch, components=["Bracket", "Frame"])
        out = _payload(dx.handler(format="step", target="Frame", file_path=str(tmp_path / "p.step")))
        assert em._calls[-1]["geom"].name == "Frame"
        assert "Frame" in out["target"]

    def test_duplicate_component_name_refused_not_exported(self, tmp_path, monkeypatch):
        # two components named 'Bracket': neither is the one asked for, so the export must refuse
        # rather than write one of them to disk under the requested name
        _, em, _ = _install(monkeypatch, components=["Bracket", "Bracket"])
        res = dx.handler(format="step", target="Bracket", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True
        # the COMPONENT step is what refuses, before the body vocabulary gets a turn: its refusal is
        # unlabelled, where the body resolver's copy of it arrives prefixed "'target': "
        assert res["message"].startswith("2 components match 'Bracket'")
        # and it offers the two vocabularies THIS tool still resolves, not a rename of the model
        assert "occurrence name/fullPathName" in res["message"]
        assert "find_geometry" in res["message"]
        assert "rename" not in res["message"].lower()
        assert em._executed is None                        # nothing was exported

    def test_an_EXACT_shared_name_refusal_keeps_its_candidate_list(self, tmp_path, monkeypatch):
        # the shared-EXACT-name refusal carries no "ambiguous" wording - only the OCCURRENCE_MISS
        # stem tells a plain miss from a refusal - so its candidate list must reach the caller
        # instead of degrading to a generic not-found.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Bolt:1", "Sub-A:1+Bolt:1"),
                                                      _occ("Bolt:1", "Sub-B:1+Bolt:1")])
        res = dx.handler(format="step", target="Bolt:1", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True
        assert "Sub-A:1+Bolt:1" in res["message"] and "Sub-B:1+Bolt:1" in res["message"]
        assert em._executed is None                        # nothing was exported


# ── path handling ────────────────────────────────────────────────────────────

class TestPathHandling:
    def test_missing_path_errors(self, monkeypatch):
        _install(monkeypatch)
        res = dx.handler(format="step")
        assert res["isError"] is True and "file_path" in res["message"]

    def test_extension_auto_appended(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        p = str(tmp_path / "noext")
        out = _payload(dx.handler(format="step", file_path=p))
        # the path handed to the exporter ends with the format extension
        assert em._calls[-1]["path"].lower().endswith(".step")
        assert out["file_path"].lower().endswith(".step")


# ── invisible-content + per-format option knobs ─────────────────────────────

class TestOptionsApplied:
    def test_an_option_the_api_refuses_is_named_not_reported_as_applied(self, tmp_path, monkeypatch):
        # A knob the platform ignores must NOT appear under options_applied: that key states the
        # value the file was written with, so a refused option listed there is a false claim.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(em, isBinaryFormat=True)   # always binary, whatever is written
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"),
                                  stl_binary=False))
        assert out.get("options_applied", {}).get("stl_binary") is None
        assert "stl_binary" in out["options_refused"]
        assert "did not take these options" in out["note"]
        # ...and 'options_refused' names the KNOB only, so the value it was attempted with is
        # recoverable from 'options_requested' or from nowhere - which is where the note points.
        assert out["options_requested"]["stl_binary"] is False
        assert "'options_requested' carries the value each was asked with" in out["note"]

    def _drops_every_write(self, em, **factory_values):
        """Options whose FACTORY values are 'factory_values' and which DROP every write to them -
        the shape a set-then-read-back cannot bite on. Measured, it is not hypothetical: a fresh
        STLExportOptions reads unitType 0 and MillimeterDistanceUnits IS 0, so an 'mm' request
        reads back off an object nothing was ever assigned to."""
        def _deaf(kind, path, geom=None, **kw):
            return _ExportOptions(kind, path, geom, drops=factory_values, seeded=factory_values,
                                  **kw)
        em._options_class = _deaf
        return _deaf

    def test_a_unit_the_options_already_read_is_published_unverified(self, tmp_path, monkeypatch):
        # THE COLLISION. Every write is dropped, yet the options object reads 'mm' because that is
        # its factory value - so the read-back equals the request and would report a clean
        # 'applied'. Measured, the file is then written in the unit of the session's last explicit
        # unitType assignment, which no read exposes (measure_api
        # stl-export-unittype-is-sticky-session-state). 'options_applied' may not stand alone here.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(
            em, unitType=dx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_units="mm"))
        assert out["options_applied"]["stl_units"] == "mm"
        assert out["options_verified"]["stl_units"] is False
        assert "UNVERIFIED" in out["note"]
        assert "options_refused" not in out          # it DID read back - a different disclosure

    def test_an_unmeasured_boolean_the_options_already_read_is_published_unverified(self, tmp_path,
                                                                                    monkeypatch):
        # A boolean has only two possible requests, so whichever one the factory holds is
        # unverifiable - about half of them. invisible_bodies is the case with NO measurement
        # tying its read to the written file, so it discloses like any unmeasured knob.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(em, isIncludingInvisibleBodies=True)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"),
                                  include_invisible_bodies=True))
        assert out["options_applied"]["invisible_bodies"] is True
        assert out["options_verified"]["invisible_bodies"] is False
        assert "invisible_bodies is set but UNVERIFIED" in out["note"]

    def test_stl_binary_publishes_no_verification_because_its_read_determines_the_file(
            self, tmp_path, monkeypatch):
        # MEASURED: isBinaryFormat's factory value is True, and its read value DETERMINES the file -
        # untouched and explicit-True are byte-identical, explicit-False writes a distinct ASCII
        # 'solid ' file. So reading back the request answers what the caller asked whoever set it,
        # and a permanent 'verified: false' on the COMMON request would be noise, not honesty.
        # Driven at the collision: the write is dropped and the factory value is the request.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(em, isBinaryFormat=True)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"),
                                  stl_binary=True))
        assert out["options_applied"]["stl_binary"] is True
        assert "stl_binary" not in out.get("options_verified", {})   # no evidence key for THIS knob
        assert "UNVERIFIED" not in out["note"]

    def test_a_dropped_stl_binary_write_is_still_refused_where_the_read_back_can_see_it(
            self, tmp_path, monkeypatch):
        # Dropping the flag must not drop the GUARD: asked for the value the factory does NOT hold,
        # a dropped write still reads back wrong and is refused, never reported as applied.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(em, isBinaryFormat=True)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"),
                                  stl_binary=False))
        assert out.get("options_applied", {}).get("stl_binary") is None
        assert "stl_binary" in out["options_refused"]

    def test_a_knob_the_options_did_not_already_read_is_published_verified(self, tmp_path,
                                                                          monkeypatch):
        # The control, and the one that keeps this honest in the other direction: the property held
        # a DIFFERENT value before the set, the write landed, so the read-back could have failed
        # and did not. A real verification, published as one, with no caveat on the wire.
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_units="in"))
        assert out["options_applied"]["stl_units"] == "in"
        assert out["options_verified"]["stl_units"] is True
        assert "UNVERIFIED" not in out["note"]

    def test_a_dropped_write_is_still_refused_where_the_read_back_can_see_it(self, tmp_path,
                                                                            monkeypatch):
        # The pre-read must not weaken the existing guard: a dropped write to a property holding
        # some OTHER value is still a refusal, not an unverified pass.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(
            em, unitType=dx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_units="in"))
        assert out.get("options_applied", {}).get("stl_units") is None
        assert "stl_units" in out["options_refused"]
        assert "options_verified" not in out         # nothing landed, so nothing to back

    def test_default_call_sets_no_extra_options(self, tmp_path, monkeypatch):
        # the common case (no opt-in flags) must keep the plain export payload shape - neither an
        # 'options_applied' nor an 'options_requested' key when nothing was requested, since an
        # empty pair of dicts is noise a caller reads past.
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step")))
        assert "options_applied" not in out
        assert "options_requested" not in out
        opts = em._calls[-1]
        assert not hasattr(opts, "isIncludingInvisibleBodies")
        assert not hasattr(opts, "isBinaryFormat")

    def test_include_invisible_bodies_and_components(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step"),
                                   include_invisible_bodies=True, include_invisible_components=True))
        opts = em._calls[-1]
        assert opts.isIncludingInvisibleBodies is True
        assert opts.isIncludingInvisibleComponents is True
        assert out["options_applied"]["invisible_bodies"] is True
        assert out["options_applied"]["invisible_components"] is True

    def test_invisible_flags_ignored_when_false(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        dx.handler(format="step", file_path=str(tmp_path / "p.step"),
                  include_invisible_bodies=False, include_invisible_components=False)
        opts = em._calls[-1]
        assert not hasattr(opts, "isIncludingInvisibleBodies")

    def test_stl_binary_true(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_binary=True))
        assert em._calls[-1].isBinaryFormat is True
        assert out["options_applied"]["stl_binary"] is True

    def test_stl_binary_false_ascii(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_binary=False))
        assert em._calls[-1].isBinaryFormat is False
        # False here states the FILE IS ASCII - options_applied carries the value that landed,
        # never a did-it-stick flag, which under this key would read as the value it is not
        assert out["options_applied"]["stl_binary"] is False

    def test_stl_binary_omitted_leaves_factory_default_untouched(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        dx.handler(format="stl", file_path=str(tmp_path / "p.stl"))
        assert not hasattr(em._calls[-1], "isBinaryFormat")

    def test_stl_units_applied_via_distance_units_enum(self, tmp_path, monkeypatch):
        # unitType takes DistanceUnits, live-verified; MeshUnits has mm/cm SWAPPED relative to it,
        # so pinning the enum family here is what catches a silent 10x-wrong-geometry regression.
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_units="in"))
        assert em._calls[-1].unitType is dx.adsk.fusion.DistanceUnits.InchDistanceUnits
        # the VALUE that landed, in the tool's own vocabulary - not a did-it-stick flag, which
        # under this key would read as the value it is not
        assert out["options_applied"]["stl_units"] == "in"

    def test_stl_units_omitted_still_assigns_mm_rather_than_inheriting_a_unit(self, tmp_path,
                                                                              monkeypatch):
        # The omitted case is NOT a neutral default: an STL whose unitType is never assigned takes
        # the unit of the last explicit assignment anywhere in the Fusion session, so leaving the
        # property alone writes an unrelated earlier export's unit. Asserted on the ENUM MEMBER the
        # options object received, not on the payload - this is the write, not the report of it.
        _, em, _ = _install(monkeypatch)
        dx.handler(format="stl", file_path=str(tmp_path / "p.stl"))
        assert em._calls[-1].unitType is dx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits

    def test_the_omitted_unit_is_not_advertised_as_a_schema_default(self, tmp_path, monkeypatch):
        # The unit an omitted stl_units bakes in is NOT the value of a schema `default`: a client
        # that materialized such a default into the call would be REFUSED on every other format, so
        # the omitted unit is substituted by the handler and read back off the payload instead.
        assert "default" not in dx._STL_UNITS.schema()
        _install(monkeypatch)
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"), stl_units="mm")
        assert res["isError"] is True and "stl_units" in res["message"]

    def test_an_omitted_unit_is_reported_in_the_payload_it_was_written_with(self, tmp_path,
                                                                            monkeypatch):
        # Writing the right unit is half of it: a caller re-importing the file needs the payload to
        # NAME the unit, and the omitted case is exactly where nothing was asked for to echo back.
        # options_applied is the value read back off the options object that wrote the file, so the
        # unit is recoverable from the receipt without the caller having asked for it.
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl")))
        assert out["options_applied"]["stl_units"] == "mm"
        # ...and the request beside it, as the sibling mesh_export publishes for every STL: the unit
        # rides on every STL export whether it was asked for or not, so it is always in both keys.
        assert out["options_requested"]["stl_units"] == "mm"

    def test_stl_units_bad_value_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        res = dx.handler(format="stl", file_path=str(tmp_path / "p.stl"), stl_units="parsecs")
        assert res["isError"] is True and "stl_units" in res["message"]

    def test_a_binary_flag_asked_for_on_a_non_stl_format_is_refused_naming_it(self, tmp_path,
                                                                              monkeypatch):
        # Dropped, it hands back a file whose ASCII-vs-binary shape the caller chose and did not
        # get, with nothing on the wire saying so - the same defect the sibling unit refusal below
        # closes. It names the value AND the format it was asked with, so neither is guesswork.
        _, em, _ = _install(monkeypatch)
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"), stl_binary=True)
        assert res["isError"] is True
        assert "'stl_binary' (true)" in res["message"] and "format=step" in res["message"]
        assert em._calls == []                              # nothing was written

    def test_the_FALSE_binary_flag_is_refused_too_not_read_as_omitted(self, tmp_path, monkeypatch):
        # THE BOUNDARY. false is the value that CHANGES an STL (ASCII rather than the factory's
        # binary), and it is the one a truthiness guard would go on dropping while the true case
        # read as fixed. The refusal keys on 'passed at all', so both values are turned away and
        # the message names the one it saw.
        _, em, _ = _install(monkeypatch)
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"), stl_binary=False)
        assert res["isError"] is True
        assert "'stl_binary' (false)" in res["message"] and "format=step" in res["message"]
        assert em._calls == []                              # nothing was written

    @pytest.mark.parametrize("fmt", [f for f in dx._FORMAT.options if f != "stl"])
    def test_every_format_but_stl_refuses_a_binary_flag_it_writes_into_nothing(self, fmt, tmp_path,
                                                                               monkeypatch):
        # stl is the ONE format this tool writes isBinaryFormat for, so the refusal is a rule over
        # the whole format Choice rather than a property of any one format: a guard narrowed to exempt
        # obj/3mf - the mesh-shaped siblings an agent is likeliest to pass this to by mistake -
        # reads as correct against a sample of two. dxf is in the list for a second reason: it is
        # reached by its OWN dispatch, so a guard sitting behind that branch never sees it, and the
        # sketch named here is the one that would be exported instead if it did.
        _, em, _ = _install(monkeypatch)
        res = dx.handler(format=fmt, stl_binary=True, dxf_sketch="Profile1",
                         file_path=str(tmp_path / ("p." + fmt)))
        assert res["isError"] is True, fmt
        assert "'stl_binary' (true)" in res["message"], res["message"]
        assert f"format={fmt}" in res["message"], res["message"]
        assert em._calls == [], fmt                         # nothing was written

    def test_the_split_path_refuses_the_binary_flag_too(self, tmp_path, monkeypatch):
        # The refusal fires before the split walk as well: a guard only the single-target branch
        # reaches would write per-component files while dropping the caller's ASCII-vs-binary
        # choice - the same silent drop, on the entry path the live sweep drives (a split STEP
        # export carrying stl_binary).
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        res = dx.handler(format="step", split_by_component=True, stl_binary=True,
                         file_path=str(tmp_path))
        assert res["isError"] is True
        assert "'stl_binary' (true)" in res["message"] and "format=step" in res["message"]
        assert em._calls == []                              # nothing built, nothing written

    def test_an_omitted_binary_flag_on_a_non_stl_format_is_not_refused(self, tmp_path, monkeypatch):
        # The refusal keys on what the CALLER passed, never on the knob existing - a STEP export
        # that never mentioned it must still run, with the STL property absent from its options
        # object rather than written with some stand-in value.
        _, em, _ = _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step")))
        assert out["exported"] is True
        assert not hasattr(em._calls[-1], "isBinaryFormat")

    @pytest.mark.parametrize("other", [f for f in dx._FORMAT.options if f not in ("stl", "dxf")])
    def test_stl_is_the_one_format_this_tool_bakes_a_unit_into(self, other, tmp_path, monkeypatch):
        # The refusals here are one half of "stl is the ONE format this tool bakes a unit into" -
        # they prove the REQUEST is turned away. This is the other half, the ASSIGNMENT: unitType
        # reaches the stl path's options object and no other, so a baking gate widened to a sibling
        # would write a unit onto an options object nothing asked one of, with every refusal test
        # still green. dxf is out of the list because it takes its own dispatch and builds no
        # *ExportOptions object at all.
        _, em, _ = _install(monkeypatch)
        dx.handler(format="stl", file_path=str(tmp_path / "p.stl"))
        assert em._calls[-1].unitType is dx.adsk.fusion.DistanceUnits.MillimeterDistanceUnits
        dx.handler(format=other, file_path=str(tmp_path / ("p." + other)))
        assert not hasattr(em._calls[-1], "unitType"), other

    def test_a_unit_asked_for_on_a_non_stl_format_is_refused_naming_it(self, tmp_path, monkeypatch):
        # Dropping it silently hands back a file whose unit nothing states - the defect this input
        # exists to close, and the refusal the sibling mesh_export makes for the same request. It
        # names the value AND the format it was asked with, so neither has to be guessed. The value
        # asked for here is the Choice's own DEFAULT: naming the default unit is still asking for
        # it, so it is refused exactly as any other value is.
        _, em, _ = _install(monkeypatch)
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"), stl_units="mm")
        assert res["isError"] is True
        assert "'mm'" in res["message"] and "format=step" in res["message"]
        assert em._calls == []                              # nothing was written

    @pytest.mark.parametrize("fmt", [f for f in dx._FORMAT.options if f != "stl"])
    def test_every_format_but_stl_refuses_a_unit_it_bakes_into_nothing(self, fmt, tmp_path,
                                                                       monkeypatch):
        # stl is the ONE format this tool bakes a unit into, so the refusal is a rule over the whole
        # format Choice rather than a property of any one format: a guard narrowed to exempt
        # obj/3mf/usd - the mesh-shaped formats an agent is likeliest to pass a unit to by mistake -
        # reads as correct against a sample of two. dxf is in the list for a second reason: it is
        # reached by its OWN dispatch, so a guard sitting behind that branch never sees it. The unit
        # here is NOT the Choice's default, so the message is pinned to the value the CALL names
        # rather than to one the resolver could supply on its own.
        _, em, _ = _install(monkeypatch)
        res = dx.handler(format=fmt, stl_units="in", dxf_sketch="Profile1",
                         file_path=str(tmp_path / ("p." + fmt)))
        assert res["isError"] is True, fmt
        assert "'in'" in res["message"] and f"format={fmt}" in res["message"], res["message"]
        assert em._calls == [], fmt                         # nothing was written

    def test_the_split_path_refuses_the_unit_too(self, tmp_path, monkeypatch):
        # The refusal fires before the split walk as well - the same entry-path hole its
        # binary-flag sibling closes one guard above. A guard only the single-target branch
        # reaches writes a file per component while dropping the unit the caller asked for, and
        # every other unit refusal here goes through the single-target path, so that narrowing
        # reads as correct. The unit is not the Choice's default, so the message is pinned to the
        # value the CALL names.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        res = dx.handler(format="step", split_by_component=True, stl_units="in",
                         file_path=str(tmp_path))
        assert res["isError"] is True
        assert "'stl_units'" in res["message"] and "'in'" in res["message"]
        assert "format=step" in res["message"]
        assert em._calls == []                              # nothing built, nothing written

    def test_an_omitted_unit_on_a_non_stl_format_is_not_refused(self, tmp_path, monkeypatch):
        # The refusal keys on what the CALLER asked for, not on the Choice's default - a STEP export
        # that never mentioned a unit must still run.
        _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step")))
        assert out["exported"] is True

    def test_the_single_target_payload_names_what_was_asked_for(self, tmp_path, monkeypatch):
        # options_applied is the value that LANDED; without the request beside it a caller cannot
        # tell a knob it asked for from one the options object already held. Same key the split path
        # and mesh_export publish, over every knob this call writes.
        _install(monkeypatch)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path / "p.stl"),
                                  stl_binary=False, include_invisible_bodies=True))
        assert out["options_requested"] == {"invisible_bodies": True, "stl_binary": False,
                                            "stl_units": "mm"}

    def test_a_non_stl_export_names_what_was_asked_for_too(self, tmp_path, monkeypatch):
        # The key is keyed on what the CALL asked for, never on the format: the invisible-* pair
        # rides on every format, so a STEP export that asked for one publishes it exactly as an STL
        # export does. Every other assertion on this key in the single-target path is made on an STL
        # call, so without this one a publish narrowed to fmt == "stl" reads as correct.
        _install(monkeypatch)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step"),
                                  include_invisible_bodies=True))
        assert out["options_requested"] == {"invisible_bodies": True}

    def test_a_refused_knob_on_a_non_stl_format_still_points_at_a_key_that_is_there(
            self, tmp_path, monkeypatch):
        # The refusal note NAMES 'options_requested', so that key has to be published on every
        # format the note can be emitted on - on a STEP export whose invisible-bodies write is
        # dropped, a key withheld would leave the sentence pointing at nothing.
        _, em, _ = _install(monkeypatch)
        self._drops_every_write(em, isIncludingInvisibleBodies=False)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path / "p.step"),
                                  include_invisible_bodies=True))
        assert out["options_refused"] == ["invisible_bodies"]
        assert out["options_requested"]["invisible_bodies"] is True
        assert "'options_requested' carries the value each was asked with" in out["note"]


# ── format=dxf (sketch / face-profile 2D export) ──────────────────────────────

class FakeSketch(Sketch):
    """A sketch that records the two writes the DXF path makes on it: deleteMe (the scratch sketch
    is removed) and project2, which grows the line count by 'project_adds'. DXF writing itself goes
    through the design's exportManager (createDXFSketchExportOptions), not a method on the sketch."""
    def __init__(self, name="Sketch1", lines=0, arcs=0, circles=0, points=0, project_adds=1):
        super().__init__(name=name, points=[None] * points,
                         curves=SketchCurves(lines=[None] * lines, arcs=[None] * arcs,
                                             circles=[None] * circles))
        self._project_adds = project_adds
        self.deleted = False
        self.project_calls = []

    def deleteMe(self):
        self.deleted = True
        return True

    def project2(self, entities, is_linked):
        self.project_calls.append((entities, is_linked))
        self.sketchCurves.sketchLines._items.extend([None] * self._project_adds)
        return [object()] * self._project_adds


class FakeSketchesColl:
    def __init__(self, sketch):
        self._sketch = sketch
        self.added_with = None

    def add(self, face):
        self.added_with = face
        return self._sketch


def FakeFaceComp(sketch):
    """The component the scratch DXF sketch is added to - its sketches collection records the face
    the add was made from."""
    comp = MakeComp("FaceComp")
    comp.sketches = FakeSketchesColl(sketch)
    return comp


def FakeFace(comp):
    """A face whose body belongs to `comp` - the hop the DXF path walks to reach its sketches."""
    return BRepFace(None, body=BRepBody("FaceBody", parent_component=comp))


class TestDxfExport:
    def test_sketch_happy_path(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=2)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        out = _payload(dx.handler(format="dxf", dxf_sketch="Profile1",
                                   file_path=str(tmp_path / "p")))
        assert out["exported"] is True
        assert out["format"] == "dxf"
        assert out["file_path"].lower().endswith(".dxf")
        assert em._calls[-1]["kind"] == "dxf"
        assert em._calls[-1]["geom"] is sk
        assert em._calls[-1]["path"] == out["file_path"]

    def test_sketch_export_defaults_include_everything(self, tmp_path, monkeypatch):
        # Sketch.saveAsDXF takes no filter options at all (unfiltered output); the options-based
        # export must reproduce that by defaulting all three content flags to True.
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=2)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        _payload(dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf")))
        opts = em._calls[-1]
        assert opts.isConstructionExported is True
        assert opts.isPointsExported is True
        assert opts.isProjectedGeometryExported is True

    def test_sketch_export_flags_can_be_narrowed(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=2)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        _payload(dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"),
                            dxf_export_construction=False, dxf_export_points=False))
        opts = em._calls[-1]
        assert opts.isConstructionExported is False
        assert opts.isPointsExported is False
        assert opts.isProjectedGeometryExported is True   # left at its default (True)

    def test_dxf_never_touches_the_units_property(self, tmp_path, monkeypatch):
        # DXFSketchExportOptions.units is a poison property (reading it aborts the live
        # transaction) - the fake's .units getter raises, so this export succeeding proves the
        # handler leaves it alone entirely. There is deliberately no dxf_units input.
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=1)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        out = _payload(dx.handler(format="dxf", dxf_sketch="Profile1",
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True

    def test_missing_sketch_and_face_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        res = dx.handler(format="dxf", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "dxf_sketch" in res["message"] and "dxf_face" in res["message"]

    def test_both_sketch_and_face_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        res = dx.handler(format="dxf", dxf_sketch="Profile1", dxf_face="H" * 40,
                          file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True and "only one" in res["message"].lower()

    def test_empty_sketch_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        sk = FakeSketch(lines=0, arcs=0, circles=0, points=0)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        res = dx.handler(format="dxf", dxf_sketch="Empty", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True and "empty" in res["message"].lower()

    def test_sketch_not_found_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (None, None))
        monkeypatch.setattr(dx._common, "all_sketch_names", lambda design: ["Sketch1"])
        res = dx.handler(format="dxf", dxf_sketch="Nope", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True and "Nope" in res["message"]

    def test_a_shared_sketch_name_is_refused_with_its_owners_not_called_missing(
            self, tmp_path, monkeypatch):
        # Two components each holding a 'Profile1' is a REFUSAL naming both, never "No sketch
        # named 'Profile1'" - that sentence states the opposite of what the walk read.
        _install(monkeypatch)
        refusal = "2 sketches are named 'Profile1' ('Profile1' in Root, 'Profile1' in Frame)"
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (None, refusal))
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert res["message"] == refusal
        assert "No sketch named" not in res["message"]

    def _two_components(self, monkeypatch, alpha_sketches, beta_sketches):
        """Two named components, each with its OWN sketches collection, and the design-wide walk
        LEFT UNSTUBBED - the scope filter is the thing under test, so a fake standing in for the
        resolver would prove nothing. Both components hold a sketch of the SAME name; with two
        different names the filter never runs and unscoped code would pass."""
        design, em, comp = _install(monkeypatch)
        alpha = _comp("Alpha", [])
        alpha.sketches = _NamedCollection(list(alpha_sketches))
        beta = _comp("Beta", [])
        beta.sketches = _NamedCollection(list(beta_sketches))
        design._all_components = [alpha, beta]
        design.rootComponent = alpha
        design.activeComponent = alpha
        return design, em, alpha, beta

    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, tmp_path,
                                                                        monkeypatch):
        a_sk, b_sk = FakeSketch("Profile1", lines=2), FakeSketch("Profile1", circles=1)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [b_sk])
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "2 sketches are named 'Profile1'" in res["message"]
        assert "'dxf_component'" in res["message"] and "Rename one" not in res["message"]
        assert em._calls == []                        # nothing was written

    def test_the_scope_writes_THAT_components_sketch(self, tmp_path, monkeypatch):
        a_sk, b_sk = FakeSketch("Profile1", lines=2), FakeSketch("Profile1", circles=1)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [b_sk])
        out = _payload(dx.handler(format="dxf", dxf_sketch="Profile1", dxf_component="Beta",
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert em._calls[-1]["geom"] is b_sk

    def test_the_sibling_component_is_reachable_by_the_same_call(self, tmp_path, monkeypatch):
        a_sk, b_sk = FakeSketch("Profile1", lines=2), FakeSketch("Profile1", circles=1)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [b_sk])
        _payload(dx.handler(format="dxf", dxf_sketch="Profile1", dxf_component="Alpha",
                            file_path=str(tmp_path / "p.dxf")))
        assert em._calls[-1]["geom"] is a_sk

    def test_an_unknown_component_is_refused_before_the_write(self, tmp_path, monkeypatch):
        a_sk, b_sk = FakeSketch("Profile1", lines=2), FakeSketch("Profile1", circles=1)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [b_sk])
        res = dx.handler(format="dxf", dxf_sketch="Profile1", dxf_component="Gamma",
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert em._calls == []

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self, tmp_path,
                                                                       monkeypatch):
        # The scope is VALIDATED: a dropped one writes Alpha's sketch to a file the caller asked
        # for from Beta, and the DXF on disk is the wrong outline with no error to say so.
        a_sk = FakeSketch("OnlyOne", lines=2)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [])
        res = dx.handler(format="dxf", dxf_sketch="OnlyOne", dxf_component="Beta",
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert em._calls == []

    def test_the_scoped_MISS_names_dxf_component_never_the_bare_component(self, tmp_path,
                                                                          monkeypatch):
        # This tool declares a STRICT schema and carries NO 'component' input, so a refusal naming
        # 'component' hands the caller a retry its own schema rejects.
        a_sk = FakeSketch("Profile1", lines=2)
        _d, em, _a, _b = self._two_components(monkeypatch, [a_sk], [FakeSketch("Other", lines=1)])
        res = dx.handler(format="dxf", dxf_sketch="Profile1", dxf_component="Beta",
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "holds no sketch named 'Profile1'" in res["message"]
        assert "'dxf_component'" in res["message"]
        assert "'component'" not in res["message"]
        assert em._calls == []

    def test_an_AMBIGUOUS_dxf_component_names_dxf_component(self, tmp_path, monkeypatch):
        from conftest import make_occurrence
        design, em, comp = _install(monkeypatch)
        root = _comp("Root", [])
        root.sketches = _NamedCollection([])
        a = _comp("Frame", [])
        a.sketches = _NamedCollection([FakeSketch("Profile1", lines=2)])
        b = _comp("Frame", [])
        b.sketches = _NamedCollection([FakeSketch("Profile1", circles=1)])
        root.allOccurrences = [make_occurrence("P2-Gimbal:1+Frame:1", a),
                               make_occurrence("P3-Gimbal:1+Frame:1", b)]
        design.rootComponent = root
        design.activeComponent = root
        design._all_components = [root, a, b]
        res = dx.handler(format="dxf", dxf_sketch="Profile1", dxf_component="Frame",
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "2 components match 'Frame'" in res["message"]
        assert "'dxf_component' also takes an occurrence fullPathName" in res["message"]
        assert "'component'" not in res["message"]
        assert em._calls == []

    def test_execute_false_is_reported_as_failure(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=1)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        em.execute = lambda opts: False
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "nothing was written" in res["message"].lower()

    def test_face_happy_path_cleans_up_scratch_sketch(self, tmp_path, monkeypatch):
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=0, project_adds=3)
        comp = FakeFaceComp(sk)
        face = FakeFace(comp)
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                   file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert comp.sketches.added_with is face
        assert sk.deleted is True
        assert "removed" in out["note"].lower()
        # the face path's entire content is projected geometry - the flag must default True
        assert em._calls[-1].isProjectedGeometryExported is True

    def test_face_no_geometry_errors_and_cleans_up(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        sk = FakeSketch(lines=0, project_adds=0)
        comp = FakeFaceComp(sk)
        face = FakeFace(comp)
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        res = dx.handler(format="dxf", dxf_face="H" * 40, file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "nothing to write" in res["message"].lower()
        assert sk.deleted is True

    def test_extension_auto_appended(self, tmp_path, monkeypatch):
        _install(monkeypatch)
        sk = FakeSketch(lines=1)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        out = _payload(dx.handler(format="dxf", dxf_sketch="Profile1",
                                   file_path=str(tmp_path / "noext")))
        assert out["file_path"].lower().endswith(".dxf")


class TestDxfFaceProjection:
    """What the face path sketches ON and projects. MEASURED on a root-owned body and on a
    sub-component's alike: project2 of the FACE into a sketch that lies on that same face raises
    '2 : InternalValidationError : res', and Fusion may have projected the outline as it created
    the sketch - so the edges are projected, into the component that owns the NATIVE face."""

    def _face_with_edges(self, comp, edges):
        return BRepFace(None, body=BRepBody("FaceBody", parent_component=comp), edges=edges)

    def test_the_faces_EDGES_are_projected_never_the_face_itself(self, tmp_path, monkeypatch):
        _, _em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=0, project_adds=2)
        comp = FakeFaceComp(sk)
        edges = [BRepEdge(None), BRepEdge(None)]
        face = self._face_with_edges(comp, edges)
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert len(sk.project_calls) == 1
        assert sk.project_calls[0][0] == edges          # the edges, not [face]

    def test_a_proxy_handle_sketches_in_the_NATIVE_faces_component(self, tmp_path, monkeypatch):
        # A find_geometry handle at a sub-component's face resolves to a PROXY; sketching in the
        # proxy's own component and projecting the proxy is the pair that raises.
        _install(monkeypatch)
        native_sk = FakeSketch(lines=0, project_adds=2)
        owner = FakeFaceComp(native_sk)
        edges = [BRepEdge(None)]
        native = self._face_with_edges(owner, edges)
        other = FakeFaceComp(FakeSketch(lines=0, project_adds=2))
        proxy = BRepFace(None, body=BRepBody("FaceBody", parent_component=other),
                         assembly_context=object(), native_object=native, edges=[BRepEdge(None)])
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (proxy, None))
        out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert owner.sketches.added_with is native      # the native face, in ITS component
        assert other.sketches.added_with is None        # never the proxy's own component
        assert native_sk.project_calls[0][0] == edges   # the NATIVE face's edges

    def test_a_sketch_fusion_already_filled_is_not_projected_a_second_time(self, tmp_path,
                                                                          monkeypatch):
        # Creating a sketch on a face can arrive with the face's edges already projected; projecting
        # again writes every outline curve to the DXF twice.
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=4, project_adds=4)
        comp = FakeFaceComp(sk)
        face = self._face_with_edges(comp, [BRepEdge(None)])
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert sk.project_calls == []
        assert em._calls[-1]["geom"] is sk

    def test_projected_false_is_refused_before_the_design_is_touched(self, tmp_path, monkeypatch):
        # MEASURED: the DXF this wrote landed ~2 KB with an EMPTY entities section, under
        # exported:true - the face path's whole content is the projected outline.
        _, em, _ = _install(monkeypatch)
        sk = FakeSketch(lines=0, project_adds=4)
        comp = FakeFaceComp(sk)
        face = self._face_with_edges(comp, [BRepEdge(None)])
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        res = dx.handler(format="dxf", dxf_face="H" * 40, dxf_export_projected=False,
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "'dxf_export_projected'" in res["message"]
        assert comp.sketches.added_with is None          # no scratch sketch was made
        assert em._calls == []                           # and nothing was written

    def test_projected_true_and_omitted_both_export(self, tmp_path, monkeypatch):
        # The refusal is keyed on the value FALSE, not on the flag being present: true must pass.
        for i, kwargs in enumerate(({"dxf_export_projected": True}, {})):
            _install(monkeypatch)
            sk = FakeSketch(lines=0, project_adds=2)
            comp = FakeFaceComp(sk)
            face = self._face_with_edges(comp, [BRepEdge(None)])
            monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
            out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                      file_path=str(tmp_path / f"p{i}.dxf"), **kwargs))
            assert out["exported"] is True, kwargs


class TestDxfWriterGuards:
    """Every read _write_dxf needs before it can write is guarded and NAMED - an absent
    exportManager, a build without the DXF factory, and a factory that raises."""

    def _sketch_export(self, tmp_path, monkeypatch, sketch=None):
        design, em, _ = _install(monkeypatch)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda d, name, remedy=None: (sketch or FakeSketch(lines=1), None))
        return design, em

    def test_a_design_with_no_export_manager_is_named(self, tmp_path, monkeypatch):
        design, _em = self._sketch_export(tmp_path, monkeypatch)
        design.exportManager = None
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "exposes no exportManager" in res["message"]

    def test_a_build_without_the_dxf_factory_is_named(self, tmp_path, monkeypatch):
        _design, em = self._sketch_export(tmp_path, monkeypatch)
        # absent, as on a build that never had it - not None-valued
        monkeypatch.delattr(FakeExportManager, "createDXFSketchExportOptions")
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "no createDXFSketchExportOptions" in res["message"]
        assert em._executed is None

    def test_a_factory_that_raises_reports_its_reason(self, tmp_path, monkeypatch):
        _design, em = self._sketch_export(tmp_path, monkeypatch)

        def boom(path, sketch):
            raise RuntimeError("the sketch is not exportable")

        em.createDXFSketchExportOptions = boom
        res = dx.handler(format="dxf", dxf_sketch="Profile1", file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "Could not create DXF export options" in res["message"]
        assert "not exportable" in res["message"]

    def test_a_failed_write_whose_scratch_sketch_also_survives_names_both(self, tmp_path,
                                                                         monkeypatch):
        # the compensating delete is best-effort: if BOTH the write and the cleanup fail, the
        # caller is told the sketch is still in their design, not just that the export failed.
        _design, em, _ = _install(monkeypatch)
        sk = FakeSketch(name="Scratch7", lines=0, project_adds=2)
        sk.deleteMe = lambda: False
        face = FakeFace(FakeFaceComp(sk))
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        em.execute = lambda opts: False
        res = dx.handler(format="dxf", dxf_face="H" * 40, file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "nothing was written" in res["message"].lower()
        assert "Scratch7" in res["message"] and "delete it manually" in res["message"]

    def test_a_written_dxf_whose_scratch_sketch_survives_says_so_in_the_note(self, tmp_path,
                                                                            monkeypatch):
        _install(monkeypatch)
        sk = FakeSketch(name="Scratch7", lines=0, project_adds=2)
        sk.deleteMe = lambda: False
        face = FakeFace(FakeFaceComp(sk))
        monkeypatch.setattr(dx._DXF_FACE, "resolve", lambda raw: (face, None))
        out = _payload(dx.handler(format="dxf", dxf_face="H" * 40,
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert "Scratch7" in out["note"] and "could not be removed" in out["note"]
        assert "the design is unchanged" not in out["note"]


class TestInputsABranchCannotReachAreRefused:
    """An input the chosen branch never hands to the writer is refused by NAME, the shape the
    stl_units/stl_binary guards set - dropped, it hands back a file the caller did not ask for."""

    def _sketch(self, monkeypatch):
        """A dxf export that WOULD succeed, so a refusal below is the guard and not a sketch miss."""
        sk = FakeSketch(lines=2)
        monkeypatch.setattr(dx._common, "find_sketch",
                            lambda design, name, remedy=None: (sk, None))
        return sk

    def test_a_target_on_a_dxf_export_is_refused_naming_it(self, tmp_path, monkeypatch):
        # _export_dxf is never handed 'target' - the DXF is the named sketch's outline whatever was
        # asked for, so a dropped 'target' writes a file of the wrong geometry with no error.
        _, em, _ = _install(monkeypatch)
        self._sketch(monkeypatch)
        res = dx.handler(format="dxf", dxf_sketch="Profile1", target="CarrierBar",
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "'target' ('CarrierBar')" in res["message"] and "format=dxf" in res["message"]
        assert "dxf_sketch" in res["message"]
        assert em._calls == []                              # nothing was written

    def test_split_by_component_on_a_dxf_export_is_refused(self, tmp_path, monkeypatch):
        # The split walk sits BEHIND the dxf dispatch, so split_by_component=true on a dxf call
        # writes one file from one sketch - the opposite of the per-component set asked for.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        self._sketch(monkeypatch)
        res = dx.handler(format="dxf", dxf_sketch="Profile1", split_by_component=True,
                         file_path=str(tmp_path))
        assert res["isError"] is True
        assert "'split_by_component' (true)" in res["message"] and "format=dxf" in res["message"]
        assert em._calls == []

    def test_include_invisible_bodies_on_a_dxf_export_is_refused_naming_it(self, tmp_path,
                                                                           monkeypatch):
        # The dxf branch never hands the include_invisible_* knobs to its options object (the type
        # does carry an isIncludingInvisible* pair - measured live), so the request is refused
        # rather than answered with a file whose hidden content is a coin toss.
        _, em, _ = _install(monkeypatch)
        self._sketch(monkeypatch)
        res = dx.handler(format="dxf", dxf_sketch="Profile1", include_invisible_bodies=True,
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "'include_invisible_bodies' (true)" in res["message"]
        assert "format=dxf" in res["message"]
        assert em._calls == []

    def test_include_invisible_components_on_a_dxf_export_is_refused_too(self, tmp_path,
                                                                         monkeypatch):
        # The SECOND knob of the pair: a guard narrowed to bodies alone reads as correct against the
        # test above, and goes on dropping the components request.
        _, em, _ = _install(monkeypatch)
        self._sketch(monkeypatch)
        res = dx.handler(format="dxf", dxf_sketch="Profile1", include_invisible_components=True,
                         file_path=str(tmp_path / "p.dxf"))
        assert res["isError"] is True
        assert "'include_invisible_components' (true)" in res["message"]
        assert em._calls == []

    def test_a_dxf_export_that_asked_for_none_of_them_still_writes(self, tmp_path, monkeypatch):
        # THE BOUNDARY. All four guards key on what the CALLER passed: false and "" are the DEFAULTS
        # every dxf call carries, so a guard keyed on the knob existing would refuse every one.
        _, em, _ = _install(monkeypatch)
        sk = self._sketch(monkeypatch)
        out = _payload(dx.handler(format="dxf", dxf_sketch="Profile1", target="",
                                  split_by_component=False, include_invisible_bodies=False,
                                  include_invisible_components=False,
                                  file_path=str(tmp_path / "p.dxf")))
        assert out["exported"] is True
        assert em._calls[-1]["geom"] is sk

    def test_a_target_with_split_by_component_is_refused_naming_it(self, tmp_path, monkeypatch):
        # The split walk exports EVERY top-level occurrence off its own census, never 'target' - so a
        # dropped 'target' hands back a directory of files for parts the caller did not ask for.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        res = dx.handler(format="step", split_by_component=True, target="Cab:1",
                         file_path=str(tmp_path))
        assert res["isError"] is True
        assert "'target' ('Cab:1')" in res["message"]
        assert "split_by_component" in res["message"]
        assert em._calls == []                              # nothing built, nothing written
        assert list(tmp_path.iterdir()) == []              # and no directory of parts on disk


# ── split_by_component (one file per top-level occurrence) ─────────────────────

class TestSplitByComponent:
    def test_one_file_per_occurrence(self, tmp_path, monkeypatch):
        occs = [_occ("Body:1"), _occ("Cab:1"), _occ("Wheels:1")]
        _, em, _ = _install(monkeypatch, occurrences=occs)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["split_by_component"] is True
        assert out["file_count"] == 3
        # each occurrence was the geometry handed to the exporter (one execute per part)
        geoms = [c["geom"].name for c in em._calls]
        assert set(geoms) == {"Body:1", "Cab:1", "Wheels:1"}

    def test_a_BREP_split_writes_each_file_from_the_occurrences_COMPONENT(self, tmp_path,
                                                                          monkeypatch):
        # MEASURED: the BRep-neutral factories RAISE on an Occurrence, so handing them the split's
        # raw occurrences fails EVERY file of the default-format "one file per part" workflow.
        occs = [_occ("Body:1"), _occ("Cab:1")]
        _, em, _ = _install(monkeypatch, occurrences=occs)
        out = _payload(dx.handler(format="step", file_path=str(tmp_path),
                                  split_by_component=True))
        assert out["file_count"] == 2
        assert [c["geom"].name for c in em._calls] == ["Body", "Cab"]     # the COMPONENTS
        assert "written from that occurrence's COMPONENT" in out["note"]

    def test_a_MESH_split_still_writes_each_file_from_the_occurrence(self, tmp_path, monkeypatch):
        # The exact boundary: the reroute is per-FORMAT. stl took an occurrence live, and routing
        # it to the component would silently change what a print job's file holds.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert [c["geom"].name for c in em._calls] == ["Body:1", "Cab:1"]  # the OCCURRENCES
        assert "COMPONENT" not in out["note"]

    def test_split_publishes_the_option_knobs_PER_FILE(self, tmp_path, monkeypatch):
        # The split path configures its own options object PER FILE, so the read-back is per file
        # too - the same applied/requested shape mesh_export publishes. One file's read-back
        # standing in for the rest would report a knob as landed on a file that never read it back.
        occs = [_occ("Body:1"), _occ("Cab:1")]
        _, _em, _ = _install(monkeypatch, occurrences=occs)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True,
                                  stl_binary=True))
        assert out["file_count"] == 2
        # stl_units rides on every STL export, asked for or not - its omitted case inherits a unit
        # from the session rather than defaulting to one, so each file names the unit it was written in
        assert out["options_requested"] == {"stl_binary": True, "stl_units": "mm"}
        assert [f["options_applied"] for f in out["files"]] == [
            {"stl_binary": True, "stl_units": "mm"}, {"stl_binary": True, "stl_units": "mm"}]
        # stl_binary's read is MEASURED to determine the file, so IT carries no evidence key
        assert all("stl_binary" not in (f.get("options_verified") or {}) for f in out["files"])
        # the first-file-representative key and its consistency flag are gone with the shape
        assert "options_applied" not in out and "options_applied_consistent" not in out

    def test_split_publishes_the_unverified_knob_per_file_and_counts_it(self, tmp_path,
                                                                        monkeypatch):
        # The evidence is per file for the same reason the value is: each file got its own options
        # object. Every one of them already read the request and dropped the write, so every file
        # carries false and the note counts the files rather than claiming the knob for them.
        occs = [_occ("Body:1"), _occ("Cab:1")]
        _, em, _ = _install(monkeypatch, occurrences=occs)

        def _deaf(kind, path, geom=None, **kw):
            return _ExportOptions(kind, path, geom, drops=("isIncludingInvisibleBodies",),
                                  seeded={"isIncludingInvisibleBodies": True}, **kw)

        em._options_class = _deaf
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True,
                                  include_invisible_bodies=True))
        assert [f["options_applied"] for f in out["files"]] == [
            {"invisible_bodies": True, "stl_units": "mm"},
            {"invisible_bodies": True, "stl_units": "mm"}]
        assert [f["options_verified"] for f in out["files"]] == [
            {"invisible_bodies": False, "stl_units": True},
            {"invisible_bodies": False, "stl_units": True}]
        assert ("invisible_bodies is set but UNVERIFIED for 2 of the 2 exported file(s)"
                in out["note"])
        assert "did NOT land" not in out["note"]

    def test_a_knob_that_did_not_read_back_is_null_on_that_files_record(self, tmp_path, monkeypatch):
        # An option Fusion ignored must show as null on the FILE it did not land on - an export
        # that silently wrote ASCII while 'stl_binary' was asked for is the false success this
        # catches, and 'options_requested' keeps the request readable beside it.
        occs = [_occ("Body:1"), _occ("Cab:1")]
        _, em, _ = _install(monkeypatch, occurrences=occs)

        def _stubborn(kind, path, geom=None, **kw):
            return _ExportOptions(kind, path, geom, drops=("isBinaryFormat",),
                                  seeded={"isBinaryFormat": True}, **kw)

        em._options_class = _stubborn
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True,
                                  stl_binary=False))
        assert out["options_requested"] == {"stl_binary": False, "stl_units": "mm"}
        assert [f["options_applied"] for f in out["files"]] == [
            {"stl_binary": None, "stl_units": "mm"}, {"stl_binary": None, "stl_units": "mm"}]
        assert "stl_binary did NOT land for 2 of the 2 exported file(s)" in out["note"]
        assert "options_refused" not in out

    def test_one_file_refusing_leaves_the_other_files_read_back_intact(self, tmp_path, monkeypatch):
        # The per-file shape exists for exactly this case: a knob that lands on one file and not on
        # another. A single representative value would report ONE of the two states for both.
        occs = [_occ("Body:1"), _occ("Cab:1")]
        _, em, _ = _install(monkeypatch, occurrences=occs)

        made = {"n": 0}

        def _opt(kind, path, geom=None, **kw):
            made["n"] += 1
            stubborn = made["n"] == 2                          # the SECOND file ignores the set
            rec = _ExportOptions(kind, path, geom,
                                 drops=("isBinaryFormat",) if stubborn else (),
                                 seeded={"isBinaryFormat": False} if stubborn else None, **kw)
            em._calls.append(rec)
            return rec

        monkeypatch.setattr(em, "_opt", _opt)
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path),
                                  split_by_component=True, stl_binary=True))
        assert [f["options_applied"] for f in out["files"]] == [
            {"stl_binary": True, "stl_units": "mm"}, {"stl_binary": None, "stl_units": "mm"}]
        assert "stl_binary did NOT land for 1 of the 2 exported file(s)" in out["note"]

    def test_no_option_requested_publishes_neither_key(self, tmp_path, monkeypatch):
        # Nothing was asked for and this format forces nothing, so there is nothing to state - an
        # empty applied/requested pair on every file record would be noise a caller reads past.
        # Driven on a format with no always-written knob; stl has one (the unit) by design.
        _install(monkeypatch, occurrences=[_occ("Body:1")])
        out = _payload(dx.handler(format="step", file_path=str(tmp_path), split_by_component=True))
        assert "options_requested" not in out
        assert "options_applied" not in out["files"][0]
        assert "did NOT land" not in out["note"]

    def test_a_split_stl_names_its_unit_on_every_file_with_nothing_asked_for(self, tmp_path,
                                                                             monkeypatch):
        # The counterpart, and the one that matters for a print job: split writes one options object
        # PER FILE, so a unit written on the first file only would leave the rest inheriting the
        # session's unit. Every file record must name it, not just the first.
        _install(monkeypatch, occurrences=[_occ("Body:1"), _occ("Cab:1")])
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["options_requested"] == {"stl_units": "mm"}
        assert [f["options_applied"]["stl_units"] for f in out["files"]] == ["mm", "mm"]

    def test_filenames_sanitized_and_extensioned(self, tmp_path, monkeypatch):
        _, _, _ = _install(monkeypatch, occurrences=[_occ("Loader Arm:1")])
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        fp = out["files"][0]["file_path"]
        # ':1' instance suffix dropped, space -> '_', extension applied
        assert fp.replace("\\", "/").endswith("/Loader_Arm.stl")

    def test_duplicate_stems_disambiguated(self, tmp_path, monkeypatch):
        # two instances whose sanitized stem collides must not overwrite each other
        _install(monkeypatch, occurrences=[_occ("Wheel:1"), _occ("Wheel:2")])
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        paths = [f["file_path"] for f in out["files"]]
        assert len(set(paths)) == 2                       # distinct files
        assert any(p.endswith("Wheel.stl") for p in paths)
        assert any(p.endswith("Wheel_2.stl") for p in paths)

    def test_no_occurrences_errors(self, tmp_path, monkeypatch):
        _install(monkeypatch, occurrences=[])
        res = dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True and "no top-level occurrences" in res["message"].lower()

    def test_an_unreadable_occurrence_collection_refuses_as_unread_not_as_empty(
            self, tmp_path, monkeypatch):
        # The census never happened, so the design's components are unknown. Reporting "no
        # top-level occurrences" would state a fact about the design that was never read, and
        # writing zero files would look like a clean export of nothing.
        _design, _em, comp = _install(monkeypatch, occurrences=[_occ("Body:1")])

        comp.occurrences = _NamedCollection(raises="boom")
        res = dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True
        assert "did not read" in res["message"]
        assert "no top-level occurrences" not in res["message"].lower()
        assert list(tmp_path.iterdir()) == []               # and nothing was written

    def test_partial_failure_records_failed_list(self, tmp_path, monkeypatch):
        # one occurrence exports, one fails -> exported=true, file_count counts only the good one,
        # and the failures land under a 'failed' key (not silently dropped).
        good = _occ("Good:1")
        bad = _occ("Bad:1")
        _, em, _ = _install(monkeypatch, occurrences=[good, bad])

        real_exec = em.execute
        def selective(opts):
            if "Bad" in opts["path"]:
                return False               # Fusion declines this one
            return real_exec(opts)
        em.execute = selective

        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["exported"] is True          # at least one succeeded
        assert out["file_count"] == 1
        assert [f["occurrence"] for f in out["files"]] == ["Good:1"]
        assert "failed" in out
        assert out["failed"][0]["occurrence"] == "Bad:1"
        # the shortfall is flagged AND worded - file_count alone reads like a complete export
        assert out["partial"] is True
        assert "PARTIAL" in out["note"] and "1 of 2" in out["note"]

    def test_a_split_that_lands_nothing_is_an_error_carrying_the_reasons(self, tmp_path,
                                                                        monkeypatch):
        # ZERO deliverables is a FAILED export, not an ok payload carrying exported:false - and the
        # refusal names every occurrence that failed, the only place those reasons can travel.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("A:1"), _occ("B:1")])
        em.execute = lambda opts: False
        res = dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True)
        assert res["isError"] is True
        assert "wrote NO files" in res["message"]
        assert "A:1" in res["message"] and "B:1" in res["message"]
        # the per-occurrence reason names BOTH readings: the false bool and the empty disk
        assert "execute() returned false" in res["message"]
        assert "no file was written" in res["message"]

    def test_a_split_file_that_landed_under_a_false_execute_is_kept_and_flagged(self, tmp_path,
                                                                                monkeypatch):
        # The split path reads the same disk: an occurrence whose execute() answered false while
        # its file landed belongs in 'files', flagged, not in 'failed'.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Good:1"), _occ("Quiet:1")])
        real_exec = em.execute

        def false_for_quiet(opts):
            real_exec(opts)
            return "Quiet" not in opts["path"]

        em.execute = false_for_quiet
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["file_count"] == 2 and "failed" not in out
        flagged = [f for f in out["files"] if f.get("execute_returned_false")]
        assert [f["occurrence"] for f in flagged] == ["Quiet:1"]
        assert "FALSE for 1 of the 2 file(s)" in out["note"]

    def test_execute_true_but_no_file_written_is_a_split_failure(self, tmp_path, monkeypatch):
        # execute() lying (True, but nothing landed on disk) must land the occurrence in 'failed',
        # not 'files' - the split path is gated on file existence the same as the single-target path.
        _, em, _ = _install(monkeypatch, occurrences=[_occ("Good:1"), _occ("Ghost:1")])
        real_exec = em.execute

        def lying_execute(opts):
            if "Ghost" in opts["path"]:
                em._executed = opts
                return True   # lies: writes nothing
            return real_exec(opts)

        em.execute = lying_execute
        out = _payload(dx.handler(format="stl", file_path=str(tmp_path), split_by_component=True))
        assert out["file_count"] == 1
        assert [f["occurrence"] for f in out["files"]] == ["Good:1"]
        assert out["partial"] is True
        assert out["failed"][0]["occurrence"] == "Ghost:1"
        assert "no file was written" in out["failed"][0]["error"].lower()


# ── _export_one (per-file write result) ───────────────────────────────────────

class TestExportOne:
    def test_stl_arg_order_is_geom_then_path(self, tmp_path):
        em = FakeExportManager()
        out = str(tmp_path / "out.stl")
        okk, err, applied = dx._export_one(em, "createSTLExportOptions", True, "GEOM", out)
        assert okk is True and err is None and applied == ({}, [], {})
        # STL records (geom, path); the call captured the geometry, not the path, as geom
        assert em._calls[-1]["geom"] == "GEOM" and em._calls[-1]["path"] == out

    def test_non_stl_arg_order_is_path_then_geom(self, tmp_path):
        em = FakeExportManager()
        out = str(tmp_path / "out.step")
        dx._export_one(em, "createSTEPExportOptions", False, "GEOM", out)
        assert em._calls[-1]["geom"] == "GEOM" and em._calls[-1]["path"] == out

    def test_execute_false_is_reported_as_the_bool_not_as_an_error(self, tmp_path):
        # A false execute() is a READING, not a verdict: the caller checks the disk, because a
        # component f3d export returns false over a file that landed.
        em = FakeExportManager()
        em.execute = lambda opts: False
        executed, err, applied = dx._export_one(em, "createSTEPExportOptions", False, "G",
                                                str(tmp_path / "out"))
        assert executed is False and err is None and applied == ({}, [], {})

    def test_exception_captured_as_error_string(self, tmp_path):
        em = FakeExportManager()
        def boom(path, geom=None):
            raise RuntimeError("disk full")
        em.createSTEPExportOptions = boom
        okk, err, applied = dx._export_one(em, "createSTEPExportOptions", False, "G",
                                           str(tmp_path / "out"))
        assert okk is False and "disk full" in err

    def test_configure_callback_runs_before_execute(self, tmp_path):
        em = FakeExportManager()
        seen = {}
        def configure(opts):
            seen["kind"] = opts.kind
            opts.customFlag = True
            return {"custom": True}
        okk, err, applied = dx._export_one(em, "createSTEPExportOptions", False, "G",
                                           str(tmp_path / "out"), configure)
        assert okk is True
        assert seen["kind"] == "step"
        assert em._calls[-1].customFlag is True
        assert applied == {"custom": True}

    def test_configure_never_blocks_a_failed_execute(self, tmp_path):
        # a decorative-option configure step must not stop the real reading from being reported.
        em = FakeExportManager()
        em.execute = lambda opts: False
        executed, err, applied = dx._export_one(em, "createSTEPExportOptions", False, "G",
                                                str(tmp_path / "out"),
                                                lambda opts: {"x": True})
        assert executed is False and err is None


# ── file-existence gate (single-target export) ────────────────────────────────

class TestFileExistenceGate:
    def test_execute_true_but_no_file_written_is_a_failure(self, tmp_path, monkeypatch):
        # execute() returning true is NOT proof a file landed on disk - success is gated on
        # os.path.isfile + a non-zero size.
        _, em, _ = _install(monkeypatch)

        def lying_execute(opts):
            em._executed = opts
            return True   # lies: writes nothing

        em.execute = lying_execute
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True
        assert "no file was written" in res["message"].lower()

    def test_empty_file_is_also_a_failure(self, tmp_path, monkeypatch):
        # a zero-byte file on disk is not a real export either.
        _, em, _ = _install(monkeypatch)
        target = str(tmp_path / "p.step")

        def empty_execute(opts):
            em._executed = opts
            open(opts["path"], "w").close()   # writes an empty file
            return True

        em.execute = empty_execute
        res = dx.handler(format="step", file_path=target)
        assert res["isError"] is True
        assert "no file was written" in res["message"].lower()

    def test_execute_false_over_a_landed_file_is_an_export_that_discloses_the_bool(
            self, tmp_path, monkeypatch):
        # MEASURED on a component f3d export: execute() answers false and a valid archive lands at
        # the path. The disk decides, and the false reading rides on the payload.
        _, em, _ = _install(monkeypatch)
        real_exec = em.execute

        def false_but_writes(opts):
            real_exec(opts)
            return False

        em.execute = false_but_writes
        out = _payload(dx.handler(format="f3d", file_path=str(tmp_path / "p.f3d")))
        assert out["exported"] is True and out["size_bytes"] > 0
        assert out["execute_returned_false"] is True
        assert "FALSE" in out["note"]

    def test_execute_false_with_no_file_is_still_a_failure_naming_the_bool(self, tmp_path,
                                                                          monkeypatch):
        # THE BOUNDARY the disclosure above must not wash away: false AND nothing on disk is the
        # real failure, and the refusal names both readings rather than only the missing file.
        _, em, _ = _install(monkeypatch)
        em.execute = lambda opts: False
        res = dx.handler(format="f3d", file_path=str(tmp_path / "p.f3d"))
        assert res["isError"] is True
        assert "execute() returned false" in res["message"]
        assert "no file was written" in res["message"]

    def test_a_stale_file_from_an_earlier_export_is_not_a_landing(self, tmp_path, monkeypatch):
        # the strongest form of the lie: execute() returns true, writes nothing, and a file of the
        # right name is ALREADY there - an existence-only check reports that old file as this
        # export's deliverable.
        _, em, _ = _install(monkeypatch)
        target = tmp_path / "p.step"
        target.write_text("ISO-10303-21; an export from an earlier call")
        em.execute = lambda opts: True
        res = dx.handler(format="step", file_path=str(target))
        assert res["isError"] is True
        assert "already there before this call" in res["message"]


# ── _resolve_target ordering ──────────────────────────────────────────────────

class TestResolveTargetExtra:
    def test_handle_resolving_to_non_body_is_not_found(self, tmp_path, monkeypatch):
        # a long token that resolves to something that is NOT a BRepBody -> (None) -> handler error
        design, _, _ = _install(monkeypatch, bodies=[BRepBody("Body1")])
        h = "/v" + "Z" * 70
        design._tokens[h] = object()              # not a FakeBody (BRepBody)
        res = dx.handler(format="step", target=h, file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True and "not found" in res["message"].lower()

    def test_no_active_design_errors(self, tmp_path, monkeypatch):
        # design() returns None -> the no-design error, not a crash
        monkeypatch.setattr(dx._common, "design", lambda: None)
        res = dx.handler(format="step", file_path=str(tmp_path / "p.step"))
        assert res["isError"] is True and "no active design" in res["message"].lower()
