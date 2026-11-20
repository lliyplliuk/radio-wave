from types import SimpleNamespace

import pytest

from radio_wave import online_radio
from radio_wave.state import AppState
from test_auto_radio import IndexFixture, create_service, make_release, wait_with_peer
from test_torrent_service_flow import PlayerProbe, wait_for


class PausableProbe(PlayerProbe):
    pause = PlayerProbe.stop

    def is_loaded(self):
        return bool(self.paths)


def test_cold_service_preserves_one_visible_slot_and_keeps_playing(tmp_path, create_service):
    first, data, first_hash, first_peer = make_release(
        tmp_path, "Кино — Первый альбом", "https://fixture.example/first",
        filenames=[f"{index:02}. Песня {index + 1}.wav" for index in range(8)],
    )
    alternative, alternative_data, alternative_hash, alternative_peer = make_release(
        tmp_path, "Алиса — Другой альбом", "https://fixture.example/alternative",
        filenames=["Первая ночь.wav"],
    )
    service = create_service(IndexFixture([first], {first.source_url: data}), PausableProbe())
    try:
        service.toggle_play()

        def first_album_registered():
            service.tick()
            return len(service.upcoming()) >= 4

        wait_for(first_album_registered)
        assert not service.is_playing()
        [first_visible] = service.visible_upcoming()
        service.pin_visible(item["id"] for item in service.upcoming()[:4])
        assert service.queue.pinned_ids() == (first_visible["id"],)

        wait_with_peer(service, first_hash, first_peer, service.is_playing)
        [after_start] = service.visible_upcoming()
        service.pin_visible([after_start["id"]])
        wait_with_peer(service, first_hash, first_peer, lambda: service.visible_upcoming()[0]["state"] == "ready")
        service.next_track()
        assert service.is_playing()
        assert service.current()["id"] == after_start["id"]

        for _ in range(2):
            wait_with_peer(service, first_hash, first_peer, lambda: service.visible_upcoming()[0]["state"] == "ready")
            service.next_track()
        service.pause_playback()
        assert not service.is_playing()
        assert len(service.state.history) >= 4
        [shown] = service.visible_upcoming()
        service.pin_visible(item["id"] for item in service.upcoming()[:4])
        assert service.queue.pinned_ids() == (shown["id"],)
        service.play_playback()
        assert service.is_playing()
        entry = service.torrents.add_torrent_data(alternative_data)
        service._save_source(entry, {"kind": "file", "source_url": alternative.source_url, "automatic": True}, False)
        wait_with_peer(
            service, alternative_hash, alternative_peer,
            lambda: any(item.get("info_hash") == alternative_hash for item in service.visible_upcoming()),
        )
        assert service.visible_upcoming()[0]["id"] == shown["id"]
    finally:
        first_peer.pause()
        alternative_peer.pause()


@pytest.mark.parametrize("automatic", [True, False])
def test_resume_loaded_torrents_with_history_limits_only_automatic_queue(tmp_path, create_service, automatic):
    state = AppState(str(tmp_path / "state.json"))
    records = []
    for title, names in (
        ("Кино — Альбом", [f"Песня {index}.wav" for index in range(8)]),
        ("Adele — Album", [f"Wonderful World {index}.wav" for index in range(8)]),
    ):
        release, data, info_hash, _peer = make_release(tmp_path, title, f"https://fixture.example/{title}",
                                                      seed=False, filenames=names)
        metadata = tmp_path / f"{info_hash}.torrent"
        metadata.write_bytes(data)
        records.append({"kind": "file", "path": str(metadata), "info_hash": info_hash,
                        "source_url": release.source_url, "automatic": automatic})
    state.data["torrent_sources"] = records
    state.remember({"id": f"{records[0]['info_hash']}:0", "artist": "Кино", "title": "Песня 0", "language": "ru"})
    service = create_service(IndexFixture([], {}), PausableProbe())

    assert len(service.state.history) == 1
    assert len(service.upcoming()) == 8
    visible = service.visible_upcoming()
    if not automatic:
        assert len(visible) == 4
        return
    assert {track["info_hash"] for track in visible} == {record["info_hash"] for record in records}
    assert len(visible) == 2
    service.pin_visible(track["id"] for track in service.upcoming())
    assert service.queue.pinned_ids() == tuple(track["id"] for track in visible)

    alternative, data, incoming_hash, _peer = make_release(
        tmp_path, "Алиса — Новый вариант", "https://fixture.example/alisa", seed=False, filenames=["Первая ночь.wav"],
    )
    entry = service.torrents.add_torrent_data(data)
    service._save_source(entry, {"kind": "file", "source_url": alternative.source_url, "automatic": True}, False)
    expanded = service.visible_upcoming()
    assert [track["id"] for track in expanded[:2]] == [track["id"] for track in visible]
    assert expanded[2]["info_hash"] == incoming_hash


def _persist_album_pair(tmp_path, *, automatic=True):
    state = AppState(str(tmp_path / "state.json"))
    records = []
    for title, names in (
        ("Кино — Альбом", [f"Песня {i}.wav" for i in range(6)]),
        ("Adele — Album", [f"Wonderful World {i}.wav" for i in range(6)]),
    ):
        release, data, info_hash, _ = make_release(tmp_path, title, f"https://fixture.example/{title}",
                                                   seed=False, filenames=names)
        metadata = tmp_path / f"{info_hash}.torrent"
        metadata.write_bytes(data)
        records.append({"kind": "file", "path": str(metadata), "info_hash": info_hash,
                        "source_url": release.source_url, "automatic": automatic})
    state.data["torrent_sources"] = records
    state.remember({"id": f"{records[0]['info_hash']}:0", "artist": "Кино", "title": "Песня 0", "language": "ru"})


@pytest.mark.parametrize("playing", [False, True])
def test_restored_short_wave_fills_four_without_changing_playback(tmp_path, create_service, monkeypatch, playing):
    _persist_album_pair(tmp_path)
    first, first_data, first_hash, first_peer = make_release(
        tmp_path, "Алиса — Release", "https://fixture.example/alisa-fill", filenames=["Ветер.wav"],
    )
    second, second_data, second_hash, second_peer = make_release(
        tmp_path, "ABBA — Release", "https://fixture.example/abba-fill", filenames=["Tonight.wav"],
    )
    monkeypatch.setattr(online_radio, "_GENRE_ARTISTS", {})
    monkeypatch.setattr(online_radio, "_RUSSIAN_ARTISTS", ("Алиса",))
    monkeypatch.setattr(online_radio, "_FOREIGN_ARTISTS", ("ABBA",))
    index = IndexFixture([first, second], {first.source_url: first_data, second.source_url: second_data})
    player = PausableProbe()
    service = create_service(index, player)
    if playing:
        service._idle_visible_fill = False
        service._radio_active = player.playing = True
    original = [track["id"] for track in service.visible_upcoming()]
    service.pin_visible(original)
    try:
        wait_with_peer(service, first_hash, first_peer, lambda: first_hash in {
            track["info_hash"] for track in service.visible_upcoming() if track["state"] == "ready"
        })
        wait_with_peer(service, second_hash, second_peer, lambda: second_hash in {
            track["info_hash"] for track in service.visible_upcoming() if track["state"] == "ready"
        })
        visible = service.visible_upcoming()
        assert len(visible) == len({track["info_hash"] for track in visible}) == 4
        assert [track["id"] for track in visible[:2]] == original
        assert len(service.state.history) == 1 and not service.state.early_skips
        assert not player.paths and service.is_playing() is playing and not service.waiting
        assert [call for call in index.calls if not call.startswith("https://")] == ["Алиса", "ABBA"]
    finally:
        first_peer.pause()
        second_peer.pause()


def test_visible_fill_can_try_a_different_artist_before_exhausting_one(tmp_path, monkeypatch):
    same_artist = [
        make_release(tmp_path, f"Алиса — Альбом {number}", f"https://fixture.example/alisa-{number}", seed=False)[0]
        for number in range(3)
    ]
    alternate = make_release(tmp_path, "ABBA — Album", "https://fixture.example/abba", seed=False)[0]
    index = IndexFixture([*same_artist, alternate], {})
    monkeypatch.setattr(online_radio, "_RUSSIAN_ARTISTS", ("Алиса",))
    monkeypatch.setattr(online_radio, "_FOREIGN_ARTISTS", ("ABBA",))

    candidates = online_radio.visible_artist_choices(
        index, online_radio.AcquisitionTaste(), set(), 0, lambda: False, ("ru", "foreign"), set(), {},
    )

    assert [choice.artist_hint for choice in candidates] == ["Алиса", "ABBA", "Алиса"]
    assert [choice.language_target for choice in candidates] == ["ru", "foreign", "ru"]


@pytest.mark.parametrize(("restored", "command"), [
    ("automatic", "stop_playback"), ("automatic", "pause_playback"), ("manual", None), ("empty", None),
])
def test_idle_visible_fill_respects_explicit_transport_and_source_provenance(tmp_path, create_service, restored, command):
    if restored != "empty":
        _persist_album_pair(tmp_path, automatic=restored == "automatic")
    index = IndexFixture([], {})
    service = create_service(index, PausableProbe())
    service.tick()
    if restored == "automatic":
        request = service._online
        assert request is not None and request.visible_ends and request.prefetch
        getattr(service, command)()
        assert request.cancelled.is_set()
    for _ in range(5):
        service.tick()
    assert service._online is None
    assert service._visible_fill_attempts == (1 if restored == "automatic" else 0)


def test_idle_visible_fill_bounds_empty_index_with_cooldown(tmp_path, create_service, monkeypatch):
    from radio_wave import service as service_module

    _persist_album_pair(tmp_path)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    index = IndexFixture([], {})
    service = create_service(index, PausableProbe())
    for _ in range(3):
        service.tick()
        assert service._online is not None and service._online.visible_ends
        wait_for(lambda: (service._poll_online() or service._online is None))
        for _ in range(3):
            service.tick()
            assert service._online is None
        clock.now += 10
    clock.now += 100
    service.tick()
    assert service._online is None and len(index.calls) == 6


def test_visible_fill_does_not_publish_an_artist_repeated_by_verified_tags(tmp_path, create_service, monkeypatch):
    from radio_wave import tags

    _persist_album_pair(tmp_path)
    release, data, info_hash, seeder = make_release(
        tmp_path, "Алиса — Release", "https://fixture.example/duplicate-artist", filenames=["Ветер.wav"],
    )
    monkeypatch.setattr(online_radio, "_GENRE_ARTISTS", {})
    monkeypatch.setattr(online_radio, "_RUSSIAN_ARTISTS", ("Алиса",))
    monkeypatch.setattr(tags, "read_tags", lambda _path: {"artist": "Кино", "language": "ru"})
    service = create_service(IndexFixture([release], {release.source_url: data}), PausableProbe())
    original = [track["id"] for track in service.visible_upcoming()]
    service.pin_visible(original)
    try:
        wait_for(lambda: (service.tick() or (service._online is not None and service._online.track_id is not None)))
        assert [track["id"] for track in service.visible_upcoming()] == original
        wait_with_peer(service, info_hash, seeder, lambda: release.source_url in service._failed_releases)
        assert [track["id"] for track in service.visible_upcoming()] == original
        assert service.catalogue.get(f"{info_hash}:0")["artist"] == "Кино"
        assert info_hash not in service._failed_torrents
    finally:
        seeder.pause()
