"""Очередь радио, независимая от плеера и GUI."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from pathlib import PurePath
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import config
from .language import effective_language
from .ranker import (
    DISCOVERY_GENRES, STRONGLY_DISLIKED_LANGUAGE, LanguageMix, Scored, TasteProfile,
    genre_fit, genre_mix_weight, genre_suppressed, is_russian_known, language_mix, language_side,
    norm, rank, track_genre_family,
)
from .repeat_identity import recording_aliases, recording_identity

Track = Dict[str, Any]
_REPEAT_COOLDOWN = 20
_RELEASE_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_VARIETY_HISTORY = 3
_TASTE_MARGIN = 0.75
_REPEAT_AGE_MARGIN = 1
_LANGUAGE_WINDOW = 12
_GENRE_WINDOW = 12
_ARTIST_WINDOW = 8
_VISIBLE_UPCOMING = 4


def _track_id(track: Track) -> str:
    return str(track.get("id", ""))


def _artist_key(track: Track) -> str:
    artist = norm(track.get("artist"))
    if (
        artist and track.get("source") == "torrent" and track.get("duration") == 0
        and not track.get("mbid") and "artist" not in track.get("edited", ())
        and (path := track.get("path"))
    ):
        filename = PurePath(path)
        if any(artist == norm(part) for part in filename.parts[:-1]):
            if hint := norm(track.get("artist_hint")):
                return hint
            if filename.stem != track.get("title"):
                return artist
            parts = _RELEASE_SEPARATOR.split(artist, maxsplit=1)
            if len(parts) == 2 and all(parts):
                return parts[0]
    return artist


def _verified_artist(track: Track) -> bool:
    artist = norm(track.get("artist"))
    if not artist:
        return False
    if track.get("mbid") or "artist" in track.get("edited", ()):
        return True
    path = track.get("path")
    return bool(track.get("ready") and path and all(artist != norm(part) for part in PurePath(path).parts[:-1]))


def _distinct_artist(track: Track, prefix: Sequence[Track]) -> bool:
    artist = _artist_key(track)
    source = track.get("info_hash")
    return all(
        (not artist or artist != _artist_key(previous))
        and (not source or source != previous.get("info_hash") or (_verified_artist(track) and _verified_artist(previous)))
        for previous in prefix
    )


class RadioQueue:
    """Текущая запись и скользящий запас из нескольких следующих."""

    russian_only: bool = False

    def __init__(
        self,
        lookahead: int = config.QUEUE_LOOKAHEAD,
        profile: Optional[TasteProfile] = None,
        ranker: Optional[Callable[..., List[Scored]]] = None,
        seen_ids: Iterable[str] = (),
        finished_listens: Sequence[Track] = (),
        early_skips: Sequence[Track] = (),
        history: Sequence[Track] = (),
    ) -> None:
        self.lookahead = lookahead
        self.profile = profile or TasteProfile()
        self._ranker = ranker or rank
        self._catalogue: dict[str, Track] = {}
        self._ready: set[str] = set()
        self._broken: set[str] = set()
        self._skipped: set[str] = set()
        self._legacy_seen = {str(track_id) for track_id in seen_ids}
        self._history = list(history)
        self._seen: set[tuple[str, ...]] = set()
        self._recent: dict[tuple[str, ...], int] = {}
        self._identities: dict[str, tuple[str, ...]] = {}
        self._aliases: dict = {}
        self._ranked: list[Scored] = []
        self._recent_languages: list[str] = []
        self._finished_listens = list(finished_listens)
        self._early_skips = list(early_skips)
        self._similar_mbids: set[str] = set()
        self._last_liked: list[Track] = []
        self._last_disliked: list[Track] = []
        self._reasons: dict[str, str] = {}
        self._variety: dict[str, dict[str, str]] = {}
        self._genre_fits: dict[str, float] = {}
        self._genre_suppressed: set[str] = set()
        self._genre_candidates: set[str] = set()
        self._genre_weights: dict[str, float] = {}
        self._genre_weight: Callable[[Track], float] = lambda _track: 1.0
        self._genre_veto: Callable[[Track], bool] = lambda _track: False
        self._pinned: tuple[str, ...] = ()
        self._publication_sources: frozenset[str] = frozenset()
        self._offered_id: Optional[str] = None
        self._recovery_id: Optional[str] = None
        self._recovery_offers: dict[str, str] = {}
        self._language_choices: dict[str, str] = {}
        self._language_mix = LanguageMix(enabled=self.profile.language_weight > 0)
        self.current: Optional[Track] = None
        self.upcoming: List[Track] = []
        self._reindex_history()

    def set_catalogue(self, tracks: Sequence[Track], *, automatic_sources: Collection[str] = ()) -> None:
        self._publication_sources = frozenset(automatic_sources)
        self._catalogue = {_track_id(track): dict(track) for track in tracks if _track_id(track)}
        self._ready = {track_id for track_id, track in self._catalogue.items() if track.get("ready", True)}
        self._broken.intersection_update(self._catalogue)
        self._broken.update(track_id for track_id, track in self._catalogue.items() if track.get("broken"))
        self._ready.difference_update(self._broken)
        self._reindex_history()
        if self.current is not None:
            self.current = self._catalogue.get(_track_id(self.current), self.current)
        self.fill()

    def set_history(self, history: Sequence[Track]) -> None:
        """Загрузить историю от старых прослушиваний к новым."""
        self._history = list(history)
        self._reindex_history()
        self.fill()

    def _reindex_history(self) -> None:
        records = {_track_id(track): track for track in self._history if _track_id(track)}
        records.update(self._catalogue)
        self._aliases = recording_aliases(records.values())
        self._identities = {
            track_id: recording_identity(track, self._aliases)
            for track_id, track in records.items()
        }
        history = [self._identity(track) for track in self._history]
        self._seen = set(history)
        self._seen.update(self._identities.get(track_id, ("file", track_id)) for track_id in self._legacy_seen)
        self._remember_recent(reversed(history))
        self._recent_languages = [effective_language(track)[0] for track in self._history[-_LANGUAGE_WINDOW:]]

    def _remember_recent(self, ordered) -> None:
        distinct = dict.fromkeys(ordered)
        self._recent = {key: index for index, key in enumerate(list(distinct)[:_REPEAT_COOLDOWN])}

    def _identity(self, track: Track) -> tuple[str, ...]:
        return self._identities.get(_track_id(track)) or recording_identity(track, self._aliases)

    def is_unheard(self, track: Track) -> bool:
        return self._identity(track) not in self._seen

    def _repeat_tier(self, track: Track) -> int:
        identity = self._identity(track)
        if identity not in self._seen:
            return 0
        return 2 if identity in self._recent else 1

    def set_ready(self, track_id: str, ready: bool = True) -> None:
        if ready:
            self._ready.add(track_id)
            self._broken.discard(track_id)
        else:
            self._ready.discard(track_id)

    def set_broken(self, track_id: str) -> None:
        if track_id in self._broken:
            return
        self._broken.add(track_id)
        self._ready.discard(track_id)
        if self.current and _track_id(self.current) == track_id:
            self.current = None
        self.upcoming = [track for track in self.upcoming if _track_id(track) != track_id]
        self.fill()

    def clear_broken(self, track_id: str) -> None:
        self._broken.discard(track_id)

    def is_skipped(self, track_id: str) -> bool:
        return track_id in self._skipped

    def skip_waiting(self, track_id: str) -> None:
        self._skipped.add(track_id)
        self.fill()

    def reset_skips(self) -> None:
        if self._skipped:
            self._skipped.clear()
            self.fill()

    def is_ready(self, track_id: str) -> bool:
        return track_id in self._ready

    def ready_ids(self) -> List[str]:
        return sorted(self._ready)

    def set_similar_recordings(self, mbids: Iterable[str]) -> None:
        """Добавить результаты поиска похожих записей к оценке медиатеки."""
        known = {norm(mbid) for mbid in mbids if mbid}
        if known.difference(self._similar_mbids):
            self._similar_mbids.update(known)
            self.rebuild()

    def set_finished_listens(self, finished_listens: Sequence[Track]) -> None:
        """Обновить слабый положительный сигнал от дослушанных записей."""
        self._finished_listens = list(finished_listens)
        self.rebuild()

    def set_early_skips(self, early_skips: Sequence[Track]) -> None:
        """Слабая обратная связь от явного раннего «Следующий»."""
        updated = list(early_skips)
        if self._early_skips != updated:
            self._early_skips = updated
            self.rebuild()

    def pin_visible(self, track_ids: Iterable[str]) -> None:
        """Зафиксировать фактически показанное начало очереди."""
        shown = tuple(track_ids)
        if len(shown) > len(self._pinned) and shown == tuple(
            _track_id(track) for track in self.upcoming[:len(shown)]
        ):
            self._pinned = shown

    def pinned_ids(self) -> tuple[str, ...]:
        return self._pinned

    def offered_id(self) -> Optional[str]:
        return self._offered_id

    def language_reserve_targets(self) -> dict[str, int]:
        return self._language_mix.reserve_targets(max(1, self.lookahead // 2))

    def missing_language(self, *, skip: Iterable[str] = ()) -> str | None:
        """Незаполненная часть запаса с учётом уже запланированных загрузок."""
        targets = self.language_reserve_targets()
        protected = {self._identity(track): self._fresh_language(track) for track in self.upcoming}
        counts = Counter(protected.values())
        excluded = set(skip)
        shortages = {side: (target - counts[side], target) for side, target in targets.items()
                     if side not in excluded and counts[side] < target}
        return max(shortages, key=shortages.__getitem__, default=None)

    def genre_reserve_targets(self, *, language: str | None = None) -> dict[str, int]:
        if not self.profile.genre_weight:
            return {}
        weights = {}
        for family in DISCOVERY_GENRES:
            seed = {"genre": family, "language": "ru" if language == "ru" else "unknown"}
            if not self._genre_veto(seed):
                weights[family] = self._genre_weight(seed)
        if language == "foreign":
            for track in (*self._last_liked, *self._history[-_GENRE_WINDOW:]):
                family = track_genre_family(track)
                if family in DISCOVERY_GENRES and language_side(track) == language and not self._genre_veto(track):
                    weights[family] = max(weights.get(family, 0.0), self._genre_weight(track))
        reserve: Counter[str] = Counter()
        if weights:
            budget = max(1, self.lookahead // 2)
            maximum = budget - 1 if len(weights) > 1 and budget > 1 else budget
            for _ in range(budget):
                family = max((key for key in weights if reserve[key] < maximum), key=lambda key: weights[key] / (reserve[key] + 1))
                reserve[family] += 1
        return dict(reserve)

    def _fresh_genre(self, track: Track) -> str:
        track_id = _track_id(track)
        return track_genre_family(track) if (
            self.is_unheard(track) and self._compatible_recovery(track)
            and (track_id in self._genre_candidates or (
                track_id in self._pinned and all(_track_id(item) != track_id for item in self._last_disliked)
            ))
        ) else ""

    def missing_genre(self, *, language: str | None = None, skip: Iterable[str] = ()) -> str | None:
        """Недостающее семейство в пригодном запасе, включая ожидающие загрузки."""
        targets = self.genre_reserve_targets(language=language)
        excluded = set(skip)
        stock = Counter(self._fresh_genre(track) for track in self.upcoming if language is None or language_side(track) == language)
        heard = Counter(track_genre_family(track) for track in self._history[-_GENRE_WINDOW:])
        shortages = {family: (target - stock[family], -heard[family]) for family, target in targets.items()
                     if family not in excluded and stock[family] < target}
        return max(shortages, key=shortages.__getitem__, default=None)

    def artist_exposure(self) -> Counter[str]:
        return Counter(key for track in (*self._history[-_ARTIST_WINDOW:], *self.upcoming) if (key := _artist_key(track)))

    def visible_artist_shortage(self) -> tuple[Track, ...]:
        if not self._publication_sources:
            return ()
        visible = self.visible_upcoming()
        return tuple(visible) if 0 < len(visible) < _VISIBLE_UPCOMING else ()

    def visible_artist_keys(self, tracks: Iterable[Track]) -> set[str]:
        return {key for track in tracks if (key := _artist_key(track))}

    def concentrated_artists(self) -> list[Track]:
        """Запас одного-двух исполнителей, который почти занял очередь."""
        stock = [(key, track) for track in self.upcoming if (key := _artist_key(track)) and self.is_unheard(track)]
        groups = Counter(key for key, _track in stock).most_common(2)
        if len(groups) != 2 or sum(count for _key, count in groups) < self.lookahead - 1:
            return []
        artists = {key for key, _count in groups}
        return [track for key, track in stock if key in artists]

    def recovery_id(self) -> Optional[str]:
        return self._recovery_id

    def recovery_ids(self) -> tuple[str, ...]:
        return tuple(self._recovery_offers.values())

    def offer_recovery(self, track_id: str) -> bool:
        """Удержать по одному языковому пополнению каждой стороны."""
        if (side := self._language_choices.get(track_id)) is None or self._recovery_offers.get(side) not in (None, track_id):
            return False
        self._recovery_offers[side] = track_id
        self.fill()
        return self._recovery_offers.get(side) == track_id

    def offer_discovery(self, track_id: str) -> bool:
        """Сохранить одну новую запись для ближайшей незакреплённой позиции."""
        if self._offered_id not in (None, track_id):
            return False
        if track_id not in self._pinned:
            candidate = next((item for item in self._ranked if item.id == track_id and self.is_unheard(item.track)), None)
            if candidate is None or any(self._disfavor(item) < self._disfavor(candidate) for item in self._ranked):
                return False
        self._offered_id = track_id
        self.fill()
        return self._offered_id == track_id

    def _viable_pins(self) -> list[Track]:
        current_identity = self._identity(self.current) if self.current is not None else None
        tracks = [
            track for track_id in self._pinned
            if (track := self._catalogue.get(track_id)) is not None
            and track_id not in self._broken and track_id not in self._skipped
            and not track.get("unavailable")
            and self._identity(track) != current_identity
        ]
        self._pinned = tuple(_track_id(track) for track in tracks)
        return tracks

    def _compatible_recovery(self, track: Track) -> bool:
        return _track_id(track) not in self._genre_suppressed

    def _fresh_language(self, track: Track) -> str | None:
        if not self.is_unheard(track):
            return None
        track_id = _track_id(track)
        if track_id in self._pinned and self._compatible_recovery(track) and all(
            _track_id(item) != track_id for item in self._last_disliked
        ):
            return language_side(track)
        return self._language_choices.get(track_id)

    def _language_options(self) -> dict[str, str]:
        if not self._language_mix.enabled or not self._ranked:
            return {}

        taste_floor = self._base_taste(self._ranked[0]) - _TASTE_MARGIN
        return {
            item.id: side for item in self._ranked
            if (side := language_side(item.track)) is not None and side not in self._language_mix.suppressed
            and not self._strongly_disfavored(item) and self._compatible_recovery(item.track)
            and (item.similar or self._genre_fits[item.id] > 0 or self._base_taste(item) >= taste_floor)
        }

    @staticmethod
    def _base_taste(item: Scored) -> float:
        # Положительный жанровый бонус своей стороны не делает другую сторону непригодной.
        return item.score - item.language_bias - max(0.0, item.genre_preference)

    def _balance_languages(
        self, options: list[Scored], languages: list[str], first: Scored, *, prefer_ready: bool = False,
    ) -> list[Scored]:
        if not self._language_choices or self._repeat_tier(first.track) == 2 or language_side(first.track) is None:
            return options
        recent = [value for value in languages[-(_LANGUAGE_WINDOW - 1):] if value in {"ru", "en", "other"}]
        available = ([item for item in options if item.id in self._ready] or options) if prefer_ready else options
        deficit = self._language_mix.russian_share * (len(recent) + 1) - recent.count("ru")
        side = language_side(first.track) if deficit == 0.5 else ("ru" if deficit > 0.5 else "foreign")
        if side in self._language_mix.suppressed:
            side = "foreign" if side == "ru" else "ru"
        return [item for item in available if self._language_choices.get(item.id) == side] or options

    def _eligible(self, pinned: Sequence[Track]) -> list[Scored]:
        current_identity = self._identity(self.current) if self.current is not None else None
        reserved = {self._identity(track) for track in pinned}
        reserved.add(current_identity)
        candidates = [
            track for track_id, track in self._catalogue.items()
            if self._identity(track) not in reserved and track_id not in self._broken and track_id not in self._skipped
            and not track.get("unavailable")
        ]
        options = {
            "liked": self._last_liked,
            "disliked": self._last_disliked,
            "recent_ids": [_track_id(track) for track in candidates if self._identity(track) in self._recent],
            "russian_only": self.russian_only,
            "profile": self.profile,
        }
        if self._similar_mbids:
            options["similar_mbids"] = self._similar_mbids
        if self._finished_listens:
            options["finished_listens"] = self._finished_listens
        if self._early_skips:
            options["early_skips"] = self._early_skips
        scored = [item for item in self._ranker(candidates, **options) if not item.excluded]
        current_artist = _artist_key(self.current) if self.current else ""
        liked_artists = {norm(item.get("artist")) for item in self._last_liked}
        deprioritized_artist = current_artist if current_artist not in liked_artists else ""
        ordered = sorted(scored, key=lambda item: (
            self._repeat_tier(item.track),
            item.id not in self._ready,
            -self._recent.get(self._identity(item.track), 0),
            bool(deprioritized_artist and _artist_key(item.track) == deprioritized_artist),
        ))
        by_recording = {}
        for item in ordered:
            by_recording.setdefault(self._identity(item.track), item)
        return list(by_recording.values())

    def fill(
        self,
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> None:
        """Пополнить запас новыми песнями и сохранить подходящие загрузки."""
        if liked is not None:
            self._last_liked = list(liked)
        if disliked is not None:
            self._last_disliked = list(disliked)

        pinned = self._viable_pins()
        self._ranked = self._eligible(pinned)
        self._variety = {item.id: self._variety_keys(item.track) for item in self._ranked}
        fit = genre_fit(self._last_liked, self.profile, disliked=self._last_disliked)
        self._genre_weight = genre_mix_weight(self._last_liked, self._last_disliked, self.profile)
        self._genre_veto = genre_suppressed(self._last_liked, self._last_disliked, self.profile, fit=fit)
        relevant = {_track_id(track): track for track in (*pinned, *(item.track for item in self._ranked))}
        self._genre_fits = {track_id: fit(track) for track_id, track in relevant.items()}
        self._genre_suppressed = {track_id for track_id, track in relevant.items() if self._genre_veto(track)}
        self._genre_weights = {item.id: self._genre_weight(item.track) for item in self._ranked}
        floor = self._base_taste(self._ranked[0]) - _TASTE_MARGIN if self._ranked else 0
        self._genre_candidates = {
            item.id for item in self._ranked if self._compatible_recovery(item.track)
            and (item.similar or self._base_taste(item) >= floor or item.track.get("genre_hint"))
        }
        self._language_mix = language_mix(
            self._last_liked, self._last_disliked, self._finished_listens, self._early_skips,
            weight=self.profile.language_weight,
        )
        self._language_choices = self._language_options()
        self._recovery_id = None
        by_id = {item.id: item for item in self._ranked}
        if self._offered_id is not None and self._offered_id not in self._pinned:
            offered = by_id.get(self._offered_id)
            if offered is None or not self.is_unheard(offered.track):
                self._offered_id = None
        keep = [] if pinned else [
            by_id[_track_id(track)] for track in self.upcoming if _track_id(track) in by_id
        ]
        chosen: list[Scored] = []
        chosen_ids: set[str] = set()
        played_languages = self._recent_languages + [effective_language(track)[0] for track in pinned]
        targets = self.language_reserve_targets()
        planned = Counter(self._fresh_language(track) for track in pinned)
        genre_targets = self.genre_reserve_targets()
        planned_genres = Counter(self._fresh_genre(track) for track in pinned)
        scheduled_keys = self._variety_context(pinned)
        while len(pinned) + len(chosen) < self.lookahead:
            remaining = [item for item in self._ranked if item.id not in chosen_ids]
            if not remaining:
                break
            position = len(pinned) + len(chosen)
            visible_position = position < min(_VISIBLE_UPCOMING, self.lookahead - 1)
            recent_languages = played_languages + [
                effective_language(item.track)[0] for item in chosen
            ]
            remaining = self._space_languages_for_playback(
                remaining, recent_languages, preserve_ready=visible_position,
            )
            tier = self._repeat_tier(remaining[0].track)
            remaining = [item for item in remaining if self._repeat_tier(item.track) == tier]
            if visible_position and any(item.id in self._ready for item in remaining):
                ready_sides = {self._language_choices.get(item.id) for item in remaining if item.id in self._ready}
                # В фоновой загрузке можно ждать отсутствующую сторону; видимая очередь продолжит играть готовое.
                remaining = [item for item in remaining if item.id in self._ready
                             or (side := self._language_choices.get(item.id)) is None or side in ready_sides]
            if visible_position:
                verified = [item for item in remaining if item.id in self._ready or not item.track.get("artist_hint")]
                remaining = verified or remaining
            if visible_position and self._publication_sources and (pinned or chosen):
                remaining = self._prefer_unpublished_artist(remaining, (*pinned, *(item.track for item in chosen)))
            remaining_ids = {item.id for item in remaining}
            retained = next((item for item in keep if item.id in remaining_ids), remaining[0])
            offered = by_id.get(self._offered_id) if self._offered_id in remaining_ids else None
            last_hidden = len(pinned) + len(chosen) + 1 == self.lookahead
            reserve_side = None
            if not visible_position:
                available_sides = {self._fresh_language(item.track) for item in remaining}
                reserve_side = max(
                    (side for side in targets if side in available_sides and planned[side] < targets[side]),
                    key=lambda side: targets[side] - planned[side], default=None,
                )
            if offered is not None and last_hidden and (
                all(self._disfavor(offered) <= self._disfavor(item) for item in remaining)
            ):
                selection = offered
            else:
                if reserve_side is None:
                    choices = self._balance_languages(remaining, recent_languages, retained, prefer_ready=visible_position)
                    preferred = self._offered_id
                else:
                    choices = [item for item in remaining if self._fresh_language(item.track) == reserve_side]
                    preferred = self._recovery_offers.get(reserve_side)
                if not visible_position and genre_targets and not (reserve_side and any(item.id == preferred for item in choices)):
                    families = {item.id: self._fresh_genre(item.track) for item in choices}
                    available_genres = set(families.values())
                    needed = max(
                        (family for family in genre_targets if family in available_genres and planned_genres[family] < genre_targets[family]),
                        key=lambda family: genre_targets[family] - planned_genres[family], default=None,
                    )
                    if needed is not None:
                        choices = [item for item in choices if families[item.id] == needed]
                first = retained if retained in choices else choices[0]
                selection = self._diversify(choices, first, scheduled_keys, preferred=preferred)
                if reserve_side is not None and self._recovery_id is None:
                    self._recovery_id = selection.id
            planned[self._fresh_language(selection.track)] += 1
            planned_genres[self._fresh_genre(selection.track)] += 1
            chosen.append(selection)
            chosen_ids.add(selection.id)
            scheduled_keys.append(self._variety[selection.id])

        if (
            self._offered_id is not None and len(pinned) < self.lookahead
            and self._offered_id not in self._pinned and self._offered_id not in chosen_ids
        ):
            # Не удерживаем право на предложение, пока оно вне плана загрузки.
            self._offered_id = None
        self._recovery_offers = {
            side: track_id for side, track_id in self._recovery_offers.items() if track_id in chosen_ids or track_id in self._pinned
        }
        self.upcoming = [dict(track) for track in pinned] + [dict(item.track) for item in chosen]
        current_reason = self.reason(self.current) or "Выбрано из коллекции"
        pinned_reasons = {_track_id(track): self.reason(track) for track in pinned}
        self._reasons = pinned_reasons | {
            item.id: (
                f"Ещё не звучало · {item.reason}" if self.is_unheard(item.track) else item.reason
            )
            for item in chosen
        }
        if self.current is not None:
            self._reasons[_track_id(self.current)] = current_reason

    def _space_languages(self, options: list[Scored], recent_languages: list[str]) -> list[Scored]:
        """После серии дизлайков чередовать язык с другими песнями."""
        alternatives = [
            item for item in options
            if item.language_bias > STRONGLY_DISLIKED_LANGUAGE
            or effective_language(item.track)[0] not in recent_languages[-3:]
        ]
        if len(alternatives) == len(options):
            return options
        all_genres_suppressed = all(self._disfavor(item)[0] for item in options)
        if any(
            effective_language(item.track)[1] == 1
            and self._disfavor(item)[0] == all_genres_suppressed
            for item in alternatives
        ):
            return alternatives
        return options

    def _space_languages_for_playback(
        self, options: list[Scored], recent_languages: list[str], *, preserve_ready: bool,
    ) -> list[Scored]:
        spaced = self._space_languages(options, recent_languages)
        if (
            preserve_ready and spaced is not options and any(item.id in self._language_choices and item.id not in self._ready for item in spaced)
            and not any(item.id in self._ready and self.is_unheard(item.track) for item in spaced)
        ):
            # Фоновое пополнение не прерывает уже возможное воспроизведение.
            return [item for item in options if item.id in self._ready and self.is_unheard(item.track)] or spaced
        return spaced

    def _variety_keys(self, track: Track) -> dict[str, str]:
        language, confidence = effective_language(track)
        values = {
            "artist": _artist_key(track),
            "torrent": norm(track.get("info_hash")),
            "language": language if confidence == 1 and self.profile.language_weight else "",
            "genre": track_genre_family(track) if self.profile.genre_weight else "",
        }
        return {kind: value for kind, value in values.items() if value}

    def _variety_context(self, pinned: Sequence[Track] = ()) -> list[dict[str, str]]:
        recent = [
            self._catalogue.get(_track_id(track), track) for track in self._history[-_GENRE_WINDOW:]
        ]
        if self.current is not None:
            if recent and _track_id(recent[-1]) == _track_id(self.current):
                recent[-1] = self.current
            else:
                recent = [*recent, self.current][-_GENRE_WINDOW:]
        return [self._variety_keys(track) for track in (*recent, *pinned)]

    def _strongly_disfavored(self, item: Scored) -> bool:
        return any(self._disfavor(item))

    def _disfavor(self, item: Scored) -> tuple[bool, bool]:
        return not self._compatible_recovery(item.track), item.language_bias <= STRONGLY_DISLIKED_LANGUAGE

    def _diversify(
        self, options: list[Scored], first: Scored, context: list[dict[str, str]], *, preferred: str | None = None,
    ) -> Scored:
        oldest_repeat = self._repeat_tier(first.track) == 2
        if oldest_repeat:
            ready = first.id in self._ready
            same_readiness = [item for item in options if (item.id in self._ready) == ready]
            ages = {item.id: self._recent[self._identity(item.track)] for item in same_readiness}
            oldest = max(ages.values())
            options = [item for item in same_readiness if ages[item.id] >= oldest - _REPEAT_AGE_MARGIN]
            first = next(item for item in options if ages[item.id] == oldest)
        previous: dict[str, str] = {}
        for keys in context[-_VARIETY_HISTORY:]:
            previous.update(keys)
        used = Counter(pair for keys in context[-_VARIETY_HISTORY:] for pair in keys.items())
        artist_counts = Counter(keys.get("artist") for keys in context[-_ARTIST_WINDOW:] if keys.get("artist"))
        recent_genres = Counter(keys.get("genre") for keys in context[-_GENRE_WINDOW:] if keys.get("genre"))
        last_release = previous.get("torrent")
        space_releases = bool(last_release) and not oldest_repeat and all(
            (item.id in self._ready) == (first.id in self._ready) for item in options
        )
        known_dimensions = tuple((kind, previous[kind]) for kind in ("language", "genre") if kind in previous)
        genres = {}
        first_artist = self._variety[first.id].get("artist")
        same_genre = self._variety[first.id].get("genre")

        def comparable(item: Scored) -> bool:
            if oldest_repeat:
                return item.score + _TASTE_MARGIN >= first.score
            margin = _TASTE_MARGIN
            if artist_counts[first_artist] and self._variety[item.id].get("genre") == same_genre and same_genre:
                margin += min(1.0, self.profile.artist_weight)
            return item.score - max(0.0, item.genre_preference) + margin >= first.score - max(0.0, first.genre_preference)

        if self.profile.genre_weight and not oldest_repeat:
            least_disfavor = min(map(self._disfavor, options))
            for item in options:
                family = self._variety[item.id].get("genre")
                if family and comparable(item) and self._disfavor(item) == least_disfavor:
                    genres[family] = max(genres.get(family, 0.0), self._genre_weights[item.id])
        total_weight = sum(genres.values())
        exposures = sum(recent_genres[family] for family in genres) + 1
        genre_deficit = {family: exposures * weight / total_weight - recent_genres[family] for family, weight in genres.items()} if len(genres) > 1 else {}
        studio = {(self._variety[item.id].get("genre"), item.id in self._ready) for item in options
                  if not item.is_live and comparable(item) and self._disfavor(item) == self._disfavor(first)}

        def priority(item: Scored) -> tuple:
            keys = self._variety[item.id]
            # Поля без подтверждённых значений не разрывают серию языка или жанра.
            observed = (*keys.items(), *((kind, value) for kind, value in known_dimensions if kind not in keys))
            return (
                self._disfavor(item),
                not comparable(item),
                not oldest_repeat and item.is_live and (keys.get("genre"), item.id in self._ready) in studio,
                not oldest_repeat and bool(keys.get("artist") and keys["artist"] == previous.get("artist")),
                -genre_deficit.get(keys.get("genre"), 0.0),
                space_releases and keys.get("torrent") == last_release,
                0 if oldest_repeat else artist_counts[keys.get("artist")],
                sum(previous.get(kind) == value for kind, value in observed),
                sum(used[pair] for pair in observed),
                item.id != preferred,
                first.similar and not item.similar,
                item is not first,
            )

        return min(options, key=priority)

    def _next_ready(self) -> Optional[Track]:
        return next((track for track in self.upcoming if _track_id(track) in self._ready), None)

    def _play_candidates(self, allow_repeats: bool) -> list[Scored]:
        options = self._space_languages_for_playback(self._ranked, self._recent_languages, preserve_ready=True)
        if not options:
            return []
        tier = self._repeat_tier(options[0].track)
        if tier == 2 and not allow_repeats:
            return []
        candidates = [item for item in options if self._repeat_tier(item.track) == tier]
        last = next(iter(self._recent), None)
        alternatives = [item for item in candidates if self._identity(item.track) != last]
        return alternatives or candidates

    def pending_head(self, *, allow_repeats: bool = True) -> Optional[Track]:
        """Вернуть ближайшую ожидаемую загрузку в доступной группе."""
        if self._pinned and (front := self._catalogue.get(self._pinned[0])) is not None:
            if (allow_repeats or self._repeat_tier(front) != 2) and not self.is_ready(self._pinned[0]):
                return front
            return None
        pending = {item.id: item.track for item in self._play_candidates(allow_repeats) if item.id not in self._ready}
        return next((track for track in self.upcoming if _track_id(track) in pending), next(iter(pending.values()), None))

    def has_pending(self, *, allow_repeats: bool = True) -> bool:
        return self.pending_head(allow_repeats=allow_repeats) is not None

    def next_available(self) -> Optional[Track]:
        """Взять первый готовый трек, сохраняя порядок остальных позиций."""
        ready = self._next_ready()
        if ready is None:
            ranked = [item for item in self._ranked if item.id in self._ready]
            if ranked:
                ranked = self._space_languages_for_playback(ranked, self._recent_languages, preserve_ready=True)
                ready = ranked[0].track
        return self._activate(ready) if ready is not None else None

    def next_visible_ready(self, *, allow_repeats: bool) -> Optional[Track]:
        """Воспроизвести подходящую готовую запись из уже показанных позиций."""
        disliked = {_track_id(track) for track in self._last_disliked}
        for track in self.visible_upcoming():
            track_id = _track_id(track)
            if (
                track["state"] == "ready"
                and track_id not in disliked
                and (not self.russian_only or is_russian_known(track))
                and (allow_repeats or self._repeat_tier(track) != 2)
            ):
                return self._activate(self._catalogue[track_id])
        return None

    def _activate(self, track: Track) -> Track:
        identity = self._identity(track)
        self._history.append(dict(track))
        self._seen.add(identity)
        self._remember_recent((identity, *self._recent))
        self._recent_languages = self._recent_languages[-(_LANGUAGE_WINDOW - 1):] + [effective_language(track)[0]]
        self.current = dict(track)
        self.upcoming = [other for other in self.upcoming if self._identity(other) != identity]
        self.fill()
        return self.current

    def start(
        self,
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> Optional[Track]:
        return self.next_track(liked, disliked)

    def next_track(
        self,
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
        *,
        allow_repeats: bool = True,
    ) -> Optional[Track]:
        """Выбрать готовую песню из самой свежей доступной группы."""
        self.fill(liked, disliked)
        if self._pinned:
            front = self.upcoming[0]
            if not self.is_ready(self._pinned[0]) or (not allow_repeats and self._repeat_tier(front) == 2):
                return None
            return self._activate(front)
        ready = [item for item in self._play_candidates(allow_repeats) if item.id in self._ready]
        if not ready:
            return None
        candidates = {item.id: item for item in ready}
        first = next(
            (candidates[_track_id(track)] for track in self.upcoming if _track_id(track) in candidates),
            ready[0],
        )
        choices = self._balance_languages(ready, self._recent_languages, first)
        selected = self._diversify(
            choices, first if first in choices else choices[0], self._variety_context(), preferred=self._offered_id,
        )
        return self._activate(selected.track)

    def select(
        self,
        track_id: str,
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> Optional[Track]:
        """Явный выбор готовой записи из коллекции вне фильтров радио."""
        self.fill(liked, disliked)
        if track_id not in self._ready or track_id not in self._catalogue:
            return None
        return self._activate(self._catalogue[track_id])

    def rebuild(
        self,
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> None:
        """Пересчитать весь запас, сохранив ближайшую готовую запись."""
        if not self._pinned:
            keep = self._next_ready()
            self.upcoming = [keep] if keep is not None else []
        self.fill(liked, disliked)

    def like(
        self,
        track: Optional[Track],
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> None:
        if track is not None:
            self.rebuild(liked, disliked)

    def dislike(
        self,
        track: Optional[Track],
        liked: Optional[Sequence[Track]] = None,
        disliked: Optional[Sequence[Track]] = None,
    ) -> None:
        if track is not None:
            self.rebuild(liked, disliked)

    def reason(self, track: Optional[Track]) -> str:
        return self._reasons.get(_track_id(track), "") if track is not None else ""

    def snapshot(self) -> List[Dict[str, Any]]:
        """Текущая запись и очередь с состояниями для интерфейса."""
        items: list[Track] = []
        if self.current is not None:
            items.append(dict(self.current, state="playing", reason=self.reason(self.current)))
        for track in self.upcoming:
            state = "ready" if self.is_ready(_track_id(track)) else "pending"
            items.append(dict(track, state=state, reason=self.reason(track)))
        return items

    def visible_upcoming(self, *, automatic_sources: Collection[str] | None = None) -> list[Track]:
        """Показать непрерывный префикс с учётом разнообразия автораздач."""
        upcoming = [item for item in self.snapshot() if item["state"] != "playing"][:_VISIBLE_UPCOMING]
        if automatic_sources is None:
            automatic_sources = self._publication_sources
        for index, track in enumerate(upcoming):
            unverified = track.get("artist_hint") and track["state"] != "ready"
            duplicate = track.get("info_hash") in automatic_sources and not _distinct_artist(track, upcoming[:index])
            if index >= len(self._pinned) and (unverified or duplicate):
                return upcoming[:index]
        return upcoming

    def _prefer_unpublished_artist(self, options: list[Scored], prefix: Sequence[Track]) -> list[Scored]:
        ready = any(item.id in self._ready for item in options)
        strongest = min(map(self._disfavor, options))
        distinct = [item for item in options if _distinct_artist(item.track, prefix)
                    and (not ready or item.id in self._ready) and self._disfavor(item) == strongest]
        return distinct or options
