from ghcr.diff_filter import FilteredDiff
from ghcr.models import CoverageVerdict, Finding
from ghcr.pipeline import (
    classify_test_signal,
    dedup_findings,
    parse_lens_payload,
    parse_score,
    synthesize_markdown,
)

ARR = '[{"severity":"WARNING","file":"a.py","area":"f","issue":"off by one","fix":"use <="}]'


# -- parse_lens_payload (findings lenses) -----------------------------------
def test_parse_clean_array():
    findings, cov = parse_lens_payload(ARR, "correctness")
    assert cov is None
    assert len(findings) == 1
    assert findings[0].severity == "WARNING" and findings[0].lens == "correctness"


def test_parse_fenced_array_with_prose_preamble():
    raw = "Sure, here are the issues I found:\n```json\n" + ARR + "\n```\nHope that helps."
    findings, _ = parse_lens_payload(raw, "security")
    assert len(findings) == 1 and findings[0].lens == "security"


def test_parse_empty_array_is_success_not_failure():
    findings, _ = parse_lens_payload("[]", "correctness")
    assert findings == []


def test_parse_total_garbage_returns_none():
    assert parse_lens_payload("the model refused", "correctness") is None


def test_parse_drops_one_bad_element_keeps_rest():
    raw = '[{"severity":"NOPE","file":"x"},{"severity":"BLOCKER","file":"y.py","issue":"npe"}]'
    findings, _ = parse_lens_payload(raw, "correctness")
    assert len(findings) == 1 and findings[0].severity == "BLOCKER"


# -- parse_lens_payload (test_coverage object) ------------------------------
def test_parse_coverage_object():
    raw = '{"verdict":{"has_tests":false,"detail":"no test for foo"},"findings":' + ARR + "}"
    findings, cov = parse_lens_payload(raw, "test_coverage")
    assert isinstance(cov, CoverageVerdict) and cov.has_tests is False
    assert cov.detail == "no test for foo"
    assert len(findings) == 1


def test_parse_coverage_non_object_is_failure():
    assert parse_lens_payload("[]", "test_coverage") is None


# -- parse_score ------------------------------------------------------------
def test_parse_score_clamps_and_reads_reason():
    assert parse_score('{"confidence": 91, "reason": "real"}') == (91, "real")
    assert parse_score('{"confidence": 250}')[0] == 100
    assert parse_score('{"confidence": -5}')[0] == 0


def test_parse_score_garbage_is_none():
    assert parse_score("no json here") is None
    assert parse_score('{"reason":"x"}') is None


# -- dedup ------------------------------------------------------------------
def test_dedup_collapses_same_file_similar_issue_keeps_severe():
    fs = [
        Finding(severity="WARNING", file="a.py", issue="null pointer on user", lens="correctness"),
        Finding(severity="BLOCKER", file="a.py", issue="null pointer on user object", lens="security"),
        Finding(severity="MINOR", file="b.py", issue="rename var", lens="maintainability"),
    ]
    out = dedup_findings(fs)
    assert len(out) == 2
    a = [f for f in out if f.file == "a.py"][0]
    assert a.severity == "BLOCKER"  # kept the more severe instance


# -- classify_test_signal ---------------------------------------------------
def test_classify_test_signal():
    globs = ("**/test_*.py", "**/tests/**")
    fd = FilteredDiff(
        text="", kept_paths=["src/app.py", "tests/test_app.py"],
        skipped_paths=[], changed_lines=2, kept_bytes=1,
    )
    assert classify_test_signal(fd, globs) == (True, True)
    fd2 = FilteredDiff(text="", kept_paths=["src/app.py"], skipped_paths=[], changed_lines=1, kept_bytes=1)
    assert classify_test_signal(fd2, globs) == (True, False)


# -- synthesize_markdown ----------------------------------------------------
def test_synthesize_includes_coverage_and_findings():
    findings = [Finding(severity="BLOCKER", file="a.py", area="f", issue="npe", fix="guard", confidence=95)]
    cov = CoverageVerdict(has_tests=False, detail="new fn foo untested")
    md = synthesize_markdown(findings, cov)
    assert "## Summary" in md and "## Test coverage" in md and "## Findings" in md
    assert "⚠️" in md and "new fn foo untested" in md
    assert "[BLOCKER]" in md and "confidence 95" in md


def test_synthesize_clean_when_no_findings_and_tests_present():
    cov = CoverageVerdict(has_tests=True, detail="tests added for new endpoint")
    md = synthesize_markdown([], cov)
    assert "No high-confidence issues" in md and "✅" in md
    assert "## Findings" not in md


def test_synthesize_notes_lens_errors():
    md = synthesize_markdown([], None, lens_errors=["security"])
    assert "## Notes" in md and "security" in md
