"""Каталог записей (библиотека): импорт папок, метаданные, язык.

Каталог хранится отдельно от состояния приложения — отдельный JSON-файл
в XDG_DATA_HOME/radio-wave/catalogue/catalogue.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional

from . import config, tags
from .language_codes import normalize_language_code
from .tags import normalize_language as _normalize_language


def _track_id(path: str, size: int, mtime: float) -> str:
    raw = f"{os.path.abspath(path)}|{size}|{int(mtime)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def is_audio(path: str) -> bool:
    """Только аудиофайлы — расширение известного аудиоформата."""
    return os.path.splitext(path)[1].lower() in config.AUDIO_EXTENSIONS


class Catalogue:
    """Список записей с метаданными."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or config.catalogue_path()
        self.tracks: List[Dict[str, Any]] = []
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> "Catalogue":
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.tracks = []
            return self
        if isinstance(data, dict):
            data = data.get("tracks", [])
        self.tracks = [t for t in data if isinstance(t, dict)]
        recovered = False
        for track in self.tracks:
            path = track.get("path") or ""
            if (
                os.path.splitext(path)[1].lower() == ".mp3"
                and isinstance(track.get("genre"), str) and not tags.uses_literal_genre(track)
            ):
                track["genre"] = tags.normalize_id3_genre(track["genre"])
                if tags.normalize_id3_genre(track["genre"]) != track["genre"]:
                    track["genre_literal"] = True
            raw_language = track.get("language")
            language = _normalize_language(raw_language)
            if track.get("lang_source") == "title":
                track["language"] = language
                track.pop("language_code", None)
                continue
            explicit = normalize_language_code(track.get("language_code"))
            code = normalize_language_code(raw_language) or explicit
            if code and language == "unknown":
                language = _normalize_language(code)
            if not code and language == "other" and track.get("lang_source") in ("tag", None, ""):
                audio_path = track.get("path") or ""
                if os.path.isfile(audio_path):
                    code = tags.read_tags(audio_path).get("language_code", "")
                    if code:
                        language = _normalize_language(code)
                        track["lang_source"] = "tag"
                        recovered = True
            track["language"] = language
            if code and _normalize_language(code) == language:
                track["language_code"] = code
            else:
                track.pop("language_code", None)
        if recovered:
            try:
                self.save()
            except OSError:
                pass
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"tracks": self.tracks}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------- access
    def all(self) -> List[Dict[str, Any]]:
        return list(self.tracks)

    def get(self, track_id: str) -> Optional[Dict[str, Any]]:
        for track in self.tracks:
            if track.get("id") == track_id:
                return track
        return None

    def has_path(self, path: str) -> bool:
        target = os.path.abspath(path)
        return any(os.path.abspath(t.get("path", "")) == target for t in self.tracks)

    def __len__(self) -> int:
        return len(self.tracks)

    # ------------------------------------------------------------ import
    def scan_path(self, path: str, mark_russian: bool = False) -> List[Dict[str, Any]]:
        """Просканировать файл или папку. Возвращает список добавленных записей."""
        added: List[Dict[str, Any]] = []
        for audio in self._iter_audio(path):
            if self.has_path(audio):
                continue
            track = self._make_track(audio, mark_russian=mark_russian)
            self.tracks.append(track)
            added.append(track)
        if added:
            self.save()
        return added

    def scan_paths(self, paths: Iterable[str], mark_russian: bool = False) -> List[Dict[str, Any]]:
        added: List[Dict[str, Any]] = []
        for path in paths:
            added.extend(self.scan_path(path, mark_russian=mark_russian))
        return added

    def _iter_audio(self, path: str):
        if os.path.isfile(path):
            if is_audio(path):
                yield path
            return
        if not os.path.isdir(path):
            return
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                candidate = os.path.join(root, name)
                if is_audio(candidate) and not name.startswith("."):
                    yield candidate

    def _make_track(self, path: str, mark_russian: bool = False) -> Dict[str, Any]:
        stat = os.stat(path)
        meta = tags.read_tags(path)
        artist = meta.get("artist", "") or ""
        title = meta.get("title", "") or os.path.splitext(os.path.basename(path))[0]
        genre = meta.get("genre", "") or ""
        language = _normalize_language(meta.get("language", ""))
        code = normalize_language_code(meta.get("language_code") or meta.get("language"))
        lang_source = "tag" if language != "unknown" else ""
        if mark_russian:
            language, code, lang_source = "ru", "ru", "bulk"
        track = {
            "id": _track_id(path, stat.st_size, stat.st_mtime),
            "path": os.path.abspath(path),
            "artist": artist,
            "title": title,
            "genre": genre,
            "language": language,
            "lang_source": lang_source,
            "mbid": meta.get("mbid", "") or "",
            "duration": 0.0,
            "source": "local",
            "size": stat.st_size,
            "added_at": time.time(),
        }
        if code:
            track["language_code"] = code
        if meta.get("genre_literal"):
            track["genre_literal"] = True
        return track

    def add_external(self, track: Dict[str, Any]) -> Dict[str, Any]:
        """Добавить запись, полученную извне (например, загрузка из торрента)."""
        self.tracks.append(track)
        self.save()
        return track

    # ------------------------------------------------------------ editing
    def update(self, track_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        track = self.get(track_id)
        if track is None:
            return None
        for key, value in fields.items():
            if value is not None and key in ("artist", "title", "genre", "mbid"):
                track[key] = value
                if key == "genre":
                    track["genre_literal"] = True
        group, exact = fields.get("language"), fields.get("language_code")
        if group is not None or exact is not None:
            code = normalize_language_code(exact if exact is not None else group)
            previous = track.get("language", "unknown")
            language = _normalize_language(group) if group is not None else _normalize_language(code or previous)
            if exact is None and not code and language == previous == "other":
                code = normalize_language_code(track.get("language_code"))
            track.update(language=language, lang_source="manual")
            if code and _normalize_language(code) == language:
                track["language_code"] = code
            else:
                track.pop("language_code", None)
        self.save()
        return track

    def known_languages(self) -> List[str]:
        return sorted({t.get("language", "unknown") for t in self.tracks})
