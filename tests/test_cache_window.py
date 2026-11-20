import hashlib
import io
from pathlib import Path
import struct
import time
import wave

import pytest

from radio_wave import torrent
from radio_wave.cache_window import CacheFile, cache_files_to_prune
from radio_wave.torrent import TorrentManager, _remove_owned_audio


HASH = "1" * 40


def record(number, source="torrent", *, info_hash=HASH, path=None):
    return {
        "id": str(number), "source": source, "info_hash": info_hash, "file_index": number,
        "path": path or f"/cache/{info_hash}/{number}.wav",
    }


def test_rolling_window_protects_current_upcoming_recent_and_pins():
    tracks = [record(i) for i in range(12)]
    history = [tracks[i] for i in (0, 1, 2, 3, 4, 5, 6, 7, 8, 8, 9, 10)]
    removed = cache_files_to_prune(
        tracks, history, current_id="10", upcoming_ids=("0", "4", "8", "11"), pinned_ids=("2", "1"),
    )
    assert set(removed) == {CacheFile(HASH, 3), CacheFile(HASH, 5)}


def test_eight_upcoming_slots_do_not_extend_the_four_previous_tracks():
    tracks = [record(i) for i in range(20)]
    removed = cache_files_to_prune(
        tracks, tracks[:12], current_id="11", upcoming_ids=(str(i) for i in range(12, 20)),
    )
    assert set(removed) == {CacheFile(HASH, i) for i in range(7)}


def test_local_import_alias_and_unplayed_alias_protect_physical_track(tmp_path):
    target = tmp_path / "cached.wav"
    target.write_bytes(b"music")
    alias = tmp_path / "local.wav"
    alias.symlink_to(target)
    tracks = [record(0, path=str(target)), *[record(i) for i in range(1, 8)]]
    tracks += [
        record(9, "local", path=str(alias)),
        {**record(1), "id": "unplayed-alias"},
        record(10, "local", path=str(tmp_path / "owned.wav")),
    ]
    history = [*tracks[:8], tracks[10]]
    removed = cache_files_to_prune(tracks, history, current_id=None)
    assert CacheFile(HASH, 0) not in removed
    assert CacheFile(HASH, 1) not in removed
    assert CacheFile(HASH, 10) not in removed
    assert CacheFile(HASH, 2) in removed


def test_safe_unlink_leaves_symlinks_traversal_partial_and_hard_links(tmp_path):
    directory = tmp_path / HASH
    directory.mkdir()
    (tmp_path / "outside.wav").write_bytes(b"music")
    (directory / "symlink.wav").symlink_to(tmp_path / "outside.wav")
    (directory / "folder").symlink_to(tmp_path, target_is_directory=True)
    (directory / "hardlink.wav").hardlink_to(tmp_path / "outside.wav")
    (directory / "partial.wav").write_bytes(b"mu")
    for name in ("symlink.wav", "folder/outside.wav", "../outside.wav", "partial.wav", "hardlink.wav"):
        assert not _remove_owned_audio(str(tmp_path), HASH, name, 5)
    assert (directory / "symlink.wav").is_symlink()
    assert (directory / "hardlink.wav").read_bytes() == b"music"
    assert (directory / "partial.wav").read_bytes() == b"mu"
    assert (tmp_path / "outside.wav").read_bytes() == b"music"


@pytest.mark.parametrize("mount_ids", (
    ("cache", "cache", "different"), ("cache", "cache", ""), ("cache", "substituted"),
))
def test_safe_unlink_leaves_directory_on_another_or_unknown_mount(tmp_path, monkeypatch, mount_ids):
    directory = tmp_path / HASH
    directory.mkdir()
    path = directory / "audio.wav"
    path.write_bytes(b"music")
    ids = iter(mount_ids)
    monkeypatch.setattr(torrent, "_fd_mount_id", lambda _fd: next(ids))
    assert not _remove_owned_audio(str(tmp_path), HASH, "audio.wav", 5)
    assert path.read_bytes() == b"music"


def _audio(value):
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<h", value) * 11_070)
    return payload.getvalue()


def _local_session(lt, host="127.0.0.1"):
    return lt.session({
        "listen_interfaces": f"{host}:0",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    })


def _wait_for(condition):
    end = time.monotonic() + 12
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("Локальная раздача не перешла в ожидаемое состояние")


def test_real_prune_then_redownload_while_siblings_are_retained_and_downloading(tmp_path):
    lt = pytest.importorskip("libtorrent")
    source = tmp_path / "seed"
    album = source / "album"
    album.mkdir(parents=True)
    entries = [("old.wav", _audio(100)), ("new.wav", _audio(200)), ("pending.wav", _audio(300) * 96)]
    for name, body in entries:
        (album / name).write_bytes(body)

    piece_length = 16_384
    total = b"".join(body for _, body in entries)
    data = bytes(lt.bencode({
        "info": {
            "name": b"album",
            "files": [{"path": [name.encode()], "length": len(body)} for name, body in entries],
            "piece length": piece_length,
            "pieces": b"".join(
                hashlib.sha1(total[offset:offset+piece_length]).digest()
                for offset in range(0, len(total), piece_length)
            ),
        },
    }))
    def seeder(host):
        session = _local_session(lt, host)
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(lt.bdecode(data))
        params.save_path = str(source)
        params.flags |= lt.torrent_flags.seed_mode
        session.add_torrent(params)
        return session

    seed = seeder("127.0.0.1")
    second_seed = None
    manager = TorrentManager(str(tmp_path / "downloads"), session=_local_session(lt))
    try:
        entry = manager.add_torrent_data(data)
        assert manager.set_file_priorities(entry.info_hash, [7, 4, 0])
        entry.handle.connect_peer(("127.0.0.1", seed.listen_port()))

        def complete(index):
            manager.poll()
            return entry.is_complete(index)

        _wait_for(lambda: complete(0) and complete(1))
        files = [Path(entry.file_path(index)) for index in range(3)]
        old = CacheFile(entry.info_hash, 0)
        assert not manager.prune([old])
        assert files[0].read_bytes() == entries[0][1]

        entry.handle.set_download_limit(64 * 1024)
        assert manager.set_file_priorities(entry.info_hash, [0, 4, 5])
        entry.handle.connect_peer(("127.0.0.1", seed.listen_port()))

        def downloading():
            manager.poll()
            downloaded = entry.files[2]["downloaded"]
            return 0 < downloaded < entry.files[2]["size"]

        _wait_for(downloading)
        assert entry.is_complete(0)
        assert manager._prunable(entry, 0, manager.save_dir)
        # A previously played file can be physically intact after a sibling
        # eviction while libtorrent still lacks one of its boundary pieces.
        entry.complete.discard(0)
        assert not entry.is_complete(0)
        assert not manager.prune([old])
        assert entry.info_hash in manager._cache_flushes

        def ready_to_prune():
            manager.poll()
            return manager._cache_flushes[entry.info_hash].finished

        _wait_for(ready_to_prune)
        assert not manager.prune([])  # The user requested this formerly old file again.
        assert files[0].read_bytes() == entries[0][1]

        def prune():
            manager.poll()
            return manager.prune([old])

        assert _wait_for(prune) == (old,)
        assert not files[0].exists()
        assert not entry.is_complete(0)
        assert entry.is_complete(1)
        assert not entry.is_complete(2)
        assert files[1].read_bytes() == entries[1][1]
        assert files[2].exists()
        assert entry.handle.is_valid()

        entry.handle.set_download_limit(0)
        assert manager.set_file_priorities(entry.info_hash, [7, 4, 5])
        checking = {
            lt.torrent_status.checking_files, lt.torrent_status.checking_resume_data,
            lt.torrent_status.queued_for_checking,
        }

        def ready_for_peer():
            manager.poll()
            status = entry.handle.status()
            return not status.paused and status.state not in checking and entry.info_hash not in manager._cache_rechecks

        _wait_for(ready_for_peer)
        # Rechecking disconnects peers. Use another actual local endpoint just
        # discovered by the client, without depending on a previous peer's backoff.
        second_seed = seeder("127.0.0.2")
        entry.handle.connect_peer(("127.0.0.2", second_seed.listen_port()))
        _wait_for(lambda: complete(0) and complete(2))
        assert files[0].read_bytes() == entries[0][1]
        assert files[1].read_bytes() == entries[1][1]
        assert files[2].read_bytes() == entries[2][1]
    finally:
        manager.shutdown()
        seed.pause()
        if second_seed is not None:
            second_seed.pause()
