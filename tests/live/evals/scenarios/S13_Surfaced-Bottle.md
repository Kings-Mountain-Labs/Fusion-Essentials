---
id: S13_Surfaced-Bottle
fixture: none
---

## Prompt

GOAL - a BOTTLE, one hollow body, built as a surface product:

- Section: an ellipse at the base (about 80 x 55 mm; state your numbers) that stays elliptical up
  the body.
- Spine: the body leans; its centreline is a large-radius arc (about R750 mm), not a vertical line,
  and the body is roughly 200 mm tall along it.
- Shoulder and neck: a crowned shoulder blending into a round neck of about 28 mm diameter. The
  neck is round, the body is elliptical, and the transition is smooth.
- Skin: the outer form is built as surfaces (a loft or sweep of the section along the spine with
  guide curves, a crown surface that shapes the shoulder, a neck) and then closed into one solid by
  stitching; the wall is made once, by a shell or a thicken, to about 2 mm.
- Finish: a cosmetic fillet on the shoulder edge, and the mouth face offset inward by 0.05 mm as a
  cap clearance.

Name the sketches for their job: the base section, the profiles, the crown.

Report: the one solid's volume and bounding box; the spine arc's radius as a driving dimension and
the base ellipse's two radii with whether it reads fully constrained; the timeline order showing the
surface rows before the single shell or thicken; the wall thickness read at two places on a section;
the neck as a cylindrical face of the diameter you stated; and whether the screenshot reads as a
leaning bottle with a crowned shoulder, no facets and no step.

## Grader notes

- A good result, opened in Fusion (the bar is Autodesk's Bottle sample: a swept ellipse on an R750
  spine, a crown on projected edges, extend before trim, patch, stitch, chord-length fillets, the
  mouth offset): one smooth leaning body, elliptical low and round at the neck, a uniform 2 mm wall
  in section, the sketches named for their jobs.
- What a weak agent does: a stack of extrudes and a revolve, a straight body, a faceted shoulder, or
  a second shell.
- Axis this discriminates: the prompt. Every tool this needs exists; whether "built as a surface
  product" and the named steps are what makes the agent work surface-first is the question, and the
  B variant beside this file asks the same bottle in a client's words.
- First A/B to run: this file against S13_Surfaced-Bottle.B, three runs each, judged on the timeline
  order and the look.
- Earlier runs surfaced: the sweep carries no guide rail on the wire, so the section is lofted
  through placed ellipses; a crown built as a vertical-axis revolve cannot close on a leaning body
  and a loft from the body and shoulder ellipses to the neck circle reconciles it; view_screenshot
  has no iso-front-right view.
