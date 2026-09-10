# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a machine in the LOCAL machine library from one of Fusion's machine templates, named so
cam_edit_setup(machine=...) can assign it. Creation lives on adsk.cam.Machine as statics -
MachineLibrary itself exposes no create method. cam_delete_machine is the other half."""

import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _inputs
# A new name is checked through resolve_machine, the same read an assignment resolves by. The
# machine_catalog walk is not used: it reads capabilities on every bundled machine, which runs past
# the handler cap.
from ._cam_common import machine_kinds, machine_library, machine_location, resolve_machine

# Wire value -> the adsk.cam.MachineTemplate member it builds from. The template fixes the new
# machine's kinematics tree; a member this Fusion version does not expose is refused by name.
_TEMPLATES = {
    "generic_3_axis": "Generic3Axis",
    "generic_4_axis": "Generic4Axis",
    "generic_5_axis_head_head": "Generic5AxisHeadHead",
    "generic_5_axis_head_table": "Generic5AxisHeadTable",
    "generic_5_axis_table_table": "Generic5AxisTableTable",
    "generic_fff": "GenericFFF",
    "generic_lathe": "GenericLathe",
}
_TEMPLATE = _inputs.Choice("template", options=list(_TEMPLATES), default="generic_3_axis")

# resolve_machine's no-match error opens with this - the ONE outcome that proves the name is FREE.
# Any other resolver answer (a hit, an ambiguity refusal, a library error) means the name is not
# provably free and the create must refuse rather than risk retargeting an assignment string.
_NAME_FREE_PREFIX = "No machine matches"


def _name_clash(lib, name):
    """What `name` already reaches, through the SAME query cam_edit_setup assigns by.
    Returns (machine, rung, location, None) on a clash, (None, None, None, None) when the name is
    provably free, or (None, None, None, error) when freeness cannot be proven (an ambiguous name
    IS a clash: several machines answer to it)."""
    found, label, rerr = resolve_machine(name)
    if found is None:
        if rerr and rerr.strip().startswith(_NAME_FREE_PREFIX):
            return None, None, None, None
        return None, None, None, (f"'{name}' cannot be proven free: {rerr}")
    # The rung the name matched on, recomputed against the ONE machine the resolver returned - the
    # same keys the resolver selects by (label, then 'vendor model', then model).
    want = name.strip().lower()
    vendor, model = (safe(lambda: found.vendor) or ""), (safe(lambda: found.model) or "")
    rung = "name"
    for key, value in (("name", label), ("vendor model", (vendor + " " + model).strip()),
                       ("model", model)):
        if (value or "").strip().lower() == want:
            rung = key
            break
    return found, rung, machine_location(lib, found), None


def _write_field(machine, prop, value):
    """Set one Machine field and read it back. Returns an error string, or '' when the value landed.
    Every field is written BEFORE the machine is stored, so a refusal here leaves the library
    untouched."""
    try:
        setattr(machine, prop, value)
    except Exception as e:
        return f"Could not set Machine.{prop} to '{value}': {e}."
    landed = safe(lambda: getattr(machine, prop))
    if landed != value:
        return (f"Set Machine.{prop} to '{value}' but it reads back '{landed}' - the value did not "
                "land, so nothing was stored in the library.")
    return ""


def handler(name: str = "", template: str = "generic_3_axis", vendor: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' - the new machine's name. It becomes Machine.description, the "
                     "label cam_edit_setup(machine=...) resolves an assignment by.")
    key, terr = _TEMPLATE.resolve(template)
    if terr:
        return error(terr)
    member = getattr(adsk.cam.MachineTemplate, _TEMPLATES[key], None)
    if member is None:
        return error(f"This Fusion version's MachineTemplate has no '{_TEMPLATES[key]}' member.")

    lib, lerr = machine_library()
    if lerr:
        return error(lerr)

    # Refuse before anything is created when the name is ALREADY how the library reaches some other
    # machine - by its label, its model, or its 'vendor model'. Taking such a name does not just
    # duplicate a label: it retargets an assignment string that resolves to another machine.
    clash, clash_key, clash_loc, cerr = _name_clash(lib, name)
    if cerr:
        return error(cerr)
    if clash:
        c_label, c_vendor, c_model = (safe(lambda: clash.description) or "",
                                      safe(lambda: clash.vendor) or "",
                                      safe(lambda: clash.model) or "")
        return error(f"'{name}' is already how the {clash_loc} machine library reaches "
                     f"'{c_label or c_model}' (vendor '{c_vendor}', model '{c_model}') - it "
                     f"matches that machine's {clash_key}. An assignment resolves by those keys, "
                     "so pick another 'name'.")

    try:
        machine = adsk.cam.Machine.createFromTemplate(member)
    except Exception as e:
        return error(f"Machine.createFromTemplate('{key}') failed: {e}.")
    if machine is None:
        return error(f"Machine.createFromTemplate('{key}') returned nothing - no machine was created.")

    # A machine off a template arrives carrying that template's own description/vendor/model, which
    # every machine built from it shares, so the name REPLACES them. It goes on the MODEL too: the
    # library query is keyed on that field, not on the description.
    for prop, value in (("description", name), ("vendor", (vendor or "").strip()),
                        ("model", name)):
        if not value:
            continue
        werr = _write_field(machine, prop, value)
        if werr:
            return error(werr)

    root = safe(lambda: lib.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation))
    if root is None:
        return error("Could not resolve the Local machine library location to save into.")
    try:
        url = lib.importMachine(machine, root, name)
    except Exception as e:
        return error(f"Storing machine '{name}' in the Local machine library failed: {e}.")
    if not url:
        return error(f"Storing machine '{name}' in the Local machine library returned no URL - "
                     "the machine was not stored.")
    if safe(lambda: lib.machineAtURL(url)) is None:
        return error(f"importMachine returned a URL for '{name}' but no machine loads back from it "
                     "- the create did not land.")

    # The honesty gate: re-resolve through the SAME query cam_edit_setup assigns from. A machine
    # that cannot be found again by its name is a create the caller cannot use.
    stored_url = safe(lambda: url.toString())
    found, label, rerr = resolve_machine(name)
    if found is None:
        return error(f"Machine '{name}' was stored in the Local machine library ({stored_url}) but "
                     f"it does not resolve back through the query cam_edit_setup assigns from: "
                     f"{rerr} The stored machine is still there.")
    if (label or "").strip().lower() != name.lower():
        return error(f"Machine '{name}' was stored in the Local machine library ({stored_url}) but "
                     f"that name resolves to '{label}' - an assignment would pick a different "
                     "machine. The stored machine is still there.")

    # The resolver gate above IS the catalog evidence: cam_get(include=['machines']) reads the same
    # locations through the same library reads.
    has_sim = bool(safe(lambda: found.hasSimulationModel, False))
    note = ("Machine created and re-resolved through the query cam_edit_setup assigns from - the "
            f"same read the cam_get(include=['machines']) catalog is built on. Assign it: "
            f"cam_edit_setup(setup=..., machine='{label}'). It persists in the local machine "
            f"library until cam_delete_machine(name='{label}') removes it.")
    if has_sim:
        note += (" It carries a simulation model, which the assignment refuses - pass "
                 "machine_strip_simulation=true to cam_edit_setup.")
    return ok({
        "created": True,
        "name": label,
        "machine_id": safe(lambda: found.id),
        "template": key,
        "location": "local",     # importMachine targeted the Local root and machineAtURL confirmed
        "url": stored_url,
        "asset_name": safe(lambda: url.leafName),
        "vendor": safe(lambda: found.vendor),
        "model": safe(lambda: found.model),
        "kind": machine_kinds(found),
        "has_post": bool(safe(lambda: found.hasPost, False)),
        "has_simulation_model": has_sim,
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Create a MACHINE in the LOCAL machine library from a Fusion machine template, so "
    "cam_edit_setup(machine=...) can assign it by name."
)

tool = (
    Tool.create_simple(name="cam_create_machine", description=TOOL_DESCRIPTION)
    .add_input_property("name", {"type": "string"})
    .add_input_property(*_TEMPLATE.as_property())
    .add_input_property("vendor", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # Every identity field published (name, machine_id, vendor, model, kind, has_post) is read off
    # the machine the resolver hands back after the store, not off the request.
    verification=Verification(
        kind="effect",
        evidence_test="tests/unit/test_cam_create_machine.py"
                      "::TestStoreAndGate"
                      "::test_a_name_resolving_to_a_different_machine_is_an_error",
        rung="value"))


def register_tool():
    register(item)
