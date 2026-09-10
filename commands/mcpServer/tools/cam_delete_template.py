# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Delete ONE template from the LOCAL toolpath template library, guarded and irreversible."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import named_with_remainder, ok, error, safe
# The shared CAM substrate: the ONE leafName-or-stem asset matcher every library DELETE addresses
# its target with - the same reads cam_delete_machine resolves on.
from ._cam_common import asset_key, asset_leaf, assets_named, library_assets
from ._cam_templates import _find_template_by_name, _location_enum, _template_library

# How many local asset names a refusal spells out before named_with_remainder counts the rest.
_ASSET_NAMES_CAP = 12


def _local_template_assets(lib):
    """(assets, truncated) under the LOCAL template library root, or (None, None) when that root
    does not resolve. Folders are recursed by the ONE bounded library walk every CAM library read
    uses. A build carrying no LocalLibraryLocation member is refused earlier, by the by-name search
    that resolves the same root."""
    root = safe(lambda: lib.urlByLocation(_location_enum("local")))
    if root is None:
        return None, None
    return library_assets(lib, root)


def handler(name: str = "", confirm_name: str = "") -> dict:
    """Delete one template from the LOCAL template library; see TOOL_DESCRIPTION for the
    confirm_name gate."""
    name = (name or "").strip()
    confirm_name = (confirm_name or "").strip()
    if not name:
        return error("Provide 'name' - the template to delete, as "
                     "cam_get(include=['templates'], template_location='local') lists it.")
    if not confirm_name:
        return error("Provide 'confirm_name' - the template's exact name again, as a safety "
                     "confirmation. Template deletion is not undoable from this server.")

    lib, err = _template_library()
    if err:
        return error(err)

    # ONE resolve, LOCAL-only, through the same by-name search cam_apply_template runs: a name
    # several templates answer to is REFUSED rather than resolved to the first folder walked.
    template, where = _find_template_by_name(lib, "local", name)
    if template is None:
        # An ambiguity hint is already a complete message - it WAS found, in more than one place.
        if where and "ambiguous" in where:
            return error(where + " Nothing was deleted.")
        return error(f"No template named '{name}' in the LOCAL template library - this tool deletes "
                     f"from the Local library only. {where or ''} Nothing was deleted.")
    label = (safe(lambda: template.name) or "").strip()

    # Case-SENSITIVE confirmation against the RESOLVED name, not the request: the caller confirms
    # the template the search actually reached (only surrounding whitespace is forgiven).
    if label != confirm_name:
        return error(f"Name mismatch - refusing to delete. '{name}' resolves to the template "
                     f"'{label}', but confirm_name was '{confirm_name}'. Pass "
                     f"confirm_name='{label}' if you really mean this template.")

    assets, truncated = _local_template_assets(lib)
    if assets is None:
        return error("Could not resolve the Local template library location, so the template's "
                     "asset cannot be addressed. Nothing was deleted.")
    # The ONE match set for the pre-delete search AND the post-delete read-back: two reads searched
    # with different sets is how a delete reports "gone" against a set it never covered.
    wanted_names = {label.lower()}
    hits = assets_named(assets, wanted_names)
    if not hits:
        listing = named_with_remainder(sorted(asset_leaf(a) for a in assets), cap=_ASSET_NAMES_CAP)
        return error(f"No asset in the Local template library is named '{label}'"
                     + f". Local assets: {listing or '(none)'}."
                     + (" The walk hit its own bound, so this list is incomplete."
                        if truncated else "")
                     + " Nothing was deleted.")
    if len(hits) > 1:
        return error(f"'{label}' names {len(hits)} assets in the Local template library "
                     f"({named_with_remainder([str(asset_key(a)) for a in hits], cap=_ASSET_NAMES_CAP)})"
                     " - refusing to guess which one to delete. Remove the duplicate in Fusion's "
                     "template library first.")
    # An INCOMPLETE walk cannot support the one-asset conclusion above: a second asset of the same
    # name beyond the walk's bound would have been refused, and this delete is irreversible - so it
    # fails CLOSED rather than firing on one of an unknown number.
    if truncated:
        return error(f"The Local template library walk hit its own bound before it finished, so "
                     f"'{label}' cannot be shown to name only ONE asset - a duplicate past the "
                     "bound would not have been seen. Nothing was deleted.")

    url = hits[0]
    # The asset is only deletable as the template the caller confirmed: load it back and compare the
    # name, so an asset whose FILE name matches while it holds another template is refused.
    at_url = safe(lambda: lib.templateAtURL(url))
    if at_url is None:
        return error(f"The Local library asset '{asset_leaf(url)}' does not load a template, so "
                     "what it holds cannot be confirmed. Nothing was deleted.")
    at_name = (safe(lambda: at_url.name) or "").strip()
    if at_name.lower() != label.lower():
        return error(f"The Local library asset '{asset_leaf(url)}' holds the template '{at_name}', "
                     f"not '{label}' - refusing to delete an asset that is not the template that "
                     "was confirmed.")

    # deleteAsset addresses a stored asset by url; importTemplate stores a template under a leafName
    # whose STEM is the template's name. A False is reported as the refusal it is, and the
    # read-backs below are what the claim is made from.
    try:
        did = lib.deleteAsset(url)
    except Exception as e:
        return error(f"Deleting '{label}' from the Local template library failed: {e}.")
    if not did:
        return error(f"Fusion declined to delete '{label}' from the Local template library "
                     "(deleteAsset returned false) - it is still there.")

    # The LOAD-BEARING read-back is the library's own asset walk - the read this delete was
    # addressed through. A walk that could not answer, or did not finish, leaves the delete
    # UNCONFIRMED rather than letting an empty match pass for proof.
    after, after_truncated = _local_template_assets(lib)
    if after is None or after_truncated:
        return error(f"deleteAsset returned true for '{label}', but the Local template library "
                     + ("location no longer resolves" if after is None
                        else "asset walk hit its own bound before finishing")
                     + ", so the delete could not be read back and is UNCONFIRMED. Re-read with "
                       "cam_get(include=['templates'], template_location='local').")
    still_listed = [asset_leaf(a) for a in assets_named(after, wanted_names)]
    if still_listed:
        return error(f"deleteAsset returned true but the Local template library still lists "
                     f"'{still_listed[0]}' - the delete did not take. Re-read with "
                     "cam_get(include=['templates'], template_location='local').")
    # The second leg, on the address the delete was aimed at. The read MUST be safe()-wrapped:
    # templateAtURL RAISES '3 : Given URL does not point to a template' on a url whose asset was
    # just deleted, rather than returning null as its documentation says.
    loads_after = safe(lambda: lib.templateAtURL(url)) is not None
    if loads_after:
        return error(f"deleteAsset returned true and '{label}' is gone from the Local template "
                     "library's asset walk, but a template still loads from its url "
                     f"({asset_key(url)}) - the two reads disagree, so the delete is UNCONFIRMED.")
    return ok({
        "deleted": True,
        "template": label,
        "location": "local",
        "asset_name": asset_leaf(url),
        "url": safe(lambda: url.toString()),
        "loads_after_delete": loads_after,
        "local_assets_remaining": len(after),   # the walk that answered above, not a second read
        "note": (f"Template deleted from the Local template library: its asset '{asset_leaf(url)}' "
                 "is gone from a re-walk of the library's own assets, and nothing loads from its "
                 "url any more. cam_save_template writes a new one; "
                 "cam_get(include=['templates'], template_location='local') lists what is left."),
    })


TOOL_DESCRIPTION = (
    "Delete a template from the LOCAL toolpath template library by name; 'confirm_name' must match "
    "the resolved name exactly. Counterpart to cam_save_template."
)

tool = (
    Tool.create_simple(name="cam_delete_template", description=TOOL_DESCRIPTION)
    .add_input_property("name", {"type": "string"})
    .add_input_property("confirm_name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_delete_template.py::TestDeleteTemplateEffect"
                      "::test_an_asset_still_listed_after_a_true_delete_is_an_error",
        rung="value")
)


def register_tool():
    register(item)
