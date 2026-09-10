# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Search and page the installed Fusion API declarations without invoking their operations."""

import importlib
import inspect
import itertools
import re

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe


_API_MODULES = ("adsk.core", "adsk.fusion", "adsk.cam", "adsk.drawing",
                "adsk.sim", "adsk.electron", "adsk.volume")
_MAX_RESULTS = 40
_DOC_CHARS = 1200


def _load_modules(namespace_filter, unavailable=None):
    """Return importable API modules in scope and record failed imports when requested."""
    mod_parts = []
    for part in (namespace_filter or "").split("."):
        if part[:1].isupper():
            break
        mod_parts.append(part)
    want = ".".join(mod_parts)
    out = []
    for name in _API_MODULES:
        if want and not (name == want or name.startswith(want + ".") or want.startswith(name + ".")):
            continue
        try:
            out.append((name, importlib.import_module(name)))
        except Exception as exc:
            if unavailable is not None:
                unavailable.append({"namespace": name, "error": str(exc)[:200],
                                    "exception": type(exc).__name__})
    return out


def _class_filter_from(namespace_filter):
    """Return the exact class component of a namespace/class filter."""
    for part in (namespace_filter or "").split("."):
        if part[:1].isupper():
            return part
    return None


def _trim(doc, offset=0):
    """Return a bounded excerpt from a docstring."""
    text = (doc or "").strip()[offset:]
    return text[:_DOC_CHARS].rstrip() + " ..." if len(text) > _DOC_CHARS else text


def _signature(member):
    """Return an introspected signature, or None when unavailable."""
    return safe(lambda: str(inspect.signature(member)))


def _doc_fields(doc, offset, parent_doc=""):
    """Return docstring paging fields and explicit status markers from its documentation."""
    text = (doc or "").strip()
    status_text = (parent_doc + " " + text).lower()
    markers = []
    for marker, pattern in (("preview", r"preview (?:feature|state)|in preview"),
                            ("retired", r"\bretired\b|\bdeprecated\b"),
                            ("unsupported", r"not officially supported")):
        if re.search(pattern, status_text):
            markers.append(marker)
    end = offset + _DOC_CHARS
    return {"doc": _trim(text, offset), "doc_offset": offset, "doc_chars": len(text),
            "doc_truncated": bool(text) and (offset > 0 or end < len(text)),
            "next_doc_offset": end if end < len(text) else None,
            "documentation_markers": markers}


def _classes(modules, class_filter):
    """Yield API class declarations in stable module/name order."""
    for namespace, module in modules:
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if not safe(lambda: cls.__module__, "").startswith("adsk"):
                continue
            if class_filter and name != class_filter:
                continue
            yield namespace, name, cls


def _class_matches(classes, regex, category, doc_offset):
    """Yield matching class records, searching full docstrings before clipping."""
    if category == "member":
        return
    for namespace, name, cls in classes:
        doc = safe(lambda: cls.__doc__, "") or ""
        name_hit = category != "description" and regex.search(name)
        doc_hit = category in ("description", "all") and regex.search(doc)
        if name_hit or doc_hit:
            yield {"type": "class", "name": name, "namespace": namespace,
                   **_doc_fields(doc, doc_offset)}


def _member_matches(classes, regex, category, doc_offset):
    """Yield matching public members without truncating the class scan."""
    if category == "class":
        return
    for namespace, cls_name, cls in classes:
        parent_doc = safe(lambda: cls.__doc__, "") or ""
        for name, member in inspect.getmembers(cls):
            if name.startswith("_"):
                continue
            scalar = isinstance(member, (int, float, str))
            doc = "" if scalar else (safe(lambda: member.__doc__, "") or "")
            name_hit = category != "description" and regex.search(name)
            doc_hit = category in ("description", "all") and regex.search(doc)
            if not (name_hit or doc_hit):
                continue
            kind = "function" if callable(member) and not inspect.isclass(member) else "property"
            entry = {"type": kind, "name": name, "class": cls_name, "namespace": namespace,
                     **_doc_fields(doc, doc_offset, parent_doc)}
            if isinstance(member, property):
                entry.update({"readable": member.fget is not None, "writable": member.fset is not None})
                signature = _signature(member.fget) if member.fget else None
            else:
                signature = _signature(member)
            if signature:
                entry["signature"] = signature
            if isinstance(member, (int, bool)):
                entry.update({"type": "constant", "value": member})
            yield entry


def _page(records, offset, cap):
    """Return one page and whether an additional matching record exists."""
    page = list(itertools.islice(records, offset, offset + cap + 1))
    return page[:cap], len(page) > cap


def handler(searchPattern: str = "", apiCategory: str = "all", filter: str = "",
            max_results: int = _MAX_RESULTS, offset: int = 0, doc_offset: int = 0) -> dict:
    """Return matching API declarations with result and docstring continuation offsets."""
    if not searchPattern:
        return error("Provide 'searchPattern' (a regex matched against API names/docs).")
    try:
        regex = re.compile(searchPattern, re.IGNORECASE)
    except re.error as exc:
        return error(f"Invalid regex 'searchPattern' {searchPattern!r}: {exc}")
    category = (apiCategory or "all").strip().lower()
    if category not in ("class", "member", "description", "all"):
        return error(f"apiCategory must be one of: class, member, description, all; got {apiCategory!r}.")
    for name, value in (("offset", offset), ("doc_offset", doc_offset)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return error(f"'{name}' must be a non-negative integer; got {value!r}.")
    try:
        cap = max(1, min(int(max_results), _MAX_RESULTS))
    except (TypeError, ValueError, OverflowError):
        return error(f"'max_results' must be an integer; got {max_results!r}.")

    unavailable = []
    modules = _load_modules(filter, unavailable)
    if not modules:
        return error(f"No API modules in scope for filter {filter!r}. "
                     f"Use an importable namespace from {', '.join(_API_MODULES)}. "
                     f"Import failures: {', '.join(row['namespace'] for row in unavailable) or 'none'}.")
    classes = list(_classes(modules, _class_filter_from(filter)))
    classes_out, more_classes = _page(_class_matches(classes, regex, category, doc_offset), offset, cap)
    members_out, more_members = _page(_member_matches(classes, regex, category, doc_offset), offset, cap)
    truncated = more_classes or more_members
    return ok({
        "search": searchPattern, "category": category, "filter": filter or None,
        "classes": classes_out, "members": members_out,
        "counts": {"classes": len(classes_out), "members": len(members_out)},
        "offset": offset, "next_offset": offset + cap if truncated else None,
        "truncated": truncated, "modules": [name for name, _ in modules],
        "unavailable_modules": unavailable, "scope_complete": not unavailable,
        "note": ("Declaration search only; runtime behavior and entitlement are not tested. "
                 "Keep the query and cap unchanged when using next_offset; use next_doc_offset "
                 "as doc_offset for more text. Documentation markers are not support guarantees."),
    })


TOOL_DESCRIPTION = (
    "Search installed Fusion API declarations and full docs. "
    "Follow next_offset or next_doc_offset for more."
)

tool = (
    Tool.create_simple(name="sys_get_api_doc", description=TOOL_DESCRIPTION)
    .add_input_property("searchPattern", {"type": "string",
                        "description": "Case-insensitive regex; description/all include docstrings."})
    .add_input_property("apiCategory", {"type": "string",
                        "enum": ["class", "member", "description", "all"]})
    .add_input_property("filter", {"type": "string",
                        "description": "Namespace or exact class, e.g. adsk.volume.Graph."})
    .add_input_property("max_results", {"type": "integer",
                        "description": f"Cap per kind, clamped to 1..{_MAX_RESULTS}."})
    .add_input_property("offset", {"type": "integer", "minimum": 0,
                        "description": "next_offset; keep query and cap."})
    .add_input_property("doc_offset", {"type": "integer", "minimum": 0,
                        "description": "A row's next_doc_offset."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
