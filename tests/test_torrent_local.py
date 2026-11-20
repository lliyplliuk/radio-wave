"""Настоящий локальный BitTorrent-граф: сидер + загрузчик через libtorrent.

Сеть не используется: сидер и загрузчик соединяются напрямую по адресу
127.0.0.1 через параметр peers. Файл синтетический (случайные байты),
третьих записей не касаемся.
"""

import hashlib
import os
import random
import time

import pytest

libtorrent = pytest.importorskip("libtorrent")

from radio_wave import config  # noqa: E402
from radio_wave.torrent import TorrentManager, TorrentEntry, torrent_files  # noqa: E402

def bencode(value) -> bytes:
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return bencode(value.encode("utf-8"))
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        out = b"d"
        for key in sorted(value):
            out += bencode(key) + bencode(value[key])
        return out + b"e"
    raise TypeError(type(value))


def make_single_file_torrent(name: str, payload: bytes, piece_length: int = 16384) -> bytes:
    pieces = b""
    for offset in range(0, len(payload), piece_length):
        pieces += hashlib.sha1(payload[offset:offset + piece_length]).digest()
    info = {
        "name": name,
        "piece length": piece_length,
        "pieces": pieces,
        "length": len(payload),
    }
    return bencode({"info": info})


def make_multi_file_torrent(name: str, payloads, piece_length: int = 16384) -> bytes:
    """Торрент с несколькими файлами (порядок кусков — по порядку файлов)."""
    blob = b"".join(payloads)
    pieces = b""
    for offset in range(0, len(blob), piece_length):
        pieces += hashlib.sha1(blob[offset:offset + piece_length]).digest()
    info = {
        "name": name,
        "piece length": piece_length,
        "pieces": pieces,
        "files": [
            {"length": len(payload), "path": [f"file{index}.bin"]}
            for index, payload in enumerate(payloads)
        ],
    }
    return bencode({"info": info})


def local_settings() -> dict:
    return {
        "listen_interfaces": "127.0.0.1:0",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    }


def wait_for(predicate, timeout=90.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def shutdown(session) -> None:
    for name in ("shutdown", "abort"):
        method = getattr(session, name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            return


def test_local_seeder_to_leecher(tmp_path):
    """Настоящий обмен по TCP между двумя сессиями libtorrent."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    payload = random.Random(0).randbytes(120_000)
    (source_dir / "sample.bin").write_bytes(payload)

    torrent_bytes = make_single_file_torrent("sample.bin", payload)
    info = libtorrent.torrent_info(libtorrent.bdecode(torrent_bytes))

    seeder_session = libtorrent.session(local_settings())
    seeder_params = libtorrent.add_torrent_params()
    seeder_params.ti = info
    seeder_params.save_path = str(source_dir)
    seeder_params.file_priorities = [config.PRIORITY_TOP]
    seeder_params.flags |= libtorrent.torrent_flags.seed_mode
    seeder_session.add_torrent(seeder_params)

    leech_dir = tmp_path / "leech"
    leech_dir.mkdir()
    leech_session = libtorrent.session(local_settings())
    leech_params = libtorrent.add_torrent_params()
    leech_params.ti = info
    leech_params.save_path = str(leech_dir)
    leech_params.file_priorities = [config.PRIORITY_TOP]
    try:
        assert wait_for(lambda: seeder_session.listen_port() > 0), "сидер не открыл локальный порт"
        leech_params.peers = [("127.0.0.1", seeder_session.listen_port())]
        leecher = leech_session.add_torrent(leech_params)
        assert wait_for(lambda: leecher.status().is_seeding), "файл не скачался с локального сидера"
        assert (leech_dir / "sample.bin").read_bytes() == payload
    finally:
        shutdown(seeder_session)
        shutdown(leech_session)


def test_schedule_disables_unrelated_files_and_prioritises_current(tmp_path):
    """Несвязанные файлы отключаются, текущий получает максимальный приоритет."""
    payload = os.urandom(40_000)
    torrent_bytes = make_multi_file_torrent("multi", [payload, payload])
    info = libtorrent.torrent_info(libtorrent.bdecode(torrent_bytes))

    manager = TorrentManager(save_dir=str(tmp_path / "dl"), session=libtorrent.session(local_settings()))
    params = libtorrent.add_torrent_params()
    params.ti = info
    params.save_path = str(tmp_path / "dl")
    handle = manager.session.add_torrent(params)

    hex_hash = str(info.info_hash())
    torrent_entry = TorrentEntry(hex_hash, info.name(), handle)
    torrent_entry.files = [{**item, "priority": 0} for item in torrent_files(info)]
    manager._torrents[hex_hash] = torrent_entry

    manager.schedule([
        {"info_hash": hex_hash, "file_index": 0, "level": config.PRIORITY_TOP},
        {"info_hash": hex_hash, "file_index": 1, "level": config.PRIORITY_DEFAULT},
    ])

    def priorities():
        return list(handle.get_file_priorities())

    assert wait_for(lambda: priorities()[:1] == [config.PRIORITY_TOP]), "приоритет текущего не выставлен"
    assert priorities() == [config.PRIORITY_TOP, config.PRIORITY_DEFAULT]

    # Новый план: второй файл снова отключается.
    manager.schedule([{"info_hash": hex_hash, "file_index": 0, "level": config.PRIORITY_TOP}])
    assert wait_for(lambda: priorities() == [config.PRIORITY_TOP, config.PRIORITY_IGNORE]), \
        "несвязанный файл не отключён"
    manager.shutdown()


def test_verified_progress_waits_for_disk_before_publishing_file(tmp_path):
    data = b"RIFF" + bytes(1020)
    info_hash = "1" * 40
    directory = tmp_path / info_hash
    directory.mkdir()
    file = directory / "track.wav"
    following = directory / "next.wav"
    file.write_bytes(bytes(len(data)))  # The correct file size can be preallocated with holes.
    following.write_bytes(bytes(len(data)))

    class Handle:
        piece_granularity = 1
        flushes = 0
        progress = [len(data), 0]

        def file_progress(self, _granularity):
            return self.progress

        def info_hash(self):
            return info_hash

        def flush_cache(self):
            self.flushes += 1

    class cache_flushed_alert:
        def __init__(self, handle):
            self.handle = handle

    class Session:
        alerts = []

        def pop_alerts(self):
            alerts, self.alerts = self.alerts, []
            return alerts

        def get_settings(self):
            return {"alert_mask": 0}

        def apply_settings(self, _settings):
            pass

    session = Session()
    handle = Handle()
    manager = TorrentManager(save_dir=str(tmp_path), session=session)
    entry = TorrentEntry(info_hash, "album", handle, str(directory))
    entry.files = [
        {"index": index, "path": path.name, "size": len(data)}
        for index, path in enumerate((file, following))
    ]
    manager._torrents[info_hash] = entry

    assert manager.poll() == []
    assert not entry.is_complete(0)
    handle.progress = [len(data), len(data)]
    assert manager.poll() == []
    assert handle.flushes == 1
    file.write_bytes(data)
    session.alerts.append(cache_flushed_alert(handle))
    assert manager.poll() == [(info_hash, entry, 0)]
    assert entry.is_complete(0)
    assert not entry.is_complete(1)
    assert file.read_bytes() == data
    assert handle.flushes == 2
    following.write_bytes(data)
    session.alerts.append(cache_flushed_alert(handle))
    assert manager.poll() == [(info_hash, entry, 1)]
    assert following.read_bytes() == data
