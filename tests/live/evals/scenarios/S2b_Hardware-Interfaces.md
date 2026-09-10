---
id: S2b_Hardware-Interfaces
fixture: S2a_Hardware-Structure
---

## Prompt

The active document is the gyroscope with its primary bodies: frame with a pedestal sub-component,
carrier, two coplanar rings, rotor, rotor shaft and crank, built on a shared skeleton (a centre point
and three perpendicular pivot construction lines), engaged at its supports (carrier on pedestal,
crank on frame) and clear of overlap everywhere.

GOAL - build the pivot interfaces the next stage will joint:

- Physical pivot pins, real solid pins along the pivot construction lines, each bridging the gap at
  its interface and threading both parts (carrier to outer ring on one axis, outer to inner ring on
  the perpendicular axis); each pin its own body, housed sensibly.
- Matching through bores in both parts at every pin, all the way through the part's local material,
  with real clearance between pin and bore. Add a boss or lug only where the material at the
  interface is too thin to bore, and keep any added material clear of every other body.
- The rotor shaft's bearing seat in the inner ring: the shaft ends ride in through bores or open
  seats with clearance, so the shaft can spin. Measure first. The inner ring is a peripheral band
  and the shaft is central, so there may be no material on the shaft axis at all; if so, add a
  spanning member (a yoke, spider or cross-bar) from the band to the axis, clear of the rotor's
  sweep, before there is anything to bore. Report the measurement that told you which case you had.
- Parametric: the pins, bores and seats are driven by the design's shared parameters (their sketch
  dimensions carry expressions), so the whole design still scales as one.
- Every pin is owned: a pivot pin is fixed to exactly one of the two parts it threads and clearance
  fits the other. Each pin ends up structurally attached to its owner (a body inside that component,
  or its own component rigid-grouped to it); name the owner per pin. A pin belonging to nobody stays
  put while the ring turns into it.
- No joints yet; the parts stay where they are.

Take a volumetric inventory after each pin, bore and seat lands and once at the end: per-body
volumes and a body census. A bore whose volume delta is zero cut nothing; a delta far larger than
the bore ate a neighbour.

Check with fresh reads and report the values: every body accounted for, each pin one connected
solid, no orphan lump; each pin's axis collinear with its pivot line and its extent reaching into
both parts; each bore through its local material, bore radius strictly larger than pin radius; the
shaft seats with their clearance; at least one expression per added interface, read from the
sketch; the interference check at zero overlapping pairs with only the two support engagements as
contacts; each pin's owner and the structural fact that proves it; for each pin and shaft end, the
feature that stops it sliding out axially, or a plain statement that none exists; the timeline
healthy.

## Grader notes

- A good result, opened in Fusion: four pins along the two in-plane axes threading their rings, each
  in a slightly larger bore, a spanning member across the inner ring carrying the shaft seats, every
  added sketch dimension an expression, and the report naming an owner per pin and saying plainly
  that nothing retains the pins axially (retention is out of scope; the honest disclosure is the
  pass).
- What a weak agent does: builds the pins from literal coordinates so the design stops scaling
  (the regeneration stage catches it one stage late), parks all four pins in one free component
  attached to nothing, hunts for a wall to bore at the shaft axis and finds none, or reports a bore
  whose volume did not change.
- Axis this discriminates: MCP tooling. Whether the wire offers a through-all cut, a scoped cut
  (target bodies), and a volume read the agent can find decides this stage more than the brief.
- First A/B to run: `--deny mcp__fusion-essentials__model_inspect` to see whether the volumetric
  audit survives without the census read, and `--skill parametric-cad-design` for the practice axis.
- Earlier runs surfaced: a redrawn "clean" sketch with literal radii severed the parameter chain
  while every health read stayed green; four pins in one free occurrence passed every read until the
  motion stage tilted the rings into them.
