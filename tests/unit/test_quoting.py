"""Unit tests for the text-parameter quoting helpers.

``sketch_set_text.py`` and ``cam_set_nc_comment.py`` each expose a ``_quote`` /
``_unquote`` pair for Fusion's quoted text-parameter expressions ('foo'): the
CAM tool through ``_cam_common``'s ``quote_expression`` / ``unquote_expression``
(the codec its read-back compare runs on), the sketch tool through
``_sketch_detail.unquote_text`` plus its own quoting half. Quoting bugs corrupt
user text silently (a stray quote breaks the expression, or escaping is lost on
round-trip), so these get round-trip and edge coverage. The two are tested
together to confirm they behave identically — if they ever diverge, that's a
finding.
"""

import pytest

from conftest import load_tool

sketch_text = load_tool("sketch_set_text")
nc_comment = load_tool("cam_set_nc_comment")

MODULES = [pytest.param(sketch_text, id="sketch_set_text"),
           pytest.param(nc_comment, id="cam_set_nc_comment")]


@pytest.mark.parametrize("mod", MODULES)
class TestUnquote:
    def test_strips_single_quotes(self, mod):
        assert mod._unquote("'hello'") == "hello"

    def test_strips_double_quotes(self, mod):
        assert mod._unquote('"hello"') == "hello"

    def test_unquoted_string_passes_through(self, mod):
        assert mod._unquote("hello") == "hello"

    def test_none_passes_through(self, mod):
        assert mod._unquote(None) is None

    def test_mismatched_quotes_not_stripped(self, mod):
        # "'hello\"" — first/last differ, so it is NOT a quoted string.
        assert mod._unquote("'hello\"") == "'hello\""

    def test_single_char_not_treated_as_quoted(self, mod):
        # A lone quote is length 1 — must not index out of range or strip to "".
        assert mod._unquote("'") == "'"


@pytest.mark.parametrize("mod", MODULES)
class TestQuote:
    def test_wraps_in_single_quotes(self, mod):
        assert mod._quote("hello") == "'hello'"

    def test_escapes_inner_single_quote(self, mod):
        # A name with an apostrophe must be escaped so the expression stays valid.
        assert mod._quote("O'Brien") == "'O\\'Brien'"

    def test_empty_string(self, mod):
        assert mod._quote("") == "''"


@pytest.mark.parametrize("mod", MODULES)
class TestRoundTrip:
    @pytest.mark.parametrize("text", ["plain", "with space", "Roughing-01", "", "123"])
    def test_quote_then_unquote_recovers_text(self, mod, text):
        # For text without quote characters, quote->unquote must be the identity.
        assert mod._unquote(mod._quote(text)) == text
