# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: project existing geometry into a sketch - Fusion's Project command - plus the
two projections that do not land flat on the sketch plane: curves ONTO model faces
(Sketch.projectToSurface) and a section through the sketch plane (Sketch.intersectWithSketchPlane).
WRITES. Only into_sketch's project2 carries a link flag; the other two always create reference
curves.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, all_sketch_names
from . import _assert
from . import _common
from . import _inputs
from . import _outputs
from . import _sketch_detail

app = adsk.core.Application.get()

_ACTIONS = ("into_sketch", "to_surface", "intersect")
_ACTION = _inputs.Choice("action", list(_ACTIONS), default="into_sketch")

# The geometry to project: find_geometry handles at edges/faces/vertices (a face projects all its
# edges). require="any" so faces, edges, and vertices are all accepted; the Sketch method rejects
# anything it can't take and that surfaces as the mutation error.
_ENTITIES = _inputs.GeometryHandleList("entities", require="any", required=True,
    description="A face gives all its edges.")

# projectToSurface's first argument is typed std::vector<BRepFace>, so a non-face here is a SWIG
# TypeError rather than a wrong-but-working call.
_TARGET_FACES = _inputs.GeometryHandleList("target_faces", require="face", required=True)

_CURVE_HANDLES = _inputs.GeometryHandleList("curve_handles", require="edge")

_BODIES = _inputs.BodyRefList("bodies")

_PROJECT_TYPE = _inputs.Choice("project_type", ["closest_point", "along_vector"])

# projectToSurface wants an ENTITY for directionEntity (a ConstructionAxis is accepted), so
# entity_only refuses a face handle - a face resolves to a direction VECTOR the call cannot consume.
_DIRECTION = _inputs.AxisRef("direction", entity_only=True)

# The sketch-entity ref kinds sketch_constrain / sketch_dimension can address ('<type>:<index>') -
# _common.ENTITY_REF_KINDS is the single source of truth; defer to it instead of a local copy that
# can drift.
_ADDRESSABLE = _common.ENTITY_REF_KINDS

_REFS_NOTE = ("'entity_refs' are '<type>:<index>' handles for sketch_constrain / sketch_dimension ("
              + "/".join(_ADDRESSABLE) + ").")
_SHORTFALL_NOTE = (" Some created curves have no '<type>:<index>' ref - list them with "
                   "sketch_get(include_entities=true).")

# input name -> the actions whose Sketch method can consume it. Each action calls a different method
# with a different signature, so an input the chosen method has no argument for is refused; accepting
# it would put a value in the payload that never reached Fusion.
_ACTION_ONLY = (
    ("link", ("into_sketch",)),
    ("entities", ("into_sketch", "intersect")),
    ("target_faces", ("to_surface",)),
    ("source_sketch", ("to_surface",)),
    ("source_component", ("to_surface",)),
    ("curve_refs", ("to_surface",)),
    ("curve_handles", ("to_surface",)),
    ("project_type", ("to_surface",)),
    ("direction", ("to_surface",)),
    ("bodies", ("intersect",)),
)

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsValue("entity_refs",
        "the created sketch-entity refs ('line:0','circle:1',...) to constrain/dimension",
        consumers=["sketch_constrain", "sketch_dimension"]),
]


def _addressable_counts(sketch) -> dict:
    """Per-collection counts for the ref-addressable kinds, in creation order (matches
    _common.resolve_entity_ref's indexing so the reported refs resolve back to these entities)."""
    counts = {}
    for kind in _ADDRESSABLE:
        coll = _common.entity_collection(sketch, kind)
        counts[kind] = safe(lambda coll=coll: coll.count, 0) if coll is not None else 0
    return counts


def _new_refs(before: dict, after: dict) -> list:
    """The '<type>:<index>' refs for entities that appeared in each addressable collection - the tail
    range [before, after) each holds the newly-created entities of that kind."""
    refs = []
    for kind in _ADDRESSABLE:
        for i in range(before.get(kind, 0), after.get(kind, 0)):
            refs.append(f"{kind}:{i}")
    return refs


def _delta(before: dict, after: dict) -> int:
    """How many addressable entities appeared across all tracked collections."""
    return sum(after.get(k, 0) - before.get(k, 0) for k in _ADDRESSABLE)


def _as_list(created) -> list:
    """The entities a Sketch projection returned, as a plain list."""
    if created is None:
        return []
    return safe(lambda: list(created), []) or []


def _refuse_foreign_inputs(act: str, supplied: dict) -> str:
    """The error naming an input that belongs to a different action, or ''."""
    for name, owners in _ACTION_ONLY:
        if act not in owners and supplied.get(name) not in (None, "", [], {}):
            return (f"'{name}' applies only to action=" + " / ".join(f"'{o}'" for o in owners)
                    + f"; action='{act}' calls a different Sketch method, which has no argument "
                    "for it.")
    return ""


def _flag_tally(items, attr):
    """(true, false, unreadable) counts for a boolean field read off each created entity. A field
    that will not read is counted apart from one that read False - an unreadable read is evidence of
    neither state, so it can never be folded into a claim about the geometry."""
    yes = no = unknown = 0
    for c in items:
        v = safe(lambda c=c: getattr(c, attr))
        if v is True:
            yes += 1
        elif v is False:
            no += 1
        else:
            unknown += 1
    return yes, no, unknown


def _landed_flag(yes: int, no: int, unknown: int):
    """read_flag semantics over a whole created set, from its _flag_tally counts: True/False when
    every created entity agrees, None when nothing was returned, the field would not read, or the
    entities disagree."""
    n = yes + no + unknown
    if not n or unknown:
        return None
    if yes == n:
        return True
    if no == n:
        return False
    return None


def _link_note(landed, requested: bool, yes: int, no: int, unknown: int) -> str:
    """The sentence about a project2 result's linkage, worded from the isLinked read-back on the
    created curves - each branch states only what it observed."""
    asked = "true" if requested else "false"
    if landed is True:
        return "Linked: the curves read back as LINKED, so they update when the source moves."
    if landed is False:
        return ("Static copy: the curves read back as NOT linked, so they do not track the source "
                "geometry.")
    n = yes + no + unknown
    unconfirmed = f"link={asked} is the value REQUESTED, not a confirmed state."
    if not n:
        return "project2 returned no entities to read, so " + unconfirmed
    if unknown == n:
        return f"isLinked did not read on any of the {n} created curves, so " + unconfirmed
    if unknown:
        return f"isLinked did not read on {unknown} of the {n} created curves, so " + unconfirmed
    return (f"Of {n} created curves, {yes} read back as LINKED and {no} did not - a split result, "
            f"so no one linkage holds for them ({unconfirmed})")


def _linkage_note(n: int, ref_yes: int, ref_no: int, ref_unknown: int, source: str) -> str:
    """The sentence about the created curves' linkage, worded from the isReference read-back. Empty
    when the call returned nothing to read - with zero reads there is no linkage to claim either way.
    A field that would not read is reported apart from one that read False."""
    if not n:
        return ""
    if ref_yes == n:
        return f"All {n} read back as REFERENCE curves, each linked to {source}."
    if ref_unknown == n:
        return (f"This projection creates REFERENCE curves linked to {source}; isReference did not "
                "read on the created curves.")
    return (f"Of {n} created curves, {ref_yes} read back as REFERENCE curves linked to {source}"
            + (f", {ref_no} did not" if ref_no else "")
            + (f", and isReference did not read on {ref_unknown}" if ref_unknown else "")
            + ".")


def _plane_note(n: int, flat_yes: int, flat_no: int, flat_unknown: int) -> str:
    """Where the to_surface curves landed, worded from the is2D read-back. is2D is False for a curve
    lying on a 3D face and True for one on the sketch x-y plane. Empty when the call returned nothing
    to read - a universal about where the curves landed needs at least one read behind it."""
    if not n:
        return ""
    if flat_unknown == 0 and flat_no == n:
        return ("All of them read as lying on the face, off the sketch x-y plane, so they do not "
                "close a profile to extrude.")
    if flat_unknown == 0 and flat_yes == n:
        return "All of them read as lying on the sketch x-y plane, not on the target face(s)."
    return (f"{flat_no} read as lying on the face and {flat_yes} on the sketch x-y plane"
            + (f"; is2D did not read on {flat_unknown}." if flat_unknown else ".")
            + " A curve on the face does not close a profile to extrude.")


def _ref_list(raw) -> list:
    """The '<type>:<index>' ids in a JSON list or a comma-separated string (an id holds a colon, not
    a comma, so splitting on commas cannot shred one)."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, (list, tuple)):
        return [str(r).strip() for r in raw if str(r).strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def _resolve_curve(sketch, ref, label):
    """(curve, error) for a '<type>:<index>' reference read against `sketch`."""
    ent = _common.resolve_entity_ref(sketch, ref)
    if ent is None:
        return None, (f"'{label}' did not resolve '{ref}' in sketch "
                      f"'{safe(lambda: sketch.name)}'. Use '<type>:<index>', type = "
                      + "/".join(_ADDRESSABLE)
                      + " - ids come from sketch_get(include_entities=true).")
    return ent, None


def _same_sketch(a, b):
    """True when two sketch reads denote the SAME sketch, False for DIFFERENT ones, None when the
    comparison COULD NOT BE MADE - so callers branch on all three states."""
    # An entityToken is DOCUMENT-LOCAL: two components brought in by two references of ONE source
    # design read byte-identical tokens, so a token compare answers "same sketch" for two different
    # ones. ``_common.native_identity`` carries the urn half that tells them apart.
    if a is None or b is None:
        return None
    if a is b:
        return True
    ia, ib = _common.native_identity(a), _common.native_identity(b)
    if ia is None or ib is None:
        return None
    return ia == ib


def _source_label(ent) -> str:
    """A name for one source entity in the payload/error: its own name, else the body it sits on."""
    nm = safe(lambda: ent.name)
    if isinstance(nm, str) and nm:
        return nm
    body = safe(lambda: ent.body)
    bn = safe(lambda: body.name) if body is not None else None
    cls = type(ent).__name__
    return f"{cls} on {bn}" if isinstance(bn, str) and bn else cls


def _source_key(ent):
    """The comparable identity of a source entity, or None when unreadable. entityToken is the key -
    a body read twice is not the same python object, so identity cannot match a created curve's
    referencedEntity back to the input it came from."""
    tok = safe(lambda: ent.entityToken)
    return tok if isinstance(tok, str) and tok else None


def _attribute(sources, labels, items) -> list:
    """[{source, created}] - how many curves each input entity contributed. A created curve's
    referencedEntity is the body or face it was sectioned from (measured for BRepBody and BRepFace
    inputs); it reads null for a non-parametric reference, which drops the split."""
    counts = {}
    for c in items:
        ref = safe(lambda c=c: c.referencedEntity)
        key = _source_key(ref) if ref is not None else None
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return [{"source": lab, "created": counts.get(_source_key(s), 0)}
            for s, lab in zip(sources, labels)]


def _source_component(ent):
    """The component that OWNS a source entity - its own parentComponent, or, for a face/edge/vertex,
    the parent component of the body it belongs to. None when neither reads."""
    comp = safe(lambda: ent.parentComponent)
    if comp is not None:
        return comp
    body = safe(lambda: ent.body)
    return safe(lambda: body.parentComponent) if body is not None else None


def _refuse_foreign_context(sketch, sources, labels) -> str:
    """The error naming a source the sketch's component cannot section, or ''; a component that will
    not read is left alone."""
    # intersectWithSketchPlane accepts only entities owned by the sketch's OWN component context: an
    # occurrence PROXY body creates nothing and does not raise, and a foreign component's NATIVE
    # body raises '2 : InternalValidationError : Utils::getObjectPath(...)'.
    sk_comp = safe(lambda: sketch.parentComponent)
    if sk_comp is None:
        return ""
    for ent, label in zip(sources, labels):
        comp = _source_component(ent)
        # `is not False`: only a PROVEN difference refuses; an identity that did not read is not one.
        if comp is None or _common.same_component(comp, sk_comp) is not False:
            continue
        owner = safe(lambda comp=comp: comp.name) or "another component"
        return (f"'{label}' lives in component '{owner}' but sketch "
                f"'{safe(lambda: sketch.name)}' is in "
                f"'{safe(lambda: sk_comp.name)}': intersectWithSketchPlane accepts only entities "
                "owned by the sketch's own component - an occurrence proxy is silently ignored and "
                f"a foreign component's native body raises InternalValidationError. Create the "
                f"sketch in '{owner}' with sketch_create (on a face or plane there) and re-run.")
    return ""


def _plane_label(sketch) -> str:
    """The name of the plane/face the sketch sits on, for the nothing-crossed error."""
    rp = safe(lambda: sketch.referencePlane)
    nm = safe(lambda: rp.name) if rp is not None else None
    return nm if isinstance(nm, str) and nm else "its plane"


def _surface_project_type(key):
    """The adsk.fusion.SurfaceProjectTypes member for a project_type option."""
    if key == "along_vector":
        return adsk.fusion.SurfaceProjectTypes.AlongVectorSurfaceProjectType
    return adsk.fusion.SurfaceProjectTypes.ClosestPointSurfaceProjectType


def _direction_entity(raw, comp):
    """(directionEntity, error) for an along_vector projection. A world-axis key becomes `comp`'s own
    origin ConstructionAxis - a ConstructionAxis is accepted as the directionEntity, while the
    direction VECTOR an AxisRef resolves a world key to is not an entity the call can consume."""
    tagged, err = _DIRECTION.resolve(raw)
    if err:
        return None, err
    if tagged and tagged[0] == "edge":
        return tagged[1], None
    key = (raw or "").strip().lower()
    ent = _inputs.world_construction_axis(comp, key)
    if ent is None:
        return None, (f"'direction': component '{safe(lambda: comp.name)}' has no {key} origin "
                      "construction axis. Pass a find_geometry handle at a straight edge or sketch "
                      "line instead.")
    return ent, None


def _source_curves(design, sketch, source_sketch, curve_refs, curve_handles, source_component=""):
    """(curves, error) for projectToSurface. Sketch curves must live in a DIFFERENT sketch from the
    one being projected into: Fusion refuses a self-projection with 'Not support projecting sketch
    geometry into same sketch, please change the target sketch or geometry.'"""
    curves = []
    refs = _ref_list(curve_refs)
    if refs:
        name = (source_sketch or "").strip()
        if not name:
            return None, ("'curve_refs' needs 'source_sketch' - the sketch those ids are read "
                          "against, which must not be the sketch being projected into.")
        # The source is a SECOND by-name sketch reference, so it gets its own scope: 'component'
        # narrows the sketch being projected INTO and would not narrow this one.
        src, refusal = _sketch_detail.scoped_sketch(design, name, source_component,
                                                    "source_component")
        if refusal:
            return None, refusal
        if src is None:
            return None, (f"No sketch named '{name}'. Available: "
                          + (", ".join(n for n in all_sketch_names(design) if n) or "(none)"))
        # `is True`: this refusal STATES the two references are one sketch, so only a PROVEN match
        # raises it. An identity that would not read supports no such claim - the call goes through
        # and Fusion's own refusal (quoted below) reaches the caller verbatim if it was right.
        if _same_sketch(src, sketch) is True:
            return None, (f"'source_sketch' is '{name}', the sketch being projected into. Fusion "
                          "refuses that: 'Not support projecting sketch geometry into same sketch, "
                          "please change the target sketch or geometry.' Draw the curves in another "
                          "sketch, or set 'sketch_name' to a different receiving sketch.")
        for ref in refs:
            ent, rerr = _resolve_curve(src, ref, "curve_refs")
            if rerr:
                return None, rerr
            curves.append(ent)
    if curve_handles not in (None, "", []):
        edges, herr = _CURVE_HANDLES.resolve(curve_handles)
        if herr:
            return None, herr
        curves.extend(edges)
    if not curves:
        return None, ("action='to_surface' needs the curves to project: 'curve_refs' ("
                      "'<type>:<index>' ids in 'source_sketch') and/or 'curve_handles' "
                      "(find_geometry handles at model edges).")
    return curves, None


def _into_sketch(sketch, entities, link) -> dict:
    """project2(entities, isLinked) - the modern Project (project is deprecated and has no link
    control). Pass the resolved live entities as a list."""
    ents, eerr = _ENTITIES.resolve(entities)
    if eerr:
        return error(eerr)
    requested = True if link is None else bool(link)

    before = _addressable_counts(sketch)
    try:
        created = sketch.project2(ents, requested)
    except Exception as e:
        return error(f"Projection failed in sketch '{safe(lambda: sketch.name)}': {e}")

    # Honesty read-back: a projection that adds nothing is a failure, not a false ok. Trust the count
    # of entities the API says it created, then confirm the collections actually grew.
    items = _as_list(created)
    created_count = len(items)
    after = _addressable_counts(sketch)
    if created_count <= 0 and _delta(before, after) <= 0:
        return error(
            f"Projection created no sketch entities in '{safe(lambda: sketch.name)}'. The geometry "
            "may already be projected, or lies out of the sketch plane's projectable set. Nothing "
            "was added.")

    refs = _new_refs(before, after)
    # 'linked' is READ OFF the created curves - the same read-back shape the other two actions use
    # for isReference - so it is the landed state, never the request echoed at the caller.
    link_yes, link_no, link_unknown = _flag_tally(items, "isLinked")
    linked = _landed_flag(link_yes, link_no, link_unknown)
    # project2 honours its isLinked argument exactly, so a set that UNANIMOUSLY reads the opposite
    # of the request is the call failing. Split / unreadable / empty results support no such claim
    # and stay disclosed in the note instead.
    if linked is not None and linked != requested:
        return error(
            f"link={'true' if requested else 'false'} was requested, but all {created_count} "
            f"curve(s) project2 created in sketch '{safe(lambda: sketch.name)}' read back as "
            + ("LINKED" if linked else "NOT linked")
            + ". The curves WERE created and remain in the sketch"
            + (" (" + ", ".join(refs) + ")" if refs else "")
            + " - delete them with sketch_delete_entity if that linkage is wrong for the job.")
    note = ("Geometry projected. " + _REFS_NOTE + " "
            + _link_note(linked, requested, link_yes, link_no, link_unknown)
            + " Extrude a resulting profile via sketch_get -> model_extrude.")
    if created_count > len(refs):
        note += _SHORTFALL_NOTE

    return ok({
        "projected": True,
        "action": "into_sketch",
        "sketch": safe(lambda: sketch.name),
        "linked": linked,
        "link_requested": requested,
        "created_count": created_count,
        "entity_refs": refs,
        "note": note,
    })


def _to_surface(design, sketch, target_faces, source_sketch, curve_refs, curve_handles,
                project_type, direction, source_component="") -> dict:
    """projectToSurface(faces, curves, projectType[, directionEntity]) - FACES first, plain lists."""
    faces, ferr = _TARGET_FACES.resolve(target_faces)
    if ferr:
        return error(ferr)
    curves, cerr = _source_curves(design, sketch, source_sketch, curve_refs, curve_handles,
                                  source_component)
    if cerr:
        return error(cerr)
    # 'closest_point' when omitted, which is NOT a schema default: _refuse_foreign_inputs refuses a
    # project_type on the other actions, so sending it is not the same call as omitting it.
    ptype, perr = _PROJECT_TYPE.resolve(project_type or "closest_point")
    if perr:
        return error(perr)

    dir_ent = None
    if ptype == "along_vector":
        # Fusion raises '3 : invalid parameter directionEntity' rather than falling back to the
        # closest-point method, so a missing direction is refused before the mutation.
        if not (direction or "").strip():
            return error("project_type='along_vector' needs 'direction' - Fusion refuses the call "
                         "without it ('3 : invalid parameter directionEntity') and does not fall "
                         "back to closest_point.")
        dir_ent, derr = _direction_entity(direction, safe(lambda: sketch.parentComponent))
        if derr:
            return error(derr)
    elif (direction or "").strip():
        return error("'direction' steers project_type='along_vector' only; closest_point ignores "
                     "it, so it is refused here rather than reported as if it had been used.")

    before = _addressable_counts(sketch)
    try:
        if dir_ent is None:
            created = sketch.projectToSurface(faces, curves, _surface_project_type(ptype))
        else:
            created = sketch.projectToSurface(faces, curves, _surface_project_type(ptype), dir_ent)
    except Exception as e:
        return error(f"projectToSurface failed in sketch '{safe(lambda: sketch.name)}': {e}")

    items = _as_list(created)
    created_count = len(items)
    after = _addressable_counts(sketch)
    if created_count <= 0 and _delta(before, after) <= 0:
        return error(
            f"projectToSurface created no curves in '{safe(lambda: sketch.name)}': "
            f"{len(curves)} source curve(s) onto {len(faces)} target face(s) with "
            f"project_type='{ptype}' added nothing.")

    refs = _new_refs(before, after)
    flat_yes, flat_no, flat_unknown = _flag_tally(items, "is2D")
    ref_yes, ref_no, ref_unknown = _flag_tally(items, "isReference")
    note = " ".join(p for p in (
        "Curves projected onto the target face(s).",
        _linkage_note(created_count, ref_yes, ref_no, ref_unknown,
                      "the source curve it came from"),
        _plane_note(created_count, flat_yes, flat_no, flat_unknown),
        (f"projectToSurface returned no entities, so the {len(refs)} new sketch entities are "
         "reported from the collection census alone." if not created_count else ""),
        _REFS_NOTE) if p)
    if created_count > len(refs):
        note += _SHORTFALL_NOTE

    return ok({
        "projected": True,
        "action": "to_surface",
        "sketch": safe(lambda: sketch.name),
        "target_face_count": len(faces),
        "source_curve_count": len(curves),
        "project_type": ptype,
        "created_count": created_count,
        # only the curves that READ as off the sketch x-y plane, i.e. on the target face
        "off_plane_count": flat_no,
        "entity_refs": refs,
        "note": note,
    })


def _intersect(sketch, entities, bodies) -> dict:
    """intersectWithSketchPlane(entities) - the section curves where bodies/faces cross the plane."""
    sources = []
    if entities not in (None, "", []):
        ents, eerr = _ENTITIES.resolve(entities)
        if eerr:
            return error(eerr)
        sources.extend(ents)
    if bodies not in (None, "", []):
        bods, berr = _BODIES.resolve(bodies)
        if berr:
            return error(berr)
        sources.extend(bods)
    if not sources:
        return error("action='intersect' needs 'bodies' (the bodies to section) and/or 'entities' "
                     "(find_geometry handles at faces/edges/vertices) to cross with the sketch "
                     "plane.")
    labels = [_source_label(s) for s in sources]
    cerr = _refuse_foreign_context(sketch, sources, labels)
    if cerr:
        return error(cerr)

    before = _addressable_counts(sketch)
    try:
        created = sketch.intersectWithSketchPlane(sources)
    except Exception as e:
        return error(f"intersectWithSketchPlane failed in sketch '{safe(lambda: sketch.name)}': {e}")

    # An entity that misses the plane returns an EMPTY result with no error, so zero created is this
    # tool's own refusal - the API will not raise one.
    items = _as_list(created)
    created_count = len(items)
    after = _addressable_counts(sketch)
    if created_count <= 0 and _delta(before, after) <= 0:
        # The up-front context guard only refuses a source whose owning component READS as a
        # different one, so a proxy whose owner is unreadable can still land here - name that as the
        # second candidate cause rather than blaming the plane alone.
        return error(
            f"Nothing you passed crosses the plane of sketch '{safe(lambda: sketch.name)}' "
            f"({_plane_label(sketch)}), so no sketch geometry was created: " + ", ".join(labels)
            + ". Move the sketch plane through the geometry, or pass geometry that spans it. An "
            "occurrence proxy is also ignored silently: the entity must be owned by the sketch's "
            "own component.")

    refs = _new_refs(before, after)
    ref_yes, ref_no, ref_unknown = _flag_tally(items, "isReference")
    note = " ".join(p for p in (
        "Section curves created where the geometry crosses the sketch plane.",
        _linkage_note(created_count, ref_yes, ref_no, ref_unknown,
                      "the body or face it was sectioned from"),
        ("They lie on the sketch plane." if created_count else
         f"intersectWithSketchPlane returned no entities, so the {len(refs)} new sketch entities "
         "are reported from the collection census alone."),
        _REFS_NOTE) if p)

    out = {
        "projected": True,
        "action": "intersect",
        "sketch": safe(lambda: sketch.name),
        "source_count": len(sources),
        "created_count": created_count,
        "entity_refs": refs,
    }

    per_source = _attribute(sources, labels, items)
    # Only publish the split when every created curve was matched back to an input; an unmatched
    # curve would otherwise show up as a source that contributed nothing.
    if sum(r["created"] for r in per_source) == created_count:
        out["per_source"] = per_source
        zero = [r["source"] for r in per_source if r["created"] == 0]
        if zero:
            note += (f" {len(zero)} of the {len(sources)} entities you passed do not cross the "
                     "plane and contributed nothing: " + ", ".join(zero) + ".")
    if created_count > len(refs):
        note += _SHORTFALL_NOTE
    out["note"] = note
    return ok(out)


def handler(entities="", sketch_name: str = "", link: bool = None, action: str = "into_sketch",
            target_faces="", source_sketch: str = "", curve_refs="", curve_handles="",
            project_type: str = "", direction: str = "", bodies="", component: str = "",
            source_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    act, aerr = _ACTION.resolve(action)
    if aerr:
        return error(aerr)
    ferr = _refuse_foreign_inputs(act, {
        "link": link, "entities": entities, "target_faces": target_faces,
        "source_sketch": source_sketch, "source_component": source_component,
        "curve_refs": curve_refs, "curve_handles": curve_handles,
        "project_type": project_type, "direction": direction, "bodies": bodies})
    if ferr:
        return error(ferr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    sk, requested, refusal = _sketch_detail.scoped_or_recent_sketch(design, sketch_name, component)
    if refusal:
        return error(refusal)
    if not sk:
        if requested:
            names = all_sketch_names(design)
            return error(f"No sketch named '{requested}'. Available: "
                         + (", ".join(n for n in names if n) or "(none)")
                         + ". Create one with sketch_create.")
        return error("No sketch to project into. Create one first with sketch_create.")

    if act == "to_surface":
        return _to_surface(design, sk, target_faces, source_sketch, curve_refs, curve_handles,
                           project_type, direction, source_component)
    if act == "intersect":
        return _intersect(sk, entities, bodies)
    return _into_sketch(sk, entities, link)


TOOL_DESCRIPTION = (
    "Create sketch curves from model geometry.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="sketch_project", description=TOOL_DESCRIPTION)
    .add_input_property(*_ACTION.as_property())
    .add_input_property("entities", _ENTITIES.schema())
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("link", {"type": "boolean"})
    .add_input_property(*_TARGET_FACES.as_property())
    .add_input_property("source_sketch", {"type": "string"})
    .add_input_property("source_component", {"type": "string"})
    .add_input_property("curve_refs", {"type": "array", "items": {"type": "string"}})
    .add_input_property(*_CURVE_HANDLES.as_property())
    .add_input_property(*_PROJECT_TYPE.as_property())
    .add_input_property(*_DIRECTION.as_property())
    .add_input_property(*_BODIES.as_property())
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The fingerprint gate: a projection that added no curve to the target sketch is an error.
    postconditions=[_assert.SketchCurvesChanged(scope_keys=("component",))],
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sketch_project.py::TestLinkReadBack"
                      "::test_curves_that_all_land_unlinked_against_the_request_are_an_error"))


def register_tool():
    register(item)
