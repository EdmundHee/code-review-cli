"""Every interaction with GitHub, via the ``gh`` binary.

The bot PAT is injected as ``GH_TOKEN`` into the child env only, and the user's
``GITHUB_TOKEN`` is stripped from that child env, so the daemon always acts as
the bot account and never as the interactive login. The binary is always invoked
by absolute path (config ``gh_path``) to bypass shell shims/aliases.
"""

from __future__ import annotations

import json
import os
import subprocess

from .models import PriorComment, PullRequest
from .prior_context import parse_issue_comments, parse_review_comments

_PR_JSON_FIELDS = (
    "number,headRefOid,isDraft,author,url,title,additions,deletions,"
    "changedFiles,labels,baseRefName,headRefName"
)


class GhError(Exception):
    def __init__(self, args, returncode: int, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"gh {' '.join(args)} -> exit {returncode}: {stderr}")

    def is_rate_limited(self) -> bool:
        s = self.stderr.lower()
        return "rate limit" in s or "429" in s or "403" in s


def _pr_from_json(repo: str, d: dict) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=int(d["number"]),
        head_sha=d["headRefOid"],
        title=d.get("title", ""),
        author=((d.get("author") or {}).get("login") or ""),
        is_draft=bool(d.get("isDraft")),
        url=d.get("url", ""),
        additions=int(d.get("additions", 0) or 0),
        deletions=int(d.get("deletions", 0) or 0),
        changed_files=int(d.get("changedFiles", 0) or 0),
        labels=tuple((lb.get("name", "") for lb in (d.get("labels") or []))),
        base_ref=d.get("baseRefName", ""),
        head_ref=d.get("headRefName", ""),
    )


class GhClient:
    def __init__(self, gh_path: str, token: str, timeout: int = 60):
        self.gh_path = gh_path
        self.token = token
        self.timeout = timeout

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)  # must not shadow GH_TOKEN
        env["GH_TOKEN"] = self.token
        return env

    def _run(self, args, input_text: str | None = None) -> str:
        # A subprocess timeout is normalized to GhError (not raw TimeoutExpired)
        # so callers' best-effort `except GhError` handlers catch it — a slow
        # network must degrade a fetch, never hard-fail the review. Other OSErrors
        # (e.g. a missing gh binary) are left to propagate: that's a config fault.
        try:
            proc = subprocess.run(
                [self.gh_path, *args],
                input=input_text,
                capture_output=True,
                text=True,
                env=self._child_env(),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise GhError(args, -1, f"timed out after {self.timeout}s") from e
        if proc.returncode != 0:
            raise GhError(args, proc.returncode, (proc.stderr or "").strip())
        return proc.stdout

    def whoami(self) -> str:
        return self._run(["api", "user", "--jq", ".login"]).strip()

    def list_open_prs(self, repo: str, limit: int = 100) -> list[PullRequest]:
        out = self._run(
            ["--repo", repo, "pr", "list", "--state", "open", "--limit", str(limit),
             "--json", _PR_JSON_FIELDS]
        )
        return [_pr_from_json(repo, d) for d in json.loads(out or "[]")]

    def get_pr(self, repo: str, number: int) -> PullRequest:
        out = self._run(
            ["--repo", repo, "pr", "view", str(number), "--json", _PR_JSON_FIELDS]
        )
        return _pr_from_json(repo, json.loads(out))

    def get_pr_diff(self, repo: str, number: int) -> str:
        # NOTE: gh buffers the full diff into stdout. The caller bounds memory via
        # diff.hard_max_diff_bytes on the returned string (review-size caps are
        # applied later, post-filter); a diff bigger than the ceiling still costs
        # one full buffering — an accepted risk.
        return self._run(["--repo", repo, "pr", "diff", str(number), "--patch"])

    def search_code(self, repo: str, query: str, limit: int = 5) -> list[dict]:
        """Locate files matching ``query`` within ``repo`` via GitHub code search.

        Best-effort: a malformed payload degrades to ``[]``. NOTE: GitHub code search
        indexes the default branch only (not the PR head), so a fresh file may not be
        found — the caller then has no referenced context for that symbol."""
        out = self._run(
            ["search", "code", query, "--repo", repo, "--json", "path", "--limit", str(limit)]
        )
        try:
            rows = json.loads(out or "[]")
        except ValueError:
            return []
        return rows if isinstance(rows, list) else []

    def get_file_content(self, repo: str, path: str, ref: str) -> str:
        """Raw contents of ``path`` at commit ``ref`` (the PR head SHA).

        Raises ``GhError`` on a missing path / API error; the caller treats that as the
        symbol being unresolved and proceeds without it."""
        return self._run(
            ["api", "-H", "Accept: application/vnd.github.raw",
             f"repos/{repo}/contents/{path}?ref={ref}"]
        )

    def _api_jsonl(self, path: str, jq: str) -> list[dict]:
        """Run a paginated ``gh api ... --jq`` whose filter emits one compact JSON
        object per line, and parse each line. Unparseable lines are skipped so a
        single odd row never sinks the batch."""
        out = self._run(["api", "--paginate", path, "--jq", jq])
        rows: list[dict] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def get_issue_comments(self, repo: str, number: int) -> list[PriorComment]:
        """The PR conversation timeline — this is where the bot's own past reviews
        (posted via ``pr comment``) and humans' top-level remarks live."""
        rows = self._api_jsonl(
            f"repos/{repo}/issues/{number}/comments",
            ".[] | {id: .id, login: .user.login, body: .body, created_at: .created_at}",
        )
        return parse_issue_comments(rows)

    def get_review_comments(self, repo: str, number: int) -> list[PriorComment]:
        """Inline review comments anchored to specific diff lines."""
        rows = self._api_jsonl(
            f"repos/{repo}/pulls/{number}/comments",
            ".[] | {id: .id, login: .user.login, body: .body, created_at: .created_at, "
            "path: .path, line: .line, original_line: .original_line}",
        )
        return parse_review_comments(rows)

    def post_comment(self, repo: str, number: int, body: str) -> str:
        out = self._run(
            ["--repo", repo, "pr", "comment", str(number), "--body-file", "-"],
            input_text=body,
        )
        return out.strip()  # gh echoes the created comment URL
