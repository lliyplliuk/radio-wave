"""Recognize explicitly labeled concert recordings."""

import re
from pathlib import PurePath


_LABEL = (
    r"(?:live(?:\s+(?:(?:at|in|from|on)\s+[^()\[\]]+|version|recording|(?:19|20)\d{2}))?"
    r"|(?:bbc|mtv)\s+live(?:\s+(?:19|20)\d{2})?"
    r"|концерт(?:\b[^()\[\]]*)?|концертн(?:ая запись|ая версия|ое исполнение)"
    r"|запись с концерта|живой звук|вживую)"
)
_WRAPPED = re.compile(rf"\s*[\[(]\s*{_LABEL}\s*[\])]\s*$", re.IGNORECASE)
_SEPARATED = re.compile(rf"\s+[-–—]\s+{_LABEL}\s*$", re.IGNORECASE)
_VENUE_SUFFIX = re.compile(r"(?<=\S)\s+live\s+(?:at|in|from|on)\s+(?!the\s)\S.*$", re.IGNORECASE)
_VERB_SUBJECT = re.compile(r"\b(?:i|we|you|they)\s*$", re.IGNORECASE)
_AUDIO_LABEL = re.compile(
    r"\s*[\[(]\s*(?:aac|alac|ape|flac|mp3|ogg|opus|wav|wv|lossless|lossy)\b[^()\[\]]*[\])]\s*$",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"^(?:(?:(?:bbc|mtv)\s+)?live(?:$|[\s,:]+(?:(?:at|in|from|on)\s+|(?:19|20)\d{2}\b|version\b|recording\b))|"
    r"концерт\b|концертн(?:ая запись|ая версия|ое исполнение)\b|запись с концерта\b|живой звук\b|вживую\b)",
    re.IGNORECASE,
)


def strip_audio_label(title: str) -> str:
    return _AUDIO_LABEL.sub("", title.strip())


def live_suffix_base(title: str) -> str | None:
    """Return the underlying title when the ending labels a live version."""
    title = strip_audio_label(title)
    for pattern in (_WRAPPED, _SEPARATED, _VENUE_SUFFIX):
        match = pattern.search(title)
        if match and match.start() > 0:
            base = title[:match.start()].strip()
            if pattern is not _VENUE_SUFFIX or not _VERB_SUBJECT.search(base):
                return base
    return None


def is_live_context(label: str) -> bool:
    """Release, directory, or disambiguation labels supply extra evidence."""
    label = strip_audio_label(label)
    return bool(_CONTEXT.match(label) or live_suffix_base(label))


def is_live_path(relative_path: str, release_title: str = "") -> bool:
    path = PurePath(relative_path)
    return bool(
        live_suffix_base(path.stem)
        or any(is_live_context(part) for part in path.parts[:-1])
        or is_live_context(release_title)
    )


def is_live_track(track) -> bool:
    path = PurePath(track.get("path") or "")
    parts = path.parts
    info_hash = track.get("info_hash")
    if info_hash and info_hash in parts:
        path = PurePath(*parts[parts.index(info_hash) + 1:])
    elif path.is_absolute():
        path = PurePath(*parts[-2:])
    return bool(
        track.get("live_recording")
        or live_suffix_base(track.get("title") or "")
        or is_live_context(track.get("disambiguation") or "")
        or is_live_path(str(path), track.get("release_title") or "")
    )
