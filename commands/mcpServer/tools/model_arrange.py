# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: ARRANGE (nest/pack) component occurrences within a sketch-profile boundary.

  arrange -> an Arrange feature packing the given occurrences inside a 2D envelope taken from a
             sketch profile. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from . import _common
from . import _inputs
from . import _assert
from . import _sketch_detail

app = adsk.core.Application.get()

_SOLVERS = {
"true_shape": "Arrange2DTrueShapeSolverType",
"trueshape": "Arrange2DTrueShapeSolverType",
"true": "Arrange2DTrueShapeSolverType",
"rectangular": "Arrange2DRectangularSolverType",
"rect": "Arrange2DRectangularSolverType",
}

_SOLVER = _inputs.Choice("solver", list(_SOLVERS), default="true_shape")


# Occurrences to arrange, via the shared OccurrenceRefList kind (fullPathName-preferring,
# ambiguity-refusing - no silent wrong-instance grab).
_SHAPES = _inputs.OccurrenceRefList("shapes", required=False)


def handler(boundary_sketch: str = "", shapes: str = "", solver: str = "true_shape",
            spacing: float = 0.0, units: str = "mm", boundary_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    solver_key = (solver or "true_shape").strip().lower()
    if solver_key not in _SOLVERS:
        return error("Unknown solver '%s'. Use 'true_shape' or 'rectangular'." % solver)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    sketch, refusal = _sketch_detail.scoped_sketch(design, (boundary_sketch or "").strip(),
                                                   boundary_component, "boundary_component")
    if refusal:
        return error(refusal)
    if not sketch:
        return error(f"No sketch named '{boundary_sketch}' for the boundary. Use sketch_get.")
    # profiles.item(0) below is a blind index off the sketch's own collection, so a deferred sketch
    # would hand the envelope whichever region was first before the deferral.
    stale = _inputs.deferred_sketch_refusal("boundary_sketch", sketch)
    if stale:
        return error(stale)
    profiles = safe(lambda: sketch.profiles)
    if not profiles or safe(lambda: profiles.count, 0) == 0:
        return error(f"Boundary sketch '{boundary_sketch}' has no closed profile to use as the "
    "envelope. Draw a closed boundary shape first.")
    envelope_profile = profiles.item(0)

    if not (shapes or "").strip() if isinstance(shapes, str) else not shapes:
        return error("Provide 'shapes' - the occurrence name(s) to arrange (comma-separated).")
    occs, shapes_err = _SHAPES.resolve(shapes)
    if shapes_err:
        return error(shapes_err)
    if not occs:
        return error("Provide 'shapes' - at least one occurrence to arrange.")
    resolved = [safe(lambda o=o: o.name) for o in occs]

    af = safe(lambda: design.rootComponent.features.arrangeFeatures)
    if af is None:
        return error("This design does not expose Arrange features.")

    # Effect evidence read BEFORE the add: the solver can leave the named occurrences unmoved and
    # mint envelope copies instead, which only these two reads distinguish from a real nest.
    def _translation(o):
        t = safe(lambda: o.transform2.translation)
        return (safe(lambda: t.x), safe(lambda: t.y), safe(lambda: t.z)) if t is not None else None
    before_pos = {nm: _translation(o) for nm, o in zip(resolved, occs)}
    before_paths = set(_common.occurrence_paths(design))

    try:
        ST = adsk.fusion.ArrangeSolverTypes
        inp = af.createInput(getattr(ST, _SOLVERS[solver_key]))
        if not inp:
            return error("Could not create the arrange input (solver may be unavailable).")
        env = inp.setProfileOrFaceEnvelope([envelope_profile])
        if env is None:
            return error("Could not set the boundary envelope from the sketch profile.")
        if spacing:
            env.objectSpacing = adsk.core.ValueInput.createByReal(float(spacing) * k)
        for o in occs:
            inp.arrangeComponents.add(o)
        feature = af.add(inp)
    except Exception as e:
        msg = str(e)
        if any(t in msg.lower() for t in ("extension", "entitle", "license", "subscrib")):
            return error(f"Arrange ({solver_key}) appears to need a Fusion extension on this "
                          f"account: {msg}. Try solver='rectangular', or enable the extension.")
        return error(f"Arrange failed: {msg}")
    if not feature:
        return error(_common.no_feature_error(design, "Arrange"))

    # Read the effect back: which inputs actually MOVED, and which occurrence paths the feature
    # ADDED (the solver restructures parts under Envelope occurrences and can mint copies).
    moved = []
    for nm, o in zip(resolved, occs):
        now = _translation(o)
        was = before_pos.get(nm)
        if now is not None and was is not None and any(
                a is not None and b is not None and abs(a - b) > 1e-6 for a, b in zip(now, was)):
            moved.append(nm)
    new_paths = sorted(set(_common.occurrence_paths(design)) - before_paths)
    if not moved and not new_paths:
        rolled = bool(safe(lambda: feature.deleteMe(), False))
        return error("Arrange reported success but NOTHING happened - no input occurrence moved "
                     "and no occurrence was added. "
                     + ("The empty arrange feature was rolled back." if rolled
                        else "The empty arrange feature could not be rolled back - remove it with "
                             "design_delete_feature.")
                     + " Check the boundary profile holds the shapes at this spacing.")

    note = "Shapes arranged within the boundary. Pair with view_screenshot (top) to view the nest."
    if new_paths and not moved:
        note += (" NOTE: the solver placed COPIES under new Envelope occurrences - the named "
                 "input occurrences did NOT move (positions read back unchanged). Re-running an "
                 "identical arrange STACKS another coincident copy set; delete the originals or "
                 "the envelope (design_delete_feature on this feature) if copies were not intended.")
    elif new_paths:
        note += " The solver restructured the arranged parts under new Envelope occurrences."
    payload = {
        "arranged": True,
        "feature": safe(lambda: feature.name),
        "solver": "true_shape" if "TrueShape" in _SOLVERS[solver_key] else "rectangular",
        "boundary_sketch": safe(lambda: sketch.name),
        "arranged_count": len(resolved),
        "shapes": resolved,
        "moved": moved,
        "spacing": round(float(spacing), 6) if spacing else 0.0,
        "units": units,
        "note": note,
    }
    if new_paths:
        payload["new_occurrences"] = new_paths[:12]
        payload["new_occurrence_count"] = len(new_paths)
    return ok(payload)


TOOL_DESCRIPTION = (
"Nest component occurrences inside a 2D boundary taken from a sketch profile."
)

tool = (
    Tool.create_simple(name="model_arrange", description=TOOL_DESCRIPTION)
    .add_input_property("boundary_sketch", {"type": "string",
            "description": "Sketch whose profile is the boundary."})
    .add_input_property("boundary_component", {"type": "string",
            "description": "Component holding 'boundary_sketch'."})
    .add_input_property(*_SHAPES.as_property())
    .add_input_property(*_SOLVER.as_property())
    .add_input_property("spacing", {"type": "number",
            "description": "Clearance between parts, in 'units'."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_model_arrange.py::TestHonesty"
                                               "::test_nothing_happened_is_an_error_and_rolls_back"))


def register_tool():
    register(item)
