"""Unit tests for ``doc_insert_occurrence.py`` placement transform + occurrence resolution.

The cloud URN resolution needs live data; these pin the placement/orientation logic (x/y/z scales
to cm, rotate_deg builds a rotation, bad units/axis are rejected), plus into_component/
remove_existing resolution via the shared OccurrenceRef kind (fullPathName/name-preferring,
ambiguity REFUSED - never the wrong instance). The DataFile resolution is monkeypatched so the
test stays offline; occurrence resolution goes through the real _inputs kind against a fake design
(dual seam: both io._common.design and io._inputs._common.design point at the same fake).
"""

import json

from conftest import (FakeApplication, FakeData, FakeDataFile, FakeDocumentReference,
                      FakeFusionDocument, FakeMatrix3D, FakePoint, FakeVector3D, MakeComp,
                      MakeDesign, _NamedCollection, load_tool, make_occurrence)

io = load_tool("doc_insert_occurrence")


class FakeOccurrences(_NamedCollection):
    """component.occurrences as an insert reaches it: the shared walk plus addByInsert, which
    records the transform and the as-reference flag and answers the occurrence `insert_result`
    names, 'default' being an inserted reference that reads back valid."""
    def __init__(self):
        super().__init__()
        self.last_transform = None
        self.last_as_ref = None
        self.insert_result = "default"

    def addByInsert(self, data_file, transform, as_ref):
        self.last_transform = transform
        self.last_as_ref = as_ref
        if self.insert_result == "default":
            return make_occurrence(path="Part:1", referenced=as_ref)
        return self.insert_result


def FakeComp(name="Root"):
    """A component whose occurrences collection is the insert seam."""
    comp = MakeComp(name)
    comp.occurrences = FakeOccurrences()
    return comp


def FakeOcc(name, component=None, full_path=None, delete_returns=True):
    """One assembly occurrence: name + fullPathName + .component + deleteMe()."""
    return make_occurrence(path=full_path or name, component=component,
                           delete_ok=delete_returns)


def _install(monkeypatch, occurrences=(), root_comp=None, data_file=None, host_document=None):
    """Point BOTH design seams (the tool's and the shared _inputs resolver's) at one fake design.
    `host_document` is the design's parent Document, which carries the documentReferences an insert
    is read back through; `data_file` is the source the patched resolve hands back."""
    root_comp = root_comp or FakeComp("Root")
    root_comp.allOccurrences = list(occurrences)
    design = (MakeDesign(comp=root_comp) if host_document is None
              else MakeDesign(comp=root_comp, parent_document=host_document))
    source = FakeDataFile("Part") if data_file is None else data_file
    monkeypatch.setattr(io._common, "design", lambda: design)
    monkeypatch.setattr(io._inputs._common, "design", lambda: design)
    monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (source, raw, [raw]))
    import adsk.core
    monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(FakeMatrix3D))
    monkeypatch.setattr(adsk.core.Vector3D, "create",
                        staticmethod(lambda x, y, z: FakeVector3D(x, y, z)))
    monkeypatch.setattr(adsk.core.Point3D, "create",
                        staticmethod(lambda x, y, z: FakePoint(x, y, z)))
    return design, root_comp


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


class TestPlacement:
    def test_default_identity(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        _payload(io.handler(document_id="urn:x"))
        m = root_comp.occurrences.last_transform
        assert m.asArray() == FakeMatrix3D().asArray()   # nothing placed, nothing rotated

    def test_invalid_inserted_occurrence_bites(self, monkeypatch):
        # addByInsert returns an occurrence object that reads isValid=false -> error, not ok
        design, root_comp = _install(monkeypatch)
        root_comp.occurrences.insert_result = make_occurrence(path="Part:1", valid=False)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "isValid=false" in res["message"]

    def test_position_scales_to_cm(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x", x=10, y=0, z=5, units="mm"))
        t = root_comp.occurrences.last_transform.translation
        assert abs(t.x - 1.0) < 1e-9 and abs(t.z - 0.5) < 1e-9
        assert out["placed_at"]["x"] == 10

    def test_rotation_built(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x", rotate_deg=90, rotate_axis="y"))
        # setToRotation takes RADIANS about world Y at the origin: +90 deg there sends +X to -Z.
        # Degrees passed through unconverted, another axis, or a pivot off the origin all land the
        # vector somewhere else.
        m = root_comp.occurrences.last_transform
        assert [round(v, 6) for v in m._apply_vector(1.0, 0.0, 0.0)] == [0.0, 0.0, -1.0]
        assert [round(v, 6) for v in m._apply_point(0.0, 0.0, 0.0)] == [0.0, 0.0, 0.0]
        assert out["rotate_deg"] == 90

    def test_bad_units(self, monkeypatch):
        _install(monkeypatch)
        res = io.handler(document_id="urn:x", x=5, units="furlongs")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_bad_rotate_axis(self, monkeypatch):
        _install(monkeypatch)
        res = io.handler(document_id="urn:x", rotate_deg=45, rotate_axis="w")
        assert res["isError"] is True and "rotate_axis" in res["message"]


class TestSavedVersionWireSentence:
    def test_result_note_states_it_reads_the_last_saved_version(self, monkeypatch):
        # insert brings in the source's last SAVED cloud version, not live in-session edits.
        design, root_comp = _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert "last SAVED cloud version" in out["note"]

    def test_description_states_the_saved_version_rule(self):
        assert "last SAVED cloud version" in io.TOOL_DESCRIPTION


class TestTheVersionTheReferenceBound:
    """A fresh insert bound its source at v2 while that source's stream had reached v3, so the
    reference read out of date on the very next walk. The version the reference HOLDS is read off
    the reference itself and published beside the source's tip - a caller may not assume a fresh
    insert is current."""

    def _inserted(self, monkeypatch, bound, latest, lineage="urn:src"):
        source = FakeDataFile("Part", file_id=lineage, version=latest, latest_version=latest)
        host = FakeFusionDocument(
            name="Host", references=[FakeDocumentReference(data_file=source, version=bound)])
        _install(monkeypatch, data_file=source, host_document=host)
        return _payload(io.handler(document_id=lineage))

    def test_a_reference_behind_the_source_tip_is_published_as_not_the_tip(self, monkeypatch):
        out = self._inserted(monkeypatch, bound=2, latest=3)
        assert out["bound_version"] == 2
        assert out["source_latest_version"] == 3
        assert out["bound_is_tip"] is False
        assert "bound_is_tip" in out["note"] and "doc_update_xref" in out["note"]

    def test_a_reference_on_the_source_tip_reads_as_the_tip(self, monkeypatch):
        # the other side of the same equality: equal numbers are current, not merely 'not stale'.
        out = self._inserted(monkeypatch, bound=3, latest=3)
        assert out["bound_is_tip"] is True

    def test_a_version_that_does_not_read_is_null_never_false(self, monkeypatch):
        # no reference row answers here, so the bound version is UNKNOWN - published as false it
        # would read as a stale insert the walk never saw.
        _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["bound_version"] is None
        assert out["bound_is_tip"] is None

    def test_a_reference_id_carrying_a_version_suffix_still_matches_its_source(self, monkeypatch):
        # the id is matched on its LINEAGE key, as every other reference walk here matches. A
        # '?version=' suffix was NOT seen on this path; raw equality would miss one if it appeared.
        source = FakeDataFile("Part", file_id="urn:src", version=3, latest_version=3)
        suffixed = FakeDataFile("Part", file_id="urn:src?version=2", version=2, latest_version=3)
        host = FakeFusionDocument(
            name="Host", references=[FakeDocumentReference(data_file=suffixed, version=2)])
        _install(monkeypatch, data_file=source, host_document=host)
        out = _payload(io.handler(document_id="urn:src"))
        assert out["bound_version"] == 2
        assert out["bound_is_tip"] is False

    def test_a_source_id_that_did_not_read_matches_nothing(self, monkeypatch):
        # an unread id keys to '' - and so does every unread REFERENCE id, so a bare key compare
        # would bind this insert to whatever other reference also failed to read.
        source = FakeDataFile("Part", file_id=None, version=3, latest_version=3)
        other = FakeDataFile("Other", file_id=None, version=7, latest_version=7)
        host = FakeFusionDocument(
            name="Host", references=[FakeDocumentReference(data_file=other, version=7)])
        _install(monkeypatch, data_file=source, host_document=host)
        out = _payload(io.handler(document_id="urn:src"))
        assert out["bound_version"] is None
        assert out["bound_is_tip"] is None

    def test_a_matched_row_whose_version_did_not_read_is_unknown_not_agreement(self, monkeypatch):
        # two references to ONE source, one of them unreadable: {2, None} is not "bound at 2" - the
        # unread row could hold any version, so the answer is UNKNOWN.
        source = FakeDataFile("Part", file_id="urn:src", version=3, latest_version=3)
        host = FakeFusionDocument(name="Host", references=[
            FakeDocumentReference(data_file=source, version=2),
            FakeDocumentReference(data_file=source, version=None)])
        _install(monkeypatch, data_file=source, host_document=host)
        out = _payload(io.handler(document_id="urn:src"))
        assert out["bound_version"] is None
        assert out["bound_is_tip"] is None

    def test_two_references_at_two_versions_have_no_single_answer(self, monkeypatch):
        # one source can be referenced twice; two rows disagreeing is not one bound version, and
        # picking either would name a version the other reference does not hold.
        source = FakeDataFile("Part", file_id="urn:src", version=3, latest_version=3)
        host = FakeFusionDocument(name="Host", references=[
            FakeDocumentReference(data_file=source, version=2),
            FakeDocumentReference(data_file=source, version=3)])
        _install(monkeypatch, data_file=source, host_document=host)
        out = _payload(io.handler(document_id="urn:src"))
        assert out["bound_version"] is None and out["bound_is_tip"] is None


class TestAlwaysReference:
    """The tool inserts ONLY a live external reference - never an embedded copy."""

    def test_inserts_as_reference(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert root_comp.occurrences.last_as_ref is True
        assert out["is_reference"] is True

    def test_embedded_result_bites(self, monkeypatch):
        # addByInsert hands back an occurrence that came in embedded (no link) -> error, not a
        # false ok: the tool promises a reference, so a severed copy is a silent failure.
        design, root_comp = _install(monkeypatch)
        root_comp.occurrences.insert_result = type(
            "Embedded", (), {"name": "Part:1", "isValid": True, "isReferencedComponent": False})()
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "not an external reference" in res["message"].lower()


# ── _resolve_data_file: URN / web-URL identifier resolution ───────────────────

import base64


def _set_data(monkeypatch, known):
    """app.data.findFileById answers a DataFile only for the ids in `known`."""
    data = FakeData(files_by_id=known)
    monkeypatch.setattr(io._data_common, "app", FakeApplication(data=data))
    return data


class TestResolveDataFile:
    def test_plain_urn_resolves_directly(self, monkeypatch):
        df = object()
        _set_data(monkeypatch, {"urn:adsk.wipprod:dm.lineage:abc": df})
        got, resolved, candidates = io._resolve_data_file("urn:adsk.wipprod:dm.lineage:abc")
        assert got is df and resolved == "urn:adsk.wipprod:dm.lineage:abc"

    def test_urn_extracted_from_surrounding_text(self, monkeypatch):
        df = object()
        urn = "urn:adsk.wipprod:dm.lineage:xYz123"
        _set_data(monkeypatch, {urn: df})
        # raw isn't itself a known id, but the embedded urn:adsk... token is
        got, resolved, candidates = io._resolve_data_file(f"some text {urn} trailing")
        assert got is df and resolved == urn

    def test_web_url_base64_segment_decoded(self, monkeypatch):
        urn = "urn:adsk.wipprod:dm.lineage:Zb64Decoded"
        # build a base64url segment that decodes to the urn (as a Fusion web URL embeds it)
        seg = base64.b64encode(urn.encode()).decode().replace('+', '-').replace('/', '_').rstrip('=')
        df = object()
        _set_data(monkeypatch, {urn: df})
        url = f"https://myhub.autodesk360.com/g/data/{seg}/something"
        got, resolved, candidates = io._resolve_data_file(url)
        assert got is df and resolved == urn

    def test_unresolvable_returns_none(self, monkeypatch):
        _set_data(monkeypatch, {})
        got, resolved, candidates = io._resolve_data_file("urn:adsk.nope:1")
        assert got is None and resolved is None


# ── into_component / remove_existing via the shared OccurrenceRef kind ────────

class TestIntoComponent:
    def test_empty_uses_root(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        out = _payload(io.handler(document_id="urn:x"))
        assert out["into_component"] == "root component"

    def test_named_occurrence_resolves_its_component(self, monkeypatch):
        chassis_comp = FakeComp("Chassis")
        occ = FakeOcc("Chassis:1", component=chassis_comp)
        design, root_comp = _install(monkeypatch, occurrences=[occ])
        out = _payload(io.handler(document_id="urn:x", into_component="Chassis:1"))
        assert "Chassis" in out["into_component"]
        assert chassis_comp.occurrences.last_transform is not None

    def test_unknown_occurrence_errors(self, monkeypatch):
        _install(monkeypatch, occurrences=[FakeOcc("A:1", component=FakeComp("A"))])
        res = io.handler(document_id="urn:x", into_component="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_ambiguous_into_component_refused_not_first_match(self, monkeypatch):
        # two instances share local name "Bolt:1" under different sub-assemblies — a bare "Bolt"
        # substring must ERROR (naming both fullPathNames), NOT silently grab the first.
        a = FakeOcc("Bolt:1", component=FakeComp("Bolt"), full_path="Sub-A:1+Bolt:1")
        b = FakeOcc("Bolt:1", component=FakeComp("Bolt"), full_path="Sub-B:1+Bolt:1")
        _install(monkeypatch, occurrences=[a, b])
        res = io.handler(document_id="urn:x", into_component="Bolt")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert "Sub-A:1+Bolt:1" in res["message"] and "Sub-B:1+Bolt:1" in res["message"]


class TestRemoveExisting:
    def test_missing_errors(self, monkeypatch):
        _install(monkeypatch)
        res = io.handler(document_id="urn:x", remove_existing="OldPart")
        assert res["isError"] is True and "OldPart" in res["message"]

    def test_removes_then_inserts(self, monkeypatch):
        old = FakeOcc("OldPart:1", component=FakeComp("OldPart"))
        design, root_comp = _install(monkeypatch, occurrences=[old])
        out = _payload(io.handler(document_id="urn:x", remove_existing="OldPart:1"))
        assert old._deleted is True
        assert out["removed_occurrence"] == "OldPart:1"

    def test_removal_note_carries_only_the_measured_side_effect(self, monkeypatch):
        # a removal's blast radius is stated from measurement: a feature that referenced the
        # occurrence's geometry STAYS in the timeline with reference failures. The CAM-selection
        # claim was measured FALSE (the operation's selection survives valid and unwarned), so no
        # wording may re-assert it.
        old = FakeOcc("OldPart:1", component=FakeComp("OldPart"))
        _install(monkeypatch, occurrences=[old])
        out = _payload(io.handler(document_id="urn:x", remove_existing="OldPart:1"))
        note = out["note"]
        assert "referenced its geometry carrying reference failures" in note
        assert "CAM" not in note and "stripped" not in note

    def test_no_removal_note_when_nothing_was_removed(self, monkeypatch):
        # the sentence is about a removal; a plain insert must not claim one happened
        _install(monkeypatch, occurrences=[])
        out = _payload(io.handler(document_id="urn:x"))
        assert out["removed_occurrence"] is None

    def test_delete_returns_false_errors(self, monkeypatch):
        old = FakeOcc("OldPart:1", component=FakeComp("OldPart"), delete_returns=False)
        _install(monkeypatch, occurrences=[old])
        res = io.handler(document_id="urn:x", remove_existing="OldPart:1")
        assert res["isError"] is True and "OldPart:1" in res["message"]

    def test_ambiguous_remove_existing_refused(self, monkeypatch):
        a = FakeOcc("Wheel:1", component=FakeComp("Wheel"), full_path="Sub-A:1+Wheel:1")
        b = FakeOcc("Wheel:1", component=FakeComp("Wheel"), full_path="Sub-B:1+Wheel:1")
        _install(monkeypatch, occurrences=[a, b])
        res = io.handler(document_id="urn:x", remove_existing="Wheel")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert a._deleted is False and b._deleted is False

    def test_an_insert_failure_after_the_removal_discloses_the_part_is_gone(self, monkeypatch):
        # remove_existing DELETES before addByInsert runs: a failure after that point must say the
        # removed occurrence is already gone, or the caller retries into an assembly missing a part
        # it believes is still there.
        old = FakeOcc("OldPart:1", component=FakeComp("OldPart"))
        design, root_comp = _install(monkeypatch, occurrences=[old])
        root_comp.occurrences.insert_result = None       # addByInsert returns nothing
        res = io.handler(document_id="urn:x", remove_existing="OldPart:1")
        assert res["isError"] is True
        assert old._deleted is True
        assert "OldPart:1" in res["message"] and "ALREADY REMOVED" in res["message"]

    def test_a_clean_failure_before_any_removal_makes_no_removed_claim(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        root_comp.occurrences.insert_result = None
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True
        assert "ALREADY REMOVED" not in res["message"]

    def test_bad_placement_inputs_refuse_before_the_removal_deletes_anything(self, monkeypatch):
        # placement validation runs BEFORE the destructive removal - a typo in units/axis must not
        # cost the caller a part.
        old = FakeOcc("OldPart:1", component=FakeComp("OldPart"))
        _install(monkeypatch, occurrences=[old])
        res = io.handler(document_id="urn:x", remove_existing="OldPart:1",
                         x=5, units="furlongs")
        assert res["isError"] is True
        assert old._deleted is False
        res = io.handler(document_id="urn:x", remove_existing="OldPart:1",
                         rotate_deg=45, rotate_axis="w")
        assert res["isError"] is True
        assert old._deleted is False

    def test_a_refused_setToRotation_bool_is_an_error_not_an_unrotated_insert(self, monkeypatch):
        # the live API answers a bool: a false means the rotation never landed on the matrix, so
        # the occurrence would insert UNROTATED while the payload echoed rotate_deg
        design, root_comp = _install(monkeypatch)

        class RefusingMatrix(FakeMatrix3D):
            """The platform declining the rotation - the bool a caller must gate on."""
            def setToRotation(self, angle, axis, origin):
                return False

        import adsk.core
        monkeypatch.setattr(adsk.core.Matrix3D, "create", staticmethod(RefusingMatrix))
        res = io.handler(document_id="urn:x", rotate_deg=90, rotate_axis="y")
        assert res["isError"] is True
        assert "setToRotation" in res["message"]
        assert root_comp.occurrences.last_transform is None   # nothing was inserted


# ── handler error gates that don't reach placement ────────────────────────────

class TestHandlerGates:
    def test_empty_document_id_errors(self):
        res = io.handler(document_id="")
        assert res["isError"] is True and "document_id" in res["message"]

    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(io._common, "design", lambda: None)
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "No active design" in res["message"]

    def test_unresolvable_document_errors(self, monkeypatch):
        monkeypatch.setattr(io._common, "design", lambda: object())
        monkeypatch.setattr(io, "_resolve_data_file", lambda raw: (None, None, []))
        res = io.handler(document_id="urn:nope")
        assert res["isError"] is True and "Could not resolve" in res["message"]

    def test_addByInsert_returns_nothing_errors(self, monkeypatch):
        design, root_comp = _install(monkeypatch)
        root_comp.occurrences.insert_result = None
        res = io.handler(document_id="urn:x")
        assert res["isError"] is True and "addByInsert returned nothing" in res["message"]
