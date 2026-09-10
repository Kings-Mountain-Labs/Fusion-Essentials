"""Lint: a shared helper lives in exactly ONE module - its home (CLAUDE.md "Reuse before you write").
A denylisted symbol (one optional leading underscore allowed) is defined in its home module and
nowhere else under tools/; a same-named definition doing a different job takes an _ALLOWLIST entry.
Two shared VALUE shapes are matched by shape below, since a renamed copy slips a name denylist."""

import ast
import os
import re

import _corpus
from conftest import TOOLS_DIR

# symbol -> (home module, is a function def vs a plain module-level assignment)
# Each entry names the ONE file allowed to define it; every other tool module must import it from
# there instead of re-implementing it.
_DENYLIST = {
    "get_cam": ("_cam_common", "def"),
    # The cutting-tool identity pair the create and the edit both read Operation.tool back with -
    # one home, so the '#<n> - ' prefix strip cannot drift between the two comparisons and turn a
    # landed assignment into a false refusal on one of them.
    "_tool_facts": ("cam_create_operation", "def"),
    "_names_the_same_tool": ("cam_create_operation", "def"),
    "find_setup": ("_cam_common", "def"),
    "find_operation": ("_cam_common", "def"),
    "walk_operations": ("_cam_common", "def"),
    # The kind -> child-collection map (operations/folders/patterns) the walk and both leaf
    # readers share - a local copy is how a reorder's re-read comes to read a DIFFERENT
    # collection than the walk that resolved its nodes.
    "CHILD_COLLECTIONS": ("_cam_common", "assign"),
    # The ONE operation pool (walk_cam_tree filtered to kind == 'operation') and the ONE unscoped
    # resolve that hands back the refusal AND the available list off a single walk of it. A local
    # re-filter is how a caller's remedy comes to describe a different census than the refusal it
    # accompanies - and how one unscoped miss walks the tree twice.
    "operation_nodes": ("_cam_common", "def"),
    "resolve_operation": ("_cam_common", "def"),
    "setup_names": ("_cam_common", "def"),
    "op_state_tally": ("_cam_common", "def"),
    "op_state_facts": ("_cam_common", "def"),
    # The ONE hasToolpath tally over a program's held operations, keeping the unreadable flag apart
    # from a False one; a second copy is how cam_post's posted_operations and cam_get's come to
    # count an operation the other one does not.
    "toolpath_present_tally": ("_cam_common", "def"),
    "op_primary_state": ("_cam_common", "def"),
    "op_is_suppressed": ("_cam_common", "def"),
    # The rail-PAIR drive parameter's NAME and the read over it. cam_select_geometry feeds a rail
    # pair into it and cam_get_status keys its rail triage on carrying it, so a second copy is how
    # renaming the parameter leaves one of them promising a triage the other never offers.
    "SWARF_CONTOURS_PARAM": ("_cam_common", "assign"),
    "is_rail_driven": ("_cam_common", "def"),
    # The ONE document-independent OperationStrategy.createFromString seam and the tri-state
    # entitlement read over it; a second copy is how the orient's capability block and cam_generate's
    # launch pre-flight come to disagree about one strategy.
    "create_strategy": ("_cam_common", "def"),
    "strategy_generation_allowed": ("_cam_common", "def"),
    "validity_basis": ("_cam_common", "def"),
    # The ONE 'max_results' clamp every capped read runs under (CAM and non-CAM alike): one home so
    # the not-a-number fallback and the 1..ceiling hold cannot be right in one read and stale in the
    # next (each read still passes its OWN default/ceiling pair).
    "clamp_rows": ("_cam_common", "def"),
    # The ONE compute-failure read (healthState classed error/warning + the condensed one-sentence
    # message before the 'Compute Failed' marker).
    "compute_failure": ("_assert", "def"),
    "compute_failure_message": ("_assert", "def"),
    # compute_failure's other half: whether a compute state ANSWERED at all, which its None hides.
    # One home, or a surface turns "could not read it" into a published healthy:true while its
    # sibling withholds the flag on the same entity.
    "health_state_read": ("_assert", "def"),
    # The ONE dispatch over those two halves: entity first, then its timelineObject, answering
    # 'broken'/'healthy'/'unknown' plus the failure the verdict was taken from. A re-roll is how two
    # surfaces reach DIFFERENT verdicts on one entity - an as-built joint whose state only its
    # timeline item answers reads broken in one payload and healthy in the next.
    "compute_state": ("_assert", "def"),
    # The ONE any-True / False-if-any-False / else-None collapse over per-body is_solid flags, and
    # the two projections over it (names + flags, names + that verdict); a re-roll is how one
    # surface tool reads an unreadable flag as an open sheet while its sibling says unknown.
    "_solid_verdict": ("_surface_common", "def"),
    "_result_body_report": ("_surface_common", "def"),
    "_body_names_and_solid": ("_surface_common", "def"),
    # The ONE identical-open-documents test (a visible tab and its dependency instance share the
    # name AND the lineage URN - that is ONE document, not an ambiguity; measured live). The write
    # guard's expect_document check and _doc_common's URN resolver both answer through it - a
    # local re-roll is how the two resolvers drift into opposite verdicts on the same state.
    "one_open_document": ("_write_guard", "def"),
    # The ONE create-flow rename-with-disclosure: sets entity.name, reads it back, returns
    # (final_name, warning-or-None). A per-tool try/except-pass copy is the swallowed-rename defect
    # this helper replaced - the payload must disclose a rename that declined or landed deduped.
    "apply_rename": ("_common", "def"),
    # The ONE CAM tree traversal + refusal resolver (setups/ops/folders/patterns; a duplicated name
    # is refused with each hit's setup path) - every cam_* tool resolves names through these.
    "walk_cam_tree": ("_cam_common", "def"),
    "tree_nodes": ("_cam_common", "def"),
    "resolve_cam_node": ("_cam_common", "def"),
    "operations_under": ("_cam_common", "def"),
    # Its node-carrying form: the scoped walk started from the node's OWN path, so every row keeps
    # the 'Setup / ... / op' breadcrumb. A local re-roll walks from "" and prints ' / Drill1',
    # naming neither setup nor folder - which is the one thing separating two same-named operations.
    "operation_nodes_under": ("_cam_common", "def"),
    # The ONE 'parameters' request parser ({name: expression} or 'name=value, ...'): the operation
    # and setup parameter editors validate the SAME two wire forms, so a second copy is how one of
    # them starts accepting a form the other refuses.
    "parse_parameters": ("_cam_common", "def"),
    # The ONE "does this parameter store its value QUOTED" decision on a CAM write path (the
    # operation editor, the setup editor, the geometry-mode writer). A second copy is how one of
    # them starts sending a bare enumeration value Fusion refuses while its sibling wraps it.
    "matched_quoting": ("_cam_common", "def"),
    # The ONE wrap-in-single-quotes-and-escape-the-apostrophes codec. A re-roll is how one writer
    # starts escaping a character its sibling sends through, on a store nothing measured.
    "quote_expression": ("_cam_common", "def"),
    # The ONE CAM library folder-tree walk (tool / post / template libraries all nest folders under
    # a LibraryLocations root) plus its collect-the-asset-urls projection and its raise-tolerant
    # child read. A local re-roll is how one library read loses the depth/folder bound - an
    # unbounded cloud enumeration is a measured way to hang the add-in - while the others keep it.
    "walk_library_folders": ("_cam_common", "def"),
    "library_assets": ("_cam_common", "def"),
    "library_children": ("_cam_common", "def"),
    # The ONE 'which asset answers to this name' matcher every library DELETE addresses its target
    # with: a stored leafName carries the file EXTENSION the object's own name does not, so an asset
    # answers to its whole leafName AND to its stem, both compared EXACTLY. A local re-roll is how
    # one delete tool matches on a substring and removes the neighbour whose name merely starts the
    # same, or splits on the FIRST dot and cannot find 'Mill v1.2.mch' at all.
    "asset_key": ("_cam_common", "def"),
    "asset_leaf": ("_cam_common", "def"),
    "asset_leaf_keys": ("_cam_common", "def"),
    "assets_named": ("_cam_common", "def"),
    # The ONE MachineLibrary handle (off CAMManager.libraryManager, so no open CAM job is needed)
    # and the ONE 'which location holds this machine' read - a single FILTERED Local query. A second
    # copy is how the create's clash report and the delete's local-only gate start answering
    # 'fusion360' and 'local' about one machine.
    "machine_library": ("_cam_common", "def"),
    "machine_location": ("_cam_common", "def"),
    "_b64url_decode": ("_data_common", "def"),
    # The ONE DataFile epoch-seconds -> ISO-8601-UTC conversion (None for anything that is not a
    # number); the raw integer ships beside it, so a second copy is a second convention.
    "_epoch_iso": ("_data_read", "def"),
    "_urn_candidates": ("_data_common", "def"),
    # The ONE DataFile reference resolver (lineage URN / web URL, or a name scoped to a project,
    # refusing a name that matches several) plus the name-carries-the-true-extension fact every
    # file-scoped data tool gates on - a per-tool copy is how one of them starts guessing.
    "resolve_file_reference": ("_data_common", "def"),
    "name_extension": ("_data_common", "def"),
    "FUSION_NATIVE_EXTENSIONS": ("_data_common", "assign"),
    # The ONE folder-PATH walk from a project root: it hands back the folder, its cleaned path, or
    # the miss facts (segment, deepest folder reached, that folder's subfolder names - None when the
    # enumeration RAISED). A second copy is how one cloud tool starts reporting an UNREAD folder as
    # an empty one while the others say unknown.
    "navigate_folder_path": ("_data_common", "def"),
    # The ONE timeline by-name resolve-and-refuse: exact case-insensitive match, 'name@index' for a
    # repeated name, a miss listing what IS there. design_delete_feature, design_edit_timeline and
    # the FeatureRef kind all answer the same wire form through it; a second copy is how one string
    # names two different features across two tools.
    "resolve_timeline_object": ("_inputs", "def"),
    # The ONE active-document identity read, and the same one the write guard stamps 'acted_on'
    # from: a local re-roll is how a read names one document while the write that follows it
    # names another.
    "_active_identity": ("_write_guard", "def"),
    # The ONE per-instance document key every store that outlives one MCP call is keyed by (a view
    # snapshot, a driven-joint registry, a live generation), its backing registry, and the prune
    # that evicts a closed document from it. A second copy is how one consumer keeps the BACKWARD
    # prune walk while the other's forward walk skips the entry that slid into the freed slot and
    # then indexes past the end, raising IndexError out of every handler that reads a key. One
    # home, and the walk is pinned once.
    "document_key": ("_write_guard", "def"),
    "prune_closed_documents": ("_write_guard", "def"),
    "on_key_evicted": ("_write_guard", "def"),
    "on_key_renamed": ("_write_guard", "def"),
    "UNSAVED_DOC_KEYS": ("_write_guard", "assign"),
    "UNSAVED_DOC_SEQ": ("_write_guard", "assign"),
    "sanitize": ("_export", "def"),
    # The ONE design-wide by-name component resolve: exactly one component carrying the name
    # resolves, a name several carry is refused with the count. A second copy is how one tool
    # exports/imports into whichever same-named component its own walk reached first.
    "find_component": ("_export", "def"),
    "verify_written": ("_export", "def"),
    # The ONE unit key -> DistanceUnits member map every writer of an STL bakes unitType from, and
    # the read that resolves it. STLExportOptions.unitType takes DistanceUnits and NOT MeshUnits,
    # whose mm/cm ints are SWAPPED - a second copy is how one exporter starts writing 10x-wrong
    # geometry for the two commonest units while its sibling stays right.
    "STL_UNIT_MEMBERS": ("_export", "assign"),
    "stl_unit_enum": ("_export", "def"),
    # The ONE export-options knob writer both exporters set every option through: pre-read, write,
    # post-read, returning (key, changed). The PRE-read is the load-bearing half - it separates "the
    # assignment put this value here" from "the value was already there" - and a naive set-then-
    # compare cannot: STLExportOptions.unitType reads 0 both when unset (which writes INCHES) and
    # when explicitly millimetres, because MillimeterDistanceUnits IS 0. A local three-line re-roll
    # drops the pre-read and silently loses that distinction.
    "applied_pair": ("_export", "def"),
    # The ONE output-path prep every file writer runs: strip the request, APPEND the format's
    # extension when the name lacks it, create the directory. A local re-roll is how one exporter
    # appends and the next silently writes PNG bytes to a .jpg name.
    "prepare_out_path": ("_export", "def"),
    # The ONE clock-bounded doEvents-pumping wait an async Fusion write is gated on - one home so
    # the bound, the pump and the give-up cannot be right in one export tool and stale in the next.
    "pump_until": ("_export", "def"),
    # The ONE adsk.drawing enum member read BY NAME (None on an absent family/member, never a
    # raise) and the ONE drawing-standard decode - behaviorally identical re-rolls survive every
    # test, so this entry is the only thing that keeps them from coming back.
    "enum_value": ("_drawing_common", "def"),
    "standard_label": ("_drawing_common", "def"),
    "coordinate_unit": ("_drawing_common", "def"),
    # The drawing tables and the DrawingDocument cast: the size<->standard pairing Fusion silently
    # ignores at creation and RAISES on at assignment, the strategy family, the unit a drawing's own
    # numbers are authored in, and the documentReferences-carrying cast. A per-tool copy is how one
    # drawing tool starts offering a size or strategy the other refuses.
    "active_drawing_document": ("_drawing_common", "def"),
    "SHEET_SIZE_MAP": ("_drawing_common", "assign"),
    "DIMENSION_STRATEGIES": ("_drawing_common", "assign"),
    "DOCUMENT_UNIT": ("_drawing_common", "assign"),
    "ptxyz": ("_common", "def"),
    "target_sketch": ("_common", "def"),
    # The ONE design-wide by-name sketch walk and the three contracts over it (collect-all,
    # resolve-one-or-refuse, name-or-most-recent in both its refusing and its silent form). A second
    # walk is how one tool's census matched case-insensitively while the resolve beside it asked
    # itemByName - the name that "exists" in the census then resolves to nothing.
    "find_sketches_by_name": ("_common", "def"),
    "find_sketch": ("_common", "def"),
    "resolve_sketch": ("_common", "def"),
    "find_or_recent_sketch": ("_common", "def"),
    "resolve_or_recent_sketch": ("_common", "def"),
    # The component SCOPE over that same walk: one match rule (exact, case-insensitive) and one
    # unknown-component refusal, so a scoped list and a scoped by-name read can never select
    # different components or teach different vocabulary. find_sketch_in filters by component
    # IDENTITY, since two components CAN wear one name and a name comparison cannot tell them apart.
    "components_in_scope": ("_common", "def"),
    "find_sketch_in": ("_common", "def"),
    # The occurrence PATH is the only spelling that separates two same-named components. Both halves
    # of a placement are read off ONE occurrence, so no caller has to ask whether two components are
    # the same - which nothing can answer while two distinct components report one entityToken.
    "component_placements": ("_common", "def"),
    "placement_paths_named": ("_common", "def"),
    # The address a shared-owner-name refusal names its owners by, read off ONE occurrence (its own
    # component answers to the name AND hands back the sketch). A local re-roll drops the sketch
    # half and offers an owner that owns nothing, or pairs a hit against the walk - which gives
    # every namesake every other's path while two components report one entityToken.
    "placements_holding_sketch": ("_common", "def"),
    # The ONE capped wire list: a truncated candidate list that does not COUNT what it dropped reads
    # as the complete set, and a caller picks its next call out of it.
    "named_with_remainder": ("_common", "def"),
    # The ONE head a message quotes a caller's own value back as, and the ONE disclosed cut. A
    # find_geometry handle runs past 200 chars, so a local re-roll is how one refusal floods the
    # sentence naming the way forward while its sibling stays readable.
    "short_ref": ("_common", "def"),
    # The ONE substitution a listing makes when a name REPEATS - the name where it identifies one
    # row, that row's discriminator where it does not. Every re-roll of this rule is a listing that
    # prints one name twice and has told the caller only the count it already stated.
    "told_apart": ("_common", "def"),
    # The spellings a case-insensitive component match ACTUALLY read - the clause that keeps a scope
    # refusal from asserting the query is a name some component carries.
    "spelled_as_read": ("_common", "def"),
    # The 'component' scope's vocabulary (component name / occurrence path / handle) - one home, so
    # sketch_get's two depths cannot accept different spellings.
    "scope_components": ("_sketch_detail", "def"),
    "scope_component": ("_sketch_detail", "def"),
    "timeline_health": ("_common", "def"),
    "result_bodies": ("_common", "def"),
    "body_facts": ("_common", "def"),
    # The ONE abort for a partial-computing createInput transaction (trim, boundary fill): a
    # per-tool copy is how a refused cancel silently stops being reported in one of them.
    "cancel_input": ("_common", "def"),
    # The ONE mode gate for a Features.*.add() that returns nothing, plus the refusal text a site
    # with no feature-independent effect check returns: a per-tool copy is how one tool keeps
    # calling a landed direct-mode edit a failure after the shared rule is fixed.
    "direct_feature_absence": ("_common", "def"),
    "no_feature_error": ("_common", "def"),
    "failed_effect_remedy": ("_common", "def"),
    # The body census a feature-free effect check counts on: one home so the resolve-ONCE rule and
    # the measured "the pieces land in the TARGET's parentComponent" scoping cannot be right in one
    # tool and stale in the next.
    "census_host": ("_common", "def"),
    "body_count": ("_common", "def"),
    # The ONE same-component test. Component wrappers are measured never identity-stable, so this
    # cannot be re-rolled as `a is b` anywhere: one home keeps the token-then-name hedge in step.
    "same_component": ("_common", "def"),
    # The ONE (nativeObject or self).entityToken read, and the ONE identity key built from it - the
    # key any two body references are compared or de-duplicated on. A native and its occurrence proxy
    # carry DIFFERENT tokens of their own, so a local `safe(lambda: b.entityToken)` re-roll is how one
    # de-dup counts a body twice while the same-body guard beside it never fires; and a token is
    # DOCUMENT-LOCAL, so a local (token-only) key is how one de-dup merges two x-ref'd bodies into one.
    "native_token": ("_common", "def"),
    "native_identity": ("_common", "def"),
    # The body key the by-name body walk groups a native and its proxy with: native_identity, else
    # the (name, scope) pair. Home is _inputs, the walk that needs the scope half - it names which
    # wrapper an ambiguity refusal lists. A copy keyed on the WRAPPER's entityToken splits one body
    # reached through two wrappers and merges two bodies sharing a document-local token. A caller
    # wanting the identity alone imports _common.native_identity; a before/after diff over ONE
    # component keys on native_identity plus the bare NAME, since the scope half re-splits the
    # native/proxy pair this key exists to join.
    "_body_key": ("_inputs", "def"),
    # The ONE feature-path resolver (sweep / pipe / path pattern / on-path datum): one home so the
    # 'sketch:<name>' chain rule and the single-handle chaining rule cannot be right in one tool
    # and stale in the next.
    "build_path": ("_common", "def"),
    # The timeline walk and the EXACT-match/'name@index' matcher behind FeatureRef: one home, so a
    # tool cannot re-roll the matcher with a substring fallback.
    "_timeline_objects": ("_inputs", "def"),
    "_match_timeline_objects": ("_inputs", "def"),
    "open_profile_from_sketch": ("_common", "def"),
    "most_recent_body": ("_common", "def"),
    "resolve_entity_ref": ("_common", "def"),
    "resolve_entity_refs": ("_common", "def"),
    # The ONE entity-anchored position grammar - a sketch ref's optional third segment
    # (':start/:end/:mid/:center') and the resolve to that SketchPoint. sketch_dimension and
    # sketch_constrain both read it; a local copy is how one verb accepts an anchor form the other
    # rejects, which is exactly the asymmetry this home removes.
    "parse_anchor_ref": ("_common", "def"),
    "anchor_point": ("_common", "def"),
    "midpoint_sketch_point": ("_common", "def"),
    "SKETCH_ANCHORS": ("_common", "assign"),
    "entity_component": ("_inputs", "def"),
    "axis_line_of": ("_inputs", "def"),
    # The ONE assembly-context walk that lifts a possibly-foreign entity into the single occurrence
    # placing its owner - and REFUSES a component placed several times. A copy is how one consumer
    # starts proxying into an arbitrary instance.
    "single_placement": ("_inputs", "def"),
    # The ONE occurrence census and the ONE unresolved-reference detector. A local
    # `safe(root.allOccurrences) or []` is the exact re-roll this owns: that property RAISES on a
    # design holding an unresolved reference, and the empty list is then published as fact.
    "occurrence_walk": ("_common", "def"),
    "component_walk": ("_common", "def"),
    "broken_reference": ("_common", "def"),
    "all_occurrences": ("_common", "def"),
    "occurrence_paths": ("_common", "def"),
    "component_contains": ("_common", "def"),
    "null_feature_note": ("_common", "def"),
    "design_wide_counts": ("_common", "def"),
    "CM_TO_UNIT": ("_common", "assign"),
    "OPERATIONS": ("_common", "assign"),
    # The ONE count/item(i) walk over a Fusion collection - a local re-roll drops the guard that
    # makes an ABSENT collection yield nothing instead of raising, and a skipped unreadable item
    # into a None in the caller's list.
    "iter_collection": ("_common", "def"),
    # The ONE 'that body, or the most recent one' resolution every whole-body edit runs (the tool
    # supplies only its own no-body sentence) - a copy is how one of them stops refusing an
    # ambiguous name.
    "resolve_body_or_recent": ("_common", "def"),
    # The ONE boolean-flag read that answers None for unreadable. A re-roll is written as
    # safe(getter, False), which is exactly the confident-False this exists to stop.
    "read_flag": ("_common", "def"),
    # The ONE integer-count read that answers None for unreadable (a body's lumps, a built path's
    # entities). A re-roll is written inline as `int(n) if isinstance(n, int) else None` beside a
    # DIFFERENT one written safe(read, 0), which is how two tools start disagreeing about whether an
    # unreadable count is a zero.
    "counted": ("_common", "def"),
    # The ONE read of the depth an extrude-family feature reports, and the band a landed depth may
    # differ from the request by. A copy drifts on the TYPE GUARD alone - one accepting int-or-float
    # beside one accepting only float is one feature the solid extrude calls unverifiable while the
    # surface extrude verifies it - and the measured contract (sign kept, the per-side number for a
    # symmetric extent, unclipped on a cut) holds only where it is stated once.
    "landed_extent_cm": ("_common", "def"),
    # The second side's twin, for a two-sided extent. It carries its own measured caveat - the
    # positive-request measurement says nothing about what a NEGATIVE two-sided side stores - and a
    # local copy is how one caller drops that caveat and compares against a convention nothing read.
    "landed_extent2_cm": ("_common", "def"),
    "EXTENT_MATCH_TOL_CM": ("_common", "assign"),
    # The ONE no-volume-change band every material-changing feature judges "the API reported success
    # but nothing moved" against; a site whose signal is not a volume keeps its own named tolerance.
    "NO_VOLUME_CHANGE_CM3": ("_common", "assign"),
    # The literal-or-parameter-expression length trio (extrude distance, offset plane) - one home
    # beside the Distance kind.
    "looks_like_expression": ("_inputs", "def"),
    # World axis key -> origin ConstructionAxis: the name map + the entity accessor, one home.
    "WORLD_AXIS_ATTRS": ("_inputs", "assign"),
    "world_construction_axis": ("_inputs", "def"),
    "length_value_input": ("_inputs", "def"),
    "expression_report": ("_inputs", "def"),
    # The plane-then-face two-pass every '*_to_surface' operand resolves through, and the label its
    # payload publishes: one home beside the SurfaceRef kind that declares them, so the curved-face
    # contract and the resolved-entity read-back can never be right in one sketch tool and stale in
    # the other.
    "resolve_surface": ("_inputs", "def"),
    "surface_ref_label": ("_inputs", "def"),
    "unit_vector": ("_geom", "def"),
    "unit_vector_between": ("_geom", "def"),
    "evaluator_normal_at": ("_geom", "def"),
    # The two vector products. Each is a one-liner, which is exactly how a None guard or a
    # tuple-vs-list return comes to differ quietly between tools comparing the same two vectors.
    "dot": ("_geom", "def"),
    "cross": ("_geom", "def"),
    "body_aabb": ("_geom", "def"),
    "owning_bodies": ("_geom", "def"),
    "volumes": ("_geom", "def"),
    "volume_delta": ("_geom", "def"),
    # The face-count counterpart of the volume pair - the signal a topology-changing feature
    # (delete-face, split-face) verifies with; one home so both read the same "unreadable is not
    # zero" contract.
    "face_counts": ("_geom", "def"),
    "face_count_delta": ("_geom", "def"),
    # The two not-touching reads a JOIN is verified with: the BRep lump count, and the AABB gap that
    # stands in for it on a body kind carrying no lumps. One home so "unreadable is not zero" and
    # "a positive gap PROVES they cannot touch" cannot be right in one combine tool and stale in the
    # other.
    "lump_count": ("_geom", "def"),
    "aabb_gap": ("_geom", "def"),
    # The ONE occurrence name a measure disclosure addresses a row by: fullPathName, which the
    # measure tools' own target input resolves back, else name. A local re-roll names an occurrence
    # by its LEAF, which two sub-assemblies can each answer to - so the row hands back an address
    # that resolves to a different occurrence than the one measured.
    "address": ("_geom", "def"),
    # The ONE bounded-gap measure for two PARALLEL PLANAR faces, and the sentence it discloses the
    # result with. A per-tool copy is how one measure tool starts judging a clearance on a plane
    # separation the other has already bounded - the two would then answer differently about the
    # same pair of faces, which is the drift a shared verdict cannot afford.
    "parallel_plane_facts": ("_geom", "def"),
    # The thread-table walk that turns a designation into a ThreadInfo, shared by the tapped hole
    # and the thread-an-existing-cylinder tools.
    # The ONE sketch local -> world frame: sketch_create publishes it on the way in and sketch_get on
    # the way out, so a second copy is how the numbers a caller PLACES against stop matching the ones
    # it VERIFIES against.
    "sketch_world_frame": ("_sketch_detail", "def"),
    "resolve_thread_info": ("_threads", "def"),
    "build_joint_geometry": ("_joints", "def"),
    "component_body_names": ("_common", "def"),
    "join_new_body_clause": ("_common", "def"),
    "apply_motion": ("_joints", "def"),
    "find_joint": ("_joints", "def"),
    "all_joints": ("_joints", "def"),
    "current_joint_type": ("_joints", "def"),
    # The ONE joint-kind -> drivable-DOF pairing (revolute+cylindrical turn, slider+cylindrical
    # slide). Every rotate-or-slide member a caller reads is selected through it - the driven value,
    # the limits bounding it, the heading published beside it - so a local copy is how one surface
    # starts reading a member off a kind the other says has no such DOF.
    "DRIVES_ANGLE": ("_joints", "assign"),
    "DRIVES_SLIDE": ("_joints", "assign"),
    # Their union - which joint kinds carry a drivable value AT ALL. joint_drive REFUSES every other
    # kind and joint_create_as_built's pose pointer sends every other kind to assembly_move, so a
    # second copy is how one of them starts offering joint_drive for a motion the other refuses.
    "DRIVES_ANY": ("_joints", "assign"),
    # The ONE JointOrigin walk + its leaf ops - the traversal _joint_inputs and model_inspect share
    # (resolve-one / collect-names / read-axes all sit on all_joint_origins).
    "all_joint_origins": ("_joints", "def"),
    "find_joint_origins_by_name": ("_joints", "def"),
    "jo_assembly_proxy": ("_joints", "def"),
    "jo_reference_names": ("_joints", "def"),
    # The ONE moved-but-uncaptured position read and the refusal every joint CREATE returns while it
    # is set. A local safe(..., False) re-roll is both the confident-False this exists to stop and
    # how one creation tool starts running through a pending move the others refuse.
    "pending_position": ("_joints", "def"),
    "pending_move_guard": ("_joints", "def"),
    "PENDING_MOVE_REFUSAL": ("_joints", "assign"),
    # The ONE already-jointed-PAIR diagnosis both create tools append, and the platform words it is
    # gated on. A second copy is how one create keeps naming the pair while the other relays the
    # platform text bare.
    "PAIR_USED": ("_joints", "assign"),
    "PAIR_USED_ANCHOR_NOTE": ("_joints", "assign"),
    "pair_used_clause": ("_joints", "def"),
    # The orient + refresh-then-grab capture mechanics both screenshot tools share.
    "apply_named_view": ("_view_common", "def"),
    "capture_png_b64": ("_view_common", "def"),
    # The standoff an orient rebuilds the eye at, and the distance that stands in when the camera's
    # own will not read. view_set carried a re-rolled `... or 10.0`, ten times nearer than
    # apply_named_view's 100 and disclosing nothing - one orient silently framing unlike the other.
    "standoff_distance": ("_view_common", "def"),
    "STANDOFF_FALLBACK_CM": ("_view_common", "assign"),
    # The ONE workspace activate-then-read-back: Workspace.isActive first, the UI's own active
    # workspace where that flag will not read. One home, so a switch and a restore cannot judge the
    # same workspace by different reads.
    "activate_workspace": ("_view_common", "def"),
}


# (module without .py, symbol) -> why this same-named definition is a DIFFERENT job, not a re-roll
# of the shared helper. Each reason must describe the local definition's own job. Kept honest by
# test_allowlist_entries_still_exist_and_still_differ: the entry must still match the pattern in
# that module AND its definition must still differ from the home module's - a local copy that
# becomes identical to home is a true duplicate and loses its exemption.
_ALLOWLIST = {}


def _pattern(symbol, kind):
    # `_?`: a re-roll hiding behind a leading underscore is the same duplication.
    if kind == "def":
        return re.compile(r"^def _?" + re.escape(symbol) + r"\(", re.M)
    return re.compile(r"^_?" + re.escape(symbol) + r"\s*=", re.M)


def _all_tool_files():
    return [fn for fn in sorted(os.listdir(TOOLS_DIR))
            if fn.endswith(".py") and fn != "__init__.py"]


def _read(fn):
    # Shared with every other lint reading this corpus: the denylist sweep asks for each tool file
    # once per symbol (~150 x ~190), and _corpus reads each one once per process.
    return _corpus.text(os.path.join(TOOLS_DIR, fn))


def _bracket_delta(line):
    return sum(line.count(o) for o in "([{") - sum(line.count(c) for c in ")]}")


def _definition_block(src, match):
    """The full statement starting at `match`: a def's body (lines until the next column-0 line) or
    a possibly-multi-line assignment (lines until brackets balance). Good enough for a lint's
    same-or-different comparison; strings containing brackets can only over-extend the block."""
    lines = src[match.start():].splitlines()
    block = [lines[0]]
    depth = _bracket_delta(lines[0])
    for line in lines[1:]:
        if depth <= 0 and line and not line[0].isspace():
            break
        block.append(line)
        depth += _bracket_delta(line)
    return "\n".join(block).strip()


def _normalized(block, symbol):
    """The block with the symbol's optional leading underscore dropped, so `_OPERATIONS = (...)`
    compares against home's `OPERATIONS = (...)` on content, not on the private prefix."""
    return re.sub(r"\b_" + re.escape(symbol) + r"\b", symbol, block)


class TestHelperDefinedOnlyInItsHomeModule:
    def test_denylisted_symbols_have_exactly_one_definition(self):
        offenders = []
        for symbol, (home, kind) in _DENYLIST.items():
            home_file = home + ".py"
            pattern = _pattern(symbol, kind)
            defined_in = []
            for fn in _all_tool_files():
                src = _read(fn)
                # the symbol's own name is a literal in both patterns, so a file that never
                # mentions it cannot define it - and that is nearly every (symbol, file) pair
                if symbol in src and pattern.search(src):
                    defined_in.append(fn)
            elsewhere = [fn for fn in defined_in
                         if fn != home_file and (fn[:-3], symbol) not in _ALLOWLIST]
            if elsewhere:
                offenders.append(
                    f"'{symbol}' is defined outside its home module {home_file} in: "
                    f"{', '.join(elsewhere)} - import it from {home} instead of re-implementing it "
                    f"(a leading-underscore variant counts; a genuinely different job gets an "
                    f"_ALLOWLIST entry with a reason)"
                )
            if home_file not in defined_in:
                offenders.append(
                    f"'{symbol}' is not defined in its declared home module {home_file} - "
                    f"the denylist entry is stale, fix it or move the definition back"
                )
        assert not offenders, "helper duplication:\n  " + "\n  ".join(offenders)

    def test_allowlist_entries_still_exist_and_still_differ(self):
        # An entry that no longer matches anything is dead weight hiding a regression check; an
        # entry whose local definition became a verbatim copy of home's is a true duplicate that
        # must collapse, not stay exempted.
        stale = []
        for (mod_name, symbol), reason in _ALLOWLIST.items():
            assert reason.strip(), f"({mod_name}, {symbol}) allowlist entry needs a reason"
            assert symbol in _DENYLIST, f"({mod_name}, {symbol}): '{symbol}' is not denylisted"
            home, kind = _DENYLIST[symbol]
            path = os.path.join(TOOLS_DIR, mod_name + ".py")
            if not os.path.exists(path):
                stale.append(f"({mod_name}, {symbol}): no such module - remove the entry")
                continue
            src = _read(mod_name + ".py")
            m = _pattern(symbol, kind).search(src)
            if not m:
                stale.append(f"({mod_name}, {symbol}): the module no longer defines it - "
                             f"remove the entry")
                continue
            home_src = _read(home + ".py")
            hm = _pattern(symbol, kind).search(home_src)
            if hm and (_normalized(_definition_block(src, m), symbol)
                       == _normalized(_definition_block(home_src, hm), symbol)):
                stale.append(f"({mod_name}, {symbol}): now identical to {home}'s definition - "
                             f"a true duplicate; collapse it onto {home} and remove the entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)


# The value-shape arm: two shared maps a local copy re-rolls under a NAME the denylist above cannot
# match (`_OPERATIONS`, `_SURFACE_OPS`, `_AXES`), so they are matched on the literal's shape instead.

def _names_a_feature_operation(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.endswith("FeatureOperation"))


def _is_feature_operations_map(node):
    return isinstance(node, ast.Dict) and any(_names_a_feature_operation(v) for v in node.values)


def _is_unit_axis_vector(node):
    """True for a (1,0,0)/(0,1,0)/(0,0,1)-shaped literal: 3 numeric elements, one 1 and two 0s."""
    if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) != 3:
        return False
    vals = []
    for e in node.elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, (int, float)) and not isinstance(e.value, bool):
            vals.append(e.value)
        else:
            return False
    return sorted(vals) == [0, 0, 1]


def _is_xyz_vector_map(node):
    """True for a dict literal mapping the keys 'x', 'y' AND 'z' to unit-axis vectors."""
    if not isinstance(node, ast.Dict):
        return False
    keyed = set()
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and _is_unit_axis_vector(v):
            keyed.add(k.value.lower())
    return {"x", "y", "z"}.issubset(keyed)


def _shape_offenders(home_file, matches):
    offenders = []
    for fn in _all_tool_files():
        if fn == home_file:
            continue
        for node in ast.walk(_corpus.tree(os.path.join(TOOLS_DIR, fn))):
            if matches(node):
                offenders.append(f"{fn}:{getattr(node, 'lineno', '?')}")
    return offenders


class TestSharedValueMapsAreNotReRolled:
    def test_no_local_feature_operations_map(self):
        offenders = _shape_offenders("_common.py", _is_feature_operations_map)
        assert not offenders, (
            "a local operation->FeatureOperations map diverges from _common.OPERATIONS - import and "
            "use it (`getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])`):\n  "
            + "\n  ".join(offenders))

    def test_no_local_world_axis_vector_map(self):
        offenders = _shape_offenders("_inputs.py", _is_xyz_vector_map)
        assert not offenders, (
            "a local world-axis x/y/z -> unit-vector map re-rolls _inputs._AXIS_VECS - import and "
            "use it (or the AxisRef kind, which also accepts an edge/face handle):\n  "
            + "\n  ".join(offenders))
