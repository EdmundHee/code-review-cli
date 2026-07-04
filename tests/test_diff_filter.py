from ghcr.diff_filter import (
    chunk_filtered_diff,
    chunk_view,
    count_changed_lines,
    file_hunks,
    filter_diff,
    is_probably_binary,
    should_skip_file,
    split_into_file_diffs,
)

SRC_AND_LOCK = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def f():
-    return 1
+    return 2
+    # added
diff --git a/poetry.lock b/poetry.lock
index aaaaaaa..bbbbbbb 100644
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1 @@
-old
+new
"""

BINARY = """\
diff --git a/data.bin b/data.bin
new file mode 100644
index 0000000..1234567
Binary files /dev/null and b/data.bin differ
"""


def test_should_skip_matches_nested_and_top_level():
    globs = ["**/*.lock", "**/dist/**"]
    assert should_skip_file("poetry.lock", globs)          # top-level
    assert should_skip_file("backend/poetry.lock", globs)  # nested
    assert should_skip_file("web/dist/bundle.js", globs)   # dir glob
    assert not should_skip_file("src/app.py", globs)


def test_split_and_counts():
    files = split_into_file_diffs(SRC_AND_LOCK)
    assert [f.path for f in files] == ["src/app.py", "poetry.lock"]
    app = files[0]
    assert app.added == 2 and app.removed == 1


def test_count_changed_lines_ignores_headers():
    assert count_changed_lines(SRC_AND_LOCK) == 2 + 1 + 1 + 1  # app:3 + lock:2


def test_binary_detection():
    assert is_probably_binary(BINARY)
    files = split_into_file_diffs(BINARY)
    assert files[0].is_binary


def test_filter_drops_lockfile_keeps_source():
    fd = filter_diff(SRC_AND_LOCK, ["**/*.lock"])
    assert fd.kept_paths == ["src/app.py"]
    assert fd.skipped_paths == ["poetry.lock"]
    assert fd.changed_lines == 3
    assert "poetry.lock" not in fd.text
    assert "return 2" in fd.text


def test_filter_empty_when_all_skipped():
    fd = filter_diff(SRC_AND_LOCK, ["**/*.lock", "**/*.py"])
    assert fd.kept_paths == []
    assert fd.changed_lines == 0


def test_empty_diff():
    assert split_into_file_diffs("") == []
    fd = filter_diff("", ["**/*.lock"])
    assert fd.changed_lines == 0 and fd.text == ""


def test_file_hunks_slices_only_the_named_file():
    out = file_hunks(SRC_AND_LOCK, "src/app.py")
    assert "return 2" in out
    assert "poetry.lock" not in out


def test_file_hunks_none_when_path_absent_or_diff_empty():
    assert file_hunks(SRC_AND_LOCK, "nope.py") is None
    assert file_hunks("", "src/app.py") is None


# -- chunking ----------------------------------------------------------------
def _mk_file(path: str, lines: int) -> str:
    body = "".join(f"+line {i} of {path}\n" for i in range(lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1..2 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +{lines} @@\n"
        f"{body}"
    )


def test_chunk_under_budget_is_single_identical_chunk():
    text = _mk_file("src/a.py", 3) + _mk_file("src/b.py", 3)
    chunks = chunk_filtered_diff(text, 1_000_000)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].paths == ("src/a.py", "src/b.py")
    assert chunks[0].truncated_paths == ()


def test_chunk_greedy_whole_file_packing_covers_every_file_once():
    files = [f"src/f{i}.py" for i in range(6)]
    text = "".join(_mk_file(p, 10) for p in files)
    per_file = len(_mk_file(files[0], 10).encode())
    chunks = chunk_filtered_diff(text, per_file * 2 + 10)  # ~2 files per chunk
    assert len(chunks) == 3
    seen = [p for ch in chunks for p in ch.paths]
    assert sorted(seen) == sorted(files)          # every kept file exactly once
    assert "".join(ch.text for ch in chunks) == text  # nothing lost, whole files
    assert all(ch.byte_size <= per_file * 2 + 10 for ch in chunks)


def test_chunk_groups_by_top_level_dir():
    # Interleaved dirs: grouping should reorder so same-dir files sit together.
    text = (_mk_file("a/one.py", 5) + _mk_file("b/x.py", 5)
            + _mk_file("a/two.py", 5) + _mk_file("b/y.py", 5))
    per_file = len(_mk_file("a/one.py", 5).encode())
    chunks = chunk_filtered_diff(text, per_file * 2 + 40)
    assert len(chunks) == 2
    assert set(chunks[0].paths) == {"a/one.py", "a/two.py"}
    assert set(chunks[1].paths) == {"b/x.py", "b/y.py"}


def test_chunk_single_oversized_file_truncated_at_hunk_boundary():
    big = _mk_file("src/huge.py", 200)
    small = _mk_file("src/tiny.py", 2)
    budget = len(small.encode()) * 4
    chunks = chunk_filtered_diff(small + big, budget)
    assert any(ch.truncated_paths == ("src/huge.py",) for ch in chunks)
    trunc = next(ch for ch in chunks if ch.truncated_paths)
    assert trunc.byte_size <= budget
    assert "omitted" in trunc.text and "src/huge.py" in trunc.text
    assert "`" not in trunc.text.splitlines()[-1]  # marker can't break the ```diff fence
    # The intact small file still gets its own untruncated chunk.
    assert any(ch.paths == ("src/tiny.py",) and not ch.truncated_paths for ch in chunks)


def test_chunk_empty_input():
    assert chunk_filtered_diff("", 1000) == []


def test_chunk_view_recomputes_sizes_keeps_skipped():
    fd = filter_diff(SRC_AND_LOCK, ["**/*.lock"])
    chunks = chunk_filtered_diff(fd.text, 1_000_000)
    view = chunk_view(fd, chunks[0])
    assert view.text == fd.text
    assert view.kept_paths == ["src/app.py"]
    assert view.skipped_paths == ["poetry.lock"]
    assert view.changed_lines == fd.changed_lines
    assert view.kept_bytes == chunks[0].byte_size
