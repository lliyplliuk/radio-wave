"""Управление библиотекой, очередью, плеером и загрузками."""

from __future__ import annotations

import math
import os
import shutil
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock
from typing import Any

from . import catalog, config, tags
from .cache_window import cache_files_to_prune
from .candidate_window import candidate_window
from .diagnostics import playback_log, trace
from .language import effective_language
from .language_codes import normalize_language_code
from .library import Catalogue, _normalize_language, is_audio
from .metadata_repair import repair_saved_text
from .online_radio import (
    AcquisitionTaste,
    FAILED_RETRY_SECONDS,
    METADATA_WAIT_SECONDS,
    OnlineChoice,
    SOURCE_ATTEMPTS,
    acquisition_taste,
    ambiguous_recording_copies,
    cached_recording_matches,
    cached_recording_order,
    compatible_recording_copies,
    is_live_path,
    matching_recording_paths,
    matching_title_paths,
    radio_choices,
    recording_key,
    release_genre_hint,
    release_choices,
    search_choices,
    SOURCE_WAIT_SECONDS,
    studio_first_paths,
    title_from_audio_path,
    visible_artist_choices,
)
from .queue import RadioQueue
from .ranker import DISCOVERY_GENRES, TasteProfile, genre_fit, genre_suppressed, language_side, suppressed_genres, track_genre_family
from .state import AppState


def _diagnostic_source(track: dict | None) -> str:
    if track is None:
        return "unknown"
    return "torrent" if track.get("source") == "torrent" else "local"


def _diagnostic_provider(release) -> str:
    name = getattr(release, "provider", "")
    return name if name in {"rutor", "jacred", "torznab"} else "configured"


def _track_label(artist: str, title: str) -> str:
    return " — ".join(part for part in (artist, title) if part) or "Выбранная запись"


_SOURCE_FAILURES = {
    "attempt_budget": "Не удалось дождаться файла.",
    "foreground_budget": "Не удалось дождаться файла.",
    "no_peers": "Нет активных раздающих.",
    "no_verified_progress": "Раздающие не передали файл.",
    "no_matching_audio": "В раздачах не оказалось нужной записи.",
    "index_unavailable": "Поиск сейчас недоступен.",
    "metadata_timeout": "Раздающие не передали данные для выбранного трека.",
}


@dataclass
class _OnlineRequest:
    prefetch: bool
    recording: Any = None
    choices: deque[OnlineChoice] = field(default_factory=deque)
    cancelled: Event = field(default_factory=Event)
    future: Future | None = None
    choice: OnlineChoice | None = None
    entry: Any = None
    metadata: bytes | None = None
    deadline: float = 0.0
    attempt_ends: float = 0.0
    attempt_started: float = 0.0
    foreground_ends: float = 0.0
    downloaded: int = 0
    track_id: str | None = None
    attempts: int = 0
    progress_bucket: int = -1
    discovery: bool = False
    manual: bool = False
    label: str = ""
    last_rejection: str = ""
    language_target: str | None = None
    genre_target: str | None = None
    alternate_language: OnlineChoice | None = None
    genre_denied: Callable[[dict], bool] | None = None
    visible_ends: float = 0.0
    visible_artists: frozenset[str] = frozenset()

    @property
    def inactivity_limit(self) -> int:
        return METADATA_WAIT_SECONDS if self.prefetch else config.FOREGROUND_IDLE_SECONDS

    @property
    def attempt_limit(self) -> int:
        return SOURCE_WAIT_SECONDS if self.prefetch else config.FOREGROUND_FILE_SECONDS

    def start_foreground(self, now: float) -> None:
        self.prefetch = False
        self.foreground_ends = now + config.FOREGROUND_SEARCH_SECONDS
        if self.choice is not None:
            self.deadline = min(self.deadline or now + self.inactivity_limit, now + self.inactivity_limit)
            self.attempt_ends = min(self.attempt_ends, now + self.attempt_limit)


@dataclass
class _PendingWait:
    track_id: str
    started: float
    changed: float
    downloaded: int


class RadioService:
    def __init__(
        self,
        state=None,
        catalogue=None,
        player=None,
        torrents=None,
        start_torrents: bool = True,
        online: bool = True,
        index=None,
    ) -> None:
        config.ensure_dirs()
        self.state = state if state is not None else AppState()
        self.catalogue = catalogue if catalogue is not None else Catalogue()
        self.state.restore_feedback_language_codes(self.catalogue.all())
        self.player = player
        self.torrents = torrents
        self._shutdown_lock = Lock()
        self._shutdown_prepared = False
        self._shutdown_started = False
        self.on_seek: Callable[[float], None] | None = None
        self._bulk_russian: set[str] = set()
        self._awaiting = False
        self._prefer_visible_ready_on_wait = False
        self._requested: str | None = None
        self._starting: dict | None = None
        self._manual_start: str | None = None
        self._confirmed_track_id: str | None = None
        self._audible_seconds = 0.0
        self._listening_sample: tuple[float, float] | None = None
        self._status = "Готов к работе"
        self._missing_local_paths: set[str] = set()
        self._torrent_health: dict[str, bool] = {}
        self._catalogue_revision = 0
        self._awaited_revision = -1
        self._pending_wait: _PendingWait | None = None
        self._catalogue_batch_depth = 0
        self._catalogue_save_pending = False
        self._catalogue_refresh_pending = False
        self._prune_signature = None
        self._prune_candidates = ()
        self._catalogue_snapshot: list[dict] = []
        self._active_tracks: list[dict] = []
        self._active_ids: set[str] = set()
        self._online_enabled = online and torrents is not None and player is not None
        self._index = index
        self._idle_visible_fill = self._online_enabled and bool(self._automatic_source_files())
        self._visible_fill_cycle: int | None = None
        self._visible_fill_best = 0
        self._visible_fill_attempts = 0
        self._visible_fill_retry_at = 0.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radio-index")
        self._online: _OnlineRequest | None = None
        self._radio_active = False
        self._stopped = True
        self._previous_cursor: int | None = None
        self._last_refill = -1
        self._new_discovery_offer: str | None = None
        self._retry_refill_at = 0.0
        self._retry_prefetch_at = 0.0
        self._last_language_refill: dict[str, int] = {}
        self._retry_language_at: dict[str, float] = {}
        self._last_genre_refill = -1
        self._retry_genre_at: dict[tuple[str | None, str], float] = {}
        self._failed_releases: dict[str, float] = {}
        self._skipped_release_urls: set[str] = set()
        self._language_mismatches: dict[tuple[str, str], float] = {}
        self._failed_recordings: dict[str, float] = {}
        self._failed_torrents: dict[str, float] = {}
        self._pending_progress: dict[str, tuple[dict[str, int], float]] = {}
        self._pending_started: dict[str, float] = {}
        self._discoveries: list[catalog.CatalogRecording] = []
        self._language_lookup: tuple[Future, str, str] | None = None

        self.queue = RadioQueue(
            lookahead=config.QUEUE_LOOKAHEAD,
            profile=TasteProfile.from_dict(self.state.taste),
            seen_ids=[item["id"] for item in self.state.history if item.get("id")],
            finished_listens=self.state.finished_listens,
        )
        self.queue.set_history(self.state.history)
        if self.state.early_skips:
            self.queue.set_early_skips(self.state.early_skips)
        self.queue.fill(self.state.likes, self.state.dislikes)

        if self.player is not None:
            self.player.set_on_eos(self._on_eos)
            self.player.set_on_error(self._on_error)
            self.player.set_volume(float(self.state.setting("volume", 0.8)))

        with self._batch_catalogue_updates():
            if self.torrents is not None:
                self.torrents.on_file_complete = self._on_file_complete
                self.torrents.on_metadata = self._on_metadata
                if start_torrents:
                    self._restore_sources()
            self._refresh_catalogue()

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, text: str) -> None:
        self._status = text

    def _refresh_catalogue(self) -> None:
        if self._catalogue_batch_depth:
            self._catalogue_refresh_pending = True
        else:
            self._refresh_catalogue_now()

    def _save_catalogue(self) -> None:
        if self._catalogue_batch_depth:
            self._catalogue_save_pending = True
        else:
            self.catalogue.save()

    @contextmanager
    def _batch_catalogue_updates(self):
        with self.state.batch_saves():
            self._catalogue_batch_depth += 1
            try:
                yield
            finally:
                try:
                    if self._catalogue_batch_depth == 1:
                        if self._catalogue_refresh_pending:
                            self._catalogue_refresh_pending = False
                            self._refresh_catalogue_now()
                        if self._catalogue_save_pending:
                            self._catalogue_save_pending = False
                            self.catalogue.save()
                finally:
                    self._catalogue_batch_depth -= 1

    def _refresh_catalogue_now(self) -> None:
        tracks = []
        missing = set()
        torrent_health: dict[str, bool] = {}
        repaired = False
        repairs = {}
        for track in self.catalogue.all():
            track["ready"] = self.is_ready(track)
            touched, fields = repair_saved_text(track, available=track["ready"])
            repaired |= touched
            if fields:
                repairs[track["id"]] = (track, fields)
            if track.get("source") == "torrent":
                info_hash = track.get("info_hash", "")
                if info_hash not in torrent_health:
                    torrent_health[info_hash] = self._torrent_hash_usable(info_hash)
                track.pop("broken", None)  # Old snapshots persisted transient source failures.
                tracks.append({**track, "unavailable": not torrent_health[info_hash] and not track["ready"]})
            else:
                if not track["ready"] and track.get("path"):
                    missing.add(track["path"])
                tracks.append({**track, "unavailable": not track["ready"]})
        if repaired:
            self._save_catalogue()
            if repairs:
                self.state.repair_feedback_text(repairs)
                self.queue.set_history(self.state.history)
        if missing != self._missing_local_paths:
            playback_log.info("local_availability missing=%d", len(missing))
        self._missing_local_paths = missing
        self._torrent_health = torrent_health
        self._catalogue_snapshot = tracks
        self._refresh_candidate_projection()

    def _refresh_candidate_projection(self) -> None:
        current = self.queue.current
        request = self._online
        automatic_sources = self._automatic_source_files()
        disliked = {item["id"] for item in self.state.dislikes if item.get("id")}

        def is_automatic(track: dict) -> bool:
            return self._is_automatic_track(track, automatic_sources)

        fit = genre_fit(self.state.likes, self.queue.profile, disliked=self.state.dislikes)
        self._active_tracks = candidate_window(
            self._catalogue_snapshot,
            is_automatic=is_automatic,
            is_unheard=self.queue.is_unheard,
            is_schedulable=lambda track: track["id"] not in disliked and not self.queue.is_skipped(track["id"]),
            taste_score=fit,
            is_genre_suppressed=genre_suppressed(self.state.likes, self.state.dislikes, self.queue.profile, fit=fit),
            language_reserves=self.queue.language_reserve_targets(),
            genre_reserves=self.queue.genre_reserve_targets(),
            upcoming_ids=(
                *(track["id"] for track in self.queue.upcoming),
                *(track_id for track_id in (
                    self.queue.offered_id(), *self.queue.recovery_ids(), self._new_discovery_offer,
                ) if track_id is not None),
            ),
            pinned_ids=(
                current["id"] if current is not None else None,
                self._requested,
                self._starting["id"] if self._starting is not None else None,
                request.track_id if request is not None else None,
            ),
        )
        self._active_ids = {track["id"] for track in self._active_tracks}
        purely_automatic = self._online_enabled and not any(
            not track.get("unavailable") and not is_automatic(track) for track in self._active_tracks
        )
        self._catalogue_revision += 1
        self.queue.set_catalogue(self._active_tracks, automatic_sources=automatic_sources.keys() if purely_automatic else ())
        self._plan_priorities()

    def _refresh_local_changes(self) -> None:
        if any(os.path.isfile(path) for path in self._missing_local_paths) or any(
            track.get("source") != "torrent" and not self.is_ready(track)
            for track in self.queue.upcoming
        ):
            self._refresh_catalogue()

    def is_ready(self, track: dict) -> bool:
        path = track.get("path", "")
        if not path:
            return False
        if track.get("source") != "torrent":
            return os.path.isfile(path)
        if self.torrents is None:
            return False
        entry = self.torrents.get(track.get("info_hash", ""))
        index = track.get("file_index", -1)
        return entry is not None and index in entry.complete and entry.file_path(index) == path and entry.is_complete(index)

    def tracks(self) -> list[dict]:
        """Tracks currently displayed in Collection."""
        return list(self._active_tracks)

    def search_tracks(self) -> list[dict]:
        """Explicit search can reach dormant records from any known source."""
        return self.catalogue.all()

    def import_paths(self, paths: list[str], mark_russian: bool = False) -> int:
        added = self.catalogue.scan_paths(paths, mark_russian=mark_russian)
        for path in paths:
            if os.path.isdir(path):
                self.state.add_directory(path)
        self._refresh_catalogue()
        self._set_status(f"Импортировано записей: {len(added)}")
        return len(added)

    def update_track(
        self, track_id: str, artist=None, title=None, genre=None, language=None, mbid=None, language_code=None,
    ) -> bool:
        fields = dict(artist=artist, title=title, genre=genre, language=language, mbid=mbid, language_code=language_code)
        track = self.catalogue.get(track_id)
        if track is None:
            return False
        edited = set(track.get("edited", []))
        edited.update(key for key, value in fields.items() if value is not None)
        track["edited"] = sorted(edited)
        self.catalogue.update(track_id, **fields)
        if language is not None or language_code is not None:
            self._refresh_language_feedback(track)
        self._refresh_catalogue()
        return True

    def _refresh_language_feedback(self, track: dict) -> None:
        if self.state.update_feedback_language(track):
            self.queue.set_history(self.state.history)
            self.queue.set_finished_listens(self.state.finished_listens)
            self.queue.rebuild(self.state.likes, self.state.dislikes)

    @property
    def first_run(self) -> bool:
        return len(self.catalogue) == 0

    def current(self) -> dict | None:
        return self.queue.current

    def upcoming(self) -> list[dict]:
        return [item for item in self.queue.snapshot() if item.get("state") != "playing"]

    def visible_upcoming(self) -> list[dict]:
        return self.queue.visible_upcoming()

    def pin_visible(self, track_ids: Iterable[str]) -> None:
        """Protect the upcoming prefix explicitly rendered by the native queue."""
        self.queue.pin_visible(tuple(track_ids)[:len(self.visible_upcoming())])

    def _automatic_source_files(self) -> dict[str, set[int]]:
        return {
            item["info_hash"]: set(item.get("manual_file_indices", ())) for item in self._source_records()
            if item.get("info_hash") and item.get("source_url")
            and item.get("automatic", "file_index" not in item and "file_indices" not in item)
        }

    @staticmethod
    def _is_automatic_track(track: dict, sources: dict[str, set[int]]) -> bool:
        manual_files = sources.get(track.get("info_hash"))
        return track.get("source") == "torrent" and manual_files is not None and track.get("file_index") not in manual_files

    def reason_for(self, track: dict | None) -> str:
        return self.queue.reason(track)

    def status_text(self) -> str:
        if self._awaiting:
            if self._online is not None and not self._online.prefetch:
                return self._status
            return "Ожидание файла…"
        if self.is_playing():
            return "Играет"
        return self._status

    @property
    def waiting(self) -> bool:
        return self._awaiting

    def _begin_playback(self, track: dict, *, autoplay: bool = True) -> None:
        self._awaiting = False
        self._pending_wait = None
        self._requested = None
        if self.player is None:
            self._set_status("Плеер недоступен")
            return
        if not self.player.load(track["path"]):
            playback_log.info("playback_load_failed source=%s", _diagnostic_source(track))
            if track.get("source") != "torrent" and not self.is_ready(track):
                self.queue.current = None
                self._refresh_catalogue()
            else:
                self.queue.set_broken(track["id"])
            if self._manual_start is not None:
                self._report_manual_failure(self._manual_start, "Не удалось открыть аудиофайл.")
            else:
                self._set_status("Файл не найден — трек пропущен")
                self._awaiting = autoplay
            return
        self._prefer_visible_ready_on_wait = False
        self._starting = track
        self._confirmed_track_id = None
        self._audible_seconds = 0.0
        self._listening_sample = None
        if autoplay:
            self._stopped = False
            self._listening_sample = (time.monotonic(), 0.0)
            self.player.play()
            self._set_status("Запуск воспроизведения…")
        else:
            self.queue.set_history(self.state.history)
            self._set_status("Остановлено" if self._stopped else "Пауза")
        self._plan_priorities()

    def _confirm_playback(self) -> None:
        if self._starting is not None:
            playback_log.info("audio_started source=%s", _diagnostic_source(self._starting))
            trace(
                "playback_started", track_id=self._starting["id"], artist=self._starting.get("artist"),
                title=self._starting.get("title"), requested=self._manual_start,
                source=_diagnostic_source(self._starting),
            )
            self.state.remember(self._starting)
            self.queue.set_history(self.state.history)
            self._confirmed_track_id = self._starting["id"]
            self._starting = None
            self._manual_start = None
            self._refresh_candidate_projection()
            self._set_status("Играет")

    def _sample_listening(self) -> None:
        current = self.queue.current
        if current is None or current["id"] != self._confirmed_track_id or not self.is_playing():
            self._listening_sample = None
            return
        now = time.monotonic()
        position = self.position()
        prior, self._listening_sample = self._listening_sample, (now, position)
        if prior is not None and math.isfinite(position):
            elapsed = max(0.0, now - prior[0])
            advanced = position - prior[1]
            # A position discontinuity is not audible time.
            if 0 <= advanced <= elapsed + 0.05:
                self._audible_seconds += min(elapsed, advanced)

    def play_current(self) -> None:
        self._radio_active = True
        current = self.queue.current
        if current is not None and self.is_ready(current):
            self._begin_playback(current)
            return
        self.next_track(prefer_visible_ready=False)

    def play_track(self, track: dict, *, request_label: str | None = None, autoplay: bool = True) -> None:
        """Включить явно выбранную запись, при необходимости скачав её."""
        self._clear_online_playback()
        self._radio_active = autoplay
        self._manual_start = request_label or _track_label(track.get("artist", ""), track.get("title", ""))
        trace("request_library_track", track_id=track.get("id"), requested=self._manual_start)
        chosen = self.catalogue.get(track.get("id", "")) or track
        if not self.is_ready(chosen):
            library = self.catalogue.all()
            key = recording_key(chosen.get("artist") or "", chosen.get("title") or "")
            copies = [candidate for candidate in library if
                      recording_key(candidate.get("artist") or "", candidate.get("title") or "") == key]
            ambiguous = ambiguous_recording_copies([chosen, *copies])
            ready_copy = next((
                candidate for candidate in copies
                if compatible_recording_copies(chosen, candidate, ambiguous=ambiguous) and self.is_ready(candidate)
            ), None)
            if ready_copy is not None:
                trace("request_ready_copy", track_id=chosen.get("id"), ready_track_id=ready_copy["id"])
                chosen = ready_copy
        if self._failed_torrents.pop(chosen.get("info_hash"), None) is not None:
            self._refresh_catalogue()
        self._play_known(chosen, autoplay=autoplay)

    def _play_known(self, track: dict, *, autoplay: bool = True) -> None:
        known = self.catalogue.get(track.get("id", ""))
        if known is None:
            return
        if self.player is not None and self.is_playing():
            self._confirm_playback()
        self._starting = None
        if self.is_ready(known):
            if known["id"] not in self._active_ids:
                self._requested = known["id"]
                self._refresh_candidate_projection()
            self.queue.set_ready(known["id"])
            selected = self.queue.select(known["id"], self.state.likes, self.state.dislikes)
            if selected is not None:
                self._begin_playback(selected, autoplay=autoplay)
            return
        if not autoplay:
            self._report_manual_failure(self._manual_start or _track_label(known.get("artist", ""), known.get("title", "")),
                                        "Аудиофайл недоступен.")
            return
        if known.get("source") != "torrent" or not self._has_torrent(known):
            if self._manual_start is not None:
                self._report_manual_failure(self._manual_start, "Аудиофайл недоступен.")
            else:
                self._set_status("Файл недоступен")
            return
        if self.player is not None:
            self.player.stop()
        self.queue.current = None
        self._requested = known["id"]
        self._awaiting = True
        self._track_pending_wait(known["id"])
        if known["id"] not in self._active_ids:
            self._refresh_candidate_projection()
        self._plan_priorities()

    def next_track(self, *, autoplay: bool = True, prefer_visible_ready: bool = True) -> None:
        self._previous_cursor = None
        if not autoplay:
            self._refresh_local_changes()
            selected = self.queue.next_track(self.state.likes, self.state.dislikes) or self.queue.next_available()
            if selected is not None:
                self._manual_start = None
                self._begin_playback(selected, autoplay=False)
            return
        if self._awaiting:
            self._cancel_online()
        self._radio_active = True
        if self.player is not None:
            if self.is_playing():
                self._confirm_playback()
            self.player.stop()
        self._starting = None
        self._manual_start = None
        self._requested = None
        self.queue.current = None
        self._advance_queue(prefer_visible_ready=prefer_visible_ready)

    def user_next(self, *, autoplay: bool = True) -> None:
        """Explicit Next may contribute weak feedback from this playback."""
        self._previous_cursor = None
        if self._awaiting:
            self._skip_foreground("user_next")
            return
        if self.is_playing():
            self._confirm_playback()
        self._sample_listening()
        current = self.queue.current
        weak_feedback = False
        duration = 0.0
        if current is not None and current["id"] == self._confirmed_track_id:
            duration = self.duration()
            if math.isfinite(duration) and duration > 0 and self._audible_seconds < duration * 0.25:
                if self.state.record_early_skip(current):
                    self.queue.set_early_skips(self.state.early_skips)
                    weak_feedback = True
        trace(
            "user_next", track_id=current["id"] if current is not None else None,
            listened_seconds=round(self._audible_seconds, 2), duration_seconds=round(duration, 2),
            weak_feedback=weak_feedback,
        )
        self.next_track(autoplay=autoplay)

    def _skip_foreground(self, reason: str) -> None:
        request = self._online
        online_id = request.track_id if request is not None and not request.prefetch else None
        track_id = self._requested or online_id or (self._pending_wait.track_id if self._pending_wait else None)
        elapsed = time.monotonic() - self._pending_wait.started if self._pending_wait else 0.0
        trace("waiting_track_skipped", reason=reason, track_id=track_id, elapsed_seconds=round(elapsed, 2))
        if request is not None and not request.prefetch:
            if not request.manual and request.choice is not None and online_id is None:
                self._skipped_release_urls.add(request.choice.release.source_url)
            self._cancel_online()
        self._pending_wait = None
        self._requested = None
        self._manual_start = None
        self.queue.current = None
        self._radio_active = True
        if track_id is not None:
            self.queue.skip_waiting(track_id)
        self._advance_queue(prefer_ready=True, prefer_visible_ready=True)

    def _advance_queue(self, *, prefer_ready: bool = False, prefer_visible_ready: bool = False) -> None:
        self._prefer_visible_ready_on_wait = prefer_visible_ready
        self._refresh_local_changes()
        self._awaited_revision = self._catalogue_revision
        can_discover = self._online_enabled and self._radio_active and (
            self._online is not None or time.monotonic() >= self._retry_refill_at
        )
        candidate = self.queue.next_available() if prefer_ready else None
        if candidate is None:
            candidate = self.queue.next_track(
                self.state.likes, self.state.dislikes, allow_repeats=not can_discover,
            )
        if candidate is None and prefer_visible_ready:
            candidate = self.queue.next_visible_ready(allow_repeats=not can_discover)
        if candidate is not None:
            self._begin_playback(candidate)
            return
        was_awaiting = self._awaiting
        pending = self.queue.pending_head(allow_repeats=not can_discover)
        self._awaiting = pending is not None
        self._track_pending_wait(pending["id"] if pending is not None else None)
        if self._awaiting and not was_awaiting:
            trace("wave_waiting", allow_repeats=not can_discover, upcoming=len(self.queue.upcoming))
        if not self._awaiting:
            if self._online is not None:
                if self._online.prefetch:
                    self._online.start_foreground(time.monotonic())
                    self._set_status("Ищем следующий трек…")
                self._awaiting = True
            elif can_discover:
                self._lookup_online()
            else:
                self._set_status("Нет доступных треков")
        self._plan_priorities()

    def current_liked(self) -> bool:
        current = self.queue.current
        return current is not None and self.state.is_liked(current["id"])

    def toggle_like_current(self) -> None:
        current = self.queue.current
        if current is None or self._starting is not None:
            return
        if not self.state.unlike(current["id"]):
            self.like_current()
            return
        self.queue.rebuild(self.state.likes, self.state.dislikes)
        self._plan_priorities()
        self._set_status("Лайк снят")

    def like_current(self) -> None:
        current = self.queue.current
        if current is not None and self._starting is None:
            self.state.like(current)
            self.queue.set_early_skips(self.state.early_skips)
            self.queue.like(current, self.state.likes, self.state.dislikes)
            self._plan_priorities()
            self._set_status("Отмечено: нравится")

    def dislike_current(self) -> None:
        current = self.queue.current
        if current is not None and self._starting is None:
            self.state.dislike(current)
            self.queue.set_early_skips(self.state.early_skips)
            self.queue.dislike(current, self.state.likes, self.state.dislikes)
            self._refresh_candidate_projection()
            self._set_status("Отмечено: не нравится")
            self.next_track()

    def toggle_play(self) -> None:
        if self._radio_active and (self.waiting or self.queue.current is not None):
            self.pause_playback()
        else:
            self.play_playback()

    def play_playback(self) -> None:
        if self.player is None or (self._radio_active and (self.waiting or self.queue.current is not None)):
            return
        self._idle_visible_fill = False
        if not self._stopped and self.queue.current is not None and self.player.is_loaded():
            self._radio_active = True
            self._listening_sample = (time.monotonic(), self.position())
            self.player.play()
            self._set_status("Играет")
            return
        self._retry_refill_at = 0.0
        self.queue.reset_skips()
        self._skipped_release_urls.clear()
        self.play_current()

    def pause_playback(self) -> None:
        self._idle_visible_fill = False
        if self.player is None or not self._radio_active:
            if self._online is not None and self._online.visible_ends:
                self._cancel_online()
                self._plan_priorities()
            return
        self._radio_active = False
        self._cancel_online()
        if self._awaiting:
            self._awaiting = False
            self._requested = None
            self._pending_wait = None
            self._manual_start = None
            self._plan_priorities()
        elif self.is_playing():
            self._confirm_playback()
            self._sample_listening()
            self.player.pause()
        elif self.queue.current is not None and self.player.is_loaded():
            self.player.pause()
        self._listening_sample = None
        self._set_status("Пауза")

    def stop_playback(self) -> None:
        self._idle_visible_fill = False
        if self.player is None or (self._stopped and not self.waiting and self._online is None):
            return
        if self.is_playing():
            self._confirm_playback()
        self._cancel_online()
        self.player.stop()
        self._stopped = True
        self._previous_cursor = None
        self._radio_active = self._awaiting = False
        self._starting = self._manual_start = self._requested = self._pending_wait = None
        self._confirmed_track_id = self._listening_sample = None
        self._audible_seconds = 0.0
        self._set_status("Остановлено")
        self._plan_priorities()

    def playback_status(self) -> str:
        if self._radio_active and (self.waiting or self.queue.current is not None and not self._stopped):
            return "Playing"
        return "Paused" if self.queue.current is not None and not self._stopped else "Stopped"

    def can_play(self) -> bool:
        return self.player is not None and bool(self.queue.current or self.queue.upcoming or self._online_enabled)

    def can_go_next(self) -> bool:
        return self.player is not None and bool(self.waiting or self.queue.upcoming or self._online_enabled)

    def _previous_track(self) -> tuple[int, dict] | None:
        current = self.queue.current
        current_id = current["id"] if current is not None else None
        ready = {track["id"]: track for track in self._catalogue_snapshot if track.get("ready")}
        history = self.state.history
        stop = len(history) if self._previous_cursor is None else self._previous_cursor
        for index in range(stop - 1, -1, -1):
            track_id = history[index].get("id")
            track = ready.get(track_id)
            if track_id != current_id and track is not None and self.is_ready(track):
                return index, track
        return None

    def can_go_previous(self) -> bool:
        return self.player is not None and (self.can_seek() or self._previous_track() is not None)

    def previous_track(self) -> None:
        if self.player is None:
            return
        if self.is_playing():
            self._confirm_playback()
        previous = self._previous_track()
        if previous is not None:
            index, track = previous
            self.play_track(track, autoplay=self.playback_status() == "Playing")
            self._previous_cursor = index
        elif self.can_seek():
            self.seek(0.0)

    def can_seek(self) -> bool:
        return self.player is not None and self.player.can_seek()

    def seek(self, seconds: float) -> bool:
        if self.player is not None:
            if self.is_playing():
                self._confirm_playback()
            self._sample_listening()
            accepted = self.player.seek(seconds)
            if accepted:
                target = min(max(0.0, seconds), self.duration())
                self._listening_sample = (time.monotonic(), target) if self.is_playing() else None
                if self.on_seek is not None:
                    self.on_seek(target)
            trace("seek", accepted=accepted, seconds=seconds)
            return accepted
        return False

    def set_volume(self, value: float) -> None:
        self.state.set_setting("volume", float(value))
        if self.player is not None:
            self.player.set_volume(value)

    def volume(self) -> float:
        return float(self.state.setting("volume", 0.8))

    def position(self) -> float:
        return self.player.position() if self.player is not None else 0.0

    def duration(self) -> float:
        return self.player.duration() if self.player is not None else 0.0

    def is_playing(self) -> bool:
        return self.player.is_playing() if self.player is not None else False

    def _on_eos(self) -> None:
        if self.queue.current is None or not self._radio_active:
            return
        self._confirm_playback()
        if self.queue.current is not None:
            self.state.remember_finished(self.queue.current)
            self.queue.set_finished_listens(self.state.finished_listens)
        self.next_track()

    def _on_error(self, message: str) -> None:
        self._starting = None
        playback_log.info("decoder_failed source=%s", _diagnostic_source(self.queue.current))
        if self.queue.current is None:
            return
        if self.queue.current is not None:
            current = self.queue.current
            if current.get("source") != "torrent" and not self.is_ready(current):
                self.queue.current = None
                self._refresh_catalogue()
            else:
                self.queue.set_broken(current["id"])
        if self._manual_start is not None:
            self.player.stop()
            self.queue.current = None
            self._report_manual_failure(self._manual_start, "Не удалось декодировать аудио.")
            return
        if not self._radio_active:
            self.player.stop()
            self._set_status(f"Ошибка воспроизведения: {message}")
            return
        self.next_track()
        if self.queue.current is None and not self._awaiting:
            self._set_status(f"Ошибка воспроизведения: {message}")

    @property
    def taste(self) -> dict[str, float]:
        return self.state.taste

    def set_taste(self, **kwargs: Any) -> None:
        taste = self.state.taste
        for key, value in kwargs.items():
            if key in taste:
                taste[key] = float(value)
        self.state.save()
        self.queue.profile = TasteProfile.from_dict(taste)
        self.queue.upcoming = []
        self._refresh_catalogue()

    def set_similar_recordings(self, mbids) -> None:
        self.queue.set_similar_recordings(mbids)
        self._catalogue_revision += 1
        self._plan_priorities()

    def set_discovery_candidates(self, recordings) -> None:
        self._discoveries = list(recordings)
        self.set_similar_recordings(recording.mbid for recording in self._discoveries)

    def _index_provider(self):
        if self._index is None:
            from . import torrent_index

            self._index = torrent_index
        return self._index

    def _provider_label(self) -> str:
        return "public" if getattr(self._index, "__name__", "") == "radio_wave.torrent_index" else "configured"

    def _sources_to_skip(self, include_saved: bool) -> set[str]:
        now = time.monotonic()
        saved = {record["source_url"] for record in self._source_records() if record.get("source_url")}
        self._failed_releases = {url: until for url, until in self._failed_releases.items() if until > now}
        online_only = saved | self._skipped_release_urls if include_saved else set()
        return online_only | self._failed_releases.keys()

    def _acquisition_taste(self) -> AcquisitionTaste:
        profile = self.queue.profile
        return acquisition_taste(
            self.state.likes, genre_weight=profile.genre_weight,
            avoided_genres=suppressed_genres(self.state.likes, self.state.dislikes, profile),
            genre_denied=(genre_suppressed(self.state.likes, self.state.dislikes, profile)
                          if profile.genre_weight and self.state.dislikes else None),
            artist_exposure=self.queue.artist_exposure(),
        )

    def _missing_language(self) -> str | None:
        now = time.monotonic()
        cycle = len(self.state.history)
        excluded = {
            side for side in ("ru", "foreign")
            if self._last_language_refill.get(side) == cycle or now < self._retry_language_at.get(side, 0)
        }
        return self.queue.missing_language(skip=excluded)

    def _missing_genre(self, language: str | None, *, only: set[str] | None = None) -> str | None:
        if self._last_genre_refill == len(self.state.history):
            return None
        now = time.monotonic()
        excluded = {genre for (side, genre), at in self._retry_genre_at.items() if side == language and now < at}
        if only is not None:
            excluded.update(set(DISCOVERY_GENRES) - only)
        return self.queue.missing_genre(language=language, skip=excluded)

    def _learned_genre_shortage(self, cycle: int) -> tuple[str | None, str | None]:
        if self._last_genre_refill == cycle or not (self.state.likes or self.state.dislikes):
            return None, None
        neutral = self.queue.genre_reserve_targets()
        allowed_sides = self.queue.language_reserve_targets()
        now = time.monotonic()
        for side in (("ru", "foreign") if cycle % 2 == 0 else ("foreign", "ru")):
            if side not in allowed_sides or self._last_language_refill.get(side) == cycle or now < self._retry_language_at.get(side, 0):
                continue
            stronger = {genre for genre, target in self.queue.genre_reserve_targets(language=side).items()
                        if target > neutral.get(genre, 0)}
            if stronger and (genre := self._missing_genre(side, only=stronger)) is not None:
                return side, genre
        return None, None

    def _concentrated_artist_stock(self) -> tuple[str, str] | None:
        threshold = self.queue.lookahead // 2
        if max(self.queue.artist_exposure().values(), default=0) < threshold:
            return None
        allowed_sides = self.queue.language_reserve_targets() if self.queue.profile.language_weight else {"ru", "foreign"}
        genre_denied = genre_suppressed(self.state.likes, self.state.dislikes, self.queue.profile)
        concentrated = self.queue.concentrated_artists()
        automatic = self._automatic_source_files()
        if concentrated and all(self._is_automatic_track(track, automatic) for track in concentrated):
            groups = list(dict.fromkeys((track_genre_family(track), side) for track in concentrated
                                        if (side := language_side(track)) in allowed_sides and not genre_denied(track)))
            candidates = [(family, side) for family, side in groups if family in DISCOVERY_GENRES]
            if not candidates:
                candidates = [(family, side) for _genre, side in groups
                              if (family := self.queue.missing_genre(language=side)) is not None]
            if candidates:
                return candidates[len(self.state.history) % len(candidates)]
        supplied = Counter((track_genre_family(track), language_side(track))
                           for track in self.queue.upcoming if not genre_denied(track))
        return next(((family, side) for (family, side), count in supplied.most_common()
                     if family in DISCOVERY_GENRES and side in allowed_sides and count >= threshold), None)

    def _lookup_online(
        self, recording=None, prefetch: bool = False, *, taste: AcquisitionTaste | None = None,
        language_target: str | None = None, genre_target: str | None = None,
        artist_fallback: tuple[str, str] | None = None,
        visible_fill: tuple[dict, ...] = (),
    ) -> None:
        label = _track_label(recording.artist, recording.title) if recording is not None else ""
        if recording is None and not prefetch and language_target is None:
            language_target = self._missing_language()
            genre_target = genre_target or self._missing_genre(language_target)
        request = _OnlineRequest(
            prefetch, recording, discovery=recording is None, manual=recording is not None, label=label,
            language_target=language_target,
            genre_target=genre_target,
            visible_ends=time.monotonic() + 40 if visible_fill else 0,
        )
        if language_target is not None:
            self._last_language_refill[language_target] = len(self.state.history)
        if genre_target is not None:
            self._last_genre_refill = len(self.state.history)
        self._online = request
        index = self._index_provider()
        mode = "prefetch" if prefetch else ("exact" if recording is not None else "radio")
        radio_taste = (taste or self._acquisition_taste()) if recording is None else None
        if radio_taste is not None:
            request.genre_denied = radio_taste.genre_denied
        playback_log.info("lookup_started provider=%s mode=%s", self._provider_label(), mode)
        trace(
            "request_started", mode=mode, artist=recording.artist if recording else None,
            title=recording.title if recording else None, genre_target=genre_target,
            suppressed_genres=",".join(sorted(radio_taste.avoided_genres)) if radio_taste else "",
            language_target=language_target,
            artist_fallback=artist_fallback[0] if artist_fallback else None,
            visible_fill=bool(visible_fill),
        )
        skip_sources = self._sources_to_skip(include_saved=recording is None)
        if recording is not None:
            if not recording.mbid and not recording.disambiguation and not recording.album_titles:
                request.future = self._executor.submit(
                    search_choices, index, label, skip_sources, cancelled=request.cancelled.is_set,
                )
            else:
                request.future = self._executor.submit(
                    release_choices, index, recording, skip_sources, allow_unconfirmed=True,
                    cancelled=request.cancelled.is_set,
                )
        elif visible_fill:
            request.visible_artists = frozenset(self.queue.visible_artist_keys(visible_fill))
            sides = self.queue.language_reserve_targets() if self.queue.profile.language_weight else {"ru", "foreign"}
            counts = Counter(language_side(track) for track in visible_fill)
            rotation = len(self.state.history) + len(visible_fill) + self._visible_fill_attempts - 1
            ordered_sides = sorted(sides, key=lambda side: (counts[side], (side == "ru") != bool(rotation % 2)))
            request.future = self._executor.submit(
                visible_artist_choices, index, radio_taste, skip_sources, rotation, request.cancelled.is_set,
                ordered_sides, request.visible_artists,
                {side: self.queue.missing_genre(language=side) for side in ordered_sides},
            )
        else:
            favourites = []
            for item in reversed(self.state.likes):
                track = self.catalogue.get(item["id"]) or item
                favourites.append(dict(track))
            if not favourites:
                favourites = [dict(row) for row in reversed(self.state.history)]
            known = {
                recording_key(item.get("artist", ""), item.get("title", ""))
                for item in self.catalogue.all()
            }
            now = time.monotonic()
            self._failed_recordings = {key: until for key, until in self._failed_recordings.items() if until > now}
            self._language_mismatches = {
                key: until for key, until in self._language_mismatches.items() if until > now
            }
            skip_language_sources = {
                url for side, url in self._language_mismatches if side == language_target
            }
            request.future = self._executor.submit(
                radio_choices, index, favourites, known, skip_sources,
                set(self._failed_recordings), len(self.state.history), request.cancelled.is_set,
                tuple(self._discoveries), radio_taste, genre_target=genre_target, language_target=language_target,
                skip_language_sources=skip_language_sources, foreground=not prefetch, artist_fallback=artist_fallback,
            )
        if not prefetch:
            request.start_foreground(time.monotonic())
            self._awaiting = True
            self._set_status("Ищем раздачи с музыкой…")

    def _clear_online_playback(self) -> None:
        self._idle_visible_fill = False
        self._prefer_visible_ready_on_wait = False
        self._cancel_online()
        self._previous_cursor = None
        if self.is_playing():
            self._confirm_playback()
        if self.player is not None:
            self.player.stop()
        self.queue.current = None
        self._starting = None
        self._manual_start = None
        self._requested = None
        self._pending_wait = None
        self._radio_active = True

    def play_recording(self, recording) -> None:
        """Искать аудиофайл для названия и исполнителя из MusicBrainz."""
        library = self.catalogue.all()
        key = recording_key(recording.artist, recording.title)
        ambiguous = ambiguous_recording_copies([
            recording, *(track for track in library
                         if recording_key(track.get("artist") or "", track.get("title") or "") == key),
        ])
        cached = min(
            (track for track in library
             if cached_recording_matches(recording, track, ambiguous=ambiguous) and self.is_ready(track)),
            key=lambda track: cached_recording_order(recording, track), default=None,
        )
        if cached is not None:
            self.play_track(cached, request_label=_track_label(recording.artist, recording.title))
            return
        if not self._online_enabled:
            self._clear_online_playback()
            self._report_manual_failure(
                _track_label(recording.artist, recording.title), "Поиск раздач сейчас недоступен.",
            )
            return
        self._clear_online_playback()
        self._lookup_online(recording)

    def play_search(self, query: str) -> None:
        if not self._online_enabled or not query.strip():
            self._set_status("Укажите название песни и исполнителя")
            return
        self._clear_online_playback()
        request = _OnlineRequest(False, manual=True, label=query.strip())
        request.start_foreground(time.monotonic())
        request.future = self._executor.submit(
            search_choices, self._index_provider(), query.strip(), self._sources_to_skip(include_saved=False),
            cancelled=request.cancelled.is_set,
        )
        self._online = request
        self._awaiting = True
        self._set_status("Ищем песню и раздачи…")
        playback_log.info("lookup_started provider=%s mode=title", self._provider_label())
        trace("request_started", mode="title", query=request.label)

    def play_release(self, release, recording=None) -> None:
        """Загрузить выбранную раздачу, при необходимости найти в ней песню."""
        if not self._online_enabled:
            self._set_status("Поиск раздач недоступен")
            return
        self._clear_online_playback()
        label = _track_label(recording.artist, recording.title) if recording else release.title
        self._online = _OnlineRequest(False, recording, deque([OnlineChoice(release, recording)]), manual=True, label=label)
        self._online.start_foreground(time.monotonic())
        self._awaiting = True
        self._set_status("Открываем раздачу…")
        trace("request_started", mode="release", requested=label, provider=_diagnostic_provider(release))

    def _cancel_online(self) -> None:
        request, self._online = self._online, None
        if request is None:
            return
        playback_log.info("lookup_cancelled attempts=%d", request.attempts)
        request.cancelled.set()
        if request.future is not None:
            request.future.cancel()
        self._discard_unregistered(request.entry)

    def _discard_unregistered(self, entry) -> None:
        if entry is not None and not any(record.get("info_hash") == entry.info_hash for record in self._source_records()):
            self.torrents.discard(entry)

    def _reject_online_source(self, request: _OnlineRequest, reason: str) -> None:
        now = time.monotonic()
        playback_log.info(
            "source_rejected attempt=%d reason=%s provider=%s peers=%d verified_bytes=%d elapsed_seconds=%.1f",
            request.attempts, reason, _diagnostic_provider(request.choice.release),
            getattr(request.entry, "peers", 0), request.downloaded,
            max(0.0, now - request.attempt_started),
        )
        until = now + FAILED_RETRY_SECONDS
        source_url = request.choice.release.source_url
        if reason == "language_mismatch":
            self._language_mismatches[request.choice.language_target, source_url] = until
            if not request.prefetch and request.alternate_language is None:
                request.alternate_language = replace(request.choice, language_target=None)
        elif reason != "temporarily_skipped":
            self._failed_releases[source_url] = until
        request.last_rejection = reason
        trace(
            "source_rejected", requested=request.label, attempt=request.attempts,
            provider=_diagnostic_provider(request.choice.release), reason=reason,
            selection_id=getattr(request.entry, "info_hash", None),
        )
        if request.entry is not None and request.track_id is not None and reason not in {"known_recording", "known_artist", "language_mismatch"}:
            self._failed_torrents[request.entry.info_hash] = until
        self._discard_unregistered(request.entry)
        request.choice = None
        request.entry = None
        request.metadata = None
        request.track_id = None
        request.downloaded = 0
        request.progress_bucket = -1
        self._requested = None
        self._refresh_catalogue()

    def _report_manual_failure(self, label: str, reason: str) -> None:
        self._manual_start = None
        self._pending_wait = None
        self._requested = None
        self._awaiting = False
        self._radio_active = False
        self._set_status(f"«{label}». {reason}")

    def _finish_online(self, request: _OnlineRequest, error: str = "") -> None:
        self._online = None
        if request.visible_ends and request.prefetch:
            if error:
                self._visible_fill_retry_at = time.monotonic() + 8
            trace("visible_fill_finished", attempts=request.attempts, accepted=not bool(error))
            self._plan_priorities()
            return
        retry_at = time.monotonic() + FAILED_RETRY_SECONDS
        if request.language_target is not None and (
            error or request.choice is None or request.choice.language_target != request.language_target
        ):
            self._retry_language_at[request.language_target] = retry_at
        if request.genre_target is not None and (
            error or request.choice is None or request.choice.genre_hint != request.genre_target
        ):
            self._retry_genre_at[request.language_target, request.genre_target] = retry_at
        if not error:
            playback_log.info("lookup_accepted attempts=%d prefetch=%s", request.attempts, request.prefetch)
            return
        playback_log.info("lookup_exhausted attempts=%d prefetch=%s", request.attempts, request.prefetch)
        if request.recording is not None:
            self._failed_recordings[request.recording.mbid] = time.monotonic() + FAILED_RETRY_SECONDS
        if request.manual:
            reason = _SOURCE_FAILURES.get(request.last_rejection, "Не нашли доступную аудиозапись.")
            trace("request_unavailable", requested=request.label, reason=request.last_rejection or "no_sources")
            self._report_manual_failure(request.label, reason)
            return
        if request.prefetch:
            if request.language_target is None and request.genre_target is None:
                self._retry_prefetch_at = retry_at
            return
        self._retry_refill_at = retry_at
        self._awaiting = False
        self._requested = None
        self._pending_wait = None
        self._set_status(error)
        ready = self.queue.next_available()
        if ready is not None:
            playback_log.info("playback_fallback source=%s", _diagnostic_source(ready))
            self._begin_playback(ready)
        elif self.queue.has_pending(allow_repeats=False):
            self._advance_queue(prefer_visible_ready=self._prefer_visible_ready_on_wait)
        else:
            self._radio_active = False

    def _open_online_source(self, request: _OnlineRequest, metadata: bytes | None = None) -> None:
        release = request.choice.release
        request.metadata = None
        if metadata is not None:
            try:
                request.entry = self.torrents.add_torrent_data(metadata)
                request.metadata = metadata
            except (ValueError, OSError, RuntimeError):
                pass
        if request.entry is None and release.magnet:
            try:
                request.entry = self.torrents.add_magnet(release.magnet)
            except (ValueError, OSError, RuntimeError):
                pass
        if request.entry is None:
            self._reject_online_source(request, "metadata_unavailable")
            return
        request.deadline = time.monotonic() + request.inactivity_limit
        playback_log.info(
            "source_opened attempt=%d format=%s",
            request.attempts, "torrent" if request.metadata is not None else "magnet",
        )
        trace(
            "source_opened", requested=request.label, attempt=request.attempts,
            provider=_diagnostic_provider(release), selection_id=request.entry.info_hash,
        )
        if not request.prefetch:
            self._set_status("Ищем аудиодорожки в раздаче…")

    def _accept_online_source(self, request: _OnlineRequest) -> None:
        entry = request.entry
        choice = request.choice
        recording = choice.recording
        audio_files = {}
        skipped = False
        existing_tracks = {
            track["id"]: track for track in self.catalogue.all() if track.get("info_hash") == entry.info_hash
        }
        for item in entry.files:
            index = item["index"]
            path = entry.file_path(index)
            if not path or not item["size"] or not is_audio(path):
                continue
            existing = existing_tracks.get(f"{entry.info_hash}:{index}")
            if request.discovery and existing is not None and not self.queue.is_unheard(existing):
                continue
            if recording is not None:
                previous_mbid = existing.get("mbid") if existing is not None else None
                if previous_mbid and recording.mbid and previous_mbid != recording.mbid:
                    if not choice.allow_live_fallback or not is_live_path(item["path"], choice.release.title):
                        continue
            if not request.manual and self.queue.is_skipped(f"{entry.info_hash}:{index}"):
                skipped = True
                continue
            audio_files[item["path"]] = index
        if recording is not None:
            matches_with_identity = matching_recording_paths(
                recording, audio_files, choice.release.title,
                allow_title_extension=choice.allow_title_extension,
                allow_live_fallback=choice.allow_live_fallback,
            )
            if matches_with_identity:
                recording = matches_with_identity[0][1]
            matches = [audio_files[path] for path, _identity in matches_with_identity]
        elif choice.query is not None:
            matches = [audio_files[path] for path in matching_title_paths(choice.query, audio_files, choice.release.title)]
        else:
            matches = [audio_files[path] for path in studio_first_paths(audio_files, choice.release.title)]
        if not matches:
            self._reject_online_source(request, "temporarily_skipped" if skipped else "no_matching_audio")
            return
        target_title = ""
        genre_hint = release_genre_hint(choice.release) or choice.genre_hint
        check_genre = request.genre_denied is not None and len(matches) > 1
        if choice.language_target is not None or check_genre:
            candidates = []
            for index in matches:
                observed = title_from_audio_path(entry.files[index]["path"])
                existing = existing_tracks.get(f"{entry.info_hash}:{index}")
                evidence = dict(existing or ())
                if not (evidence.get("lang_source") or evidence.get("ready") or "title" in evidence.get("edited", ())):
                    evidence["title"] = observed
                evidence["genre_hint"] = evidence.get("genre_hint") or genre_hint
                if choice.language_target is None or language_side(evidence) == choice.language_target:
                    denied = request.genre_denied(evidence) if check_genre else False
                    candidates.append((denied, index, observed, evidence))
            if not candidates:
                self._reject_online_source(request, "language_mismatch")
                return
            preferred = [candidate for candidate in candidates if not candidate[0]] or candidates
            if choice.language_target is not None:
                _denied, selected, target_title, _evidence = min(preferred, key=lambda candidate: (
                    is_live_path(entry.files[candidate[1]]["path"], choice.release.title),
                    -effective_language(candidate[3])[1],
                ))
                matches = [selected]
            else:
                matches = [index for _denied, index, _observed, _evidence in preferred]
        playback_log.info(
            "source_metadata_ready attempt=%d matching_files=%d selected_bytes=%d peers=%d",
            request.attempts, len(matches), entry.files[matches[0]]["size"], entry.peers,
        )
        record = {"source_url": choice.release.source_url, "release_title": choice.release.title,
                  "automatic": request.discovery}
        if choice.artist_hint and request.visible_ends:
            record["artist_hint"] = choice.artist_hint
        if genre_hint:
            record["genre_hint"] = genre_hint
        if request.metadata is not None:
            stored = self._metadata_path(entry.info_hash)
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(request.metadata)
            record.update(kind="file", path=str(stored))
        else:
            record.update(kind="magnet", uri=choice.release.magnet)
        saved = next((item for item in self._source_records() if item.get("info_hash") == entry.info_hash), None)
        if saved is not None and not saved.get("release_title"):
            saved["release_title"] = choice.release.title
            self.state.save()
            self._on_metadata(entry.info_hash, entry)
        single_file = recording is not None or choice.query is not None or bool(request.visible_ends and request.prefetch)
        if saved is None:
            if single_file:
                record["file_indices"] = matches[:1]
            self._save_source(entry, record, False)
        elif "file_indices" in saved or "file_index" in saved:
            previous = saved.pop("file_index", None)
            if previous is not None:
                saved.setdefault("file_indices", [previous])
            if not single_file:
                if request.discovery and not saved.get("automatic", False):
                    saved["manual_file_indices"] = sorted(
                        set(saved.get("manual_file_indices", ())) | set(saved["file_indices"])
                    )
                    saved["automatic"] = True
                del saved["file_indices"]
            elif matches[0] not in saved["file_indices"]:
                saved["file_indices"].append(matches[0])
            self.state.save()
            self._on_metadata(entry.info_hash, entry)
        if saved is not None and genre_hint and not saved.get("genre_hint"):
            saved["genre_hint"] = genre_hint
            self.state.save()
            self._on_metadata(entry.info_hash, entry)
        track_id = f"{entry.info_hash}:{matches[0]}"
        if recording is not None:
            track = self.catalogue.get(track_id)
            edited = track.get("edited", ()) if track is not None else ()
            values = {"artist": recording.artist, "title": recording.title, "mbid": recording.mbid}
            self.update_track(track_id, **{key: value for key, value in values.items() if value and key not in edited})
            if recording.mbid:
                self.set_similar_recordings([recording.mbid])
            track = self.catalogue.get(track_id)
            if (recording.mbid and track is not None and track.get("lang_source") in {None, "", "title"}
                    and self._language_lookup is None):
                future = self._executor.submit(catalog.recording_language_code, recording.mbid)
                self._language_lookup = (future, track_id, recording.mbid)
        track = self.catalogue.get(track_id)
        if track is None:
            self._reject_online_source(request, "catalogue_entry_missing")
            return
        if target_title and not self.is_ready(track) and "title" not in track.get("edited", ()):
            if track.get("title") != target_title:
                track["title"] = target_title
                self._save_catalogue()
                self._refresh_catalogue()
        if request.discovery and self.is_ready(track):
            self._on_file_complete(entry.info_hash, entry, track["file_index"])
            if not self.queue.is_unheard(track):
                self._reject_online_source(request, "known_recording")
                return
            if choice.language_target is not None and language_side(track) != choice.language_target:
                self._reject_online_source(request, "language_mismatch")
                return
        trace(
            "source_matched", requested=request.label, track_id=track["id"], file_index=track["file_index"],
            artist=track.get("artist"), title=track.get("title"), provider=_diagnostic_provider(choice.release),
        )
        if request.prefetch:
            if request.visible_ends:
                request.track_id = track["id"]
                request.downloaded = entry.files[matches[0]].get("downloaded", 0)
                request.deadline = time.monotonic() + request.inactivity_limit
                self._refresh_candidate_projection()
                return
            language_offer = choice.language_target is not None and choice.language_target == request.language_target
            held = None if language_offer else self.queue.offered_id()
            if choice.offer and held is None:
                self._new_discovery_offer = track["id"]
                self._refresh_candidate_projection()
                self._new_discovery_offer = None
                offer = self.queue.offer_recovery if language_offer else self.queue.offer_discovery
                if offer(track["id"]):
                    trace(
                        "discovery_offered", track_id=track["id"], artist=track.get("artist"),
                        genre_hint=choice.genre_hint, language_target=choice.language_target,
                    )
                    self._plan_priorities()
            self._finish_online(request)
            return
        request.track_id = track_id
        request.downloaded = entry.files[matches[0]].get("downloaded", 0)
        request.deadline = time.monotonic() + request.inactivity_limit
        if request.manual:
            self._manual_start = request.label
        self._play_known(track)
        if self._awaiting:
            self._set_status("Скачиваем выбранный трек…")
        else:
            self._finish_online(request)

    def _poll_online(self) -> None:
        request = self._online
        if request is None:
            return
        now = time.monotonic()
        if request.prefetch and request.visible_ends and now >= request.visible_ends:
            self._cancel_online()
            self._finish_online(request, "Время пополнения очереди истекло")
            return
        if request.foreground_ends and now >= request.foreground_ends:
            trace("foreground_lookup_expired", requested=request.label, attempts=request.attempts,
                  budget_seconds=config.FOREGROUND_SEARCH_SECONDS)
            request.last_rejection = "foreground_budget"
            if request.track_id is not None and not request.manual:
                self.queue.skip_waiting(request.track_id)
            self._cancel_online()
            self._finish_online(request, "Время поиска истекло")
            self._plan_priorities()
            return
        if request.future is not None:
            if request.choice is not None and now >= min(request.attempt_ends, request.deadline):
                request.future.cancel()
                request.future = None
                self._reject_online_source(request, "metadata_timeout")
                return
            if not request.future.done():
                return
            future, request.future = request.future, None
            try:
                result = future.result()
            except (RuntimeError, OSError, ValueError) as exc:
                playback_log.info(
                    "lookup_worker_failed stage=%s error_class=%s",
                    "search" if request.choice is None else "metadata", type(exc).__name__,
                )
                if request.choice is None:
                    request.last_rejection = "index_unavailable"
                result = None
            if request.choice is None:
                request.choices.extend(result or ())
                playback_log.info("lookup_result provider=%s candidates=%d", self._provider_label(), len(request.choices))
                trace("request_candidates", requested=request.label, count=len(request.choices))
            else:
                self._open_online_source(request, result)
        if request.entry is not None:
            now = time.monotonic()
            if request.track_id is None and request.entry.files and now < request.attempt_ends:
                try:
                    self._accept_online_source(request)
                except (OSError, RuntimeError, ValueError):
                    self._reject_online_source(request, "source_registration_failed")
                return
            if request.track_id is not None:
                track = self.catalogue.get(request.track_id)
                if track is not None and self.is_ready(track):
                    if request.discovery and not self.queue.is_unheard(track):
                        self._reject_online_source(request, "known_recording")
                        return
                    if request.choice.language_target is not None and language_side(track) != request.choice.language_target:
                        self._reject_online_source(request, "language_mismatch")
                        return
                    if request.visible_ends and request.prefetch:
                        if request.visible_artists & self.queue.visible_artist_keys((track,)):
                            self._reject_online_source(request, "known_artist")
                            return
                        self._finish_online(request)
                        return
                    playback_log.info(
                        "source_audio_ready attempt=%d peers=%d verified_bytes=%d",
                        request.attempts, request.entry.peers, track["size"],
                    )
                    if request.manual:
                        self._manual_start = request.label
                    self._play_known(track)
                    self._finish_online(request)
                    return
                downloaded = request.entry.files[track["file_index"]].get("downloaded", 0) if track else 0
                if downloaded > request.downloaded:
                    request.downloaded = downloaded
                    request.deadline = now + request.inactivity_limit
                    total = request.entry.files[track["file_index"]]["size"]
                    bucket = (10 * downloaded) // max(1, total)
                    if bucket > request.progress_bucket:
                        request.progress_bucket = bucket
                        playback_log.info(
                            "source_progress attempt=%d peers=%d verified_bytes=%d total_bytes=%d",
                            request.attempts, request.entry.peers, downloaded, total,
                        )
            if request.entry.state != "error" and now < min(request.deadline, request.attempt_ends):
                return
            if request.entry.state == "error":
                reason = "torrent_error"
            elif now >= request.attempt_ends:
                reason = "attempt_budget"
            elif request.track_id is None:
                reason = "metadata_timeout"
            else:
                reason = "no_peers" if request.entry.peers == 0 else "no_verified_progress"
            self._reject_online_source(request, reason)
        if not request.choices and request.alternate_language is not None and request.attempts < SOURCE_ATTEMPTS:
            request.choices.append(request.alternate_language)
            request.alternate_language = None
        if not request.choices:
            self._finish_online(request, "Нет доступной раздачи. Нажмите воспроизведение, чтобы попробовать снова.")
            return
        request.choice = request.choices.popleft()
        request.attempts += 1
        request.attempt_started = now
        request.attempt_ends = now + request.attempt_limit
        request.deadline = now + request.inactivity_limit
        playback_log.info(
            "source_attempt provider=%s attempt=%d advertised_seeders=%d budget_seconds=%d inactivity_seconds=%d",
            _diagnostic_provider(request.choice.release), request.attempts, request.choice.release.seeders,
            request.attempt_limit, request.inactivity_limit,
        )
        if request.choice.release.torrent_url:
            request.future = self._executor.submit(self._index_provider().fetch_torrent, request.choice.release)
        else:
            self._open_online_source(request)

    def _maybe_prefetch(self) -> None:
        if not self._online_enabled or self._online:
            return
        if self._maybe_fill_visible() or not self._radio_active:
            return
        cycle = len(self.state.history)
        now = time.monotonic()
        playing = self.is_playing()
        regular_due = (
            playing and self.queue.offered_id() is None
            and cycle != self._last_refill and now >= self._retry_prefetch_at
        )
        target = self._missing_language()
        genre = self._missing_genre(target) if target is not None else None
        if target is None and playing:
            target, genre = self._learned_genre_shortage(cycle)
            if genre is None:
                genre = self._missing_genre(None)
        artist_fallback = self._concentrated_artist_stock() if playing and (target or genre or regular_due) else None
        if target is not None or genre is not None:
            self._lookup_online(prefetch=True, language_target=target, genre_target=genre, artist_fallback=artist_fallback)
            return
        if not regular_due:
            return
        self._last_refill = cycle
        fresh = sum(self.queue.is_unheard(track) for track in self.queue.upcoming)
        if fresh < self.queue.lookahead or artist_fallback is not None:
            self._lookup_online(prefetch=True, artist_fallback=artist_fallback)

    def _maybe_fill_visible(self) -> bool:
        playing = self._radio_active and self.is_playing()
        if not playing and not self._idle_visible_fill:
            return False
        visible = self.queue.visible_artist_shortage()
        if not visible:
            return False
        cycle = len(self.state.history) if playing else None
        if cycle != self._visible_fill_cycle or len(visible) > self._visible_fill_best:
            self._visible_fill_cycle = cycle
            self._visible_fill_best = len(visible)
            self._visible_fill_attempts = 0
        if self._visible_fill_attempts >= 3 or time.monotonic() < self._visible_fill_retry_at:
            return False
        self._visible_fill_attempts += 1
        self._lookup_online(prefetch=True, visible_fill=visible)
        return True

    def _poll_language(self) -> None:
        if self._language_lookup is None or not self._language_lookup[0].done():
            return
        future, track_id, mbid = self._language_lookup
        self._language_lookup = None
        try:
            code = normalize_language_code(future.result())
        except (RuntimeError, ValueError, OSError):
            return
        track = self.catalogue.get(track_id)
        if (not code or track is None or track.get("lang_source") not in {None, "", "title"}
                or track.get("mbid") != mbid):
            return
        track.update(language=_normalize_language(code), language_code=code, lang_source="musicbrainz")
        self.catalogue.save()
        self._refresh_language_feedback(track)
        self._refresh_catalogue()

    def _source_records(self) -> list[dict]:
        return self.state.data.setdefault("torrent_sources", [])

    def _metadata_path(self, info_hash: str) -> Path:
        return Path(self.state.path).resolve().parent / "torrents" / f"{info_hash}.torrent"

    def _save_source(self, entry, record: dict, mark_russian: bool) -> None:
        saved = next((item for item in self._source_records() if item["info_hash"] == entry.info_hash), None)
        if saved is None:
            self._source_records().append({
                **record,
                "info_hash": entry.info_hash,
                "mark_russian": mark_russian,
            })
        else:
            if mark_russian:
                saved["mark_russian"] = True
            if not record.get("source_url"):
                saved["automatic"] = False
                saved.pop("file_index", None)
                saved.pop("file_indices", None)
        self.state.save()
        if mark_russian:
            self._bulk_russian.add(entry.info_hash)
        if entry.files:
            self._on_metadata(entry.info_hash, entry)
        self._set_status(f"Добавлена раздача: {entry.name}")

    def _restore_sources(self) -> None:
        for record in self._source_records():
            try:
                if record["kind"] == "magnet":
                    entry = self.torrents.add_magnet(
                        record["uri"], metadata_path=str(self._metadata_path(record["info_hash"]))
                    )
                else:
                    entry = self.torrents.add_torrent_file(record["path"])
                if record.get("mark_russian"):
                    self._bulk_russian.add(entry.info_hash)
                if entry.files:
                    self._on_metadata(entry.info_hash, entry)
            except (KeyError, ValueError, OSError, RuntimeError):
                self._set_status("Не удалось восстановить одну из раздач")

    def add_magnet(self, uri: str, mark_russian: bool = False) -> str | None:
        if self.torrents is None:
            return "Загрузки недоступны: не установлен libtorrent"
        try:
            entry = self.torrents.add_magnet(uri.strip())
            self._save_source(entry, {"kind": "magnet", "uri": uri.strip()}, mark_russian)
        except (ValueError, OSError, RuntimeError) as exc:
            return f"Не удалось добавить magnet: {exc}"
        return None

    def add_torrent_file(self, path: str, mark_russian: bool = False) -> str | None:
        if self.torrents is None:
            return "Загрузки недоступны: не установлен libtorrent"
        try:
            entry = self.torrents.add_torrent_file(path)
            stored = self._metadata_path(entry.info_hash)
            stored.parent.mkdir(parents=True, exist_ok=True)
            if Path(path).resolve() != stored:
                shutil.copyfile(path, stored)
            self._save_source(entry, {"kind": "file", "path": str(stored)}, mark_russian)
        except (ValueError, OSError, RuntimeError) as exc:
            return f"Не удалось добавить .torrent: {exc}"
        return None

    def _on_metadata(self, info_hash: str, entry) -> None:
        record = next((item for item in self._source_records() if item.get("info_hash") == info_hash), None)
        if record is None:
            return
        stored = self._metadata_path(info_hash)
        is_magnet = record.get("kind") == "magnet"
        if is_magnet:
            try:
                stored.parent.mkdir(parents=True, exist_ok=True)
                stored.write_bytes(self.torrents.export_metadata(entry))
            except (OSError, RuntimeError):
                pass  # The magnet link remains a usable source for the next launch.
        now = time.time()
        mark_russian = info_hash in self._bulk_russian
        genre_hint = record.get("genre_hint", "")
        artist_hint = record.get("artist_hint", "")
        changed = False
        existing_tracks = {track["id"]: track for track in self.catalogue.all() if track.get("info_hash") == info_hash}
        for item in entry.files:
            index = item["index"]
            if record.get("file_index", index) != index:
                continue
            if "file_indices" in record and index not in record["file_indices"]:
                continue
            track_id = f"{info_hash}:{index}"
            existing = existing_tracks.get(track_id)
            if existing is not None:
                if not existing.get("live_recording") and is_live_path(item["path"], record.get("release_title") or entry.name):
                    existing["live_recording"] = True
                    changed = True
                if genre_hint and not existing.get("genre_hint"):
                    existing["genre_hint"] = genre_hint
                    changed = True
                if artist_hint and not existing.get("artist_hint"):
                    existing["artist_hint"] = artist_hint
                    changed = True
                if mark_russian and existing.get("language") == "unknown" and "language" not in existing.get("edited", []):
                    existing.update(language="ru", language_code="ru", lang_source="bulk")
                    changed = True
                continue
            path = entry.file_path(index)
            if path is None or not item["size"] or not is_audio(path):
                continue
            self.catalogue.tracks.append({
                "id": track_id,
                "path": path,
                "artist": entry.name,
                "title": Path(path).stem,
                "genre": "",
                "genre_hint": genre_hint,
                **({"artist_hint": artist_hint} if artist_hint else {}),
                "language": "ru" if mark_russian else "unknown",
                **({"language_code": "ru"} if mark_russian else {}),
                "lang_source": "bulk" if mark_russian else "",
                "mbid": "",
                "duration": 0.0,
                "source": "torrent",
                "size": item["size"],
                "added_at": now,
                "info_hash": info_hash,
                "file_index": index,
                "live_recording": is_live_path(item["path"], record.get("release_title") or entry.name),
            })
            changed = True
        if changed:
            self._save_catalogue()
        self._refresh_catalogue()

    def _on_file_complete(self, info_hash: str, entry, file_index: int) -> None:
        track = self.catalogue.get(f"{info_hash}:{file_index}")
        if track is None or not entry.is_complete(file_index):
            return
        edited = track.get("edited", [])
        try:
            metadata = tags.read_tags(track["path"])
        except OSError:
            metadata = {}
        if metadata.get("genre") and "genre" not in edited:
            if metadata.get("genre_literal"):
                track["genre_literal"] = True
            else:
                track.pop("genre_literal", None)
        _touched, repairs = repair_saved_text(track, available=True, album=entry.name, metadata=metadata)
        if repairs:
            self.state.repair_feedback_text({track["id"]: (track, repairs)})
            self.queue.set_history(self.state.history)
        for key in ("artist", "title", "genre", "mbid"):
            if metadata.get(key) and key not in edited:
                track[key] = metadata[key]
        code = normalize_language_code(metadata.get("language_code") or metadata.get("language"))
        language = _normalize_language(code or metadata.get("language", ""))
        manual_language = track.get("lang_source") in {"manual", "bulk"} or bool({"language", "language_code"} & set(edited))
        if not manual_language and language != "unknown":
            if code:
                track["language_code"] = code
            elif language != track.get("language"):
                track.pop("language_code", None)
            track.update(language=language, lang_source="tag")
            self._refresh_language_feedback(track)
        self._save_catalogue()
        self._refresh_catalogue()

    def _has_torrent(self, track: dict) -> bool:
        if self.torrents is None:
            return False
        entry = self.torrents.get(track.get("info_hash", ""))
        return entry is not None and entry.state != "error"

    def _torrent_usable(self, track: dict) -> bool:
        return self._torrent_hash_usable(track.get("info_hash", ""))

    def _torrent_hash_usable(self, info_hash: str) -> bool:
        if self.torrents is None or self._failed_torrents.get(info_hash, 0.0) > time.monotonic():
            return False
        entry = self.torrents.get(info_hash)
        return entry is not None and entry.state != "error"

    def _plan_priorities(self) -> None:
        if self.torrents is None:
            return
        requested = self.catalogue.get(self._requested) if self._requested else None
        upcoming = [track for track in self.queue.upcoming if requested is None or track["id"] != requested["id"]]
        request = self._online
        if request is not None and request.prefetch and request.visible_ends and request.track_id:
            if all(track["id"] != request.track_id for track in upcoming):
                candidate = self.catalogue.get(request.track_id)
                if candidate is not None:
                    upcoming = [*upcoming[:self.queue.lookahead - 1], candidate]
        candidates = [requested or self.queue.current, *upcoming]
        plan = []
        for position, track in enumerate(candidates):
            if not track or (not self._torrent_usable(track) and track is not requested):
                continue
            plan.append({
                "info_hash": track["info_hash"],
                "file_index": track["file_index"],
                "level": config.PRIORITY_TOP if position <= (0 if candidates[0] else 1) else config.PRIORITY_DEFAULT,
            })
        self.torrents.schedule(plan)

    def _track_pending_wait(self, track_id: str | None) -> None:
        if track_id is None:
            self._pending_wait = None
        elif self._pending_wait is None or self._pending_wait.track_id != track_id:
            now = time.monotonic()
            self._pending_wait = _PendingWait(track_id, now, now, self._pending_bytes(track_id))

    def _pending_bytes(self, track_id: str) -> int:
        track = self.catalogue.get(track_id)
        if track is not None and self._has_torrent(track):
            entry = self.torrents.get(track["info_hash"])
            index = track["file_index"]
            if 0 <= index < len(entry.files):
                return entry.files[index].get("downloaded", 0)
        return 0

    def _expire_pending_wait(self, now: float) -> None:
        wait = self._pending_wait
        if wait is None or self.queue.is_ready(wait.track_id):
            return
        downloaded = self._pending_bytes(wait.track_id)
        if downloaded > wait.downloaded:
            wait.changed, wait.downloaded = now, downloaded
        if now >= wait.started + config.FOREGROUND_FILE_SECONDS:
            reason = "file_budget"
        elif now >= wait.changed + config.FOREGROUND_IDLE_SECONDS:
            reason = "no_verified_progress"
        else:
            return
        if self._requested is not None and self._manual_start is not None:
            trace("foreground_file_expired", requested=self._manual_start, track_id=wait.track_id,
                  reason=reason, elapsed_seconds=round(now - wait.started, 2))
            self._report_manual_failure(self._manual_start, "Не удалось дождаться файла.")
            self._plan_priorities()
        else:
            self._skip_foreground(reason)

    def _watch_pending_tracks(self, now: float) -> None:
        """Let a different release take a slot if its predecessor stalls."""
        request = self._online
        active: dict[str, dict[str, int]] = {}
        candidates = list(self.queue.upcoming) if self._radio_active else []
        explicit = self.catalogue.get(self._requested) if self._requested else None
        if explicit is not None:
            candidates.append(explicit)
        for track in candidates:
            if track.get("source") != "torrent" or track.get("ready") or not self._torrent_usable(track):
                continue
            entry = self.torrents.get(track["info_hash"])
            index = track["file_index"]
            if not 0 <= index < len(entry.files):
                continue
            active.setdefault(entry.info_hash, {})[track["id"]] = entry.files[index].get("downloaded", 0)

        tracked = {}
        starts = {}
        health_changed = False
        for info_hash, downloaded in active.items():
            previous, expires = self._pending_progress.get(info_hash, ({}, now + METADATA_WAIT_SECONDS))
            if any(size > previous.get(track_id, 0) for track_id, size in downloaded.items()):
                expires = now + METADATA_WAIT_SECONDS
            started = self._pending_started.get(info_hash, now)
            expires = min(expires, started + SOURCE_WAIT_SECONDS)
            foreground = request is not None and request.track_id in downloaded
            if now < expires or foreground:
                tracked[info_hash] = (downloaded, expires)
                starts[info_hash] = started
                continue
            self._failed_torrents[info_hash] = now + FAILED_RETRY_SECONDS
            health_changed = True
            entry = self.torrents.get(info_hash)
            playback_log.info(
                "queued_source_stalled scheduled_tracks=%d peers=%d verified_bytes=%d",
                len(downloaded), entry.peers, sum(downloaded.values()),
            )
        self._pending_progress = tracked
        self._pending_started = starts
        if health_changed:
            self._refresh_catalogue()

    def _prune_cache(self) -> None:
        if self.torrents is None:
            return
        current = self.queue.current
        request = self._online
        pinned = {self._requested}
        if self._starting is not None:
            pinned.add(self._starting["id"])
        resolving_hash = None
        if request is not None:
            if request.track_id is not None:
                pinned.add(request.track_id)
            elif request.entry is not None:
                resolving_hash = request.entry.info_hash
        history = self.state.history
        current_id = current["id"] if current is not None else None
        upcoming = tuple(track["id"] for track in self.queue.upcoming)
        signature = (self._catalogue_revision, len(history), current_id, upcoming, frozenset(pinned), resolving_hash)
        if signature != self._prune_signature:
            tracks = self.catalogue.all()
            if resolving_hash is not None:
                pinned.update(track["id"] for track in tracks if track.get("info_hash") == resolving_hash)
            self._prune_candidates = cache_files_to_prune(
                tracks, history, current_id=current_id, upcoming_ids=upcoming, pinned_ids=pinned,
            )
            self._prune_signature = signature
        evicted = self.torrents.prune(self._prune_candidates)
        if evicted:
            playback_log.info("cache_evicted files=%d", len(evicted))
            self._refresh_catalogue()

    def _resume_ready_during_discovery(self) -> None:
        request = self._online
        if (
            not self._awaiting or not self._prefer_visible_ready_on_wait
            or self._awaited_revision == self._catalogue_revision
            or request is None or request.prefetch or request.manual
        ):
            return
        self._awaited_revision = self._catalogue_revision
        ready = self.queue.next_visible_ready(allow_repeats=False)
        if ready is not None:
            self._cancel_online()
            self._begin_playback(ready)

    def tick(self) -> None:
        if self._shutdown_prepared:
            return
        self._refresh_local_changes()
        if self.torrents is not None:
            with self._batch_catalogue_updates():
                self.torrents.poll()
                self.torrents.refresh()
                now = time.monotonic()
                expired = [key for key, until in self._failed_torrents.items() if now >= until]
                for key in expired:
                    del self._failed_torrents[key]
                if any(self._torrent_hash_usable(key) != ready for key, ready in self._torrent_health.items()):
                    self._refresh_catalogue()
            self._watch_pending_tracks(now)
        self._resume_ready_during_discovery()
        self._poll_online()
        self._poll_language()
        if self.is_playing():
            self._confirm_playback()
            self._sample_listening()
        elif self._starting is None:
            self._listening_sample = None
        self._maybe_prefetch()
        self._prune_cache()
        if not self._awaiting:
            return
        if self._online is not None and not self._online.prefetch:
            return
        if self._requested is None:
            if self._awaited_revision != self._catalogue_revision:
                self._advance_queue(prefer_visible_ready=self._prefer_visible_ready_on_wait)
            if self._awaiting:
                self._expire_pending_wait(time.monotonic())
            return
        requested = self.catalogue.get(self._requested)
        if requested is not None and self.is_ready(requested):
            self._play_known(requested)
        elif requested is None or not self._torrent_usable(requested):
            self._requested = None
            self._awaiting = False
            self._radio_active = False
            if self._manual_start is not None:
                self._report_manual_failure(self._manual_start, "Раздающие не передали файл.")
            else:
                self._set_status("Выбранный трек пока недоступен.")
            self._plan_priorities()
        else:
            self._expire_pending_wait(time.monotonic())

    def begin_shutdown(self) -> None:
        """Detach main-loop callbacks and signal cancellation without closing backends."""
        with self._shutdown_lock:
            if self._shutdown_prepared:
                return
            self._shutdown_prepared = True
            request = self._online
            if request is not None:
                request.cancelled.set()
                if request.future is not None:
                    request.future.cancel()
            if self._language_lookup is not None:
                self._language_lookup[0].cancel()
            detach = getattr(self.player, "detach_callbacks", None)
            if detach is not None:
                try:
                    detach()
                except Exception as error:
                    playback_log.error("cleanup_failed stage=player_callbacks error_class=%s", type(error).__name__)

    def quit(self) -> None:
        self.begin_shutdown()
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        started = time.monotonic()
        steps = (
            ("player", getattr(self.player, "shutdown", None)),
            ("lookup", self._cancel_online),
            ("torrents", getattr(self.torrents, "shutdown", None)),
            ("state", self.state.save),
            ("catalogue", self.catalogue.save),
            ("executor", lambda: self._executor.shutdown(wait=True, cancel_futures=True)),
        )
        for stage, action in steps:
            if action is None:
                continue
            try:
                action()
            except Exception as error:
                playback_log.error("cleanup_failed stage=%s error_class=%s", stage, type(error).__name__)
        trace("service_cleanup_finished", elapsed_ms=round((time.monotonic() - started) * 1000))
