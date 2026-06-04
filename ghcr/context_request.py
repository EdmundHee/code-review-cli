"""Pure helpers for the referenced-context fetch (multi-pass review).

No I/O, no SDK. The planner pass's raw model output comes in as text and a list of
``ContextRequest`` goes out; a fetched file's text + a symbol name go in and the
symbol's definition comes out; resolved ``ReferencedSnippet`` objects go in and one
capped, labelled markdown block comes out (injected into every lens + scoring prompt).

The actual ``gh`` fetch and the planner model call live in ``review.py``; everything
here is deterministic and unit-testable without network or the SDK.
"""

from __future__ import annotations

import re

from .models import ContextRequest, ReferencedSnippet
from .pipeline import _parse_json  # reuse the defensive fence/balanced-block JSON parser

# Extensions that mark a hint as already being a file path rather than a module.
_CODE_EXT = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".cs", ".rs",
    ".kt", ".kts", ".cpp", ".cc", ".c", ".h", ".hpp", ".php", ".swift", ".scala",
    ".m", ".mm",
)


# -- planner output ---------------------------------------------------------
def parse_context_requests(raw: str) -> list[ContextRequest]:
    """Parse the planner pass's ``{"requests":[...]}`` output. Defensive like the
    lens parsers: strip fences, validate per element, never raise — junk degrades to
    an empty list (review proceeds with no referenced context)."""
    obj = _parse_json(raw, "{", "}")
    if not isinstance(obj, dict):
        return []
    items = obj.get("requests")
    if not isinstance(items, list):
        return []
    out: list[ContextRequest] = []
    for el in items:
        if not isinstance(el, dict):
            continue
        sym = str(el.get("symbol", "") or "").strip()
        if not sym:
            continue
        out.append(ContextRequest(
            symbol=sym,
            module_hint=str(el.get("module_hint", "") or "").strip(),
            reason=str(el.get("reason", "") or "").strip(),
        ))
    return out


# -- symbol resolution helpers ----------------------------------------------
def module_to_path(hint: str) -> str | None:
    """Best-effort map of a planner ``module_hint`` to a repo file path.

    Handles a dotted Python module (``a.b.c`` -> ``a/b/c.py``), an explicit path
    (passed through), and ``from x.y import Z`` / ``import x.y`` statements. Returns
    ``None`` for a bare symbol with no locating information — the caller then falls
    back to code search."""
    h = (hint or "").strip()
    if not h:
        return None
    if h.startswith("from ") and " import " in h:
        h = h[len("from "):h.index(" import ")].strip()
    elif h.startswith("import "):
        h = h[len("import "):].split(" as ")[0].split(",")[0].strip()
    if not h:
        return None
    if "/" in h or h.endswith(_CODE_EXT):
        return h
    if "." in h:
        return h.replace(".", "/") + ".py"
    return None


def _cap(s: str, max_chars: int) -> str:
    s = s.rstrip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "…"


def _slice_block(lines: list[str], start: int, indent_len: int) -> str:
    """From ``start``, take lines until one returns to ``indent_len`` or shallower
    (the def/class/assignment block boundary)."""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent_len:
            end = j
            break
    return "\n".join(lines[start:end])


def extract_definition(file_text: str, symbol: str, *, max_chars: int = 2000) -> str | None:
    """Slice the definition of ``symbol`` out of a fetched file.

    Recognises ``def``/``class`` and module-level ``symbol =``/``symbol:`` blocks via
    indentation. Falls back to a small window around the first mention. Returns
    ``None`` if the symbol never appears. Result is capped to ``max_chars``."""
    lines = file_text.splitlines()
    esc = re.escape(symbol)
    defpat = re.compile(rf"^(\s*)(?:async\s+def|def|class)\s+{esc}\b")
    asgpat = re.compile(rf"^(\s*){esc}\s*[:=]")
    for pat in (defpat, asgpat):
        for i, ln in enumerate(lines):
            m = pat.match(ln)
            if m:
                return _cap(_slice_block(lines, i, len(m.group(1))), max_chars)
    # Fallback: first bare mention → a small context window.
    for i, ln in enumerate(lines):
        if symbol in ln:
            lo = max(0, i - 3)
            hi = min(len(lines), i + 12)
            return _cap("\n".join(lines[lo:hi]), max_chars)
    return None


# -- render -----------------------------------------------------------------
_HEADER = (
    "## REFERENCED DEFINITIONS (fetched from the repo at this commit — authoritative "
    "for code not in the diff)\n"
    "Real definitions of symbols the diff uses but does not show. Treat them as ground "
    "truth; do not assume behavior beyond what they reveal.\n"
)


def render_referenced_context(snippets, *, max_chars: int) -> str:
    """Render resolved snippets into one capped, newest-first-irrelevant block.

    ``max_chars`` budgets the rendered snippet entries (not the fixed header). The
    first snippet is always kept; later ones are dropped once the budget is spent and
    an omitted-count note is appended. Returns ``""`` when empty or disabled."""
    if not snippets or max_chars <= 0:
        return ""
    kept: list[str] = []
    used = 0
    omitted = 0
    for s in snippets:
        entry = f"### {s.symbol} — {s.path}\n```\n{s.text.strip()}\n```"
        if kept and used + len(entry) > max_chars:
            omitted += 1
            continue
        kept.append(entry)
        used += len(entry)
    parts = [_HEADER, "\n\n".join(kept)]
    if omitted:
        parts.append(
            f"\n({omitted} more definition{'s' if omitted != 1 else ''} "
            "omitted to fit the context budget)"
        )
    return "\n".join(parts).strip() + "\n"
