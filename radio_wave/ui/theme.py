"""Тёмная тёплая тема GTK4 для «Радио из коллекции».

Палитра: глубокий navy/plum фон, сиреневый (lilac) акцент.
Цвета заданы константами, чтобы их можно было переиспользовать
в прикладном коде (например, для текста на акцентных кнопках).

Стилизация выполняется через :class:`Gtk.CssProvider`, который
регистрируется для дисплея окна — поэтому оформление одинаково
применяется и к главному окну, и к диалогам.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402  (импорт после require_version)

#: Основной фон окна — холодный тёмный navy.
NAVY = "#1b1a2a"

#: Тёплый тёмный plum — поверхности, панели, «карточки».
DARK_PLUM = "#2a1f33"

#: Акцент — сиреневый.
LILAC = "#c4a7e7"

#: Приглушённый сиреневый — второстепенные акценты, рамки.
LILAC_DIM = "#8f7cb0"

#: Основной текст.
TEXT = "#f2eef7"

#: Второстепенный текст (метаданные, подписи).
TEXT_MUTED = "#a89fb8"

#: Поверхность (плашки, строки списка).
SURFACE = "#2f2439"

#: Поверхность при наведении.
SURFACE_HI = "#3a2c46"

#: Ошибка/дизлайк.
DANGER = "#e08a8a"

#: Успех/лайк.
SUCCESS = "#9fd6a0"


#: Общий CSS. Токены подставлены константами выше.
CSS = f"""
@define-color rw_navy {NAVY};
@define-color rw_plum {DARK_PLUM};
@define-color rw_lilac {LILAC};
@define-color rw_lilac_dim {LILAC_DIM};
@define-color rw_text {TEXT};
@define-color rw_text_muted {TEXT_MUTED};
@define-color rw_surface {SURFACE};
@define-color rw_surface_hi {SURFACE_HI};
@define-color rw_danger {DANGER};
@define-color rw_success {SUCCESS};

window {{
    background-color: @rw_navy;
    color: @rw_text;
}}

.dim-label {{
    color: @rw_text_muted;
}}

/* --- заголовок приложения ------------------------------------------ */
.rw-header {{
    background-color: @rw_plum;
    padding: 12px 18px;
}}
.rw-app-title {{
    font-size: 17px;
    font-weight: 700;
}}
.rw-app-status {{
    font-size: 13px;
    color: @rw_text_muted;
}}
/* --- блок «Сейчас играет» ------------------------------------------- */
.rw-now-artist {{
    font-size: 15px;
    color: @rw_lilac;
}}
.rw-now-title {{
    font-size: 32px;
    font-weight: 800;
}}
.rw-now-meta {{
    font-size: 13px;
    color: @rw_text_muted;
}}

/* --- очередь -------------------------------------------------------- */
.rw-row {{
    padding: 7px 10px;
}}
.rw-queue-title {{
    font-size: 14px;
    font-weight: 600;
}}
.rw-queue-meta {{
    font-size: 12px;
    color: @rw_text_muted;
}}
.rw-state {{
    font-size: 11px;
    font-weight: 700;
    border-radius: 8px;
    padding: 2px 8px;
    background-color: @rw_surface_hi;
    color: @rw_text_muted;
}}
.rw-state-ready {{
    background-color: @rw_surface_hi;
    color: @rw_lilac;
}}
.rw-state-pending {{
    background-color: @rw_surface_hi;
    color: @rw_text_muted;
}}
.rw-state-playing {{
    background-color: @rw_lilac;
    color: @rw_navy;
}}

/* --- коллекция ------------------------------------------------------ */
.rw-library-row {{
    padding: 6px 10px;
}}
.rw-library-title {{
    font-size: 13px;
    font-weight: 600;
}}
.rw-library-meta {{
    font-size: 11px;
    color: @rw_text_muted;
}}
.rw-section-title {{
    font-size: 12px;
    font-weight: 700;
    color: @rw_text_muted;
}}

/* --- транспорт ------------------------------------------------------ */
.rw-transport {{
    background-color: @rw_plum;
    padding: 10px 18px;
}}
.rw-time {{
    font-size: 12px;
    color: @rw_text_muted;
    font-family: monospace;
}}

/* --- элементы управления -------------------------------------------- */
button {{
    background-image: none;
    background-color: @rw_surface;
    border-color: @rw_surface_hi;
    color: @rw_text;
    border-radius: 8px;
    padding: 6px 12px;
}}
button:hover {{
    background-color: @rw_surface_hi;
}}
button:checked {{
    background-color: @rw_lilac;
    color: @rw_navy;
}}
button:disabled {{
    background-color: @rw_plum;
    color: @rw_text_muted;
}}
button:focus-visible, entry:focus-visible, notebook tab:focus-visible {{
    outline: 2px solid @rw_lilac;
    outline-offset: -2px;
}}
entry {{
    background-image: none;
    background-color: @rw_surface;
    color: @rw_text;
    border-radius: 8px;
    padding: 6px 10px;
}}
entry:focus {{
    background-color: @rw_surface_hi;
}}
scale trough {{
    background-color: @rw_surface;
    border-radius: 5px;
    min-height: 5px;
}}
scale slider {{
    background-color: @rw_lilac;
    border-radius: 9px;
    min-width: 18px;
    min-height: 18px;
}}
scale highlight {{
    background-color: @rw_lilac_dim;
    border-radius: 5px;
}}
listbox {{
    background-color: @rw_plum;
}}
listbox row {{
    background-color: @rw_plum;
}}
listbox row:hover {{
    background-color: @rw_surface;
}}
listbox row.selected {{
    background-color: @rw_surface_hi;
}}
notebook, notebook > stack {{
    background-color: @rw_navy;
    color: @rw_text;
    border-color: @rw_surface_hi;
}}
notebook > header {{
    background-color: @rw_plum;
    border-color: @rw_surface_hi;
}}
notebook tab {{
    color: @rw_text_muted;
}}
notebook tab:checked {{
    color: @rw_lilac;
    box-shadow: inset 0 -2px @rw_lilac;
}}
"""


def apply_theme(window: Gtk.Window | None = None) -> Gtk.CssProvider:
    """Применить тёмную тему к окну (и ко всем его диалогам).

    :param window: окно, для которого регистрируется тема. Если ``None``,
        используется текущий дисплей по умолчанию.
    :return: установленный :class:`Gtk.CssProvider`.
    """
    settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS.encode("utf-8"))

    display = None
    if window is not None:
        window.add_css_class("radio-wave")
        display = window.get_display()
    if display is None:
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    return provider
