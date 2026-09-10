# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""MCP Item wrapper that bundles a primitive (Tool) with its handler, plus the Verification kind a
write declares when no postcondition kernel captures how its effect is proven."""

from .tool import Tool


class Verification:
    """How a WRITE tool's effect is verified, where no kernel postcondition captures it.

    A closed classification passed to ``Item.create_tool_item(verification=...)``, refused on a
    read, which mutates nothing to verify. Beside ``postconditions=[...]`` only an inline or
    effect kind that declares a ``rung`` may stand: the kernel kinds gate the call, and the
    declaration says how much more the handler's own read-back proves. It is DATA - the
    reasoning lives in the evidence test each declaration names, never in prose here.

      inline    the handler re-reads the requested effect and errors on a mismatch
      effect    the success payload is built from the live post-write state
      deferred  the effect lands asynchronously; a NAMED poller read tool confirms completion
      external  the effect is outside the design state (a user interaction, server lifecycle)
      dynamic   the requested effect is caller-authored (the script hatch)
      gap       no adequate verification exists; carries the ledger id of the recorded defect

    ``evidence_test`` is a pytest node id whose test proves the classification's obligation;
    ``poller`` names the read tool a deferred payload sends the caller to; ``evidence_receipt``
    points at the live receipt row for an effect no in-process test can observe; ``defect_id`` is
    the ledger id a gap carries. ``rung`` is how much the read-back proves - count, exists, value
    or geometry (the _assert.RUNGS ladder) - declared by an inline, effect or deferred
    verification so the lint can hold the write's verb to a minimum.
    tests/lints/test_postconditions_declared.py resolves each of them against the test tree, the
    registry and the receipt.
    """

    KINDS = ("inline", "effect", "deferred", "external", "dynamic", "gap")
    RUNGS = ("count", "exists", "value", "geometry")
    # The kinds whose handler reads an effect back, and so can say how much the read proves.
    _RUNG_KINDS = ("inline", "effect", "deferred")

    _FIELD_NAMES = ("evidence_test", "poller", "evidence_receipt", "defect_id")
    # kind -> (fields it REQUIRES, fields it PERMITS)
    _FIELDS = {
        "inline": (("evidence_test",), ("evidence_test",)),
        "effect": (("evidence_test",), ("evidence_test",)),
        "deferred": (("evidence_test", "poller"), ("evidence_test", "poller")),
        "external": ((), ("evidence_test", "evidence_receipt")),
        "dynamic": ((), ()),
        "gap": (("defect_id",), ("defect_id",)),
    }

    __slots__ = _FIELD_NAMES + ("kind", "rung")

    def __init__(self, kind: str, evidence_test: str = None, poller: str = None,
                 evidence_receipt: str = None, defect_id: str = None, rung: str = None):
        if kind not in self.KINDS:
            raise ValueError(f"verification kind must be one of {list(self.KINDS)}, got {kind!r}")
        if rung is not None and rung not in self.RUNGS:
            raise ValueError(f"verification rung must be one of {list(self.RUNGS)}, got {rung!r}")
        if rung is not None and kind not in self._RUNG_KINDS:
            raise ValueError(f"verification kind '{kind}' reads no effect back, so it declares no rung")
        self.rung = rung
        given = dict(zip(self._FIELD_NAMES,
                         (evidence_test, poller, evidence_receipt, defect_id)))
        required, permitted = self._FIELDS[kind]
        for field, value in given.items():
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"verification {field} must be a non-empty string, got {value!r}")
            if field not in permitted:
                raise ValueError(f"verification kind '{kind}' takes no {field}")
        missing = [f for f in required if given[f] is None]
        if missing:
            raise ValueError(f"verification kind '{kind}' requires {', '.join(missing)}")
        # An external effect proves itself in process or through a named live receipt - one
        # channel, declared. Neither means no observable completion proof, which is a gap.
        if kind == "external" and (evidence_test is None) == (evidence_receipt is None):
            raise ValueError("verification kind 'external' declares exactly one of evidence_test "
                             "(an in-process proof) or evidence_receipt (a live receipt row)")
        self.kind = kind
        for field, value in given.items():
            setattr(self, field, value)

    def __repr__(self) -> str:
        parts = [f"kind={self.kind!r}"] + [f"{f}={getattr(self, f)!r}"
                                           for f in self._FIELD_NAMES if getattr(self, f)]
        if self.rung:
            parts.append(f"rung={self.rung!r}")
        return f"Verification({', '.join(parts)})"


def _defaults_from_signature(tool, handler):
    """Stamp each input's DEFAULT into the schema from the handler's own signature default - a
    bool, a non-zero number or a non-empty string - so the value an omitted input takes is
    structure on the wire, not prose. A property that already carries a default keeps it."""
    import inspect
    from ..tools._inputs import schema_default
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return
    props = tool.input_schema.get("properties") or {}
    for name, p in params.items():
        prop = props.get(name)
        if (p.default is inspect.Parameter.empty or not isinstance(prop, dict)
                or "default" in prop or not schema_default(p.default)):
            continue
        prop["default"] = p.default


class Item:
    """Bundles an MCP primitive with the callable that fulfills it.

    run_on_main_thread defaults to True: any handler that touches the Fusion API
    MUST run on Fusion's main thread (marshalled via TaskManager). Only set this
    False for handlers that are pure Python and never call adsk.*.
    """

    def __init__(self, primitive: Tool, handler: callable, run_on_main_thread: bool = True,
                 enforce_timeout: bool = True, verification: 'Verification' = None,
                 deferred_capable: bool = False):
        if not isinstance(primitive, Tool):
            raise ValueError("Primitive must be a Tool instance")
        if not callable(handler):
            raise ValueError("Handler must be a callable function")
        self.name = primitive.name
        self.primitive = primitive
        self.handler = handler
        self.run_on_main_thread = run_on_main_thread
        # enforce_timeout=False exempts a tool from the server's main-thread task timeout. Use it
        # ONLY for tools whose work cannot be interrupted AND would still commit if we "timed out"
        # (e.g. sys_execute_script) - timing those out would report a false failure for a change
        # that actually applied. Default True keeps the safety timeout for everything else.
        self.enforce_timeout = enforce_timeout
        # Registry-side metadata only: it never reaches to_dict(), so nothing here crosses the wire.
        self.verification = verification
        self.deferred_capable = bool(deferred_capable)

    def get_name(self) -> str:
        return self.primitive.name

    def get_type(self) -> str:
        return "tool"

    def to_dict(self) -> dict:
        return self.primitive.to_dict()

    def __str__(self) -> str:
        return f"Item(type='{self.get_type()}', name='{self.get_name()}')"

    def __repr__(self) -> str:
        return f"Item(primitive={self.primitive}, handler={self.handler})"

    @classmethod
    def create_tool_item(cls, tool: Tool, handler: callable, run_on_main_thread: bool = True,
                         enforce_timeout: bool = True, write: str = None,
                         postconditions: list = None,
                         verification: 'Verification' = None,
                         deferred_capable: bool = False) -> 'Item':
        """Build a tool Item. ``write`` declares the tool's write-status, applied to the tool's
        annotations (readOnlyHint / destructiveHint) so the server reports it as structured data:
          'read'        -> read-only (does not modify state)
          'write'       -> modifies state
          'destructive' -> a hard-to-reverse write (delete, history-discarding conversion, close doc)
        Every tool must pass one (enforced by test_write_status_annotations.py)."""
        _defaults_from_signature(tool, handler)
        if deferred_capable:
            if write not in ("write", "destructive") or not run_on_main_thread:
                raise ValueError("deferred_capable requires a main-thread write tool")
            tool.add_input_property("deferred", {"type": "boolean",
                    "description": "True accepts durable work; poll drawing_get_status."})
            tool.add_input_property("request_key", {"type": "string",
                    "description": "Caller-known idempotency key, required with deferred=true."})
        if write == "read":
            tool.reads()
        elif write == "write":
            tool.writes()
        elif write == "destructive":
            tool.writes(destructive=True)
        elif write is not None:
            raise ValueError(f"write must be 'read'/'write'/'destructive', got {write!r}")
        # VERIFICATION CLASSIFICATION: how a write with no kernel postcondition proves its effect
        # (see Verification). Registry metadata, resolved by the postcondition lint - it changes
        # neither the handler nor the wire. Read the annotation the tool just declared rather than
        # the `write` argument: a tool wiring its own guard passes write=None (sys_request_selection).
        if verification is not None:
            if not isinstance(verification, Verification):
                raise ValueError(f"verification on '{tool.name}' must be a Verification kind, got "
                                 f"{type(verification)}")
            if postconditions and not (verification.kind in ("inline", "effect")
                                       and verification.rung):
                raise ValueError(f"'{tool.name}' declares postconditions AND a verification - "
                                 "beside kernel kinds only an inline or effect verification that "
                                 "declares a rung may stand, saying how much more the handler's "
                                 "own read-back proves")
            ann = tool.annotations
            if ann is None or ann.read_only is not False:
                raise ValueError(f"verification declared on a non-write tool '{tool.name}' - a "
                                 "read mutates nothing to verify")
        # WRITE-DOCUMENT BINDING (the concurrency guard): a write can land on the WRONG document if the
        # active doc moved since the agent's read (async open / a human switching tabs). Wrap every
        # write/destructive handler with the shared guard - it accepts an optional expect_document
        # (REFUSE on mismatch) and stamps acted_on on the result. One seam covers all write tools; read
        # tools are untouched. (Lazy import: item.py is a primitive; the guard lives in tools/.)
        if write in ("write", "destructive"):
            # POSTCONDITION KERNEL (verify-the-effect): wrapped INSIDE the write guard so the guard
            # stamps acted_on on the verified result. Runs capture -> handler -> verify and converts a
            # success whose declared effect did not take into an error (see tools/_assert.py). A read
            # tool never verifies (nothing mutated); postconditions on a read are a wiring mistake.
            if postconditions:
                from ..tools import _assert
                handler = _assert.wrap(handler, postconditions)
            from ..tools import _write_guard
            handler = _write_guard.wrap(handler)
            tool.add_input_property(*_write_guard.EXPECT_DOCUMENT_PROP)
        elif postconditions:
            raise ValueError(f"postconditions declared on a non-write tool '{tool.name}' - a read "
                             "mutates nothing to verify")
        # READ-DOCUMENT STAMP: every read result reports the document it read from
        # ('active_document') so a read taken against the wrong active document is visible in the
        # payload, mirroring the write side's acted_on. Main-thread tools only - the identity read
        # touches adsk, which a pure-Python off-thread handler must never do.
        if write == "read" and run_on_main_thread:
            from ..tools import _write_guard
            handler = _write_guard.wrap_read(handler)
        return cls(primitive=tool, handler=handler, run_on_main_thread=run_on_main_thread,
                   enforce_timeout=enforce_timeout, verification=verification,
                   deferred_capable=deferred_capable)
