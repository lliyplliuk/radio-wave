from radio_wave import catalog
from radio_wave.online_radio import (
    cached_recording_matches, cached_recording_order, matching_recording_paths,
    matching_title_paths, search_choices, studio_first_paths,
)
from radio_wave.queue import RadioQueue
from radio_wave.ranker import rank
from radio_wave.recording_version import is_live_path, is_live_track
from radio_wave.torrent_index import TorrentRelease


def test_studio_first_within_mixed_release_and_live_fallback():
    live = "Deluxe/Disc 2/01 - Linkin Park - Two Faced (Live).mp3"
    studio = "Deluxe/Disc 1/01 - Linkin Park - Two Faced.mp3"
    album = "Linkin Park — From Zero (2CD Deluxe Edition)"
    typed = catalog.CatalogRecording("Two Faced", "Linkin Park", "")
    verified = catalog.CatalogRecording("Two Faced", "Linkin Park", "studio-id")

    assert studio_first_paths([live, studio], album) == [studio, live]
    assert matching_title_paths("Two Faced", [live, studio], album) == [studio, live]
    assert matching_title_paths("Two Faced (Live)", [studio, live], album) == [live]
    assert [path for path, _recording in matching_recording_paths(typed, [live, studio], album)] == [studio, live]
    assert matching_recording_paths(verified, [live, studio], album) == [(studio, verified)]
    assert matching_recording_paths(verified, [live], album) == []
    assert matching_recording_paths(typed, [live], album) == [
        (live, catalog.CatalogRecording("Two Faced (Live)", "Linkin Park", "")),
    ]


def test_recording_version_honors_explicit_venue_year_and_artist():
    named = catalog.CatalogRecording("Песня", "Кино", "live-id", disambiguation="live, 1989")
    path = "01 - Кино - Песня.mp3"
    assert matching_recording_paths(named, [path], "Кино — Концерт 1989") == [(path, named)]
    assert matching_recording_paths(named, [path], "Кино — Концерт 1990") == []
    assert matching_recording_paths(named, [path], "Кино — Студия 1989") == []
    qualified = "Drive Me Wild (Live at Wembley)"
    paths = ["Foo Fighters - Drive Me Wild (Live at Club).mp3", f"Foo Fighters - {qualified}.mp3"]
    assert matching_title_paths(qualified, paths, "Foo Fighters") == paths[1:]
    assert matching_title_paths("Foo Fighters", paths, "Foo Fighters") == []


def test_title_and_album_evidence_avoid_common_false_positives():
    for title in ("Live Forever", "Live and Let Die", "Song (Remastered)", "Live in the Moment", "I Live in the City"):
        assert not is_live_path(f"Deluxe/{title}.mp3", "Artist — Album (Remastered)")
        assert not is_live_track({"title": title})
    for path, album in (
        ("Drive Me Wild (BBC Live).mp3", "BBC Rarities"),
        ("Two Faced (Live) [FLAC].mp3", "Mixed release"),
        ("Song Live at Wembley.mp3", "Album"),
        ("Live in London/01 - Song.mp3", "Best Of"),
        ("01 - Песня.mp3", "Кино — Концерт 1989"),
        ("01 - Песня (живой звук).mp3", "Сборник"),
        ("01 - Song.mp3", "Artist - Live at Wembley [FLAC]"),
    ):
        assert is_live_path(path, album)


def test_seeded_studio_source_beats_live_and_live_is_explicitly_selectable(monkeypatch):
    def release(title, seeders):
        return TorrentRelease(title, "magnet", None, seeders, f"https://index.example/{title}")

    live = release("Linkin Park — Two Faced (Live)", 10)
    studio = release("Linkin Park — Two Faced", 2)
    unseeded = release("Linkin Park — Two Faced (Remastered)", 0)

    class Index:
        def search_audio(self, _query):
            return [live, studio, unseeded]

    monkeypatch.setattr(catalog, "search_recordings", lambda *_args, **_kwargs: [])
    plain = search_choices(Index(), "Two Faced", set())
    named = search_choices(Index(), "Linkin Park - Two Faced", set())
    explicit = search_choices(Index(), "Linkin Park - Two Faced (Live)", set())
    assert [choice.release for choice in plain] == [studio, live]
    assert [choice.release for choice in named][:2] == [studio, live]
    assert explicit[0].release == live


def test_inferred_text_id_can_fall_back_to_live_without_adopting_studio_id(monkeypatch):
    recording = catalog.CatalogRecording("Song", "Artist", "studio-id")
    monkeypatch.setattr(catalog, "search_recordings", lambda *_args, **_kwargs: [recording])
    release = TorrentRelease("Artist - Song (Live)", "magnet", None, 4, "https://index.example/live")

    class Index:
        def search_audio(self, _query):
            return [release]

    path = "Artist - Song (Live) [FLAC].mp3"
    for query in ("Artist - Song", "Song"):
        [choice] = search_choices(Index(), query, set())
        assert choice.recording == recording and choice.allow_live_fallback
        assert matching_recording_paths(
            choice.recording, [path], release.title, allow_live_fallback=choice.allow_live_fallback,
        ) == [(path, catalog.CatalogRecording("Song (Live)", "Artist", ""))]
    assert matching_recording_paths(recording, [path], release.title) == []


def test_cached_recording_matches_exact_version_and_prioritizes_exact_id():
    studio = {"artist": "Linkin Park", "title": "Two Faced"}
    exact = dict(studio, mbid="studio-id")
    live = dict(studio, live_recording=True)
    explicit_live = catalog.CatalogRecording("Two Faced", "Linkin Park", "live-id", disambiguation="live")
    undisambiguated = catalog.CatalogRecording("Two Faced", "Linkin Park", "live-id")
    assert not cached_recording_matches(explicit_live, studio)
    assert cached_recording_matches(explicit_live, dict(live, mbid="live-id"))
    assert cached_recording_matches(undisambiguated, dict(live, mbid="live-id"))
    assert not cached_recording_matches(undisambiguated, dict(live, mbid="different-id"))
    recording = catalog.CatalogRecording("Two Faced", "Linkin Park", "studio-id")
    compatible = [track for track in (live, studio, exact) if cached_recording_matches(recording, track)]
    assert sorted(compatible, key=lambda track: cached_recording_order(recording, track)) == [exact, studio]


def test_wave_prefers_comparable_studio_and_keeps_live_available():
    live = {"id": "a", "artist": "Linkin Park", "title": "Two Faced (Live)", "genre": "Rock"}
    studio = dict(live, id="z", title="Two Faced")
    file_evidence = dict(live, id="b", title="Drive Me Wild", live_recording=True)
    results = rank([live, studio, file_evidence])
    assert [item.id for item in results] == ["z", "a", "b"]
    assert all(item.is_live and not item.excluded and "концертная запись" in item.reason for item in results[1:])
    queue = RadioQueue()
    queue.set_catalogue([live, studio])
    assert queue.next_track()["id"] == studio["id"]
    assert queue.next_track()["id"] == live["id"]
    queue = RadioQueue()
    queue.set_catalogue([live, dict(studio, ready=False)])
    assert queue.next_track()["id"] == live["id"]
