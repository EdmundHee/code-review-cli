from ghcr.github import GhClient


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
