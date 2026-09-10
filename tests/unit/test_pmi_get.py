"""Tests for `pmi_get` (rich read over the design's PMI) and the shared `_pmi` substrate it rides:
the router's composition (default light records, include= slices, truncation, geometry narrowing),
the {symbol} markup codec, and find_annotation's ambiguity refusal."""

import json
import math
from types import SimpleNamespace

import pytest

from conftest import (load_tool, error_message, FakePMIHoleThreadNote, FakePMILeaderLineNote,
                      MakeComp, _NamedCollection, make_pmi_tolerance, make_pmi_value)

pg = load_tool("pmi_get")

import adsk.fusion  # noqa: E402  the installed mock - PMISymbolTypes members come from it


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _ann(name="Note1", suffix="PMILeaderLineNote", text="DEBURR", visible=True,
         out_of_date=False, warning="", **extra):
    """One annotation of `suffix`'s kind over the shared note fakes; `extra` seeds the callout
    members a detail slice reads. An IMPORTED suffix carries the leader note's surface under that
    kind label - the imported classes have no shape dump of their own."""
    cls = (FakePMIHoleThreadNote if suffix.endswith("HoleThreadNote")
           else FakePMILeaderLineNote)
    a = cls(name=name, object_type="adsk::fusion::" + suffix, text=text, visible=visible,
            out_of_date=out_of_date, warning=warning)
    for key, value in extra.items():
        setattr(a, key, value)
    return a


def _tol(raw):
    """A symmetric PMIGeometricValueTolerance holding `raw` on both bounds. Each fixture below
    writes a bound in the same number system it writes that bound's value in, so a test can pin
    which conversion the read applies to it."""
    return make_pmi_tolerance(upper=raw, lower=raw)


def _gv(raw, tol=None):
    """A PMIGeometricValue holding `raw`, the unconverted number the read scales."""
    return make_pmi_value(raw, tolerance=tol)


@pytest.fixture
def two_notes(monkeypatch):
    comp = MakeComp("Root")
    anns = [_ann("Note1"), _ann("Hole Note1", suffix="PMIHoleThreadNote", text="QTY",
                 quantity=2, isThrough=True, isThreaded=False,
                 diameter=SimpleNamespace(hasValue=True, value=0.6))]
    monkeypatch.setattr(pg._common, "design", lambda: object())
    monkeypatch.setattr(pg._pmi, "walk_annotations",
                        lambda d, stats=None: iter((comp, a) for a in anns))
    return comp, anns


class TestDefaultSlice:
    def test_default_is_light_records_plus_counts(self, two_notes):
        out = _payload(pg.handler())
        assert out["total"] == 2
        assert out["by_kind"] == {"note": 1, "hole_note": 1}
        recs = out["annotations"]
        assert [r["name"] for r in recs] == ["Note1", "Hole Note1"]
        assert recs[0]["kind"] == "note" and recs[1]["kind"] == "hole_note"
        # heavy slices absent by default
        assert "markup" not in recs[0] and "parametric" not in recs[0] and "diameter" not in recs[1]

    def test_default_note_advertises_the_slices(self, two_notes):
        out = _payload(pg.handler())
        assert "segments" in out["note"] and "detail" in out["note"]

    def test_healthy_records_omit_noise_flags(self, two_notes):
        rec = _payload(pg.handler())["annotations"][0]
        assert "out_of_date" not in rec and "suppressed" not in rec and "warning" not in rec

    def test_out_of_date_and_warning_surface(self, monkeypatch):
        comp = MakeComp("Root")
        bad = _ann("Note9", out_of_date=True, warning="reference lost")
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(comp, bad)]))
        rec = _payload(pg.handler())["annotations"][0]
        assert rec["out_of_date"] is True and rec["warning"] == "reference lost"


class TestSlices:
    def test_segments_slice_adds_markup(self, two_notes, monkeypatch):
        monkeypatch.setattr(pg._pmi, "segments_markup", lambda a: "{flatness}0.05")
        rec = _payload(pg.handler(include=["segments"]))["annotations"][0]
        assert rec["markup"] == "{flatness}0.05"

    def test_detail_slice_scales_hole_numbers_to_units(self, two_notes):
        recs = _payload(pg.handler(include=["detail"], units="mm"))["annotations"]
        hole = recs[1]
        assert hole["diameter"] == {"value": 6.0}      # 0.6 cm -> 6 mm, no override/tolerance
        assert hole["quantity"] == 2 and hole["is_through"] is True

    def test_kind_filter_narrows(self, two_notes):
        out = _payload(pg.handler(kind="hole_note"))
        assert out["total"] == 1 and out["annotations"][0]["kind"] == "hole_note"

    def test_component_filter_narrows(self, monkeypatch):
        c1 = SimpleNamespace(name="A")
        c2 = SimpleNamespace(name="B")
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(c1, _ann("N1")), (c2, _ann("N2"))]))
        out = _payload(pg.handler(component="B"))
        assert [r["name"] for r in out["annotations"]] == ["N2"]

    def test_unknown_include_is_refused(self, two_notes):
        msg = error_message(pg.handler(include=["bogus"]))
        assert "bogus" in msg and "segments" in msg


class TestHoleValueUnits:
    """A hole callout's angle reports degrees and its length reports display units - and each
    tolerance is published through the same conversion as the value it bounds, so the display
    unit never touches an angle bound."""

    @pytest.fixture
    def csink(self, monkeypatch):
        comp = MakeComp("Root")
        ann = _ann("Hole Note1", suffix="PMIHoleThreadNote", text="QTY",
                   countersinkAngle=_gv(math.radians(90), _tol(math.radians(1))),
                   diameter=_gv(0.6, _tol(0.05)))
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(comp, ann)]))
        return ann

    def _hole(self, units):
        return _payload(pg.handler(include=["detail"], units=units))["annotations"][0]

    @pytest.mark.parametrize("units", ["mm", "in"])
    def test_angle_tolerance_is_degrees_whatever_the_length_unit(self, csink, units):
        angle = self._hole(units)["countersink_angle_deg"]
        assert angle["value"] == 90.0
        assert angle["tolerance"]["upper"] == 1.0 and angle["tolerance"]["lower"] == 1.0

    def test_length_tolerance_still_scales_to_the_display_unit(self, csink):
        dia = self._hole("mm")["diameter"]
        assert dia["value"] == 6.0                      # 0.6 cm -> 6 mm
        assert dia["tolerance"]["upper"] == 0.5         # 0.05 cm -> 0.5 mm

    def test_a_value_without_a_tolerance_omits_the_key(self, csink):
        csink.diameter = _gv(0.6)
        assert "tolerance" not in self._hole("mm")["diameter"]


class TestImportedDetail:
    """One per-kind builder per imported class. Each reads names that exist on that class in the
    generated api_surface, and publishes only what it could read."""

    def _rec(self, ann, out_f=10.0):
        rec = {}
        pg._detail_common(rec, ann)
        builder = pg._DETAIL_BY_KIND[pg._pmi.kind_of(ann)]
        builder(rec, ann, out_f)
        return rec

    def test_imported_dimension_publishes_nominal_and_the_angle_relator(self):
        ann = _ann("Dim1", suffix="PMIImportedDimension",
                   nominalDistance=_gv(0.6), shaftTolerance=_tol(0.05),
                   angleRelatorType=0, referencedEntities=[])
        rec = self._rec(ann)
        assert rec["nominal"]["value"] == 6.0
        assert rec["shaft_tolerance"]["upper"] == 0.5
        assert "angle_relator" in rec

    def test_imported_geometric_tolerance_walks_its_datum_frame(self):
        mod = SimpleNamespace(type=0, hasValue=True, value=3)
        dr = SimpleNamespace(referenceDatum=SimpleNamespace(label="A"), modifiers=[mod])
        ann = _ann("GT1", suffix="PMIImportedGeometricTolerance",
                   tolerance=0.05, datumReferences=[dr], referencedEntities=[])
        rec = self._rec(ann)
        assert rec["tolerance"] == 0.5
        assert rec["datum_references"][0]["datum"] == "A"
        assert rec["datum_references"][0]["modifiers"][0]["value"] == 3

    def test_a_datum_reference_with_no_modifiers_omits_the_key(self):
        dr = SimpleNamespace(referenceDatum=SimpleNamespace(label="B"), modifiers=[])
        ann = _ann("GT2", suffix="PMIImportedGeometricTolerance",
                   tolerance=None, datumReferences=[dr], referencedEntities=[])
        rec = self._rec(ann)
        assert rec["datum_references"] == [{"datum": "B"}]
        assert "tolerance" not in rec

    def test_imported_gdt_datum_publishes_its_label_and_scaled_target_lengths(self):
        target = SimpleNamespace(targetId="1", type=0, hasPointTarget=True,
                                 pointTarget=SimpleNamespace(x=0.1, y=0.2, z=0.3),
                                 lengths=[0.5, 1.0])
        ann = _ann("A", suffix="PMIImportedGDTDatum", label="A", datumTargets=[target],
                   referencedEntities=[])
        rec = self._rec(ann)
        assert rec["label"] == "A"
        assert rec["datum_targets"][0]["lengths"] == [5.0, 10.0]
        assert rec["datum_targets"][0]["point"] == {"x": 1.0, "y": 2.0, "z": 3.0}

    def test_imported_note_publishes_its_text_and_the_pmi_it_references(self):
        ann = _ann("N1", suffix="PMIImportedNote", note="SEE SHEET 2",
                   reference=SimpleNamespace(name="Dim1"), referencedEntities=[])
        rec = self._rec(ann)
        assert rec["note_text"] == "SEE SHEET 2" and rec["references_pmi"] == "Dim1"

    def test_an_imported_note_with_no_reference_omits_the_pointer(self):
        ann = _ann("N2", suffix="PMIImportedNote", note="FLAT", reference=None,
                   referencedEntities=[])
        assert "references_pmi" not in self._rec(ann)

    def test_imported_surface_texture_publishes_roughness_limits_and_sub_records(self):
        rough = SimpleNamespace(hasValue=True, value=1.6, parameterType=0, isMaximum=True)
        ann = _ann("ST1", suffix="PMIImportedSurfaceTexture", standard=0, surfaceTextureType=0,
                   laySymbolType=0, hasRoughness=True, roughness=3.2,
                   hasRoughnessLimits=True, minimumRoughness=0.8, maximumRoughness=6.3,
                   machineMethod="GROUND", hasProcessingAllowance=True, processingAllowance=0.2,
                   cutoff=rough, waviness=None, secondaryRoughness=None, tertiaryRoughness=None,
                   referencedEntities=[])
        rec = self._rec(ann)
        assert rec["roughness_um"] == 3.2
        assert rec["roughness_min_um"] == 0.8 and rec["roughness_max_um"] == 6.3
        assert rec["machine_method"] == "GROUND" and rec["processing_allowance"] == 0.2
        assert rec["cutoff"]["value"] == 1.6 and rec["cutoff"]["is_maximum"] is True
        assert "waviness" not in rec

    def test_a_roughness_sub_record_with_no_value_is_omitted(self):
        assert pg._roughness(None) is None
        assert pg._roughness(SimpleNamespace(hasValue=False)) is None

    def test_imported_folder_lists_what_it_contains(self):
        ann = _ann("F1", suffix="PMIImportedFolder",
                   containedPMI=[_ann("Dim1"), _ann("Dim2")], referencedEntities=[])
        assert self._rec(ann)["contains"] == ["Dim1", "Dim2"]

    def test_imported_graphical_gets_the_common_detail_and_no_per_kind_builder(self):
        # imported_graphical is in the kind vocabulary but has no structured payload of its own -
        # the shared detail (health, references, timeline index) is what it carries.
        ann = _ann("G1", suffix="PMIImportedGraphical", isParametric=False,
                   timelineObject=SimpleNamespace(index=4),
                   referencedEntities=[SimpleNamespace(objectType="adsk::fusion::BRepFace")])
        rec = {}
        pg._detail_common(rec, ann)
        assert pg._DETAIL_BY_KIND.get("imported_graphical") is None
        assert rec["timeline_index"] == 4 and rec["referenced_entities"] == ["BRepFace"]

    def test_the_detail_router_adds_the_created_placement_only_for_authored_kinds(self):
        note = _ann("Note1", isParametric=True, referencedEntities=[],
                    annotationTextPoint=SimpleNamespace(x=0.1, y=0.0, z=0.0),
                    horizontalAlignment=0, verticalAlignment=0, isPerpendicularLine=False,
                    leaderLineExtension=0.5, annotationPlaneType=0,
                    supportedAnnotationPlaneTypes=[])
        rec = {"kind": "note"}
        pg._slice_detail(rec, note, 10.0)
        assert rec["text_point"]["x"] == 1.0 and rec["leader_extension"] == 5.0
        imported = _ann("Dim1", suffix="PMIImportedDimension", nominalDistance=None,
                        shaftTolerance=None, angleRelatorType=0, referencedEntities=[])
        rec2 = {"kind": "imported_dimension"}
        pg._slice_detail(rec2, imported, 10.0)
        assert "leader_extension" not in rec2 and "align" not in rec2


class TestDetailBuildersReadRealMembers:
    """Every name a per-kind builder reads off its annotation - and every name the leaf walks read
    off the collection/record objects those annotations hand back - exists on that class in the
    generated api_surface. A SWIG proxy answers an unknown name with an AttributeError at runtime
    and nothing else catches the typo, so this is where a misspelled read shows up."""

    _BUILDER_CLASS = {
        "_detail_note": "fusion.PMILeaderLineNote",
        "_detail_hole_note": "fusion.PMIHoleThreadNote",
        "_detail_imported_dimension": "fusion.PMIImportedDimension",
        "_detail_imported_geometric_tolerance": "fusion.PMIImportedGeometricTolerance",
        "_detail_imported_gdt_datum": "fusion.PMIImportedGDTDatum",
        "_detail_imported_note": "fusion.PMIImportedNote",
        "_detail_imported_surface_texture": "fusion.PMIImportedSurfaceTexture",
        "_detail_imported_folder": "fusion.PMIImportedFolder",
        "_detail_created": "fusion.PMILeaderLineNote",
        "_detail_common": "fusion.PMILeaderLineNote",
    }

    # The leaf records reached only by walking one of the above - no create* factory returns them,
    # so they earn their api_surface row through gen_api_surface's explicit class list.
    _LEAF_READS = {
        "fusion.PMIAnnotations": ["itemsByEntities", "leaderLineNotes", "holeThreadNotes",
                                  "count", "item"],
        "fusion.PMILeaderLineNotes": ["createInput", "add"],
        "fusion.PMIHoleThreadNotes": ["createInput", "add"],
        "fusion.PMIDatumReference": ["referenceDatum", "modifiers"],
        "fusion.PMIDatumModifier": ["type", "hasValue", "value"],
        "fusion.PMIDatumTarget": ["targetId", "type", "hasPointTarget", "pointTarget", "lengths"],
        "fusion.PMIRoughness": ["hasValue", "value", "parameterType", "isMaximum"],
    }

    def _reads_on_ann(self, func_name):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(getattr(pg, func_name)))
        return {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "ann"}

    @pytest.mark.parametrize("func_name", sorted(_BUILDER_CLASS))
    def test_every_annotation_read_exists_on_the_class(self, func_name):
        import api_surface
        cls = self._BUILDER_CLASS[func_name]
        members = set(api_surface.PROPERTIES[cls])
        reads = self._reads_on_ann(func_name)
        assert reads, f"{func_name} reads nothing off 'ann' - the scan has broken"
        assert reads <= members, f"{func_name} reads {sorted(reads - members)} - absent on {cls}"

    @pytest.mark.parametrize("cls", sorted(_LEAF_READS))
    def test_every_collection_and_leaf_read_exists_on_its_class(self, cls):
        import api_surface
        members = set(api_surface.PROPERTIES[cls])
        missing = [n for n in self._LEAF_READS[cls] if n not in members]
        assert not missing, f"{cls} has no {missing} - the pmi walks read names it does not define"


class TestHoleFlagReads:
    def test_an_unreadable_is_hole_omits_the_key_instead_of_claiming_true(self, monkeypatch):
        # A default of True publishes "this callout annotates a hole" as though it were measured.
        comp = MakeComp("Root")
        ann = _ann("Hole Note1", suffix="PMIHoleThreadNote")
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(comp, ann)]))
        rec = _payload(pg.handler(include=["detail"]))["annotations"][0]
        assert "is_hole" not in rec

    def test_a_readable_is_hole_false_is_published(self, monkeypatch):
        comp = MakeComp("Root")
        ann = _ann("Hole Note1", suffix="PMIHoleThreadNote", isHoleAnnotation=False)
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(comp, ann)]))
        rec = _payload(pg.handler(include=["detail"]))["annotations"][0]
        assert rec["is_hole"] is False


class TestRowCap:
    """pmi_get's OWN page-cap contract, pinned exactly as the docstring states it - an unusable
    request falls back to the default, everything else is held inside 1..cap."""

    def test_an_over_cap_request_is_clamped_not_refused(self):
        assert pg._row_cap(pg._MAX_RESULTS_CAP + 500) == pg._MAX_RESULTS_CAP

    def test_zero_and_absent_and_junk_fall_back_to_the_default(self):
        assert pg._row_cap(0) == pg._MAX_RESULTS_DEFAULT
        assert pg._row_cap(None) == pg._MAX_RESULTS_DEFAULT
        assert pg._row_cap("lots") == pg._MAX_RESULTS_DEFAULT

    def test_a_negative_request_clamps_to_one(self):
        # NOT the default: a negative number parses, so it goes through the 1..cap clamp rather
        # than the unparseable fallback. The fleet's other capped reads do not all agree here,
        # which is why the docstring claims this contract for pmi_get alone.
        assert pg._row_cap(-5) == 1
        assert pg._row_cap(-1) == 1

    def test_an_in_range_request_is_honoured_exactly(self):
        assert pg._row_cap(3) == 3

    def test_an_over_cap_request_is_not_refused_end_to_end(self, monkeypatch):
        comp = MakeComp("Root")
        anns = [_ann(f"N{i}") for i in range(3)]
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter((comp, a) for a in anns))
        out = _payload(pg.handler(max_results=99999))          # an error() here would raise
        assert out["total"] == 3 and len(out["annotations"]) == 3


class TestBoundsAndGuards:
    def test_truncates_at_max_results_but_counts_all(self, monkeypatch):
        comp = MakeComp("Root")
        anns = [_ann(f"N{i}") for i in range(5)]
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter((comp, a) for a in anns))
        out = _payload(pg.handler(max_results=2))
        assert out["total"] == 5 and len(out["annotations"]) == 2 and out["truncated"] is True

    @pytest.mark.parametrize("raw", [None, "", [], "segments", "SEGMENTS, Detail",
                                     ["Detail"], ["segments", "detail"], "  detail  "])
    def test_include_normalizes_exactly_like_its_sibling_reads(self, raw):
        # Seven <domain>_get reads carry this same normalization; a divergent copy here means an
        # include= form that works against design_get silently does something else against pmi_get.
        sibling = load_tool("design_get")
        assert pg._normalize_include(raw) == sibling._normalize_include(raw)

    def test_a_comma_string_include_is_split_and_lowercased(self, two_notes, monkeypatch):
        monkeypatch.setattr(pg._pmi, "segments_markup", lambda a: "{flatness}0.05")
        out = _payload(pg.handler(include=" SEGMENTS "))
        assert out["annotations"][0]["markup"] == "{flatness}0.05"

    def test_an_empty_include_is_the_default_slice_not_an_unknown_slice(self, two_notes):
        for empty in ("", [], None):
            out = _payload(pg.handler(include=empty))
            assert "markup" not in out["annotations"][0]
            assert "segments" in out["note"]

    def test_no_active_design_is_an_error(self, monkeypatch):
        monkeypatch.setattr(pg._common, "design", lambda: None)
        assert "No active design" in error_message(pg.handler())

    def test_bad_units_refused(self, two_notes):
        assert "units" in error_message(pg.handler(units="furlong"))

    def test_geometry_resolve_error_surfaces(self, two_notes, monkeypatch):
        monkeypatch.setattr(pg._GEOMETRY, "resolve", lambda raw: (None, "stale handle"))
        assert "stale handle" in error_message(pg.handler(geometry=["h1"]))

    def test_geometry_narrows_via_items_by_entities(self, monkeypatch):
        keep = _ann("Kept")
        drop = _ann("Dropped")
        coll = SimpleNamespace(itemsByEntities=lambda ents: [keep])
        comp = SimpleNamespace(name="Root", pmiAnnotations=coll)
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._common, "all_components", lambda d: [comp])
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter([(comp, keep), (comp, drop)]))
        monkeypatch.setattr(pg._GEOMETRY, "resolve", lambda raw: ([object()], None))
        out = _payload(pg.handler(geometry=["h1"]))
        assert [r["name"] for r in out["annotations"]] == ["Kept"] and out["total"] == 1


class TestGeometryFilterIdentity:
    """geometry= intersects by (component, name), never by object identity - so the payload SAYS
    so, rather than leaving a caller to assume the match named one annotation."""

    def _rig(self, monkeypatch, walk, matched):
        coll = SimpleNamespace(itemsByEntities=lambda ents: matched)
        comp = SimpleNamespace(name="Root", pmiAnnotations=coll)
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._common, "all_components", lambda d: [comp])
        monkeypatch.setattr(pg._pmi, "walk_annotations",
                            lambda d, stats=None: iter((comp, a) for a in walk))
        monkeypatch.setattr(pg._GEOMETRY, "resolve", lambda raw: ([object()], None))
        return comp

    def test_the_name_keyed_identity_is_disclosed_when_geometry_filters(self, monkeypatch):
        keep = _ann("Kept")
        self._rig(monkeypatch, [keep], [keep])
        out = _payload(pg.handler(geometry=["h1"]))
        assert out["filter_identity"] == "component+name"
        assert "identity" in out["filter_note"] and "same name" in out["filter_note"]

    def test_a_twin_name_in_one_component_rides_along_and_the_payload_says_why(self, monkeypatch):
        # the behaviour the disclosure exists for: only ONE of the two matched the geometry, and
        # both are published because the key is the name
        matched, twin = _ann("Twin"), _ann("Twin")
        self._rig(monkeypatch, [matched, twin], [matched])
        out = _payload(pg.handler(geometry=["h1"]))
        assert out["total"] == 2 and [r["name"] for r in out["annotations"]] == ["Twin", "Twin"]
        assert out["filter_identity"] == "component+name"

    def test_an_unfiltered_read_makes_no_identity_claim(self, monkeypatch):
        # the boundary: no geometry= means no name-keyed intersection ran, so neither key appears
        self._rig(monkeypatch, [_ann("Kept")], [])
        out = _payload(pg.handler())
        assert "filter_identity" not in out and "filter_note" not in out


class TestWalkHolesArePublished:
    """total/by_kind count what the walk could READ - a component or item it could not read is
    published beside them, so a partial design is never handed over as the whole one."""

    def _rig(self, monkeypatch, holes):
        comp = MakeComp("Root")
        anns = [_ann("N1"), _ann("N2")]

        def walk(d, stats=None):
            if stats is not None:
                stats["components_unreadable"] = holes[0]
                stats["items_unreadable"] = holes[1]
            return iter((comp, a) for a in anns)
        monkeypatch.setattr(pg._common, "design", lambda: object())
        monkeypatch.setattr(pg._pmi, "walk_annotations", walk)

    def test_unreadable_components_and_items_ride_beside_the_tallies(self, monkeypatch):
        self._rig(monkeypatch, (2, 3))
        out = _payload(pg.handler())
        assert out["total"] == 2
        assert out["components_unreadable"] == 2 and out["items_unreadable"] == 3
        assert "may hold more PMI" in out["incomplete_note"]

    def test_a_single_unreadable_item_is_enough_to_disclose(self, monkeypatch):
        # the exact boundary against the clean walk below: one hole, not zero
        self._rig(monkeypatch, (0, 1))
        out = _payload(pg.handler())
        assert out["components_unreadable"] == 0 and out["items_unreadable"] == 1

    def test_a_clean_walk_publishes_no_incompleteness_keys(self, monkeypatch):
        self._rig(monkeypatch, (0, 0))
        out = _payload(pg.handler())
        assert "components_unreadable" not in out and "incomplete_note" not in out

    def test_the_hole_keys_are_not_the_row_cap_truncation(self, monkeypatch):
        # 'truncated' is the cap biting; the hole keys are reads that failed. A payload that
        # conflated them would let a capped page read as an unreadable design.
        self._rig(monkeypatch, (0, 0))
        out = _payload(pg.handler(max_results=1))
        assert out["truncated"] is True and "components_unreadable" not in out


# ── the shared _pmi substrate (codec + resolver), reached through this module's import ────────────

class TestMarkupCodec:
    def test_unknown_token_error_lists_the_vocabulary(self):
        segs, err = pg._pmi.build_segments("{bogus}0.05")
        assert segs is None and "'{bogus}'" in err and "flatness" in err and "mmc" in err

    def test_empty_text_is_refused(self):
        segs, err = pg._pmi.build_segments("")
        assert segs is None and "empty" in err

    def test_symbol_text_and_linebreak_segment_counts(self):
        segs, err = pg._pmi.build_segments("{flatness}0.05")
        assert err is None and len(segs) == 2       # symbol + text
        segs, err = pg._pmi.build_segments("A\nB")
        assert err is None and len(segs) == 3       # text + break + text

    def test_markup_round_trips_through_segments(self):
        sym = SimpleNamespace(objectType="adsk::fusion::PMISymbolSegment",
                              pmiSymbolType=adsk.fusion.PMISymbolTypes.FlatnessPMISymbolType)
        txt = SimpleNamespace(objectType="adsk::fusion::PMITextSegment", text="0.05")
        ann = SimpleNamespace(segments=[sym, txt])
        assert pg._pmi.segments_markup(ann) == "{flatness}0.05"


class TestFindAnnotation:
    def _design(self, monkeypatch, comps):
        monkeypatch.setattr(pg._pmi._common, "all_components", lambda d: comps)
        return object()

    def test_ambiguous_across_components_is_refused_naming_each(self, monkeypatch):
        c1 = SimpleNamespace(name="A", pmiAnnotations=_NamedCollection([_ann("Note1")]))
        c2 = SimpleNamespace(name="B", pmiAnnotations=_NamedCollection([_ann("Note1")]))
        d = self._design(monkeypatch, [c1, c2])
        ann, comp, err = pg._pmi.find_annotation(d, "Note1")
        assert ann is None and "2 components" in err and "A" in err and "B" in err

    def test_component_scope_disambiguates(self, monkeypatch):
        c1 = SimpleNamespace(name="A", pmiAnnotations=_NamedCollection([_ann("Note1")]))
        c2 = SimpleNamespace(name="B", pmiAnnotations=_NamedCollection([_ann("Note1")]))
        d = self._design(monkeypatch, [c1, c2])
        ann, comp, err = pg._pmi.find_annotation(d, "note1", component="B")
        assert err is None and comp is c2

    def test_miss_lists_available_names(self, monkeypatch):
        c1 = SimpleNamespace(name="A", pmiAnnotations=_NamedCollection([_ann("Note1")]))
        d = self._design(monkeypatch, [c1])
        ann, comp, err = pg._pmi.find_annotation(d, "Nope")
        assert ann is None and "Note1" in err
