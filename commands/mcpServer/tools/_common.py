# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The response/resolve substrate every tool module imports: ``ok``/``error``/``safe``, the active
``design()``/``target_component()`` resolvers, the by-name sketch walk, the identity/census reads,
and cm-based unit ``scale``/``UNIT_TO_CM``."""

import json
import math

import adsk.core
import adsk.fusion

MAP_BLURB = (
    "response+resolve: ok/error/safe (per-FIELD guard, never a MUTATION), "
    "measured/read_flag/counted (None, never a coerced 0/False), design/target_component, "
    "find_sketch/resolve_sketch (a shared name REFUSED), timeline_health/set_verified "
    "(effect reads), same_component/native_identity/occurrence_walk/broken_reference "
    "(TRI-STATE identity, census), scale/iter_collection/named_with_remainder/told_apart")

app = adsk.core.Application.get()


# ── response builders (the MCP tool-result contract) ────────────────────────

def ok(payload: dict) -> dict:
    """A successful tool result: JSON-encodes ``payload`` as the text content."""
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False}


def error(text: str) -> dict:
    """A failed tool result. ``message`` mirrors the text so callers can read either field."""
    return {"content": [{"type": "text", "text": text}], "isError": True, "message": text}


# ── safe getter ─────────────────────────────────────────────────────────────

def safe(getter, default=None):
    """Call ``getter()`` and swallow any exception, returning ``default`` - the per-FIELD read
    guard for a Fusion object model where a missing property raises. Never around a MUTATION."""
    try:
        return getter()
    except Exception:
        return default


_UNREADABLE = object()      # a getter that RAISED - distinct from one that returned None


def read_flag(getter):
    """A BOOLEAN flag read: True, False, or None when the getter raised or the flag read None.
    ``safe(read, False)`` would make an unreadable flag a confident False, which is an answer
    ("suppressed: no") and lets a swallowed write pass a ``now != wanted`` gate."""
    v = safe(getter, _UNREADABLE)
    return None if (v is _UNREADABLE or v is None) else bool(v)


def measured(getter, scale=1.0, places=6):
    """A measured number, scaled and rounded - or None when it cannot be read, since zero is an
    answer here ("no gap", "no mass", "parallel"). A COUNT goes through ``counted``."""
    v = safe(getter)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        return None
    scaled = v * scale
    if not math.isfinite(scaled):
        return None
    return round(scaled, places)


def counted(getter):
    """An integer COUNT read: the int, or None - for a count the caller goes on to COMPARE, where
    an unreadable read is neither 0 nor 1. A bool or a non-int reads as unknown."""
    n = safe(getter)
    return int(n) if isinstance(n, int) and not isinstance(n, bool) else None


# ── design / component resolution ───────────────────────────────────────────

def design():
    """The active Design, or None - falling back from ``activeProduct`` to the active document's
    DesignProductType, so it answers while a CAM product is active."""
    d = adsk.fusion.Design.cast(app.activeProduct)
    if not d:
        d = safe(lambda: adsk.fusion.Design.cast(
            app.activeDocument.products.itemByProductType('DesignProductType')))
    return d


def target_component(d):
    """Where new geometry lands: the ACTIVE edit target (``design.activeComponent``), else the
    root."""
    comp = safe(lambda: d.activeComponent)
    return comp if comp is not None else d.rootComponent


def same_component(a, b):
    """TRI-STATE: True when `a` and `b` are the SAME component, False when they are different, None
    when the comparison could not be made. Every caller branches on `is True` / `is False` /
    `is None` - a bare `if` reads None as the "different component" answer nothing supports."""
    # MEASURED: component identity is NEVER stable - two reads of design.rootComponent hand back
    # different Python objects sharing ONE entityToken - so `a is b` alone always takes the
    # "different" branch. The comparison runs on native_identity, never a bare (document-local) token.
    if a is None or b is None:
        return None
    if a is b:
        return True
    ia, ib = native_identity(a), native_identity(b)
    # Both sides through the None gate first: `ia == ib` alone would call two components whose
    # identities BOTH failed to read the same one, which is the merge this pair exists to refuse.
    if ia is None or ib is None:
        return None
    return ia == ib


def _native_of(entity):
    """The entity a wrapper STANDS FOR: its ``nativeObject``, else the wrapper itself (a kind that
    does not answer nativeObject at all is its own native)."""
    return safe(lambda: entity.nativeObject) or entity


def native_token(entity):
    """``(nativeObject or self).entityToken``, or None - DOCUMENT-LOCAL, so half an identity; the
    key a comparison or de-dup runs on is ``native_identity``."""
    # MEASURED: a body and its occurrence PROXY carry DIFFERENT tokens (each stable per wrapper),
    # so a de-dup on the wrapper's own token counts one body twice - hence the native first. A
    # published HANDLE is the wrapper's own token, which is what resolves back to that proxy.
    return safe(lambda: _native_of(entity).entityToken)


def _source_document_urn(native):
    """The lineage id of the document ``native`` lives in, or None (a never-saved document carries
    no dataFile, which is an answer: it cannot be x-ref'd, so its token is already unique)."""
    # MEASURED: BRepBody/MeshBody carry parentComponent and NOT parentDesign; Component carries
    # parentDesign and NOT parentComponent. Exactly ONE chain reads per kind.
    design = (safe(lambda: native.parentComponent.parentDesign)
              or safe(lambda: native.parentDesign))
    return safe(lambda: design.parentDocument.dataFile.id)


def native_identity(entity):
    """The PHYSICAL-entity key two entity references are compared or de-duplicated on: the pair
    ``(native_token, source-document lineage urn)``, or None when the token does not read (there is
    then no identity, and a caller keys on a name plus a scope or refuses)."""
    # MEASURED: a bare entityToken is DOCUMENT-LOCAL. Two DISTINCT components named 'Frame', one out
    # of each of two x-refs, read byte-identical tokens; so did the 7 root components of a CAM job
    # assembled from 7 source documents, whose urns all read distinct. The token alone merges them.
    token = native_token(entity)
    if not token:
        return None
    return (token, _source_document_urn(_native_of(entity)))


def root_body_advisory(d, comp):
    """A note (or '') for a build tool that just built into ROOT with no component active - fired
    only while switching is still cheap (root holds <=1 solid body and no sub-components)."""
    # same_component, not `is`: wrappers are never identity-stable, so `comp is not d.rootComponent`
    # reads True even AT the root. Only a proven True fires an advisory that ASSERTS root.
    if comp is None or same_component(comp, safe(lambda: d.rootComponent)) is not True:
        return ""                                  # a component IS active - the good path, say nothing
    body_n = safe(lambda: comp.bRepBodies.count, 0) or 0
    occ_n = safe(lambda: d.rootComponent.occurrences.count, 0) or 0
    if body_n > 1 or occ_n > 0:
        return ""                                  # past the early window - advising now would just nag
    return ("Built into the ROOT component (no component was active). Best practice is one component per "
            "part: create it with model_create_component(activate=true) FIRST, then build. Promoting a "
            "root body into a component later re-serializes it (invalidating held handles) and clutters "
            "the root timeline - cheap to switch now, costly later.")


def all_components(d):
    """Every component in the design (root included) as a flat list, falling back to just the root.
    ``allComponents`` is a property of the DESIGN - Component has no such attribute, and reading it
    there silently degrades this walk to root-only."""
    root = safe(lambda: d.rootComponent)
    if root is None:
        return []
    comps = safe(lambda: d.allComponents)
    if comps is None:
        return [root]
    n = safe(lambda: comps.count, 0)
    out = [safe(lambda i=i: comps.item(i)) for i in range(n)]
    out = [c for c in out if c is not None]
    return out or [root]


def broken_reference(occ):
    """(is_broken, detail) for ONE occurrence - the ONE unresolved-external-reference detector."""
    # MEASURED: ``occ.component`` RAISING is the only honest gate - every other candidate signal
    # (isReferencedComponent included) was measured LYING on a real broken reference. ``name`` still
    # reads; such a row is present in component.occurrences and ABSENT from childOccurrences.
    try:
        occ.component
    except Exception as exc:                    # noqa: BLE001 - the raise IS the signal
        return True, (str(exc) or type(exc).__name__)
    return False, None


# The walk that produced an occurrence census, published beside every count it feeds.
WALK_FAST = "allOccurrences"        # root.allOccurrences enumerated
WALK_RECURSED = "recursed"          # it raised; the census was rebuilt from component.occurrences
WALK_UNREADABLE = "unreadable"      # neither path enumerated - the census is UNKNOWN, not empty

_WALK_MAX_DEPTH = 24                # a guard on the recursion, not a modelling limit
_WALK_MAX_NODES = 20000


class OccurrenceWalk:
    """ONE design-wide occurrence census: ``occurrences`` (component READS - measurable, nameable),
    ``broken`` ({name, parent_path, detail} per unresolved reference, kept apart because
    fullPathName/isVisible/childOccurrences.count all raise on one), and HOW it was taken."""

    __slots__ = ("occurrences", "broken", "broken_occurrences", "method", "complete", "_census")

    def __init__(self, occurrences, broken, method, complete, broken_occurrences=None):
        self.occurrences = occurrences
        self.broken = broken
        # Pinned BEFORE any cap slices the row list: a cap bounds what is rendered, never what the
        # design was found to hold.
        self._census = len(occurrences) + len(broken)
        # The raw objects behind `broken` (which is pure wire data), in the same order - what a
        # resolver matching an unresolved occurrence by name reads.
        self.broken_occurrences = list(broken_occurrences or [])
        self.method = method
        self.complete = complete

    @property
    def readable(self):
        return self.method != WALK_UNREADABLE

    @property
    def total(self):
        """How many occurrences the design holds, unresolved included - or None when no census was
        taken. From an INCOMPLETE walk it is a lower bound, which ``complete`` is what says."""
        return None if not self.readable else self._census

    def names(self):
        """The unresolved occurrences' names, for a wire string that must NAME them."""
        return [b["name"] for b in self.broken]


def _broken_record(occ, parent_path, detail):
    """One unresolved-reference row - ``name`` is the only identity such an occurrence answers, and
    a row whose name will not read still ships, saying so."""
    name = safe(lambda: occ.name)
    return {"name": name if name else "(unreadable name)",
            "name_readable": name is not None,
            "parent_path": parent_path,
            "detail": detail}


def _recursed_walk(root):
    """(occurrences, broken, complete, broken_occurrences) for one component's subtree WITHOUT
    ``allOccurrences``, which an unresolved reference anywhere in that subtree makes raise
    (measured). complete is False on a node that would not enumerate or a depth/node cap."""
    # Two collections per node: assembly-context children (childOccurrences) carry the true
    # fullPathNames but silently DROP an unresolved child, while component.occurrences holds it.
    occs, broken, broken_occs = [], [], []
    state = {"complete": True, "n": 0}

    def record_broken(occ, parent_path, detail):
        broken.append(_broken_record(occ, parent_path, detail))
        broken_occs.append(occ)

    def enumerate_coll(coll):
        """(items, readable) for one collection - the COUNT through ``counted``, so a collection
        whose count raises is UNREADABLE, not empty (``iter_collection`` alone swallows that raise
        into a zero-length walk)."""
        if coll is None:
            return [], False
        n = counted(lambda: coll.count)
        if n is None:
            return [], False
        return list(iter_collection(coll)), True

    def scan_broken(comp, parent_path):
        """The unresolved children of one component-local collection."""
        children, readable = enumerate_coll(safe(lambda: comp.occurrences))
        if not readable:
            state["complete"] = False
            return
        for child in children:
            is_broken, detail = broken_reference(child)
            if is_broken:
                record_broken(child, parent_path, detail)

    def descend(occ, path, depth):
        if depth > _WALK_MAX_DEPTH or state["n"] >= _WALK_MAX_NODES:
            state["complete"] = False
            return
        comp = safe(lambda: occ.component)
        if comp is None:
            state["complete"] = False
            return
        scan_broken(comp, path)
        kids, readable = enumerate_coll(safe(lambda: occ.childOccurrences))
        if not readable:
            state["complete"] = False
            return
        walk_children(kids, path, depth)

    def walk_children(children, path, depth):
        for child in children:
            state["n"] += 1
            if state["n"] > _WALK_MAX_NODES:
                state["complete"] = False
                return
            # Measured: childOccurrences drops an unresolved row. One surfacing here is skipped -
            # the component-local scan is the ONE place they are collected.
            if broken_reference(child)[0]:
                continue
            occs.append(child)
            name = safe(lambda c=child: c.name) or "?"
            descend(child, f"{path}+{name}" if path else name, depth + 1)

    top, readable = enumerate_coll(safe(lambda: root.occurrences))
    if not readable:
        return [], [], False, []       # neither walk answered: the census is UNKNOWN, not empty
    # The root's own collection is BOTH the component-local and the assembly-context one, so the
    # unresolved scan runs over it directly rather than through descend().
    for child in top:
        is_broken, detail = broken_reference(child)
        if is_broken:
            record_broken(child, safe(lambda: root.name), detail)
    walk_children(top, "", 0)
    return occs, broken, state["complete"], broken_occs


def component_walk(comp, cap=None):
    """The ONE occurrence census over a COMPONENT's subtree, saying which walk answered. ``cap``
    bounds the returned rows for a caller that only renders; the counts stay whole."""
    # MEASURED: ``allOccurrences`` is the fast path and the only source of true assembly
    # fullPathNames, but the PROPERTY ACCESS raises (InternalValidationError : occ) on a subtree
    # holding an unresolved reference - `safe(read) or []` there publishes it as an empty assembly.
    if comp is None:
        return OccurrenceWalk([], [], WALK_UNREADABLE, False)
    flat = safe(lambda: list(comp.allOccurrences))
    if flat is not None:
        occs, broken, broken_occs = [], [], []
        for o in flat:
            # A None slot is an item the collection would not hand over, not an unresolved
            # reference: it stays in the census so the count matches the collection's own size.
            if o is None:
                occs.append(o)
                continue
            is_broken, detail = broken_reference(o)
            if is_broken:
                broken.append(_broken_record(o, None, detail))
                broken_occs.append(o)
            else:
                occs.append(o)
        walk = OccurrenceWalk(occs, broken, WALK_FAST, True, broken_occs)
    else:
        occs, broken, complete, broken_occs = _recursed_walk(comp)
        if not occs and not broken and not complete:
            return OccurrenceWalk([], [], WALK_UNREADABLE, False)
        walk = OccurrenceWalk(occs, broken, WALK_RECURSED, complete, broken_occs)
    if cap is not None:
        walk.occurrences = walk.occurrences[:cap]
    return walk


def occurrence_walk(d, cap=None):
    """The design-wide occurrence census: ``component_walk`` over the ROOT component - the only
    scope whose occurrence handles carry true assembly fullPathNames."""
    return component_walk(safe(lambda: d.rootComponent) if d else None, cap=cap)


def all_occurrences(d, cap=None):
    """Every USABLE occurrence in the design, in ASSEMBLY context, as a flat list - unresolved
    references OMITTED. A caller publishing a COUNT, a completeness claim or a health verdict calls
    ``occurrence_walk``: this list alone cannot say whether it is the whole design."""
    return occurrence_walk(d, cap=cap).occurrences


def occurrence_paths(d):
    """Every occurrence fullPathName in the design, as a set - the census a structural edit diffs.
    A path that will not read contributes '' rather than being dropped (a dropped row would make
    the diff report a phantom new path); unresolved references raise on fullPathName."""
    walk = occurrence_walk(d)
    paths = {(safe(lambda o=o: o.fullPathName) or "") for o in walk.occurrences}
    if walk.broken:
        paths.add("")
    return paths


def component_contains(outer, inner):
    """TRI-STATE cycle test a structural edit refuses on. A False is a POSITIVE claim ("this edit
    is legal"), so it needs the WHOLE subtree enumerated and every comparison answered; a caller
    REFUSES on None rather than reading it as "no cycle"."""
    # walk.broken is checked separately from walk.complete: a census that RECORDED an unresolved
    # reference still stamps complete=True, and a broken row's component raises, so what it holds
    # beneath it was never enumerated and cannot be ruled out as the cycle.
    self_match = same_component(outer, inner)
    if self_match is True:
        return True
    walk = component_walk(outer)
    if not walk.readable:
        return None
    unproven = self_match is None or not walk.complete or bool(walk.broken)
    for o in walk.occurrences:
        match = same_component(safe(lambda o=o: o.component), inner)
        if match is True:
            return True
        if match is None:
            unproven = True
    return None if unproven else False


def all_meshes(d):
    """Every MeshBody in the design as (component, mesh) pairs - the ONE design-wide mesh walk,
    reached through COMPONENTS, so a component with no occurrence anywhere is still walked."""
    out = []
    for comp in all_components(d):
        for m in iter_collection(safe(lambda c=comp: c.meshBodies)):
            out.append((comp, m))
    return out


def design_wide_counts(d):
    """(bodies, sketches) summed across every component - the ONE design-wide count every summary
    read shares. ``bRepBodies``/``sketches`` are per-COMPONENT, so a root-only read of a
    multi-part design under-reports (a sketches-in-sub-components doc reads sketches:0)."""
    bodies = sketches = 0
    for comp in all_components(d):
        bodies += safe(lambda c=comp: c.bRepBodies.count, 0) or 0
        sketches += safe(lambda c=comp: c.sketches.count, 0) or 0
    return bodies, sketches


def result_bodies(feature):
    """The bodies a parametric feature produced, as FRESH references read straight off the feature -
    the references a caller passed in can go invalid once the feature rebuilds. Empty when the
    feature or its bodies will not read."""
    fb = safe(lambda: feature.bodies)
    n = int(safe(lambda: fb.count, 0) or 0) if fb else 0
    out = []
    for i in range(n):
        b = safe(lambda i=i: fb.item(i))
        if b is not None:
            out.append(b)
    return out


def body_facts(bodies):
    """[{name, is_solid}] read LIVE per body - the projection a feature result reports its bodies
    with. is_solid is TRI-STATE through ``read_flag``, never a coerced False: a body whose flag will
    not read is not an open surface, and a plain all()/any() folds that unknown into a verdict."""
    return [{"name": safe(lambda b=b: b.name), "is_solid": read_flag(lambda b=b: b.isSolid)}
            for b in bodies]


def component_body_names(comp):
    """The names of the bodies `comp` holds right now, or None when the collection or any one name
    will not read - the BEFORE image a feature's result bodies are told new-vs-existing against."""
    bodies = safe(lambda: comp.bRepBodies)
    n = counted(lambda: bodies.count) if bodies is not None else None
    if n is None:
        return None
    names = [f["name"] for f in body_facts([safe(lambda i=i: bodies.item(i)) for i in range(n)])]
    return None if any(nm is None for nm in names) else names


JOIN_NEW_BODY_NOTE = (
    "operation='join' landed a NEW body ({name}) instead of growing one of the bodies present "
    "before this call; model_combine(join) merges them.")


def join_new_body_clause(op_key, before_names, result_names):
    """The clause for a join whose result body is NOT one the host component held before the call -
    '' for any other operation, for a before-image that did not read or held no body, and for a
    result body that already existed."""
    if op_key != "join" or not before_names or not result_names:
        return ""
    new = [n for n in result_names if n and n not in before_names]
    return JOIN_NEW_BODY_NOTE.format(name=new[0]) if new else ""


def most_recent_body(comp):
    """The most recently created body in a component, or None - the blank-input fallback for a tool
    that defaults to 'the body you just made'."""
    bodies = safe(lambda: comp.bRepBodies)
    n = safe(lambda: bodies.count, 0) if bodies else 0
    return bodies.item(n - 1) if n else None


def resolve_body_or_recent(body_ref, comp, raw, no_body_error):
    """(body, error) for a 'that body, or the most recent one' input - the ONE resolution every
    whole-body edit runs. A given value goes through the caller's own BodyRef kind; empty falls back
    to ``most_recent_body`` in ``comp``, and ``no_body_error`` is the caller's own empty sentence."""
    if raw in (None, "", []):
        body = most_recent_body(comp)
        return (body, None) if body else (None, no_body_error)
    return body_ref.resolve(raw)


def open_profile_from_sketch(comp, sketch, verb, no_curves_error=None):
    """(open_profile, error) from a sketch's unclosed curves via Component.createOpenProfile, so an
    open path can become a SURFACE. It wants an ObjectCollection of the individual curve entities,
    NOT the SketchCurves collection object (which raises "invalid input curves")."""
    curves = safe(lambda: sketch.sketchCurves)
    n = safe(lambda: curves.count, 0) if curves is not None else 0
    if not n:
        return None, (no_curves_error or "Sketch has no curves to build an open profile from.")
    coll = adsk.core.ObjectCollection.create()
    for i in range(n):
        c = safe(lambda i=i: curves.item(i))
        if c is not None:
            coll.add(c)
    try:
        prof = comp.createOpenProfile(coll, True)
    except Exception as e:
        return None, f"Could not build an open profile {verb}: {e}"
    if not prof:
        return None, "Could not build an open profile from the sketch's curves (createOpenProfile returned nothing)."
    return prof, None


def find_sketches_by_name(d, name):
    """Every (sketch, owning_component) carrying EXACTLY ``name``, one entry per component holding
    it - a LIST, because a sketch name is only component-locally unique. One hit resolves, several
    are REFUSED with the owning components, and the first hit is never taken."""
    # The walk is all_components and de-duplicates NOTHING: that collection lists each component
    # once, while any de-dup keyed on component identity MERGES two components sharing an
    # entityToken (measured), turning a name two sketches carry into a silent single hit.
    nm = (name or "").strip()
    if not nm:
        return []
    out = []
    for comp in all_components(d):
        sk = safe(lambda c=comp: c.sketches.itemByName(nm))
        if sk is not None:
            out.append((sk, comp))
    if out:
        return out
    # Only on an EMPTY result: all_components can degrade to [root], which would leave a sketch in
    # an activated sub-component unreachable. Firing on a non-empty result would re-ask components
    # already asked and report one component's sketch as several.
    seen = set()
    for comp in (target_component(d), safe(lambda: d.rootComponent)):
        if comp is None:
            continue
        sk = safe(lambda c=comp: c.sketches.itemByName(nm))
        if sk is None or id(sk) in seen:
            continue
        seen.add(id(sk))
        out.append((sk, comp))
    return out


# How many candidates a refusal NAMES before it counts the rest. The cap is DISCLOSED: a silently
# truncated list reads as the COMPLETE set.
_MAX_NAMED_CANDIDATES = 8


def named_with_remainder(items, cap=_MAX_NAMED_CANDIDATES):
    """``', '``-joined ``items``, capped, with any remainder COUNTED rather than dropped - the ONE
    place a capped wire list is rendered."""
    head = ", ".join(items[:cap])
    if len(items) > cap:
        head += f", ... (+{len(items) - cap} more not listed)"
    return head


# A find_geometry handle is an entityToken plus a locator, so it runs past 200 characters.
_ECHO_CHARS = 60


def short_ref(value):
    """A caller's value as a message echoes it: the head, a cut marked with the full length."""
    s = "" if value is None else str(value)
    return s if len(s) <= _ECHO_CHARS else s[:_ECHO_CHARS] + f"... ({len(s)} chars)"


def told_apart(rows):
    """One rendered label per ``(name, discriminator)`` row - the ONE place a listing replaces a
    REPEATED name with its discriminator. A unique name, or an empty discriminator, keeps it."""
    counts = {}
    for name, _disc in rows:
        counts[name] = counts.get(name, 0) + 1
    return [disc if (counts[name] > 1 and disc) else name for name, disc in rows]


# The fallback way forward, for a caller offering none of its own - the weakest one, since a shared
# sketch name usually comes from two REFERENCED documents and renaming means editing another file.
_RENAME_REMEDY = ("Rename one so the name resolves to a single sketch, then retry (sketch_get lists "
                  "the sketches).")


def find_sketch(d, name, remedy=None):
    """(sketch, error_or_None) for ONE sketch by name design-wide - a name SEVERAL sketches carry
    is refused naming each owning component; a name NO sketch carries is (None, None), leaving the
    caller its own not-found error. ``remedy`` replaces the refusal's closing sentence."""
    hits = find_sketches_by_name(d, name)
    if len(hits) == 1:
        return hits[0][0], None
    if not hits:
        return None, None
    nm = (name or "").strip()
    named, placements = _sketch_owner_rows(d, nm, hits)
    # The parenthetical enumerates the HITS, one row each. Placements get their own sentence: a
    # component of a shared name is routinely placed more than once, and four addresses inside
    # "2 sketches are named ..." would read as four sketches.
    rows = named_with_remainder([f"'{nm}' in {owner}" for owner in named])
    where = f" ({rows})" if rows else ""
    tail = ""
    if placements:
        one = len(placements) == 1
        tail = (f" Owners sharing a name are not told apart by it - {len(placements)} occurrence"
                f"{'' if one else 's'} place{'s' if one else ''} one holding this sketch: "
                f"{named_with_remainder(placements)}.")
    return None, (f"{len(hits)} sketches are named '{nm}'{where} - sketch names are only unique "
                  f"within a component.{tail} " + (remedy or _RENAME_REMEDY))


def _sketch_owner_rows(d, nm, hits):
    """(owner-NAME rows, PLACEMENT rows) - kept apart because a name row stands for one HIT and a
    placement row for one OCCURRENCE. An owner name several hits share becomes one placement row
    per occurrence holding a sketch of ``nm``, padded with bare name rows."""
    owners = [safe(lambda c=c: c.name) or "(unnamed component)" for _sk, c in hits]
    counts = {o: owners.count(o) for o in owners}
    rows, expanded = [], set()
    for owner in owners:
        if counts[owner] == 1:
            rows.append((owner, None))
            continue
        if owner in expanded:
            continue
        expanded.add(owner)
        paths = placements_holding_sketch(d, owner, nm)
        rows.extend([(owner, p) for p in paths])
        rows.extend([(owner, None)] * max(0, counts[owner] - len(paths)))
    labels = told_apart(rows)
    named = [label for (owner, _d), label in zip(rows, labels) if label == owner]
    placements = [label for (owner, _d), label in zip(rows, labels) if label != owner]
    return named, placements


def _component_is_named(comp, name):
    """True when ``comp``'s OWN name IS ``name`` - case-insensitive and EXACT, never a substring:
    a 'Frame' scope must not select 'Frame Bracket'."""
    return (safe(lambda: comp.name) or "").strip().lower() == (name or "").strip().lower()


def components_in_scope(d, name):
    """(components, error_or_None) for a component SCOPE - a BLANK name selects every component.
    The refusal names the offending value plus the component names that DO exist."""
    comps = all_components(d)
    want = (name or "").strip()
    if not want:
        return comps, None
    scoped = [c for c in comps if _component_is_named(c, want)]
    if len(scoped) > 1:
        # Case-insensitive matching WIDENS the hit list and must not manufacture an ambiguity: one
        # hit matching the SPELLING asked for is the answer, so 'Beta' and 'BETA' each address
        # themselves. TWO hits spelled exactly as asked stay ambiguous.
        cased = [c for c in scoped if (safe(lambda c=c: c.name) or "") == want]
        if len(cased) == 1:
            scoped = cased
    if scoped:
        return scoped, None
    known = ", ".join(n for n in (safe(lambda c=c: c.name) for c in comps) if n)
    return None, f"No component named '{want}'. Components: {known or '(none)'}."


def spelled_as_read(comps, want):
    """The clause naming how the matched components are ACTUALLY spelled, or '' when every one is
    spelled as asked - the scope match is case-insensitive, so a design holding 'Beta' and 'BETA'
    answers 'beta' with two components and NEITHER is named 'beta'. De-duplicated."""
    names = []
    for c in comps:
        n = safe(lambda c=c: c.name)
        if n is not None and n not in names:
            names.append(n)
    if not names or names == [want]:
        return ""
    quoted = ["'" + n + "'" for n in names]
    return f" (named {named_with_remainder(quoted)})"


def component_placements(d):
    """[(occurrence fullPathName, that occurrence's component)] for every occurrence, both halves
    read off the SAME occurrence - so nothing here asks whether two components are the same one
    (two distinct ones can read byte-identical tokens). Either half unreadable omits the row."""
    out = []
    for occ in all_occurrences(d):
        comp = safe(lambda o=occ: o.component)
        path = safe(lambda o=occ: o.fullPathName)
        if comp is not None and path:
            out.append((path, comp))
    return out


def placement_paths_named(d, name):
    """The fullPathName of every occurrence placing a component whose OWN name matches ``name`` -
    "which placements answer to this name", not "which hold this exact component"."""
    return [p for p, c in component_placements(d) if _component_is_named(c, name)]


def placements_holding_sketch(d, owner_name, sketch_name):
    """The fullPathName of every occurrence whose OWN component answers to ``owner_name`` AND holds
    a sketch named ``sketch_name`` - each path independently true, and NOT "this hit's placement",
    which nothing readable can pair while two components share one token."""
    want = (sketch_name or "").strip()
    if not want:
        return []
    return [p for p, c in component_placements(d)
            if _component_is_named(c, owner_name)
            and safe(lambda c=c: c.sketches.itemByName(want)) is not None]


def find_sketch_in(d, name, comp, label, input_name="component"):
    """(sketch, error_or_None) for ONE sketch inside ONE already-resolved component, asking that
    component's OWN collection rather than filtering design-wide hits (two distinct components can
    read one token). ``label`` is the scope as spelled; ``input_name`` is the retry input."""
    nm = (name or "").strip()
    found = safe(lambda: comp.sketches.itemByName(nm)) if nm else None
    if found is not None:
        return found, None
    hits = find_sketches_by_name(d, nm)
    # Name the component AS READ: 'label' may be a different casing or an occurrence PATH, neither
    # of which is a name any component carries. The scope is echoed when it differs.
    read_name = safe(lambda: comp.name)
    shown = f"'{read_name}'" if read_name else "the scoped component"
    via = "" if (read_name or "") == label else f" (scope '{label}')"
    if hits:
        owners = named_with_remainder([f"'{safe(lambda c=c: c.name) or '(unnamed component)'}'"
                                       for _sk, c in hits])
        return None, (f"Component {shown}{via} holds no sketch named '{nm}' - that name is in "
                      f"{owners}. Retry with one of those as '{input_name}'.")
    held = [n for n in (safe(lambda s=s: s.name)
                        for s in iter_collection(safe(lambda: comp.sketches))) if n]
    return None, (f"Component {shown}{via} holds no sketch named '{nm}'. Component {shown} holds: "
                  + (named_with_remainder(held) or "(no sketches)") + ".")


def resolve_sketch(d, name):
    """The live Sketch a name addresses design-wide, or None both when NO sketch carries it and
    when SEVERAL do. A caller that can surface WHY calls ``find_sketch``."""
    return find_sketch(d, name)[0]


def find_or_recent_sketch(d, name, remedy=None):
    """(sketch-or-None, the stripped name or None when blank, ambiguity_error-or-None): a NAME
    resolves design-wide, an EMPTY name means the most recent sketch in the ACTIVE component, and
    the third value separates a name NO sketch carries from one SEVERAL carry."""
    nm = (name or "").strip()
    if nm:
        sk, ambiguous = find_sketch(d, nm, remedy)
        return sk, nm, ambiguous
    coll = safe(lambda: target_component(d).sketches)
    n = safe(lambda: coll.count, 0) if coll is not None else 0
    return (coll.item(n - 1) if n else None), None, None


def resolve_or_recent_sketch(d, name):
    """(sketch-or-None, the stripped name or None when blank) - the same name-or-default contract
    with the ambiguity refusal DROPPED, for a caller with nowhere to put it."""
    sketch, requested, _ambiguous = find_or_recent_sketch(d, name)
    return sketch, requested


def all_sketch_names(d):
    """Every sketch name across the design, for an 'Available: ...' message - a name SEVERAL carry
    rendered QUALIFIED by its owning component ("Plate (Alpha)"), through ``told_apart``."""
    pairs = []
    for comp in all_components(d):
        coll = safe(lambda c=comp: c.sketches)
        owner = safe(lambda c=comp: c.name)
        for i in range(safe(lambda: coll.count, 0) if coll else 0):
            nm = safe(lambda i=i, cl=coll: cl.item(i).name)
            if nm:
                pairs.append((nm, owner))
    return told_apart([(nm, f"{nm} ({owner})" if owner else None) for nm, owner in pairs])


# ── terse: drop default-valued fields from a repeated record ────────────────

def terse(rec: dict, noise: dict) -> dict:
    """A copy of ``rec`` with any key whose value equals its default in ``noise`` removed, so a
    uniform list shows only the rows that differ ({"is_suppressed": False, "health": "healthy"})."""
    return {k: v for k, v in rec.items() if not (k in noise and v == noise[k])}


def timeline_marker(design):
    """(marker_position, count) for the parametric timeline, or (None, None) in direct mode - a
    marker below the count means the features after it are ROLLED BACK, not in the current model."""
    tl = safe(lambda: design.timeline)
    if tl is None:
        return None, None
    return safe(lambda: tl.markerPosition), (safe(lambda: tl.count, 0) or 0)


def set_verified(obj, prop, value, label, owner_name):
    """Set `prop` on a FeatureInput and CONFIRM it took - an error string, or '' on success.
    `value` None means the enum member was unavailable, which is reported rather than set."""
    # LIVE-VERIFIED: a SWIG proxy ACCEPTS an assignment to a name it does not define - the value
    # lands on a dead Python attribute while the object keeps its API default - so a misspelled
    # property cannot raise. Reading the value back is the only thing that catches it.
    if value is None:
        return f"'{label}' is not available on this Fusion version."
    try:
        setattr(obj, prop, value)
    except Exception as e:
        return f"Could not set {label}: {e}"
    if safe(lambda: getattr(obj, prop)) != value:
        return (f"Setting {label} did not take - {owner_name}.{prop} reads back unchanged, so the "
                "operation would run on its default settings.")
    return ""


def apply_rename(entity, new_name):
    """(final_name, warning_or_None) - the create succeeded, so a declined rename is a DISCLOSURE,
    not an error. The platform can decline silently or DEDUPE ('Foo' -> 'Foo(1)'), and writing the
    name an entity ALREADY reads dedupes it against ITSELF, so that write is skipped."""
    want = (new_name or "").strip()
    if not want:
        return safe(lambda: entity.name), None
    if safe(lambda: entity.name) == want:
        return want, None
    try:
        entity.name = want
    except Exception as e:
        got = safe(lambda: entity.name)
        return got, f"created, but the rename to '{want}' failed: {e} (the name is '{got}')."
    got = safe(lambda: entity.name)
    if got != want:
        return got, f"created, but the requested name '{want}' did not take - it is named '{got}'."
    return got, None


def cancel_input(inp, what):
    """Abort an OPEN feature-input transaction: "" when there was nothing to cancel or the cancel
    took, else a sentence to APPEND to the error the caller is returning (`what` names it)."""
    # A createInput that partial-computes (trimFeatures, boundaryFillFeatures) STARTS a transaction
    # only add() or cancel() ends; the API's doc says leaving it open risks undo problems and a
    # crash. A false from cancel() leaves Fusion holding the compute, so it is surfaced.
    if inp is None:
        return ""
    if safe(lambda: inp.cancel()):
        return ""
    return (f" The open {what} transaction could NOT be cancelled - Fusion may be left mid-compute; "
            "undo in Fusion before continuing.")


# ── the DIRECT-mode no-feature shape (a Features.*.add() that returns nothing) ──────────────

# MEASURED in a direct-mode design: six Features.*.add() classes (combine, move, splitBody, scale,
# offsetFaces, deleteFace) returned None WHILE the edit landed and eleven others returned real
# features, so the None is PER-CLASS - hence a MODE gate here, not a class table.
DIRECT_FEATURE_NOTE = ("This design is in DIRECT mode, where this operation creates no timeline "
                       "feature object to name - what is reported here is read back off the model.")


def direct_feature_absence(design, feature) -> bool:
    """True when a falsy Features.*.add() return is the DIRECT-mode shape rather than a failure -
    it never says anything LANDED (a direct combine of two non-touching bodies returned None with
    body count and volume unmoved), so the caller confirms its effect off the MODEL."""
    # _inputs imports _common, so the ONE mode reader is bound at call time, not at import.
    from . import _inputs
    return not feature and _inputs.current_design_type(design) == _inputs.MODE_DIRECT


def null_feature_note(design, feature, base_feature_name, what) -> str:
    """The sentence a payload appends when a write's add() came back without a feature object - the
    DIRECT-mode note, else the base-feature edit scope that is the reason instead."""
    if direct_feature_absence(design, feature):
        return DIRECT_FEATURE_NOTE
    if base_feature_name:
        return (f"'feature' is null: this {what} ran inside the base-feature edit scope "
                f"'{base_feature_name}' that a parametric mesh write requires, and the add returned "
                "no feature there - the result is read back off the model.")
    return (f"'feature' is null and no base-feature scope was opened - the {what} result is read "
            "back off the model.")


def census_host(entity, fallback):
    """The component a before/after census must be counted on: the target's OWN parentComponent,
    else `fallback`. Resolve it ONCE, BEFORE the mutation, and count the SAME object twice."""
    # MEASURED: a direct splitBodyFeatures.add put the new piece in the TARGET's parentComponent
    # (SplitHost 1 -> 2) while the ACTIVE component held still (root 3 -> 3), so a census scoped to
    # target_component(design) is blind to it.
    return safe(lambda: entity.parentComponent) or fallback


def body_count(host):
    """How many BRep bodies `host` holds, or None if the collection cannot be read."""
    return safe(lambda: host.bRepBodies.count)


def failed_effect_remedy(design, feature) -> str:
    """The remediation sentence a wrong-effect error ends with - the timeline entry to delete, or
    Fusion's own undo where direct mode leaves neither a feature nor a timeline to name."""
    if direct_feature_absence(design, feature):
        return ("This design is in DIRECT mode: there is no timeline feature to remove, so whatever "
                "did change is already in the model - undo in Fusion, or re-run with corrected "
                "inputs.")
    return "The feature remains in the timeline; remove it with design_delete_feature."


def no_feature_error(design, what, hint="") -> str:
    """The refusal for a falsy add() whose ONLY evidence was the feature object - a plain failure
    in a parametric design; in a direct one, a no-op it cannot tell from a landed edit."""
    text = f"{what} returned no feature."
    if hint:
        text = f"{text} {hint}"
    if direct_feature_absence(design, None):
        text += (" This design is in DIRECT mode, where some feature classes return no feature "
                 "object even though the edit LANDED, so this result cannot tell a no-op from a "
                 "silent success. Read the model back with design_get before retrying - a retry "
                 "would repeat an edit that may already be in the model.")
    return text


class _HealthName(str):
    """One unhealthy timeline item's name that serializes as the name while ``==`` compares the
    row's ENTITY token - timeline names are NOT unique across components (measured), so a bare-name
    diff is blind to a namesake's damage. ``__hash__`` stays the NAME's hash."""

    def __new__(cls, name, token=None):
        obj = str.__new__(cls, name)
        obj.token = token or None
        return obj

    def __eq__(self, other):
        mine, theirs = self.token, getattr(other, "token", None)
        if mine and theirs:
            return mine == theirs
        return str.__eq__(self, other)

    def __ne__(self, other):
        same = self.__eq__(other)
        return same if same is NotImplemented else not same

    __hash__ = str.__hash__


def timeline_health(design, limit=None):
    """(error_names, warning_names, total) over the parametric timeline by healthState (2=error,
    1=warning) - the shared before/after guard for edits that break downstream features; a direct
    design yields empty lists. Each name is a ``_HealthName``, compared on the row's entity token."""
    # ``limit`` bounds the walk to the first n items, keeping a CREATE's guard off the entry it just
    # added: a freshly added assembly constraint's own healthState RAISES '1 : Unknown exception'
    # (measured), and that caught error inside Python.Run rolled the whole transaction back.
    errors, warnings, total = [], [], 0
    tl = safe(lambda: design.timeline)
    if tl is None:
        return errors, warnings, total
    count = safe(lambda: tl.count, 0) or 0
    if limit is not None:
        count = min(count, max(int(limit), 0))
    for i in range(count):
        it = tl.item(i)
        total += 1
        hs = safe(lambda it=it: it.healthState)
        if hs not in (1, 2):
            continue
        # A TimelineObject carries no entityToken of its own; the ENTITY it stands for does, and
        # that token re-reads identical within a session (measured). TimelineObject.entity is None
        # for a group row and for a feature class with no public-API form, which keys on its name.
        label = _HealthName(safe(lambda it=it: it.name) or f"#{i}",
                            safe(lambda it=it: it.entity.entityToken))
        (errors if hs == 2 else warnings).append(label)
    return errors, warnings, total


# A before/after volume difference (cm3) smaller than this is NO CHANGE - the ONE band every
# material-changing feature judges "the API reported success but nothing moved" against.
NO_VOLUME_CHANGE_CM3 = 1e-9


# The band a landed extent may differ from the requested one by and still be the same length: both
# are internal cm, so it absorbs their float representation, not a real depth difference.
EXTENT_MATCH_TOL_CM = 1e-6


def landed_extent_cm(feature):
    """The depth an extrude-family feature REPORTS for its first side, in internal cm, or None."""
    # MEASURED: extentOne's .distance ModelParameter reads cm and keeps the requested SIGN (-15 mm
    # reads -1.5); a symmetric extent reads the per-SIDE number asked for and a taper does not
    # perturb it; a CUT reads the REQUESTED distance, not one clipped to the material consumed.
    got = safe(lambda: feature.extentOne.distance.value)
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        return float(got)
    return None


def landed_extent2_cm(feature):
    """The depth an extrude-family feature REPORTS for its SECOND side, in internal cm, or None - a
    one-sided feature has no extentTwo and answers None rather than raising."""
    # MEASURED on POSITIVE requests: no swap between the two sides, each .distance reporting the
    # magnitude that side asked for. A NEGATIVE two-sided request is unmeasured, so a caller
    # comparing against these gates on the requested sign itself.
    got = safe(lambda: feature.extentTwo.distance.value)
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        return float(got)
    return None


# ── unit scaling (Fusion's internal length unit is cm) ──────────────────────

UNIT_TO_CM = {"mm": 0.1, "cm": 1.0, "in": 2.54, "inch": 2.54}
CM_TO_UNIT = {u: 1.0 / f for u, f in UNIT_TO_CM.items()}


def scale(units: str):
    """cm-per-unit factor for ``units`` (mm/cm/in), or None if the unit is unknown."""
    return UNIT_TO_CM.get((units or "mm").strip().lower())


def ptxyz(p, f):
    """{x, y, z} for a Point3D ``p``, scaled by ``f`` and rounded to 6dp - or None when ANY of the
    three will not read, since 0 is an answer here ("on the origin") and a point is one answer."""
    if p is None:
        return None
    x, y, z = safe(lambda: p.x), safe(lambda: p.y), safe(lambda: p.z)
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
               for c in (x, y, z)):
        return None
    scaled = (x * f, y * f, z * f)
    if not all(math.isfinite(c) for c in scaled):
        return None
    return {"x": round(scaled[0], 6), "y": round(scaled[1], 6), "z": round(scaled[2], 6)}


# ── measurement (the one measureMinimumDistance core both measure tools share) ────────────────────

def min_distance(entity_a, entity_b):
    """(MeasureResults, None) or (None, error_result) from measureMinimumDistance - a READ, so a
    failure is surfaced. ``.value`` and ``.positionOne``/``.positionTwo`` are all cm."""
    mgr = safe(lambda: app.measureManager)
    if not mgr:
        return None, error("MeasureManager unavailable.")
    try:
        mr = mgr.measureMinimumDistance(entity_a, entity_b)
    except Exception as e:
        return None, error(f"Distance measurement failed: {e}. (Target a specific body/face - an "
                           "occurrence whose bodies are proxies can be rejected; a find_geometry "
                           "face/body handle is the precise input.)")
    if not mr:
        return None, error("measureMinimumDistance returned nothing for these two targets.")
    return mr, None


# ── sketch entity / feature-operation resolution ────────────────────────────

def target_sketch(comp, name):
    """The sketch named ``name`` in ``comp``, or its most recently created sketch when ``name`` is
    empty. Returns (sketch-or-None, the requested name)."""
    coll = safe(lambda: comp.sketches)
    nm = (name or "").strip()
    if coll is None:
        return None, nm
    if nm:
        return safe(lambda: coll.itemByName(nm)), nm
    n = safe(lambda: coll.count, 0)
    return (coll.item(n - 1) if n else None), nm


# The '<type>:<index>' ref kinds resolve_entity_ref addresses. spline = fitted, cv_spline =
# control-point, fixed_spline = fixed/NURBS-referenced: three distinct SketchCurves collections,
# each with its own creation-order index space.
ENTITY_REF_KINDS = ("line", "arc", "circle", "ellipse", "elliptical_arc", "conic", "point",
                    "spline", "cv_spline", "fixed_spline")

# kind -> the SketchCurves sub-collection it indexes ('point' lives on the sketch itself).
_ENTITY_REF_CURVE_ATTR = {
    "line": "sketchLines",
    "arc": "sketchArcs",
    "circle": "sketchCircles",
    "ellipse": "sketchEllipses",
    "elliptical_arc": "sketchEllipticalArcs",
    "conic": "sketchConicCurves",
    "spline": "sketchFittedSplines",
    "cv_spline": "sketchControlPointSplines",
    "fixed_spline": "sketchFixedSplines",
}


def entity_collection(sketch, kind):
    """The raw collection a '<type>:<index>' ref of this ``kind`` indexes, or None - the one place
    that knows which SketchCurves sub-collection (or sketchPoints) each ref token names."""
    if kind == "point":
        return safe(lambda: sketch.sketchPoints)
    attr = _ENTITY_REF_CURVE_ATTR.get(kind)
    if attr is None:
        return None
    curves = safe(lambda: sketch.sketchCurves)
    if curves is None:
        return None
    return safe(lambda: getattr(curves, attr))


def resolve_entity_ref(sketch, ref):
    """A sketch entity from a '<type>:<index>' ref, indexing that curve/point collection in creation
    order. type is one of ENTITY_REF_KINDS. Returns the entity, or None."""
    s = (ref or "").strip().lower()
    if ":" not in s:
        return None
    kind, _, idx = s.rpartition(":")
    try:
        i = int(idx)
    except Exception:
        return None
    if kind not in ENTITY_REF_KINDS:
        return None
    coll = entity_collection(sketch, kind)
    if coll is None:
        return None
    if i < 0 or i >= safe(lambda: coll.count, 0):
        return None
    return safe(lambda: coll.item(i))


def resolve_entity_refs(sketch, raw, field="entities"):
    """(entities, refs, error) for a COMMA-SEPARATED '<type>:<index>' list, in the order given -
    ``refs`` is kept even on failure, and the error names the FIRST ref that did not resolve."""
    refs = [r.strip() for r in (raw or "").split(",") if r.strip()]
    ents = []
    for ref in refs:
        ent = resolve_entity_ref(sketch, ref)
        if ent is None:
            return None, refs, (f"Could not resolve '{ref}' in '{field}' (use '<type>:<index>', "
                                f"type = {'/'.join(ENTITY_REF_KINDS)}) - ids come from "
                                "sketch_get(include_entities=true).")
        ents.append(ent)
    return ents, refs, None


# ── entity-anchored POSITION references ('<type>:<index>:<anchor>') ──────────

# The optional THIRD colon-segment of a sketch entity ref names WHICH point is meant. Pinning on
# the owning entity's own point beats a bare 'point:N', which mis-attaches when two entities share
# coordinates and each mints its own point index.
SKETCH_ANCHORS = ("start", "end", "mid", "midpoint", "center")


def parse_anchor_ref(ref):
    """Split '<type>:<index>[:<anchor>]' -> (entity_ref, anchor_or_None, error). An unrecognized
    third segment errors naming the valid anchors, never mis-resolving to the bare entity."""
    s = (ref or "").strip()
    parts = s.split(":")
    if len(parts) <= 2:
        return s, None, None
    anchor = parts[-1].strip().lower()
    if anchor not in SKETCH_ANCHORS:
        return None, None, (f"'{ref}': unknown anchor '{parts[-1]}'. Valid: "
                            f"{', '.join(SKETCH_ANCHORS)} (e.g. 'line:0:end', 'circle:2:center').")
    return ":".join(parts[:-1]), anchor, None


def _midpoint_sketch_point(sketch, line):
    """(point, None) or (None, error) for a SketchPoint welded to a line's MIDPOINT - created at the
    geometric midpoint, then constrained with addMidPoint so it tracks the line."""
    sp = safe(lambda: line.startSketchPoint.geometry)
    ep = safe(lambda: line.endSketchPoint.geometry)
    if sp is None or ep is None:
        return None, "anchor 'mid' needs a line with two endpoints."
    mid = adsk.core.Point3D.create((sp.x + ep.x) / 2.0, (sp.y + ep.y) / 2.0,
                                   ((safe(lambda: sp.z, 0.0) or 0.0)
                                    + (safe(lambda: ep.z, 0.0) or 0.0)) / 2.0)
    pt = sketch.sketchPoints.add(mid)               # MUTATION - let a failure raise into the handler
    if pt is None:
        return None, "could not create a midpoint anchor point."
    # The weld is what makes 'mid' PARAMETRIC: an unwelded point sits at today's midpoint and
    # silently stops tracking on the next edit, so a failed weld rolls the point back.
    welded = safe(lambda: sketch.geometricConstraints.addMidPoint(pt, line), _UNREADABLE)
    if welded is _UNREADABLE or welded is None:
        rolled = bool(safe(lambda: pt.deleteMe()))
        return None, ("the midpoint weld (addMidPoint) failed, so the anchor would NOT track the "
                      "line - " + ("the anchor point was rolled back." if rolled else
                                   "and the anchor point could not be removed; delete it in the "
                                   "sketch.") + " Anchor to 'start'/'end' instead.")
    return pt, None


def anchor_point(sketch, entity, anchor):
    """(point, error) for the SketchPoint an anchored ref names - mid/midpoint CREATES a welded
    point, where every other anchor only reads one."""
    start = safe(lambda: entity.startSketchPoint)
    end = safe(lambda: entity.endSketchPoint)
    center = safe(lambda: entity.centerSketchPoint)
    if anchor == "start":
        return (start, None) if start is not None else (None, "anchor 'start' needs a line or arc.")
    if anchor == "end":
        return (end, None) if end is not None else (None, "anchor 'end' needs a line or arc.")
    if anchor == "center":
        return (center, None) if center is not None else (None, "anchor 'center' needs a circle or arc.")
    # mid / midpoint - a line only (a well-defined addMidPoint target; a circle/arc uses 'center')
    if center is not None or start is None or end is None:
        return None, "anchor 'mid' applies to a LINE (line:N:mid); for a circle/arc use 'center'."
    return _midpoint_sketch_point(sketch, entity)


# Operation name -> adsk.fusion.FeatureOperations attribute (extrude/revolve/sweep/loft-style features).
OPERATIONS = {
    "new": "NewBodyFeatureOperation",
    "new_body": "NewBodyFeatureOperation",
    "join": "JoinFeatureOperation",
    "cut": "CutFeatureOperation",
    "intersect": "IntersectFeatureOperation",
    "new_component": "NewComponentFeatureOperation",
}


# ── the ONE path resolver (sweep / pipe / path pattern / on-path datum) ──────

def build_path(comp, path_raw):
    """(path, label, error): 'sketch:<name>' chains that sketch's connected curves, ONE edge handle
    chains from that seed across TANGENT connections (a sharp corner stops the chain, so the built
    Path's count is the truth), and a JSON list of handles is used exactly. Requesting chaining is
    not getting it, so the label reports that BUILT count."""
    # _inputs imports _common, so the edge-handle kind is bound at call time rather than at import.
    from . import _inputs
    if isinstance(path_raw, str) and path_raw.strip().lower().startswith("sketch:"):
        nm = path_raw.split(":", 1)[1].strip()
        sk, _ = target_sketch(comp, nm)
        if not sk:
            return None, None, f"No sketch named '{nm}' for the path. Use sketch_get or sketch_create."
        curves = safe(lambda: sk.sketchCurves)
        cn = safe(lambda: curves.count, 0) if curves else 0
        if not cn:
            return None, None, f"Path sketch '{nm}' has no curves to build a path from."
        seed = safe(lambda: curves.item(0))
        try:
            p = comp.features.createPath(seed, True) # isChain=True: chain the connected curves
        except Exception as e:
            return None, None, f"Could not build a path from sketch '{nm}': {e}"
        if not p:
            return None, None, f"createPath returned nothing for sketch '{nm}'."
        return p, f"sketch:{nm}", None

    # A single handle is kept whole - a composite handle carries commas in its locator, so it must
    # NOT be comma-split; several must arrive as a JSON list.
    if path_raw in (None, "", []):
        return None, None, ("'path' is required: a find_geometry edge 'handle' (or a JSON list of "
                            "them), or 'sketch:<name>' for a path sketch.")
    handles = [path_raw] if isinstance(path_raw, str) else list(path_raw)
    edges, err = _inputs.GeometryHandleList("path", require="edge").resolve(handles)
    if err:
        return None, None, err
    if len(edges) == 1:
        try:
            p = comp.features.createPath(edges[0], True) # request chaining from the seed edge
        except Exception as e:
            return None, None, f"Could not build a path from the edge: {e}"
    else:
        coll = adsk.core.ObjectCollection.create()
        for e in edges:
            coll.add(e)
        try:
            # Multiple edges: use them exactly (noChainedCurves); they must connect into one path.
            p = adsk.fusion.Path.create(coll, adsk.fusion.ChainedCurveOptions.noChainedCurves)
        except Exception as e:
            return None, None, f"Could not build a path from the {len(edges)} edges: {e}"
    if not p:
        return None, None, "Path build returned nothing (the edges may not connect into one path)."
    # MEASURED behind the tangent rule: an arc seed between two tangent lines built a 3-entity
    # path, a tangent-continuous closed loop chained all 8 of its edges from one seed, and a sharp
    # corner stopped it - open vs closed does not decide it.
    built = safe(lambda: int(p.count), 0) or 0
    how = ("from 1 seed handle" if len(edges) == 1
           else f"from {len(edges)} handles, used exactly")
    return p, (f"{built} edge(s) {how}" if built else f"{how}; edge count unreadable"), None


def iter_collection(coll):
    """Yield each item of a Fusion count/item(i) collection - the measured live protocol - and
    nothing when the collection is absent."""
    for i in range(safe(lambda: coll.count, 0) or 0):
        it = safe(lambda i=i: coll.item(i))
        if it is not None:
            yield it
