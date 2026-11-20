"""Named recording metadata from MusicBrainz and ListenBrainz.

MusicBrainz search: https://musicbrainz.org/doc/MusicBrainz_API/Search/RecordingSearch
Lyrics languages: https://musicbrainz.org/doc/How_to_Use_Works
ListenBrainz discovery: https://labs.api.listenbrainz.org/similar-recordings
iTunes metadata: https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/UnderstandingSearchResults.html
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
import re
from threading import Lock
from time import monotonic, sleep, time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from .diagnostics import trace
from .language_codes import normalize_language_code
from .tags import normalize_language


_MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording"
_MUSICBRAINZ_WORK = "https://musicbrainz.org/ws/2/work"
_LISTENBRAINZ = "https://labs.api.listenbrainz.org/similar-recordings/json"
_ITUNES = "https://itunes.apple.com/search"
_SIMILARITY_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50"
    "_skip_30_top_n_listeners_1000"
)
_TIMEOUT_SECONDS = 6
_RETRYABLE_HTTP_ERRORS = {408, 500, 502, 503, 504}
_USER_AGENT = "RadioWaveCollection/0.1 (GTK desktop music library)"
_mb_lock = Lock()
_mb_last_request = float("-inf")
_mb_retry_at = float("-inf")


class CatalogError(RuntimeError):
    """A catalogue request failed or returned unexpected data."""

    def __init__(self, message, *, kind="response", status=None, retryable=False, retry_after=0.0):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class CatalogRecording:
    title: str
    artist: str
    mbid: str
    duration_ms: int | None = None
    disambiguation: str = ""
    album_titles: tuple[str, ...] = ()
    require_cached_mbid: bool = False


def normalize(value: str) -> str:
    """Normalize catalogue text for local search and matching."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(value.split())


def _retry_delay(value: str | None) -> float:
    if not value:
        return 0.0
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time())
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _get_json(url: str, parameters: dict):
    request = Request(
        f"{url}?{urlencode(parameters, doseq=True)}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
        raise CatalogError(
            f"HTTP {exc.code}", kind="http", status=exc.code,
            retryable=exc.code in _RETRYABLE_HTTP_ERRORS and not retry_after,
            retry_after=_retry_delay(retry_after),
        ) from exc
    except (URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        raise CatalogError(
            type(reason).__name__, kind="network",
            retryable=isinstance(reason, (TimeoutError, ConnectionError)),
        ) from exc
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc


def _mb_query(parameters: dict, *, url: str = _MUSICBRAINZ):
    global _mb_last_request, _mb_retry_at

    with _mb_lock:
        now = monotonic()
        if now < _mb_retry_at:
            raise CatalogError("MusicBrainz Retry-After", kind="cooldown", status=503)
        wait = _mb_last_request + 1 - now
        if wait > 0:
            sleep(wait)
        _mb_last_request = monotonic()
        try:
            return _get_json(url, parameters)
        except CatalogError as error:
            if error.retry_after:
                _mb_retry_at = monotonic() + error.retry_after
            raise


def _mbid(value) -> str | None:
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _phrase(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _search_query(title: str, artist: str) -> str:
    if artist:
        return f"recording:{_phrase(title)} AND artist:{_phrase(artist)}"
    terms = title.split()
    if len(terms) == 1:
        return f"recording:{_phrase(title)}^4 OR artist:{_phrase(title)}"
    matches = [
        f"(recording:{_phrase(term)} OR artist:{_phrase(term)})"
        for term in terms
    ]
    return f"recording:{_phrase(title)}^4 OR ({' AND '.join(matches)})"


def _parse_musicbrainz(row: dict) -> CatalogRecording | None:
    if row.get("video") is True:
        return None
    title = _clean(row.get("title"))
    mbid = _mbid(row.get("id"))
    credits = row.get("artist-credit")
    if not title or not mbid or not isinstance(credits, list):
        return None
    artist_parts = []
    for credit in credits:
        if not isinstance(credit, dict):
            continue
        fallback = credit.get("artist")
        name = _clean(credit.get("name"))
        if not name and isinstance(fallback, dict):
            name = _clean(fallback.get("name"))
        join_phrase = credit.get("joinphrase", "")
        artist_parts.append(name + (join_phrase if isinstance(join_phrase, str) else ""))
    artist = "".join(artist_parts).strip()
    if not artist:
        return None
    length = row.get("length")
    releases = row.get("releases")
    albums = {}
    if isinstance(releases, list):
        for release in releases:
            album = _clean(release.get("title")) if isinstance(release, dict) else ""
            if album:
                albums.setdefault(normalize(album), album)
    return CatalogRecording(
        title, artist, mbid,
        length if type(length) is int and length > 0 else None,
        _clean(row.get("disambiguation")),
        tuple(albums.values()),
    )


def search_recordings(
    query: str, *, artist: str = "", limit: int = 15
) -> list[CatalogRecording]:
    """Search MusicBrainz by title, free text, or `Artist — title`."""
    title = " ".join(unicodedata.normalize("NFKC", query).split())
    artist = " ".join(unicodedata.normalize("NFKC", artist).split())
    if not artist and re.search(r"\s[—–-]\s", title):
        artist, title = re.split(r"\s[—–-]\s", title, maxsplit=1)
    if not title or limit < 1:
        return []
    parameters = {
        "query": _search_query(title, artist),
        "fmt": "json",
        "limit": min(limit, 100),
    }
    trace("musicbrainz_search_started", query=title, artist=artist)
    for attempt in (1, 2):
        try:
            payload = _mb_query(parameters)
            break
        except CatalogError as error:
            if attempt == 1 and error.retryable:
                trace("musicbrainz_search_retry", query=title, kind=error.kind, http_status=error.status)
                continue
            trace("musicbrainz_search_failed", query=title, kind=error.kind, http_status=error.status)
            raise
    rows = payload.get("recordings") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        trace("musicbrainz_search_failed", query=title, kind="response")
        raise CatalogError("MusicBrainz вернул неожиданный ответ")
    found = {}
    for row in rows:
        if isinstance(row, dict) and (recording := _parse_musicbrainz(row)):
            found.setdefault(recording.mbid, recording)
    recordings = list(found.values())[:limit]
    trace("musicbrainz_search_result", query=title, count=len(recordings), outcome="found" if recordings else "empty")
    return recordings


def _search_itunes(query: str, limit: int) -> list[CatalogRecording]:
    trace("itunes_search_started", query=query)
    try:
        payload = _get_json(_ITUNES, {
            "term": query, "country": "US", "media": "music", "entity": "song",
            "limit": min(max(limit * 2, 20), 50),
        })
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CatalogError("iTunes вернул неожиданный ответ")
    except CatalogError as error:
        trace("itunes_search_failed", query=query, kind=error.kind, http_status=error.status)
        raise

    terms = normalize(query).split()
    found: dict[tuple[str, str], CatalogRecording] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "song":
            continue
        title, artist = _clean(row.get("trackName")), _clean(row.get("artistName"))
        key = normalize(artist), normalize(title)
        if not title or not artist or not all(term in " ".join(key) for term in terms):
            continue
        album = _clean(row.get("collectionName"))
        record = found.get(key)
        if record is None:
            length = row.get("trackTimeMillis")
            found[key] = CatalogRecording(
                title, artist, "", length if type(length) is int and length > 0 else None,
                album_titles=(album,) if album else (),
            )
        elif album and all(normalize(album) != normalize(prior) for prior in record.album_titles):
            found[key] = replace(record, album_titles=(*record.album_titles, album))
    recordings = list(found.values())[:limit]
    trace("itunes_search_result", query=query, count=len(recordings), outcome="found" if recordings else "empty")
    return recordings


def search_picker_recordings(query: str, *, limit: int = 15) -> list[CatalogRecording]:
    """Resolve a picker query using an independent song catalogue when needed."""
    query = " ".join(query.split())
    if not query or limit < 1:
        return []
    try:
        recordings = search_recordings(query, limit=limit)
    except CatalogError as error:
        if error.kind == "http" and error.status not in _RETRYABLE_HTTP_ERRORS | {429}:
            raise
        return _search_itunes(query, limit)
    return recordings or _search_itunes(query, limit)


def recording_language(mbid: str) -> str | None:
    """Return the broad language category of the exact recording."""
    code = recording_language_code(mbid)
    return normalize_language(code) if code else None


def recording_language_code(mbid: str) -> str | None:
    """Return the exact canonical language code of the credited lyrics."""
    identifier = _mbid(mbid)
    return _recording_language(identifier) if identifier else None


@lru_cache(maxsize=256)
def _recording_language(identifier: str) -> str | None:
    recording = _mb_query(
        {"inc": "work-rels", "fmt": "json"}, url=f"{_MUSICBRAINZ}/{identifier}",
    )
    if not isinstance(recording, dict) or _mbid(recording.get("id")) != identifier:
        return None
    relations = recording.get("relations")
    if not isinstance(relations, list):
        return None
    performances = [
        relation for relation in relations
        if isinstance(relation, dict)
        and relation.get("type") == "performance"
        and relation.get("target-type") == "work"
    ]
    if len(performances) != 1:
        return None
    performance, = performances
    attributes = performance.get("attributes") or []
    if any(attribute in ("instrumental", "karaoke", "medley") for attribute in attributes):
        return None
    work = performance.get("work")
    if not isinstance(work, dict) or not (work_id := _mbid(work.get("id"))):
        return None
    if "languages" not in work and "language" not in work:
        work = _mb_query({"fmt": "json"}, url=f"{_MUSICBRAINZ_WORK}/{work_id}")
        if not isinstance(work, dict) or _mbid(work.get("id")) != work_id:
            return None
    languages = work.get("languages", [work.get("language")])
    if not isinstance(languages, list) or len(languages) != 1:
        return None
    return normalize_language_code(languages[0]) or None


@lru_cache(maxsize=32)
def _similar_by_seed(seeds: tuple[str, ...]) -> tuple[CatalogRecording, ...]:
    rows = _get_json(_LISTENBRAINZ, {
        "recording_mbids": seeds,
        "algorithm": _SIMILARITY_ALGORITHM,
    })
    if not isinstance(rows, list):
        raise CatalogError("ListenBrainz вернул неожиданный ответ")
    groups = {seed: deque() for seed in seeds}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mbid = _mbid(row.get("recording_mbid"))
        seed = _mbid(row.get("reference_mbid"))
        title = _clean(row.get("recording_name"))
        artist = _clean(row.get("artist_credit_name"))
        if mbid and mbid not in groups and seed in groups and title and artist:
            groups[seed].append(CatalogRecording(title, artist, mbid))
    found = {}
    active = deque(group for group in groups.values() if group)
    while active:
        group = active.popleft()
        recording = group.popleft()
        found.setdefault(recording.mbid, recording)
        if group:
            active.append(group)
    return tuple(found.values())


def similar_recordings(mbids: Iterable[str], *, limit: int = 20) -> list[CatalogRecording]:
    """Find named neighbours, allowing each supplied seed into the results."""
    seeds = tuple(dict.fromkeys(mbid for value in mbids if (mbid := _mbid(value))))
    if not seeds or limit < 1:
        return []
    return list(_similar_by_seed(seeds)[:limit])
