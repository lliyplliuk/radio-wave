"""Regression coverage for large previously imported torrent collections."""

from pathlib import Path

import pytest

from radio_wave import config, tags
from radio_wave.candidate_window import AUTO_CANDIDATE_LIMIT
from radio_wave.library import Catalogue
from radio_wave.online_radio import OnlineChoice
from radio_wave.service import RadioService, _OnlineRequest
from radio_wave.state import AppState
from radio_wave.torrent_index import TorrentRelease
from test_torrent_service_flow import PlayerProbe


class TransferEntry:
    def __init__(self, info_hash, count, root):
        self.info_hash = info_hash
        self.name = f"Сборник {info_hash[-1]}"
        self.files = [
            {"downloaded": 0, "size": 32768, "index": index, "path": f"{index}.wav"}
            for index in range(count)
        ]
        self.complete = set()
        self.paths = [str(root / info_hash / f"{index}.wav") for index in range(count)]
        self.state = "downloading"
        self.peers = 1

    def file_path(self, index):
        return self.paths[index]

    def is_complete(self, index):
        return index in self.complete


class Transfers:
    def __init__(self, entries):
        self.entries = {entry.info_hash: entry for entry in entries}
        self.lookups = 0
        self.completions = []

    def get(self, info_hash):
        self.lookups += 1
        return self.entries.get(info_hash)

    def poll(self):
        completions, self.completions = self.completions, []
        for entry, index in completions:
            entry.complete.add(index)
            self.on_file_complete(entry.info_hash, entry, index)

    def refresh(self):
        pass

    def prune(self, _candidates):
        return ()

    def schedule(self, plan):
        self.plan = plan

    def shutdown(self):
        pass


class IndexedPlayer(PlayerProbe):
    def load(self, path):
        self.paths.append(path)
        return True


@pytest.fixture
def scale(tmp_path, monkeypatch):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    transfers = Transfers([
        TransferEntry(f"{number:040x}", size, tmp_path)
        for number, size in enumerate((571, 500, 523), start=1)
    ])
    state = AppState(str(tmp_path / "state.json"))
    catalogue = Catalogue(str(tmp_path / "catalogue.json"))
    for entry in transfers.entries.values():
        state.data.setdefault("torrent_sources", []).append({
            "info_hash": entry.info_hash, "kind": "file",
            "source_url": f"https://fixture.example/{entry.info_hash[-1]}", "automatic": True,
        })
        for index, path in enumerate(entry.paths):
            catalogue.tracks.append({
                "id": f"{entry.info_hash}:{index}", "source": "torrent",
                "info_hash": entry.info_hash, "file_index": index, "path": path,
                "title": f"Дорожка {index}", "artist": entry.name, "genre": "",
                "lang_source": "", "language": "unknown", "mbid": "", "size": 32768,
            })
    monkeypatch.setattr(tags, "read_tags", lambda _path: {"genre": "Рок"})
    service = RadioService(
        state=state, catalogue=catalogue, player=IndexedPlayer(), torrents=transfers,
        online=False, start_torrents=False,
    )
    yield service, transfers
    service.quit()


def test_large_completion_batch_writes_and_projects_once(scale, monkeypatch):
    service, transfers = scale
    entry = next(iter(transfers.entries.values()))
    transfers.completions.extend((entry, index) for index in range(571))
    events = []
    real_write, real_projection = service.catalogue.save, service.queue.set_catalogue

    def write():
        events.append("write")
        real_write()

    def project(tracks, **kwargs):
        events.append("project")
        real_projection(tracks, **kwargs)

    monkeypatch.setattr(service.catalogue, "save", write)
    monkeypatch.setattr(service.queue, "set_catalogue", project)

    service.tick()

    assert events == ["project", "write"]
    assert len(service.search_tracks()) == 1594
    assert sum(track["ready"] for track in service.search_tracks()) == 571
    assert len(service.tracks()) == AUTO_CANDIDATE_LIMIT
    assert len(service.upcoming()) == 8

    events.clear()
    transfers.lookups = 0
    service.tick()
    assert events == []
    assert transfers.lookups == len(transfers.entries)


def test_sliding_window_keeps_departed_history_and_explicit_replay(scale):
    service, transfers = scale
    for entry in transfers.entries.values():
        entry.complete.update(range(len(entry.files)))
    service._refresh_catalogue()
    assert len(service.tracks()) == AUTO_CANDIDATE_LIMIT
    service.toggle_play()
    for turn in range(20):
        service.tick()
        assert len(service.tracks()) <= AUTO_CANDIDATE_LIMIT + 1
        assert len(service.upcoming()) == 8
        if turn < 19:
            service.user_next()

    played = [item["id"] for item in service.state.history]
    assert len(played) == len(set(played)) == 20
    assert len(service.search_tracks()) == 1594
    dormant = next(track for track in service.search_tracks() if track["id"] == played[0])
    assert dormant["id"] not in {track["id"] for track in service.tracks()}
    service.play_track(dormant)
    service.tick()
    assert service.current()["id"] == dormant["id"]
    assert len(service.state.history) == 21
    assert Path(service.player.paths[-1]) == Path(dormant["path"])
    assert len(service.tracks()) <= AUTO_CANDIDATE_LIMIT + 1


def test_playback_starts_with_eight_pending_torrents_and_prioritises_current(scale):
    service, transfers = scale
    entry = next(iter(transfers.entries.values()))
    entry.complete.add(0)
    service._refresh_catalogue()
    service.toggle_play()
    service.tick()

    current = service.current()
    upcoming = service.upcoming()
    assert service.is_playing()
    assert len(upcoming) == 8 and all(track["state"] == "pending" for track in upcoming)
    assert transfers.plan == [
        {"info_hash": track["info_hash"], "file_index": track["file_index"], "level": priority}
        for track, priority in [(current, config.PRIORITY_TOP), *(
            (track, config.PRIORITY_DEFAULT) for track in upcoming
        )]
    ]


def test_discovery_broadens_a_manual_selection_without_exposing_whole_album(scale):
    service, transfers = scale
    entry = next(iter(transfers.entries.values()))
    manual_id = f"{entry.info_hash}:0"
    saved = service.state.data["torrent_sources"][0]
    saved.update(automatic=False, file_indices=[0])
    service.catalogue.tracks = [
        track for track in service.search_tracks()
        if track["info_hash"] != entry.info_hash or track["id"] == manual_id
    ]
    service._refresh_catalogue()
    sibling = TorrentRelease("Сборник", None, None, 8, "https://fixture.example/alias-of-selection")
    request = _OnlineRequest(True, choice=OnlineChoice(sibling), entry=entry, discovery=True)
    service._online = request

    service._accept_online_source(request)

    assert saved["automatic"] is True
    assert saved["manual_file_indices"] == [0]
    assert "file_indices" not in saved
    assert {manual_id}.issubset(track["id"] for track in service.tracks())
    assert len(service.tracks()) == AUTO_CANDIDATE_LIMIT + 1
    assert len(service.search_tracks()) == 1594
