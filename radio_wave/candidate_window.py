"""Скользящий список будущих записей для «Волны» и коллекции."""

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from itertools import chain, islice
from typing import Any

from .ranker import STRONGLY_DISLIKED_GENRE, language_side, track_genre_family

AUTO_CANDIDATE_LIMIT = 16
_READY_FALLBACK_RESERVE = 4
_RUSSIAN_FRESH_RESERVE = 3
Track = dict[str, Any]


def _interleave_sources(tracks: list[Track]) -> Iterator[Track]:
    groups: dict[str, deque[Track]] = defaultdict(deque)
    for track in tracks:
        source = track.get("info_hash") or track["id"]
        groups[source].append(track)
    active = deque(groups.values())
    while active:
        group = active.popleft()
        yield group.popleft()
        if group:
            active.append(group)


def _ready(track: Track) -> bool:
    return bool(track.get("ready")) and not track.get("unavailable")


def _order_by_taste(tracks: list[Track], score: Callable[[Track], float] | None) -> Iterator[Track]:
    if score is None:
        yield from _interleave_sources(tracks)
        return

    groups: dict[tuple[float, bool], list[Track]] = defaultdict(list)
    for track in tracks:
        groups[score(track), _ready(track)].append(track)
    for priority in sorted(groups, reverse=True):
        yield from _interleave_sources(groups[priority])


def candidate_window(
    tracks: Iterable[Track],
    *,
    is_automatic: Callable[[Track], bool],
    is_unheard: Callable[[Track], bool],
    is_schedulable: Callable[[Track], bool] | None = None,
    taste_score: Callable[[Track], float] | None = None,
    is_genre_suppressed: Callable[[Track], bool] | None = None,
    reserve_russian: bool = False,
    language_reserves: Mapping[str, int] | None = None,
    genre_reserves: Mapping[str, int] | None = None,
    upcoming_ids: Iterable[str] = (),
    pinned_ids: Iterable[str | None] = (),
    limit: int = AUTO_CANDIDATE_LIMIT,
) -> list[Track]:
    """Выбрать витрину записей, оставив полный каталог и историю у вызывающего кода.

    Локальные и явные записи всегда видны. Активная очередь занимает часть
    лимита автоматических кандидатов. Текущий или запрошенный трек может
    остаться видимым сверх этого лимита.
    """
    upcoming = tuple(dict.fromkeys(upcoming_ids))
    upcoming_set = set(upcoming)
    pinned = set(pinned_ids)
    schedulable = is_schedulable if is_schedulable is not None else lambda _track: True
    manual, additional, upcoming_auto = [], [], {}
    tiers: list[list[Track]] = [[], [], [], []]
    less_wanted: list[list[Track]] = [[], [], [], []]
    ratings: dict[str, float] = {}

    def genre_suppressed(track: Track, rating: float | None = None) -> bool:
        if is_genre_suppressed is not None:
            return is_genre_suppressed(track)
        if rating is None:
            rating = taste_score(track) if taste_score is not None else 0.0
        return rating <= STRONGLY_DISLIKED_GENRE

    for track in tracks:
        if track.get("source") != "torrent" or not is_automatic(track):
            manual.append(track)
            continue

        track_id = track.get("id")
        if not track_id:
            continue
        if track_id in pinned:
            additional.append(track)
        elif track_id in upcoming_set:
            upcoming_auto[track_id] = track
        elif schedulable(track):
            ready = _ready(track)
            if ready or not (track.get("broken") or track.get("unavailable")):
                priority = (0 if is_unheard(track) else 2) + (0 if ready else 1)
                rating = taste_score(track) if taste_score is not None else 0.0
                ratings[track_id] = rating
                group = less_wanted if genre_suppressed(track, rating) else tiers
                group[priority].append(track)

    capacity = max(0, limit)
    selected = [upcoming_auto[track_id] for track_id in upcoming if track_id in upcoming_auto][:capacity]
    ready_reserve = min(capacity, _READY_FALLBACK_RESERVE)
    ready_fallbacks = (
        track for track in chain(manual, selected)
        if _ready(track) and track.get("id") not in pinned and schedulable(track)
        and not genre_suppressed(track)
    )
    ready_count = sum(1 for _ in islice(ready_fallbacks, ready_reserve))

    # Если новые записи ещё качаются, оставляем запас готовых, в том числе
    # уже слышанных. Это даёт «Волне» вариант при сбое источника или офлайн.
    selected_ids = {track["id"] for track in selected}
    if taste_score is None and is_genre_suppressed is None:
        ready_tiers = (tiers[0], tiers[2])
        remaining = tiers
        order = None
    else:
        ready_tiers = (tiers[0] + tiers[2], less_wanted[0] + less_wanted[2])
        remaining = [tiers[0] + tiers[1], tiers[2] + tiers[3],
                     less_wanted[0] + less_wanted[1], less_wanted[2] + less_wanted[3]]
        order = (lambda track: ratings[track["id"]]) if taste_score is not None else None
    for tier in ready_tiers:
        for track in _order_by_taste(tier, order):
            if ready_count >= ready_reserve or len(selected) >= capacity:
                break
            if track["id"] not in selected_ids:
                selected.append(track)
                selected_ids.add(track["id"])
                ready_count += 1

    language_targets = language_reserves if language_reserves is not None else ({"ru": _RUSSIAN_FRESH_RESERVE} if reserve_russian else {})
    reserves = (
        (classify, category, target)
        for classify, targets in ((language_side, language_targets), (track_genre_family, genre_reserves or {}))
        for category, target in targets.items()
    )
    for classify, category, target in reserves:
        # Однотипная раздача легко заполняет общий лимит. Оставляем место
        # для свежих песен с обеих сторон языкового баланса.
        if target <= 0 or len(selected) >= capacity:
            continue
        language_in_window = (
            track for track in chain(manual, selected)
            if track.get("id") not in pinned and not (track.get("broken") or track.get("unavailable"))
            and schedulable(track) and is_unheard(track)
            and not genre_suppressed(track)
            and classify(track) == category
        )
        reserved = sum(1 for _ in islice(language_in_window, target))
        slots = min(capacity - len(selected), target - reserved)
        if slots:
            fresh = chain.from_iterable(
                _order_by_taste([
                    track for track in tier if track["id"] not in selected_ids and not track.get("broken")
                    and classify(track) == category
                ], order)
                for tier in (tiers[0], tiers[1])
            )
            for track in islice(fresh, slots):
                selected.append(track)
                selected_ids.add(track["id"])

    # После резерва готовых файлов жанровый вкус может поднять пока ещё
    # скачивающийся трек выше готового, когда оба ещё не звучали.
    for tier in remaining:
        for track in _order_by_taste(tier, order):
            if len(selected) >= capacity:
                return [*manual, *additional, *selected]
            if track["id"] not in selected_ids:
                selected.append(track)
                selected_ids.add(track["id"])

    return [*manual, *additional, *selected]
