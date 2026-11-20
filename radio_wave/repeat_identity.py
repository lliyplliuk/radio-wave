"""Identity of a song across copies in the catalogue and playback history."""

import re
from unicodedata import normalize
from uuid import UUID

from .tags import is_corrupted_text


_PREFIX = re.compile(r"^\d{1,3}[._-]\s*")
_SEPARATORS = re.compile(r"[\W_]+")
_PLACEHOLDERS = {"unknown", "unknown artist", "untitled", "без названия", "неизвестно", "неизвестный исполнитель"}


def _name_part(value) -> str:
    if not isinstance(value, str) or is_corrupted_text(value):
        return ""
    text = _PREFIX.sub("", normalize("NFKC", value).strip().casefold().replace("ё", "е"))
    text = _SEPARATORS.sub(" ", text).strip()
    return text if text not in _PLACEHOLDERS else ""


def _name(track):
    artist, title = (_name_part(track.get(key)) for key in ("artist", "title"))
    return (artist, title) if artist and title else None


def _mbid(track) -> str:
    value = track.get("mbid")
    if isinstance(value, str):
        try:
            mbid = UUID(value)
            return str(mbid) if mbid.int else ""
        except ValueError:
            pass
    return ""


def recording_aliases(tracks) -> dict:
    """Link a tag-only copy to a recording if its name identifies one MBID."""
    by_name = {}
    for track in tracks:
        name, mbid = _name(track), _mbid(track)
        if name and mbid:
            by_name.setdefault(name, set()).add(mbid)
    return {name: next(iter(mbids)) for name, mbids in by_name.items() if len(mbids) == 1}


def recording_identity(track, aliases) -> tuple[str, ...]:
    name = _name(track)
    mbid = _mbid(track) or aliases.get(name)
    if mbid:
        return ("mbid", mbid)
    if name:
        return ("name", *name)
    return ("file", str(track.get("id", "")))
