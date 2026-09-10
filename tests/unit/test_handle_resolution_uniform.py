"""Lint + behavioural anchor: handle resolution is UNIFORM across every InputKind.

find_geometry mints COMPOSITE handles ('<token>|@<kind>:x,y,z'); passing one raw to
findEntityByToken corrupts the token (the '|@locator' suffix) with no self-heal - a fresh handle
straight from find_geometry gets rejected. Every kind (GeometryHandle/BodyRef/PlaneRef/ProfileRef/
AxisRef) must route through `_resolve_token_entity`, which `_split_handle`s off the locator and
falls back to it when the token is stale.

This locks the invariant two ways:
  1. SOURCE LINT - `findEntityByToken(` is CALLED in _inputs.py only inside `_resolve_token_entity`
     (the single sanctioned resolution path). A new kind that hand-rolls findEntityByToken trips this.
  2. BEHAVIOUR - each handle-taking kind round-trips a COMPOSITE handle whose bare token is the only
     key in the map. A kind that passes the whole composite string to findEntityByToken won't resolve
     it, and the test fails.
"""

import os
import re

import adsk.core

from conftest import load_tool, TOOLS_DIR

inp = load_tool("_inputs")


# ── 1) source lint ──────────────────────────────────────────────────────────

class TestFindEntityByTokenIsCentralised:
    def test_findentitybytoken_called_only_inside_resolve_token_entity(self):
        src = open(os.path.join(TOOLS_DIR, "_inputs.py"), encoding="utf-8").read()
        # Find every line that CALLS findEntityByToken (an open-paren after it), ignoring prose/comments.
        offenders = []
        in_resolver = False
        resolver_indent = None
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("def "):
                in_resolver = stripped.startswith("def _resolve_token_entity")
                resolver_indent = len(line) - len(stripped)
            # a call (not a comment) to findEntityByToken(
            if "findEntityByToken(" in line and not stripped.startswith("#"):
                if not in_resolver:
                    offenders.append((i, line.strip()))
        assert not offenders, (
            "findEntityByToken(...) must be called ONLY inside _resolve_token_entity (the self-healing, "
            "composite-handle-aware path). Route handle resolution through _resolve_token_entity instead. "
            f"Offending call(s): {offenders}")

    def test_no_tool_guesses_handle_vs_name_by_length(self):
        # A `len(name) > 60` heuristic mis-routes a long body/component NAME into the handle path.
        # The sanctioned way to tell handle from name is to ask findEntityByToken (a non-token
        # returns nothing and the caller falls through to the name lookup) - no tool may guess by
        # length. _inputs.py is exempt: it owns the resolver.
        length_guess = re.compile(r"len\([^)]*\)\s*>\s*60")
        offenders = []
        for fn in sorted(os.listdir(TOOLS_DIR)):
            if not fn.endswith(".py") or fn == "_inputs.py":
                continue
            src = open(os.path.join(TOOLS_DIR, fn), encoding="utf-8")
            for i, line in enumerate(src, 1):
                if length_guess.search(line) and not line.lstrip().startswith("#"):
                    offenders.append((fn, i, line.strip()))
            src.close()
        assert not offenders, (
            "a tool guesses handle-vs-name by string length (`len(...) > 60`). Resolve through "
            "_inputs._resolve_token_entity instead (a non-token name returns None and you fall through "
            f"to the name lookup). Offending line(s): {offenders}")


# ── 2) behavioural round-trip per handle kind ───────────────────────────────
#
# Each kind gets a fake entity of the type it accepts, registered under a BARE token, and is handed the
# COMPOSITE handle. Resolution must succeed — proving the kind splits the '|@locator' off before lookup.

_SEP = inp._HANDLE_SEP


class _PlanarFace:
    def __init__(self):
        # surfaceType carries the measured int (seeded from live_api_facts), like a live face.
        self.geometry = type("G", (), {"surfaceType": adsk.core.SurfaceTypes.PlaneSurfaceType})()


class _LinearEdge:
    def __init__(self):
        self.geometry = type("G", (), {"curveType": adsk.core.Curve3DTypes.Line3DCurveType})()


class _Body:
    def __init__(self, name="B"):
        self.name = name


def _wire(handle_map, *, faces=False, edges=False, bodies=False):
    """Install a design resolving the BARE token (not the composite) + the adsk type wiring each kind
    isinstance-checks against."""
    import adsk.fusion
    if faces:
        adsk.fusion.BRepFace = _PlanarFace
    if edges:
        adsk.fusion.BRepEdge = _LinearEdge
    if bodies:
        adsk.fusion.BRepBody = _Body

    class _D:
        def findEntityByToken(self, t):
            e = handle_map.get(t)
            return [e] if e is not None else []
        rootComponent = type("R", (), {"bRepBodies": type("BB", (), {"itemByName": staticmethod(lambda n: None)})()})()
    d = _D()
    inp._common.design = lambda: d
    inp._common.target_component = lambda x: d.rootComponent
    return d


class TestEveryHandleKindAcceptsACompositeHandle:
    def test_geometry_handle(self):
        f = _PlanarFace()
        _wire({"T": f}, faces=True)
        val, err = inp.GeometryHandle("h", require="planar_face").resolve(f"T{_SEP}planar_face:0,0,0")
        assert err is None and val is f

    def test_body_ref(self):
        b = _Body("Blk")
        _wire({"T": b}, bodies=True)
        val, err = inp.BodyRef("h").resolve(f"T{_SEP}body:1,2,3")
        assert err is None and val is b

    def test_plane_ref(self):
        f = _PlanarFace()
        _wire({"T": f}, faces=True)
        val, err = inp.PlaneRef("h").resolve(f"T{_SEP}planar_face:0,0,0")
        assert err is None and val is f

    def test_axis_ref(self):
        # AxisRef resolves a composite find_geometry handle to a linear edge, like every handle kind.
        e = _LinearEdge()
        _wire({"T": e}, edges=True)
        val, err = inp.AxisRef("h").resolve(f"T{_SEP}edge:0,0,0")
        assert err is None and val == ("edge", e)
