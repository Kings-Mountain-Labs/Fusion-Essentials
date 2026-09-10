---
id: S12_Parametric-Family
fixture: none
---

## Prompt

GOAL - a HEX DUMBBELL as a parametric family, two components: a turned steel HANDLE and a
hexagonal WEIGHT used twice (one component, two occurrences).

- Handle: a revolved profile, a grip of one diameter, a shoulder, and a threaded stub at each end of
  a smaller diameter, with a real thread feature on the stubs.
- Weight: a hexagonal prism (across-flats width, length) with a bore that fits the stub and a
  chamfer on every hex edge; the hex is drawn with the sketch polygon and held by constraints, not
  by six typed lines.
- The family: every size that matters is a named user parameter (handle length, grip diameter, stub
  diameter, thread length, weight width, weight length); the chamfer is derived from the weight
  width by a ratio, not typed; the sketch dimensions carry those names as expressions.
- Three weights: author three variants as a configuration table (10, 25, 50; the numbers are a
  label, the geometry follows weight width and length), one row active. A configuration table can
  only be authored on a saved document: before you author it, save the document once as
  P12-Dumbbell into project {{PROJECT}}, folder {{FOLDER}}.
- The label: the weight's face carries an embossed or engraved text whose string comes from a text
  parameter, so one parameter change relabels both weights.
- Assembly: both weights sit on the stubs, held by joints or as-built joints so that changing the
  handle length moves them apart; no floating bodies.

Prove the family: activate a second configuration row (or change the weight width by 20 percent),
read fresh geometry from the weight and the assembly spacing, say how each moved, then restore.

Report: the parameter list with expressions; the handle sketch's constraint and dimension counts,
whether it reads fully constrained, and the symmetry it carries; the hex sketch's polygon
constraint; the feature list including the thread, the chamfer, the engrave and the configuration
table's rows; the two weight occurrences and their joints; the propagation numbers for weight width,
chamfer size and weight spacing; whether the text string is bound to the parameter, read from the
sketch.

## Grader notes

- A good result, opened in Fusion (the bar is Autodesk's Configured Dumbbell sample: twelve
  parameters, chamfer as a ratio of width, a text parameter driving the emboss, a handle sketch held
  by symmetry constraints): two chamfered hex weights on a turned, threaded bar, a legible label on
  each weight that changes when the text parameter changes, and a configuration table whose rows
  resize the weights and move them apart.
- What a weak agent does: six typed lines for the hex, a literal chamfer, a label typed rather than
  bound, a distance dimension between two opposite hex edges (that binds the corner span, not the
  flats), or a bore whose feature lands in the wrong component.
- Axis this discriminates: MCP tooling. The text-parameter binding on the wire, the polygon
  constraint, the configuration table on a saved document, and where model_hole hosts its feature
  have all been questions the wire answered badly.
- First A/B to run: `--deny mcp__fusion-essentials__sys_get_guidance` against the bare brief (the
  guided and unguided runs read the same so far), then `--deny mcp__fusion-essentials__model_hole`.
- Earlier runs surfaced: sketch_set_text refuses text and parameter together while requiring text,
  so the binding only lands with an empty text beside the parameter; model_hole cut the right body
  but placed its feature in the wrong component twice; sketch_constrain fix reported applied while
  the line stayed free; the configuration conversion moves the document to a new lineage on save.
