## Assemble

**one-assembly-idiom** - Pick one idiom for the document - joints, or assembly constraints plus rigid groups - and keep it; mixing them is why large timelines read as noise when the first part is placed; not for a rigid group over an already-joined sub-mechanism. Prove: `assembly_get`: joint_count and relations agree with the idiom you chose.

**connected-reference-path** - Establish one intentional reference path from the grounded part; do not ground parts to hide missing joints when an assembly has a fixed moving mechanism; not for floating or multiple independent mechanisms. Prove: `assembly_get`: one ground_to_parent root and every joint reachable from it.

**as-built-for-parts-modelled-in-place** - Join it with as-built joints - nothing moves - and let the joint limits carry the stroke when a part was modelled against its neighbours where it sits; not for a part placed from elsewhere, which takes a joint with joint origins. Prove: `assembly_get`: the joint's limits hold the travel the brief named.

**align-knobs-not-spacers** - Use the joint's align angle and align offset; do not add spacer bodies to fix a pose when a joint needs a flip or an offset; not for a real spacer part in the bill of materials. Prove: `assembly_get`: the joint's frame sits where the offset put it, and the occurrence list holds no spacer that is not a real part.

**exercise-the-mechanism** - Drive it to home and a representative extreme; keep moving joints few, the rest rigid; link two joints with a motion link when one motion implies the other when the mechanism has a driven joint; not for a joint with no travel of interest. Prove: `joint_drive`: the value at each pose; `assembly_inspect_interference`: overlaps at each pose, intended fit or defect.
Example: a nut overlapping a plain shaft is a thread modelled as a cylinder.

**fasteners-mint-parameters** - Expect a set of adsk_ parameters per fastener and read only the authored set; name each fastener instance by its spec when library fasteners are inserted; not for a design with no library parts. Prove: `param_get`: the authored user parameters are what comes back and generated_skipped counts the adsk_ ones the fasteners minted; include_generated=true lists those.

### Recipes

#### A part modelled in place and joined as built

Use when a rocker, bracket or lever designed between parts that already sit where they belong.

1. `model_create_component` - a new component named for the part, activated. Read back: the component is active.
2. `sketch_project` - project the neighbours' bores and faces into the new sketch. Read back: projected entities present.
3. `sketch_add_geometry` - the link profile between the projected bores (see the link recipe). Read back: closed profiles.
4. `model_extrude` - extrude the profile to the part thickness. Read back: one body in the new component.
5. `model_hole` - counterbored holes for the screws. Read back: hole count.
6. `joint_create_as_built` - a revolute as-built joint to each bearing it pivots on; a cylindrical one where it also slides. Read back: the joint reports its motion type.
7. `joint_edit` - set the limits to the stroke the brief names. Read back: limits read back.
8. `joint_drive` - drive to each extreme. Read back: the value at each pose.
9. `assembly_inspect_interference` - check at each pose. Read back: no unintended overlap.

Bar - measure: assembly_get lists the as-built joints with the limits set, and joint_drive reaches both extremes without interference. Eyes: the link sits on its bearings and swings through its arc.
Exemplar: bike frame (urn:adsk.wipprod:dm.lineage:ghXi2gxeQWqAdfsv8o1MOA) - rows 111-124: ROCKER sketched between bearings, three as-built joints, extrude, fillet, mirror, holes, a fourth as-built joint. Access: Autodesk Design Samples; needs hub access, read only

#### A screw cap that turns and travels

Use when a threaded cap, lead screw or any turn-to-advance pair.

1. `assembly_ground` - ground the body part to its parent. Read back: ground_to_parent true.
2. `joint_create_as_built` - a revolute joint between the turning part and the body on the thread axis. Read back: dof 1.
3. `joint_create_as_built` - a slider joint between the advancing part and the body along the same axis - a second joint on the SAME pair is refused as over-constrained. Read back: dof 1.
4. `joint_edit` - slide limits equal to the travel. Read back: limits read back.
5. `joint_motion_link` - link the revolute to the slider with ratio = pitch / 360 (mm of slide per degree). Read back: linked true, ratio_applied true.
6. `joint_drive` - drive the revolute 90 deg - a full 360 is an equivalent pose and moves nothing. Read back: value_now on both joints.

Bar - measure: after a 90 deg drive assembly_get's value_now reads 90 on the revolute and a quarter pitch on the slider. Eyes: the advancing part moves along the axis as the other turns.
Exemplar: End Mill Case (urn:adsk.wipprod:dm.lineage:bp5koQG0RD-wHFxJHo2mUQ) - Motion Link 5 over two cylindrical joints on one pair - made in the UI; through the API one pair takes one joint. Access: Autodesk Design Samples, read only
