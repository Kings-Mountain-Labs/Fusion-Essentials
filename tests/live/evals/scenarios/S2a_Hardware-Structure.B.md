---
id: S2a_Hardware-Structure
fixture: S1_Foundation
---

## Prompt

The active document is a starting point: a sketch-only foundation for a three-axis gyroscope,
with a shared skeleton (a centre point and three perpendicular pivot lines) and one component per
part. Treat it as a draft you own. Keep what serves you, change or replace anything that does not:
sketches, components, parameters, positions. The only thing to preserve is the skeleton's centre
and its three axes, because every later stage is built around them.

GOAL - the gyroscope's primary bodies as solids, one per component: a frame with a pedestal
sub-component, a carrier that will yaw on the pedestal, an outer ring and an inner ring on
perpendicular pivots, a rotor on a shaft inside the inner ring, and a crank on the frame.

The machine has to work:

- The carrier rests on the pedestal and the crank mounts on the frame: real flush contact.
- Everything that moves stays clear: rotor and shaft inside the inner ring, inner ring inside
  outer ring, outer ring inside the carrier's reach and the frame opening. Rings are bands,
  coplanar at rest. Leave the shaft short of the inner ring; its seats come later.
- Each ring will later tilt 30 degrees either way, so leave the volume it sweeps through empty.
- No pivot pins, no pivot bores, no joints yet.

Build one part at a time and read it back before the next; a wrong part is cheaper to fix than a
wrong plan.

Report with fresh reads: one connected solid per component with its volume; the two support
contacts proven by a near-zero distance or the interference check's coincident-face listing; zero
overlapping pairs; the radial clearance at each nesting step; the rings' mid-planes on the shared
centre; the rotor coaxial with its shaft; the timeline healthy; and how far you believe each ring
can tilt.

## Grader notes

- The A/B partner of S2a_Hardware-Structure: the same machine and the same checks, with the agent
  UNLOCKED - the foundation is a draft it may change, not a contract it must satisfy. The A brief
  holds the agent to the foundation's sketched positions and declared interfaces.
- What it measures: the A brief stalls the executor after its first dozen calls in every run so
  far, and the earlier "work part by part" variant stalled the same way; the hypothesis is that
  reconciling a handed-over plan with the machine's needs is the planning load. If the unlocked
  brief builds where A stalls, the constraint was the load and the chain's fixtures become
  drafts; if it stalls too, the stage is too large for one blind run and splits.
- A good result is the A file's: eight solids on the skeleton, supports touching, rings nested in
  one plane with gaps, an open carrier, zero overlaps. Whether it kept or replaced the foundation's
  sketches is recorded, not graded.
