"""Главное окно приложения «Радио из коллекции».

Один :class:`Gtk.ApplicationWindow` с вертикальной компоновкой:

1. шапка — название приложения, статус и режим;
2. блок «Сейчас играет» — крупная типографика и кнопка «Почему этот трек?»;
3. вкладки «Дальше» / «Коллекция» / «Вкус»;
4. закреплённый внизу транспорт.

Весь обмен данными идёт через :class:`radio_wave.service.RadioService`.
Модуль намеренно импортирует сервис лениво, чтобы ``radio_wave.ui.app``
оставался импортируемым даже до появления ``radio_wave/service.py``.
"""

from __future__ import annotations

from importlib import import_module
import os
import sys
from threading import Thread
from time import perf_counter

import gi

gi.require_version("Gtk", "4.0")

from typing import Any, Callable, Dict, List, Optional

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from radio_wave import config  # noqa: E402
from radio_wave.catalog_dialog import show_catalog  # noqa: E402
from radio_wave.diagnostics import playback_log, trace  # noqa: E402
from radio_wave.mpris import MprisPlayer  # noqa: E402

from . import theme, widgets  # noqa: E402

APP_ID = "org.radio-wave.RadioWave"

#: Как часто опрашивать сервис (мс).
TICK_INTERVAL_MS = 500

#: Порядок языков в выпадающем списке редактора трека.
_LANGUAGE_CODES = widgets.LANGUAGE_CODES
_LANGUAGE_NAMES = [widgets.LANGUAGE_LABELS[code] for code in _LANGUAGE_CODES]


def _service_shutdown(service: Any, method: str) -> None:
    action = getattr(service, method, None)
    if action is not None:
        try:
            action()
        except Exception as error:
            playback_log.error("cleanup_failed stage=%s error_class=%s", method, type(error).__name__)


def _start_cleanup(cleanup: Callable[[], Any]) -> bool:
    try:
        Thread(target=cleanup, name="radio-shutdown", daemon=False).start()
    except RuntimeError as error:
        playback_log.error("cleanup_failed stage=worker_start error_class=%s", type(error).__name__)
        cleanup()
    return GLib.SOURCE_REMOVE


class RadioWaveApp(Gtk.Application):
    """Приложение GTK4, владеющее окном и сервисом."""

    def __init__(self, service: Optional[Any] = None) -> None:
        super().__init__(application_id=APP_ID)
        self._service = service
        self._window: Optional["MainWindow"] = None
        self._media_keys: MprisPlayer | None = None
        self._audio_closed = False
        self._reopen_requested = False
        self._launch_cleanup: Callable[[], bool] | None = None

    @property
    def service(self) -> Any:
        """Сервис приложения; создаётся при первом обращении."""
        if self._service is None:
            from radio_wave.player import Player
            from radio_wave.service import RadioService  # noqa: PLC0415
            from radio_wave.torrent import TorrentManager

            import_module("libtorrent")
            config.ensure_dirs()
            player = Player()
            torrents = TorrentManager(config.downloads_dir())
            try:
                self._service = RadioService(player=player, torrents=torrents)
            except (OSError, RuntimeError, ValueError):
                player.shutdown()
                torrents.shutdown()
                raise
        return self._service

    def do_activate(self) -> None:  # noqa: N802 (имя из GTK)
        """Показать главное окно."""
        if self._audio_closed:
            self._reopen_requested = True
            return
        if self._window is None:
            try:
                service = self.service
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                message = f"Проверь, что установлены GStreamer и libtorrent.\n\n{error}"
                print(f"Радио не запустилось. {message}", file=sys.stderr)
                self.hold()
                alert = Gtk.AlertDialog(message="Радио не запустилось", detail=message)
                alert.choose(None, None, lambda *_args: self.release())
                return
            self._window = build_window(service, application=self)
            connection = self.get_dbus_connection()
            if (connection is not None and getattr(service, "player", None) is not None
                    and hasattr(service, "playback_status")):
                try:
                    self._media_keys = MprisPlayer(connection, service, self._window.present)
                except GLib.Error as error:
                    playback_log.info("mpris_unavailable error_class=%s", type(error).__name__)
        self._window.present()

    def refresh_media_keys(self) -> None:
        if self._media_keys is not None:
            self._media_keys.refresh()

    def _shutdown_audio(self, *, keep_running: bool = True) -> None:
        if self._audio_closed:
            return
        self._audio_closed = True
        media, self._media_keys = self._media_keys, None
        _service_shutdown(media, "detach")
        service = self._service
        if service is None and media is None:
            return
        _service_shutdown(service, "begin_shutdown")
        if keep_running:
            self.hold()

        def cleanup():
            started = perf_counter()
            try:
                _service_shutdown(service, "quit")
            finally:
                _service_shutdown(media, "release_name")
                trace("gtk_cleanup_finished", elapsed_ms=round((perf_counter() - started) * 1000))
                if keep_running:
                    GLib.idle_add(self._cleanup_done)
            return GLib.SOURCE_REMOVE

        def launch() -> bool:
            if self._launch_cleanup is not launch:
                return GLib.SOURCE_REMOVE
            self._launch_cleanup = None
            return _start_cleanup(cleanup)

        self._launch_cleanup = launch
        if keep_running:
            GLib.idle_add(launch)
        else:
            launch()

    def _cleanup_done(self) -> bool:
        self._window = None
        self._service = None
        self._audio_closed = False
        reopen, self._reopen_requested = self._reopen_requested, False
        try:
            if reopen:
                self.activate()
        finally:
            self.release()
        return GLib.SOURCE_REMOVE

    def quit(self) -> None:
        self._reopen_requested = False
        if self._audio_closed:
            return
        if self._window is not None:
            self._window.close()
        elif self._service is not None and self.get_is_registered():
            self._shutdown_audio()
        else:
            super().quit()

    def do_shutdown(self) -> None:  # noqa: N802
        if self._window is not None:
            self._window._stop_timer()
        if self._launch_cleanup is not None:
            self._launch_cleanup()
        else:
            self._shutdown_audio(keep_running=False)
        Gtk.Application.do_shutdown(self)


class MainWindow(Gtk.ApplicationWindow):
    """Главное окно: шапка, «Сейчас играет», вкладки и транспорт."""

    def __init__(self, application: Optional[Gtk.Application], service: Any) -> None:
        super().__init__(application=application, title="Радио из коллекции")
        self.service = service
        self.set_default_size(960, 720)
        self.set_size_request(560, 480)

        self._updating = False  # защита от рекурсии при обновлении виджетов
        self._seek_dragging = False
        self._seek_drag_start_x = 0.0
        self._seek_drag_track_id: Optional[str] = None
        self._timer_id: Optional[int] = None
        self._closing = False
        self._library_tracks: dict[str, Dict[str, Any]] = {}
        self._library_order: tuple[str, ...] = ()
        self._library_rows: dict[str, widgets.TrackRow] = {}
        self._queue_rows: dict[str, Gtk.ListBoxRow] = {}
        self._queue_tracks: dict[str, Dict[str, Any]] = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_hexpand(True)
        self.set_child(root)

        root.append(self._build_header())
        root.append(self._build_first_run_banner())
        root.append(self._build_now_playing())
        root.append(self._build_notebook())
        root.append(self._build_transport())

        self._timer_id = GLib.timeout_add(TICK_INTERVAL_MS, self._on_tick)
        self.connect("close-request", self._on_close_request)
        self.refresh()

    # ------------------------------------------------------------ сборка
    def _build_header(self) -> Gtk.Widget:
        """Шапка приложения со статусом и поиском."""
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.add_css_class("rw-header")

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.set_hexpand(True)
        app_title = Gtk.Label(label="Радио из коллекции")
        app_title.set_xalign(0.0)
        app_title.add_css_class("rw-app-title")
        self._status_label = Gtk.Label(label="")
        self._status_label.set_xalign(0.0)
        self._status_label.add_css_class("rw-app-status")
        titles.append(app_title)
        titles.append(self._status_label)
        header.append(titles)

        for label, callback in (("Поиск", self._open_catalog), ("Новое", self._open_discovery)):
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.connect("clicked", callback)
            header.append(button)

        return header

    def _build_first_run_banner(self) -> Gtk.Widget:
        """Начать радио одним нажатием при пустой коллекции."""
        banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        banner.add_css_class("rw-header")
        banner.set_margin_top(8)
        banner.set_margin_start(18)
        banner.set_margin_end(18)

        self._startup_play_button = Gtk.Button(label="Слушать", halign=Gtk.Align.START)
        self._startup_play_button.add_css_class("suggested-action")
        self._startup_play_button.set_tooltip_text("Найти и запустить первую песню")
        self._startup_play_button.connect("clicked", lambda _b: self._on_toggle_play())
        banner.append(self._startup_play_button)

        self._first_run_banner = banner
        return banner

    def _build_now_playing(self) -> Gtk.Widget:
        """Блок «Сейчас играет»: только типографика, без обложек."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_bottom(12)

        self._artist_label = Gtk.Label(label="")
        self._artist_label.set_xalign(0.0)
        self._artist_label.set_ellipsize(3)
        self._artist_label.add_css_class("rw-now-artist")

        self._title_label = Gtk.Label(label="")
        self._title_label.set_xalign(0.0)
        self._title_label.set_ellipsize(3)
        self._title_label.add_css_class("rw-now-title")

        self._meta_label = Gtk.Label(label="")
        self._meta_label.set_xalign(0.0)
        self._meta_label.add_css_class("rw-now-meta")

        box.append(self._artist_label)
        box.append(self._title_label)
        box.append(self._meta_label)

        self._reason_popover = Gtk.Popover()
        self._reason_popover.set_autohide(True)
        reason_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        reason_box.set_margin_top(10)
        reason_box.set_margin_bottom(10)
        reason_box.set_margin_start(10)
        reason_box.set_margin_end(10)
        self._reason_label = Gtk.Label(label="")
        self._reason_label.set_wrap(True)
        self._reason_label.set_max_width_chars(48)
        self._reason_label.set_xalign(0.0)
        reason_box.append(self._reason_label)
        self._reason_popover.set_child(reason_box)

        self._reason_button = Gtk.Button(label="Почему этот трек?")
        self._reason_button.set_halign(Gtk.Align.START)
        self._reason_button.set_margin_top(8)
        self._reason_button.connect("clicked", self._on_why_clicked)
        widgets.set_accessible_label(
            self._reason_button,
            "Почему этот трек?",
            "Показать причину, по которой выбран текущий трек",
        )
        self._reason_button.set_tooltip_text(
            "Показать причину, по которой выбран текущий трек"
        )
        self._reason_popover.set_parent(self._reason_button)
        box.append(self._reason_button)

        return box

    def _build_notebook(self) -> Gtk.Widget:
        """Вкладки: очередь «Дальше», панель «Коллекция», крутилки вкуса."""
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        notebook.set_margin_start(12)
        notebook.set_margin_end(12)
        notebook.set_margin_bottom(12)

        notebook.append_page(self._build_queue_page(), Gtk.Label(label="Дальше"))
        notebook.append_page(self._build_library_page(), Gtk.Label(label="Коллекция"))
        notebook.append_page(self._build_taste_page(), Gtk.Label(label="Вкус"))
        self._notebook = notebook
        notebook.connect("switch-page", self._on_page_switched)
        return notebook

    def _build_queue_page(self) -> Gtk.Widget:
        """Плотная очередь из четырёх строк."""
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

        self._queue_list = Gtk.ListBox()
        self._queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._queue_list.set_activate_on_single_click(True)
        self._queue_list.connect("row-activated", self._on_queue_row_activated)
        self._queue_list.connect("map", lambda _list: self._refresh_queue())
        empty = Gtk.Label(label="Очередь пока пуста", xalign=0)
        empty.add_css_class("rw-app-status")
        empty.set_margin_top(10)
        empty.set_margin_start(10)
        self._queue_empty_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=empty)
        widgets.set_accessible_label(
            self._queue_list, "Очередь", "Следующие треки радио"
        )
        scroller.set_child(self._queue_list)
        return scroller

    def _build_library_page(self) -> Gtk.Widget:
        """Панель коллекции: импорт, список треков, редактирование."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_top(10)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_bottom(10)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_button = Gtk.Button(label="Импорт музыки")
        folder_button.connect("clicked", lambda _b: self._on_import_folder())
        magnet_button = Gtk.Button(label="Magnet-ссылка")
        magnet_button.connect("clicked", self._focus_magnet)
        torrent_button = Gtk.Button(label="Файл .torrent")
        torrent_button.connect("clicked", lambda _b: self._on_import_torrent())
        edit_button = Gtk.Button(label="Изменить выбранный")
        edit_button.connect("clicked", lambda _b: self._on_edit_track())
        for button in (folder_button, magnet_button, torrent_button, edit_button):
            widgets.set_accessible_label(button, button.get_label() or "")
            toolbar.append(button)
        outer.append(toolbar)

        magnet_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._magnet_entry = Gtk.Entry()
        self._magnet_entry.set_hexpand(True)
        self._magnet_entry.set_placeholder_text("magnet:?xt=urn:btih:…")
        widgets.set_accessible_label(
            self._magnet_entry, "Magnet-ссылка", "Ссылка magnet для загрузки трека"
        )
        magnet_add = Gtk.Button(label="Добавить")
        magnet_add.connect("clicked", lambda _b: self._on_import_magnet())
        self._magnet_entry.connect("activate", lambda _entry: self._on_import_magnet())
        widgets.set_accessible_label(magnet_add, "Добавить magnet-ссылку")
        magnet_box.append(self._magnet_entry)
        magnet_box.append(magnet_add)
        outer.append(magnet_box)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self._library_model = Gtk.StringList.new([])
        self._library_selection = Gtk.SingleSelection.new(self._library_model)
        self._library_selection.set_autoselect(False)
        self._library_selection.set_can_unselect(True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_library_row)
        factory.connect("bind", self._bind_library_row)
        factory.connect("unbind", self._unbind_library_row)
        self._library_list = Gtk.ListView.new(self._library_selection, factory)
        self._library_list.set_single_click_activate(True)
        self._library_list.connect("activate", self._on_library_row_activated)
        widgets.set_accessible_label(
            self._library_list, "Список треков коллекции"
        )
        scroller.set_child(self._library_list)
        outer.append(scroller)

        self._library_empty = Gtk.Label(label="В коллекции пока нет треков")
        self._library_empty.add_css_class("rw-app-status")
        self._library_empty.set_xalign(0.0)
        outer.append(self._library_empty)

        return outer

    def _build_taste_page(self) -> Gtk.Widget:
        """Крутилки вкуса."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(14)
        page.set_margin_start(14)
        page.set_margin_end(14)
        page.set_margin_bottom(14)

        taste: Dict[str, float] = dict(self.service.taste or {})
        self._taste_scales: Dict[str, Gtk.Scale] = {}

        specs = [
            ("artist_weight", "Вес исполнителя", 0.0, 3.0, 0.05),
            ("genre_weight", "Вес жанра", 0.0, 3.0, 0.05),
            ("catalogue_weight", "Вес каталога", 0.0, 3.0, 0.05),
            ("language_weight", "Вес языка", 0.0, 3.0, 0.05),
            ("recent_penalty", "Штраф за недавнее", 0.0, 5.0, 0.05),
        ]
        for key, title, lower, upper, step in specs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            caption = Gtk.Label(label=title)
            caption.set_size_request(170, -1)
            caption.set_xalign(0.0)
            caption.add_css_class("rw-now-meta")
            scale = widgets.make_scale(
                title, float(taste.get(key, 0.0)), lower, upper, step, self._on_taste_changed
            )
            row.append(caption)
            row.append(scale)
            page.append(row)
            self._taste_scales[key] = scale

        note = Gtk.Label(
            label="Чем выше вес языка, тем чаще в очереди звучат песни на языках, которые вы лайкаете."
        )
        note.set_wrap(True)
        note.set_xalign(0.0)
        note.add_css_class("rw-app-status")
        page.append(note)
        return page

    def _build_transport(self) -> Gtk.Widget:
        """Закреплённый транспорт: воспроизведение, навигация, громкость, оценки."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.add_css_class("rw-transport")

        self._like_icon = Gtk.Label(label="♡")
        self._like_button = Gtk.ToggleButton(child=self._like_icon)
        self._like_button.connect("clicked", lambda _b: self._on_like())
        widgets.set_accessible_label(
            self._like_button, "Нравится", "Добавить или убрать лайк текущего трека"
        )
        self._like_button.set_tooltip_text("Нравится")

        self._dislike_button = Gtk.Button(icon_name="dialog-error-symbolic")
        self._dislike_button.connect("clicked", lambda _b: self._on_dislike())
        widgets.set_accessible_label(
            self._dislike_button,
            "Не нравится",
            "Отметить текущий трек как не понравившийся",
        )
        self._dislike_button.set_tooltip_text("Не нравится")
        bar.append(self._like_button)
        bar.append(self._dislike_button)

        self._previous_button = Gtk.Button(icon_name="media-skip-backward-symbolic")
        self._previous_button.connect("clicked", lambda _b: self._on_previous())
        widgets.set_accessible_label(
            self._previous_button, "Предыдущий трек", "Вернуться к предыдущему треку"
        )
        self._previous_button.set_tooltip_text("Предыдущий трек")
        bar.append(self._previous_button)

        self._play_button = Gtk.Button(icon_name="media-playback-start-symbolic")
        self._play_button.connect("clicked", lambda _b: self._on_toggle_play())
        widgets.set_accessible_label(
            self._play_button, "Играть или пауза", "Переключить воспроизведение"
        )
        self._play_button.set_tooltip_text("Играть / пауза")
        bar.append(self._play_button)

        self._next_button = Gtk.Button(icon_name="media-skip-forward-symbolic")
        self._next_button.connect("clicked", lambda _b: self._on_next())
        widgets.set_accessible_label(
            self._next_button, "Следующий трек", "Перейти к следующему треку"
        )
        self._next_button.set_tooltip_text("Следующий трек")
        bar.append(self._next_button)

        self._position_label = Gtk.Label(label="0:00")
        self._position_label.add_css_class("rw-time")
        self._duration_label = Gtk.Label(label="0:00")
        self._duration_label.add_css_class("rw-time")

        self._seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 1.0)
        self._seek_scale.set_hexpand(True)
        self._seek_scale.set_draw_value(False)
        self._seek_scale.set_digits(0)
        self._seek_scale.connect("value-changed", self._on_seek_changed)
        # Захватываем жест целиком до штатного Gtk.Range, чтобы второй
        # контроллер не отменял отпускание кнопки после начала перемотки.
        seek_drag = Gtk.GestureDrag()
        seek_drag.set_button(1)
        seek_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        seek_drag.connect("drag-begin", self._on_seek_drag_begin)
        seek_drag.connect("drag-update", self._on_seek_drag_update)
        seek_drag.connect("drag-end", self._on_seek_drag_end)
        seek_drag.connect("cancel", self._on_seek_drag_cancel)
        self._seek_scale.add_controller(seek_drag)
        widgets.set_accessible_label(
            self._seek_scale, "Позиция воспроизведения", "Перемотка трека"
        )
        self._seek_scale.set_tooltip_text("Перемотка")

        bar.append(self._position_label)
        bar.append(self._seek_scale)
        bar.append(self._duration_label)

        self._volume_icon = Gtk.Image()
        bar.append(self._volume_icon)
        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.02)
        self._volume_scale.set_size_request(140, -1)
        self._volume_scale.set_draw_value(False)
        state = getattr(self.service, "state", None)
        setting = getattr(state, "setting", None)
        self._volume_scale.set_value(float(setting("volume", 0.8)) if setting else 0.8)
        self._update_volume_icon(self._volume_scale.get_value())
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        widgets.set_accessible_label(
            self._volume_scale, "Громкость", "Уровень громкости воспроизведения"
        )
        self._volume_scale.set_tooltip_text("Громкость")
        bar.append(self._volume_scale)

        return bar

    # --------------------------------------------------------- обновление
    def refresh(self) -> None:
        """Обновить все виджеты из текущего состояния сервиса."""
        self._updating = True
        try:
            self._refresh_header()
            self._refresh_now_playing()
            self._refresh_queue()
            self._refresh_library()
            self._refresh_transport()
        finally:
            self._updating = False

    def _refresh_header(self) -> None:
        """Обновить статус и стартовую кнопку."""
        try:
            status = self.service.status_text()
        except Exception:  # noqa: BLE001 - статус не должен ронять интерфейс
            status = ""
        self._status_label.set_text(status or "")
        try:
            first_run = bool(self.service.first_run)
        except Exception:  # noqa: BLE001
            first_run = False
        self._first_run_banner.set_visible(first_run and not getattr(self.service, "waiting", False))

    def _refresh_now_playing(self) -> None:
        """Исполнитель, название и метаданные текущего трека."""
        current = self.service.current()
        if not current:
            self._artist_label.set_text("")
            self._title_label.set_text("Ничего не играет")
            self._meta_label.set_text("")
            self._reason_button.set_visible(False)
            return
        artist = (current.get("artist") or "").strip()
        self._reason_button.set_visible(True)
        title = (current.get("title") or "").strip()
        self._artist_label.set_text(artist)
        self._title_label.set_text(title or artist or "Без названия")
        self._meta_label.set_text(widgets.track_meta(current))
        self._reason_button.set_sensitive(True)

    def _refresh_queue(self) -> None:
        """Четыре строки очереди с состоянием и причиной выбора."""
        visible_upcoming = getattr(self.service, "visible_upcoming", self.service.upcoming)
        upcoming = list(visible_upcoming() or [])[:4]
        self._queue_tracks = {str(track["id"]): track for track in upcoming}
        for track_id in self._queue_rows.keys() - self._queue_tracks.keys():
            self._queue_list.remove(self._queue_rows.pop(track_id))
        if upcoming and self._queue_empty_row.get_parent() is not None:
            self._queue_list.remove(self._queue_empty_row)
        for position, (track_id, track) in enumerate(self._queue_tracks.items()):
            row = self._queue_rows.get(track_id)
            previous = self._queue_list.get_row_at_index(position)
            if row is None:
                row = Gtk.ListBoxRow(activatable=True, selectable=False, child=widgets.QueueRow())
                row.track_id = track_id
                row.add_css_class("rw-row")
                self._queue_rows[track_id] = row
            if row is not previous:
                if row.get_parent() is not None:
                    self._queue_list.remove(row)
                self._queue_list.insert(row, position)
            row.get_child().update(track)
        if not upcoming and self._queue_empty_row.get_parent() is None:
            self._queue_list.append(self._queue_empty_row)
        if upcoming and self._queue_list.get_mapped():
            publish = getattr(self.service, "pin_visible", None)
            if publish is not None:
                publish(tuple(self._queue_tracks))

    def _on_page_switched(self, _notebook, _page, page_number: int) -> None:
        if page_number == 1:
            self._refresh_library(force=True)

    def _setup_library_row(self, _factory, item: Gtk.ListItem) -> None:
        item.set_child(widgets.TrackRow())

    def _bind_library_row(self, _factory, item: Gtk.ListItem) -> None:
        track_id = item.get_item().get_string()
        row = item.get_child()
        self._library_rows[track_id] = row
        row.update(self._library_tracks[track_id])

    def _unbind_library_row(self, _factory, item: Gtk.ListItem) -> None:
        row = item.get_child()
        track_id = row.track_id
        if self._library_rows.get(track_id) is row:
            self._library_rows.pop(track_id)

    def _refresh_library(self, *, force: bool = False) -> None:
        """Наполнять список только на вкладке «Коллекция» и обновлять видимые строки."""
        if not force and self._notebook.get_current_page() != 1:
            return
        self._library_tracks = {
            str(track["id"]): track for track in self.service.tracks() or [] if track.get("id")
        }
        order = tuple(self._library_tracks)
        if order != self._library_order:
            selected = self._library_selection.get_selected_item()
            selected_id = selected.get_string() if selected is not None else None
            old = self._library_order
            shared = min(len(old), len(order))
            prefix = suffix = 0
            while prefix < shared and old[prefix] == order[prefix]:
                prefix += 1
            while suffix < shared - prefix and old[-1 - suffix] == order[-1 - suffix]:
                suffix += 1
            self._library_order = order
            self._library_model.splice(prefix, len(old) - prefix - suffix, order[prefix:len(order) - suffix])
            if selected_id in self._library_tracks:
                self._library_selection.set_selected(order.index(selected_id))
        for track_id, row in self._library_rows.items():
            if track := self._library_tracks.get(track_id):
                row.update(track)
        self._library_empty.set_visible(not order)

    def _refresh_transport(self) -> None:
        """Позиция, длительность, состояние воспроизведения и громкость."""
        try:
            position = float(self.service.position() or 0.0)
            duration = float(self.service.duration() or 0.0)
            playing = bool(self.service.is_playing())
            can_seek = getattr(self.service, "can_seek", None)
            seekable = duration > 0 and (can_seek is None or can_seek())
        except Exception:  # noqa: BLE001
            position, duration, playing = 0.0, 0.0, False
            seekable = False

        seek = self._seek_scale
        seek.set_sensitive(seekable)
        if self._seek_dragging and (not seekable or self._seek_drag_track_id != self._seek_track_id()):
            self._on_seek_drag_cancel()
        if not self._seek_dragging:
            seek.set_range(0.0, duration if duration > 0 else 1.0)
            seek.set_value(min(position, duration) if duration > 0 else 0.0)
        displayed_position = seek.get_value() if self._seek_dragging else position
        self._position_label.set_text(widgets.format_time(displayed_position))
        self._duration_label.set_text(widgets.format_time(duration))

        volume_getter = getattr(self.service, "volume", None)
        if volume_getter is not None:
            volume = volume_getter()
            if self._volume_scale.get_value() != volume:
                self._volume_scale.set_value(volume)
                self._update_volume_icon(volume)

        waiting = bool(getattr(self.service, "waiting", False))
        current = self.service.current()
        can_go_previous = getattr(self.service, "can_go_previous", None)
        self._previous_button.set_sensitive(bool(can_go_previous and can_go_previous()))
        liked_check = getattr(self.service, "current_liked", None)
        liked = bool(current is not None and liked_check and liked_check())
        self._like_button.set_sensitive(current is not None)
        self._like_button.set_active(liked)
        self._like_icon.set_text("♥" if liked else "♡")
        self._like_button.set_tooltip_text("Убрать лайк" if liked else "Нравится")
        icon = (
            "media-playback-pause-symbolic" if playing or waiting else "media-playback-start-symbolic"
        )
        if self._play_button.get_icon_name() != icon:
            self._play_button.set_icon_name(icon)
        hint = "Остановить ожидание" if waiting else "Играть / пауза"
        if self._play_button.get_tooltip_text() != hint:
            self._play_button.set_tooltip_text(hint)

    # ------------------------------------------------------------ сигналы
    def _on_tick(self) -> bool:
        """Тикер: опросить сервис и обновить интерфейс."""
        started = perf_counter()
        try:
            self.service.tick()
        except Exception:  # noqa: BLE001 - ошибка сервиса не должна ронять UI
            pass
        service_done = perf_counter()
        self.refresh()
        application = self.get_application()
        if isinstance(application, RadioWaveApp):
            application.refresh_media_keys()
        done = perf_counter()
        if done - started >= TICK_INTERVAL_MS / 1000:
            trace("gtk_slow_tick", service_ms=round((service_done - started) * 1000),
                  widgets_ms=round((done - service_done) * 1000))
        return True

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if self._closing:
            return False
        self._closing = True
        started = perf_counter()
        self._stop_timer()
        for dialog in Gtk.Window.list_toplevels():
            if dialog.get_transient_for() is self:
                dialog.close()
        self.set_visible(False)
        application = self.get_application()
        if isinstance(application, RadioWaveApp):
            application._shutdown_audio()
        else:
            _service_shutdown(self.service, "begin_shutdown")
            GLib.idle_add(_start_cleanup, lambda: _service_shutdown(self.service, "quit"))
        trace("gtk_window_hidden", elapsed_ms=round((perf_counter() - started) * 1000))
        return False

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _on_why_clicked(self, _button: Gtk.Button) -> None:
        """Показать причину выбора текущего трека."""
        current = self.service.current()
        if not current:
            widgets.show_error(self, "Сейчас ничего не играет.")
            return
        try:
            reason = self.service.reason_for(current)
        except Exception:  # noqa: BLE001
            reason = ""
        if not reason:
            reason = str(current.get("reason") or "").strip()
        self._reason_label.set_text(reason or "Причина выбора неизвестна.")
        self._reason_popover.popup()

    def _on_taste_changed(self, scale: Gtk.Scale) -> None:
        """Крутилки вкуса сохраняются через сервис."""
        if self._updating:
            return
        for key, widget in self._taste_scales.items():
            if widget is scale:
                try:
                    self.service.set_taste(**{key: float(scale.get_value())})
                except Exception:  # noqa: BLE001
                    pass
                break

    def _on_toggle_play(self) -> None:
        """Play/pause."""
        try:
            self.service.toggle_play()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_next(self) -> None:
        """Следующий трек."""
        try:
            self.service.user_next()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_previous(self) -> None:
        """Предыдущий трек."""
        if not self._previous_button.get_sensitive():
            return
        try:
            self.service.previous_track()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_like(self) -> None:
        """Нравится."""
        try:
            self.service.toggle_like_current()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_dislike(self) -> None:
        """Не нравится."""
        try:
            self.service.dislike_current()
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_seek_changed(self, scale: Gtk.Scale) -> None:
        """Перемотка (только от действия пользователя)."""
        if self._updating or self._seek_dragging or not scale.get_sensitive():
            return
        try:
            self.service.seek(float(scale.get_value()))
        except Exception:  # noqa: BLE001
            pass

    def _seek_track_id(self) -> Optional[str]:
        track = self.service.current()
        return track.get("id") if track else None

    def _on_seek_drag_begin(self, gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
        if not self._seek_scale.get_sensitive():
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._seek_scale.grab_focus()
        self._seek_dragging = True
        self._seek_drag_track_id = self._seek_track_id()
        self._seek_drag_start_x = x
        self._preview_seek_at(x)

    def _preview_seek_at(self, x: float) -> None:
        scale = self._seek_scale
        trough = scale.get_range_rect()
        fraction = max(0.0, min(1.0, (x - trough.x) / max(1, trough.width)))
        if scale.get_inverted() != (scale.get_flippable() and scale.get_direction() == Gtk.TextDirection.RTL):
            fraction = 1.0 - fraction
        scale.set_value(fraction * scale.get_adjustment().get_upper())
        self._position_label.set_text(widgets.format_time(scale.get_value()))

    def _on_seek_drag_update(self, _gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        if self._seek_dragging:
            self._preview_seek_at(self._seek_drag_start_x + offset_x)

    def _on_seek_drag_end(self, gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._seek_dragging and self._seek_drag_track_id == self._seek_track_id():
            self._on_seek_drag_update(gesture, offset_x, offset_y)
            self._on_seek_drag_cancel()
            self._on_seek_changed(self._seek_scale)
        else:
            self._on_seek_drag_cancel()

    def _on_seek_drag_cancel(self, *_args) -> None:
        self._seek_dragging = False
        self._seek_drag_track_id = None

    def _update_volume_icon(self, volume: float) -> None:
        level = "muted" if volume == 0 else "low" if volume < 0.4 else "high"
        self._volume_icon.set_from_icon_name(f"audio-volume-{level}-symbolic")

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        """Громкость."""
        if self._updating:
            return
        volume = float(scale.get_value())
        self._update_volume_icon(volume)
        try:
            self.service.set_volume(volume)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------ поиск и новое
    def _open_catalog(self, _button=None, *, discovery: bool = False) -> None:
        tracks = getattr(self.service, "search_tracks", self.service.tracks)
        liked = {entry.get("id") for entry in self.service.state.likes}
        seeds = [
            track["mbid"] for track in tracks()
            if track.get("id") in liked and track.get("mbid")
        ]
        show_catalog(
            self, tracks, on_play=self._play_found_track,
            seed_mbids=seeds, is_available=self.service.is_ready,
            on_discovery=self._use_discoveries, on_play_recording=self._play_recording,
            on_play_search=self._play_search, discovery=discovery,
        )

    def _open_discovery(self, _button=None) -> None:
        self._open_catalog(discovery=True)

    def _play_found_track(self, track: dict) -> None:
        self.service.play_track(track)
        self.refresh()

    def _play_recording(self, recording) -> None:
        self.service.play_recording(recording)
        self.refresh()

    def _play_search(self, query: str) -> None:
        self.service.play_search(query)
        self.refresh()

    def _use_discoveries(self, recordings) -> None:
        self.service.set_discovery_candidates(recordings)
        self.refresh()

    # ------------------------------------------------------------- импорт
    def _on_import_folder(self) -> None:
        """Импорт аудиофайлов или папки."""
        ImportFolderDialog(
            parent=self,
            on_import=self._do_import_paths,
        ).present()

    def _do_import_paths(self, paths: List[str]) -> None:
        """Передать выбранные пути сервису."""
        if not paths or self._closing:
            return
        try:
            count = self.service.import_paths(paths)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, f"Не удалось импортировать: {exc}")
            return
        self._status_label.set_text(f"Импортировано треков: {count}")
        self.refresh()

    def _on_import_magnet(self) -> None:
        """Импорт magnet-ссылки из поля ввода."""
        uri = self._magnet_entry.get_text().strip()
        if not uri:
            widgets.show_error(self, "Введите magnet-ссылку.")
            return
        self._do_add_magnet(uri)

    def _focus_magnet(self, _button=None) -> None:
        self._notebook.set_current_page(1)
        self._magnet_entry.grab_focus()

    def _do_add_magnet(self, uri: str) -> None:
        """Передать magnet-ссылку сервису."""
        try:
            error = self.service.add_magnet(uri)
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, f"Не удалось добавить ссылку: {exc}")
            return
        if error:
            widgets.show_error(self, str(error))
            return
        self._magnet_entry.set_text("")
        self.refresh()

    def _on_import_torrent(self) -> None:
        """Импорт файла .torrent."""
        dialog = Gtk.FileDialog(title="Выберите файл .torrent")
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Файлы .torrent")
        file_filter.add_pattern("*.torrent")
        file_filter.add_mime_type("application/x-bittorrent")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)

        def on_open(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            if self._closing:
                return
            try:
                gio_file = source.open_finish(result)
            except Exception as exc:  # noqa: BLE001
                widgets.show_error(self, f"Не удалось открыть файл: {exc}")
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if not path:
                return
            try:
                error = self.service.add_torrent_file(path)
            except Exception as exc:  # noqa: BLE001
                widgets.show_error(self, f"Не удалось добавить torrent: {exc}")
                return
            if error:
                widgets.show_error(self, str(error))
                return
            self.refresh()

        dialog.open(self, None, on_open)

    # ----------------------------------------------------- редактирование
    def _on_queue_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        track = self._queue_tracks.get(getattr(row, "track_id", ""))
        if track is not None:
            self._play_found_track(track)

    def _on_library_row_activated(self, _list: Gtk.ListView, position: int) -> None:
        """Включить выбранный трек и сохранить выбор для редактора."""
        item = self._library_model.get_item(position)
        if item is not None and (track := self._library_tracks.get(item.get_string())) is not None:
            self._library_selection.set_selected(position)
            self._play_found_track(track)

    def _on_edit_track(self) -> None:
        """Кнопка «Изменить выбранный»."""
        self._open_editor(self._library_selection.get_selected_item())

    def _open_editor(self, item: Optional[Gtk.StringObject]) -> None:
        """Показать диалог редактирования метаданных трека."""
        if item is None:
            widgets.show_error(self, "Сначала выберите трек в списке.")
            return
        track = self._library_tracks.get(item.get_string())
        if track is None:
            widgets.show_error(self, "Трек не найден в коллекции.")
            return
        EditTrackDialog(
            parent=self, track=track, on_save=self._do_update_track,
        ).present()

    def _do_update_track(
        self,
        track_id: str,
        artist: Optional[str],
        title: Optional[str],
        genre: Optional[str],
        language: Optional[str],
    ) -> None:
        """Сохранить изменённые метаданные."""
        try:
            ok = self.service.update_track(
                track_id, artist=artist, title=title, genre=genre, language=language
            )
        except Exception as exc:  # noqa: BLE001
            widgets.show_error(self, f"Не удалось сохранить: {exc}")
            return
        if not ok:
            widgets.show_error(self, "Не удалось сохранить изменения трека.")
            return
        self.refresh()


def audio_filters() -> Gio.ListStore:
    file_filter = Gtk.FileFilter()
    file_filter.set_name("Аудиофайлы")
    for suffix in sorted(config.AUDIO_EXTENSIONS):
        file_filter.add_suffix(suffix.removeprefix("."))
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(file_filter)
    return filters


class ImportFolderDialog(Gtk.Window):
    """Диалог импорта аудиофайлов или папки."""

    def __init__(
        self,
        parent: Gtk.Window,
        on_import: Callable[[List[str]], None],
    ) -> None:
        super().__init__(title="Импорт музыки", transient_for=parent, modal=True)
        self.set_default_size(520, -1)
        self._on_import = on_import
        self._selected: List[str] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        hint = Gtk.Label(
            label="Выберите аудиофайлы или папку. Радио просканирует и вложенные папки."
        )
        hint.set_xalign(0.0)
        hint.set_wrap(True)
        hint.add_css_class("rw-app-status")
        box.append(hint)

        self._folder_label = Gtk.Label(label="Аудиофайлы или папка ещё не выбраны")
        self._folder_label.set_xalign(0.0)
        self._folder_label.set_ellipsize(3)
        box.append(self._folder_label)

        choose = Gtk.Button(label="Выбрать папку…")
        choose.connect("clicked", lambda _b: self._on_choose_folder())
        widgets.set_accessible_label(choose, "Выбрать папку")
        box.append(choose)
        choose_files = Gtk.Button(label="Выбрать аудиофайлы…")
        choose_files.connect("clicked", self._on_choose_files)
        box.append(choose_files)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda _b: self.close())
        widgets.set_accessible_label(cancel, "Отмена")
        confirm = Gtk.Button(label="Импортировать")
        confirm.connect("clicked", lambda _b: self._on_confirm())
        widgets.set_accessible_label(confirm, "Импортировать")
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        self.set_child(box)

    def _on_choose_folder(self) -> None:
        """Открыть системный выбор папки."""
        dialog = Gtk.FileDialog(title="Выберите папку с музыкой")

        def on_selected(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gio_file = source.select_folder_finish(result)
            except Exception:  # noqa: BLE001 - пользователь мог отменить
                return
            if gio_file is None:
                return
            path = gio_file.get_path()
            if path:
                self._selected = [path]
                self._folder_label.set_text(path)

        dialog.select_folder(self, None, on_selected)

    def _on_confirm(self) -> None:
        """Передать выбранную папку вызывающему коду."""
        if not self._selected:
            widgets.show_error(self, "Сначала выберите аудиофайлы или папку.")
            return
        self.close()
        self._on_import(self._selected)

    def _on_choose_files(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Выберите музыку", filters=audio_filters())

        def opened(source, result):
            try:
                files = source.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [file.get_path() for file in files or [] if file.get_path()]
            if paths:
                self._selected = paths
                self._folder_label.set_text(", ".join(os.path.basename(path) for path in paths))

        dialog.open_multiple(self, None, opened)


class EditTrackDialog(Gtk.Window):
    """Диалог редактирования метаданных трека."""

    def __init__(
        self,
        parent: Gtk.Window,
        track: Dict[str, Any],
        on_save: Callable[..., None],
    ) -> None:
        super().__init__(title="Изменить трек", transient_for=parent, modal=True)
        self.set_default_size(460, -1)
        self._track = track
        self._on_save = on_save

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self._artist_entry = self._add_field(box, "Исполнитель", track.get("artist", ""))
        self._title_entry = self._add_field(box, "Название", track.get("title", ""))
        self._genre_entry = self._add_field(box, "Жанр", track.get("genre", ""))

        language_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        language_caption = Gtk.Label(label="Язык")
        language_caption.set_size_request(170, -1)
        language_caption.set_xalign(0.0)
        language_caption.add_css_class("rw-now-meta")
        current_language = str(track.get("language", "unknown"))
        index = _LANGUAGE_CODES.index(
            current_language if current_language in _LANGUAGE_CODES else "unknown"
        )
        self._language_dropdown = widgets.make_dropdown(_LANGUAGE_NAMES, index)
        widgets.set_accessible_label(self._language_dropdown, "Язык трека")
        language_row.append(language_caption)
        language_row.append(self._language_dropdown)
        box.append(language_row)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda _b: self.close())
        widgets.set_accessible_label(cancel, "Отмена")
        save = Gtk.Button(label="Сохранить")
        save.connect("clicked", lambda _b: self._on_save_clicked())
        widgets.set_accessible_label(save, "Сохранить изменения трека")
        buttons.append(cancel)
        buttons.append(save)
        box.append(buttons)

        self.set_child(box)

    def _add_field(self, box: Gtk.Box, title: str, value: str) -> Gtk.Entry:
        """Добавить строку «подпись + поле ввода»."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        caption = Gtk.Label(label=title)
        caption.set_size_request(170, -1)
        caption.set_xalign(0.0)
        caption.add_css_class("rw-now-meta")
        entry = Gtk.Entry()
        entry.set_text(str(value or ""))
        entry.set_hexpand(True)
        widgets.set_accessible_label(entry, title)
        row.append(caption)
        row.append(entry)
        box.append(row)
        return entry

    def _on_save_clicked(self) -> None:
        """Сохранить изменения и закрыть диалог."""
        language = _LANGUAGE_CODES[self._language_dropdown.get_selected()]
        self.close()
        self._on_save(
            str(self._track.get("id", "")),
            self._artist_entry.get_text().strip() or None,
            self._title_entry.get_text().strip() or None,
            self._genre_entry.get_text().strip() or None,
            language,
        )


def build_window(service: Any, application: Optional[Gtk.Application] = None) -> MainWindow:
    """Собрать главное окно для переданного сервиса.

    Используется в тестах и при ручной сборке окна без запуска приложения.
    """
    window = MainWindow(application=application, service=service)
    theme.apply_theme(window)
    return window


def main(argv=None) -> int:
    """Точка входа."""
    app = RadioWaveApp()
    return app.run(["radio-wave", *(argv or [])])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
