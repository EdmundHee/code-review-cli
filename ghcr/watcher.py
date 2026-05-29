"""Watch the config file and fire a debounced callback on change (live reload).

Editors save atomically (write a temp file then rename it over the target), so a
single save can emit several events with different paths. We watch the *parent
directory*, match events against the target path, and debounce a burst into one
callback. Degrades to a logged no-op if ``watchdog`` is not installed.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("ghcr.watcher")

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_OK = True
except Exception:  # pragma: no cover - only when the optional dep is absent
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]
    _WATCHDOG_OK = False


class ConfigWatcher:
    def __init__(self, path: str, on_change, debounce_s: float = 0.4):
        # realpath (not abspath) so symlinked dirs match FSEvents paths, e.g. the
        # macOS /tmp -> /private/tmp symlink.
        self._path = os.path.realpath(os.path.expanduser(path))
        self._dir = os.path.dirname(self._path) or "."
        self._on_change = on_change
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer = None

    @property
    def available(self) -> bool:
        return _WATCHDOG_OK

    # -- core logic (testable without real OS events) --------------------
    def _matches(self, *paths) -> bool:
        return any(p and os.path.realpath(p) == self._path for p in paths)

    def _on_fs_event(self, src_path, dest_path=None) -> None:
        if self._matches(src_path, dest_path):
            self._schedule()

    def _schedule(self) -> None:
        """Debounce: (re)start the timer; fire once the burst of events settles."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self._on_change()
        except Exception:  # never let a reload error kill the watcher thread
            log.exception("config reload callback failed")

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not _WATCHDOG_OK:
            log.warning("hot-reload disabled: pip install watchdog")
            return
        self._observer = Observer()
        self._observer.schedule(_Handler(self), self._dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()
        log.info("watching %s for config changes", self._path)

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            self._observer.stop()
            try:
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None


if _WATCHDOG_OK:

    class _Handler(FileSystemEventHandler):
        def __init__(self, watcher: ConfigWatcher):
            self._w = watcher

        def on_modified(self, event):
            self._w._on_fs_event(event.src_path)

        def on_created(self, event):
            self._w._on_fs_event(event.src_path)

        def on_moved(self, event):
            self._w._on_fs_event(event.src_path, getattr(event, "dest_path", None))
