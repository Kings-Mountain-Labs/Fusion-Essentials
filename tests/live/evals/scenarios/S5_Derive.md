---
id: S5_Derive
fixture: none
---

## Prompt

The active document is new and empty; it becomes the machining-prep model for the gyroscope's
OUTER RING. The finished gyroscope is the newest document whose name starts with S4_Details in
folder {{FOLDER}} of project {{PROJECT}}. Open it (a derive needs its source open), then return to
the new document and do all your modelling there. Never modify or save the source.

GOAL - a machining-prep model of the outer ring (the middle ring, between frame and inner ring):

- Insert a derive of only the outer ring component from the source into the new document: a
  one-way linked copy that updates from the source and sends nothing back. Nothing else from the
  source may land; the machining model is the ring alone. Confirm with fresh reads that what you
  received is real, linked, current, and exactly the one component.
- Detail features on the derived body itself: external fillets on outer edges you choose (report
  the edges and the radius). A derive is locally editable; these edits live here while the source
  stays authoritative.
- A datum plane part-way along one of the ring's circular edges, anchored to the edge at a fraction
  of its length rather than at a typed coordinate, so it rides the edge if the ring resizes. Report
  the fraction and the plane's read-back position.
- Patch surfaces closing each radial cross-hole opening through the ring wall, so a toolpath sees
  them closed. The central bore stays open. These openings are hard; if a patch refuses, read its
  error, it names what the boundary needs.
- Offset surfaces from at least two faces, one at zero offset and one at a nonzero offset you
  choose.
- At least one boundary sketch projecting machining-relevant geometry.
- A joint origin at the measured centre of the derived part's bounding box (the part alone, not
  your prep surfaces), axes oriented to the machining direction you choose.

Report: the derive reference and its freshness, the tree showing exactly the one component, how
many parameters the derive imported (a low or zero count is a platform fact; report it), the
fillet and the body's volume before and after, the datum plane, the patch count and which opening
each closes with the bore still open, the offsets, the joint origin's read-back against the measured
centre, and a fresh cloud read showing the source still at the version you found.

## Grader notes

- A good result, opened in Fusion: one derived ring component, filleted, with a small patch over each
  side hole and the bore open, two offset surfaces, a boundary sketch, a joint origin dead centre, and
  a reference row that reads current against its source.
- What a weak agent does: derives the whole gyroscope, patches the bore, drops the datum plane at a
  coordinate, or opens a second copy of the source by name and derives a stale ring.
- Axis this discriminates: MCP tooling. doc_insert_derive's component scoping, surface_patch on a
  saddle-shaped opening (a single seed fails; the boundary from the opening's two half-edges works),
  and the along-edge construction plane are wire questions.
- First A/B to run: `--deny mcp__fusion-essentials__surface_patch` to see whether surface_fill or
  another route closes the holes, and a B variant that names the source document by URN instead of
  by folder search.
- Earlier runs surfaced: the parameter-import flags are a platform no-op (zero of seventeen
  imported); a by-name search for the source picked a stale lineage with a different diameter.
