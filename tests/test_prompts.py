from ghcr.diff_filter import FilteredDiff
from ghcr.models import Finding
from ghcr.prompts import (
    CONTEXT_REQUEST_PROMPT,
    LENS_NAMES,
    LENS_PROMPTS,
    SCORING_SYSTEM_PROMPT,
    build_context_request_user_prompt,
    build_scoring_user_prompt,
    build_user_prompt,
    coverage_hint,
)
from tests.fakes import make_pr


def test_every_lens_has_a_prompt_with_a_unique_header():
    assert set(LENS_PROMPTS) == set(LENS_NAMES)
    for name in LENS_NAMES:
        assert f"## LENS: {name}" in LENS_PROMPTS[name]


def test_lenses_embed_false_positive_guidance():
    for name in LENS_NAMES:
        assert "false positives" in LENS_PROMPTS[name].lower()
        assert "linter" in LENS_PROMPTS[name].lower()


def test_findings_lenses_ask_for_json_array_coverage_for_object():
    assert "JSON array" in LENS_PROMPTS["correctness"]
    assert "JSON object" in LENS_PROMPTS["test_coverage"]
    assert "has_tests" in LENS_PROMPTS["test_coverage"]


def test_scoring_prompt_has_rubric_and_route_header():
    assert "## PASS: scoring" in SCORING_SYSTEM_PROMPT
    for anchor in ("0:", "25:", "50:", "75:", "100:"):
        assert anchor in SCORING_SYSTEM_PROMPT
    assert '"confidence"' in SCORING_SYSTEM_PROMPT


def test_build_scoring_user_prompt_includes_finding_and_diff():
    fd = FilteredDiff(text="THE DIFF", kept_paths=["a.py"], skipped_paths=[], changed_lines=1, kept_bytes=8)
    f = Finding(severity="WARNING", file="a.py", area="f", issue="bug", fix="do x", lens="correctness")
    out = build_scoring_user_prompt(make_pr(), fd, f)
    assert "THE DIFF" in out and "correctness" in out and "bug" in out


def test_coverage_hint_reports_both_signals():
    h = coverage_hint(True, False)
    assert "True" in h and "False" in h


# -- prior-comment context injection ----------------------------------------
def _fd():
    return FilteredDiff(text="THE DIFF", kept_paths=["a.py"], skipped_paths=[], changed_lines=1, kept_bytes=8)


def test_build_user_prompt_omits_prior_block_by_default():
    assert "PRIOR PR DISCUSSION" not in build_user_prompt(make_pr(), _fd())


def test_build_user_prompt_includes_prior_block_when_given():
    block = "## PRIOR PR DISCUSSION\n[bot 2026-06-01] earlier note\n"
    out = build_user_prompt(make_pr(), _fd(), prior_context=block)
    assert "PRIOR PR DISCUSSION" in out and "THE DIFF" in out


def test_build_scoring_user_prompt_includes_prior_block_when_given():
    f = Finding(severity="WARNING", file="a.py", area="g", issue="bug", fix="x", lens="correctness")
    out = build_scoring_user_prompt(make_pr(), _fd(), f, prior_context="PRIOR-XYZ-MARK")
    assert "PRIOR-XYZ-MARK" in out and "bug" in out


def test_scoring_prompt_instructs_suppression_of_already_raised():
    assert "already raised" in SCORING_SYSTEM_PROMPT.lower()


# -- chunked-review note ------------------------------------------------------
def test_build_user_prompt_no_chunk_note_by_default():
    assert "part " not in build_user_prompt(make_pr(), _fd())


def test_build_user_prompt_chunk_note_when_chunked():
    out = build_user_prompt(make_pr(), _fd(), chunk_index=2, chunk_total=3)
    assert "part 2/3" in out and "reviewed separately" in out
    assert "THE DIFF" in out


# -- referenced-context injection + planner pass ----------------------------
def test_build_user_prompt_omits_referenced_block_by_default():
    assert "REFERENCED DEFINITIONS" not in build_user_prompt(make_pr(), _fd())


def test_build_user_prompt_includes_referenced_block_when_given():
    block = "## REFERENCED DEFINITIONS\nclass BaseConnector: ...\n"
    out = build_user_prompt(make_pr(), _fd(), referenced_context=block)
    assert "REFERENCED DEFINITIONS" in out and "BaseConnector" in out and "THE DIFF" in out


def test_build_scoring_user_prompt_includes_referenced_block_when_given():
    f = Finding(severity="WARNING", file="a.py", area="g", issue="bug", fix="x", lens="correctness")
    out = build_scoring_user_prompt(make_pr(), _fd(), f, referenced_context="REF-MARK-XYZ")
    assert "REF-MARK-XYZ" in out and "bug" in out


def test_context_request_prompt_has_route_header_and_requests_shape():
    assert "## PASS: context" in CONTEXT_REQUEST_PROMPT
    assert "requests" in CONTEXT_REQUEST_PROMPT


def test_context_request_prompt_offers_tests_kind():
    assert '"kind"' in CONTEXT_REQUEST_PROMPT
    assert "tests" in CONTEXT_REQUEST_PROMPT
    assert "EXISTING TESTS" in CONTEXT_REQUEST_PROMPT


def test_build_context_request_user_prompt_includes_diff():
    assert "THE DIFF" in build_context_request_user_prompt(make_pr(), _fd())


def test_lens_findings_spec_ties_blocker_to_evidence():
    p = LENS_PROMPTS["correctness"]
    assert "BLOCKER" in p
    assert "referenced definitions" in p.lower()  # severity rubric cites the fetched evidence


def test_lens_base_treats_referenced_definitions_as_authoritative():
    p = LENS_PROMPTS["correctness"]
    assert "referenced definitions" in p.lower()
    assert "authoritative" in p.lower()


def test_scoring_prompt_caps_confidence_on_unverifiable_symbols():
    low = SCORING_SYSTEM_PROMPT.lower()
    assert "referenced definitions" in low
    assert "cap confidence at 25" in low


# -- token cuts + posted-comment language boundary ----------------------------
TWO_FILE_DIFF = """\
diff --git a/a.py b/a.py
@@ -1 +1 @@
-aaa
+AAA-HUNK
diff --git a/b.py b/b.py
@@ -1 +1 @@
-bbb
+BBB-HUNK
"""


def test_scoring_user_prompt_sends_only_finding_file_hunks():
    fd = FilteredDiff(text=TWO_FILE_DIFF, kept_paths=["a.py", "b.py"], skipped_paths=[],
                      changed_lines=4, kept_bytes=len(TWO_FILE_DIFF))
    f = Finding(severity="WARNING", file="b.py", area="g", issue="bug", fix="x", lens="correctness")
    out = build_scoring_user_prompt(make_pr(), fd, f)
    assert "BBB-HUNK" in out and "AAA-HUNK" not in out


def test_scoring_user_prompt_falls_back_to_full_diff_when_file_absent():
    fd = FilteredDiff(text=TWO_FILE_DIFF, kept_paths=["a.py", "b.py"], skipped_paths=[],
                      changed_lines=4, kept_bytes=len(TWO_FILE_DIFF))
    f = Finding(severity="WARNING", file="missing.py", area="g", issue="bug", fix="x", lens="correctness")
    out = build_scoring_user_prompt(make_pr(), fd, f)
    assert "AAA-HUNK" in out and "BBB-HUNK" in out


def test_scoring_prompt_instructs_refutation_first():
    assert "refute" in SCORING_SYSTEM_PROMPT.lower()


def test_every_lens_requires_full_sentence_issue_and_fix():
    # internal prompts are compressed, but issue/fix land verbatim in the posted
    # comment — every lens must demand human-readable full sentences for them
    for name in LENS_NAMES:
        assert "full sentences" in LENS_PROMPTS[name].lower()
