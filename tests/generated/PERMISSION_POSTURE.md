# Permission Posture (generated)

_Auto-generated from the live registry by `tests/gen_posture.py`. Do not edit by hand - re-run the generator after adding/renaming a tool or changing its write= kind. `--check` (via `tests/gen_all.py --check`) fails the suite if this is stale._

Every tool declares a write= kind (read / write / destructive); the MCP readOnlyHint / destructiveHint annotations derive from it. That machine-checked fact decides which tools are safe to auto-run under Claude Code. This file maps every tool to a posture bucket and emits ready-to-paste `settings.json` presets. Rules target the MCP wire name `mcp__fusion-essentials__<tool>`.

**Tools:** 188  |  read: 30  |  write: 139  |  destructive: 18  |  script-hatch: 1

## Posture buckets

| Bucket | write= kind | Recommended handling |
|---|---|---|
| read | read | Safe to auto-allow - changes nothing. |
| write | write | Ask - mutates the model or runs an async operation. |
| destructive | destructive | Ask or deny - hard-to-reverse (delete, close, history-discarding). |
| script-hatch | destructive | NEVER auto-approve - `sys_execute_script` runs arbitrary Fusion API code. Deny by default. |

## The arbitrary-code hatch (never auto-approved)

`sys_execute_script` executes operator-supplied Fusion API scripts - it can do anything any other tool can, and more. No posture in this file ever places it in an allow list; it is denied in every preset below. Enable it only for a trusted, supervised session.

## Writes that leave the document (the modeling posture asks for these)

A write= kind says the model changes; it does not say WHERE the change lands. These write-kind tools put bytes outside the active document - on the filesystem, into a shared library other documents read, or into the add-in itself - so the modeling posture, which promises to auto-allow local model work an operator can see and undo, asks before each one. Cloud/document-lifecycle families (`data`, `doc`) ask for the same reason.

| Tool | Where the effect lands |
|---|---|
| `mcp__fusion-essentials__cam_create_machine` | writes the shared machine library, which every other document reads |
| `mcp__fusion-essentials__cam_edit_tools` | writes the shared CAM tool library, which every other document reads |
| `mcp__fusion-essentials__cam_generate_setup_sheet` | writes a setup-sheet document (HTML/Excel) to disk |
| `mcp__fusion-essentials__cam_post` | writes an NC program to disk - the file a machine then runs |
| `mcp__fusion-essentials__cam_save_template` | writes a toolpath template into the shared CAM template library |
| `mcp__fusion-essentials__design_export` | writes a design file (f3d/step/...) to disk |
| `mcp__fusion-essentials__drawing_export` | writes a drawing file (pdf/dwg/...) to disk |
| `mcp__fusion-essentials__mesh_export` | writes a mesh file (stl/obj/3mf) to disk |
| `mcp__fusion-essentials__sys_reload_addin` | restarts the add-in - it tears down and reloads the running server |

## Every tool by bucket

### read - safe to auto-allow (30)

- `mcp__fusion-essentials__assembly_get`
- `mcp__fusion-essentials__assembly_inspect_interference`
- `mcp__fusion-essentials__cam_compare_operations`
- `mcp__fusion-essentials__cam_get`
- `mcp__fusion-essentials__cam_get_status`
- `mcp__fusion-essentials__cam_inspect_toolpaths`
- `mcp__fusion-essentials__data_get`
- `mcp__fusion-essentials__data_get_upload_status`
- `mcp__fusion-essentials__design_get`
- `mcp__fusion-essentials__doc_get`
- `mcp__fusion-essentials__drawing_get`
- `mcp__fusion-essentials__drawing_get_status`
- `mcp__fusion-essentials__find_geometry`
- `mcp__fusion-essentials__mesh_get`
- `mcp__fusion-essentials__model_compute_holder`
- `mcp__fusion-essentials__model_inspect`
- `mcp__fusion-essentials__model_measure_between`
- `mcp__fusion-essentials__model_measure_relation`
- `mcp__fusion-essentials__param_get`
- `mcp__fusion-essentials__pmi_get`
- `mcp__fusion-essentials__sketch_get`
- `mcp__fusion-essentials__sys_capability_map`
- `mcp__fusion-essentials__sys_find_tool`
- `mcp__fusion-essentials__sys_get_api_doc`
- `mcp__fusion-essentials__sys_get_guidance`
- `mcp__fusion-essentials__sys_get_preferences`
- `mcp__fusion-essentials__sys_get_selection`
- `mcp__fusion-essentials__view_list_workspaces`
- `mcp__fusion-essentials__view_screenshot_multi`
- `mcp__fusion-essentials__workspace_orient`

### write - ask (139)

- `mcp__fusion-essentials__appearance_set`
- `mcp__fusion-essentials__assembly_capture_position`
- `mcp__fusion-essentials__assembly_constrain`
- `mcp__fusion-essentials__assembly_ground`
- `mcp__fusion-essentials__assembly_move`
- `mcp__fusion-essentials__assembly_rigid_group`
- `mcp__fusion-essentials__cam_activate_setup`
- `mcp__fusion-essentials__cam_apply_template`
- `mcp__fusion-essentials__cam_create_machine`
- `mcp__fusion-essentials__cam_create_operation`
- `mcp__fusion-essentials__cam_create_setup`
- `mcp__fusion-essentials__cam_edit_folders`
- `mcp__fusion-essentials__cam_edit_operation`
- `mcp__fusion-essentials__cam_edit_setup`
- `mcp__fusion-essentials__cam_edit_tools`
- `mcp__fusion-essentials__cam_generate`
- `mcp__fusion-essentials__cam_generate_setup_sheet`
- `mcp__fusion-essentials__cam_post`
- `mcp__fusion-essentials__cam_reorder`
- `mcp__fusion-essentials__cam_save_template`
- `mcp__fusion-essentials__cam_select_geometry`
- `mcp__fusion-essentials__cam_set_nc_comment`
- `mcp__fusion-essentials__cam_show_toolpath`
- `mcp__fusion-essentials__data_create_folder`
- `mcp__fusion-essentials__data_create_project`
- `mcp__fusion-essentials__data_download_file`
- `mcp__fusion-essentials__data_move_file`
- `mcp__fusion-essentials__data_switch_hub`
- `mcp__fusion-essentials__data_upload_file`
- `mcp__fusion-essentials__design_activate_component`
- `mcp__fusion-essentials__design_add_instance`
- `mcp__fusion-essentials__design_configure`
- `mcp__fusion-essentials__design_export`
- `mcp__fusion-essentials__design_move_occurrence`
- `mcp__fusion-essentials__design_recompute`
- `mcp__fusion-essentials__design_remove_feature`
- `mcp__fusion-essentials__design_set_name`
- `mcp__fusion-essentials__doc_activate`
- `mcp__fusion-essentials__doc_copy`
- `mcp__fusion-essentials__doc_insert_derive`
- `mcp__fusion-essentials__doc_insert_import`
- `mcp__fusion-essentials__doc_insert_occurrence`
- `mcp__fusion-essentials__doc_new`
- `mcp__fusion-essentials__doc_open`
- `mcp__fusion-essentials__doc_restore_version`
- `mcp__fusion-essentials__doc_save`
- `mcp__fusion-essentials__doc_save_as`
- `mcp__fusion-essentials__doc_save_milestone`
- `mcp__fusion-essentials__doc_update_xref`
- `mcp__fusion-essentials__drawing_add_sketch`
- `mcp__fusion-essentials__drawing_create`
- `mcp__fusion-essentials__drawing_dimension`
- `mcp__fusion-essentials__drawing_export`
- `mcp__fusion-essentials__drawing_insert_image`
- `mcp__fusion-essentials__drawing_update`
- `mcp__fusion-essentials__joint_at_geometry`
- `mcp__fusion-essentials__joint_create`
- `mcp__fusion-essentials__joint_create_as_built`
- `mcp__fusion-essentials__joint_create_origin`
- `mcp__fusion-essentials__joint_drive`
- `mcp__fusion-essentials__joint_edit`
- `mcp__fusion-essentials__joint_motion_link`
- `mcp__fusion-essentials__mesh_combine`
- `mcp__fusion-essentials__mesh_export`
- `mcp__fusion-essentials__mesh_generate_face_groups`
- `mcp__fusion-essentials__mesh_insert`
- `mcp__fusion-essentials__mesh_plane_cut`
- `mcp__fusion-essentials__mesh_reduce`
- `mcp__fusion-essentials__mesh_remesh`
- `mcp__fusion-essentials__mesh_repair`
- `mcp__fusion-essentials__mesh_reverse_normal`
- `mcp__fusion-essentials__mesh_separate`
- `mcp__fusion-essentials__mesh_shell`
- `mcp__fusion-essentials__mesh_smooth`
- `mcp__fusion-essentials__mesh_to_brep`
- `mcp__fusion-essentials__model_arrange`
- `mcp__fusion-essentials__model_base_feature`
- `mcp__fusion-essentials__model_chamfer`
- `mcp__fusion-essentials__model_combine`
- `mcp__fusion-essentials__model_construction`
- `mcp__fusion-essentials__model_create_component`
- `mcp__fusion-essentials__model_draft`
- `mcp__fusion-essentials__model_emboss`
- `mcp__fusion-essentials__model_extrude`
- `mcp__fusion-essentials__model_fillet`
- `mcp__fusion-essentials__model_hole`
- `mcp__fusion-essentials__model_loft`
- `mcp__fusion-essentials__model_mirror`
- `mcp__fusion-essentials__model_move`
- `mcp__fusion-essentials__model_offset_face`
- `mcp__fusion-essentials__model_pattern_circular`
- `mcp__fusion-essentials__model_pattern_path`
- `mcp__fusion-essentials__model_pattern_rectangular`
- `mcp__fusion-essentials__model_pipe`
- `mcp__fusion-essentials__model_replace_face`
- `mcp__fusion-essentials__model_revolve`
- `mcp__fusion-essentials__model_scale`
- `mcp__fusion-essentials__model_set_material`
- `mcp__fusion-essentials__model_shell`
- `mcp__fusion-essentials__model_split`
- `mcp__fusion-essentials__model_stitch`
- `mcp__fusion-essentials__model_sweep`
- `mcp__fusion-essentials__model_thread`
- `mcp__fusion-essentials__model_unstitch`
- `mcp__fusion-essentials__param_add`
- `mcp__fusion-essentials__param_set`
- `mcp__fusion-essentials__param_set_favorite`
- `mcp__fusion-essentials__pmi_create`
- `mcp__fusion-essentials__pmi_edit`
- `mcp__fusion-essentials__save_as_mesh`
- `mcp__fusion-essentials__sketch_add_3d_line`
- `mcp__fusion-essentials__sketch_add_geometry`
- `mcp__fusion-essentials__sketch_constrain`
- `mcp__fusion-essentials__sketch_copy`
- `mcp__fusion-essentials__sketch_create`
- `mcp__fusion-essentials__sketch_dimension`
- `mcp__fusion-essentials__sketch_edit_curve`
- `mcp__fusion-essentials__sketch_insert_svg`
- `mcp__fusion-essentials__sketch_move`
- `mcp__fusion-essentials__sketch_project`
- `mcp__fusion-essentials__sketch_set_text`
- `mcp__fusion-essentials__surface_create_ruled`
- `mcp__fusion-essentials__surface_delete_face`
- `mcp__fusion-essentials__surface_extend`
- `mcp__fusion-essentials__surface_extrude`
- `mcp__fusion-essentials__surface_fill`
- `mcp__fusion-essentials__surface_offset`
- `mcp__fusion-essentials__surface_patch`
- `mcp__fusion-essentials__surface_reverse_normal`
- `mcp__fusion-essentials__surface_revolve`
- `mcp__fusion-essentials__surface_thicken`
- `mcp__fusion-essentials__surface_trim`
- `mcp__fusion-essentials__surface_untrim`
- `mcp__fusion-essentials__sys_reload_addin`
- `mcp__fusion-essentials__sys_request_selection`
- `mcp__fusion-essentials__view_screenshot`
- `mcp__fusion-essentials__view_section`
- `mcp__fusion-essentials__view_set`
- `mcp__fusion-essentials__view_switch_workspace`

### destructive - ask / deny (18)

- `mcp__fusion-essentials__assembly_edit_contacts`
- `mcp__fusion-essentials__assembly_edit_relations`
- `mcp__fusion-essentials__cam_delete`
- `mcp__fusion-essentials__cam_delete_machine`
- `mcp__fusion-essentials__cam_delete_template`
- `mcp__fusion-essentials__data_delete_file`
- `mcp__fusion-essentials__data_delete_folder`
- `mcp__fusion-essentials__design_delete_feature`
- `mcp__fusion-essentials__design_delete_occurrence`
- `mcp__fusion-essentials__design_edit_timeline`
- `mcp__fusion-essentials__design_set_mode`
- `mcp__fusion-essentials__doc_close`
- `mcp__fusion-essentials__drawing_edit_sheet`
- `mcp__fusion-essentials__mesh_delete`
- `mcp__fusion-essentials__param_delete`
- `mcp__fusion-essentials__pmi_delete`
- `mcp__fusion-essentials__sketch_delete_entity`
- `mcp__fusion-essentials__sys_set_preferences`

## Ready-to-paste settings.json presets

Paste one `permissions` block into `.claude/settings.json` (or merge its arrays into an existing one). Both presets are generated from the registry, so they stay complete as tools are added. Anything not listed falls through to Claude Code's default prompt.

### Preset: conservative

Auto-allow reads only. Every write asks; destructive writes and the arbitrary-code hatch are denied. The safest default.

```json
{
  "permissions": {
    "allow": [
      "mcp__fusion-essentials__assembly_get",
      "mcp__fusion-essentials__assembly_inspect_interference",
      "mcp__fusion-essentials__cam_compare_operations",
      "mcp__fusion-essentials__cam_get",
      "mcp__fusion-essentials__cam_get_status",
      "mcp__fusion-essentials__cam_inspect_toolpaths",
      "mcp__fusion-essentials__data_get",
      "mcp__fusion-essentials__data_get_upload_status",
      "mcp__fusion-essentials__design_get",
      "mcp__fusion-essentials__doc_get",
      "mcp__fusion-essentials__drawing_get",
      "mcp__fusion-essentials__drawing_get_status",
      "mcp__fusion-essentials__find_geometry",
      "mcp__fusion-essentials__mesh_get",
      "mcp__fusion-essentials__model_compute_holder",
      "mcp__fusion-essentials__model_inspect",
      "mcp__fusion-essentials__model_measure_between",
      "mcp__fusion-essentials__model_measure_relation",
      "mcp__fusion-essentials__param_get",
      "mcp__fusion-essentials__pmi_get",
      "mcp__fusion-essentials__sketch_get",
      "mcp__fusion-essentials__sys_capability_map",
      "mcp__fusion-essentials__sys_find_tool",
      "mcp__fusion-essentials__sys_get_api_doc",
      "mcp__fusion-essentials__sys_get_guidance",
      "mcp__fusion-essentials__sys_get_preferences",
      "mcp__fusion-essentials__sys_get_selection",
      "mcp__fusion-essentials__view_list_workspaces",
      "mcp__fusion-essentials__view_screenshot_multi",
      "mcp__fusion-essentials__workspace_orient"
    ],
    "ask": [
      "mcp__fusion-essentials__appearance_set",
      "mcp__fusion-essentials__assembly_capture_position",
      "mcp__fusion-essentials__assembly_constrain",
      "mcp__fusion-essentials__assembly_ground",
      "mcp__fusion-essentials__assembly_move",
      "mcp__fusion-essentials__assembly_rigid_group",
      "mcp__fusion-essentials__cam_activate_setup",
      "mcp__fusion-essentials__cam_apply_template",
      "mcp__fusion-essentials__cam_create_machine",
      "mcp__fusion-essentials__cam_create_operation",
      "mcp__fusion-essentials__cam_create_setup",
      "mcp__fusion-essentials__cam_edit_folders",
      "mcp__fusion-essentials__cam_edit_operation",
      "mcp__fusion-essentials__cam_edit_setup",
      "mcp__fusion-essentials__cam_edit_tools",
      "mcp__fusion-essentials__cam_generate",
      "mcp__fusion-essentials__cam_generate_setup_sheet",
      "mcp__fusion-essentials__cam_post",
      "mcp__fusion-essentials__cam_reorder",
      "mcp__fusion-essentials__cam_save_template",
      "mcp__fusion-essentials__cam_select_geometry",
      "mcp__fusion-essentials__cam_set_nc_comment",
      "mcp__fusion-essentials__cam_show_toolpath",
      "mcp__fusion-essentials__data_create_folder",
      "mcp__fusion-essentials__data_create_project",
      "mcp__fusion-essentials__data_download_file",
      "mcp__fusion-essentials__data_move_file",
      "mcp__fusion-essentials__data_switch_hub",
      "mcp__fusion-essentials__data_upload_file",
      "mcp__fusion-essentials__design_activate_component",
      "mcp__fusion-essentials__design_add_instance",
      "mcp__fusion-essentials__design_configure",
      "mcp__fusion-essentials__design_export",
      "mcp__fusion-essentials__design_move_occurrence",
      "mcp__fusion-essentials__design_recompute",
      "mcp__fusion-essentials__design_remove_feature",
      "mcp__fusion-essentials__design_set_name",
      "mcp__fusion-essentials__doc_activate",
      "mcp__fusion-essentials__doc_copy",
      "mcp__fusion-essentials__doc_insert_derive",
      "mcp__fusion-essentials__doc_insert_import",
      "mcp__fusion-essentials__doc_insert_occurrence",
      "mcp__fusion-essentials__doc_new",
      "mcp__fusion-essentials__doc_open",
      "mcp__fusion-essentials__doc_restore_version",
      "mcp__fusion-essentials__doc_save",
      "mcp__fusion-essentials__doc_save_as",
      "mcp__fusion-essentials__doc_save_milestone",
      "mcp__fusion-essentials__doc_update_xref",
      "mcp__fusion-essentials__drawing_add_sketch",
      "mcp__fusion-essentials__drawing_create",
      "mcp__fusion-essentials__drawing_dimension",
      "mcp__fusion-essentials__drawing_export",
      "mcp__fusion-essentials__drawing_insert_image",
      "mcp__fusion-essentials__drawing_update",
      "mcp__fusion-essentials__joint_at_geometry",
      "mcp__fusion-essentials__joint_create",
      "mcp__fusion-essentials__joint_create_as_built",
      "mcp__fusion-essentials__joint_create_origin",
      "mcp__fusion-essentials__joint_drive",
      "mcp__fusion-essentials__joint_edit",
      "mcp__fusion-essentials__joint_motion_link",
      "mcp__fusion-essentials__mesh_combine",
      "mcp__fusion-essentials__mesh_export",
      "mcp__fusion-essentials__mesh_generate_face_groups",
      "mcp__fusion-essentials__mesh_insert",
      "mcp__fusion-essentials__mesh_plane_cut",
      "mcp__fusion-essentials__mesh_reduce",
      "mcp__fusion-essentials__mesh_remesh",
      "mcp__fusion-essentials__mesh_repair",
      "mcp__fusion-essentials__mesh_reverse_normal",
      "mcp__fusion-essentials__mesh_separate",
      "mcp__fusion-essentials__mesh_shell",
      "mcp__fusion-essentials__mesh_smooth",
      "mcp__fusion-essentials__mesh_to_brep",
      "mcp__fusion-essentials__model_arrange",
      "mcp__fusion-essentials__model_base_feature",
      "mcp__fusion-essentials__model_chamfer",
      "mcp__fusion-essentials__model_combine",
      "mcp__fusion-essentials__model_construction",
      "mcp__fusion-essentials__model_create_component",
      "mcp__fusion-essentials__model_draft",
      "mcp__fusion-essentials__model_emboss",
      "mcp__fusion-essentials__model_extrude",
      "mcp__fusion-essentials__model_fillet",
      "mcp__fusion-essentials__model_hole",
      "mcp__fusion-essentials__model_loft",
      "mcp__fusion-essentials__model_mirror",
      "mcp__fusion-essentials__model_move",
      "mcp__fusion-essentials__model_offset_face",
      "mcp__fusion-essentials__model_pattern_circular",
      "mcp__fusion-essentials__model_pattern_path",
      "mcp__fusion-essentials__model_pattern_rectangular",
      "mcp__fusion-essentials__model_pipe",
      "mcp__fusion-essentials__model_replace_face",
      "mcp__fusion-essentials__model_revolve",
      "mcp__fusion-essentials__model_scale",
      "mcp__fusion-essentials__model_set_material",
      "mcp__fusion-essentials__model_shell",
      "mcp__fusion-essentials__model_split",
      "mcp__fusion-essentials__model_stitch",
      "mcp__fusion-essentials__model_sweep",
      "mcp__fusion-essentials__model_thread",
      "mcp__fusion-essentials__model_unstitch",
      "mcp__fusion-essentials__param_add",
      "mcp__fusion-essentials__param_set",
      "mcp__fusion-essentials__param_set_favorite",
      "mcp__fusion-essentials__pmi_create",
      "mcp__fusion-essentials__pmi_edit",
      "mcp__fusion-essentials__save_as_mesh",
      "mcp__fusion-essentials__sketch_add_3d_line",
      "mcp__fusion-essentials__sketch_add_geometry",
      "mcp__fusion-essentials__sketch_constrain",
      "mcp__fusion-essentials__sketch_copy",
      "mcp__fusion-essentials__sketch_create",
      "mcp__fusion-essentials__sketch_dimension",
      "mcp__fusion-essentials__sketch_edit_curve",
      "mcp__fusion-essentials__sketch_insert_svg",
      "mcp__fusion-essentials__sketch_move",
      "mcp__fusion-essentials__sketch_project",
      "mcp__fusion-essentials__sketch_set_text",
      "mcp__fusion-essentials__surface_create_ruled",
      "mcp__fusion-essentials__surface_delete_face",
      "mcp__fusion-essentials__surface_extend",
      "mcp__fusion-essentials__surface_extrude",
      "mcp__fusion-essentials__surface_fill",
      "mcp__fusion-essentials__surface_offset",
      "mcp__fusion-essentials__surface_patch",
      "mcp__fusion-essentials__surface_reverse_normal",
      "mcp__fusion-essentials__surface_revolve",
      "mcp__fusion-essentials__surface_thicken",
      "mcp__fusion-essentials__surface_trim",
      "mcp__fusion-essentials__surface_untrim",
      "mcp__fusion-essentials__sys_reload_addin",
      "mcp__fusion-essentials__sys_request_selection",
      "mcp__fusion-essentials__view_screenshot",
      "mcp__fusion-essentials__view_section",
      "mcp__fusion-essentials__view_set",
      "mcp__fusion-essentials__view_switch_workspace"
    ],
    "deny": [
      "mcp__fusion-essentials__assembly_edit_contacts",
      "mcp__fusion-essentials__assembly_edit_relations",
      "mcp__fusion-essentials__cam_delete",
      "mcp__fusion-essentials__cam_delete_machine",
      "mcp__fusion-essentials__cam_delete_template",
      "mcp__fusion-essentials__data_delete_file",
      "mcp__fusion-essentials__data_delete_folder",
      "mcp__fusion-essentials__design_delete_feature",
      "mcp__fusion-essentials__design_delete_occurrence",
      "mcp__fusion-essentials__design_edit_timeline",
      "mcp__fusion-essentials__design_set_mode",
      "mcp__fusion-essentials__doc_close",
      "mcp__fusion-essentials__drawing_edit_sheet",
      "mcp__fusion-essentials__mesh_delete",
      "mcp__fusion-essentials__param_delete",
      "mcp__fusion-essentials__pmi_delete",
      "mcp__fusion-essentials__sketch_delete_entity",
      "mcp__fusion-essentials__sys_set_preferences",
      "mcp__fusion-essentials__sys_execute_script"
    ]
  }
}
```

### Preset: modeling

Auto-allow reads and LOCAL model writes (extrude, joint, sketch, ...). Cloud/document writes, writes whose effect LEAVES the document (see 'Writes that leave the document' above), and every destructive write ask; the script hatch is denied.

```json
{
  "permissions": {
    "allow": [
      "mcp__fusion-essentials__assembly_get",
      "mcp__fusion-essentials__assembly_inspect_interference",
      "mcp__fusion-essentials__cam_compare_operations",
      "mcp__fusion-essentials__cam_get",
      "mcp__fusion-essentials__cam_get_status",
      "mcp__fusion-essentials__cam_inspect_toolpaths",
      "mcp__fusion-essentials__data_get",
      "mcp__fusion-essentials__data_get_upload_status",
      "mcp__fusion-essentials__design_get",
      "mcp__fusion-essentials__doc_get",
      "mcp__fusion-essentials__drawing_get",
      "mcp__fusion-essentials__drawing_get_status",
      "mcp__fusion-essentials__find_geometry",
      "mcp__fusion-essentials__mesh_get",
      "mcp__fusion-essentials__model_compute_holder",
      "mcp__fusion-essentials__model_inspect",
      "mcp__fusion-essentials__model_measure_between",
      "mcp__fusion-essentials__model_measure_relation",
      "mcp__fusion-essentials__param_get",
      "mcp__fusion-essentials__pmi_get",
      "mcp__fusion-essentials__sketch_get",
      "mcp__fusion-essentials__sys_capability_map",
      "mcp__fusion-essentials__sys_find_tool",
      "mcp__fusion-essentials__sys_get_api_doc",
      "mcp__fusion-essentials__sys_get_guidance",
      "mcp__fusion-essentials__sys_get_preferences",
      "mcp__fusion-essentials__sys_get_selection",
      "mcp__fusion-essentials__view_list_workspaces",
      "mcp__fusion-essentials__view_screenshot_multi",
      "mcp__fusion-essentials__workspace_orient",
      "mcp__fusion-essentials__appearance_set",
      "mcp__fusion-essentials__assembly_capture_position",
      "mcp__fusion-essentials__assembly_constrain",
      "mcp__fusion-essentials__assembly_ground",
      "mcp__fusion-essentials__assembly_move",
      "mcp__fusion-essentials__assembly_rigid_group",
      "mcp__fusion-essentials__cam_activate_setup",
      "mcp__fusion-essentials__cam_apply_template",
      "mcp__fusion-essentials__cam_create_operation",
      "mcp__fusion-essentials__cam_create_setup",
      "mcp__fusion-essentials__cam_edit_folders",
      "mcp__fusion-essentials__cam_edit_operation",
      "mcp__fusion-essentials__cam_edit_setup",
      "mcp__fusion-essentials__cam_generate",
      "mcp__fusion-essentials__cam_reorder",
      "mcp__fusion-essentials__cam_select_geometry",
      "mcp__fusion-essentials__cam_set_nc_comment",
      "mcp__fusion-essentials__cam_show_toolpath",
      "mcp__fusion-essentials__design_activate_component",
      "mcp__fusion-essentials__design_add_instance",
      "mcp__fusion-essentials__design_configure",
      "mcp__fusion-essentials__design_move_occurrence",
      "mcp__fusion-essentials__design_recompute",
      "mcp__fusion-essentials__design_remove_feature",
      "mcp__fusion-essentials__design_set_name",
      "mcp__fusion-essentials__drawing_add_sketch",
      "mcp__fusion-essentials__drawing_create",
      "mcp__fusion-essentials__drawing_dimension",
      "mcp__fusion-essentials__drawing_insert_image",
      "mcp__fusion-essentials__drawing_update",
      "mcp__fusion-essentials__joint_at_geometry",
      "mcp__fusion-essentials__joint_create",
      "mcp__fusion-essentials__joint_create_as_built",
      "mcp__fusion-essentials__joint_create_origin",
      "mcp__fusion-essentials__joint_drive",
      "mcp__fusion-essentials__joint_edit",
      "mcp__fusion-essentials__joint_motion_link",
      "mcp__fusion-essentials__mesh_combine",
      "mcp__fusion-essentials__mesh_generate_face_groups",
      "mcp__fusion-essentials__mesh_insert",
      "mcp__fusion-essentials__mesh_plane_cut",
      "mcp__fusion-essentials__mesh_reduce",
      "mcp__fusion-essentials__mesh_remesh",
      "mcp__fusion-essentials__mesh_repair",
      "mcp__fusion-essentials__mesh_reverse_normal",
      "mcp__fusion-essentials__mesh_separate",
      "mcp__fusion-essentials__mesh_shell",
      "mcp__fusion-essentials__mesh_smooth",
      "mcp__fusion-essentials__mesh_to_brep",
      "mcp__fusion-essentials__model_arrange",
      "mcp__fusion-essentials__model_base_feature",
      "mcp__fusion-essentials__model_chamfer",
      "mcp__fusion-essentials__model_combine",
      "mcp__fusion-essentials__model_construction",
      "mcp__fusion-essentials__model_create_component",
      "mcp__fusion-essentials__model_draft",
      "mcp__fusion-essentials__model_emboss",
      "mcp__fusion-essentials__model_extrude",
      "mcp__fusion-essentials__model_fillet",
      "mcp__fusion-essentials__model_hole",
      "mcp__fusion-essentials__model_loft",
      "mcp__fusion-essentials__model_mirror",
      "mcp__fusion-essentials__model_move",
      "mcp__fusion-essentials__model_offset_face",
      "mcp__fusion-essentials__model_pattern_circular",
      "mcp__fusion-essentials__model_pattern_path",
      "mcp__fusion-essentials__model_pattern_rectangular",
      "mcp__fusion-essentials__model_pipe",
      "mcp__fusion-essentials__model_replace_face",
      "mcp__fusion-essentials__model_revolve",
      "mcp__fusion-essentials__model_scale",
      "mcp__fusion-essentials__model_set_material",
      "mcp__fusion-essentials__model_shell",
      "mcp__fusion-essentials__model_split",
      "mcp__fusion-essentials__model_stitch",
      "mcp__fusion-essentials__model_sweep",
      "mcp__fusion-essentials__model_thread",
      "mcp__fusion-essentials__model_unstitch",
      "mcp__fusion-essentials__param_add",
      "mcp__fusion-essentials__param_set",
      "mcp__fusion-essentials__param_set_favorite",
      "mcp__fusion-essentials__pmi_create",
      "mcp__fusion-essentials__pmi_edit",
      "mcp__fusion-essentials__save_as_mesh",
      "mcp__fusion-essentials__sketch_add_3d_line",
      "mcp__fusion-essentials__sketch_add_geometry",
      "mcp__fusion-essentials__sketch_constrain",
      "mcp__fusion-essentials__sketch_copy",
      "mcp__fusion-essentials__sketch_create",
      "mcp__fusion-essentials__sketch_dimension",
      "mcp__fusion-essentials__sketch_edit_curve",
      "mcp__fusion-essentials__sketch_insert_svg",
      "mcp__fusion-essentials__sketch_move",
      "mcp__fusion-essentials__sketch_project",
      "mcp__fusion-essentials__sketch_set_text",
      "mcp__fusion-essentials__surface_create_ruled",
      "mcp__fusion-essentials__surface_delete_face",
      "mcp__fusion-essentials__surface_extend",
      "mcp__fusion-essentials__surface_extrude",
      "mcp__fusion-essentials__surface_fill",
      "mcp__fusion-essentials__surface_offset",
      "mcp__fusion-essentials__surface_patch",
      "mcp__fusion-essentials__surface_reverse_normal",
      "mcp__fusion-essentials__surface_revolve",
      "mcp__fusion-essentials__surface_thicken",
      "mcp__fusion-essentials__surface_trim",
      "mcp__fusion-essentials__surface_untrim",
      "mcp__fusion-essentials__sys_request_selection",
      "mcp__fusion-essentials__view_screenshot",
      "mcp__fusion-essentials__view_section",
      "mcp__fusion-essentials__view_set",
      "mcp__fusion-essentials__view_switch_workspace"
    ],
    "ask": [
      "mcp__fusion-essentials__assembly_edit_contacts",
      "mcp__fusion-essentials__assembly_edit_relations",
      "mcp__fusion-essentials__cam_create_machine",
      "mcp__fusion-essentials__cam_delete",
      "mcp__fusion-essentials__cam_delete_machine",
      "mcp__fusion-essentials__cam_delete_template",
      "mcp__fusion-essentials__cam_edit_tools",
      "mcp__fusion-essentials__cam_generate_setup_sheet",
      "mcp__fusion-essentials__cam_post",
      "mcp__fusion-essentials__cam_save_template",
      "mcp__fusion-essentials__data_create_folder",
      "mcp__fusion-essentials__data_create_project",
      "mcp__fusion-essentials__data_delete_file",
      "mcp__fusion-essentials__data_delete_folder",
      "mcp__fusion-essentials__data_download_file",
      "mcp__fusion-essentials__data_move_file",
      "mcp__fusion-essentials__data_switch_hub",
      "mcp__fusion-essentials__data_upload_file",
      "mcp__fusion-essentials__design_delete_feature",
      "mcp__fusion-essentials__design_delete_occurrence",
      "mcp__fusion-essentials__design_edit_timeline",
      "mcp__fusion-essentials__design_export",
      "mcp__fusion-essentials__design_set_mode",
      "mcp__fusion-essentials__doc_activate",
      "mcp__fusion-essentials__doc_close",
      "mcp__fusion-essentials__doc_copy",
      "mcp__fusion-essentials__doc_insert_derive",
      "mcp__fusion-essentials__doc_insert_import",
      "mcp__fusion-essentials__doc_insert_occurrence",
      "mcp__fusion-essentials__doc_new",
      "mcp__fusion-essentials__doc_open",
      "mcp__fusion-essentials__doc_restore_version",
      "mcp__fusion-essentials__doc_save",
      "mcp__fusion-essentials__doc_save_as",
      "mcp__fusion-essentials__doc_save_milestone",
      "mcp__fusion-essentials__doc_update_xref",
      "mcp__fusion-essentials__drawing_edit_sheet",
      "mcp__fusion-essentials__drawing_export",
      "mcp__fusion-essentials__mesh_delete",
      "mcp__fusion-essentials__mesh_export",
      "mcp__fusion-essentials__param_delete",
      "mcp__fusion-essentials__pmi_delete",
      "mcp__fusion-essentials__sketch_delete_entity",
      "mcp__fusion-essentials__sys_reload_addin",
      "mcp__fusion-essentials__sys_set_preferences"
    ],
    "deny": [
      "mcp__fusion-essentials__sys_execute_script"
    ]
  }
}
```
