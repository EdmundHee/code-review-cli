"""Pure helpers for reading the existing PR conversation back to the reviewer.

No I/O, no SDK — the raw ``gh api`` dicts come in, a single capped, newest-first
markdown block goes out. The block is injected into every lens user prompt (for
discussion context) and every scoring user prompt (so the scorer can suppress a
finding already raised by this bot on an earlier commit or by a human).

Bot vs human is decided here, not at fetch time: ``GhClient`` does not know the
configured ``bot_login``. We reuse ``comment.extract_marker_shas`` so a comment
that carries the bot's hidden review marker counts as the bot even if it was
posted under a different login.
"""

from __future__ import annotations

from .comment import extract_marker_shas
from .models import PriorComment

_BODY_CAP = 600  # per-comment body chars before truncation


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

_HEADER = (
    "## PRIOR PR DISCUSSION (most recent first)\n"
    "These are existing comments on this PR — this bot's previous automated "
    "reviews and human remarks. Use them as context: do NOT re-raise an issue "
    "already raised here unless this diff reintroduces or changes it, and respect "
    "decisions already made in the thread.\n"
)


def parse_issue_comments(raw) -> list[PriorComment]:
    """Map ``gh api .../issues/{n}/comments`` dicts to PriorComments (kind=issue)."""
    out: list[PriorComment] = []
    for el in raw or []:
        if not isinstance(el, dict):
            continue
        body = str(el.get("body", "") or "").strip()
        if not body:
            continue
        out.append(PriorComment(
            author=str(el.get("login", "") or "").strip(),
            body=body,
            created_at=str(el.get("created_at", "") or ""),
            kind="issue",
            comment_id=_as_int(el.get("id")),
        ))
    return out


def parse_review_comments(raw) -> list[PriorComment]:
    """Map ``gh api .../pulls/{n}/comments`` (inline) dicts to PriorComments.

    ``line`` is null on outdated comments; fall back to ``original_line``."""
    out: list[PriorComment] = []
    for el in raw or []:
        if not isinstance(el, dict):
            continue
        body = str(el.get("body", "") or "").strip()
        if not body:
            continue
        line = el.get("line")
        if line is None:
            line = el.get("original_line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        out.append(PriorComment(
            author=str(el.get("login", "") or "").strip(),
            body=body,
            created_at=str(el.get("created_at", "") or ""),
            kind="review",
            path=str(el.get("path", "") or "").strip(),
            line=line,
            comment_id=_as_int(el.get("id")),
        ))
    return out


def _is_bot(c: PriorComment, bot_login: str) -> bool:
    if bot_login and c.author.lower() == bot_login.lower():
        return True
    return bool(extract_marker_shas(c.body))


def _render(c: PriorComment, bot_login: str) -> str:
    who = "bot" if _is_bot(c, bot_login) else f"human {c.author}".rstrip()
    date = c.created_at[:10] if c.created_at else "?"
    loc = ""
    if c.kind == "review" and c.path:
        loc = f" · {c.path}:{c.line}" if c.line is not None else f" · {c.path}"
    body = c.body if len(c.body) <= _BODY_CAP else c.body[:_BODY_CAP] + "…"
    return f"[{who} {date}{loc}] {body}"


def build_prior_context(comments, *, bot_login: str, max_chars: int) -> str:
    """Render comments into one newest-first context block, capped at ``max_chars``.

    ``max_chars`` budgets the rendered comment entries (not the fixed header). When
    the budget truncates older comments, an omitted-count note is appended. Returns
    ``""`` when there is nothing to show or the feature is disabled (max_chars <= 0).
    """
    if not comments or max_chars <= 0:
        return ""
    ordered = sorted(comments, key=lambda c: c.created_at or "", reverse=True)
    kept: list[str] = []
    used = 0
    omitted = 0
    for c in ordered:
        entry = _render(c, bot_login)
        # Always keep the newest; otherwise stop once the budget is exhausted.
        if kept and used + len(entry) > max_chars:
            omitted += 1
            continue
        kept.append(entry)
        used += len(entry)
    lines = [_HEADER, "\n".join(kept)]
    if omitted:
        lines.append(f"\n({omitted} older comment{'s' if omitted != 1 else ''} omitted to fit the context budget)")
    return "\n".join(lines).strip() + "\n"


# -- @mention re-review trigger detection -----------------------------------
def find_mention_triggers(comments, *, bot_login: str, after_id: int) -> list[PriorComment]:
    """Comments that should summon a fresh review: a non-bot author @mentions the
    bot and the comment is newer than the watermark ``after_id``. Bot-authored
    comments (incl. its own reviews) never qualify — loop-safety."""
    needle = f"@{bot_login}".lower()
    return [
        c for c in comments
        if c.comment_id > after_id
        and not _is_bot(c, bot_login)
        and needle in c.body.lower()
    ]


def max_comment_id(comments) -> int:
    """Highest comment id seen (0 when empty) — the new watermark after a scan."""
    return max((c.comment_id for c in comments), default=0)
