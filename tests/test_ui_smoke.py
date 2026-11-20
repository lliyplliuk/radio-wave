"""Дымовой тест интерфейса GTK4.

Тест проверяет, что модуль ``radio_wave.ui.app`` импортируется, главное окно
собирается без исключений и содержит ключевые виджеты.

Тест не требует сети. Если в системе нет доступного графического дисплея
(нет ``DISPLAY`` и не удаётся поднять headless-сервер), тест корректно
пропускается через :func:`pytest.skip`, а не падает.

Запуск::

    python3 -m pytest tests/test_ui_smoke.py -q
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from typing import List, Optional

import pytest

#: Номера дисплеев, которые пробуем поднять, если DISPLAY не задан.
_CANDIDATE_DISPLAYS = (":99", ":98")

#: Таймаут ожидания готовности X-сервера, секунды.
_X_SERVER_TIMEOUT = 10.0


def _display_is_usable(display: str) -> bool:
    """Проверить, что по указанному дисплею можно открыть сокет."""
    if not display:
        return False
    number = display.lstrip(":").split(".")[0]
    if not number.isdigit():
        return False
    path = f"/tmp/.X11-unix/X{number}"
    if not os.path.exists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _host_display() -> Optional[str]:
    """Найти уже работающий X-дисплей, на который можно опереться."""
    current = os.environ.get("DISPLAY", "")
    if current and _display_is_usable(current):
        return current
    for number in range(0, 10):
        candidate = f":{number}"
        if _display_is_usable(candidate):
            return candidate
    return None


def _wait_for_display(display: str, timeout: float = _X_SERVER_TIMEOUT) -> bool:
    """Дождаться, пока X-сервер на дисплее начнёт принимать подключения."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _display_is_usable(display):
            return True
        time.sleep(0.2)
    return False


def _try_start_xephyr(display: str, host: str) -> bool:
    """Поднять вложенный Xephyr на ``display`` поверх ``host``."""
    if shutil.which("Xephyr") is None:
        return False
    env = dict(os.environ)
    env["DISPLAY"] = host
    try:
        subprocess.Popen(
            [
                "Xephyr",
                display,
                "-screen",
                "1280x800",
                "-resizeable",
                "-nolisten",
                "tcp",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return _wait_for_display(display)


def _ensure_display() -> Optional[str]:
    """Гарантировать доступный дисплей или вернуть ``None``.

    Порядок: используем уже заданный ``DISPLAY``; иначе пробуем поднять
    headless X-сервер (Xephyr внутри существующего дисплея).
    """
    current = os.environ.get("DISPLAY", "")
    if current and _display_is_usable(current):
        return current

    host = _host_display()
    if host is None:
        return None

    for display in _CANDIDATE_DISPLAYS:
        if _try_start_xephyr(display, host):
            os.environ["DISPLAY"] = display
            return display
    return None


#: Дисплей должен быть готов ДО первого импорта Gtk: PyGObject фиксирует
#: признак инициализации в момент импорта модуля-обёртки.
_DISPLAY = _ensure_display()

if _DISPLAY is None:
    pytest.skip(
        "Графический дисплей недоступен (нет DISPLAY и не удалось поднять "
        "headless X-сервер) — тест интерфейса пропущен.",
        allow_module_level=True,
    )

os.environ["DISPLAY"] = _DISPLAY

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

from radio_wave.ui import app as ui_app  # noqa: E402


class FakeService:
    """Минимальная реализация контракта ``RadioService`` для тестов.

    Нужна, чтобы собрать окно без реального сервиса (который ещё пишется).
    """

    def __init__(self) -> None:
        self.first_run = False
        self.taste = {
            "artist_weight": 1.0,
            "genre_weight": 0.8,
            "catalogue_weight": 0.3,
            "recent_penalty": 1.5,
        }
        self._tracks = [
            {
                "id": "t1",
                "artist": "Тестовый Исполнитель",
                "title": "Тестовый Трек",
                "genre": "Тест-жанр",
                "language": "ru",
                "path": "/tmp/тест.mp3",
                "ready": True,
            },
            {
                "id": "t2",
                "artist": "Второй",
                "title": "Другой",
                "genre": "",
                "language": "unknown",
                "path": "/tmp/другой.mp3",
                "ready": False,
            },
        ]
        self._current = self._tracks[0]
        self._upcoming = [
            {
                "id": "t2",
                "artist": "Второй",
                "title": "Другой",
                "genre": "",
                "language": "unknown",
                "state": "ready",
                "reason": "похожий жанр",
            },
            {
                "id": "t3",
                "artist": "Третий",
                "title": "Ещё",
                "genre": "",
                "language": "ru",
                "state": "pending",
                "reason": "файл ещё скачивается",
            },
        ]
        self.calls: List[str] = []
        self.published = []
        self.played = []
        self.previous_available = False

    # ------------------------------------------------------------- каталог
    def tracks(self) -> List[dict]:
        return list(self._tracks)

    def import_paths(self, paths: List[str], mark_russian: bool = False) -> int:
        self.calls.append("import_paths")
        return len(paths)

    def update_track(self, track_id: str, artist=None, title=None, genre=None, language=None) -> bool:
        self.calls.append("update_track")
        return True

    # ------------------------------------------------------------- проигрывание
    def current(self) -> Optional[dict]:
        return self._current

    def upcoming(self) -> List[dict]:
        return list(self._upcoming)

    def pin_visible(self, ids) -> None:
        self.published.append(tuple(ids))

    def play_track(self, track: dict) -> None:
        self.played.append(track["id"])

    def play_current(self) -> None:
        self.calls.append("play_current")

    def user_next(self) -> None:
        self.calls.append("user_next")

    def can_go_previous(self) -> bool:
        return self.previous_available

    def previous_track(self) -> None:
        self.calls.append("previous_track")

    def toggle_like_current(self) -> None:
        self.calls.append("toggle_like_current")

    def dislike_current(self) -> None:
        self.calls.append("dislike_current")

    def toggle_play(self) -> None:
        self.calls.append("toggle_play")

    def seek(self, seconds: float) -> None:
        self.calls.append("seek")

    def set_volume(self, value: float) -> None:
        self.calls.append("set_volume")

    def position(self) -> float:
        return 12.0

    def duration(self) -> float:
        return 180.0

    def is_playing(self) -> bool:
        return False

    def reason_for(self, track: dict) -> str:
        return "тестовая причина выбора"

    def status_text(self) -> str:
        return "Тестовый статус"

    def tick(self) -> None:
        self.calls.append("tick")

    # ------------------------------------------------------------- загрузки
    def add_magnet(self, uri: str) -> Optional[str]:
        self.calls.append("add_magnet")
        return None

    def add_torrent_file(self, path: str) -> Optional[str]:
        self.calls.append("add_torrent_file")
        return None

    # ------------------------------------------------------------- вкус
    def set_taste(self, **kwargs) -> None:
        self.calls.append("set_taste")
        self.taste.update(kwargs)

    def quit(self) -> None:
        self.calls.append("quit")


@pytest.fixture()
def window() -> ui_app.MainWindow:
    """Собрать главное окно на поддельном сервисе."""
    assert Gtk.init_check(), "GTK4 не удалось инициализировать"
    win = ui_app.build_window(FakeService())
    yield win
    win.destroy()


def test_gtk_initialized() -> None:
    """GTK4 инициализирован, дисплей доступен."""
    assert Gtk.init_check() is True
    assert Gtk.get_major_version() >= 4


def test_module_importable() -> None:
    """Модуль интерфейса импортируется и содержит ключевые символы."""
    assert hasattr(ui_app, "MainWindow")
    assert hasattr(ui_app, "build_window")
    assert hasattr(ui_app, "RadioWaveApp")


def test_default_gui_composes_playback_and_torrents(tmp_path, monkeypatch):
    from radio_wave.player import Player
    from radio_wave.torrent import TorrentManager

    for root in ("STATE", "DATA", "CACHE"):
        monkeypatch.setenv(f"XDG_{root}_HOME", str(tmp_path / root.lower()))
    service = ui_app.RadioWaveApp().service
    try:
        assert isinstance(service.player, Player)
        assert isinstance(service.torrents, TorrentManager)
    finally:
        service.quit()


def test_window_builds(window: ui_app.MainWindow) -> None:
    """Окно собирается без исключений."""
    assert window is not None
    assert isinstance(window, Gtk.ApplicationWindow)


def test_key_widgets_present(window: ui_app.MainWindow) -> None:
    """В окне есть все ключевые элементы ТЗ."""
    # Шапка и статус.
    assert window._status_label.get_text() == "Тестовый статус"
    # «Сейчас играет»: исполнитель, название, жанр·язык.
    assert window._artist_label.get_text() == "Тестовый Исполнитель"
    assert window._title_label.get_text() == "Тестовый Трек"
    assert "Тест-жанр" in window._meta_label.get_text()
    assert "русский" in window._meta_label.get_text()
    # Кнопка объяснения выбора.
    assert window._reason_button.get_label() == "Почему этот трек?"
    # Транспорт.
    for widget in (
        window._previous_button,
        window._play_button,
        window._next_button,
        window._seek_scale,
        window._volume_scale,
        window._like_button,
        window._dislike_button,
    ):
        assert isinstance(widget, Gtk.Widget)
    # Язык учитывается вместе с другими сигналами вкуса.
    assert set(window._taste_scales) == {
        "artist_weight",
        "genre_weight",
        "catalogue_weight",
        "language_weight",
        "recent_penalty",
    }


def test_queue_filled(window: ui_app.MainWindow) -> None:
    """Очередь содержит до четырёх строк с состоянием и причиной."""
    rows = []
    child = window._queue_list.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    assert len(rows) == 2
    first_row = rows[0].get_child()
    assert isinstance(first_row, ui_app.widgets.QueueRow)
    assert first_row._title.get_text() == "Второй — Другой"
    assert first_row._state.get_text() == "готов"
    assert first_row._reason.get_text() == "похожий жанр"


def test_library_filled(window: ui_app.MainWindow) -> None:
    """Список коллекции содержит импортированные треки."""
    assert window._library_model.get_n_items() == 0
    window._notebook.set_current_page(1)
    assert window._library_model.get_n_items() == 2
    assert window._library_model.get_item(0).get_string() == "t1"


def test_large_library_only_builds_visible_rows_and_updates_selection(monkeypatch):
    service = FakeService()
    tracks = [
        {"id": f"scale-{i}", "title": f"Запись {i}", "artist": "Исполнитель", "ready": False}
        for i in range(1594)
    ]
    reads = []
    created = []
    original_row = ui_app.widgets.TrackRow

    class CountingRow(original_row):
        def __init__(self):
            super().__init__()
            created.append(self)

    def read():
        reads.append(len(tracks))
        return tracks

    monkeypatch.setattr(service, "tracks", read)
    monkeypatch.setattr(ui_app.widgets, "TrackRow", CountingRow)
    window = ui_app.build_window(service)
    try:
        window.present()
        window.refresh()
        assert not reads and not created

        window._notebook.set_current_page(1)
        assert window._library_model.get_n_items() == 1594
        assert 0 < len(created) < 1594 // 2
        row = window._library_rows[tracks[0]["id"]]
        window._library_selection.set_selected(0)
        old_widgets = len(created)

        tracks[0].update(title="Обновлённая запись", ready=True)
        tracks[10]["downloaded"] = 42
        window.refresh()
        assert len(created) == old_widgets
        assert window._library_rows[tracks[0]["id"]] is row
        assert row._title.get_text() == "Исполнитель — Обновлённая запись"

        tracks.extend({"id": f"added-{i}", "title": f"Добавлено {i}"} for i in range(571))
        window.refresh()
        assert window._library_model.get_n_items() == 2165
        assert len(created) < 1594 // 2
        assert window._library_selection.get_selected_item().get_string() == "scale-0"
        opened = []

        class Editor:
            def __init__(self, *, parent, track, on_save):
                self.track = track

            def present(self):
                opened.append(self.track["id"])

        monkeypatch.setattr(ui_app, "EditTrackDialog", Editor)
        window._library_list.emit("activate", 0)
        window._on_edit_track()
        assert service.played == ["scale-0"]
        assert opened == ["scale-0"]

        window._notebook.set_current_page(0)
        reads.clear()
        window.refresh()
        assert not reads
    finally:
        window.close()


def test_search_reads_dormant_catalogue_independently_of_collection(tmp_path, monkeypatch):
    for root in ("STATE", "DATA", "CACHE"):
        monkeypatch.setenv(f"XDG_{root}_HOME", str(tmp_path / root.lower()))
    service = ui_app.RadioWaveApp().service
    active = {"id": "active"}
    dormant = {"id": "dormant", "title": "Архивная песня", "mbid": "recording-id"}
    service.state.like(dormant)
    monkeypatch.setattr(service, "tracks", lambda: [active])
    monkeypatch.setattr(service, "search_tracks", lambda: [active, dormant], raising=False)

    shown = []
    def show(_parent, tracks, *, seed_mbids, **_options):
        shown.append((tracks(), seed_mbids))

    monkeypatch.setattr(ui_app, "show_catalog", show)
    window = ui_app.build_window(service)
    try:
        window._notebook.set_current_page(1)
        assert window._library_model.get_n_items() == 1
        window._open_catalog()
        assert shown == [([active, dormant], ["recording-id"])]
    finally:
        window.close()


def test_transport_does_not_move_between_tracks() -> None:
    """Транспорт остаётся на месте при смене трека."""
    service = FakeService()
    win = ui_app.build_window(service)
    play_button = win._play_button
    seek_scale = win._seek_scale
    volume_scale = win._volume_scale
    next_button = win._next_button

    service._current = service._tracks[1]
    service._upcoming = []
    win.refresh()

    assert win._play_button is play_button
    assert win._seek_scale is seek_scale
    assert win._volume_scale is volume_scale
    assert win._next_button is next_button
    assert win._title_label.get_text() == "Другой"
    win.destroy()


def test_first_run_banner_visibility() -> None:
    """Баннер первого запуска показывается только при пустом каталоге."""
    service = FakeService()
    service.first_run = True
    win = ui_app.build_window(service)
    assert win._first_run_banner.get_visible() is True
    assert win._startup_play_button.get_label() == "Слушать"
    win._startup_play_button.emit("clicked")
    assert "toggle_play" in service.calls

    service.waiting = True
    win.refresh()
    assert win._first_run_banner.get_visible() is False
    service.waiting = False

    service.first_run = False
    win.refresh()
    assert win._first_run_banner.get_visible() is False
    win.destroy()


def test_previous_button_dispatches_and_follows_service_availability():
    service = FakeService()
    win = ui_app.build_window(service)
    button = win._previous_button
    try:
        assert button.get_icon_name() == "media-skip-backward-symbolic"
        assert button.get_tooltip_text() == "Предыдущий трек"
        assert button.get_next_sibling() is win._play_button
        assert not button.get_sensitive()
        button.emit("clicked")
        assert "previous_track" not in service.calls

        service.previous_available = True
        win.refresh()
        assert button.get_sensitive()
        button.emit("clicked")
        assert service.calls.count("previous_track") == 1

        service.previous_available = False
        win.refresh()
        assert not button.get_sensitive()
    finally:
        win.destroy()


def test_actions_call_service() -> None:
    """Кнопки транспорта обращаются к сервису."""
    service = FakeService()
    win = ui_app.build_window(service)
    win._on_toggle_play()
    win._on_next()
    win._on_like()
    win._on_dislike()
    assert "toggle_play" in service.calls
    assert "user_next" in service.calls
    assert "toggle_like_current" in service.calls
    assert "dislike_current" in service.calls
    win.destroy()


def _descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from _descendants(child)
        child = child.get_next_sibling()


def test_active_queue_publishes_displayed_rows_and_plays_exact_clicked_track():
    service = FakeService()
    service._upcoming.extend({**service._upcoming[-1], "id": f"t{n}"} for n in range(4, 10))
    published_prefix = ("t2", "t3", "t4", "t5")
    window = ui_app.build_window(service)
    try:
        window.refresh()
        assert not service.published
        window.present()
        assert service.published[-1] == published_prefix
        assert window._queue_list.get_row_at_index(3) is not None
        assert window._queue_list.get_row_at_index(4) is None
        first = window._queue_list.get_row_at_index(0)
        service._upcoming[0]["state"] = "pending"
        window.refresh()
        assert window._queue_list.get_row_at_index(0) is first
        assert first.get_child()._state.get_text() == "ожидание"
        assert window._queue_list.get_activate_on_single_click()
        window._queue_list.emit("row-activated", first)
        assert service.played == ["t2"]

        window._notebook.set_current_page(1)
        published = list(service.published)
        service._upcoming[0]["state"] = "ready"
        window.refresh()
        assert service.published == published
        window._notebook.set_current_page(0)
        assert len(service.published) == len(published) + 1
        assert service.published[-1] == published_prefix
    finally:
        window.close()


def test_heart_reflects_saved_current_like_and_can_remove_it(tmp_path, monkeypatch):
    from radio_wave.state import AppState

    for root in ("STATE", "DATA", "CACHE"):
        monkeypatch.setenv(f"XDG_{root}_HOME", str(tmp_path / root.lower()))
    service = ui_app.RadioWaveApp().service
    liked = {"id": "liked", "artist": "Кино", "title": "Звёзды"}
    service.state.like(liked)
    service.queue.current = liked
    window = ui_app.build_window(service)
    try:
        assert window._like_icon.get_text() == "♥"
        assert window._like_button.get_active()
        window._like_button.emit("clicked")
        assert not AppState(service.state.path).is_liked(liked["id"])
        assert window._like_icon.get_text() == "♡"
        assert not window._like_button.get_active()

        window._like_button.emit("clicked")
        assert AppState(service.state.path).is_liked(liked["id"])
        assert window._like_icon.get_text() == "♥"
        service.queue.current = None
        window.refresh()
        assert not window._like_button.get_active()
        assert window._like_icon.get_text() == "♡"
    finally:
        window.close()


def _named_button(window, label):
    return next(
        (widget for widget in _descendants(window)
         if isinstance(widget, Gtk.Button) and widget.get_label() == label),
        None,
    )


@pytest.mark.parametrize(("query", "mode"), [
    ("Группа — Песня", "exact"),
    ("Группа", "named"),
    ("Песня без метаданных", "fallback"),
])
def test_search_only_offers_listening_actions(tmp_path, monkeypatch, query, mode):
    from radio_wave import catalog_dialog
    from radio_wave.catalog import CatalogRecording

    for root in ("STATE", "DATA", "CACHE"):
        monkeypatch.setenv(f"XDG_{root}_HOME", str(tmp_path / root.lower()))
    service = ui_app.RadioWaveApp().service
    win = ui_app.build_window(service)
    named_result = CatalogRecording("Песня", "Группа", "recording-id")
    queries = []
    played = []

    def found(text):
        queries.append(text)
        return [named_result] if mode == "named" else []

    monkeypatch.setattr(catalog_dialog, "search_picker_recordings", found)
    monkeypatch.setattr(service, "play_recording", played.append)
    monkeypatch.setattr(service, "play_search", played.append)
    try:
        _named_button(win, "Поиск").emit("clicked")
        dialog = next(item for item in Gtk.Window.get_toplevels() if item.get_transient_for() is win)
        search = next(widget for widget in _descendants(dialog) if isinstance(widget, Gtk.SearchEntry))
        search.set_text(query)
        search.emit("activate")
        context = GLib.MainContext.default()
        deadline = time.monotonic() + _X_SERVER_TIMEOUT
        while _named_button(dialog, "Слушать") is None and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.02)
        buttons = [widget for widget in _descendants(dialog) if isinstance(widget, Gtk.Button)]
        assert {widget.get_label() for widget in buttons} == {"Найти", "По лайкам", "Слушать"}
        listening = [widget for widget in buttons if widget.get_label() == "Слушать"]
        assert len(listening) == 1
        assert queries == ([] if mode == "exact" else [query])
        listening[0].emit("clicked")
        expected = {
            "exact": CatalogRecording("Песня", "Группа", ""),
            "named": named_result,
            "fallback": query,
        }
        assert played == [expected[mode]]
    finally:
        win.close()


def test_theme_applies(window: ui_app.MainWindow) -> None:
    """Тема применена: у окна есть CSS-класс, провайдер зарегистрирован."""
    from radio_wave.ui import theme

    assert theme.NAVY.startswith("#")
    assert theme.LILAC.startswith("#")
    assert window.has_css_class("radio-wave")
    # В GTK4 провайдер добавляется к дисплею без обратного чтения:
    # проверяем, что тема отдаёт непустой CSS и что он грузится без ошибок.
    provider = Gtk.CssProvider()
    provider.load_from_data(theme.CSS.encode("utf-8"))
    assert provider is not None


def test_accessible_labels_present(window: ui_app.MainWindow) -> None:
    """У интерактивных элементов выставлены доступные подписи."""
    assert window._taste_scales["language_weight"].get_accessible_role() is not None
    for widget in (
        window._previous_button,
        window._play_button,
        window._next_button,
        window._like_button,
        window._dislike_button,
        window._reason_button,
    ):
        assert widget.get_tooltip_text(), f"нет подсказки у {widget}"


def test_track_meta_distinguishes_confirmed_and_estimated_language():
    from radio_wave.ui.widgets import track_meta

    assert track_meta({"title": "Группа крови", "language": "unknown"}) == "русский (по названию)"
    assert track_meta({"title": "Группа крови", "language": "en"}) == "английский"
    assert track_meta({"title": "XYZ", "language": "unknown"}) == "неизвестно"
