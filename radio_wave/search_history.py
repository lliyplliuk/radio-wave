"""Recent submitted queries stored only in the desktop app's state directory."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import config
from .catalog import normalize


_HISTORY_LIMIT = 25
_SUGGESTION_LIMIT = 6


class SearchHistory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path(config.state_dir()) / "search_history.json"
        self._terms = self._load()

    def _load(self) -> list[str]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return []
        if not isinstance(stored, list):
            return []
        unique = {}
        for term in stored:
            if isinstance(term, str) and (key := normalize(term)):
                unique.setdefault(key, term)
        return list(unique.values())[:_HISTORY_LIMIT]

    def remember(self, query: str) -> None:
        query = query.strip()
        key = normalize(query)
        if not key:
            return
        self._terms = [query, *(term for term in self._terms if normalize(term) != key)][:_HISTORY_LIMIT]
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                dir=self.path.parent, mode="w", encoding="utf-8", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self._terms, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def matches(self, query: str) -> list[str]:
        key = normalize(query)
        if not key:
            return []
        prefix, within = [], []
        for term in self._terms:
            candidate = normalize(term)
            if candidate.startswith(key):
                prefix.append(term)
            elif key in candidate:
                within.append(term)
        return (prefix + within)[:_SUGGESTION_LIMIT]

    def recent(self) -> list[str]:
        return self._terms[:_SUGGESTION_LIMIT]
