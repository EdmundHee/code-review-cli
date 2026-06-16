from ghcr.context_request import (
    extract_definition,
    extract_symbol_snippet,
    module_path_candidates,
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


# -- module_path_candidates ---------------------------------------------------
def test_candidates_dotted_module_is_python_layout():
    out = module_path_candidates("db.base")
    assert out[0] == "db/base.py"
    assert "db/base/__init__.py" in out


def test_candidates_js_relative_path_fans_out_extensions():
    out = module_path_candidates("./utils/helpers")
    assert "utils/helpers.ts" in out and "utils/helpers.js" in out
    assert "utils/helpers/index.ts" in out


def test_candidates_alias_tries_src_prefix():
    out = module_path_candidates("@/lib/auth")
    assert "src/lib/auth.ts" in out and "lib/auth.ts" in out


def test_candidates_explicit_path_passes_through():
    assert module_path_candidates("pkg/mod.py") == ["pkg/mod.py"]


def test_candidates_empty_for_bare_symbol_and_parent_relative():
    assert module_path_candidates("BareSymbol") == []
    assert module_path_candidates("../sibling/mod") == []
    assert module_path_candidates("") == []


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


# -- extract_symbol_snippet (kind-aware, language-aware) ----------------------
def test_snippet_python_def_is_definition_kind():
    text, kind = extract_symbol_snippet("def foo():\n    return 1\n", "foo")
    assert kind == "definition" and "return 1" in text


def test_snippet_bare_mention_is_usage_kind():
    text, kind = extract_symbol_snippet("x = apply(SOMETHING, 2)\n", "SOMETHING")
    assert kind == "usage" and "apply(SOMETHING" in text


def test_snippet_js_function_slices_brace_block():
    src = (
        "export function helper(a) {\n"
        "  if (a) {\n"
        "    return 1;\n"
        "  }\n"
        "  return 2;\n"
        "}\n"
        "const bar = 1;\n"
    )
    text, kind = extract_symbol_snippet(src, "helper")
    assert kind == "definition"
    assert "return 2;" in text and "bar" not in text


def test_snippet_js_const_arrow_is_definition():
    src = "const foo = (a) => a + 1;\nconst bar = 2;\n"
    text, kind = extract_symbol_snippet(src, "foo")
    assert kind == "definition" and "a + 1" in text and "bar" not in text


def test_snippet_go_method_receiver():
    src = (
        "func (s *Server) Handle(w http.ResponseWriter) {\n"
        "\ts.mu.Lock()\n"
        "}\n"
        "\n"
        "func other() {}\n"
    )
    text, kind = extract_symbol_snippet(src, "Handle")
    assert kind == "definition" and "s.mu.Lock()" in text and "other" not in text


def test_snippet_ts_interface():
    src = "export interface Props {\n  id: string;\n}\nlet x = 1;\n"
    text, kind = extract_symbol_snippet(src, "Props")
    assert kind == "definition" and "id: string;" in text and "x = 1" not in text


def test_snippet_class_body_method_shorthand():
    src = (
        "class A {\n"
        "  async handle(req) {\n"
        "    return req.id;\n"
        "  }\n"
        "  other() {}\n"
        "}\n"
    )
    text, kind = extract_symbol_snippet(src, "handle")
    assert kind == "definition" and "return req.id;" in text and "other" not in text


def test_snippet_python_class_with_dict_keeps_whole_body():
    # a brace inside the body must not trigger brace-slicing on a Python class
    src = (
        "class Foo:\n"
        "    MAP = {\n"
        "        'a': 1,\n"
        "    }\n"
        "    def bar(self):\n"
        "        return 2\n"
        "TOP = 1\n"
    )
    text, kind = extract_symbol_snippet(src, "Foo")
    assert kind == "definition" and "def bar" in text and "TOP = 1" not in text


def test_snippet_absent_returns_none():
    assert extract_symbol_snippet("a = 1\n", "Nope") is None


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


def test_render_marks_usage_snippets_as_unverified():
    s = ReferencedSnippet(symbol="foo", path="a.js", text="x = foo(1)", kind="usage")
    out = render_referenced_context([s], max_chars=6000)
    assert "NOT a verified definition" in out


def test_render_definition_snippets_carry_no_unverified_caveat():
    out = render_referenced_context([_snip()], max_chars=6000)
    assert "NOT a verified definition" not in out


def test_render_lists_unresolved_symbols_with_cap_instruction():
    out = render_referenced_context([], max_chars=6000, unresolved=("Alpha", "Beta"))
    assert "REFERENCED DEFINITIONS" in out
    assert "UNRESOLVED" in out and "Alpha" in out and "Beta" in out
    assert "cap confidence" in out.lower()


def test_render_empty_without_unresolved_stays_blank():
    assert render_referenced_context([], max_chars=6000, unresolved=()) == ""
