---
id: S4_Details
fixture: S3_Motion
---

## Prompt

The active document is the working gyroscope: frame and pedestal fixed; yaw, two ring pivots and
rotor spin as revolutes; the crank motion-linked to the rotor spin; everything healthy and at rest.

GOAL - the production details a real part needs, without breaking the mechanism that already
moves:

- Mounting holes: a symmetric set of through fastener holes in the frame plate, placed clear of the
  rings' swing.
- Soften the frame's exposed edges with a fillet, and bevel the central opening's edges with a
  chamfer. The part is round, so interpret "exposed edges" for the shape you have.
- A turned boss on the pedestal: a small raised ring coaxial with a bore on the pedestal (use an
  existing bore, or drill one first and say so), built as turned geometry, a profile swung about that
  bore's own axis, so the concentricity is constructional. Report the bore you used and the read that
  proves the boss shares its axis.
- One detail of your choice a machinist would thank you for, built as raised material (a boss, a
  lug, a rib, not a cut) on a part that swings, a ring or the rotor. Name it and the part carrying it.
- The mechanism stays alive: joints healthy, motion link intact, parts still at rest where you found
  them.

Prove it: volume before and after for each part you changed; a fresh assembly read after all the
features; the interference check at rest and at each ring pivot's two travel extremes. Declare each
ring pivot's travel and justify it, either by driving past your extreme until the checker names a
real contact or by arithmetic from geometry you read. That sweep is a regression check on the
mechanism only: material that never reaches a ring reports zero without anyone measuring it, and a
hole removes material, so a hole bored into a ring's path reports zero too. Prove clearance by
measurement instead, once per subject: for your raised detail, the turned boss and the worst-placed
mounting hole, at the pose where a ring comes closest, take a minimum-distance read naming both
targets and report the gap in millimetres, or derive it from geometry you read and state the
arithmetic. Zero or negative means the swing reaches it.

Report the hole count with the volume arithmetic behind it, the boss axis against the bore axis, the
fillet and chamfer edge counts, the three gaps, and the assembly health.

## Grader notes

- A good result, opened in Fusion: a bolt circle in the frame plate outside the rings' sweep,
  softened outer edges and a chamfered opening, a small turned ring on the pedestal that is truly
  coaxial with its bore, one raised detail on a ring or the rotor with a measured gap to the swing,
  and a mechanism that still drives to its extremes with zero overlaps.
- What a weak agent does: fillets the wrong edges on an assembled occurrence, cuts instead of
  raising, declares a five-degree travel and sweeps it, or trusts the sweep for clearance and never
  measures the gaps.
- Axis this discriminates: MCP tooling. Edge and face handles on jointed occurrences, model_revolve
  about a bore's axis, and a minimum-distance read between two named targets are what this stage
  needs from the wire.
- First A/B to run: `--skill parametric-cad-design` against the bare brief, then `--deny
  mcp__fusion-essentials__model_measure_between` to see how the gaps get proven without it.
- Earlier runs surfaced: a run under the skill fit the same brief in fewer calls; the raised-detail
  clause exists because a cut detail has no clearance question to get wrong.
