"""Ограниченный журнал воспроизведения и подробная трассировка по запросу."""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from unicodedata import category

from . import config


playback_log = logging.getLogger("radio_wave.playback")
_FILE_HANDLER = "playback-file"
_DEV_HANDLER = "playback-developer"
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,47}")
_SENSITIVE_FIELD = re.compile(r"(?:^|_)(?:token|password|secret|key|auth|uri|url|path|headers?)(?:_|$)")
_URI = re.compile(r"""(?i)(?:[a-z][a-z0-9+.-]*://|magnet:|file:|urn:(?:btih|btmh):|www\.)[^\s"'<>]+""")
_CREDENTIAL = re.compile(
    r"""(?i)\b(?:api[-_ ]?key|access[-_ ]?token|passkey|password|secret|token|authorization|auth)"""
    r"""\s*[:=]\s*(?:bearer\s+|basic\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
_ABSOLUTE_PATH = re.compile(r"""(?<![\w\]])(?:/[^/\s"']|[a-zA-Z]:[\\/])""")


def _safe_text(value: str, limit: int) -> str:
    # Сначала чистим управляющие символы, в том числе переносы и bidi.
    value = "".join(" " if category(char).startswith("C") else char for char in value[:4096])
    value = _URI.sub("[redacted-uri]", value)
    value = _CREDENTIAL.sub("[redacted-credential]", value)
    if path := _ABSOLUTE_PATH.search(value):
        value = value[:path.start()] + "[redacted-path]"
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _safe_text(super().format(record), 1600)


def trace(event: str, **fields: object) -> None:
    """Записать этап обработки в режиме разработки, только со скалярными полями."""
    if not playback_log.isEnabledFor(logging.DEBUG) or not _FIELD.fullmatch(event):
        return
    terms = [event]
    for key, value in fields.items():
        if not _FIELD.fullmatch(key) or _SENSITIVE_FIELD.search(key):
            continue
        if isinstance(value, str):
            terms.append(f"{key}={json.dumps(_safe_text(value, 180), ensure_ascii=False)}")
        elif type(value) in (bool, int, float):
            terms.append(f"{key}={json.dumps(value)}")
    playback_log.debug(" ".join(terms))


def configure_playback_log(*, developer: bool = False) -> None:
    """Писать краткий журнал всегда, а при --dev также выводить трассу в stderr."""
    path = Path(config.state_dir()).absolute() / "playback.log"
    formatter = _SafeFormatter("%(asctime)s %(levelname)s %(message)s")
    playback_log.setLevel(logging.DEBUG if developer else logging.INFO)
    playback_log.propagate = False

    named = {handler.name: handler for handler in playback_log.handlers}
    if developer and _DEV_HANDLER not in named:
        terminal = logging.StreamHandler(sys.stderr)
        terminal.set_name(_DEV_HANDLER)
        terminal.setFormatter(formatter)
        playback_log.addHandler(terminal)
    elif not developer and (terminal := named.get(_DEV_HANDLER)) is not None:
        playback_log.removeHandler(terminal)
        terminal.close()

    file_handler = named.get(_FILE_HANDLER)
    if file_handler is None or file_handler.baseFilename != str(path):
        if file_handler is not None:
            playback_log.removeHandler(file_handler)
            file_handler.close()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=250 * 1024, backupCount=1, encoding="utf-8",
            )
        except OSError as error:
            playback_log.warning("playback_log_unavailable error=%s", type(error).__name__)
        else:
            file_handler.set_name(_FILE_HANDLER)
            file_handler.setFormatter(formatter)
            playback_log.addHandler(file_handler)
    playback_log.info("application_started mode=%s", "dev" if developer else "normal")
