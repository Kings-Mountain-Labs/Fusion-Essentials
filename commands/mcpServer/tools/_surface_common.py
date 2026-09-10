# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The isSolid verdict, created-face walk and open-profile build the surface tools share."""

import adsk.core
import adsk.fusion

from ._common import safe
from . import _common
from . import _geom
from . import _inputs

MAP_BLURB = (
    "the surface-body substrate: _solid_verdict - the ONE any-True/False-if-any-False/else-None "
    "collapse over isSolid flags; _result_body_report/_body_names_and_solid - a feature's result "
    "bodies as names plus flags, or plus that verdict; _created_bodies - the bodies owning the "
    "faces a feature CREATED, never feature.bodies; _landed_length - a length read off the "
    "feature's own parameter")

# Surface create/join only - cut/intersect aren't meaningful for a new open sheet.
_SURFACE_OPS = ("new", "new_body", "join")

# What the surface-aware model tools accept (the name->FeatureOperations map is _common.OPERATIONS).
_OPERATION_KEYS = ("new", "new_body", "join", "cut", "intersect")

# curves: an OPEN chain of edge/sketch-curve handles to use as the profile (instead of a sketch).
_CURVES = _inputs.EdgeLoopRef("curves", closed=False, required=False,
    description="The profile.")


def _feature_operation(op_key):
    return getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])


def _solid_verdict(flags):
    """The ONE any-solid collapse over True/False/None flags: True when any body reads solid, False
    when a flag read and none did, None when no flag read at all (an empty list included)."""
    if True in flags:
        return True
    return False if False in flags else None


def _result_body_report(feature):
    """(body names, per-body isSolid flags) read OFF THE FEATURE - a flag that will not read is
    None (UNKNOWN), never an open surface, so a verdict over these must keep None apart."""
    facts = _common.body_facts(_common.result_bodies(feature))
    return [f["name"] for f in facts], [f["is_solid"] for f in facts]


def _body_names_and_solid(feature):
    """(names, is_solid) - the per-body flags collapsed through _solid_verdict into one tri-state."""
    names, flags = _result_body_report(feature)
    return names, _solid_verdict(flags)


def _any_solid(bodies):
    """_solid_verdict over bodies read HERE - read_flag, so a body whose isSolid will not read
    answers None rather than 'a surface'."""
    return _solid_verdict([_common.read_flag(lambda b=b: b.isSolid) for b in bodies])


def _created_bodies(feature):
    """(bodies, created_face_count, readable) over the faces the feature CREATED; readable=False
    means feature.faces could not be read at all."""
    # feature.bodies also lists the pre-existing SOURCE solid, so an isSolid read over it calls a
    # genuine open surface 'solid'; the bodies owning the created FACES are the product.
    faces = safe(lambda: feature.faces)
    if faces is None:
        return [], 0, False
    n = int(safe(lambda: faces.count, 0) or 0)
    return _geom.owning_bodies(_common.iter_collection(faces)), n, True


def _landed_length(getter, want_cm, k, subject, field):
    """(length in display units, error) read off the CREATED feature's own ModelParameter, which
    reads CM. A length that cannot be read comes back None for the caller's `unverified`."""
    got = safe(getter)
    if not isinstance(got, float):
        return None, ""
    if abs(got - want_cm) > 1e-6:
        return None, (f"The {subject} was created but its {field} reads back {round(got / k, 6)}, "
                      f"not the requested {round(want_cm / k, 6)}.")
    return round(got / k, 6), ""


def _curve_host_component(ents, fallback):
    """The component that OWNS the first curve/edge, so its profile + feature are built there -
    building on the active component while the source lives elsewhere is the bSet trap."""
    e = ents[0] if ents else None
    owner = safe(lambda: e.body.parentComponent)          # BRep edge
    if owner is None:
        owner = safe(lambda: e.parentSketch.parentComponent)   # sketch curve
    return owner or fallback


def _open_profile_from_curves(comp, ents):
    """(open profile, error) from curve/edge entities - B-Rep edges through createBRepEdgeProfile,
    sketch curves through createOpenProfile."""
    coll = adsk.core.ObjectCollection.create()
    for e in ents:
        coll.add(e)
    is_brep_edge = _inputs._isinstance(ents[0], adsk.fusion.BRepEdge) if ents else False
    try:
        if is_brep_edge:
            return comp.createBRepEdgeProfile(coll), None
        return comp.createOpenProfile(coll, False), None
    except Exception as e:
        return None, f"Could not build an open profile from the curves: {e}"
