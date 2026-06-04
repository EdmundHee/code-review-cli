import threading
import time

import ghcr.watcher as wmod
from ghcr.watcher import ConfigWatcher


def test_debounce_coalesces_burst(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("x")
    calls: list[int] = []
    done = threading.Event()

    def cb():
        calls.append(1)
        done.set()

    w = ConfigWatcher(str(p), cb, debounce_s=0.05)
    for _ in range(5):
        w._on_fs_event(str(p))
    assert done.wait(1.0)
    time.sleep(0.1)  # let any stray timers fire
    assert calls == [1]
    w.stop()


def test_ignores_unrelated_paths(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("x")
    calls: list[int] = []
    w = ConfigWatcher(str(p), lambda: calls.append(1), debounce_s=0.05)
    w._on_fs_event(str(tmp_path / "other.yaml"))
    time.sleep(0.15)
    assert calls == []
    w.stop()


def test_moved_onto_target_fires(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("x")
    done = threading.Event()
    w = ConfigWatcher(str(p), done.set, debounce_s=0.05)
    w._on_fs_event(str(tmp_path / ".config.swp"), dest_path=str(p))
    assert done.wait(1.0)
    w.stop()


def test_missing_watchdog_start_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(wmod, "_WATCHDOG_OK", False)
    w = ConfigWatcher(str(tmp_path / "config.yaml"), lambda: None)
    w.start()  # must not raise
    w.stop()
    assert w._observer is None
