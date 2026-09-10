# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Delete a cloud document (a saved DataFile) by its lineage URN, guarded and irreversible."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, read_flag, safe
from ._data_common import _MAX_XREFS, _data

app = adsk.core.Application.get()


def _parent_ref_summary(data_file):
    """The files that REFERENCE this DataFile, bounded: (parents, unreadable), where unreadable NAMES
    the read that failed ('hasParentReferences' or 'parentReferences.asArray()') else None. [] for a
    failed read is indistinguishable from a file nothing points at, so the destructive path fails
    CLOSED on that sentinel instead of proceeding to deleteMe()."""
    has = read_flag(lambda: data_file.hasParentReferences)
    if has is None:
        return [], "hasParentReferences"
    if has is False:
        return [], None
    refs = safe(lambda: data_file.parentReferences.asArray())
    if refs is None:
        return [], "parentReferences.asArray()"
    out = []
    for r in refs:
        out.append({"name": safe(lambda: r.name), "id": safe(lambda: r.id)})
        if len(out) >= _MAX_XREFS:
            break
    return out, None


def _is_document_open(file_id):
    """True if a document with this lineage id is currently open in the session.

    deleteMe() fails on an open file; checking first lets us return a clear message.
    """
    if not file_id:
        return False
    try:
        docs = app.documents
        for d in iter_collection(docs):
            df = safe(lambda d=d: d.dataFile)
            if df and safe(lambda df=df: df.id) == file_id:
                return True
    except Exception:
        pass
    return False


def handler(document_id: str = "", confirm_name: str = "", force: bool = False) -> dict:
    """Delete a cloud document by URN, guarded; see TOOL_DESCRIPTION for the confirm_name/force gates."""
    document_id = (document_id or "").strip()
    confirm_name = (confirm_name or "").strip()
    if not document_id:
        return error("Provide 'document_id' (the lineage URN of the file to delete).")
    if not confirm_name:
        return error("Provide 'confirm_name' - the exact current name of the file, as a "
    "safety confirmation. Get it from data_get or "
    "doc_get.")

    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    try:
        df = data.findFileById(document_id)
    except Exception as e:
        return error(f"findFileById failed for '{document_id}': {e}")
    if not df:
        return error(f"No file found for document_id '{document_id}'. It may already be "
            "deleted. Verify with data_get.")

    actual_name = safe(lambda: df.name) or "(unknown)"
    # Case-SENSITIVE confirmation: this is a safety gate, so require an exact match
    # (only surrounding whitespace is forgiven).
    if actual_name.strip() != confirm_name:
        return error(
            f"Name mismatch - refusing to delete. document_id resolves to '{actual_name}', "
            f"but confirm_name was '{confirm_name}'. Pass confirm_name='{actual_name}' if you "
            "really mean this file.")

    if _is_document_open(document_id):
        return error(f"'{actual_name}' is currently OPEN - close it before deleting "
            "(Fusion will not delete an open document).")

    parents, refs_unreadable = _parent_ref_summary(df)
    if refs_unreadable and not force:
        # Fail CLOSED: the read that would have shown the orphan risk is the one that failed, so the
        # file is NOT provably unreferenced. The refusal names WHICH read failed.
        return error(
            f"Whether '{actual_name}' is referenced by other files could not be read "
            f"({refs_unreadable} failed), so it is NOT provably unreferenced - deleting it may "
            "orphan references this call cannot list. Refusing. Retry once the file reads "
            "(data_get(file=<urn>)), or pass force=true to delete WITHOUT the reference check. "
            "Nothing was deleted.")
    if parents and not force:
        names = ", ".join(p.get("name") or "?" for p in parents)
        return error(
            f"'{actual_name}' is referenced by {len(parents)} other file(s): {names}. "
            "Deleting it would orphan those references. Pass force=true to delete anyway "
            "(Fusion may still reject it).")

    try:
        did = df.deleteMe()  # adsk.core: DataFile.deleteMe() -> bool
    except Exception as e:
        return error(f"Delete failed for '{actual_name}': {e}")
    if not did:
        return error(f"Fusion declined to delete '{actual_name}' (it may be referenced or "
    "open). No change was made.")

    payload = {
    "deleted": True,
    "name": actual_name,
    "document_id": document_id,
    # Null, never [], when the reference read did not answer: an empty list says "nothing
    # referenced this file", which is not what an unreadable read supports.
    "was_referenced_by": None if refs_unreadable else parents,
    "forced": bool(force and (parents or refs_unreadable)),
    }
    if refs_unreadable:
        payload["reference_state_unreadable"] = refs_unreadable
        payload["note"] = (f"The file's reference state could not be read ({refs_unreadable} "
                           "failed) and force=true deleted it anyway, so whether other files "
                           "referenced it - and are now orphaned - is unknown; "
                           "'was_referenced_by' is null, not empty.")
    return ok(payload)


TOOL_DESCRIPTION = (
    "Delete a document on the cloud, IRREVERSIBLY, by its lineage URN: 'confirm_name' must EXACTLY "
    "match the file's current name."
)

tool = (
    Tool.create_with_string_input(
        name="data_delete_file",
        description=TOOL_DESCRIPTION,
        input_param_name="document_id",
        input_param_description="The file's lineage URN (data_get, doc_get).",
    )
    .add_input_property("confirm_name", {"type": "string", "description": "Case-sensitive."})
    .add_input_property("force", {"type": "boolean",
        "description": "Delete despite references."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler,
    run_on_main_thread=True,
    # deleteMe()'s own answer is the whole gate: whether findFileById stops resolving a just-deleted
    # lineage - and how long the data model takes to show that - is not measured here.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_data_delete_file.py::TestDeleteDocument"
                      "::test_delete_me_false_reported",
        rung="exists")
)


def register_tool():
    register(item)
