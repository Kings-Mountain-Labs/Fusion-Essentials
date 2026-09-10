# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Write-document binding: the guard wrapped at registration around every WRITE tool's handler.
'expect_document' REFUSES the write (blocked_by:['active_document_changed'], or
'ambiguous_document_name' when a bare name several open documents share), and 'acted_on' stamps the
document actually mutated; a main-thread read gets wrap_read's 'active_document' stamp instead.
Main-thread tools only - the identity read touches adsk. document_key answers the same identity
question for a STORE that outlives one MCP call."""

import json
import uuid

import adsk.core

from . import _common

app = adsk.core.Application.get()

MAP_BLURB = (
    "_active_identity + one_open_document - document identity reads; document_key - store key; "
    "document_handle + resolve_document_handle - exact session addresses until close/reload; "
    "prune_closed_documents + on_key_evicted/on_key_renamed - store lifecycle hooks")


def _active_identity():
    """(name, document_id_urn) of the active document; either may be None (no doc / unsaved)."""
    try:
        doc = app.activeDocument
    except Exception:
        return None, None
    if not doc:
        return None, None
    name = None
    urn = None
    try:
        name = doc.name
    except Exception:
        pass
    try:
        df = doc.dataFile
        if df:
            urn = df.id
    except Exception:
        pass
    return name, urn


# The (document, key) pairs this session minted a key for. A scanned LIST, not a dict, because the
# match is `==`: a Document wrapper is not identity-stable - the same open document reads as a new
# wrapper on each app.activeDocument access, so `is` reads False while `==` reads True.

# A Document carries no entityToken of its own, and rootComponent's reads BYTE-IDENTICAL across two
# distinct never-saved documents - it cannot key a document, and it collides silently.
_UNSAVED_DOC_KEYS = []
_UNSAVED_DOC_SEQ = 0

# Session handles are opaque and remain tied to the native Document across save and tab movement.
_SESSION_DOCUMENTS = []

# A LIST of listeners, not a per-call callback: the registry is SHARED, so whichever consumer's
# read triggers a prune must drop what EVERY consumer parked under that key.
_KEY_EVICTION_LISTENERS = []

# The same shape for a key that CHANGES while its document stays open.
_KEY_RENAME_LISTENERS = []


def on_key_evicted(callback):
    """Register callback(key) for every key the prune drops - the key a CLOSED document last
    answered, not necessarily the token minted for it. Call it at module import."""
    _KEY_EVICTION_LISTENERS.append(callback)


def on_key_renamed(callback):
    """Register callback(old_key, new_key) for every key a HELD document re-keys onto. Call it at
    module import; a consumer holding state under old_key MOVES it to new_key. Fired only while the
    document stays open, so both keys name the same document."""
    _KEY_RENAME_LISTENERS.append(callback)


def prune_closed_documents():
    """Drop key-registry entries whose document is gone, telling every listener which key went. A
    closed document's leftover wrapper reads isValid False (.name on it raises); only a definite
    False evicts, since an isValid that will not read proves nothing and a LIVE document losing its
    key would be minted a second one with its saved state split in half."""
    # Walked BACKWARDS: deleting at i slides the next entry into i, and range() is sized before the
    # list starts shrinking - so a forward walk skips an entry and then indexes past the end.
    for i in range(len(_UNSAVED_DOC_KEYS) - 1, -1, -1):
        known, key = _UNSAVED_DOC_KEYS[i]
        if _common.read_flag(lambda known=known: known.isValid) is False:
            del _UNSAVED_DOC_KEYS[i]
            for listener in _KEY_EVICTION_LISTENERS:
                listener(key)


def _prune_session_documents():
    """Drop handles whose native document wrapper is not certainly live."""
    for i in range(len(_SESSION_DOCUMENTS) - 1, -1, -1):
        if _common.read_flag(lambda i=i: _SESSION_DOCUMENTS[i][0].isValid) is not True:
            del _SESSION_DOCUMENTS[i]


def document_handle(doc=None):
    """Return an opaque session handle for a live Document wrapper, minting by native equality."""
    if doc is None:
        doc = _common.safe(lambda: app.activeDocument)
    if doc is None or _common.read_flag(lambda doc=doc: doc.isValid) is not True:
        return None
    _prune_session_documents()
    for known, handle in list(_SESSION_DOCUMENTS):
        if bool(_common.safe(lambda known=known: known == doc, False)):
            return handle
    handle = "session:" + uuid.uuid4().hex
    _SESSION_DOCUMENTS.append((doc, handle))
    return handle


def resolve_document_handle(handle):
    """Resolve an opaque session handle, refusing unknown or closed Documents."""
    if not isinstance(handle, str) or not handle.startswith("session:"):
        return None
    for i, (doc, known) in enumerate(list(_SESSION_DOCUMENTS)):
        if known != handle:
            continue
        if _common.read_flag(lambda doc=doc: doc.isValid) is not True:
            del _SESSION_DOCUMENTS[i]
            return None
        return doc
    return None


def is_document_handle(value):
    """Whether a value uses the opaque session-document handle address form."""
    return isinstance(value, str) and value.startswith("session:")


def active_document_handle():
    """Return the active document's session handle, or None when its identity is unreadable."""
    return document_handle()


def document_key():
    """The key the ACTIVE document is remembered by across MCP calls - its data-file id, else a
    per-instance token minted on first sight and matched by document handle EQUALITY, never by
    NAME (several open documents answer "Untitled"). A token that LATER gives way to an id is
    announced through on_key_renamed. None when no document reads at all."""
    global _UNSAVED_DOC_SEQ
    # Pruned FIRST, before any branch can return: the read that finds a document CLOSED is usually
    # taken while a DIFFERENT document is active, so a later prune never runs when it is needed.
    prune_closed_documents()
    doc = _common.safe(lambda: app.activeDocument)
    if doc is None:
        return None
    df = _common.safe(lambda: doc.dataFile)
    did = _common.safe(lambda: df.id) if df is not None else None
    for i, (known, held) in enumerate(_UNSAVED_DOC_KEYS):
        if not bool(_common.safe(lambda known=known: known == doc, False)):
            continue
        # A held document that reads NO id keeps the key it holds: an id that stopped reading is not
        # evidence the document went back to having none.
        if not did or did == held:
            return held
        # Through a save the id may answer a path-form string before the lineage urn resolves, so
        # one document can re-key twice; the entry is rewritten, not removed, so the second flip is
        # caught too. PROBE NEEDED (KEY-2): that path-form id is mechanism, not a ledger fact.
        _UNSAVED_DOC_KEYS[i] = (doc, did)
        for listener in _KEY_RENAME_LISTENERS:
            listener(held, did)
        return did
    if did:
        return did          # first sight of a document that already answers an id - nothing to mint
    _UNSAVED_DOC_SEQ += 1
    key = "unsaved:%d" % _UNSAVED_DOC_SEQ
    _UNSAVED_DOC_KEYS.append((doc, key))
    return key


def _refusal(expect, name, urn):
    """The structured refusal payload (no write happened)."""
    payload = {
        "blocked_by": ["active_document_changed"],
        "expected": expect,
        "actual": {"name": name, "document_id": urn},
        "requires": {"tool": "doc_activate", "argument": expect},
        "note": ("The active document is not the one you targeted (expect_document) - it moved between "
                 "your read and this write (async open / a human switching tabs). Refused WITHOUT "
                 "writing. Switch with doc_activate, then retry. (Omit expect_document to write the "
                 "current active doc regardless.)"),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": True, "message": "active_document_changed: expected %r, active is %r"
            % (expect, name)}


def _unknown_handle_refusal(expect, name, urn):
    """Refuse a session handle that no longer resolves, with a usable reacquisition route."""
    payload = {
        "blocked_by": ["unknown_document_handle"],
        "expected": expect,
        "actual": {"name": name, "document_id": urn},
        "requires": {"tool": "doc_get",
                     "result": "open_documents[].document_handle"},
        "note": ("The expected session document handle is unknown, closed, or expired after "
                 "reload. Refused WITHOUT writing. Call doc_get and retry with the target row's "
                 "current document_handle."),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": True,
            "message": "unknown_document_handle: %r cannot be resolved; call doc_get" % expect}


def _open_documents():
    """Return open-document rows, preserving failed item reads as unreadable rows."""
    out = []
    try:
        docs = app.documents
    except Exception:
        return out
    active = _common.safe(lambda: app.activeDocument)
    if not docs:
        return out
    try:
        total = int(docs.count)
    except Exception:
        return out
    for i in range(total):
        try:
            d = docs.item(i)
        except Exception:
            d = None
        if d is None:
            out.append({"name": None, "readable": False})
            continue
        name = urn = None
        try:
            name = d.name
        except Exception:
            pass
        try:
            df = d.dataFile
            if df:
                urn = df.id
        except Exception:
            pass
        is_active = False
        try:
            # EQUALITY, never identity: Document wrappers are not identity-stable - `d is active`
            # reads False for the one open, active document while `d == active` reads True.
            is_active = bool(d == active)
        except Exception:
            pass
        out.append({"name": name, "document_id": urn, "document_handle": document_handle(d), "open_index": i, "is_active": is_active})
    return out


def _scan_refusal(expect, name, urn):
    """Refuse a bare-name write when the open-document uniqueness scan is incomplete."""
    payload = {
        "blocked_by": ["document_collection_unreadable"],
        "expected": expect,
        "actual": {"name": name, "document_id": urn},
        "requires": {"tool": "doc_get", "result": "open_documents[].document_handle"},
        "note": ("Open-document names did not establish a unique target for expect_document %r. "
                 "Refused WITHOUT writing. Call doc_get and retry with the exact document_handle." % expect),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": True,
            "message": "document_collection_unreadable: pass expect_document as an exact handle"}


def _collision_refusal(expect, candidates):
    """Structured refusal when a bare NAME is shared by MORE THAN ONE open document (no write happened).
    Lists each candidate's name + URN; an UNSAVED candidate has no URN, so its open_index (doc_get's
    session address) is given instead. Reuses _refusal's shape/style; instructs passing the URN."""
    rows, seen_urns = [], set()
    for c in candidates:
        cid = c.get("document_id")
        if cid and cid in seen_urns:
            continue        # one document loaded twice (tab + dependency instance) is ONE candidate
        if cid:
            seen_urns.add(cid)
        row = {"name": c.get("name"), "document_id": cid,
               "document_handle": c.get("document_handle")}
        if not cid:
            row["open_index"] = c.get("open_index")    # positional fallback
        rows.append(row)
    payload = {
        "blocked_by": ["ambiguous_document_name"],
        "expected": expect,
        "candidates": rows,
        "note": (f"{len(candidates)} open documents share the name '{expect}', so a bare name does not "
                 "identify which one to write - refused WITHOUT writing. Pass expect_document as the "
                 "document_handle from doc_get or the lineage URN. Activate that same address first. "
                 "Session handles expire on close/add-in reload; open_index is positional."),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": True, "message": "ambiguous_document_name: %d open docs named '%s' - pass the URN"
            % (len(candidates), expect)}


def one_open_document(document_ids):
    """True when these open-document ids (dataFile.id values) are ONE document, not an ambiguity.
    An assembly loads its references as REAL Documents, so a visible tab and the dependency instance
    a referencing document loaded repeat one name AND lineage URN. Ids that DIFFER, and an id that
    did not read, both answer False."""
    ids = list(document_ids)
    return bool(ids) and all(ids) and len(set(ids)) == 1


def _document_refusal(expect, name, urn):
    """None = proceed; a refusal dict = do NOT write. A URN match is exact and sufficient. A bare NAME
    is safe ONLY when it is session-unique: if more than one open document shares that name, the active
    one may not be the doc the agent read, so REFUSE and demand the URN. Both the wrapped write path
    and sys_request_selection's own main-thread check gate on THIS helper, so the contract is one."""
    e = (expect or "").strip()
    if not e:
        return None
    if is_document_handle(e):
        if resolve_document_handle(e) is None:
            return _unknown_handle_refusal(expect, name, urn)
        if active_document_handle() == e:
            return None
        return _refusal(expect, name, urn)
    if e == (urn or ""):
        return None                              # exact URN - unambiguous, always wins
    if e != (name or ""):
        return _refusal(expect, name, urn)       # the active doc is simply not the target
    # e matches the ACTIVE doc's NAME - a match only if that name is unique across the open session.
    scan = _open_documents()
    if any(not isinstance(d.get("name"), str) or not d["name"] for d in scan):
        return _scan_refusal(expect, name, urn)
    same = [d for d in scan if d.get("name") == e]
    if not same:
        return _scan_refusal(expect, name, urn)
    if len(same) > 1:
        # One document listed twice (tab + dependency instance) is not an ambiguity: when every
        # candidate is that one document AND it is the active one, the write lands where meant.
        urns = [d.get("document_id") for d in same]
        if one_open_document(urns) and urn in urns:
            return None
        return _collision_refusal(e, same)
    return None


def _stamp(result, key, name, urn):
    """Inject {key: {name,document_id}} into a successful JSON result. Leaves errors + non-JSON
    results (e.g. an image block) untouched."""
    if not isinstance(result, dict) or result.get("isError"):
        return result
    content = result.get("content")
    if not (isinstance(content, list) and content and isinstance(content[0], dict)):
        return result
    block = content[0]
    if block.get("type") != "text":
        return result
    try:
        payload = json.loads(block["text"])
    except Exception:
        return result                      # non-JSON text result; nothing to stamp
    if not isinstance(payload, dict):
        return result
    payload.setdefault(key, {"name": name, "document_id": urn})
    block["text"] = json.dumps(payload, indent=2)
    return result


def _stamp_acted_on(result, name, urn):
    """Fill acted_on={name,document_id} into a successful JSON result (an error wasn't an action on a
    document). FILL-IF-ABSENT: a handler that already published its own acted_on is AUTHORITATIVE and
    keeps it - see wrap()."""
    return _stamp(result, "acted_on", name, urn)


def wrap_read(handler):
    """Wrap a READ handler with the active_document stamp: every result reports the document it read
    from ({name, document_id}). No guard, no expect_document. Main-thread tools only (the identity
    read touches adsk)."""
    def stamped(**kwargs):
        result = handler(**kwargs)
        name, urn = _active_identity()
        return _stamp(result, "active_document", name, urn)
    stamped.__name__ = getattr(handler, "__name__", "stamped")
    stamped.__wrapped__ = handler
    return stamped


def wrap(handler):
    """Wrap a WRITE handler with the expect_document guard + acted_on stamp. Returns a new callable with
    the same call shape. expect_document is consumed here (popped from kwargs) - the handler never sees
    it."""
    def guarded(**kwargs):
        expect = kwargs.pop("expect_document", None)
        name, urn = _active_identity()
        if expect:
            refusal = _document_refusal(expect, name, urn)
            if refusal is not None:
                return refusal                       # REFUSE - no handler call, no mutation
        result = handler(**kwargs)
        # Re-read identity AFTER the handler runs: a doc-switching write (doc_new/doc_open/
        # doc_activate) makes a DIFFERENT document active, and acted_on must report that one. The
        # stamp is FILL-IF-ABSENT, so a write targeting a NON-active document keeps its own.
        name, urn = _active_identity()
        return _stamp_acted_on(result, name, urn)
    guarded.__name__ = getattr(handler, "__name__", "guarded")
    guarded.__wrapped__ = handler                    # so tests/introspection can reach the original
    return guarded


# The input property advertised on every write tool (so an agent knows it can target a document).
EXPECT_DOCUMENT_PROP = ("expect_document", {
    "type": "string",
    "description": "Refused unless this doc is active.",
})
