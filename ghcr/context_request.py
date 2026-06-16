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
# Extensions tried for a path-like hint with no extension of its own. JS-ish first:
# the dotted-module branch already covers Python layouts.
_EXT_TRY = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rb")


def module_path_candidates(hint: str) -> list[str]:
    """Best-effort map of a planner ``module_hint`` to candidate repo file paths,
    most likely first. The caller tries each with a (cheap, best-effort) fetch.

    Handles a dotted Python module (``a.b.c`` -> ``a/b/c.py`` + package init), an
    explicit path (passed through), ``from x.y import Z`` / ``import x.y``
    statements, and JS/TS specifiers (``./utils/x``, ``@/lib/auth``) by fanning out
    over common extensions + index files. Returns ``[]`` for a bare symbol or a
    parent-relative path (no anchor to resolve against) — the caller then falls
    back to code search."""
    h = (hint or "").strip()
    if h.startswith("from ") and " import " in h:
        h = h[len("from "):h.index(" import ")].strip()
    elif h.startswith("import "):
        h = h[len("import "):].split(" as ")[0].split(",")[0].strip()
    h = h.strip("\"'")
    if not h or "../" in h:
        return []
    if h.endswith(_CODE_EXT):
        return [h]
    if h.startswith("./"):
        bases = [h[2:]]
    elif h.startswith("@/"):
        bases = ["src/" + h[2:], h[2:]]  # "@/" conventionally aliases the source root
    elif "/" in h:
        bases = [h]
    elif "." in h:
        b = h.replace(".", "/")
        return [b + ".py", b + "/__init__.py"]
    else:
        return []
    out: list[str] = []
    for b in bases:
        out.extend(b + ext for ext in _EXT_TRY)
        out.extend((b + "/index.ts", b + "/index.js"))
    return out[:12]


def module_to_path(hint: str) -> str | None:
    """The single most likely path for a ``module_hint`` (first candidate), or
    ``None`` when unresolvable. Kept for compatibility; resolution should prefer
    ``module_path_candidates``."""
    cands = module_path_candidates(hint)
    return cands[0] if cands else None


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


def _slice_braces(lines: list[str], start: int) -> str | None:
    """From ``start``, take lines until the block's braces balance — but only when
    the header line itself (or an Allman-style next line) opens the brace. Returns
    ``None`` otherwise, so a brace deep inside a Python body never hijacks the
    indent-based slice."""
    if "{" in lines[start]:
        open_at = start
    elif start + 1 < len(lines) and lines[start + 1].lstrip().startswith("{"):
        open_at = start + 1
    else:
        return None
    depth = 0
    for j in range(start, len(lines)):
        for ch in lines[j]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        if j >= open_at and depth <= 0:
            return "\n".join(lines[start : j + 1])
    return "\n".join(lines[start:])  # unbalanced (truncated file) — take the rest


def _definition_patterns(symbol: str) -> tuple[re.Pattern, ...]:
    """Definition-line shapes across the languages the watched repos actually use
    (Python, JS/TS, Go, Ruby) — not just Python."""
    s = re.escape(symbol)
    return tuple(re.compile(p) for p in (
        rf"^(\s*)(?:async\s+def|def|class)\s+{s}\b",                                   # Python / Ruby
        rf"^(\s*)(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+{s}\b",
        rf"^(\s*)(?:export\s+)?type\s+{s}\b",                                          # TS alias / Go type
        rf"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*{s}\s*\(",  # JS/TS function
        rf"^(\s*)func\s+(?:\([^)]*\)\s+)?{s}\s*\(",                                    # Go func / method
        rf"^(\s*)(?:export\s+)?(?:const|let|var)\s+{s}\s*[:=]",                        # JS/TS binding
        rf"^(\s*)(?:(?:public|private|protected|static|async|override|readonly)\s+)*{s}\s*\([^)]*\)\s*(?::[^={{}}]*)?\{{",  # class-body method
        rf"^(\s*){s}\s*[:=]",                                                          # module-level assignment
    ))


def extract_symbol_snippet(file_text: str, symbol: str, *, max_chars: int = 2000) -> tuple[str, str] | None:
    """Slice a snippet for ``symbol`` out of a fetched file, reporting how good it is.

    Returns ``(text, kind)`` where kind is "definition" (a recognized definition
    line — block sliced by braces or indentation) or "usage" (only a mention window
    was found; NOT a verified definition). Returns ``None`` if the symbol never
    appears. Result is capped to ``max_chars``."""
    lines = file_text.splitlines()
    for pat in _definition_patterns(symbol):
        for i, ln in enumerate(lines):
            m = pat.match(ln)
            if m:
                block = _slice_braces(lines, i)
                if block is None:
                    block = _slice_block(lines, i, len(m.group(1)))
                return _cap(block, max_chars), "definition"
    # Fallback: first bare mention → a small context window. Usage only — the
    # renderer must not present this as ground truth.
    for i, ln in enumerate(lines):
        if symbol in ln:
            lo = max(0, i - 3)
            hi = min(len(lines), i + 12)
            return _cap("\n".join(lines[lo:hi]), max_chars), "usage"
    return None


def extract_definition(file_text: str, symbol: str, *, max_chars: int = 2000) -> str | None:
    """Snippet text only (see ``extract_symbol_snippet``); ``None`` when absent."""
    got = extract_symbol_snippet(file_text, symbol, max_chars=max_chars)
    return got[0] if got else None


# -- render -----------------------------------------------------------------
_HEADER = (
    "## REFERENCED DEFINITIONS (fetched from the repo at this commit — authoritative "
    "for code not in the diff)\n"
    "Real definitions of symbols the diff uses but does not show. Treat them as ground "
    "truth; do not assume behavior beyond what they reveal.\n"
)

_USAGE_CAVEAT = " (nearby usage only — NOT a verified definition; do not treat as ground truth)"


def render_referenced_context(snippets, *, max_chars: int, unresolved=()) -> str:
    """Render resolved snippets into one capped block.

    ``max_chars`` budgets the rendered snippet entries (not the fixed header). The
    first snippet is always kept; later ones are dropped once the budget is spent and
    an omitted-count note is appended. A "usage"-kind snippet is explicitly labelled
    as NOT a verified definition so it can never pose as ground truth. ``unresolved``
    names symbols whose lookup failed entirely — they are listed so the scorer's
    cap-at-25 rule has something concrete to fire on. Returns ``""`` when there is
    nothing to say or the feature is disabled."""
    if max_chars <= 0 or (not snippets and not unresolved):
        return ""
    kept: list[str] = []
    used = 0
    omitted = 0
    for s in snippets:
        caveat = _USAGE_CAVEAT if s.kind == "usage" else ""
        entry = f"### {s.symbol} — {s.path}{caveat}\n```\n{s.text.strip()}\n```"
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
    if unresolved:
        parts.append(
            "\nUNRESOLVED (definition lookup failed — treat these symbols as unknown; "
            "cap confidence at 25 for any finding that depends on their behavior): "
            + ", ".join(unresolved)
        )
    return "\n".join(parts).strip() + "\n"
