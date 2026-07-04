"""Pure helpers for the multi-pass review pipeline.

No I/O, no threads, no SDK — just JSON parsing, finding dedup, test-file
classification, and markdown synthesis. Kept separate from ``review.py`` (the
I/O composition root) so the riskiest logic is trivially unit-testable.

The model is thinking-enabled and tends to wrap JSON in ``` fences and prefix it
with reasoning prose, so parsing is defensive: strip fences, try a direct load,
then fall back to the first balanced block, and validate per-element. A single
malformed element is dropped; total failure returns ``None`` so the caller can
mark the lens failed without raising.
"""

from __future__ import annotations

import fnmatch
import json
import re

from .models import CoverageVerdict, Finding, SEVERITY_ORDER

_VALID_SEV = set(SEVERITY_ORDER)
_SEV_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}  # lower = more severe
_STOP = {
    "the", "a", "an", "is", "are", "to", "of", "in", "on", "and", "or", "this",
    "that", "it", "be", "for", "with", "not", "no", "should", "could", "may",
}


# -- JSON extraction --------------------------------------------------------
def _strip_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _first_block(s: str, open_ch: str, close_ch: str) -> str | None:
    i = s.find(open_ch)
    j = s.rfind(close_ch)
    if i == -1 or j == -1 or j < i:
        return None
    return s[i : j + 1]


def _parse_json(raw: str, open_ch: str, close_ch: str):
    """Best-effort parse of one JSON value of the given bracket kind, or None."""
    if not raw:
        return None
    candidates: list[str] = []
    stripped = _strip_fences(raw)
    candidates.append(stripped)
    for src in (stripped, raw):
        blk = _first_block(src, open_ch, close_ch)
        if blk and blk not in candidates:
            candidates.append(blk)
    for c in candidates:
        try:
            return json.loads(c)
        except (ValueError, TypeError):
            continue
    return None


def _findings_from_list(items, lens: str) -> list[Finding]:
    out: list[Finding] = []
    if not isinstance(items, list):
        return out
    for el in items:
        if not isinstance(el, dict):
            continue
        sev = str(el.get("severity", "")).upper().strip()
        if sev not in _VALID_SEV:
            continue
        f = Finding(
            severity=sev,
            file=str(el.get("file", "")).strip(),
            area=str(el.get("area", "")).strip(),
            issue=str(el.get("issue", "")).strip(),
            fix=str(el.get("fix", "")).strip(),
            lens=lens,
        )
        if not f.issue and not f.file:
            continue  # empty junk element
        out.append(f)
    return out


def parse_lens_payload(raw: str, lens: str):
    """Parse one lens's raw model output.

    Returns ``(list[Finding], CoverageVerdict | None)`` on success, or ``None`` on
    total parse failure (caller marks the lens failed). An empty-but-valid result
    is success with zero findings.
    """
    if lens == "test_coverage":
        obj = _parse_json(raw, "{", "}")
        if not isinstance(obj, dict):
            return None
        v = obj.get("verdict") or {}
        coverage = CoverageVerdict(
            has_tests=bool(v.get("has_tests", True)),
            detail=str(v.get("detail", "")).strip(),
        )
        return _findings_from_list(obj.get("findings"), lens), coverage
    arr = _parse_json(raw, "[", "]")
    if not isinstance(arr, list):
        return None
    return _findings_from_list(arr, lens), None


def parse_score(raw: str):
    """Return ``(confidence_int_0_100, reason)`` or ``None`` if unparseable."""
    obj = _parse_json(raw, "{", "}")
    if not isinstance(obj, dict):
        return None
    try:
        c = int(round(float(obj.get("confidence"))))
    except (TypeError, ValueError):
        return None
    reason = str(obj.get("reason", "")).strip()
    return max(0, min(100, c)), reason


# -- dedup ------------------------------------------------------------------
def _sig_words(f: Finding) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", f.issue.lower()) if w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_DEDUP_SIMILARITY = 0.6


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse near-duplicate findings (same file + similar issue text) raised by
    different lenses. Keeps the most severe instance; preserves first-seen order.

    Two findings merge only when their file matches AND their issue word-sets have
    Jaccard similarity >= 0.6 — when in doubt they stay separate (scoring filters)."""
    kept: list[tuple[set[str], Finding]] = []
    for f in findings:
        sw = _sig_words(f)
        for idx, (ksw, kf) in enumerate(kept):
            if kf.file.strip() == f.file.strip() and _jaccard(sw, ksw) >= _DEDUP_SIMILARITY:
                if _SEV_RANK.get(f.severity, 9) < _SEV_RANK.get(kf.severity, 9):
                    kept[idx] = (ksw | sw, f)  # upgrade to the more severe instance
                break
        else:
            kept.append((sw, f))
    return [kf for _, kf in kept]


# -- test-file classification ----------------------------------------------
def is_test_path(path: str, test_globs) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in test_globs)


def classify_test_signal(fd, test_globs) -> tuple[bool, bool]:
    """Return ``(has_source_changes, has_test_changes)`` over the kept diff files."""
    has_source = has_test = False
    for p in fd.kept_paths:
        if is_test_path(p, test_globs):
            has_test = True
        else:
            has_source = True
    return has_source, has_test


def merge_coverage(verdicts) -> CoverageVerdict | None:
    """Combine per-chunk coverage verdicts into one PR-level verdict.

    Any chunk reporting untested behavior wins (``has_tests = all(...)``); the
    detail comes from the first failing verdict, else the first non-empty one.
    ``None`` entries (chunks whose coverage lens failed) are dropped; all-None
    → ``None`` (rendered as "Not assessed")."""
    got = [v for v in verdicts if v is not None]
    if not got:
        return None
    failing = next((v for v in got if not v.has_tests), None)
    detail = failing.detail if failing else next((v.detail for v in got if v.detail), "")
    return CoverageVerdict(has_tests=failing is None, detail=detail)


# -- synthesis --------------------------------------------------------------
def synthesize_markdown(findings, coverage, *, lens_errors=(), scored_total=0, threshold=None,
                        unreviewed_files=()) -> str:
    """Render surviving findings + the coverage verdict into the comment body
    (same shape the old single-pass SYSTEM_PROMPT produced).

    ``scored_total`` is how many findings were scored before the confidence
    filter; ``threshold`` is the bar they had to clear. When some scored findings
    were dropped, the summary says so — otherwise an all-dropped review reads as
    "nothing found" when the lenses did surface issues, just below the bar.
    """
    n = len(findings)
    dropped = max(0, scored_total - n)
    bar = f" (confidence ≥{threshold})" if threshold is not None else ""
    lines: list[str] = ["## Summary"]
    if n == 0 and dropped:
        lines.append(f"No findings cleared the confidence bar{bar}: {dropped} scored lower and were dropped.")
    elif n == 0:
        lines.append("No high-confidence issues found in this diff.")
    elif dropped:
        lines.append(
            f"{n} high-confidence finding{'s' if n != 1 else ''} after scoring "
            f"({dropped} more below the bar{bar})."
        )
    else:
        lines.append(f"{n} high-confidence finding{'s' if n != 1 else ''} after scoring.")

    lines.append("\n## Test coverage")
    if coverage is None:
        lines.append("Not assessed.")
    elif coverage.has_tests:
        lines.append(f"✅ {coverage.detail or 'Tests present, or not required for this change.'}")
    else:
        lines.append(f"⚠️ {coverage.detail or 'New behavior in this PR has no accompanying unit test.'}")

    if n:
        lines.append("\n## Findings")
        for sev in SEVERITY_ORDER:
            for f in (x for x in findings if x.severity == sev):
                loc = f.file + (f":{f.area}" if f.area else "")
                fix = f" _Fix:_ {f.fix}" if f.fix else ""
                conf = f" _(confidence {f.confidence})_" if f.confidence is not None else ""
                lines.append(f"- **[{sev}]** {loc} — {f.issue}{fix}{conf}")

    notes: list[str] = []
    if lens_errors:
        notes.append(f"Review lenses that failed and were skipped: {', '.join(lens_errors)}.")
    if unreviewed_files:
        notes.append(
            f"Not reviewed (review chunk cap reached): {', '.join(unreviewed_files)} — "
            "raise diff.max_review_chunks to cover them."
        )
    if notes:
        lines.append("\n## Notes")
        lines.extend(notes)

    return "\n".join(lines).strip() + "\n"
