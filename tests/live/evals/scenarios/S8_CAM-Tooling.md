---
id: S8_CAM-Tooling
fixture: S7_Template-Skeleton
---

## Prompt

The active document is a CAM template skeleton: a model component holding a placeholder part with
a curved feature and a through hole, a parametric stock with a self-centering joint origin, and a
vise fixture reference gripping the stock.

GOAL - the template's manufacturing layer:

- Build four tools in the document's tool library, each with a holder and sensible presets: a face
  mill, a drill, a flat endmill and a ball endmill. Sizes your choice for a small benchtop job; state
  them.
- Two setups whose model, stock and fixture selections are the components themselves, so whatever
  lives inside them now or later is consumed: a primary top setup and a second setup for the
  opposite side. The stock mode consumes the parametric stock; the work coordinate system sits on
  the stock-centre joint origin.
- Operations on the placeholder in the primary setup exercising all four tools, each aimed at real
  geometry: a facing pass on the top, a drilling operation into the placeholder's existing through
  hole, a roughing or contour pass with the endmill, and a finishing pass with the ball endmill on
  the curved feature. Do not add geometry to invent a target. Generate the toolpaths and poll to
  completion; every operation computes healthy, and every operation cuts real material. A healthy
  compute is not that proof: read each operation's heights against the stock's top and bottom and
  the placeholder's surface, and the tool against the feature it aims at (drill diameter against
  hole diameter, ball radius against the curve's radius, facing height against the stock top). An
  operation whose span sits outside the material cuts air; fix it or report it as a failure.
- Show each operation's toolpath one at a time with a screenshot against the part and say whether
  the picture agrees with the numbers.
- Activate the second setup, confirm it took, and activate the primary again.
- Save the job as a reusable CAM template in the template library as well, and report where it
  landed.

Report: the four tools with holders as the library read lists them; the two setups with the
components they select and the WCS; the four operations, their compute state, and the heights and
tool-versus-feature arithmetic per operation; the activation round trip; the template library
location.

## Grader notes

- A good result, opened in Fusion: a tool library of four tools with holders, two setups selecting
  components with the WCS on the stock origin, four operations that all compute and all touch
  material, and a template library entry.
- What a weak agent does: a drill that re-drills an open hole through the stock, a facing pass with
  its top above the stock, a setup that selects bodies rather than components, or new geometry on the
  placeholder to give itself a target.
- Axis this discriminates: MCP tooling. cam_edit_tools with holders and presets, setup component
  selection, WCS on a joint origin, cam_select_geometry for the hole and the curve, and per-operation
  height reads are what the wire has to carry.
- First A/B to run: `--deny mcp__fusion-essentials__cam_select_geometry` to see whether the
  operations still aim at the hole and the curve, and `--skill parametric-cad-design` for the
  practice axis.
- Earlier runs surfaced: CAM validity reads stale until the Manufacture workspace has been entered
  once; each template save adds a new library entry that no tool deletes.
