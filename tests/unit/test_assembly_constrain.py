"""Unit tests for ``assembly_constrain.py`` - Constrain Components over a relationship SET.

Pinned here, no live Fusion: the '<occurrence>:<snap>' grammar, the value encoding (offset is a
cm-scaled length, an angle a 'deg' string), and what the create VERIFIES - the constraint solved,
it broke no existing timeline feature, it holds every relationship submitted, and which parts it
repositioned.
"""

import pytest

import live_api_facts as _api_facts
from conftest import (FakeMatrix3D, FakeTimeline, FakeTimelineObject, MakeComp, _NamedCollection,
                      install, load_tool, make_design, make_placed_occurrence, payload)

ja = load_tool("assembly_constrain")

_HEALTHY = _api_facts.ENUMS["fusion.FeatureHealthStates"]["HealthyFeatureHealthState"]
_WARNING = _api_facts.ENUMS["fusion.FeatureHealthStates"]["WarningFeatureHealthState"]
_ERROR = _api_facts.ENUMS["fusion.FeatureHealthStates"]["ErrorFeatureHealthState"]

_ORIGIN = (0.0, 0.0, 0.0)


# ── fakes ───────────────────────────────────────────────────────────────────
#
# AssemblyConstraint, AssemblyConstraints and the relationship list carry no SHAPES dump in
# tests/live_api_facts.py, so no shared conftest fake stands for them and all four stay bespoke
# here. Their members are the ones api_surface.py records for the live types.

class FakeGeoRels:
    """geometricRelationships, on the constraint INPUT (where rels are added) and on the CREATED
    constraint (where the count is read back). 'fixed' pins the count to something other than what was
    added; 'blind' makes the count unreadable - the case where publishing the REQUEST as the count
    would invent a measurement."""

    def __init__(self, fixed=None, blind=False):
        self.added = []
        self._fixed = fixed
        self._blind = blind

    @property
    def count(self):
        if self._blind:
            raise RuntimeError("relationship count unreadable")
        return len(self.added) if self._fixed is None else self._fixed

    def add(self, *args):
        self.added.append(args)
        return ("rel", len(self.added))


class FakeConstraintInput:
    def __init__(self):
        self.geometricRelationships = FakeGeoRels()


def created_constraint(health=_HEALTHY, message="", count=0, blind_health=False, blind_count=False):
    """The AssemblyConstraint add() hands back. healthState error/warning is the pair assembly_get
    publishes as healthy:false; blind_health models a state that cannot be READ at all, which needs a
    raising property rather than an attribute."""
    def _health(self):
        if blind_health:
            raise RuntimeError("healthState unreadable")
        return health
    return type("C", (), {
        "name": "Constraint1", "errorOrWarningMessage": message,
        "geometricRelationships": FakeGeoRels(fixed=count, blind=blind_count),
        "healthState": property(_health)})()


class FakeAssemblyConstraints(_NamedCollection):
    """assemblyConstraints: the shared walk plus createInput() + add(input). The created constraint
    carries the health the test asks for and the relationship count the input actually received;
    on_add runs the assembly recompute the add triggers - what moves a part or breaks a joint."""

    def __init__(self, health=_HEALTHY, message="", blind_health=False, blind_count=False,
                 count=None, on_add=None):
        super().__init__()
        self.last_input = None
        self.added = 0
        self._health = health
        self._message = message
        self._blind_health = blind_health
        self._blind_count = blind_count
        self._count = count
        self.on_add = on_add

    def createInput(self):
        self.last_input = FakeConstraintInput()
        return self.last_input

    def add(self, inp):
        self.added += 1
        n = inp.geometricRelationships.count if self._count is None else self._count
        if self.on_add is not None:
            self.on_add()
        return created_constraint(health=self._health, message=self._message, count=n,
                                  blind_health=self._blind_health, blind_count=self._blind_count)


class _PoisonTimelineObject(FakeTimelineObject):
    """A timeline entry whose healthState RAISES, recording each read it takes in ``reads``."""

    # This is a freshly added assembly constraint's own entry - measured raising '1 : Unknown
    # exception' right after the add, and the same caught error inside a script context rolled the
    # whole transaction back, so the assertion worth making is that nothing read it at all.
    def __init__(self, name, index=0):
        self.reads = []
        super().__init__(name=name, index=index)

    @property
    def healthState(self):
        self.reads.append(self.name)
        raise RuntimeError("1 : Unknown exception")

    @healthState.setter
    def healthState(self, value):
        self._health = value


_part = make_placed_occurrence


@pytest.fixture
def constrain(monkeypatch):
    """Factory: install a design for assembly_constrain; returns (design, assemblyConstraints).

    Occurrences carry readable world positions, the assemblyConstraints collection is configurable,
    and the optional timeline is what the add can break. Stubs the shared snap resolver so a
    '<occ>:<snap>' pair yields an opaque entity (a real BRep proxy needs a live session).
    """
    def _make(occ_specs=(("A:1", _ORIGIN), ("B:1", _ORIGIN)), timeline_items=None, **ac_kwargs):
        ac = FakeAssemblyConstraints(**ac_kwargs)
        # a spec is (name, pos) or (name, pos, fullPathName) - the third form is a NESTED occurrence,
        # whose leaf name differs from the path that names it uniquely
        occs = [_part(s[2] if len(s) > 2 else s[0], s[1]) for s in occ_specs]
        comp = MakeComp(occurrences=occs)
        comp.assemblyConstraints = ac
        timeline = FakeTimeline(timeline_items) if timeline_items is not None else None
        design = make_design(comp=comp, timeline=timeline)
        install(ja, design)
        monkeypatch.setattr(ja, "_resolve_snap_entity",
                            lambda d, occ, snap: (f"ENT[{occ}:{snap}]", "planar", None))
        return design, ac
    return _make


# ── assembly_constrain ──────────────────────────────────────────────────────

class TestAssemblyConstraint:
    def test_missing_occurrence_errors(self, constrain):
        constrain(occ_specs=(("A:1", _ORIGIN),))
        res = ja.handler(occurrence_one="Ghost", occurrence_two="A:1")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_resolves_both_occurrences(self, constrain):
        # With no snaps and no selection, the handler should ask for geometry, not crash.
        constrain()
        res = ja.handler(occurrence_one="A:1", occurrence_two="B:1")
        assert res["isError"] is True
        assert "geometry" in res["message"].lower() or "select" in res["message"].lower()


class TestAssemblyConstraintSnaps:
    """Autonomous geometry snaps (no human selection) — '<occurrence>:<snap>'."""

    def _install_with_snaps(self, constrain):
        return constrain(occ_specs=(("TrussMast:1", _ORIGIN), ("Boom:1", _ORIGIN)))

    def test_snap_specs_resolve_and_build_relationship(self, constrain):
        _design, ac = self._install_with_snaps(constrain)
        out = payload(ja.handler(
            snap_one="TrussMast:1:top", snap_two="Boom:1:bottom", offset=0))
        # a relationship was added with the two resolved entities
        rels = ac.last_input.geometricRelationships.added
        assert len(rels) == 1
        e1, e2 = rels[0][0], rels[0][1]
        assert e1 == "ENT[TrussMast:1:top]" and e2 == "ENT[Boom:1:bottom]"
        assert out["created"] is True

    def test_flip_defaults_false(self, constrain):
        _design, ac = self._install_with_snaps(constrain)
        ja.handler(snap_one="A:1:top", snap_two="B:1:top",
                                       offset=10, units="mm")
        # flip is the 3rd arg of add(); unset it must be False (the offset VALUE
        # encoding is pinned in TestConstraintValueEncoding)
        args = ac.last_input.geometricRelationships.added[0]
        assert args[2] is False           # flipped

    def test_unresolvable_snap_errors(self, constrain, monkeypatch):
        constrain(occ_specs=(("A:1", _ORIGIN),))
        def fail_resolve(d, occ, snap):
            return (None, None, f"no '{snap}' on '{occ}'")
        monkeypatch.setattr(ja, "_resolve_snap_entity", fail_resolve)
        res = ja.handler(snap_one="A:1:top", snap_two="A:1:bottom")
        assert res["isError"] is True
        assert "no 'top'" in res["message"] or "no 'bottom'" in res["message"]


class TestMultiRelationshipConstraint:
    """ONE constraint with MULTIPLE relationships solved together (Fusion's actual model) - avoids the
    over-determined skew a single-relationship-at-a-time constraint would produce."""

    def test_relationships_list_builds_one_constraint_many_rels(self, constrain):
        _design, ac = constrain(occ_specs=(("Boom:1", _ORIGIN), ("TrussMast:1", _ORIGIN)))
        out = payload(ja.handler(relationships=[
            {"snap_one": "Boom:1:bottom", "snap_two": "TrussMast:1:top", "flip": True},
            {"snap_one": "Boom:1:back",   "snap_two": "TrussMast:1:back", "offset": 10},
            {"snap_one": "Boom:1:left",   "snap_two": "TrussMast:1:left", "offset": 30},
        ]))
        # ONE constraint, THREE relationships added to it
        added = ac.last_input.geometricRelationships.added
        assert len(added) == 3
        assert out["created"] is True
        assert out["relationship_count"] == 3

    def test_per_relationship_flip_respected(self, constrain):
        _design, ac = constrain()
        ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top", "flip": True},
            {"snap_one": "A:1:left",   "snap_two": "B:1:left"},   # flip defaults false
        ])
        added = ac.last_input.geometricRelationships.added
        assert added[0][2] is True     # flipped on first
        assert added[1][2] is False    # not on second

    def test_single_pair_still_works(self, constrain):
        # back-compat: snap_one/snap_two shorthand == a one-relationship list
        constrain()
        out = payload(ja.handler(snap_one="A:1:top", snap_two="B:1:top"))
        assert out["relationship_count"] == 1

    def test_bad_relationship_item_errors(self, constrain):
        constrain(occ_specs=(("A:1", _ORIGIN),))
        res = ja.handler(relationships=[{"snap_one": "A:1:top"}])  # missing snap_two
        assert res["isError"] is True
        assert "snap_two" in res["message"]

    def test_relationships_must_be_a_list(self, constrain):
        # passing a non-list (e.g. a dict or string) must error cleanly, not iterate chars/keys.
        constrain(occ_specs=(("A:1", _ORIGIN),))
        res = ja.handler(relationships={"snap_one": "A:1:top", "snap_two": "A:1:bottom"})
        assert res["isError"] is True
        assert "must be a list" in res["message"]


# ── the constraint VALUE encoding: offset (length, cm-scaled) vs angle (deg string) ─────────────
# rels.add(e1, e2, flip, value). 'value' is a ValueInput: an offset is createByReal(offset_cm) where
# offset_cm = offset * UNIT_TO_CM; an angle uses createByString("<deg> deg"). This is unit-conversion +
# a branch that the existing tests don't pin (they only check the flip arg).

class TestConstraintValueEncoding:
    def _stub(self, constrain):
        _design, ac = constrain()
        import adsk.core
        # echo the encoded value so the test can assert which factory + magnitude was used
        adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
        adsk.core.ValueInput.createByString = staticmethod(lambda s: ("string", s))
        return ac

    def test_offset_scaled_to_cm(self, constrain):
        ac = self._stub(constrain)
        ja.handler(snap_one="A:1:top", snap_two="B:1:top", offset=10, units="mm")
        value = ac.last_input.geometricRelationships.added[0][3]
        assert value == ("real", 1.0)        # 10 mm -> 1.0 cm via createByReal

    def test_offset_inch_scaling(self, constrain):
        ac = self._stub(constrain)
        ja.handler(snap_one="A:1:top", snap_two="B:1:top", offset=2, units="in")
        value = ac.last_input.geometricRelationships.added[0][3]
        assert value[0] == "real" and abs(value[1] - 5.08) < 1e-9   # 2 in -> 5.08 cm

    def test_unknown_units_errors_not_silently_treated_as_mm(self, constrain):
        # An unrecognized unit must be REFUSED, not silently treated as mm, like joint_create errors
        # on the same bad input.
        self._stub(constrain)
        res = ja.handler(snap_one="A:1:top", snap_two="B:1:top",
                                             offset=10, units="furlong")
        assert res["isError"] is True
        assert "furlong" in res["message"]

    def test_angle_uses_deg_string_not_offset(self, constrain):
        ac = self._stub(constrain)
        ja.handler(relationships=[
            {"snap_one": "A:1:right", "snap_two": "B:1:left", "angle_deg": 30}])
        value = ac.last_input.geometricRelationships.added[0][3]
        assert value == ("string", "30.0 deg")  # angle path -> createByString, NOT a cm offset

    def test_zero_offset_is_real_zero(self, constrain):
        ac = self._stub(constrain)
        ja.handler(snap_one="A:1:top", snap_two="B:1:top", offset=0)
        value = ac.last_input.geometricRelationships.added[0][3]
        assert value == ("real", 0.0)


# ── what the create VERIFIES: the constraint solved, it broke nothing else, and who moved ───────
# A constraint that is ADDED is not a constraint that WORKS: the platform hands back a constraint
# object whose healthState can read warning/error, the recompute the add triggers can leave EXISTING
# joints unhealthy, and the parts it is supposed to locate may not have moved at all.

def _constrain_pair(**kw):
    args = {"snap_one": "A:1:top", "snap_two": "B:1:bottom"}
    args.update(kw)
    return ja.handler(**args)


class TestConstraintSolveState:
    """healthState on the created constraint: error, or warning - the pair assembly_get's relations
    slice publishes as healthy:false. Either one means the constraint is not locating the parts, and
    an UNREADABLE state means nothing here says it is."""

    def test_failed_solve_is_refused_naming_the_delete_path(self, constrain):
        constrain(health=_ERROR, message="over-constrained")
        res = _constrain_pair()
        assert res["isError"] is True
        assert "FAILED to solve" in res["message"]
        assert "over-constrained" in res["message"]
        # the constraint is still in the design, so the refusal has to name how to get rid of it
        assert "assembly_edit_relations" in res["message"]
        assert "name='Constraint1'" in res["message"] and "action='delete'" in res["message"]

    def test_compute_warning_is_refused_too(self, constrain):
        # a warning state is what assembly_get reports as healthy:false - reporting created:true here
        # would claim the parts are located on the one reading that says they are not
        constrain(health=_WARNING)
        res = _constrain_pair()
        assert res["isError"] is True
        assert "compute WARNING" in res["message"]
        assert "action='delete'" in res["message"]

    def test_unreadable_health_state_is_refused_as_unconfirmed(self, constrain):
        # healthState RAISES: publishing created:true would report a solve nobody read
        constrain(blind_health=True)
        res = _constrain_pair()
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "Constraint1" in res["message"] and "assembly_get" in res["message"]
        assert "action='delete'" in res["message"]

    def test_a_healthy_constraint_is_reported_created(self, constrain):
        constrain(health=_HEALTHY)
        out = payload(_constrain_pair())
        assert out["created"] is True and out["constraint"] == "Constraint1"


class TestConstraintCollateralDamage:
    """The add recomputes the assembly, and that recompute can break joints/motion links the parts
    already carried. Those go unhealthy under their OWN names, so only a before/after delta separates
    the damage this call did from what was already broken."""

    def test_relations_broken_by_the_add_are_named(self, constrain):
        rev1, rev2, link = (FakeTimelineObject(name="Rev1", index=0),
                            FakeTimelineObject(name="Rev2", index=1),
                            FakeTimelineObject(name="Link1", index=2))
        _design, ac = constrain(timeline_items=[rev1, rev2, link])
        # the recompute leaves two joints in warning and the motion link in error
        def _break():
            rev1.healthState, rev2.healthState, link.healthState = _WARNING, _WARNING, _ERROR
        ac.on_add = _break
        res = _constrain_pair()
        assert res["isError"] is True
        for name in ("Rev1", "Rev2", "Link1"):
            assert name in res["message"], name
        assert "3 existing" in res["message"]
        assert "action='delete'" in res["message"]

    def test_a_warning_only_break_is_not_swallowed(self, constrain):
        # the measured damage reads as a compute WARNING, not an error - an errors-only delta would
        # report this add as a clean success
        rev1 = FakeTimelineObject(name="Rev1", index=0)
        _design, ac = constrain(timeline_items=[rev1])
        ac.on_add = lambda: setattr(rev1, "healthState", _WARNING)
        res = _constrain_pair()
        assert res["isError"] is True and "Rev1" in res["message"]

    def test_a_feature_already_unhealthy_is_not_blamed_on_this_add(self, constrain):
        # it was broken BEFORE the add - refusing here would make the tool unusable on a design that
        # already carries a warning
        constrain(timeline_items=[FakeTimelineObject(name="Rev1", index=0, health=_WARNING),
                                  FakeTimelineObject(name="Rev2", index=1, health=_ERROR)])
        out = payload(_constrain_pair())
        assert out["created"] is True

    def test_a_clean_timeline_reports_created(self, constrain):
        constrain(timeline_items=[FakeTimelineObject(name="Rev1", index=0),
                                  FakeTimelineObject(name="Rev2", index=1)])
        out = payload(_constrain_pair())
        assert out["created"] is True

    def test_the_constraint_does_not_blame_itself(self, constrain):
        # the constraint's own timeline entry carries its name, so an unhealthy entry named like the
        # constraint is the constraint - counting it would make a healthy add refuse itself
        own = FakeTimelineObject(name="Constraint1", index=0)
        _design, ac = constrain(timeline_items=[own])
        ac.on_add = lambda: setattr(own, "healthState", _ERROR)
        out = payload(_constrain_pair())
        assert out["created"] is True

    def test_the_fresh_timeline_entry_is_never_read(self, constrain):
        # its healthState is a POISON READ (raises, and the caught error rolled back the whole
        # transaction inside a script context), so the walk stops at the pre-add item count
        rev1 = FakeTimelineObject(name="Rev1", index=0)
        design, ac = constrain(timeline_items=[rev1])
        poison = _PoisonTimelineObject("Constraint1", index=1)

        def _add_entry():
            design.timeline = FakeTimeline([rev1, poison])
        ac.on_add = _add_entry
        out = payload(_constrain_pair())
        assert out["created"] is True
        assert poison.reads == []               # the new entry was never touched, not merely survived


class TestConstraintRelationshipCount:
    """relationship_count is a READ off the created constraint or it is null - never the request. A
    request echoed as a count reports the ask back as a measurement."""

    def test_the_count_is_read_off_the_constraint(self, constrain):
        constrain()
        out = payload(ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"}]))
        assert out["relationship_count"] == 2
        assert out["relationships_submitted"] == 2

    def test_an_unreadable_count_publishes_null_not_the_request(self, constrain):
        constrain(blind_count=True)
        out = payload(ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"}]))
        assert out["relationship_count"] is None
        assert out["relationships_submitted"] == 2
        assert "'relationship_count' is null" in out["note"]

    def test_a_count_short_of_the_request_is_refused(self, constrain):
        # the constraint holds ONE relationship where three were submitted: the two missing pairs
        # constrain nothing, so the parts are not located the way the call describes. The sibling
        # rigid group refuses the same shortfall; a note-only disclosure would ship created:true.
        constrain(count=1)
        res = ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"},
            {"snap_one": "A:1:back", "snap_two": "B:1:back"}])
        assert res["isError"] is True
        assert "only 1 of the 3" in res["message"]
        assert "action='delete'" in res["message"]      # it REMAINS - name the removal path

    def test_one_short_of_the_request_is_refused(self, constrain):
        # the N-1 boundary: two landed of three submitted is still a shortfall
        constrain(count=2)
        res = ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"},
            {"snap_one": "A:1:back", "snap_two": "B:1:back"}])
        assert res["isError"] is True
        assert "only 2 of the 3" in res["message"]

    def test_a_full_landing_is_created(self, constrain):
        # the other side of the boundary: N of N submitted stays ok, with no shortfall wording
        constrain(count=3)
        out = payload(ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"},
            {"snap_one": "A:1:back", "snap_two": "B:1:back"}]))
        assert out["created"] is True and out["relationship_count"] == 3
        assert "of the 3" not in out["note"]

    def test_a_count_above_the_request_is_disclosed_not_refused(self, constrain):
        # a surplus is not a shortfall: nothing the caller asked for is missing, so it is said out
        # loud in the note and the constraint stands
        constrain(count=3)
        out = payload(ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"}]))
        assert out["created"] is True
        assert "reads 3 for the 2" in out["note"]

    def test_an_unreadable_count_is_not_treated_as_a_shortfall(self, constrain):
        # None is not "fewer than submitted" - it is unknown, and refusing on it would fail every
        # call on a build whose count cannot be read
        constrain(blind_count=True)
        out = payload(ja.handler(relationships=[
            {"snap_one": "A:1:bottom", "snap_two": "B:1:top"},
            {"snap_one": "A:1:left", "snap_two": "B:1:left"}]))
        assert out["created"] is True and out["relationship_count"] is None


class TestConstraintMovedVerdict:
    """The tool's job is LOCATING parts, so the payload says whether a part moved: a distance per
    repositioned occurrence, an empty list when nothing moved, null when no position could be sampled
    (an unread transform is not a 'no')."""

    def test_a_repositioned_part_is_published_with_its_distance(self, constrain):
        design, ac = constrain()
        moving = design.rootComponent.allOccurrences[1]
        # 5 cm = 50 mm
        ac.on_add = lambda: setattr(moving, "transform2", FakeMatrix3D(t=(5.0, 0.0, 0.0)))
        out = payload(_constrain_pair())
        assert out["moved"] == [{"occurrence": "B:1", "distance_mm": 50.0,
                                 "direction": [1.0, 0.0, 0.0]}]
        assert "B:1 by 50.0 mm" in out["note"]

    def test_nothing_moved_publishes_an_empty_list_and_says_so(self, constrain):
        constrain()
        out = payload(_constrain_pair())
        assert out["moved"] == []
        assert "NO target occurrence moved" in out["note"]

    def test_unreadable_positions_publish_null_not_an_empty_list(self, constrain):
        # both transforms RAISE: "nothing moved" would be a claim about a reading nobody took
        constrain(occ_specs=(("A:1", None), ("B:1", None)))
        out = payload(_constrain_pair())
        assert out["moved"] is None
        assert "UNKNOWN" in out["note"] and "not a 'no'" in out["note"]

    def test_both_fields_label_a_nested_part_by_its_full_path(self, constrain):
        # 'occurrences' and the moved rows are ONE labelling scheme, or a caller cannot correlate them:
        # the snap string carries the leaf name, the payload publishes the unique path
        design, ac = constrain(occ_specs=(("Base:1", _ORIGIN),
                                          ("Inner:1", _ORIGIN, "Outer:1+Inner:1")))
        nested = design.rootComponent.allOccurrences[1]
        ac.on_add = lambda: setattr(nested, "transform2", FakeMatrix3D(t=(0.0, 0.0, 1.0)))
        out = payload(ja.handler(snap_one="Base:1:top",
                                                      snap_two="Inner:1:bottom"))
        assert out["occurrences"] == ["Base:1", "Outer:1+Inner:1"]
        assert [m["occurrence"] for m in out["moved"]] == ["Outer:1+Inner:1"]

    def test_both_constrained_parts_are_sampled(self, constrain):
        design, ac = constrain()
        a, b = design.rootComponent.allOccurrences
        def _both():
            a.transform2 = FakeMatrix3D(t=(0.0, 1.0, 0.0))
            b.transform2 = FakeMatrix3D(t=(0.0, 0.0, 2.0))
        ac.on_add = _both
        out = payload(_constrain_pair())
        assert sorted(m["occurrence"] for m in out["moved"]) == ["A:1", "B:1"]
        assert {m["distance_mm"] for m in out["moved"]} == {10.0, 20.0}
