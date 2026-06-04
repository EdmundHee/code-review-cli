from ghcr.comment import marker
from ghcr.models import PriorComment
from ghcr.prior_context import (
    build_prior_context,
    parse_issue_comments,
    parse_review_comments,
)


# -- parsing ----------------------------------------------------------------
def test_parse_issue_comments_maps_fields():
    raw = [{"login": "alice", "body": "looks good", "created_at": "2026-06-01T10:00:00Z"}]
    assert parse_issue_comments(raw) == [
        PriorComment(author="alice", body="looks good", created_at="2026-06-01T10:00:00Z", kind="issue")
    ]


def test_parse_issue_comments_skips_non_dict_and_empty_body():
    raw = ["junk", {"login": "a", "body": "  "}, {"login": "b", "body": "real"}]
    assert [c.body for c in parse_issue_comments(raw)] == ["real"]


def test_parse_review_comments_keeps_path_and_line():
    raw = [{"login": "bob", "body": "off by one", "path": "src/app.py", "line": 42,
            "created_at": "2026-06-02T00:00:00Z"}]
    out = parse_review_comments(raw)
    assert out[0].kind == "review" and out[0].path == "src/app.py" and out[0].line == 42


def test_parse_review_comments_line_falls_back_to_original_line():
    raw = [{"login": "bob", "body": "x", "path": "a.py", "line": None, "original_line": 7}]
    assert parse_review_comments(raw)[0].line == 7


# -- rendering --------------------------------------------------------------
def test_build_prior_context_empty_is_blank():
    assert build_prior_context([], bot_login="reviewbot", max_chars=6000) == ""


def test_build_prior_context_labels_bot_and_human():
    comments = [
        PriorComment(author="reviewbot", body="bot review body", created_at="2026-06-02T00:00:00Z"),
        PriorComment(author="alice", body="human remark", created_at="2026-06-01T00:00:00Z"),
    ]
    out = build_prior_context(comments, bot_login="reviewbot", max_chars=6000)
    assert "PRIOR PR DISCUSSION" in out
    assert "[bot 2026-06-02]" in out
    assert "[human alice 2026-06-01]" in out


def test_build_prior_context_detects_bot_by_marker_even_if_author_differs():
    body = marker("a" * 40, "deepseek-v4-pro") + "\nautomated review"
    comments = [PriorComment(author="ghost", body=body, created_at="2026-06-02T00:00:00Z")]
    out = build_prior_context(comments, bot_login="reviewbot", max_chars=6000)
    assert "[bot 2026-06-02]" in out and "[human ghost" not in out


def test_build_prior_context_inline_shows_path_and_line():
    comments = [PriorComment(author="bob", body="off by one", created_at="2026-06-02T00:00:00Z",
                             kind="review", path="src/app.py", line=42)]
    assert "src/app.py:42" in build_prior_context(comments, bot_login="reviewbot", max_chars=6000)


def test_build_prior_context_orders_newest_first():
    comments = [
        PriorComment(author="a", body="OLDER", created_at="2026-06-01T00:00:00Z"),
        PriorComment(author="b", body="NEWER", created_at="2026-06-03T00:00:00Z"),
    ]
    out = build_prior_context(comments, bot_login="x", max_chars=6000)
    assert out.index("NEWER") < out.index("OLDER")


def test_build_prior_context_caps_to_budget_and_notes_omitted():
    comments = [
        PriorComment(author="a", body="AAAA " * 50, created_at="2026-06-03T00:00:00Z"),
        PriorComment(author="b", body="BBBB " * 50, created_at="2026-06-02T00:00:00Z"),
        PriorComment(author="c", body="CCCC " * 50, created_at="2026-06-01T00:00:00Z"),
    ]
    out = build_prior_context(comments, bot_login="x", max_chars=300)
    assert "AAAA" in out          # newest kept
    assert "CCCC" not in out      # oldest dropped to fit
    assert "older comment" in out  # omitted note present


def test_build_prior_context_truncates_long_body():
    comments = [PriorComment(author="a", body="x" * 5000, created_at="2026-06-03T00:00:00Z")]
    out = build_prior_context(comments, bot_login="z", max_chars=10000)
    assert "…" in out and ("x" * 5000) not in out
