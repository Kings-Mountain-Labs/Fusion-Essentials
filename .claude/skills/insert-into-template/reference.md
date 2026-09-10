# Reference - Workflow Template methodology

Loaded on demand by the `insert-into-template` skill when a crawl result is ambiguous.
Distilled from the AU2024 class *Templates, Configurations, and Containers for Agile
Prototype Machining in Autodesk Fusion* (MFG3914) and the Fusion-Essentials tool notes.
No shop-specific data here -- only the published framework concepts and the API facts needed
to read a template correctly.

## The core idea: reconfiguring, not reprogramming

Replace the model in a template and have toolpaths regenerate; switch fixtures or machines via
a configuration table -- all while keeping joints and CAM selections intact. The structures
below exist to make that possible.

## Components as slots (the AU class's "Component Containers")

A Component holds the CAD; the AU class names this slot role a "Component Container". A CAM
setup's Model selection points at the *component*, not the geometry inside it -- so the setup
keeps its selection even when the component's contents are replaced (measured: `Setup.models`
answers the same Occurrence after the component's only body is deleted and a different one
extruded, and the setup stays valid). The relative stock box is a CACHED evaluation: measured on a
40x40x10 mm body swapped for a 60x60x20 mm one, it still read the original `-21/21` after
`computeAll` AND after a face toolpath regenerated valid against the new body, and only a save +
reopen refreshed it to the new body's `-31/31`. So an in-session read of that box describes the
part that is gone - which is why this procedure sizes the stock itself. A Body is just geometry and
must live inside a Component; a Component can be empty (only its origin planes/axes).

Consequence for crawling: a setup's `models` / `fixtures` / `stockSolids` entries are usually
**component Occurrences** playing that slot role (in shipped templates named like `Model
Component`, `Fixture Container`, `Stock Container`). Reading only the top level shows the slot
component, not its contents - descend into it (`design_get(include=['tree'],
component=<the slot>)`) to see the real parts.

## Joint Origin Containers (JOC) and joint survival

A JOC is an empty component with a Joint Origin at its coordinate origin, joined to the real
geometry, so joints survive model replacement. Joints survive across files via Save-As lineage
from a common ancestor (shared EntityIDs let Fusion auto-repair joints). This is why
replaceable fixtures are often named like "...Save-as and replace to make a new fixture."

## The Replaceable Fixturing Assembly (RFA) - what to expect inside a setup

A setup's Model selection is a component nesting three standardized typed files:

| Typed file | Role |
|---|---|
| **Vise Type** | Grips the stock; standardized attachment points for jaws / zero-point systems. |
| **Clamping Unit / Pallet Type** | Mounts the vise to the machine; defines the Machine Model Attachment point used in simulation. |
| **WCS Type** | A **simple cube** that explicitly defines the Z and X directions for the CAM setup, so the WCS is always accurately located. |

So when the tree read descends a setup's model/fixture component, expect: a Clamping
Unit/Pallet + a Vise + a WCS cube + the machined model.

## Two classification rules

1. **A slot component is not one opaque object.** Descend it; the structure only becomes
   legible when you read the nesting.
2. **The WCS cube is not a placeholder.** A lone cube referenced by a setup is almost
   certainly the WCS Type -- it defines the WCS orientation. Never delete it.

## Selectionless toolpaths and parametric stock

- **Selectionless toolpaths** (e.g. 3D Adaptive, Bore) point at the model component and use
  geometry recognition + diameter ranges, so they regenerate automatically when a new part is
  inserted.
- **Parametric stock** is driven by user parameters; jaws adjust to the workpiece via
  configured joints. The stock params live in `Design.userParameters` (named like stock /
  fixture dimensions); their `.expression` may reference other parameters (the parametric
  linkage). These are the params the skill writes the measured part size to (PART_PARAMS).

## How this maps to the Fusion API (for the building blocks)

- A CAM setup's `models` / `fixtures` / `stockSolids` are typically component Occurrences;
  `cam_get` reports them per setup (`selected_models` / `fixtures` / `stock_solids`) and
  `design_get(include=['tree'])` descends them and resolves external references.
- An external reference is `Occurrence.isReferencedComponent == True`; it resolves via
  `Occurrence.documentReference.dataFile` -> `.id` (lineage UID / URN), `.name`,
  `.fusionWebURL`.
- CAM data is reachable WITHOUT switching to the Manufacture workspace -- `cam_get` already
  does this.
- `cam_compare_operations` reports exact parameter expressions (including float jitter like
  `38.10000000000001`) deliberately. Do not round or filter; reason about precision.

## Copying a CAM template safely: save-as, never DataFile.copy

There are two ways to duplicate a cloud document, and only one is safe for a CAM template:

- **`doc_copy`** wraps `DataFile.copy(targetFolder)` on a CLOSED cloud file. It triggers a cold,
  server-side reconciliation of the document's whole external-reference graph. For a
  multi-reference configured-design CAM template this reliably destabilises the session.
- **`doc_save_as`** wraps `Document.saveAs(...)` on the OPEN, already-loaded document. Fusion has
  already resolved its references in-session, so save-as just writes the loaded state to a new
  lineage (references preserved) — no cold reconciliation.

So the template-copy step is: OPEN the library template, then `doc_save_as` it as `<model>_CAM`.
`Document.saveAs` makes the saved-as copy the ACTIVE document, so no separate re-open is needed; the
whole copy→settle→open sequence collapses to open→save-as. The library original is never modified.

## Opening a configured-design / multi-reference CAM document via the API

A `doc_open` with `force_api_open=true` opens these fine WHEN the document is settled and the open is
a single, unhurried step. Instability appears when two heavy reference-graph operations overlap on
the main thread (e.g. opening while a fresh copy is still resolving, or a heavy geometry edit before
the open has finished loading). Practical rule: after any open/save-as of such a doc, confirm it is
active (`doc_get`) and let it settle before the next write. (`is_cam_template=true` is a more
conservative refuse-to-open mode; the save-as path above avoids needing it.)

## Why an inserted part must be UN-GROUNDED before a positioning joint

An occurrence inserted into a parent component is `ground_to_parent = TRUE` by default — rigidly locked to
its parent. A rigid joint that needs the part to MOVE to mate then can't resolve: the joint computes
as a failed/warning state ("Can't resolve component positions — conflicts with assembly
relationships") and `assembly_get` reports its `occurrence_two` as null. Setting
`ground_to_parent = false` on the inserted occurrence frees it, and the identical joint then computes
healthy. (A healthy joint to a ROOT-level joint origin still reports `occurrence_two = null` — that is
normal for a root-anchored JO; trust the `healthy` flag and the part's measured position, not that
field.)

## Joining a JO inside a referenced occurrence (the assembly-context proxy)

A joint origin that lives inside an inserted/referenced part (like "Center of Model") is a NATIVE
object of that part's component. `Joints.createInput` on the template's root component rejects the
native JO — and its `.geometry` — with **"Provided input paths for joint are not valid"**: joint
inputs must be in the ROOT's assembly context. The fix is the occurrence PROXY:
`nativeJO.createForAssemblyContext(<the occurrence that instances the part>)` — the proxy carries
the occurrence path, and the joint then computes.

`joint_create` does this proxying automatically when you pass the JO by NAME (bare, or scoped as
`<occurrence>:<JO name>` when several instances carry the same JO). This is why the skill joins
with `joint_create` and never scripts the joint by hand: a hand-rolled script that reaches for
`component.jointOrigins.itemByName(...)` gets the native JO and hits the error above.

## Part-space extents and orientation (the oriented bounding box)

The "Center of Model" JO is built (`joint_create_origin(anchor="bbox_center", ...)`) with its Z along
the machining direction (`zdir`) the operator picked, and located at the part's bounding-box centre.
To report extents IN that part frame (not world axes), `model_inspect(target=<body>, frame="Center of
Model")` measures with `measureManager.getOrientedBoundingBox(body, lenDir, widDir)` passing the JO's
secondary (X) and third (Y) axis vectors: the result's `x`/`y`/`z` (length/width/height) then
correspond to part-space X / Y / Z, where Z is the machining axis. This makes `extents_mm` meaningful
regardless of how the part was modelled relative to world axes - it is always reported in the
machining frame. `model_inspect`'s `frame=` parameter is the typed home for this measurement; no other
tool computes an oriented (as opposed to world-axis-aligned) bounding box.

## Determinism (why the phases are ordered this way)

Reads establish ground truth before any write; every write states its EXPECT and is re-read
afterward; a failed expectation stops the run instead of improvising. Same starting state,
same chain, same result.
