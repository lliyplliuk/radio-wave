"""Окно хранения проигранных файлов в торрент-кэше."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from typing import Any

from .config import AUDIO_EXTENSIONS, CACHE_RECENT_PLAYED

Track = Mapping[str, Any]


@dataclass(frozen=True)
class CacheFile:
    info_hash: str
    file_index: int


def _torrent_file(track: Track) -> CacheFile | None:
    if track.get("source") != "torrent":
        return None
    info_hash = track.get("info_hash")
    file_index = track.get("file_index")
    if isinstance(info_hash, str) and type(file_index) is int and file_index >= 0:
        return CacheFile(info_hash, file_index)
    return None


def _path(track: Track) -> str:
    path = track.get("path")
    return os.path.abspath(path) if isinstance(path, str) and path else ""


def cache_files_to_prune(
    tracks: Iterable[Track],
    history: Iterable[Track],
    *,
    current_id: str | None,
    upcoming_ids: Iterable[str] = (),
    pinned_ids: Iterable[str | None] = (),
) -> tuple[CacheFile, ...]:
    """Выбрать сыгранные торрент-файлы, вышедшие из всех окон защиты."""
    played = list(dict.fromkeys(
        item["id"] for item in reversed(list(history)) if item.get("id") and item["id"] != current_id
    ))
    kept = {current_id, *upcoming_ids, *pinned_ids, *played[:CACHE_RECENT_PLAYED]}
    eviction_ids = set(played[CACHE_RECENT_PLAYED:]) - kept
    if not eviction_ids:
        return ()

    candidates, protected_files, protected_paths, local_paths = {}, set(), set(), []
    for track in tracks:
        file = _torrent_file(track)
        path = _path(track)
        if file is not None and track.get("id") in eviction_ids and os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS:
            candidates[file] = path
        elif track.get("source") != "torrent":
            if path:
                local_paths.append(path)
        elif track.get("id") not in eviction_ids:
            if file is not None:
                protected_files.add(file)
            if path:
                protected_paths.add(path)
    if candidates:
        protected_paths.update(os.path.realpath(path) for path in local_paths)
    return tuple(
        file for file, path in candidates.items()
        if file not in protected_files and path not in protected_paths
    )
