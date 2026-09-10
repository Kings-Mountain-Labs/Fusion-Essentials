## Surface

**skin-first-solid-later** - Build the skin as surfaces - sweep, loft, patch - then trim, stitch once with a stated tolerance and thicken once; the solid comes last when the outer form is the product; not for a form one solid feature already describes. Prove: `design_get`: Stitch and Thicken rows after every surface row, not between them.

**extend-before-trim** - Extend the cutting surface a few millimetres past the body first, so the trim never lands on an edge when one surface will trim another; not for a cutter that already overhangs. Prove: `design_get`: an Extend row immediately before the Trim row.

**blend-by-split-and-loft** - Split the faces where the blend starts, delete the faces between, and loft between the split edges; a fillet only where the transition is a constant radius when two skins must meet with a smooth transition; not for a constant-radius corner. Prove: `find_geometry`: no crease: adjacent face normals agree along the seam.

**master-skin-derived-into-parts** - Keep the skin in a master document with named skeleton sketches; each part derives it and closes it with a boundary fill, then adds its own walls, bosses and clips when several parts share one outer form; not for a single-part shell. Prove: `design_get`: each part's timeline starts with a derive row then a BoundaryFill row.

**peel-a-solid-to-a-skin** - Delete the faces you do not want; the remaining skin is the surface to build on when the quickest form is a solid but only some faces are wanted; not for a solid you will keep whole. Prove: `design_get`: the tree's body row reads is_solid false after the delete.

### Recipes

#### A swept and surfaced bottle

Use when a container whose section changes along a curved spine, its neck narrower than the body.

1. `sketch_create` - 'Bottle_Bottom' on XY: an ellipse dimensioned by its radii. Read back: fully constrained.
2. `sketch_create` - 'Bottle_Profiles' on XZ: spine arc, offset rail, cv_spline neck. Read back: the arc and offset dimensions.
3. `model_loft` - loft the ellipse to the neck section with the rails, as_surface true. Read back: is_solid false.
4. `sketch_create` - 'Bottle_Crown' on XZ: the shoulder profile, body radius to neck. Read back: fully constrained.
5. `surface_revolve` - revolve the profile about the bottle axis. Read back: a second surface body.
6. `surface_extend` - extend the NECK until it crosses the crown. Read back: the distance.
7. `surface_trim` - trim ONLY where the crown crosses the body, keeping that cell. Read back: the cells removed.
8. `model_stitch` - stitch the two at 0.1 mm. Read back: became_solid true.
9. `model_shell` - shell to the wall, opening the mouth. Read back: volume dropped.
10. `model_fillet` - chord-length fillets on the shoulder. Read back: face count grew.
11. `model_offset_face` - offset the mouth face -0.05 mm. Read back: the distance landed.

Bar - measure: became_solid true, a Shell row after the stitch, one body. Eyes: a smooth leaning body with a crowned shoulder and no facet at the crown.
Exemplar: Bottle (urn:adsk.wipprod:dm.lineage:NX9msEStSlaONb6W4KI4ZA) - Sweep1 with a rail, Top_Crown on projected edges, Extend 5 mm before Trim, Stitch 0.10 mm, chord fillets, OffsetFaces -0.05 mm. Access: Autodesk Design Samples, read only

#### One skin, several parts

Use when a product whose top, base and middle share one outer surface.

1. `sketch_create` - named skeleton sketches in the master: side, top and section profiles as cv_splines smooth to dimensioned guides. Read back: each sketch named; zero profiles is expected.
2. `model_loft` - loft the skin between the section profiles with the side and top profiles as rails. Read back: a surface body.
3. `model_mirror` - mirror the half skin about the symmetry plane. Read back: two surface bodies.
4. `model_stitch` - stitch the halves. Read back: one surface body; became_solid false is fine for a skin.
5. `doc_insert_derive` - in each part document, derive the master's skin body. Read back: the derived body and its source version.
6. `surface_extrude` - extrude the parting line into a surface through the skin. Read back: a surface body.
7. `surface_fill` - boundary fill with the skin and the parting surface as the tools, keeping the part's cell. Read back: one solid body.
8. `model_shell` - shell to the wall thickness. Read back: the volume dropped as expected.
9. `model_offset_face` - offset the mating faces for fit clearance. Read back: the distance landed.

Bar - measure: each part's design_get starts with a derive row and a BoundaryFill row, and model_inspect gives one solid. Eyes: the parts assemble into the skin with no step at the parting line.
Exemplar: Mouse ASM (urn:adsk.wipprod:dm.lineage:u9j3iHSpRTq4-_zqT1ukdw) - the Mouse master's five named profiles; Base, Middle and Top each open with Context1 (derive) and BoundaryFill1. Access: Autodesk Design Samples, read only
