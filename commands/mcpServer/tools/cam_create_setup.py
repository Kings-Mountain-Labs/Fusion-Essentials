# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a CAM (Manufacture) setup on a part: pick an operation type and the bodies to machine (or
default to every body in the root component), producing a new Setup ready for cam_apply_template /
cam_create_operation."""

import adsk.core
import adsk.cam
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import apply_rename, ok, error, safe
from ._cam_common import get_cam, setup_names
from . import _common
from . import _inputs

app = adsk.core.Application.get()

_OP_TYPES = {"milling": "MillingOperation", "turning": "TurningOperation"}

_OP_TYPE = _inputs.Choice("operation_type", options=list(_OP_TYPES), default="milling")
# models: bodies (handle/name) OR component occurrences (name); omitted -> all root bodies.
# Setup.models accepts an Occurrence, a BRepBody or a MeshBody. Measured: an OCCURRENCE keeps that
# selection when the component's contents are replaced, and the setup stays valid.
_MODELS = _inputs.TargetRefList("models", required=False,
                                description="Omit = every root-component body.")


def setup_name_clash(cam, want, current=""):
    """The refusal for a name setups already answer to, shared by the create and the rename arm.
    None when the name is free, empty, or already the caller's own (`current`)."""
    want = (want or "").strip()
    if not want or want.lower() == (current or "").strip().lower():
        return None
    taken = [n for n in setup_names(cam) if (n or "").lower() == want.lower()]
    if not taken:
        return None
    # Measured: Setup.name dedupes silently rather than refusing - 'LegSetup' with one taken lands
    # 'LegSetup1' - so a taken name is refused here instead of landing unasked-for.
    return (f"{len(taken)} setup(s) already answer to '{want}'. Setup.name dedupes rather than "
            f"refusing, so it would land as something like '{want}1' - a name nothing asked for. "
            "Pick one no setup carries; cam_get lists them.")


def _all_root_bodies(design):
    """Every BRep body in the root component - solid AND surface - the default machining set;
    Setup.models is typed to BRepBody, and a setup does carry a surface body."""
    root = safe(lambda: design.rootComponent)
    return list(_common.iter_collection(safe(lambda: root.bRepBodies) if root else None))


def handler(operation_type: str = "milling", models=None, name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    op_key, oerr = _OP_TYPE.resolve(operation_type)
    if oerr:
        return error(oerr)

    cam, cam_err = get_cam()
    if cam_err:
        return error(cam_err)

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    # Resolve the models: explicit BodyRefList (handles/names), else all root bodies.
    if models not in (None, "", []):
        body_list, merr = _MODELS.resolve(models)
        if merr:
            return error(merr)
    else:
        body_list = _all_root_bodies(design)
    if not body_list:
        return error("No bodies to machine. The root component holds no bodies "
    "- add geometry first, or pass 'models' = body handles/names (a body inside a "
    "sub-component is not in the default set).")

    # The name is refused BEFORE the add, through the same check the rename arm runs: a deduped
    # name would otherwise land silently and the caller would address the setup by the wrong one.
    clash = setup_name_clash(cam, name)
    if clash:
        return error(clash)

    try:
        op_enum = getattr(adsk.cam.OperationTypes, _OP_TYPES[op_key])
        inp = cam.setups.createInput(op_enum)
        inp.models = list(body_list)
        setup = cam.setups.add(inp)
    except Exception as e:
        return error(f"Failed to create the {op_key} setup: {e}")
    if not setup:
        return error("Setup creation returned nothing.")
    # The setup has landed, so a declined or deduped name is a DISCLOSURE, not a failed create -
    # what the payload publishes is the name Setup.name reads back.
    new_name, rename_warning = apply_rename(setup, name)
    if new_name:
        landed = any(safe(lambda s=s: s.name) == new_name
                     for s in _common.iter_collection(safe(lambda: cam.setups)))
        if not landed:
            return error(f"setups.add returned '{new_name}' but it does not appear when the setups "
                         "are re-listed - the setup did not land.")

    result = {
        "created": True,
        "setup_name": new_name,
        "operation_type": op_key,
        "model_count": len(body_list),
    "models": [safe(lambda b=b: b.name) for b in body_list],
    "operation_count": safe(lambda: setup.allOperations.count, 0),   # total incl. foldered ops (0 at creation)
    "note": ("Setup created (no operations yet). Add toolpaths with cam_apply_template (a "
            "COMPATIBLE template - a milling setup needs a milling template), then "
            "cam_generate. Be in the Manufacture workspace before generating."),
    }
    if rename_warning:
        result["rename_warning"] = rename_warning
    return ok(result)


TOOL_DESCRIPTION = (
    "Create a CAM (Manufacture) setup, then add toolpaths with cam_create_operation."
)

tool = (
    Tool.create_simple(name="cam_create_setup", description=TOOL_DESCRIPTION)
    .add_input_property(_OP_TYPE.name, _OP_TYPE.schema())
    .add_input_property(_MODELS.name, _MODELS.schema())
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The name apply_rename reads back off Setup.name is looked for in the setups RE-LISTED off the
    # CAM product, and a name the listing does not carry is an error, not a created=true.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_create_setup.py::TestOperationType"
                      "::test_phantom_setup_that_never_lands_bites",
        rung="value"))


def register_tool():
    register(item)
