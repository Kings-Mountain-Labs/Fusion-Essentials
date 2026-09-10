---
id: S3_Motion
fixture: S2b_Hardware-Interfaces
---

## Prompt

The active document is the gyroscope hardware, every part solid, no joints yet. The parts sit on a
shared skeleton: pedestal post (the yaw axis), carrier, two coplanar rings on physical pivot pins
along two perpendicular in-plane axes, rotor on its shaft, and a crank on the frame.

GOAL - a working three-axis gyroscope mechanism with a crank drive:

- Fix the frame with its pedestal so it cannot move.
- A yaw revolute: the carrier pivots on the pedestal's post.
- The ring pivots: outer ring to carrier, inner ring to outer ring, each revolute on its physical
  pin's axis.
- A spin revolute: the rotor on its shaft inside the inner ring.
- A crank revolute: the crank on its mount on the frame.
- Couple the crank revolute to the rotor spin with a motion link at a ratio you declare, and build
  the link before you drive anything. Once it exists, drive that pair only from the crank side; never
  drive the rotor spin directly.
- Joints must not teleport parts: after each joint, fresh position reads show the parts still seated
  where they were.

Prove it with a range sweep, not a single pose. The two ring pivots must each drive to +30 and to
-30 degrees; that number is the scenario's, not yours, a gimbal whose rings cannot tilt 30 degrees
is not a working gimbal. Sweep each ring pivot across that range at four stations including both
extremes. For the yaw and the crank, declare your own travel and sweep it the same way; drive the
rotor only through the crank link and report the crank angle and the rotor follow at each station.
Read fresh orientation after each station. Restore the rest pose afterwards and read it back.

Then measure the limit: for each ring pivot, find the angle at which the first overlap appears by
bisection (drive, check interference, halve the interval) until the bracket is under one degree, and
report it as a number with the two bodies that meet there. Report it whether or not 30 degrees
passed; if the mechanism clears 30, keep going until it binds or you reach 90, and say which.

Run the interference check at rest and at each joint's two travel extremes. Pins ride in clearance
bores and the shaft in a clearance seat, so the expected result is zero overlapping pairs at every
pose; the support engagements are flush contacts, not overlaps. Name any overlap with its volume
and the two bodies. A ring swinging into the pedestal or the carrier inside the 30-degree range is a
binding defect: report it as a failure with the pose and volume, never as a smaller range.

Report the joint set with the occurrences each connects, the three joint axis directions with their
pairwise dot products, the motion link and the two angles that prove its ratio, the station angles
and the interference result at each, the bind angle per ring pivot, and the assembly health.

## Grader notes

- A good result, opened in Fusion: five revolutes and one motion link, rings that drive cleanly to
  30 degrees, a bind angle per ring reported as a measured number (they swing between about 5 and
  55 degrees across builds, and the cause is always upstream carrier geometry), a rest pose that
  reads back, and zero overlaps at every checked pose.
- What a weak agent does: declares a range the design already clears and sweeps that, drives the
  rotor directly (the platform has crashed on driving the linked member in xref context), builds a
  motion link between two joints on the same chain (Fusion refuses it), or reads the spin axis
  parallel to the outer pivot as a defect (at rest that is correct).
- Axis this discriminates: MCP tooling. joint_at_geometry on real pin cylinders, joint_drive with
  fresh orientation reads, and assembly_inspect_interference posed are the levers; the brief is
  already explicit.
- First A/B to run: `--deny mcp__fusion-essentials__joint_at_geometry` to see whether the origin-snap
  route carries the same mechanism, and a B variant that drops the bisection paragraph to see whether
  the bind angle gets measured unprompted.
- Earlier runs surfaced: an executor met a bind at 15 degrees, narrowed its declared range to 5
  degrees, swept that clean and reported a pass; the fixed 30-degree requirement exists because of
  it.
