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


def file_hunks(diff: str, path: str) -> str | None:
    """The diff section(s) for one file, or ``None`` when the path is absent.

    Lets the scoring pass send a single finding's file hunks instead of re-sending
    the whole diff on every vote."""
    parts = [f.text for f in split_into_file_diffs(diff) if f.path == path]
    return "".join(parts) or None


def count_changed_lines(diff: str) -> int:
    added = removed = 0
    for ln in diff.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added + removed


@dataclass(frozen=True)
class DiffChunk:
    """One whole-file slice of a filtered diff, sized to fit a review call."""

    text: str
    paths: tuple[str, ...]
    byte_size: int
    truncated_paths: tuple[str, ...] = ()  # files whose hunks were cut to fit


def _truncation_marker(path: str) -> str:
    # Plain bracketed line (no backticks, no +/- prefix) so it can never break
    # the ```diff fence it is embedded in, nor count as a changed line.
    return f"[ghcr: remaining hunks of {path} omitted - diff for this file exceeds the review chunk size]\n"


def _truncate_file_diff(text: str, max_bytes: int, path: str) -> str:
    """Cut one file's diff to ``max_bytes`` at the last fitting hunk (@@) boundary,
    falling back to a line boundary when even the first hunk overflows. Appends an
    explicit marker line so the cut is never silent."""
    marker = _truncation_marker(path)
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    kept: list[str] = []
    size = 0
    hunk_starts: list[int] = []  # indices in ``kept`` where a hunk begins
    for ln in text.splitlines(keepends=True):
        if ln.startswith("@@"):
            hunk_starts.append(len(kept))
        n = len(ln.encode("utf-8"))
        if size + n > budget:
            break
        kept.append(ln)
        size += n
    else:
        return text  # everything fit (caller shouldn't hit this, but be safe)
    # Drop a partially-included trailing hunk so we never end mid-hunk — unless
    # that would drop the ONLY hunk, in which case a line-boundary cut is better
    # than reviewing nothing of the file.
    if hunk_starts and hunk_starts[-1] < len(kept) and len(hunk_starts) > 1:
        kept = kept[: hunk_starts[-1]]
    return "".join(kept) + marker


def _group_by_top_dir(files: list[FileDiff]) -> list[FileDiff]:
    """Stably order files so same-top-level-directory files are adjacent (groups in
    order of first appearance, original order within a group) — related code and
    its tests tend to land in the same chunk."""
    order: list[str] = []
    groups: dict[str, list[FileDiff]] = {}
    for f in files:
        key = f.path.split("/", 1)[0] if "/" in f.path else "."
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)
    return [f for key in order for f in groups[key]]


def chunk_filtered_diff(text: str, max_bytes: int) -> list[DiffChunk]:
    """Split a filtered unified diff into chunks of whole files, each <= ``max_bytes``.

    Greedy fill over files grouped by top-level directory. A single file bigger
    than ``max_bytes`` becomes its own chunk, truncated at a hunk boundary with an
    explicit marker (path recorded in ``truncated_paths``) — so every chunk is
    guaranteed under the budget and no cut is silent. ``"" -> []``.
    """
    files = split_into_file_diffs(text)
    if not files:
        return []
    chunks: list[DiffChunk] = []
    cur: list[FileDiff] = []
    cur_size = 0

    def flush():
        nonlocal cur, cur_size
        if cur:
            chunks.append(DiffChunk(
                text="".join(f.text for f in cur),
                paths=tuple(f.path for f in cur),
                byte_size=cur_size,
            ))
            cur, cur_size = [], 0

    for f in _group_by_top_dir(files):
        fsize = len(f.text.encode("utf-8"))
        if fsize > max_bytes:
            flush()
            cut = _truncate_file_diff(f.text, max_bytes, f.path)
            chunks.append(DiffChunk(
                text=cut,
                paths=(f.path,),
                byte_size=len(cut.encode("utf-8")),
                truncated_paths=(f.path,),
            ))
            continue
        if cur_size + fsize > max_bytes:
            flush()
        cur.append(f)
        cur_size += fsize
    flush()
    return chunks


def chunk_view(fd: FilteredDiff, chunk: DiffChunk) -> FilteredDiff:
    """A ``FilteredDiff`` presenting one chunk, so the existing prompt builders and
    lens plumbing work per-chunk unchanged. ``skipped_paths`` is preserved (shown
    once per chunk header)."""
    return FilteredDiff(
        text=chunk.text,
        kept_paths=list(chunk.paths),
        skipped_paths=fd.skipped_paths,
        changed_lines=count_changed_lines(chunk.text),
        kept_bytes=chunk.byte_size,
    )


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
