# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Remove a machine from the LOCAL machine library - the other half of cam_create_machine's
lifecycle. What gets deleted is the library ASSET; this tool reads no setup."""

import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, named_with_remainder
# The shared CAM substrate: the same machine-library handle and by-name resolver cam_create_machine
# and cam_edit_setup(machine=...) run through, the bounded library asset walk, and the ONE
# leafName-or-stem asset matcher every library delete addresses its target with.
from ._cam_common import (assets_named, asset_key, asset_leaf, library_assets, machine_label,
                          machine_library, machine_location, resolve_machine)

# How many local asset names a refusal spells out before named_with_remainder counts the rest.
_ASSET_NAMES_CAP = 12

# Which read addressed the asset, published so the caller knows what the delete was keyed on. The
# file name never addresses one ALONE - it only narrows a tie between assets holding the same
# machine, which is the one thing the label cannot separate.
_BY_MACHINE_NAME = "the machine name the asset holds"
_BY_MACHINE_NAME_AND_FILE = "the machine name the asset holds, narrowed by the asset's file name"


def _local_assets(lib):
    """(assets, truncated) under the Local machine library root, or (None, None) when the root does
    not resolve. Folders are recursed by the ONE bounded library walk every CAM library read uses."""
    root = safe(lambda: lib.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation))
    if root is None:
        return None, None
    return library_assets(lib, root)


def _assets_holding(lib, assets, wanted):
    """The Local assets whose LOADED machine label is one of `wanted`, deduped by url - the name
    cam_create_machine and cam_get report, which the asset's stored file name need not carry."""
    keys, hits = set(), []
    for a in assets:
        m = safe(lambda a=a: lib.machineAtURL(a))
        if m is None:
            continue
        if (machine_label(m) or "").strip().lower() in wanted:
            key = asset_key(a)
            if key not in keys:
                keys.add(key)
                hits.append(a)
    return hits


def _misfiled_clause(lib, assets, wanted):
    """The near-miss sentence for an asset FILED under a name the caller used that does NOT hold the
    machine - what it holds instead, or that it loads nothing. '' when no asset is filed under one."""
    for a in assets_named(assets, wanted):
        held = safe(lambda a=a: lib.machineAtURL(a))
        if held is None:
            return (f" The asset '{asset_leaf(a)}' is FILED under that name but does not load a "
                    "machine.")
        return (f" The asset '{asset_leaf(a)}' is FILED under that name but holds "
                f"'{machine_label(held)}'.")
    return ""


def _addressed(lib, assets, label, wanted):
    """(hits, the read that found them) - the assets HOLDING the machine `label` names, narrowed by
    the FILE name when several hold it and exactly one is also filed under a name the caller used.
    A machine stored under another file name is still reached by the name every read reports it."""
    want = (label or "").strip().lower()
    hits = _assets_holding(lib, assets, {want}) if want else []
    if len(hits) > 1:
        narrowed = assets_named(hits, wanted)
        if len(narrowed) == 1:
            return narrowed, _BY_MACHINE_NAME_AND_FILE
    return hits, _BY_MACHINE_NAME


def handler(name: str = "", confirm_name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    name = (name or "").strip()
    confirm_name = (confirm_name or "").strip()
    if not name:
        return error("Provide 'name' - the machine to delete, as cam_get(include=['machines']) "
                     "lists it.")
    if not confirm_name:
        return error("Provide 'confirm_name' - the machine's exact name again, as a safety "
                     "confirmation. Machine deletion is not undoable from this server.")

    lib, lerr = machine_library()
    if lerr:
        return error(lerr)

    # ONE resolve, through the SAME query cam_edit_setup assigns by: an ambiguous name is refused
    # with the resolver's own candidate list rather than resolved to whichever machine walked first.
    found, label, rerr = resolve_machine(name)
    if found is None:
        return error(f"{rerr} Nothing was deleted.")

    # Fails CLOSED on all three answers, not just 'fusion360': 'local or fusion360' is what
    # machine_location returns when the Local query ITSELF raised, so nothing read says this machine
    # is local - and an irreversible delete may not run on a location that was never established.
    location = machine_location(lib, found)
    if location != "local":
        return error(
            (f"The Local machine library query failed, so whether '{label}' is reached from the "
             "local or fusion360 library could not be read. "
             if location == "local or fusion360" else
             f"'{label}' is reached from the {location} machine library, not the Local one. ")
            + "This tool deletes from the LOCAL library only (the machines this Fusion install "
              "ships with are not yours to remove), so nothing was deleted.")

    # Case-SENSITIVE confirmation against the RESOLVED name, not the request: the caller confirms
    # the machine the resolver actually reached (only surrounding whitespace is forgiven).
    if (label or "").strip() != confirm_name:
        return error(f"Name mismatch - refusing to delete. '{name}' resolves to the machine "
                     f"'{label}', but confirm_name was '{confirm_name}'. Pass "
                     f"confirm_name='{label}' if you really mean this machine.")

    assets, truncated = _local_assets(lib)
    if assets is None:
        return error("Could not resolve the Local machine library location, so the machine's asset "
                     "cannot be addressed. Nothing was deleted.")
    # The ONE file-name set for the pre-delete search AND the post-delete read-back: an asset's leaf
    # name can equal the machine's label or the name it was reached by, which differ.
    wanted_names = {name.lower(), (label or "").lower()}
    hits, matched_by = _addressed(lib, assets, label, wanted_names)
    if not hits:
        listing = named_with_remainder(sorted(asset_leaf(a) for a in assets), cap=_ASSET_NAMES_CAP)
        # The one actionable near-miss, named because the listing alone would read as 'the name is
        # simply absent': an asset FILED under it that holds something else, or nothing.
        return error(f"No asset in the Local machine library holds a machine named '{label}'."
                     + _misfiled_clause(lib, assets, wanted_names)
                     + f" Local asset file names: {listing or '(none)'}."
                     + (" The walk hit its own bound, so this list is incomplete."
                        if truncated else "")
                     + " Nothing was deleted.")
    if len(hits) > 1:
        return error(f"{len(hits)} assets in the Local machine library hold a machine named "
                     f"'{label}' "
                     f"({named_with_remainder([str(asset_key(a)) for a in hits], cap=_ASSET_NAMES_CAP)})"
                     f", and no ONE of them is filed as '{name}' either - refusing to guess which "
                     "to delete. Remove the duplicate in Fusion's machine library first.")
    # An INCOMPLETE walk cannot support the one-asset conclusion above: a second asset of the same
    # name beyond the walk's bound would have been refused, and this delete is irreversible - so it
    # fails CLOSED rather than firing on one of an unknown number.
    if truncated:
        return error(f"The Local machine library walk hit its own bound before it finished, so "
                     f"'{label}' cannot be shown to name only ONE asset - a duplicate past the "
                     "bound would not have been seen. Nothing was deleted.")

    # The asset was reached BY the label it loads back, so it needs no second load to confirm what
    # it holds - an asset that loads nothing, or another machine, never entered `hits`.
    url = hits[0]

    # MEASURED: machineLibrary.deleteAsset(url) returns True and a re-query no longer lists the
    # machine. A False is reported as the refusal it is, never as a false ok.
    try:
        did = lib.deleteAsset(url)
    except Exception as e:
        return error(f"Deleting '{label}' from the Local machine library failed: {e}.")
    if not did:
        return error(f"Fusion declined to delete '{label}' from the Local machine library "
                     "(deleteAsset returned false) - it is still there.")

    # The read-back is the library's own asset walk: the machine-library query is keyed on
    # (vendor, model) and does not reach a machine by a description that is not its model, so a
    # re-resolve answering nothing is no evidence of a delete.
    after, after_truncated = _local_assets(lib)
    if after is None or after_truncated:
        return error(f"deleteAsset returned true for '{label}', but the Local library "
                     + ("location no longer resolves" if after is None
                        else "asset walk hit its own bound before finishing")
                     + ", so the delete could not be read back and is UNCONFIRMED. Re-read with "
                       "cam_get(include=['machines']).")
    # Compared by asset KEY, not by re-loading every remaining machine: the url this call deleted is
    # the exact thing that must be gone, and a load-back read after an irreversible delete buys
    # nothing the key comparison does not already settle.
    if asset_key(url) in {asset_key(a) for a in after}:
        return error(f"deleteAsset returned true but the Local machine library still lists "
                     f"'{asset_leaf(url)}' - the delete did not take. Re-read with "
                     "cam_get(include=['machines']).")
    # The second leg, and only where it ANSWERS: re-resolve through the NAME the resolve at the top
    # of this handler reached `found` by, and compare IDS - the same machine still resolving is a
    # delete that did not take.
    again, again_label, _rerr = resolve_machine(name)
    found_id = safe(lambda: found.id)
    again_id = safe(lambda: again.id) if again is not None else None
    # MEASURED: Machine.id is the DESCRIPTION, so where both libraries hold the name the SHIPPED
    # copy reads the deleted local one's id exactly. The library the re-resolve reaches is what
    # separates them.
    again_location = machine_location(lib, again) if again is not None else None
    resolves_after = (again is not None and again_id == found_id
                      and again_location == "local")
    if resolves_after:
        if again_id is None:
            return error(f"deleteAsset returned true and the asset is gone, but '{name}' still "
                         "resolves to a LOCAL machine whose id cannot be read - neither can the "
                         "deleted machine's, so nothing here tells them apart and the delete is "
                         "UNCONFIRMED. Re-read with cam_get(include=['machines']).")
        return error(f"deleteAsset returned true and the asset is gone, but '{name}' still "
                     "resolves to the same LOCAL machine through the query cam_edit_setup assigns "
                     "by - the delete did not take.")
    note = (f"Machine deleted from the Local machine library: its asset '{asset_leaf(url)}', "
            f"addressed by {matched_by}, is gone "
            "from a re-walk of the library's own assets. That is the whole claim - no setup was "
            "read here, so this says nothing about a setup that already carries this machine; "
            "cam_get's default setups slice reads that. cam_create_machine builds a replacement.")
    if again is None:
        note += (f" A re-resolve of '{name}' now answers nothing, which on its own proves nothing - "
                 "MEASURED, the library query is keyed on vendor/model and does not reach a machine "
                 "by a description that is not its model - so the asset walk is what confirms this.")
    elif again_location != "local":
        note += (f" The name '{name}' now reaches the {again_location} library's '{again_label}' - "
                 "an assignment by that name gets that copy from here on.")
    else:
        note += (f" The name '{name}' now resolves to a DIFFERENT machine ('{again_label}') - an "
                 "assignment by that name reaches it from here on.")
    return ok({
        "deleted": True,
        "machine": label,
        "location": "local",
        "asset_name": asset_leaf(url),
        "matched_by": matched_by,
        "url": safe(lambda: url.toString()),
        "resolves_after_delete": resolves_after,
        "resolves_from": again_location,   # null = the name answers nothing now
        "local_assets_remaining": len(after),   # the walk that answered above, not a second read
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Delete a machine from the LOCAL machine library by name; 'confirm_name' must match the "
    "resolved name exactly. Counterpart to cam_create_machine."
)

tool = (
    Tool.create_simple(name="cam_delete_machine", description=TOOL_DESCRIPTION)
    .add_input_property("name", {"type": "string"})
    .add_input_property("confirm_name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_create_machine.py::TestDeleteMachineEffect"
                      "::test_an_asset_still_listed_after_a_true_delete_is_an_error",
        rung="value"))


def register_tool():
    register(item)
