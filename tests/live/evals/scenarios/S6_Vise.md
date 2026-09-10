---
id: S6_Vise
fixture: none
---

## Prompt

GOAL - a self-centering machine VISE as separate parts: a body with a jaw slideway and two jaws,
each its own component with its own solid.

This is a machinist's tool, not three boxes; someone who owns a vise should recognise it at a
glance. Each jaw's gripping face carries a step, a shallow horizontal ledge near the top that the
workpiece seats on, so a gripped part rides proud of the jaw tops where a cutter can reach it.
Beyond the step the detail is yours (proportions, how a jaw meets its slideway, edge treatment where
hands and tools go); state your envelope and spend real care on it, the screenshots are part of the
deliverable.

- Each jaw rides on a slider joint to the body, its slide axis along the slideway. The jaws are
  movable components, not geometry redrawn inside static ones: a later stage grips stock between
  them and machines against them, so the assembly must know the jaws move.
- The two sliders are coupled with a motion link at ratio -1, so driving one jaw moves both,
  mirrored about the vise centre, the way a real self-centering vise closes.
- One user parameter also drives the jaw opening: changing that single parameter (and recomputing)
  moves both jaw components symmetrically about the centre at any opening. How you couple it to the
  sliders is your choice; a joint's offset is a model parameter an expression can drive, and it moves
  the jointed part along the joint frame's Z axis. Mark the opening parameter a favourite.
- The body carries a tee-slot or keyway along the slideway, drawn as real slot geometry in the
  slideway sketch.
- The vise carries its name, "{{FOLDER}} VISE", as real text on a visible flat of the body, legible
  at a glance, in a font you choose and name. The text lives in the model, read back by a fresh
  sketch read, not only in a screenshot.
- Sensible proportions for a small benchtop vise.

Prove the self-centering: set the opening parameter to two different values; after each, read the
jaw component positions from a fresh assembly read and show the gap's midpoint sits at the vise
centre. Then drive one slider and read both jaws mirrored. Take a volumetric inventory after each
part lands and once at the end: per-body volumes and a body census.

Report: three components each holding one connected solid with its volume; each jaw's step depth
and height read from its faces; the two slider joints with the occurrences they connect; the motion
link and the mirrored positions; the opening parameter's two values with the positions and midpoint
arithmetic; the interference check at both openings; the slot's curves and the text's string and
font as a sketch read returns them.

## Grader notes

- A good result, opened in Fusion: squat proportions, wider than tall, jaw towers rising off a
  carriage on a visible slideway with a tee-slot, stepped gripping faces whose seat ledge faces up,
  chamfers on working edges, legible engraved text, and a favourite parameter that closes both jaws
  symmetrically. The bar is a production self-centering vise.
- What a weak agent does: three cubes, a seat ledge that faces down (an overhang), jaws redrawn in
  place instead of moved as occurrences, a rectangle where the slot should be, or a slider-slider
  link it never tries.
- Axis this discriminates: the prompt. Craft and proportion are the question here; the tools all
  exist. How much the brief has to say to get a vise rather than boxes is what a B variant measures.
- First A/B to run: a B variant that names the bar ("as good as a Lang Makro-Grip") and nothing about
  the step, against this brief.
- Earlier runs surfaced: the offset-drive route (a joint offset driven by an expression) moves the
  occurrence along the joint frame's Z, which is not necessarily world Z; the sketch text surface with
  a named font was found only when the brief asked for a font by name.
