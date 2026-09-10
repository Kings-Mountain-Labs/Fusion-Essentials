# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The CAM toolpath TEMPLATE library substrate: the library handle, the bounded folder listing
cam_get(include=['templates']) returns, and the by-name search every template tool resolves through."""

import adsk.core
import adsk.cam

from ._common import ok, error, safe
# The shared CAM substrate: the ONE bounded library folder walk (and its collect-the-assets
# projection), plus the ONE leafName-or-stem asset matcher every library DELETE addresses its
# target with - the same reads cam_delete_machine resolves on.
from ._cam_common import asset_leaf_keys, library_children, walk_library_folders
from . import _inputs

app = adsk.core.Application.get()

MAP_BLURB = (
    "the CAM TEMPLATE library substrate: _template_library - the handle off the CAMManager "
    "singleton, not the CAM product; list_cam_templates_handler - the bounded folder tree "
    "cam_get(include=['templates']) returns, each row carrying the ASSET url cam_apply_template "
    "takes; _find_template_by_name - the by-name search REFUSING a name several templates answer "
    "to; _LOCATION - the location selector")

# Friendly location name -> the LibraryLocations enum MEMBER name. The member is resolved via getattr
# on the enum (the enum owns the value), never a hand-coded int - matching cam_edit_setup /
# cam_edit_tools / cam_post.
_LOCATION_MEMBERS = {
    "local": "LocalLibraryLocation",
    "cloud": "CloudLibraryLocation",
    "network": "NetworkLibraryLocation",
    "samples": "OnlineSamplesLibraryLocation",
    "external": "ExternalLibraryLocation",
    "fusion": "Fusion360LibraryLocation",
    "hub": "HubLibraryLocation",
}


def _location_enum(key):
    """The LibraryLocations enum member for a friendly location key, or None when the key is unknown
    OR this Fusion build has no such member (getattr on the enum member, not a hand-coded int)."""
    member = _LOCATION_MEMBERS.get((key or "").lower())
    if not member:
        return None
    return getattr(adsk.cam.LibraryLocations, member, None)


# The wire-validated selector for a library location: its enum carries the legal names, so an unknown
# location fails at the schema instead of the handler re-listing them (see honesty contract).
_LOCATION = _inputs.Choice("location", options=list(_LOCATION_MEMBERS), default="cloud",
                           description="Which template library to read/write.")

_MAX_NODES = 1500

_TREE_NOTE = (
    "A row's 'url' is what cam_apply_template(template_url=...) takes. url_basis "
    "'folder_position' means the asset at that template's INDEX in its folder - how the shipped "
    "hole templates are addressed, their leafNames spelling other than their names; alignment "
    "measured on 2705.1.4 (measure_api cam-template-asset-index-alignment). 'templates_collided' "
    "counts arrivals dropped as already listed.")


def _template_library():
    """(templateLibrary, None) or (None, reason) - the library manager lives on the CAMManager
    singleton, not on the CAM product."""
    try:
        mgr = adsk.cam.CAMManager.get()
    except Exception as e:
        return None, f"Could not access CAMManager: {e}"
    if not mgr:
        return None, "CAMManager not available."
    try:
        lib = mgr.libraryManager.templateLibrary
    except Exception as e:
        return None, f"Could not access the template library: {e}"
    if not lib:
        return None, "Template library not available."
    return lib, None


# ---------------------------------------------------------------------------
# template listing engine -> cam_get(include=['templates'])
# ---------------------------------------------------------------------------

def list_cam_templates_handler(location: str = "cloud", url: str = "", max_depth: int = 4) -> dict:
    """Navigate the template library. Start at a location root (or a folder 'url')."""
    lib, err = _template_library()
    if err:
        return error(err)

    # Resolve the starting URL: explicit url wins, else the named location's root.
    start_url = None
    if url.strip():
        start_url = safe(lambda: adsk.core.URL.create(url.strip()))
        if not start_url:
            return error(f"Invalid library URL: '{url}'.")
    else:
        loc_key, lerr = _LOCATION.resolve(location)
        if lerr:
            return error(lerr)
        loc = _location_enum(loc_key)
        if loc is None:
            return error(f"Location '{loc_key}' is not available in this Fusion build.")
        start_url = safe(lambda: lib.urlByLocation(loc))
        if not start_url:
            return error(f"Could not resolve the '{location}' library root "
    "(it may not be configured/available).")

    try:
        depth = max(1, min(int(max_depth), 8))
    except Exception:
        depth = 4

    counter = {"n": 0, "truncated": False}
    try:
        tree = _walk_library(lib, start_url, 0, depth, counter)
    except Exception as e:
        return error(f"Could not read the template library: {e}")

    return ok({
    "location": location if not url.strip() else None,
    "root_url": safe(lambda: start_url.toString()),
    "node_count": counter["n"],
    "truncated": counter["truncated"],
    "tree": tree,
    "note": _TREE_NOTE,
    })


def _asset_index(lib, folder_url):
    """(urls, by_name) for ONE folder's child assets: the url strings in library order, and every
    url each lowercased name answers to - asset_leaf_keys is the shared leafName/stem reading."""
    assets = library_children(lib, folder_url, "childAssetURLs")
    urls = []
    by_name = {}
    for u in assets:
        text = safe(lambda u=u: u.toString())
        if not text:
            continue
        urls.append(text)
        for key in asset_leaf_keys(u):
            by_name.setdefault(key, []).append(text)
    return urls, by_name


def _folder_templates(lib, folder_url, by_name):
    """(rows, collided) for one folder - [(name, template, url_or_None)] keyed on the ASSET a name
    resolves to, so two arrivals of ONE asset become one row (counted in `collided`) while a name
    ONE asset does not answer keeps every arrival."""
    rows, seen, collided = [], set(), 0
    for t in library_children(lib, folder_url, "childTemplates"):
        name = safe(lambda t=t: t.name)
        urls = by_name.get((name or "").lower()) or []
        # One asset answering the name IS this row's identity. Several (or none) leave the arrivals
        # unidentified, and two unidentified arrivals are not shown to be one template.
        url = urls[0] if len(urls) == 1 else None
        if url is not None and url in seen:
            collided += 1
            continue
        if url is not None:
            seen.add(url)
        rows.append((name, t, url))
    return rows, collided


def _walk_library(lib, folder_url, depth, max_depth, counter):
    """Recursively summarize a library folder: its templates + subfolders."""
    node = {
    "folder": safe(lambda: lib.displayName(folder_url)),
    "url": safe(lambda: folder_url.toString()),
    "templates": [],
    "folders": [],
    }

    # A CAMTemplate carries no url of its own, so the folder's child ASSET urls are the only address
    # cam_apply_template(template_url=...) can be given - carried here beside each template.
    asset_urls, by_name = _asset_index(lib, folder_url)
    templates, collided = _folder_templates(lib, folder_url, by_name)
    # Position pairs a template with its asset where NO name in the folder resolved one and the two
    # lists are the same length (measure_api row cam-template-asset-index-alignment).
    by_position = len(templates) == len(asset_urls) and not any(u for _n, _t, u in templates)
    for i, (tname, t, url) in enumerate(templates):
        if counter["n"] >= _MAX_NODES:
            counter["truncated"] = True
            break
        counter["n"] += 1
        positioned = by_position and i < len(asset_urls)
        row = {
        "name": tname,
        "description": safe(lambda t=t: t.description),
        "is_valid": safe(lambda t=t: t.isValidTemplate),
        "is_hole_template": safe(lambda t=t: t.isHoleTemplate),
        "url": asset_urls[i] if positioned else url,
        }
        if positioned:
            row["url_basis"] = "folder_position"
        node["templates"].append(row)
    if collided:
        # Arrivals that resolved to an asset already listed here - dropped as the same template.
        node["templates_collided"] = collided

    # Subfolders.
    if depth + 1 < max_depth:
        try:
            for sub in (lib.childFolderURLs(folder_url) or []):
                if counter["n"] >= _MAX_NODES:
                    counter["truncated"] = True
                    break
                counter["n"] += 1
                node["folders"].append(_walk_library(lib, sub, depth + 1, max_depth, counter))
        except Exception:
            pass
    else:
        try:
            if lib.childFolderURLs(folder_url):
                node["folders_truncated"] = True
        except Exception:
            pass

    return node


def _find_template_by_name(lib, location, name):
    """Search a library location (recursively) for a template by name. Returns (template, hint). A
    name that matches in MORE THAN ONE folder is REFUSED (None + an 'ambiguous' hint naming the
    folders) rather than first-DFS-matched - template_url is the precise escape (the resolver idiom
    _cam_common uses for CAM tree names)."""
    if (location or "cloud").lower() not in _LOCATION_MEMBERS:
        return None, f"Unknown location '{location}'."
    loc = _location_enum(location or "cloud")
    if loc is None:
        return None, f"Location '{location}' is not available in this Fusion build."
    root = safe(lambda: lib.urlByLocation(loc))
    if not root:
        return None, f"Could not resolve the '{location}' library root."

    want = name.lower()
    seen_names = []
    matches = []          # (template, containing-folder display name)

    # The shared bounded folder walk; the LEAF op (read this folder's templates, record every name,
    # keep the matches) is this search's own.
    def visit(folder_url):
        # _folder_templates collapses two arrivals of ONE asset, so a listing that hands the same
        # template back twice resolves; a name TWO assets in the folder answer stays two rows here
        # and is refused below.
        _urls, by_name = _asset_index(lib, folder_url)
        for tn, t, _url in _folder_templates(lib, folder_url, by_name)[0]:
            if tn:
                seen_names.append(tn)
            if tn and tn.lower() == want:
                matches.append((t, safe(lambda folder_url=folder_url:
                                        lib.displayName(folder_url)) or "?"))
        return False      # every folder is searched: a duplicate name must be REFUSED, not raced

    walk_library_folders(lib, root, visit, max_folders=_MAX_NODES)
    if len(matches) == 1:
        return matches[0][0], None
    if len(matches) > 1:
        folders = ", ".join(f for _, f in matches)
        return None, (f"'{name}' is ambiguous - {len(matches)} templates share that name (in: "
                      f"{folders}). Pass the precise template_url instead.")
    hint = f"Templates seen: {', '.join(seen_names[:25]) or '(none)'}."
    return None, hint
