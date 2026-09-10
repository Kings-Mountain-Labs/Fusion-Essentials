# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared substrate for the pattern tools: what gets patterned (bodies or occurrences), which
component the feature must be built in, and the direction/axis entity resolved into that component.
"""

import adsk.core

from ._common import safe
from . import _inputs

MAP_BLURB = (
    "the pattern substrate: _resolve_input_entities - the ObjectCollection a pattern seeds from, "
    "bodies taking precedence over occurrences; _owning_component - the component the feature must "
    "be built in (the axis and the feature must share one or Fusion raises getObjectPath); "
    "_direction_entity - a world-axis key as that component's own origin axis, anything else "
    "lifted into its assembly context")

_BODIES = _inputs.BodyRefList("bodies", required=False)

_OCCURRENCES = _inputs.OccurrenceRefList("occurrences", required=False,
                                         description="Or 'bodies'.")


def _resolve_input_entities(design, occurrences, bodies):
    """Build the ObjectCollection to pattern: 'bodies' (BodyRefList) takes precedence, else
    'occurrences' (OccurrenceRefList). Returns (collection, resolved_names, error)."""
    if bodies not in (None, "", []):
        ents, berr = _BODIES.resolve(bodies)
        if berr:
            return None, None, berr
        coll = adsk.core.ObjectCollection.create()
        for b in ents:
            coll.add(b)
        if coll.count == 0:
            return None, None, "No valid bodies resolved to pattern."
        return coll, [safe(lambda b=b: b.name) for b in ents], None

    occs, oerr = _OCCURRENCES.resolve(occurrences)
    if oerr:
        return None, None, oerr
    if not occs:
        return None, None, ("Provide 'occurrences' (occurrence name(s)) or 'bodies' (body "
                            "handles/names) to pattern.")
    coll = adsk.core.ObjectCollection.create()
    for o in occs:
        coll.add(o)
    return coll, [safe(lambda o=o: o.name) for o in occs], None


def _direction_key(kind, raw):
    """The direction as the caller gave it, falling back to the kind's default for a blank."""
    return raw if isinstance(raw, str) and raw.strip() else kind.default


def _in_context(kind, ent, comp, design):
    """(entity, error) - the direction entity in a form the pattern's component can consume."""
    occ, err = _inputs.single_placement(f"'{kind.name}': that direction", ent, comp, design)
    if err:
        return None, err
    if occ is None:
        return ent, None
    proxy = safe(lambda: ent.createForAssemblyContext(occ))
    if proxy is None:
        path = safe(lambda: occ.fullPathName) or "its one occurrence"
        return None, (f"'{kind.name}': that direction could not be brought into the pattern's "
                      f"assembly context ({path}). Pass a handle at geometry in the pattern's own "
                      "component, or a world axis (x/y/z).")
    return proxy, None


def _direction_entity(kind, raw, comp, design):
    """(entity, error) for one direction/axis input: a world-axis key becomes `comp`'s own origin
    ConstructionAxis, anything else the resolved entity in `comp`'s assembly context."""
    key = _direction_key(kind, raw)
    tagged, err = kind.resolve(key)
    if err:
        return None, err
    if tagged and tagged[0] == "edge":
        return _in_context(kind, tagged[1], comp, design)
    ent = _inputs.world_construction_axis(comp, key)
    if ent is None:
        return None, (f"'{kind.name}': component '{safe(lambda: comp.name)}' has no {key} origin "
                      "construction axis. Pass a find_geometry handle at a straight edge or sketch "
                      "line instead.")
    return ent, None


def _direction_label(kind, raw, ent):
    """What the direction/axis RESOLVED to, for the payload: the world-axis key, else the entity's
    own name falling back to its type - a BRepEdge and a SketchLine carry no name."""
    key = _direction_key(kind, raw)
    if key.lower() in _inputs.WORLD_AXIS_ATTRS:
        return key.lower()
    name = safe(lambda: ent.name)
    return name if isinstance(name, str) and name else type(ent).__name__


def _owning_component(design, coll, bodies):
    """The component the pattern feature must be built in - the bodies' parent for a body pattern,
    else root. Falls back to root if a parent can't be read."""
    root = safe(lambda: design.rootComponent)
    if bodies not in (None, "", []):
        first = safe(lambda: coll.item(0))
        parent = safe(lambda: first.parentComponent) if first is not None else None
        if parent is not None:
            return parent
    return root
