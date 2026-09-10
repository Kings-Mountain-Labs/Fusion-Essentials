# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: set the color/appearance of a body, occurrence, or component. WRITES."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _materials

# An appearance's see-through-ness is its Prism material class, not its Color alpha, so the color
# written here is always fully opaque; 'opacity' below is the separate browser Opacity Control.
_COLOR_ALPHA = 255

# The one appearance every color override is copied from, resolved by EXACT name.
_BASE_LIBRARY = "Fusion Appearance Library"
_BASE_SOURCE = "Paint - Enamel Glossy (White)"
_BASE_NAME = "MCP Neutral Base"

# The one ColorProperty id on that base carrying its color; its second one is not written.
_ALBEDO_IDS = ("opaque_albedo",)


def _parse_color(spec):
    """Parse '#RRGGBB' / 'RRGGBB' / 'r,g,b' -> (r, g, b) ints 0-255, or (None, msg)."""
    s = (spec or "").strip()
    if not s:
        return None, "Provide 'color' as '#RRGGBB', 'RRGGBB', or 'r,g,b' (0-255 each)."
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip() != ""]
        if len(parts) != 3:
            return None, f"'{spec}' is not 'r,g,b' (three 0-255 components)."
        try:
            rgb = tuple(int(p) for p in parts)
        except ValueError:
            return None, f"'{spec}' has non-integer components; use 'r,g,b' (0-255 each)."
    else:
        h = s[1:] if s.startswith("#") else s
        if len(h) != 6:
            return None, f"'{spec}' is not a 6-digit hex color (e.g. '#1E8E3E')."
        try:
            rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None, f"'{spec}' is not valid hex; use '#RRGGBB'."
    if any(c < 0 or c > 255 for c in rgb):
        return None, f"'{spec}' has a component outside 0-255."
    return rgb, None


# A MeshBody has no appearance override, so mesh is left out of the target universe.
_TARGET = _inputs.TargetRef("target", allow=("body", "face", "occurrence", "component", "design"))


def _base_appearance(appearances):
    """The Appearance every color override is copied FROM, as (appearance, reused, err) - resolved by
    EXACT name, copied into the document once under _BASE_NAME and looked up by it thereafter."""
    held = safe(lambda: appearances.itemByName(_BASE_NAME))
    if held is not None:
        return held, True, None
    lib, lerr = _materials.find_library(_BASE_LIBRARY)
    if lerr:
        return None, False, (f"'{_BASE_LIBRARY}' is where the color base '{_BASE_SOURCE}' is read "
                             f"from. {lerr}")
    src = safe(lambda: lib.appearances.itemByName(_BASE_SOURCE))
    if src is None:
        return None, False, (f"No appearance named '{_BASE_SOURCE}' in '{_BASE_LIBRARY}' - it is "
                             "the base every color override is copied from. List that library with "
                             "design_get(include=['appearances'], library='" + _BASE_LIBRARY + "').")
    copied = safe(lambda: appearances.addByCopy(src, _BASE_NAME))
    if copied is None:
        # a parallel call can land the name between the lookup and the copy - re-check once
        copied = safe(lambda: appearances.itemByName(_BASE_NAME))
        if copied is not None:
            return copied, True, None
        return None, False, (f"Could not copy '{_BASE_SOURCE}' into this document as "
                             f"'{_BASE_NAME}' - addByCopy declined and no appearance of that name "
                             "is in the document. A name already taken RAISES 'appearance name "
                             "already exists in document', so the name is not the obstacle.")
    return copied, False, None


def _channel_rgb(value):
    """(r, g, b) off a ColorProperty's value, or None when any component did not read - which is no
    evidence either way about the write."""
    got = []
    for ch in ("red", "green", "blue"):
        c = safe(lambda ch=ch: getattr(value, ch))
        if not isinstance(c, (int, float)) or isinstance(c, bool):
            return None
        got.append(int(c))
    return tuple(got)


def _write_albedo(appr, color):
    """Write `color` into the appearance's albedo channel(s) -> (written, seen, unchanged, unread):
    the ids whose read-back did not contradict the write, every ColorProperty id the appearance
    exposed, the ids that accepted the write and read a DIFFERENT color back, and the ids whose
    read-back gave no color at all - which refutes nothing and is disclosed, not refused."""
    want = _channel_rgb(color)
    written, seen, unchanged, unread = [], [], [], []
    for p in _common.iter_collection(safe(lambda: appr.appearanceProperties)):
        if safe(lambda p=p: type(p).__name__) != "ColorProperty":
            continue
        pid = safe(lambda p=p: p.id)
        seen.append(str(pid))
        if pid not in _ALBEDO_IDS:
            continue
        try:
            p.value = color
        except Exception:
            continue  # a read-only / texture-backed channel; try the next albedo one
        # A ColorProperty can accept the assignment and store nothing. The comparison needs BOTH
        # sides - the color written and the color read back - so either one unreadable is DISCLOSED
        # as unconfirmed, never refused; only a color that READ and differs is a refusal.
        back = _channel_rgb(safe(lambda p=p: p.value))
        if want is None or back is None:
            unread.append(str(pid))
        elif back != want:
            unchanged.append(str(pid))
            continue
        written.append(str(pid))
    return written, seen, unchanged, unread


def _make_colored_appearance(design, rgb, opacity, name):
    """An appearance named `name` at the requested color - one already in the design is REUSED
    (addByCopy refuses a duplicate name), else the base is copied in -> (appearance, reused,
    base_reused, unread_channels, err); base_reused is None when no base was consulted, and
    unread_channels names the albedo ids whose read-back gave no color."""
    appearances = safe(lambda: design.appearances)
    if appearances is None:
        return None, False, None, [], ("The design's appearances collection could not be read, so "
                                       "no color override could be made.")
    appr = safe(lambda: appearances.itemByName(name))
    reused = appr is not None
    base_reused = None
    if appr is None:
        base, base_reused, berr = _base_appearance(appearances)
        if berr:
            return None, False, None, [], berr
        appr = safe(lambda: appearances.addByCopy(base, name))
        if not appr:
            # a parallel call can land the name between the lookup and the copy - re-check once
            appr = safe(lambda: appearances.itemByName(name))
            reused = appr is not None
        if not appr:
            return None, False, None, [], (
                f"Could not create the appearance copy '{name}' - addByCopy declined and no "
                "appearance of that name is in the document. A name already taken RAISES "
                "'appearance name already exists in document', so the name is not the obstacle.")
    color = adsk.core.Color.create(rgb[0], rgb[1], rgb[2], opacity)
    written, seen, unchanged, unread = _write_albedo(appr, color)
    if not written:
        hint = (f" Pass a different 'name' to mint a fresh override from '{_BASE_NAME}'."
                if reused else "")
        if unchanged:
            return None, False, None, [], (
                f"'{name}' accepted the color on {', '.join(unchanged)} and reads a different one "
                f"back, so the color was not applied." + hint)
        return None, False, None, [], (
            f"'{name}' exposes no writable albedo color property, so the color was not applied "
            f"(looked for {', '.join(_ALBEDO_IDS)}; its color properties: "
            f"{', '.join(seen) or 'none'})." + hint)
    return appr, reused, base_reused, unread, None


def _reads_as(entity, appr_id, appr_name):
    """True / False / None - does `entity` now read the exact appearance this call applied?"""
    # Neither key alone is an instance: names repeat across the catalog, and a copy keeps its
    # source's id, so name pins the instance among same-id copies and id the asset among strangers.
    got_id = safe(lambda: entity.appearance.id)
    got_name = safe(lambda: entity.appearance.name)
    if got_id is None or got_name is None or appr_id is None or appr_name is None:
        return None
    return got_id == appr_id and got_name == appr_name


def _occurrence_fanout(occ, appr_id, appr_name):
    """(reached, not_reached, unverified) after an OCCURRENCE-level appearance write - each body is
    compared through _reads_as, and a comparison that cannot be made is 'unverified'."""
    # The occurrence's own .appearance reads back as the newly set one even for a body that kept its
    # own, so it is not proof the bodies changed.
    reached, not_reached, unverified = [], [], []
    for i, b in enumerate(_common.iter_collection(safe(lambda: occ.bRepBodies))):
        bname = safe(lambda b=b: b.name) or f"body #{i}"
        verdict = _reads_as(b, appr_id, appr_name)
        if verdict is None:
            unverified.append(bname)
        elif verdict:
            reached.append(bname)
        else:
            not_reached.append({"body": bname,
                                "appearance": safe(lambda b=b: b.appearance.name)})
    return reached, not_reached, unverified


def _opacity_note(kind, asked, seen):
    """What the caller has to know about where the override landed and what it renders as."""
    bits = []
    if kind == "occurrence":
        bits.append("An occurrence carries no opacity of its own, so this was set on its COMPONENT "
                    "- every instance of that component renders with it.")
    if seen is None:
        bits.append("The rendered opacity could not be read back, so it is UNCONFIRMED.")
        if kind == "body":
            # MEASURED: BRepBody.visibleOpacity RAISES on a native body before AND after the write.
            bits.append("A NATIVE body does not answer visibleOpacity - the rendered value reads "
                        "through its occurrence PROXY only, so target the occurrence to confirm it.")
    elif abs(seen - asked) > 1:
        bits.append(f"It RENDERS at {seen}%, not the {asked}% set here - the read-back did not "
                    "match what was written.")
    return " ".join(bits)


def _apply_opacity(entity, kind, percent):
    """Set the OPACITY OVERRIDE on the target -> (visible_percent_or_None, error_or_None). An
    occurrence carries no settable opacity, so it writes through its component."""
    want = percent / 100.0
    if kind == "face":
        return None, ("A FACE has no opacity of its own - the override lives on the body or the "
                      "component. Target the body (or the occurrence) instead.")
    holder = entity
    if kind == "occurrence":
        holder = safe(lambda: entity.component)
        if holder is None:
            return None, "Could not reach the component behind this occurrence to set its opacity."
    try:
        holder.opacity = want
    except Exception as e:
        return None, f"Could not set opacity: {e}"
    # visibleOpacity folds inheritance in; .opacity reads back the number just written regardless.
    reader = entity if kind != "component" else None
    seen = safe(lambda: reader.visibleOpacity) if reader is not None else None
    if seen is None:
        first = next(iter(_common.iter_collection(safe(lambda: holder.bRepBodies))), None)
        seen = safe(lambda: first.visibleOpacity) if first is not None else None
    return (None if seen is None else round(seen * 100)), None


def handler(target: str = "", color: str = "", opacity=None, name: str = "") -> dict:
    """Apply a solid-color appearance override and/or an opacity override to the target. WRITES."""
    want_opacity = opacity is not None and str(opacity).strip() != ""
    if want_opacity:
        try:
            opacity = int(opacity)
        except (TypeError, ValueError):
            return error("'opacity' must be a whole percent from 0 (invisible) to 100 (opaque).")
        if not 0 <= opacity <= 100:
            return error(f"'opacity'={opacity} is outside 0-100. It is a PERCENT - the browser's "
                         "Opacity Control - not a 0-255 color alpha.")
    rgb, cerr = (None, None) if not (color or "").strip() and want_opacity else _parse_color(color)
    if cerr:
        return error(cerr)

    design = _common.design()
    if not design:
        return error("No active design with geometry.")

    resolved, terr = _TARGET.resolve(target)
    if terr:
        return terr if isinstance(terr, dict) else error(terr)
    entity, kind = resolved
    if kind == "design":
        kind = "component"      # whole design = the root component; color all its bodies
    desc = (f"{kind} '{safe(lambda: entity.fullPathName) or safe(lambda: entity.name)}'"
            if safe(lambda: entity.name) else kind)

    # Every precondition runs BEFORE the appearance is minted; minting first leaves an orphan
    # appearance asset in the design on each refusal.
    bodies = None
    if kind == "component":
        bodies = safe(lambda: entity.bRepBodies)
        if (safe(lambda: bodies.count, 0) or 0) == 0:
            return error(f"{desc} has no bodies to color.")

    opacity_seen = None
    if want_opacity:
        opacity_seen, oerr = _apply_opacity(entity, kind, opacity)
        if oerr:
            return error(oerr)
    if rgb is None:
        return ok({
            "applied": True,
            "target": desc,
            "kind": kind,
            "opacity": opacity,
            "opacity_rendered": opacity_seen,
            "note": ("Opacity override applied - the browser's 'Opacity Control'. "
                     + _opacity_note(kind, opacity, opacity_seen)),
        })

    appr_name = (name or "").strip() or f"AgentColor_{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    appr, appr_reused, base_reused, unread_channels, aerr = _make_colored_appearance(
        design, rgb, _COLOR_ALPHA, appr_name)
    if aerr:
        return error(aerr)

    applied_to = []
    failed = []
    not_reached = []
    unverified = []
    # Read ONCE off the appearance applied, so a later re-read cannot drift the comparison.
    appr_id, appr_landed_name = safe(lambda: appr.id), safe(lambda: appr.name)
    if kind == "component":
        # a Component has no single .appearance; apply to each body and collect per-body failures
        for b in _common.iter_collection(bodies):
            name = safe(lambda b=b: b.name)
            try:
                b.appearance = appr
            except Exception as e:
                failed.append({"body": name, "error": str(e)})
                continue
            if _reads_as(b, appr_id, appr_landed_name) is False:
                got = safe(lambda b=b: b.appearance.name)
                failed.append({"body": name, "error": f"appearance still reads '{got}' after the set"})
            else:
                applied_to.append(name)
        if not applied_to:
            return error(f"Could not apply appearance to any body of {desc}: "
                         f"{failed[0]['error'] if failed else 'unknown error'}.")
    else:
        # body / occurrence / face all carry a settable .appearance
        try:
            entity.appearance = appr
        except Exception as e:
            return error(f"Could not apply appearance to {desc}: {e}")
        if _reads_as(entity, appr_id, appr_landed_name) is False:
            got = safe(lambda: entity.appearance.name)
            return error(f"Assignment was accepted but {desc} still reads appearance '{got}' - "
                         "the override did not take.")
        # a BRepFace has no .name; fall back to the target description
        applied_to.append(safe(lambda: entity.name) or desc)
        if kind == "occurrence":
            # The occurrence read-back agrees with the write even for a body it never reached, so
            # the BODIES are read back to publish where the color landed.
            reached, not_reached, unverified = _occurrence_fanout(entity, appr_id,
                                                                  appr_landed_name)
            applied_to.extend(reached)
            if not reached and not_reached:
                names = ", ".join(o["body"] for o in not_reached[:5])
                return error(f"Assignment to {desc} reached NONE of its {len(not_reached)} "
                             f"body(ies) - each still reads a different appearance ({names}). "
                             "Color the bodies directly (target = the body name).")

    note = ("Appearance override applied. Set a new color anytime; to revert, the override is on "
            "the body/occurrence (.appearance). Pair with view_screenshot to see it.")
    if base_reused is False:
        note = (f"Copied '{_BASE_SOURCE}' from '{_BASE_LIBRARY}' into this document as "
                f"'{_BASE_NAME}' - the base every color override here is copied from. " + note)
    if kind in ("body", "component"):
        note = ("This write landed on the BODY, which is the component's NATIVE body - the color "
                "shows on EVERY instance of that component, not just one. To color one instance, "
                "target the OCCURRENCE (its fullPathName). " + note)
    if failed:
        note = (f"Appearance applied to {len(applied_to)} of {len(applied_to) + len(failed)} bodies; "
                f"{len(failed)} failed - see 'failed'. " + note)
    if not_reached:
        note = (f"PARTIAL: {len(not_reached)} body(ies) of this occurrence do NOT carry the new "
                f"appearance ({', '.join(o['body'] for o in not_reached[:5])}) - each still reads the "
                "one named in 'bodies_not_reached'; color those directly (target = the body). " + note)
    if unverified:
        note = (f"{len(unverified)} body(ies) could not be compared, so the color is UNCONFIRMED "
                "there - see 'unverified_bodies'. " + note)
    if unread_channels:
        note = (f"The color was written to {', '.join(unread_channels)} but could not be compared "
                "with a read-back there, so the appearance's own color is UNCONFIRMED. " + note)

    result = {
        "applied": True,
        "target": desc,
        "kind": kind,
        "color_rgb": list(rgb),
        "color_hex": f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}",
        "opacity": opacity,
        "opacity_rendered": opacity_seen,
        "appearance": appr_landed_name,
        "appearance_reused": appr_reused,
        "applied_to": applied_to,
        "note": note,
    }
    if base_reused is not None:
        # Only a call that consulted the base can say what the appearance was copied from.
        result["base_appearance"] = _BASE_NAME
        result["base_reused"] = base_reused
    if failed:
        result["failed"] = failed
    if not_reached:
        result["bodies_not_reached"] = not_reached
    if unverified:
        result["unverified_bodies"] = unverified
    if unread_channels:
        result["color_unconfirmed_channels"] = unread_channels
    return ok(result)


_DESC = (
"Set the color and/or opacity of a face, body, occurrence or component as a revertible override."
)

tool = (
    Tool.create_simple(name="appearance_set", description=_DESC)
    .add_input_property(*_TARGET.as_property())
    .add_input_property("color", {"type": "string",
            "description": "'#RRGGBB', 'RRGGBB', or 'r,g,b' (0-255 each)."})
    .add_input_property("opacity", {"type": "integer",
            "description": "A percent: 0 invisible, 100 opaque."})
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # Every assignment arm re-reads through the two-key _reads_as compare and errors on a mismatch.
    # The albedo write re-reads its channel too; opacity discloses what renders instead of gating.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_appearance_set.py::TestApply"
                      "::test_a_body_left_holding_a_same_base_copy_is_not_a_success"))


def register_tool():
    register(item)
