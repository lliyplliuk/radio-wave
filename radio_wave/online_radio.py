"""Поиск источников и сопоставление записей с дорожками раздачи."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import PurePath

from . import catalog, torrent_index
from .ranker import language_side, track_genre_family
from .recording_version import is_live_context, is_live_path, is_live_track, live_suffix_base, strip_audio_label

SOURCE_ATTEMPTS = 3
METADATA_WAIT_SECONDS = 18
SOURCE_WAIT_SECONDS = 90
FAILED_RETRY_SECONDS = 120

_FOREIGN_ARTISTS = ("Adele", "ABBA", "Daft Punk", "Sade", "Mylène Farmer", "Stromae")
_RUSSIAN_ARTISTS = ("Кино", "Ёлка", "МакSим", "Земфира", "Полина Гагарина", "Звери")
_GENRE_ARTISTS = {
    ("ru", "rock"): ("Кино", "Алиса", "Машина времени", "Сектор Газа", "Сплин", "Агата Кристи"),
    ("foreign", "rock"): ("Nirvana", "Linkin Park", "Iron Maiden", "Dire Straits", "Foo Fighters"),
}
_GENRE_SEARCHES = {
    "ru": {"rock": "Русский рок", "pop": "Русский поп", "chanson": "Русский шансон", "jazz": "Джаз"},
    "foreign": {"rock": "Rock", "pop": "Pop", "chanson": "French chanson", "jazz": "Jazz"},
}
_LEADING_TRACK = re.compile(r"^\s*(?:\d+\s*[-._]\s*)?\d{1,3}[\s._-]+")
_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)
_ARTIST_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_AUDIO_FORMAT = re.compile(r"\b(?:aac|alac|ape|flac|mp3|ogg|opus|wav|wv|lossless|lossy)\b")
_BITRATE = re.compile(r"\b(?:\d+\s*(?:kbps|kbit|кбит)|vbr|cbr)\b")
_VERSION_LABELS = tuple(map(re.compile, (
    r"\b(?:remix|ремикс\w*)\b",
    r"\b(?:acoustic|акустич\w*)\b", r"\b(?:demo|демо)\b", r"\bradio edit\b",
)))
_COPY_VERSION_LABELS = (*_VERSION_LABELS, re.compile(r"\b(?:remaster\w*|ремастер\w*)\b"))


@dataclass(frozen=True)
class OnlineChoice:
    release: object
    recording: catalog.CatalogRecording | None = None
    query: str | None = None
    allow_title_extension: bool = False
    genre_hint: str = ""
    offer: bool = False
    language_target: str | None = None
    allow_live_fallback: bool = False
    artist_hint: str = ""


@dataclass(frozen=True)
class AcquisitionTaste:
    avoided_genres: frozenset[str] = frozenset()
    russian_artists: tuple[str, ...] = ()
    foreign_artists: tuple[str, ...] = ()
    liked_genre_artists: tuple[tuple[str | None, str, str], ...] = ()
    artist_exposure: dict[str, int] = field(default_factory=dict)
    genre_denied: Callable[[dict], bool] | None = None


def acquisition_taste(likes, *, genre_weight: float, avoided_genres=(), genre_denied=None, artist_exposure=None) -> AcquisitionTaste:
    avoided_genres = frozenset(avoided_genres)
    known = [(track_genre_family(track), track) for track in likes]
    liked_genre_artists, russian_artists, foreign_artists = {}, {}, {}
    for family, track in known:
        artist = _LEADING_TRACK.sub("", track.get("artist") or "").strip()
        if artist and family not in avoided_genres and (genre_denied is None or not genre_denied(track)):
            side = language_side(track)
            if genre_weight and family:
                liked_genre_artists.setdefault((side, family, words(artist)), (side, family, artist))
            if side == "ru":
                russian_artists.setdefault(words(artist), artist)
            elif side == "foreign":
                foreign_artists.setdefault(words(artist), artist)
    return AcquisitionTaste(
        avoided_genres=avoided_genres, russian_artists=tuple(russian_artists.values()),
        foreign_artists=tuple(foreign_artists.values()), genre_denied=genre_denied,
        liked_genre_artists=tuple(liked_genre_artists.values()),
        artist_exposure={words(artist): count for artist, count in (artist_exposure or {}).items()},
    )


def words(text: str) -> str:
    return _SEPARATORS.sub(" ", catalog.normalize(text)).strip()


def recording_key(artist: str, title: str) -> tuple[str, str]:
    return words(artist), words(title)


def _contains_words(text: str, pattern: str) -> bool:
    return bool(pattern) and f" {pattern} " in f" {text} "


def title_path_matches(title: str, relative_path: str) -> bool:
    filename = strip_audio_label(_LEADING_TRACK.sub("", PurePath(relative_path).stem))
    parts = _ARTIST_SEPARATOR.split(filename, maxsplit=1)
    return bool(words(title)) and words(title) in {words(filename), words(parts[-1])}


def title_from_audio_path(relative_path: str) -> str:
    """Return the observed title, excluding a track number and optional artist."""
    filename = strip_audio_label(_LEADING_TRACK.sub("", PurePath(relative_path).stem))
    return _ARTIST_SEPARATOR.split(filename, maxsplit=1)[-1].strip()


def studio_first_paths(paths: Iterable[str], release_title: str) -> list[str]:
    return sorted(paths, key=lambda path: is_live_path(path, release_title))


def _live_title_matches(title: str, candidate: str) -> bool:
    base = live_suffix_base(title)
    candidate_base = live_suffix_base(candidate) or candidate
    if base and words(strip_audio_label(title).removeprefix(base)) not in {
        "live", "live version", "live recording", "концерт", "живой звук", "вживую",
    }:
        return False  # A requested venue or broadcast must match verbatim.
    return words(base or title) == words(candidate_base)


def matching_title_paths(title: str, paths: Iterable[str], release_title: str) -> list[str]:
    explicit_live = bool(live_suffix_base(title))
    matches = []
    for path in studio_first_paths(paths, release_title):
        live = is_live_path(path, release_title)
        if explicit_live and not live:
            continue
        filename = strip_audio_label(_LEADING_TRACK.sub("", PurePath(path).stem))
        if title_path_matches(title, path) or (live and any(
            _live_title_matches(title, candidate) for candidate in (filename, title_from_audio_path(path))
        )):
            matches.append(path)
    return matches


def recording_path_matches(
    recording: catalog.CatalogRecording, relative_path: str, release_title: str, *, allow_live_fallback: bool = False,
) -> bool:
    """Require an exact title and evidence of the artist in the file or release."""
    path = PurePath(relative_path)
    filename = strip_audio_label(_LEADING_TRACK.sub("", path.stem))
    artist, title = recording_key(recording.artist, recording.title)
    if not artist or not title:
        return False
    explicit_live = _live_recording(recording)
    live = is_live_path(relative_path, release_title)
    if (explicit_live and not live) or (
        live and recording.mbid and not explicit_live and not allow_live_fallback
    ):
        return False
    version = words(recording.disambiguation)
    evidence = words(f"{relative_path} {release_title}")
    for pattern in _VERSION_LABELS:
        if pattern.search(version) and not pattern.search(evidence):
            return False
    if version and any(year not in evidence.split() for year in re.findall(r"\b(?:19|20)\d{2}\b", version)):
        return False
    venue = re.search(r"\blive\s+(?:at|in|from|on)\s+(.+)", version)
    if venue and not _contains_words(evidence, venue[1]):
        return False
    parts = _ARTIST_SEPARATOR.split(filename, maxsplit=1)
    if len(parts) == 2 and words(parts[0]) == artist and (
        words(parts[1]) == title or (live and _live_title_matches(recording.title, parts[1]))
    ):
        return True
    return (words(filename) == title or (live and _live_title_matches(recording.title, filename))) and (
        _contains_words(words(release_title), artist)
        or _contains_words(words(str(path.parent)), artist)
    )


def _live_recording(recording: catalog.CatalogRecording) -> bool:
    return bool(live_suffix_base(recording.title) or is_live_context(recording.disambiguation))


def cached_recording_matches(recording: catalog.CatalogRecording, track, *, ambiguous=()) -> bool:
    existing_id = track.get("mbid")
    if recording.mbid and existing_id:
        return catalog.normalize(recording.mbid) == catalog.normalize(existing_id)
    if recording.require_cached_mbid or recording_key(recording.artist, recording.title) in ambiguous:
        return False
    live = is_live_track(track)
    explicit_live = _live_recording(recording)
    if live and recording.mbid and not explicit_live:
        return False
    if recording.disambiguation or (explicit_live and not live):
        return False
    title = track.get("title") or ""
    return words(track.get("artist") or "") == words(recording.artist) and (
        words(title) == words(recording.title) or (live and _live_title_matches(recording.title, title))
    )


def cached_recording_order(recording: catalog.CatalogRecording, track) -> tuple[bool, bool]:
    exact_id = bool(recording.mbid and track.get("mbid") == recording.mbid)
    return not exact_id, is_live_track(track) != _live_recording(recording)


def _copy_details(track):
    if isinstance(track, catalog.CatalogRecording):
        track = {
            "artist": track.artist, "title": track.title, "mbid": track.mbid,
            "disambiguation": track.disambiguation,
        }
    key = recording_key(track.get("artist") or "", track.get("title") or "")
    mbid = catalog.normalize(track.get("mbid") or "")
    label = words(track.get("disambiguation") or "")
    path = PurePath(track.get("path") or "")
    evidence = words(f"{track.get('title') or ''} {path.name} {track.get('release_title') or ''} {label}")
    versions = tuple(pattern.pattern for pattern in _COPY_VERSION_LABELS if pattern.search(evidence))
    return key, mbid, is_live_track(track), label, versions


def ambiguous_recording_copies(tracks) -> set[tuple[str, str]]:
    """Find names for which an unlabeled file cannot identify one known recording."""
    identities: dict[tuple[str, str], set[str]] = {}
    for track in tracks:
        if not (track.mbid if isinstance(track, catalog.CatalogRecording) else track.get("mbid")):
            continue
        key, mbid, live, label, versions = _copy_details(track)
        if all(key) and not (live or label or versions):
            identities.setdefault(key, set()).add(mbid)
    return {key for key, mbids in identities.items() if len(mbids) > 1}


def compatible_recording_copies(first, second, *, ambiguous=()) -> bool:
    """Require compatible identity and version before replacing a selected copy."""
    first_key, first_id, first_live, first_label, first_versions = _copy_details(first)
    second_key, second_id, second_live, second_label, second_versions = _copy_details(second)
    if not all(first_key) or first_key != second_key or first_live != second_live:
        return False
    if first_label and second_label and first_label != second_label:
        return False
    if first_id and second_id:
        return first_id == second_id and (
            first_versions == second_versions or not (first_versions and second_versions)
        )
    return first_key not in ambiguous and not (
        first_live or first_label or second_label or first_versions or second_versions
    )


def _unversioned_title_extension(title: str, candidate: str, evidence: str = "") -> bool:
    return title != candidate and _contains_words(candidate, title) and not any(
        pattern.search(f"{candidate} {evidence}") for pattern in _VERSION_LABELS
    )


def matching_recording_paths(
    recording: catalog.CatalogRecording,
    paths: Iterable[str],
    release_title: str,
    *,
    allow_title_extension: bool = False,
    allow_live_fallback: bool = False,
) -> list[tuple[str, catalog.CatalogRecording]]:
    """Предпочесть точное имя, иначе принять одно однозначное расширение названия."""
    paths = studio_first_paths(paths, release_title)
    exact = []
    for path in paths:
        if not recording_path_matches(recording, path, release_title, allow_live_fallback=allow_live_fallback):
            continue
        observed = title_from_audio_path(path)
        identity = recording
        if is_live_path(path, release_title) and not _live_recording(recording) and (
            recording.mbid or words(observed) != words(recording.title)
        ):
            identity = catalog.CatalogRecording(observed, recording.artist, "")
        exact.append((path, identity))
    if exact or not allow_title_extension or recording.mbid or recording.disambiguation:
        return exact

    artist, title = recording_key(recording.artist, recording.title)
    expanded = []
    for path in paths:
        if is_live_path(path, release_title):
            continue
        relative = PurePath(path)
        filename = _LEADING_TRACK.sub("", relative.stem)
        parts = _ARTIST_SEPARATOR.split(filename, maxsplit=1)
        if len(parts) == 2:
            if words(parts[0]) != artist:
                continue
            candidate = parts[1]
        elif _contains_words(words(f"{relative.parent} {release_title}"), artist):
            candidate = filename
        else:
            continue
        if _unversioned_title_extension(title, words(candidate), words(f"{relative.parent} {release_title}")):
            expanded.append((path, catalog.CatalogRecording(candidate, recording.artist, "")))
    return expanded if len(expanded) == 1 else []


def _can_try_release(release, allow_unconfirmed=False):
    provider = getattr(release, "provider", "")
    return release.seeders > 0 or (
        provider == "jacred" and bool(release.magnet)
    ) or (
        allow_unconfirmed and provider == "rutor" and bool(release.magnet or getattr(release, "torrent_url", None))
    )


def _release_family(title: str) -> str:
    return " ".join(_BITRATE.sub(" ", _AUDIO_FORMAT.sub(" ", words(title))).split())


def _version_order(release, explicit_live=False):
    return release.seeders <= 0, is_live_context(release.title) != explicit_live


def _ordered_releases(candidates, explicit_live=False):
    ranked = sorted(candidates.values(), key=lambda item: (
        item[0] >= 2, *_version_order(item[1], explicit_live), item[0], -item[1].seeders,
    ))
    first, variants, families = [], [], set()
    for priority, release in ranked:
        # Only defer format alternatives when their title says nothing about
        # the requested recording or its known albums.
        family = _release_family(release.title)
        if priority >= 2 and family in families:
            variants.append(release)
        else:
            first.append(release)
            if priority >= 2:
                families.add(family)
    return first + variants


def _search_audio(index, query: str, cancelled: Callable[[], bool]):
    if cancelled():
        return []
    if index is torrent_index:
        return index.search_audio(query, cancelled=cancelled)
    releases = index.search_audio(query)
    return [] if cancelled() else releases


def release_choices(
    index, recording: catalog.CatalogRecording, skip_sources: set[str], *, allow_unconfirmed: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[OnlineChoice]:
    """Give recording and verified album hits the limited source slots first."""
    artist, title = recording_key(recording.artist, recording.title)
    albums = {}
    for label in recording.album_titles:
        key = words(label)
        if key and key != title:
            albums.setdefault(key, label)
        if len(albums) == SOURCE_ATTEMPTS - 1:
            break
    candidates = {}
    searched = set()
    search_error = None
    searches_succeeded = 0

    def collect(query: str, query_priority: int) -> None:
        nonlocal search_error, searches_succeeded
        query_key = words(query)
        if cancelled() or not query_key or query_key in searched:
            return
        searched.add(query_key)
        try:
            releases = _search_audio(index, query, cancelled)
        except (OSError, RuntimeError, ValueError) as exc:
            search_error = exc
            return
        searches_succeeded += 1
        for release in releases:
            url = release.source_url
            if url in skip_sources or not _can_try_release(release, allow_unconfirmed):
                continue
            label = words(release.title)
            if _contains_words(label, artist) and _contains_words(label, title):
                priority = 0
            elif _contains_words(label, artist) and any(_contains_words(label, album) for album in albums):
                priority = 1
            else:
                priority = 2 + query_priority + int(release.seeders <= 0) * 3
            previous = candidates.get(url)
            if previous is None or (priority, -release.seeders) < (previous[0], -previous[1].seeders):
                candidates[url] = (priority, release)

    collect(f"{recording.artist} {recording.title}", query_priority=0)
    for title_hint in albums.values():
        collect(f"{recording.artist} {title_hint}", query_priority=1)
    explicit_live = _live_recording(recording)
    if sum(release.seeders > 0 for release in _ordered_releases(candidates, explicit_live)[:SOURCE_ATTEMPTS]) < SOURCE_ATTEMPTS:
        collect(recording.artist, query_priority=2)
    if cancelled():
        return []
    if not searches_succeeded and search_error is not None:
        raise search_error
    return [OnlineChoice(release, recording) for release in _ordered_releases(candidates, explicit_live)[:SOURCE_ATTEMPTS]]


def search_choices(
    index, query: str, skip_sources: set[str], *, cancelled: Callable[[], bool] = lambda: False,
) -> list[OnlineChoice]:
    """Resolve a title by named metadata or by an exact audio filename."""
    if cancelled():
        return []
    parts = _ARTIST_SEPARATOR.split(query.strip(), maxsplit=1)
    if len(parts) == 2 and all(words(part) for part in parts):
        artist, title = parts
        normalized_artist, normalized_title = recording_key(artist, title)
        try:
            named = {
                row.mbid: row for row in catalog.search_recordings(title, artist=artist)
                if row.mbid and words(row.artist) == normalized_artist and (
                    not row.disambiguation or (live_suffix_base(title) and words(row.title) == normalized_title)
                )
            }
        except catalog.CatalogError:
            named = {}
        exact = [row for row in named.values() if words(row.title) == normalized_title]
        extended = [
            row for row in named.values()
            if not _live_recording(row) and _unversioned_title_extension(normalized_title, words(row.title))
        ]
        known = exact or extended
        verified = known[0] if len(known) == 1 else None
        # Не выдаём неоднозначное название из MusicBrainz за точный MBID.
        album_titles = tuple(dict.fromkeys(album for row in exact for album in row.album_titles))
        recording = verified or catalog.CatalogRecording(title, artist, "", album_titles=album_titles)
        choices = release_choices(index, recording, skip_sources, allow_unconfirmed=True, cancelled=cancelled)
        return [
            OnlineChoice(
                choice.release, recording,
                allow_title_extension=verified is None and not exact and len(extended) <= 1,
                allow_live_fallback=verified is not None and not _live_recording(recording),
            )
            for choice in choices
        ]

    try:
        matches = {
            recording.mbid: recording
            for recording in catalog.search_recordings(query)
            if words(recording.title) == words(query) and (not recording.disambiguation or live_suffix_base(query))
        }
    except catalog.CatalogError:
        matches = {}
    if len(matches) == 1:
        choices = release_choices(index, next(iter(matches.values())), skip_sources, cancelled=cancelled)
        if choices:
            return [OnlineChoice(
                choice.release, choice.recording,
                allow_live_fallback=not _live_recording(choice.recording),
            ) for choice in choices]
    seeded = sorted(_search_audio(index, query, cancelled), key=lambda item: (
        *_version_order(item, bool(live_suffix_base(query))), -item.seeders,
    ))
    return [
        OnlineChoice(release, query=query) for release in seeded
        if release.source_url not in skip_sources and _can_try_release(release)
    ][:SOURCE_ATTEMPTS]


_INDEX_GENRE_LABELS = {
    "chanson": ("шансон", "chanson"),
    "rock": ("рок", "rock", "metal", "метал"),
    "pop": ("поп", "pop"),
    "jazz": ("джаз", "jazz"),
}
_INDEX_LANGUAGE_LABELS = {
    "ru": {"русский", "русская", "русское", "русские", "русскоязычный", "russian"},
    "en": {"английский", "английская", "англоязычный", "english"},
    "fr": {"французский", "французская", "франкоязычный", "français", "française", "french"},
}


def _marked_genres(release) -> set[str]:
    parts = _ARTIST_SEPARATOR.split(release.title, maxsplit=1)
    header = words(parts[0])
    evidence = words(parts[1]) if len(parts) == 2 else words(" ".join(re.findall(r"\[([^]]+)\]", release.title)))
    locale_words = set().union(*_INDEX_LANGUAGE_LABELS.values())
    return {
        family for family, labels in _INDEX_GENRE_LABELS.items()
        if any(_contains_words(evidence, label) or header == label or any(
            (header + " ").startswith(f"{prefix} {label} ") for prefix in locale_words
        ) or any((header + " ").startswith(f"{label} {suffix} ")
                 for suffix in ("музыка", "music", "сборник", "compilation")) for label in labels)
    }


def release_genre_hint(release) -> str:
    genres = _marked_genres(release)
    return next(iter(genres)) if len(genres) == 1 else ""


def _release_disfavored(release, taste: AcquisitionTaste) -> bool:
    genres = _marked_genres(release)
    title_words = words(release.title).split()
    markers = set(zip(title_words, title_words[1:]))
    for genre in genres:
        codes = [code for code, aliases in _INDEX_LANGUAGE_LABELS.items() if any(
            (alias, label) in markers for alias in aliases for label in _INDEX_GENRE_LABELS[genre]
        )]
        if len(codes) == 1 and taste.genre_denied is not None:
            if taste.genre_denied({"genre_hint": genre, "language_code": codes[0]}):
                return True
        elif genre in taste.avoided_genres:
            return True
    return False


def _genre_compatible(items, taste, get_release):
    preferred = [item for item in items if not _release_disfavored(get_release(item), taste)]
    return preferred or items


def _starting_choices(
    index, skip_sources: set[str], rotation: int, taste: AcquisitionTaste | None = None, *,
    cancelled: Callable[[], bool] = lambda: False,
    search_alternative: bool = True,
) -> list[OnlineChoice]:
    if cancelled():
        return []
    taste = taste or AcquisitionTaste()
    try:
        latest = index.latest_audio(cancelled=cancelled) if index is torrent_index else index.latest_audio()
        releases = [item for item in latest if item.seeders > 0 and item.source_url not in skip_sources]
    except (OSError, RuntimeError, ValueError):
        releases = []
    familiar_russian = {words(artist) for artist in _RUSSIAN_ARTISTS}
    preferred = [item for item in releases if (getattr(item, "russian_candidate", False)
                 or words(_ARTIST_SEPARATOR.split(item.title, maxsplit=1)[0]) in familiar_russian)]
    unconfirmed = []
    if search_alternative and sum(not _release_disfavored(item, taste) for item in preferred) < SOURCE_ATTEMPTS:
        seen = {item.source_url for item in preferred} | skip_sources
        try:
            artist = _ordered_artists(taste, _RUSSIAN_ARTISTS, rotation)[0]
            candidates = _search_audio(index, artist, cancelled)
        except (OSError, RuntimeError, ValueError):
            candidates = []
        for item in candidates:
            if item.source_url in seen or not _can_try_release(item):
                continue
            seen.add(item.source_url)
            target = preferred if item.seeders > 0 else unconfirmed
            target.append(item)
            if sum(not _release_disfavored(release, taste) for release in preferred) >= SOURCE_ATTEMPTS:
                break
    preferred_urls = {item.source_url for item in preferred}
    fallback = [item for item in releases if item.source_url not in preferred_urls]
    grouped = (preferred, fallback, unconfirmed)
    ordered = [item for group in grouped for item in sorted(group, key=lambda item: _source_order(taste, item))]
    releases = _genre_compatible(ordered, taste, lambda item: item)
    return [] if cancelled() else [OnlineChoice(item) for item in releases[:SOURCE_ATTEMPTS]]


def _source_order(taste: AcquisitionTaste, release) -> tuple:
    exposure = max((count for artist, count in taste.artist_exposure.items()
                    if _release_artist_matches(release.title, artist)), default=0)
    return (*_version_order(release), exposure, -release.seeders)


def _release_artist_matches(title: str, artist_key: str) -> bool:
    parts = _ARTIST_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2:
        return words(parts[0]) == artist_key
    return (words(title) + " ").startswith(artist_key + " ")


def _ordered_artists(taste: AcquisitionTaste, artists: tuple[str, ...], rotation: int = 0) -> list[str]:
    pivot = rotation % len(artists) if artists else 0
    return sorted(artists[pivot:] + artists[:pivot], key=lambda artist: taste.artist_exposure.get(words(artist), 0))


def _artist_release_choices(
    index, query: str, skip_sources: set[str], taste: AcquisitionTaste, *, genre_hint="", language_target=None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[OnlineChoice]:
    try:
        releases = _search_audio(index, query, cancelled)
    except (OSError, RuntimeError, ValueError):
        return []
    name = words(query)
    candidates = [
        release for release in releases
        if release.source_url not in skip_sources and _can_try_release(release)
        and _release_artist_matches(release.title, name)
        and (not genre_hint or not _marked_genres(release) or genre_hint in _marked_genres(release))
    ]
    candidates = [release for release in candidates if not _release_disfavored(release, taste)]
    return [
        OnlineChoice(release, genre_hint=genre_hint, offer=True, language_target=language_target, artist_hint=query)
        for release in sorted(candidates, key=lambda item: _source_order(taste, item))[:SOURCE_ATTEMPTS]
    ]


def visible_artist_choices(index, taste, skip_sources, rotation, cancelled, sides, visible_artists, genres):
    """Try at most two named artists to fill an unpublished position."""
    excluded = {words(artist) for artist in visible_artists}
    primary = []
    backup = []
    for side in sides:
        liked = taste.russian_artists if side == "ru" else taste.foreign_artists
        defaults = _RUSSIAN_ARTISTS if side == "ru" else _FOREIGN_ARTISTS
        rock = _GENRE_ARTISTS.get((side, "rock"), ()) if genres.get(side) == "rock" else ()
        names = tuple(artist for artist in dict.fromkeys((*rock, *liked, *defaults)) if words(artist) not in excluded)
        for position, artist in enumerate(_ordered_artists(taste, names, rotation)[:2]):
            (primary if position == 0 else backup).append((artist, side, "rock" if artist in rock else ""))

    candidates = []
    for artist, side, genre in (primary + backup)[:2]:
        if cancelled():
            return []
        choices = _artist_release_choices(index, artist, skip_sources, taste, genre_hint=genre,
                                          language_target=side, cancelled=cancelled)
        if candidates and choices:
            # Use the existing attempt budget to try a second artist before another release from the first.
            return (candidates[:1] + choices[:1] + candidates[1:] + choices[1:])[:SOURCE_ATTEMPTS]
        if not candidates and choices:
            candidates = choices
            if len(candidates) == 1:
                break
    return candidates


def _genre_choices(index, taste, skip_sources, rotation, cancelled, genre, language, *, foreground):
    side = language or ("ru" if rotation % 2 == 0 else "foreign")
    liked = tuple(artist for known_side, family, artist in taste.liked_genre_artists if known_side == side and family == genre)
    defaults = _GENRE_ARTISTS.get((side, genre), ())
    artists = _ordered_artists(taste, liked, rotation) + _ordered_artists(taste, defaults, rotation)
    artists.sort(key=lambda artist: taste.artist_exposure.get(words(artist), 0))
    if artists:
        choices = _artist_release_choices(
            index, artists[0], skip_sources, taste, genre_hint=genre, language_target=language, cancelled=cancelled,
        )
        if choices or foreground:
            return choices
    try:
        releases = _search_audio(index, _GENRE_SEARCHES[side][genre], cancelled)
    except (OSError, RuntimeError, ValueError):
        return []
    candidates = [release for release in releases if release.source_url not in skip_sources
                  and _can_try_release(release) and release_genre_hint(release) == genre
                  and not _release_disfavored(release, taste)]
    return [OnlineChoice(release, genre_hint=genre, offer=True, language_target=language)
            for release in sorted(candidates, key=lambda item: _source_order(taste, item))[:SOURCE_ATTEMPTS]]


def _language_choices(index, taste, skip_sources, rotation, cancelled, target, *, foreground) -> list[OnlineChoice]:
    liked = taste.russian_artists if target == "ru" else taste.foreign_artists
    defaults = _RUSSIAN_ARTISTS if target == "ru" else _FOREIGN_ARTISTS
    fallback = _ordered_artists(taste, defaults, rotation)[0]
    preferred = _ordered_artists(taste, liked, rotation)
    first = preferred[0] if preferred and taste.artist_exposure.get(words(preferred[0]), 0) <= taste.artist_exposure.get(words(fallback), 0) else fallback
    for query in dict.fromkeys((first, fallback)):
        if cancelled():
            return []
        choices = _artist_release_choices(
            index, query, skip_sources, taste, language_target=target, cancelled=cancelled,
        )
        if choices:
            return choices
        if foreground:
            break  # Leave time in the same foreground budget for normal discovery.
    return []


def _suggested_choices(index, suggestions, played, skip_sources, skip_recordings, taste, cancelled, *, target=None):
    for recording in suggestions:
        if cancelled():
            return []
        key = recording_key(recording.artist, recording.title)
        if recording.mbid in skip_recordings or key in played:
            continue
        if target is not None and language_side({"title": recording.title}) != target:
            continue
        try:
            choices = release_choices(index, recording, skip_sources, cancelled=cancelled)
        except (OSError, RuntimeError, ValueError):
            choices = []
        compatible = [choice for choice in choices if not _release_disfavored(choice.release, taste)]
        if target is not None:
            return [replace(choice, offer=True, language_target=target) for choice in compatible]
        return compatible  # Only one suggested recording is resolved per refill.
    return []


def radio_choices(
    index, favourites, played, skip_sources, skip_recordings, rotation, cancelled, suggestions=(),
    taste: AcquisitionTaste | None = None, genre_target: str | None = None,
    language_target: str | None = None, skip_language_sources=frozenset(), foreground: bool = False,
    artist_fallback: tuple[str, str] | None = None,
) -> list[OnlineChoice]:
    """Resolve new names from the taste profile, or pick a seeded release."""
    if cancelled():
        return []
    taste = taste or AcquisitionTaste()
    if language_target is not None or genre_target is not None or artist_fallback is not None:
        targeted_skip = set(skip_sources) | skip_language_sources
        choices = []
        rotate_first = (
            not foreground and genre_target is None and artist_fallback is not None
            and artist_fallback[1] == language_target
        )
        if genre_target is not None:
            choices = _genre_choices(index, taste, targeted_skip, rotation, cancelled, genre_target, language_target,
                                     foreground=foreground)
        elif rotate_first:
            artist_genre, artist_side = artist_fallback
            choices = _genre_choices(index, taste, targeted_skip, rotation, cancelled, artist_genre, artist_side,
                                     foreground=True)
        if not choices and language_target is not None and not foreground:
            choices = _suggested_choices(
                index, suggestions, played, targeted_skip, skip_recordings, taste, cancelled, target=language_target,
            )
        if not choices and language_target is not None:
            choices = _language_choices(
                index, taste, targeted_skip, rotation, cancelled, language_target,
                foreground=foreground or genre_target is not None,
            )
        if not choices and artist_fallback is not None and not rotate_first:
            artist_genre, artist_side = artist_fallback
            choices = _genre_choices(index, taste, skip_sources, rotation, cancelled, artist_genre, artist_side,
                                     foreground=True)
        if foreground:
            other = _starting_choices(
                index, set(skip_sources) | {choice.release.source_url for choice in choices},
                rotation, taste, cancelled=cancelled, search_alternative=not choices,
            )
            if choices:
                return choices[:SOURCE_ATTEMPTS - 1] + other[:1]
            if other:
                return other
        elif choices or language_target is not None or genre_target is not None:
            return choices
    seeds = list(dict.fromkeys(
        item["mbid"] for item in favourites if item.get("mbid")
    ))[:SOURCE_ATTEMPTS]
    first = next((item for item in favourites if item.get("artist") and item.get("title")), None)
    if not suggestions and not seeds and first is not None and not cancelled():
        try:
            key = recording_key(first["artist"], first["title"])
            seeds = [
                row.mbid for row in catalog.search_recordings(first["title"], artist=first["artist"], limit=3)
                if recording_key(row.artist, row.title) == key
            ][:1]
        except catalog.CatalogError:
            pass
    if not suggestions and seeds and not cancelled():
        try:
            suggestions = catalog.similar_recordings(seeds)
        except catalog.CatalogError:
            suggestions = []
    choices = _suggested_choices(index, suggestions, played, skip_sources, skip_recordings, taste, cancelled)
    if choices:
        return choices
    return _starting_choices(index, skip_sources, rotation, taste, cancelled=cancelled)
