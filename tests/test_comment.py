from ghcr.comment import (
    build_comment,
    build_notice_comment,
    extract_marker_shas,
    marker,
)
from ghcr.models import PullRequest

SHA = "a" * 40
PR = PullRequest(repo="o/r", number=7, head_sha=SHA, title="t", author="alice")


def test_marker_roundtrip():
    body = marker(SHA, "deepseek-v4-pro")
    assert extract_marker_shas(body) == {SHA}


def test_extract_multiple_and_ignores_other_html():
    body = f"{marker(SHA, 'm')}\n<!-- unrelated -->\n{marker('b' * 40, 'm')}"
    assert extract_marker_shas(body) == {SHA, "b" * 40}


def test_extract_empty():
    assert extract_marker_shas("") == set()
    assert extract_marker_shas("no marker here") == set()


def test_build_comment_has_marker_and_footer():
    body = build_comment(
        content="## Summary\nok",
        pr=PR,
        model="deepseek-v4-pro",
        timestamp="2026-05-29T14:05Z",
        kept_files=2,
        skipped_files=["poetry.lock", "logo.png"],
        changed_lines=42,
    )
    assert SHA in extract_marker_shas(body)
    assert "deepseek-v4-pro" in body
    assert "42 lines / 2 files" in body
    assert "2 skipped: poetry.lock, logo.png" in body
    assert "verify before acting" in body


def test_build_comment_renders_trigger_note():
    body = build_comment(
        content="## Summary\nok", pr=PR, model="m", timestamp="t",
        kept_files=1, skipped_files=[], changed_lines=1,
        trigger_note="Re-review requested by @alice",
    )
    assert "Re-review requested by @alice" in body
    assert SHA in extract_marker_shas(body)  # marker still present


def test_build_comment_omits_trigger_note_by_default():
    body = build_comment(
        content="x", pr=PR, model="m", timestamp="t",
        kept_files=1, skipped_files=[], changed_lines=1,
    )
    assert "Re-review requested" not in body


def test_build_comment_chunk_footer():
    body = build_comment(
        content="x", pr=PR, model="m", timestamp="t",
        kept_files=9, skipped_files=[], changed_lines=100,
        chunks=3, truncated_files=["big/huge.py"],
    )
    assert "reviewed in 3 chunks" in body
    assert "truncated to fit: big/huge.py" in body


def test_build_comment_no_chunk_footer_by_default():
    body = build_comment(
        content="x", pr=PR, model="m", timestamp="t",
        kept_files=1, skipped_files=[], changed_lines=1,
    )
    assert "chunks" not in body and "truncated" not in body


def test_notice_comment_has_marker():
    body = build_notice_comment(
        kind="oversized", pr=PR, model="m", timestamp="t", detail="too big"
    )
    assert extract_marker_shas(body) == {SHA}
    assert "too big" in body
    assert "skipped" in body.lower()
