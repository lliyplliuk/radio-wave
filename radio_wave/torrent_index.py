"""Read-only music lookup in RuTor and public JacRed Torznab.

RuTor search path and table selectors are verified against the upstream Jackett
definition and the live music category. The index client never opens a swarm.
"""

from base64 import b32decode
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.client import HTTPException
import re
from time import monotonic
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree

from .catalog import normalize


_RUTOR_ORIGINS = ("https://rutor.info", "https://rutor.is")
_JACRED_ORIGIN = "https://jac.black"
_JACRED_HOSTS = frozenset({urlsplit(_JACRED_ORIGIN).hostname})
_TORZNAB_PATH = re.compile(r"/torznab/api/?")
_TORZNAB_ATTR = "{http://torznab.com/schemas/2015/feed}attr"
_INDEX_HOSTS = frozenset(urlsplit(url).hostname for url in _RUTOR_ORIGINS)
_DOWNLOAD_HOSTS = _INDEX_HOSTS | {"d.rutor.info", "d.rutor.is"}
_HTTP_TIMEOUT = 6
_HTML_LIMIT = 2 * 1024 * 1024
_TORRENT_LIMIT = 16 * 1024 * 1024
_ARTISTS = frozenset({
    "ария", "би 2", "дельфин", "земфира", "кино", "король и шут",
    "мельница", "серьга", "сплин",
})
_WORDS = re.compile(r"[^\W_]+")
_TORRENT_PATH = re.compile(r"/download/\d+/?")
_DETAILS_PATH = re.compile(r"/torrent/\d+(?:/.*)?")
_INFOHASH = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})", re.IGNORECASE)
_AUDIO_MARKER = re.compile(
    r"\b(?:mp3|мр3|flac|alac|ape|wav|ogg|opus|m4a|lossless|dts[.\s-]?cd)\b", re.IGNORECASE,
)
_VIDEO_MARKER = re.compile(
    r"\b(?:x26[45]|[xh][ .-]?26[45]|hdtv|mkv|b[dr]rip|dvd[ .-]?rip|tsrip|"
    r"hdr(?:10)?|hevc|av1|(?:480|576|720|1080|2160)[pi]|[48]k)\b",
    re.IGNORECASE,
)


class TorrentIndexError(RuntimeError):
    """The public index is unavailable or returned unexpected data."""


@dataclass(frozen=True)
class TorrentRelease:
    title: str
    magnet: str | None
    torrent_url: str | None
    seeders: int
    source_url: str
    russian_candidate: bool = False
    provider: str = "rutor"


def _path_at_host(url, hosts, path=None):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "https" and parts.hostname in hosts
            and not parts.username and not parts.password
            and parts.port in (443, None)
            and (path is None or path.fullmatch(parts.path))
        ):
            return url
    except ValueError:
        pass
    return None


class _SameProviderRedirect(HTTPRedirectHandler):
    def __init__(self, hosts, path, name):
        self.hosts = hosts
        self.path = path
        self.name = name

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        if not _path_at_host(new_url, self.hosts, self.path):
            raise TorrentIndexError(f"{self.name} перенаправил запрос на неизвестный адрес")
        return super().redirect_request(request, fp, code, msg, headers, new_url)


def _request(url, *, hosts, limit, path=None, name="RuTor"):
    if not _path_at_host(url, hosts, path):
        raise TorrentIndexError(f"Недопустимый адрес {name}")
    opener = build_opener(ProxyHandler({}), _SameProviderRedirect(hosts, path, name))
    request = Request(url, headers={"User-Agent": "RadioWaveCollection/0.2"})
    deadline = monotonic() + _HTTP_TIMEOUT
    try:
        with opener.open(request, timeout=_HTTP_TIMEOUT) as response:
            payload = bytearray()
            while len(payload) <= limit:
                if monotonic() >= deadline:
                    raise TorrentIndexError(f"{name} не ответил вовремя")
                chunk = response.read1(min(65536, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
    except (OSError, HTTPException) as error:
        raise TorrentIndexError(f"{name} недоступен: {error}") from error
    if len(payload) > limit:
        raise TorrentIndexError(f"Слишком большой ответ {name}")
    return bytes(payload)


def _infohash(magnet):
    if not magnet or not magnet.lower().startswith("magnet:?"):
        return None
    try:
        identifiers = parse_qs(urlsplit(magnet).query).get("xt", [])
    except ValueError:
        return None
    digests = set()
    for identifier in identifiers:
        if match := _INFOHASH.fullmatch(identifier):
            digest = match[1]
            digests.add(b32decode(digest.upper()).hex() if len(digest) == 32 else digest.lower())
    return next(iter(digests)) if len(digests) == 1 else None


def _russian_candidate(title):
    words = " ".join(_WORDS.findall(normalize(title)))
    artist = re.split(r"\s[-–—]\s", title, maxsplit=1)[0].split("(", 1)[0]
    artist = " ".join(_WORDS.findall(normalize(artist)))
    return artist in _ARTISTS or bool(re.search(
        r"\bрусск\w*\s+(?:хит\w*|рок\w*|песн\w*|эстрад\w*|шанс\w*|музык\w*|дискотек\w*)\b"
        r"|\brussian (?:hits|rock|songs|music)\b|\bпесен нашего радио\b",
        words,
    ))


@dataclass
class _Row:
    title: list[str] = field(default_factory=list)
    peer_text: list[str] = field(default_factory=list)
    magnet: str | None = None
    torrent: str | None = None
    details: str | None = None


class _ReleaseParser(HTMLParser):
    def __init__(self, origin):
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.releases = []
        self.inside_results = 0
        self.has_results = False
        self.row: _Row | None = None
        self.reading_title = False
        self.reading_seeders = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "div":
            if self.inside_results:
                self.inside_results += 1
            elif attrs.get("id") == "index":
                self.inside_results = 1
                self.has_results = True
        if not self.inside_results:
            return
        if tag == "tr":
            self.row = _Row()
        if self.row is None:
            return
        classes = (attrs.get("class") or "").split()
        if tag == "span" and "green" in classes:
            self.reading_seeders = True
        if tag != "a" or not (href := attrs.get("href")):
            return
        if _infohash(href):
            self.row.magnet = href
            return
        try:
            url = urljoin(self.origin, href)
        except ValueError:
            return
        if "downgif" in classes and _path_at_host(url, _DOWNLOAD_HOSTS, _TORRENT_PATH):
            self.row.torrent = url
        elif _path_at_host(url, _INDEX_HOSTS, _DETAILS_PATH):
            self.row.details = url
            self.reading_title = True

    def handle_data(self, data):
        if self.row is not None:
            if self.reading_title:
                self.row.title.append(data)
            if self.reading_seeders:
                self.row.peer_text.append(data)

    def handle_endtag(self, tag):
        if tag == "div" and self.inside_results:
            self.inside_results -= 1
        if tag == "a":
            self.reading_title = False
        if tag == "span":
            self.reading_seeders = False
        if tag == "tr" and self.row is not None:
            self._emit_row()
            self.row = None
            self.reading_title = self.reading_seeders = False

    def _emit_row(self):
        row = self.row
        if not row or not row.details or not (row.magnet or row.torrent):
            return
        title = " ".join("".join(row.title).split())
        if not title:
            return
        count = "".join(row.peer_text).strip().replace("\xa0", "").replace(" ", "")
        seeders = int(count) if len(count) <= 9 and count.isdecimal() else 0
        self.releases.append(TorrentRelease(
            title, row.magnet, row.torrent, seeders, row.details, _russian_candidate(title),
        ))


def _parse_releases(html, origin):
    parser = _ReleaseParser(origin)
    parser.feed(html)
    parser.close()
    if not parser.has_results:
        raise TorrentIndexError("Ответ RuTor не содержит списка раздач")
    return parser.releases


def _deduplicate(releases):
    """Keep the best seeded candidate for each hash or source URL."""
    hashes, sources = set(), set()
    unique = []
    for release in sorted(releases, key=lambda item: (-item.seeders, not item.torrent_url)):
        digest = _infohash(release.magnet)
        if (digest and digest in hashes) or release.source_url in sources:
            continue
        if digest:
            hashes.add(digest)
        sources.add(release.source_url)
        unique.append(release)
    return unique


def _details_url(item, digest):
    for field in ("guid", "comments"):
        try:
            parts = urlsplit(item.findtext(field) or "")
            if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
                return parts._replace(fragment="").geturl()
        except ValueError:
            continue
    return f"magnet:?xt=urn:btih:{digest}"


def _canonical_magnet(magnet, digest):
    pairs = [("xt", f"urn:btih:{digest}")]
    pairs.extend(
        (key, value) for key, value in parse_qsl(urlsplit(magnet).query, keep_blank_values=True)
        if key.casefold() != "xt"
    )
    return "magnet:?" + urlencode(pairs, safe=":")


def _parse_jacred(payload):
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, LookupError) as error:
        raise TorrentIndexError("Некорректный ответ JacRed") from error
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise TorrentIndexError("Ответ JacRed не содержит списка раздач")
    releases = []
    for item in channel.findall("item"):
        title = " ".join((item.findtext("title") or "").split())
        nodes = item.findall(_TORZNAB_ATTR)
        attrs = {node.get("name"): node.get("value") for node in nodes}
        categories = [node.get("value") for node in nodes if node.get("name") == "category"]
        categories += [node.text for node in item.findall("category")]
        excluded = any(
            category in {"3020", "3030"}
            or (len(category) == 4 and category.startswith("2"))
            for label in categories for category in re.findall(r"\d+", label or "")
        )
        # This public mirror can label films as requested music. Require an
        # actual audio format in the title and reject explicit video markers.
        if (
            excluded or not _AUDIO_MARKER.search(title)
            or _VIDEO_MARKER.search(title)
        ):
            continue
        enclosure = item.find("enclosure")
        magnet = attrs.get("magneturl") or (enclosure.get("url") if enclosure is not None else None)
        digest = _infohash(magnet)
        if not digest:
            continue
        count = attrs.get("seeders") or ""
        seeders = int(count) if len(count) <= 9 and count.isdecimal() else 0
        releases.append(TorrentRelease(
            title, _canonical_magnet(magnet, digest), None, seeders, _details_url(item, digest),
            _russian_candidate(title), "jacred",
        ))
    return releases


def _jacred_search(query):
    parameters = urlencode({"t": "music", "cat": "3000", "q": query})
    payload = _request(
        f"{_JACRED_ORIGIN}/torznab/api?{parameters}", hosts=_JACRED_HOSTS, limit=_HTML_LIMIT,
        path=_TORZNAB_PATH, name="JacRed",
    )
    return _parse_jacred(payload)


def _lookup(path, *, cancelled=None):
    errors = []
    for origin in _RUTOR_ORIGINS:
        if cancelled is not None and cancelled():
            return []
        try:
            payload = _request(f"{origin}{path}", hosts=_INDEX_HOSTS, limit=_HTML_LIMIT)
            return _parse_releases(payload.decode("utf-8"), origin)
        except (TorrentIndexError, UnicodeError) as error:
            errors.append(str(error))
    raise TorrentIndexError(errors[0])


def search_audio(query: str, *, limit: int = 15, cancelled=None) -> list[TorrentRelease]:
    """Merge music candidates by title, allowing either public index to fail."""
    words = _WORDS.findall(query)
    if not words or limit < 1:
        return []
    keywords = " ".join(words)
    releases, errors = [], []
    encoded = quote(keywords, safe="")
    path = f"/search/0/2/100/2/{encoded}/"
    try:
        releases.extend(_lookup(path) if cancelled is None else _lookup(path, cancelled=cancelled))
    except TorrentIndexError as error:
        errors.append(error)
    if cancelled is not None and cancelled():
        return []
    try:
        releases.extend(_jacred_search(keywords))
    except TorrentIndexError as error:
        errors.append(error)
    if cancelled is not None and cancelled():
        return []
    if len(errors) == 2:
        raise errors[0]
    required = set(_WORDS.findall(normalize(query)))
    matches = [
        release for release in releases
        if required <= set(_WORDS.findall(normalize(release.title)))
    ]
    return _deduplicate(matches)[:limit]


def latest_audio(*, limit: int = 15, cancelled=None) -> list[TorrentRelease]:
    """Find active music releases, favoring Russian-oriented titles."""
    if limit < 1:
        return []
    path = "/browse/0/2/0/2"
    releases = _deduplicate(_lookup(path) if cancelled is None else _lookup(path, cancelled=cancelled))
    return sorted(
        (release for release in releases if release.seeders > 0),
        key=lambda release: (not release.russian_candidate, -release.seeders),
    )[:limit]


def fetch_torrent(release: TorrentRelease) -> bytes:
    """Fetch a bounded RuTor .torrent response for validation by libtorrent."""
    if not release.torrent_url:
        raise TorrentIndexError("У раздачи нет файла .torrent")
    payload = _request(
        release.torrent_url, hosts=_DOWNLOAD_HOSTS, limit=_TORRENT_LIMIT, path=_TORRENT_PATH,
    )
    if not payload.startswith(b"d") or not payload.endswith(b"e"):
        raise TorrentIndexError("RuTor вернул данные другого формата вместо .torrent")
    return payload
