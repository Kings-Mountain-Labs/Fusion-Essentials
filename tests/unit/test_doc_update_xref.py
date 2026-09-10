"""Unit tests for ``doc_update_xref.py`` - refresh out-of-date external references.

Pins: no-refs early-out, matched/skipped/error paths, and that getLatestVersion
raises rather than being swallowed when the call fails. A STALE reference refuses both refresh
routes by default (the measured derive-link state), so a row whose refresh lands says refreshes=True.
"""

import json

import pytest

from conftest import (FakeApplication, FakeDataFile, FakeDocumentReference, FakeFeatures,
                      FakeFusionDocument, FakeProducts, MakeComp, MakeDesign, _NamedCollection,
                      load_tool)

xr = load_tool("doc_update_xref")


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def FakeRef(name, is_out_of_date=True, version=1, latest_returns=True, latest_raises=None,
            stays_stale=False, latest_version=None, setter_raises=None, file_id=None,
            refreshes=False):
    """One reference row, driving BOTH refresh paths doc_update_xref uses: getLatestVersion()
    (occurrence xrefs) and the 'version' property SETTER (derive links - getLatestVersion() raises
    live for those). A STALE reference REFUSES both routes by default (measured on a derive link),
    so `refreshes` is how a row whose refresh LANDS opts in. `file_id` is the source DataFile's
    LINEAGE id, what says two rows point at one file; the default None models the id that will not
    read."""
    source = FakeDataFile(name, file_id=file_id, version=version,
                          latest_version=version + 1 if latest_version is None else latest_version)
    return FakeDocumentReference(data_file=source, version=version, out_of_date=is_out_of_date,
                                 refresh_ok=latest_returns, stays_out_of_date=stays_stale,
                                 latest_raises=latest_raises, setter_raises=setter_raises,
                                 refresh_lands=refreshes)


class FakeDeriveFeat:
    """A DeriveFeature holding one DocumentReference. DeriveFeature carries no live SHAPES dump, so
    it has no shared fake."""
    def __init__(self, name, dref=None):
        self.name = name
        self.documentReference = dref


@pytest.fixture
def _install(monkeypatch):
    """The active document doc_update_xref walks. `derive_feats` (if given) populate the ROOT
    component's features.deriveFeatures - the design-level walk that runs ALONGSIDE
    Document.documentReferences."""
    def _wire(refs=None, derive_feats=None, active=True, doc=None):
        if active and doc is None:
            design = None
            if derive_feats is not None:
                features = FakeFeatures()
                features.deriveFeatures = _NamedCollection(list(derive_feats))
                comp = MakeComp("Root")
                comp.features = features
                design = MakeDesign(comp=comp)
            doc = FakeFusionDocument(name="Host", references=refs or [],
                                     products=FakeProducts(design=design))
        monkeypatch.setattr(xr, "app",
                            FakeApplication(active_document=doc if active else None))
        return doc
    return _wire


class _NoProducts(FakeFusionDocument):
    """A document exposing no products at all (the design product is unresolvable) - the derive walk
    must not crash on it."""
    def __init__(self, **kw):
        super().__init__(**kw)
        del self.products


class TestGuards:
    def test_no_active_document(self, _install):
        _install(active=False)
        res = xr.handler()
        assert res["isError"] is True and "No active document" in res["message"]

    def test_no_refs_reports_zero(self, _install):
        _install([])
        out = _payload(xr.handler())
        assert out["updated_count"] == 0

    def test_name_not_found_lists_available(self, _install):
        _install([FakeRef("PartA"), FakeRef("PartB")])
        res = xr.handler(name="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]
        assert "PartA" in res["message"] and "PartB" in res["message"]


class TestUpdateBehavior:
    def test_updates_out_of_date_refs(self, _install):
        ref = FakeRef("PartA", is_out_of_date=True, version=2, refreshes=True)
        _install([ref])
        out = _payload(xr.handler())
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "PartA"
        assert out["updated"][0]["was_out_of_date"] is True

    def test_skips_up_to_date_refs_when_flag_set(self, _install):
        ref = FakeRef("PartA", is_out_of_date=False)
        _install([ref])
        out = _payload(xr.handler(only_out_of_date=True))
        assert out["updated_count"] == 0
        assert out["skipped"][0]["name"] == "PartA"

    def test_an_all_skipped_run_sends_stale_looking_geometry_at_the_recompute(self, _install):
        # MEASURED: an OPEN host's derive link follows the source's newly saved version by itself,
        # so this tool correctly refreshes nothing while the derived bodies still show the older
        # content. Repeating the refreshed-to-latest note there leaves the caller with no next step.
        _install([FakeRef("PartA", is_out_of_date=False)])
        note = _payload(xr.handler(only_out_of_date=True))["note"]
        assert "Nothing to refresh" in note
        assert "design_recompute" in note

    def test_a_run_that_refreshed_something_keeps_the_refreshed_note(self, _install):
        _install([FakeRef("PartA", is_out_of_date=True, refreshes=True)])
        note = _payload(xr.handler(only_out_of_date=True))["note"]
        assert "References refreshed to their latest version" in note
        assert "design_recompute" not in note

    def test_one_refreshed_beside_one_skipped_is_still_a_refreshed_run(self, _install):
        # the note turns on 'nothing was refreshed', NOT on 'something was skipped': a run that
        # updated one reference and left a current one alone did do the work it reports.
        _install([FakeRef("PartA", is_out_of_date=True, refreshes=True),
                  FakeRef("PartB", is_out_of_date=False)])
        out = _payload(xr.handler(only_out_of_date=True))
        assert out["updated_count"] == 1 and len(out["skipped"]) == 1
        assert "References refreshed to their latest version" in out["note"]
        assert "Nothing to refresh" not in out["note"]

    def test_force_updates_up_to_date_when_flag_false(self, _install):
        ref = FakeRef("PartA", is_out_of_date=False, latest_returns=True)
        _install([ref])
        out = _payload(xr.handler(only_out_of_date=False))
        assert out["updated_count"] == 1

    def test_still_stale_after_true_return_is_an_error(self, _install):
        # the platform lie: getLatestVersion returns true but the ref still reads out of date
        ref = FakeRef("PartA", is_out_of_date=True, latest_returns=True, stays_stale=True)
        _install([ref])
        res = xr.handler()
        assert res["isError"] is True
        assert "still out of date" in res["message"]

    def test_false_return_from_get_latest_reported_as_error(self, _install):
        ref = FakeRef("PartA", is_out_of_date=True, latest_returns=False)
        _install([ref])
        res = xr.handler()
        assert res["isError"] is True
        assert "returned false" in res["message"]

    def test_get_latest_raises_propagates(self, _install):
        # A getLatestVersion() raise must propagate out of the handler so the MCP framework
        # reports it - swallowing it would misreport the raise as "returned false".
        ref = FakeRef("PartA", is_out_of_date=True, latest_raises="network timeout")
        _install([ref])
        with pytest.raises(RuntimeError, match="network timeout"):
            xr.handler()

    def test_name_filter_updates_only_matching(self, _install):
        r1 = FakeRef("Alpha", is_out_of_date=True, refreshes=True)
        r2 = FakeRef("Beta", is_out_of_date=True)
        _install([r1, r2])
        out = _payload(xr.handler(name="Alpha"))
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "Alpha"


class TestNameFilterIdentity:
    """'name' is a source document's display NAME, so it is matched case-INSENSITIVELY (the caller
    types what a read printed) - and a name is not an identity: Fusion allows same-name files in
    different folders. Every row of the matched FILE refreshes together; a name covering two
    DIFFERENT files is refused instead of refreshing both."""

    def test_match_is_case_insensitive(self, _install):
        _install([FakeRef("PartA", is_out_of_date=True, file_id="urn:a", refreshes=True)])
        out = _payload(xr.handler(name="parta"))
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "PartA"

    def test_every_row_of_one_file_refreshes_together(self, _install):
        # an occurrence xref and a derive off the SAME source document: one file, two rows.
        xref = FakeRef("Src", is_out_of_date=True, file_id="urn:one", refreshes=True)
        dref = FakeRef("Src", is_out_of_date=True, file_id="urn:one", refreshes=True)
        _install(refs=[xref], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler(name="Src"))
        assert out["updated_count"] == 2
        assert {row["kind"] for row in out["updated"]} == {"xref", "derive"}

    def test_two_distinct_files_sharing_a_name_are_refused(self, _install):
        # THE boundary: 2 distinct source files behind one name - refuse, refreshing NEITHER.
        a = FakeRef("Bolt", is_out_of_date=True, file_id="urn:a")
        b = FakeRef("Bolt", is_out_of_date=True, file_id="urn:b")
        _install([a, b])
        res = xr.handler(name="Bolt")
        assert res["isError"] is True
        assert "2 DIFFERENT source files" in res["message"]
        assert "urn:a" in res["message"] and "urn:b" in res["message"]
        assert "Omit 'name'" in res["message"]          # the escape that refreshes everything
        assert a.version == 1 and b.version == 1       # neither was touched
        assert a.isOutOfDate is True and b.isOutOfDate is True

    def test_unreadable_source_ids_cannot_prove_one_file(self, _install):
        # two rows whose file ids do not read cannot be SHOWN to be one file, so the ambiguity is
        # reported rather than merged on an assumption the reads do not support.
        _install([FakeRef("Bolt", is_out_of_date=True), FakeRef("Bolt", is_out_of_date=True)])
        res = xr.handler(name="Bolt")
        assert res["isError"] is True
        assert "source file id unreadable" in res["message"]

    def test_a_single_matched_file_still_refreshes(self, _install):
        # the other side of the boundary: 1 distinct file, however many rows point at it.
        _install([FakeRef("Bolt", is_out_of_date=True, file_id="urn:a", refreshes=True),
                  FakeRef("Nut", is_out_of_date=True, file_id="urn:b")])
        out = _payload(xr.handler(name="Bolt"))
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "Bolt"

    def test_a_case_variant_miss_still_lists_the_available_names(self, _install):
        _install([FakeRef("PartA", file_id="urn:a")])
        res = xr.handler(name="Ghost")
        assert res["isError"] is True and "PartA" in res["message"]


# ── derive links (Document.documentReferences can miss these entirely - confirmed live) ───────────

class TestDeriveReferences:
    def test_derive_refresh_uses_the_version_setter_not_get_latest_version(self, _install):
        # Confirmed LIVE: calling getLatestVersion() on a DeriveFeature's documentReference raises
        # InternalValidationError (it works fine for an occurrence's) - the derive path must advance
        # via the 'version' property setter instead. latest_raises would blow up this test if the
        # derive path ever called getLatestVersion() again.
        dref = FakeRef("DeriveSrc", is_out_of_date=True, version=1, refreshes=True,
                       latest_raises="InternalValidationError: derive path must not call this")
        _install(refs=[], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler())
        assert out["updated_count"] == 1
        assert out["updated"][0]["version_after"] == 2
        assert out["updated"][0]["was_out_of_date"] is True

    def test_stale_derive_is_enumerated_and_refreshed(self, _install):
        # Document.documentReferences is EMPTY here (no occurrence xrefs) - only the derive walk
        # (component.features.deriveFeatures) can find this reference.
        dref = FakeRef("DeriveSrc", is_out_of_date=True, version=1, refreshes=True)
        _install(refs=[], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler())
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "DeriveSrc"
        assert out["updated"][0]["kind"] == "derive"

    def test_setter_failure_is_reported_honestly_not_swallowed(self, _install):
        # MEASURED on a stale derive link, and this fake's DEFAULT: the version setter refuses just
        # as getLatestVersion does. The refusal must surface as an honest, actionable per-reference
        # error (never a false "updated") and must not crash the call with a bare stack trace.
        _install(refs=[], derive_feats=[FakeDeriveFeat("Derive1", FakeRef("DeriveSrc", version=1))])
        res = xr.handler()
        assert res["isError"] is True
        assert "DeriveSrc" in res["message"]
        assert "re-derive" in res["message"]
        assert "InternalValidationError" in res["message"]

    def test_up_to_date_derive_is_skipped(self, _install):
        dref = FakeRef("DeriveSrc", is_out_of_date=False)
        _install(refs=[], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler())
        assert out["updated_count"] == 0
        assert out["skipped"][0]["name"] == "DeriveSrc"
        assert out["skipped"][0]["kind"] == "derive"

    def test_empty_derive_features_no_crash(self, _install):
        _install(refs=[], derive_feats=[])
        out = _payload(xr.handler())
        assert out["updated_count"] == 0
        assert "no external references" in out["note"].lower()

    def test_missing_products_attribute_no_crash(self, _install):
        # a document exposing no .products at all (design product unresolvable) must not crash the
        # derive walk - every step is guarded by safe().
        _install(doc=_NoProducts(name="Host"))
        out = _payload(xr.handler())
        assert out["updated_count"] == 0

    def test_both_kinds_refreshed_together_and_counted(self, _install):
        xref = FakeRef("PartA", is_out_of_date=True, refreshes=True)
        dref = FakeRef("DeriveSrc", is_out_of_date=True, refreshes=True)
        _install(refs=[xref], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler())
        assert out["updated_count"] == 2
        assert out["total_references"] == 2
        kinds = {row["name"]: row["kind"] for row in out["updated"]}
        assert kinds == {"PartA": "xref", "DeriveSrc": "derive"}

    def test_name_filter_matches_a_derive_by_its_source_name(self, _install):
        xref = FakeRef("PartA", is_out_of_date=True)
        dref = FakeRef("DeriveSrc", is_out_of_date=True, refreshes=True)
        _install(refs=[xref], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler(name="DeriveSrc"))
        assert out["updated_count"] == 1
        assert out["updated"][0]["name"] == "DeriveSrc"

    def test_zero_document_references_but_derive_present_still_refreshes(self, _install):
        # THE live-confirmed gap this fix closes: on a cold reopen, Document.documentReferences can
        # read count=0 (the derive's link isn't resolved in-session) even though a genuinely stale
        # derive exists - the derive walk must find it regardless of documentReferences' state.
        dref = FakeRef("DeriveSrc", is_out_of_date=True, version=2, refreshes=True)
        _install(refs=[], derive_feats=[FakeDeriveFeat("Derive1", dref)])
        out = _payload(xr.handler())
        assert out["total_references"] == 1
        assert out["updated_count"] == 1
        assert out["updated"][0]["version_after"] == 3
