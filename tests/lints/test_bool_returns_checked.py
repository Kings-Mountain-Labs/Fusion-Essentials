# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""An adsk call that answers "did it work" may not have its answer thrown away.

An `api_surface.BOOL_METHODS` call on a resolved feature input fails as a bare statement or a bare
`safe(lambda: ...)`; `if not safe(...)` reads the bool and passes. _ALLOWED sites are exempt."""

import ast

import _corpus
import api_surface
from _input_resolution import _iter_tool_files, input_scopes

# Calls whose bool is genuinely uninteresting, keyed (file, the statement's own source text), each
# with the reason. A text repeated in one file exempts each copy. Shrink-only.
_ALLOWED = {
    ("assembly_move.py","mat.setToRotation(math.radians(float(rotate_deg)), axis_dir, pt)"):
        "Matrix3D math on a LOCAL matrix - the resulting transform is written to the occurrence and "
        "read back, and an unchanged pose after a move is already an error",
    ("assembly_move.py","mat.setToRotation(math.radians(float(rotate_deg)), "
                              "adsk.core.Vector3D.create(*axis_vec), origin)"): "as above",
    ("assembly_move.py","r.setToRotation(math.radians(float(ang)), "
                              "adsk.core.Vector3D.create(*vec), origin)"): "as above",
    ("assembly_move.py","mat.transformBy(r)"): "as above",
    ("assembly_move.py","mat.transformBy(tmat)"): "as above",
    ("model_create_component.py", "matrix.setToRotation(math.radians(float(rotate_deg)), "
                                  "adsk.core.Vector3D.create(*axis_vec), "
                                  "adsk.core.Point3D.create(0, 0, 0))"):
        "Matrix3D math on a local matrix that places a new occurrence, whose creation is verified "
        "by re-listing",
}


def _site(source, node):
    """The statement's OWN source text, whitespace-collapsed - the allowlist key, which a line move
    cannot stale."""
    return " ".join((ast.get_source_segment(source, node) or "").split())


def _bool_call(node):
    """The method name when `node` is a call to an unambiguously bool-returning adsk member."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    name = node.func.attr
    return name if name in api_surface.BOOL_METHODS else None


def _receiver(call):
    """The variable a method call is made ON ('inp.setDistanceExtent(...)' -> 'inp')."""
    owner = call.func.value
    return owner.id if isinstance(owner, ast.Name) else None


def _offenders_in(path):
    """[(lineno, site, var, method, how)] for every discarded bool from a call on a FEATURE INPUT -
    a refresh()/fit() elsewhere returning false is cosmetic, a declined setter is not."""
    out = []
    source = _corpus.text(path)
    for nodes, bound in input_scopes(_corpus.tree(path)):
        for node in nodes:
            # Only a BARE STATEMENT discards the value. `if not safe(lambda: x.setQuality(q)):`
            # reads the bool through safe() and acts on it, which is what this gate asks for.
            if not isinstance(node, ast.Expr):
                continue
            call, how = node.value, "the result is discarded"
            if isinstance(call, ast.Call):
                fname = call.func.attr if isinstance(call.func, ast.Attribute) else \
                    getattr(call.func, "id", None)
                if fname == "safe" and call.args and isinstance(call.args[0], ast.Lambda):
                    call = call.args[0].body
                    how = "wrapped in a bare safe(), so the bool AND any exception are swallowed"
            name = _bool_call(call)
            if name and _receiver(call) in bound:
                out.append((node.lineno, _site(source, node), _receiver(call), name, how))
    return out


class TestBoolReturnsChecked:
    def test_no_documented_bool_return_is_discarded(self):
        offenders = []
        for name, path in _iter_tool_files():
            for lineno, site, var, method, how in _offenders_in(path):
                if (name, site) in _ALLOWED:
                    continue
                offenders.append(f"{name}:{lineno}: {var}.{method}() returns bool and {how}")
        assert not offenders, (
            'each of these documents "Returns true if successful" - a false answer means the '
            "setting never took, and the handler runs on to report success anyway:\n  "
            + "\n  ".join(offenders))

    def test_every_allowlisted_site_still_exists(self):
        live = set()
        for name, path in _iter_tool_files():
            for _lineno, site, _v, _m, _h in _offenders_in(path):
                live.add((name, site))
        stale = sorted(set(_ALLOWED) - live)
        assert not stale, ("these allowlist entries name no discarded bool in that file - the site "
                           "was reworded or fixed. Update the text or remove them:\n  "
                           + "\n  ".join(f"{n}: looked for `{s}`" for n, s in stale))

    def test_a_builder_helper_and_a_tuple_binding_are_followed(self, tmp_path):
        # model_hole's shape: a helper builds the input and returns (input, error), so the caller
        # binds it through a TUPLE target rather than `name = <collection>.createXInput(...)`.
        src = tmp_path / "builder.py"
        src.write_text(
            "def _build(holes, d):\n"
            "    return holes.createSimpleInput(d), None\n"
            "\n"
            "def handler(d, t):\n"
            "    holes = safe(lambda: comp.features.holeFeatures)\n"
            "    hin, berr = _build(holes, d)\n"
            "    hin.setDistanceExtent(d)\n"                        # discarded -> offender
            "    if hin.setToTappedHole(t) is False:\n"             # read and acted on -> clean
            "        return None\n",
            encoding="utf-8")
        found = _offenders_in(str(src))
        assert sorted(m for _l, _s, _v, m, _h in found) == ["setDistanceExtent"], found

    def test_a_builder_binds_the_input_at_the_position_it_returned_it(self, tmp_path):
        # the tuple POSITION carries the class, not the unpacking: binding every element would
        # make the ERROR slot an input too.
        src = tmp_path / "position.py"
        src.write_text(
            "def _build(holes, d):\n"
            "    return None, holes.createSimpleInput(d)\n"
            "\n"
            "def handler(d):\n"
            "    holes = safe(lambda: comp.features.holeFeatures)\n"
            "    err, hin = _build(holes, d)\n"
            "    err.setDistanceExtent(d)\n"                     # not an input -> clean
            "    hin.setDistanceExtent(d)\n",                    # the input -> the ONE offender
            encoding="utf-8")
        found = _offenders_in(str(src))
        assert sorted(v for _l, _s, v, _m, _h in found) == ["hin"], found

    def test_a_factory_on_an_unresolved_receiver_is_followed_by_its_method_name(self, tmp_path):
        # sketch_constrain's shape: the collection arrives as a PARAMETER, so nothing in the file
        # binds it - the METHOD still names one input class across the whole API.
        src = tmp_path / "param_recv.py"
        src.write_text(
            "def _apply(gc, ents, kind):\n"
            "    pin = gc.createRectangularPatternInput(ents, kind)\n"
            "    pin.setDirectionOne(a, b, c)\n",
            encoding="utf-8")
        found = _offenders_in(str(src))
        assert sorted(m for _l, _s, _v, m, _h in found) == ["setDirectionOne"], found

    def test_an_ambiguous_factory_method_does_not_resolve_a_receiver(self, tmp_path):
        # the boundary of the rule above: `createInput`'s declarations DISAGREE on what they
        # return, so on an UNRESOLVED receiver it names none of them.
        src = tmp_path / "ambiguous.py"
        src.write_text(
            "def _apply(anything, a, b):\n"
            "    inp = anything.createInput(a, b)\n"
            "    inp.setDistanceExtent(b)\n",
            encoding="utf-8")
        assert _offenders_in(str(src)) == []

    def test_the_generated_name_list_is_populated_and_narrowed(self):
        names = api_surface.BOOL_METHODS
        assert len(names) >= 300, f"only {len(names)} bool-returning names - the scrape has broken."
        # ambiguous names must be OUT: Features.add returns a feature, deleteMe varies by class
        for ambiguous in ("add", "deleteMe", "item", "createInput"):
            assert ambiguous not in names, f"'{ambiguous}' is not unambiguously bool-returning"
        # the ones this class is about must be IN
        for expected in ("setDistanceExtent", "setOneSideExtent", "setPositionAtCenter",
                         "finishEdit", "rollTo"):
            assert expected in names, f"'{expected}' should be a known bool-returning member"
