import subprocess

import pytest

from ghcr.github import GhClient, GhError


def test_run_timeout_normalized_to_gherror(monkeypatch):
    """A subprocess timeout must surface as ``GhError`` — not the raw
    ``subprocess.TimeoutExpired`` — so the best-effort ``except GhError`` fetch
    handlers (comments, referenced context, PR list) catch it. A fetch timing out
    must degrade the review, never hard-fail it."""
    c = GhClient(gh_path="/usr/bin/true", token="t", timeout=5)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=5)

    monkeypatch.setattr("ghcr.github.subprocess.run", boom)
    with pytest.raises(GhError) as ei:
        c._run(["api", "user"])
    assert ei.value.returncode != 0
    assert "timed out" in ei.value.stderr.lower()


def test_run_missing_binary_not_swallowed(monkeypatch):
    """Scope guard: only a *timeout* is normalized. A missing gh binary is a
    config error and must propagate (not be masked as a best-effort GhError that
    callers would silently read as 'no data')."""
    c = GhClient(gh_path="/no/such/gh", token="t", timeout=5)

    def boom(*args, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr("ghcr.github.subprocess.run", boom)
    with pytest.raises(FileNotFoundError):
        c._run(["api", "user"])


def _client(captured, out=""):
    """A GhClient whose subprocess layer is replaced by a capture stub."""
    c = GhClient(gh_path="/usr/bin/true", token="t", timeout=10)

    def fake_run(args, input_text=None):
        captured.append(args)
        return out

    c._run = fake_run
    return c


def test_search_code_parses_json_paths_and_threads_args():
    cap = []
    c = _client(cap, out='[{"path":"src/base.py"},{"path":"src/other.py"}]')
    hits = c.search_code("owner/repo", "BaseConnector", limit=3)
    assert [h["path"] for h in hits] == ["src/base.py", "src/other.py"]
    args = cap[0]
    assert "search" in args and "code" in args
    assert "owner/repo" in args and "BaseConnector" in args and "3" in args


def test_search_code_bad_json_degrades_to_empty():
    assert _client([], out="the index was unavailable").search_code("owner/repo", "X") == []
    assert _client([], out="").search_code("owner/repo", "X") == []


def test_get_file_content_requests_raw_blob_at_ref():
    cap = []
    c = _client(cap, out="class BaseConnector:\n    pass\n")
    text = c.get_file_content("owner/repo", "src/base.py", "deadbeef")
    assert "class BaseConnector" in text
    joined = " ".join(cap[0])
    assert "api" in cap[0]
    assert "repos/owner/repo/contents/src/base.py?ref=deadbeef" in joined
    assert any("raw" in a for a in cap[0])  # the Accept: ...github.raw header


def _jq_arg(args):
    """The value passed after ``--jq`` in a captured gh argv."""
    return args[args.index("--jq") + 1]


def test_issue_comment_jq_projects_id():
    """Regression: the @mention re-review watermark keys on the comment id, so the
    projection MUST fetch ``.id``. Omitting it pins every comment_id to 0, which
    freezes the watermark at 0 and makes ``find_mention_triggers`` (id > watermark)
    never fire — the exact bug behind 'tagged the bot but it never re-reviewed'."""
    cap = []
    c = _client(cap, out="")
    c.get_issue_comments("owner/repo", 7)
    assert ".id" in _jq_arg(cap[0])


def test_review_comment_jq_projects_id():
    cap = []
    c = _client(cap, out="")
    c.get_review_comments("owner/repo", 7)
    assert ".id" in _jq_arg(cap[0])


def test_issue_comments_round_trip_populates_comment_id():
    """End-to-end through the parser: a row carrying an id yields a non-zero
    comment_id (the value the watermark advances on)."""
    cap = []
    c = _client(cap, out='{"id":999,"login":"alice","body":"@bot please","created_at":"2026-06-04T09:46:54Z"}')
    out = c.get_issue_comments("owner/repo", 7)
    assert [x.comment_id for x in out] == [999]
