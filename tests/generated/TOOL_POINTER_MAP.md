# Tool pointer map (generated)

_Auto-generated from the tool source by `tests/gen_wiring.py`. Do not edit by hand._ For an
agent DEVELOPING tools in this repo, to diagnose the surface agents CONSUMING these tools
navigate by: where each tool's text (its **description** = the manual, its runtime **note/error**
= the situational tip) names ANOTHER tool. Act on the Blindspots below - fix dead references,
close orphans, factor duplicated guards into shared helpers.

**Tools:** 188  |  **description breadcrumbs:** 268  |  **note/error breadcrumbs:** 469
  |  **guidance smells flagged:** 4
## Blindspots to engineer

### Dead references (a detected literal tip names something that is not a tool - FIX THESE)
- none detected in the scanned literals.

### Orphans (no incoming breadcrumb detected in this map)
**Read/Acquire (6)** - higher concern, a check-your-work tool nothing points to:
  `cam_compare_operations`, `cam_inspect_toolpaths`, `drawing_get_status`, `model_compute_holder`, `model_measure_relation`, `sys_get_api_doc`

**Edit (55)** - usually leaf actions, scan for genuine gaps:
  `assembly_edit_contacts`, `cam_activate_setup`, `cam_delete_template`, `cam_generate_setup_sheet`, `cam_reorder`, `cam_set_nc_comment`, `cam_show_toolpath`, `data_create_project`, `data_delete_folder`, `design_configure`, `design_remove_feature`, `doc_insert_derive`, `doc_insert_import`, `doc_save_milestone`, `drawing_add_sketch`, `drawing_dimension`, `drawing_insert_image`, `joint_create_as_built`, `mesh_combine`, `mesh_delete`, `mesh_generate_face_groups`, `mesh_plane_cut`, `mesh_repair`, `mesh_reverse_normal`, `mesh_separate`, `mesh_shell`, `mesh_smooth`, `model_arrange`, `model_base_feature`, `model_draft`, `model_loft`, `model_pattern_path`, `model_pattern_rectangular`, `model_pipe`, `model_replace_face`, `model_scale`, `model_set_material`, `model_sweep`, `model_thread`, `model_unstitch`, `param_delete`, `param_set_favorite`, `sketch_add_3d_line`, `sketch_copy`, `sketch_insert_svg`, `sketch_move`, `sketch_project`, `surface_create_ruled`, `surface_delete_face`, `surface_extend`, `surface_fill`, `surface_offset`, `surface_revolve`, `surface_untrim`, `sys_reload_addin`

### Duplicated guard strings (>=4 copies = factor into a shared _common helper)
- **50x** across 50 module(s): "No active design. Create or open a document first (see doc_new)."
- **23x** across 23 module(s): "No active design. Open or create a document first (see doc_new)."
- **9x** across 3 module(s): "' with design_delete_feature."
- **8x** across 8 module(s): "No active design with components."
- **6x** across 3 module(s): "Could not create output directory '"
- **5x** across 5 module(s): "'. Use: new, join, cut, intersect."
- **5x** across 5 module(s): "Fusion declined to delete '"
- **4x** across 1 module(s): "Edits already applied before the failure:"
- **4x** across 4 module(s): "No active design (open a document with design geometry)."
- **4x** across 4 module(s): "deleteMe() reported success for '"
- **4x** across 4 module(s): "is not available on this Fusion version."
- **4x** across 1 module(s): "setMotionData reported success on '"

### Hubs (most breadcrumbs lead here - the connective tissue)
- `doc_new`  <- 82  (desc 0, note 82)
- `find_geometry`  <- 38  (desc 13, note 25)
- `design_delete_feature`  <- 37  (desc 16, note 21)
- `design_get`  <- 35  (desc 9, note 26)
- `view_screenshot`  <- 34  (desc 5, note 29)
- `cam_get`  <- 24  (desc 11, note 13)
- `data_get`  <- 23  (desc 10, note 13)
- `doc_open`  <- 22  (desc 5, note 17)
- `sketch_get`  <- 22  (desc 5, note 17)
- `sketch_create`  <- 21  (desc 7, note 14)
- `model_inspect`  <- 18  (desc 3, note 15)
- `assembly_get`  <- 16  (desc 3, note 13)

## The detected guidance surface

Static runtime **note/warning** literals per tool, attributed from each registered handler and one
level of local helper calls. Imported or deeper helpers and assembled messages are not
exhaustively covered. Use this surface to judge consistency and best-practice guidance.
Smells are auto-tagged: `war-story` (narrates history), `cause-guess` (asserts an
unverified cause), `hedge` (waffles). (Pure error-validation strings - 'must be a number' -
are omitted; this is the GUIDANCE layer, not input validation.)

### `appearance_set`
- Appearance override applied. Set a new color anytime; to revert, the override is on the body/occurrence (.appearance). Pair with view_screenshot to see it.
- ' into this document as '
- ' - the base every color override here is copied from.
- This write landed on the BODY, which is the component's NATIVE body - the color shows on EVERY instance of that component, not just one. To color one instance, target the OCCURRENCE (its fullPathNa...
- Appearance applied to
- failed - see 'failed'.
- body(ies) of this occurrence do NOT carry the new appearance (
- ) - each still reads the one named in 'bodies_not_reached'; color those directly (target = the body).
- body(ies) could not be compared, so the color is UNCONFIRMED there - see 'unverified_bodies'.
- The color was written to
- but could not be compared with a read-back there, so the appearance's own color is UNCONFIRMED.
- No active design with geometry.
- is outside 0-100. It is a PERCENT - the browser's Opacity Control - not a 0-255 color alpha.
- has no bodies to color.
- Opacity override applied - the browser's 'Opacity Control'.
- Could not apply appearance to any body of
- Assignment was accepted but
- still reads appearance '
- ' - the override did not take.
- 'opacity' must be a whole percent from 0 (invisible) to 100 (opaque).
- Could not apply appearance to
- body(ies) - each still reads a different appearance (
- ). Color the bodies directly (target = the body name).

### `assembly_capture_position`
- Latest captured position discarded - back to the last captured position that remains (or the joint-defined state when nothing else was ever captured).
- has_pending = a moved-but-uncaptured position exists (a joint_drive pose sets it the same way a free move does; a design_add_instance placement and an assembly_constrain relationship do NOT). Use c...
- Current position captured into the timeline.
- Uncaptured move thrown away - the assembly is back at its last captured position (or the joint-defined state when nothing was ever captured). Captured markers are untouched; use revert to drop the ...
- Captured position removed from the timeline; later captured positions (if any) survive a recompute unchanged.
- This design does not expose snapshots (capture position).
- has_pending is null - the pending-position flag could not be read, so whether a moved-but-uncaptured position exists is UNKNOWN here (it is not a 'no'). The captured markers below were still read.
- Nothing to revert - there are no captured positions.
- Fusion declined to revert the latest captured position.
- - the latest captured position was not removed. List the markers with assembly_capture_position(action='status').
- Revert reported success but
- Nothing to capture - there is no pending position change. Move a jointed component first (its pose is transient until captured).
- snapshots.add() returned nothing - the position was not captured.
- Capture reported success but the snapshot count did not advance (
- after) - the position was not captured.
- Capture reported success (Fusion named the marker '
- ') but taking it MOVED
- . The marker does NOT hold the pose you captured, and it REMAINS: remove it with assembly_capture_position(action='delete', marker='
- '), re-apply the move, and read the positions back with assembly_get before and after any retry.
- Nothing to discard - there is no pending position change.
- Fusion declined to discard the pending position change (revertPendingSnapshot returned false) - the move still stands.
- Discard ran, but the pending-position flag could not be re-read - the confirming read could not be taken, so the move may or may not have been thrown away. Call action='status' before acting on thi...  **[hedge]**
- Discard reported success but a pending position change is still reported - the move was not thrown away.
- action='delete' needs 'marker' (the captured position's name, from action='status').
- Nothing to delete - there are no captured positions.
- No captured position named '
- captured positions - marker names should be unique; check the timeline directly.
- Fusion declined to delete captured position '
- Delete reported success but '
- ' is still present in the snapshot collection.

### `assembly_constrain`
- Components constrained with the relationship set (type inferred from geometry).
- No active design with components.
- '. Valid: mm, cm, in.
- Assembly constraint creation returned nothing.
- ' was created but its healthState cannot be read, so whether it SOLVED is UNCONFIRMED - nothing here says the parts are located. Read it back with assembly_get(include=['relations']).
- Relax or remove one of its relationships.
- ' solved, but adding it left
- existing timeline feature(s) unhealthy:
- Deleting it does not restore them automatically - check them with assembly_get afterwards.
- ' was created but holds only
- relationship(s) submitted - the missing one(s) constrain nothing, so the parts are not located the way this call describes.
- Then re-submit the relationships that must solve together.
- 'relationships' must be a list of {snap_one, snap_two, flip?, offset?}.
- No relationships to constrain. Provide 'relationships' or snap_one/snap_two.
- Assembly constraint failed:
- ] needs both 'snap_one' and 'snap_two'.
- Provide 'relationships' or 'snap_one'/'snap_two' ('<occurrence>:<snap>') for autonomous geometry, OR select ONE entity on each occurrence in Fusion first then call again. (Got
- Could not read the two selected entities. Re-select and try again.
- ' is not a valid '<occurrence>:<snap>' (snap = center/top/bottom/left/right/front/back/cylinder/origin).
- ' is not a valid '<occurrence>:<snap>'.

### `assembly_edit_contacts`
- No active design. Open or create a document first (see doc_new).
- This design reports no contactSets collection, so contact sets cannot be read or changed here.
- Fusion declined to delete contact set '
- ' (deleteMe returned false) - it is still in the design.
- deleteMe reported success but contact set '
- ' is still listed - it was not deleted.
- The contact set is no longer listed; 'remaining' counts the readable sets left. Undo in Fusion if unintended - the API cannot restore it. Build another with action='create'.
- Deleting contact set '
- Contact analysis is OFF: no contact analysis is performed and every contact set is inert. 'scope' reads all_bodies while analysis is off, and the scope in force before it was disabled comes back on...
- isContactAnalysisEnabled cannot be read after setting it to
- , so the change is UNCONFIRMED - contact analysis may be in either state. Re-read with assembly_get(include=['contacts']).
- Setting isContactAnalysisEnabled=
- did not take - it reads
- Contact analysis is ON, but isContactSetAnalysis cannot be read, so what it is scoped to is unknown here.
- Contact analysis is ON and runs
- using the design's contact sets.
- between ALL bodies, ignoring every contact set - switch with action='set_analysis_scope', scope='contact_sets'.
- Could not set isContactAnalysisEnabled=
- Contact analysis now runs
- using the design's contact sets - list them with assembly_get(include=['contacts']).
- between ALL bodies, ignoring every contact set.
- 'scope' is required for action='set_analysis_scope': contact_sets (analysis uses the sets) or all_bodies (analysis ignores them).
- isContactAnalysisEnabled cannot be read on this design, so whether contact analysis is on - the precondition for a scope write - is UNKNOWN. Nothing was changed. Re-read with assembly_get(include=[...
- Contact analysis is not enabled on this design (isContactAnalysisEnabled reads
- ), and the platform refuses a scope write while it is off - assigning isContactSetAnalysis raises '3 : Contact analysis is disabled.'. Run action='enable_analysis' first, then set the scope. Nothin...
- isContactSetAnalysis cannot be read after setting it to
- , so the scope change is UNCONFIRMED. Re-read with assembly_get(include=['contacts']).
- Setting isContactSetAnalysis=
- Could not set isContactSetAnalysis=
- The set was created but reports no name - find it with assembly_get(include=['contacts']).
- Fusion named the set '
- ' - address it by that name from here.
- Inspect it with assembly_get(include=['contacts']).
- The contact set was created but is not what was asked for:
- The create call also raised '
- Creating the contact set failed:
- contact set(s); it held
- contactSets.add returned nothing
- contactSets.add raised (
- ' appeared that cannot be re-read. Inspect the design with assembly_get(include=['contacts']).
- The members read back above are what the set now holds.
- Could not set the members of contact set '
- The set is now named '
- 'new_name' is required for action='rename'.
- ' reports no name after the rename, so nothing confirms it.
- Renaming contact set '
- ' did not take - it still reads '
- Fusion landed the name '
- ', not the requested '
- ' - a name already in use is auto-deduped to 'Name (1)'. Address the set by '
- ' - the platform changed it and the reason is not readable from here. Address the set by '
- Could not rename contact set '
- isSuppressed cannot be read on contact set '
- ' after setting it to
- , so the change is UNCONFIRMED. Re-read the set with assembly_get(include=['contacts']).
- Setting isSuppressed=
- ' did not take - it reads
- isSuppressed now reads
- '; the set stays in the design until action='delete'. What suppression does to contact behavior is not measured here.
- Could not set isSuppressed on contact set '

### `assembly_edit_relations`
- ' does not apply to a
- No active design with components.
- Coupling re-valued - 'interpreted' states how the ratio was read, and value_one/value_two are the link's own parameters READ BACK after the set. The SIGN of ratio SETS the direction, so a positive ...
- 'ratio' must be non-zero (a 0 ratio links no motion).
- ' does not report both coupled motions (motionOne/motionTwo), so its values cannot be re-set without guessing which degrees of freedom it links.
- Fusion declined to re-value motion link '
- ' (setMotionData returned false) - its ratio is unchanged.
- setMotionData reported success on '
- ' but its valueOne/valueTwo parameters cannot be read back, so nothing confirms the new ratio.
- ' and its parameters read
- , but isReversed cannot be read back, so the direction the SIGN of ratio sets is UNCONFIRMED. Re-read the link with assembly_get(include=['relations']).
- ' but it reads isReversed=
- - the direction did not take.
- 'ratio' must be a number (got
- setMotionData on motion link '
- . (The platform refuses a coupling it cannot solve; the link is unchanged.)
- . Set the opposite action to restore it.
- isSuppressed cannot be read on
- ' after setting it to
- , so the change is UNCONFIRMED. Re-read the relation with assembly_get(include=['relations']).
- Setting isSuppressed=
- ' did not take - it reads
- Could not set isSuppressed on
- is no longer listed (re-read to confirm); 'remaining' counts the readable
- s left. Undo in Fusion if unintended - the API cannot restore it. Re-create one with
- Fusion declined to delete
- ' (deleteMe returned false) - it is still in the design.
- deleteMe reported success but
- ' is still listed - it was not deleted.
- ' does not report isReversed, so there is no direction to flip.
- isReversed cannot be read on motion link '
- , so the flip is UNCONFIRMED. Re-read the link with assembly_get(include=['relations']).
- The linked joints now move in the opposite sense relative to each other. Drive ONE member (joint_drive) and read the partner back.
- Could not set isReversed on motion link '

### `assembly_get`
- Structured kinematic state. CHECK is_healthy FIRST - false means a joint, relation or feature FAILED TO COMPUTE, or the design holds an occurrence whose external reference does not resolve; broken_...
- '. Use mm, cm, or in.
- No active design. Open or create a document first (see doc_new).

### `assembly_ground`
- Parent lock set. isGroundToParent relocks to the TIMELINE placement and discards free moves. assembly_get's grounded_occurrences lists only the UI Ground/Fix flag (not settable here), so it stays e...
- Specify 'ground_to_parent' (true/false). true locks the occurrence to its timeline placement; false releases it.
- No active design with components.
- isGroundToParent cannot be read on '
- ' after setting it to
- , so the change is UNCONFIRMED. Re-read the occurrence with assembly_get.
- Assignment was accepted but '
- ' still reads isGroundToParent=
- - the flag did not take.
- Could not set isGroundToParent on '

### `assembly_inspect_interference`
- No interference - every part fits.
- interfering pair(s) - parts overlap in space. Each lists the two occurrences and their total overlap volume; fix positioning/sizing/joints. (A self-pair means two bodies of the same occurrence over...
- No active design to analyze.
- Cannot check interference: this design exposes
- comparable solid entit
- occurrence(s) at any depth,
- root-level solid body(ies)), and interference needs at least two. No verdict was formed - this is NOT a pass.
- Cannot certify interference-free:
- root-level solid body(ies) WERE compared and none of them interfere, but that is not a verdict over the whole assembly - no pass was formed. Resolve the reference (see workspace_orient health.unres...
- Interference analysis failed:

### `assembly_move`
- Occurrence repositioned (free move, no joint). This pose is UNCAPTURED - creating a joint ANYWHERE in the assembly (even on other parts) or a recompute can silently REVERT it; call assembly_capture...
- Occurrence posed (jointed - see jointed_warning). Pair with view_screenshot to view.
- '. Use mm, cm, or in.
- Provide a translation (dx/dy/dz), rotate_deg, or rotate_x/y/z - no movement specified.
- Use EITHER rotate_deg (single axis) OR rotate_x/y/z (multi-axis), not both.
- No active design with components.
- Move was accepted but '
- ' reads an unchanged transform - it did not move. A grounded/jointed occurrence can snap back: free it (assembly_ground false) or pose it through its joint (joint_drive).
- ' was moved but its transform could not be read
- the change, so the move is UNCONFIRMED - nothing here confirms the occurrence actually moved, and it may have snapped back. Re-read the position with assembly_get (occurrence origin) or model_inspect.
- ' reads a CHANGED transform but its body geometry did NOT move - the reposition did not reach the part, and the transform it now reads is a claim nothing carried out. A pattern/mirror FEATURE re-de...

### `assembly_rigid_group`
- No active design with components.
- A rigid group needs at least two occurrences.
- Rigid group creation returned nothing.
- ' was created but reports only
- - it locks parts that were not asked for. Remove it with assembly_edit_relations(kind='rigid_group', name='
- ', action='delete') and retry.
- Occurrences locked together as a rigid group.
- Could not create rigid group:

### `cam_activate_setup`
- Setup activated and view fit. Use view_screenshot to capture it.
- Setup activated. The view fit did not complete (
- ), so the camera may not frame this setup - orient it with view_set before view_screenshot.
- Provide 'setup' - the name of the setup to activate.
- ' still reads isActive=false - the setup did not become active.
- activate() ran but isActive cannot be read on '
- ', so the activation is UNCONFIRMED. Re-read the setups with cam_get.

### `cam_apply_template`
- Provide 'setup' - the name of the setup to apply the template to.
- Provide 'template_url' or 'template_name'.
- ' is not in a valid state to apply.
- createFromCAMTemplate2 ran but the setup's operation count did not increase (
- before and after) - no operations were added. The template may not be compatible with this setup.
- Invalid template URL: '
- No template found at URL:
- 'template_url' loads the template '
- ', but 'template_name' says '
- ' - nothing was applied. Pass the url alone to apply '
- ', or the name alone to search for '
- Failed to apply template:

### `cam_compare_operations`
- Provide both 'operation_a' and 'operation_b' (operation names).

### `cam_create_machine`
- Machine created and re-resolved through the query cam_edit_setup assigns from - the same read the cam_get(include=['machines']) catalog is built on. Assign it: cam_edit_setup(setup=..., machine='
- '). It persists in the local machine library until cam_delete_machine(name='
- Provide 'name' - the new machine's name. It becomes Machine.description, the label cam_edit_setup(machine=...) resolves an assignment by.
- This Fusion version's MachineTemplate has no '
- machine library reaches '
- ') - it matches that machine's
- . An assignment resolves by those keys, so pick another 'name'.
- Machine.createFromTemplate('
- ') returned nothing - no machine was created.
- Could not resolve the Local machine library location to save into.
- ' in the Local machine library returned no URL - the machine was not stored.
- importMachine returned a URL for '
- ' but no machine loads back from it - the create did not land.
- ' was stored in the Local machine library (
- ) but it does not resolve back through the query cam_edit_setup assigns from:
- The stored machine is still there.
- ) but that name resolves to '
- ' - an assignment would pick a different machine. The stored machine is still there.
- ' in the Local machine library failed:

### `cam_create_operation`
- No toolpath yet: select the geometry it cuts with cam_select_geometry, THEN compute it (cam_generate, or generate=true here). Generating before the geometry is selected leaves it reading valid with...
- ' isn't compatible with setup '
- The setup offers no compatible strategies at all.
- operations.add returned no operation.
- operations.add returned '
- ' but the setup's operation count could not be read
- the add, so the operation's landing is UNCONFIRMED. Re-read the setup with cam_get(include=['operations']).
- ' but the setup's operation count did not increase (
- after) - the operation did not land.
- ' but Operation.tool reads back null - it carries no cutting tool and cannot generate. Assign one with cam_edit_operation(tool_scope/tool_library_url, tool_index), or remove it with cam_delete.
- ' but Operation.tool reads
- , which does not name the requested
- - it carries a tool this call did not ask for. Re-assign it with cam_edit_operation(tool_scope/tool_library_url, tool_index), or remove it with cam_delete.
- Operation created but toolpath generation errored:
- Operation created; toolpath generation started (async). Poll it with cam_get_status(handle='
- '), or confirm with cam_get(include=['operations']) once generation completes.
- Provide a tool reference: 'tool_scope=document' + 'tool_index', OR 'tool_library_url' + 'tool_index' (from cam_edit_tools).
- Could not assign the tool to a '

### `cam_create_setup`
- Setup created (no operations yet). Add toolpaths with cam_apply_template (a COMPATIBLE template - a milling setup needs a milling template), then cam_generate. Be in the Manufacture workspace befor...
- No active design. Open or create a document first (see doc_new).
- No bodies to machine. The root component holds no bodies - add geometry first, or pass 'models' = body handles/names (a body inside a sub-component is not in the default set).
- Setup creation returned nothing.
- setups.add returned '
- ' but it does not appear when the setups are re-listed - the setup did not land.

### `cam_delete`
- Provide 'entity' - the CAM item name to delete (see cam_get / cam_get(include=['operations']) / cam_edit_folders).
- Fusion declined to delete '
- ' (deleteMe returned false). It may be locked, referenced, or not deletable in its current state.
- deleteMe returned true but '
- ' still resolves in the CAM tree - the delete did not take. Re-read with cam_get.
- CAM entity removed - verified gone by a re-resolve over the tree. (design_delete_* don't reach CAM - this is the CAM-side delete.)

### `cam_delete_machine`
- Machine deleted from the Local machine library: its asset '
- , is gone from a re-walk of the library's own assets. That is the whole claim - no setup was read here, so this says nothing about a setup that already carries this machine; cam_get's default setup...
- Provide 'name' - the machine to delete, as cam_get(include=['machines']) lists it.
- Provide 'confirm_name' - the machine's exact name again, as a safety confirmation. Machine deletion is not undoable from this server.
- This tool deletes from the LOCAL library only (the machines this Fusion install ships with are not yours to remove), so nothing was deleted.
- The Local machine library query failed, so whether '
- ' is reached from the local or fusion360 library could not be read.
- ' is reached from the
- machine library, not the Local one.
- Name mismatch - refusing to delete. '
- ' resolves to the machine '
- ', but confirm_name was '
- '. Pass confirm_name='
- ' if you really mean this machine.
- Could not resolve the Local machine library location, so the machine's asset cannot be addressed. Nothing was deleted.
- The walk hit its own bound, so this list is incomplete.
- Local asset file names:
- No asset in the Local machine library holds a machine named '
- assets in the Local machine library hold a machine named '
- ), and no ONE of them is filed as '
- ' either - refusing to guess which to delete. Remove the duplicate in Fusion's machine library first.
- The Local machine library walk hit its own bound before it finished, so '
- ' cannot be shown to name only ONE asset - a duplicate past the bound would not have been seen. Nothing was deleted.
- Fusion declined to delete '
- ' from the Local machine library (deleteAsset returned false) - it is still there.
- , so the delete could not be read back and is UNCONFIRMED. Re-read with cam_get(include=['machines']).
- deleteAsset returned true for '
- ', but the Local library
- location no longer resolves
- asset walk hit its own bound before finishing
- deleteAsset returned true but the Local machine library still lists '
- ' - the delete did not take. Re-read with cam_get(include=['machines']).
- deleteAsset returned true and the asset is gone, but '
- ' still resolves to the same LOCAL machine through the query cam_edit_setup assigns by - the delete did not take.
- ' from the Local machine library failed:
- ' still resolves to a LOCAL machine whose id cannot be read - neither can the deleted machine's, so nothing here tells them apart and the delete is UNCONFIRMED. Re-read with cam_get(include=['machi...

### `cam_delete_template`
- Provide 'name' - the template to delete, as cam_get(include=['templates'], template_location='local') lists it.
- Provide 'confirm_name' - the template's exact name again, as a safety confirmation. Template deletion is not undoable from this server.
- ' in the LOCAL template library - this tool deletes from the Local library only.
- Name mismatch - refusing to delete. '
- ' resolves to the template '
- ', but confirm_name was '
- '. Pass confirm_name='
- ' if you really mean this template.
- Could not resolve the Local template library location, so the template's asset cannot be addressed. Nothing was deleted.
- The walk hit its own bound, so this list is incomplete.
- No asset in the Local template library is named '
- assets in the Local template library (
- ) - refusing to guess which one to delete. Remove the duplicate in Fusion's template library first.
- The Local template library walk hit its own bound before it finished, so '
- ' cannot be shown to name only ONE asset - a duplicate past the bound would not have been seen. Nothing was deleted.
- The Local library asset '
- ' does not load a template, so what it holds cannot be confirmed. Nothing was deleted.
- ' holds the template '
- ' - refusing to delete an asset that is not the template that was confirmed.
- Fusion declined to delete '
- ' from the Local template library (deleteAsset returned false) - it is still there.
- , so the delete could not be read back and is UNCONFIRMED. Re-read with cam_get(include=['templates'], template_location='local').
- deleteAsset returned true for '
- ', but the Local template library
- location no longer resolves
- asset walk hit its own bound before finishing
- deleteAsset returned true but the Local template library still lists '
- ' - the delete did not take. Re-read with cam_get(include=['templates'], template_location='local').
- deleteAsset returned true and '
- ' is gone from the Local template library's asset walk, but a template still loads from its url (
- ) - the two reads disagree, so the delete is UNCONFIRMED.
- Template deleted from the Local template library: its asset '
- ' is gone from a re-walk of the library's own assets, and nothing loads from its url any more. cam_save_template writes a new one; cam_get(include=['templates'], template_location='local') lists wh...
- ' from the Local template library failed:

### `cam_edit_folders`
- Folders organise the operation tree. Create with action='create', move ops in with action='move'. (Patterns are created in the UI - the API won't add them.)
- Provide 'name' for the new folder.
- ' already exists in setup '
- ' did not take - addFolder returned a folder whose name reads back as
- Folder created and found in the setup's re-listed folders. Move operations into it with action='move'.
- Provide 'folder' (the existing folder) and 'new_name'.
- Could not rename folder '
- Each move was read back off the destination folder's own membership; 'operations' are the names it carries them under.
- Provide 'folder' (destination) and 'operations' (names to move into it).
- ' (move not allowed). (Moved so far:
- ' did not take - moveInto returned true, but the folder re-lists
- before this move, and the moved item reads its name back as

### `cam_edit_operation`
- Provide 'operation' - the CAM operation name to edit (see cam_get(include=['operations'])).
- Provide 'parameters' - at least one name=value to set (e.g. {'tool_feedCutting': '3000'}) - or 'preset', a preset on this operation's tool, 'tool_index' (with 'tool_scope=document' or 'tool_library...
- ' parameters cannot be read before assignment; no write was attempted. Re-read it with cam_get(include=['parameters']).
- ' has no parameter(s):
- . cam_get(include=['parameters'], operation=...) lists the rows Fusion SHOWS and counts the rest as hidden_count: a row behind a switch reads isEnabled false and is absent from that list, yet still...
- parameter(s), each restored expression re-read.
- ' parameters cannot be read after tool/preset assignment; no explicit parameter write was attempted.
- ' lost parameter(s) after tool/preset assignment:
- . No parameter write was attempted.

### `cam_edit_setup`
- Setup edited. Existing toolpaths are now OUT OF DATE - regenerate with cam_generate. A WCS bound via 'wcs' is a LIVE reference to the selected geometry or Joint Origin (bound_entities), so the WCS ...
- Provide 'setup' - the CAM setup name (see cam_get).
- Nothing to do. Provide 'parameters' {name: expression}, 'models'/'fixtures'/'stock' body lists, a 'machine', a 'stock_mode', a 'wcs' binding, and/or 'rename'.
- ' has no parameter(s):
- . (Read the setup's parameter names first; only existing ones are settable.)
- ' does not accept a write to:
- (isEditable reads False on each). Nothing was applied. A setup exposes many parameters it takes no write to; set one it does - cam_get(include=['parameters'], setup=...) marks each refusing row edi...
- parameter(s); no change was applied.
- ' and a 'stock' body list in one call set the setup's stock two ways - 'stock' is the from-solid mode with its bodies. Pass one or the other.
- This Fusion build's SetupStockModes carries no '
- ' member, so 'stock_mode=
- ' cannot be assigned. Pick another mode.
- Stock mode did not take on setup '
- ' but Setup.stockMode now reads '
- - the assignment did not take.
- Machine assignment did not take on setup '
- ' but the setup now reports '
- Could not set stock_mode='
- Could not assign machine '
- ' has no WCS mode parameter '
- bound no geometry - '
- ' reads back empty after the set. The handle may not be a valid WCS reference for this setup.
- Set the name of setup '
- ' but Setup.name does not read back, so the rename is UNCONFIRMED. Re-read it with cam_get. Every other change in this call was applied and is NOT rolled back.
- ' did not take - Setup.name still reads
- . Every other change in this call was applied and is NOT rolled back.
- Could not switch setup '
- ' to from-solid stock (SolidStock mode):
- Could not strip the simulation model from '
- Could not set WCS mode '
- Could not enable fixtures on setup '

### `cam_edit_tools`
- Cannot create a library in the document scope. Use scope=local/cloud/hub.
- Provide 'library' as the new library's name (create_library).
- Tool libraries unavailable.
- Could not resolve the '
- Could not create an empty tool library.
- importToolLibrary returned a URL but no library loads back from it - the create did not land.
- Library created and persisted. List it with action='list'. (Local=disk, Cloud/Hub=your Autodesk account; a duplicate name gets a numeric suffix.)
- No hub folder to create the library in.
- Each seed entry must be {library_url, index}; got
- Could not read the sample tool libraries - tool types are unavailable.
- Pass one of these as add_tools[].from_type to clone a sample of that type.
- Provide 'add_tools' - entries to add. Each: {from_type:'drill'} (create from a sample of that type) or {library_url, index} (copy an existing tool); optional 'description'/'diameter'/'product_id'/'...
- Auto-assigned tool numbers
- but after the add they read back
- - the tool-number assignment did not persist.
- Auto-assigned free tool number(s)
- (next free per tool, so multiple adds do not collide - cam_post refuses duplicate tool numbers).
- updateToolLibrary reported success but the library re-read from its url holds
- - the persist did not land.
- Auto-assigned tool number(s)
- but the library re-read from its url holds numbers
- - the assignment did not reach the stored library.
- Provide 'remove_indices' - the tool indices to remove.
- Index/indices out of range (library has
- Provide a valid 'tool' index (0..
- Provide 'parameters' {name: expression} to set on the tool.
- Tool has no parameter(s):
- . (Read the tool's parameters first.)
- : expression did not evaluate -
- parameter(s) and did not commit, so the library keeps what it held.
- (A tool expression must reference parameters this tool carries and resolve to a value - check names and units.)
- ' but the tool re-read from the library holds '
- ' - the edit did not persist.
- The tool already has a preset named '
- ' - remove it first (action='remove_preset') or pick another name.
- Could not add a preset to the tool at index
- Added a preset named '
- ' but the tool's presets read back as
- - the add did not take.
- The tool has no preset named '
- '. Presets on this tool:
- presets on this tool (indices
- ) - the removal is refused rather than picking one of them.
- ) reported failure - preset '
- preset(s) of that name survive on the tool - the removal did not take.
- 'where_used' is only available for the document library (scope='document') - a shared library has no operations.
- Operations that use this tool.
- This tool is not used by any operation.
- Every parameter's name/expression/value (value is null where unreadable). 'formula_source' marks a parameter tracking another (editing it overwrites that relationship). Set one with action='edit'.

### `cam_generate`
- Generation launch returned no future (nothing to generate?).
- Omit 'target' to generate the whole document.
- Failed to launch generation for
- Pass skip_valid=false to force-regenerate it.
- No generation launched in
- : every operation outside the
- that read isGenerationAllowed false failed to launch (

### `cam_generate_setup_sheet`
- Setup sheet written. The file is named after the DOCUMENT, not the scope, so another call into this folder OVERWRITES it - use a distinct output_folder per sheet you want to keep.
- Setup sheet written OVER an existing sheet of the same name (the file is named after the document, not the scope).
- Provide 'output_folder' - the directory the setup sheet will be written to.
- adsk.cam.SetupSheetFormats is unavailable on this Fusion version.
- Fusion declined to generate the setup sheet (returned false) for
- ' - nothing was written.
- generateSetupSheet returned true but no
- s - the generation did not complete, so there is no deliverable to report.
- Could not create output folder '
- Omit 'scope' to sheet the whole document.
- Setup-sheet generation failed:

### `cam_get`
- Operation rows capped at
- . Pass 'setup' to scope to one setup, or add 'default' to include for the per-setup operation_count.
- rows; 'setup' scopes it, strategy_count is the true total.
- include=['parameters'] needs 'operation' - the operation whose settings to read (scope first with cam_get(setup=..., include=['operations'])); or 'setup' alone for that SETUP's own parameters (stoc...
- include=['tool'] needs 'operation' - the operation whose tool to read.
- '. Valid: mm, cm, in.
- ' on this tool. Presets on this tool:
- This tool carries no presets at all, so '
- ' names none. cam_edit_tools(action='add_preset') authors one on a library tool.
- preset(s) but none of their names read, so '
- ' cannot be matched against them.

### `cam_get_status`
- No generation with handle '
- . Omit 'handle' to read live document state, or pass 'target' (a setup/operation name) to read an inline generation by name.
- 'latest' resolves to handle '
- ', which is not registered - a generation is dropped from the registry once it completes. Active handles:
- . Omit 'handle' to read the ACTIVE document's live state, or pass 'target' (a setup/operation name) to read an inline generation by name.
- cam_get(include=['operations']) for per-op detail.
- No operation in scope has generating left to do.
- Still generating in the background - check again later.
- Generation complete (
- The per-op tallies could not be read (
- ), so this rests on the generation Future alone - cam_get for the job's health.

### `cam_inspect_toolpaths`
- The toolpath validity check returned
- , not a true/false verdict - there is no verdict to report.

### `cam_post`
- Post did not report clean success - review before running.
- ' posted AS-IS from its stored configuration -
- file(s), nothing reconfigured. 'readiness' carries its health.
- file(s). 'readiness' carries its health.
- Provide 'program_name' - the NC Program name or number (some posts require a number).
- An as-is post uses the program's stored output units; omit 'units' or pass units='document' before posting it unchanged.
- Provide 'output_folder' - the directory where the NC file(s) will be written.
- Pass 'scope' or 'setups', not both - 'scope' names ONE setup/folder/operation and 'setups' names the several setups one program holds.
- No valid toolpaths to post - every operation is out-of-date, errored, or ungenerated. Run cam_generate (in the Manufacture workspace) first. (
- Omit both to post the whole document.
- ' already exists, but its stored operations cannot be compared with what 'scope' resolves to:
- requested operation(s) have no readable operationId, so whether reconfiguring would overwrite a machinist-curated program is unknown. Omit 'scope', 'setups', 'post', and 'output_folder' to post it ...
- ' already exists and its stored operations differ from what 'scope' resolves to - reconfiguring would overwrite a program that may be machinist-curated. Omit 'scope', 'setups', 'post', and 'output_...
- ' output folder to post as-is against - configure it once with 'output_folder' and 'post'.
- (the API returned null).
- The NC Program has no '
- ' parameter, so the output folder could not be set to '
- '. Unresolved parameters:
- Post processing raised:
- ; check the post matches the machine/operations.)
- Omit 'scope' to post the whole document.

### `cam_reorder`
- Provide 'entity' (to move) and 'reference' (to move it relative to).
- '. Use 'before' or 'after'.
- 'entity' and 'reference' are the same item - nothing to reorder.
- ' resolve to the SAME item (at '
- ') - nothing to reorder.
- ' was not allowed (e.g. moving an operation out of its setup, or across incompatible parents (setup or folder)).
- Fusion allowed the move, but the order could NOT be read back here: '
- '), which a parent holds in separate collections, so the two share no ordered list. Read the sequence with cam_get(include=['operations']).
- Fusion allowed the move, but the order could NOT be read back here:
- ' collection to re-read. Read the sequence with cam_get(include=['operations']).
- ' was allowed but did not land: re-reading the collection under
- , where the requested move leaves
- CAM item reordered - 'order' is the sibling collection under
- , re-read after the move and matching the requested placement, with the moved item at 'entity_index'.
- Fusion allowed the move and the collection under
- re-reads as the order the requested placement leaves, but WHICH item landed was not measured: that collection carries
- ', which the row of names cannot tell apart. Read the sequence with cam_get(include=['operations']).

### `cam_save_template`
- Provide 'template_name' for the new template.
- Provide 'setup' - the setup containing the operations.
- Provide 'operations' - a comma-separated list of operation names to bundle.
- Operations not found in '
- createFromOperations returned nothing.
- createFromOperations did not yield a usable CAMTemplate (got
- ). The operation set may not be templatable together, or this Fusion build's API returns an unexpected shape - please report.
- The created template is not in a valid state (the operation set may not be templatable together).
- ' is not available in this Fusion build.
- Could not resolve the '
- importTemplate returned no URL (save may have failed).
- importTemplate returned a URL but no template loads back from it - the save did not land.
- The template was stored at
- but it loads back as '
- ' - the saved template is not the one this call named. Re-read with cam_get(include=['templates']).
- New template saved. Verify with cam_get(include=['templates']) (which reports each template's asset URL). This tool always creates a NEW template; overwriting an existing one is a separate capability.
- Could not read operations in '
- Could not build template from operations:
- Failed to save the template:
- Could not create destination folder '

### `cam_select_geometry`
- Selection applied; generation is launched - check cam_get_status(target='
- ') until completed=true. has_toolpath False on completion means no path was produced, and the warning channel can be silent there - check the heights and the selection.
- Selection applied; pass generate=true (or cam_generate) to compute the toolpath.
- Selection applied but generation failed to launch:
- . The selection is saved - fix the cause, then run cam_generate(target='
- selection must be one of
- selection='pocket_recognition' is disabled by default because native pocket recognition can terminate Fusion. Pass allow_pocket_recognition=true for an explicit diagnostic attempt; use selection='p...
- '. Use mm, cm, or in.
- Selection applied but the operation reports 0 selections - the geometry was rejected. Check the geometry matches the strategy (edges for chain, the pocket floor face for pocket, bodies for silhouet...
- chain_groups requires selection='chain' and replaces handles.
- chain_groups must contain nonempty lists of edge handles.
- No cylinder faces left after the diameter filter.
- ' already separates each reference; use handles here.
- chain_groups did not resolve one edge per handle; refresh the handles.
- Multiple chain handles need chain_groups: one list per contour. Wrap connected edges in one group; separate disconnected contours. No heights or selections were changed.

### `cam_set_nc_comment`
- Provide a non-empty 'comment' (and/or 'set_name') - the value(s) to write. Refusing: an empty comment with no name would blank the comment on every matched NC program.
- This document has no NC programs.
- No NC program named '
- NC program comment/name updated. Most posts emit the Comment near the top of the G-code. (No re-post is performed.)
- ' parameter; aborting before any change.
- Comment on NC program '
- ' is not editable; aborting before any change (nothing was modified).
- ' is not editable/found; aborting before any change (nothing was modified).
- Failed to set comment on NC program '
- . NOTE: any programs processed before this one were already changed.
- Failed to set name on NC program '

### `cam_show_toolpath`
- Toolpath shown. Toolpaths render in the Manufacture workspace; pair with view_screenshot.
- operation(s) did not read back isLightBulbOn=false after the hide.
- Only this folder's generated toolpaths are shown.
- operation(s) did not read back isLightBulbOn=true after the show - see toggle_failures.
- operation(s) did not read back isLightBulbOn=false after the hide - see hide_failures; their toolpaths may still be drawn.
- Provide 'operation' - the operation name to
- Use cam_show_toolpath(list) to see every operation.
- isLightBulbOn did not take for '
- This operation has no generated toolpath yet - nothing to display. Generate it first (cam_generate).
- Provide 'folder' - the folder or setup name to show.
- Use cam_show_toolpath(list) or cam_get(include=['operations']).

### `data_create_folder`
- Provide 'folder_name'.
- Provide 'project' (name) or 'project_id'.
- ' already exists at '
- Folder creation returned nothing for '
- dataFolders.add returned a folder but '
- ' does not appear when '
- ' is re-listed - the creation did not land.
- Could not access project root folder:
- Could not prepare parent path '
- Failed to create folder '

### `data_create_project`
- Provide 'name' for the new project.
- ). Use a different name.
- Project creation returned nothing for '
- dataProjects.add returned a project but '
- ' does not appear when the projects are re-listed - the creation did not land.
- Failed to create project '

### `data_delete_file`
- The file's reference state could not be read (
- failed) and force=true deleted it anyway, so whether other files referenced it - and are now orphaned - is unknown; 'was_referenced_by' is null, not empty.
- Provide 'document_id' (the lineage URN of the file to delete).
- Provide 'confirm_name' - the exact current name of the file, as a safety confirmation. Get it from data_get or doc_get.
- No file found for document_id '
- '. It may already be deleted. Verify with data_get.
- Name mismatch - refusing to delete. document_id resolves to '
- ', but confirm_name was '
- '. Pass confirm_name='
- ' if you really mean this file.
- ' is currently OPEN - close it before deleting (Fusion will not delete an open document).
- ' is referenced by other files could not be read (
- failed), so it is NOT provably unreferenced - deleting it may orphan references this call cannot list. Refusing. Retry once the file reads (data_get(file=<urn>)), or pass force=true to delete WITHO...
- . Deleting it would orphan those references. Pass force=true to delete anyway (Fusion may still reject it).
- Fusion declined to delete '
- ' (it may be referenced or open). No change was made.
- findFileById failed for '

### `data_delete_folder`
- failed), so what went with it is unknown - 'contained_files'/'contained_subfolders' are null, not zero.
- The folder's contents could not be read before the delete (
- Provide 'folder_id' (the id of the folder to delete; from data_get(include=['folders'])).
- Provide 'confirm_name' - the exact current name of the folder, as a safety confirmation. Get it from data_get(include=['folders']).
- No folder found for folder_id '
- '. It may already be deleted. Verify with data_get(include=['folders']).
- Refusing to delete a project ROOT folder.
- Name mismatch - refusing to delete. folder_id resolves to '
- ', but confirm_name was '
- '. Pass confirm_name='
- ' if you really mean this folder.
- Fusion declined to delete folder '
- '. No change was made.
- findFolderById failed for '
- ' could not be read (
- failed), so it is NOT provably empty - it may hold an entire subtree this delete would remove irreversibly, and no blast-radius preview can be built. Refusing. Retry once the folder reads (data_get...
- ' to delete it WITHOUT a census. Nothing was deleted.
- Delete failed for folder '
- ' is not empty (immediate files:
- ). Deleting it RECURSIVELY removes its ENTIRE subtree:
- subfolder(s) total - and bypasses the per-file reference-orphan check.
- Pass force=true AND recursive_confirm='
- ' to do this, or empty it first (data_delete_file for files).
- ' (a deliberate second acknowledgment). Nothing was deleted.
- RECURSIVE DELETE of '
- ' would remove its ENTIRE subtree:
- subfolder(s) - and bypasses the per-file reference-orphan check (nested referenced files would be orphaned). This is irreversible.
- To proceed, pass recursive_confirm='

### `data_download_file`
- Downloaded synchronously (Fusion was frozen for the transfer) and gated on a non-empty file landing on disk - see size_bytes.
- Provide 'destination_folder' - the LOCAL folder to write the file into.
- ' is Fusion-native data (.
- ) and cannot be downloaded: DataFile.download handles only non-Fusion files. Open it (doc_open) and export instead - design_export for a design, drawing_export for a drawing, mesh_export for a mesh.
- The cloud file's name could not be read - pass 'file_name' to choose the local filename explicitly.
- 'file_name' must be a bare filename, not a path: '
- '. The folder comes from 'destination_folder'.
- ' already exists. Pass overwrite=true to replace it, or set 'file_name'. (Refusing keeps a stale file from being reported as this download's result.)
- Could not create download staging directory in '
- Could not create destination folder '

### `data_get`
- Active hub + its projects. Pass project=<name|id> to list its FILES (add 'folder' to scope, or include=['folders'] for the tree); 'file'=<name|URN> reads ONE file's full record. include=['hubs'] li...
- One file's record: metadata, version state and LINK state. Dates are UNIX epoch seconds with the UTC ISO string beside each. 'file_extension' is unreliable for a non-CAD upload - the file NAME carr...
- Files in the project (each with its lineage URN + openable fusionWebURL). 'folder'=<path> scopes to one folder; include=['folders'] shows the folder tree instead; 'file'=<name|URN> reads ONE file's...
- All hubs (is_active flags the current one). Switch with data_switch_hub - it CLOSES every open document. Then pass project=<name> to list files.
- Folder tree under 'folder' (the whole project when none is given). Drop include=['folders'] to list a folder's FILES instead.
- does not apply to the 'file' scope (it reads one file's record in full). Drop 'file' to use include, or drop include.

### `data_get_upload_status`
- Upload complete - the cloud confirms the file has fully landed and processed. Use file_id with doc_open or data_get.
- No uploads have been launched in this session. Call data_upload_file first.
- Provide 'handle' (from data_upload_file's upload_handle, or 'latest') or 'file_name' to look up an upload.
- Upload failed. Check the source file's format/permissions and retry data_upload_file.
- No upload with handle '
- No tracked upload matches file_name='
- Still transferring the file to the cloud - poll again.
- File transfer finished; the cloud is still processing it (e.g. translating a neutral format into a Fusion design) - poll again.

### `data_move_file`
- Verified by re-resolving the file and reading its parentFolder back. A project's ROOT folder reports the PROJECT's name, so a move to '/' shows that name.
- Provide 'target_folder' - the destination folder PATH inside the file's own project (e.g. 'Parts/Fixtures'), or '/' for the project root.
- Could not read the project root folder that '
- ' lives in - the move destination cannot be resolved against its project.
- ' could not be resolved: the folders inside '
- ' could not be read, so whether '
- ' exists is unknown. Nothing was moved - re-check with data_get(project=<name>, include=['folders']) and retry.
- ' does not exist in project '
- . This tool creates nothing - make the folder with data_create_folder first.
- ' resolved but neither its id nor its name could be read, so a move into it could not be verified afterwards. Refusing to move unverifiably - re-check with data_get(project=<name>, include=['folder...
- DataFile.move returned false for '
- ' - Fusion declined the move to '
- move() reported success for '
- ' but its parent folder could not be re-read, so the move is UNCONFIRMED. Re-check with data_get(project=<name>, include=['folders']) before moving it again.
- move() returned true for '
- ' but it still reports parent folder '
- ' - the move did NOT take. Reporting failure rather than a success the data model does not show.
- ' - nothing was moved.
- ' but its new parent folder and the target share no readable identity to compare on, so the move is UNCONFIRMED. Re-check with data_get(project=<name>, include=['folders']).

### `data_switch_hub`
- '. Use: list, switch.
- Data not available (not signed in?).
- Provide 'hub' - the name or id of the hub to switch to (see action='list').
- . Switch hubs from the Fusion data panel (the hub dropdown), then retry.
- Could not switch to hub '
- ): the re-read after the assignment shows the active hub as
- - the assignment raised:
- Active hub switched. This CLOSES documents open before the switch (Fusion reloads the data context). Re-list projects with data_get, and re-resolve any URNs - they are hub-scoped. Reopen the docume...
- ' is already the active hub - nothing to do.

### `data_upload_file`
- Provide 'file_path' - the full path to a local CAD file.
- File not found on disk:
- Provide 'project' (name) or 'project_id' for the destination.
- Upload returned no future object.
- ' reports FAILED immediately - the file was not accepted. Check the format and the destination folder.
- Upload is asynchronous and processes on the cloud (neutral formats like STEP are translated into a Fusion design). Poll data_get_upload_status(handle=upload_handle) for the actual uploading/process...
- Could not access project root folder:
- Upload failed to start for '
- Destination folder path not found: '
- '). Folders available at '
- . Pass create_path=true to create missing folders, or use data_get(include=['folders']) to see the structure.
- Could not prepare destination path '

### `design_activate_component`
- No active design. Create or open a document first (see doc_new).
- Occurrence.activate() returned false for '
- ' - could not make it the active edit target.
- activate() returned true for '
- ' but the design reads activeOccurrence=
- and isRootComponentActive=
- - the activation is not confirmed on that instance.
- This component is now the active edit target - sketch_create / model_extrude / sketch_dimension build into it. Activate 'root' (or '') to return to the root.
- Design.activateRootComponent() returned false - the edit target did not return to the root component.
- activateRootComponent() returned true but the design reads isRootComponentActive=
- and activeOccurrence=
- - the edit target is not confirmed at the root.
- Root component is the active edit target - new geometry builds at the root.

### `design_add_instance`
- '. Fusion numbers a new instance from a per-component counter across the whole design, so use the landed name/full_path, not a predicted one. The instance SHARES the component's geometry - editing ...
- '. Use mm, cm, or in.
- No active design. Open or create a document first (see doc_new).
- Could not reach the component behind '
- Refusing to instance '
- ' itself or sits inside it, so the component would contain an instance of itself. Pick a target outside it (omit 'into_component' for root).
- : whether that target already sits inside '
- ' could not be determined - the subtree could not be searched to a verdict, so the instance could make the component contain itself. Check for unresolved external references with assembly_get, then...
- Could not access the occurrences of
- addExistingComponent returned nothing - no instance of '
- addExistingComponent returned an occurrence for '
- ' but it reads isValid=false - the instance did not land.
- ', but the assembly census that confirms it could not be read - the instance may or may not have landed. Re-read with design_get(include=['tree']).  **[hedge]**
- The call reported an occurrence for '
- ' but no new instance appeared in the assembly tree. Re-read with design_get(include=['tree']).
- ' has no component to instance into.
- Could not reach the root component to instance into.
- Unknown rotate_axis '
- Could not build the placement rotation (
- ') - the instance was not created.

### `design_configure`
- No active design. Open or create a document first.
- The active design is not yet a configured design. Run action='create' first.
- Save the document first (doc_save_as), THEN run create. Converting an unsaved document builds the table only in memory - it won't materialize as a configured design (no DataFile to carry it, and th...
- createConfiguredDesign() returned no table.
- Design converted to a configured design (one configuration so far). Add configurations and columns with the other actions. To see it in the UI: SAVE, then REOPEN by the URN that save reports - the ...
- Design is already a configured design; reusing its configuration table. Add configurations with action='add_configuration' and columns with add_parameter / add_suppress / add_visibility / set_appea...
- Configuration switched + rebuilt. Pair with view_screenshot to view it, or design_get(include=['timeline']) / param_get to see what changed.
- Provide 'name' - the configuration to activate (a configuration name or id).
- No configuration matched '
- Activating configuration '
- ' failed (activate() returned false).
- activate() returned true but the active configuration still reads '
- ') - the switch did not take.
- Provide 'name' for the new configuration (e.g. 'Large').
- A configuration named '
- Adding configuration '
- Configuration row added, and adding it ACTIVATED it - 'active_configuration' is what the design shows now; switch back with action='activate'. Set its values via add_parameter/add_suppress/add_visi...
- Provide 'name' (the existing configuration) and 'new_name' (what to call it).
- No configuration named '
- ' did not take (the API may still be persisting a recent save - retry shortly).
- Configuration renamed. Address it by the new name from now on.
- Provide 'parameter' - the name of a model parameter to vary across configurations.
- '. (Add/expose it first; a parameter column only matters if the parameter drives geometry.)
- Values reference configurations that don't exist:
- parameter: its cells read the expression quoted ('10'), with the value only on textValue. This tool sets and verifies plain expressions, so it does not configure a Text column. Vary a length/number...
- addParameterColumn for '
- Parameter column added and per-configuration expressions set. Switch with design_configure(action='activate', name=...) - the geometry rebuilds only if this parameter drives a dimension.
- No cell for configuration '
- Relabel per configuration with param_set after activating the row.
- The column has been rolled back.
- The column could NOT be auto-removed and is still on the table - delete it before retrying.
- after the set - the expression '
- ' did not verifiably take
- Provide 'feature' - the timeline feature name to suppress per configuration.
- suppressed_in names unknown configurations:
- addSuppressColumn for '
- Suppress column added; the feature is suppressed in the listed configurations (present in the others).
- No suppress cell for configuration '
- ' does not read suppressed after the set - the suppression did not verifiably take.
- Provide 'body' - the body name whose visibility varies per configuration.
- hidden_in names unknown configurations:
- addVisibilityColumn for '
- Visibility column added; the body is hidden in the listed configurations.
- No visibility cell for configuration '
- ' does not read hidden after the set - the hide did not verifiably take.
- Provide 'body' - the body to color per configuration.
- Appearance map references unknown configurations:
- This design has no appearance table.
- appearanceTable.columns.add for '
- The appearance table holds
- theme rows after adding, not the
- this call needs - one per configuration named in 'appearances'. Name fewer configurations, or add the theme rows in the UI first.
- The appearance table has no theme column (parentTableColumn) to link configurations.
- Appearance theme column added and configurations linked to theme rows. Switch configurations to see the color change (design_configure(action='activate', name=...)).
- No appearance named '
- ' in the design. Copy it in first (design.appearances.addByCopy) - appearance_set copies the Fusion Appearance Library's 'Paint - Enamel Glossy (White)' and keeps it in the document as 'MCP Neutral...
- No appearance cell/row on theme row
- Appearance cell on theme row '
- after the set - the assignment did not verifiably take.
- No theme cell for configuration '
- after the set - the theme link did not verifiably take.
- Material column added for this body and each listed configuration linked to its own theme row (calling this again for the same body updates that column rather than adding a second one). The names a...
- Provide 'body' - the body whose physical material varies per configuration.
- Provide 'materials' - {configuration_name: material_name} for at least one configuration.
- Material map references unknown configurations:
- This design has no material table.
- materialTable.columns.add for '
- The material table has no theme column (parentTableColumn) to link configurations.
- No material cell on theme row '
- ' for configuration '
- Material cell for configuration '
- ' reads back no material after setting '
- ' - the assignment could not be confirmed.
- ' after the set - the material '
- ' after the set (expected '
- ') - the theme link did not take.
- Adding a theme row for configuration '
- ' is not in the material table after adding it.
- ' moved to a new theme row, but column '
- ' did not carry its material over - the material table is partially built; inspect it before retrying.
- Provide 'insert_part' - the configured part to insert (lineage urn or its name in the active project).
- Could not find a configured part '
- It must be saved in the SAME project as this assembly.
- ' is not a configured design - use a normal insert for a non-configured part. (Only configured parts get an insert column.)
- ' exposes no configuration rows.
- insert_map references assembly configurations that don't exist:
- insert_map references part configurations that don't exist:
- ' is not a configuration of '
- ) returned no occurrence (same-project requirement, or the part isn't accessible).
- addInsertColumn returned null.
- Configured part inserted and an insert column added: each listed assembly configuration now selects the mapped part configuration (nested config). Switch with design_configure(action='activate', na...
- No insert cell for assembly configuration '
- ' still selects part configuration '
- ' after the set - the mapping did not take.

### `design_delete_feature`
- Timeline feature deleted. Geometry it produced is removed; instances it created (pattern/mirror copies) go with it. Pair with design_get(include=['timeline']) / workspace_orient to confirm.
- Remove FEATURE deleted - the occurrence it had taken out is back in the assembly. Confirm with design_get(include=['tree']).
- deleteMe() reported success for '
- ', but its absence is UNVERIFIED:
- , so the re-read cannot prove the object is gone - and a check that could not read is not a check that found nothing. Nothing was rolled back; re-read design_get(include=['timeline']) to see what i...
- Provide 'feature' - the timeline object name to delete (see design_get(include=['timeline'])).
- No active design (open a document with design geometry).
- This design has no timeline (a direct-modelling design has no deletable timeline features). Delete bodies/occurrences directly instead.
- ' is a timeline GROUP, which has no deletable entity. Ungroup it (or delete its member features) instead.
- ' has no associated entity to delete (it may be a group or an unsupported timeline object).
- Fusion declined to delete '
- ' (deleteMe returned false). It may be depended on in a way that blocks deletion.
- deleteMe() reported success but the timeline still carries
- ' - as many as before the delete (
- ), so it was NOT removed. Nothing was rolled back; re-read design_get(include=['timeline']) to see what is actually there.
- ' names a RemoveFeature in
- ) - refusing to guess which one this timeline object belongs to.

### `design_delete_occurrence`
- Occurrence deleted. If it was the last instance of its component, the component was removed too. Pair with workspace_orient / design_get(include=['tree']) to confirm the assembly.
- deleteMe() reported success for '
- ', but its absence is UNVERIFIED: the occurrence walk did not carry '
- ' even before the delete, so the re-read cannot prove the instance is gone - and a check that could not read is not a check that found nothing. Nothing was rolled back; re-read design_get(include=[...
- No active design with components.
- Fusion refused to delete '
- ': deleteMe() returned false, which carries no reason. Read design_get(include=['timeline']) to see which feature built this instance - an instance a pattern/mirror feature owns is removed by editi...
- deleteMe() reported success but '
- ' is still in the assembly's occurrence walk - it was NOT deleted. Nothing was rolled back; re-read design_get(include=['tree']) to see what is actually there.

### `design_edit_timeline`
- No active design (open a document with design geometry).
- This design has no timeline (a direct-modelling design keeps no history), so there is no marker to move and nothing to group.
- Could not read markerPosition, so what lies after the marker is unknown - nothing was deleted.
- Nothing lies after the marker: it is at
- (the end of the timeline). Roll it back first with action='roll'.
- Refusing: this DISCARDS
- timeline item(s) after the marker at
- - and the features and geometry they produced. Pass confirm_delete_after_marker=true to proceed. Nothing was deleted.
- Fusion declined to delete after the marker (returned false); the timeline still holds
- deleteAllAfterMarker reported success but the timeline still holds
- - nothing was discarded.
- Those items and their geometry are gone. Undo in Fusion if unintended - the API cannot restore them.
- deleteAllAfterMarker failed:
- Items after the marker are rolled back - they are not computed and their geometry is absent until the marker returns. Roll to='end' when done.
- action='roll' without a 'feature' takes to='beginning'/'end'/'next'/'previous' (got '
- ' - that one names a position relative to a 'feature').
- The marker move to the
- but markerPosition cannot be read back, so nothing confirms it.
- Fusion declined to move the marker to the
- (returned false); it is still at
- reported success but markerPosition is
- action='roll' with a 'feature' takes to='before' or to='after' (got '
- Fusion declined to roll the marker
- ' (rollTo returned false); markerPosition is still
- Moving the marker to the
- step reported success but markerPosition is still
- rollTo reported success but markerPosition is still
- ' does not report isRolledBack - the roll is unconfirmed.
- rollTo reported success but '
- ' reads isRolledBack=
- A suppressed item is skipped when the model rebuilds; its geometry is absent until it is unsuppressed with suppressed=false.
- . A downstream feature consumed what this one produced - set suppressed=false to restore it.
- action='suppress' needs 'feature' - the timeline object to suppress or unsuppress (from design_get(include=['timeline'])).
- Setting isSuppressed=
- ' did not take - it reads
- Could not set isSuppressed on '
- Remove the group with action='ungroup' - its items are kept.
- action='group' needs 'feature' (the first item) and 'end_feature' (the last item) - the range to group, from design_get(include=['timeline']).
- Could not read the timeline index of '
- ', so the range to group is unknown.
- ' - pass 'feature' and 'end_feature' in timeline order.
- A timeline group cannot hold another group, and
- . Remove it with action='ungroup' first, or pick a range without it.
- This timeline exposes no timelineGroups collection to add to.
- Fusion returned no group for timeline items
- - nothing was grouped.
- add() returned a group but timelineGroups still holds
- - the group did not land.
- overlap the expanded group '
- ), and a timeline item can belong to only one group. Remove it with action='ungroup' first, or pick a range clear of it.
- Grouping timeline items
- action='ungroup' needs 'feature' - the name of the timeline group to remove (from design_get(include=['timeline'])).
- This timeline exposes no timelineGroups collection.
- No timeline group named '
- ) - rename one in Fusion, so the target is unambiguous.
- Fusion declined to remove timeline group '
- ' (deleteMe returned false);
- deleteMe reported success but timelineGroups still holds
- The group is gone; the items it held stay in the timeline, expanded. Delete an item itself with design_delete_feature.
- Removing timeline group '
- The attribute is attached to the entity the timeline item wraps, not to the timeline item. Remove it with action='delete_attribute'.
- 'attribute_value' takes a string; got
- characters; this tool carries at most
- . Store the bulk elsewhere and tag a reference to it.
- ' reads no attribute '
- ' back after adding it - nothing was attached.
- ' after setting it to '
- ' - the value did not take.
- Attaching attribute '
- ' no longer reads attribute '
- deleteMe returned false, but '
- ' back - the attribute is gone.
- ' raised, so whether it is there cannot be told and nothing was deleted.
- ' carries no attribute '
- ', so nothing was deleted. Attach one with action='set_attribute'.
- but reading it back raised, so nothing confirms it is gone - the delete is UNCONFIRMED. Run this same delete_attribute call again: a refusal naming '
- ' as absent is the attribute being gone.
- but itemByName still returns it (value '
- ') - it was not deleted.

### `design_export`
- Exported to local disk. To round-trip into the cloud, upload it with data_upload_file (STEP/IGES are translated to a Fusion design on the cloud).
- ') applies to format=stl only, and this call asked for format=
- - refusing rather than dropping it. Export as stl to bake the unit into the file, or omit 'stl_units'.
- ) applies to format=stl only, and this call asked for format=
- - refusing rather than dropping it. Export as stl to choose binary or ASCII, or omit 'stl_binary'.
- ') and split_by_component=true cannot be combined: the split writes one file per TOP-LEVEL occurrence and would not narrow to that target - refusing rather than dropping it. Omit 'target' to split ...
- Provide 'file_path' - the local output path (a file, or a DIRECTORY when split_by_component=true). The format extension is appended if missing.
- No active design to export. Open or create a document first (see doc_new).
- component(s) to separate
- files. Each top-level occurrence is one file - ready to print/assemble individually.
- takes no occurrence, so each file was written from that occurrence's COMPONENT.
- top-level occurrence(s) exported to separate
- produced NO file - see 'failed'.
- ' not found. Pass a body/component NAME, an occurrence fullPathName (e.g. Bracket:2 - the precise way to pick one instance), or omit 'target' to export the whole design.
- ') does not apply to format=dxf, which writes the 2D geometry named by 'dxf_sketch' or 'dxf_face' - refusing rather than dropping it. Name the sketch or face instead, or omit 'target'.
- 'split_by_component' (true) does not apply to format=dxf, which writes one sketch or face to one file - refusing rather than dropping it. Call design_export once per sketch/face, or omit 'split_by_...
- The root component's occurrences did not read, so which components this split would write one file each for is unknown - refusing rather than reporting a zero-file export. Export without split_by_c...
- No top-level occurrences to split - the design has no component instances. Export without split_by_component to write the whole design as one file.
- split export wrote NO files - all
- occurrence(s) failed:
- ' (true) applies to the 3D formats only, and this call asked for format=dxf, whose source is one named sketch or face - refusing rather than dropping it. Export as a 3D format, or omit '
- Could not create output directory '
- Provide 'file_path' - the local .dxf output path.
- Pass only one of 'dxf_sketch' or 'dxf_face' for format=dxf, not both.
- format=dxf needs either 'dxf_sketch' (a sketch NAME) or 'dxf_face' (a find_geometry planar-face handle) to know what 2D geometry to write.

### `design_get`
- . 'max_depth'/'component'/'name_filter'/'max_results'/'tree_bodies'/'tree_handles' scope the tree; 'group'/'include_suppressed'/'timeline_params' the timeline; 'library'/'name_filter'/'max_results'...
- Orientation slice. Pull deeper with include=
- contents.occurrences_walk='unreadable': NEITHER root.allOccurrences nor the component.occurrences fallback enumerated, so the occurrence count is missing because it is UNKNOWN, not because the desi...
- No active design. Open or create a document first (see doc_new).
- Component/occurrence not found: '
- Could not read root occurrences:
- This design has no timeline (direct-modeling, or no history):
- Could not read the timeline:
- The active design is not a Configured Design (it has no configuration table) - e.g. a design with Variant A/Variant B style options.
- include=['attributes'] needs 'attribute_group' - the group to read (the same group design_edit_timeline(action='set_attribute') wrote with). Leave 'attribute_key' empty to get every key in that group.

### `design_move_occurrence`
- '. A move keeps the part's WORLD position (measured) - it changes where the instance sits in the browser tree, not where the geometry is. Every path beneath it changed too, so re-read with design_g...
- No active design. Open a document first (see doc_open / doc_new).
- 'into_component' is required (an occurrence whose component receives the instance). This call cannot move an instance back to the TOP LEVEL: the API moves an occurrence into another OCCURRENCE, and...
- ' has no component to move into.
- Could not read the component behind '
- ' - refusing to move it.
- : that target is its own component '
- ' or sits inside it, so the component would contain an instance of itself. Pick a target outside it.
- ' already sits inside that target could not be determined - its subtree could not be searched to a verdict, so a move there could make the component contain an instance of itself. Check the target ...
- moveToComponent returned nothing - '
- moveToComponent ran for '
- ', but the assembly census that confirms it could not be read - the move may or may not have taken. Check with design_get(include=['tree']) before acting on this result.  **[hedge]**
- The move reported success but the assembly is unchanged - '
- '. Re-read with design_get(include=['tree']).

### `design_recompute`
- Full recompute done; downstream features rebuilt.
- . Inspect with design_get.
- Recompute ran and surfaced
- feature error(s) not present when it started:

### `design_remove_feature`
- Remove is a TIMELINE feature - suppress it (design_edit_timeline) or delete it (design_delete_feature) to bring the item back. Pair with design_get(include=['tree']) to confirm the assembly.
- No active design. Create or open a document first (see doc_new).
- Provide EITHER 'body' ('
- ') OR 'occurrence' ('
- ') - one Remove feature takes one item. Call the tool twice to remove two things.
- Provide 'body' (a find_geometry handle or a body name) or 'occurrence' (a handle or fullPathName from design_get(include=['tree'], tree_handles=true)) - the item to remove.
- , which is what the removal is verified against - nothing was changed.
- Could not read the component that owns the
- to remove - nothing was changed.
- ' exposes no removeFeatures collection - the Remove feature is unavailable here.
- - the collection this tool re-scans to confirm a removal - could not be read, so the effect could not be verified. Nothing was changed.
- Refusing to remove: the
- ' is not visible in the collection this tool re-scans to confirm a removal, so the effect could not be verified. Re-read the target with design_get(include=['tree'], tree_handles=true) / find_geome...
- removeFeatures.add ran for '
- ', but the collection re-scan that confirms it could not be read - the removal may or may not have taken. Check with design_get(include=['tree']) before acting on this result.  **[hedge]**
- Remove reported success but '
- ' is still present in '
- after) - treat the removal as failed.
- Remove failed (removeFeatures.add raised):

### `design_set_mode`
- The design mode does not read back after the assignment, so the conversion is UNCONFIRMED - 'converted' and 'history_discarded' are null. Re-read with design_get(include=['mode']) to see what the d...
- No active design. Create or open a document first (see doc_new).
- 'target' must be one of:
- Converting to DIRECT destroys the timeline and all design history (irreversible). Re-call with confirm_history_loss=true to proceed.
- Re-run design_get(include=['mode']) to see the updated capability map.
- Assignment did not take - design is still
- . Nothing was converted and no history was discarded.

### `design_set_name`
- 'new_name' is required - a non-empty name to give the target.
- No active design. Open a document first (see doc_open / doc_new).
- ' but the name could not be read back, so the rename is unverified. Check the browser (design_get(include=['tree'])).
- The rename did not take -
- ' after setting the name to '
- Could not reach the component behind
- ' is the ROOT component and Fusion refuses to rename it ('root component name cannot be changed') - its name IS the document name. Rename the document instead (doc_save_as), or target a body/sub-co...
- ' is this design's ROOT component could not be read, and the platform's refusal to rename the root aborts the enclosing transaction even when it is caught - so this rename was not attempted. Target...
- already holds the name '

### `doc_activate`
- Switch ACCEPTED but not yet active - activation is async and hasn't propagated. Call doc_get to confirm it took before acting on the new document.
- Provide 'name' - the open document to activate (a display name, or a lineage URN / web URL to be unambiguous).
- Activate failed for '

### `doc_close`
- No document was closed.
- . Fusion keeps at least one document open.
- discarding unsaved changes
- No documents are open.
- . No document was closed.
- No active document to close.

### `doc_copy`
- The copy preserves external references: each referenced component still points at its ORIGINAL source file - the references are not re-copied. This tool does not offer a Document.saveAs-based copy ...
- Provide 'document_id' (lineage URN, preferred) or 'name'.
- Provide 'project' (name) or 'project_id' for the destination.
- Destination project not found:
- Copy was not attempted. Retry after the folder is readable or copy into a different 'folder'.
- Copy into a different 'folder'
- ' already exists in '
- ). Copy into a different 'folder'
- Copy returned nothing for document '
- No file found for document_id '
- '. Pass the file's lineage id (URN) from data_get.
- When using 'name', also provide 'source_project' (name) or 'source_project_id' so the lookup is unambiguous.
- Source project not found:
- Could not access the root folder of source project '
- ) to skip the walk, or narrow it with source_folder='<path>'.
- ). The walk is bounded because each folder is a slow cloud fetch on Fusion's main thread. Pass document_id (the lineage URN, from data_get
- By-name search stopped at its budget: visited
- ' without covering it (
- . Refusing because an unread file name or folder can hide a same-name twin. Retry after the folders are readable, pass document_id (URN), or narrow the search with source_folder='<path>'.
- By-name source search could not completely read
- ; matches read so far:
- . Use data_get, or pass document_id (URN).
- files share it in project '
- . Fusion allows same-name files in different folders; refusing rather than copying the wrong one. Pass document_id (the lineage URN above) to copy one exactly.
- Could not access destination project root:
- Copy failed for document '
- findFileById failed for '
- source_folder path not found: '
- '. Folders at project root:
- . Use data_get(include=['folders']) to see the structure.
- Destination folder path not found: '
- '). Folders at project root:
- . Pass create_path=true, or use data_get(include=['folders']) to see the structure.
- Could not prepare destination path '

### `doc_get`
- No active document. Open or create one first (doc_open / doc_new).
- active is the focused document; document_id is its cloud lineage URN. document_handle addresses the exact open document, saved or unsaved: use it with doc_activate, doc_close and expect_document. I...
- numbers_may_lag true: the tip is under
- s old and these numbers may trail the cloud - re-read; null: its date did not read. A row the collection lists whose flag reads false is is_milestone=true + flag_lagging=true. is_milestone null, mi...
- The active document has no cloud DataFile (never saved to the cloud); no version history exists. Save it first (doc_save_as).
- Covers three link kinds: kind='xref' (referenced occurrences), kind='derive' (derive features) and kind='unresolved' (an occurrence whose referenced component could not be loaded). all_current is a...
- No active Design (the active product is not a design); the xref walk needs a design document.
- The active design has no root component.
- references = documents that USE this one (drawings made from it, parent assemblies that insert it); the mirror of include=['xref_tree'] (what this design consumes). query_complete is authoritative ...
- The active document has no cloud DataFile (never saved to the cloud); it cannot be referenced by anything yet. Save it first (doc_save_as).
- parentReferences could not be read (permission/cloud read failure); the where-used relationship is UNKNOWN, not empty - do not conclude nothing uses this document.

### `doc_insert_derive`
- One-way linked COPY of the source's last SAVED cloud version - unsaved in-session edits in the source are NOT derived (save the source, then doc_update_xref). Edits made here (a fillet, a patch, an...
- Provide 'document_id' - the lineage URN (or web URL) of the saved cloud document to derive.
- No active design. Open or create the host document first (see doc_new).
- ' to a saved document. Tried:
- . Pass a lineage URN or web URL (from data_get). The document must be SAVED to the cloud.
- The source document '
- ' is not open. This tool derives from an ALREADY-OPEN source (Fusion loads documents asynchronously - it cannot load one within a single call). Open it first: doc_open(file_id='
- ', force_api_open=true), confirm it loaded with workspace_orient, then retry - the derive reuses the loaded source.
- ' has no Design product to derive from (not a Fusion design file?).
- has no deriveFeatures collection (unexpected).
- deriveFeatures.createInput returned nothing - the source design could not be prepared for derive. The source may not be fully loaded yet; confirm it with workspace_orient (re-open with doc_open if ...
- (deriveFeatures.add returned nothing.)
- Derive was created but FAILED to compute:
- Derive was created but its documentReference reads isOutOfDate=true immediately at creation - the link did not land against the resolved version.
- Derive created a feature but nothing landed - no bodies appeared and no new derived occurrence. The link may not have resolved; check the source scope.
- Derive created a feature and geometry appeared, but nothing reports isDerived=true - the one-way link may not have formed correctly.
- ' has no component to derive into.
- Could not configure the derive:
- ' to receive the derive (Occurrence.activate() returned false). Nothing was derived.
- Derive landed at the ROOT component (
- - the target activation did not take, so the nesting failed. The derive EXISTS at root: delete its feature (design_delete_feature) and retry, or keep it and move on.

### `doc_insert_import`
- file_path is required - the full path to a CAD file on this machine's disk.
- file cannot be imported to a new document - importToNewDocument does not accept DXF or SVG options. Import into the open design instead (new_document=false): DXF creates sketches in a component, SV...
- No readable file at '
- '. Pass a full path on THIS machine's disk; a cloud file must be downloaded first, or referenced with doc_insert_occurrence.
- Application.importManager is unavailable - nothing can be imported.
- No active design to import into. Open or create a document first (see doc_new), or pass new_document=true.
- import reported no failure but nothing landed in
- : importToTarget2 returned no objects and the component gained no body and no occurrence. Read the design back with design_get(include=['tree']) - the geometry may have landed elsewhere.
- Imported as solid/surface geometry. An assembly file lands as sub-occurrences, a single part as bodies. Inspect it with design_get(include=['tree']) and pick faces/edges for the model tools with fi...
- importToNewDocument returned null, which the API reports for a FAILED import - no document was created.
- A new document was opened for '
- ' but it carries no Design product to read the imported geometry back from. The document is open
- - inspect it with workspace_orient.
- A new document was created for '
- ' but holds no body and no occurrence - the import landed nothing.
- The new document is UNSAVED and is now the active document. Save it with doc_save_as to give it a cloud identity, or discard it with doc_close.
- Import failed (importToNewDocument raised):
- No sketch to import the SVG into. SVG curves land in an EXISTING sketch - make one with sketch_create, then name it in 'sketch'.
- createSVGImportOptions returned nothing for '
- ' - the file could not be prepared as SVG.
- The SVG import reported no failure but sketch '
- ' gained no curves (still
- ) and importToTarget2 returned no objects. The file may hold no path geometry.
- SVG curves landed in the sketch at 1/96 inch per SVG unit (measured: a 96-unit square lands 25.4 mm), with SVG's y-down axis landing as NEGATIVE sketch y. Measure one curve with model_measure_betwe...
- . SVG imports into an EXISTING sketch - make one with sketch_create.
- createDXF2DImportOptions returned nothing for '
- ' - the file could not be prepared as DXF, or the plane is not a construction plane / planar face.
- The DXF import reported no failure but no sketch landed in
- : importToTarget2 returned no objects, DXF2DImportOptions.results is empty and the component gained no sketch. A DXF holding only 3D geometry imports nothing - a 2D import ignores it.
- One sketch per DXF layer that carries 2D geometry, named after the layer. Read the curves with sketch_get, then extrude a profile with model_extrude.

### `doc_insert_occurrence`
- Provide 'document_id' - the lineage URN (or web URL) of the saved cloud document to insert.
- No active design. Open the host document first.
- ' to a saved document. Tried:
- . Pass a lineage URN or web URL (from data_get). The document must be SAVED to the cloud.
- '. Use mm, cm, or in.
- addByInsert returned nothing (the insert did not produce an occurrence).
- addByInsert returned an occurrence but it reads isValid=false - the insert did not land.
- Insert landed but the occurrence is NOT an external reference (isReferencedComponent=false) - the associative link did not form. Confirm the source and host share a project, then retry.
- Inserted at the requested placement, from the source's last SAVED cloud version. bound_version is the version this reference holds - null where none read it or two disagree; bound_is_tip whether it...
- ' has no component to insert into.
- Unknown rotate_axis '
- ) was refused - the placement rotation could not be built, so nothing was inserted or removed.
- Failed to remove existing occurrence '
- ' (deleteMe returned false). It may be referenced/locked.
- . (An external reference requires the source and host in the SAME PROJECT - save the host into the source's project, then retry.)

### `doc_new`
- New blank design created. Use document_handle with doc_activate/expect_document; it expires on close/add-in reload. Check is_active before modelling; save with doc_save_as.
- New-document creation returned nothing.
- Failed to create a new design document:

### `doc_open`
- Provide 'file_id' - a DataFile id or URL from the data-model tools: a lineage 'id', a 'versionId', or a 'fusionWebURL'/'source_url'.
- doc_open needs you to DECLARE INTENT. Pass force_api_open=true to open a NORMAL document via the API, OR is_cam_template=true if this is a multi-reference CAM/Manufacture template (the tool then in...
- . Pass a DataFile 'id'/'versionId' or a 'fusionWebURL' from data_get / design_get(include=['tree']) / cam_get(include=['references']) (it may not exist or you may lack access).
- This is declared a multi-reference CAM template. Opening it (or even resolving its references) via the API crashes Fusion, so the API open is refused. Open it MANUALLY in the Fusion UI (Data Panel ...

### `doc_restore_version`
- promoted to latest; a new tip version
- now carries its content (history is preserved). Reopen/reload the document to see it in-session.
- No active document to restore a version of.
- The active document has no cloud DataFile (never saved to the cloud); there is no version history to restore. Save it first (doc_save_as).
- Specify which version to restore: pass version_number (an integer) or version_id.
- promote() returned false restoring version
- ; the restore did not take effect.
- promote() returned true, but this document's latestVersionNumber could not be read BEFORE the call - so whether a new tip appeared is not decidable here (the read after the call reports
- ). Confirm with doc_get include=['versions'].
- promote() returned true but the latest version has NOT advanced after
- s of re-reading the cloud file (latest reads
- ) - no new tip carrying version
- 's content was observed. Confirm with doc_get include=['versions'] before promoting again.
- in this document's history. Available version numbers (newest-first):
- The cloud tip advanced to
- while this call re-read it, and
- is not in the refreshed history. Available version numbers (newest-first):
- promote() raised while restoring version
- is already the latest version; nothing to restore -

### `doc_save`
- Document.save returned true; confirmation fields report local completion and observed cloud version state.
- No active document to save.
- The active document has never been saved (no cloud file yet). Use doc_save_as to give it a name and folder first.
- Fusion declined to save '
- Document had no unsaved changes - nothing to version.
- THIS SAVE MOVED THE DOCUMENT TO A NEW LINEAGE URN. Address the file by lineage_changed.to from now on - lineage_changed.from opens the file this one forked from, and its version history does not co...

### `doc_save_as`
- The saved document becomes the active document. Its 'document_id' is the lineage URN - the stable identity to address it by (doc_open/doc_activate/data_delete_file); a NAME can be shared by several...
- NAME CENSUS INCOMPLETE - allow_duplicate_name authorized the save despite an incomplete pre-save collision check.
- Provide 'name' for the saved document.
- Provide 'project' (name) or 'project_id' for the destination.
- No active document to save. Open a document first.
- Destination project not found:
- doc_save_as cannot verify that the destination name is free. Retry after the folder is readable, choose another folder, or pass allow_duplicate_name=true only if creating a same-name fork is intent...
- doc_save_as would add yet ANOTHER file of that name (a new lineage) - refused by default. To add a version to one of the files above, open that URN (doc_open) and use doc_save; to create a same-nam...
- ' already exists in '
- ). doc_save_as would FORK a SECOND file with the same name (a new lineage) - refused by default. To add a version to the EXISTING file, open it by that URN (doc_open) and use doc_save; to deliberat...
- saveAs reported an error but the file DID land in the destination (verified by reading the saved document/folder back) - reporting success rather than a false negative, which would send a retry int...
- Fusion declined to save '
- ' (saveAs returned false). The complete name readback did not establish whether this call landed, so no success is reported.
- NAME COLLISION - see 'name_collision'.
- Could not access destination project root:
- saveAs returned false for '
- ', and the destination name census was incomplete, so whether a file landed is unconfirmed:
- A different file named '
- ' already existed in this folder (
- ); this saveAs created a SECOND file with the same name (a new lineage - Fusion allows this). To add a version to the EXISTING file instead, open it (doc_open by that URN) and use doc_save; or dele...
- Destination folder path not found: '
- '). Folders at project root:
- . Pass create_path=true, or use data_get(include=['folders']) to see the structure.
- ), and the destination name census was incomplete, so whether a file landed is unconfirmed:
- ); this saveAs added another one (a new lineage - Fusion allows this). To add a version to one of them instead, open that URN (doc_open) and use doc_save. Address files by URN, not name, from here.
- Could not prepare destination path '

### `doc_save_milestone`
- Provide 'milestone_name'. Fusion accepts an empty name and invents one, but an unnamed milestone cannot be found by name in the version history afterwards.
- No active document to milestone.
- The active document has never been saved to the cloud (no DataFile), and saveMilestone cannot create one. Save it first with doc_save_as, then milestone the next change.
- ' has no unsaved changes. On an unmodified document saveMilestone reports success but creates NO version and NO milestone, so this call is refused instead of returning a false success. This tool on...
- saveMilestone returned false for milestone '
- '; no version and no milestone were created.
- saveMilestone returned true, but
- . Cloud version advancement is unknown. Read doc_get include=['versions'] before retrying.
- Cloud version advancement was not observed within
- s of re-fetching; confirmation remains pending. Read doc_get include=['versions'] before retrying.
- Fresh cloud reads confirmed version advancement and milestone '
- '. Read doc_get include=['versions'] for the version history.
- . Milestone confirmation remains pending; read doc_get include=['versions'] before retrying.
- Fresh cloud reads confirmed version advancement, but
- saveMilestone raised saving '

### `doc_update_xref`
- No external reference named '
- '. References in this document:
- Some references failed to update:
- This document has no external references (occurrence xrefs or derive links).
- DIFFERENT source files referenced by this document:
- . Fusion allows same-name files in different folders, so refreshing them all could pull a version you did not ask for - refusing. Omit 'name' to refresh every out-of-date reference, or inspect the ...

### `drawing_add_sketch`
- Nothing to draw on: the active document is not a drawing. Open the drawing and make it active (doc_open, or the Fusion UI), then retry.
- ' exposes no sketches collection - cannot add a sketch to it.
- Adding a sketch to sheet '
- entities onto sketch '
- : Drawing.deleteEntities raises 'API Function not yet implemented' on a drawn curve, so delete the whole sketch in the Fusion UI if it is not wanted.
- curves, counted off its own collections. Coordinates were taken as
- , which the drawing STANDARD fixes - 'sheet_units' is the dimension display unit and does not move the geometry. Drawing.deleteEntities raises 'API Function not yet implemented' on a drawn curve, s...
- Could not add a sketch to sheet '

### `drawing_create`
- Drawing created as a CLOUD file (NOT opened). To reach it: doc_open(file_id, force_api_open=true), then drawing_export for the PDF - measured on 2705.0.87, a drawing never reviewed in the Fusion UI...
- creation_mode 'manual' requires template_file, which was empty.
- This call stops here without creating anything. Pass the template's DataFile id/URL as template_file, or use creation_mode 'automatic'.
- No active design to draw. Open or create a design first (see doc_new), then retry.
- DrawingManager is unavailable in this Fusion session - cannot create a drawing.
- createDrawingInput returned null - Fusion could not start a drawing from this design.
- createDrawing returned null - Fusion did not generate a drawing (nothing created).
- createDrawing returned a drawing DataFile but no file_id could be read from it, so the created drawing cannot be located for export. Treating this as a failure.
- size but standard is '
- ) or switch the standard.
- portrait orientation is not supported for the largest
- '); use landscape or a smaller sheet.
- sheet_size 'custom' requires both custom_width_mm and custom_height_mm.
- custom_width_mm and custom_height_mm must be positive (got
- custom_width_mm/custom_height_mm only apply when sheet_size='custom'.
- ' could not be resolved to a file. Tried:
- . Pass a DataFile id/versionId or a fusionWebURL from data_get / design_get(include=['tree']).
- sheet_types must be a list of sheet-type names (e.g. ['component', 'main_assembly']).
- sheet_types has unknown value(s)
- ' cannot be applied: adsk.drawing has no
- enum on this Fusion version (the namespace carries
- classes instead), so the setting has no API to reach and no drawing was created. Leave
- createDrawingInput failed:
- createDrawing failed:
- custom_width_mm and custom_height_mm must be numbers (got

### `drawing_dimension`
- Save the drawing with doc_save to keep them.
- Auto-dimensioned one view.
- The document's modified flag could not be read, so nothing here confirms the dimensioning took.
- No drawing to dimension: the active document is not a drawing. Open the drawing (doc_open by file_id) and make it active, then retry.
- ' has no views to dimension. Drawing views are created by the automatic generator (drawing_create) or in the Fusion UI - the API cannot add one.
- Provide 'view' - the index of the view to dimension, 0 to
- is out of range: sheet '
- view(s), so the legal indices are 0 to
- could not be read off sheet '
- ' - nothing to dimension.
- createAutoDimensionInput returned nothing - this sheet cannot be auto-dimensioned.
- Setting the view did not take - AutoDimensionInput.view reads back null after assigning view index
- , so the dimensioning would run on no view.
- autoDimension returned false for view index
- ' - Fusion placed nothing. Treating this as a failure.
- autoDimension reported success for view index
- but the document is still unmodified, so nothing was placed. Treating this as a failure.
- The document was ALREADY modified before this call, so the modified flag cannot confirm this dimensioning on its own.
- 'view' must be an integer view index (got
- createAutoDimensionInput failed:
- Could not set the view to dimension (index

### `drawing_edit_sheet`
- The active document is not a drawing, so it has no sheets. Open the drawing (doc_open by file_id) and make it active, then retry.
- Provide 'sheet_size' - the preset size to give the sheet.
- Provide 'orientation' - landscape or portrait.
- Sheet added, inheriting its size and orientation from the ACTIVE sheet. 'sheets' reports collection positions; export/PDF order is unavailable. Export all sheets and inspect the PDF before selectin...
- The drawing's sheets could not be read - cannot add a sheet.
- Sheets.add returned nothing - no sheet was added.
- Sheets.add returned a sheet but the drawing still holds
- sheet(s) - the add did not take.
- Fusion refused the sheet add:
- Sheet.copy returned nothing for '
- '; its effect is unverified. The drawing may still be updating asynchronously. Re-read drawing_get before retrying, to avoid a duplicate copy.
- Sheet.copy returned '
- ' but the sheet count still reads
- ); its effect is unverified. Nothing was rolled back. Re-read drawing_get before retrying.
- Sheet copied; the facts come from the returned copy, which carries the SOURCE sheet's size, orientation, sketches and tables (not the active sheet's). Collection positions are listed; export/PDF or...
- ' is the only sheet this drawing holds (
- ) - refusing to delete it. Add a sheet first (action='add'), then delete this one.
- Fusion refused to delete sheet '
- ' (deleteMe returned false). The sheet is still there.
- Fusion accepted deletion (deleteMe=true); deleted=null until a later call confirms removal. This cannot be undone. The count and 'sheets_still_read' are observations inside this call; neither is a ...
- Provide 'new_name' - the name to give the sheet.
- ' but the sheet name could not be read back, so the rename is unverified.
- The rename did not take - the sheet still reads '
- ' after being set to '
- '. Sheet names are case-insensitively unique in a drawing: a name another sheet holds, or a case variant of it, is ignored. Pick another name.
- The sheet already holds the name '
- Could not rename sheet '
- This Fusion build has no sheet size '
- ' is not valid for this drawing: its standard reads
- , and Fusion rejects a size that does not belong to the active drawing standard. Choose one of the
- The size did not take - sheet '
- Sheet size set and read back - width and height follow the size and cannot be set directly. action='tidy_up' lays the sheet's views out again.
- Fusion refused sheet size '
- This Fusion build has no sheet orientation '
- Fusion does not support portrait orientation on the
- ' keeps its current orientation ('
- '). Set a smaller size first (action='set_size').
- The orientation did not take - sheet '
- Orientation set and read back - the sheet's width and height swap with it. action='tidy_up' lays the sheet's views out again.
- Fusion refused orientation '
- Sheet tidied (tidyUp returned true); the view COUNT does not change. The drawing is modified in-session but NOT saved - doc_save persists it, drawing_export shows the result.
- The document was already modified before this call, so the modified flag cannot confirm this tidy on its own - drawing_export is the check.
- Sheet.tidyUp returned false for '
- ' - Fusion did not tidy the sheet.
- Tidy up reported success for '
- ' but the document is still unmodified - nothing on the sheet changed.

### `drawing_export`
- Active drawing exported to local disk as
- Provide 'file_path' - the local output path for the drawing file. The
- extension is appended if missing.
- No drawing to export: the active document is not a drawing. Open a drawing first (drawing_create makes one; doc_open opens it by file_id), then export it as the active document.
- The drawing has no export manager - cannot export.
- This Fusion build's drawing export manager has no
- is not available here.
- export returned false - Fusion wrote nothing. Treating this as a failure.
- export reported success but
- s of the export call returning. Treating this as a failure, not a false success.
- export options could not be created:
- Could not create output directory '

### `drawing_get`
- collection_index is 1-based in the native collection; export_index is unknown. Export all sheets and inspect the PDF before choosing a page range. Width/height are mm; custom-size sheets read sheet...
- Unknown include value(s):
- The active document is not a 2D drawing. Activate the drawing document first (doc_activate), then read it.
- The drawing's sheet count could not be read, so sheet name '
- ' cannot be resolved. Retry drawing_get after the drawing finishes updating.

### `drawing_get_status`
- This is the stored operation state and raw terminal tool result. It does not add an effect verdict or replay unresolved work.
- Provide the request_key used for deferred drawing work.
- No durable drawing job has this request_key.

### `drawing_insert_image`
- Image placed on the sheet.
- The document's modified flag could not be read, so nothing here confirms the insert took.
- Provide 'image_path' - the local path of the image file to place.
- Unsupported image file '
- '. A sheet image is one of:
- Provide both 'x' and 'y' - the sheet position to place the image at.
- No drawing to place an image on: the active document is not a drawing. Open the drawing (doc_open by file_id) and make it active, then retry.
- The active drawing has no active sheet to place an image on.
- This sheet exposes no images collection - an image cannot be placed on it.
- Images.createInput returned nothing - no image can be placed on this sheet.
- Images.insert returned false for '
- ' - Fusion placed nothing. Treating this as a failure.
- Images.insert reported success for '
- ' but the document is still unmodified, so nothing was placed. Treating this as a failure.
- The document was ALREADY modified before this call, so the modified flag cannot confirm this insert on its own.
- 'x' and 'y' must be numbers in sheet units (got
- 'scale' must be greater than 0 (got
- Images.createInput failed:
- Could not set the image position:
- 'scale' must be a number (got
- 'rotate_deg' must be a number of degrees (got

### `drawing_update`
- Refresh request completed. 'references' and 'is_up_to_date' are the immediate readback; the write postcondition waits for stale references to settle. document_modified is the post-refresh native fl...
- reference(s) could not be read back (null rows) - their post-refresh staleness is unknown, so up-to-date is unverified.
- No drawing to update: the active document is not a drawing. Open the drawing (doc_open by file_id) and make it active, then retry.
- The drawing's document references could not be read, so its staleness cannot be determined - refusing to refresh blind.
- Drawing references are already up to date - nothing to refresh. Edit and SAVE the source design first, then this refreshes the drawing's views to match.
- reference(s) could not be read (null rows) - their staleness is unknown, so up-to-date is unverified. Every readable reference is current; nothing to refresh.
- The immediate readback still shows
- stale reference(s); the final verification waits for the refresh to settle.
- updateAllReferences failed:
- The immediate reference collection could not be counted, so up-to-date is unverified.

### `find_geometry`
- A match on a body that is not visible carries hidden:true.
A planar face's 'frame' is that plane in world space: the point at local (u, v) is frame.origin + u*frame.x_world + v*frame.y_world, and f...
- '. Use mm, cm, or in.
- No active design (open or create a document first).

### `joint_at_geometry`
- Verify with assembly_get (is_healthy + positions).
- Joint created AT the geometry.
- Joint creation returned nothing.
- Could not create joint input from the two geometries:
- Joint creation failed:
- . (The two geometries may be incompatible, or one part may be over-constrained.)
- Could not apply flip:

### `joint_create`
- Joint created as a timeline feature. View it with view_screenshot.
- No active design (open a document with assembly geometry).
- '. Valid: mm, cm, in.
- Provide 'occurrence_one' and 'occurrence_two' - each a Joint Origin name OR an autonomous geometry snap '<occurrence>:<snap>' (snap = origin/center/top/bottom/left/right/front/back/cylinder).
- Could not resolve joint input '
- createInput returned nothing for these inputs.
- setter returned false
- joints.add returned nothing.
- - it does not position the parts. It REMAINS in the timeline: remove it with design_delete_feature(name='
- '), or fix its inputs with joint_edit. A part locked by assembly_ground(ground_to_parent=true), itself or an ancestor, conflicts with a joint that would move it - read the state back with assembly_...
- ' WAS CREATED but FAILED to compute (health state:
- (it reports no message)
- Could not create joint input:
- Could not apply offset/angle/flip:
- Limits requested but this joint type has no motion to limit (rigid/inferred). Use revolute/slider/cylindrical.
- ' WAS CREATED, but a limit failed:
- Limits already applied before the failure:
- . Fix the limits with joint_edit(joint_name='
- ', ...) or remove the joint with design_delete_feature - do NOT re-create it.

### `joint_create_as_built`
- Occurrences joined where they already are with
- - an as-built joint moves neither part.
- Occurrences rigidly joined where they already are.
- No active design with components.
- As-built joint needs two distinct occurrences.
- ' needs 'geometry' - the anchor its motion runs on (a find_geometry handle, or '<occurrence>:<snap>' with snap = origin/center/top/bottom/left/right/front/back/cylinder). Fusion refuses a non-rigid...
- joint_type 'rigid' takes no 'geometry' - a rigid as-built joint locks the two occurrences with no anchor to move along, so '
- ' would be ignored. Drop 'geometry', or set joint_type to the motion you want at that geometry.
- asBuiltJoints.createInput returned nothing for these two occurrences.
- As-built joint creation returned nothing.
- The as-built joint was created as '
- ', not the requested '
- '. It remains in the design - remove it with design_delete_feature and retry.
- The as-built joint was created but its motion could not be read back, so '
- ' is unconfirmed. Check it with assembly_get before relying on the degree of freedom.
- 'geometry' resolved to the Joint Origin '
- '. An as-built joint anchors on a JointGeometry - real geometry (a face/edge/vertex handle, or an '<occurrence>:<snap>'). To joint AT a Joint Origin use joint_create.
- As-built joint input failed:
- motion on the as-built joint input:
- setter returned false
- As-built joint failed:
- The as-built joint WAS created but renaming it to '
- ' did not take - AsBuiltJoint.name still reads '
- '. Rename it in the browser, or remove it with design_delete_feature and retry with a different name.
- The as-built joint WAS created (Fusion named it '
- ') but renaming it to '
- . Rename it in the browser, or remove it with design_delete_feature and retry with a different name.

### `joint_create_origin`
- Joint origin created. frame_axes shows the resulting Z/X/Y directions. For an oriented frame: anchor='bbox_center' (Z = orient_axis) / 'face_center' (Z = face normal) / a sketch line. anchor='coord...
- No active design. Open or create a document first (see doc_new).
- '. Valid: mm, cm, in.
- Could not build joint geometry from the given anchor.
- createInput returned nothing for this geometry.
- jointOrigins.add returned nothing.
- Joint origin landed on component '
- ', not the requested '
- '. Rolled it back; nothing changed.
- 'component': occurrence '
- ' has no readable component to receive the joint origin.
- Could not create joint-origin input:
- Joint origin creation failed:
- Could not set the coordinate offsets on the joint origin:
- Coordinate offsets did not take: asked
- cm but the joint origin reports
- cm. Rolled the origin back; nothing changed.
- Joint origin landed at
- but the computed anchor was
- cm). Rolled the origin back; nothing changed.

### `joint_drive`
- Joint driven (the Drive Joints command). This pose is TRANSIENT: a recompute resets it unless captured, so call assembly_capture_position (action='capture') to write it into the timeline as a Posit...
- Provide 'angle_deg' (revolute/cylindrical) and/or 'distance' (slider/cylindrical) to drive the joint to.
- '. Use mm, cm, or in.
- No active design with components.
- '. Use assembly_get or design_get(include=['timeline']) to list joint names.
- - only revolute, slider, and cylindrical joints can be driven by value. (rigid has no value; for a ball joint pose the part with assembly_move.)
- ' is a slider - it has no rotation. Use 'distance', not 'angle_deg'.
- ' is a revolute - it has no slide. Use 'angle_deg', not 'distance'.
- Could not read the motion of joint '
- ' (a new token) clears this refusal.
- ' is motion-linked to '
- ', already driven this session, and the pair did NOT read as wholly native - an occurrence of one joint reads as a REFERENCED component, sits under one, did not answer isReferencedComponent, or did...
- . Fusion IGNORES an out-of-range drive (the value stays where it was), so nothing would move. Command a value inside the limits (a command exactly AT a bound lands on it), or widen them with joint_...
- Refused: the command lies beyond the enabled joint limits of '
- Could not drive joint '
- . Read the pose back with assembly_get.
- did NOT land the commanded value -
- . The assignments made before the failure (
- ) were accepted; no value was read back here, so where the mechanism stands now is not known from this receipt. Read the pose back with assembly_get.
- ' moved the placement of '
- mm but its body geometry did not move (
- mm) - the transform is a claim, the body corner is the evidence. Read the pose back with assembly_get.

### `joint_edit`
- Joint edited + recomputed, but the timeline still has errored feature(s) (
- ) - the edit may over-constrain something.
- '. Use design_get(include=['timeline']) or check the name.
- Posing a joint to a rotation value is joint_drive's job. Use joint_drive(joint_name=..., angle_deg=...) to drive it; joint_edit changes the joint definition (type/axis/snaps/limits), not its pose.
- '. Valid: mm, cm, in.
- Nothing to change. Provide at least one of: input_one/input_two, joint_type (+axis), world_axis, flip, offset (+units), angle, min_deg/max_deg/rest_deg (rotation), min_mm/max_mm/rest_mm (linear).
- Joint edited in place + full recompute (downstream features settled). view_screenshot to view.
- Joint edited in place, but the full recompute RAISED - downstream features may be unsettled and their health unread. Run design_recompute and check workspace_orient before trusting the model state.
- world_axis given but the joint's current motion type is not axis-based (rigid/ball have no single axis to re-point).
- Could not resolve input_one '
- Could not resolve input_two '
- to that Joint Origin: the Joint Origin is LATER in the timeline (position
- ) than the joint (position
- ). Editing a joint rolls the timeline to just before it, where a later feature does not exist yet. Create the Joint Origin before the joint, or delete the joint and recreate it after the Joint Orig...
- setter returned false
- Edits already applied before the failure:
- This joint has no offset parameter (rigid/inferred or already 0-DOF).
- This joint has no angle parameter.
- This joint has no editable motion (rigid/inferred has no limits).
- ' is an AS-BUILT joint, which exposes no offset parameter for ANY motion type - its position cannot be driven by a parameter or an expression. Delete it (design_delete_feature) and build the pair w...
- ' is an AS-BUILT joint, which exposes no offset/angle ModelParameter for ANY motion type - no expression can drive it. Delete it (design_delete_feature) and build the pair with joint_create instead.

### `joint_motion_link`
- Joints linked - value_one/value_two are the link's own parameters READ BACK after the set. Whether the link moves the partner is not claimed here: joint_drive ONE member and its receipt answers whe...
- Provide 'joint_one' and 'joint_two' - the two joints to link.
- joint_one and joint_two must be different joints.
- ratio must be non-zero (a 0 ratio links no motion).
- Motion link creation returned nothing - check that both joints permit motion (revolute/slider/cylindrical); a rigid joint cannot be linked.
- REMAINS in the design holding the pair its parameters read. Re-value it with assembly_edit_relations(kind='motion_link', name=..., action='set_values'), or remove it with action='delete'; assembly_...
- setMotionData reported success but
- (its name could not be read)
- ratio must be a number (got
- . Link two joints that permit motion (revolute/slider/cylindrical).
- Could not create the motion link:
- . (Two joints already coupled through the same kinematic chain cannot be linked - the platform refuses them here.)

### `mesh_combine`
- Mesh bodies combined ('enhanced' yields fewer triangles than 'legacy'). The edit lands on the COMPONENT, so EVERY instance carries it; a tool addressed '<occurrence>:<mesh>' lands where that occurr...
- No active design. Create or open a document first (see doc_new).
- REFUSED before combining: a
- needs the tool to OVERLAP the target, and
- ' (bounding boxes are separated; the gap is a lower bound). The API would report success while consuming the tool and changing nothing. Move the tool into the target (model_move) and combine again....
- This design has no meshCombineFeatures collection (mesh combine unavailable here).
- Combine reported success but the target mesh is unchanged (
- mesh bodies before and after) - the tool meshes may not overlap the target.
- reported success but the target mesh '
- triangles before and after) - the tool did not overlap it. The tool mesh was CONSUMED by the operation and could not be restored (mesh combine keeps no tools). The AABB pre-check cannot see overlap...
- A tool body is the same as the target - pick distinct mesh bodies (the target is combined INTO, the tools are combined FROM).
- meshCombineFeatures.createInput returned nothing.
- Could not create the mesh-combine input:
- Mesh combine failed (meshCombineFeatures.add raised):
- . (For cut / intersect the meshes must overlap; all must be MESH bodies.)

### `mesh_delete`
- No active design. Create or open a document first (see doc_new).
- Delete reported success but a mesh named '
- ' still resolves in component '
- ' - treat the delete as failed.
- Mesh body removed. (design_delete_feature / design_delete_occurrence don't reach mesh bodies - this is the mesh-side delete.)
- This design has no meshRemoveFeatures collection (parametric mesh delete unavailable here).
- meshRemoveFeatures.createInput returned nothing.
- deleteMe() answered false for mesh '
- ' - it was NOT deleted (
- ' resolve in component '
- Could not create the mesh-remove input:
- Mesh delete failed (meshRemoveFeatures.add raised):

### `mesh_export`
- ') applies to format=stl only, and this call asked for format=
- - refusing rather than dropping it. Export as stl to bake the unit into the file, or omit 'stl_units'.
- ') and split_by_component=true cannot be combined: the split writes one file per TOP-LEVEL occurrence and would not narrow to that target - refusing rather than dropping it. Omit 'target' to split ...
- Provide 'file_path' - the local output path (a file, or a DIRECTORY when split_by_component=true). The format extension is appended if missing.
- No active design to export. Open or create a document first (see doc_new).
- ' not found. Pass a body HANDLE from find_geometry (precise), a body/mesh/component/occurrence NAME, or omit 'target' to export the whole design.
- This design exposes no exportManager - cannot export.
- This build's ExportManager has no
- export is unavailable here.
- export returned false - nothing was written.
- export reported success but
- . execute() returned True but produced nothing - treating this as a FAILURE, not a false success. Check the target geometry and the output path are valid.
- The root component's occurrences did not read, so which components this split would write one file each for is unknown - refusing rather than reporting a zero-file export. Export without split_by_c...
- No top-level occurrences to split - the design has no component instances. Export without split_by_component to write the whole design as one file.
- split export wrote NO files - all
- occurrence(s) failed:
- export wrote no file for this MESH target: the redirect to its owning component (
- ) produced no file either. Convert the mesh with mesh_to_brep and export the resulting solid, or place it in a component that exports.
- Could not create output directory '

### `mesh_generate_face_groups`
- Face-group generation ran -
- . Convert with mesh_to_brep(method='prismatic').
- No active design. Open or create a document first (see doc_new).
- This design has no meshGenerateFaceGroupsFeatures collection (generate face groups unavailable here).
- mesh_generate_face_groups reported no error, but add() returned no feature and neither the mesh's face group count nor its group ids read before or after - nothing observed what this generation did.
- meshGenerateFaceGroupsFeatures.createInput returned nothing.
- adsk.fusion.MeshGenerateFaceGroupsMethodTypes is unavailable on this Fusion version.
- Could not create the face-groups input:
- Generate face groups failed (meshGenerateFaceGroupsFeatures.add raised):

### `mesh_get`
- These are MESH bodies (not BRep). Inspect one with model_inspect (it reports mesh stats on a mesh target), edit with mesh_reduce / mesh_remesh, or convert with mesh_to_brep. A mesh has no BRep face...
- No active design. Open or create a document first (see doc_new).
- List one instance's meshes by its occurrence name/fullPathName (design_get(include=['tree']) lists the instances), or pass target='' to scan the whole design.
- No component/occurrence named '
- '. List the tree with design_get(include=['tree']), or pass target='' to scan the whole design.

### `mesh_insert`
- Convert to BRep with mesh_to_brep to use find_geometry / fillet / CAM on it.
- Imported as MESH body(ies).
- Direct design - no base-feature scope needed.
- Wrapped in BaseFeature '%s' (parametric design requires it).
- 'name' was not applied: the import landed %d mesh bodies - rename each with design_set_name, using the names in 'bodies'.
- file_path is required - a full path to a .stl / .obj / .3mf file.
- Unsupported mesh file '
- '. Import needs one of:
- . (To import from the data model, first resolve the file to a local path with the data_* tools, then pass that path.)
- No active design. Open or create a document first (see doc_new).
- ' for mesh import. Use mm, cm, m, in, or ft.
- Mesh import returned no bodies (the file may be empty or unreadable as a mesh).
- Omit target_component to import into the ACTIVE component, and set which that is with design_activate_component (it takes the occurrence, so it can name one of them).
- ' to import into. Omit target_component to use the active component, or list components with design_get(include=['tree']).
- Mesh import failed (meshBodies.add raised):

### `mesh_plane_cut`
- Mesh cut by the plane. 'trim' keeps one side, 'split_body' makes two mesh bodies, 'split_faces' cuts the triangulation in place. fill controls the new opening (none / minimal / uniform). Use flip=t...
- No active design. Open or create a document first (see doc_new).
- This design has no meshPlaneCutFeatures collection (mesh plane cut unavailable here).
- Refusing before any cut: '
- . The plane does not straddle the mesh, and
- . Move the plane so it passes THROUGH the mesh (mesh_get reports the mesh's bounding box in display units; any clearance quoted here is in cm), then retry.
- 'plane': could not read the plane geometry off that face handle.
- meshPlaneCutFeatures.createInput returned nothing.
- adsk.fusion.MeshPlaneCutTypes is unavailable on this Fusion version.
- cut ANNIHILATED mesh '
- triangles were removed and the mesh body now reads 0 triangles, so no geometry of it is left.
- Could not create the mesh-plane-cut input:
- adsk.fusion.MeshPlaneCutFillTypes is unavailable on this Fusion version.
- Mesh plane cut failed (meshPlaneCutFeatures.add raised):
- cut changed nothing: mesh '
- triangles, unchanged.
- Move the plane into the mesh (mesh_get reports its bounding box), then retry.

### `mesh_reduce`
- No active design. Open or create a document first (see doc_new).
- For target=proportion, 'value' is a percent in (0, 100].
- For target=max_deviation, 'value' must be a positive length (in 'units').
- This design has no meshReduceFeatures collection (mesh reduce unavailable here).
- Reduce reported success but the triangle count did not decrease (
- ). The mesh may already be at/below the target; treat it as unreduced.
- 'value' must be a number.
- For target=face_count, 'value' must be a positive integer face count - got
- For target=face_count, 'value' must be a WHOLE face count -
- is not an integer. Truncating it here would silently decimate to a different (or zero) target, so pass the exact integer you mean.
- meshReduceFeatures.createInput returned nothing.
- Could not create the mesh-reduce input:
- Could not configure the mesh-reduce input:
- Mesh reduce failed (meshReduceFeatures.add raised):

### `mesh_remesh`
- Triangle count is unchanged (
- . Read the mesh back with mesh_get before building on it.
- No active design. Open or create a document first (see doc_new).
- This design has no meshRemeshFeatures collection (mesh remesh unavailable here).
- meshRemeshFeatures.createInput returned nothing.
- Could not create the mesh-remesh input:
- 'density' did not land: set
- . Re-run without 'density' for the default remesh.
- Mesh remesh failed (meshRemeshFeatures.add raised):
- 'density' did not take on this build:

### `mesh_repair`
- Mesh repaired. Re-read the body with mesh_get.
- Nothing changed: the mesh reads the same before and after (
- found nothing of its kind to fix. A MeshBody exposes no defect count beyond is_closed, so this is reported as it was measured rather than judged.
- No active design. Open or create a document first (see doc_new).
- applies to repair_type='rebuild' only (got repair_type='
- ') - drop it, or switch repair_type to 'rebuild'.
- 'offset' applies to rebuild_method='accurate' only (got rebuild_method='
- This design has no meshRepairFeatures collection (mesh repair unavailable here).
- repair raised no error, but nothing could be read back off the mesh afterwards (triangle/vertex counts, is_closed and volume are all unreadable) - the repair is UNVERIFIED, so it is reported as a f...
- repair reported success but the mesh is unchanged (
- vertices) and is STILL not watertight - the holes it was asked to close are still there. Try repair_type='rebuild', or check the mesh with mesh_get.
- PARTIAL: the mesh changed but is still NOT watertight (is_closed false) - holes remain. Re-run close_holes or try repair_type='one_touch_fix', then check with mesh_get.
- 'density' must be between
- meshRepairFeatures.createInput returned nothing.
- 'density' must be a number between
- Could not create the mesh-repair input:
- Could not configure the mesh-repair input:
- Mesh repair failed (meshRepairFeatures.add raised):
- The rebuild was created with
- was requested - Fusion did not take the value. The mesh has been rebuilt at
- the API accepts, or undo in Fusion.

### `mesh_reverse_normal`
- Mesh normals flipped. is_closed and is_oriented do not move across a reverse, so they are not evidence - the flip reported here is the signed volume and the per-node normals. Pair with view_screens...
- No active design. Open or create a document first (see doc_new).
- This design has no meshReverseNormalFeatures collection (mesh reverse normal unavailable here).
- meshReverseNormalFeatures.createInput returned nothing.
- Mesh reverse normal raised no error, but neither the body's signed volume nor its per-node normals could be read back - the flip is UNVERIFIED, so it is reported as a failure. is_closed and is_orie...
- Mesh reverse normal reported success but '
- ' still points the same way - the signed volume kept its sign and the per-node normals are unchanged.
- Could not create the mesh-reverse-normal input:
- Mesh reverse normal failed (meshReverseNormalFeatures.add raised):

### `mesh_separate`
- shells. 'pieces' names them as Fusion auto-named them, read back from the component - a mesh feature reports no bodies of its own. Re-read them with mesh_get.
- No active design. Open or create a document first (see doc_new).
- This design has no meshSeparateFeatures collection (mesh separate unavailable here).
- meshSeparateFeatures.createInput returned nothing.
- Mesh separate raised no error, but the component's mesh body list could not be read back - whether the mesh was divided is UNVERIFIED, so it is reported as a failure. Check the component with mesh_...
- Could not create the mesh-separate input:
- Mesh separate failed (meshSeparateFeatures.add raised):

### `mesh_shell`
- Mesh hollowed in place - the same body, re-triangulated. Re-read it with mesh_get.
- No active design. Open or create a document first (see doc_new).
- This design has no meshShellFeatures collection (mesh shell unavailable here).
- meshShellFeatures.createInput returned nothing.
- Mesh shell raised no error, but nothing could be read back off the mesh afterwards (triangle and vertex counts and volume are all unreadable) - the hollow is UNVERIFIED, so it is reported as a fail...
- Mesh shell reported success but '
- ) - nothing was hollowed.
- ' NO LONGER watertight (is_closed went true -> false), so this is a loss of closure, NOT a hollow.
- Could not create the mesh-shell input:
- Could not set the shell thickness:
- Mesh shell failed (meshShellFeatures.add raised):
- The shell was created with thickness =
- was requested - Fusion did not take the value.
- The body does not report itself watertight after the shell, so a volume drop cannot be read as material coming out - the hollow is NOT confirmed. Check the body with mesh_get.
- The mesh changed but its enclosed volume did not drop (volume_change
- ), so the hollow is NOT confirmed by volume - check the body with mesh_get.
- The mesh changed but its enclosed volume could not be read at both ends, so the hollow is NOT confirmed by volume - check the body with mesh_get.

### `mesh_smooth`
- No active design. Open or create a document first (see doc_new).
- This design has no meshSmoothFeatures collection (mesh smooth unavailable here).
- meshSmoothFeatures.createInput returned nothing.
- Mesh smooth raised no error, but the mesh node coordinates could not be read back. Triangle and vertex counts hold still across a smooth, so there is nothing else to judge it on - the smooth is UNV...
- Mesh smooth reported success but every one of the
- ' is at its original coordinate - nothing was smoothed.
- 'smoothness' must be between
- Could not create the mesh-smooth input:
- Mesh smooth failed (meshSmoothFeatures.add raised):
- The smooth was created with smoothness =
- was requested - Fusion did not take the value.
- 'smoothness' must be a number between
- Could not set the smoothness:

### `mesh_to_brep`
- Converted to BRep - find_geometry / fillet / chamfer / CAM can now act on these bodies. 'prismatic' merges flat face groups (fewest faces); 'faceted' is one face per triangle (exact, heavy).
- No active design. Open or create a document first (see doc_new).
- This mesh is NOT watertight (is_closed=false), so it has no closed volume to convert to a solid. Repair it first with mesh_remesh (or fill the holes), then retry. Refusing up front so you don't get...
- method='organic' requires the Product Design Extension to be active - it is not available in this session. Use method='prismatic' (best for machined/scanned parts) or 'faceted' (exact, one BRep fac...
- This design has no meshConvertFeatures collection (mesh->BRep unavailable here).
- Mesh->BRep conversion did not produce a BRep body. The mesh may be non-watertight or too dense to convert.
- meshConvertFeatures.createInput returned nothing.
- Could not create the mesh-convert input:
- Could not configure the mesh-convert input:
- Mesh->BRep conversion failed (meshConvertFeatures.add raised):
- . A common cause is a non-watertight or very dense mesh.

### `model_arrange`
- Shapes arranged within the boundary. Pair with view_screenshot (top) to view the nest.
- '. Use mm, cm, or in.
- Unknown solver '%s'. Use 'true_shape' or 'rectangular'.
- No active design. Create or open a document first (see doc_new).
- ' for the boundary. Use sketch_get.
- ' has no closed profile to use as the envelope. Draw a closed boundary shape first.
- Provide 'shapes' - the occurrence name(s) to arrange (comma-separated).
- Provide 'shapes' - at least one occurrence to arrange.
- This design does not expose Arrange features.
- Check the boundary profile holds the shapes at this spacing.
- Arrange reported success but NOTHING happened - no input occurrence moved and no occurrence was added.
- The empty arrange feature was rolled back.
- The empty arrange feature could not be rolled back - remove it with design_delete_feature.
- Could not create the arrange input (solver may be unavailable).
- Could not set the boundary envelope from the sketch profile.
- ) appears to need a Fusion extension on this account:
- . Try solver='rectangular', or enable the extension.

### `model_base_feature`
- No scope was open in this session to close.
- captured open base-feature scope(s); design is now
- scope(s) did NOT confirm closed (
- ) - each may still be OPEN, which keeps the design reading direct and the timeline inaccessible. Their handles are KEPT (an open base feature can be reached no other way), so model_base_feature(act...
- No active design. Create or open a document first (see doc_new).
- 'action' must be one of:
- Base-feature edit OPEN - geometry from subsequent tool calls lands in this scope. While it is open the design READS as 'direct' and the timeline is inaccessible; that reverts on finish. ALWAYS pair...
- This component has no baseFeatures collection - cannot create a base feature here.
- BaseFeatures.add() returned nothing - could not create a base feature.
- Could not enter base-feature edit (startEdit returned false).

### `model_combine`
- Bodies combined. Pair with view_screenshot to view the result.
- '. Use: join, cut, intersect.
- No active design. Create or open a document first (see doc_new).
- No valid tool bodies resolved.
- A tool body is the same as the target - pick distinct bodies.
- . (Bodies must overlap for cut/intersect; all bodies must be solids in the same component.)
- Combine ran in a DIRECT design, which returns no feature object, and neither '
- ' body count nor the target's volume could be read back - so whether the bodies were combined is UNVERIFIED. Check with design_get(include=['tree']) / model_inspect.
- . For cut/intersect the bodies must overlap; confirm with design_get(include=['tree']) / model_inspect.
- Combine reported no error but nothing it could measure changed -
- ' measures the same volume (
- cm3) after the combine, which is what a tool that does not overlap the target produces. Tools:
- . Move the tool into the target (model_move) and combine again.
- Nothing was combined.

### `model_compute_holder`
- No active design. Open the holder model first (see doc_open).
- 'axis' must be a CYLINDRICAL or CONICAL face, or a straight EDGE - that handle doesn't define an axis of rotation. Use find_geometry(kind=cylinder_face / line_edge) on the holder.
- 'end_datum' must be a PLANAR face (or edge/vertex) NORMAL to the axis - that handle isn't a valid end datum for this axis. Pick the flat end face of the holder.
- No holder profile could be derived - no coaxial faces reduced to segments. Check that 'axis' is the true axis of revolution and the body is a turned holder.
- Holder profile computed (segments in mm: height, lower/upper diameter). This does NOT write to a tool library - take 'holder_json' and add it to a library yourself (a holder in a document is a FORK...
- Could not reduce the body to a holder profile:
- . (The body should be a solid of revolution about the chosen axis.)

### `model_construction`
- Construction datum created - snap joints/sketches to it (e.g. joint_create_origin).
- '. Use mm, cm, or in.
- '. Use: point, axis, plane.
- No active design. Create or open a document first (see doc_new).
- creation returned nothing.
- Could not add construction geometry:
- Could not add construction geometry: this datum mode isn't supported in the current modeling mode. Only a bare coordinate point or a world-axis-through-a-point needs DIRECT-modeling; every geometry...

### `model_create_component`
- . Activate it (or it is active) then model into it with sketch_create / extrude; ground / joint it as an assembly part.
- Empty component created
- '. Use mm, cm, or in.
- No active design. Create or open a document first (see doc_new).
- Could not access the target occurrences collection to create the component.
- Component creation returned nothing.
- Unknown rotate_axis '
- Could not create component:

### `model_draft`
- Faces tapered to the pull direction. Pair with view_screenshot to view.
- 'angle_deg' must be non-zero - a 0 deg draft tapers nothing.
- 'angle_deg' must be between -90 and 90 degrees (got
- No active design. Create or open a document first (see doc_new).
- Draft feature was created but failed to compute:
- . Try a smaller angle, 'flip', or a different pull direction.
- 'angle_deg' must be a number (draft angle in degrees).
- deg (setSingleAngle returned false), so nothing was drafted.
- . (The pull direction may not suit these faces, or the angle undercuts the geometry - try a smaller angle or 'flip'.)
- Draft computed but tapered nothing -
- measures the volume it had before, so the
- deg taper moved no material. Check 'pull_direction' is the plane the faces taper relative to, and try 'flip' or a face that is not already parallel to it.

### `model_emboss`
- Profile stamped onto the face(s). 'mode' ECHOES the sign of the depth requested; the call is refused when the body's measured volume moves the other way, so the mode reported here is also the direc...
- No active design. Create or open a document first (see doc_new).
- 'faces' resolved to face(s) with no readable owning body - cannot emboss.
- ) - an emboss stamps the faces of ONE body. Pass faces from a single body, one call per body.
- 'faces' sit on a body in component '
- ', but 'profiles' belong to component '
- ' - an emboss is built on the profile's component, so both must be the same one. Sketch the profile on the target body's component.
- Whether the 'faces' body (component '
- ') and the 'profiles' sketch (component '
- ') belong to the SAME component could not be read, and an emboss across two components is refused by Fusion at createInput. Pass faces and profiles from one component - find_geometry with 'target' ...
- EmbossFeatures.createInput returned nothing, so no emboss was attempted. Re-check that the profiles sit over the target face(s).
- Emboss was created but failed to compute:
- . Try a smaller depth, or move the profile fully onto the target face(s).
- Emboss raised no error, but the affected body's volume could not be read back afterwards - whether the profile was raised or engraved is UNVERIFIED, so it is reported as a failure. Re-read the body...
- Emboss reported success but the body's volume is unchanged - nothing was raised or engraved.
- Emboss went the wrong way: depth
- Could not start the emboss:

### `model_extrude`
- Open profile extruded into a SURFACE (no end caps) - pair with model_stitch to close several surfaces into a solid.
- '. Use mm, cm, or in.
- 'to_object' is not used with extent='
- '. Drop 'to_object', or use extent='to_face' (or the default 'distance').
- extent='to_face' needs 'to_object' (a find_geometry face handle).
- Provide a non-zero 'distance' to extrude, or 'to_object' to extrude up to a face.
- '. Use: new, join, cut, intersect.
- No active design. Create or open a document first (see doc_new).
- profile_index mixes the sketch text '
- ' with other regions. A sketch text extrudes on its own - pass just '
- ', and a separate call for the closed regions.
- as_surface is not used with the sketch text '
- ' - a text extrudes as a solid. Drop as_surface, or pass a closed profile / an open path.
- Extrude reported success but this
- changed nothing: no solid body lost material and none was consumed, so the scoped bodies (
- ) are untouched. A cut/intersect can only affect bodies named in 'target_bodies' - check the profile overlaps them in the extrude direction (a negative 'distance' reverses it).
- Sketch text extruded into a solid. To stamp text onto an existing face instead, use model_emboss.
- Profile extruded into a solid. Pair with view_screenshot (iso) to view it.
- extent='two_side' needs non-zero 'distance' and 'distance2' (one per side).
- extent='two_side' does not use 'symmetric' - pass equal 'distance' and 'distance2' for a symmetric two-sided extrude, or use extent='distance' with symmetric=true.
- profile_index resolved a profile whose parent sketch could not be read.
- No sketch to extrude. Create one and draw a closed profile first.
- Could not start extrude:
- Could not set extrude extent:
- 'target_bodies' only applies to cut/join/intersect (a 'new' body has no participants). Remove it, or change the operation.
- Use sketch_get or sketch_create.
- taper_deg is not supported with extent=to_object/to_face - a to-entity extrude takes no taper. Use a distance extent, or drop the taper.
- Fusion refused the to_object extent (setOneSideExtent returned false), so nothing was extruded. Check the target face is reachable from the profile in the extrude direction.
- Could not scope to target_bodies:
- Extrude reported success but extent=through_all removed no material from
- . through_all follows the sketch-plane normal, which on an on-face sketch points away from the body - pass the opposite 'distance' sign. '
- ' remains in the timeline (this check read only those bodies); remove it with design_delete_feature.
- ' has no closed profile to extrude. Draw a closed region (e.g. a rectangle or circle) first, or pass as_surface=true to extrude an open path into a surface.
- taper_deg is not supported with extent=through_all (a through-all extent carries no taper).
- Fusion rejected a symmetric extent=through_all (setTwoSidesExtent returned false).
- extent=through_all (setOneSideExtent returned false).
- taper_deg is not supported with extent=two_side (setTwoSidesDistanceExtent takes no taper).
- Fusion rejected extent=two_side (setTwoSidesDistanceExtent returned false).
- Fusion refused a symmetric tapered extent (
- deg), so nothing was extruded.
- Fusion refused a one-sided tapered extent (
- , so nothing was extruded.

### `model_fillet`
- A rule fillet selects FACES, so 'edges' cannot be passed with fillet_type='rule'. Drop 'edges', or use fillet_type='constant' to round exactly those edge handles.
- A variable-radius fillet cannot take 'faces': its radius runs from the start of the edge chain to the far end, and a face set carries no such order. Pass 'edges' handles in chain order, or fillet_t...
- A variable-radius fillet needs 'edges' - find_geometry edge handles for a single edge, or a tangentially connected chain listed in order from its start end. An edge_filter sweep has no such order, ...
- A chord-length fillet needs 'chord_length' - the straight-line distance across the rounded corner. 'radius' does not drive this type.
- Rule fillet created - the rounded edge set is defined by the selected FACES, not by individual edge handles. Pair with view_screenshot.
- '. Use mm, cm, or in.
- A rule fillet needs 'faces' - find_geometry face handles. Every edge of those faces is rounded; add 'second_faces' to round only the edges between the two sets.
- No active design. Create or open a document first (see doc_new).
- Provide a positive radius: the expression '
- Rule fillet reported success but rounded nothing. topology '
- ' may exclude every edge of the selected faces ('rounds_only' takes convex edges, 'fillets_only' concave ones), or the faces meet smoothly and have no corner to round. The feature has been rolled b...
- (The inert fillet feature could not be auto-removed.)
- The rule fillet was created but its radius reads back
- ' with design_delete_feature.
- The rule fillet was created but its topology is not the requested '
- Provide a positive radius.
- The rule fillet refused the given faces, so nothing was created. Re-run find_geometry for fresh face handles.
- 'radius' must be a number or a parameter-expression string like 'WallT/2'.

### `model_hole`
- Hole feature added (a real Hole, with hole/thread metadata - not an extrude-cut). For a bolt circle, pass every position in 'points' in ONE call - the pattern tools take bodies/occurrences, not hol...
- fit). Diameter set from the standard clearance table (the API tags the fastener but doesn't auto-size on this version).
- Clearance hole drilled + TAGGED for
- Provide 'diameter' (e.g. '8 mm') or a 'fastener' (e.g. 'M6 Socket Head Cap Screw') to size the hole.
- points_space='world' applies to placement='sketch_points' (got '
- ', which positions the hole off 'edge'/offsets instead).
- A counterbore hole needs 'cbore_diameter' and 'cbore_depth'.
- A countersink hole needs 'csink_diameter' and 'csink_angle' (e.g. '90 deg').
- 'modeled' (a real helical thread) only applies to a tapped hole; pass 'tap' too.
- '. Use 'blind' (with 'depth') or 'through'.
- A blind hole needs 'depth' (e.g. '10 mm'). For a hole through the body use extent='through'.
- Could not resolve 'face' to a planar face. Pass a find_geometry face handle.
- This component does not support hole features.
- '. Use mm, cm, or in.
- hole point(s) cut NOTHING - the feature created
- The hole was created through the host component
- with design_delete_feature, then retry with 'face' taken from find_geometry on the instance you mean.
- Provide 'points' - a list of [x, y, z] positions on the face to drill at.
- ' is not a drillable hole - a blind hole needs a POSITIVE depth (e.g. '10 mm'), or use extent='through'.
- Could not resolve 'edge' to an edge. Pass a find_geometry edge handle.
- (sketches.add returned nothing)
- The hole was drilled but carries no tap, so '
- ' did not take. Remove '
- ' with design_delete_feature.
- The hole was tapped '
- ', not the requested '
- placement='center' needs 'edge' - a find_geometry handle at the circular/elliptical edge to center the hole on.
- Could not resolve 'offset_edge_one' to an edge.
- Fusion refused to centre the hole on that edge, so nothing was placed. Check that 'edge' is a circular/elliptical edge ON 'face'.
- A modeled thread was requested, but the hole's thread feature could not be read back, so there is no proof the helix was cut. Remove '
- The tap was requested
- placement='on_edge' needs 'edge' - a find_geometry handle at the edge to position the hole along.
- placement='on_edge' needs 'edge_position' - one of:
- placement='plane_offsets' needs 'point' - an approximate [x, y, z] hole location (picks the solution when several are possible).
- placement='plane_offsets' needs 'offset_edge_one' and 'offset_one'.
- placement='plane_offsets': 'offset_edge_two' and 'offset_two' must be given together.
- Could not resolve 'offset_edge_two' to an edge.
- Could not position the hole at the edge's center:
- is not available on this Fusion version.
- Fusion refused to place the hole at the '
- ' of that edge, so nothing was placed. Check that 'edge' borders 'face'.
- Fusion refused the plane-and-offsets placement, so nothing was placed. Check that both offset edges border 'face' and the offsets reach a point on it.
- Could not position the hole on the edge:
- ; expected [x, y, z] in '
- Could not position the hole by plane and offsets:

### `model_inspect`
- Mesh target: triangle/vertex counts + watertight (is_closed) + bbox. (A mesh has no B-Rep bounding box or mass; target a solid body/occurrence for include=['mass'].)
- Bounding box over the SOLID/SURFACE/MESH bodies only - sketch and construction geometry (planes, axes) are excluded, so an orphaned datum does not inflate it. Add include=['mass'] for full physical...
- No active design. Open or create a document first (see doc_new).
- '. Valid: mm, cm, in.
- No bounding box available for
- (it may have no solid geometry).
- has no min/max points.
- MeasureManager unavailable.
- has no B-Rep body to measure in a frame. Target a specific body/occurrence (design_get(include=['tree']) lists them).
- getOrientedBoundingBox returned nothing for this target.
- Measured in the joint-origin frame; x/y/z are the part-space extents. Feed these to param_set to drive stock size. The frame is the Joint Origin its owning COMPONENT carries - the same one for ever...
- Oriented bounding-box measurement failed:
- . (The X/Y axes of the frame must be perpendicular, and the target must be B-Rep geometry.)
- Mass is driven by each body's PHYSICAL MATERIAL (density), not its appearance - if a mass looks wrong, check 'density'. Inertia_world is about the WORLD origin; principal_moments are about the cent...
- '. Use mm, cm, or in.
- '. Use: low, medium, high, very_high.
- Could not compute physical properties for
- (no measurable solid? an empty or surface-only target has no mass).

### `model_loft`
- Lofted through %d profiles in order.
- '. Use: new, join, cut, intersect.
- No active design. Create or open a document first (see doc_new).
- Loft needs at least 2 profiles (got
- centerLineOrRails takes a centerline OR rails, not both.
- Loft reported success but the feature owns no result body - nothing was built.
- Could not start loft:
- Could not add loft sections:
- Could not set loft centerline/rails:
- . Common causes: a cut/intersect with no body in the loft's path (the API says 'No target body' for that), or incompatible profiles (a mix of open/closed, or a self-intersecting path - profiles mus...
- Loft reported success but this
- changed nothing - every solid body in '
- ' measures the volume it had before and none was consumed, so the lofted shape does not overlap any of them. Check the profiles bracket the target body (an 'intersect' whose target lies entirely IN...
- Could not set loft solid/surface mode:

### `model_measure_between`
- No active design. Open or create a document first (see doc_new).
- '. Use 'distance' or 'angle'.
- '. Valid: mm, cm, in.
- Minimum gap between the two targets (0 = touching/overlapping). closest_point_on_a/b are the nearest points; their separation IS the distance.
- MeasureManager unavailable.
- measureAngle returned nothing for these two targets.
- measureAngle returned a result whose value read as
- , not a number, so the angle is UNKNOWN - reporting it as 0 would read as parallel.
- Angle between the two targets. Two planar faces give the angle between their planes; a face + an edge the angle between them.
- measureMinimumDistance returned a result whose value read as
- , not a number, so the distance is UNKNOWN - reporting it as 0 would read as touching. Re-run find_geometry for fresh handles and retry.
- Distance 0 with both closest points at (0,0,0): the targets touch or OVERLAP and this point pair is degenerate - it does NOT locate the contact. Use assembly_inspect_interference on the pair to get...
- Angle measurement failed:
- . (Angle needs two entities with a defined direction - two planar faces, or a face and an edge; a whole occurrence may be rejected. Use find_geometry face/edge handles.)

### `model_measure_relation`
- No active design. Open or create a document first (see doc_new).
- '. Valid: mm, cm, in.
- tolerance_deg must be >= 0 (degrees).
- tolerance_deg must be a number (degrees).

### `model_mirror`
- Feature(s) mirrored across the plane. Confirm with design_get(include=['timeline']) / view_screenshot.
- Bodies mirrored across the plane. Pair with view_screenshot to view.
- No active design. Create or open a document first (see doc_new).
- Give ONE thing to mirror: 'bodies' (
- Nothing to mirror. Pass 'bodies' (body handles/names) or 'features' (timeline feature names from design_get(include=['timeline'])).
- Nothing resolved to mirror.
- and moved no volume - nothing was mirrored.
- 'join' combines mirrored BODIES with their originals and is documented as ignored for a feature mirror - re-run without 'join', or mirror the bodies.
- ' was created but neither the body count of
- nor a volume could be read back, so its effect is UNVERIFIED.
- ' moved no volume, and the body count of
- could not be read back, so whether it added a body is UNVERIFIED.
- , and no volume could be read back, so whether it moved material is UNVERIFIED.

### `model_move`
- Geometry repositioned. To reposition a component instance instead, use assembly_move.
- A move feature cannot move FACES - pass 'bodies'. createInput2 accepts a BRepFace collection and then raises InternalValidationError inside the kernel. To push or pull a face, use model_offset_face.
- 'bodies' is required - the bodies to move.
- No active design. Create or open a document first (see doc_new).
- Move feature was created but failed to compute:
- . Try a smaller move, or a different axis/point selection.
- ' move definition, so nothing was moved.
- mode 'along_entity' needs 'axis' - the linear entity the move runs along.
- mode 'rotate' needs 'axis' - the linear entity to rotate about.
- mode 'rotate' needs 'angle_deg' - the rotation in degrees.
- 'angle_deg' must be non-zero - a 0 deg rotation moves nothing.
- mode 'point_to_point' needs
- - a vertex handle from find_geometry.
- 'angle_deg' must be a number (rotation in degrees), got

### `model_offset_face`
- Face(s) pushed/pulled along their normal. Positive extends outward (adds material); negative pushes inward (removes material).
- No active design. Create or open a document first (see doc_new).
- 'faces' resolved to face(s) with no readable owning body - cannot offset.
- Offset face was created but failed to compute:
- . Try a smaller distance or a different face selection.
- Offset face ran in a DIRECT design, which returns no feature object, and no affected body's volume could be read back - so whether the faces moved is UNVERIFIED. Re-read the body with model_inspect.
- Offset face reported success but the affected body's volume is unchanged - nothing was actually pushed or pulled.
- . (The distance may be too large for the geometry, or the faces may not support a uniform offset together - try a smaller distance or fewer faces.)

### `model_pattern_circular`
- quantity must be >= 2 for a circular pattern.
- No active design. Open or create a document with components first.
- were requested. The feature is left in the timeline for inspection - design_delete_feature removes it.
- patterned around the axis. Pair with view_screenshot to view.
- No pattern was created.
- Circular pattern failed:

### `model_pattern_path`
- Instances placed along the path. Copies keep the seed's orientation; they do not rotate to follow the path. Pair with view_screenshot to view.
- quantity must be >= 2 for a path pattern (the original plus at least one copy).
- No active design. Open or create a document with components first.
- is not available on this Fusion version.
- Path pattern createInput returned nothing, so no pattern was created.
- were requested - the path may be too short for the spacing asked for. The feature is left in the timeline for inspection - design_delete_feature removes it.
- Could not start the path pattern:
- No pattern was created.
- . Check that the path is one connected chain and that the entities sit on or near it.

### `model_pattern_rectangular`
- '. Use mm, cm, or in.
- quantity_one must be >= 1.
- spacing_one=0 would stack every instance exactly on the seed (coincident duplicates). Provide a non-zero spacing_one.
- spacing_two=0 would stack the second-direction instances exactly on the first row (coincident duplicates). Provide a non-zero spacing_two.
- No active design. Open or create a document with components first.
- ). The feature is left in the timeline for inspection - design_delete_feature removes it.
- patterned in a grid. Pair with view_screenshot to view.
- Fusion refused the second pattern direction (setDirectionTwo returned false), so no pattern was created.
- Rectangular pattern failed:

### `model_pipe`
- Pipe built along the path. Pair with view_screenshot (iso) to view it.
- 'hollow' is false but 'wall_thickness' is
- - a wall thickness only exists on a hollow pipe. Drop one of the two.
- 'path_fraction_reverse' is
- but 'path_fraction' is not set - the forward extent must be given before the reverse one.
- ) plus 'path_fraction_reverse' (
- - the two directions together cannot cover more than the whole path (1.0).
- No active design. Create or open a document first (see doc_new).
- but this path is OPEN. Fusion IGNORES the reverse extent on an open path, so it is refused here instead of reported as applied - use 'path_fraction' alone, or close the path.
- Pipe createInput returned nothing, so no pipe was created.
- 'path_fraction_reverse' needs a CLOSED path and this path did not report whether it is closed, so the reverse extent could not be verified. Re-run without 'path_fraction_reverse'.
- 'target_bodies' only applies to cut/join/intersect (a 'new' body has no participants). Remove it, or change the operation.
- Could not start the pipe:
- . (The path must form one connected chain of edges or sketch curves.)
- Could not set section_size:
- . (A 'cut'/'intersect' needs existing geometry to act on; the section must fit around the path's corners.)
- Pipe reported success but created no body. Check that the path is one connected chain and the section size fits around its corners.
- Pipe created a body with no volume, so nothing usable was built.
- Setting 'wall_thickness' switched the pipe input back to SOLID, so a hollow pipe cannot be built from these inputs. No pipe was created.
- Could not scope to target_bodies:
- Pipe ran in a DIRECT design, which returns no feature object, and the component's body count did not rise - so no pipe body can be shown to exist. Read the model back with design_get before retrying.
- Pipe ran in a DIRECT design, which returns no feature object, and no body's volume could be read back - so whether the
- changed anything is UNVERIFIED. Re-read the bodies with model_inspect.
- Pipe reported success but no body's volume changed, so the
- Could not set wall_thickness:

### `model_replace_face`
- The listed face(s) of that body now follow the target surface; the deltas below are the measured change on the body.
- The feature computed cleanly, but NEITHER the body's volume NOR its face count could be read back, so there is no geometric proof the faces were replaced - no deltas are reported. Re-read the body ...
- No active design. Create or open a document first (see doc_new).
- 'faces' resolved to face(s) with no readable owning body - cannot replace.
- 'faces' must all be on ONE body, but they span
- ). Replace the faces of one body per call.
- Replace face was created but failed to compute:
- Replace face could not build its feature input (createInput returned nothing) - nothing was changed.
- Replace face ran in a DIRECT design, which returns no feature object, and neither the body's volume nor its face count could be read back - so whether the faces were replaced is UNVERIFIED. Re-read...
- Replace face reported success but body '

### `model_revolve`
- Profile revolved into a solid. Pair with view_screenshot (iso) to view it.
- '. Use: new, join, cut, intersect.
- Provide a non-zero 'angle_deg' to revolve (e.g. 360 for a full revolve).
- No active design. Create or open a document first (see doc_new).
- No sketch to revolve. Create one and draw a closed profile first.
- Could not resolve axis '
- use x | y | z, a straight-edge/sketch handle, or line:<index>.
- angle_deg must be a number (degrees).
- Use sketch_get or sketch_create.
- ' has no closed profile to revolve.
- out of range - sketch has
- 'target_bodies' only applies to cut/intersect operations.
- Could not start revolve:
- . (The axis must not pass through the profile in a way that self-intersects.)
- Could not set revolve angle:
- . (A 'cut'/'intersect' needs existing geometry to act on. An axis outside the profile's plane is projected onto it, so that is not the cause; a profile that CROSSES the axis is refused.)
- Revolve reported success but this
- changed nothing - every solid body in '
- ' measures the volume it had before and none was consumed, so the revolved shape does not overlap any of them. Check that the profile and axis put the swept solid inside the target body (an 'inters...
- Fusion refused a two-sided revolve extent (
- deg), so nothing was revolved.
- deg, so nothing was revolved.
- Could not set target_bodies before revolve:

### `model_scale`
- Bodies resized about the anchor point, which stays put. Factors are unitless: 2 doubles every dimension and multiplies volume by 8.
- A per-axis scale needs all three factors - got
- . Give all three, or use 'factor' for a uniform scale.
- Give EITHER 'factor' (uniform) OR x_factor/y_factor/z_factor (per-axis), not both.
- 'factor' is required (the uniform scale factor), or give x_factor, y_factor and z_factor together for a per-axis scale.
- No active design. Create or open a document first (see doc_new).
- Scale feature was created but failed to compute:
- . Try a factor closer to 1, or a different anchor.
- No 'anchor' given and the active component has no origin construction point to scale about. Pass a vertex handle from find_geometry.
- . (A parameter expression may not resolve - check it with param_get - or the factor may collapse the geometry; try a factor closer to 1.)
- setToNonUniform refused the per-axis factors, so nothing was scaled. Retry as a uniform scale with 'factor'.

### `model_set_material`
- ). model_inspect mass/density now reflects this material. This is NOT color - use appearance_set for cosmetic color.
- Empty target: assigned to the bodies of all
- component(s) the design listed.
- failed - see 'failed' ('<component>/<body>' names each).
- No active design with geometry.
- has no bodies to assign a material to.
- Selected material source identity is unavailable (name or id could not be read); refusing mutation. Read design_get(include=['materials']) and retry with a source carrying both fields.
- No body assignment was verified for material '
- Empty target: the design's component list did not read, so only the ROOT component's bodies were reached - a child component's bodies keep the material they had.

### `model_shell`
- Body hollowed into a shell. Pair with view_section to inspect the wall thickness.
- No active design. Create or open a document first (see doc_new).
- (The body could not be hollowed at this thickness.)
- Shell returned feature '
- ', but its effect is unverified:
- . Nothing was rolled back. Inspect the retained feature with model_inspect and view_section before retrying.

### `model_split`
- No active design. Create or open a document first (see doc_new).
- Faces split; result_count is the net face-count increase. Pair with view_screenshot.
- 'faces' is required for split=face (the faces to split).
- Split produced no new faces - the cutter did not cross the
- target face(s). It must intersect them; try extend_tool=true or a larger cutter.
- . (The cutter must cross the faces - try extend_tool=true or a larger cutter.)
- Split face ran in a DIRECT design, which returns no feature object, and the owning bodies' face count could not be read back - so whether the faces were split is UNVERIFIED. Check with model_inspec...
- Body split into pieces. Pair with design_get(include=['tree']) / view_screenshot.
- 'target' is required for split=body (the body to split).
- body - the cutter did not divide '
- '. It must fully intersect the body; try extend_tool=true or a cutter that crosses it.
- . (The cutter must fully cross the body - try extend_tool=true, or a larger cutter/plane.)
- Split body ran in a DIRECT design, which returns no feature object, and the component's body count could not be read back - so whether the body was divided is UNVERIFIED. Check with design_get(incl...

### `model_stitch`
- Surfaces closed into a SOLID within tolerance.
- '. Use: new, join, cut, intersect.
- '. Use mm, cm, or in.
- No active design. Create or open a document first (see doc_new).
- Stitch needs at least 2 surface bodies (got
- Stitch reported success but the feature owns no result body - nothing was stitched.
- Surfaces did NOT close into a solid within tolerance (
- ). The result is still a surface - increase tolerance or check for gaps/overlaps.
- The stitch ran, but at least one result body's isSolid flag could not be read back, so whether the surfaces closed into a SOLID is UNVERIFIED - check the body with model_inspect or design_get(inclu...
- Could not start stitch:
- . (Surfaces must be adjacent/overlapping within tolerance.)

### `model_sweep`
- Swept into a SURFACE (no end caps) - pair with model_stitch to close several surfaces into a solid.
- Profile swept into a solid along the path. Pair with view_screenshot (iso) to view it.
- '. Use: new, join, cut, intersect.
- Unknown orientation '
- '. Use: perpendicular, parallel.
- No active design. Create or open a document first (see doc_new).
- Sweep reported success but created no body. Check that the profile sits on the path and the path forms a valid, connected sweep.
- Could not start sweep:
- . (The path must geometrically connect and the profile should sit on/near the path start.)
- Could not configure the sweep:
- 'target_bodies' only applies to cut/join/intersect (a 'new' body has no participants). Remove it, or change the operation.
- . (A 'cut'/'intersect' needs existing geometry to act on; the profile and path must form a valid sweep.)
- Sweep reported success but this
- changed nothing - every solid body in '
- ' measures the volume it had before and none was consumed, so the swept profile does not overlap any of them. Check the path runs through the target body (an 'intersect' whose target lies entirely ...
- Could not scope to target_bodies:
- measure the volumes they had before and none was consumed, so the profile does not sweep through any of them. A cut/intersect can only affect bodies named in 'target_bodies' - check the path runs t...
- The sweep feature was rolled back.
- Remove the empty feature with design_delete_feature.

### `model_thread`
- Provide 'designation' - the thread call-out, e.g. 'M8x1.25' or '1/4-20 UNC'.
- 'offset' positions a partial thread, so it needs 'length' too; without 'length' the thread runs the whole cylinder and the offset is ignored.
- 'location' picks which end a partial thread is measured from, so it needs 'length' too.
- No active design. Create or open a document first (see doc_new).
- Could not read the outward normal of face(s)
- (0-based), so whether they are bores or shafts is unknown. Re-run find_geometry for fresh handles and pass faces whose 'normal' it reports.
- One Thread feature cannot mix internal and external faces: face(s)
- (0-based) are bores and the rest are shafts. Thread each side in its own call.
- This component does not support thread features.
- 'faces' resolved to face(s) with no readable owning body, so a modeled thread's cut cannot be verified. Re-run find_geometry for fresh handles.
- threadFeatures.createInput returned nothing for '
- ' was created but failed to compute:
- The thread was created but carries designation '
- ', not the requested '
- '. Remove it with design_delete_feature (feature '
- The thread was created as an
- thread, but the face(s) are
- . Remove it with design_delete_feature (feature '
- Thread input could not be built for '
- Could not apply the thread settings:
- A partial thread was requested, but the feature's own extent could not be read back, so there is no proof it took. Remove '
- ' with design_delete_feature.
- A partial thread was requested (length
- ' reads back as full length - the partial extent did not take. Remove it with design_delete_feature.
- The thread was created but sits at the wrong end of the cylinder - '
- ' was requested. Remove '
- The thread was created, but no affected body's volume could be read, so there is no proof the helix was cut. The feature remains in the timeline; remove it with design_delete_feature (feature '
- A modeled thread cuts the helix into the cylinder, but the affected body's volume is unchanged - nothing was cut. The feature remains in the timeline; remove it with design_delete_feature (feature '
- A modeled thread cuts material away, but the body's volume GREW by
- ' does not fit this cylinder, so the thread form was built outside it. Check the designation against the cylinder's diameter. The feature remains in the timeline; remove it with design_delete_featu...
- is not available on this Fusion version.
- The thread was created but its

### `model_unstitch`
- No active design. Create or open a document first (see doc_new).
- Unstitch needs a 'target' body (to fully explode) or 'faces' (to peel off).
- Pass EITHER 'target' (a whole body) OR 'faces' (specific faces), not both.
- The target may already be loose surfaces, or the faces are not unstitchable.
- Unstitch divided NOTHING - '
- ' produced the same body count (
- ) and a single result body: the input was already a loose surface, so this was an identity operation.
- The feature was rolled back.
- Remove the empty feature with design_delete_feature.
- Exploded into %d surface body(ies) - each is now an open surface. Edit a face, then model_stitch to re-close.
- . (Target may already be loose surfaces, or the faces aren't unstitchable.)

### `param_add`
- User parameter added; timeline verified (no new errors).
- 'params' must be a list of {name, expression, ...} dicts.
- user parameters added; timeline verified.
- ] must be a dict with 'name' and 'expression'.

### `param_delete`
- Provide 'name' - the parameter to delete.
- No USER parameter named '
- ' (only user parameters can be deleted; model/feature parameters cannot).
- . Re-point or remove those first.
- Fusion refused to delete '
- ' (it may be in use).
- deleteMe() reported success for '
- ' but the design's user parameters would not re-read, so whether it is gone is UNVERIFIED - a list that did not read is not a list without it. Re-read with param_get before deleting more.
- deleteMe() reported success but '
- ' is still in the design's user parameters - it was NOT deleted. Nothing was rolled back; re-read with param_get to see what is actually there.
- ' introduced a timeline error (
- ). The deletion stands - undo in Fusion if needed.
- User parameter deleted - the name is gone from the design's user parameters, and the timeline walks clean (no new errors).

### `param_get`
- No active design (open a document with design geometry).
- Parameter not found: '
- Could not read user parameters:
- Could not read model parameters:
- Could not read a parameter name while classifying model parameters.
- Could not classify parameter '

### `param_set`
- Provide 'name' - the parameter to set.
- Provide 'expression' - the new value/expression for the parameter.
- No active design (open a document with design geometry).
- Assignment raised no error but '
- ' still reads expression '
- Parameter not found: '
- '. Use param_get to list them, or pass create=true to make it a new user parameter.
- Creating user parameter '
- . (Model/feature parameters may be read-only or require a valid expression; text parameters need quotes, e.g. "'text'".)
- Could not create user parameter '

### `param_set_favorite`
- No USER parameter named '
- Could not set favorite on '

### `pmi_create`
- Verify placement visually with view_screenshot; read all PMI with pmi_get.
- No active design. Create or open a document first (see doc_new).
- '. Use mm, cm, or in.
- 'flags'/'values'/'display' apply to kind='hole_note' only.
- 'plane'/'plane_face'/'leader_point' apply to kind='note' only - a hole note derives its plane and leader from the hole faces.
- (the annotation WAS created: '
- ' - reposition with pmi_edit)
- 'leader_extension' must be a number (in 'units').

### `pmi_delete`
- The delete is confirmed by the annotation object itself (isValid=
- ). The PMI re-walk could not read
- annotation(s), so 'remaining_pmi' counts only what was reachable.
- No active design. Create or open a document first (see doc_new).
- ) reports isDeletable=false - the platform refuses to delete it (e.g. PMI owned by an imported folder). Nothing was changed.
- deleteMe() declined for '
- ) - the annotation was NOT deleted.
- deleteMe() reported success but '
- ' still resolves in '
- match) - treat the delete as failed.
- deleteMe() reported success and '
- ' no longer resolves, but the annotation object still reports isValid - treat the delete as failed.
- deleteMe() reported success for '
- ' and nothing contradicts it, but the check is INCOMPLETE:
- , and the annotation's own isValid did not read either. A walk that could not read everything is not a walk that found nothing. Nothing was rolled back - re-run pmi_get to see what is actually there.

### `pmi_edit`
- No active design. Create or open a document first (see doc_new).
- '. Use mm, cm, or in.
- ' is already Fusion-authored (
- ) - nothing to convert.
- Conversion declined -
- with this reference geometry is not convertible (imported dimensions -> hole notes and imported notes -> leader notes are the supported paths). The original PMI is unchanged.
- The text edit did not take (segments unreadable after the set).
- The note's segments read back as '
- ', not the requested '
- ' - the text edit did not take as asked. The annotation is left carrying what is quoted above, not the request.
- Setting the note text failed:
- action='rename' needs 'new_name'.
- The rename did not take - the annotation is still named '
- Light bulb is on but the PMI is still not visible - a containing folder's or the component's PMI light bulb is off.
- The visibility set did not take (isLightBulbOn is still
- Visibility toggle failed:
- set_leader_point applies to leader notes only ('
- - a hole callout leads to its hole).
- set_plane applies to leader notes only.
- action='set_plane' needs 'plane'.
- ' is not supported on this note's geometry. Supported:
- The plane set did not take (still
- setAnnotationPlane failed:
- . plane=face needs plane_face (an ADJACENT face); custom_face needs plane_face.
- action='set_alignment' needs align, valign, and/or perpendicular.
- action='set_extension' needs 'leader_extension' (in 'units').
- The extension set did not take (re-read %s %s).
- 'leader_extension' must be a number.
- set_flags applies to hole/thread callouts only.
- action='set_flags' needs a non-empty 'flags' - {threaded: true, through: false}.
- set_values applies to hole/thread callouts only.
- action='set_values' needs a non-empty 'values' - {diameter: 6.2} or {diameter: {value, tolerance: {type, ...}}}.
- set_display applies to hole/thread callouts only.
- action='set_display' needs 'display' - {precision, units, leading_zeros, trailing_zeros, unit_abbreviation, secondary: {...}}.
- A suppressed PMI is expected to drop out of the pmi_get listing entirely (unconfirmed on this Fusion build) - pmi_edit(action='unsuppress') brings it back by name either way, and verifies it reappe...
- ' has no timeline feature - only parametric PMI can be suppressed.
- The suppression set did not take (isSuppressed is still
- Timeline suppression toggle failed:
- Already up to date - nothing to dismiss.
- markUpToDate() declined - the warnings cannot be dismissed without changes; the PMI stays out of date. Re-attach or edit the referenced geometry.
- markUpToDate() failed:
- suppressed timeline features - rename the non-PMI feature or unsuppress it in the timeline first.
- ' was unsuppressed and now names a PMI in
- ) - the annotation IS back and was left unsuppressed. Re-run action='unsuppress' with component= to report which one.
- ' is not a PMI annotation (no PMI reappeared) - it was left suppressed.
- Timeline unsuppress failed:
- Unsuppressed timeline feature '
- ' is not a PMI annotation, and re-suppressing it failed - check the timeline.

### `pmi_get`
- - 'segments' adds the {symbol} markup (round-trips into pmi_create/pmi_edit text), 'detail' adds per-kind structure (placement/format, hole values+tolerances+thread+display, imported dimension/GDT/...
- Light records. Pull deeper with include=
- No active design. Create or open a document first (see doc_new).
- '. Use mm, cm, or in.
- Unknown include slice(s)

### `save_as_mesh`
- Inspect it with model_inspect (mesh target), edit with mesh_reduce / mesh_remesh, or export it with mesh_export.
- Tessellated the BRep body into a persistent MESH body.
- Wrapped in a BaseFeature edit scope (parametric design requires it for a mesh write).
- Direct design - no base-feature scope needed.
- No active design. Open or create a document first (see doc_new).
- 'body' is already a MESH body - save_as_mesh tessellates a BRep solid/surface. To re-triangulate an existing mesh use mesh_remesh; to copy/export it use mesh_export.
- Could not resolve a component to add the mesh body into.
- Tessellation produced no coordinate/index data - cannot build a mesh body.
- meshBodies.addByTriangleMeshData returned nothing - no mesh body was created.
- addByTriangleMeshData returned a mesh body but the component's mesh body count did not increase (
- after) - the mesh body did not actually land.
- This body has no meshManager - cannot tessellate it into a mesh.
- meshManager.createMeshCalculator() returned nothing - cannot tessellate.
- Mesh calculator returned no TriangleMesh (tessellation produced nothing).
- Fusion refused mesh quality '
- ' (setQuality returned false), so nothing was exported at that quality.
- Mesh tessellation (calculate) failed:

### `sketch_add_3d_line`
- Line drawn in 3D. The end point's non-zero z places it off the sketch's x-y plane. View it from an iso angle with view_screenshot (a top view hides the out-of-plane component).
- '. Valid: mm, cm, in.
- No active design. Create or open a document first (see doc_new).
- No sketch to draw on. Create one first with sketch_create.
- 3D line creation returned no entity.
- Provide the end point: x2, y2, z2 (the start defaults to the origin, 0,0,0; set coincident_start_to_origin=true to lock it there).
- '. Use sketch_get or sketch_create.
- Failed to draw 3D line:
- Line was drawn but could not be marked construction:

### `sketch_add_geometry`
- '. Valid: mm, cm, in.
- No active design. Create or open a document first (see doc_new).
- No sketch to draw on. Create one first with sketch_create.
- '. Use sketch_get to list them, or sketch_create first.
- Draw more with sketch_add_geometry, or view_screenshot to view the sketch.
- Rectangle drawn. NO horizontal/vertical constraint took, so its sides are held only by their coordinates - a later edit can skew it. Add them with sketch_constrain (horizontal / vertical) before di...
- horizontal/vertical constraint(s) applied to its sides, as the UI does - the constructor itself lands none. Its corners already share points; what remains free is position and size, so dimension th...
- Control-point spline drawn - constrain or dimension it as 'cv_spline:<index>' (sketch_get lists the index).
- Arc slot drawn out of SketchArcs - 'curves_added' counts them and each is addressable as 'arc:<index>' for sketch_dimension / sketch_constrain (sketch_get(include_entities=true) lists the indexes).
- Slot drawn from 2 solid SketchLines, 1 CONSTRUCTION SketchLine (the centre-to-centre line) and 2 SketchArc end caps - 5 curves, of which 'curves_added' counts the 3 lines. Address any of them as 'l...
- Slot drawn - 'curves_added' counts its SketchLines: three, four when a length or angle is passed. Its two end caps are SketchArcs. Address either as 'line:<index>' / 'arc:<index>' for sketch_dimens...
- Closed path drawn and a profile forms. The seam is WELDED - the closing segment ends on the first segment's start point, so the loop shares that point instead of carrying two at the same coordinate...
- drawn. Its centre is a sketch point of its own: center_point=
- - address points by that ref rather than by counting the ones you drew.

### `sketch_constrain`
- Geometric constraint applied - the sketch is now parametric for this relationship.
- The sketch text's anchor is
- rectangle lines of its definition, the degree of freedom no geometric constraint can address.
- The sketch now reads FULLY CONSTRAINED.
- The sketch is still NOT fully constrained - other geometry holds the remaining freedom (sketch_get(include_entities=true) shows what).
- The sketch's constrained state did not read back.
- sketch entities - read their '<type>:<index>' refs with sketch_get.
- applied with EVERY instance suppressed, so it created no curves - the pattern constraint itself is in the sketch. Re-run with fewer 'suppressed' flags set for a pattern that draws.

### `sketch_copy`
- ', and an added curve APPENDS at the end of its kind, so the ids already in use keep their entities - re-read sketch_get(include_entities=true) for the new ones. 'returned_entity_count' counts the ...
- The new curves' ids are in '
- ' but NONE could be identified by entityToken - re-read sketch_get(include_entities=true) for their ids.
- could be identified (
- ) - re-read sketch_get(include_entities=true) for the rest.
- copy returned no collection for
- ' - nothing was copied.
- entity(ies) but sketch '
- curve(s) - nothing landed in it.
- ' for 'target_sketch'. Available:

### `sketch_create`
- Draw on it with sketch_add_geometry (target this sketch by name).
- No active design. Create or open a document first (see doc_new).
- Sketch creation returned nothing on
- Failed to create sketch on
- ' instead - 'on_face' takes a planar-FACE handle from find_geometry.
- ', which is a construction PLANE name, not a face handle. Pass it as plane='

### `sketch_delete_entity`
- | constraint | dimension | text (e.g. 'circle:0', 'dimension:2'). sketch_get(include_entities=true) lists the curve/constraint/dimension indexes; a text index is the one sketch_set_text edits by.
- Provide 'target' as '<type>:<index>' - type =
- | constraint | dimension | text.
- Unknown target type '
- . Indexes are 0-based in creation order; list them with sketch_get.
- ). The entity may be consumed by a dimension/constraint - remove those first.
- Entity removed. Deleting a curve can cascade to constraints/dimensions that referenced it; re-read with sketch_get before adding more.
- ' has a non-integer index; use '<type>:<index>' (e.g. 'line:1').
- did not take (constraint count
- ). It may be a fixed/driving constraint the solver won't remove.
- Constraint removed. Re-constrain if needed (see sketch_constrain).
- did not take (dimension count
- ). The dimension is still in the sketch.
- Dimension removed. Re-read sketch_get(include_entities=true) for the sketch's remaining dimensions and its constrained state.
- did not take (sketch text count
- ). The text is still in the sketch.
- Sketch text removed. Create a replacement with sketch_set_text(create=true).
- was called but the sketch's
- count would not read back, so whether it was removed is UNVERIFIED - a count that will not read is not a count of zero. The sketch held
- (s) before the call. Re-read it with sketch_get before deleting more.

### `sketch_dimension`
- No active design. Create or open a document first (see doc_new).
- No sketch to dimension. Create one first with sketch_create.
- Drive it later by name via param_set.

### `sketch_edit_curve`
- Curve ids are creation-order indexes per kind: removing a curve RENUMBERS the ones after it, while an added curve APPENDS at the end (both measured) - re-read sketch_get(include_entities=true) befo...
- The whole curve was consumed: a trim on a curve with no intersections deletes it outright.
- '. Valid: mm, cm, in.
- No active design. Create or open a document first (see doc_new).
- No sketch to edit. Draw one first with sketch_create + sketch_add_geometry.
- ' needs the pick point x1,y1 (in 'units') - it chooses which segment, end, quadrant or side of the curve the edit applies to.
- ' needs a pick point on EACH curve: x1,y1 on entity_one and x2,y2 on entity_two - together they choose the quadrant to build in.
- fillet needs 'radius' > 0 (in 'units'); got
- extend changed nothing - the end nearest the pick point could not be extended. The sketch still holds
- curve(s). Re-read sketch_get(include_entities=true) and pick a point ON the curve.
- returned no curves and the sketch still holds
- curve(s), so nothing changed.
- Re-read sketch_get(include_entities=true) for the current ids and pick a point ON the curve.
- chamfer needs 'distance' > 0 (in 'units') - the setback along entity_one; got
- chamfer takes EITHER 'distance_two' (a second setback) OR 'angle_deg' (the angle from entity_one), not both.
- 'distance_two' must be > 0; got
- 'angle_deg' must be between 0 and 180 exclusive; got
- offset needs 'distance' > 0 (in 'units'); the SIDE comes from the pick point x1,y1, so the distance is a magnitude. Got

### `sketch_get`
- ), so a row's 'component' does not identify which one holds it, and this list does not tell those rows apart. 'placements' lists every occurrence path placing a component of one of the names just l...
- More than one component wears the same name here (
- No active design (open or create a document with design geometry).
- Could not read sketches:

### `sketch_insert_svg`
- 'scale' must be greater than 0, got
- No active design. Open or create a document first (see doc_new).
- No sketch to import the SVG into. SVG curves land in an EXISTING sketch - make one with sketch_create, then name it in 'sketch_name'.
- Fusion refused the SVG import (importSVG returned false); sketch '
- before the call. Check the file opens as SVG.
- importSVG returned true but sketch '
- ' gained no curves (still
- ) - nothing was imported. Measured: an empty-but-valid SVG and a non-SVG file carrying an .svg name both answer true this way.
- If the art is the wrong size, change 'scale' and re-import.
- SVG curves APPEND to the sketch: the '<type>:<index>' ids already in use keep their entities and the new curves take the ids after them - list them with sketch_get(include_entities=true). Each land...
- 'scale' must be a number - a multiplier on the SVG's own size (got '
- . SVG curves land in an EXISTING sketch - make one with sketch_create.

### `sketch_move`
- The entities keep their ids - a move adds and removes nothing, so no renumbering. Re-read sketch_get(include_entities=true) for the new coordinates.
- read the same coordinates afterwards - an existing constraint or dimension refused the move for those, or the transform leaves them where they were.
- Sketch.move returned false yet the coordinates changed - this reports what the geometry shows, not the return value.
- Fusion declined the move in sketch '
- ' (Sketch.move returned false) and none of
- read the same coordinates afterwards, so nothing in the sketch changed. Either the transform is one this geometry is symmetric under (a circle rotated about its own centre), or a constraint refused...

### `sketch_project`
- No active design. Create or open a document first (see doc_new).
- No sketch to project into. Create one first with sketch_create.
- . Create one with sketch_create.
- Extrude a resulting profile via sketch_get -> model_extrude.
- Projection created no sketch entities in '
- '. The geometry may already be projected, or lies out of the sketch plane's projectable set. Nothing was added.
- - delete them with sketch_delete_entity if that linkage is wrong for the job.
- . The curves WERE created and remain in the sketch
- was requested, but all
- curve(s) project2 created in sketch '
- Projection failed in sketch '
- Curves projected onto the target face(s).
- the source curve it came from
- projectToSurface returned no entities, so the
- new sketch entities are reported from the collection census alone.
- projectToSurface created no curves in '
- target face(s) with project_type='
- project_type='along_vector' needs 'direction' - Fusion refuses the call without it ('3 : invalid parameter directionEntity') and does not fall back to closest_point.
- 'direction' steers project_type='along_vector' only; closest_point ignores it, so it is refused here rather than reported as if it had been used.
- projectToSurface failed in sketch '
- Section curves created where the geometry crosses the sketch plane.
- the body or face it was sectioned from
- They lie on the sketch plane.
- intersectWithSketchPlane returned no entities, so the
- action='intersect' needs 'bodies' (the bodies to section) and/or 'entities' (find_geometry handles at faces/edges/vertices) to cross with the sketch plane.
- . Move the sketch plane through the geometry, or pass geometry that spans it. An occurrence proxy is also ignored silently: the entity must be owned by the sketch's own component.
- Nothing you passed crosses the plane of sketch '
- ), so no sketch geometry was created:
- intersectWithSketchPlane failed in sketch '

### `sketch_set_text`
- and design recomputed so any engraving/emboss that consumes it rebuilt
- Provide 'text' or 'parameter' - the string to display or the Text parameter to bind.
- No active design (open a document with sketch text).
- '. Use mm, cm, or in.
- No sketch text matched index
- 'parameter' binds an EXISTING sketch text, so it cannot ride a create. Create the text with its starting string first, then call again with parameter='
- ' are two different string sources for one text. Pass 'parameter' alone to bind it, or 'text' alone to store a literal.
- 'height' must be > 0.
- No sketch text found in a sketch named '
- . (Use sketch_get to list sketches; the text must live in a sketch with that exact name.)
- 'height' must be a number (text height in 'units').
- ' could not be read (a stale or deleted text proxy holds that index). Re-read the sketch with sketch_get(include_entities=true) and retry with a readable index.
- Setting the font of sketch text in '
- ' did not take - SketchText.fontName reads back '
- Its string WAS set to '
- ' before the resize was checked, so that one text carries the new string at its old size.
- set the font of sketch text in '
- ' before the resize was checked.
- Sketch text created (verified: sketchTexts
- ). (x,y) are SKETCH-plane coordinates - on an on-face sketch use the 'frame' from sketch_create to keep the text on the face. Extrude/emboss the sketch to engrave it, or edit it later with sketch_s...
- Sketch text created on '
- ). A CLOSED path such as a circle wraps the text right around it. Extrude/emboss the sketch to engrave it, or edit the string later with sketch_set_text (without create).
- create=true needs 'sketch_name' - the sketch to add the text to (create one first with sketch_create).
- ' is not available on this Fusion version.
- . Create it first with sketch_create.
- Sketch text did not materialize in '
- ': sketchTexts count stayed at
- after add(). Nothing was created.
- ' but the new text reports '
- ', so the font did not take. The text WAS created - remove it with sketch_delete_entity(sketch_name='
- ' text placement (it returned false), so no text was placed.
- Could not create sketch text in '
- 'character_spacing' must be a number - the percent change from the default spacing (0 = default, 50 = half again as wide).
- create sketch text in '

### `surface_create_ruled`
- The result reads back SOLID (isSolid=true), not the open sheet a ruled surface makes - inspect it before building on it.
- '. Use mm, cm, or in.
- ruled_type='direction' needs a 'direction' entity - Fusion refuses to build the input without one (measured: "3 : invalid argument direction").
- 'direction' was given with ruled_type='
- ', which sweeps off the face the edge bounds - the direction is ignored, not applied. Pass ruled_type='direction' to sweep along '
- ', or drop 'direction'.
- No active design. Create or open a document first (see doc_new).
- 'edges' resolved to no edges. Pass find_geometry edge handles.
- is not available on this Fusion version.
- The ruled surface feature was created but added nothing: every body it reports was already in '
- New open surface body (isSolid=false), separate from the body the edges came from. Join it with model_stitch, or thicken it with surface_thicken.
- Not read back off the feature:
- The ruled surface was created, but no result body's isSolid flag could be read back, so whether it is the open sheet a ruled surface makes or a SOLID is UNVERIFIED - check it with model_inspect.
- 'angle_deg' must be a number of degrees, got '
- Fusion built no ruled-surface input from those edges, so nothing was created. Confirm the handles still resolve with find_geometry.
- Ruled surface failed:
- . The measured working shape is an edge chain on ONE body whose edges bound a face - tangent and normal are both measured off that face, so an edge with no adjacent face has nothing to leave from.

### `surface_delete_face`
- %s%s; body face count %d -> %d (%d face(s) requested).
- %s %s Neither the result-body list nor 'bodies_consumed' is available without a feature object - check the bodies with design_get(include=['tree']).
- %d input body(ies) were fully consumed by the delete - no result body remains. Deleting every face of a body removes the body.
- The face count ROSE by %d - unexpected for a delete, which normally lowers it. The edit did land (the count moved), but the requested face(s) may not be what was removed: inspect the body with desi...
- No active design. Create or open a document first (see doc_new).
- 'faces' resolved to no faces. Pass find_geometry face handles.
- Delete-face reported no error, but the face count of %d result body(ies) (%s) would not read back, so whether any face was deleted is UNVERIFIED - a count that will not read is not a count of zero....
- Delete-face reported no error but no input body's face count changed (%d -> %d) - nothing was deleted.
- The body could not be healed - retry with heal=false to remove the faces without healing.
- Delete-face ran in a DIRECT design, which returns no feature object, and no input body's face count could be read back - so whether the faces were deleted is UNVERIFIED. The body may also have been...
- Delete-face reported no error but no input body's face count changed - nothing was deleted.
- Delete-face (heal) failed:
- . The opening could not be healed - retry with heal=false to just remove the faces (a solid then becomes a surface).

### `surface_extend`
- Surface extended from its open edges.
- '. Use mm, cm, or in.
- Provide a non-zero 'distance' to extend.
- Unknown extend_type '
- '. Use: natural, tangent, perpendicular.
- Unknown extend_alignment '
- '. Use: free_edges, align_edges.
- No active design. Create or open a document first (see doc_new).
- 'edges' resolved to no edges. Pass the outer edges of ONE surface body.
- . (Extend the OUTER edges of ONE open body; tangent/perpendicular need edges connected at endpoints.)

### `surface_extrude`
- Open surface body created (isSolid=false). Feed it to surface_trim/extend/patch/thicken.
- '. Use mm, cm, or in.
- Provide a non-zero 'distance' to extrude.
- '. Surface extrude supports: new, join.
- No active design. Create or open a document first (see doc_new).
- Surface extrude reported success but the feature owns no result body - no sheet was created.
- The result reads back SOLID (isSolid=true) - the profile closed into a solid, not a sheet.
- Surface body created, but no result body's isSolid flag could be read back - whether it is an open sheet is UNVERIFIED.
- 'curves' resolved to no edges/curves.
- No sketch or 'curves' to extrude. Draw an OPEN chain first, or pass curves.
- , so no surface was extruded.
- Surface extrude failed:
- '. Use sketch_get or sketch_create.

### `surface_fill`
- . cells_volume_picked is the volume the input predicted for the kept cell(s); result_volume is what the feature's bodies measure now.
- No active design. Create or open a document first (see doc_new).
- remove_tools=true is not supported with operation='
- '. For join/cut/intersect the target body must itself be among 'tools', and remove_tools consumes the tools AFTER the boolean - measured on join: the merged result is deleted along with them, leavi...
- boundaryFillFeatures.createInput returned nothing - no boundary could be calculated from the tools given.
- The selected cell(s) produced nothing.
- The open transaction was cancelled.
- Boundary fill reported success but produced no body, consumed no tool, and left every solid in the design at its old volume - nothing was sealed.
- The empty feature was rolled back.
- The empty feature could NOT be deleted - remove it with design_delete_feature.
- Boundary fill failed:
- . (The tools must enclose a volume between them.)
- The boundary-fill input's bRepCells could not be read, so which volumes the tools enclose is unknown - nothing was created.
- Boundary fill found no cell: the tools given do not enclose a volume between them. Extend or add bodies until the region is closed.
- Boundary fill computed
- cells and 'cells' was not given - name the one(s) to keep by index:
- . Re-run with cells=[index] (several indices seal several cells in one feature). These indices are valid for the NEXT call only - measured: the same tools enumerated their cells in a different orde...

### `surface_offset`
- Faces offset into a new surface.
- Faces copied as a COINCIDENT surface (distance=0) - the zero-offset copy-face idiom.
- '. Use mm, cm, or in.
- '. Offset supports: new, new_component.
- No active design. Create or open a document first (see doc_new).
- Offset reported success but created no faces - nothing was offset. The feature remains in the timeline; remove it with design_delete_feature.

### `surface_patch`
- Some loops failed - see 'errors'.
- '. Patch supports: new, new_component.
- '. Use: connected, tangent, curvature.
- No active design. Create or open a document first (see doc_new).
- 'interior_rails' fits ONE patch surface, so it goes with 'boundary' (a single loop). With 'boundaries' every loop would be handed the same rails.
- Pass 'boundary' (one loop) or 'boundaries' (a list of loops, each an edge handle Fusion auto-completes - the way to patch every hole in one call).

### `surface_reverse_normal`
- Normals flipped - isParamReversed toggled on all %d face(s), read back off the feature.
- Reverse Normal feature created and consumed %d face(s), but the isParamReversed read-back did NOT confirm a full flip (before_reversed=%d, after_reversed=%d of %d faces). Verify with view_screenshot.
- No active design. Create or open a document first (see doc_new).
- 'bodies' resolved to no surface bodies. Pass open surface body handles/names.
- Reverse normal failed:
- . (Pass OPEN surface bodies - a solid has no free normal to flip.)

### `surface_revolve`
- Open surface body created (isSolid=false).
- Provide a non-zero 'angle_deg' to revolve (e.g. 360 for a full revolve).
- '. Surface revolve supports: new, join.
- No active design. Create or open a document first (see doc_new).
- Could not resolve the
- -axis of the active component.
- Surface revolve reported success but the feature owns no result body - no sheet was created.
- The result reads back SOLID (isSolid=true) - the profile closed into a solid, not a sheet.
- Surface body created, but no result body's isSolid flag could be read back - whether it is an open sheet is UNVERIFIED.
- angle_deg must be a number (degrees).
- 'curves' resolved to no edges/curves.
- No sketch or 'curves' to revolve. Draw an OPEN chain first, or pass curves.
- deg, so no surface was revolved.
- Surface revolve failed:
- . (The profile must be coplanar with the axis.)
- '. Use sketch_get or sketch_create.

### `surface_thicken`
- Faces thickened into a wall reading back isSolid=true - a SOLID. The surface->solid bridge.
- Faces thickened, but no created body's isSolid flag could be read back, so whether the wall closed into a SOLID is UNVERIFIED.
- '. Use mm, cm, or in.
- Provide a non-zero 'thickness' to thicken.
- '. Thicken supports: new, join, cut.
- Unknown thicken_type '
- '. Use: sharp, rounded.
- No active design. Create or open a document first (see doc_new).
- Thicken reported success but the feature owns no result body - no wall was created, so there is nothing to read isSolid back off.
- Thicken reported success but no CREATED body reads isSolid=true - the wall did not close into a solid. The feature remains in the timeline; inspect it with model_inspect or remove it with design_de...

### `surface_trim`
- Surface trimmed. Selected cells removed; the open transaction was committed via add().
- No active design. Create or open a document first (see doc_new).
- (The tool may not intersect the surface.)
- The open transaction was cancelled.
- Trim committed but the surface area did not decrease (
- cm2 before and after) - no cell was actually removed.
- Trim aborted: kept area
- mm2 is larger than target area
- mm2. HIDE other surfaces with view_set, re-read the target and retry. The transaction was cancelled.
- . (The trim tool must INTERSECT the surface and divide it.)

### `surface_untrim`
- Faces untrimmed - extent restored (area %.6f -> %.6f cm^2).
- Untrim feature created, but the untrimmed area did not exceed the original (area_before=%.6f, area_after=%.6f cm^2). The loop may already be at the natural boundary, or the created faces could not ...
- '. Use: all, external, internal.
- '. Use mm, cm, or in.
- No active design. Create or open a document first (see doc_new).
- 'faces' resolved to no faces. Pass find_geometry face handles.
- The selected loops could not be removed.
- ] belongs to a SOLID body - untrim only restores faces on OPEN surface bodies. Unstitch or delete-face the solid first.
- 'extension' must be positive (0 = untrim to the natural boundary, no extension).
- Untrim could not build an input from those faces - a selected loop may have a connected face (only single-face loops can be untrimmed).
- . (Only loops with no connected face, on an OPEN surface, can be untrimmed.)
- 'extension' must be a number.

### `sys_capability_map`
- The BREADTH map (what families exist + each one's entry tool). To go deeper, search within a family with sys_find_tool (e.g. sys_find_tool('surface')). sys_get_guidance carries this server's packag...
- A gated tool with enabled_now false is disabled here; use enable_path. For a tool this map names as present, 'No such tool available' can mean a stale client tool list or deny rule. Compare schema_...

### `sys_find_tool`
- No tool or input-kind matched. Try broader/different keywords, or see sys_capability_map for the family overview (breadth) to pick a branch to search.
- Provide 'query' - keywords to search tool names/descriptions/inputs (and the _inputs.py kinds). E.g. 'profile', 'cam geometry', 'reference a body'.
- Before adding a tool input that REFERENCES existing geometry/profile/body/etc., use one of these _inputs.py kinds (extend the kind if it's close); don't hand-roll a name/index. See CLAUDE.md 'Input...

### `sys_get_api_doc`
- Provide 'searchPattern' (a regex matched against API names/docs).
- apiCategory must be one of: class, member, description, all; got
- No API modules in scope for filter
- . Use an importable namespace from
- Declaration search only; runtime behavior and entitlement are not tested. Keep the query and cap unchanged when using next_offset; use next_doc_offset as doc_offset for more text. Documentation mar...
- Invalid regex 'searchPattern'
- ' must be a non-negative integer; got
- 'max_results' must be an integer; got

### `sys_get_guidance`
- Ask for one or the other: section='
- ' returns that section's rules, recipe='
- ' returns that one recipe. Call twice.
- The packaged guidance document carries no section '
- The packaged guidance document carries no recipe '

### `sys_get_preferences`
- nest one level further, by product name: sys_set_preferences addresses those members as '<group>.<product>.<member>'. tier 'W' = sys_set_preferences can set it; tier 'R' = refused there, with the r...
- (one group per name).
- Application preferences - they belong to the application, not to any document. Pull deeper with include=
- app.preferences did not read - the application preferences are unavailable.

### `sys_get_selection`
- No Fusion user interface available.
- Nothing is selected in Fusion. Ask the user to click an entity, then call sys_get_selection again (or re-run sys_request_selection).
- Could not read the selection:

### `sys_reload_addin`
- ), so firing it would reach nothing and the add-in would keep running the code already in memory. Reload it from Fusion's Scripts and Add-Ins dialog (Shift+S) instead - stop the add-in, then run it.
- Reload NOT scheduled: the deferred-reload event is not installed (

### `sys_request_selection`
- No selection was made within
- s. Nothing was picked - an expected outcome, not a tool defect. Do NOT re-fire this tool in a loop: an unanswered hold usually means the user is not at the Fusion window or never learned a pick was...
- A sys_request_selection call is already waiting (
- s so far) - only one can be pending at a time. Wait for it to finish or time out, then retry.
- Could not set up the selection request (main thread unreachable).
- Could not start the selection request:
- Could not read the completed selection:

### `sys_set_preferences`
- Application preference - it belongs to no document, and there is no undo and no version history. Restore by calling this tool with the 'previous' value.
- Provide 'member' as the path sys_get_preferences reports it at - it lists every member, its value and its tier.
- Provide 'value' for '
- ' - nothing was written.
- ' is read-only through this server -
- . Nothing was written; read it with sys_get_preferences.
- app.preferences did not read - nothing was written.
- ' raises when read on this build, so its current value cannot be captured - and a preference has no undo, so an unrestorable value is not written.
- but now RAISES when read, so whether it landed cannot be verified. Its value before the write was
- - restore it by hand if the application misbehaves.
- was requested, it now reads
- before the call. Set it again with
- if the value it holds now is wrong.

### `view_list_workspaces`
- Could not list workspaces:

### `view_screenshot`
- No active viewport (is a document open?).
- fit_to: nothing matched '
- '. Use design_get(include=['tree']) for occurrences, find_geometry for a body.

### `view_screenshot_multi`
- No active viewport (is a document open?).
- No views were captured.

### `view_section`
- Use view_screenshot to study the interior; flip=true cuts the other half; view_section(clear) removes the cut.
- and the camera is aimed at the cut face.
- ; the camera was left where it was (auto_view=false).
- No active design. Open a document with design geometry first.
- All section analyses removed - the model is no longer cut.
- section analysis(es), but the remaining count could not be read back - view_section(list) confirms whether the model is still cut.
- '. Use mm, cm, or in.
- Section creation returned nothing (
- Could not read how many section analyses exist - nothing was removed. Retry, or delete them from the browser's Analysis folder.
- section analysis(es) (
- ) - the model is STILL cut by those.
- were removed. Delete the rest from the browser's Analysis folder.
- section analysis(es) but sectionAnalyses still reads
- - the model may still be cut.
- ' has no readable bounding box, so there is no centre to cut through and nothing was cut. Pass 'plane' with an explicit 'offset' to place the cut yourself.
- Provide 'plane' (an origin alias xy/xz/yz, a construction-plane name, or a planar-face handle from find_geometry) or 'through' (an occurrence).
- Failed to create section (

### `view_set`
- 'projection'/'perspective_angle_deg' apply to action='orient', not action='
- No active design. Open a document with design geometry first.
- Current camera, visual style, and all occurrence visibility saved. Explore freely; call view_set(restore) to put it all back.
- PARTIAL: this assembly holds more than
- occurrences, so only the first
- had their visibility saved - restore will not reinstate the rest. Camera and visual style are complete.
- Camera aimed. Call view_screenshot to capture.
- Could not read what the viewport currently shows, so the view could not be framed on '
- ' and the camera was NOT moved. Re-run with fit=false to re-aim only.
- Camera aimed and framed on '
- '. Call view_screenshot to capture.
- ' WITHOUT zooming to it - fit=false keeps the current eye-to-target distance. Pass fit=true (the default) to frame it.
- has a readable bounding box, so there is nothing to frame on. Re-run with fit=false to re-aim only.
- Unknown orientation '
- 'perspective_angle_deg' is a field-of-view angle Fusion accepts from 1 to just under 150 degrees (got
- 'perspective_angle_deg'=
- needs a perspective camera, but the projection in effect is '
- '. Pass projection='perspective' in the same call.
- ' but the camera's cameraType could not be read back - the projection is unverified.
- ' but the viewport camera reads back '
- ' - the change did not take.
- Set 'perspective_angle_deg'=
- but the camera's perspectiveAngle could not be read back
- - the field of view is unverified.
- Set the camera extents to frame '
- ) but the viewport reads back
- - the framing did not take, and the view is left where it was.
- 'perspective_angle_deg' must be a number (got '
- needs a perspective camera, but the camera's cameraType could not be read - pass projection='perspective' in the same call to set it explicitly.
- ' against the current view (a bounding box, the camera's axes, or its extents would not read), so the view is NOT framed on it. Re-run with fit=false to re-aim only.
- 'focus': nothing named '
- ' to frame - no occurrence and no sketch carries that name.
- Occurrence lookup said:
- Visibility changed. view_screenshot to view; view_set(restore) to undo.
- Visibility changed. view_screenshot to view. Body bulbs are NOT captured by snapshot/restore - undo a body with the opposite hide/show.
- PARTIAL: only the first
- occurrences were checked - an isolation past the cap is still set.
- isolate needs exactly one occurrence;
- targets resolved. For several, use snapshot, clear_isolation, then hide unwanted occurrences and show selected occurrence lists. restore reapplies saved occurrence visibility.
- ' but isLightBulbOn reads back
- - the change did not take.
- ' but isIsolated reads back
- ' but the ancestor bulb does not read back on for
- , so the body stays hidden.
- ': the light bulb does not read back on for
- , so it stays hidden.
- (the per-component folder bulbs; each entity's own bulb is untouched, so re-showing restores what was individually visible before). view_screenshot to see the result.
- Provide 'visible' - true to show the chosen categories, false to hide them.
- Unknown display categories:
- 'visible' must be true or false; got '
- Provide 'style' - one of:
- Camera, visual style, and visibility restored to the pre-snapshot state.
- PARTIAL: this assembly is past the
- occurrence(s) were restored and the rest keep whatever visibility they carry now. Camera and visual style are restored in full.
- . The snapshot was KEPT so view_set(restore) can be retried.
- saved state(s) did not read back as restored (
- ; the camera assignment failed:
- No snapshot saved for '
- '. Call view_set(snapshot) first. (Snapshots are held in memory for this session only - reloading the add-in clears them. To recover a clean state without a snapshot, use clear_isolation then show ...
- Provide 'view_name' to save the current camera as a named view.
- This design does not expose Named Views.
- Failed to save named view '
- Camera saved as a persistent named view. Recall it with apply_view, or pair with view_section for a section perspective.
- ' already exists and deleteMe() refused to remove it - saving now would leave two views sharing that name. Choose another 'view_name'.
- Provide 'view_name' to apply.
- apply() returned false for named view '
- ' - the camera was not moved.
- Camera moved to the named view (camera only - does not change any active section cut or visibility). If a section is live and this view was a section perspective, re-issue view_section(cut, ...) to...
- Applying named view '

### `view_switch_workspace`
- Provide 'workspace' - an id, visible name, or alias (e.g. 'design', 'manufacture').
- Workspace not found: '
- activate() returned true, but neither Workspace.isActive nor the UI's active workspace could be read back - the switch is UNVERIFIED. view_list_workspaces reports which workspace is active.
- Could not enumerate workspaces:
- ' failed (it may not be valid to switch to right now, e.g. no document open).
- activate() returned true for '
- ', its isActive flag would not read, and the UI reports '
- ' as the active workspace - the switch did not take.
- Failed to switch to '
- Workspace was already active.
- ' but it still reads isActive=false - the workspace did not become active.

### `workspace_orient`
- Document is UNSAVED - no URN/project yet; save before addressing it by id.
- browser_digest is DEPTH-1: is_xref describes each row ITSELF, so a reference nested below one leaves every flag false - doc_get(include=['xref_tree']) walks every depth. references.referenced_docum...
- Design is LARGE - prefer scoped calls.
- A document is open but no Design product is active. Switch to the Design workspace, or use the CAM tools if has_cam is true.
- No active document. Open or create one first (see doc_new / doc_open).
- out-of-date reference(s) - run doc_update_xref.
- A null size/center component could not be read or converted to '
- ' - it is reported as null rather than a fabricated 0 or an unconverted centimetre value.

