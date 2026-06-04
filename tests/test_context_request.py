from ghcr.context_request import (
    extract_definition,
    module_to_path,
    parse_context_requests,
    render_referenced_context,
)
from ghcr.models import ContextRequest, ReferencedSnippet


# -- parse_context_requests -------------------------------------------------
def test_parse_maps_fields():
    raw = '{"requests":[{"symbol":"BaseConnector","module_hint":"db.base","reason":"subclassed"}]}'
    assert parse_context_requests(raw) == [
        ContextRequest(symbol="BaseConnector", module_hint="db.base", reason="subclassed")
    ]


def test_parse_strips_fences_and_prose():
    raw = 'Here is what I need:\n```json\n{"requests":[{"symbol":"foo"}]}\n```\n'
    out = parse_context_requests(raw)
    assert [c.symbol for c in out] == ["foo"]
    assert out[0].module_hint == "" and out[0].reason == ""


def test_parse_skips_junk_and_symbolless_elements():
    raw = '{"requests":["junk", {"reason":"no symbol"}, {"symbol":"  "}, {"symbol":"keep"}]}'
    assert [c.symbol for c in parse_context_requests(raw)] == ["keep"]


def test_parse_unparseable_or_empty_returns_empty_list():
    assert parse_context_requests("the model rambled, no json here") == []
    assert parse_context_requests("") == []
    assert parse_context_requests('{"requests":[]}') == []


def test_parse_never_raises_on_wrong_shape():
    assert parse_context_requests('{"requests": "not a list"}') == []
    assert parse_context_requests('[1,2,3]') == []


# -- module_to_path ---------------------------------------------------------
def test_module_to_path_dotted_becomes_py_path():
    assert module_to_path("bingo.backend.connectors.base") == "bingo/backend/connectors/base.py"


def test_module_to_path_passes_through_explicit_paths():
    assert module_to_path("pkg/mod.py") == "pkg/mod.py"
    assert module_to_path("base.py") == "base.py"


def test_module_to_path_parses_import_statements():
    assert module_to_path("from x.y import Z") == "x/y.py"
    assert module_to_path("import x.y") == "x/y.py"


def test_module_to_path_none_for_unresolvable():
    assert module_to_path("") is None
    assert module_to_path("   ") is None
    assert module_to_path("BareSymbol") is None  # no dot, no slash -> let search handle it


# -- extract_definition -----------------------------------------------------
def test_extract_class_block_stops_at_dedent():
    text = (
        "import os\n"
        "\n"
        "class BaseConnector:\n"
        "    def close(self):\n"
        "        self._c = None\n"
        "\n"
        "NEXT = 1\n"
    )
    out = extract_definition(text, "BaseConnector")
    assert "class BaseConnector:" in out
    assert "def close" in out
    assert "NEXT = 1" not in out


def test_extract_def_block():
    text = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    out = extract_definition(text, "foo")
    assert "def foo" in out and "return 1" in out
    assert "bar" not in out


def test_extract_module_level_assignment():
    text = "X = 1\nFilterParam = namedtuple('FilterParam', 'col op val')\nY = 2\n"
    out = extract_definition(text, "FilterParam")
    assert "FilterParam = namedtuple" in out
    assert "Y = 2" not in out


def test_extract_fallback_window_for_mere_usage():
    text = "a = 1\nb = 2\nresult = apply(SOMETHING, b)\nc = 3\n"
    out = extract_definition(text, "SOMETHING")
    assert "result = apply(SOMETHING, b)" in out


def test_extract_returns_none_when_absent():
    assert extract_definition("a = 1\n", "Nope") is None


def test_extract_caps_length():
    big = "def f():\n" + "    x = 1\n" * 1000
    out = extract_definition(big, "f", max_chars=50)
    assert len(out) <= 51 and out.endswith("…")


# -- render_referenced_context ----------------------------------------------
def _snip(symbol="BaseConnector", path="db/base.py", text="class BaseConnector:\n    pass"):
    return ReferencedSnippet(symbol=symbol, path=path, text=text)


def test_render_empty_is_blank():
    assert render_referenced_context([], max_chars=6000) == ""
    assert render_referenced_context([_snip()], max_chars=0) == ""


def test_render_includes_header_symbol_path_and_body():
    out = render_referenced_context([_snip()], max_chars=6000)
    assert "REFERENCED DEFINITIONS" in out
    assert "BaseConnector" in out and "db/base.py" in out
    assert "class BaseConnector:" in out


def test_render_caps_to_budget_and_notes_omitted():
    a = _snip(symbol="A", path="a.py", text="A" * 400)
    b = _snip(symbol="B", path="b.py", text="B" * 400)
    out = render_referenced_context([a, b], max_chars=300)
    assert "a.py" in out          # first kept
    assert "B" * 400 not in out   # second dropped to fit
    assert "omitted" in out.lower()
