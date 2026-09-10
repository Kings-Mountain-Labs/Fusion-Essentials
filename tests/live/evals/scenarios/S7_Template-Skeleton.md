---
id: S7_Template-Skeleton
fixture: none
---

## Prompt

The active document is new and empty; it becomes a reusable CAM template. The vise it will hold is
the newest document whose name starts with S6_Vise in folder {{FOLDER}} of project {{PROJECT}}.
Open it once so you can reference it, then return to the template and work there; when the
staleness step needs the vise edited, edit that same open document, save it, and come back.

GOAL - the template skeleton a machining job drops into:

- Three top-level components: one for the MODEL (the part to machine), one for the STOCK, one for
  the FIXTURE.
- In the stock component, a parametric stock block driven by user parameters (StockX, StockY, StockZ
  or your naming), plus a joint origin at the stock's centre by offset expressions tied to those
  parameters, so resizing the stock re-centres the origin. Prove it: resize via the parameters and
  read the origin's position at both sizes.
- Insert the vise into the fixture component as an external reference, a linked instance, not a
  copy. Then prove the link is alive: open the vise source, change its jaw-opening parameter, save
  it; back in the template a fresh reference read shows the vise stale; update the reference; a fresh
  read shows it current and the jaws moved.
- Grip the stock in the vise with jaw-to-stock joints at this document's level so it sits held
  between the jaws at the vise centre. A rigid park of the stock to a body is parking, not clamping.
  The vise is self-centering, both jaws move when the opening changes, so a grip that rigidly follows
  one jaw drifts off centre. Measure the vise yourself first: jaw geometry, grip-face size, reachable
  opening, all from fresh reads; size the stock so its clamped width fits and each jaw's grip face
  sits flush on a stock flank.
- In the model component, a placeholder part with a simple solid carrying a modest curved feature (a
  fillet, a rounded boss, a curved top) and one through hole, so the CAM layer has a curve to finish
  and a hole to drill. It sits inside the stock's envelope with machining allowance on every side,
  and its features cut the placeholder alone, never the stock or the fixture.
- Tag the template for its consumers: attach named attributes to its key timeline features (the
  stock feature and the fixture insert at least), a group name of your choosing and a key/value per
  feature. Prove the tags land by querying them back and report what the query returned.

Report: the three components and their contents; the origin's read-back against the measured stock
centre at two sizes and again after gripping; the stock width against the jaw opening and the jaw
face you read; the reference's version at each step of the stale-update cycle; the grip joints and
the per-jaw flush distance before and after the vise update; a whole-template interference check
with only the grip faces as contacts; the placeholder's box inside the stock's box with the
allowances; the tag query results.

## Grader notes

- A good result, opened in Fusion: a template with three named components, a stock block whose joint
  origin sits at its centre at any size, the vise as a live reference gripping the stock flush on both
  flanks and still centred after the jaws moved, a small placeholder with a rounded feature and a hole
  fully inside the stock, and tags that a query finds.
- What a weak agent does: parks the stock rigidly to one jaw and drifts off centre when the jaws
  move, sizes the stock from numbers in its head rather than the vise it read, drills the placeholder's
  hole through the stock, or inserts a stale vise found by name.
- Axis this discriminates: MCP tooling. doc_insert_occurrence as a reference, doc_update_xref, the
  attribute tagging and query, and jaw-to-stock joints across a reference are all wire questions.
- First A/B to run: `--deny mcp__fusion-essentials__doc_update_xref` (does the agent find another
  way to refresh, or report the wall), then `--skill insert-into-template`.
- Earlier runs surfaced: a placeholder shipped entirely above the stock with its hole drilled through
  the stock; asserted jaw numbers in a brief went stale against the real vise, which is why the brief
  forbids assumed numbers; post-save version reads lag by seconds, so a stale first read honestly
  re-read is good behaviour.
