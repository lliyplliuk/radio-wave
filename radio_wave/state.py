"""Постоянное состояние приложения: JSON-файл в XDG_STATE_HOME.

Хранится: настройки, лайки/дизлайки, история прослушиваний, импортированные
каталоги, зарегистрированные загрузки. Каталог записей (библиотека) лежит
отдельно — см. radio_wave.library.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from itertools import chain
from typing import Any, Dict, List

from . import config
from .language import effective_language
from .language_codes import normalize_language_code
from .tags import normalize_id3_genre, normalize_language, uses_literal_genre

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Крутилки вкуса — влияют на ранжирование (см. radio_wave.ranker.TasteProfile).
    "taste": {
        "artist_weight": 1.0,
        "genre_weight": 0.8,
        "catalogue_weight": 0.3,
        "recent_penalty": 1.5,
        "language_weight": 1.0,
    },
    "volume": 0.8,
    "last_directory": "",
}


def _now() -> float:
    return time.time()


class AppState:
    """Простое JSON-хранилище с атомарной записью."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or config.app_state_path()
        self._save_depth = 0
        self._save_pending = False
        self.data: Dict[str, Any] = {
            "settings": json.loads(json.dumps(DEFAULT_SETTINGS)),
            "likes": [],
            "dislikes": [],
            "early_skips": [],
            "history": [],
            "finished_listens": [],
            "directories": [],
            "downloads": [],
        }
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> "AppState":
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if key == "settings" and isinstance(value, dict):
                    merged = dict(DEFAULT_SETTINGS)
                    merged.update(value)
                    merged.pop("russian_only", None)
                    taste = dict(DEFAULT_SETTINGS["taste"])
                    taste.update(value.get("taste", {}) or {})
                    merged["taste"] = taste
                    self.data["settings"] = merged
                else:
                    self.data[key] = value
        return self

    def save(self) -> None:
        if self._save_depth:
            self._save_pending = True
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @contextmanager
    def batch_saves(self):
        self._save_depth += 1
        try:
            yield
        finally:
            self._save_depth -= 1
            if self._save_depth == 0 and self._save_pending:
                self._save_pending = False
                self.save()

    # ------------------------------------------------------------ settings
    @property
    def settings(self) -> Dict[str, Any]:
        return self.data["settings"]

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.save()

    @property
    def taste(self) -> Dict[str, Any]:
        return self.settings.setdefault("taste", dict(DEFAULT_SETTINGS["taste"]))

    # -------------------------------------------------------------- likes
    @property
    def likes(self) -> List[Dict[str, Any]]:
        return self.data["likes"]

    @property
    def dislikes(self) -> List[Dict[str, Any]]:
        return self.data["dislikes"]

    @property
    def early_skips(self) -> List[Dict[str, Any]]:
        return self.data["early_skips"]

    def _like_entry(self, track: Dict[str, Any]) -> Dict[str, Any]:
        entry = {
            "id": track.get("id", ""),
            "artist": track.get("artist", ""),
            "title": track.get("title", ""),
            "genre": track.get("genre", ""),
            "at": _now(),
        }
        self._retain_literal_genre(entry, track)
        self._set_feedback_language(entry, track)
        return entry

    @staticmethod
    def _retain_literal_genre(entry: Dict[str, Any], track: Dict[str, Any]) -> None:
        value = entry.get("genre") or ""
        if isinstance(value, str) and uses_literal_genre(track) and normalize_id3_genre(value) != value:
            entry["genre_literal"] = True
        else:
            entry.pop("genre_literal", None)

    @staticmethod
    def _set_feedback_language(entry: Dict[str, Any], track: Dict[str, Any]) -> None:
        language, confidence = effective_language(track)
        code = ""
        if track.get("lang_source") != "title":
            code = normalize_language_code(track.get("language_code")) or normalize_language_code(track.get("language"))
        code_group = normalize_language(code)
        explicit_group = normalize_language(track.get("language"))
        if code and explicit_group in ("unknown", code_group):
            language, confidence = code_group, 1.0
            entry["language_code"] = code
        else:
            entry.pop("language_code", None)
        entry["language"] = language
        if 0 < confidence < 1:
            entry["lang_source"] = "title"
        else:
            entry.pop("lang_source", None)

    def like(self, track: Dict[str, Any]) -> None:
        entry = self._like_entry(track)
        self.data["likes"] = [x for x in self.likes if x.get("id") != entry["id"]]
        self.data["likes"].append(entry)
        self.data["dislikes"] = [x for x in self.dislikes if x.get("id") != entry["id"]]
        self.data["early_skips"] = [x for x in self.early_skips if x.get("id") != entry["id"]]
        self.save()

    def unlike(self, track_id: str) -> bool:
        likes = [entry for entry in self.likes if entry.get("id") != track_id]
        if len(likes) == len(self.likes):
            return False
        self.data["likes"] = likes
        self.save()
        return True

    def dislike(self, track: Dict[str, Any]) -> None:
        entry = self._like_entry(track)
        self.data["dislikes"] = [x for x in self.dislikes if x.get("id") != entry["id"]]
        self.data["dislikes"].append(entry)
        self.data["likes"] = [x for x in self.likes if x.get("id") != entry["id"]]
        self.data["early_skips"] = [x for x in self.early_skips if x.get("id") != entry["id"]]
        self.save()

    def record_early_skip(self, track: Dict[str, Any]) -> bool:
        entry = self._like_entry(track)
        track_id = entry["id"]
        if not track_id or self.is_disliked(track_id):
            return False
        self.data["early_skips"] = [item for item in self.early_skips if item.get("id") != track_id] + [entry]
        self.save()
        return True

    def is_liked(self, track_id: str) -> bool:
        return any(x.get("id") == track_id for x in self.likes)

    def is_disliked(self, track_id: str) -> bool:
        return any(x.get("id") == track_id for x in self.dislikes)

    # ----------------------------------------------------------- history
    @property
    def history(self) -> List[Dict[str, Any]]:
        return self.data["history"]

    def remember(self, track: Dict[str, Any]) -> None:
        self.history.append(self._like_entry(track))
        self.save()

    @property
    def finished_listens(self) -> List[Dict[str, Any]]:
        return self.data["finished_listens"]

    def remember_finished(self, track: Dict[str, Any]) -> None:
        entry = self._like_entry(track)
        self.data["finished_listens"] = [
            item for item in self.finished_listens if item.get("id") != entry["id"]
        ] + [entry]
        self.save()

    def update_feedback_language(self, track: Dict[str, Any]) -> bool:
        """Уточнить язык существующих реакций, сохранив время реакции."""
        track_id = track.get("id")
        if not track_id:
            return False
        changed = False
        for item in chain(self.likes, self.dislikes, self.history, self.finished_listens, self.early_skips):
            if item.get("id") == track_id:
                fields = ("language", "lang_source", "language_code")
                old_language = tuple(item.get(key) for key in fields)
                self._set_feedback_language(item, track)
                changed |= old_language != tuple(item.get(key) for key in fields)
        if changed:
            self.save()
        return changed

    def restore_feedback_language_codes(self, tracks: Iterable[Dict[str, Any]]) -> bool:
        """Recover exact language from catalogue metadata in old feedback snapshots."""
        known = {
            track["id"]: track for track in tracks
            if track.get("id") and track.get("lang_source") != "title"
            and normalize_language_code(track.get("language_code"))
        }
        if not known:
            return False
        changed = False
        for item in chain(self.likes, self.dislikes, self.history, self.finished_listens, self.early_skips):
            track = known.get(item.get("id"))
            if track is None or normalize_language_code(item.get("language_code")):
                continue
            group = normalize_language(item.get("language"))
            if group in ("unknown", "other", track.get("language")) or item.get("lang_source") == "title":
                self._set_feedback_language(item, track)
                changed |= bool(item.get("language_code"))
        if changed:
            self.save()
        return changed

    def repair_feedback_text(self, repairs: dict[str, tuple[dict, dict]]) -> bool:
        """Update recognizable historical copies without altering their IDs or order."""
        changed = False
        for collection in (self.likes, self.dislikes, self.history, self.finished_listens, self.early_skips):
            for item in collection:
                repair = repairs.get(item.get("id"))
                if repair is None:
                    continue
                track, fields = repair
                for key, (old, corrected) in fields.items():
                    if item.get(key) == old:
                        item[key] = corrected
                        changed = True
                        if key == "genre":
                            self._retain_literal_genre(item, track)
                        if key == "title" and not item.get("language_code"):
                            self._set_feedback_language(item, track)
        if changed:
            self.save()
        return changed

    def recent_ids(self, limit: int = 50) -> List[str]:
        return [item.get("id", "") for item in self.history[-limit:]][::-1]

    # ------------------------------------------------------- directories
    @property
    def directories(self) -> List[str]:
        return self.data["directories"]

    def add_directory(self, path: str) -> None:
        if path and path not in self.directories:
            self.directories.append(path)
            self.save()

    def remove_directory(self, path: str) -> None:
        self.data["directories"] = [p for p in self.directories if p != path]
        self.save()
