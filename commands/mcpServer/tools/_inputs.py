# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Typed INPUT KINDS: each kind bundles schema + ``resolve()`` + validation + a contract line for
one tool input, so a tool references existing geometry through a handle or typed selector instead
of a hand-rolled ``name``/``index``. ``resolve_inputs(...)`` resolves every declared input at once."""

import math

import adsk.core
import adsk.fusion

from . import _common
from . import _geom     # owning_bodies - the ONE identity-keyed owning-body walk
from . import _joints   # the JointOrigin walk (all_joint_origins / find_joint_origins_by_name / proxy)
from ._export import find_component as _find_component   # the one design-wide by-name component resolve

MAP_BLURB = (
    "the typed reference kinds (table above); resolve_inputs/apply_to_tool (wire and resolve "
    "an input spec), length_value_input/expression_report (a length as a number OR a "
    "parameter expression), world_construction_axis/axis_line_of (a world axis as an entity; "
    "an AxisRef's line), single_placement/entity_component (the assembly-context lift), "
    "resolve_surface/surface_ref_label (the *_to_surface operand)")

app = adsk.core.Application.get()


# ── base ────────────────────────────────────────────────────────────────────

def schema_default(value) -> bool:
    """Whether `value` is a default worth carrying on the wire: a bool, a non-zero number or a
    non-empty string. None, an empty string, an empty list and 0 all mean 'omitted', which the
    schema already says by the input's absence."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and bool(value)


class InputKind:
    """One declared tool input: name + schema + how to resolve/validate it + its contract line."""

    json_type = "string"

    # A terse "what this kind references + the gotcha it avoids", read by gen_manifest.py into the
    # CLAUDE.md catalog. A kind with MAP_HINT="" shows up blank there - the signal to fill it in.
    MAP_HINT = ""

    def __init__(self, name, description="", required=False, default=None):
        self.name = name
        self.description = description
        self.required = required
        self.default = default

    def schema(self, brief=False) -> dict:
        """The JSON-schema property dict for this input (merged with the kind's contract note)."""
        return {"type": self.json_type, **self._desc(brief)}

    def as_property(self, brief=False):
        """(name, schema) for splatting into Tool.add_input_property(*kind.as_property()).
        brief=True drops the contract note - for the SECOND and later inputs of one kind on one
        tool, where the first already spelled it."""
        return self.name, self.schema(brief)

    def _full_desc(self, brief=False) -> str:
        note = "" if brief else self.contract_note()
        return (self.description + (" " + note if note else "")).strip()

    def _desc(self, brief=False) -> dict:
        """The 'description' and 'default' entries of the schema - each absent when there is
        nothing to say, so an input with no prose and no default costs the wire nothing. A
        default is STRUCTURE: the value an omitted input takes, never restated in prose."""
        out = {}
        text = self._full_desc(brief)
        if text:
            out["description"] = text
        if schema_default(self.default):
            out["default"] = self.default
        return out

    def contract_note(self) -> str:
        """One-line 'what this input needs' - assembled into the tool's CONTRACT block."""
        return ""

    def resolve(self, raw):
        """(value, error). Default: pass the raw value through (or the default if missing)."""
        if raw is None:
            if self.required:
                return None, f"'{self.name}' is required."
            return self.default, None
        return raw, None


# ── geometry handle (the ROOT-CAUSE-1 kind) ─────────────────────────────────

# require -> (human label, predicate(entity) -> bool)
_GEOMETRY_REQUIREMENTS = {
    "any": ("any geometry", lambda e: True),
    "face": ("a face", lambda e: isinstance(e, adsk.fusion.BRepFace)),
    "planar_face": ("a PLANAR face", lambda e: isinstance(e, adsk.fusion.BRepFace)
                    and _common.safe(lambda: e.geometry.surfaceType) == adsk.core.SurfaceTypes.PlaneSurfaceType),
    "cylinder_face": ("a CYLINDRICAL face", lambda e: isinstance(e, adsk.fusion.BRepFace)
                      and _common.safe(lambda: e.geometry.surfaceType) == adsk.core.SurfaceTypes.CylinderSurfaceType),
    "edge": ("an edge", lambda e: isinstance(e, adsk.fusion.BRepEdge)),
    "vertex": ("a vertex", lambda e: isinstance(e, adsk.fusion.BRepVertex)),
}


class GeometryHandle(InputKind):
    """A reference to EXISTING geometry, as a SHORT-LIVED handle from find_geometry (an entityToken).
    'require' constrains the kind (planar_face / cylinder_face / edge / vertex / face / any) at
    resolve time. Tokens are not stable across separate queries - re-find if one fails."""

    MAP_HINT = "one face/edge/vertex by find_geometry handle (require=face/edge/...), not a coordinate"

    def __init__(self, name, require="any", **kw):
        super().__init__(name, **kw)
        self.require = require if require in _GEOMETRY_REQUIREMENTS else "any"

    def contract_note(self) -> str:
        label, _ = _GEOMETRY_REQUIREMENTS[self.require]
        return f"A find_geometry 'handle' at {label}."

    def resolve(self, raw):
        h = (raw or "").strip() if isinstance(raw, str) else raw
        if not h:
            if self.required:
                return None, (f"'{self.name}' needs a geometry handle from find_geometry "
                              f"({_GEOMETRY_REQUIREMENTS[self.require][0]}).")
            return self.default, None
        des = _common.design()
        if not des:
            return None, "No active design to resolve the geometry handle against."
        # _resolve_token_entity self-heals: the entityToken first, then the handle's kind+position
        # locator when that token has gone stale.
        ent = _resolve_token_entity(des, h)
        if ent is None:
            if _LAST_REFIND_REFUSAL:
                return None, (f"'{self.name}': handle did not resolve - {_LAST_REFIND_REFUSAL}. "
                              "Re-run find_geometry for a fresh handle.")
            return None, (f"'{self.name}': handle did not resolve - the entityToken is stale AND no "
                          "geometry locator recovered it. Re-run find_geometry for a fresh handle "
                          "(the geometry itself may have changed, or this isn't a find_geometry handle).")
        label, ok_pred = _GEOMETRY_REQUIREMENTS[self.require]
        if not ok_pred(ent):
            return None, (f"'{self.name}' must be {label}, but the handle points at a "
                          f"{type(ent).__name__}. Use find_geometry(kind=...) to get the right one.")
        return ent, None


class GeometryHandleList(GeometryHandle):
    """A LIST of geometry handles (e.g. the specific edges to fillet, the bodies to mirror). Accepts a
    JSON list of handles OR a comma-separated string, each resolved through GeometryHandle."""

    json_type = "array"
    MAP_HINT = "several faces/edges by handles (fillet/drill THESE)"

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, **self._desc(brief)}

    def contract_note(self) -> str:
        label, _ = _GEOMETRY_REQUIREMENTS[self.require]
        return f"find_geometry 'handle's at {label}."

    def resolve(self, raw):
        if raw is None or raw == "" or raw == []:
            if self.required:
                return None, (f"'{self.name}' needs a list of geometry handles from find_geometry "
                              f"({_GEOMETRY_REQUIREMENTS[self.require][0]}).")
            return (self.default if self.default is not None else []), None
        if isinstance(raw, (list, tuple)):
            items = list(raw)
        elif isinstance(raw, str) and _HANDLE_SEP in raw:
            # A COMPOSITE handle ('<token>|@<kind>:x,y,z') carries commas INSIDE its locator, so
            # comma-splitting one shreds it into fragments. A '|@' string is ONE handle.
            items = [raw.strip()]
        else:
            items = [s.strip() for s in str(raw).split(",") if s.strip()]
        ents = []
        for i, h in enumerate(items):
            ent, err = GeometryHandle.resolve(self, h)
            if err:
                return None, f"'{self.name}'[{i}]: {err}"
            ents.append(ent)
        if not ents:
            return None, f"'{self.name}': no valid handles resolved."
        return ents, None


# ── edge-loop / boundary reference (a SET of edge/curve handles treated as a boundary) ──────────

class EdgeLoopRef(GeometryHandleList):
    """A boundary defined by edge handles from find_geometry.
    closed=True is a CLOSED loop, closed=False an OPEN chain of OUTER edges from ONE body (a
    multi-body chain is rejected before any mutation). Resolves to (ObjectCollection, {entities,
    body_count}); a single edge is allowed, Fusion auto-finding the connected loop."""

    MAP_HINT = "a closed/open edge-loop boundary from edge handles"

    def __init__(self, name, closed=True, **kw):
        super().__init__(name, require="edge", **kw)
        self.closed = closed

    def contract_note(self) -> str:
        if self.closed:
            return "Edge 'handle's forming a CLOSED loop (one edge suffices)."
        return "Edge 'handle's forming an OPEN chain on one surface body."

    def resolve(self, raw):
        ents, err = super().resolve(raw)        # reuse handle resolution + staleness + edge-kind check
        if err:
            return None, err
        if not ents:
            if self.required:
                return None, f"'{self.name}' needs at least one edge handle from find_geometry."
            return (None, {"entities": [], "body_count": 0}), None
        # Through the ONE shared walk: edge.body hands back a FRESH PROXY per read (measured), so
        # an id()-keyed set counts one body once PER EDGE, refusing a legal single-body chain.
        body_count = len(_geom.owning_bodies(ents))
        if not self.closed and body_count > 1:
            return None, (f"'{self.name}': the edges to extend must all come from ONE surface body, "
                          "but they span more than one. Pass only the outer edges of a single body.")
        coll = adsk.core.ObjectCollection.create()
        for e in ents:
            coll.add(e)
        return (coll, {"entities": ents, "body_count": body_count}), None


# ── body reference (name OR handle - bodies have auto-names, so a handle is the precise path) ───

def _resolve_token_entity(des, s):
    """The entity `s` resolves to as an entityToken handle when it names exactly ONE, else None -
    handle-vs-name is never guessed from the string's shape, just asked of findEntityByToken. A
    dead token self-heals through the composite handle's kind+position locator."""
    # MEASURED: findEntityByToken returns a VECTOR - splitting a face made the pre-split token
    # resolve to BOTH survivors - so taking the first acts on geometry the caller never picked. The
    # locator decides between them, or the handle is REFUSED via _LAST_REFIND_REFUSAL.
    global _LAST_REFIND_REFUSAL
    _LAST_REFIND_REFUSAL = None
    if not isinstance(s, str) or not s:
        return None
    token, locator = _split_handle(s)
    found = _common.safe(lambda: des.findEntityByToken(token))
    hits = list(found) if found else []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        picked, reason = _pick_by_locator(hits, locator) if locator else (None, None)
        if picked is not None:
            return picked
        _LAST_REFIND_REFUSAL = _ambiguous_token_refusal(hits, locator, reason)
        return None
    # token dead -> re-find by the locator (kind + world position), if the handle carries one.
    if locator:
        return _refind_by_locator(des, locator)
    return None


def _entity_context_label(ent):
    """What tells one candidate of an ambiguous token from the others: its type plus the assembly
    path it is placed in, else its owning body/component."""
    kind = type(ent).__name__
    where = (_common.safe(lambda: ent.assemblyContext.fullPathName)
             or _common.safe(lambda: ent.body.parentComponent.name)
             or _common.safe(lambda: ent.parentComponent.name))
    return f"{kind} in '{where}'" if where else kind


# Narrower than the shared cap: this list is one clause of a sentence a consuming kind wraps in its
# own, and every candidate reads as the same entity type in a different place.
_TOKEN_CANDIDATES_LISTED = 4


def _ambiguous_token_refusal(hits, locator, locator_reason=None):
    """The reason text for a token that resolved to SEVERAL entities - the COUNT, each candidate
    (through the shared capped-list renderer), and why the locator did not settle it."""
    cands = _common.named_with_remainder([_entity_context_label(e) for e in hits],
                                         cap=_TOKEN_CANDIDATES_LISTED)
    why = (locator_reason if locator_reason else
           ("this handle's position locator matched none of them" if locator else
            "this bare token carries no position locator to tell them apart"))
    return (f"the token resolved to {len(hits)} entities - {cands} - and {why}. A split face "
            "resolves this way, so acting on any of them would be a guess")


# Why the last handle resolution was REFUSED (set by _resolve_token_entity / _refind_by_locator,
# cleared per resolve) - an error-detail channel only, never driving behavior.
_LAST_REFIND_REFUSAL = None


def _handle_refusal_suffix():
    """The clause a NAME-fallback miss appends when the same value was also refused as a HANDLE, so
    "that named nothing" does not stand for a token that resolved to several entities or died.
    Empty when no handle refusal is pending."""
    if not _LAST_REFIND_REFUSAL:
        return ""
    return (f" It was also tried as a handle: {_LAST_REFIND_REFUSAL} - re-run find_geometry for a "
            "fresh handle.")


# ── composite, self-healing geometry handle ──────────────────────────────────

# A handle find_geometry mints is '<entityToken>|@<kind>:<x>,<y>,<z>' (positions in cm, the API
# unit); the token is the fast path and the '@' locator the stale-token fallback. A bare token still
# works, with no fallback. The marker is '|@' so it cannot collide with base64 token chars.
_HANDLE_SEP = "|@"


def make_handle(entity, kind, position_cm):
    """A composite handle from a live entity: its entityToken plus a kind+position locator
    (`position_cm` is a face centroid or a point on an edge), so a later stale token self-heals. A
    BRep entity also carries its body's revisionId (';rv='), which tells benign token rotation from
    a model edit - recovering across one silently measures the WRONG entity, live-proven."""
    token = _common.safe(lambda: entity.entityToken) or ""
    if not token or position_cm is None:
        return token
    x, y, z = position_cm
    handle = f"{token}{_HANDLE_SEP}{kind}:{x:.6f},{y:.6f},{z:.6f}"
    rev = _common.safe(lambda: entity.body.revisionId)
    if rev:
        handle += f";rv={rev}"
    return handle


def is_handle(v) -> bool:
    """True if v looks like a HANDLE, not an int/index/name - a composite carries the '|@<kind>:'
    locator, a bare entityToken is a long non-numeric base64 string. An int, a list, '0,2,3',
    'all' and a short name are NOT handles."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if _HANDLE_SEP in s:
        return True
    if not s or s.lower() in ("all", "*"):
        return False
    return len(s) > 40 and not all(c.isdigit() or c in ", " for c in s)


def _split_handle(s):
    """('<token>', (kind, x, y, z, rev)) for a composite handle, else ('<token>', None) - 'rev' is
    the minting body's revisionId where the handle carries one (';rv=<id>')."""
    if not isinstance(s, str) or _HANDLE_SEP not in s:
        return s, None
    token, loc = s.split(_HANDLE_SEP, 1)
    try:
        kind, coords = loc.split(":", 1)
        rev = None
        if ";rv=" in coords:
            coords, rev = coords.split(";rv=", 1)
            rev = rev or None
        x, y, z = (float(c) for c in coords.split(","))
        return token, (kind, x, y, z, rev)
    except Exception:
        return token, None


def handle_token(s):
    """The bare entityToken part of a (possibly composite) handle - the '|@<locator>' stripped."""
    return _split_handle(s)[0] if isinstance(s, str) else s


def _entity_point_cm(ent):
    """A representative world point (cm) for a face (centroid) or edge (point on it), else None."""
    if isinstance(ent, adsk.fusion.BRepFace):
        c = _common.safe(lambda: ent.centroid)
    elif isinstance(ent, adsk.fusion.BRepEdge):
        c = _common.safe(lambda: ent.pointOnEdge)
    elif isinstance(ent, adsk.fusion.BRepVertex):
        c = _common.safe(lambda: ent.geometry)
    else:
        c = None
    return (c.x, c.y, c.z) if c else None


def _locator_sketch_remedy(sk_name):
    """The way forward a SHARED sketch name inside a profile handle's own locator can offer - the
    name was minted into the handle by the read that published it, so no scope input on the
    consuming tool narrows it and the remedy has to re-mint the handle."""
    return (f"That name lives inside the handle, so no input here narrows it - mint a fresh handle "
            f"with sketch_get(sketch_name='{sk_name}', component=<the one you mean>), which lists "
            "that sketch's profiles.")


def _tied_profile_refusal(sketches):
    """The reason text for a locator SEVERAL profiles match equally well - the count and the
    sketches holding them, `sketches` being one entry per tied profile in scan order. Re-minting
    the handle is not the way out (a fresh one ties again); the {sketch, profile_index} selector
    is, since it addresses a profile by POSITION in one named sketch rather than by geometry."""
    names = [f"'{n}'" if n else "a sketch whose name did not read"
             for n in (_common.safe(lambda s=sk: s.name) for sk in sketches)]
    return (f"{len(sketches)} profiles match this handle's locator equally well - in "
            f"{_common.named_with_remainder(names)} - and nothing in the locator separates them, "
            "so it cannot name exactly one. Address the one you mean as a "
            "{sketch, profile_index} selector instead - sketch_get lists each profile's 'index' "
            "in its own sketch.")


def _refind_profile(des, kind, want_pt):
    """The Profile a locator re-finds, or None. The kind may carry 'profile[<sketch>~<area_cm2>]':
    the sketch scopes the scan and the area tells same-centroid profiles apart (an annulus band and
    its full disk share a centroid); a locator sketch name SEVERAL sketches carry is REFUSED here."""
    # VERIFIED LIVE: findEntityByToken returns NOTHING for a sub-component sketch profile's token,
    # so for profiles the locator is the real resolution path, not just staleness recovery.
    global _LAST_REFIND_REFUSAL
    sk_name, want_area = "", None
    if "[" in kind and kind.endswith("]"):
        payload = kind[kind.index("[") + 1:-1]
        head, sep, area_s = payload.rpartition("~")
        if sep:
            try:
                want_area = float(area_s)
                sk_name = head
            except Exception:
                sk_name = payload
        else:
            sk_name = payload
    if sk_name:
        # find_sketch, not resolve_sketch: the collapsing form answers None for a name NO sketch
        # carries AND for one SEVERAL carry, so a collision reached the caller as "not found".
        sk, ambiguous = _common.find_sketch(des, sk_name, remedy=_locator_sketch_remedy(sk_name))
        if ambiguous:
            _LAST_REFIND_REFUSAL = ambiguous
            return None
        sketches = [sk] if sk is not None else []
    else:
        sketches = []
        for comp in _common.all_components(des):
            coll = _common.safe(lambda c=comp: c.sketches)
            for i in range(_common.safe(lambda: coll.count, 0) if coll else 0):
                sketches.append(coll.item(i))
    lx, ly, lz = want_pt
    best, best_score, tied = None, None, []
    at_point = []                # the sketches holding a profile AT the recorded point
    for sk in sketches:
        profs = _common.safe(lambda s=sk: s.profiles)
        for i in range(_common.safe(lambda: profs.count, 0) if profs else 0):
            p = profs.item(i)
            ap = _common.safe(lambda p=p: p.areaProperties())
            c = _common.safe(lambda: ap.centroid) if ap else None
            area = _common.safe(lambda: ap.area) if ap else None
            if c is None:
                continue
            dist = ((c.x - lx) ** 2 + (c.y - ly) ** 2 + (c.z - lz) ** 2) ** 0.5
            if dist > 0.1:                              # cm - not the recorded region
                continue
            at_point.append(sk)
            if want_area is not None:
                if area is None:
                    continue
                rel = abs(area - want_area) / max(abs(want_area), 1e-9)
                if rel > 0.01:                          # wrong region sharing the centroid
                    continue
                score = (dist, rel)
            else:
                score = (dist, 0.0)
            if best_score is None or score < best_score:
                best, best_score, tied = p, score, [sk]
            elif score == best_score:
                # An equal score leaves only the scan ORDER to pick between them, and a handle
                # naming two profiles names neither.
                tied.append(sk)
    if len(tied) > 1:
        _LAST_REFIND_REFUSAL = _tied_profile_refusal(tied)
        return None
    if best is None:
        # The scan matched nothing. A DEFERRED sketch answers `profiles` with the pre-deferral set,
        # so that is the reason to state - but only for a sketch this locator reached: a NAMELESS
        # one scans the whole design, where another deferred sketch is unrelated to the miss.
        scanned = sketches if sk_name else at_point
        for sk in scanned:
            stale = deferred_sketch_note(sk)
            if stale:
                _LAST_REFIND_REFUSAL = stale
                break
    return best


# How far (cm) a candidate may sit from a locator's recorded point and still BE that geometry: 1
# micron, far below any modelling tolerance. The ONE gate both locator paths judge on.
_LOCATOR_TOL_CM = 1e-4


def _pick_by_locator(entities, locator):
    """(the ONE entity `locator` names out of `entities`, refusal reason): the candidate must sit
    AT the recorded point, be the ONLY one there, and still carry the handle's minting revisionId.
    (None, None) means nothing sits at that point; (None, reason) means candidates were gated out."""
    # MEASURED: a concentric face split puts BOTH survivors at the identical centroid and the hit
    # order is not stable across runs, so a tie is refused rather than first-matched.
    lx, ly, lz = locator[1], locator[2], locator[3]
    want_rev = locator[4] if len(locator) > 4 else None
    best, best_d = None, None
    within_tol = 0
    for ent in entities:
        p = _entity_point_cm(ent)
        if p is None:
            continue
        d = ((p[0] - lx) ** 2 + (p[1] - ly) ** 2 + (p[2] - lz) ** 2) ** 0.5
        if d <= _LOCATOR_TOL_CM:
            within_tol += 1
        if best_d is None or d < best_d:
            best, best_d = ent, d
    if best is None or best_d is None or best_d > _LOCATOR_TOL_CM:
        return None, None
    if within_tol > 1:
        return None, (f"{within_tol} of them sit at the recorded position (co-located candidates - "
                      "a concentric split puts both survivors at one centroid), so the locator "
                      "cannot name exactly one")
    if want_rev:
        got_rev = _common.safe(lambda: best.body.revisionId)
        if got_rev != want_rev:
            # LIVE-VERIFIED: a rotated rebuild lands its record point EXACTLY on the original's, so
            # position alone cannot tell a rebuilt entity from the one the handle meant.
            return None, ("the model CHANGED since this handle was minted (the geometry at the "
                          "recorded position belongs to a different/rebuilt body), so locator "
                          "recovery would bind the wrong entity")
    return best, None


def _refind_by_locator(des, locator):
    """The entity a locator re-finds by scanning the design's BRep geometry, or None - the
    staleness recovery, gathering candidates here and leaving the match to _pick_by_locator. A
    profile locator routes to _refind_profile, since a sketch profile is not BRep."""
    global _LAST_REFIND_REFUSAL
    kind, lx, ly, lz = locator[0], locator[1], locator[2], locator[3]
    if kind.startswith("profile"):
        return _refind_profile(des, kind, (lx, ly, lz))
    root = _common.safe(lambda: des.rootComponent)
    if not root:
        return None
    want_faces = kind.endswith("face") or kind == "face"
    want_edges = kind.endswith("edge") or kind == "edge"
    want_verts = kind == "vertex"
    # Root bodies plus every occurrence's bodies (proxied) - the family find_geometry searched.
    bodies = []
    for coll in (_common.safe(lambda: root.bRepBodies),):
        n = _common.safe(lambda: coll.count, 0) if coll else 0
        bodies += [coll.item(i) for i in range(n)]
    # The shared census, not a bare root.allOccurrences: that property RAISES on a design holding
    # an unresolved reference, which would confine the self-heal to ROOT bodies.
    for o in _common.all_occurrences(des):
        coll = _common.safe(lambda o=o: o.bRepBodies)
        n = _common.safe(lambda: coll.count, 0) if coll else 0
        bodies += [coll.item(i) for i in range(n)]
    candidates = []
    for b in bodies:
        if b is None:
            continue
        if want_faces:
            fs = _common.safe(lambda b=b: b.faces)
            for i in range(_common.safe(lambda: fs.count, 0) if fs else 0):
                candidates.append(fs.item(i))
        if want_edges:
            es = _common.safe(lambda b=b: b.edges)
            for i in range(_common.safe(lambda: es.count, 0) if es else 0):
                candidates.append(es.item(i))
        if want_verts:
            vs = _common.safe(lambda b=b: b.vertices)
            for i in range(_common.safe(lambda: vs.count, 0) if vs else 0):
                candidates.append(vs.item(i))
    best, reason = _pick_by_locator(candidates, locator)
    if reason:
        _LAST_REFIND_REFUSAL = reason
    return best


def _isinstance(b, type_or_tuple) -> bool:
    """isinstance that degrades to False when the second arg is not a real class - an un-modelled
    adsk attribute under test would otherwise crash the kind discrimination."""
    try:
        return isinstance(b, type_or_tuple)
    except TypeError:
        return False


def _is_brep(b) -> bool:
    """True if `b` is a BRepBody (solid OR open surface)."""
    return _isinstance(b, adsk.fusion.BRepBody)


def _is_mesh(b) -> bool:
    """True if `b` is a MeshBody - NOT a BRepBody: the two live in separate collections
    (bRepBodies vs meshBodies) and only one of these predicates ever holds."""
    return _isinstance(b, adsk.fusion.MeshBody)


# kind -> (human label, predicate(body) -> bool), reading isSolid LIVE each call. 'brep' accepts a
# SOLID or an OPEN SURFACE but EXCLUDES a mesh; 'any' accepts all three.
_BODY_KINDS = {
    "solid":   ("a SOLID body",          lambda b: _is_brep(b) and bool(_common.safe(lambda: b.isSolid))),
    "surface": ("an OPEN SURFACE body",  lambda b: _is_brep(b) and not bool(_common.safe(lambda: b.isSolid))),
    "brep":    ("a BRep (non-mesh) body",   lambda b: _is_brep(b)),
    "mesh":    ("a MESH body",           lambda b: _is_mesh(b)),
    # 'any' re-checks no type: what _resolve_any_body returned is already a body.
    "any":     ("a body",                lambda b: True),
}

# kind -> the redirect a WRONG-kind body should suggest (the high-value fix-path text).
_BODY_REDIRECTS = {
    "solid":   "Use the solid-modelling tools, or convert it (a surface -> thicken/stitch; a mesh -> mesh_to_brep).",
    "surface": "Use the surface_* tools. A solid has no open surface to act on; a mesh isn't a BRep surface.",
    "brep":    "A mesh is not a BRep body - convert it with mesh_to_brep, or use the mesh_* tools.",
    "mesh":    "Use the mesh_* tools. A BRep solid/surface isn't a mesh - convert with save_as_mesh if you need one.",
    "any":     "",
}


def _body_kind_label(b) -> str:
    """What kind of body this IS, for the redirect message: SOLID / OPEN SURFACE / MESH."""
    if _is_mesh(b):
        return "MESH"
    if _is_brep(b):
        return "SOLID" if bool(_common.safe(lambda: b.isSolid)) else "OPEN SURFACE"
    return type(b).__name__


def _body_context(b):
    """Where this body lives, for an ambiguity candidate list: its occurrence fullPathName, else
    its owning component's name."""
    occ = _common.safe(lambda: b.assemblyContext)
    if occ is not None:
        fp = _common.safe(lambda: occ.fullPathName)
        if fp:
            return fp
    return _common.safe(lambda: b.parentComponent.name) or "?"


def _body_key(b):
    """The PHYSICAL-body key that GROUPS one body's wrappers: ``_common.native_identity``, else the
    (name, scope) pair, which can only merge wrappers of ONE body since a body name is unique inside
    its component. Which wrapper is a CANDIDATE is ``_collect_bodies_by_name``'s decision."""
    # Neither other key works: the wrapper's own entityToken reads one body reached natively and as
    # a proxy as two (a spurious ambiguity), and identity keys every fresh wrapper apart, since
    # every collection read mints a new one of the same physical body.
    return (_common.native_identity(b)
            or (_common.safe(lambda: b.name), _body_context(b)))


def _bodies_named_in(comp, name):
    """Every DISTINCT body named `name` in ONE component/occurrence scope (brep AND mesh), matched
    case-insensitively EXACT through two overlapping lookups ``_body_key`` then collapses:
    ``itemByName`` (the only lookup on a collection without count/item) and an iteration pass (the
    only mesh lookup, since ``meshBodies`` has no itemByName)."""
    # MEASURED: an Occurrence hands back a MeshBodyVector whose .count and .item(i) BOTH raise
    # AttributeError - the two idioms iter_collection walks with - so an occurrence scope
    # contributes BReps only and _collect_bodies_by_name reaches meshes through the components.
    out, seen = [], set()

    def add(b):
        key = _body_key(b)
        if key not in seen:
            seen.add(key)
            out.append(b)

    exact = _common.safe(lambda: getattr(comp, "bRepBodies").itemByName(name))
    if exact is not None:
        add(exact)
    want = (name or "").strip().lower()
    for coll_name in ("bRepBodies", "meshBodies"):
        for b in _common.iter_collection(_common.safe(lambda: getattr(comp, coll_name))):
            if (_common.safe(lambda b=b: b.name) or "").lower() == want:
                add(b)
    return out


def _body_scope_keys(b, ctx):
    """The scope prefixes a qualified '<scope>:<body>' reference may use for body `b`, lowercased:
    its context, the occurrence's own name ('Frame:1'), and the owning component's ('Frame')."""
    keys = {ctx, _common.safe(lambda: b.parentComponent.name)}
    occ = _common.safe(lambda: b.assemblyContext)
    if occ is not None:
        keys.add(_common.safe(lambda: occ.name))
    return {k.strip().lower() for k in keys if isinstance(k, str) and k.strip()}


def qualified_body_name(b, ctx=None):
    """One body as '<occurrence-or-component>:<body>' - the single spelling an ambiguity refusal
    lists, ``_qualified_body`` resolves, and a payload echoes back."""
    return f"{ctx if ctx is not None else _body_context(b)}:{_common.safe(lambda: b.name) or '?'}"


def _qualified_body_names(matches):
    """Each (body, context) pair from ``_collect_bodies_by_name`` in the qualified form."""
    return [qualified_body_name(b, ctx) for b, ctx in matches]


def _candidates_of_one_body(members):
    """The (body, context) candidates ONE physical body offers from its {context: wrapper} members:
    its PLACEMENTS when it has any, else the body itself. A placed body's proxies are the
    candidates and the native is dropped - each proxy carries the assembly context and the
    '<occurrence>:<body>' spelling that resolves back to that instance, which the native cannot."""
    wrappers = list(members.values())
    placed = [(b, _body_context(b)) for b in wrappers
              if _common.safe(lambda b=b: b.assemblyContext) is not None]
    return placed or [(b, _body_context(b)) for b in wrappers]


def _collect_bodies_by_name(des, comp, name):
    """Every DISTINCT body whose name matches `name` (case-insensitive exact) across the design
    (active component, root, each occurrence's proxies, plus every component's meshBodies), as
    (body, context) pairs - a body name is only LOCALLY unique, so the caller refuses an ambiguous
    one with this candidate list. Grouped by ``_body_key``, never by identity."""
    groups = {}

    def add(b):
        if b is None:
            return
        # Keyed by the wrapper's OWN token (stable per wrapper across re-fetches, distinct between
        # a native and each proxy). The printable context is the fallback only: keying on it would
        # MERGE two placements whose fullPathName will not read into one silent pick.
        member_key = _common.safe(lambda b=b: b.entityToken) or _body_context(b)
        groups.setdefault(_body_key(b), {}).setdefault(member_key, b)

    root = _common.safe(lambda: des.rootComponent) if des else None
    for scope in (comp, root):
        if scope is not None:
            for b in _bodies_named_in(scope, name):
                add(b)
    # The shared census, not a bare root.allOccurrences: that property RAISES on a design holding
    # an unresolved reference, and an empty walk would drop every NESTED placement of the body.
    for o in _common.all_occurrences(des):
        for b in _bodies_named_in(o, name):
            add(b)
    # The occurrence pass above contributes BReps only (see _bodies_named_in), so meshes come from
    # _common.all_meshes, the ONE design-wide mesh traversal. Grouping by _body_key keeps a mesh
    # already added by the comp/root passes from counting twice.
    if des is not None:
        want = (name or "").strip().lower()
        for _comp, m in _common.all_meshes(des):
            if (_common.safe(lambda m=m: m.name) or "").lower() == want:
                add(m)
    out = []
    for members in groups.values():
        out.extend(_candidates_of_one_body(members))
    return out


# The stem of _resolve_any_body's MISS refusal. A caller whose target vocabulary is WIDER than a
# body matches on this to tell a plain miss - replaceable with its own wider message - from a real
# REFUSAL (an ambiguous name, a scope holding no such body) it must pass through.
BODY_MISS = "no body or component named"


def _named_scope(des, key):
    """(scope, error) for the occurrence or component `key` names - the two vocabularies a
    qualified reference's prefix is built from; (None, None) is "names neither". A prefix several
    components answer to identifies no scope, so that refusal is carried, not flattened to a miss."""
    occ, occ_err = _resolve_occurrence("scope", key, exact_only=True)
    if occ is not None:
        return occ, None
    if occ_err and OCCURRENCE_MISS not in occ_err:
        return None, occ_err
    return _find_component(des, key)


def _qualified_body(label, des, spec):
    """(body, err) for a qualified '<occurrence-or-component>:<body name>' reference ('/' separates
    as well as ':'). (None, None) means "not a qualified body reference", leaving the bare-name and
    component paths their turn; a prefix that DOES name a scope holding no such body is a REFUSAL,
    since a mistyped 'Frame:Pinn' would otherwise fall through to that component's single body."""
    if not isinstance(spec, str):
        return None, None
    cut = max(spec.rfind(":"), spec.rfind("/"))
    if cut <= 0:
        return None, None
    head, tail = spec[:cut].strip(), spec[cut + 1:].strip()
    if not head or not tail:
        return None, None
    # Only "is the whole spec one scope's name?" is asked, so a refusal is NOT returned: a spec
    # several components answer to can still read as '<scope>:<body>' below.
    if _named_scope(des, spec)[0] is not None:
        return None, None            # the whole spec IS a scope name, not '<scope>:<body>'
    # Per-INSTANCE first: one component instanced twice refuses ('Jaw:Pin' names both instances'
    # bodies) rather than collapsing to the native one.
    want = head.lower()
    hits = [(b, ctx) for b, ctx in _collect_bodies_by_name(des, None, tail)
            if want in _body_scope_keys(b, ctx)]
    if len(hits) == 1:
        return hits[0][0], None
    if len(hits) > 1:
        cands = _common.named_with_remainder([f"'{q}'" for q in _qualified_body_names(hits)])
        return None, (f"'{label}': '{spec}' is ambiguous - it still names {len(hits)} bodies "
                      f"({cands}). Pass one of those, or a find_geometry 'handle'.")
    scope, scope_err = _named_scope(des, head)
    if scope_err:
        # The PREFIX names several components, so it names no scope; one instance's fullPathName
        # is a prefix that does resolve.
        return None, (f"'{label}': {scope_err} Use one instance's occurrence name/fullPathName as "
                      f"the prefix ('<instance>:{tail}'), or pass a find_geometry 'handle'.")
    if scope is None:
        return None, None            # no such scope - leave the remaining paths their turn
    # Ask the scope itself, which reaches a component the design-wide walk cannot - one with no
    # occurrence anywhere - and refuse when it holds no such name.
    named = _bodies_named_in(scope, tail)
    if not named:
        # Gated on the EMPTY ANSWER, never on an exception: that is what keeps a component scope's
        # own mesh, already found above, out of this list a second time as a spurious ambiguity.
        placed = _placed_meshes_named(scope, tail)
        if _unlifted(placed):
            refusal = _placement_refusal(label, spec, _placed_component(scope), tail, des)
            if refusal:
                return None, refusal
        named = placed
    if len(named) == 1:
        return named[0], None
    if len(named) > 1:
        cands = _common.named_with_remainder([f"'{qualified_body_name(b)}'" for b in named])
        return None, (f"'{label}': '{spec}' is ambiguous - it names {len(named)} bodies ({cands}).")
    if tail.isdigit():
        return None, None            # 'Frame:1' - an instance SUFFIX, not a body name
    held = _common.named_with_remainder([f"'{_common.safe(lambda b=b: b.name) or '?'}'"
                                         for b in _component_bodies(scope)])
    return None, (f"'{label}': '{head}' holds no body named '{tail}'"
                  + (f" - it holds {held}." if held else " - it holds no bodies."))


def _placed_component(scope):
    """The component an OCCURRENCE places, or None - MEASURED: Occurrence carries ``component`` and
    Component does NOT, so this doubles as the test for "is this scope an occurrence at all"."""
    return _common.safe(lambda: scope.component)


def _placed_meshes_named(scope, name):
    """The MESH bodies named `name` under an OCCURRENCE scope, each LIFTED into that placement -
    the fallback for a scope that answered nothing off its own collections, and empty for a
    component scope. A mesh whose lift hands back nothing stays the component's native body."""
    # MEASURED meshbodyvector-shape: an Occurrence's meshBodies is a MeshBodyVector - len() and
    # ITERATION answer while .count/.item(i) raise - so the counted walk reads nothing off it and
    # the meshes come off the placed COMPONENT; meshbody-assembly-context-proxy: the lift below.
    comp = _placed_component(scope)
    if comp is None:
        return []
    return [_common.safe(lambda b=b: b.createForAssemblyContext(scope)) or b
            for b in _bodies_named_in(comp, name) if _is_mesh(b)]


def _unlifted(bodies):
    """The bodies among `bodies` that carry NO assembly context - what a placement lift that handed
    back nothing leaves behind, and the only case an instance-qualified mesh is still ambiguous."""
    return [b for b in bodies if _common.safe(lambda b=b: b.assemblyContext) is None]


def _placement_refusal(label, spec, comp, tail, des):
    """The refusal for an INSTANCE-qualified mesh that did NOT lift into its placement and whose
    component is not placed exactly once, else ''. One placement makes the unlifted body that
    instance's anyway; an unreadable placement census establishes nothing, so the refusal stands."""
    root = _common.safe(lambda: des.rootComponent) if des is not None else None
    occs = _common.safe(lambda: root.allOccurrencesByComponent(comp)) if root is not None else None
    count = _common.counted(lambda: occs.count) if occs is not None else None
    cname = _common.safe(lambda: comp.name) or "its component"
    remedy = (f" Address it as '{cname}:{tail}' - the component's own mesh body, which is the one "
              "every instance shows.")
    if count == 1:
        return ""
    if not count:
        return (f"'{label}': '{spec}' names a MESH body of component '{cname}' that did not lift "
                "into this placement - the body that answered carries no assembly context - and "
                "how many times that component is placed did not read, so whether this address "
                "names a single instance is unknown." + remedy)
    paths = _common.named_with_remainder(
        [f"'{_common.safe(lambda i=i: occs.item(i).fullPathName) or '?'}'" for i in range(count)])
    return (f"'{label}': '{spec}' names a MESH body of component '{cname}' that did not lift into "
            "this placement - the body that answered carries no assembly context - and that "
            f"component is placed {count} times ({paths}), so this address does not pick one "
            "instance's mesh." + remedy)


def _component_bodies(scope):
    """Every body (BRep or mesh) `scope` - a component OR an occurrence - holds, for a listing. An
    occurrence is also asked for the meshes of the component it places, since the counted walk reads
    nothing off its MeshBodyVector (meshbodyvector-shape). De-duplicated on ``_body_key``."""
    out, seen = [], set()

    def add(b):
        if not (_is_brep(b) or _is_mesh(b)):
            return
        key = _body_key(b)
        if key not in seen:
            seen.add(key)
            out.append(b)

    for host, coll_names in ((scope, ("bRepBodies", "meshBodies")),
                             (_placed_component(scope), ("meshBodies",))):
        if host is None:
            continue
        for coll_name in coll_names:
            coll = _common.safe(lambda h=host, n=coll_name: getattr(h, n, None))
            for b in _common.iter_collection(coll):
                add(b)
    return out


# ── the component SCOPE a body NAME is narrowed by ──────────────────────────────────────────────

# Fusion names every component's first body 'Body1', so a design-wide body name is shared BY
# CONSTRUCTION. A consuming tool that declares a component-scope input hands its value here.

def _body_scope_remedy(input_name):
    """The second way out a consumer carrying a component-scope input can offer on a shared body
    NAME, or '' for one that carries none - worded as what the input DOES, since a component name
    answers EVERY placement and leaves two instances of one component ambiguous."""
    if not input_name:
        return ""
    return (f" Or pass the owning component as '{input_name}', which narrows this reference to that "
            "one component's bodies.")


def _body_scope(des, raw, input_name):
    """(scope, error) for a component-scope input's value, in ``_sketch_detail.scope_component``'s
    vocabulary so one input narrows a body reference by exactly what it narrows a sketch by.
    ``scope`` is (host, keys, spelled): the component-or-occurrence to ask directly, the reference
    keys a body inside it answers to, and the caller's own spelling for the refusals."""
    from . import _sketch_detail       # _sketch_detail imports this module, so the import is local
    comp, occ, err = _sketch_detail.scope_component(des, raw, input_name)
    if err:
        return None, err
    host = occ if occ is not None else comp
    reads = ((_common.safe(lambda: occ.fullPathName), _common.safe(lambda: occ.name))
             if occ is not None else (_common.safe(lambda: comp.name),))
    keys = {k.strip().lower() for k in reads if isinstance(k, str) and k.strip()}
    if not keys:
        return None, (f"'{input_name}': '{raw}' resolved, but nothing that names what it resolved to "
                      "could be read, so a body reference cannot be narrowed to it. Pass a "
                      "find_geometry 'handle' instead.")
    return (host, keys, (raw or "").strip()), None


def _scoped_body(label, des, s, scope):
    """(body, error) for the ONE body named `s` inside `scope` - the DESIGN-WIDE candidates
    FILTERED by the scope's keys, so a scope chooses WHICH candidate answers and never changes what
    a body reference resolves to. Only when that filter answers NOTHING is the scope asked directly
    (the one case the walk cannot reach: a component no occurrence places anywhere)."""
    host, keys, spelled = scope
    hits = [(b, ctx) for b, ctx in _collect_bodies_by_name(des, _common.target_component(des), s)
            if keys & _body_scope_keys(b, ctx)]
    if len(hits) > 1:
        # Case-insensitive matching WIDENS the hit list and must not manufacture an ambiguity: one
        # hit spelled exactly as asked is the answer, as on the unscoped path.
        cased = [m for m in hits if _common.safe(lambda b=m[0]: b.name) == s]
        if len(cased) == 1:
            hits = cased
    if len(hits) == 1:
        return hits[0][0], None
    if len(hits) > 1:
        cands = _common.named_with_remainder([f"'{q}'" for q in _qualified_body_names(hits)])
        return None, (f"'{label}': inside '{spelled}', '{s}' still names {len(hits)} bodies "
                      f"({cands}) - a component name answers every placement of that component. "
                      "Pass one of those qualified names, or a find_geometry 'handle'.")
    named = _bodies_named_in(host, s)
    if len(named) == 1:
        return named[0], None
    if len(named) > 1:
        cands = _common.named_with_remainder([f"'{qualified_body_name(b)}'" for b in named])
        return None, (f"'{label}': '{spelled}' holds {len(named)} bodies named '{s}' ({cands}).")
    held = _common.named_with_remainder([f"'{_common.safe(lambda b=b: b.name) or '?'}'"
                                         for b in _component_bodies(host)])
    return None, (f"'{label}': '{spelled}' holds no body named '{s}'"
                  + (f" - it holds {held}." if held else " - it holds no bodies."))


def _resolve_any_body(name, raw, source=None, scope=None, scope_input=None):
    """(body, error) for `raw` as a live BRepBody OR MeshBody, handle-first then name and
    KIND-AGNOSTIC, so the caller's wrong-kind error can name the required kind. `source` collects
    the vocabulary that answered ('handle' / 'name'); `scope` is a resolved ``_body_scope`` record
    narrowing every NAME path, and `scope_input` the input a shared-name refusal offers."""
    s = (raw or "").strip() if isinstance(raw, str) else raw
    if not s:
        return None, f"'{name}' is required (a body handle or name)."
    des = _common.design()
    if not des:
        return None, "No active design to resolve the body against."
    # By what RESOLVES, not by string length: the entity token first, then a name lookup, so a long
    # body NAME is never mistaken for a handle.
    ent = _resolve_token_entity(des, s)
    if source is not None:
        source.append("handle" if ent is not None else "name")
    if ent is not None:
        if _is_brep(ent) or _is_mesh(ent):
            return ent, None
        # A face/edge/vertex handle names its OWNING body. find_geometry mints no body handle, so
        # this is what makes the ambiguous-name error's "pass a handle" advice resolvable.
        owner = _common.safe(lambda: ent.body)
        if owner is not None and (_is_brep(owner) or _is_mesh(owner)):
            return owner, None
        return None, f"'{name}': handle points at a {type(ent).__name__}, not a body."
    # Captured NOW, for the caller's OWN value: the qualified-body vocabulary below rsplits on the
    # ':' inside a composite handle's locator, and re-resolving that mutilated prefix OVERWRITES
    # the refusal channel with a reason about a string the caller never passed (measured).
    handle_suffix = _handle_refusal_suffix()
    if scope is not None:
        return _scoped_body(name, des, s, scope)
    # Name path: an AMBIGUOUS name is refused with the QUALIFIED candidate list, never first-matched.
    matches = _collect_bodies_by_name(des, _common.target_component(des), s)
    if len(matches) > 1:
        # A widened (case-insensitive) hit list must not manufacture an ambiguity: one hit spelled
        # exactly as asked is the answer.
        cased = [m for m in matches if _common.safe(lambda b=m[0]: b.name) == s]
        if len(cased) == 1:
            matches = cased
    if len(matches) == 1:
        return matches[0][0], None
    if len(matches) > 1:
        cands = _common.named_with_remainder([f"'{q}'" for q in _qualified_body_names(matches)])
        return None, (f"'{name}': '{s}' is ambiguous - it names {len(matches)} bodies ({cands}). "
                      "Pass one of those qualified '<occurrence-or-component>:<body>' names, or a "
                      "find_geometry 'handle'." + _body_scope_remedy(scope_input))
    # The qualified form the refusal above lists, picking one body out of a shared name.
    qbody, qerr = _qualified_body(name, des, s)
    if qbody is not None:
        return qbody, None
    if qerr:
        return None, qerr
    # A COMPONENT or exact OCCURRENCE resolves to its body when that is unambiguous. Body names
    # win above; resolving through _named_scope preserves the instance suffix instead of treating a
    # missing 'Frame:999' as the component named 'Frame'.
    comp, comp_err = _named_scope(des, s)
    if comp_err:
        return None, (f"'{name}': {comp_err} Pass a qualified '<occurrence-or-component>:<body>' "
                      "name, or a find_geometry 'handle'.")
    if comp is not None:
        bodies = _component_bodies(comp)
        if len(bodies) == 1:
            return bodies[0], None
        if len(bodies) > 1:
            cands = _common.named_with_remainder(
                [f"'{_common.safe(lambda b=b: b.name) or '?'}'" for b in bodies])
            return None, (f"'{name}': '{s}' is a component holding {len(bodies)} bodies ({cands}) - "
                          "name one of them, or pass a find_geometry handle.")
        return None, f"'{name}': component '{s}' holds no bodies to act on."
    return None, (f"'{name}': {BODY_MISS} '{s}'. Pass a body handle from "
                  "find_geometry, a body name (bare, or '<occurrence-or-component>:<body>'), or a "
                  "single-body component/occurrence name "
                  "(see design_get(include=['tree']) / model_extrude output)."
                  + handle_suffix)


class BodyRef(InputKind):
    """A reference to a BODY, by a 'handle' from find_geometry (precise - bodies are auto-named
    Body1/Body2... so names are fragile) OR by name, against BOTH bRepBodies AND meshBodies. A name
    matches case-insensitively in ANY component, bare when unique design-wide, else qualified as
    '<occurrence-or-component>:<body>'. A wrong `kind` returns a REDIRECTING error."""

    MAP_HINT = "a body by handle (precise), body name, or single-body component name; kind=solid/surface/mesh"

    # The consuming tool's component-scope input, when it declares one (BodyRefList takes it as a
    # constructor argument). A plain BodyRef carries none, so its refusals name no scope.
    scope_input = None

    def __init__(self, name, kind="any", **kw):
        super().__init__(name, **kw)
        self.kind = kind if kind in _BODY_KINDS else "any"

    def contract_note(self) -> str:
        label, _ = _BODY_KINDS[self.kind]
        lead = "A body" if self.kind == "any" else f"{label[0].upper() + label[1:]}"
        return f"{lead} 'handle' or name."

    def _redirect(self, body, raw, source) -> str:
        """The wrong-kind refusal, worded off the vocabulary that ACTUALLY answered - saying "that
        handle" for a bare name sends the caller after a handle it never gave."""
        label, _ = _BODY_KINDS[self.kind]
        got = _body_kind_label(body)
        hint = _BODY_REDIRECTS.get(self.kind, "")
        via = (f"that handle points at a {got} body" if source == "handle"
               else f"the name '{raw}' resolves to a {got} body")
        return f"'{self.name}' must be {label}, but {via}. {hint}".strip()

    def _check_kind(self, body, raw, source):
        """(body, None) if `body` matches self.kind, else (None, redirect_error)."""
        _, ok_pred = _BODY_KINDS[self.kind]
        if not ok_pred(body):
            return None, self._redirect(body, raw, source)
        return body, None

    def resolve(self, raw, scope=None):
        s = (raw or "").strip() if isinstance(raw, str) else raw
        if not s:
            if self.required:
                return None, f"'{self.name}' is required (a body handle or name)."
            return self.default, None
        source = []
        body, err = _resolve_any_body(self.name, s, source=source, scope=scope,
                                      scope_input=self.scope_input)
        if err:
            return None, err
        return self._check_kind(body, s, source[0])


class BodyRefList(BodyRef):
    """A LIST of body references (handles or names) - for tools that act on several bodies. Kind-checks
    EVERY element BEFORE returning, so a wrong-kind body fails the call before any mutation runs.
    ``scope_input`` names the consuming tool's own component-scope input, inside which every NAME in
    the list then resolves; a scope passed with an EMPTY list is REFUSED."""

    json_type = "array"
    MAP_HINT = "several bodies (handles or names)"

    # KEYWORD-ONLY: BodyRef takes `kind` at this position, so a positional value would mean one
    # thing on the base class and another here.
    def __init__(self, name, *, scope_input=None, **kw):
        super().__init__(name, **kw)
        self.scope_input = scope_input or None

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, **self._desc(brief)}

    def contract_note(self) -> str:
        label, _ = _BODY_KINDS[self.kind]
        suffix = "" if self.kind == "any" else f", each {label}"
        return f"Body 'handle's or names{suffix}."

    def resolve(self, raw, component=""):
        scoped = bool(self.scope_input) and bool((component or "").strip())
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' needs at least one body (handle or name)."
            # A scope narrows the NAMES in this list, so on an empty list it applies to nothing.
            # Refused rather than resolved-and-dropped: only the refusal tells the caller.
            if scoped:
                return None, (f"'{self.scope_input}' was passed with an empty '{self.name}' - a "
                              f"scope narrows the names in '{self.name}', and there are none, so it "
                              f"would apply to nothing. Name the bodies to act on in '{self.name}', "
                              f"or drop '{self.scope_input}'.")
            return [], None
        items = raw if isinstance(raw, (list, tuple)) else [s.strip() for s in str(raw).split(",") if s.strip()]
        scope = None
        if scoped:
            des = _common.design()
            if des is None:
                return None, "No active design to resolve the body names against."
            # Resolved ONCE and applied to EVERY name: a scope narrowing only the first element
            # would let one component's body ride along beside another's.
            scope, serr = _body_scope(des, component, self.scope_input)
            if serr:
                return None, serr
        out = []
        for i, item in enumerate(items):
            b, err = BodyRef.resolve(self, item, scope=scope)
            if err:
                return None, f"'{self.name}'[{i}]: {err}"
            out.append(b)
        if not out:
            return None, f"'{self.name}': no valid bodies resolved."
        return out, None


# thin convenience aliases so call sites read well (per the synthesis spec):
def SurfaceBodyRef(name, **kw):
    """BodyRef constrained to OPEN SURFACE bodies (isSolid==False BRepBodies)."""
    return BodyRef(name, kind="surface", **kw)


def SurfaceBodyRefList(name, **kw):
    """BodyRefList constrained to OPEN SURFACE bodies."""
    return BodyRefList(name, kind="surface", **kw)


def MeshBodyRef(name, **kw):
    """BodyRef constrained to MESH bodies (adsk.fusion.MeshBody)."""
    return BodyRef(name, kind="mesh", **kw)


# ── feature reference (a TIMELINE object by name - the non-unique name space) ────────────────────

def _timeline_objects(timeline):
    """Every timeline object the timeline can hand back, an unreadable one SKIPPED rather than
    raising - so this list's positions are NOT the objects' own .index values."""
    return list(_common.iter_collection(timeline))


def _match_timeline_objects(objs, want):
    """Every timeline object `want` names: the 'name@index' pair - the object whose OWN .index is
    that number, confirmed by name - else an EXACT case-insensitive name match, never a substring.
    '@index' reads each object's .index rather than indexing this list, which carries holes."""
    # MEASURED: Fusion names an occurrence-create timeline object with a LEADING SPACE
    # (' InsProbe:1'), invisible in every listing an agent reads, so both sides are STRIPPED and
    # two objects differing only by whitespace are one ambiguity.
    base, at, idx = want.rpartition("@")
    if at and base.strip() and idx.strip().isdigit():
        i = int(idx.strip())
        low = base.strip().lower()
        return [o for o in objs
                if _common.safe(lambda o=o: o.index) == i and _name_key(o) == low]
    low = want.strip().lower()
    return [o for o in objs if _name_key(o) == low]


def _name_key(obj):
    """A timeline object's name, stripped and lower-cased - both sides of every comparison."""
    return (_common.safe(lambda: obj.name) or "").strip().lower()


def _index_mismatch(objs, want):
    """The refusal for a 'name@index' pair whose halves name different objects - what sits at that
    index, and the index the name is at now - or None when `want` is not that form, or its name is
    nowhere in `objs` (a plain miss)."""
    base, at, idx = want.rpartition("@")
    if not (at and base.strip() and idx.strip().isdigit()):
        return None
    i, name = int(idx.strip()), base.strip()
    named = [str(_common.safe(lambda o=o: o.index))
             for o in objs if _name_key(o) == name.lower()]
    if not named:
        return None
    at_i = [o for o in objs if _common.safe(lambda o=o: o.index) == i]
    seat = (f"index {i} is '{_common.safe(lambda: at_i[0].name)}'" if at_i
            else f"no timeline item reads index {i}")
    return (f"'{want}': {seat}, and '{name}' is at index "
            f"{_common.named_with_remainder(named)}. Re-read design_get(include=['timeline']) for "
            "the current indices.")


def resolve_timeline_object(objs, want, label, miss_hint=None):
    """(timeline object, error) - the ONE object `want` names out of `objs`: a miss lists a sample
    of what IS there, a name several objects carry is refused with the 'name@index' candidates.
    `label` is the caller's own noun, prefixing the refusal; `miss_hint(want)` is consulted on a
    MISS only, standing in for the generic text where the caller can explain the absence."""
    hits = _match_timeline_objects(objs, want)
    if not hits:
        stale = _index_mismatch(objs, want)
        if stale:
            return None, f"{label}: {stale}"
        hinted = miss_hint(want) if miss_hint is not None else None
        if hinted:
            return None, f"{label}: {hinted}"
        sample = ", ".join(n for n in (_common.safe(lambda o=o: o.name) for o in objs[:12]) if n)
        return None, (f"{label}: no timeline feature named '{want}'. Available (sample): "
                      f"{sample or '(none)'}. Use design_get(include=['timeline']) for the full "
                      "list.")
    if len(hits) > 1:
        cands = _common.named_with_remainder(
            [f"{_common.safe(lambda o=o: o.name)}@{_common.safe(lambda o=o: o.index)}"
             for o in hits])
        return None, (f"{label}: '{want}' matches {len(hits)} timeline objects ({cands}) - name "
                      "one with the 'name@index' form.")
    return hits[0], None


class FeatureRef(InputKind):
    """A reference to ONE timeline FEATURE by name, as design_get(include=['timeline']) lists it.
    Resolves to (entity, label) - the timeline object's `.entity` plus the name the TIMELINE object
    carries. An ambiguous name is refused with the 'name@index' candidates; a timeline GROUP is
    refused, having no feature entity."""

    MAP_HINT = "a timeline feature by name (refuses an ambiguous name; 'name@index' picks one)"

    def contract_note(self) -> str:
        return "A timeline feature name (design_get timeline)."

    def _objects(self):
        """(timeline objects, error) for the active design."""
        des = _common.design()
        if not des:
            return None, "No active design to resolve the feature name against."
        timeline = _common.safe(lambda: des.timeline)
        if timeline is None:
            return None, ("This design has no timeline, so it has no features to name. Act on the "
                          "bodies instead.")
        return _timeline_objects(timeline), None

    def _find_one(self, objs, want, label):
        """(timeline object, error) - the ONE object `want` names, or a refusal."""
        return resolve_timeline_object(objs, want, label)

    def _entity_of(self, obj, want, label):
        """((entity, timeline name), error) for one resolved timeline object."""
        name = _common.safe(lambda: obj.name) or want
        if _common.safe(lambda: obj.isGroup):
            return None, (f"{label}: '{name}' is a timeline GROUP, which has no feature entity. Name "
                          "the features inside it instead.")
        entity = _common.safe(lambda: obj.entity)
        if entity is None:
            return None, f"{label}: '{name}' has no feature entity."
        return (entity, name), None

    def resolve(self, raw):
        s = (raw or "").strip() if isinstance(raw, str) else raw
        if not s:
            if self.required:
                return None, f"'{self.name}' is required (a timeline feature name)."
            return self.default, None
        objs, err = self._objects()
        if err:
            return None, err
        obj, ferr = self._find_one(objs, s, f"'{self.name}'")
        if ferr:
            return None, ferr
        return self._entity_of(obj, s, f"'{self.name}'")


class FeatureRefList(FeatureRef):
    """A LIST of timeline features, resolving to (entities, labels) - the entity list plus the
    timeline names in the same order. The SAME timeline object named twice is refused: a duplicate
    would silently double what the caller asked for once."""

    json_type = "array"
    MAP_HINT = "several timeline features by name"

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, **self._desc(brief)}

    def contract_note(self) -> str:
        return "Timeline feature names (design_get timeline)."

    def resolve(self, raw):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' needs at least one timeline feature name."
            return ([], []), None
        items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        items = [str(s).strip() for s in items if str(s).strip()]
        if not items:
            return None, f"'{self.name}' needs at least one timeline feature name."
        objs, err = self._objects()
        if err:
            return None, err
        ents, labels, picked = [], [], set()
        for i, want in enumerate(items):
            label = f"'{self.name}'[{i}]"
            obj, ferr = self._find_one(objs, want, label)
            if ferr:
                return None, ferr
            # Before the group/entity checks, so naming one object twice is refused as a duplicate
            # rather than by whatever the second pass finds on it.
            index = _common.safe(lambda: obj.index)
            if index is not None and index in picked:
                return None, (f"{label}: '{want}' names the timeline object at index {index}, which "
                              "is already in this call. List each feature once.")
            picked.add(index)
            pair, eerr = self._entity_of(obj, want, label)
            if eerr:
                return None, eerr
            ents.append(pair[0])
            labels.append(pair[1])
        return (ents, labels), None


# ── ModeGuard: declare the design mode / base-feature scope an op needs ──────────────────────────

MODE_PARAMETRIC = "parametric"
MODE_DIRECT = "direct"
MODE_BASE_FEATURE = "base_feature"


def current_design_type(design) -> str:
    """The active design's modelling mode as 'parametric' / 'direct' / 'unknown' - the ONE source
    design_get's mode slice and every ModeGuard share, so the report and the guards cannot drift.
    A missing/mocked designType degrades to 'unknown' rather than crashing."""
    if design is None:
        return "unknown"
    dt = _common.safe(lambda: design.designType)
    if dt is None:
        return "unknown"
    types = _common.safe(lambda: adsk.fusion.DesignTypes)
    param = _common.safe(lambda: types.ParametricDesignType)
    direct = _common.safe(lambda: types.DirectDesignType)
    if param is not None and dt == param:
        return MODE_PARAMETRIC
    if direct is not None and dt == direct:
        return MODE_DIRECT
    # Confirmed live: ParametricDesignType == 1, DirectDesignType == 0 - for a build where
    # DesignTypes is not a comparable enum and designType reads as a bare int.
    if isinstance(dt, int) and not isinstance(dt, bool):
        if dt == 1:
            return MODE_PARAMETRIC
        if dt == 0:
            return MODE_DIRECT
    return "unknown"


def _in_base_feature_scope(design) -> bool:
    """Is an OPEN base-feature edit scope in effect? True only where activeEditObject reads as a
    BaseFeature - the public API exposes no scope flag, so an unknown answers False and the guard
    fails CLOSED rather than letting an unscoped mutation through."""
    if design is None:
        return False
    edit_obj = _common.safe(lambda: design.activeEditObject)
    if edit_obj is None:
        return False
    bf_type = _common.safe(lambda: adsk.fusion.BaseFeature)
    if bf_type is not None and isinstance(edit_obj, bf_type):
        return True
    return False


class ModeGuard:
    """A declarative precondition - not an InputKind: 'this op needs <mode>'. check(design) runs
    BEFORE mutating and returns (ok, error_result_or_None), the error DERIVED from self.requires so
    it cannot invert. `why` explains the API constraint, `fix_hint` how to satisfy it."""

    def __init__(self, requires, why="", fix_hint=""):
        self.requires = requires
        self.why = why
        self.fix_hint = fix_hint

    def check(self, design):
        """(ok: bool, error_result | None) - run before any mutation, so a rejection leaves
        nothing half-applied."""
        if self.requires == MODE_BASE_FEATURE:
            if _in_base_feature_scope(design):
                return True, None
            return False, self._err("no base-feature scope")
        actual = current_design_type(design)
        if actual == self.requires:
            return True, None
        return False, self._err(actual)

    def _err(self, actual):
        # Text DERIVED from self.requires -> structurally cannot point the wrong way.
        if self.requires == MODE_BASE_FEATURE:
            head = ("This needs a BASE-FEATURE edit scope (the mesh/base-feature insert must run "
                    "inside BaseFeature.startEdit()/finishEdit()), but none is open.")
        else:
            head = f"This needs {self.requires} mode but the design is in {actual} mode."
        return _common.error(f"{head} {self.why} {self.fix_hint}".strip())

    def contract_note(self) -> str:
        if self.requires == MODE_BASE_FEATURE:
            return "Requires a base-feature edit scope."
        return f"Requires {self.requires} mode."


# ── plane reference (MULTI-SOURCE: origin alias | construction name | face handle) ──────────────

_ORIGIN_PLANES = {"xy": "xY", "xz": "xZ", "yz": "yZ", "top": "xY", "front": "xZ", "right": "yZ"}
# 'xy plane' / 'XYPlane' name the same origin plane as the bare alias (whitespace is stripped before
# the lookup). Only the AXIS aliases take the suffix: 'Top plane' stays a construction-plane NAME.
_ORIGIN_PLANES.update({f"{a}plane": _ORIGIN_PLANES[a] for a in ("xy", "xz", "yz")})


def _planes_named_in(comp, want):
    """Every construction plane in ONE component whose name matches `want` (already lower-cased)
    case-insensitively EXACT - never a substring. A LIST: what several hits mean is the caller's."""
    hits = []
    for cp in _common.iter_collection(_common.safe(lambda: comp.constructionPlanes)):
        nm = _common.safe(lambda cp=cp: cp.name)
        if isinstance(nm, str) and nm.strip().lower() == want:
            hits.append(cp)
    return hits


def _construction_planes_named(des, name):
    """Every (construction plane, owning component) named `name` design-wide - a datum created in a
    sub-component is invisible to a root-only lookup, and the name space is NOT unique, so one hit
    resolves and several are refused with the qualified '<occurrence>:<plane name>' candidates."""
    want = (name or "").strip().lower()
    if not want:
        return []
    return [(cp, comp) for comp in _common.all_components(des)
            for cp in _planes_named_in(comp, want)]


def _plane_reference_names(des, cp, comp):
    """The reference string(s) that resolve back to THIS construction plane: its bare name for a
    root-owned one (the root has no qualified form), else '<occurrence fullPathName>:<name>' per
    placing occurrence - so what an ambiguity refusal offers is what PlaneRef accepts."""
    nm = _common.safe(lambda: cp.name) or "?"
    root = _common.safe(lambda: des.rootComponent)
    # `is True`: an owner PROVEN to be the root has only the bare form. An unproven one falls to
    # the occurrence walk, which checks itself, where claiming the root prints an unverified row.
    if comp is None or _common.same_component(comp, root) is True:
        return [nm]
    occs = _common.safe(lambda: root.allOccurrencesByComponent(comp)) if root is not None else None
    out = [f"{p}:{nm}" for o in _common.iter_collection(occs)
           if (p := _common.safe(lambda o=o: o.fullPathName))]
    return out or [nm]


class PlaneRef(InputKind):
    """A reference to a PLANE to act on, resolved from ANY of three shapes a user might supply:
    an origin-plane alias (xy/xz/yz or top/front/right) against the ACTIVE component, a
    construction plane's NAME, or a find_geometry 'handle' at a PLANAR FACE. A NAME resolves
    design-wide - the active component first, a unique one elsewhere PROXIED, a shared one refused."""

    MAP_HINT = "a plane: xy/xz/yz alias, construction-plane name, OR planar-face handle"

    def contract_note(self) -> str:
        return "xy/xz/yz/top/front/right, a construction-plane name, or a planar-face 'handle'."

    def resolve(self, raw, component=None):
        # `component` is the context the plane is resolved FOR - the active component unless the
        # caller builds somewhere else (a document-level Section Analysis resolves against the root).
        s = (raw or "").strip() if isinstance(raw, str) else raw
        if not s or not isinstance(s, str):
            if self.required:
                return None, f"'{self.name}' is required (a plane alias, name, or handle)."
            if not isinstance(self.default, str) or not self.default.strip():
                return self.default, None
            # A declared default is a plane REFERENCE like any other, so it takes the same path a
            # caller's value takes - never the raw 'xy' string handed to the API as a plane.
            s = self.default.strip()
        des = _common.design()
        if not des:
            return None, "No active design to resolve the plane against."
        comp = component if component is not None else _common.target_component(des)
        # 1) origin-plane alias
        key = _ORIGIN_PLANES.get(s.lower().replace(" ", ""))
        if key:
            pl = _common.safe(lambda: getattr(comp, f"{key}ConstructionPlane"))
            return (pl, None) if pl else (None, f"Could not get the {key} origin plane.")
        # 2) a handle -> planar face or construction plane. By what RESOLVES, not by string length,
        # so a long construction-plane NAME is never mistaken for a stale handle.
        ent = _resolve_token_entity(des, s)
        if ent is not None:
            if isinstance(ent, adsk.fusion.BRepFace):
                if _common.safe(lambda: ent.geometry.surfaceType) == adsk.core.SurfaceTypes.PlaneSurfaceType:
                    return ent, None
                return None, f"'{self.name}': that face handle is not PLANAR (can't sketch/mirror on a curved face)."
            if isinstance(ent, adsk.fusion.ConstructionPlane):
                return ent, None
            return None, f"'{self.name}': handle points at a {type(ent).__name__}, not a plane/planar face."
        # 3) a named construction plane, in ANY component
        return self._resolve_named(des, comp, s)

    def _resolve_named(self, des, comp, s):
        """(plane, error) for the NAME forms, in the order that keeps a component-local name usable:
        the qualified '<occurrence>:<plane name>' picking one instance, then the ACTIVE component's
        own planes, then the rest of the design (unique, else refused)."""
        cp, qerr = self._resolve_qualified(des, s)
        if cp is not None or qerr:
            return cp, qerr
        # The active component's own names win: Fusion default-names the first datum of EVERY
        # component 'Plane1', so a design-wide vote would refuse the commonest name there is.
        local = _planes_named_in(comp, s.strip().lower()) if comp is not None else []
        if len(local) == 1:
            return local[0], None
        matches = _construction_planes_named(des, s)
        if len(matches) == 1:
            return self._in_context(des, comp, *matches[0])
        if len(matches) > 1:
            return None, self._ambiguous(des, s, matches)
        return None, (f"'{self.name}': '{s}' is not an origin alias (xy/xz/yz, or top/front/right), a "
                      "known construction plane name, or a planar-face/construction-plane handle from "
                      "find_geometry - which is the form an arbitrary or angled plane takes.")

    def _resolve_qualified(self, des, spec):
        """(plane, err) for '<occurrence>:<plane name>', proxied into that occurrence's context -
        (None, None) when spec is not a qualified form, leaving the bare-name paths their turn."""
        if ":" not in spec:
            return None, None
        head, _, tail = spec.rpartition(":")
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            return None, None
        occ, _occ_err = _resolve_occurrence(self.name, head)
        if occ is None:
            return None, None                 # head isn't an occurrence - let the bare-name paths try
        hits = _planes_named_in(_common.safe(lambda: occ.component), tail.lower())
        if not hits:
            return None, (f"'{self.name}': occurrence '{head}' has no construction plane named "
                          f"'{tail}'.")
        if len(hits) > 1:
            return None, (f"'{self.name}': occurrence '{head}' has {len(hits)} construction planes "
                          f"named '{tail}' - rename them, or pass the plane's handle.")
        return _proxy_or_refuse(f"'{self.name}': construction plane '{tail}'", hits[0], occ,
                                "Pass the plane's handle from find_geometry.")

    def _in_context(self, des, comp, cp, owner):
        """(the plane usable where this call builds, error). A plane native to ANOTHER component is
        component-LOCAL and Fusion refuses it in the current context, so it is PROXIED into the
        single occurrence placing its owner; a root-owned plane is handed back native, and an owner
        placed several times - or one whose identity did not read - is refused, never guessed."""
        root = _common.safe(lambda: des.rootComponent)
        at_root = _common.same_component(owner, root)
        if at_root is True:
            return cp, None
        # No `owner is None` short-circuit: a missing owner IS one that did not read, and
        # same_component already answers None for it, so it falls into the refusal below.
        if at_root is None:
            nm = _common.safe(lambda: cp.name) or "?"
            return None, (f"'{self.name}': construction plane '{nm}' - whether its owning component "
                          "is this design's root could not be read, so whether the plane is already "
                          "in the assembly's space is unknown and using it would be a guess. Pass "
                          "the plane's handle from find_geometry.")
        occ, err = single_placement(f"'{self.name}': that construction plane", cp, comp, des)
        if err:
            return None, self._instance_refusal(des, cp, owner)
        if occ is None:
            return cp, None
        nm = _common.safe(lambda: cp.name) or "?"
        return _proxy_or_refuse(f"'{self.name}': construction plane '{nm}'", cp, occ,
                                "Pass the plane's handle from find_geometry.")

    def _ambiguous(self, des, s, matches):
        """The refusal for a name several components carry, every hit named as the string that
        resolves to it. A ROOT plane has no qualified form - only the bare name with the root
        active - so that remedy is stated whenever one is a hit."""
        cands = list(dict.fromkeys(c for cp, owner in matches
                                   for c in _plane_reference_names(des, cp, owner)))
        fix = (" The bare name reaches the ROOT component's own plane only while the root is active "
               "(design_activate_component)." if any(":" not in c for c in cands) else "")
        return (f"'{self.name}': '{s}' is ambiguous - {len(matches)} construction planes share that "
                f"name ({_common.named_with_remainder(cands)}). Pass one of these qualified "
                f"names.{fix}")

    def _instance_refusal(self, des, cp, owner):
        """The refusal for a plane whose owning component is placed several times - each instance
        holding it somewhere different - or not at all, pointing at the form that picks one."""
        nm = _common.safe(lambda: cp.name) or "?"
        owner_name = _common.safe(lambda: owner.name) or "another component"
        cands = [c for c in _plane_reference_names(des, cp, owner) if ":" in c]
        if not cands:
            return (f"'{self.name}': construction plane '{nm}' is on component '{owner_name}', which "
                    "is not placed in this assembly, so it cannot be brought into the assembly's "
                    "space.")
        return (f"'{self.name}': construction plane '{nm}' is on component '{owner_name}', which is "
                f"placed {len(cands)} times. Each instance holds it somewhere different, so the "
                f"instance is not guessed - pass one of: "
                f"{_common.named_with_remainder(cands)}.")


# ── the 'surface' operand: a plane, or - where the API takes one - any face ─────────────────────

_SURFACE_PLANE = PlaneRef("surface")
_SURFACE_ANY_FACE = GeometryHandle("surface", require="face", required=False)


def resolve_surface(raw, allow_curved=False):
    """(surface, error) for the FACE/PLANE a sketch entity is constrained or dimensioned to.
    PlaneRef carries the whole expected vocabulary and is tried first; ``allow_curved`` is the
    CALLING API's own contract - a plain ``surface`` argument accepts a cylindrical/spherical/
    conical face and one naming ``planarSurface`` does not, so only the former takes a second pass."""
    surf, serr = _SURFACE_PLANE.resolve(raw)
    if serr is None or not allow_curved:
        return surf, serr
    wide, werr = _SURFACE_ANY_FACE.resolve(raw)
    return (wide, None) if werr is None else (None, serr)


def surface_ref_label(surf):
    """What a resolved 'surface' IS - a construction plane's name, else the entity type - published
    instead of the raw input token, so the payload reports the thing that was used."""
    return _common.safe(lambda: surf.name) or type(surf).__name__


class SurfaceRef(InputKind):
    """The FACE/PLANE a sketch entity is constrained or dimensioned to. Schema and resolution come
    from this ONE kind, so the contract cannot say planar-only while the handler takes a curve.
    ``curved_ops`` names the operations whose API argument is a plain ``surface: Base`` (planar,
    cylindrical, spherical, conical); every other one names ``planarSurface`` and takes a plane."""

    MAP_HINT = ("the *_to_surface operand: plane alias / construction plane / planar face, plus the "
                "curved faces the operations named in curved_ops accept")

    def __init__(self, name, curved_ops=(), **kw):
        super().__init__(name, **kw)
        self.curved_ops = tuple(curved_ops)

    def contract_note(self) -> str:
        note = "xy/xz/yz, a construction-plane name, or a planar-face 'handle'"
        if self.curved_ops:
            note += " (a curved face too for " + "/".join(self.curved_ops) + ")"
        return note + "."

    def resolve(self, raw, operation=None):
        """(surface, error) for ``operation`` - the dim_type/constraint being applied, which
        selects the curved-face second pass exactly where that call's API accepts one."""
        return resolve_surface(raw, operation in self.curved_ops)


# ── axis reference (a world axis x/y/z OR an edge handle the axis runs along) ────────────────────

_AXIS_VECS = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}

# world axis key -> a component's origin construction-axis ATTRIBUTE name (the entity form of a world
# direction - what a feature input that wants a ConstructionAxis entity, not a vector, consumes).
WORLD_AXIS_ATTRS = {"x": "xConstructionAxis", "y": "yConstructionAxis", "z": "zConstructionAxis"}


def world_construction_axis(comp, key):
    """The component's origin ConstructionAxis entity for a world-axis key ('x'/'y'/'z').
    None for an unknown key or an unreadable component (the caller words its own error)."""
    attr = WORLD_AXIS_ATTRS.get((key or "").strip().lower())
    if not attr:
        return None
    return _common.safe(lambda: getattr(comp, attr))


def _axis_from_face(name, face):
    """(tagged, err) for a face used as an axis SOURCE - a PLANAR face's normal, a CYLINDRICAL or
    CONICAL face's axis - tagged ('world', unit_vec), the SAME shape a world axis uses, so 'world'
    here means 'a fixed direction vector' rather than necessarily a world axis."""
    st = _common.safe(lambda: face.geometry.surfaceType)
    g = _common.safe(lambda: face.geometry)
    ST = adsk.core.SurfaceTypes
    vec = None
    if st == _common.safe(lambda: ST.PlaneSurfaceType):
        vec = _common.safe(lambda: g.normal)
    elif st in (_common.safe(lambda: ST.CylinderSurfaceType), _common.safe(lambda: ST.ConeSurfaceType)):
        vec = _common.safe(lambda: g.axis)
    if vec is None:
        return None, (f"'{name}': that face is neither planar (a normal) nor cylindrical/conical (an "
                      "axis), so it has no single axis direction.")
    d = (_common.safe(lambda: vec.x, 0.0), _common.safe(lambda: vec.y, 0.0), _common.safe(lambda: vec.z, 0.0))
    n = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
    if n <= 1e-12:
        return None, f"'{name}': the face's direction is degenerate (zero-length)."
    return ("world", (d[0] / n, d[1] / n, d[2] / n)), None


def entity_component(ent):
    """The component an ENTITY belongs to - a face's or edge's body's parent, a sketch line's
    sketch's parent, or a datum's own component - else None. A datum is read through `.component`
    before `.parent`, which returns a BASE FEATURE for a non-parametric datum."""
    return (_common.safe(lambda: ent.body.parentComponent)
            or _common.safe(lambda: ent.parentSketch.parentComponent)
            or _common.safe(lambda: ent.component)
            or _common.safe(lambda: ent.parent))


def single_placement(label, ent, comp, design):
    """The assembly-context walk every consumer of a possibly-foreign entity runs first, as
    (occurrence, error): (None, None) nothing to lift, (occ, None) the owner's ONE placement to
    proxy into, (None, err) refused naming every fullPathName. `label` opens that refusal."""
    # MEASURED: a NATIVE entity owned by ANOTHER component is accepted by a feature input and then
    # fails at add(), while the same entity proxied into its occurrence is accepted. A component
    # placed SEVERAL times is REFUSED - no feature read-back tells a right instance from a wrong one.
    if _common.safe(lambda: ent.assemblyContext) is not None:
        return None, None
    # entity_component covers a face/edge/sketch-line/datum; a BODY answers only its own
    # parentComponent, and a body is what a move feature's host walk carries here.
    owner = entity_component(ent) or _common.safe(lambda: ent.parentComponent)
    if owner is None:
        return None, None
    # TRI-STATE: only a PROVEN same component means "nothing to lift". Both outcomes of a guess are
    # wrong - a lift proxying an entity already in context, or a native rejected at add().
    here = _common.same_component(owner, comp) if comp is not None else False
    if here is True:
        return None, None
    owner_name = _common.safe(lambda: owner.name) or "another component"
    if here is None:
        return None, (f"{label} belongs to component '{owner_name}', and whether that is the "
                      "component this call builds in could not be read, so whether it must be "
                      "brought into the assembly's space is unknown. Pass a handle at geometry in "
                      "the instance you mean, or a world axis (x/y/z).")
    root = _common.safe(lambda: design.rootComponent) if design is not None else None
    if root is None:
        return None, (f"{label} belongs to component '{owner_name}' and this design's root component "
                      "could not be read, so where that component sits in the assembly is unknown.")
    occs = _common.safe(lambda: root.allOccurrencesByComponent(owner))
    count = (_common.safe(lambda: occs.count, 0) or 0) if occs is not None else 0
    if count == 1:
        occ = _common.safe(lambda: occs.item(0))
        if occ is None:
            return None, (f"{label} belongs to component '{owner_name}', whose single placement "
                          "could not be read, so it cannot be brought into the assembly's space.")
        return occ, None
    if count > 1:
        # Capped: a component can be placed dozens of times, and the caller picks the instance it
        # re-addresses out of this list.
        paths = _common.named_with_remainder(
            [str(_common.safe(lambda i=i: occs.item(i).fullPathName)) for i in range(count)])
        return None, (f"{label} belongs to component '{owner_name}', which is placed {count} times "
                      f"({paths}). Each instance holds it somewhere different, so the instance must "
                      "not be guessed. Pass a handle at geometry in the instance you mean, or a "
                      "world axis (x/y/z).")
    return None, (f"{label} belongs to component '{owner_name}', which is not placed in the "
                  "assembly, so it cannot be brought into the assembly's space. Pass a handle at "
                  "geometry in the instance you mean, or a world axis (x/y/z).")


def _proxy_or_refuse(label, ent, occ, fix_hint):
    """(the entity proxied into `occ`, error) - the leaf op an assembly-context lift ends on. A
    createForAssemblyContext handing back nothing is REFUSED naming the entity and the occurrence:
    an `or ent` fallback returns the very object the lift exists to avoid, which Fusion then rejects
    ('object is not in the assembly context of this component') with nothing pointing at why."""
    proxy = _common.safe(lambda: ent.createForAssemblyContext(occ))
    if proxy is not None:
        return proxy, None
    path = (_common.safe(lambda: occ.fullPathName) or _common.safe(lambda: occ.name)
            or "its one occurrence")
    return None, (f"{label} could not be read in the assembly's space ({path}), so where it sits in "
                  f"the model is unknown. {fix_hint}")


def _datum_world_line(name, ent):
    """(the datum axis's line in WORLD space, error) for a ConstructionAxis."""
    # MEASURED: a ConstructionAxis has NO worldGeometry, and its `.geometry` is COMPONENT-LOCAL for
    # a native datum - off by the owning component's placement, so a caller treating it as world
    # turns about the wrong line. The leaf op here is reading .geometry off the proxy.
    des = _common.design()
    root = _common.safe(lambda: des.rootComponent) if des else None
    occ, err = single_placement(f"'{name}': that construction axis", ent, root, des)
    if err:
        return None, err
    if occ is None:
        # already a proxy (it reads WORLD), or root-owned (local IS world)
        return _common.safe(lambda: ent.geometry), None
    proxy = _common.safe(lambda: ent.createForAssemblyContext(occ))
    g = _common.safe(lambda: proxy.geometry) if proxy is not None else None
    if g is None:
        path = _common.safe(lambda: occ.fullPathName) or "its one occurrence"
        return None, (f"'{name}': that construction axis could not be read in the assembly's space "
                      f"({path}), so where it sits in the model is unknown. Pass a world axis "
                      "(x/y/z) or a handle at a straight edge.")
    return g, None


def axis_line_of(name, ent):
    """((Point3D on the line, unit Vector3D), err) for the world line a straight entity runs along.
    A bounded edge/sketch line's Line3D carries only startPoint/endPoint, so the direction is
    DERIVED; an InfiniteLine3D carries .origin/.direction. A BRepEdge/SketchLine reads WORLD through
    `.worldGeometry`; a ConstructionAxis has none and takes the lift in _datum_world_line."""
    if _isinstance(ent, adsk.fusion.ConstructionAxis):
        line, lerr = _datum_world_line(name, ent)
        if lerr:
            return None, lerr
    else:
        line = _common.safe(lambda: ent.worldGeometry) or _common.safe(lambda: ent.geometry)
    sp = _common.safe(lambda: line.startPoint) if line is not None else None
    ep = _common.safe(lambda: line.endPoint) if line is not None else None
    if sp is not None and ep is not None:
        vec = _common.safe(lambda: sp.vectorTo(ep))
        if vec is None or _common.safe(lambda: vec.length, 0.0) <= 1e-12:
            return None, f"'{name}': that edge/sketch line is degenerate (zero length) - no axis direction."
        _common.safe(lambda: vec.normalize())
        return (sp, vec), None
    origin = _common.safe(lambda: line.origin) if line is not None else None
    direction = _common.safe(lambda: line.direction) if line is not None else None
    if origin is not None and direction is not None:
        return (origin, direction), None
    return None, f"'{name}': could not read the line geometry off that edge/sketch line."


def _construction_axis_by_name(label, comp, want):
    """(ConstructionAxis, error, available names) for a construction-axis NAME in `comp` - the one
    place an axis resolves by name, so a datum an agent created is reachable without a handle.
    Case-insensitive EXACT, never a substring; scope is the ACTIVE component only, a name being
    unique per component rather than per design."""
    names, hits = [], []
    for ax in _common.iter_collection(_common.safe(lambda: comp.constructionAxes)):
        nm = _common.safe(lambda ax=ax: ax.name)
        if not isinstance(nm, str):
            continue
        names.append(nm)
        if nm.strip().lower() == want.strip().lower():
            hits.append(ax)
    if len(hits) > 1:
        return None, (f"'{label}': '{want}' names {len(hits)} construction axes in "
                      f"'{_common.safe(lambda: comp.name)}' - which one is meant cannot be told from "
                      "the name, so it is refused rather than guessed. Rename them, or pass the "
                      "axis 'handle' from the model_construction call that created it."), names
    return (hits[0] if hits else None), None, names


class AxisRef(InputKind):
    """A direction/axis: a world axis (x / y / z), a 'handle' pointing at a straight (linear) EDGE, a
    SKETCH LINE or a CONSTRUCTION AXIS (the axis runs ALONG that entity), the NAME of a construction
    axis in the active component, OR a FACE handle used as a direction source (a planar face -> its
    NORMAL, a cylindrical/conical face -> its AXIS)."""

    # Resolves to ('world', vector) or ('edge', entity). entity_only=True refuses a face handle,
    # whose VECTOR a feature input wanting a linear ENTITY cannot consume; face_entity=True is the
    # opposite - an axis-bearing face resolves to ('edge', face), carrying the axis POSITION.

    MAP_HINT = ("a direction: world x/y/z, a construction axis (name or handle), a straight-edge/"
                "sketch-line handle, OR a face normal/axis")

    def __init__(self, name, entity_only=False, face_entity=False, **kw):
        super().__init__(name, **kw)
        self.entity_only = entity_only
        self.face_entity = face_entity

    def contract_note(self) -> str:
        # Where a NAME is looked up (the active component only) is in the miss refusal, which reads
        # that component and names it.
        if self.entity_only:
            return "x/y/z, a construction-axis name, or a straight-edge/line 'handle'."
        if self.face_entity:
            return "x/y/z, a construction-axis name, or an edge/line/round-face 'handle'."
        # The face forms RESOLVE (a planar face to its normal, a round one to its axis), so no
        # refusal ever states them - this note is their only home.
        return ("A world axis x/y/z, a construction-axis name, or an edge/line/face 'handle' "
                "(planar = normal, round = axis).")

    def _from_entity(self, ent):
        """(tagged value, error) for the entity a handle resolved to."""
        if isinstance(ent, adsk.fusion.BRepEdge):
            ct = _common.safe(lambda: ent.geometry.curveType)
            if ct == adsk.core.Curve3DTypes.Line3DCurveType:
                return ("edge", ent), None
            return None, f"'{self.name}': that edge is not straight - an axis needs a LINEAR edge."
        if isinstance(ent, adsk.fusion.SketchLine):
            return ("edge", ent), None      # a SketchLine is always straight by construction
        if _isinstance(ent, adsk.fusion.ConstructionAxis):
            # .geometry is an InfiniteLine3D (origin + direction) - axis_line_of reads that shape.
            return ("edge", ent), None
        if _isinstance(ent, adsk.fusion.BRepFace):
            if self.face_entity:
                return self._axis_defining_face(ent)
            if self.entity_only:
                return None, (f"'{self.name}': a face gives a direction VECTOR, and this input "
                              "needs a linear ENTITY. Pass a world axis (x/y/z), a construction-axis "
                              "name, or a handle at a straight edge or sketch line.")
            return _axis_from_face(self.name, ent)   # planar normal / cylinder-cone axis
        return None, (f"'{self.name}': handle points at a {type(ent).__name__}, not an edge, "
                      "sketch line, construction axis, or face.")

    def _axis_defining_face(self, face):
        """(tagged value, error) for a face on a face_entity input: the FACE itself where its
        surface defines an axis (cylinder / cone / torus), refused where it does not."""
        st = _common.safe(lambda: face.geometry.surfaceType)
        ST = adsk.core.SurfaceTypes
        axis_bearing = (_common.safe(lambda: ST.CylinderSurfaceType),
                        _common.safe(lambda: ST.ConeSurfaceType),
                        _common.safe(lambda: ST.TorusSurfaceType))
        if st is not None and st in axis_bearing:
            return ("edge", face), None
        return None, (f"'{self.name}': that face has no axis to turn about - only a cylindrical, "
                      "conical or toroidal face defines one (a planar face gives a direction, not a "
                      "line). Pass a world axis (x/y/z), a construction-axis name, or a handle at a "
                      "straight edge or sketch line.")

    def resolve(self, raw):
        s = (raw or "").strip() if isinstance(raw, str) else raw
        if not s:
            if self.required:
                return None, f"'{self.name}' is required (a world axis x/y/z or an edge handle)."
            return self.default, None
        if not isinstance(s, str):
            # An axis is either a world-axis alias or a handle STRING - a non-string can be neither.
            return None, (f"'{self.name}': expected a world axis (x/y/z) or an edge handle string, "
                          f"got {type(raw).__name__}.")
        low = s.lower()
        if low in _AXIS_VECS:
            return ("world", _AXIS_VECS[low]), None
        # else treat as an edge handle
        des = _common.design()
        if not des:
            return None, "No active design to resolve the axis against."
        # Through _resolve_token_entity so a COMPOSITE handle resolves: it splits the '|@locator'
        # suffix off before findEntityByToken, which the whole string would never resolve.
        ent = _resolve_token_entity(des, s)
        if ent is not None:
            return self._from_entity(ent)
        # Not a token: a construction axis by NAME, resolved by what resolves, so a long axis name
        # is never mistaken for a stale handle.
        comp = _common.safe(lambda: _common.target_component(des))
        axis, aerr, names = _construction_axis_by_name(self.name, comp, s)
        if aerr:
            return None, aerr
        if axis is not None:
            return ("edge", axis), None
        # entity_only refuses a face handle above, so the miss message must not offer one either.
        forms = "edge/sketch line handle." if self.entity_only else "edge/sketch line / face handle."
        # The name lookup only walked the ACTIVE component, so the refusal says where it looked -
        # otherwise a datum in another component reads as nonexistent.
        comp_name = _common.safe(lambda: comp.name) if comp is not None else None
        where = f" in the active component '{comp_name}'" if comp_name else ""
        found = ""
        if names:
            found = (f" Construction axes in '{comp_name}': "
                     + _common.named_with_remainder(names, cap=10) + ".")
        elif comp_name:
            found = f" '{comp_name}' has no construction axes of its own."
        return None, (f"'{self.name}': '{s}' is not a world axis (x/y/z), a construction-axis name"
                      f"{where}, or a resolvable {forms}{found}")


# ── distance / units (carries its own unit handling) ────────────────────────

class Distance(InputKind):
    """A length value in display 'units', resolved to Fusion's internal cm. The companion 'units'
    input is declared separately (UnitField); resolve() is given the already-chosen scale factor."""

    json_type = "number"
    MAP_HINT = "a length in display units (pair with one UnitField)"

    def __init__(self, name, allow_zero=False, allow_negative=True, **kw):
        super().__init__(name, **kw)
        self.allow_zero = allow_zero
        self.allow_negative = allow_negative

    def contract_note(self) -> str:
        bits = []
        if not self.allow_zero:
            bits.append("non-zero")
        if not self.allow_negative:
            bits.append("positive")
        return ("In 'units'" + ("; " + ", ".join(bits) if bits else "") + ".")

    def resolve_scaled(self, raw, scale_factor):
        if raw is None:
            if self.required:
                return None, f"'{self.name}' is required (a length in 'units')."
            return self.default, None
        try:
            v = float(raw)
        except Exception:
            return None, f"'{self.name}' must be a number."
        # NaN and +/-Infinity are floats but not LENGTHS, and Python's JSON decoder accepts those
        # literals. Both guards below let them through - every comparison against NaN is False.
        if not math.isfinite(v):
            return None, f"'{self.name}' must be a finite number, got {v}."
        if not self.allow_zero and v == 0:
            return None, f"'{self.name}' must be non-zero, got {v}."
        if not self.allow_negative and v < 0:
            return None, f"'{self.name}' must be positive, got {v}."
        return v * scale_factor, None


def looks_like_expression(v) -> bool:
    """True if v is a non-numeric string - a parameter EXPRESSION ('StockZ/2', '25 mm'). A plain
    numeric string ('25') is a literal, resolved the numeric way."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s:
        return False
    try:
        float(s)
        return False
    except ValueError:
        return True


def length_value_input(raw, k, design, label):
    """(ValueInput, value_cm, error) for a length that may be a literal number OR a
    parameter-expression string: a number is scaled to internal cm (createByReal), a string built
    with createByString, which ties the feature to a live parameter. ``value_cm`` is the internal-cm
    value the validation computed - the number a caller guards a sign or a read-back with."""
    if looks_like_expression(raw):
        expr = raw.strip()
        um = _common.safe(lambda: design.unitsManager)
        try:
            # evaluateExpression raises on an unresolvable/dimension-incompatible expression, which
            # is what refuses it BY NAME here rather than opaquely at feature add().
            value_cm = um.evaluateExpression(
                expr, _common.safe(lambda: um.defaultLengthUnits) or "mm")
        except Exception as e:
            return None, None, (f"'{label}' expression '{expr}' did not evaluate - use a length "
                                f"expression like 'StockZ/2' or '25 mm' and confirm the parameter "
                                f"names exist (param_get): {e}")
        if not isinstance(value_cm, (int, float)) or isinstance(value_cm, bool):
            value_cm = None      # a read that is not a number is not a length
        return adsk.core.ValueInput.createByString(expr), value_cm, None
    try:
        cm = float(raw) * k
    except (TypeError, ValueError):
        return None, None, f"'{label}' must be a number or a parameter-expression string."
    return adsk.core.ValueInput.createByReal(cm), cm, None


def expression_report(value):
    """The length value echoed back: an expression string as-is, else the rounded literal number."""
    if looks_like_expression(value):
        return value.strip()
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return value


class UnitField(InputKind):
    """The 'units' selector. resolve() returns the cm-per-unit scale factor.
    schema() emits a JSON-schema `enum` of mm/cm/in, so the legal values are validated rather than
    spelled in prose."""

    _UNITS = ["mm", "cm", "in"]
    MAP_HINT = ("the 'units' selector (mm/cm/in enum, mm default) for a Distance; every "
                "geometry-reporting read takes one and scales its output via CM_TO_UNIT")

    def __init__(self, name="units", **kw):
        super().__init__(name, default="mm", **kw)

    def schema(self, brief=False) -> dict:
        return {"type": "string", "enum": list(self._UNITS), **self._desc(brief)}

    def contract_note(self) -> str:
        return ""      # the legal values live in the schema `enum`, the default in `default`

    def resolve(self, raw):
        f = _common.scale(raw or "mm")
        if f is None:
            return None, f"Unknown units '{raw}'. Use mm, cm, or in."
        return f, None


# ── enum / choice ───────────────────────────────────────────────────────────

class Choice(InputKind):
    """One of a fixed set of string options. Emits a JSON-schema `enum` so the legal values are
    machine-validated and carried by the SCHEMA - the description does NOT re-list them."""

    MAP_HINT = "one of a fixed set -> JSON enum"

    def __init__(self, name, options, **kw):
        super().__init__(name, **kw)
        self.options = list(options)

    def schema(self, brief=False) -> dict:
        # the values live in `enum` (validated), not spelled into the description.
        return {"type": "string", "enum": list(self.options), **self._desc(brief)}

    def contract_note(self) -> str:
        # the enum carries the option list and the schema `default` the default - nothing in prose.
        return ""

    def resolve(self, raw):
        v = (raw or self.default or "").strip().lower()
        if not v and not self.required:
            return self.default, None
        if v not in [o.lower() for o in self.options]:
            return None, f"'{self.name}' must be one of: {', '.join(self.options)} (got '{raw}')."
        return v, None


# ── occurrence reference (an assembly instance, by its entityToken handle or a path/name) ─────────

# Neither an occurrence's name nor its fullPathName is unique, so the entityToken is the FIRST form
# tried and every by-string form refuses an ambiguity instead of first-matching.

def _occurrence_discriminator(occ) -> str:
    """What tells THIS instance apart from another wearing the same path/name: its component,
    whether it is an external reference, and the entityToken 'handle' the caller passes straight
    back - unique within the ONE document this resolver searches."""
    comp = _common.safe(lambda o=occ: o.component.name) or "?"
    # isReferencedComponent is trustworthy HERE and only here: it is measured LYING on an
    # unresolved reference, and this list comes from occurrence_walk.occurrences, which excludes
    # every broken row. Read off any other source it reports a broken xref as local.
    ref = _common.read_flag(lambda o=occ: o.isReferencedComponent)
    ref_label = {True: "referenced", False: "local"}.get(ref, "reference state unreadable")
    token = _common.safe(lambda o=occ: o.entityToken) or "(unreadable)"
    return f"component '{comp}', {ref_label}, handle '{token}'"


def _occurrence_candidates(occs):
    """The candidate list a PATH-collision refusal names - one discriminator per hit, the shared
    path being already quoted in the sentence. Each row is parenthesised because a discriminator
    carries commas of its own, which the renderer's ``, `` join would blur into the next row."""
    return _common.named_with_remainder([f"({_occurrence_discriminator(o)})" for o in occs])


def _occurrence_path_candidates(occs):
    """(rendered, collide) for a by-NAME refusal: each hit as its fullPathName, except a path
    SEVERAL hits wear, which renders as that path plus its discriminator. MEASURED - two
    occurrences can wear one byte-identical fullPathName, and ``collide`` is what decides the
    REMEDY there, since "pass the exact fullPathName" would name a string that comes back here."""
    rows = []
    for occ in occs:
        path = _common.safe(lambda o=occ: o.fullPathName) or "?"
        rows.append((path, f"{path} ({_occurrence_discriminator(occ)})"))
    paths = [p for p, _ in rows]
    return _common.named_with_remainder(_common.told_apart(rows)), len(set(paths)) < len(paths)


# The stem of _resolve_occurrence's MISS refusal. A caller whose target vocabulary is WIDER than an
# occurrence matches on this to tell a plain miss - keep trying - from a REFUSAL it must pass
# through. The mirror of BODY_MISS.
OCCURRENCE_MISS = "no occurrence matching"

# The refusal for an occurrence that EXISTS but whose external reference will not load - distinct
# from OCCURRENCE_MISS, since "no such occurrence" would be false while the browser tree shows it.
# A caller propagating a hard refusal matches on this phrase rather than re-deriving the condition.
UNRESOLVED_REFERENCE_REFUSAL = "referenced component could not be loaded"


def _resolve_occurrence(name, raw, candidates=None, exact_only=False):
    """(occurrence, error) for `raw`; exact_only skips the legacy unique-substring fallback."""
    want = (raw or "").strip() if isinstance(raw, str) else raw
    if not want:
        return None, f"'{name}' is required (an occurrence handle or fullPathName from design_get(include=['tree']))."
    des = _common.design()
    if not des:
        return None, "No active design to resolve the occurrence against."
    # 1) an entityToken handle, by what RESOLVES rather than by the string's shape. A token
    # resolving to something ELSE is REFUSED naming what it found: falling through would report
    # "no occurrence matching <token>" and hide the real mistake.
    ent = _resolve_token_entity(des, want)
    if ent is not None:
        if _isinstance(ent, adsk.fusion.Occurrence):
            return ent, None
        return None, (f"'{name}': that handle points at a {type(ent).__name__}, not an occurrence. "
                      "design_get(include=['tree']) emits an occurrence handle.")
    walk = _common.occurrence_walk(des)
    occs = walk.occurrences
    # MEASURED: Fusion mints occurrence names carrying a LEADING SPACE (' Handle:1'), invisible in
    # every listing, so the NAME comparison strips both sides as _match_timeline_objects does - and
    # two occurrences differing only by that space are ONE ambiguity, never a first match.
    paths = [(_common.safe(lambda o=o: o.fullPathName) or "") for o in occs]
    names = [(_common.safe(lambda o=o: o.name) or "").strip() for o in occs]
    # 2) exact fullPathName - ALL hits, never the first. Two siblings CAN wear one path, and no
    # string tells them apart, so the refusal hands back the handles that do.
    by_path = [o for o, fp in zip(occs, paths) if fp == want]
    if len(by_path) == 1:
        return by_path[0], None
    if len(by_path) > 1:
        if candidates is not None:
            candidates.extend(by_path)
        return None, (f"'{name}': the path '{want}' is worn by {len(by_path)} occurrences - "
                      f"{_occurrence_candidates(by_path)}. Pass the 'handle' of the one you mean.")
    # 3) exact name - ALL hits, never the first. MEASURED: Occurrence.name is NOT unique, since
    # instancing a sub-assembly replicates its children's names verbatim ("SubA:1+Bolt:2" and
    # "SubA:2+Bolt:2" both read "Bolt:2"), so a first match targets the wrong instance.
    exact = [o for o, nm in zip(occs, names) if nm == want]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        if candidates is not None:
            candidates.extend(exact)
        cands, collide = _occurrence_path_candidates(exact)
        if collide:
            return None, (f"'{name}': '{want}' names {len(exact)} occurrences whose fullPathNames "
                          f"do not tell them all apart - {cands}. Pass the 'handle' of the one you "
                          "mean (design_get(include=['tree']) emits it beside the path).")
        return None, (f"'{name}': '{want}' names {len(exact)} occurrences ({cands}). Pass the exact "
                      "fullPathName, or a 'handle' (design_get(include=['tree']) emits both).")
    if exact_only:
        # A qualified body scope names an occurrence instance, so a nearly matching ':10' or
        # another path containing the text cannot stand in for the requested instance.
        case_exact = [o for o, nm in zip(occs, names) if nm.lower() == want.lower()]
        if len(case_exact) == 1:
            return case_exact[0], None
        if len(case_exact) > 1:
            if candidates is not None:
                candidates.extend(case_exact)
            cands, collide = _occurrence_path_candidates(case_exact)
            if collide:
                return None, (f"'{name}': '{want}' names {len(case_exact)} occurrences whose fullPathNames "
                              f"do not tell them all apart - {cands}. Pass the 'handle' of the one you "
                              "mean (design_get(include=['tree']) emits it beside the path).")
            return None, (f"'{name}': '{want}' names {len(case_exact)} occurrences ({cands}). Pass the exact "
                          "fullPathName, or a 'handle' (design_get(include=['tree']) emits both).")
    # 4) substring on name - but ONLY if unique
    low = want.lower()
    if exact_only:
        hits = []
    else:
        hits = [o for o, nm in zip(occs, names) if low in nm.lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        if candidates is not None:
            candidates.extend(hits)
        cands, collide = _occurrence_path_candidates(hits)
        if collide:
            return None, (f"'{name}': '{want}' is ambiguous - it matches {len(hits)} occurrences "
                          f"whose fullPathNames do not tell them all apart - {cands}. Pass the "
                          "'handle' of the one you mean (design_get(include=['tree']) emits it "
                          "beside the path).")
        return None, (f"'{name}': '{want}' is ambiguous - matches {len(hits)} occurrences "
                      f"({cands}). Pass the exact fullPathName, or a 'handle' "
                      "(design_get(include=['tree']) emits both).")
    # 5) an UNRESOLVED reference: the occurrence EXISTS but its component will not load, so it was
    # kept out of the lists above. Matched on the EXACT name or the published '<parent path>+<name>'
    # form, never a substring, which would attribute the miss to the wrong row.
    unresolved = [(o, b) for o, b in zip(walk.broken_occurrences, walk.broken)
                  if b["name"] == want or want.endswith("+" + b["name"])]
    if unresolved:
        first = unresolved[0][1]
        return None, (f"'{name}': '{want}' matches {len(unresolved)} occurrence(s) whose "
                      f"{UNRESOLVED_REFERENCE_REFUSAL} ({first['detail']}), so there is nothing to "
                      "act on - no geometry, placement or children are readable. The source file is "
                      "not reachable through the API; open the browser tree in Fusion and hover the "
                      "flagged node. workspace_orient health.unresolved_references lists them.")
    sample = ", ".join(p for p in paths[:12] if p)
    return None, (f"'{name}': {OCCURRENCE_MISS} '{want}'. Available (sample): {sample or '(none)'}. "
                  "Use design_get(include=['tree']) for the full list (each row carries its handle)."
                  + _handle_refusal_suffix())


class OccurrenceRef(InputKind):
    """A reference to an assembly OCCURRENCE (a component instance): a `handle` - its entityToken, which
    design_get(include=['tree']) emits - or its `fullPathName` / `name`. Fusion enforces no name
    uniqueness at any level, so a path or name several instances answer to is refused with each
    candidate's handle rather than first-matched."""

    MAP_HINT = "an assembly occurrence by entityToken handle (exact) or fullPathName/name (refuses ambiguity)"

    def contract_note(self) -> str:
        return "An occurrence 'handle' (design_get tree) or fullPathName/name."

    def resolve(self, raw):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' is required (an occurrence handle or fullPathName)."
            return self.default, None
        return _resolve_occurrence(self.name, raw)


class OccurrenceRefList(InputKind):
    """A list of occurrence references (JSON list or comma-separated), each resolved via OccurrenceRef's
    handle-first, ambiguity-refusing logic. ALL must resolve (an unresolved/ambiguous element
    fails the whole list, with its value named, so a tool never half-applies)."""

    json_type = "array"
    MAP_HINT = "several occurrences (entityToken handles or fullPathNames/names)"

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, **self._desc(brief)}

    def contract_note(self) -> str:
        return "Occurrence 'handle's (design_get tree) or fullPathNames/names."

    def resolve(self, raw):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' is required (occurrence handles or fullPathNames)."
            return self.default, None
        if isinstance(raw, str):
            wanted = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            wanted = [str(s).strip() for s in raw if str(s).strip()]
        out = []
        for i, w in enumerate(wanted):
            occ, err = _resolve_occurrence(f"{self.name}[{i}]", w)
            if err:
                return None, err
            out.append(occ)
        return out, None


# ── joint-origin reference (a reusable WCS frame, by handle OR name; ambiguity refused) ───────────────

# Composes the ONE JO walk in _joints (all_joint_origins / find_joint_origins_by_name /
# jo_assembly_proxy) - it never re-rolls the traversal.

def _jo_available_rows(des):
    """The ``(name, discriminator)`` rows a Joint Origin MISS lists, one per JointOrigin, each side
    quoted so the two render alike. The discriminator is the qualified
    '<occurrence fullPathName>:<name>' form(s) joined on ' / ', so a row carrying several stays ONE
    entry; a form EQUAL to the name is dropped, a root-owned JO having nothing to qualify it."""
    rows = []
    for jo, comp in _joints.all_joint_origins(des):
        nm = _common.safe(lambda jo=jo: jo.name)
        if not nm:
            continue
        forms = [f for f in _joints.jo_reference_names(des, jo, comp) if f and f != nm]
        rows.append((f"'{nm}'", " / ".join(f"'{f}'" for f in forms)))
    return rows


class JointOriginRef(InputKind):
    """A reference to a Joint Origin (a reusable WCS coordinate frame), as EITHER a 'handle' (the
    entityToken assembly_get(include=['joint_origins']) mints) OR a name - bare when unique across
    the design, else the qualified '<occurrence>:<JO name>' form picking the exact instance.
    Resolves in assembly context, or with native=True to the JO its owning component carries."""

    MAP_HINT = "a Joint Origin by assembly_get handle OR name (bare if unique, else '<occ>:<JO name>'); refuses ambiguity"

    def __init__(self, name, native=False, **kw):
        """native=True selects the JO as its OWNING COMPONENT carries it, minting no proxy - for a
        consumer that READS the frame rather than jointing to it, the proxy being a different object
        with an unmeasured space contract."""
        super().__init__(name, **kw)
        self.native = native

    def contract_note(self) -> str:
        if self.native:
            return "A Joint Origin 'handle' (assembly_get) or name; its owning component's frame."
        return "A Joint Origin 'handle' (assembly_get joint_origins) or name."

    def resolve(self, raw):
        s = (raw or "").strip() if isinstance(raw, str) else raw
        if not s:
            if self.required:
                return None, f"'{self.name}' is required (a Joint Origin handle or name)."
            return self.default, None
        if not isinstance(s, str):
            return None, (f"'{self.name}': expected a Joint Origin handle or name string, got "
                          f"{type(raw).__name__}.")
        des = _common.design()
        if not des:
            return None, "No active design to resolve the Joint Origin against."
        # 1) a handle -> a JointOrigin; a non-token name yields nothing and falls through.
        ent = _resolve_token_entity(des, s)
        if ent is not None:
            if _joints.is_joint_origin(ent):
                if self.native:
                    # MEASURED: an assembly_get JO handle round-trips to the PROXY, not the native
                    # - one handle per occurrence, each with its own world origin - so the native
                    # selector steps back to nativeObject, which reads None on a native already.
                    return _common.safe(lambda: ent.nativeObject) or ent, None
                return ent, None
            return None, (f"'{self.name}': that handle points at a {type(ent).__name__}, not a Joint "
                          "Origin. Use assembly_get(include=['joint_origins']) for a JO handle.")
        # 2) a qualified '<occurrence>:<JO name>' (the disambiguating form)
        jo, qerr = self._resolve_qualified(des, s)
        if jo is not None:
            return jo, None
        if qerr:
            return None, qerr
        # 3) a bare name - resolve only when unique, else refuse with the qualified candidates
        return self._resolve_bare(des, s)

    def _resolve_qualified(self, des, spec):
        """(jo, err) for '<occurrence>:<JO name>' proxied into that occurrence's context - (None,
        None) when spec is not a qualified form, so bare-name resolution still runs."""
        if ":" not in spec:
            return None, None
        head, _, tail = spec.rpartition(":")
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            return None, None
        occ, _occ_err = _resolve_occurrence(self.name, head)
        if occ is None:
            return None, None                 # head isn't an occurrence - let the bare-name path try
        native = _common.safe(lambda: occ.component.jointOrigins.itemByName(tail))
        if native is None:
            return None, (f"'{self.name}': occurrence '{head}' has no Joint Origin named '{tail}'.")
        if self.native:
            return self._native_for_instance(des, native, occ, tail)
        return _proxy_or_refuse(
            f"'{self.name}': Joint Origin '{tail}'", native, occ,
            "Pass its handle from assembly_get(include=['joint_origins']).")

    def _native_for_instance(self, des, jo, occ, nm):
        """(the native JointOrigin, error) for an INSTANCE-qualified reference under the native
        selector, which reads one frame per COMPONENT - so a reference naming one instance of a
        component placed SEVERAL times is REFUSED, honoring the '<occurrence>:' half in name only.
        A component placed once resolves; the bare name and the handle keep working either way."""
        comp = _common.safe(lambda: occ.component)
        root = _common.safe(lambda: des.rootComponent)
        occs = (list(_common.safe(lambda: root.allOccurrencesByComponent(comp)) or [])
                if root is not None and comp is not None else [])
        if len(occs) > 1:
            paths = [p for p in (_common.safe(lambda o=o: o.fullPathName) for o in occs) if p]
            where = f" ({_common.named_with_remainder(paths)})" if paths else ""
            return None, (
                f"'{self.name}': Joint Origin '{nm}' sits on a component placed {len(occs)} times"
                f"{where}, and this input reads the frame its owning COMPONENT carries - the same "
                f"one for every instance - so naming an instance does not select a different frame. "
                f"Pass the bare name '{nm}', or any of its handles from "
                "assembly_get(include=['joint_origins']) - that read lists one row per instance and "
                "this input reads the same component frame from each.")
        return jo, None

    def _resolve_bare(self, des, name):
        matches = _joints.find_joint_origins_by_name(des, name)
        if len(matches) == 1:
            jo, comp = matches[0]
            if self.native:
                return jo, None          # the component's own frame; no instance to pick between
            return _joints.jo_assembly_proxy(des, jo, comp)
        if len(matches) > 1:
            cands = []
            multi_placed = 0     # hits dropped because their forms all name ONE frame (native only)
            bare_only = 0        # hits whose ONLY reference form IS the name being refused
            for jo, comp in matches:
                names = _joints.jo_reference_names(des, jo, comp)
                # One string per OCCURRENCE, and under the native selector they all name the one
                # frame this would return - so a multi-placed owner's forms pick nothing.
                if self.native and len(names) > 1:
                    multi_placed += 1
                    continue
                # A candidate EQUAL to the name just refused is no way out: pass it back and this
                # branch refuses it again. It is counted below instead, with the handle as its
                # only reference.
                kept = [n for n in names if n != name]
                if not kept:
                    bare_only += 1
                cands.extend(kept)
            if not cands:
                # Nothing in the '<occurrence>:<JO name>' vocabulary names ONE of these frames, and
                # why is counted rather than assumed - a hit can drop out for either reason.
                why = []
                if multi_placed:
                    why.append(f"{multi_placed} match(es) sit on a component placed several times, "
                               "so no '<occurrence>:<JO name>' form picks one of them")
                if bare_only:
                    why.append(f"{bare_only} match(es) carry no reference form other than '{name}' "
                               "itself")
                return None, (f"'{self.name}': '{name}' is ambiguous - {len(matches)} Joint Origins "
                              f"share that name, and {', and '.join(why)}. Pass a handle from "
                              "assembly_get(include=['joint_origins']).")
            tail = (f" {bare_only} further match(es) carry no reference form other than '{name}' "
                    "itself - reach those by handle." if bare_only else "")
            return None, (f"'{self.name}': '{name}' is ambiguous - {len(matches)} Joint Origins share "
                          f"that name ({_common.named_with_remainder(cands)}). Pass one of these "
                          f"qualified names, or a handle from "
                          f"assembly_get(include=['joint_origins']).{tail}")
        avail = _common.told_apart(_jo_available_rows(des))
        hint = (" Available: " + _common.named_with_remainder(avail) + ".") if avail else ""
        return None, (f"'{self.name}': no Joint Origin named '{name}'.{hint} "
                      "Use assembly_get(include=['joint_origins']) to list them.")


# ── target reference (MULTI-SOURCE: a thing to MEASURE/COLOUR - body/face/mesh/occurrence/component/design) ──

# Composes the existing resolvers (_resolve_token_entity / _resolve_occurrence / _resolve_any_body /
# _export.find_component) rather than re-implementing them.


class TargetRef(InputKind):
    """A reference to a THING to measure/colour, resolved from any of several shapes:
    a find_geometry 'handle' at a body/face/mesh body, an occurrence fullPathName or name, a
    component name, a body name, or empty/'' for the WHOLE design. Resolves to (entity, kind) so
    the consumer can branch; 'allow' restricts which kinds are accepted."""

    # The DEFAULT accepted set. The extended kinds (an edge, a construction axis/plane) resolve too,
    # but a caller must OPT IN by listing them in allow=, or _check refuses them.
    _ALL_KINDS = ("body", "face", "mesh", "occurrence", "component", "design")
    MAP_HINT = "a thing to measure/colour: handle (body/face/mesh; edge+construction when allowed) OR occurrence/component/body name; ''=whole design"

    def __init__(self, name, allow=None, collapse_ambiguous_occurrences=False, **kw):
        super().__init__(name, **kw)
        self.allow = tuple(allow) if allow else self._ALL_KINDS
        # OPT-IN: let a name matching several instances of ONE component resolve to that COMPONENT.
        # It WIDENS what a call acts on, so a body/appearance/measure caller never inherits it.
        self.collapse_ambiguous_occurrences = bool(collapse_ambiguous_occurrences)

    def contract_note(self) -> str:
        # Built from `allow`, so a narrowed TargetRef never advertises a shape _check refuses.
        kinds = [k for k in ("body", "face", "mesh", "edge") if k in self.allow]
        if "construction_axis" in self.allow or "construction_plane" in self.allow:
            kinds.append("construction")
        named = [k for k in ("occurrence", "component", "body") if k in self.allow]
        parts = [f"A 'handle' ({'/'.join(kinds)})"] if kinds else []
        if named:
            article = "an" if named[0][0] in "aeiou" else "a"
            parts.append(f"{article} {'/'.join(named)} name")
        base = " or ".join(parts) if parts else "A target"
        if not parts:
            return base + "."
        if not kinds:
            base = base[0].upper() + base[1:]
        return base + ("; '' = whole design." if "design" in self.allow else ".")

    def _check(self, ent, kind):
        if kind not in self.allow:
            return None, (f"'{self.name}': that target is a {kind}, but this needs one of: "
                          f"{', '.join(self.allow)}.")
        return (ent, kind), None

    def _owning_body(self, ent, kind):
        """A face/edge handle resolves to the entity's OWNING body where 'body' is allowed, else
        the precise wrong-kind refusal naming `kind` stands."""
        owner = _common.safe(lambda: ent.body)
        if owner is not None and "body" in self.allow:
            return self._check(owner, "mesh" if _is_mesh(owner) else "body")
        return self._check(ent, kind)

    def resolve(self, raw):
        s = (raw or "").strip() if isinstance(raw, str) else raw
        des = _common.design()
        if not des:
            return None, "No active design to resolve the target against."
        # empty -> the whole design (root component)
        if not s:
            if "design" not in self.allow:
                return None, f"'{self.name}' is required (a handle, occurrence/component/body name)."
            return (_common.safe(lambda: des.rootComponent), "design"), None
        if not isinstance(s, str):
            return None, f"'{self.name}': expected a handle or a name string, got {type(raw).__name__}."
        # 1) a handle -> body / face / edge / mesh / construction datum, by the entity type it
        # resolves to; the edge and construction kinds are gated by allow=.
        ent = _resolve_token_entity(des, s)
        if ent is not None:
            if _isinstance(ent, adsk.fusion.BRepFace):
                if "face" in self.allow:
                    return self._check(ent, "face")
                # find_geometry mints no body handle, so for a body-consuming caller a face handle
                # NAMES its owning body - the same owner-walk _resolve_any_body does.
                return self._owning_body(ent, "face")
            if _isinstance(ent, adsk.fusion.BRepEdge):
                if "edge" in self.allow:
                    return self._check(ent, "edge")
                return self._owning_body(ent, "edge")
            if _isinstance(ent, adsk.fusion.ConstructionAxis):
                return self._check(ent, "construction_axis")
            if _isinstance(ent, adsk.fusion.ConstructionPlane):
                return self._check(ent, "construction_plane")
            if _is_mesh(ent):
                return self._check(ent, "mesh")
            if _is_brep(ent):
                return self._check(ent, "body")
            return None, f"'{self.name}': handle points at a {type(ent).__name__}, not a measurable target."
        # Captured NOW, for the caller's OWN value: step 4 re-attempts the string through
        # vocabularies that mutilate a composite handle and overwrite the refusal channel.
        handle_suffix = _handle_refusal_suffix()
        # 2) an occurrence (fullPathName, then name). An AMBIGUOUS name is a hard error here rather
        # than a fall-through, which could resolve to an unrelated entity and mask it.
        ambiguous = []
        occ, occ_err = _resolve_occurrence(self.name, s, candidates=ambiguous)
        if occ is not None:
            return self._check(occ, "occurrence")
        # ...unless this caller OPTED IN and every ambiguous hit is an instance of ONE component:
        # the ambiguity is then only about WHICH INSTANCE, and the component answers what was asked
        # (measured: a 'Bolt' with three instances dead-ends a component-level call).
        if ambiguous and self.collapse_ambiguous_occurrences and "component" in self.allow:
            # `is True` for EVERY hit: an identity that did not read leaves the ambiguity standing
            # rather than collapsing onto a component nothing proved owns all of it.
            shared = _common.safe(lambda: ambiguous[0].component)
            if shared is not None and all(_common.same_component(
                    shared, _common.safe(lambda o=o: o.component)) is True for o in ambiguous[1:]):
                return self._check(shared, "component")
        if occ_err and OCCURRENCE_MISS not in occ_err:
            # The OCCURRENCE_MISS stem, not the word "ambiguous": only ONE of the resolver's
            # refusals carries that word, so matching it would drop the two that name colliding
            # candidates and their handles, plus the unresolved-reference one.
            return None, occ_err
        # 3) a component by name, a shared name being a hard error here too: falling through to the
        # body vocabulary would measure/colour one of them without saying which was picked.
        comp, comp_err = _find_component(des, s)
        if comp_err:
            return None, (f"'{self.name}': {comp_err} Pass one instance's occurrence "
                          "name/fullPathName, or a find_geometry handle.")
        if comp is not None:
            return self._check(comp, "component")
        # 4) a body by name (brep or mesh). BODY_MISS decides here as OCCURRENCE_MISS does at step
        # 2: only a plain miss falls through to the wording below, every other refusal reaching the
        # caller intact, since each was READ off the design.
        body, body_err = _resolve_any_body(self.name, s)
        if body is not None:
            return self._check(body, "mesh" if _is_mesh(body) else "body")
        if body_err and BODY_MISS not in body_err:
            return None, body_err
        # The ''-means-whole-design hint only where this kind ACCEPTS it - a kind excluding 'design'
        # from allow= would otherwise advertise an unreachable form.
        empty_hint = ", or '' (whole design)" if "design" in self.allow else ""
        return None, (f"'{self.name}': '{s}' did not resolve to a body handle, an occurrence/component/"
                      f"body name{empty_hint}. See design_get(include=['tree']) / find_geometry."
                      + handle_suffix)


class TargetRefList(InputKind):
    """A LIST of targets - each a BODY (handle/name) or a component OCCURRENCE (name/fullPathName),
    resolved via TargetRef and kind-checked before any mutation. A bare COMPONENT name maps to its
    single occurrence, 0 or several being a hard error. 'contract' overrides the CAM-flavored
    contract note; 'with_kinds' returns (entity, kind) pairs so a consumer can branch."""

    json_type = "array"
    MAP_HINT = "several body-or-occurrence targets: bodies (handles/names) and/or component occurrences (names)"

    # Bodies and occurrences; a component resolves to its occurrence, mesh is allowed.
    _CAM_KINDS = ("body", "mesh", "occurrence", "component")

    def __init__(self, name, contract="", with_kinds=False, **kw):
        super().__init__(name, **kw)
        self._contract = contract
        self._with_kinds = with_kinds
        self._ref = TargetRef(name, allow=self._CAM_KINDS)   # one owned resolver, never a second

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, "description": self._full_desc(brief)}

    def contract_note(self) -> str:
        return self._contract or (
            "Body 'handle's/names and/or component occurrence names (an occurrence selects the "
            "whole component).")

    def _component_occurrence(self, comp):
        """The single occurrence referencing `comp`, or (None, error) on 0 or >1 (never guess)."""
        des = _common.design()
        root = _common.safe(lambda: des.rootComponent) if des else None
        occs = _common.safe(lambda: list(root.allOccurrencesByComponent(comp))) or [] if root else []
        if not occs:
            return None, (f"component '{_common.safe(lambda: comp.name)}' has no occurrence "
                          "(it is not instanced in the assembly).")
        if len(occs) > 1:
            return None, (f"component '{_common.safe(lambda: comp.name)}' has {len(occs)} occurrences - "
                          "ambiguous which to select; pass the occurrence by its fullPathName instead.")
        return occs[0], None

    def resolve(self, raw):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' needs at least one target (a body or component name)."
            return [], None
        items = raw if isinstance(raw, (list, tuple)) else [s.strip() for s in str(raw).split(",") if s.strip()]
        out = []
        for i, item in enumerate(items):
            resolved, err = self._ref.resolve(item)
            if err:
                return None, f"'{self.name}'[{i}]: {err}"
            ent, kind = resolved
            if kind == "component":
                occ, occ_err = self._component_occurrence(ent)   # the instance, not the definition
                if occ_err:
                    return None, f"'{self.name}'[{i}]: {occ_err}"
                ent, kind = occ, "occurrence"
            out.append((ent, kind) if self._with_kinds else ent)
        if not out:
            return None, f"'{self.name}': no valid targets resolved."
        return out, None


# ── profile reference (a STABLE handle, or a {sketch, profile_index} legacy selector) ────────────

def _profile_sketch(name, sketch_name, component="", scope_input=None):
    """(sketch, error) for the sketch a profile selector addresses - a NAME resolving DESIGN-WIDE
    (one several components carry REFUSED), blank meaning the most recent sketch in the active
    component. ``scope_input`` names the CONSUMING tool's own component-scope input and
    ``component`` its value; without one this resolves design-wide."""
    des = _common.design()
    if not des:
        return None, "No active design to resolve the profile against."
    if scope_input:
        # _sketch_detail imports this module, so the import is local; it owns the scope vocabulary,
        # so a profile selector narrows by exactly what a by-name sketch EDIT narrows by.
        from . import _sketch_detail
        sketch, sk_name, ambiguous = _sketch_detail.scoped_or_recent_sketch(
            des, sketch_name, component, scope_input)
    else:
        sketch, sk_name, ambiguous = _common.find_or_recent_sketch(des, sketch_name)
    if sketch:
        return sketch, None
    if ambiguous:
        return None, f"'{name}': {ambiguous}"
    if sk_name:
        # The SAME listing SketchRefList's miss prints, so the two answers cannot differ in length.
        return None, f"'{name}': no sketch named '{sk_name}'. Available: {_available_sketch_names(des)}."
    return None, f"'{name}': no sketch to take a profile from. Create one with a closed region."


def deferred_sketch_note(sketch):
    """The UNLABELLED sentence a profile reference into a DEFERRED sketch earns, or '' - the one
    home for that wording, for a caller that embeds it in a sentence of its own."""
    if sketch is None or _common.read_flag(lambda: sketch.isComputeDeferred) is not True:
        return ""
    return (f"sketch '{_common.safe(lambda: sketch.name) or '?'}' reads isComputeDeferred=true, so "
            "its profiles can be stale and this reference is not resolved against them. Resume "
            "compute - draw with sketch_add_geometry, or set isComputeDeferred=false with "
            "sys_execute_script - then re-read sketch_get for a fresh handle.")


def deferred_sketch_refusal(label, sketch):
    """That same sentence under the offending INPUT's name, or '' - what a tool resolving a profile
    off a sketch itself returns as its whole error, rather than re-rolling the wording."""
    note = deferred_sketch_note(sketch)
    return f"'{label}': {note}" if note else ""


def _text_count(sketch):
    """How many SketchTexts the sketch holds - the 'text:<i>' address space."""
    texts = _common.safe(lambda: sketch.sketchTexts)
    return _common.safe(lambda: texts.count, 0) if texts is not None else 0


def _resolve_profile_legacy(name, sketch_name, profile_index, allow_text=False, component="",
                            scope_input=None):
    """(profile, error) for a {sketch, profile_index} selector - the sketch through _profile_sketch,
    narrowed to `component` where the consumer declares a scope input, and a bounds-checked index."""
    sketch, serr = _profile_sketch(name, sketch_name, component, scope_input)
    if serr:
        return None, serr
    stale = deferred_sketch_refusal(name, sketch)
    if stale:
        return None, stale
    profiles = _common.safe(lambda: sketch.profiles)
    pcount = _common.safe(lambda: profiles.count, 0) if profiles else 0
    if pcount == 0:
        msg = f"'{name}': sketch '{_common.safe(lambda: sketch.name)}' has no closed profile. "
        # A text-only sketch never has a region to draw, so where this input takes a text, the
        # address that reaches it is the way forward rather than "draw one".
        ntext = _text_count(sketch) if allow_text else 0
        if ntext:
            addr = "'text:0'" if ntext == 1 else f"'text:0'..'text:{ntext - 1}'"
            return None, (msg + f"It holds {ntext} sketch text(s) - pass {addr} to stamp the text "
                          "itself.")
        return None, msg + "Draw a closed region first."
    try:
        idx = int(profile_index)
    except Exception:
        return None, f"'{name}': profile_index '{profile_index}' is not an integer."
    if idx < 0 or idx >= pcount:
        return None, (f"'{name}': profile_index {idx} out of range - sketch has {pcount} profile(s) "
                      f"(0..{pcount-1}).")
    return profiles.item(idx), None


# ── sketch TEXT as a profile input ───────────────────────────────────────────────────────────────

# A SketchText carries no Profile of its own, and needs none: EmbossFeatures.createInput and
# ExtrudeFeatures.createInput each document the SketchText itself as an accepted profile argument,
# while SweepFeatures, RevolveFeatures and LoftSections do not - which is what allow_text gates.
_TEXT_PREFIX = "text:"


def _split_text_ref(s):
    """How `s` reads as a SKETCH TEXT address - the id sketch_get publishes, optionally qualified
    as '<sketch>/text:<i>' and split on the LAST '/': ('<sketch or blank>', <index>) when it
    parses, ('<sketch or blank>', None) when it opens with 'text:' but carries no whole-number
    index (its own answer, so 'text:abc' is not answered by a message never mentioning text)."""
    if not isinstance(s, str):
        return None
    body = s.strip()
    sketch = ""
    if "/" in body:
        sketch, _, body = body.rpartition("/")
    if body[:len(_TEXT_PREFIX)].lower() != _TEXT_PREFIX:
        return None
    try:
        idx = int(body[len(_TEXT_PREFIX):].strip())
    except ValueError:
        idx = None
    return sketch.strip(), idx


def is_text_ref(v) -> bool:
    """True when `v` is a COMPLETE sketch-TEXT address: 'text:<i>' or '<sketch>/text:<i>' - the
    routing predicate for an input carrying both a text address and an index selector, since
    is_handle answers False for one and a tool asking only that reads 'text:0' as an index. Through
    the same parser ProfileRef resolves with, so routing and resolution cannot disagree."""
    parsed = _split_text_ref(v)
    return parsed is not None and parsed[1] is not None


def _resolve_sketch_text(name, sketch_name, index, raw, component="", scope_input=None):
    """(text, error) for the SketchText at 'text:<index>' in the addressed sketch, handed to a
    feature's profile slot as itself. `index` None means the address carried no whole number."""
    sketch, serr = _profile_sketch(name, sketch_name, component, scope_input)
    if serr:
        return None, serr
    sname = _common.safe(lambda: sketch.name)
    n = _text_count(sketch)
    if not n:
        # An UNQUALIFIED address took the most recent sketch, so the qualified form is the way to
        # reach a different one; a caller who already qualified it is not offered that again.
        elsewhere = "" if sketch_name else f" address another sketch as '<sketch>/{raw}',"
        return None, (f"'{name}': '{raw}' addresses a sketch text, but sketch '{sname}' holds none. "
                      f"Create one with sketch_set_text,{elsewhere} or pass a closed profile.")
    if index is None:
        return None, (f"'{name}': '{raw}' carries no whole-number text index - sketch '{sname}' "
                      f"holds {n} sketch text(s) (text:0..text:{n - 1}).")
    if index < 0 or index >= n:
        return None, (f"'{name}': '{raw}' is out of range - sketch '{sname}' holds {n} sketch "
                      f"text(s) (text:0..text:{n - 1}).")
    text = _common.safe(lambda: sketch.sketchTexts.item(index))
    if text is None:
        return None, f"'{name}': sketch '{sname}' would not hand back '{raw}'."
    return text, None


def profile_host_component(profile, sketch, fallback):
    """The component whose features collection can consume this profile: the one OWNING its sketch.
    VERIFIED LIVE: another component's native profile makes features.createInput raise
    'InternalValidationError : bSet'. Duck-typed, falling back where no owner is readable."""
    sk = _common.safe(lambda: profile.parentSketch)
    return _common.safe(lambda: (sk or sketch).parentComponent) or fallback


def _resolve_one_profile(name, raw, allow_text=False, component="", scope_input=None):
    """(profile, err) for a stable handle, a 'text:<i>' address (only where allow_text), or a
    {sketch, profile_index} selector dict - handle-first. `component` narrows the two forms that
    address a sketch BY NAME; a handle names one entity outright."""
    if isinstance(raw, dict):
        sk = raw.get("sketch", raw.get("sketch_name", ""))
        return _resolve_profile_legacy(name, sk, raw.get("profile_index", 0), allow_text,
                                       component, scope_input)
    s = (raw or "").strip() if isinstance(raw, str) else raw
    if not s:
        return None, f"'{name}' is required (a profile handle or a {{sketch, profile_index}} selector)."
    text_ref = _split_text_ref(s)
    if text_ref is not None:
        if not allow_text:
            return None, (f"'{name}': '{s}' addresses a SKETCH TEXT, which this input does not take. "
                          "Stamp text onto a face with model_emboss.")
        return _resolve_sketch_text(name, text_ref[0], text_ref[1], s, component, scope_input)
    des = _common.design()
    if not des:
        return None, "No active design to resolve the profile against."
    ent = _resolve_token_entity(des, s)
    if ent is not None:
        if isinstance(ent, adsk.fusion.Profile):
            stale = deferred_sketch_refusal(name, _common.safe(lambda: ent.parentSketch))
            return (None, stale) if stale else (ent, None)
        return None, f"'{name}': handle points at a {type(ent).__name__}, not a profile."
    if _LAST_REFIND_REFUSAL:
        # REFUSED for a stated reason. The generic sentence below says the value was not a profile
        # handle, which is a different fact from the one that was read.
        return None, f"'{name}': handle did not resolve - {_LAST_REFIND_REFUSAL}"
    return None, (f"'{name}': '{s}' did not resolve to a profile handle. Pass an entityToken from a "
                  "profile, or a {sketch, profile_index} selector.")


class ProfileRef(InputKind):
    """A reference to a sketch PROFILE - a stable 'handle' (entityToken, order-stable across rebuilds)
    OR a legacy {sketch, profile_index} selector (a blind index into an order-unstable collection).
    Resolves handle-first to the live adsk.fusion.Profile."""

    MAP_HINT = "a sketch profile by stable handle, not sketch_name+profile_index"

    # allow_text: this input's feature takes a SketchText in its profile slot. scope_input: the
    # consuming tool's own component-scope input, which narrows the {sketch, profile_index} and
    # '<sketch>/text:<i>' forms; left None, both resolve a sketch name design-wide.
    def __init__(self, name, allow_text=False, scope_input=None, **kw):
        super().__init__(name, **kw)
        self.allow_text = bool(allow_text)
        self.scope_input = scope_input or None

    def _text_note(self) -> str:
        if not self.allow_text:
            return ""
        return " Sketch text 'text:<i>'."

    def schema(self, brief=False) -> dict:
        # Both forms _resolve_one_profile takes: a handle/text STRING and the {sketch,
        # profile_index} OBJECT. A bare "string" bars a validating client from the latter.
        return {"type": ["string", "object"], **self._desc(brief)}

    def contract_note(self) -> str:
        return "A profile 'handle' or {sketch, profile_index}." + self._text_note()

    def resolve(self, raw, component=""):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' is required (a profile handle or {{sketch, profile_index}})."
            return self.default, None
        return _resolve_one_profile(self.name, raw, self.allow_text,
                                    component if self.scope_input else "", self.scope_input)


class ProfileRefList(ProfileRef):
    """An ORDERED list of profile references - for loft, where profile ORDER is load-bearing (the loft
    runs through the sections in the order given). PRESERVES ORDER: no sort, no dedupe."""

    json_type = "array"
    MAP_HINT = "an ORDERED list of profiles (loft - order is load-bearing)"

    def schema(self, brief=False) -> dict:
        # items carries BOTH element forms ProfileRef.resolve takes.
        return {"type": "array", "items": {"type": ["string", "object"]}, **self._desc(brief)}

    def contract_note(self) -> str:
        return "Profiles in order: 'handle's or {sketch, profile_index}." + self._text_note()

    def resolve(self, raw, component=""):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' needs at least one profile (handle or selector)."
            return [], None
        items = raw if isinstance(raw, (list, tuple)) else [s.strip() for s in str(raw).split(",") if s.strip()]
        scope = component if self.scope_input else ""
        out = []
        for i, item in enumerate(items):
            p, err = _resolve_one_profile(self.name, item, self.allow_text, scope, self.scope_input)
            if err:
                return None, f"'{self.name}'[{i}]: {err}"
            out.append(p)          # append in order - NO sort/dedupe (loft order is load-bearing)
        if not out:
            return None, f"'{self.name}': no valid profiles resolved."
        return out, None


# ── sketch reference (a SKETCH by name, design-wide - the non-unique name space) ─────────────────

# A COUNT cap, not a cut of the joined string: a duplicated name comes back owner-qualified
# ('Plate (Alpha)'), so a character cut lands inside a name and prints an unpassable fragment.
_SKETCH_NAMES_LISTED = 30


def _available_sketch_names(d):
    """The 'Available: ...' list every sketch-name MISS prints - up to _SKETCH_NAMES_LISTED whole
    names then a count of the rest, or '(none)'."""
    names = [n for n in _common.all_sketch_names(d) if n]
    if not names:
        return "(none)"
    return _common.named_with_remainder(names, cap=_SKETCH_NAMES_LISTED)


class SketchRefList(InputKind):
    """A LIST of SKETCHES by name - the reference an operation taking WHOLE sketches needs (a CAM
    SketchSelection's inputGeometry). A name carried by SEVERAL sketches is REFUSED naming each
    owning component, and the match is EXACT, so 'profile' does not answer 'Profile'.
    ``scope_input`` names the component-scope input inside which every name then resolves."""

    json_type = "array"
    MAP_HINT = "several sketches by name (refuses a name several sketches share)"

    def __init__(self, name, scope_input=None, **kw):
        super().__init__(name, **kw)
        self.scope_input = scope_input or None

    def schema(self, brief=False) -> dict:
        return {"type": "array", "items": {"type": "string"}, "description": self._full_desc(brief)}

    def contract_note(self) -> str:
        return "Sketch names, each naming exactly one sketch."

    def resolve(self, raw, component=""):
        if raw in (None, "", []):
            if self.required:
                return None, f"'{self.name}' needs at least one sketch name."
            return [], None
        items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        items = [str(s).strip() for s in items if str(s).strip()]
        if not items:
            return None, f"'{self.name}' needs at least one sketch name."
        d = _common.design()
        if d is None:
            return None, "No active design to resolve the sketch names against."
        scope = component if self.scope_input else ""
        out = []
        for i, want in enumerate(items):
            if self.scope_input:
                # _sketch_detail imports this module, so the import is local. Resolved per name
                # through the same helper a by-name sketch edit uses, so an unknown scope refuses.
                from . import _sketch_detail
                sk, ambiguous = _sketch_detail.scoped_sketch(d, want, scope, self.scope_input)
            else:
                sk, ambiguous = _common.find_sketch(d, want)
            if ambiguous:
                return None, f"'{self.name}'[{i}]: {ambiguous}"
            if sk is None:
                return None, (f"'{self.name}'[{i}]: no sketch named '{want}'. Available: "
                              f"{_available_sketch_names(d)}.")
            out.append(sk)
        return out, None


# ── the resolver: resolve all declared inputs at once ───────────────────────

def resolve_inputs(spec, raw_args):
    """(values_dict, error) for a list of InputKinds against the raw MCP args - a UnitField is
    resolved first to a scale factor, then each Distance is scaled by it. The error is a
    ready-to-return _common.error() result on the first failure."""
    values = {}
    # units first (Distance depends on it)
    scale_factor = 1.0
    unit_field = next((k for k in spec if isinstance(k, UnitField)), None)
    if unit_field is not None:
        sf, err = unit_field.resolve(raw_args.get(unit_field.name))
        if err:
            return None, _common.error(err)
        scale_factor = sf
        values[unit_field.name] = raw_args.get(unit_field.name) or unit_field.default

    for kind in spec:
        if kind is unit_field:
            continue
        raw = raw_args.get(kind.name)
        if isinstance(kind, Distance):
            val, err = kind.resolve_scaled(raw, scale_factor)
        else:
            val, err = kind.resolve(raw)
        if err:
            return None, _common.error(err)
        values[kind.name] = val
    return values, None


def apply_to_tool(tool, spec):
    """Add every InputKind's schema property to a Tool, marking the required ones, and return the
    tool - so the schema generates from the same declaration that drives resolution."""
    for kind in spec:
        tool.add_input_property(kind.name, kind.schema())
        if kind.required:
            tool.add_required_input(kind.name)
    return tool


def contract_block(spec, header="INPUTS") -> str:
    """The per-input contract notes as a description block; tools append their own FAILS-IF /
    PRODUCES lines."""
    lines = [f"{header}:"]
    for kind in spec:
        note = kind.contract_note() or ""
        req = " (required)" if kind.required else ""
        lines.append(f"- {kind.name}{req}: {note}".rstrip())
    return "\n".join(lines)


# ── shared input singletons (the recurring enums, defined ONCE) ──────────────────────────────────

# A tool wires one with `.add_input_property(*_inputs.UNITS.as_property())`; the factories below let
# it tweak the default or description while still sharing the option set.

def units_property(description="Length units.", default="mm"):
    """(name, schema) for a 'units' input, enum-backed via UnitField. Use *units_property()."""
    return UnitField(description=description).as_property()


# The ready-to-splat default units property (mm|cm|in, default mm).
UNITS = UnitField()


def boolean_op(name="operation", options=("new", "join", "cut", "intersect"), default="new",
               description="The feature operation."):
    """A Choice for a boolean/feature operation - tools pass the subset they support."""
    return Choice(name, list(options), default=default, description=description)


def frame_axis(name="axis", default="z", description="Axis: x, y, or z."):
    """A Choice for an x|y|z axis. WHICH frame that axis is read in is the calling tool's own
    contract - world for some, the component/joint frame for others - so it passes the wording."""
    return Choice(name, ["x", "y", "z"], default=default, description=description)


# The ONE set a joint tool's motion input is cut from, always as a Choice, so the options an agent
# reads ARE the ones the input accepts. A tool whose set differs passes `options=` explicitly.
JOINT_MOTIONS = ("rigid", "revolute", "slider", "cylindrical", "planar", "ball")


def joint_motion(name="joint_type", options=JOINT_MOTIONS, default="rigid",
                 description="The joint motion type."):
    """A Choice for a joint motion type. Pass `options=` to restrict to a tool's supported subset."""
    return Choice(name, list(options), default=default, description=description)
