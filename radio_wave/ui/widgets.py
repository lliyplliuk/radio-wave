"""Переиспользуемые элементы интерфейса GTK4.

Содержит мелкие виджеты и вспомогательные функции, которые используются
окном приложения: форматирование времени, подписи состояния трека,
строка очереди и обёртки для доступности.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from typing import Any, Dict, Optional

from gi.repository import GObject, Gtk  # noqa: E402

from radio_wave.language import effective_language  # noqa: E402

#: Человекочитаемые названия состояний трека из очереди.
STATE_LABELS: Dict[str, str] = {
    "ready": "готов",
    "pending": "ожидание",
    "playing": "играет",
}

#: Человекочитаемые названия языков.
LANGUAGE_LABELS: Dict[str, str] = {
    "ru": "русский",
    "en": "английский",
    "other": "другой",
    "unknown": "неизвестно",
}

#: Коды языков в том же порядке, что и в :data:`LANGUAGE_LABELS`.
LANGUAGE_CODES = ("ru", "en", "other", "unknown")


def set_accessible_label(widget: Gtk.Widget, label: str, description: str = "") -> None:
    """Задать доступное имя (и описание) для интерактивного элемента.

    :param widget: виджет, которому назначается подпись.
    :param label: осмысленное имя элемента для программ экранного доступа.
    :param description: необязательное подробное описание.
    """
    values = [GObject.Value(str, label)]
    props = [Gtk.AccessibleProperty.LABEL]
    if description:
        values.append(GObject.Value(str, description))
        props.append(Gtk.AccessibleProperty.DESCRIPTION)
    widget.update_property(props, values)


def format_time(seconds: float) -> str:
    """Человекочитаемое время ``M:SS`` (или ``H:MM:SS`` для длинных треков)."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def language_label(code: str) -> str:
    """Подпись языка для показа пользователю."""
    return LANGUAGE_LABELS.get((code or "unknown").strip().lower(), "неизвестно")


def state_label(state: str) -> str:
    """Подпись состояния трека из очереди."""
    return STATE_LABELS.get((state or "").strip().lower(), state or "")


def track_meta(track: Dict[str, Any]) -> str:
    """Строка метаданных трека: ``жанр · язык``."""
    genre = (track.get("genre") or "").strip()
    code, confidence = effective_language(track)
    language = language_label(code)
    if 0 < confidence < 1:
        language = f"{language} (по названию)"
    if genre:
        return f"{genre} · {language}"
    return language


def track_title(track: Dict[str, Any]) -> str:
    """Заголовок трека: исполнитель и название (для анонимных — только имя файла)."""
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "").strip()
    if artist and title:
        return f"{artist} — {title}"
    return title or artist or "Без названия"


class QueueRow(Gtk.Box):
    """Компактная строка очереди «Дальше».

    Показывает название трека, состояние готовности и причину выбора.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("rw-row")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        self._title = Gtk.Label(label="")
        self._title.set_xalign(0.0)
        self._title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._title.add_css_class("rw-queue-title")
        self._reason = Gtk.Label(label="")
        self._reason.set_xalign(0.0)
        self._reason.set_ellipsize(3)
        self._reason.add_css_class("rw-queue-meta")
        text_box.append(self._title)
        text_box.append(self._reason)

        self._state = Gtk.Label(label="")
        self._state.add_css_class("rw-state")
        self._state.set_valign(Gtk.Align.CENTER)

        self.append(text_box)
        self.append(self._state)

    def update(self, track: Dict[str, Any]) -> None:
        """Обновить содержимое строки по данным трека."""
        title = track_title(track)
        self._title.set_text(title)
        reason = (track.get("reason") or "").strip()
        self._reason.set_text(reason)
        state = (track.get("state") or "").strip().lower()
        label = state_label(state)
        self._state.set_text(label)
        self._state.set_visible(bool(label))
        self._state.set_css_classes(["rw-state"])
        self._state.add_css_class(f"rw-state-{state}" if state else "rw-state")
        set_accessible_label(
            self,
            f"{title}. Состояние: {label or 'неизвестно'}"
            + (f". Причина: {reason}" if reason else ""),
        )
        self.set_tooltip_text(reason or title)


class TrackRow(Gtk.Box):
    """Строка списка коллекции: название трека и его метаданные."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.track_id = ""
        self._rendered = None
        self.add_css_class("rw-library-row")

        self._title = Gtk.Label(xalign=0.0, ellipsize=3)
        self._title.add_css_class("rw-library-title")
        self._meta = Gtk.Label(xalign=0.0, ellipsize=3)
        self._meta.add_css_class("rw-library-meta")
        self.append(self._title)
        self.append(self._meta)

    def update(self, track: Dict[str, Any]) -> None:
        self.track_id = str(track["id"])
        ready = bool(track.get("ready", True))
        rendered = (track_title(track), track_meta(track), ready)
        if self._rendered == rendered:
            return
        self._rendered = rendered
        title, meta, _ready = rendered
        self._title.set_text(title)
        self._meta.set_text(meta)
        set_accessible_label(
            self,
            f"{title}. {meta}." + ("" if ready else " Файл ещё не готов."),
        )
        self.set_tooltip_text(meta)


def make_scale(
    title: str,
    value: float,
    lower: float,
    upper: float,
    step: float,
    on_change,
) -> Gtk.Scale:
    """Создать шкалу вкуса с подписью и доступным описанием.

    :param title: подпись шкалы.
    :param value: текущее значение.
    :param lower: минимальное значение.
    :param upper: максимальное значение.
    :param step: шаг изменения.
    :param on_change: обработчик ``value-changed``.
    """
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lower, upper, step)
    scale.set_value(float(value))
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.RIGHT)
    scale.set_hexpand(True)
    scale.set_digits(2)
    scale.connect("value-changed", on_change)
    set_accessible_label(scale, title)
    scale.set_tooltip_text(title)
    return scale


def make_dropdown(labels: list[str], selected: int = 0) -> Gtk.DropDown:
    """Создать выпадающий список с заданными подписями."""
    dropdown = Gtk.DropDown.new_from_strings(labels)
    dropdown.set_selected(selected)
    return dropdown


def label_with_css(text: str, css_class: str, xalign: float = 0.0) -> Gtk.Label:
    """Создать метку с готовым CSS-классом."""
    label = Gtk.Label(label=text)
    label.set_xalign(xalign)
    label.add_css_class(css_class)
    return label


def show_error(parent: Optional[Gtk.Window], message: str) -> None:
    """Показать сообщение об ошибке в модальном диалоге."""
    dialog = Gtk.AlertDialog(message=message)
    dialog.choose(parent, None, None)
