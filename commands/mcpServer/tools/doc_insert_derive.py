# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: derive another saved document's design (whole, or a named subset) into a
component of the active design - a one-way linked COPY that updates FROM the source while edits here
never travel back. Requires a PARAMETRIC destination design and an ALREADY-OPEN source: Fusion opens
documents asynchronously, so createInput returns None for a source that is not open yet."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _outputs
from ._data_common import _resolve_data_file

app = adsk.core.Application.get()

_INTO_COMPONENT = _inputs.OccurrenceRef("into_component",
        description="Default: the root component.")

# Insert > Derive (Component.features.deriveFeatures) has no Direct-modeling equivalent - confirmed
# against the live API surface (deriveFeatures is a parametric feature collection).
_MODE_GUARD = _inputs.ModeGuard(
    _inputs.MODE_PARAMETRIC,
    why="Insert > Derive (Component.features.deriveFeatures) has no Direct-modeling equivalent.",
    fix_hint="Switch to parametric mode first (design_set_mode).")

_ERROR_HEALTH = 2  # adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState (see _common.timeline_health)

_UNREAD = object()      # an activeOccurrence read that RAISED - distinct from one that reads None


def _as_names(raw):
    """A clean list of names from a JSON array or a comma-separated string; [] when empty."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def _names(values):
    """Comma-joined available-names list for a 'not found' message, or '(none)'."""
    return ", ".join(n for n in values if n) or "(none)"


def _find_open_document(data_file):
    """An already-open Document referencing the same DataFile LINEAGE, or None. Compared by
    DataFile.id (the lineage URN, version-independent) - Fusion never opens one lineage twice."""
    target_id = safe(lambda: data_file.id)
    if not target_id:
        return None
    for d in _common.iter_collection(safe(lambda: app.documents)):
        df = safe(lambda d=d: d.dataFile)
        if df is not None and safe(lambda df=df: df.id) == target_id:
            return d
    return None


# ── source-entity resolution (names in the SOURCE design - a CLOSED doc, so no active-design kind
# applies; resolve by case-insensitive EXACT match and REFUSE an ambiguous name per the constitution) ──

def _source_components(source_design):
    """Every component in the SOURCE design (root + sub-components), as a list."""
    return list(_common.iter_collection(safe(lambda: source_design.allComponents)))


def _owned_bodies(comp):
    """Every body (BRep + mesh) directly owned by a source component."""
    out = []
    for coll_name in ("bRepBodies", "meshBodies"):
        out.extend(_common.iter_collection(safe(lambda: getattr(comp, coll_name, None))))
    return out


def _resolve_source_components(source_design, names):
    """(components, error). Each name -> the source Component by case-INSENSITIVE EXACT match
    (the find_setup idiom - a source component name is scope-unique). An AMBIGUOUS name (2+ distinct
    components share it) is REFUSED, never first-match; a miss lists the available component names."""
    comps = _source_components(source_design)
    available = [safe(lambda c=c: c.name) for c in comps]
    out = []
    for want in names:
        w = want.strip().lower()
        matches = [c for c in comps if (safe(lambda c=c: c.name) or "").lower() == w]
        if len(matches) > 1:
            return None, (f"Source component name '{want}' is ambiguous - {len(matches)} distinct "
                          "components share it; refusing rather than deriving the wrong one. "
                          f"Available: {_names(available)}.")
        if not matches:
            return None, (f"Source component '{want}' not found in the source design. "
                          f"Available: {_names(available)}.")
        out.append(matches[0])
    return out, None


def _resolve_source_bodies(source_design, specs):
    """(bodies, error). Each spec is a body NAME (resolved design-wide in the source) or
    'Component/Body' (scoped to that component's bodies). A NAME that matches bodies in 2+ components
    is REFUSED (names the holders, asks to qualify); a miss is named. Bodies are a legal sourceEntity
    (BRepBody/MeshBody), live-verified against DeriveFeatureInput.sourceEntities."""
    comps = _source_components(source_design)
    out = []
    for spec in specs:
        scope, sep, body_name = spec.rpartition("/")
        body_name = body_name.strip()
        scope = scope.strip()
        search = comps
        if sep and scope:
            search = [c for c in comps if (safe(lambda c=c: c.name) or "").lower() == scope.lower()]
            if not search:
                return None, (f"Source body '{spec}': no component '{scope}' in the source. "
                              f"Available components: {_names([safe(lambda c=c: c.name) for c in comps])}.")
        matches = [(c, b) for c in search for b in _owned_bodies(c)
                   if (safe(lambda b=b: b.name) or "").lower() == body_name.lower()]
        if len(matches) > 1:
            holders = _names(sorted({safe(lambda c=c: c.name) or "?" for c, _ in matches}))
            return None, (f"Source body '{spec}' is ambiguous - a body named '{body_name}' exists in "
                          f"{holders}. Qualify it as 'Component/Body'.")
        if not matches:
            return None, f"Source body '{spec}' not found in the source design."
        out.append(matches[0][1])
    return out, None


def _component_occurrences(source_root, comp):
    """Every occurrence (at any assembly level) of `comp` in the source - the browser-selection analog
    of picking that part for the derive. Occurrence is a first-class sourceEntity (live-verified)."""
    return list(_common.iter_collection(safe(lambda: source_root.allOccurrencesByComponent(comp))))


def _collect_source_entities(source_design, component_names, body_names):
    """Resolve component + body names to the entity list DeriveFeatureInput.sourceEntities takes.
    A named component contributes its occurrence(s) (the root component contributes ITSELF - the
    whole-design case); a named body contributes that BRepBody/MeshBody. Returns (entities, labels,
    error). Empty names -> ([], [], None) so the caller can fall back to the whole design."""
    entities, labels = [], []
    source_root = safe(lambda: source_design.rootComponent)
    comps, err = _resolve_source_components(source_design, component_names)
    if err:
        return None, None, err
    for want, comp in zip(component_names, comps):
        # same_component, not a NAME compare: the resolved component and source_design.rootComponent
        # are separate wrappers (component identity is never stable), and a name that will not read
        # on one of them would send the ROOT down the occurrence branch, where it has none.
        at_source_root = _common.same_component(comp, source_root)
        if at_source_root is True:
            entities.append(comp)                       # the whole source design (Component form)
            labels.append(f"{want} (whole design)")
            continue
        if at_source_root is None:
            # The two branches build DIFFERENT source entities (the Component itself vs its
            # occurrences), so an unproven answer picks the wrong sourceEntities half the time - and
            # the occurrence branch's own refusal would blame a missing instance instead.
            return None, None, (f"Whether source component '{want}' is the source design's ROOT "
                                "could not be read, and the root derives as itself while any other "
                                "component derives through its occurrences - so which entities to "
                                "derive is unknown. Name a sub-component, or omit 'components' to "
                                "derive the whole source design.")
        occs = _component_occurrences(source_root, comp)
        if not occs:
            return None, None, (f"Source component '{want}' has no occurrence in the assembly to "
                                "derive (it is defined but not instanced).")
        entities.extend(occs)
        labels.append(want if len(occs) == 1 else f"{want} (x{len(occs)})")
    bodies, err = _resolve_source_bodies(source_design, body_names)
    if err:
        return None, None, err
    entities.extend(bodies)
    labels.extend(body_names)
    return entities, labels, None


# ── read-back: what ACTUALLY landed. feature.bodies is EMPTY for an occurrence-tree derive (the
# content lands nested under a derived occurrence), so bodies are counted off those occurrences too ──

def _occurrence_tokens(comp):
    """entityToken of every occurrence directly in `comp` right now - a before/after snapshot so a
    NEW derived occurrence can be told apart from one that was already there."""
    tokens = set()
    for o in _common.iter_collection(safe(lambda: comp.occurrences)):
        tok = safe(lambda o=o: o.entityToken)
        if tok:
            tokens.add(tok)
    return tokens


def _subtree_body_count(occ):
    """Bodies in an occurrence's whole subtree (its own component's bodies + every descendant's) -
    a whole-design derive lands ONE top occurrence whose bodies live in nested sub-components, so a
    top-level count reads 0 while 10 bodies are really there (live-verified)."""
    total = safe(lambda: occ.bRepBodies.count, 0) or 0
    for ch in _common.iter_collection(safe(lambda: occ.childOccurrences)):
        total += _subtree_body_count(ch)
    return total


def _new_derived_occurrences(comp, before_tokens):
    """[{name, body_count}] for occurrences now directly in `comp` that (a) were not there before
    add() and (b) report isDerived=true - the occurrence-side of the landing, with real body counts."""
    out = []
    for o in _common.iter_collection(safe(lambda: comp.occurrences)):
        tok = safe(lambda o=o: o.entityToken)
        if tok and tok in before_tokens:
            continue
        if safe(lambda o=o: o.isDerived, False):
            out.append({"name": safe(lambda o=o: o.name), "body_count": _subtree_body_count(o)})
    return out


def handler(document_id: str = "", into_component: str = "",
            source_components="", source_bodies="", exclude_components="", exclude_bodies="",
            include_parameters: bool = True, include_favorite_parameters: bool = True,
            place_at_origin: bool = True) -> dict:
    """See TOOL_DESCRIPTION."""
    raw = (document_id or "").strip()
    if not raw:
        return error("Provide 'document_id' - the lineage URN (or web URL) of the saved cloud "
    "document to derive.")

    design = _common.design()
    if not design:
        return error("No active design. Open or create the host document first (see doc_new).")

    good, mode_err = _MODE_GUARD.check(design)
    if not good:
        return mode_err

    into_occ = None
    if (into_component or "").strip():
        into_occ, into_err = _INTO_COMPONENT.resolve(into_component)
        if into_err:
            return error(into_err)
        comp = safe(lambda: into_occ.component)
        if not comp:
            return error(f"Occurrence '{into_component}' has no component to derive into.")
        comp_desc = f"component '{safe(lambda: comp.name)}'"
    else:
        comp = design.rootComponent
        comp_desc = "root component"

    data_file, resolved, candidates = _resolve_data_file(raw)
    if not data_file:
        tried = ", ".join(candidates) if candidates else raw
        return error(f"Could not resolve '{raw}' to a saved document. Tried: {tried}. Pass a "
    "lineage URN or web URL (from data_get). The document must be SAVED to the cloud.")

    # Derive needs the source design LOADED. Fusion opens documents ASYNCHRONOUSLY and this handler
    # holds the main thread until it returns, so a source that is not already open cannot load
    # within the call - createInput returns None on the half-loaded design.
    source_doc = _find_open_document(data_file)
    if source_doc is None:
        return error(f"The source document '{safe(lambda: data_file.name) or resolved}' is not "
                     "open. This tool derives from an ALREADY-OPEN source (Fusion loads documents "
                     "asynchronously - it cannot load one within a single call). Open it first: "
                     f"doc_open(file_id='{resolved}', force_api_open=true), confirm it loaded with "
                     "workspace_orient, then retry - the derive reuses the loaded source.")
    # The source is caller-managed from here on (we never opened it), so a failure below just
    # returns error() and leaves the source document exactly as found.

    source_design = safe(lambda: adsk.fusion.Design.cast(
        source_doc.products.itemByProductType('DesignProductType')))
    if not source_design:
        return error(f"Source document '{safe(lambda: data_file.name) or resolved}' has no Design "
                      "product to derive from (not a Fusion design file?).")

    # Resolve the (optional) named subset in the SOURCE. Empty selectors -> the whole design.
    comp_names = _as_names(source_components)
    body_names = _as_names(source_bodies)
    excl_comp_names = _as_names(exclude_components)
    excl_body_names = _as_names(exclude_bodies)
    if comp_names or body_names:
        source_entities, scope_labels, sel_err = _collect_source_entities(
            source_design, comp_names, body_names)
        if sel_err:
            return error(sel_err)
        scope_desc = ", ".join(scope_labels)
    else:
        source_entities = [safe(lambda: source_design.rootComponent)]
        scope_desc = "whole design"
    excluded_entities, _excl_labels, excl_err = _collect_source_entities(
        source_design, excl_comp_names, excl_body_names)
    if excl_err:
        return error(excl_err)

    derive_feats = safe(lambda: comp.features.deriveFeatures)
    if derive_feats is None:
        return error(f"{comp_desc} has no deriveFeatures collection (unexpected).")

    di = safe(lambda: derive_feats.createInput(source_design))
    if di is None:
        return error("deriveFeatures.createInput returned nothing - the source design could not be "
                      "prepared for derive. The source may not be fully loaded yet; confirm it with "
                      "workspace_orient (re-open with doc_open if needed), then retry.")

    try:
        # sourceEntities/excludedEntities take a plain Python list (SWIG std::vector<Ptr<Base>>), NOT
        # an adsk.core.ObjectCollection - an ObjectCollection raises a SWIG type error (live-verified).
        di.sourceEntities = source_entities
        if excluded_entities:
            di.excludedEntities = excluded_entities
        di.isIncludeComponentParameters = bool(include_parameters)
        di.isIncludeFavoriteParameters = bool(include_favorite_parameters)
        di.isPlaceObjectsAtOrigin = bool(place_at_origin)
    except Exception as e:
        return error(f"Could not configure the derive: {e}")

    before_params = safe(lambda: design.userParameters.count, 0) or 0
    # A source value can land as a read-only MODEL parameter the userParameters delta never sees,
    # and it lands at DESIGN level rather than on the derived component - so the count that sees it
    # is a design-wide before/after delta.
    before_all_params = _common.counted(lambda: design.allParameters.count)
    before_bodies, _ = _common.design_wide_counts(design)
    before_occ_tokens = _occurrence_tokens(comp)
    root = safe(lambda: design.rootComponent)
    # same_component, not `is`: component wrappers are never identity-stable, so `comp is not root`
    # reads True even at the root. The branch is an optimisation only, so an unproven answer takes
    # the walk, which is right whichever component `comp` turns out to be.
    target_is_root = _common.same_component(comp, root)
    before_root_tokens = (before_occ_tokens if target_is_root is True
                          else _occurrence_tokens(root))

    # The platform routes a derive into the ACTIVE component, IGNORING which component's
    # deriveFeatures collection built the input: without activation a derive "into" a non-active
    # component lands as a ROOT sibling. So nesting = activate the target, add, restore.
    prev_active = safe(lambda: design.activeComponent.name)
    active_after = _UNREAD
    if into_occ is not None:
        if not safe(lambda: into_occ.activate(), False):
            return error(f"Could not activate '{into_component}' to receive the derive "
                         "(Occurrence.activate() returned false). Nothing was derived.")
    try:
        try:
            feature = derive_feats.add(di)
        except Exception as e:
            return error(f"Derive failed: {e}.")
    finally:
        if into_occ is not None:
            # activateRootComponent() answers True even when the root is ALREADY active, so its
            # answer cannot separate "came back" from "was already there" - only the
            # activeOccurrence read-back can, and that is what the payload reports.
            safe(lambda: design.activateRootComponent())
            active_after = safe(lambda: design.activeOccurrence, _UNREAD)
    if not feature:
        return error(_common.no_feature_error(design, "Derive",
                                              "(deriveFeatures.add returned nothing.)"))

    # Force a full recompute so world-space reads (bounding box, joint-origin frames) taken right
    # after this call are FRESH - without it a bbox/center can read stale until an unrelated edit
    # recomputes the design. computeAll returns True only that it ran, not that every item is healthy.
    safe(lambda: design.computeAll())

    # --- verify the effect (rung 4: read the landed bodies back, inline: each check gates the return) ---
    health = safe(lambda: feature.healthState)
    if health == _ERROR_HEALTH:
        msg = safe(lambda: feature.errorOrWarningMessage) or "(no message)"
        return error(f"Derive was created but FAILED to compute: {msg}")

    doc_ref = safe(lambda: feature.documentReference)
    out_of_date = safe(lambda: doc_ref.isOutOfDate) if doc_ref is not None else None
    if out_of_date:
        return error("Derive was created but its documentReference reads isOutOfDate=true "
                      "immediately at creation - the link did not land against the resolved version.")

    # feature.bodies holds DIRECT top-level bodies only: an occurrence-tree derive lands them nested
    # under a derived occurrence and reads empty here, so both are counted plus the design-wide delta.
    direct_bodies = [{"name": safe(lambda b=b: b.name),
                      "is_derived": bool(safe(lambda b=b: b.isDerived, False))}
                     for b in _common.result_bodies(feature)]
    derived_components = _new_derived_occurrences(comp, before_occ_tokens)
    # `is False` only: the net's error says the nesting FAILED, and run against a target that may
    # itself be the root a successful root derive would trip it. An unproven answer skips the net and
    # DISCLOSES that the landing was not checked.
    landing_note = None
    if not derived_components and target_is_root is None:
        landing_note = ("Whether the derive's target component is this design's root could not be "
                        "read, so the check for a derive that landed at ROOT instead of nested was "
                        "not run - the bodies reported below landed, but WHERE they nested is "
                        "unverified. Confirm with design_get(include=['tree']).")
    if not derived_components and target_is_root is False:
        # Landing net: nothing new in the TARGET component - if the derive surfaced at ROOT
        # instead, the nesting failed and saying so beats a silent root sibling.
        strays = _new_derived_occurrences(root, before_root_tokens)
        if strays:
            names = _names([s.get("name") for s in strays])
            return error(f"Derive landed at the ROOT component ({names}), not in {comp_desc} - "
                         "the target activation did not take, so the nesting failed. The derive "
                         "EXISTS at root: delete its feature (design_delete_feature) and retry, "
                         "or keep it and move on.")
    after_bodies, _ = _common.design_wide_counts(design)
    bodies_landed = max(0, after_bodies - before_bodies)
    any_derived_marker = (any(b["is_derived"] for b in direct_bodies) or bool(derived_components))

    if bodies_landed == 0 and not direct_bodies and not derived_components:
        return error("Derive created a feature but nothing landed - no bodies appeared and no new "
                      "derived occurrence. The link may not have resolved; check the source scope.")
    if not any_derived_marker:
        return error("Derive created a feature and geometry appeared, but nothing reports "
                      "isDerived=true - the one-way link may not have formed correctly.")

    after_params = safe(lambda: design.userParameters.count, 0) or 0
    parameters_imported = max(0, after_params - before_params)
    after_all_params = _common.counted(lambda: design.allParameters.count)
    model_parameters_added = (None if before_all_params is None or after_all_params is None
                              else after_all_params - before_all_params)
    param_warning = None
    if (include_parameters or include_favorite_parameters) and parameters_imported == 0:
        param_warning = ("include_parameters/include_favorite_parameters was requested but 0 new "
                         "user parameters landed - this Fusion API flag is reported flaky; confirm "
                         "with param_get (the source design may also simply define none). Source "
                         "values can land as read-only model parameters instead - read them with "
                         "param_get(include_model_parameters=true).")

    representative = (derived_components[0]["name"] if derived_components
                      else (direct_bodies[0]["name"] if direct_bodies else None))

    result = {
    "derived": True,
    "feature_name": safe(lambda: feature.name),
    "into_component": comp_desc,
    "scope": scope_desc,
    "document_id": resolved,
    "document_name": safe(lambda: data_file.name),
    "source_version": safe(lambda: data_file.versionNumber),
    "is_parametric": bool(safe(lambda: feature.isParametric, False)),
    "derived_occurrence": representative,
    "derived_components": derived_components,
    "derived_bodies": direct_bodies,
    "bodies_landed": bodies_landed,
    "parameters_imported": parameters_imported,
    "note": ("One-way linked COPY of the source's last SAVED cloud version - unsaved in-session "
            "edits in the source are NOT derived (save the source, then doc_update_xref). Edits made "
            "here (a fillet, a patch, an offset) never travel back to the source, and the source "
            "itself was not modified. Build prep on top of the derived body/bodies."),
    }
    if landing_note:
        result["landing_unverified"] = landing_note
    if model_parameters_added is not None:
        # Beside parameters_imported (the userParameters delta), because the two count different
        # things and only the pair shows whether source values arrived at all. Omitted rather than
        # zeroed when either count was unreadable - an unknown delta is not "nothing arrived".
        result["model_parameters_added"] = model_parameters_added
    if excluded_entities:
        result["excluded"] = ", ".join(excl_comp_names + excl_body_names)
    if param_warning:
        result["parameter_warning"] = param_warning
    if into_occ is not None:
        if active_after is not _UNREAD:
            result["active_occurrence_after"] = (
                None if active_after is None
                else safe(lambda: active_after.fullPathName) or safe(lambda: active_after.name)
                or "(unnamed occurrence)")
        if active_after is not None:
            reads = ("activeOccurrence did not read" if active_after is _UNREAD
                     else f"activeOccurrence reads {result['active_occurrence_after']!r}")
            result["root_restore_note"] = (
                f"nesting activated {comp_desc} and the restore to root ran, but {reads} - the "
                "return to ROOT is not confirmed. Set the edit target with "
                "design_activate_component(occurrence='root').")
        elif prev_active and prev_active != (safe(lambda: root.name) or ""):
            result["edit_target_note"] = (f"the active edit target was '{prev_active}' before this "
                                          "call and reads ROOT now (nesting activates the target, "
                                          "then returns to root). Re-activate with "
                                          "design_activate_component if needed.")
    return ok(result)


RETURNS = [
    _outputs.ReturnsName("feature_name", of="derive feature", consumers=["design_delete_feature"]),
    _outputs.ReturnsUrn("document_id", consumers=["doc_open", "doc_update_xref"]),
    _outputs.ReturnsName("derived_occurrence", of="derived body or occurrence",
                          consumers=["model_fillet", "joint_create"]),
]


TOOL_DESCRIPTION = (
    "Insert a one-way linked DERIVE of an OPEN document's design into a component, at its last "
    "SAVED cloud version.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_with_string_input(
        name="doc_insert_derive",
        description=TOOL_DESCRIPTION,
        input_param_name="document_id",
        input_param_description="Lineage URN or web URL, from data_get.",
    )
    .add_input_property(*_INTO_COMPONENT.as_property())
    .add_input_property("source_components", {"type": "array", "items": {"type": "string"}})
    .add_input_property("source_bodies", {"type": "array", "items": {"type": "string"}})
    .add_input_property("exclude_components", {"type": "array", "items": {"type": "string"}})
    .add_input_property("exclude_bodies", {"type": "array", "items": {"type": "string"}})
    .add_input_property("include_parameters", {"type": "boolean"})
    .add_input_property("include_favorite_parameters", {"type": "boolean"})
    .add_input_property("place_at_origin", {"type": "boolean"})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The landed occurrences are read off the REQUESTED target component, and a derive that surfaced
    # at root instead is an error naming what it left there.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_doc_insert_derive.py::TestIntoComponent"
                      "::test_root_stray_landing_is_an_honest_error",
        rung="value"))


def register_tool():
    register(item)
