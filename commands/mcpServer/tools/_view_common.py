# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Camera-orientation table for the standard named views, plus the shared capture mechanics."""

import base64
import os
import tempfile

import adsk.core

from . import _common
from . import _geom

MAP_BLURB = (
    "view_direction/look_direction/up_vector/is_ortho_face - a named view's vectors; "
    "apply_named_view/capture_png_b64 - orient and grab; "
    "standoff_distance/STANDOFF_FALLBACK_CM - the orient standoff; "
    "activate_workspace - activate and read back; "
    "DISPLAY_FOLDERS/all_display_components - non-body clutter; "
    "keep_visible/all_bodies/same_body/isolate_for_fit/restore_message/focus_box - framing one "
    "subject")

app = adsk.core.Application.get()


def user_interface():
    """The Fusion UI, raising when there is none - the ONE handle the workspace tools read."""
    ui = app.userInterface
    if not ui:
        raise RuntimeError("No user interface available.")
    return ui


def activate_workspace(ui, ws):
    """(verdict, active_label, basis) for activating workspace 'ws': verdict True when it reads back
    active, False when activate() declined or a read names another workspace, None when neither read
    answered; basis is the read the verdict came from - 'refused', 'flag' or 'ui'."""
    # activate() returning true is not proof the workspace changed, so Workspace.isActive is read
    # back and the UI's own active workspace answers where that flag will not. The call itself is a
    # MUTATION and raises: a caller restoring a workspace it switched away from catches it.
    if not ws.activate():
        return False, None, "refused"
    flag = _common.read_flag(lambda: ws.isActive)
    if flag is not None:
        return flag, None, "flag"
    active_id = _common.safe(lambda: ui.activeWorkspace.id)
    if active_id is None:
        return None, None, "ui"
    label = _common.safe(lambda: ui.activeWorkspace.name) or active_id
    return active_id == _common.safe(lambda: ws.id), label, "ui"


# Display category -> the Component FOLDER bulb controlling it. The folder bulb is separate from
# each entity's own bulb, so toggling it never disturbs per-entity state.
DISPLAY_FOLDERS = {
    "sketches": "isSketchFolderLightBulbOn",
    "construction": "isConstructionFolderLightBulbOn",
    "origins": "isOriginFolderLightBulbOn",
    "joints": "isJointsFolderLightBulbOn",
}


def all_display_components(design):
    """Every component ONCE (root + allComponents), keyed by _common.native_identity."""
    # entityToken is document-local: every document's root component carries the same one, so a
    # bare-token key merges the roots of distinct source documents. native_identity pairs the token
    # with the source-document urn; `or id(c)` over-counts an identity-less component, never merges.
    root = _common.safe(lambda: design.rootComponent)
    comps = ([root] if root is not None else []) + list(
        _common.safe(lambda: design.allComponents, []) or [])
    out, seen = [], set()
    for c in comps:
        key = _common.native_identity(c) or id(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def focus_box(entity):
    """(the box a framing is measured on, 'solids' or 'all_geometry') - the BODIES-ONLY extents of a
    subject that places bodies, else its whole box."""
    # An occurrence's plain boundingBox sweeps its sketches and construction geometry, so a part
    # beside a large sketch in one component frames the sketch. boundingBox2 is the bodies-only read;
    # a sketch carries none, and a component placing no body answers None through it.
    box = _geom.body_aabb(entity)
    if box is not None and callable(_common.safe(lambda: entity.boundingBox2)):
        return box, "solids"
    if box is None:
        box = _common.safe(lambda: entity.boundingBox)
    return box, "all_geometry"


def keep_visible(o_path, target_path):
    """True if an occurrence path IS the target path, an ANCESTOR of it, or a DESCENDANT of it."""
    # Nesting reads off fullPathName ('Frame:1+Pedestal:1', '+' per level). The API mints a fresh
    # occurrence proxy on each access, so Python `is` is never true across two walks.
    if not o_path or not target_path:
        return False
    return (o_path == target_path
            or target_path.startswith(o_path + "+")     # o is an ancestor of the target
            or o_path.startswith(target_path + "+"))     # o is a descendant of the target


def all_bodies(design):
    """Every solid/surface/mesh body in the design - the root component's, plus each occurrence's."""
    root = _common.safe(lambda: design.rootComponent)
    # The shared census, not root.allOccurrences: that property RAISES on a design holding an
    # unresolved external reference.
    holders = ([root] if root is not None else []) + list(_common.all_occurrences(design))
    out = []
    for h in holders:
        for attr in ("bRepBodies", "meshBodies"):
            out.extend(_common.iter_collection(
                _common.safe(lambda h=h, attr=attr: getattr(h, attr))))
    return out


_UNREAD = object()


def _instance_path(body):
    """The assembly path a body wrapper is reached through: its placing occurrence's fullPathName,
    '' for a native body in no context, or None when neither read answered."""
    occ = _common.safe(lambda: body.assemblyContext, _UNREAD)
    if occ is _UNREAD:
        return None
    if occ is None:
        return ""
    return _common.safe(lambda: occ.fullPathName)


def same_body(a, b):
    """Whether two body reads are the SAME body in the SAME instance - the API mints a fresh proxy
    per access, so `is` alone misses one reached through a second walk."""
    # native_identity, never a bare entityToken: that token is document-local, so two bodies out of
    # two x-refs read the same one. An unread identity answers False rather than matching another
    # unread one, which keeps a body VISIBLE rather than hiding the subject by mistake.
    if a is b:
        return True
    ia = _common.native_identity(a)
    if ia is None or ia != _common.native_identity(b):
        return False
    # One native body placed twice hands BOTH proxies that one identity, so the identity alone
    # answers True for every instance. Two PLACEMENT paths separate them; a native body reads no
    # path at all and is the same body as its own proxy, so only two read paths split the match.
    pa, pb = _instance_path(a), _instance_path(b)
    return not (pa and pb) or pa == pb


def _relight(entities):
    """Turn each bulb back on and answer the names whose read-back did NOT come back True - the one
    restore both isolate_for_fit's early exit and its restore() report their stuck bulbs from."""
    stuck = []
    for o in entities:
        _common.safe(lambda o=o: setattr(o, "isLightBulbOn", True))
        if _common.safe(lambda o=o: o.isLightBulbOn) is not True:
            stuck.append(_common.safe(lambda o=o: o.fullPathName)
                         or _common.safe(lambda o=o: o.name) or "?")
    return stuck


def isolate_for_fit(name, ref):
    """Hide everything outside the named subject so a fit frames it; 'ref' is the caller's
    TargetRef, resolving an occurrence (its own subtree stays lit) or a single BODY. Returns
    (restore, target, error) - restore() answers the names whose bulb it could not put back."""
    design = _common.design()
    root = _common.safe(lambda: design.rootComponent) if design else None
    if not root:
        return None, None, f"{ref.name}: no active design to resolve '{_common.short_ref(name)}' against."
    resolved, err = ref.resolve(name)
    if resolved is None:
        # The kind quotes the caller's whole value back, so the echo is substituted into its message.
        return None, None, err.replace(name, _common.short_ref(name)) if err and name else err
    target, kind = resolved
    prev = []
    if kind in ("body", "mesh"):
        # A single-body root design places no occurrence at all, so the subject is the BODY and the
        # other BODIES' bulbs are what a fit has to clear.
        was_lit = _common.read_flag(lambda: target.isLightBulbOn)
        for b in all_bodies(design):
            if same_body(b, target) or not _common.safe(lambda b=b: b.isLightBulbOn):
                continue
            prev.append(b)
            _common.safe(lambda b=b: setattr(b, "isLightBulbOn", False))
        # The subject's OWN bulb, re-read: this walk never writes it, so a bulb that read on before
        # and off after went off with another placement's. Capturing now would return a picture of
        # nothing as a success. A bulb that did not read either time is no evidence and stands.
        if was_lit is True and _common.read_flag(lambda: target.isLightBulbOn) is False:
            # WHY it went dark, read rather than assumed: another body this walk hid is the SAME
            # physical body in a second placement. Without that reading the message states only
            # what it saw - a bulb this call never wrote going off.
            ident = _common.native_identity(target)
            shared = ident is not None and any(
                _common.native_identity(b) == ident for b in prev)
            stuck = _relight(prev)
            msg = (f"{ref.name}: hiding the other bodies turned the SUBJECT's own bulb off, which "
                   f"this call never wrote"
                   + (f" - another placement of the same body was hidden, so '{_common.short_ref(name)}' "
                      "shares one bulb with it and cannot be lit alone." if shared else ".")
                   + " Frame the occurrence by its fullPathName instead. Nothing was captured.")
            if stuck:
                msg += (f" {len(stuck)} bulb(s) did NOT come back on: "
                        f"{', '.join(str(s) for s in stuck[:5])} - the document is left with those "
                        "hidden; view_set(action='show', target=...) restores them.")
            return None, None, msg
    else:
        target_path = _common.safe(lambda: target.fullPathName)
        for o in _common.all_occurrences(design):
            if keep_visible(_common.safe(lambda o=o: o.fullPathName), target_path):
                continue
            was = _common.safe(lambda o=o: o.isLightBulbOn)
            if was:
                prev.append(o)
                _common.safe(lambda o=o: setattr(o, "isLightBulbOn", False))

    # Viewport.fit() frames every VISIBLE entity, so construction geometry outside the isolated
    # subject still blows the frame open; the folder bulbs switch that clutter off design-wide.
    folder_prev = []                       # (component, attr) - only bulbs we moved
    for comp in all_display_components(design):
        for attr in DISPLAY_FOLDERS.values():
            if _common.read_flag(lambda comp=comp, attr=attr: getattr(comp, attr)):
                folder_prev.append((comp, attr))
                _common.safe(lambda comp=comp, attr=attr: setattr(comp, attr, False))

    def restore():
        stuck = _relight(prev)
        for comp, attr in folder_prev:
            _common.safe(lambda comp=comp, attr=attr: setattr(comp, attr, True))
            if _common.read_flag(lambda comp=comp, attr=attr: getattr(comp, attr)) is not True:
                stuck.append(f"{_common.safe(lambda comp=comp: comp.name) or '?'}:{attr}")
        return stuck
    # What the isolate actually darkened, so the restore's message names bodies on a body subject
    # and occurrences on an occurrence one rather than one word for both.
    restore.hidden = "bodies" if kind in ("body", "mesh") else "occurrences"
    return restore, target, None


def restore_message(restore, label, purpose):
    """Run an isolate_for_fit restore and return the sentence naming what it could NOT put back, or
    None when everything came back."""
    if not restore:
        return None
    try:
        stuck = restore() or []
    except Exception as e:
        stuck = [f"the restore raised: {e}"]
    if not stuck:
        return None
    hidden = getattr(restore, "hidden", "occurrences")
    return (f"{label} hid the other {hidden} {purpose} and could NOT turn "
            f"{len(stuck)} of them back on: {', '.join(str(s) for s in stuck[:5])}. "
            f"The document is left with those {hidden} hidden - view_set(action='show', "
            "target=...) restores them.")

# eye - target direction per named view, not pre-normalized; view_direction() normalizes on read.
# Fusion is Z-up: a positive z aims the camera down at the TOP face, so an iso-bottom-* entry
# carries a NEGATIVE z and mirrors its iso-top-* twin across z.
VIEW_DIRECTIONS = {
    "front": (0, -1, 0),
    "back": (0, 1, 0),
    "top": (0, 0, 1),
    "bottom": (0, 0, -1),
    "right": (1, 0, 0),
    "left": (-1, 0, 0),
    "iso-top-right": (1, -1, 1),
    "iso-top-left": (-1, -1, 1),
    "iso-bottom-right": (1, -1, -1),
    "iso-bottom-left": (-1, -1, -1),
}

# Up vector per named view - the SAME for view_direction and look_direction.
UP_VECTORS = {
    "front": (0, 0, 1), "back": (0, 0, 1),
    "top": (0, 1, 0), "bottom": (0, 1, 0),
    "right": (0, 0, 1), "left": (0, 0, 1),
    "iso-top-right": (0, 0, 1), "iso-top-left": (0, 0, 1),
    "iso-bottom-right": (0, 0, 1), "iso-bottom-left": (0, 0, 1),
}

# The 6 true orthographic faces - these force an orthographic camera; the iso corners keep
# whatever camera type is already active.
ORTHO_FACE_VIEWS = {"front", "back", "top", "bottom", "right", "left"}


def _normalize(vec):
    x, y, z = vec
    ss = x * x + y * y + z * z
    inv = ss ** -0.5 if ss else 1.0
    return (x * inv, y * inv, z * inv)


def view_direction(name):
    """Unit (eye - target) direction for a named view, or None if 'name' is not a known view."""
    v = VIEW_DIRECTIONS.get(name)
    return _normalize(v) if v is not None else None


def look_direction(name):
    """Unit (target - eye) direction - the negation of view_direction() - for a named view."""
    v = view_direction(name)
    if v is None:
        return None
    x, y, z = v
    return (-x, -y, -z)


def up_vector(name):
    """Up vector for a named view, or None if 'name' is not a known view."""
    return UP_VECTORS.get(name)


def is_ortho_face(name):
    """True if 'name' is one of the 6 true orthographic faces (front/back/top/bottom/left/right)."""
    return name in ORTHO_FACE_VIEWS


# The eye-target standoff used when the camera's own distance is UNUSABLE - it did not read, or it
# read non-positive, which rebuilds the eye ON the target and leaves the view no direction at all.
STANDOFF_FALLBACK_CM = 100.0


def standoff_distance(cam):
    """(the eye-target distance to rebuild the eye at, the fallback it stands in for or None) - the
    camera's own distance, else STANDOFF_FALLBACK_CM. The ONE standoff read every orient shares."""
    dist = _common.safe(lambda: cam.eye.distanceTo(cam.target))
    if dist is None or dist <= 0:
        return STANDOFF_FALLBACK_CM, STANDOFF_FALLBACK_CM
    return dist, None


def apply_named_view(vp, name):
    """Point the viewport's camera at a named view and fit; no-op for an unknown/'current' name.
    Returns STANDOFF_FALLBACK_CM when the camera's eye-target distance did not read or read
    non-positive, else None."""
    # Exact eye/up vectors, not a viewOrientation assignment: that leaves a tilt off world axes.
    look = look_direction(name)
    if look is None:
        return None
    up = up_vector(name)
    cam = vp.camera
    tgt = cam.target
    dist, fallback = standoff_distance(cam)
    cam.eye = adsk.core.Point3D.create(
        tgt.x - look[0] * dist, tgt.y - look[1] * dist, tgt.z - look[2] * dist)
    cam.upVector = adsk.core.Vector3D.create(*up)
    if is_ortho_face(name):
        cam.cameraType = adsk.core.CameraTypes.OrthographicCameraType
    vp.camera = cam                   # assigning back applies the change
    vp.fit()
    return fallback


def _write_image(vp, path, width, height, transparent_background, anti_aliased):
    """Render the viewport to 'path' at width x height; returns (did, name of the overload used)."""
    # saveAsImageFileWithOptions is the only overload carrying isBackgroundTransparent/isAntiAliased.
    if transparent_background is None and anti_aliased is None:
        return bool(vp.saveAsImageFile(path, width, height)), "saveAsImageFile"
    opts = adsk.core.SaveImageFileOptions.create(path)
    # A fresh options object starts at width/height 0, so the size is assigned explicitly.
    opts.width = width
    opts.height = height
    if transparent_background is not None:
        opts.isBackgroundTransparent = bool(transparent_background)
    if anti_aliased is not None:
        opts.isAntiAliased = bool(anti_aliased)
    return bool(vp.saveAsImageFileWithOptions(opts)), "saveAsImageFileWithOptions"


def capture_png_b64(vp, width, height, prefix="fe_mcp_shot", transparent_background=None,
                    anti_aliased=None):
    """Grab the viewport as a base64 PNG via a temp file (always removed); returns (b64, error)."""
    # The refresh below is required: a capture can otherwise race an un-refreshed frame and a
    # camera or visibility change that has not drawn yet reads as blank.
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".png")
        os.close(fd)
        _common.safe(lambda: vp.refresh())
        did, api = _write_image(vp, temp_path, width, height, transparent_background, anti_aliased)
        if not did or not os.path.exists(temp_path):
            return None, f"Viewport capture failed ({api} returned false)."
        with open(temp_path, "rb") as f:
            raw = f.read()
        # mkstemp already created the file, so existence proves nothing about the render.
        if not raw:
            return None, f"Viewport capture failed ({api} wrote a 0-byte file)."
        return base64.b64encode(raw).decode("ascii"), None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
