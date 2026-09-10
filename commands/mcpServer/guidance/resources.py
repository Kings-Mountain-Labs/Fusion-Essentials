# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The static MCP Resources this server publishes: the packaged guidance, whole and per section.

MCP's resource channel is application-controlled content a client reads by address, which is what
packaged guidance is. Every entry is built from the same document ``sys_get_guidance`` answers from
and rendered through the same ``render``, so the channels cannot serve different guidance.

``entry.py`` builds this catalog at startup and hands it to ``start_server``: the transport serves
the value it was given and reads no product file of its own.
"""

from . import loader
from . import render

# The address space this server names its packaged documents in. The id in the address is the
# document's own, so the URI a tool payload publishes and the URI the resource listing carries are
# one string built one way.
URI_PREFIX = "fusion-essentials://guidance/"

# Rendered guidance is Markdown - headings and rules, not a data structure to parse.
MIME_TYPE = "text/markdown"


def uri_for(guidance_id):
    """The resource URI naming one guidance document."""
    return f"{URI_PREFIX}{guidance_id}"


def section_uri_for(guidance_id, section_id):
    """The resource URI naming ONE section of one guidance document."""
    return f"{URI_PREFIX}{guidance_id}/{section_id}"


def _entry(uri, name, title, description, text):
    return {
        "uri": uri,
        "name": name,
        "title": title,
        "description": description,
        "mimeType": MIME_TYPE,
        # Bytes of the text a read returns - the size the client is deciding whether to fetch.
        "size": len(text.encode("utf-8")),
        "text": text,
    }


def catalog(path=None):
    """The resource entries this server can serve, each carrying its own rendered text: the whole
    document, then one per non-kernel section so a client can fetch the playbook it needs alone.

    Raises ``loader.GuidanceUnavailable`` when the packaged document does not read: the caller then
    publishes nothing and advertises no resource capability, rather than an address that answers
    with an error."""
    doc, _sha256 = loader.load(path)
    guidance_id = doc.get("guidance_id")
    entries = [_entry(uri_for(guidance_id), doc.get("name"), doc.get("title"),
                      doc.get("description"), render.body(doc))]
    for sec in (doc.get("sections") or []):
        if not isinstance(sec, dict) or sec.get("id") == loader.KERNEL:
            continue
        section_id = sec.get("id")
        entries.append(_entry(section_uri_for(guidance_id, section_id),
                              f"{guidance_id}/{section_id}", sec.get("title"),
                              sec.get("use_when"), render.section_text(doc, section_id)))
    return entries
