"""Чтение тегов аудиофайлов без внешних зависимостей.

Поддерживаются ID3v1/ID3v2 (mp3) и Vorbis-комментарии (flac/ogg/opus).
Если тегов нет или формат неизвестен — возвращается пустой словарь,
а приложение честно показывает «неизвестно» вместо выдуманных данных.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Dict
from unicodedata import category

from .language_codes import normalize_language_code

_ID3_FIELDS = {
    "TIT2": "title",
    "TT2": "title",
    "TPE1": "artist",
    "TP1": "artist",
    "TCON": "genre",
    "TCO": "genre",
    "TLAN": "language",
    "TLA": "language",
    "TSRC": "isrc",
}

_MBID_KEYS = ("musicbrainz recording id", "musicbrainz_recordingid", "musicbrainz track id")

# ID3v1 codes 0–79 and the Winamp extensions listed in ID3v2.3 appendix A.
_ID3_GENRES = (
    "Blues|Classic Rock|Country|Dance|Disco|Funk|Grunge|Hip-Hop|Jazz|Metal|"
    "New Age|Oldies|Other|Pop|R&B|Rap|Reggae|Rock|Techno|Industrial|"
    "Alternative|Ska|Death Metal|Pranks|Soundtrack|Euro-Techno|Ambient|Trip-Hop|Vocal|Jazz+Funk|"
    "Fusion|Trance|Classical|Instrumental|Acid|House|Game|Sound Clip|Gospel|Noise|"
    "Alternative Rock|Bass|Soul|Punk|Space|Meditative|Instrumental Pop|Instrumental Rock|Ethnic|Gothic|"
    "Darkwave|Techno-Industrial|Electronic|Pop-Folk|Eurodance|Dream|Southern Rock|Comedy|Cult|Gangsta|"
    "Top 40|Christian Rap|Pop/Funk|Jungle|Native American|Cabaret|New Wave|Psychedelic|Rave|Showtunes|"
    "Trailer|Lo-Fi|Tribal|Acid Punk|Acid Jazz|Polka|Retro|Musical|Rock & Roll|Hard Rock|"
    "Folk|Folk-Rock|National Folk|Swing|Fast Fusion|Bebop|Latin|Revival|Celtic|Bluegrass|"
    "Avantgarde|Gothic Rock|Progressive Rock|Psychedelic Rock|Symphonic Rock|Slow Rock|Big Band|Chorus|"
    "Easy Listening|Acoustic|Humour|Speech|Chanson|Opera|Chamber Music|Sonata|Symphony|Booty Bass|Primus|"
    "Porn Groove|Satire|Slow Jam|Club|Tango|Samba|Folklore|Ballad|Power Ballad|Rhythmic Soul|Freestyle|"
    "Duet|Punk Rock|Drum Solo|A cappella|Euro-House|Dance Hall"
).split("|")
_GENRE_CODE = re.compile(r"[0-9]{1,3}")
_GENRE_REFERENCE = re.compile(r"\(([0-9]{1,3}|RX|CR)\)")


def _genre_for_code(code: str) -> str:
    if code in ("RX", "CR"):
        return "Remix" if code == "RX" else "Cover"
    index = int(code)
    return _ID3_GENRES[index] if index < len(_ID3_GENRES) else ""


@lru_cache(maxsize=512)
def normalize_id3_genre(value: str) -> str:
    """Resolve ID3 references, preserving a free-form genre or refinement."""
    label = value.strip()
    if _GENRE_CODE.fullmatch(label):
        return _genre_for_code(label) or label
    if " / " in label:
        parts = label.split(" / ")
        if all(_GENRE_CODE.fullmatch(part) or _GENRE_REFERENCE.fullmatch(part) for part in parts):
            return " / ".join(normalize_id3_genre(part) for part in parts)

    references = []
    offset = 0
    while reference := _GENRE_REFERENCE.match(label, offset):
        references.append(_genre_for_code(reference[1]) or reference[0])
        offset = reference.end()
    tail = label[offset:]
    if tail.startswith("(("):
        tail = tail[1:]
    if tail.strip():
        references = references[:-1] + [tail.strip()]
    return " / ".join(references)


def uses_literal_genre(track: dict) -> bool:
    path = track.get("path") or ""
    return bool(
        track.get("genre_literal") or "genre" in (track.get("edited") or ())
        or path and os.path.splitext(path)[1].lower() != ".mp3"
    )


def read_tags(path: str) -> Dict[str, str]:
    """Вернуть словарь {artist,title,genre,language,mbid,...} или {}."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            tags = _read_id3(path)
        elif ext in (".flac", ".ogg", ".oga", ".opus"):
            tags = _read_vorbis(path)
        elif ext == ".mp4" or ext == ".m4a":
            tags = _read_mp4(path)
        else:
            tags = {}
    except (OSError, ValueError, IndexError):
        tags = {}
    result = {key: value for key, value in tags.items() if value and not is_corrupted_text(value)}
    if raw_language := result.get("language"):
        result["language"] = normalize_language(raw_language)
        if language_code := normalize_language_code(raw_language):
            result["language_code"] = language_code
    return result


def is_corrupted_text(value: str) -> bool:
    """Detect decoded binary/control data that cannot be displayed as a tag."""
    return any(
        character in "\ufffd\ufffe" or category(character) in {"Cc", "Cs"}
        for character in value
    )


# --------------------------------------------------------------------- ID3
def _decode_legacy(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        russian = raw.decode("cp1251")
    except UnicodeDecodeError:
        russian = ""
    # Many Cyrillic encoders wrote cp1251 under the Latin-1 flag. Require
    # a Cyrillic word rather than mistaking a single Western accent for one.
    return russian if re.search(r"\b[а-яё]{2,}\b", russian, re.IGNORECASE) else raw.decode("latin-1")


def _id3_strings(data: bytes) -> list[str]:
    if not data:
        return []
    encoding, raw = data[0], data[1:]
    try:
        if encoding == 0:
            text = _decode_legacy(raw)
        elif encoding == 1 and raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        elif encoding == 2:
            text = raw.decode("utf-16-be")
        elif encoding == 3:
            text = raw.decode("utf-8")
        else:
            return []
    except UnicodeDecodeError:
        return []
    parts = [part.removeprefix("\ufeff").strip() for part in text.split("\x00")]
    return [part if not is_corrupted_text(part) else "" for part in parts]


def _read_id3(path: str) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    with open(path, "rb") as fh:
        header = fh.read(10)
        if header[:3] == b"ID3" and len(header) == 10:
            major = header[3]
            flags = header[5]
            size = _syncsafe(header[6:10])
            body = fh.read(size)
            if major in (2, 3, 4) and not (major == 2 and flags & 0x40):
                if flags & 0x40:
                    if len(body) < 4:
                        body = b""
                    else:
                        ext_size = _syncsafe(body[:4]) if major == 4 else 4 + int.from_bytes(body[:4], "big")
                        body = body[ext_size:]
                tags.update(_parse_id3_frames(body, major))
        # ID3v1 в конце файла
        try:
            fh.seek(-128, os.SEEK_END)
            tail = fh.read(128)
            if len(tail) == 128 and tail[:3] == b"TAG":
                v1 = _parse_id3v1(tail)
                for key, value in v1.items():
                    tags.setdefault(key, value)
        except OSError:
            pass
    return tags


def _parse_id3v1(tail: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, start, length in (("title", 3, 30), ("artist", 33, 30)):
        raw = tail[start:start + length]
        value = raw.split(b"\x00")[0].decode("latin-1", "ignore").strip()
        if value and not is_corrupted_text(value):
            out[key] = value
    if tail[127] < len(_ID3_GENRES):
        out["genre"] = _ID3_GENRES[tail[127]]
    return out


def _syncsafe(raw: bytes) -> int:
    return (raw[0] & 0x7F) << 21 | (raw[1] & 0x7F) << 14 | (raw[2] & 0x7F) << 7 | (raw[3] & 0x7F)


def _parse_id3_frames(body: bytes, major: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    pos = 0
    id_size, header_size = (3, 6) if major == 2 else (4, 10)
    while pos + header_size <= len(body):
        frame_id = body[pos:pos + id_size]
        if not frame_id.strip(b"\x00"):
            break
        try:
            raw_size = body[pos + id_size:pos + 2 * id_size]
            size = _syncsafe(raw_size) if major >= 4 else int.from_bytes(raw_size, "big")
        except IndexError:
            break
        start = pos + header_size
        pos = start + size
        if pos > len(body):
            break
        data = body[start:pos]
        key = _ID3_FIELDS.get(frame_id.decode("latin-1", "ignore"))
        if key and data:
            values = _id3_strings(data)
            if key == "genre":
                values = [normalize_id3_genre(value) for value in values]
            value = " / ".join(part for part in (values if major == 4 else values[:1]) if part)
            if value:
                if key == "genre" and key not in out and normalize_id3_genre(value) != value:
                    out["genre_literal"] = "1"
                out.setdefault(key, value)
        elif frame_id in (b"TXX", b"TXXX") and data:
            fields = _id3_strings(data)
            if len(fields) >= 2 and fields[0].casefold() in _MBID_KEYS and fields[1]:
                out["mbid"] = fields[1]
    return out


# ------------------------------------------------------------------ Vorbis
def _read_vorbis(path: str) -> Dict[str, str]:
    with open(path, "rb") as fh:
        head = fh.read(4)
        if head == b"fLaC":
            return _parse_flac_comments(fh)
        fh.seek(0)
        return _parse_ogg_comments(fh)


def _parse_flac_comments(fh) -> Dict[str, str]:
    out: Dict[str, str] = {}
    while True:
        block = fh.read(4)
        if len(block) < 4:
            break
        is_last = bool(block[0] & 0x80)
        btype = block[0] & 0x7F
        size = int.from_bytes(block[1:4], "big")
        data = fh.read(size)
        if btype == 4:
            out = _parse_vorbis_block(data)
            break
        if is_last:
            break
    return out


def _parse_vorbis_block(data: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        pos = 0
        vendor_len = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4 + vendor_len
        count = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        for _ in range(count):
            length = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            item = data[pos:pos + length].decode("utf-8", "replace")
            pos += length
            key, _, value = item.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "language" and value:
                existing = out.setdefault(key, value)
                if normalize_language_code(existing) != normalize_language_code(value):
                    out[key] = existing + " / " + value
            elif key in ("title", "artist", "genre", "tracknumber"):
                out.setdefault(key, value)
            elif key in _MBID_KEYS:
                out["mbid"] = value
    except (IndexError, ValueError):
        return {}
    return out


def _parse_ogg_comments(fh) -> Dict[str, str]:
    data = fh.read(1 << 20)
    idx = data.find(b"\x03vorbis")
    if idx < 0:
        return {}
    try:
        length = int.from_bytes(data[idx + 7:idx + 11], "little")
        block = data[idx + 11:idx + 11 + length]
        return _parse_vorbis_block(block)
    except (IndexError, ValueError):
        return {}


# --------------------------------------------------------------------- MP4
def _read_mp4(path: str) -> Dict[str, str]:
    """Минимальный разбор атомов MP4/M4A для тегов ilst."""
    out: Dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 22)
    except OSError:
        return out
    idx = data.find(b"ilst")
    if idx < 0:
        return out
    names = {b"\xa9nam": "title", b"\xa9ART": "artist", b"\xa9gen": "genre"}
    for marker, key in names.items():
        pos = data.find(marker, idx)
        if pos < 0:
            continue
        chunk = data[pos + 4:pos + 4 + 256]
        start = chunk.find(b"\x00\x00\x00\x01")
        if start >= 0:
            text = chunk[start + 4:]
            text = text.split(b"\x00")[0]
            value = text.decode("utf-8", "replace").strip()
            if value:
                out.setdefault(key, value)
    return out


def normalize_language(value: str) -> str:
    """Привести явный языковой тег или ручную отметку к категории вкуса."""
    code = str(value or "").strip().casefold()
    if any(separator in code for separator in ("/", ",", ";", "+", "\x00")):
        return "unknown"
    code = code.replace("_", "-").split("-", 1)[0]
    if code in {"ru", "rus", "russian", "русский"}:
        return "ru"
    if code in {"en", "eng", "english", "английский"}:
        return "en"
    if code in {"", "unknown", "und", "zxx", "mis", "mul", "none", "instrumental"}:
        return "unknown"
    return "other"
