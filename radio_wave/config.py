"""Пути, расширения и константы приложения."""

from __future__ import annotations

import os

APP_NAME = "Радио из коллекции"
ORG_NAME = "radio-wave"

#: Сколько следующих треков готовим к воспроизведению.
QUEUE_LOOKAHEAD = 8

FOREGROUND_IDLE_SECONDS = 5
FOREGROUND_FILE_SECONDS = 12
FOREGROUND_SEARCH_SECONDS = 15

#: Сколько предыдущих сыгранных торрент-файлов сохраняем.
CACHE_RECENT_PLAYED = 4

#: Приоритеты libtorrent (0..7). 0 — не скачивать, 4 — обычный, 7 — максимальный.
PRIORITY_IGNORE = 0
PRIORITY_DEFAULT = 4
PRIORITY_TOP = 7

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
    ".mp4", ".wav", ".aac", ".wma", ".aiff", ".aif", ".ape", ".wv",
}

LANGUAGES = ("ru", "en", "other", "unknown")

LISTENBRAINZ_API = "https://api.listenbrainz.org/1"


def _xdg(var: str, fallback: str) -> str:
    value = os.environ.get(var)
    if value:
        return os.path.join(value, ORG_NAME)
    return os.path.join(os.path.expanduser("~"), fallback, ORG_NAME)


def state_dir() -> str:
    """Каталог постоянного состояния (настройки, лайки, история)."""
    return _xdg("XDG_STATE_HOME", ".local/state")


def cache_dir() -> str:
    """Каталог временных данных (загрузки, очереди)."""
    return _xdg("XDG_CACHE_HOME", ".cache")


def data_dir() -> str:
    """Каталог данных (каталог треков хранится отдельно от состояния)."""
    return _xdg("XDG_DATA_HOME", ".local/share")


def catalogue_dir() -> str:
    """Каталог, где хранится каталог (библиотека) записей."""
    return os.path.join(data_dir(), "catalogue")


def downloads_dir() -> str:
    """Каталог загрузок BitTorrent."""
    return os.path.join(cache_dir(), "downloads")


def app_state_path() -> str:
    return os.path.join(state_dir(), "state.json")


def catalogue_path() -> str:
    return os.path.join(catalogue_dir(), "catalogue.json")


def ensure_dirs() -> None:
    for path in (state_dir(), cache_dir(), data_dir(), catalogue_dir(), downloads_dir()):
        os.makedirs(path, exist_ok=True)
