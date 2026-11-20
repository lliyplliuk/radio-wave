"""Запуск сетевого радио без импорта через локального сидера и GStreamer."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

import pytest

from radio_wave import catalog, config
from radio_wave.catalog import CatalogRecording
from radio_wave.library import Catalogue
from radio_wave.online_radio import recording_path_matches
from radio_wave.player import Player
from radio_wave.service import RadioService, _OnlineRequest
from radio_wave.state import AppState
from radio_wave.torrent import TorrentManager
from radio_wave.torrent_index import TorrentRelease
from test_torrent_service_flow import PlayerProbe, local_session, wait_for, wav

lt = pytest.importorskip("libtorrent")
_ONE = CatalogRecording("Песня 1", "Кино", "12345678-1111-4111-8111-123456789abc")
_TWO = CatalogRecording("Песня 2", "Кино", "12345678-2222-4111-8111-123456789abc")
_UNMATCHED = CatalogRecording("Другая песня", "Кино", "12345678-3333-4111-8111-123456789abc")


class IndexFixture:
    def __init__(self, releases, metainfo):
        self.releases = releases
        self.metainfo = metainfo
        self.calls = []

    def latest_audio(self):
        self.calls.append("latest")
        return self.releases

    def search_audio(self, query):
        self.calls.append(query)
        return self.releases

    def fetch_torrent(self, release):
        self.calls.append(release.source_url)
        return self.metainfo[release.source_url]


def make_release(tmp_path, title, url, *, seed=True, filenames=None):
    filenames = filenames or [f"{n:02d}. Песня {n}.wav" for n in range(1, 7)]
    payloads = {name: wav(n * 100) for n, name in enumerate(filenames, start=1)}
    data = b"".join(payloads.values())
    piece = 16_384
    metainfo = lt.bencode({
        "info": {
            "name": title.encode(),
            "files": [{"path": [name.encode()], "length": len(audio)} for name, audio in payloads.items()],
            "piece length": piece,
            "pieces": b"".join(hashlib.sha1(data[offset:offset + piece]).digest() for offset in range(0, len(data), piece)),
        }
    })
    info = lt.torrent_info(lt.bdecode(metainfo))
    info_hash = str(info.info_hash())
    release = TorrentRelease(title, None, url + "/download", 15, url, True)
    seeder = None
    if seed:
        folder = tmp_path / "seed" / title
        folder.mkdir(parents=True)
        for name, audio in payloads.items():
            (folder / name).write_bytes(audio)
        seeder = local_session()
        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = str(folder.parent)
        params.flags |= lt.torrent_flags.seed_mode
        seeder.add_torrent(params)
    return release, metainfo, info_hash, seeder


@pytest.fixture
def create_service(tmp_path, monkeypatch):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(catalog, "search_recordings", lambda *args, **kwargs: [])
    monkeypatch.setattr(catalog, "similar_recordings", lambda *args, **kwargs: [])
    monkeypatch.setattr(catalog, "recording_language_code", lambda _mbid: None)
    services = []

    def build(index, player, *, missing_paths=()):
        catalogue_path = str(tmp_path / "catalog.json")
        if missing_paths:
            persisted = Catalogue(catalogue_path)
            for path in missing_paths:
                path.write_bytes(wav(250))
            persisted.scan_paths([str(path) for path in missing_paths])
            for path in missing_paths:
                path.unlink()
        service = RadioService(
            state=AppState(str(tmp_path / "state.json")),
            catalogue=Catalogue(catalogue_path),
            player=player,
            torrents=TorrentManager(str(tmp_path / "downloads"), session=local_session()),
            index=index,
        )
        services.append(service)
        return service

    yield build
    for service in services:
        service.quit()


def wait_with_peer(service, identifier, seeder, condition, *, glib=None):
    connected = False

    def check():
        nonlocal connected
        if glib:
            context = glib.MainContext.default()
            while context.pending():
                context.iteration(False)
        service.tick()
        entry = service.torrents.get(identifier)
        if entry is not None and not connected and seeder is not None:
            entry.handle.connect_peer(("127.0.0.1", seeder.listen_port()))
            connected = True
        return condition()

    wait_for(check)


@pytest.mark.parametrize("missing_count", [0, 5])
def test_empty_play_downloads_and_plays_gstreamer(tmp_path, create_service, missing_count):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib

    album, data, info_hash, seed = make_release(
        tmp_path, "Кино — Проверка", "https://fixture.example/album",
        filenames=[f"{n:02d}. Песня {n}.wav" for n in range(1, 11)],
    )
    player = Player()
    sink = player._Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", True)
    player._playbin.set_property("audio-sink", sink)
    missing = [tmp_path / f"Отсутствующая песня {index}.wav" for index in range(missing_count)]
    service = create_service(IndexFixture([album], {album.source_url: data}), player, missing_paths=missing)
    try:
        assert len(service.tracks()) == missing_count
        assert not service.upcoming()
        assert not service.torrents.torrents()
        service.toggle_play()
        assert service.waiting
        wait_with_peer(service, info_hash, seed, lambda: service.is_playing() and bool(service.state.history), glib=GLib)
        current = service.current()
        assert current["info_hash"] == info_hash
        assert service.is_ready(current)
        assert len(service.upcoming()) == 8
        assert Path(player.current_path()) == Path(current["path"])
        assert service.state.history[0]["id"] == current["id"]
        assert all(any(track["path"] == str(path) for track in service.tracks()) for path in missing)
    finally:
        seed.pause()


def test_removed_local_file_recovers_without_clearing_decode_blacklist(tmp_path, create_service):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    moving = tmp_path / "Съёмный файл.wav"
    invalid = tmp_path / "Недекодируемый файл.wav"
    for path in (moving, invalid):
        path.write_bytes(wav(250))
    service.import_paths([str(moving), str(invalid)])
    records = {Path(track["path"]): track["id"] for track in service.tracks()}
    service.queue.set_broken(records[invalid])
    moving.unlink()
    invalid.unlink()

    service.tick()
    assert service.upcoming() == []
    assert len(service.tracks()) == 2

    for path in (moving, invalid):
        path.write_bytes(wav(300))
    service.tick()
    assert [track["id"] for track in service.upcoming()] == [records[moving]]
    assert not service.queue.is_ready(records[invalid])


def test_same_album_supports_distinct_named_recordings(tmp_path, create_service):
    album, data, info_hash, seed = make_release(tmp_path, "Кино — Сборник", "https://fixture.example/tracks")
    player = PlayerProbe()
    service = create_service(IndexFixture([album], {album.source_url: data}), player)
    try:
        service.play_recording(_ONE)
        wait_with_peer(service, info_hash, seed, lambda: len(player.paths) == 1)
        assert [track["mbid"] for track in service.tracks()] == [_ONE.mbid]
        service.play_recording(_TWO)
        wait_with_peer(service, info_hash, seed, lambda: len(player.paths) == 2)
        assert {track["mbid"] for track in service.tracks()} == {_ONE.mbid, _TWO.mbid}
        assert player.paths[0] != player.paths[1]
        assert len(service.torrents.torrents()) == 1
        service.play_recording(_UNMATCHED)
        wait_with_peer(service, info_hash, seed, lambda: not service.waiting)
        assert len(player.paths) == 2  # An explicit request must never switch to a different song.
        assert _UNMATCHED.title in service.status_text()
        assert all(item["title"] != _UNMATCHED.title for item in service.state.history)
        assert len(service.tracks()) == 2
    finally:
        seed.pause()


@pytest.mark.parametrize("trickle", [False, True])
def test_named_request_prefers_more_peers_and_tries_the_next_source(tmp_path, create_service, trickle):
    first, first_data, first_hash, _ = make_release(
        tmp_path, "Кино — Более популярный", "https://fixture.example/preferred", seed=False,
    )
    second, second_data, second_hash, seed = make_release(
        tmp_path, "Кино — Запасной", "https://fixture.example/alternate",
    )
    first = replace(first, seeders=100)
    index = IndexFixture([second, first], {first.source_url: first_data, second.source_url: second_data})
    player = PlayerProbe()
    service = create_service(index, player)
    try:
        service.play_recording(_ONE)
        wait_for(lambda: (service.tick() or service._requested is not None))
        request = service._online
        assert request.choice.release == first
        assert service._requested.startswith(first_hash)
        assert not player.paths
        now = time.monotonic()
        if trickle:
            request.attempt_ends = now - 1
            index_in_album = service.catalogue.get(service._requested)["file_index"]
            request.entry.files[index_in_album]["downloaded"] = 16_384
        else:
            request.deadline = now - 1
        service._poll_online()
        wait_with_peer(service, second_hash, seed, service.is_playing)
        assert service.current()["info_hash"] == second_hash
        assert first_hash in service._failed_torrents
        assert first.source_url in service._failed_releases
        assert not any(row["id"].startswith(first_hash) for row in service.state.history)
    finally:
        seed.pause()


def test_manual_exact_cached_identity_prefers_studio_even_when_live_was_imported_first(tmp_path, create_service):
    player = PlayerProbe()
    service = create_service(IndexFixture([], {}), player)
    live_file = tmp_path / "Песня 1 (Live).wav"
    studio = tmp_path / "Песня 1.wav"
    for path in (live_file, studio):
        path.write_bytes(wav(320))
        service.import_paths([str(path)])
        track = next(track for track in service.catalogue.all() if track["path"] == str(path))
        service.update_track(track["id"], artist="Кино", title="Песня 1", mbid=_ONE.mbid)
    service.play_recording(_ONE)
    assert player.paths == [str(studio)]


@pytest.mark.parametrize("version", ("same", "different-id", "live-path", "ambiguous"))
def test_manual_pending_copy_uses_compatible_ready_file(tmp_path, create_service, version):
    release, data, torrent_hash, _ = make_release(
        tmp_path, "Другая группа — Альбом", "https://fixture.example/pending-copy",
        filenames=["Страшилка.wav", "Другая версия.wav"] if version == "ambiguous" else ["Страшилка.wav"],
        seed=False,
    )
    player = PlayerProbe()
    service = create_service(IndexFixture([], {}), player)
    entry = service.torrents.add_torrent_data(data)
    service._save_source(entry, {"kind": "file", "source_url": release.source_url}, False)
    pending = service.catalogue.get(f"{torrent_hash}:0")
    service.update_track(pending["id"], artist="Другая группа", title="Страшилка", mbid=_ONE.mbid)
    if version == "ambiguous":
        service.update_track(f"{torrent_hash}:1", artist="Другая группа", title="Страшилка", mbid=_TWO.mbid)
    folder = tmp_path / ("Live at Leeds" if version == "live-path" else "Studio")
    folder.mkdir()
    local = folder / "Страшилка.wav"
    local.write_bytes(wav(320))
    service.import_paths([str(local)])
    ready = service.search_tracks()[-1]
    service.update_track(ready["id"], artist="Другая группа", title="Страшилка",
                         mbid=_TWO.mbid if version == "different-id" else "")
    assert service.search_tracks()[0]["id"] == pending["id"]
    assert not service.is_ready(pending) and service.is_ready(ready)

    service.play_track(pending)
    assert not service.state.history
    if version == "same":
        assert player.paths == [str(local)]
        assert service._requested is None
        service.tick()
        assert [item["id"] for item in service.state.history] == [ready["id"]]
    else:
        assert not player.paths
        assert service._requested == pending["id"]
    if version == "ambiguous":
        service.play_recording(CatalogRecording("Страшилка", "Другая группа", _ONE.mbid))
        assert not player.paths
        assert service._online is not None


def test_manual_ready_copy_plays_the_exact_selected_file(tmp_path, create_service):
    player = PlayerProbe()
    service = create_service(IndexFixture([], {}), player)
    for filename in ("other.wav", "selected.wav"):
        path = tmp_path / filename
        path.write_bytes(wav(320))
        service.import_paths([str(path)])
        track = service.search_tracks()[-1]
        service.update_track(track["id"], artist="Другая группа", title="Страшилка")

    service.play_track(track)
    assert player.paths == [str(tmp_path / "selected.wav")]
    assert not service.state.history


def test_catalogue_recording_without_mbid_uses_known_album_to_find_audio(tmp_path, create_service):
    release, data, info_hash, seeder = make_release(
        tmp_path, "Nagart — Vol. 3", "https://fixture.example/known-album", filenames=["Шкатулка.wav"],
    )

    class AlbumIndex(IndexFixture):
        def search_audio(self, query):
            self.calls.append(query)
            return self.releases if query == "Nagart Vol. 3" else []

    index = AlbumIndex([release], {release.source_url: data})
    service = create_service(index, PlayerProbe())
    try:
        service.play_recording(CatalogRecording("Шкатулка", "Nagart", "", album_titles=("Vol. 3",)))
        wait_with_peer(service, info_hash, seeder, service.is_playing)
        assert "Nagart Vol. 3" in index.calls
        assert service.current()["title"] == "Шкатулка" and not service.current()["mbid"]
    finally:
        seeder.pause()


def test_no_matching_named_source_stays_unavailable_without_playing_unrelated_track(tmp_path, create_service, caplog):
    caplog.set_level("INFO", logger="radio_wave.playback")
    release, data, _info_hash, _ = make_release(
        tmp_path, "Кино — Другая запись", "https://fixture.example/nonmatch", seed=False,
    )
    player = PlayerProbe()
    service = create_service(IndexFixture([release], {release.source_url: data}), player)
    local = tmp_path / "Другая композиция.wav"
    local.write_bytes(wav(500))
    service.import_paths([str(local)])
    service.play_recording(_UNMATCHED)
    wait_for(lambda: (service.tick() or not service.waiting))
    assert not player.paths
    assert len(service.tracks()) == 1
    assert not service.state.history
    assert _UNMATCHED.title in service.status_text()
    assert "нужной записи" in service.status_text()
    assert "lookup_result provider=configured candidates=1" in caplog.text
    assert "source_rejected attempt=1 reason=no_matching_audio" in caplog.text
    assert "audio_started" not in caplog.text
    assert release.source_url not in caplog.text
    assert _UNMATCHED.title not in caplog.text
    assert str(local) not in caplog.text


def test_automated_wave_falls_back_to_a_ready_track_after_discovery_exhausted(tmp_path, create_service):
    player = PlayerProbe()
    service = create_service(IndexFixture([], {}), player)
    local = tmp_path / "Знакомая.wav"
    local.write_bytes(wav(500))
    service.import_paths([str(local)])
    [track] = service.tracks()
    service.state.remember(track)
    service.queue.set_history(service.state.history)
    service.toggle_play()
    assert service.waiting
    wait_for(lambda: (service.tick() or service.is_playing()))
    assert player.paths == [str(local)]


def test_failed_prefetch_does_not_grant_foreground_recent_repeat(tmp_path, create_service):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    local = tmp_path / "Уже знакомая.wav"
    local.write_bytes(wav(500))
    service.import_paths([str(local)])
    [track] = service.search_tracks()
    service.state.remember(track)
    service.queue.set_history(service.state.history)
    service._finish_online(_OnlineRequest(True, discovery=True), "test unavailable")

    service.next_track()

    assert service.waiting and service._online is not None and not service._online.prefetch
    assert not service.player.paths


def test_online_discovery_rejects_recent_alias_revealed_by_downloaded_tags(tmp_path, create_service, monkeypatch):
    from radio_wave import tags

    duplicate, duplicate_data, duplicate_hash, first_seed = make_release(
        tmp_path, "Кино — Первый сборник", "https://fixture.example/recent-alias",
        filenames=["01. Загадочная дорожка.wav"],
    )
    fresh, fresh_data, fresh_hash, second_seed = make_release(
        tmp_path, "Кино — Второй сборник", "https://fixture.example/fresh",
        filenames=["01. Другая дорожка.wav"],
    )
    duplicate = replace(duplicate, seeders=100)
    index = IndexFixture([fresh, duplicate], {duplicate.source_url: duplicate_data, fresh.source_url: fresh_data})
    service = create_service(index, PlayerProbe())
    local = tmp_path / "Знакомая песня.wav"
    service.set_taste(language_weight=0)
    local.write_bytes(wav(500))
    service.import_paths([str(local)])
    [previous] = service.search_tracks()
    service.update_track(previous["id"], artist="Кино", title="Знакомая песня")
    service.state.remember(previous)
    service.queue.set_history(service.state.history)
    monkeypatch.setattr(
        tags, "read_tags",
        lambda path: {"artist": "Кино", "title": "Знакомая песня"} if duplicate_hash in path else {},
    )
    peers = {duplicate_hash: first_seed, fresh_hash: second_seed}

    def tick_until_playback():
        service.tick()
        for info_hash, seeder in tuple(peers.items()):
            entry = service.torrents.get(info_hash)
            if entry is not None:
                entry.handle.connect_peer(("127.0.0.1", seeder.listen_port()))
                del peers[info_hash]
        return service.is_playing()

    try:
        service.toggle_play()
        wait_for(tick_until_playback)
        assert duplicate.source_url in service._failed_releases
        assert service.current()["info_hash"] == fresh_hash
        assert not any(row["id"].startswith(duplicate_hash) for row in service.state.history)
    finally:
        first_seed.pause()
        second_seed.pause()


def test_plain_title_can_resolve_directly_from_torrent_filename(tmp_path, create_service):
    release, data, info_hash, seed = make_release(tmp_path, "Кино — Названия", "https://fixture.example/filename")
    service = create_service(IndexFixture([release], {release.source_url: data}), PlayerProbe())
    try:
        service.play_search("Песня 3")
        wait_with_peer(service, info_hash, seed, service.is_playing)
        assert Path(service.current()["path"]).name == "03. Песня 3.wav"
        assert len(service.tracks()) == 1
    finally:
        seed.pause()


def test_exact_request_accepts_unique_full_title_from_unconfirmed_source(tmp_path, create_service):
    wrong, wrong_data, _wrong_hash, _ = make_release(
        tmp_path, "Nagart — 13 этаж", "https://fixture.example/wrong-release", seed=False,
    )
    full, full_data, info_hash, seed = make_release(
        tmp_path, "Nagart — Сборник", "https://fixture.example/verified-release",
        filenames=["01. Мёртвая Алёна.wav", "02. Другая песня.wav"],
    )
    full = replace(full, seeders=0)
    index = IndexFixture([full, wrong], {full.source_url: full_data, wrong.source_url: wrong_data})
    service = create_service(index, PlayerProbe())
    try:
        service.play_recording(CatalogRecording("Алёна", "Nagart", ""))
        wait_with_peer(service, info_hash, seed, service.is_playing)
        assert service.current()["title"] == "Мёртвая Алёна"
        assert service.current()["artist"] == "Nagart"
        assert service.current()["mbid"] == ""
        assert wrong.source_url in service._failed_releases
    finally:
        seed.pause()


def test_existing_playback_prefetches_named_suggestion(tmp_path, create_service):
    album, data, info_hash, seed = make_release(tmp_path, "Кино — Похожее", "https://fixture.example/similar")
    player = PlayerProbe()
    service = create_service(IndexFixture([album], {album.source_url: data}), player)
    local = tmp_path / "Местная дорожка.wav"
    local.write_bytes(wav(500))
    assert service.import_paths([str(local)]) == 1
    service.set_discovery_candidates([_ONE])
    service.toggle_play()
    try:
        wait_with_peer(
            service, info_hash, seed,
            lambda: any(track["mbid"] == _ONE.mbid and track["state"] == "ready" for track in service.upcoming()),
        )
        assert player.paths == [str(local)]
        service.next_track()
        assert service.current()["mbid"] == _ONE.mbid
    finally:
        seed.pause()


def test_foreign_chanson_populates_hidden_slots_even_with_full_russian_buffer(tmp_path, create_service):
    album, data, info_hash, seed = make_release(
        tmp_path, "French chanson — Anthology", "https://fixture.example/foreign-chanson",
        filenames=["Toujours.wav"],
    )
    index = IndexFixture([album], {album.source_url: data})
    service = create_service(index, PlayerProbe())
    for n in range(10):
        file = tmp_path / f"Локальная {n}.wav"
        file.write_bytes(wav(n + 100))
        service.import_paths([str(file)])
        service.update_track(service.search_tracks()[-1]["id"], genre="Поп", language="ru")
    for n in range(4):
        service.state.remember({
            "id": f"old-{n}", "title": f"Rock history {n}", "artist": "Кино", "genre": "Рок", "language": "ru",
        })
    service.state.like({"id": "old-0", "title": "Rock history 0", "artist": "Кино", "genre": "Русский рок"})
    service.queue.set_history(service.state.history)
    service.toggle_play()
    upcoming = service.upcoming()
    assert len(upcoming) == 8 and all(service.queue.is_unheard(track) for track in upcoming)
    prefix = [track["id"] for track in upcoming[:4]]
    service.pin_visible(prefix)

    try:
        wait_with_peer(service, info_hash, seed, lambda: bool(service.queue.recovery_ids()))
        assert index.calls[0] == "French chanson"
        [offered] = service.queue.recovery_ids()
        assert [track["id"] for track in service.upcoming()[:4]] == prefix
        assert offered in [track["id"] for track in service.upcoming()[4:]]

        service.next_track()
        assert service.current()["id"] == prefix[0]
        assert [track["id"] for track in service.upcoming()[:3]] == prefix[1:]
        wait_for(lambda: (service.tick() or service.is_ready(service.catalogue.get(offered))))
        track = service.catalogue.get(offered)
        assert track["genre_hint"] == "chanson"
        assert not track["genre"]
        assert track["language"] == "unknown"
    finally:
        seed.pause()


def test_stalled_prefetch_does_not_block_next_source(tmp_path, create_service):
    stopped, stopped_data, stopped_hash, _ = make_release(
        tmp_path, "Кино — Без пиров", "https://fixture.example/stopped", seed=False,
    )
    working, working_data, working_hash, seeder = make_release(
        tmp_path, "Кино — С пирами", "https://fixture.example/working",
    )
    index = IndexFixture([working], {stopped.source_url: stopped_data, working.source_url: working_data})
    service = create_service(index, PlayerProbe())
    entry = service.torrents.add_torrent_data(stopped_data)
    service._save_source(entry, {"kind": "file", "source_url": stopped.source_url}, False)
    service.toggle_play()
    assert service.waiting
    service.tick()
    assert service._pending_progress
    service._pending_progress = {
        info_hash: (already, time.monotonic() - 1)
        for info_hash, (already, _deadline) in service._pending_progress.items()
    }
    try:
        wait_with_peer(service, working_hash, seeder, service.is_playing)
        assert stopped_hash in service._failed_torrents
        assert service.current()["info_hash"] == working_hash
        assert all(row["id"].split(":")[0] != stopped_hash for row in service.state.history)
    finally:
        seeder.pause()


def test_album_transfer_progress_keeps_other_scheduled_tracks_queued(tmp_path, create_service):
    release, data, info_hash, _ = make_release(
        tmp_path, "Кино — Медленная загрузка", "https://fixture.example/slow", seed=False,
        filenames=[f"{n:02d}. Песня {n}.wav" for n in range(1, 11)],
    )
    service = create_service(IndexFixture([], {}), PlayerProbe())
    entry = service.torrents.add_torrent_data(data)
    service._save_source(entry, {"kind": "file", "source_url": release.source_url}, False)
    service.toggle_play()
    upcoming = service.upcoming()
    assert len(upcoming) == 8
    now = time.monotonic()
    service._pending_progress[info_hash] = ({track["id"]: 0 for track in upcoming}, now - 1)
    entry.files[upcoming[-1]["file_index"]]["downloaded"] = 16_384

    service._watch_pending_tracks(now)

    assert info_hash not in service._failed_torrents
    assert service._pending_progress[info_hash][1] > now
    assert len(service.upcoming()) == 8


def test_version_and_artist_need_evidence():
    live = CatalogRecording(_ONE.title, _ONE.artist, _ONE.mbid, disambiguation="live, 1989")
    assert recording_path_matches(_ONE, "Альбом/01. Песня 1.wav", "Кино — Сборник")
    assert not recording_path_matches(_ONE, "Альбом/01. Песня 1.wav", "Другой исполнитель")
    assert not recording_path_matches(live, "Альбом/01. Песня 1.wav", "Кино — студия 1989")
    assert recording_path_matches(live, "Альбом/01. Песня 1.wav", "Кино — Концерт 1989")
