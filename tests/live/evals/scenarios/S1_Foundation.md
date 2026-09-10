---
id: S1_Foundation
fixture: none
---

## Prompt

GOAL - the parametric foundation of a THREE-AXIS GYROSCOPE, as separate parts, sketch-only.

A gyroscope is a nested-ring mechanism: a fixed frame with a pedestal, a carrier that yaws on the
pedestal, two nested rings on perpendicular pivots, a rotor spinning inside the inner ring, and a
small crank on the frame that a later stage will link to the rotor. All the rings share one plane
at rest and nest radially; it is not a stack of discs.

Lay it out so the later stages can build on it:

- One shared skeleton: a centre point and three mutually perpendicular pivot axes through it (yaw
  and the two in-plane ring pivots) as construction geometry. Every part's sketch geometry is
  positioned off this skeleton.
- Separate parts: each part is its own component holding its own sketch geometry and no solid
  yet; the pedestal is a sub-component inside the frame.
- Planned interfaces: for each place two parts will meet or pass through each other (carrier on
  pedestal, carrier to outer ring, outer to inner ring, shaft in inner ring, crank on frame), decide
  where it lives and put the sketch geometry that will carry it on the skeleton axis it pivots
  about. The rotor's spin axis lies in the ring plane, perpendicular to the inner ring's pivot.
- Nesting with clearance: rotor inside inner ring inside outer ring inside the frame opening, real
  radial clearance at each step, all concentric about the centre, ring profiles coplanar.
- Buildable in place: plan the vertical arrangement so every part can later become a solid where it
  is sketched, and the parts that must touch (carrier on pedestal, crank on frame) can reach each
  other. Say what that plan is in your report.
- One driving parameter: shared user parameters size the parts so that a single overall-diameter
  parameter propagates through every part; dimensions carry expressions, not typed numbers; mark
  the driver a favourite.

Prove the propagation: change the driver, read fresh geometry from three different components
including one interface element, say how each moved, restore, and read again.

In your report, list the interfaces you declared (which geometry carries each, on which axis), the
radii of the nested profiles with their clearances, the skeleton's three axis directions, and the
propagation numbers.

## Grader notes

- A good result, opened in Fusion: one component per part with the pedestal nested in the frame,
  six concentric circles in one plane stepping down with a real gap at each step, three construction
  lines through the centre, an interface marker on each axis, no solid bodies anywhere, and one
  favourite parameter whose change moves every profile. The report names five interfaces and the
  numbers it read.
- What a weak agent does: stacks the rings as discs at different heights, types numbers into the
  dimensions, puts the spin axis vertical (it then never meets the inner ring's band), or forgets the
  crank.
- Axis this discriminates: the prompt. The brief is open-ended on purpose; how much structure an
  agent needs to produce a shared skeleton is the question.
- First A/B to run: a B variant that drops the "Lay it out so" bullets and keeps only the GOAL
  paragraph and the propagation proof, to see whether the skeleton and the interface planning appear
  unprompted. For the tooling axis, `--deny mcp__fusion-essentials__sys_get_guidance`.
- Earlier runs surfaced: a run staged on a real assembly refused honestly, which is why the proctor
  reads the new document back empty; sketch_constrain takes endpoint references (line:0:start) that
  its description does not mention; the carrier's interface marker grazed the frame rim, an honest
  caveat the report carried.
