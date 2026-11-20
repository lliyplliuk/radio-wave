"""Recovery of Russian repertoire without disturbing the published queue."""

import time

import pytest

from radio_wave.config import QUEUE_LOOKAHEAD
from radio_wave.language import effective_language
from test_auto_radio import IndexFixture, create_service, make_release, wait_with_peer
from test_torrent_service_flow import PlayerProbe, wait_for, wav


def seed_recent_history(service):
    for n in range(5):
        service.state.remember({"id": f"past-{n}", "title": f"XQ{n}", "language": "unknown"})
    service.state.like({"id": "liked-rock", "artist": "Кино", "genre": "Рок", "title": "Кукушка", "language": "ru"})
    service.queue.set_history(service.state.history)


def test_one_ready_russian_song_does_not_suppress_background_search(create_service, monkeypatch):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    seed_recent_history(service)
    service.state.remember({"id": "last-ru", "title": "Группа крови", "language": "ru"})
    service.queue.set_history(service.state.history)
    service.queue.set_catalogue([
        {"id": "ready-ru", "title": "Кукушка", "language": "ru", "ready": True},
        *({"id": f"en-{n}", "title": f"English {n}", "language": "en", "ready": True} for n in range(8)),
    ])
    calls = []
    monkeypatch.setattr(service, "_lookup_online", lambda **kwargs: calls.append(kwargs))
    service._radio_active = True

    assert service._missing_language() == "ru"
    service._maybe_prefetch()
    assert len(calls) == 1 and calls[0]["prefetch"] and calls[0]["language_target"] == "ru"


def test_full_visible_queue_and_pending_foreign_offer_still_trigger_russian_acquisition(tmp_path, create_service):
    domestic, domestic_data, domestic_hash, seeder = make_release(
        tmp_path, "Кино — Лучшие песни", "https://fixture.example/domestic",
        filenames=["01. Intro.wav", "02. Группа крови.wav"],
    )
    overseas, overseas_data, _overseas_hash, _unused = make_release(
        tmp_path, "Nirvana — Nevermind", "https://fixture.example/overseas", seed=False,
        filenames=["Come As You Are.wav"],
    )
    index = IndexFixture([domestic, overseas], {domestic.source_url: domestic_data, overseas.source_url: overseas_data})
    service = create_service(index, PlayerProbe())
    for n in range(QUEUE_LOOKAHEAD + 2):
        path = tmp_path / f"XQ{n}.wav"
        path.write_bytes(wav(n + 100))
        service.import_paths([str(path)])
        service.update_track(
            service.search_tracks()[-1]["id"], genre="Rock", language="en" if n % 2 else "unknown",
        )
    seed_recent_history(service)
    foreign_entry = service.torrents.add_torrent_data(overseas_data)
    service._save_source(foreign_entry, {
        "kind": "file", "source_url": overseas.source_url, "automatic": True, "genre_hint": "rock",
    }, False)
    offered = f"{foreign_entry.info_hash}:0"
    assert service.queue.offer_discovery(offered)

    service.toggle_play()
    prefix = tuple(track["id"] for track in service.upcoming()[:4])
    assert len(service.upcoming()) == QUEUE_LOOKAHEAD
    assert service.queue.offered_id() == offered
    service.pin_visible(prefix)
    try:
        service.tick()
        assert service._online is not None, "an offered foreign track must not block Russian recovery"
        wait_with_peer(
            service, domestic_hash, seeder,
            lambda: service.queue.recovery_id() is not None,
        )
        recovered = service.catalogue.get(service.queue.recovery_id())
        assert recovered["info_hash"] == domestic_hash
        assert recovered["file_index"] == 1
        assert recovered["language"] == "unknown"
        assert not recovered["lang_source"]
        assert effective_language(recovered)[0] == "ru"
        assert "Кино" in index.calls and "Nirvana" not in index.calls
        assert tuple(track["id"] for track in service.upcoming()[:4]) == prefix
        assert service.queue.offered_id() == offered
        assert len(service.upcoming()) == QUEUE_LOOKAHEAD
    finally:
        seeder.pause()


def test_foreground_recovery_retains_one_normal_fallback_after_missing_title_and_timeout(tmp_path, create_service):
    from dataclasses import replace

    missing, missing_data, _missing_hash, _unused = make_release(
        tmp_path, "Кино — Без распознанных названий", "https://fixture.example/missing", seed=False,
        filenames=["01. Intro.wav"],
    )
    dead, dead_data, dead_hash, _unused = make_release(
        tmp_path, "Кино — Без раздающих", "https://fixture.example/dead", seed=False,
        filenames=["01. Группа крови.wav"],
    )
    third, third_data, third_hash, _unused = make_release(
        tmp_path, "Кино — Третий кандидат", "https://fixture.example/third", seed=False,
        filenames=["01. Кукушка.wav"],
    )
    normal, normal_data, normal_hash, seeder = make_release(
        tmp_path, "Nirvana — Normal fallback", "https://fixture.example/normal",
        filenames=["01. Come As You Are.wav"],
    )
    missing = replace(missing, seeders=50)
    dead = replace(dead, seeders=40)
    index = IndexFixture([missing, dead, third, normal], {
        missing.source_url: missing_data, dead.source_url: dead_data,
        third.source_url: third_data, normal.source_url: normal_data,
    })
    service = create_service(index, PlayerProbe())
    seed_recent_history(service)

    service.toggle_play()
    assert service._online is not None and service._online.language_target == "ru"
    connected = False

    def progress():
        nonlocal connected
        service.tick()
        request = service._online
        if request and request.entry and request.entry.info_hash == dead_hash and request.track_id:
            request.deadline = time.monotonic() - 1
            service._poll_online()
        entry = service.torrents.get(normal_hash)
        if entry is not None and not connected:
            entry.handle.connect_peer(("127.0.0.1", seeder.listen_port()))
            connected = True
        return service.is_playing()

    try:
        wait_for(progress)
        assert service.current()["info_hash"] == normal_hash
        assert ("ru", missing.source_url) in service._language_mismatches
        assert missing.source_url not in service._sources_to_skip(include_saved=False)
        assert dead.source_url in service._failed_releases
        assert service.torrents.get(third_hash) is None
    finally:
        seeder.pause()


@pytest.mark.parametrize(("disabled", "disliked_side", "expected"), [
    (True, None, None), (False, "ru", "foreign"), (False, "foreign", "ru"),
])
def test_language_refill_respects_zero_weight_and_strong_negative(create_service, disabled, disliked_side, expected):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    seed_recent_history(service)
    if disabled:
        service.set_taste(language_weight=0)
    else:
        for n in range(18):
            language = "ru" if disliked_side == "ru" else ("en" if n % 2 else "other")
            service.state.dislike({"id": f"dislike-{n}", "language": language})
        service.queue.fill(service.state.likes, service.state.dislikes)
    assert service._missing_language() == expected


@pytest.mark.parametrize(
    ("supply", "target", "artist", "filenames"),
    [
        ("ru", "foreign", "Sade", ["01. Группа крови.wav", "02. Wonderful World.wav"]),
        ("en", "ru", "МакSим", ["01. Wonderful World.wav", "02. Кукушка.wav"]),
    ],
)
def test_full_single_side_stock_refills_the_other_using_nonrock_liked_artist(
    tmp_path, create_service, supply, target, artist, filenames,
):
    release, data, info_hash, seeder = make_release(tmp_path, f"{artist} — Album", "https://fixture.example/target",
                                                 filenames=filenames)
    index = IndexFixture([release], {release.source_url: data})
    service = create_service(index, PlayerProbe())
    try:
        for n in range(QUEUE_LOOKAHEAD + 1):
            path = tmp_path / f"Stock {n}.wav"
            path.write_bytes(wav(n + 12))
            service.import_paths([str(path)])
            service.update_track(service.search_tracks()[-1]["id"], language=supply)
        service.state.like({"id": "favourite", "artist": artist, "title": "Liked", "genre": "Pop",
                            "language": "other" if target == "foreign" else "ru"})
        service.queue.fill(service.state.likes, service.state.dislikes)
        service.toggle_play()
        prefix = [track["id"] for track in service.upcoming()[:4]]
        service.pin_visible(prefix)
        assert len(service.upcoming()) == QUEUE_LOOKAHEAD
        wait_with_peer(service, info_hash, seeder, lambda: f"{info_hash}:1" in service.queue.recovery_ids())
        assert index.calls[0] == artist
        track = service.catalogue.get(f"{info_hash}:1")
        assert track["language"] == "unknown" and not track["lang_source"] and not track["genre"]
        assert [track["id"] for track in service.upcoming()[:4]] == prefix
    finally:
        seeder.pause()


def test_cold_radio_searches_both_sides_without_any_feedback(tmp_path, create_service):
    domestic, domestic_data, domestic_id, seeder_ru = make_release(
        tmp_path, "Кино — Треки", "https://fixture.example/cold-ru", filenames=["Кукушка.wav", "Группа крови.wav"],
    )
    other, other_data, foreign_id, seeder_foreign = make_release(
        tmp_path, "ABBA — Tracks", "https://fixture.example/cold-overseas", filenames=["Wonderful World.wav"],
    )
    index = IndexFixture([domestic, other], {domestic.source_url: domestic_data, other.source_url: other_data})
    service = create_service(index, PlayerProbe())
    try:
        service.toggle_play()
        wait_with_peer(service, domestic_id, seeder_ru, lambda: service.is_playing() and bool(service.state.history))
        assert service.current()["info_hash"] == domestic_id
        wait_with_peer(service, foreign_id, seeder_foreign, lambda: service.catalogue.get(f"{foreign_id}:0") is not None)
        assert "Кино" in index.calls and "ABBA" in index.calls
        assert not service.state.likes
    finally:
        seeder_ru.pause()
        seeder_foreign.pause()


def test_foreground_plays_a_usable_release_with_unknown_title_when_language_is_unavailable(tmp_path, create_service):
    release, data, info_hash, seeder = make_release(
        tmp_path, "Кино — Метаданные", "https://fixture.example/unknown", filenames=["QX 1.wav"],
    )
    service = create_service(IndexFixture([release], {release.source_url: data}), PlayerProbe())
    try:
        service.toggle_play()
        # Connect only to the ordinary fallback, which may reuse the release
        # after the directed attempt has already checked its metadata.
        wait_for(lambda: (service.tick() or (service._online and service._online.attempts == 2)))
        wait_with_peer(service, info_hash, seeder, service.is_playing)
        assert effective_language(service.current()) == ("unknown", 0.0)
        assert release.source_url not in service._failed_releases
    finally:
        seeder.pause()


def test_side_cooldown_allows_the_other_search_at_same_history_position(create_service):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    service._radio_active = True
    service._maybe_prefetch()
    assert service._online.language_target == "ru"
    wait_for(lambda: (service._poll_online() or service._online is None))
    service._maybe_prefetch()
    assert service._online.language_target == "foreign"
    wait_for(lambda: (service._poll_online() or service._online is None))
    service._maybe_prefetch()
    assert service._online is None


def test_full_kino_stock_can_add_alisa_when_genre_and_language_queries_are_empty(tmp_path, create_service):
    release, data, info_hash, seeder = make_release(
        tmp_path, "Алиса [Дискография]", "https://fixture.example/alisa", filenames=["Свет.wav"],
    )
    index = IndexFixture([release], {release.source_url: data})
    service = create_service(index, PlayerProbe())
    try:
        for n in range(QUEUE_LOOKAHEAD + 1):
            path = tmp_path / f"Трек {n}.wav"
            path.write_bytes(wav(n + 80))
            service.import_paths([str(path)])
            service.update_track(service.search_tracks()[-1]["id"], artist="Кино", language="ru", genre="Рок")
        service.toggle_play()
        shown = [track["id"] for track in service.upcoming()[:4]]
        service.pin_visible(shown)
        assert len(service.upcoming()) == QUEUE_LOOKAHEAD
        wait_with_peer(service, info_hash, seeder, lambda: service.queue.offered_id() == f"{info_hash}:0")
        assert "Алиса" in index.calls and "Кино" not in index.calls
        assert [track["id"] for track in service.upcoming()[:4]] == shown
        assert service.catalogue.get(f"{info_hash}:0")["genre_hint"] == "rock"
    finally:
        seeder.pause()


def test_concentrated_artist_refill_respects_strong_language_dislikes(create_service):
    service = create_service(IndexFixture([], {}), PlayerProbe())
    tracks = [{"id": str(n), "artist": "Кино", "title": f"Песня {n}", "language": "ru", "genre": "Рок"}
              for n in range(QUEUE_LOOKAHEAD)]
    service.queue.set_catalogue(tracks)
    assert service._concentrated_artist_stock() == ("rock", "ru")

    for n in range(18):
        service.state.dislike({"id": f"rejected-{n}", "language": "ru"})
    service.queue.fill(service.state.likes, service.state.dislikes)
    assert len(service.queue.upcoming) == QUEUE_LOOKAHEAD
    assert service._concentrated_artist_stock() is None


def test_full_automatic_ab_stock_searches_a_new_artist_across_split_genres(create_service):
    from radio_wave.torrent_index import TorrentRelease

    titles = ["Алиса — Новый альбом", "Русский шансон — Сборник", "Pop — New anthology", "Jazz — Selected works"]
    releases = [TorrentRelease(title, "magnet", None, 5, f"https://fixture.example/{n}")
                for n, title in enumerate(titles)]
    index = IndexFixture(releases, {})
    player = PlayerProbe()
    service = create_service(index, player)
    tracks = []
    for n, (artist, language, genre) in enumerate([
        ("Кино", "ru", "Rock"), ("Adele", "en", "Pop"),
        ("Кино", "ru", "Chanson"), ("Adele", "en", "Jazz"),
    ] * 2):
        source = "a" * 40 if artist == "Кино" else "b" * 40
        tracks.append({"id": f"{source}:{n}", "title": f"Track {n}", "source": "torrent", "info_hash": source,
                       "artist": artist, "genre": genre, "language": language, "file_index": n})
    service.state.data["torrent_sources"] = [
        {"info_hash": source, "source_url": f"https://fixture.example/old-{n}", "automatic": True}
        for n, source in enumerate(("a" * 40, "b" * 40))
    ]
    service.queue.set_catalogue(tracks)
    player.playing = service._radio_active = True
    assert len(service.queue.upcoming) == QUEUE_LOOKAHEAD
    assert service._missing_language() is None and service._missing_genre(None) is None
    service._maybe_prefetch()
    assert service._online is not None and service._online.prefetch
    [choice] = service._online.future.result(timeout=2)
    assert choice.release in releases
    assert len(index.calls) == 1 and index.calls[0] not in {"Кино", "Adele"}


def test_torrent_completion_keeps_exact_tag_and_manual_language_priority(tmp_path, create_service, monkeypatch):
    from radio_wave import tags

    release, data, info_hash, seeder = make_release(
        tmp_path, "Album", "https://fixture.example/exact-tag", filenames=["Wonderful World.wav"],
    )
    service = create_service(IndexFixture([release], {release.source_url: data}), PlayerProbe())
    monkeypatch.setattr(tags, "read_tags", lambda _path: {"language": "other", "language_code": "fra"})
    try:
        service.play_release(release)
        wait_with_peer(service, info_hash, seeder, service.is_playing)
        track = service.catalogue.get(f"{info_hash}:0")
        assert (track["language"], track["language_code"], track["lang_source"]) == ("other", "fr", "tag")
        track.update(language="other", language_code="de", lang_source="musicbrainz")
        entry = service.torrents.get(info_hash)
        service._on_file_complete(info_hash, entry, 0)
        assert track["language_code"] == "fr" and track["lang_source"] == "tag"
        service.update_track(track["id"], language="en")
        service._on_file_complete(info_hash, entry, 0)
        assert (track["language"], track["language_code"], track["lang_source"]) == ("en", "en", "manual")
    finally:
        seeder.pause()
