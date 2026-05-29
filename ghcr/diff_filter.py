"""Pure diff hygiene + size measurement.

Splits a unified diff into per-file chunks, drops noisy/binary files by glob,
and reports size signals. No I/O — trivially unit-testable.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_BINARY_RE = re.compile(r"^Binary files .* differ$", re.MULTILINE)


@dataclass(frozen=True)
class FileDiff:
    path: str
    text: str
    is_binary: bool
    added: int
    removed: int


@dataclass(frozen=True)
class FilteredDiff:
    text: str
    kept_paths: list[str]
    skipped_paths: list[str]
    changed_lines: int
    kept_bytes: int


def is_probably_binary(file_diff_text: str) -> bool:
    return "GIT binary patch" in file_diff_text or _BINARY_RE.search(file_diff_text) is not None


def should_skip_file(path: str, skip_globs) -> bool:
    """Match a path against skip globs.

    fnmatch's ``*`` spans ``/``, so ``**/dist/**`` matches nested paths. To also
    catch top-level matches (e.g. ``poetry.lock`` against ``**/*.lock``) we retry
    each ``**/``-prefixed glob with the prefix stripped.
    """
    for g in skip_globs:
        if fnmatch.fnmatch(path, g):
            return True
        if g.startswith("**/") and fnmatch.fnmatch(path, g[3:]):
            return True
    return False


def _path_from_chunk(chunk_lines: list[str]) -> str:
    m = _DIFF_GIT_RE.match(chunk_lines[0])
    if m:
        return m.group(2)  # the b/ (new) path; for deletes git keeps the name
    for ln in chunk_lines:
        if ln.startswith("+++ b/"):
            return ln[6:].strip()
        if ln.startswith("--- a/"):
            return ln[6:].strip()
    return "unknown"


def _count(chunk_lines: list[str]) -> tuple[int, int]:
    added = removed = 0
    for ln in chunk_lines:
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added, removed


def split_into_file_diffs(diff: str) -> list[FileDiff]:
    if not diff or not diff.strip():
        return []
    chunks: list[list[str]] = []
    cur: list[str] = []
    for ln in diff.splitlines(keepends=True):
        if ln.startswith("diff --git "):
            if cur:
                chunks.append(cur)
            cur = [ln]
        elif cur:
            cur.append(ln)
        # lines before the first "diff --git" header are ignored
    if cur:
        chunks.append(cur)

    out: list[FileDiff] = []
    for chunk in chunks:
        text = "".join(chunk)
        added, removed = _count(chunk)
        out.append(
            FileDiff(
                path=_path_from_chunk(chunk),
                text=text,
                is_binary=is_probably_binary(text),
                added=added,
                removed=removed,
            )
        )
    return out


def count_changed_lines(diff: str) -> int:
    added = removed = 0
    for ln in diff.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added + removed


def filter_diff(diff: str, skip_globs) -> FilteredDiff:
    kept_texts: list[str] = []
    kept_paths: list[str] = []
    skipped: list[str] = []
    changed = 0
    for f in split_into_file_diffs(diff):
        if f.is_binary or should_skip_file(f.path, skip_globs):
            skipped.append(f.path)
            continue
        kept_texts.append(f.text)
        kept_paths.append(f.path)
        changed += f.added + f.removed
    text = "".join(kept_texts)
    return FilteredDiff(
        text=text,
        kept_paths=kept_paths,
        skipped_paths=skipped,
        changed_lines=changed,
        kept_bytes=len(text.encode("utf-8")),
    )
