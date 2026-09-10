"""Unit tests for assembly_inspect_interference — the physical-fit 'check my work' tool.

The live analyzeInterference call needs Fusion, but the logic worth pinning is pure: mapping an
interfering body back to its OWNING occurrence (so the report is by part, not 'Body1'), aggregating
overlap volume per occurrence-pair, the clear=true path, and the <2-occurrence short-circuit.
"""

import pytest

from conftest import (_NamedCollection, BRepBody, install, load_tool, make_occurrence,
                      make_source_document, MakeComp, MakeDesign, payload)

ai = load_tool("assembly_inspect_interference")

# The x-ref identity shape, MEASURED: an entityToken is DOCUMENT-LOCAL, so two DISTINCT bodies living
# in two different source documents read byte-identical tokens (measured on a CAM job assembled from 7
# source documents - all 7 root components answered one token, and their 7 lineage urns all differed).
# The token below is deliberately opaque and shared verbatim by both bodies: a mnemonic token derived
# from a body's own name would make a bare-token key and an identity key agree, and a fixture where the
# two schemes agree cannot tell them apart.
_COLLIDING_TOKEN = "/vB+AAEAAwAAAAAAAAAAAAAA"
_URN_HOST = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_XREF = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"


def _occ(name, full_path=None, bodies=(), children=(), broken_children=()):
    """An occurrence in the analysis set: fullPathName names the INSTANCE, its component holds the
    native bodies analyzeInterference hands back."""
    # component.occurrences is the COMPONENT-LOCAL superset the fallback walk reads; the
    # assembly-context childOccurrences drops an occurrence with an unresolved reference.
    comp = MakeComp(name=name.split(":")[0], bodies=list(bodies),
                    occurrences=list(broken_children) + list(children))
    return make_occurrence(path=full_path or name, component=comp, children=children)


def _body(name, comp_name=None, occ_name=None, token=None, urn=None):
    """A NATIVE body: the entityToken an owner map keys on, plus the parentComponent/assemblyContext
    an unmapped body falls back through."""
    # LIVE shape: analyzeInterference returns NATIVE bodies - assemblyContext reads None on both
    # result entities - so the interfering INSTANCE is recovered by mapping the body's
    # _common.native_identity back to the occurrences that were put into the analysis set.
    #
    # `urn` puts the body in a named SOURCE DOCUMENT, through the chain native_identity reads its
    # second half over (parentComponent -> parentDesign -> parentDocument -> dataFile.id, built by the
    # shared conftest.make_source_document). Omitted, the chain stops short and the urn half reads
    # None - which is what an unsaved single-document design looks like, and what every fixture here
    # that is not about the x-ref collision wants.
    comp = MakeComp(name=comp_name) if comp_name else None
    if urn is not None:
        comp = MakeComp(name=comp_name or "", parent_design=make_source_document(urn))
    body = BRepBody(name=name, entity_token=token or f"TOK::{name}", parent_component=comp)
    if occ_name:
        body.assemblyContext = _occ(occ_name)
    return body


class FakeInterfBody:
    def __init__(self, volume):
        self.volume = volume


class FakeResult:
    def __init__(self, b1, b2, volume):
        self.entityOne = b1
        self.entityTwo = b2
        self.interferenceBody = FakeInterfBody(volume)


class FakeResults:
    def __init__(self, results):
        self._r = list(results)
    @property
    def count(self):
        return len(self._r)
    def item(self, i):
        return self._r[i]


class FakeInput:
    areCoincidentFacesIncluded = False


class FakeInputRejectsCoincident:
    """Raises when areCoincidentFacesIncluded is set - models the API rejecting the value. The set
    must not be swallowed by a try/except: a rejected value surfaces as an analysis failure."""
    def __setattr__(self, name, value):
        if name == "areCoincidentFacesIncluded":
            raise RuntimeError("areCoincidentFacesIncluded rejected by the API")
        object.__setattr__(self, name, value)


UNAVAILABLE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
               "external reference).")

# The walk that will not enumerate at all: reading root.allOccurrences on a design holding an
# unresolved reference RAISES, and an empty analysis set yields zero interferences.
RAISING_WALK = "2 : InternalValidationError : occ"


def _broken_occ(name="45740"):
    """An occurrence whose referenced component will not load - it carries NO geometry this analysis
    could compare, and its path will not read either."""
    return make_occurrence(path=name, raises_on={
        "component": UNAVAILABLE,
        "fullPathName": "2 : InternalValidationError : path.valid()"})


class _InterferenceDesign(MakeDesign):
    """A design whose interference analysis answers canned results."""

    def __init__(self, occurrences, results, reject_coincident=False):
        # allOccurrences is the analysis set (every depth); root-level solids join it too.
        MakeDesign.__init__(self, comp=MakeComp(name="Root", occurrences=list(occurrences)))
        self._results = results
        self._reject_coincident = reject_coincident

    def createInterferenceInput(self, occs):
        return FakeInputRejectsCoincident() if self._reject_coincident else FakeInput()

    def analyzeInterference(self, inp):
        return FakeResults(self._results)


@pytest.fixture
def world():
    """A design whose analysis set is `occurrences`, wired into the tool through install() - which
    patches both design seams and the ObjectCollection the analysis set is built in."""
    def _build(occurrences, results, reject_coincident=False):
        return install(ai, _InterferenceDesign(occurrences, results,
                                               reject_coincident=reject_coincident))
    return _build


class TestOwningOccurrence:
    def test_names_the_INSTANCE_via_the_analysis_set(self):
        # The point of the report: which INSTANCE interferes. analyzeInterference returns a native
        # body, so the path comes from the occurrence map, not off the body. An exact instance
        # carries no candidate list. The map is keyed the way _native_body_owners keys it - on
        # _common.native_identity, the same reader both sides use.
        b = _body("Body1", comp_name="Wheel")
        owners = {ai._common.native_identity(b): ["Rig:1+Wheel:2"]}
        assert ai._owning_occurrence_name(b, owners) == ("Rig:1+Wheel:2", None)

    def test_names_every_candidate_when_one_native_body_serves_several_instances(self):
        # A component instanced twice maps its native body to both - a genuine ambiguity: the label
        # says so AND the full candidate path list rides along, so the caller can discriminate
        # instead of guessing from "or N more".
        b = _body("Body1", comp_name="Wheel")
        owners = {ai._common.native_identity(b): ["Rig:1+Wheel:1", "Rig:1+Wheel:2"]}
        label, cands = ai._owning_occurrence_name(b, owners)
        assert "Rig:1+Wheel:1" in label and "1 more instance" in label
        assert cands == ["Rig:1+Wheel:1", "Rig:1+Wheel:2"]

    def test_helper_returns_the_full_list_and_a_true_count_label(self):
        # the helper never drops a suspect - the ROW caps what it publishes; the label's "or N
        # more" is the true total either way.
        b = _body("Body1", comp_name="Wheel")
        paths = [f"Wheel:{i}" for i in range(1, 5)]
        label, cands = ai._owning_occurrence_name(b, {ai._common.native_identity(b): paths})
        assert cands == paths                          # full, uncapped
        assert "3 more instance" in label              # the true total

    def test_the_multi_owner_label_claims_only_what_the_map_was_built_from(self):
        # The map is built by walking each occurrence's component bodies, so what a multi-path entry
        # records is: these occurrences' components own this one native body. Nothing in this module
        # reads a component IDENTITY, so the label must not assert the paths are instances of one
        # component.
        b = _body("Body1", comp_name="Wheel")
        label, _ = ai._owning_occurrence_name(
            b, {ai._common.native_identity(b): ["Rig:1+Wheel:1", "Rig:1+Wheel:2"]})
        assert "whose component owns this same native body" in label

    def test_falls_back_to_component_then_body_name_when_unmapped(self):
        # A root-level body belongs to no occurrence: the component name is all there is.
        assert ai._owning_occurrence_name(_body("B", comp_name="Crank"), {}) == ("Crank", None)
        assert ai._owning_occurrence_name(_body("LooseBody"), {}) == ("LooseBody", None)

    def test_a_body_with_no_readable_token_has_no_identity_and_falls_back(self):
        # native_identity answers None with no token, and None must not become a lookup key - every
        # unidentifiable body would then share one owner list. The component-name fallback covers it.
        b = _body("B", comp_name="Crank")
        b.entityToken = ""
        assert ai._common.native_identity(b) is None
        assert ai._owning_occurrence_name(b, {None: ["Wrong:1"]}) == ("Crank", None)

    def test_the_owner_map_SKIPS_a_body_with_no_identity(self):
        # The WRITE side of the same guard: storing an identity-less body under None would give
        # every unidentifiable body in the design ONE shared owner list - a merge, not an unknown -
        # and _owning_occurrence_name's None lookup would then read it back.
        good = _body("Good", comp_name="Wheel")
        blank = _body("Bad", comp_name="Wheel")
        blank.entityToken = ""
        owners = ai._native_body_owners([_occ("Wheel:1", bodies=[good, blank])])
        assert list(owners) == [ai._common.native_identity(good)]
        assert None not in owners


class TestTheTwoDocumentTokenCollision:
    """An entityToken is DOCUMENT-LOCAL: two DISTINCT native bodies in two x-ref'd documents read
    byte-identical tokens (measured). Keyed on the bare token their owner lists MERGE, and every path
    in the merged list is then published as an owner of the other document's body. These fixtures put
    two such bodies in front of the tool; a fixture whose tokens differ cannot tell a bare-token key
    from an identity key and would prove nothing."""

    def _two_documents(self):
        """(host body, x-ref body): different documents, ONE shared token, two different lineage urns."""
        return (_body("Frame", comp_name="Frame", token=_COLLIDING_TOKEN, urn=_URN_HOST),
                _body("Frame", comp_name="Lid", token=_COLLIDING_TOKEN, urn=_URN_XREF))

    def test_the_fixture_really_models_the_collision(self):
        # Both halves have to be real or every test below proves nothing: with no token collision the
        # defect the identity key exists for never fires, and with no urn difference the identity key
        # would answer the same value the bare token does.
        host, xref = self._two_documents()
        host_id, xref_id = ai._common.native_identity(host), ai._common.native_identity(xref)
        assert host is not xref
        assert host.entityToken == xref.entityToken == _COLLIDING_TOKEN     # the collision is real
        assert host_id[1] == _URN_HOST and xref_id[1] == _URN_XREF          # and so is the separation
        assert host_id != xref_id

    def test_the_owner_map_keeps_two_documents_bodies_apart(self):
        host, xref = self._two_documents()
        owners = ai._native_body_owners([_occ("Frame:1", bodies=[host]),
                                         _occ("Lid:1", bodies=[xref])])
        assert len(owners) == 2                                    # merged on the bare token: 1
        assert owners[ai._common.native_identity(host)] == ["Frame:1"]
        assert owners[ai._common.native_identity(xref)] == ["Lid:1"]

    def test_a_pair_across_two_documents_names_the_two_instances_exactly(self, world):
        # The end-to-end shape a merge wrecks: both bodies would look up ONE owner list, so both
        # sides of the row would carry the same two-path label - collapsing a genuine pair into a
        # self-pair and naming each document's instance as a suspect for the other's body.
        host, xref = self._two_documents()
        world([_occ("Frame:1", bodies=[host]), _occ("Lid:1", bodies=[xref])],
                 [FakeResult(host, xref, 3.0)])
        row = payload(ai.handler())["measured"]["interferences"][0]
        assert {row["occurrence_one"], row["occurrence_two"]} == {"Frame:1", "Lid:1"}
        assert "occurrence_one_candidates" not in row              # each side is EXACT
        assert "occurrence_two_candidates" not in row


class TestInterferenceHandler:
    def test_reports_pairs_by_occurrence_with_volume(self, world):
        wheel = _body("Body1", "Wheel:1")
        fork = _body("Body1", "Fork:1")
        world([_occ("Wheel:1"), _occ("Fork:1")],
                 [FakeResult(wheel, fork, 7.7)])
        out = payload(ai.handler())
        assert out["passed"] is False and out["measured"]["interference_count"] == 1
        assert out["relation"] == "interference_free"
        pair = out["measured"]["interferences"][0]
        assert {pair["occurrence_one"], pair["occurrence_two"]} == {"Wheel:1", "Fork:1"}
        assert pair["overlap_volume_cm3"] == 7.7
        assert ai.RETURNS[0].assert_present(out) == ""       # the verdict contract holds

    def test_aggregates_volume_per_pair(self, world):
        # two interference bodies between the SAME pair -> summed into one entry
        a, b = _body("B", "Crank:1"), _body("B", "Wheel:1")
        world([_occ("Crank:1"), _occ("Wheel:1")],
                 [FakeResult(a, b, 3.0), FakeResult(a, b, 2.0)])
        out = payload(ai.handler())
        assert out["measured"]["interference_count"] == 1
        assert out["measured"]["interferences"][0]["overlap_volume_cm3"] == 5.0

    def test_clear_when_results_empty(self, world):
        world([_occ("A:1"), _occ("B:1")], [])
        out = payload(ai.handler())
        assert out["passed"] is True and out["measured"]["interference_count"] == 0

    def test_under_two_entities_REFUSES_rather_than_passing(self, world):
        # Nothing to compare is not the same as nothing wrong. A verdict payload has no "unknown"
        # state, so the tool refuses; reporting passed=true would let a caller gate a build on a
        # verdict this tool never formed.
        world([_occ("Solo:1")], [FakeResult(_body("x"), _body("y"), 1.0)])
        res = ai.handler()
        assert res["isError"] is True
        assert "NOT a pass" in res["content"][0]["text"]

    def test_nested_parts_under_ONE_top_level_occurrence_are_still_analysed(self, world):
        # The whole assembly wrapped in a single occurrence: the analysis set is allOccurrences, so
        # the wrapper's children are compared instead of the design reading as one entity.
        a, b = _body("BoxA", "PartA"), _body("BoxB", "PartB")
        occs = [_occ("Wrapper:1", "Wrapper:1"),
                _occ("PartA:1", "Wrapper:1+PartA:1", bodies=[a]),
                _occ("PartB:1", "Wrapper:1+PartB:1", bodies=[b])]
        world(occs, [FakeResult(a, b, 500.0)])
        out = payload(ai.handler())
        assert out["passed"] is False
        assert out["measured"]["interferences"] == [
            {"occurrence_one": "Wrapper:1+PartA:1", "occurrence_two": "Wrapper:1+PartB:1",
             "overlap_volume_cm3": 500.0}]

    def test_pairs_sorted_by_descending_volume(self, world):
        # three distinct pairs with different overlap volumes -> reported largest-overlap first.
        a, b, c, d = (_body("x", "A:1"), _body("x", "B:1"),
                      _body("x", "C:1"), _body("x", "D:1"))
        world([_occ("A:1"), _occ("B:1"), _occ("C:1"), _occ("D:1")],
                 [FakeResult(a, b, 1.0), FakeResult(c, d, 9.0), FakeResult(a, c, 4.0)])
        out = payload(ai.handler())
        vols = [p["overlap_volume_cm3"] for p in out["measured"]["interferences"]]
        assert vols == [9.0, 4.0, 1.0]            # strictly descending
        assert out["measured"]["interference_count"] == 3

    def test_coincident_flag_echoed(self, world):
        world([_occ("A:1"), _occ("B:1")], [])
        default = payload(ai.handler())
        assert default["tolerance_used"]["coincident_faces_included"] is False
        incl = payload(ai.handler(include_coincident_faces=True))
        assert incl["tolerance_used"]["coincident_faces_included"] is True

    def test_occurrences_checked_count(self, world):
        world([_occ("A:1"), _occ("B:1"), _occ("C:1")], [])
        assert payload(ai.handler())["measured"]["occurrences_checked"] == 3

    def test_owning_name_falls_back_when_parent_component_name_empty(self):
        # parentComponent present but its name is falsy -> use assemblyContext, then body name.
        b = _body("BodyZ", comp_name="", occ_name="Crank:1")
        b.parentComponent = MakeComp(name="")    # present object, empty name
        assert ai._owning_occurrence_name(b, {}) == ("Crank:1", None)

    def test_multi_instance_pair_carries_candidates_and_the_note_explains(self, world):
        # A multi-instance component collides with a single-instance one: the row must list every
        # suspect path on the ambiguous side, and the note must say the platform (native bodies
        # off analyzeInterference) is why the exact instance is not named.
        shared = _body("Body1")                          # ONE native body...
        o1 = _occ("Wheel:1", bodies=[shared])
        o2 = _occ("Wheel:2", bodies=[shared])            # ...serving two instances
        fork_body = _body("Body1", token="TOK::fork-native")   # same NAME, distinct native body
        fork = _occ("Fork:1", bodies=[fork_body])
        world([o1, o2, fork], [FakeResult(shared, fork_body, 2.5)])
        out = payload(ai.handler())
        row = out["measured"]["interferences"][0]
        sides = {row["occurrence_one"]: row.get("occurrence_one_candidates"),
                 row["occurrence_two"]: row.get("occurrence_two_candidates")}
        ambiguous = [c for c in sides.values() if c]
        assert ambiguous == [["Wheel:1", "Wheel:2"]]        # the ambiguous side lists both suspects
        assert "Fork:1" in sides and sides["Fork:1"] is None   # the exact side carries no list
        assert "candidates" in out["note"] and "native bodies" in out["note"]

    def test_the_candidates_note_claims_only_the_shared_native_body(self, world):
        # The note states what the owner map records - one native body, and the occurrences whose
        # component owns it. It does NOT say the instances belong to one component: nothing in this
        # module reads a component identity to back that.
        shared = _body("Body1")
        lone = _body("Body1", token="TOK::fork-native")
        world([_occ("Wheel:1", bodies=[shared]), _occ("Wheel:2", bodies=[shared]),
                               _occ("Fork:1", bodies=[lone])], [FakeResult(shared, lone, 2.5)])
        note = payload(ai.handler())["note"]
        assert "each candidate path is an occurrence whose component owns that body" in note

    def test_both_sides_ambiguous_each_lists_its_own_candidates(self, world):
        # a rail instanced twice collides with a rod instanced twice - BOTH sides of the pair
        # carry their own candidate lists.
        rail_body, rod_body = _body("Body1", token="TOK::rail"), _body("Body1", token="TOK::rod")
        occs = [_occ("Rail:1", bodies=[rail_body]), _occ("Rail:2", bodies=[rail_body]),
                _occ("Rod:1", bodies=[rod_body]), _occ("Rod:2", bodies=[rod_body])]
        world(occs, [FakeResult(rail_body, rod_body, 0.5)])
        row = payload(ai.handler())["measured"]["interferences"][0]
        cands = {row["occurrence_one_candidates"][0], row["occurrence_two_candidates"][0]}
        assert cands == {"Rail:1", "Rod:1"}
        assert row["occurrence_one_candidates"] in (["Rail:1", "Rail:2"], ["Rod:1", "Rod:2"])
        assert row["occurrence_two_candidates"] in (["Rail:1", "Rail:2"], ["Rod:1", "Rod:2"])

    def test_exact_pair_carries_no_candidates_and_a_plain_note(self, world):
        wheel, fork = _body("B1"), _body("B2")
        world([_occ("Wheel:1", bodies=[wheel]), _occ("Fork:1", bodies=[fork])],
                 [FakeResult(wheel, fork, 1.0)])
        out = payload(ai.handler())
        row = out["measured"]["interferences"][0]
        assert "occurrence_one_candidates" not in row and "occurrence_two_candidates" not in row
        assert "candidates" not in out["note"]

    def test_candidates_over_the_cap_are_flagged_truncated_on_the_row(self, monkeypatch, world):
        # the machine-readable incompleteness signal: a capped list must never read as the full
        # suspect set - the row carries a truncated flag, not just a count buried in prose.
        monkeypatch.setattr(ai, "_CANDIDATE_CAP", 2)
        shared = _body("Body1")
        occs = [_occ(f"Wheel:{i}", bodies=[shared]) for i in range(1, 5)]
        lone = _body("B2", token="TOK::lone")
        occs.append(_occ("Fork:1", bodies=[lone]))
        world(occs, [FakeResult(shared, lone, 1.0)])
        row = payload(ai.handler())["measured"]["interferences"][0]
        side = "occurrence_one" if "occurrence_one_candidates" in row else "occurrence_two"
        assert row[f"{side}_candidates"] == ["Wheel:1", "Wheel:2"]      # capped at 2
        assert row[f"{side}_candidates_truncated"] is True
        other = "occurrence_two" if side == "occurrence_one" else "occurrence_one"
        assert f"{other}_candidates_truncated" not in row               # exact side unflagged

    def test_candidates_exactly_at_the_cap_carry_no_truncated_flag(self, monkeypatch, world):
        # the boundary: exactly cap-many candidates is a COMPLETE list - flagging it truncated
        # would claim suspects were dropped when none were (a >= guard tells that lie).
        monkeypatch.setattr(ai, "_CANDIDATE_CAP", 2)
        shared = _body("Body1")
        occs = [_occ("Wheel:1", bodies=[shared]), _occ("Wheel:2", bodies=[shared]),
                _occ("Fork:1", bodies=[_body("B2", token="TOK::lone")])]
        world(occs, [FakeResult(shared, _body("B2", token="TOK::lone"), 1.0)])
        row = payload(ai.handler())["measured"]["interferences"][0]
        side = "occurrence_one" if "occurrence_one_candidates" in row else "occurrence_two"
        assert row[f"{side}_candidates"] == ["Wheel:1", "Wheel:2"]      # complete, at the cap
        assert f"{side}_candidates_truncated" not in row
        assert "occurrence_one_candidates_truncated" not in row
        assert "occurrence_two_candidates_truncated" not in row

    def test_self_pair_note_when_same_occurrence_overlaps(self, world):
        # both bodies map to the same occurrence -> a self-pair (one entry, sorted key collapses).
        a, b = _body("x", "Wheel:1"), _body("y", "Wheel:1")
        world([_occ("Wheel:1"), _occ("Other:1")], [FakeResult(a, b, 2.0)])
        out = payload(ai.handler())
        pair = out["measured"]["interferences"][0]
        assert pair["occurrence_one"] == "Wheel:1" and pair["occurrence_two"] == "Wheel:1"

    def test_no_design_errors(self, monkeypatch):
        monkeypatch.setattr(ai._common, "design", lambda: None)
        res = ai.handler()
        assert res["isError"] is True
        assert "No active design" in res["message"]

    def test_areCoincidentFacesIncluded_failure_surfaces_as_error(self, world):
        # a rejected areCoincidentFacesIncluded assignment must raise into the handler's error path,
        # not be swallowed into a successful result that still echoes coincident_faces_included=True.
        world([_occ("A:1"), _occ("B:1")], [], reject_coincident=True)
        res = ai.handler(include_coincident_faces=True)
        assert res["isError"] is True
        assert "areCoincidentFacesIncluded rejected" in res["message"]


class TestUnresolvedReferences:
    """The cardinal sin this tool was one raise away from: root.allOccurrences raising left the
    analysis set EMPTY, and an empty set produces zero interferences - published as passed=true."""

    def test_a_raising_walk_no_longer_analyses_an_empty_set(self, world):
        a = _occ("A:1", bodies=[_body("B1", "A:1")])
        b = _occ("B:1", bodies=[_body("B2", "B:1")])
        des = world([a, b], [])
        des.rootComponent.allOccurrences = _NamedCollection(raises=RAISING_WALK)
        out = payload(ai.handler())
        assert out["measured"]["occurrences_checked"] == 2      # NOT 0, and NOT a refusal
        assert out["measured"]["occurrences_walk"] == "recursed"
        assert out["passed"] is True

    def test_a_clean_verdict_is_REFUSED_while_an_unresolved_reference_is_excluded(self, world):
        # a pass is a claim about everything, and an unresolved occurrence was never in the set.
        a = _occ("A:1", bodies=[_body("B1", "A:1")])
        b = _occ("B:1", bodies=[_body("B2", "B:1")], broken_children=[_broken_occ("45740")])
        des = world([a, b], [])
        des.rootComponent.allOccurrences = _NamedCollection(raises=RAISING_WALK)
        res = ai.handler()
        assert res["isError"] is True
        assert "Cannot certify interference-free" in res["message"]
        assert "45740" in res["message"]
        assert "NOT a pass" not in res["message"] and "no pass was formed" in res["message"]

    def test_a_POSITIVE_finding_still_stands_over_an_incomplete_set(self, world):
        # finding one overlapping pair is proof on its own - it does not depend on completeness, so
        # the refusal above must not swallow a real hit.
        shared = _body("x", "A:1")
        a = _occ("A:1", bodies=[shared])
        b = _occ("B:1", bodies=[_body("y", "B:1")], broken_children=[_broken_occ("45740")])
        des = world([a, b],
                       [FakeResult(shared, _body("y", "B:1"), 2.0)])
        des.rootComponent.allOccurrences = _NamedCollection(raises=RAISING_WALK)
        out = payload(ai.handler())
        assert out["passed"] is False
        assert out["measured"]["unresolved_references"] == ["45740"]
        assert "were NOT compared" in out["note"]

    def test_zero_unresolved_leaves_the_pass_verdict_and_the_fast_walk(self, world):
        world([_occ("A:1"), _occ("B:1")], [])
        out = payload(ai.handler())
        assert out["passed"] is True
        assert out["measured"]["occurrences_walk"] == "allOccurrences"
        assert out["measured"]["unresolved_references"] == []
        assert "were NOT compared" not in out["note"]
