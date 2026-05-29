from ghcr.diff_filter import (
    count_changed_lines,
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
