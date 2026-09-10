## Model

**form-fillets-shell-then-detail** - Build the outer form, fillet its edges, shell it LAST among the form features, then add bosses, ribs, drafts and holes, then cosmetic split faces when a moulded or cast part is built; not for a machined block, where holes may precede fillets. Prove: `design_get`: Shell after the fillets and before the bosses in the timeline.

**carve-do-not-accrete** - Extrude the block, loft or extrude the cutting form, split the block by it and remove the waste - the kept piece carries clean faces when the form is a block with a curved face or a tapered body; not for a form that is itself one extrude or revolve. Prove: `model_inspect`: one body left with the volume you expect after the split.

**one-half-mirror-combine** - Model one half against the symmetry plane, mirror it, combine into one body when the part is symmetric; not for asymmetric detail added after the mirror. Prove: `design_get`: a Mirror row followed by a Combine row.

**pattern-only-identical-intent** - Build one and pattern it; copies answering different requirements are modelled apart when the same feature repeats; not for copies that merely share a shape. Prove: `design_get`: the pattern feature in the timeline with the count you asked for.

**let-the-process-shape-the-part** - Take draft, wall thickness, minimum internal radius and hole sizes from the process; a 0.05 mm face offset is a fit clearance, a chord-length fillet is a cosmetic edge when the part will be manufactured; not for an unsettled process, which is stated rather than assumed. Prove: `find_geometry`: face normals show the draft; fillet radii meet the minimum.

**threads-by-intent** - Use the thread feature for a standard thread that a drawing or a tap will define; sweep a section along a helix with a pipe only when the ridge itself must be solid geometry when a thread is needed; not for a cosmetic thread nothing mates with. Prove: `design_get`: a Thread row, or a Sweep plus Pipe pair, not both.

**components-when-the-count-is-known** - Stay in bodies until the part count settles, then promote each body to a named component before any joint when bodies are still being carved out of one another; not for a part known from the brief, which starts as its own component. Prove: `design_get`: no unnamed 'Component' rows; every joint follows the component rows.

### Recipes

#### A moulded shell part in the right order

Use when a basket, cover, drawer front or housing with a wall, bosses and clips.

1. `model_extrude` - extrude the footprint into a block taller than the part. Read back: one body, its volume.
2. `model_loft` - loft the crowned form between two sections (a rectangle with one edge a large tangent arc). Read back: is_solid true.
3. `model_split` - split the block by the form and remove the waste. Read back: one body, the expected volume.
4. `model_fillet` - fillet the outer edges, largest radius first. Read back: face count grew.
5. `model_shell` - shell to the wall thickness, opening the open face. Read back: volume dropped to about wall area times thickness.
6. `model_extrude` - bosses and ribs from a sketch on a plane inside the wall. Read back: each joined to the shell.
7. `model_draft` - draft the bosses toward the pull direction. Read back: normals tilted by the angle.
8. `model_hole` - screw holes sized for the fastener. Read back: hole count and diameter.
9. `view_section` - section through a boss. Read back: one uniform wall, boss root filleted.

Bar - measure: model_inspect volume is wall area times thickness within 10 percent and view_section shows one uniform wall. Eyes: smooth and crowned outside; bosses and ribs on a thin wall inside.
Exemplar: Basket - Part (urn:adsk.wipprod:dm.lineage:ph2BLdbPShaZLqEV3FMLRg) - rows 0-19: Extrude, Loft, Split, RemoveBody, Draft, Fillet x4, then Shell1; bosses, drafts and a second shell follow. Access: Autodesk Design Samples; needs hub access, read only

#### A frozen body with re-cut interfaces

Use when a purchased part, an imported STEP or a generative outcome that needs holes and seats.

1. `model_create_component` - make a component named by the part's spec. Read back: the component name.
2. `model_base_feature` - start a base feature in it. Read back: the scope is open.
3. `doc_insert_import` - import the STEP or other file into that component. Read back: one body in the component.
4. `model_base_feature` - finish the base feature. Read back: the timeline shows one BaseFeature row.
5. `model_move` - align the body to the origin and the interface axes. Read back: model_inspect bounding box centred where intended.
6. `sketch_create` - sketch the hole pattern on the mounting face. Read back: profiles at the bolt centres.
7. `model_hole` - cut the holes as hole features, counterbored where a screw head seats. Read back: hole count and depth.
8. `model_chamfer` - chamfer the interface edges. Read back: face count grew.
9. `design_get` - read the timeline. Read back: BaseFeature, Move, Sketch, Hole, Chamfer - nothing edits the frozen body itself.

Bar - measure: design_get shows the base feature followed only by interface features; model_measure_between gives the hole spacing the brief asked for. Eyes: the organic or bought body is untouched; the holes sit on flat pads.
Exemplar: Main Support Skeleton (urn:adsk.wipprod:dm.lineage:3QrFqp9VRYepLTttp3M1yg) - seven rows: BaseFeature1, Align1, DeleteFace2, Sketch1, Hole1, Sketch2, Hole2 on a generative outcome. Access: Autodesk Design Samples; needs hub access, read only

#### A parametric family with configurations

Use when one part in several sizes.

1. `param_add` - the driving sizes as named parameters (length_handle, diameter_thread), the derived ones as ratios (chamfer_size = weight_width / 8). Read back: each expression evaluates.
2. `sketch_dimension` - write every profile dimension as a parameter expression, halves as diameter / 2. Read back: expressions, not literals.
3. `model_revolve` - revolve the profile, or extrude with a profile offset for a coating. Read back: is_solid true.
4. `sketch_set_text` - create the label, then bind it with parameter=<the text parameter>. Read back: bound_to naming the parameter.
5. `model_emboss` - emboss that bound text. Read back: the text lands raised or engraved by the signed depth.
6. `doc_save_as` - save the document - a configuration table refuses an unsaved one. Read back: a document id.
7. `design_configure` - create the table, a column per varying size, a row per variant; a text column takes no per-row value, so relabel by param_set. Read back: configured true, then each column id.
8. `design_configure` - activate a different row. Read back: the activate reports the row.
9. `design_get` - read the configurations slice and the timeline. Read back: the row reads active and the timeline is healthy.

Bar - measure: param_get shows only named parameters and ratios; design_get reports healthy on each configuration row. Eyes: switching rows changes size and label together with no broken feature.
Exemplar: Configured Dumbbell (urn:adsk.wipprod:dm.lineage:0Unl7fg2Q2upJxRN-cdSfQ) - 12 parameters, urethane_coating = weight_width / 10, weight_text driving the emboss, 14 configuration rows. Access: Autodesk Design Samples, read only
