## Manufacture

**rig-is-part-of-the-job** - Hold the fixture as its own components beside the part, select only the part body as the model, and take the work coordinate system from the stock; the fixture is geometry the toolpaths must avoid, not decoration when a setup is created; not for a setup sheet exercise with no fixture. Prove: `cam_get`: selected_models names the part only and the wcs origin_mode is stated.

**steep-and-shallow-are-two-strategies** - Parallel finishes shallow areas and skips steep ones by default; 3D contour does the opposite; one flag on each takes the other's areas, and scallop finishes both at a constant stepover a tenth of the tool when finishing a 3D form; not for a flat part, where face and 2D contour suffice. Prove: `cam_compare_operations`: machineSteepAreas or machineShallowAreas is the only difference between the pair.

**pocket-or-adaptive** - Adaptive takes deep stepdowns at a light radial load and needs no leads or compensation; 2D pocket takes shallow stepdowns with a finishing pass and cutter compensation - pick adaptive for bulk removal, pocket when the wall finish comes from the same operation when roughing a pocket; not for a slot, which wants the slot strategy on a closed slot contour. Prove: `cam_compare_operations`: optimalLoad and maximumStepdown on adaptive; finishing passes and compensation on pocket.

**geometry-selection-is-half-the-operation** - Compare their geometry selections - chains, extension modes, stock contours, boundaries - because a parameter diff cannot see them when two operations read as identical; not for operations that differ in parameters already. Prove: `cam_get`: each operation's references: the chains and faces it was given.

**turning-lead-out-gouges-the-remaining-stock** - 'Lead-Out has been modified due to a gouge with the remaining stock' says the exit move ran into stock the cycle leaves behind, and Fusion moved it rather than refusing. Measured on a turned flange, each remedy cleared it: doLeadOut false, or useStockToLeave true with xStockToLeave and zStockToLeave at 0.5mm. Take the allowance on a cycle a later pass follows; turn the lead-out off on the last one when a turning profile finishing cycle generates carrying a warning; not for a cycle whose exit move has to clear a face, where the lead-out is the point. Prove: `cam_get_status`: operations_with_warnings is empty for that setup and none of its operations is in empty_toolpaths.

**rest-stock-is-what-the-setup-before-left** - MEASURED on a turned flange, one whole-model adaptive with its parameters untouched: it generated EMPTY on that setup under previous_setup stock in both the 'rest' and 'setupStockSilhouette' modes, and the same operation cut 347 s the moment the setup alone was switched to a relative box. These stock extents keep the relative-box numbers and describe nothing this setup cuts, so size a clearing strategy from the preceding setup's own operations, not from these extents. An operation's OWN rest is a separate knob: defineStockBy 'rest' reads restMaterialSource 'previousOperations' - the operations before it in THIS setup, not the setup's stock mode - and with every sibling valid it computes, no 'Failed to generate rest material.' among the warnings when a milling setup takes stock_mode 'previous_setup' after a turning setup; not for the first setup of a job, whose stock is the billet. Prove: `cam_get`: the setup row reads stock_mode 'previous_setup' and stock_extents_describe says the box is not the stock; `cam_get_status`: whether the clearing operation is named in empty_toolpaths - the list reports membership, never why.

### Recipes

#### Choosing a strategy for a feature

Use when a face, pocket, wall, hole or free-form surface needs an operation.

1. `cam_get` - read the setup's allowed strategies. Read back: allowed true for the candidates; a false one cannot be created.
2. `find_geometry` - classify the feature: flat top (face), pocket floor, wall (2D contour), hole (drill, bore, thread), curved surface (parallel, contour, scallop), edge (chamfer). Read back: the face kinds and normals.
3. `cam_create_operation` - create the candidate with a named tool. Read back: its name and state.
4. `cam_select_geometry` - give it the chains or faces, with the chain extension where the cut runs past the wall. Read back: the selection count.
5. `cam_generate` - generate. Read back: a handle, then cam_get_status until completed.
6. `cam_inspect_toolpaths` - inspect the result. Read back: the operation is not among empty_toolpaths; an empty one is the wrong strategy or selection.
7. `cam_get` - read the time slice. Read back: machining time above zero.
8. `cam_compare_operations` - when unsure between two strategies, compare the pair: pocket and adaptive differ in 114 parameters, optimalLoad and stepdown among them. Read back: the parameters that differ name the trade-off.

Bar - measure: cam_inspect_toolpaths lists no empty toolpath, the time slice reads above zero, and the operation is valid with no warning. Eyes: the toolpath covers the feature and nothing else.
Exemplar: 2D - Overview of toolpaths (urn:adsk.wipprod:dm.lineage:VJJsAJVXQmiDOFFl6-xErw) - 22 operations named for their variant, '2D Contour2' beside its multiple-passes, Trimmed and Rest siblings. Access: Autodesk Design Samples; needs hub access, read only

#### Proving a toolpath before posting

Use when an operation generated and must be trusted.

1. `cam_get` - read the operation with its parameters and tool. Read back: state valid, no warning text, the tool it uses.
2. `cam_inspect_toolpaths` - read the toolpath census. Read back: the operation is valid and not empty.
3. `cam_get` - read the time slice. Read back: machining time above zero, feed and rapid distances.
4. `cam_show_toolpath` - show the toolpath and take a screenshot. Read back: the passes lie on the feature, inside the stock.
5. `cam_post` - post to a scratch folder with the machine's post. Read back: the file landed with a size above zero.
6. `cam_get` - re-read after posting. Read back: no operation went out of date.

Bar - measure: the time slice reads above zero, the census lists the operation as not empty, and the posted file exists with a size. Eyes: the shown toolpath stays on the feature, inside the stock and clear of the fixture.

#### Drilling across the turned axis

Use when a hole whose axis runs across the part axis - a cross-drilling, or a hole through a boss on a turned part.

1. `find_geometry` - find the hole's own cylindrical face. Read back: its 'axis', which is not the milling setup's Z.
2. `cam_create_setup` - give that axis a setup of its own: one per hole axis, so opposed holes are two setups, never one bore through both. Read back: the setup name and zero operations.
3. `cam_edit_setup` - bind the setup's Z to that face: wcs={'z_axis': <the handle>}. Read back: wcs_set.z_axis.bound_entities reads 1.
4. `cam_create_operation` - create the drilling cycle. Read back: the name it landed under.
5. `cam_select_geometry` - aim it with selection='holes' and the same face. Read back: selection_param 'holeFaces' and the face count.
6. `cam_generate` - generate, then read the status. Read back: on world Z it errors 'Cylindrical face not in tool orientation!'; bound to the face it errors 'Selected face may not be safe for cutting at current tool orientation!' - the bound axis points into the material.
7. `cam_edit_setup` - turn the axis round: parameters={'wcs_orientation_flipZ': 'true'}. Read back: the parameter reads back true.
8. `cam_generate` - regenerate. Read back: operations_with_errors is empty.
9. `cam_get` - read the time slice. Read back: machining time above zero for the drill.

Bar - measure: the operation carries no error and its machining time reads above zero. Eyes: the drill enters square to its face and stops in the material, not through to the far side.
