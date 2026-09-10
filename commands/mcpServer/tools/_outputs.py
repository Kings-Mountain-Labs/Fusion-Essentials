# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Typed OUTPUT KINDS: a tool declares ``RETURNS = [...]`` for each id/value its ok() payload mints,
and ``assert_present(payload)`` fails when the declared key is not there."""

MAP_BLURB = ("RETURNS kinds (ReturnsHandle/Urn/Name/Value/Verdict) - declare a tool's stable "
             "outputs once")


class OutputKind:
    """One declared tool output: ``key`` the payload field, ``in_list`` for a key inside each list
    item, ``absent_when`` the payload flag whose truth licenses omitting it."""

    def __init__(self, key, label, consumers=(), stable=True, in_list=False, absent_when=""):
        self.key = key
        self.label = label
        self.consumers = list(consumers)
        self.stable = stable
        self.in_list = in_list
        self.absent_when = absent_when

    def produces_note(self) -> str:
        who = (" -> " + ", ".join(self.consumers)) if self.consumers else ""
        when = f" (omitted when {self.absent_when}=true)" if self.absent_when else ""
        return f"{self.key}: {self.label}{who}{when}".rstrip()

    def _present_in(self, obj) -> bool:
        """True if self.key appears at obj's top level, or - when in_list - inside any list item."""
        if isinstance(obj, dict):
            if self.key in obj and obj[self.key] is not None:
                return True
            if self.in_list:
                for v in obj.values():
                    if isinstance(v, list) and any(
                            isinstance(it, dict) and it.get(self.key) is not None for it in v):
                        return True
        return False

    def assert_present(self, payload) -> str:
        """An error string if the decoded ok() payload doesn't carry self.key, else ''."""
        if self._present_in(payload):
            return ""
        if self.absent_when and isinstance(payload, dict) and payload.get(self.absent_when) is True:
            return ""
        where = "in any list item" if self.in_list else "at the payload top level"
        because = (f" - and '{self.absent_when}' is not true in this payload, so the declared "
                   "omission does not apply") if self.absent_when else ""
        return f"declared output '{self.key}' is missing {where}{because}"


class ReturnsHandle(OutputKind):
    """Mints a find_geometry-style entityToken handle, consumed by ``_inputs.GeometryHandle``."""

    def __init__(self, key="handle", require="any", in_list=True, **kw):
        # 'any' names no kind, so it drops out of the label rather than reading "a any 'handle'".
        kind = "" if require == "any" else f"{require} "
        article = "an" if kind[:1] and kind[0] in "aeiou" else "a"
        super().__init__(
            key,
            f"{article} {kind}'handle' (entityToken; short-lived - use promptly, re-find if stale)",
            stable=False, in_list=in_list, **kw)
        self.require = require


class ReturnsUrn(OutputKind):
    """Mints a data-model lineage/version URN, consumed by the doc_*/data_* tools."""

    def __init__(self, key="document_id", **kw):
        super().__init__(key, "a data-model lineage URN", stable=True, **kw)


class ReturnsName(OutputKind):
    """Mints an EXACT name a consumer keys off (occurrence / setup / operation / joint / body)."""

    def __init__(self, key, of="occurrence", in_list=False, **kw):
        super().__init__(key, f"the exact {of} name", stable=True, in_list=in_list, **kw)
        self.of = of


class ReturnsValue(OutputKind):
    """Mints a measured / computed value (extents, frame axes, cycle time) - not an id."""

    def __init__(self, key, label, **kw):
        super().__init__(key, label, stable=False, **kw)


class ReturnsVerdict(OutputKind):
    """The assertion-read contract: relation / passed / measured / tolerance_used together, never a
    bare boolean - assert_present requires all four, a real bool, and a declared relation."""

    KEYS = ("relation", "passed", "measured", "tolerance_used")

    def __init__(self, relations=(), **kw):
        super().__init__(
            "passed",
            "the pass/fail verdict, always beside its evidence (relation / passed / measured / "
            "tolerance_used)",
            stable=False, **kw)
        self.relations = tuple(relations)

    def assert_present(self, payload) -> str:
        if not isinstance(payload, dict):
            return "verdict payload is not a dict"
        missing = [k for k in self.KEYS if k not in payload]
        if missing:
            return "verdict contract keys missing: " + ", ".join(missing)
        if not isinstance(payload["passed"], bool):
            return f"verdict 'passed' must be a real boolean, got {type(payload['passed']).__name__}"
        if self.relations and payload.get("relation") not in self.relations:
            return (f"verdict 'relation' is '{payload.get('relation')}' - not one of the declared: "
                    + ", ".join(self.relations))
        return ""


def produces_block(spec, header="Produces") -> str:
    """A tool's RETURNS spec as one description line: each output key and the tools that consume
    it - the label and the omitted-when clause stay in the payload, where they are read."""
    parts = []
    for out in spec:
        who = (" -> " + "/".join(out.consumers)) if out.consumers else ""
        parts.append(f"{out.key}{who}")
    return f"{header}: " + ", ".join(parts) + "."
