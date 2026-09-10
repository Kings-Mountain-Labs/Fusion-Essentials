"""Unit tests for ``sketch_set_text.py`` — set/create sketch-text strings.

Pinned here (no live Fusion): the quote/unquote round-trip (the textParameter expression is the
QUOTED string, with single-quote escaping), the sketch-text iterator across components + sketches
and the one-sketch leaf op a NAMED edit walks, the per-sketch 0-based index selection, the
before/after change tally + the
_MAX cap, the recompute gating (only in parametric mode), the create path's unit scaling and
guards, the align-anchored multi_line box, the three layout modes with the inputs each one refuses, the definition read-back that
says which mode actually landed, and the font applied on both paths with its read-back. The actual
engraving is a live side-effect.
"""

import json
import math
import types

import pytest

from conftest import (MakeComp, MakeDesign, Sketch, SketchCurves, _NamedCollection, install,
                      load_tool)

st = load_tool("sketch_set_text")


# ── quote / unquote (round-trip + escaping) ─────────────────────────────────

class TestQuoteUnquote:
    def test_quote_wraps_in_single_quotes(self):
        assert st._quote("Hello") == "'Hello'"

    def test_quote_escapes_inner_single_quote(self):
        assert st._quote("It's") == "'It\\'s'"

    def test_unquote_strips_single_quotes(self):
        assert st._unquote("'Label'") == "Label"

    def test_unquote_strips_double_quotes(self):
        assert st._unquote('"Label"') == "Label"

    def test_unquote_passes_unquoted_through(self):
        assert st._unquote("bare") == "bare"

    def test_unquote_none_is_none(self):
        assert st._unquote(None) is None

    def test_unquote_single_char_not_stripped(self):
        # length < 2 -> can't be a quoted pair
        assert st._unquote("'") == "'"

    def test_quote_round_trips_for_quote_free_text(self):
        # _quote escapes inner quotes but _unquote only strips the outer pair (no unescape), so the
        # round-trip is an identity ONLY for text with no single quotes.
        for s in ("plain", "two words", ""):
            assert st._unquote(st._quote(s)) == s


# ── fakes ───────────────────────────────────────────────────────────────────

class FakeParam:
    def __init__(self, expr):
        self.expression = expr


class _HeightParam:
    """A SketchText.heightParameter: .value reads internal cm and takes a write, and the glyph
    geometry follows it (measured live). `skew` lands the written value off by that much, `lands`
    False models a parameter that accepts the assignment while the value stays put, `readable` False
    one whose value will not read, and `error` a setter Fusion refuses."""
    def __init__(self, text, value, lands=True, readable=True, error=None, skew=0.0):
        self._text = text
        self._v = value
        self._lands = lands
        self._readable = readable
        self._error = error
        self._skew = skew

    @property
    def value(self):
        if not self._readable:
            raise RuntimeError("heightParameter.value is not available")
        return self._v

    @value.setter
    def value(self, v):
        if self._error:
            raise RuntimeError(self._error)
        if self._lands:
            old, self._v = self._v, v + self._skew
            self._text._resize(old, self._v)


class FakeText:
    """A SketchText. `fontName` is a real property so the set-then-read-back has something to read:
    `font_error` models Fusion refusing a name (the setter raises and the font is kept), and
    `font_lands=False` models a setter that accepts the assignment while the font stays put, and
    `font_readable=False` a text whose font will not read at all.

    `height_cm` gives the text a heightParameter and a boundingBox; leaving it None models the
    plain text every non-resize test uses (neither member reads). `box=False` models a text whose
    box will not read, `box_follows=False` glyphs that stay put while the value lands, and
    `box_delta` a box that moves by a fixed amount rather than proportionally."""
    def __init__(self, expr, font="Arial", font_error=None, font_lands=True, font_readable=True,
                 height_cm=None, height_lands=True, height_readable=True, height_error=None,
                 height_skew=0.0, box=None, box_follows=True, box_delta=None):
        self.textParameter = FakeParam(expr)
        self._font = font
        self.font_error = font_error
        self.font_lands = font_lands
        self.font_readable = font_readable
        self._box_follows = box_follows
        self._box_delta = box_delta
        if height_cm is not None:
            self.heightParameter = _HeightParam(self, height_cm, height_lands, height_readable,
                                                height_error, height_skew)
            if box is not False:
                self._set_box(*(box if box else (4.0 * height_cm, height_cm)))

    def _set_box(self, w, h):
        self._w, self._h = w, h
        self.boundingBox = types.SimpleNamespace(
            minPoint=types.SimpleNamespace(x=0.0, y=0.0),
            maxPoint=types.SimpleNamespace(x=w, y=h))

    def _resize(self, old, new):
        """Measured live: the glyph geometry follows the height PROPORTIONALLY (halving the height
        halves the box width). box_delta moves the box by a fixed amount instead - the band
        boundary - and box_follows=False leaves it exactly where it was."""
        if getattr(self, "boundingBox", None) is None:
            return
        if self._box_delta is not None:
            self._set_box(self._w + self._box_delta, self._h + self._box_delta)
        elif self._box_follows and old:
            self._set_box(self._w * new / old, self._h * new / old)

    @property
    def fontName(self):
        if not self.font_readable:
            raise RuntimeError("fontName is not available")
        return self._font

    @fontName.setter
    def fontName(self, value):
        if self.font_error:
            raise RuntimeError(self.font_error)
        if self.font_lands:
            self._font = value


class _Coll(_NamedCollection):
    """The shared collection with a stale slot: item_raises_at makes item(i) raise while count
    still includes it."""
    def __init__(self, items, item_raises_at=None):
        super().__init__(items)
        self._raises_at = item_raises_at

    def item(self, i):
        if i == self._raises_at:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return super().item(i)


class FakeSketch(Sketch):
    """The shared Sketch holding the sketchTexts an edit walks."""
    def __init__(self, name, texts):
        super().__init__(name=name)
        self.sketchTexts = _Coll(texts)


class FakeComp(MakeComp):
    """The shared component whose sketches each point back at it, the way a live Sketch's
    parentComponent does."""
    def __init__(self, name, sketches):
        super().__init__(name=name, sketches=list(sketches))
        for sk in sketches:
            sk.parentComponent = self


class FakeDesign(MakeDesign):
    """The shared design plus the computeAll a parametric recompute calls, recorded in `computed`.
    resolve_sketch (used by the create path) searches rootComponent + all_components."""
    def __init__(self, comps, design_type=1):
        super().__init__(comp=comps[0] if comps else None, all_components=list(comps),
                         design_type=design_type)
        self.computed = False

    def computeAll(self):
        self.computed = True


def _install(comps, design_type=1):
    return install(st, FakeDesign(comps, design_type))


def _install_with_parameters(comps, names, unit="Text"):
    """A design whose allParameters answers `names` - the user parameters a text can bind to.
    MEASURED: a text parameter's `unit` reads 'Text' where a length parameter's reads 'mm'."""
    design = FakeDesign(comps)
    design.allParameters = _NamedCollection(
        [types.SimpleNamespace(name=n, unit=unit) for n in names])
    install(st, design)
    return design


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


# ── _iter_sketch_texts ──────────────────────────────────────────────────────

class TestIterSketchTexts:
    def test_collects_across_components_and_sketches(self):
        c1 = FakeComp("Root", [FakeSketch("S1", [FakeText("'a'")]),
                               FakeSketch("S2", [FakeText("'b'"), FakeText("'c'")])])
        c2 = FakeComp("Sub", [FakeSketch("S3", [FakeText("'d'")])])
        design = _install([c1, c2])
        got = list(st._iter_sketch_texts(design))
        assert len(got) == 4
        # tuple shape: (component_name, sketch_name, sketch_text)
        assert got[0][0] == "Root" and got[0][1] == "S1"

    def test_a_scoped_walk_stops_at_that_component(self):
        c1 = FakeComp("Root", [FakeSketch("S1", [FakeText("'a'")])])
        c2 = FakeComp("Sub", [FakeSketch("S3", [FakeText("'d'")])])
        design = _install([c1, c2])
        got = list(st._iter_sketch_texts(design, c2))
        assert [(row[0], row[1]) for row in got] == [("Sub", "S3")]

    def test_texts_in_sketch_yields_one_sketchs_texts_in_index_order(self):
        # the leaf op the NAMED path walks: one already-resolved sketch, positional
        sk = FakeSketch("Label", [FakeText("'x'"), FakeText("'y'")])
        FakeComp("Root", [sk])
        got = list(st._texts_in_sketch(sk, "Root"))
        assert [(c, n, t.textParameter.expression) for c, n, t in got] == [
            ("Root", "Label", "'x'"), ("Root", "Label", "'y'")]

    def test_no_texts_yields_empty(self):
        design = _install([FakeComp("Root", [FakeSketch("Empty", [])])])
        assert list(st._iter_sketch_texts(design)) == []

    def test_a_sketch_whose_texts_will_not_read_yields_nothing_rather_than_raising(self):
        # one unreadable sketch must not take the whole walk down - the other sketches still answer
        class _Blind:
            name = "Blind"

            @property
            def sketchTexts(self):
                raise RuntimeError("4 : An API Object refers to a deleted Object")

        blind = _Blind()
        good = FakeSketch("Good", [FakeText("'a'")])
        design = _install([FakeComp("Root", [blind, good])])
        assert list(st._texts_in_sketch(blind, "Root")) == []
        assert [row[1] for row in st._iter_sketch_texts(design)] == ["Good"]

    def test_a_component_whose_sketches_raise_is_skipped_not_fatal(self):
        class _Deaf:
            name = "Deaf"

            @property
            def sketches(self):
                raise RuntimeError("component not readable")

        design = _install([_Deaf(), FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        assert [row[0] for row in st._iter_sketch_texts(design)] == ["Root"]


# ── the 'component' SCOPE, on BOTH paths ────────────────────────────────────
# Fusion numbers sketches per component from 1, so two components each holding a "Label" is the
# norm. Both paths resolve ONE sketch by name, so both REFUSE the shared name unscoped and the
# scope is what says which component's "Label" the call means.

class TestComponentScopeOnEdits:
    def _shared(self):
        """ONE sketch name in TWO components, each holding a DIFFERENT string, so which text an
        edit reached is readable from the payload rather than from a name they share."""
        alpha = FakeSketch("Label", [FakeText("'alpha-old'")])
        beta = FakeSketch("Label", [FakeText("'beta-old'"), FakeText("'beta-second'")])
        _install([FakeComp("Alpha", [alpha]), FakeComp("Beta", [beta])])
        return alpha, beta

    def test_an_unscoped_shared_name_refuses_and_rewrites_nothing(self):
        # A sketch name is unique only within a component, so an unscoped edit of a name TWO of them
        # carry has no way to know which nameplate was meant. Writing the string into both and
        # reporting the total reads as success while two components' labels changed - so it refuses,
        # naming the owners and the input that narrows it, and neither text moves.
        alpha, beta = self._shared()
        res = st.handler(text="New", sketch_name="Label")
        assert res["isError"] is True
        assert "2 sketches are named 'Label'" in res["message"]
        assert "Alpha" in res["message"] and "Beta" in res["message"]
        assert "'component'" in res["message"] and "Rename one" not in res["message"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"
        assert beta.sketchTexts.item(0).textParameter.expression == "'beta-old'"
        assert beta.sketchTexts.item(1).textParameter.expression == "'beta-second'"

    def test_the_scope_edits_only_that_components_texts(self):
        alpha, beta = self._shared()
        out = _payload(st.handler(text="New", sketch_name="Label", component="Beta"))
        assert out["changed_count"] == 2                       # Beta's two, not Alpha's one
        assert [c["before"] for c in out["changed"]] == ["beta-old", "beta-second"]
        # every changed row names the ONE component the scope picked
        assert {c["component"] for c in out["changed"]} == {"Beta"}
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"

    def test_the_sibling_component_is_reachable_by_the_same_call(self):
        alpha, beta = self._shared()
        out = _payload(st.handler(text="New", sketch_name="Label", component="Alpha"))
        assert out["changed_count"] == 1
        assert beta.sketchTexts.item(0).textParameter.expression == "'beta-old'"

    def test_an_unknown_component_is_refused_and_nothing_is_rewritten(self):
        alpha, beta = self._shared()
        res = st.handler(text="New", sketch_name="Label", component="Gamma")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"
        assert beta.sketchTexts.item(0).textParameter.expression == "'beta-old'"

    def test_the_scope_without_a_name_edits_that_components_texts_only(self):
        # 'sketch_name' omitted is "every sketch text", and the scope narrows THAT walk too - the
        # branch a named edit does not pass through, so it needs its own coverage.
        alpha, beta = self._shared()
        out = _payload(st.handler(text="New", component="Beta"))
        assert out["changed_count"] == 2
        assert [c["before"] for c in out["changed"]] == ["beta-old", "beta-second"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"

    def test_an_unknown_component_without_a_name_is_refused_too(self):
        alpha, beta = self._shared()
        res = st.handler(text="New", component="Gamma")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"
        assert beta.sketchTexts.item(0).textParameter.expression == "'beta-old'"

    def test_a_scope_holding_no_such_sketch_names_the_scope_and_the_owner(self):
        alpha = FakeSketch("Label", [FakeText("'alpha-old'")])
        _install([FakeComp("Alpha", [alpha]),
                  FakeComp("Beta", [FakeSketch("Other", [])])])
        res = st.handler(text="New", sketch_name="Label", component="Beta")
        assert res["isError"] is True
        # the scope it looked in, AND where the name actually is - the caller's next call
        assert "'Beta' holds no sketch named 'Label'" in res["message"]
        assert "'Alpha'" in res["message"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"

    def test_a_scoped_sketch_holding_no_text_still_reports_the_scope(self):
        # the other miss: the scope DOES hold that sketch, and the sketch holds no text
        alpha = FakeSketch("Label", [FakeText("'alpha-old'")])
        _install([FakeComp("Alpha", [alpha]),
                  FakeComp("Beta", [FakeSketch("Label", [])])])
        res = st.handler(text="New", sketch_name="Label", component="Beta")
        assert res["isError"] is True and "inside component 'Beta'" in res["message"]
        assert alpha.sketchTexts.item(0).textParameter.expression == "'alpha-old'"

    def test_a_named_edit_reaches_only_that_sketch(self):
        # the name narrows WITHIN a component too: the sibling sketch's text must not move
        label = FakeSketch("Label", [FakeText("'label-old'")])
        other = FakeSketch("Other", [FakeText("'other-old'")])
        _install([FakeComp("Alpha", [label, other])])
        out = _payload(st.handler(text="New", sketch_name="Label"))
        assert out["changed_count"] == 1
        assert out["changed"][0]["sketch"] == "Label"
        assert other.sketchTexts.item(0).textParameter.expression == "'other-old'"


# ── edit handler: tally / index / recompute ─────────────────────────────────

class TestEditHandler:
    def test_sets_all_texts_and_reports_before_after(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'old1'"), FakeText("'old2'")])])])
        out = _payload(st.handler(text="New"))
        assert out["set"] is True
        assert out["changed_count"] == 2
        assert out["changed"][0]["before"] == "old1"
        assert out["changed"][0]["after"] == "New"

    def test_index_selects_one_text_within_sketch(self):
        sk = FakeSketch("S", [FakeText("'zero'"), FakeText("'one'"), FakeText("'two'")])
        design = _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="Picked", index=1))
        assert out["changed_count"] == 1
        assert out["changed"][0]["before"] == "one"
        # the other two were left as quoted originals
        assert sk.sketchTexts.item(0).textParameter.expression == "'zero'"
        assert sk.sketchTexts.item(2).textParameter.expression == "'two'"

    def test_an_unreadable_text_holds_its_index_instead_of_shifting_the_rest(self):
        # 'index' is the Nth text WITHIN the sketch - the same address sketch_delete_entity's
        # 'text:<index>' takes. A text that reads back as nothing must burn its number: dropping it
        # would slide the third text onto index 1 and edit the wrong nameplate.
        sk = FakeSketch("S", [FakeText("'zero'"), None, FakeText("'two'")])
        _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="Picked", index=2))
        assert out["changed_count"] == 1
        assert out["changed"][0]["before"] == "two" and out["changed"][0]["after"] == "Picked"
        assert sk.sketchTexts.item(0).textParameter.expression == "'zero'"

    def test_index_out_of_range_is_error(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", index=5)
        assert res["isError"] is True and "index 5" in res["message"]

    def test_a_text_whose_item_read_raises_burns_its_slot(self):
        # The stale-proxy shape one step earlier than an unreadable STRING: sketchTexts.item(i)
        # itself raises. The slot burns its index (index=2 still reaches the third text) instead
        # of the raise taking the whole edit down or sliding the address space.
        sk = FakeSketch("S", [FakeText("'zero'"), FakeText("'dead'"), FakeText("'two'")])
        sk.sketchTexts = _Coll(sk.sketchTexts._items, item_raises_at=1)
        _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="Picked", index=2))
        assert out["changed_count"] == 1
        assert out["changed"][0]["before"] == "two" and out["changed"][0]["after"] == "Picked"
        assert sk.sketchTexts.item(0).textParameter.expression == "'zero'"

    def test_selecting_the_raising_slot_is_an_honest_refusal(self):
        # editing a text that will not read is impossible - a silent skip would report success
        # over a hole, so the SELECTED unreadable index refuses and names the re-read.
        sk = FakeSketch("S", [FakeText("'zero'"), FakeText("'dead'"), FakeText("'two'")])
        sk.sketchTexts = _Coll(sk.sketchTexts._items, item_raises_at=1)
        _install([FakeComp("Root", [sk])])
        res = st.handler(text="X", index=1)
        assert res["isError"] is True
        assert "could not be read" in res["message"] and "sketch_get" in res["message"]
        assert sk.sketchTexts.item(2).textParameter.expression == "'two'"   # untouched

    def test_index_counter_is_per_sketch(self):
        # index=0 must pick the FIRST text of EACH sketch, not the first overall
        s1 = FakeSketch("S1", [FakeText("'a0'"), FakeText("'a1'")])
        s2 = FakeSketch("S2", [FakeText("'b0'"), FakeText("'b1'")])
        design = _install([FakeComp("Root", [s1, s2])])
        out = _payload(st.handler(text="Z", index=0))
        assert out["changed_count"] == 2
        befores = {c["before"] for c in out["changed"]}
        assert befores == {"a0", "b0"}

    def test_no_text_in_named_sketch_errors(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", sketch_name="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_no_text_in_design_errors(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [])])])
        res = st.handler(text="X")
        assert res["isError"] is True and "No sketch text found" in res["message"]

    def test_recompute_runs_in_parametric(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])], design_type=1)
        out = _payload(st.handler(text="X"))
        assert out["recomputed"] is True
        assert design.computed is True

    def test_recompute_skipped_in_direct_mode(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])], design_type=0)
        out = _payload(st.handler(text="X"))
        assert out["recomputed"] is False
        assert design.computed is False

    def test_set_failure_is_reported(self):
        sk = FakeSketch("S", [FakeText("'a'")])

        class _Bad:
            @property
            def expression(self):
                return "'a'"
            @expression.setter
            def expression(self, v):
                raise RuntimeError("locked")
        sk.sketchTexts.item(0).textParameter = _Bad()
        design = _install([FakeComp("Root", [sk])])
        res = st.handler(text="X")
        assert res["isError"] is True and "Failed to set sketch text" in res["message"]

    def test_max_cap_limits_changes(self):
        # build _MAX + 5 texts in one sketch; only _MAX are changed
        n = st._MAX + 5
        sk = FakeSketch("S", [FakeText("'t'") for _ in range(n)])
        design = _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="X"))
        assert out["changed_count"] == st._MAX
        # Hitting the cap must be reported, not silently truncated
        assert out["truncated"] is True

    def test_truncated_is_false_when_under_the_cap(self):
        design = _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'"), FakeText("'b'")])])])
        out = _payload(st.handler(text="X"))
        assert out["truncated"] is False

    def test_none_text_errors(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text=None)
        assert res["isError"] is True and "Provide 'text'" in res["message"]
    def test_explicit_empty_literal_is_accepted(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text=""))
        assert out["changed_count"] == 1

    def test_whitespace_literal_is_accepted(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="   "))
        assert out["changed_count"] == 1

class _StubbornParam:
    """A textParameter that ACCEPTS an expression assignment and keeps the string it already holds
    - the platform's 'success that changed nothing', which only the read-back compare catches."""
    def __init__(self, expr):
        self._expr = expr

    @property
    def expression(self):
        return self._expr

    @expression.setter
    def expression(self, value):
        pass


class _UnreadableParam(_StubbornParam):
    """A textParameter whose expression will not read back at all."""
    @property
    def expression(self):
        raise RuntimeError("4 : An API Object refers to a deleted Object")

    @expression.setter
    def expression(self, value):
        self._expr = value


class TestLandedString:
    """The edit reads the expression back, so it must COMPARE it to the string that was asked for -
    the way the font two lines above it is compared. Read and not compared, a write the platform
    swallowed is reported as 'set': true with the unchanged string sitting in 'after'."""

    def test_a_string_that_does_not_land_is_an_error_not_a_false_ok(self):
        sk = FakeSketch("S", [FakeText("'old'")])
        sk.sketchTexts.item(0).textParameter = _StubbornParam("'old'")
        _install([FakeComp("Root", [sk])])
        res = st.handler(text="New")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "reads back 'old'" in res["message"] and "not 'New'" in res["message"]

    def test_the_landed_string_matching_the_request_stays_a_success(self):
        # the boundary partner: an expression that reads back as the requested string passes
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'old'")])])])
        out = _payload(st.handler(text="New"))
        assert out["changed"][0]["after"] == "New"

    def test_an_escaped_quote_is_not_read_as_a_mismatch(self):
        # the expression keeps an inner quote ESCAPED exactly as it was written, so comparing the
        # unquoted read alone would refuse every apostrophe the caller sends
        sk = FakeSketch("S", [FakeText("'old'")])
        _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="it's"))
        assert out["changed_count"] == 1
        assert sk.sketchTexts.item(0).textParameter.expression == st._quote("it's")

    def test_an_unreadable_expression_is_not_convicted_as_a_mismatch(self):
        # an expression that will not read is evidence of neither a landed nor a dropped write
        sk = FakeSketch("S", [FakeText("'old'")])
        sk.sketchTexts.item(0).textParameter = _UnreadableParam("'old'")
        _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="New"))
        assert out["changed_count"] == 1
        assert out["changed"][0]["after"] is None

    def test_a_mismatch_names_the_texts_this_call_already_changed(self):
        sk = FakeSketch("S", [FakeText("'a'"), FakeText("'b'")])
        sk.sketchTexts.item(1).textParameter = _StubbornParam("'b'")
        _install([FakeComp("Root", [sk])])
        res = st.handler(text="New")
        assert res["isError"] is True
        assert "1 sketch text(s) earlier in this call were already updated ('S')" in res["message"]
        assert sk.sketchTexts.item(0).textParameter.expression == st._quote("New")

    def test_a_mismatch_after_a_font_change_reports_the_font_that_landed(self):
        sk = FakeSketch("S", [FakeText("'old'", font="Arial")])
        sk.sketchTexts.item(0).textParameter = _StubbornParam("'old'")
        _install([FakeComp("Root", [sk])])
        res = st.handler(text="New", font_name="Consolas")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "font WAS changed to 'Consolas'" in res["message"]


# ── create path ─────────────────────────────────────────────────────────────

class FakeTextInput:
    """A SketchTextInput: the three setAs* placements plus the formatting properties. angle/flips
    are real attributes so set_verified's read-back succeeds, as it does live. `place_returns`
    models Fusion REFUSING a placement - the setAs* call answers false and nothing is placed."""
    def __init__(self, text, height, place_returns=True):
        self.text = text
        self.height = height
        self.multiline = None
        self.along = None
        self.fit = None
        self.mode = None
        self.place_returns = place_returns
        self.angle = 0.0
        self.isHorizontalFlip = False
        self.isVerticalFlip = False
        # the input takes any string; Fusion checks the name at add()
        self.fontName = None
    def _placed(self, mode):
        if not self.place_returns:
            return False
        self.mode = mode
        return True
    def setAsMultiLine(self, p1, p2, halign, valign, spacing):
        self.multiline = (p1, p2, halign, valign, spacing)
        return self._placed("multi_line")
    def setAsAlongPath(self, path, above, halign, spacing):
        self.along = (path, above, halign, spacing)
        return self._placed("along_path")
    def setAsFitOnPath(self, path, above):
        self.fit = (path, above)
        return self._placed("fit_on_path")


# mode -> the objectType the created text's definition reports. Bindings-sourced for all three;
# the fit-on-path spelling is the one measured live, typo included.
_DEFINITION_TYPES = {"multi_line": "adsk::fusion::MultiLineTextDefinition",
                     "along_path": "adsk::fusion::AlongPathTextDefinition",
                     "fit_on_path": "adsk::fusion::FitOnPathTextDefintion"}


def _fake_definition(ipt, obj_type, blind):
    """The definition object a created text carries. Unless `blind`, it reports the placement
    values back the way AlongPath/FitOnPath/MultiLine definitions do."""
    definition = types.SimpleNamespace(objectType=obj_type)
    if blind:
        return definition
    if ipt.mode == "along_path":
        _path, above, halign, spacing = ipt.along
        definition.isAbovePath = above
        definition.horizontalAlignment = halign
        definition.characterSpacing = spacing
    elif ipt.mode == "fit_on_path":
        definition.isAbovePath = ipt.fit[1]
    elif ipt.mode == "multi_line":
        definition.horizontalAlignment = ipt.multiline[2]
        definition.characterSpacing = ipt.multiline[4]
    return definition


class FakeSketchTexts:
    """createInput2/add that model a real SketchTexts collection: add() APPENDS so `count` rises -
    unless `materialize=False`, which returns a truthy text object while the collection stays flat
    (the on-face silent-no-op the honesty read-back must catch). `raise_on_add` models Fusion
    rejecting the placement at add() time; `definition_type` forces the landed definition;
    `blind_definition` makes every placement read off it fail; `placement_override` makes the
    definition report a placement that DISAGREES with the request; `place_returns=False` makes the
    setAs* call refuse. A font is checked at ADD time: `font_raises` models Fusion rejecting the
    name there, `landed_font` a text landing with a font other than the one asked for, and
    `blind_font` a created text whose fontName will not read.

    `bbox` is the (x0, y0, x1, y1) cm box the landed text reports as SketchText.boundingBox. The
    default is the measured shape of the case that motivated reporting it - an Arial h8 label
    running 188 mm - so it is nowhere near len(text) * height and a width read off the box is
    distinguishable from one estimated from the inputs. None models a text with no readable box."""
    def __init__(self, initial=0, materialize=True, add_returns=True, raise_on_add=None,
                 definition_type=None, blind_definition=False, place_returns=True,
                 placement_override=None, font_raises=None, landed_font=None, blind_font=False,
                 bbox=(0.0, 0.0, 18.8, 0.8)):
        self.last_input = None
        self._texts = [type("T", (), {"name": f"T{i}"})() for i in range(initial)]
        self.materialize = materialize
        self.add_returns = add_returns
        self.raise_on_add = raise_on_add
        self.definition_type = definition_type
        self.blind_definition = blind_definition
        self.place_returns = place_returns
        self.placement_override = placement_override or {}
        self.font_raises = font_raises
        self.landed_font = landed_font
        self.blind_font = blind_font
        self.bbox = bbox
        self.add_calls = 0
    @property
    def count(self):
        return len(self._texts)
    def createInput2(self, text, height):
        self.last_input = FakeTextInput(text, height, place_returns=self.place_returns)
        return self.last_input
    def add(self, ipt):
        self.add_calls += 1
        if self.raise_on_add:
            raise RuntimeError(self.raise_on_add)
        if self.font_raises and getattr(ipt, "fontName", None):
            raise RuntimeError(self.font_raises)
        obj_type = (_DEFINITION_TYPES.get(ipt.mode) if self.definition_type is None
                    else self.definition_type)
        definition = _fake_definition(ipt, obj_type, self.blind_definition)
        for prop, value in self.placement_override.items():
            setattr(definition, prop, value)
        st = types.SimpleNamespace(name="Text1", definition=definition)
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            st.boundingBox = types.SimpleNamespace(
                minPoint=types.SimpleNamespace(x=x0, y=y0, z=0.0),
                maxPoint=types.SimpleNamespace(x=x1, y=y1, z=0.0))
        font = self.landed_font if self.landed_font is not None else getattr(ipt, "fontName", None)
        if font is not None and not self.blind_font:
            st.fontName = font
        if self.materialize:
            self._texts.append(st)
        return st if self.add_returns else None


class FakeSketchForCreate(Sketch):
    """The shared Sketch a create lands text in, carrying the '<type>:<index>' collections
    resolve_entity_ref indexes."""
    def __init__(self, name, texts=None, lines=0, circles=0):
        super().__init__(
            name=name,
            curves=SketchCurves(
                lines=[types.SimpleNamespace(kind="line", i=i) for i in range(lines)],
                circles=[types.SimpleNamespace(kind="circle", i=i) for i in range(circles)]))
        self.sketchTexts = texts if texts is not None else FakeSketchTexts()
        self.sketchPoints = _Coll([types.SimpleNamespace(kind="point", i=0)])


def _install_create(sketch_name="Plate", texts=None, lines=0, circles=0):
    sk = FakeSketchForCreate(sketch_name, texts=texts, lines=lines, circles=circles)
    design = _install([MakeComp(name="Root", sketches=[sk])])
    import adsk.core
    adsk.core.Point3D.create = staticmethod(lambda x, y, z: ("pt", x, y, z))
    # HorizontalAlignments/VerticalAlignments members arrive pre-seeded with the measured ints
    # (live_api_facts via conftest) - the fake reads them, never assigns them.
    return design, sk


class TestCreateComponentScope:
    """The CREATE path's half of the scope: it resolves ONE sketch by name, so a shared name
    refuses without one and the scope is what says which component's sketch receives the text."""

    def _shared(self, monkeypatch):
        import adsk.core
        alpha = FakeSketchForCreate("Label")
        beta = FakeSketchForCreate("Label")
        _install([FakeComp("Alpha", [alpha]), FakeComp("Beta", [beta])])
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: ("pt", x, y, z))
        return alpha, beta

    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, monkeypatch):
        alpha, beta = self._shared(monkeypatch)
        res = st.handler(text="LBL", create=True, sketch_name="Label", height=10)
        assert res["isError"] is True
        assert "2 sketches are named 'Label'" in res["message"]
        assert "'component'" in res["message"] and "Rename one" not in res["message"]
        assert alpha.sketchTexts.add_calls == 0 and beta.sketchTexts.add_calls == 0

    def test_the_scope_creates_in_THAT_components_sketch(self, monkeypatch):
        alpha, beta = self._shared(monkeypatch)
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Label",
                                  component="Beta", height=10))
        assert out["created"] is True
        assert beta.sketchTexts.add_calls == 1 and alpha.sketchTexts.add_calls == 0

    def test_the_sibling_component_is_reachable_by_the_same_call(self, monkeypatch):
        alpha, beta = self._shared(monkeypatch)
        _payload(st.handler(text="LBL", create=True, sketch_name="Label",
                            component="Alpha", height=10))
        assert alpha.sketchTexts.add_calls == 1 and beta.sketchTexts.add_calls == 0

    def test_an_unknown_component_is_refused(self, monkeypatch):
        alpha, beta = self._shared(monkeypatch)
        res = st.handler(text="LBL", create=True, sketch_name="Label", component="Gamma",
                         height=10)
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert alpha.sketchTexts.add_calls == 0 and beta.sketchTexts.add_calls == 0

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self):
        alpha = FakeSketchForCreate("OnlyOne")
        _install([FakeComp("Alpha", [alpha]), FakeComp("Beta", [])])
        res = st.handler(text="LBL", create=True, sketch_name="OnlyOne", component="Beta",
                         height=10)
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert alpha.sketchTexts.add_calls == 0


class TestCreate:
    def test_creates_text_with_scaled_height(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", height=10, units="mm"))
        assert out["created"] is True and out["text"] == "LBL"
        # height 10mm -> 1.0cm handed to createInput2
        assert sk.sketchTexts.last_input.height == 1.0

    def test_create_without_a_height_applies_the_documented_default(self):
        # 'height' defaults to None on the wire so an edit-time height is DETECTABLE and refused;
        # the create path is where the documented 5 is applied, and the description promises it.
        design, sk = _install_create()
        _payload(st.handler(text="A", create=True, sketch_name="Plate", units="mm"))
        assert st._DEFAULT_HEIGHT == 5.0
        assert sk.sketchTexts.last_input.height == 0.5     # 5mm -> 0.5cm

    def test_the_create_default_is_applied_in_the_requested_units(self):
        # boundary beside it: the default is a number in 'units', not a fixed centimetre value
        design, sk = _install_create()
        _payload(st.handler(text="A", create=True, sketch_name="Plate", units="cm"))
        assert sk.sketchTexts.last_input.height == 5.0

    def test_create_position_scaled(self):
        design, sk = _install_create()
        _payload(st.handler(text="A", create=True, sketch_name="Plate", x=20, y=30, units="mm"))
        p1 = sk.sketchTexts.last_input.multiline[0]
        # x 20mm -> 2cm, y 30mm -> 3cm
        assert p1 == ("pt", 2.0, 3.0, 0)

    def test_create_requires_sketch_name(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="")
        assert res["isError"] is True and "sketch_name" in res["message"]

    def test_create_unknown_units(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="Plate", units="furlong")
        assert res["isError"] is True and "Unknown units" in res["message"]

    def test_create_nonpositive_height(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="Plate", height=0)
        assert res["isError"] is True and "height" in res["message"].lower()

    def test_create_missing_sketch(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="NoSuch")
        assert res["isError"] is True and "NoSuch" in res["message"]

    # ── honesty read-back: verify the text actually materialized ──────────────

    def test_create_reports_verified_count_delta(self):
        # count must be read back off the collection and reported (0 -> 1 here)
        design, sk = _install_create(texts=FakeSketchTexts(initial=0))
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate"))
        assert out["created"] is True
        assert out["sketch_text_count"] == 1

    def test_create_count_delta_from_nonzero_base(self):
        design, sk = _install_create(texts=FakeSketchTexts(initial=3))
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate"))
        assert out["sketch_text_count"] == 4

    def test_create_silent_noop_is_error_not_false_ok(self):
        # add() returns a truthy text object but the collection count does NOT rise: nothing
        # materialized. Reporting created:true here would be a false ok - the cardinal sin.
        sk_texts = FakeSketchTexts(initial=0, materialize=False, add_returns=True)
        design, sk = _install_create(texts=sk_texts)
        res = st.handler(text="LBL", create=True, sketch_name="Plate", x=0, y=0, height=5)
        assert res["isError"] is True
        assert "did not materialize" in res["message"]
        # and it names the coordinate-space fix (sketch-plane coords / frame)
        assert "SKETCH-plane" in res["message"] or "frame" in res["message"]

    def test_create_add_returns_none_is_error(self):
        sk_texts = FakeSketchTexts(initial=0, materialize=False, add_returns=False)
        design, sk = _install_create(texts=sk_texts)
        res = st.handler(text="LBL", create=True, sketch_name="Plate")
        assert res["isError"] is True and "did not materialize" in res["message"]

    def test_create_uses_sketch_plane_not_world_coordinates(self):
        # the corner point handed to setAsMultiLine is the raw (x,y) in SKETCH space (scaled to cm),
        # with no world/model transform applied - on-face coordinate handling.
        design, sk = _install_create()
        _payload(st.handler(text="AB", create=True, sketch_name="Plate", x=12, y=8, units="mm"))
        corner, diagonal = sk.sketchTexts.last_input.multiline[0], sk.sketchTexts.last_input.multiline[1]
        assert corner[0] == "pt" and corner[3] == 0
        assert corner[1] == pytest.approx(1.2) and corner[2] == pytest.approx(0.8)   # 12mm,8mm -> cm
        # diagonal is strictly offset in BOTH axes so the text box is never degenerate
        assert diagonal[1] > corner[1] and diagonal[2] > corner[2]

    def test_default_mode_is_multi_line(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate"))
        assert out["mode"] == "multi_line"
        assert sk.sketchTexts.last_input.mode == "multi_line"

    def test_unknown_mode_is_refused(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="wrap_around")
        assert res["isError"] is True and "wrap_around" in res["message"]


# ── multi_line: the box is ANCHORED by 'align' ──────────────────────────────

# 8 characters at height 10mm: the box is 8 * 10mm = 8cm wide, and x=100mm is 10cm.
_ANCHOR_TEXT = "CENTERME"
_ANCHOR_KW = dict(create=True, sketch_name="Plate", height=10, x=100, y=0, units="mm")


class TestMultiLineAnchor:
    """halign aligns the glyphs WITHIN the box setAsMultiLine is given and does not move that box,
    so the box itself must be anchored per 'align'. Anchoring it at (x,y) whatever the align lands
    centered text half a box-width to the RIGHT of the x the caller asked for."""

    @pytest.mark.parametrize("align,corner_x,diagonal_x", [("left", 10.0, 18.0),
                                                           ("center", 6.0, 14.0),
                                                           ("right", 2.0, 10.0)])
    def test_box_corner_and_diagonal_are_anchored_by_align(self, align, corner_x, diagonal_x):
        design, sk = _install_create()
        _payload(st.handler(text=_ANCHOR_TEXT, align=align, **_ANCHOR_KW))
        corner, diagonal = sk.sketchTexts.last_input.multiline[:2]
        assert corner[1] == pytest.approx(corner_x)
        assert diagonal[1] == pytest.approx(diagonal_x)

    def test_center_puts_the_box_centre_on_the_requested_x(self):
        # the contract in one line: align='center' at x=100mm centres the text on 100mm
        design, sk = _install_create()
        _payload(st.handler(text=_ANCHOR_TEXT, align="center", **_ANCHOR_KW))
        corner, diagonal = sk.sketchTexts.last_input.multiline[:2]
        assert (corner[1] + diagonal[1]) / 2 == pytest.approx(10.0)

    def test_right_puts_the_box_end_on_the_requested_x(self):
        design, sk = _install_create()
        _payload(st.handler(text=_ANCHOR_TEXT, align="right", **_ANCHOR_KW))
        assert sk.sketchTexts.last_input.multiline[1][1] == pytest.approx(10.0)

    def test_a_longer_string_shifts_the_centered_corner_further_left(self):
        # the anchor scales with the box width - a fixed offset would drift with the string length
        design, sk = _install_create()
        _payload(st.handler(text="A" * 16, align="center", **_ANCHOR_KW))
        corner, diagonal = sk.sketchTexts.last_input.multiline[:2]
        assert corner[1] == pytest.approx(2.0) and diagonal[1] == pytest.approx(18.0)

    def test_align_leaves_y_and_the_box_height_alone(self):
        design, sk = _install_create()
        _payload(st.handler(text=_ANCHOR_TEXT, align="center", **dict(_ANCHOR_KW, y=30)))
        corner, diagonal = sk.sketchTexts.last_input.multiline[:2]
        assert corner[2] == pytest.approx(3.0) and diagonal[2] == pytest.approx(4.0)

    def test_position_still_echoes_the_requested_x_not_the_shifted_corner(self):
        design, sk = _install_create()
        out = _payload(st.handler(text=_ANCHOR_TEXT, align="center", **_ANCHOR_KW))
        assert out["position"] == {"x": 100, "y": 0, "units": "mm"}


# ── path modes: along_path / fit_on_path ────────────────────────────────────

# a find_geometry handle: the composite locator form is_handle recognises
_EDGE_HANDLE = "qwertyuiopasdfghjklzxcvbnm0123456789==|@edge:2"


class TestPathModes:
    def test_along_path_passes_curve_alignment_and_spacing(self):
        design, sk = _install_create(lines=2)
        out = _payload(st.handler(text="RIM", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:1", align="center", character_spacing=25))
        import adsk.core
        curve, above, halign, spacing = sk.sketchTexts.last_input.along
        assert curve is sk.sketchCurves.sketchLines.item(1)
        assert halign is adsk.core.HorizontalAlignments.CenterHorizontalAlignment
        assert above is True and spacing == 25.0
        assert out["mode"] == "along_path" and out["path"] == "line:1"
        # published from the DEFINITION's own read-back, not echoed from the request
        assert out["align"] == "center" and out["character_spacing"] == 25.0
        assert out["above_path"] is True and "requested" not in out

    def test_along_path_on_a_closed_circle(self):
        # the headline case: text wrapped right around a hole
        design, sk = _install_create(circles=1)
        out = _payload(st.handler(text="M8", create=True, sketch_name="Plate", mode="along_path",
                                  path="circle:0"))
        assert sk.sketchTexts.last_input.along[0] is sk.sketchCurves.sketchCircles.item(0)
        assert out["mode_verified"] is True
        assert out["definition_type"].endswith("AlongPathTextDefinition")
        # LeftHorizontalAlignment is 0: a falsy-but-real member must map back to its wire key, not
        # be read as unreadable
        assert out["align"] == "left" and "requested" not in out

    def test_above_path_false_is_passed_through(self):
        design, sk = _install_create(lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0", above_path=False))
        assert sk.sketchTexts.last_input.along[1] is False
        assert out["above_path"] is False

    def test_fit_on_path_uses_the_two_argument_call(self):
        design, sk = _install_create(lines=1)
        out = _payload(st.handler(text="FIT", create=True, sketch_name="Plate", mode="fit_on_path",
                                  path="line:0"))
        assert sk.sketchTexts.last_input.fit == (sk.sketchCurves.sketchLines.item(0), True)
        assert sk.sketchTexts.last_input.along is None
        # fit spaces the glyphs itself, so no alignment/spacing is reported
        assert "align" not in out and "character_spacing" not in out

    def test_fit_on_path_definition_typo_is_matched_as_landed(self):
        # the live objectType carries the binding's misspelled 'FitOnPathTextDefintion'
        design, sk = _install_create(lines=1)
        out = _payload(st.handler(text="FIT", create=True, sketch_name="Plate", mode="fit_on_path",
                                  path="line:0"))
        assert out["definition_type"] == "adsk::fusion::FitOnPathTextDefintion"
        assert out["mode_verified"] is True

    def test_path_mode_requires_a_path(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path")
        assert res["isError"] is True and "'path' is required" in res["message"]

    def test_edge_handle_path_is_refused_before_anything_is_created(self):
        # a BRepEdge path is accepted by setAsAlongPath and then rejected at add() live, so the
        # handle never reaches createInput2
        design, sk = _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path=_EDGE_HANDLE)
        assert res["isError"] is True
        assert "sketch_project" in res["message"] and "pSketchCurve" in res["message"]
        assert sk.sketchTexts.last_input is None
        assert sk.sketchTexts.count == 0

    def test_point_path_is_refused(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="point:0")
        assert res["isError"] is True and "POINT" in res["message"]

    def test_unresolvable_path_names_the_ref(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:7")
        assert res["isError"] is True and "line:7" in res["message"]

    def test_failing_add_surfaces_the_platform_error_verbatim(self):
        texts = FakeSketchTexts(raise_on_add="2 : InternalValidationError : pSketchCurve")
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0")
        assert res["isError"] is True
        assert "InternalValidationError : pSketchCurve" in res["message"]

    @pytest.mark.parametrize("mode,path", [("multi_line", ""), ("along_path", "line:0"),
                                           ("fit_on_path", "line:0")])
    def test_a_refused_placement_creates_nothing(self, mode, path):
        # setAs* answering false is an explicit refusal: add() must never be reached, and the call
        # must report the failure rather than a text that was never placed
        texts = FakeSketchTexts(place_returns=False)
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode=mode, path=path)
        assert res["isError"] is True
        assert mode in res["message"] and "no text was placed" in res["message"]
        assert texts.add_calls == 0
        assert texts.count == 0

    def test_path_mode_noop_error_points_at_the_curve_not_the_frame(self):
        texts = FakeSketchTexts(materialize=False)
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0")
        assert res["isError"] is True and "did not materialize" in res["message"]
        assert "line:0" in res["message"]


# ── the inputs each mode has no API slot for ────────────────────────────────

class TestModeInputRefusals:
    def test_fit_on_path_refuses_align(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="fit_on_path",
                         path="line:0", align="center")
        assert res["isError"] is True
        assert "'align'" in res["message"] and "along_path" in res["message"]

    def test_fit_on_path_refuses_character_spacing(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="fit_on_path",
                         path="line:0", character_spacing=0)
        assert res["isError"] is True and "'character_spacing'" in res["message"]

    def test_path_mode_refuses_x_and_y(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0", x=10)
        assert res["isError"] is True and "'x'" in res["message"]

    def test_multi_line_refuses_a_path(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", path="line:0")
        assert res["isError"] is True and "'path'" in res["message"]

    def test_multi_line_refuses_above_path(self):
        _install_create(lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", above_path=False)
        assert res["isError"] is True and "'above_path'" in res["message"]

    def test_multi_line_still_takes_align_and_spacing(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="AB", create=True, sketch_name="Plate", align="right",
                                  character_spacing=10))
        import adsk.core
        assert (sk.sketchTexts.last_input.multiline[2]
                is adsk.core.HorizontalAlignments.RightHorizontalAlignment)
        assert sk.sketchTexts.last_input.multiline[4] == 10.0
        assert out["align"] == "right"

    def test_multi_line_defaults_match_the_previous_hard_coded_values(self):
        import adsk.core
        design, sk = _install_create()
        _payload(st.handler(text="AB", create=True, sketch_name="Plate"))
        assert (sk.sketchTexts.last_input.multiline[2]
                is adsk.core.HorizontalAlignments.LeftHorizontalAlignment)
        assert (sk.sketchTexts.last_input.multiline[3]
                is adsk.core.VerticalAlignments.BottomVerticalAlignment)
        assert sk.sketchTexts.last_input.multiline[4] == 0.0

    def test_non_numeric_character_spacing_is_refused(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="Plate", character_spacing="wide")
        assert res["isError"] is True and "character_spacing" in res["message"]


# ── angle + flips: set on the input, published as REQUESTED ─────────────────

class TestFormatting:
    def test_angle_is_converted_from_degrees_to_radians(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", angle_deg=45))
        assert sk.sketchTexts.last_input.angle == pytest.approx(math.radians(45))
        # the payload publishes the REQUESTED degrees, not a read-back of the created text
        assert out["requested"]["angle_deg"] == 45.0

    def test_flips_are_set_and_reported(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", flip_h=True,
                                  flip_v=True))
        assert sk.sketchTexts.last_input.isHorizontalFlip is True
        assert sk.sketchTexts.last_input.isVerticalFlip is True
        assert out["requested"] == {"flip_h": True, "flip_v": True}

    def test_untouched_formatting_is_not_reported(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate"))
        assert "requested" not in out
        assert sk.sketchTexts.last_input.angle == 0.0

    def test_note_marks_the_requested_values_as_unverified(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", angle_deg=30))
        assert "REQUESTED" in out["note"] and "were not read back off the created text" in out["note"]

    def test_angle_that_does_not_take_is_an_error(self):
        # a SWIG proxy accepts an assignment to a name it does not define; only the read-back
        # catches it, and a create that silently ignored the rotation must not report success
        class _Deaf(FakeTextInput):
            @property
            def angle(self):
                return 0.0
            @angle.setter
            def angle(self, v):
                pass
        texts = FakeSketchTexts()
        texts.createInput2 = lambda text, height: setattr(
            texts, "last_input", _Deaf(text, height)) or texts.last_input
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", angle_deg=45)
        assert res["isError"] is True and "angle_deg" in res["message"]
        assert sk.sketchTexts.count == 0

    def test_non_numeric_angle_is_refused(self):
        _install_create()
        res = st.handler(text="A", create=True, sketch_name="Plate", angle_deg="sideways")
        assert res["isError"] is True and "DEGREES" in res["message"]


# ── the definition read-back decides whether the right mode landed ──────────

class TestDefinitionReadBack:
    def test_a_different_mode_landing_is_an_error_not_a_false_ok(self):
        texts = FakeSketchTexts(definition_type="adsk::fusion::MultiLineTextDefinition")
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0")
        assert res["isError"] is True
        assert "multi_line" in res["message"] and "WAS created" in res["message"]

    def test_the_wrong_mode_message_names_the_texts_own_delete_index(self):
        # sketch_delete_entity takes target='text:<index>' and the created text is the LAST in
        # sketchTexts, so the refusal hands over the real number - a placeholder or an off-by-one
        # would send the caller at a text that is not the one this call created.
        texts = FakeSketchTexts(initial=3, definition_type="adsk::fusion::MultiLineTextDefinition")
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0")
        assert "sketch_delete_entity(sketch_name='Plate', target='text:3')" in res["message"]
        assert "<index>" not in res["message"]
        assert "not text" not in res["message"]
        assert "undo in Fusion" not in res["message"]

    def test_an_unrecognised_definition_is_published_without_a_claim(self):
        texts = FakeSketchTexts(definition_type="adsk::fusion::SomethingElse")
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0"))
        assert out["mode_verified"] is False
        assert out["definition_type"] == "adsk::fusion::SomethingElse"

    def test_placement_is_published_from_the_definition_not_the_request(self):
        # asked for center/above; the definition says right/below - the payload must report what
        # the definition says, or the read-back is just an echo of the request
        import adsk.core
        texts = FakeSketchTexts(placement_override={
            "horizontalAlignment": adsk.core.HorizontalAlignments.RightHorizontalAlignment,
            "isAbovePath": False})
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0", align="center", above_path=True))
        assert out["align"] == "right" and out["above_path"] is False

    def test_unreadable_placement_is_none_and_falls_back_to_requested(self):
        texts = FakeSketchTexts(blind_definition=True)
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0", align="center", character_spacing=25,
                                  above_path=False))
        assert out["above_path"] is None and out["align"] is None
        assert out["character_spacing"] is None
        assert out["requested"] == {"above_path": False, "align": "center",
                                    "character_spacing": 25.0}

    def test_fit_on_path_reads_back_only_above_path(self):
        design, sk = _install_create(lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="fit_on_path",
                                  path="line:0", above_path=False))
        assert out["above_path"] is False
        assert "align" not in out and "character_spacing" not in out

    def test_an_alignment_that_matches_no_member_is_unreadable(self):
        texts = FakeSketchTexts(placement_override={"horizontalAlignment": "sideways"})
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0"))
        assert out["align"] is None and out["requested"]["align"] == "left"

    def test_note_omits_the_definition_clause_when_it_did_not_read(self):
        texts = FakeSketchTexts(definition_type="")
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0"))
        assert "definition None" not in out["note"]
        assert "sketchTexts 0 -> 1" in out["note"]

    def test_missing_definition_is_not_a_failure(self):
        texts = FakeSketchTexts(definition_type="")
        design, sk = _install_create(texts=texts, lines=1)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0"))
        assert out["mode_verified"] is False and out["definition_type"] is None


# ── create-only inputs on the edit path ─────────────────────────────────────

class TestCreateOnlyGuard:
    def test_mode_without_create_is_refused(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", mode="along_path")
        assert res["isError"] is True
        assert "'mode'" in res["message"] and "create=true" in res["message"]

    def test_angle_without_create_is_refused_rather_than_ignored(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", angle_deg=15)
        assert res["isError"] is True and "'angle_deg'" in res["message"]

    def test_flip_false_still_counts_as_supplied(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", flip_h=False)
        assert res["isError"] is True and "'flip_h'" in res["message"]

    def test_position_without_create_is_refused_like_the_other_create_inputs(self):
        # x/y default to None, so a supplied one is detectable - refusing flip_h but silently
        # dropping x=10 would be the asymmetry
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", x=10)
        assert res["isError"] is True and "'x'" in res["message"]

    def test_height_is_no_longer_a_create_only_input(self):
        # The edit path RESIZES now (heightParameter takes a write and the glyphs follow it), so
        # 'height' must NOT be named by the create-only refusal - naming it would refuse the very
        # capability the edit path grew.
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'", height_cm=0.8)])])])
        res = st.handler(text="X", height=20)
        assert res["isError"] is False, res
        assert "height" not in st._CREATE_ONLY

    def test_the_refusal_sentence_no_longer_claims_an_edit_changes_nothing_else(self):
        # the wire contract the resize falsifies: an edit changes the string, its font AND its size
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        res = st.handler(text="X", x=10)
        assert res["isError"] is True
        assert "and nothing else" not in res["message"]
        assert "'height'" in res["message"]

    def test_a_zero_height_on_the_edit_path_is_still_a_supplied_value(self):
        # boundary: 0 is falsy but supplied - _given must not read it as "not passed" and skip the
        # guard, which would send a zero height at the parameter
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'", height_cm=0.8)])])])
        res = st.handler(text="X", height=0)
        assert res["isError"] is True and "'height'" in res["message"]

    def test_units_stays_exempt(self):
        # 'units' keeps a non-None default a caller cannot be told apart from, and it only scales
        # 'height' - so it is not itself evidence of a create-shaped request
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X", units="in"))
        assert out["changed_count"] == 1

    def test_an_edit_that_names_no_height_is_untouched(self):
        # the other side of the boundary: height defaults to None now, and a plain edit must not
        # start refusing itself
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X"))
        assert out["changed_count"] == 1 and out["set"] is True

    def test_plain_edit_is_unaffected(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X"))
        assert out["changed_count"] == 1


# ── 'height' on the EDIT path: the resize, and the two reads that gate it ───
# heightParameter.value takes a write on an existing text and the glyph geometry follows it
# proportionally (measured live). Both reads matter: the value alone passes over a parameter that
# reports the new number while nothing moved, and the box alone cannot say which number landed.

class TestEditResize:
    def _one(self, **kw):
        sk = FakeSketch("Plate", [FakeText("'OLD'", **kw)])
        _install([FakeComp("Root", [sk])])
        return sk.sketchTexts.item(0)

    def test_the_requested_height_is_written_to_the_parameter_in_cm(self):
        text = self._one(height_cm=0.8)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        assert text.heightParameter.value == 0.4          # 4 mm -> 0.4 cm
        assert out["changed"][0]["height"] == 4.0
        assert out["changed"][0]["height_before"] == 8.0

    def test_the_box_is_re_measured_after_the_resize_not_before(self):
        # halving the height halves the box, so 16 x 4 mm is the box AFTER the write; 32 x 8 would
        # be the pre-resize read published as the result of the resize.
        self._one(height_cm=0.8)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        rec = out["changed"][0]
        assert rec["measured_width"] == 16.0 and rec["measured_height"] == 4.0

    def test_a_value_that_does_not_land_errors_naming_both_numbers(self):
        self._one(height_cm=0.8, height_lands=False)
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "8.0 mm" in res["message"] and "4.0 mm" in res["message"]

    def test_a_value_that_lands_over_a_frozen_box_is_an_error(self):
        # THE honest gate: heightParameter reports the requested number while the glyphs never
        # moved, so the resize did not happen and reporting it would be a false ok.
        self._one(height_cm=0.8, box_follows=False)
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
        assert res["isError"] is True
        assert "did not resize" in res["message"] and "unchanged from" in res["message"]

    def test_a_height_that_did_not_change_is_not_expected_to_move_the_box(self):
        # asking for the size the text already carries is a legal no-op - a box that sat still there
        # is the correct outcome, and erroring on it would refuse it
        self._one(height_cm=0.8, box_follows=False)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=8, units="mm"))
        assert out["changed"][0]["height"] == 8.0

    def test_an_unreadable_box_makes_no_claim_either_way(self):
        # the box proves nothing, so the value read-back alone carries the verdict rather than the
        # resize being refused over a read that never happened
        self._one(height_cm=0.8, box=False)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        rec = out["changed"][0]
        assert rec["height"] == 4.0 and "measured_width" not in rec

    def test_a_height_that_will_not_read_back_refuses_rather_than_claiming_the_resize(self):
        self._one(height_cm=0.8, height_readable=False)
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
        assert res["isError"] is True and "cannot be confirmed" in res["message"]

    def test_a_setter_fusion_refuses_names_the_sketch_and_the_raise(self):
        self._one(height_cm=0.8, height_error="3 : invalid height")
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
        assert res["isError"] is True
        assert "Could not set the height" in res["message"]
        assert "Plate" in res["message"] and "invalid height" in res["message"]

    def test_a_failed_resize_discloses_the_string_it_already_wrote(self):
        # the string lands BEFORE the resize is checked, so a bare "resize failed" would read as
        # nothing having happened to that text
        self._one(height_cm=0.8, height_lands=False)
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
        assert "string WAS set to 'NEW'" in res["message"]

    def test_an_edit_naming_no_height_leaves_the_parameter_alone(self):
        text = self._one(height_cm=0.8)
        out = _payload(st.handler(text="NEW", sketch_name="Plate"))
        assert text.heightParameter.value == 0.8
        assert "height" not in out["changed"][0]

    def test_a_non_numeric_height_is_refused_before_any_text_is_touched(self):
        text = self._one(height_cm=0.8)
        res = st.handler(text="NEW", sketch_name="Plate", height="tall")
        assert res["isError"] is True and "must be a number" in res["message"]
        assert text.textParameter.expression == "'OLD'"     # nothing written

    def test_an_unknown_unit_is_refused_before_any_text_is_touched(self):
        text = self._one(height_cm=0.8)
        res = st.handler(text="NEW", sketch_name="Plate", height=4, units="furlongs")
        assert res["isError"] is True and "Unknown units" in res["message"]
        assert text.textParameter.expression == "'OLD'"

    def test_the_note_names_the_height_fields_it_published(self):
        self._one(height_cm=0.8)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        assert "height_before" in out["note"] and "measured_width" in out["note"]

    def test_the_note_does_not_name_box_keys_it_did_not_publish(self):
        # the box would not read, so no entry carries measured_width/measured_height - a note naming
        # them anyway sends the caller looking for fields that are not there
        self._one(height_cm=0.8, box=False)
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        assert "height_before" in out["note"]
        assert "measured_width" not in out["note"]

    def test_the_units_input_no_longer_claims_it_is_create_only(self):
        # the wire contract the resize falsifies: 'units' scales the EDIT height too and names the
        # unit every reported number comes back in, so a caller trusting "(create only)" would ship
        # a 0.25 mm text meaning inches
        desc = st.tool.to_dict()["inputSchema"]["properties"]["units"]["description"]
        assert "create only" not in desc
        assert "edit" in desc.lower()

    def test_a_value_difference_at_the_tolerance_passes_and_one_past_it_errors(self):
        # The exact boundary of the 1e-6 cm band: 2e-6 - 1e-6 is EXACT in binary floating point, so
        # the equal case really sits ON the boundary rather than rounding under it.
        for skew, is_error in ((1e-6, False), (2e-6, True)):
            self._one(height_cm=0.8, height_skew=skew)
            res = st.handler(text="NEW", sketch_name="Plate", height=1e-6, units="cm")
            assert res["isError"] is is_error, skew

    def test_a_box_that_moved_by_exactly_the_band_did_not_move(self):
        # The other comparison's boundary. A 1e-6 cm box puts the relative band at its 1 cm floor,
        # so the band is exactly 1e-6 - and 2e-6 - 1e-6 is EXACT in binary floating point, so the
        # equal case really sits ON it. A box moving by the band itself is read noise, not a resize,
        # so the value-landed-but-nothing-moved refusal still fires.
        for delta, is_error in ((1e-6, True), (2e-6, False)):
            self._one(height_cm=0.8, box=(1e-6, 1e-6), box_delta=delta)
            res = st.handler(text="NEW", sketch_name="Plate", height=4, units="mm")
            assert res["isError"] is is_error, delta

    def test_every_matched_text_is_resized_not_just_the_first(self):
        sk = FakeSketch("Plate", [FakeText("'A'", height_cm=0.8), FakeText("'B'", height_cm=1.0)])
        _install([FakeComp("Root", [sk])])
        out = _payload(st.handler(text="NEW", sketch_name="Plate", height=4, units="mm"))
        assert [t.heightParameter.value for t in sk.sketchTexts] == [0.4, 0.4]
        assert [r["height_before"] for r in out["changed"]] == [8.0, 10.0]


# ── font_name: applied on both paths, verified by reading it back ───────────

_BAD_FONT = "ZzNoSuchFont"
# the sentence Fusion raises for a font name it does not know, on add() and on the setter alike
_FONT_SENTENCE = "3 : invalid input font name"


class TestFontOnCreate:
    def test_font_is_set_on_the_input_and_read_back_off_the_landed_text(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial"))
        assert sk.sketchTexts.last_input.fontName == "Arial"
        assert out["font"] == "Arial"
        assert "requested" not in out

    def test_a_different_font_landing_is_an_error_not_a_false_ok(self):
        # the text landed with a font other than the one asked for: reporting created:true with the
        # requested name would be an echo, not a read-back
        texts = FakeSketchTexts(landed_font="Courier New")
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial")
        assert res["isError"] is True
        assert "'Arial'" in res["message"] and "'Courier New'" in res["message"]
        assert "WAS created" in res["message"]

    def test_the_font_mismatch_message_names_the_texts_own_delete_index(self):
        # the created text is the LAST in sketchTexts, so the message can hand over the exact
        # target sketch_delete_entity takes instead of sending the caller to the Fusion UI.
        texts = FakeSketchTexts(initial=2, landed_font="Courier New")
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial")
        assert "sketch_delete_entity(sketch_name='Plate', target='text:2')" in res["message"]
        assert "undo in Fusion" not in res["message"]

    def test_unreadable_font_is_published_none_and_falls_back_to_requested(self):
        texts = FakeSketchTexts(blind_font=True)
        design, sk = _install_create(texts=texts)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial"))
        assert out["font"] is None
        assert out["requested"]["font"] == "Arial"
        assert "REQUESTED" in out["note"]

    def test_refused_font_carries_the_platform_sentence_and_the_offending_name(self):
        texts = FakeSketchTexts(font_raises=_FONT_SENTENCE)
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", font_name=_BAD_FONT)
        assert res["isError"] is True
        assert _FONT_SENTENCE in res["message"] and _BAD_FONT in res["message"]
        # no font-name validation API exists, so the message must not pretend to list legal names
        assert "no API lists the legal names" in res["message"]
        assert texts.count == 0

    def test_a_non_font_add_failure_is_not_blamed_on_the_font(self):
        # the font is named as what was applied, but only a platform sentence about the FONT earns
        # the font advice - otherwise the tool would guess the cause
        texts = FakeSketchTexts(raise_on_add="2 : InternalValidationError : pSketchCurve")
        design, sk = _install_create(texts=texts, lines=1)
        res = st.handler(text="A", create=True, sketch_name="Plate", mode="along_path",
                         path="line:0", font_name="Arial")
        assert res["isError"] is True
        assert "pSketchCurve" in res["message"] and "'Arial'" in res["message"]
        assert "no API lists the legal names" not in res["message"]

    def test_a_case_variant_landing_is_a_mismatch_not_a_match(self):
        # Fusion normalizes no case, so a text reporting 'arial' when 'Arial' was asked for is a
        # real disagreement - comparing case-insensitively would wave it through
        texts = FakeSketchTexts(landed_font="arial")
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial")
        assert res["isError"] is True
        assert "'Arial'" in res["message"] and "'arial'" in res["message"]

    def test_an_empty_font_read_back_is_no_name_at_all(self):
        # "" is not a font name: publishing it as the confirmed font would claim a read-back that
        # says nothing, so it is None and the requested name is labelled as unverified
        texts = FakeSketchTexts(landed_font="")
        design, sk = _install_create(texts=texts)
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate", font_name="Arial"))
        assert out["font"] is None
        assert out["requested"]["font"] == "Arial"

    def test_a_case_variant_font_is_refused_by_fusion_not_normalized_here(self):
        # font names are case-sensitive and nothing normalizes them: 'arial' raises the same
        # sentence, which the tool carries with the name that caused it
        texts = FakeSketchTexts(font_raises=_FONT_SENTENCE)
        design, sk = _install_create(texts=texts)
        res = st.handler(text="A", create=True, sketch_name="Plate", font_name="arial")
        assert res["isError"] is True
        assert _FONT_SENTENCE in res["message"] and "'arial'" in res["message"]

    def test_no_font_given_publishes_no_font_and_touches_the_input(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="A", create=True, sketch_name="Plate"))
        assert "font" not in out and "requested" not in out
        assert sk.sketchTexts.last_input.fontName is None


class TestFontOnEdit:
    def test_edit_applies_the_font_and_reports_the_read_back(self):
        text = FakeText("'old'", font="Courier New")
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        out = _payload(st.handler(text="X", font_name="Arial"))
        assert text.fontName == "Arial"
        assert out["changed"][0]["font"] == "Arial"
        assert out["changed"][0]["after"] == "X"
        assert "Arial" in out["note"]

    def test_refused_font_leaves_the_string_untouched(self):
        # the font goes on FIRST, so a refusal aborts before the expression is rewritten
        text = FakeText("'old'", font_error=_FONT_SENTENCE)
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        res = st.handler(text="X", font_name=_BAD_FONT)
        assert res["isError"] is True
        assert _FONT_SENTENCE in res["message"] and _BAD_FONT in res["message"]
        assert text.textParameter.expression == "'old'"
        assert "No sketch text was changed." in res["message"]

    def test_a_partial_run_names_what_was_already_updated(self):
        good = FakeText("'a'")
        bad = FakeText("'b'", font_error=_FONT_SENTENCE)
        _install([FakeComp("Root", [FakeSketch("S1", [good]), FakeSketch("S2", [bad])])])
        res = st.handler(text="X", font_name=_BAD_FONT)
        assert res["isError"] is True
        assert "1 sketch text(s) earlier in this call were already updated ('S1')" in res["message"]
        assert good.textParameter.expression == "'X'"
        assert bad.textParameter.expression == "'b'"

    def test_a_font_that_does_not_take_is_an_error(self):
        # the setter accepts the assignment and the text keeps its old font - only the read-back
        # catches it, and a silent no-op must never report success
        text = FakeText("'old'", font="Arial", font_lands=False)
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        res = st.handler(text="X", font_name="Courier New")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "'Arial'" in res["message"] and "'Courier New'" in res["message"]
        assert text.textParameter.expression == "'old'"

    def test_a_font_that_will_not_read_is_published_as_none(self):
        # the entry carries the READ-BACK, so a font that cannot be read is None - never the
        # requested name echoed back as if it had been confirmed
        text = FakeText("'old'", font_readable=False)
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        out = _payload(st.handler(text="X", font_name="Arial"))
        assert out["changed"][0]["font"] is None
        assert out["changed"][0]["after"] == "X"

    def test_a_case_variant_read_back_is_a_font_that_did_not_take(self):
        # nothing normalizes case, so a text still reporting 'arial' after 'Arial' was set kept its
        # own font - a case-insensitive compare would report that silent no-op as success
        text = FakeText("'old'", font="arial", font_lands=False)
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        res = st.handler(text="X", font_name="Arial")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "'arial'" in res["message"] and "'Arial'" in res["message"]

    def test_an_empty_font_read_back_is_no_name_at_all(self):
        # the setter accepts the name, the text reports "" - that confirms nothing, so the entry
        # publishes None rather than an empty string dressed up as a read-back
        text = FakeText("'old'", font="", font_lands=False)
        _install([FakeComp("Root", [FakeSketch("S", [text])])])
        out = _payload(st.handler(text="X", font_name="Arial"))
        assert out["changed"][0]["font"] is None
        assert out["changed"][0]["after"] == "X"

    def test_a_landed_font_with_a_failed_string_write_is_reported_as_partial(self):
        # the font landed on this text and the string did not: 'failed' on its own would read as
        # no effect at all, so the message must say the font changed
        good = FakeText("'a'")
        bad = FakeText("'b'")

        class _Locked:
            @property
            def expression(self):
                return "'b'"
            @expression.setter
            def expression(self, v):
                raise RuntimeError("locked")
        bad.textParameter = _Locked()
        _install([FakeComp("Root", [FakeSketch("S1", [good]), FakeSketch("S2", [bad])])])
        res = st.handler(text="X", font_name="Arial")
        assert res["isError"] is True
        assert "locked" in res["message"]
        assert "font WAS changed to 'Arial'" in res["message"]
        assert "1 sketch text(s) earlier in this call were already updated ('S1')" in res["message"]
        assert bad.fontName == "Arial" and bad.textParameter.expression == "'b'"

    def test_font_without_create_is_not_refused_as_a_create_only_input(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X", font_name="Arial"))
        assert out["changed_count"] == 1

    def test_no_font_given_leaves_the_edit_payload_as_it_was(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X"))
        assert "font" not in out["changed"][0]
        # the applied-font clause is absent; the read-back pointer names 'font' as a field
        # sketch_get returns, which is true whether or not this call set one
        assert "after applying" not in out["note"]


class TestReadBackPointer:
    """A written text is re-readable as its own entity record, so both paths must point at the read
    instead of leaving a screenshot as the only way to check a label."""

    def test_create_note_points_at_the_sketch_get_read_back(self):
        _install_create()
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate"))
        assert "sketch_get(sketch_name=..., include_entities=true)" in out["note"]
        assert "'text:<i>'" in out["note"]

    def test_edit_note_points_at_the_sketch_get_read_back(self):
        _install([FakeComp("Root", [FakeSketch("S", [FakeText("'a'")])])])
        out = _payload(st.handler(text="X"))
        assert "sketch_get(sketch_name=..., include_entities=true)" in out["note"]


class TestCreateReportsTheMeasuredWidth:
    """Nothing tells a caller how wide a string will run before it exists, so a create that ran past
    the face it had to fit cost a delete-text, a delete-emboss and a rebuild. The create reports the
    landed text's own box, which is the number to check the label against."""

    def test_the_width_comes_from_the_box_not_from_height_times_length(self):
        # the default rig's box is 18.8 cm wide against a 3-character string at 5 mm: an estimate
        # would answer 15 mm, the box answers 188
        design, sk = _install_create()
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", units="mm"))
        assert out["measured_width"] == 188.0
        assert out["measured_height"] == 8.0

    def test_the_extents_are_scaled_into_the_callers_units(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", units="cm"))
        assert out["measured_width"] == 18.8 and out["measured_height"] == 0.8

    def test_a_box_that_does_not_start_at_the_origin_measures_its_span(self):
        # width is the SPAN, not the far corner: a text placed away from (0,0) is not wider for it
        design, sk = _install_create(texts=FakeSketchTexts(bbox=(10.0, 5.0, 12.5, 5.8)))
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", units="mm"))
        assert out["measured_width"] == 25.0 and out["measured_height"] == 8.0

    def test_the_note_says_what_was_measured_and_in_which_units(self):
        design, sk = _install_create()
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", units="mm"))
        assert "boundingBox" in out["note"] and "in mm" in out["note"]

    def test_a_text_with_no_readable_box_publishes_nothing_rather_than_a_zero(self):
        design, sk = _install_create(texts=FakeSketchTexts(bbox=None))
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate"))
        assert out["created"] is True
        assert "measured_width" not in out and "measured_height" not in out
        assert "boundingBox" not in out["note"]

    def test_a_path_mode_create_reports_it_too(self):
        design, sk = _install_create(lines=1)
        out = _payload(st.handler(text="LBL", create=True, sketch_name="Plate", mode="along_path",
                                  path="line:0", units="mm"))
        assert out["measured_width"] == 188.0


class TestMeasuredExtents:
    """The read on its own: the two extents in display units, or nothing at all when any part of the
    box will not answer - a half-read box must not publish a width."""

    def _box(self, lo, hi):
        return types.SimpleNamespace(boundingBox=types.SimpleNamespace(minPoint=lo, maxPoint=hi))

    def _pt(self, x, y):
        return types.SimpleNamespace(x=x, y=y, z=0.0)

    def test_it_scales_both_spans(self):
        text = self._box(self._pt(1.0, 2.0), self._pt(4.0, 2.5))
        assert st._measured_extents(text, 10.0) == (30.0, 5.0)

    def test_no_box_at_all_answers_nothing(self):
        assert st._measured_extents(types.SimpleNamespace(name="T"), 10.0) == (None, None)

    def test_a_missing_corner_answers_nothing(self):
        text = types.SimpleNamespace(
            boundingBox=types.SimpleNamespace(minPoint=self._pt(0.0, 0.0)))
        assert st._measured_extents(text, 10.0) == (None, None)

    def test_a_coordinate_that_is_not_a_number_answers_nothing(self):
        # an adsk mock hands back a truthy child object for anything unmodeled; subtracting two of
        # those is not a width
        text = self._box(self._pt(0.0, 0.0), self._pt(object(), 1.0))
        assert st._measured_extents(text, 10.0) == (None, None)

    def test_a_boolean_coordinate_is_not_a_number_either(self):
        text = self._box(self._pt(False, 0.0), self._pt(True, 1.0))
        assert st._measured_extents(text, 10.0) == (None, None)


@pytest.fixture
def wired_create():
    """The create path's design - one component holding a sketch named 'Plate'."""
    return _install([MakeComp(name="Root", sketches=[FakeSketchForCreate("Plate")])])


@pytest.fixture
def wired_edit():
    """The edit path's design - one component holding a sketch named 'Plate' carrying one text."""
    return _install([FakeComp("Root", [FakeSketch("Plate", [FakeText("'a'")])])])


class TestNamedSketchMiss:
    def test_the_no_text_miss_reports_the_name_the_walk_searched_for(self, wired_edit):
        # the handler strips the name before resolving it, so the stripped form is what was
        # searched - and the message's own "that exact name" is only true of that form.
        res = st.handler(text="X", sketch_name="  Ghost  ")
        assert res["isError"] is True
        assert "in a sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_the_index_no_match_reports_the_name_the_walk_searched_for(self, wired_edit):
        # the sketch RESOLVED here (the handler strips), so the trailing name must be the stripped
        # one too - the raw form names a sketch nothing ever looked for.
        res = st.handler(text="X", sketch_name="  Plate  ", index=5)
        assert res["isError"] is True
        assert "in sketch 'Plate'" in res["message"]
        assert "'  Plate  '" not in res["message"]

    def test_reports_the_name_the_walk_searched_for_not_the_raw_input(self, wired_create):
        # the name is STRIPPED before the walk, so echoing the raw input quotes a name nothing
        # ever looked for - and the caller retries against a sketch that was never missing.
        res = st.handler(text="LBL", create=True, sketch_name="  Ghost  ")
        assert res["isError"] is True
        assert "No sketch named 'Ghost'" in res["message"]
        assert "'  Ghost  '" not in res["message"]

    def test_the_miss_still_lists_what_is_there(self, wired_create):
        res = st.handler(text="LBL", create=True, sketch_name="Ghost")
        assert "Available: Plate" in res["message"]


class TestParameterBinding:
    """'parameter' makes the string FOLLOW a user parameter: the text parameter's expression holds
    the BARE parameter name, where a literal is the same string QUOTED. param_set on that parameter
    then restrings every text bound to it."""

    def _one_text(self, expr="'10'"):
        text = FakeText(expr)
        _install_with_parameters([FakeComp("Root", [FakeSketch("Plate", [text])])],
                                 ["weight_text"])
        return text

    def test_parameter_binding_is_publicly_usable_without_text(self):
        assert "text" not in (st.tool.to_dict()["inputSchema"].get("required") or [])
        text = self._one_text()
        out = _payload(st.handler(sketch_name="Plate", parameter="weight_text"))
        assert out["bound_to"] == "weight_text"
        assert text.textParameter.expression == "weight_text"

    def test_missing_text_and_parameter_are_refused(self):
        self._one_text()
        res = st.handler(sketch_name="Plate")
        assert res["isError"] is True and "text' or 'parameter" in res["message"]
    def test_the_expression_becomes_the_bare_parameter_name(self):
        text = self._one_text()
        out = _payload(st.handler(sketch_name="Plate", parameter="weight_text"))
        assert text.textParameter.expression == "weight_text"       # bare, not "'weight_text'"
        assert out["bound_to"] == "weight_text"
        assert out["changed"][0]["expression"] == "weight_text"
        assert out["changed"][0]["before"] == "10"
        assert "text" not in out

    def test_a_binding_that_did_not_take_is_an_error(self):
        class _StuckParam:
            """A text parameter that accepts the assignment and keeps its own expression - the
            accept-and-ignore only a read-back catches."""
            def __init__(self, expr):
                self._expr = expr

            @property
            def expression(self):
                return self._expr

            @expression.setter
            def expression(self, value):
                pass

        text = self._one_text()
        text.textParameter = _StuckParam("'10'")
        res = st.handler(sketch_name="Plate", parameter="weight_text")
        assert res["isError"] is True and "did not take" in res["message"]

    def test_the_lookup_runs_through_the_shared_parameter_resolver(self, monkeypatch):
        # The bite for the ONE-home claim: stub the shared symbol alone. A re-rolled local
        # allParameters.itemByName would ignore this and still find the parameter.
        text = self._one_text()
        monkeypatch.setattr(st._param_common, "_find_parameter", lambda design, name: None)
        res = st.handler(sketch_name="Plate", parameter="weight_text")
        assert res["isError"] is True and "No parameter named 'weight_text'" in res["message"]
        assert text.textParameter.expression == "'10'"

    def test_an_unknown_parameter_is_refused_before_any_text_is_touched(self):
        text = self._one_text()
        res = st.handler(sketch_name="Plate", parameter="nope")
        assert res["isError"] is True and "No parameter named 'nope'" in res["message"]
        assert text.textParameter.expression == "'10'"

    def test_a_length_parameter_is_refused_before_the_text_is_touched(self):
        # MEASURED LIVE: Fusion ACCEPTS a length parameter in a text parameter's expression and
        # renders its value as the label, so only the unit read refuses it - without this the tool
        # reports set:true on a label that now follows a dimension.
        text = FakeText("'10'")
        _install_with_parameters([FakeComp("Root", [FakeSketch("Plate", [text])])],
                                 ["WallT"], unit="mm")
        res = st.handler(sketch_name="Plate", parameter="WallT")
        assert res["isError"] is True
        assert "'WallT' is a mm parameter" in res["message"]
        assert text.textParameter.expression == "'10'"

    def test_text_and_parameter_together_are_refused(self):
        self._one_text()
        res = st.handler(text="25", sketch_name="Plate", parameter="weight_text")
        assert res["isError"] is True and "two different string sources" in res["message"]
    def test_whitespace_text_and_parameter_are_refused_without_mutation(self):
        text = self._one_text()
        res = st.handler(text="   ", sketch_name="Plate", parameter="weight_text")
        assert res["isError"] is True and "two different string sources" in res["message"]
        assert text.textParameter.expression == "'10'"

    def test_empty_text_is_legacy_parameter_only_binding(self):
        text = self._one_text()
        out = _payload(st.handler(text="", sketch_name="Plate", parameter="weight_text"))
        assert out["bound_to"] == "weight_text"
        assert text.textParameter.expression == "weight_text"
    def test_parameter_on_a_create_names_the_two_call_route(self):
        self._one_text()
        res = st.handler(text="LBL", create=True, sketch_name="Plate", parameter="weight_text")
        assert res["isError"] is True and "cannot ride a create" in res["message"]
