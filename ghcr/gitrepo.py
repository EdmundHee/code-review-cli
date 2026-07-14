"""Local shallow-clone symbol resolution — a fast, network-resilient alternative
to the ``gh`` code-search / contents API for the referenced-context fetch.

A bare repo per project is kept under ``cache_root`` (``<owner>__<name>``). Per
review we ``git fetch --depth 1 <url> pull/<N>/head`` once — every subsequent
``git show`` / ``git grep`` is local and cannot time out on the network. This
resolves two real failures of the gh path: (a) ``gh search code`` indexes only
the default branch, never the PR head; (b) a mid-review network blip could drop
the whole context fetch (and then the review) to blind diff-only.

Auth rides the child env (``GIT_CONFIG_* extraheader``), never argv or
``.git/config`` — no remote is ever persisted, so the token touches neither disk
nor ``ps`` output nor logs. The token is scrubbed from any ``GitError.stderr``.

Divergence from ``GhClient._run``: git is an OPTIONAL accelerator, so ``_run``
normalizes a missing binary / OSError to ``GitError`` too (gh is mandatory, so it
lets that propagate as a config fault). Every public method is best-effort:
``ensure`` never raises (returns ``False``); ``show``/``grep_paths`` raise only
``GitError``, which callers catch and fall back to gh.

ponytail: no gc/eviction — objects for each reviewed head accumulate in the bare
repo. Remediation is `rm -rf <cache_root>`; add a size-bounded prune if disk bites.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess

log = logging.getLogger("ghcr.gitrepo")


class GitError(Exception):
    def __init__(self, args, returncode: int, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(args)} -> exit {returncode}: {stderr}")


def _repo_slug(repo: str) -> str:
    """``owner/name`` → ``owner__name`` (filesystem-safe cache dir name)."""
    return repo.replace("/", "__")


class GitRepoCache:
    def __init__(
        self,
        git_path: str,
        token: str,
        cache_root: str,
        timeout: int = 60,
        url_base: str = "https://github.com",
    ):
        self.git_path = git_path
        self.token = token
        self.cache_root = os.path.expanduser(cache_root)
        self.timeout = timeout
        self.url_base = url_base.rstrip("/")

    def repo_dir(self, repo: str) -> str:
        return os.path.join(self.cache_root, _repo_slug(repo))

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt
        if self.token:
            # actions/checkout scheme, but env-borne so the token never lands in
            # argv/ps/disk. Keyed on url_base so file:// test remotes ignore it.
            auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = f"http.{self.url_base}/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: basic {auth}"
        return env

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _run(self, args, cwd: str | None = None, ok_codes=(0,)) -> str:
        try:
            proc = subprocess.run(
                [self.git_path, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",  # a non-UTF8 blob must never raise
                env=self._child_env(),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(args, -1, f"timed out after {self.timeout}s") from e
        except OSError as e:  # missing binary etc. — git is optional, degrade
            raise GitError(args, -1, str(e)) from e
        if proc.returncode not in ok_codes:
            raise GitError(args, proc.returncode, self._scrub((proc.stderr or "").strip()))
        return proc.stdout

    def _has_commit(self, wd: str, sha: str) -> bool:
        try:
            self._run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=wd)
            return True
        except GitError:
            return False

    def ensure(self, repo: str, pr_number: int, head_sha: str) -> bool:
        """Make ``head_sha`` locally available in the bare cache for ``repo``.

        Short-circuits with ZERO network if the commit is already present (this is
        what makes per-chunk / per-symbol calls free after the first). Otherwise
        fetches the PR head ref shallowly. Never raises — any failure logs once and
        returns ``False`` so the caller degrades to the gh path.
        """
        wd = self.repo_dir(repo)
        try:
            if not os.path.exists(os.path.join(wd, "HEAD")):
                os.makedirs(wd, exist_ok=True)
                self._run(["init", "--bare", "--quiet", wd])
            if self._has_commit(wd, head_sha):
                return True
            self._run(
                ["fetch", "--depth", "1", f"{self.url_base}/{repo}.git",
                 f"pull/{pr_number}/head"],
                cwd=wd,
            )
            return self._has_commit(wd, head_sha)
        except GitError as e:
            log.warning("local checkout unavailable repo=%s pr=%s: %s", repo, pr_number, e)
            return False

    def show(self, repo: str, sha: str, path: str) -> str:
        """Contents of ``path`` at ``sha``. Raises ``GitError`` on a missing path."""
        return self._run(["show", f"{sha}:{path}"], cwd=self.repo_dir(repo))

    def grep_paths(self, repo: str, symbol: str, sha: str, limit: int = 5) -> list[str]:
        """Paths at ``sha`` containing ``symbol`` (literal whole-word match).

        Exit 1 = no match = ``[]``. Output lines are ``<sha>:<path>``."""
        out = self._run(
            ["grep", "-l", "-F", "-w", symbol, sha],
            cwd=self.repo_dir(repo),
            ok_codes=(0, 1),
        )
        paths = []
        for line in out.splitlines():
            _, _, p = line.partition(":")
            if p:
                paths.append(p)
            if len(paths) >= limit:
                break
        return paths

    def worktree(self, repo: str, sha: str) -> str | None:
        """Materialize a detached checkout of ``sha`` and return its path, or None.

        A sibling of the bare dir (``<slug>.wt/<sha[:12]>``), reused if already
        present. For the agentic scoring pass: a real on-disk tree the ``claude -p``
        verifier can Read/Grep. Precondition: ``ensure()`` has already fetched the
        commit into the bare cache. Best-effort — any failure logs once and returns
        None so the caller degrades to prompt-only scoring.
        """
        wt = os.path.join(self.repo_dir(repo) + ".wt", sha[:12])
        try:
            if os.path.exists(os.path.join(wt, ".git")):
                return wt  # already checked out for this head — reuse
            if os.path.exists(wt):  # stale dir without a git link → clear it first
                self._run(["worktree", "prune"], cwd=self.repo_dir(repo))
                shutil.rmtree(wt, ignore_errors=True)
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            self._run(["worktree", "add", "--detach", wt, sha], cwd=self.repo_dir(repo))
            return wt
        except (GitError, OSError) as e:
            log.warning("worktree unavailable repo=%s sha=%s: %s", repo, sha[:12], e)
            return None

    def remove_worktree(self, repo: str, path: str) -> None:
        """Tear down a worktree from ``worktree()``. Best-effort; never raises.
        ponytail: a crash between add and here leaves one worktree on disk; the
        ``prune`` in the next ``worktree()`` call reclaims it, else ``rm -rf`` the
        ``<slug>.wt`` dir."""
        try:
            self._run(["worktree", "remove", "--force", path], cwd=self.repo_dir(repo))
        except GitError as e:
            log.debug("worktree remove failed repo=%s path=%s: %s", repo, path, e)
        try:
            self._run(["worktree", "prune"], cwd=self.repo_dir(repo))
        except GitError:
            pass
