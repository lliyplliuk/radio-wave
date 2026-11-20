"""Разбор magnet-ссылок и .torrent-файлов (без сети)."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


def parse_magnet(uri: str) -> Dict[str, Any]:
    """Разобрать magnet-ссылку.

    Возвращает словарь с info_hash (hex), name, trackers. Бросает ValueError,
    если это не magnet или нет info hash.
    """
    if not uri or not uri.strip().lower().startswith("magnet:"):
        raise ValueError("Это не magnet-ссылка")
    query = urlparse(uri.strip()).query
    fields = parse_qs(query)
    info_hash = ""
    for xt in fields.get("xt", []):
        if xt.startswith("urn:btih:"):
            raw = xt[len("urn:btih:"):]
            if len(raw) == 32:
                info_hash = _base32_to_hex(raw).lower()
            elif len(raw) == 40:
                info_hash = raw.lower()
            break
    if not info_hash:
        raise ValueError("В magnet-ссылке нет info hash (urn:btih)")
    name = unquote(fields.get("dn", [""])[0])
    trackers = [unquote(t) for t in fields.get("tr", [])]
    return {"info_hash": info_hash, "name": name, "trackers": trackers, "uri": uri.strip()}


def _base32_to_hex(value: str) -> str:
    import base64
    padded = value.upper() + "=" * (-len(value) % 8)
    try:
        return base64.b32decode(padded).hex()
    except Exception as exc:  # pragma: no cover - защитный код
        raise ValueError(f"Не удалось декодировать info hash: {exc}") from exc


def read_torrent_file(path: str) -> Dict[str, Any]:
    """Прочитать .torrent через libtorrent и вернуть метаданные."""
    import libtorrent as lt

    if not os.path.isfile(path):
        raise ValueError("Файл не найден")
    with open(path, "rb") as fh:
        data = fh.read()
    info = lt.torrent_info(lt.bdecode(data))
    from .torrent import torrent_files
    files = torrent_files(info)
    return {
        "info_hash": str(info.info_hash()),
        "name": info.name(),
        "files": files,
        "total_size": info.total_size(),
        "path": os.path.abspath(path),
    }
