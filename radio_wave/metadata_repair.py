"""Repair saved metadata only when a field exhibits known decoder corruption."""

from pathlib import Path

from . import tags

TEXT_FIELDS = ("artist", "title", "genre")


def repair_saved_text(track: dict, *, available: bool, album: str = "", metadata=None):
    """Return the catalogue changes and prior text snapshots for state repair."""
    pending = set(track.get("tag_repair_pending", ()))
    corrupt = tags.is_corrupted_text
    marked = pending | {key for key in TEXT_FIELDS if corrupt(track.get(key) or "")}
    fields = marked.difference(track.get("edited", ()))
    updates = {}
    if fields:
        if available and metadata is None:
            try:
                metadata = tags.read_tags(track["path"])
            except OSError:
                available = False
        metadata = metadata or {}
        for key in fields:
            chosen = metadata.get(key, "") if available else ""
            if not chosen or corrupt(chosen):
                if key == "title":
                    chosen = Path(track.get("path") or "").stem
                else:
                    chosen = album if key == "artist" else ""
                if corrupt(chosen):
                    chosen = ""
            old = track.get(key, "")
            if old != chosen:
                track[key] = chosen
                updates[key] = (old, chosen)
    remainder = set() if available else fields
    if remainder:
        track["tag_repair_pending"] = sorted(remainder)
    else:
        track.pop("tag_repair_pending", None)
    return bool(updates or remainder != pending), updates
