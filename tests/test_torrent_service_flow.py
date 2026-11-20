"""Изолированный BitTorrent-обмен через пользовательский сценарий RadioService."""

import hashlib
import io
import struct
import time
import wave
from pathlib import Path

import pytest

from radio_wave.library import Catalogue
from radio_wave.service import RadioService
from radio_wave.state import AppState
from radio_wave.torrent import TorrentManager

lt = pytest.importorskip("libtorrent")


class PlayerProbe:
    def __init__(self):
        self.paths = []
        self.playing = False

    def set_on_eos(self, callback):
        self.eos = callback

    def set_on_error(self, callback):
        self.error = callback

    def set_volume(self, _volume):
        pass

    def load(self, path):
        with wave.open(path) as audio:
            assert audio.readframes(audio.getnframes())
        self.paths.append(path)
        return True

    def play(self):
        self.playing = True

    def stop(self):
        self.playing = False

    def shutdown(self):
        self.stop()

    def is_playing(self):
        return self.playing

    def position(self):
        return 0.0

    def duration(self):
        return 0.0


def wav(sample):
    payload = io.BytesIO()
    with wave.open(payload, mode="wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<h", sample) * 16_362)
    return payload.getvalue()


def local_session():
    return lt.session({
        "listen_interfaces": "127.0.0.1:0",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
    })


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("Локальный сидер не завершил обмен")


@pytest.mark.parametrize("magnet", [False, True])
def test_service_download_wait_and_restore(tmp_path, monkeypatch, magnet):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    album = "Синтетический сборник"
    seed_dir = tmp_path / "seed"
    (seed_dir / album).mkdir(parents=True)
    tracks = {"Один.wav": wav(100), "Два.wav": wav(200), "Три.wav": wav(300)}
    assets = {**tracks, "Обложка.bin": b"x" * (32 * 1024)}
    contents = b"".join(assets.values())
    piece_length = 16_384
    metainfo = lt.bencode({
        "info": {
            "name": album.encode(),
            "files": [{"path": [name.encode()], "length": len(data)} for name, data in assets.items()],
            "piece length": piece_length,
            "pieces": b"".join(
                hashlib.sha1(contents[offset:offset + piece_length]).digest()
                for offset in range(0, len(contents), piece_length)
            ),
        }
    })
    torrent_file = tmp_path / "sample.torrent"
    torrent_file.write_bytes(metainfo)
    info = lt.torrent_info(str(torrent_file))
    info_hash = str(info.info_hash())
    for name, data in assets.items():
        (seed_dir / album / name).write_bytes(data)

    seeder = local_session()
    seed_parameters = lt.add_torrent_params()
    seed_parameters.ti = info
    seed_parameters.save_path = str(seed_dir)
    seed_parameters.flags |= lt.torrent_flags.seed_mode
    seeder.add_torrent(seed_parameters)

    def make_service(player):
        manager = TorrentManager(str(tmp_path / "downloads"), session=local_session())
        return RadioService(
            state=AppState(str(tmp_path / "state.json")),
            catalogue=Catalogue(str(tmp_path / "catalogue.json")),
            player=player,
            torrents=manager,
            online=False,
        )

    player = PlayerProbe()
    service = make_service(player)
    try:
        if magnet:
            assert service.add_magnet(f"magnet:?xt=urn:btih:{info_hash}", mark_russian=True) is None
        else:
            assert service.add_torrent_file(str(torrent_file), mark_russian=True) is None
            torrent_file.unlink()
        entry = service.torrents.get(info_hash)
        if magnet:
            entry.handle.connect_peer(("127.0.0.1", seeder.listen_port()))
            wait_for(lambda: (service.tick() or len(service.tracks()) == 3))
        tracks_by_name = {Path(track["path"]).name: track for track in service.tracks()}
        assert set(tracks_by_name) == set(tracks)
        first = tracks_by_name["Один.wav"]
        assert first["language"] == "ru"
        assert Path(first["path"]).is_relative_to(tmp_path / "downloads" / info_hash)

        # A file with the advertised length is not enough to start playback.
        if not magnet:
            fake_path = Path(first["path"])
            fake_path.parent.mkdir(parents=True, exist_ok=True)
            with fake_path.open("wb") as handle:
                handle.truncate(first["size"])
            assert not service.is_ready(first)

        service.play_track(first)
        service.toggle_play()
        assert service.status_text() == "Пауза"
        assert not player.paths

        if not magnet:
            service.play_track(first)
            entry.handle.connect_peer(("127.0.0.1", seeder.listen_port()))
            wait_for(lambda: (service.tick() or player.paths))
        else:
            wait_for(lambda: (service.tick() or service.is_ready(first)))
            assert not player.paths
            assert service.state.history == []
            service.play_track(first)
        assert Path(first["path"]).read_bytes() == tracks["Один.wav"]
        assert not (Path(entry.save_path) / album / "Обложка.bin").exists()

        service.tick()
        assert player.paths == [first["path"]]
        assert service.state.history[0]["id"] == first["id"]
        player.eos()
        wait_for(lambda: (service.tick() or len(player.paths) == 2))
        assert player.paths[0] != player.paths[1]
    finally:
        service.quit()
        seeder.pause()

    restored = make_service(PlayerProbe())
    try:
        assert restored.torrents.get(info_hash) is not None
        assert restored.catalogue.get(first["id"])["path"] == first["path"]
        wait_for(lambda: (restored.tick() or restored.is_ready(first)))
    finally:
        restored.quit()
