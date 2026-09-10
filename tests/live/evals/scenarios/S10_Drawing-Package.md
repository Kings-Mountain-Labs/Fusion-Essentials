---
id: S10_Drawing-Package
fixture: S6_Vise
---

## Prompt

The active document is a saved solid model.

GOAL - the shop drawing package for this model:

- Generate the 2D drawing from the active design. Pick the standard and units you judge right for a
  metric benchtop part and state the choice; have the generator place dimensions automatically, and
  choose a dimensioning strategy beyond the default if one is offered that you prefer.
- The package needs a custom-size sheet: add or create one at exactly 320 x 200 mm, not a preset.
  Prove the size with the read-back values the tools give you.
- Place the shop's artwork: an image on a sheet at a position you compute to sit inside the sheet,
  at half its natural size. There is no image on disk, so first export a small screenshot through
  whatever tool writes one, then insert that file. Then deliberately attempt one more insert at a
  position far past the sheet's width and report what the tool does; the refusal text is part of
  your report.
- Run at least one dimensioning pass of your own choosing on a generated view, beyond what the
  generation placed. Report the strategy and view you chose and exactly what the tool's read-back
  confirms; if the platform offers no way to read a placed dimension back, say so plainly.
- Name the sheets meaningfully (the cover carries {{FOLDER}} in its name) and report the sheet
  listing with the export indices the tools give you.
- Export the whole package as one PDF to a path of your choosing and report the file evidence the
  tool returns (path and size). Export the full sheet set only; if a narrower export path carries a
  hazard warning in its own description, respect it and report the warning instead.

## Grader notes

- A good result: a drawing file with the stated standard and units, a sheet that reads 320 x 200 from
  the tool, an image placed on-sheet at half size with the bounds check reported, a quoted refusal
  for the off-sheet attempt naming the sheet extent, an honest line about dimensions being
  unreadable on this platform, named sheets with indices, and a PDF on disk with a nonzero size.
- What a weak agent does: reports an off-sheet insert as placed, invents dimension values, attempts
  the single-sheet export despite the warning, or picks a preset sheet size.
- Axis this discriminates: MCP tooling. The drawing tier has no general read tool beyond drawing_get,
  and dimension values cannot be read on this platform; every claim rides on tool read-backs and file
  evidence.
- First A/B to run: `--deny mcp__fusion-essentials__drawing_get` to see how the sheet claims get
  proven without the read, and a B variant that names no numbers ("a sheet of a size you choose").
- Earlier runs surfaced: the single-sheet export blocked the Fusion main thread for minutes on first
  use and its file never landed; the drawing surface cannot list placed dimensions.
