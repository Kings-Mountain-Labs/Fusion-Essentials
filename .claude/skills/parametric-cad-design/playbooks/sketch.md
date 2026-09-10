## Sketch

**construction-carries-symmetry-and-spacing** - Draw the construction line, then constrain: symmetry about it, midpoint on it, equal between the members; dimensions pin only what remains when a profile has symmetry, equal spacing or a family of equal features; not for a one-off reference sketch. Prove: `sketch_get`: more constraints than dimensions, and is_fully_constrained true.
Example: a knife layout: 43 constraints, 5 dimensions; a handle profile: ten symmetry constraints, six expressions.

**anchor-to-the-origin-by-relation** - Put the origin on a midpoint or at the crossing of two construction diagonals, never at a typed coordinate when a profile is placed; not for geometry projected from a body, which is already placed. Prove: `sketch_get`: a midpoint or coincident constraint naming the origin point.

**one-literal-per-wall** - Type it once and write the other dimensions as that dimension's name (d239) or a named parameter when the same size appears twice; not for two sizes that only happen to match today. Prove: `sketch_get`: dimension expressions referencing a name, not a repeated number.

**organic-outlines-are-control-point-splines** - Draw a control-point spline and make its ends smooth to construction lines whose angles and lengths are dimensioned; the control polygon, not the curve, carries the intent when an outline is a free curve; not for a one-off path nothing else references, where a fit-point spline is enough. Prove: `sketch_get`: a cv_spline with 'smooth' constraints to lines that carry angle dimensions.

**detail-on-projected-edges** - Project the wall's edges into the sketch and dimension the detail by offset from them; projected geometry has no freedom, so few constraints fully constrain the sketch when a clip, rib or pocket must follow an existing wall; not for detail on a plane no body touches. Prove: `sketch_get`: fixed_spline or projected entities plus offset dimensions, is_fully_constrained true.

**driven-dimensions-are-checks** - Add it as a driven dimension and read it back; a master sketch may stay not fully constrained only where its free entities are the splines when a derived length or angle matters to the brief; not for a sketch nobody will edit. Prove: `sketch_get`: dimensions with driving=false carrying the value the brief asked for.

### Recipes

#### An anchored, symmetric profile

Use when a bracket, plate or revolved profile that must resize by intent.

1. `sketch_create` - create the sketch on the origin plane the part is symmetric about. Read back: the sketch name and plane.
2. `sketch_add_geometry` - draw one construction line through the origin as the symmetry axis. Read back: its entity id.
3. `sketch_add_geometry` - draw the outline as lines and arcs on ONE side, plus the circles for bores. Read back: entity ids per curve.
4. `sketch_constrain` - midpoint of the base line on the origin; symmetry of each mirrored pair about the axis; tangent between arcs and their lines; equal between features meant to match. Read back: each constraint accepted.
5. `sketch_dimension` - one dimension per independent size; write repeated sizes as another dimension's name. Read back: the dimension names.
6. `sketch_get` - X-ray the sketch. Read back: is_fully_constrained true; constraints outnumber dimensions; the profile areas you expect.

Bar - measure: sketch_get reports is_fully_constrained true and changing one dimension moves both symmetric halves. Eyes: the profile reads as the part's silhouette with the origin on its symmetry line.
Exemplar: Configured Dumbbell (urn:adsk.wipprod:dm.lineage:0Unl7fg2Q2upJxRN-cdSfQ) - Handle/Sketch1 - 17 lines held by 10 symmetry constraints and 6 expressions such as length_handle / 2 + length_thread. Access: Autodesk Design Samples; needs hub access, read only

#### A link profile between bores

Use when a rocker, lever, connecting link or any web joining round bosses.

1. `sketch_add_geometry` - one circle per bore at each pivot, and a concentric outer circle per boss. Read back: entity ids.
2. `sketch_constrain` - concentric each bore with its boss; coincident the bore centres with construction points or projected pivot geometry. Read back: each constraint accepted.
3. `sketch_add_geometry` - two lines per web, roughly tangent between neighbouring bosses, and one large arc closing the outer edge. Read back: entity ids.
4. `sketch_constrain` - tangent each web line to both bosses it joins; tangent the closing arc to its lines. Read back: each constraint accepted.
5. `sketch_dimension` - bore and boss diameters, the closing arc radius, and the pivot spacing. Read back: dimension names.
6. `sketch_get` - X-ray. Read back: fully constrained; the web profiles are closed regions between bosses.

Bar - measure: sketch_get: is_fully_constrained true and one closed profile per web plus one per boss annulus. Eyes: the outline is the classic link: round ends joined by tangent flanks.
Exemplar: bike frame (urn:adsk.wipprod:dm.lineage:ghXi2gxeQWqAdfsv8o1MOA) - ROCKER/Sketch1 - three bores, eight tangent constraints, one R70 closing arc, four dimensions. Access: Autodesk Design Samples; needs hub access, read only

#### An organic outline that edits by intent

Use when a mouse, handle or shell silhouette that must stay smooth while its proportions change.

1. `sketch_add_geometry` - draw a cv_spline (degree 5) through the silhouette's control points - it lays its control polygon down as construction lines. Read back: the built degree (a degree the API cannot honour is clamped) and the polygon line ids.
2. `sketch_add_geometry` - one construction guide line at each end, in the tangent direction. Read back: their entity ids.
3. `sketch_constrain` - at each end: coincident the guide line's end with the spline's end FIRST, then smooth between them - a smooth moves the end point, so smooth without coincident fails at the second end. Read back: each constraint accepted.
4. `sketch_dimension` - angle each guide line and dimension the polygon's spacing. Read back: dimension names.
5. `sketch_dimension` - the overall length as a DRIVEN dimension. Read back: driving=false with the value expected.
6. `sketch_get` - X-ray. Read back: degree 5 and two smooth constraints; only the spline's interior is free.

Bar - measure: sketch_get lists smooth constraints at both ends and the driven overall length matches the brief. Eyes: the curve reads as one continuous sweep with no kink where it meets a guide.
Exemplar: Mouse ASM (urn:adsk.wipprod:dm.lineage:u9j3iHSpRTq4-_zqT1ukdw) - Mouse/Side Profile - a degree-5 spline G2-smooth to guides at 35, 40, 30 and 50 deg; Top Profile the same at degree 7. Access: Autodesk Design Samples; needs hub access, read only
