# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: mirror solid bodies or timeline features across a plane.

  model_mirror -> reflect BODIES or timeline FEATURES across a plane to make the symmetric half.
                  WRITES. MirrorFeature.resultFeatures.count reads None even for a feature mirror
                  that minted geometry, so the effect is verified by a BODY/VOLUME census instead.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _assert

# PlaneRef input (multi-source: origin alias | construction name | face/plane handle) for the mirror plane.
_PLANE = _inputs.PlaneRef("plane", default="yz")
_BODIES = _inputs.BodyRefList("bodies", required=False)
# FeatureRefList resolves each timeline name to (entity, timeline label): EXACT case-insensitive
# match, 'name@index' to pick one of several same-named objects, ambiguity refused.
_FEATURES = _inputs.FeatureRefList("features")

app = adsk.core.Application.get()


def _build_collection(entities, labels):
    """(ObjectCollection, error) - the mirror's inputEntities. ObjectCollection.add() ANSWERS
    whether the collection took the object, and an unread refusal leaves an EMPTY collection for
    createInput to run on."""
    coll = adsk.core.ObjectCollection.create()
    for ent, label in zip(entities, labels):
        if not coll.add(ent):
            return None, (f"Fusion's mirror collection refused '{label}' "
                          f"({type(ent).__name__}) - that kind of object cannot be mirrored. Mirror "
                          "the bodies it produced instead.")
    if coll.count == 0:
        return None, "Nothing resolved to mirror."
    return coll, None


def _census_hosts(entity, comp):
    """The components a mirror's new geometry can land in: the source object's OWN component and the
    component the feature is built in, resolved ONCE before the mutation and counted twice. A
    component whose identity does not read (same_component answers None) is recorded as an UNKNOWN
    host - a None entry, which makes _body_total answer None."""
    host = _common.census_host(entity, comp)
    hosts = [host] if host is not None else []
    if comp is None:
        return hosts
    verdicts = [_common.same_component(h, comp) for h in hosts]
    if any(v is True for v in verdicts):
        return hosts                       # comp is already counted by one of the hosts
    if any(v is None for v in verdicts):
        return hosts + [None]              # unknown: the body count cannot judge this mirror
    return hosts + [comp]


def _body_total(hosts):
    """Total BRep bodies across the census hosts, or None when any host's count is unreadable (an
    unreadable census is not a zero - it means the effect cannot be judged from the count). An
    UNKNOWN host (None, from _census_hosts) counts as exactly that unreadable case."""
    counts = [_common.body_count(h) for h in hosts]
    if not counts or any(c is None for c in counts):
        return None
    return sum(int(c) for c in counts)


def _volume_sample(entities, feature_mode):
    """The bodies whose VOLUME the mirror starts from: the bodies being mirrored, or - for a feature
    mirror - the bodies the source features act on, read BEFORE the mutation. De-duplicated by
    _common.native_identity: Python identity SPLITS one body (a fresh proxy per read) and a bare
    entityToken MERGES two, since a token is document-local. `or id(b)` over-counts, never merges."""
    if not feature_mode:
        return list(entities)
    out, seen = [], set()
    for ent in entities:
        for b in _common.iter_collection(safe(lambda ent=ent: ent.bodies)):
            key = _common.native_identity(b) or id(b)
            if key not in seen:
                seen.add(key)
                out.append(b)
    return out


def _volume_total(bodies):
    """(total cm3, readable) over `bodies`, through the shared per-body volume walk. readable is
    False when no volume could be read, so an unreadable set is never mistaken for a real zero."""
    vals = [v for v in _geom.volumes(bodies).values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return (sum(vals), True) if vals else (0.0, False)


def _host_names(hosts):
    """The census hosts named, for an error that says WHERE nothing changed."""
    return " / ".join(n for n in (safe(lambda h=h: h.name) for h in hosts) if n) or "the target component"


def handler(bodies=None, features=None, plane: str = "yz", join: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    has_bodies = bodies not in (None, "", [])
    has_features = features not in (None, "", [])
    # Two ways to name the same input is an ambiguity, so both-given is refused rather than resolved
    # by precedence: silently ignoring one of them mirrors something the caller did not ask for.
    if has_bodies and has_features:
        return error(f"Give ONE thing to mirror: 'bodies' ({bodies}) or 'features' ({features}), "
                     "not both.")
    if not has_bodies and not has_features:
        return error("Nothing to mirror. Pass 'bodies' (body handles/names) or 'features' (timeline "
                     "feature names from design_get(include=['timeline'])).")

    # plane is a PlaneRef input: resolves an origin alias OR a construction-plane name OR a
    # planar-face/plane handle - the kind handles all three (including arbitrary planes) + validation.
    mirror_plane, perr = _PLANE.resolve(plane)
    if perr:
        return error(perr)

    if has_features:
        # isCombine is DOCUMENTED as ignored when any input object is not a body, so a join request
        # here is refused rather than accepted and reported as done.
        if join:
            return error("'join' combines mirrored BODIES with their originals and is documented as "
                         "ignored for a feature mirror - re-run without 'join', or mirror the bodies.")
        # features is a FeatureRefList: each timeline name resolved to (entity, timeline label).
        resolved, rerr = _FEATURES.resolve(features)
        ents, labels = resolved if rerr is None else ([], [])
    else:
        # bodies is a BodyRefList: each by handle (precise) or name; resolved+validated by the kind.
        ents, rerr = _BODIES.resolve(bodies)
        labels = [safe(lambda b=b: b.name) for b in (ents or [])]
    if rerr:
        return error(rerr)
    if not ents:
        return error("Nothing resolved to mirror.")

    coll, cerr = _build_collection(ents, labels)
    if cerr:
        return error(cerr)

    # The effect census, captured BEFORE the mutation: the body count of the components the mirror
    # can land in, plus the starting volume of the bodies it works from.
    hosts = _census_hosts(ents[0], comp)
    before_total = _body_total(hosts)
    before_volume, before_readable = _volume_total(_volume_sample(ents, has_features))

    try:
        mi = comp.features.mirrorFeatures.createInput(coll, mirror_plane)
        if not has_features:
            # set_verified, not a bare assignment: a SWIG proxy accepts an assignment to a name it
            # does not define, so the value is read back before the mirror runs on its default.
            serr = _common.set_verified(mi, "isCombine", bool(join), "join", "MirrorFeatureInput")
            if serr:
                return error(serr)
        feature = comp.features.mirrorFeatures.add(mi)
    except Exception as e:
        return error(f"Mirror failed: {e}.")
    if not feature:
        return error(_common.no_feature_error(design, "Mirror"))

    name = safe(lambda: feature.name)
    after_total = _body_total(hosts)
    added = None if (before_total is None or after_total is None) else after_total - before_total
    # The after-volume comes off the FEATURE's own bodies - fresh references minted by the rebuild -
    # so a join that consumed the source body is measured on what exists now, not through a proxy
    # held across the mutation.
    result = _common.result_bodies(feature)
    after_volume, after_readable = _volume_total(result)
    vol_readable = before_readable and after_readable
    vol_delta = after_volume - before_volume
    if not (added or (vol_readable and vol_delta != 0.0)):
        # Each refusal states only the signal that was actually READ: a flat count with an unreadable
        # volume (or the reverse) is a half-measurement, and calling it a no-op would overclaim.
        where = _host_names(hosts)
        remedy = _common.failed_effect_remedy(design, feature)
        tail = f"Check with design_get(include=['tree']) / model_inspect. {remedy}"
        if added is None and not vol_readable:
            return error(f"Mirror '{name}' was created but neither the body count of {where} nor a "
                         f"volume could be read back, so its effect is UNVERIFIED. {tail}")
        if added is None:
            return error(f"Mirror '{name}' moved no volume, and the body count of {where} could not "
                         f"be read back, so whether it added a body is UNVERIFIED. {tail}")
        if not vol_readable:
            return error(f"Mirror '{name}' added no body to {where}, and no volume could be read "
                         f"back, so whether it moved material is UNVERIFIED. {tail}")
        return error(f"Mirror '{name}' added no body to {where} and moved no volume - nothing was "
                     f"mirrored. {remedy}")

    facts = _common.body_facts(result)
    out = {
        "mirrored": True,
        "mode": "features" if has_features else "bodies",
        "feature": name,
        "plane": (plane or "yz"),
        "result_bodies": [f["name"] for f in facts],
        "bodies_added": added,
    }
    if has_features:
        out["source_features"] = labels
        out["note"] = ("Feature(s) mirrored across the plane. Confirm with "
                       "design_get(include=['timeline']) / view_screenshot.")
    else:
        out["source_bodies"] = labels
        out["note"] = "Bodies mirrored across the plane. Pair with view_screenshot to view."
        # MEASURED: a body mirror created with isCombine=True reads isCombine back TRUE off the
        # FEATURE, so 'joined' publishes what the feature says it did, never the request. A
        # disagreement is partial success and is said out loud rather than papered over.
        combined = safe(lambda: feature.isCombine)
        if combined is not None:
            out["joined"] = bool(combined)
            if bool(combined) != bool(join):
                out["note"] += (f" The feature reads isCombine={bool(combined)}, so the join request "
                                "did not take on it.")
    if vol_readable:
        out["volume_change_cm3"] = round(vol_delta, 6)
    return ok(out)


TOOL_DESCRIPTION = (
    "Mirror bodies or timeline features across a plane."
)

mirror_tool = (
    Tool.create_simple(name="model_mirror", description=TOOL_DESCRIPTION)
    .add_input_property(_BODIES.name, _BODIES.schema())
    .add_input_property(*_FEATURES.as_property())
    .add_input_property(_PLANE.name, _PLANE.schema())
    .add_input_property("join", {"type": "boolean",
            "description": "Fuse into the original."})
    .strict_schema()
)
mirror_item = Item.create_tool_item(tool=mirror_tool, write="write", handler=handler, run_on_main_thread=True,
                                    postconditions=[_assert.FeatureHealthy()],
                                    verification=Verification(
                                        kind="inline", rung="geometry",
                                        evidence_test="tests/unit/test_model_mirror.py"
                                        "::TestEffectCensus"
                                        "::test_a_mirror_that_changes_nothing_is_an_error"))


def register_tool():
    register(mirror_item)
