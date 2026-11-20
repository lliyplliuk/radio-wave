"""Загрузка выбранных аудиофайлов из BitTorrent через libtorrent 2.x."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from .cache_window import CacheFile
from .config import AUDIO_EXTENSIONS


def _lt():
    import libtorrent as lt
    return lt


def _value(obj, name: str, default: Any = "") -> Any:
    attr = getattr(obj, name, None)
    if attr is None:
        return default
    return attr() if callable(attr) else attr


def file_fields(entry, index: int = 0) -> dict:
    return {
        "index": index,
        "path": str(_value(entry, "path")),
        "size": int(_value(entry, "size", 0) or 0),
    }


def torrent_files(info) -> list[dict]:
    storage = info.files()
    ignored_flags = storage.flag_symlink | storage.flag_pad_file
    return [
        {
            "index": index,
            "path": storage.file_path(index),
            "size": storage.file_size(index),
            "ignored": bool(storage.file_flags(index) & ignored_flags),
        }
        for index in range(storage.num_files())
    ]


class TorrentEntry:
    def __init__(self, info_hash: str, name: str, handle=None, save_path: str = "") -> None:
        self.info_hash = info_hash
        self.name = name
        self.handle = handle
        self.save_path = save_path
        self.files: list[dict] = []
        self.complete: set[int] = set()
        self.priorities: list[int] | None = None
        self.total_size = 0
        self.downloaded = 0
        self.peers = 0
        self.progress = 0.0
        self.state = "metadata"
        self.error = ""

    def file_path(self, index: int) -> str | None:
        if not self.save_path or not 0 <= index < len(self.files):
            return None
        item = self.files[index]
        relative = item["path"]
        if not relative or item.get("ignored") or os.path.isabs(relative):
            return None
        if any(part in {"", ".", ".."} for part in relative.split(os.sep)):
            return None
        path = os.path.join(self.save_path, relative)
        if os.path.commonpath((os.path.realpath(path), self.save_path)) != self.save_path:
            return None
        return path

    def is_complete(self, index: int) -> bool:
        path = self.file_path(index)
        if index not in self.complete or path is None:
            return False
        try:
            return os.path.isfile(path) and os.path.getsize(path) == self.files[index]["size"]
        except OSError:
            return False

    def as_dict(self) -> dict:
        return {
            "info_hash": self.info_hash,
            "name": self.name,
            "files": list(self.files),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "progress": self.progress,
            "state": self.state,
            "error": self.error,
        }


@dataclass
class _CacheFlush:
    was_paused: bool
    was_auto_managed: bool
    finished: bool = False


def _fd_mount_id(descriptor: int) -> str:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as info:
        return next((line.partition(":")[2].strip() for line in info if line.startswith("mnt_id:")), "")


def _remove_owned_audio(root: str, info_hash: str, relative: str, expected_size: int) -> bool:
    """Удалить обычный файл через дескриптор каталога, не проходя по ссылкам."""
    components = relative.split(os.sep)
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash) or any(part in {"", ".", ".."} for part in components):
        return False
    mode = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(os.sep, mode)
    try:
        root_parts = [part for part in root.split(os.sep) if part]
        parents = [*root_parts, info_hash, *components[:-1]]
        mount_id = _fd_mount_id(directory) if not root_parts else ""
        for depth, part in enumerate(parents, 1):
            root_parent_mount = _fd_mount_id(directory) if depth == len(root_parts) else ""
            parent = os.open(part, mode, dir_fd=directory)
            os.close(directory)
            directory = parent
            if depth == len(root_parts):
                mount_id = _fd_mount_id(directory)
                if not root_parent_mount or mount_id != root_parent_mount:
                    return False
            elif depth > len(root_parts) and (not mount_id or _fd_mount_id(directory) != mount_id):
                return False
        filename = components[-1]
        entry = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_size != expected_size:
            return False
        os.unlink(filename, dir_fd=directory)
        return True
    except OSError:
        return False
    finally:
        os.close(directory)


class TorrentManager:
    def __init__(self, save_dir: str | None = None, session=None) -> None:
        from . import config

        self.save_dir = save_dir or config.downloads_dir()
        self._session = session
        self._torrents: dict[str, TorrentEntry] = {}
        self._cache_flushes: dict[str, _CacheFlush] = {}
        self._playback_flushes: dict[str, set[int]] = {}
        self._cache_rechecks: set[str] = set()
        self.on_file_complete: Callable[[str, TorrentEntry, int], None] | None = None
        self.on_metadata: Callable[[str, TorrentEntry], None] | None = None

    def start(self) -> None:
        if self._session is not None:
            return
        lt = _lt()
        os.makedirs(self.save_dir, exist_ok=True)
        category = lt.alert.category_t
        self._session = lt.session({
            "listen_interfaces": "0.0.0.0:6881-6889",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "alert_mask": category.error_notification | category.status_notification
            | category.file_progress_notification | category.storage_notification,
        })

    @property
    def session(self):
        self.start()
        return self._session

    def _save_path(self, info_hash: str, save_path: str | None) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash) or not int(info_hash, 16):
            raise ValueError("Некорректный идентификатор раздачи")
        base = os.path.realpath(save_path or self.save_dir)
        path = os.path.join(base, info_hash)
        if os.path.realpath(path) != path:
            raise ValueError("Некорректный каталог раздачи")
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _read_metadata(entry: TorrentEntry, info) -> None:
        entry.name = info.name()
        entry.files = [{**item, "priority": 0} for item in torrent_files(info)]
        entry.total_size = info.total_size()
        entry.state = "downloading"

    def add_magnet(self, uri: str, save_path: str | None = None, metadata_path: str | None = None) -> TorrentEntry:
        lt = _lt()
        from .magnet import parse_magnet

        metadata = parse_magnet(uri)
        info_hash = metadata["info_hash"]
        previous = self.get(info_hash)
        if previous is not None:
            return previous
        params = lt.parse_magnet_uri(uri)
        params.save_path = self._save_path(info_hash, save_path)
        # При получении метаданных libtorrent сначала назначает файлам нулевой
        # приоритет. Сервис включит только выбранные дорожки после разбора списка.
        params.flags |= lt.torrent_flags.default_dont_download
        if metadata_path and os.path.isfile(metadata_path):
            try:
                info = lt.torrent_info(metadata_path)
                if str(info.info_hash()) == info_hash:
                    params.ti = info
                    params.file_priorities = [0] * info.num_files()
            except RuntimeError:
                pass  # A missing or corrupt cache falls back to network metadata.
        handle = self.session.add_torrent(params)
        entry = TorrentEntry(info_hash, metadata["name"] or info_hash, handle, params.save_path)
        if params.ti is not None:
            self._read_metadata(entry, params.ti)
        self._torrents[info_hash] = entry
        return entry

    def export_metadata(self, entry: TorrentEntry) -> bytes:
        lt = _lt()
        info = entry.handle.torrent_file()
        return lt.bencode(lt.create_torrent(info).generate())

    def add_torrent_file(self, path: str, save_path: str | None = None) -> TorrentEntry:
        return self._add_torrent_info(_lt().torrent_info(path), save_path)

    def add_torrent_data(self, data: bytes) -> TorrentEntry:
        lt = _lt()
        return self._add_torrent_info(lt.torrent_info(lt.bdecode(data)), None)

    def _add_torrent_info(self, info, save_path: str | None) -> TorrentEntry:
        lt = _lt()
        info_hash = str(info.info_hash())
        previous = self.get(info_hash)
        if previous is not None:
            return previous
        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = self._save_path(info_hash, save_path)
        params.flags |= lt.torrent_flags.default_dont_download
        params.file_priorities = [0] * info.num_files()
        handle = self.session.add_torrent(params)
        entry = TorrentEntry(info_hash, info.name(), handle, params.save_path)
        self._read_metadata(entry, info)
        self._torrents[info_hash] = entry
        return entry

    def discard(self, entry: TorrentEntry) -> None:
        """Remove a provisional source before it is added to the library."""
        if self.get(entry.info_hash) is entry:
            self.session.remove_torrent(entry.handle)
            self._torrents.pop(entry.info_hash, None)
            self._cache_flushes.pop(entry.info_hash, None)
            self._playback_flushes.pop(entry.info_hash, None)
            self._cache_rechecks.discard(entry.info_hash)

    def torrents(self) -> list[TorrentEntry]:
        return list(self._torrents.values())

    def get(self, info_hash: str) -> TorrentEntry | None:
        return self._torrents.get(info_hash)

    def set_file_priorities(self, info_hash: str, priorities: list[int]) -> bool:
        entry = self.get(info_hash)
        if entry is None or entry.handle is None:
            return False
        if priorities == entry.priorities:
            return True
        try:
            entry.handle.prioritize_files(priorities)
        except RuntimeError:
            return False
        entry.priorities = list(priorities)
        for file, level in zip(entry.files, priorities):
            file["priority"] = level
        return True

    def schedule(self, plan: list[dict]) -> None:
        """Применить один вектор приоритетов на раздачу."""
        from . import config

        vectors = {entry.info_hash: [0] * len(entry.files) for entry in self.torrents() if entry.files}
        for item in plan:
            priorities = vectors.get(item["info_hash"])
            index = item["file_index"]
            if priorities is not None and 0 <= index < len(priorities):
                priorities[index] = max(priorities[index], item.get("level", config.PRIORITY_DEFAULT))
        for info_hash, priorities in vectors.items():
            self.set_file_priorities(info_hash, priorities)

    def _prunable(self, entry: TorrentEntry, file_index: int, root: str) -> bool:
        if (
            entry.state == "error"
            or not re.fullmatch(r"[0-9a-f]{40}", entry.info_hash)
            or entry.save_path != os.path.join(root, entry.info_hash)
            or not 0 <= file_index < len(entry.files)
            or entry.priorities is None
            or file_index >= len(entry.priorities)
            or entry.priorities[file_index]
        ):
            return False
        path = entry.file_path(file_index)
        if path is None or os.path.realpath(path) != path or os.path.splitext(path)[1].lower() not in AUDIO_EXTENSIONS:
            return False
        # The caller supplies previously played files. A missing shared piece
        # can invalidate libtorrent's completion flag without truncating one.
        try:
            status = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        return (
            stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and status.st_size == entry.files[file_index]["size"] > 0
        )

    def _flush_for_prune(self, entry: TorrentEntry) -> None:
        lt = _lt()
        handle = entry.handle
        flags = handle.flags()
        self._cache_flushes[entry.info_hash] = _CacheFlush(
            bool(flags & lt.torrent_flags.paused), bool(flags & lt.torrent_flags.auto_managed),
        )
        handle.unset_flags(lt.torrent_flags.auto_managed)
        handle.pause()
        handle.flush_cache()

    def _enable_cache_alerts(self, *, include_recheck: bool = False) -> None:
        category = _lt().alert.category_t
        needed = category.storage_notification
        if include_recheck:
            needed |= category.status_notification
        current = self._session.get_settings()["alert_mask"]
        if current & needed != needed:
            self._session.apply_settings({"alert_mask": current | needed})

    def prune(self, candidates: Iterable[CacheFile]) -> tuple[CacheFile, ...]:
        """Начать или продолжить очистку после свежей проверки окна хранения.

        Вызывать после poll(), пока libtorrent не подтвердит сброс дискового
        кэша. При каждом вызове использовать актуальный список кандидатов.
        """
        if self._session is None:
            return ()
        root = os.path.abspath(self.save_dir)
        selected: dict[str, set[int]] = {}
        for file in candidates:
            if file.info_hash in self._cache_rechecks or file.info_hash in self._playback_flushes:
                continue
            entry = self.get(file.info_hash)
            if entry is not None and self._prunable(entry, file.file_index, root):
                selected.setdefault(file.info_hash, set()).add(file.file_index)

        lt = _lt()
        deleted = []
        for info_hash, flush in list(self._cache_flushes.items()):
            indexes = selected.pop(info_hash, ())
            if not flush.finished:
                continue
            del self._cache_flushes[info_hash]
            entry = self.get(info_hash)
            if entry is None:
                continue
            removed = False
            for index in indexes:
                file = entry.files[index]
                if _remove_owned_audio(root, info_hash, file["path"], file["size"]):
                    entry.complete.discard(index)
                    file["downloaded"] = 0
                    deleted.append(CacheFile(info_hash, index))
                    removed = True
            if removed:
                entry.handle.force_recheck()
                self._cache_rechecks.add(info_hash)
            if not flush.was_paused:
                entry.handle.resume()
            if flush.was_auto_managed:
                entry.handle.set_flags(lt.torrent_flags.auto_managed)

        if selected:
            self._enable_cache_alerts(include_recheck=True)
            checking = {
                lt.torrent_status.checking_files, lt.torrent_status.checking_resume_data,
                lt.torrent_status.queued_for_checking,
            }
            for info_hash in selected:
                entry = self._torrents[info_hash]
                if entry.handle.status().state not in checking:
                    self._flush_for_prune(entry)
        return tuple(deleted)

    def verified_prefix_pieces(self, info_hash: str, file_index: int = 0) -> int:
        """Сколько проверенных кусков доступно подряд с начала файла."""
        entry = self.get(info_hash)
        if entry is None or not 0 <= file_index < len(entry.files) or not entry.files[file_index]["size"]:
            return 0
        info = entry.handle.torrent_file()
        if info is None:
            return 0
        storage = info.files()
        size = info.piece_length()
        offset = storage.file_offset(file_index)
        last = (offset + storage.file_size(file_index) - 1) // size
        length = 0
        for piece in range(offset // size, last + 1):
            if not entry.handle.have_piece(piece):
                break
            length += 1
        return length

    def poll(self) -> list[tuple[str, TorrentEntry, int]]:
        """Выдать проверенные файлы после завершения записи на диск."""
        if self._session is None:
            return []
        flushed_for_playback: dict[str, set[int]] = {}
        for alert in self._session.pop_alerts():
            kind = type(alert).__name__
            if kind == "cache_flushed_alert":
                info_hash = str(alert.handle.info_hash())
                entry = self.get(info_hash)
                if entry is None or entry.handle != alert.handle:
                    continue
                flush = self._cache_flushes.get(info_hash)
                if flush is not None:
                    flush.finished = True
                elif info_hash in self._playback_flushes:
                    flushed_for_playback[info_hash] = self._playback_flushes.pop(info_hash)
            elif kind == "torrent_checked_alert":
                entry = self.get(str(alert.handle.info_hash()))
                if entry is not None and entry.handle == alert.handle:
                    self._cache_rechecks.discard(entry.info_hash)
            elif kind == "torrent_error_alert":
                entry = self.get(str(alert.handle.info_hash()))
                if entry is not None:
                    entry.state = "error"
                    entry.error = str(alert.error)
        completed = []
        for entry in self.torrents():
            if not entry.files and entry.handle.status().has_metadata:
                info = entry.handle.torrent_file()
                if info is not None:
                    self._read_metadata(entry, info)
                    if self.on_metadata:
                        self.on_metadata(entry.info_hash, entry)
            if not entry.files or entry.state == "error":
                continue
            progress = entry.handle.file_progress(entry.handle.piece_granularity)
            ready_for_read = flushed_for_playback.get(entry.info_hash, ())
            needs_flush = set()
            for item, downloaded in zip(entry.files, progress):
                item["downloaded"] = downloaded
                index = item["index"]
                if index in entry.complete or not item["size"] or downloaded != item["size"]:
                    continue
                path = entry.file_path(index)
                if path is None or not os.path.isfile(path):
                    continue
                if index not in ready_for_read:
                    needs_flush.add(index)
                    continue
                entry.complete.add(index)
                if not entry.is_complete(index):
                    entry.complete.discard(index)
                    continue
                completed.append((entry.info_hash, entry, index))
                if self.on_file_complete:
                    self.on_file_complete(entry.info_hash, entry, index)
            # Hash-verified pieces may still have queued disk writes. Do not
            # publish a preallocated file until those writes have been flushed.
            if (
                needs_flush
                and entry.info_hash not in self._playback_flushes
                and entry.info_hash not in self._cache_flushes
                and entry.info_hash not in self._cache_rechecks
            ):
                self._enable_cache_alerts()
                entry.handle.flush_cache()
                self._playback_flushes[entry.info_hash] = needs_flush
        return completed

    def refresh(self) -> None:
        for entry in self.torrents():
            if entry.state == "error":
                continue
            status = entry.handle.status()
            entry.downloaded = status.total_wanted_done
            entry.peers = status.num_peers
            entry.progress = status.progress
            if entry.files:
                entry.state = "finished" if status.is_finished else "downloading"

    def shutdown(self) -> None:
        if self._session is not None:
            self._session.pause()
            self._torrents.clear()
            self._cache_flushes.clear()
            self._playback_flushes.clear()
            self._cache_rechecks.clear()
            self._session = None
