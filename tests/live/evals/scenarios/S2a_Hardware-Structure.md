---
id: S2a_Hardware-Structure
fixture: S1_Foundation
---

## Prompt

The active document is the gyroscope's foundation: sketch-only components around a shared
skeleton (a centre point and three perpendicular pivot lines), with the interfaces between parts
planned on those lines. Its report declared where the parts meet; the sketches carry that plan.

GOAL - give every part its solid, each owned by its own component, at its sketched position on the
skeleton. Never move an occurrence to solve a problem. This is a real machine, so build it to the
way it has to work:

- Engage where a part rests on another: the carrier genuinely seats on the pedestal (its hub reaches
  and rests on the post; extend or add carrier geometry if the plan left it short), and the crank
  genuinely mounts on the frame. Support contact is flush, face on face, never penetration.
- Clear where parts move relative to each other: rotor and shaft inside the inner ring, inner ring
  inside outer ring, outer ring inside the carrier's reach and the frame opening; the rings are
  bands, nested radially, coplanar and symmetric about the ring plane. The rotor spins on its shaft
  along the spin axis; leave the shaft short of the inner ring, its bearing seats are the next stage.
- The frame's central opening stays open; the pedestal may grow away from the ring plane to keep the
  centre free for the rotor's swing.
- It must still move once jointed: the next stage drives each ring pivot to plus and minus 30
  degrees. A ring of radius R tilted by 30 degrees drops its edge by half its radius, so nothing may
  occupy the volume a ring sweeps into above or below the ring plane out to its outer radius. The
  member a ring pivots in is open, a ring or a fork, not a cup with a floor.
- The foundation may not carry every part as a closed profile (the shaft is the usual one). Create
  what is missing, on the skeleton, and say in your report which parts you found and which you made.
- No pivot pins, no pivot bores, no joints yet.

Check your work with fresh reads and report the values: every component holds exactly one connected
solid (a body census and per-body volumes); the two engagements are real contact (a near-zero
distance between the mating faces, or the interference check naming that pair as coincident faces);
the interference check reports zero overlapping pairs and names only your engagements as contacts;
the radial clearance at each nesting step; the rings' mid-planes and the shared centre; the rotor
coaxial with its shaft; the timeline healthy. Say how far you believe each ring can tilt and what
would stop it.

## Grader notes

- A good result, opened in Fusion: eight solids in eight components sitting on the skeleton, the
  carrier resting on the post and the crank on the frame, the rings as thin bands in one plane with
  visible gaps, the rotor a disc on a short shaft that does not reach the ring, and the carrier open
  above and below the ring plane. The interference check the report quotes says zero overlaps.
- What a weak agent does: a floating exhibit (a carrier hovering above its post passes every
  clearance check), a carrier cup with a floor that the rings hit at 15 degrees, or an unscoped cut
  that eats a neighbour.
- Axis this discriminates: the prompt. Every run of this brief so far stalls after its first dozen
  calls: the executor reads the foundation, then thinks past the ten-minute watch without acting
  (one stalled turn read 42k estimated thinking tokens; the CLI's thinking cap does not bound a
  turn). The bullets read as an invitation to plan the whole machine in thought.
- First A/B to run: S2a_Hardware-Structure.B beside this file, the same machine with the motion
  clause cut to one sentence and a "work part by part, read after each" instruction; compare the
  stall rate first, the machines second.
- Earlier runs surfaced: the stall pattern above; a carrier built as a closed cup below the ring plane
  bound the rings at 15 degrees in the motion stage.
