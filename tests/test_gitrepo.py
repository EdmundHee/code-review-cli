"""Real-git tests for GitRepoCache against file:// remotes (no network).

Each test builds an origin bare-ish repo with a commit and a
``refs/pull/7/head`` ref, then points the cache's ``url_base`` at ``file://``.
"""

import subprocess

import pytest

from ghcr.gitrepo import GitError, GitRepoCache

REPO = "owner/name"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_origin(tmp_path, files: dict[str, str]) -> tuple[str, str]:
    """Build an origin work repo with ``files`` committed and a pull/7/head ref.
    Returns (url_base, head_sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    for path, text in files.items():
        p = origin / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(origin, "add", "-A")
    _git(origin, "commit", "--quiet", "-m", "init")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(origin, "update-ref", "refs/pull/7/head", head)
    # url_base + "/owner/name.git" must resolve to the origin dir
    base = tmp_path / "file-base"
    (base / "owner").mkdir(parents=True)
    (base / "owner" / "name.git").symlink_to(origin)
    return f"file://{base}", head


def _cache(tmp_path, url_base, token="") -> GitRepoCache:
    return GitRepoCache(
        git_path="git",
        token=token,
        cache_root=str(tmp_path / "cache"),
        timeout=30,
        url_base=url_base,
    )


def test_ensure_fetches_and_show_returns_head_content(tmp_path):
    url, head = _make_origin(tmp_path, {"db/base.py": "class Base:\n    pass\n"})
    c = _cache(tmp_path, url)
    assert c.ensure(REPO, 7, head) is True
    assert "class Base" in c.show(REPO, head, "db/base.py")


def test_ensure_second_call_short_circuits_no_fetch(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    assert c.ensure(REPO, 7, head) is True

    calls = []
    orig = c._run

    def spy(args, cwd=None, ok_codes=(0,)):
        calls.append(list(args))
        return orig(args, cwd=cwd, ok_codes=ok_codes)

    c._run = spy
    assert c.ensure(REPO, 7, head) is True
    assert not any(a and a[0] == "fetch" for a in calls)  # commit present → no network


def test_ensure_unreachable_url_returns_false(tmp_path):
    c = _cache(tmp_path, "file:///no/such/base")
    assert c.ensure(REPO, 7, "deadbeef" * 5) is False


def test_ensure_sha_mismatch_returns_false(tmp_path):
    url, _head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    # fetch succeeds but the requested sha isn't the one on pull/7/head
    assert c.ensure(REPO, 7, "0" * 40) is False


def test_show_missing_path_raises(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    with pytest.raises(GitError):
        c.show(REPO, head, "does/not/exist.py")


def test_grep_paths_hit_and_no_match(tmp_path):
    url, head = _make_origin(
        tmp_path,
        {"db/base.py": "class BaseConnector:\n    pass\n", "other.py": "y = 2\n"},
    )
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    assert c.grep_paths(REPO, "BaseConnector", head) == ["db/base.py"]
    assert c.grep_paths(REPO, "NoSuchSymbol", head) == []


def test_grep_whole_word_not_substring(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "class BaseConnectorImpl:\n    pass\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    # -w: 'Base' must not match inside 'BaseConnectorImpl'
    assert c.grep_paths(REPO, "Base", head) == []


def test_grep_respects_limit(tmp_path):
    url, head = _make_origin(
        tmp_path, {f"f{i}.py": "Widget = 1\n" for i in range(5)}
    )
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    assert len(c.grep_paths(REPO, "Widget", head, limit=2)) == 2


def test_timeout_normalized_to_giterror(tmp_path, monkeypatch):
    c = _cache(tmp_path, "file:///x")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr("ghcr.gitrepo.subprocess.run", boom)
    with pytest.raises(GitError) as ei:
        c._run(["show", "HEAD:a.py"])
    assert "timed out" in ei.value.stderr.lower()


def test_missing_binary_normalized_to_giterror(tmp_path, monkeypatch):
    """Unlike GhClient (gh mandatory), a missing git binary degrades to GitError —
    local checkout is an optional accelerator, so ensure() → False, gh fallback."""
    c = _cache(tmp_path, "file:///x")

    def boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("ghcr.gitrepo.subprocess.run", boom)
    with pytest.raises(GitError):
        c._run(["show", "HEAD:a.py"])
    # and ensure swallows it
    assert c.ensure(REPO, 7, "a" * 40) is False


def test_worktree_checks_out_head_content(tmp_path):
    url, head = _make_origin(tmp_path, {"pkg/mod.py": "def f():\n    return 42\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    wt = c.worktree(REPO, head)
    assert wt is not None
    import os
    assert (tmp_path / "cache").exists()
    with open(os.path.join(wt, "pkg/mod.py")) as f:
        assert "return 42" in f.read()


def test_worktree_second_call_reuses_no_readd(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    first = c.worktree(REPO, head)
    assert first is not None

    calls = []
    orig = c._run

    def spy(args, cwd=None, ok_codes=(0,)):
        calls.append(list(args))
        return orig(args, cwd=cwd, ok_codes=ok_codes)

    c._run = spy
    second = c.worktree(REPO, head)
    assert second == first
    assert not any(a[:2] == ["worktree", "add"] for a in calls)  # reused, no re-add


def test_worktree_bad_sha_returns_none(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    assert c.worktree(REPO, "0" * 40) is None  # sha not present → None, never raises


def test_remove_worktree_never_raises(tmp_path):
    url, head = _make_origin(tmp_path, {"a.py": "x = 1\n"})
    c = _cache(tmp_path, url)
    c.ensure(REPO, 7, head)
    wt = c.worktree(REPO, head)
    c.remove_worktree(REPO, wt)  # real removal
    import os
    assert not os.path.exists(os.path.join(wt, ".git"))
    c.remove_worktree(REPO, "/no/such/worktree")  # nonexistent → no raise


def test_token_in_env_not_argv_and_scrubbed(tmp_path, monkeypatch):
    c = _cache(tmp_path, "https://github.com", token="SECRET_TOKEN")
    seen = {}

    def fake_run(argv, cwd=None, capture_output=None, text=None, errors=None, env=None, timeout=None):
        seen["argv"] = argv
        seen["env"] = env

        class P:
            returncode = 1
            stdout = ""
            stderr = "auth failed for SECRET_TOKEN in header"

        return P()

    monkeypatch.setattr("ghcr.gitrepo.subprocess.run", fake_run)
    with pytest.raises(GitError) as ei:
        c._run(["fetch", "x"])
    import base64
    b64 = base64.b64encode(b"x-access-token:SECRET_TOKEN").decode()
    assert "SECRET_TOKEN" not in " ".join(seen["argv"])         # never in argv
    assert b64 not in " ".join(seen["argv"])                    # not even encoded
    assert any(b64 in v for v in seen["env"].values())          # only in env (extraheader)
    assert "SECRET_TOKEN" not in ei.value.stderr                # scrubbed from error
    assert "***" in ei.value.stderr
