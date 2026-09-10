"""Unit tests for the typed OUTPUT KINDS framework (_outputs.py).

This is the producer-side mirror of _inputs.py. Its value is that a tool DECLARES what it returns (the
payload key a consumer reads), so (a) the "returns X → consumed by Y" prose is generated once instead of
hand-written in the producer and paraphrased in every consumer, and (b) a test can assert the handler
actually mints the declared key — a renamed field fails the suite instead of silently lying to consumers.
Pinned: produces_note/produces_block generation, and the assert_present hook (top-level AND in-list).
"""

from conftest import load_tool

out = load_tool("_outputs")


class TestProducesNote:
    def test_handle_note_names_key_and_consumers(self):
        k = out.ReturnsHandle("handle", require="any",
                              consumers=["joint_at_geometry", "model_fillet"])
        note = k.produces_note()
        assert note.startswith("handle:")
        assert "entityToken" in note
        assert " -> joint_at_geometry, model_fillet" in note   # the ASCII consumer arrow

    def test_note_without_consumers_omits_arrow(self):
        # The consumer arrow crosses the wire as ASCII " -> " (see produces_note); with NO declared
        # consumers the note must carry no arrow text at all - not a dangling "key: label -> ".
        k = out.ReturnsValue("became_solid", "whether the stitch closed into a solid")
        note = k.produces_note()
        assert note.startswith("became_solid:")
        assert "->" not in note

    def test_the_article_matches_the_required_kind(self):
        # 'any' names no kind at all, so the label drops it rather than reading "a any 'handle'".
        assert out.ReturnsHandle("handle", require="any").produces_note().startswith(
            "handle: a 'handle' (entityToken")
        assert "an edge 'handle'" in out.ReturnsHandle("handle", require="edge").produces_note()
        assert "a face 'handle'" in out.ReturnsHandle("handle", require="face").produces_note()

    def test_urn_and_name_labels(self):
        assert "URN" in out.ReturnsUrn("document_id").produces_note()
        assert "occurrence name" in out.ReturnsName("occurrence_one", of="occurrence").produces_note()


class TestProducesBlock:
    def test_block_is_one_line_of_keys_and_consumers(self):
        spec = [out.ReturnsHandle("handle", consumers=["joint_at_geometry"]),
                out.ReturnsValue("match_count", "how many matched")]
        block = out.produces_block(spec)
        assert block == "Produces: handle -> joint_at_geometry, match_count."
        assert "how many matched" not in block       # the label lives in the payload, not the wire


class TestAssertPresentTopLevel:
    def test_present_returns_empty(self):
        k = out.ReturnsUrn("document_id")
        assert k.assert_present({"document_id": "urn:adsk.file:abc", "name": "Part"}) == ""

    def test_missing_returns_error_naming_key(self):
        k = out.ReturnsUrn("document_id")
        err = k.assert_present({"name": "Part"})
        assert "document_id" in err and "missing" in err

    def test_null_value_counts_as_missing(self):
        # A declared id that came back null is not "present" — a consumer can't use it.
        k = out.ReturnsUrn("document_id")
        assert k.assert_present({"document_id": None}) != ""


class TestReturnsVerdict:
    """The assertion-read contract: relation/passed/measured/tolerance_used, a REAL boolean verdict."""

    def _good(self):
        return {"relation": "coaxial", "passed": True,
                "measured": {"angle_deg": 0.1}, "tolerance_used": {"tolerance_deg": 0.5},
                "note": "PASS"}

    def test_full_verdict_shape_passes(self):
        k = out.ReturnsVerdict(relations=("coaxial", "parallel"))
        assert k.assert_present(self._good()) == ""

    def test_missing_contract_key_is_named(self):
        k = out.ReturnsVerdict()
        p = self._good()
        del p["tolerance_used"]
        err = k.assert_present(p)
        assert "tolerance_used" in err

    def test_non_boolean_passed_rejected(self):
        # a truthy string/int verdict is exactly the bare-boolean sloppiness the kind exists to ban
        k = out.ReturnsVerdict()
        p = self._good()
        p["passed"] = "yes"
        err = k.assert_present(p)
        assert "boolean" in err

    def test_undeclared_relation_rejected(self):
        k = out.ReturnsVerdict(relations=("coaxial",))
        p = self._good()
        p["relation"] = "frobnicated"
        err = k.assert_present(p)
        assert "frobnicated" in err

    def test_produces_note_names_the_contract_keys(self):
        note = out.ReturnsVerdict().produces_note()
        assert "passed" in note and "measured" in note and "tolerance_used" in note


class TestAssertPresentInList:
    def test_handle_inside_a_matches_list_is_found(self):
        # find_geometry shape: the handle lives inside each item of a list, not at the top level.
        k = out.ReturnsHandle("handle", in_list=True)
        payload = {"match_count": 2, "matches": [
            {"handle": "tok1", "kind": "cylinder_face"},
            {"handle": "tok2", "kind": "planar_face"}]}
        assert k.assert_present(payload) == ""

    def test_in_list_but_no_item_has_the_key_errors(self):
        k = out.ReturnsHandle("handle", in_list=True)
        payload = {"matches": [{"kind": "cylinder_face"}]}   # items lack 'handle'
        assert k.assert_present(payload) != ""

    def test_empty_list_is_missing(self):
        k = out.ReturnsHandle("handle", in_list=True)
        assert k.assert_present({"matches": []}) != ""

    def test_top_level_key_not_treated_as_in_list_when_flag_off(self):
        # A non-list output must NOT be satisfied by a nested occurrence of the key.
        k = out.ReturnsName("occurrence_one", of="occurrence", in_list=False)
        payload = {"joints": [{"occurrence_one": "X"}]}      # only nested
        assert k.assert_present(payload) != ""


class TestAbsentWhen:
    """absent_when licenses OMITTING a declared output - but only when the payload itself publishes
    the condition. Both directions matter: the license must apply when the flag is true, and must
    NOT apply otherwise, or a run that quietly drops the key passes as declared-conditional."""

    def _kind(self):
        # design_delete_feature's shape: a direct-mode edit creates no timeline feature, so the
        # feature name is legitimately absent - and the payload says direct_mode: true.
        return out.ReturnsName("feature_name", of="feature", absent_when="direct_mode")

    def test_flag_true_licenses_the_omission(self):
        assert self._kind().assert_present({"direct_mode": True, "deleted": 1}) == ""

    def test_flag_false_does_not_license_the_omission(self):
        # THE load-bearing direction: a parametric run that silently dropped feature_name while
        # publishing direct_mode: false is a broken output, not a declared gap.
        err = self._kind().assert_present({"direct_mode": False, "deleted": 1})
        assert "feature_name" in err and "direct_mode" in err and "not true" in err

    def test_flag_missing_entirely_does_not_license_the_omission(self):
        err = self._kind().assert_present({"deleted": 1})
        assert "feature_name" in err and "direct_mode" in err

    def test_a_truthy_non_true_flag_does_not_license_the_omission(self):
        # The condition is a published BOOLEAN, not "anything truthy" - a note string or a count
        # sitting in that key must not buy an omission.
        for flag in ("yes", 1, ["direct"]):
            assert self._kind().assert_present({"direct_mode": flag}) != ""

    def test_the_key_being_present_wins_regardless_of_the_flag(self):
        k = self._kind()
        assert k.assert_present({"direct_mode": False, "feature_name": "Extrude1"}) == ""
        assert k.assert_present({"direct_mode": True, "feature_name": "Extrude1"}) == ""

    def test_a_null_value_is_not_satisfied_by_a_false_flag(self):
        assert self._kind().assert_present({"direct_mode": False, "feature_name": None}) != ""

    def test_note_tells_the_agent_when_to_expect_the_gap(self):
        note = self._kind().produces_note()
        assert note.endswith("(omitted when direct_mode=true)")

    def test_a_kind_without_absent_when_errors_without_the_condition_clause(self):
        err = out.ReturnsName("feature_name", of="feature").assert_present({"deleted": 1})
        assert "feature_name" in err and "not true" not in err
