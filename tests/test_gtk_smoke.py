"""Smoke-тест GTK4-интерфейса.

Тест требует X-сервер. Если DISPLAY недоступен — тест пропускается
(в CI/headless без иксов это ожидаемое поведение), а не падает.
"""

import os
from threading import Event, get_ident
from time import monotonic

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402


def _display_available() -> bool:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        return Gtk.init_check()
    except Exception:
        return False


@pytest.mark.skipif(not _display_available(), reason="нет X/Wayland-дисплея")
def test_main_window_builds_and_shows(tmp_path):
    from radio_wave.service import RadioService
    from radio_wave.state import AppState
    from radio_wave.library import Catalogue
    from radio_wave.ui.app import MainWindow, RadioWaveApp

    service = RadioService(
        state=AppState(str(tmp_path / "state.json")),
        catalogue=Catalogue(str(tmp_path / "catalogue.json")),
        player=None,
        torrents=None,
    )
    app = RadioWaveApp(service=service)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    result = {}

    def check():
        window = app.get_active_window()
        result["ok"] = isinstance(window, MainWindow) and window.get_visible()
        if window is not None:
            window.close()
        app.quit()
        return GLib.SOURCE_REMOVE

    def on_activate(_app):
        GLib.idle_add(check)

    app.connect_after("activate", on_activate)
    assert app.run(["radio-wave"]) == 0
    assert result.get("ok"), "главное окно не появилось после запуска приложения"


@pytest.mark.skipif(not _display_available(), reason="нет X/Wayland-дисплея")
def test_window_close_hides_before_cleanup_and_serializes_reactivation(tmp_path, monkeypatch):
    from radio_wave.library import Catalogue
    from radio_wave.service import RadioService
    from radio_wave.state import AppState
    from radio_wave.ui.app import RadioWaveApp

    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))

    def create_service(suffix):
        return RadioService(state=AppState(str(tmp_path / f"state-{suffix}.json")),
                            catalogue=Catalogue(str(tmp_path / f"catalogue-{suffix}.json")),
                            player=None, torrents=None, online=False)

    old = create_service("old")
    real_quit = old.quit
    allow_cleanup = Event()
    cleaned = Event()
    workers = []
    result = {}
    first_window = None

    def blocked_cleanup():
        workers.append(get_ident())
        result["released"] = allow_cleanup.wait(2)
        real_quit()
        cleaned.set()

    monkeypatch.setattr(old, "quit", blocked_cleanup)

    class TestApp(RadioWaveApp):
        @property
        def service(self):
            if self._service is None:
                result["created_after_cleanup"] = cleaned.is_set()
                self._service = create_service("new")
            return self._service

    app = TestApp(service=old)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    main_thread = get_ident()

    def close_first():
        started = monotonic()
        first_window.close()
        result["close_latency"] = monotonic() - started
        result["hidden"] = not first_window.get_visible() and first_window._timer_id is None
        result["prepared"] = getattr(old, "_shutdown_prepared", False)
        first_window.close()
        app.activate()
        result["not_reopened_yet"] = app.get_active_window() is None
        GLib.timeout_add(1, advance_cleanup)
        return GLib.SOURCE_REMOVE

    def advance_cleanup():
        if not workers:
            return GLib.SOURCE_CONTINUE
        result["main_loop_live_during_cleanup"] = not cleaned.is_set()
        allow_cleanup.set()
        return GLib.SOURCE_REMOVE

    def activated(_app):
        nonlocal first_window
        window = app.get_active_window()
        if first_window is None:
            first_window = window
            GLib.idle_add(close_first)
        elif window is not None and window is not first_window:
            result["reopened"] = window.get_visible()
            window.close()

    app.connect_after("activate", activated)
    watchdog = GLib.timeout_add_seconds(4, lambda: (Gio.Application.quit(app), GLib.SOURCE_REMOVE)[1])
    try:
        assert app.run(["radio-wave"]) == 0
    finally:
        allow_cleanup.set()
        if GLib.MainContext.default().find_source_by_id(watchdog) is not None:
            GLib.source_remove(watchdog)
    assert result["hidden"] and result["close_latency"] < 0.5
    assert result["prepared"] and result["not_reopened_yet"] and result["released"] and result["main_loop_live_during_cleanup"]
    assert result["created_after_cleanup"] and result["reopened"]
    assert len(workers) == 1 and workers[0] != main_thread and cleaned.is_set()
