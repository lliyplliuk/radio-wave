from __future__ import annotations

from types import SimpleNamespace

import pytest

from radio_wave import service as service_module
from radio_wave.library import Catalogue
from radio_wave.service import RadioService
from radio_wave.state import AppState
from test_torrent_service_flow import PlayerProbe, wav


@pytest.fixture
def files(tmp_path, monkeypatch):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    return str(tmp_path / "state.json"), str(tmp_path / "catalog.json")


def id3v23():
    frames = []
    for key, value in (("TIT2", "Синяя вода"), ("TPE1", "Татьяна"), ("TCON", "Поп")):
        payload = b"\x01" + value.encode("utf-16") + b"\0\0"
        frames.append(key.encode() + len(payload).to_bytes(4, "big") + b"\0\0" + payload)
    body = b"".join(frames)
    size = bytes((len(body) >> 21 & 0x7f, len(body) >> 14 & 0x7f, len(body) >> 7 & 0x7f, len(body) & 0x7f))
    return b"ID3\x03\x00\x00" + size + body


@pytest.mark.parametrize("temporarily_missing", [False, True])
def test_legacy_text_repair_preserves_ids_edits_and_reaction_order(tmp_path, files, temporarily_missing):
    state_path, catalogue_path = files
    file = tmp_path / "Файловое название.mp3"
    file.write_bytes(id3v23())
    catalogue = Catalogue(catalogue_path)
    [track] = catalogue.scan_paths([str(file)])
    broken = "ÿþС\x00и\x00"
    track.update(title=broken, artist=broken, genre="Авторский жанр", edited=["genre"])
    catalogue.save()
    before = AppState(state_path)
    before.like(track)
    before.remember(track)
    when = before.likes[0]["at"]
    if temporarily_missing:
        file.unlink()

    service = RadioService(state=AppState(state_path), catalogue=Catalogue(catalogue_path), start_torrents=False)
    repaired = service.tracks()[0]
    if temporarily_missing:
        assert repaired["title"] == file.stem
        assert repaired["tag_repair_pending"] == ["artist", "title"]
        file.write_bytes(id3v23())
        service.tick()
        repaired = service.tracks()[0]
    assert repaired["id"] == track["id"]
    assert repaired["title"] == "Синяя вода"
    assert repaired["artist"] == "Татьяна"
    assert repaired["genre"] == "Авторский жанр"
    assert "tag_repair_pending" not in repaired
    persisted = AppState(state_path)
    assert persisted.likes[0]["title"] == "Синяя вода"
    assert persisted.history[0]["artist"] == "Татьяна"
    assert persisted.likes[0]["id"] == track["id"]
    assert persisted.likes[0]["at"] == when
    service.quit()


class ClockedPlayer(PlayerProbe):
    def __init__(self):
        super().__init__()
        self.at = 0.0
        self.length = 100.0
        self.starts = True
        self.accept_seek = True

    def load(self, path):
        self.at = 0.0
        return super().load(path)

    def play(self):
        if self.starts:
            super().play()

    def toggle(self):
        self.playing = not self.playing

    def pause(self):
        self.playing = False

    def is_loaded(self):
        return bool(self.paths)

    def position(self):
        return self.at

    def duration(self):
        return self.length

    def seek(self, seconds):
        if not self.accept_seek:
            return False
        self.at = seconds
        return True


@pytest.fixture
def listening(tmp_path, monkeypatch, files):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    state_path, catalogue_path = files
    player = ClockedPlayer()
    service = RadioService(state=AppState(state_path), catalogue=Catalogue(catalogue_path), player=player)
    paths = [tmp_path / name for name in ("Первая.wav", "Вторая.wav")]
    for path in paths:
        path.write_bytes(wav(250))
    service.import_paths([str(path) for path in paths], mark_russian=True)
    yield service, player, clock
    service.quit()


@pytest.mark.parametrize("elapsed,weak", [(24.9, True), (25.0, False), (55.0, False)])
def test_explicit_next_before_exact_threshold(listening, elapsed, weak):
    service, player, clock = listening
    service.toggle_play()
    first = service.current()
    clock.now = player.at = elapsed
    service.user_next()
    persisted = AppState(service.state.path)
    assert not persisted.dislikes
    assert [item["id"] for item in persisted.early_skips] == ([first["id"]] if weak else [])
    if weak:
        assert persisted.early_skips[0]["language"] == "ru"


def test_early_next_keeps_persisted_like_and_records_weak_skip(listening):
    service, player, clock = listening
    service.toggle_play()
    service.tick()
    first = service.current()
    service.like_current()
    assert service.current_liked()
    clock.now = player.at = 10.0
    service.user_next()

    reloaded = AppState(service.state.path)
    assert reloaded.is_liked(first["id"])
    assert [item["id"] for item in reloaded.early_skips] == [first["id"]]
    assert not reloaded.dislikes

    reopened = RadioService(
        state=reloaded, catalogue=Catalogue(service.catalogue.path), player=ClockedPlayer(),
    )
    try:
        reopened.play_track(first)
        reopened.tick()
        assert reopened.current()["id"] == first["id"]
        assert reopened.current_liked()
    finally:
        reopened.quit()


def test_dislike_immediately_advances_without_weak_skip(listening):
    service, player, clock = listening
    service.toggle_play()
    service.tick()
    first = service.current()
    service.like_current()
    clock.now = player.at = 10.0
    service.dislike_current()

    assert service.current()["id"] != first["id"]
    assert player.playing
    assert len(player.paths) == 2
    reloaded = AppState(service.state.path)
    assert [item["id"] for item in reloaded.dislikes] == [first["id"]]
    assert not reloaded.likes
    assert not reloaded.early_skips
    assert reloaded.record_early_skip(first) is False


def test_seek_and_pause_do_not_change_taste_or_count_as_audible_time(listening):
    service, player, clock = listening
    service.toggle_play()
    first = service.current()
    clock.now = player.at = 10.0
    assert service.seek(90.0)
    assert not service.state.early_skips
    service.toggle_play()
    clock.now = 80.0
    service.tick()
    assert not service.state.early_skips
    service.toggle_play()
    clock.now = 81.0
    player.at = 91.0
    service.user_next()
    assert [item["id"] for item in service.state.early_skips] == [first["id"]]


def test_rejected_seek_preserves_progress_across_threshold(listening):
    service, player, clock = listening
    service.toggle_play()
    clock.now = player.at = 24.9
    player.accept_seek = False
    assert service.seek(90.0) is False
    clock.now = player.at = 25.1
    service.user_next()
    assert not service.state.early_skips


@pytest.mark.parametrize("event", ["automatic", "eos", "error", "unconfirmed", "unknown_duration"])
def test_nonmatching_events_do_not_create_weak_negative(listening, event):
    service, player, clock = listening
    if event == "unconfirmed":
        player.starts = False
    elif event == "unknown_duration":
        player.length = 0.0
    service.toggle_play()
    clock.now = player.at = 5.0
    if event == "automatic":
        service.next_track()
    elif event == "eos":
        player.eos()
    elif event == "error":
        player.error("decoder")
    else:
        service.user_next()
    assert not service.state.early_skips
    assert not service.state.dislikes
