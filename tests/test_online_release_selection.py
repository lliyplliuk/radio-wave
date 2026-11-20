"""Exact song search uses known albums before generic artist releases."""

from radio_wave import catalog
from radio_wave.catalog import CatalogRecording
from radio_wave.online_radio import matching_recording_paths, release_choices, search_choices
from radio_wave.torrent_index import TorrentRelease


class IndexProbe:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search_audio(self, query):
        self.queries.append(query)
        releases = self.results.get(query, [])
        if isinstance(releases, Exception):
            raise releases
        return releases


def release(title, key, seeders):
    return TorrentRelease(
        title, None, "https://fixture.example/download/" + key, seeders,
        "https://fixture.example/torrent/" + key,
    )


def recording(title, *albums):
    return CatalogRecording(title, "Nagart", "", album_titles=albums)


def test_verified_album_cannot_be_crowded_out_by_three_high_seed_search_hits():
    flac = release("Nagart — 13 этаж [FLAC]", "flac", 80)
    mp3 = release("Nagart — 13 этаж [MP3 320kbps]", "mp3", 70)
    unrelated = release("Nagart — Секреты оборотня", "unrelated", 60)
    verified_album = release("Nagart — Vol.3 (2024) FLAC", "album", 2)
    record = recording("Шкатулка", "Vol. 3")
    index = IndexProbe({
        "Nagart Шкатулка": [flac, mp3, unrelated],
        "Nagart Vol. 3": [verified_album],
    })

    choices = release_choices(index, record, set(), allow_unconfirmed=True)

    assert [choice.release for choice in choices] == [verified_album, flac, unrelated]
    assert index.queries == ["Nagart Шкатулка", "Nagart Vol. 3"]
    assert matching_recording_paths(record, ["Vol. 3/06. Шкатулка.flac"], verified_album.title)
    assert not matching_recording_paths(record, ["13 этаж/03. Не сейчас.flac"], flac.title)


def test_confirmed_pugalo_album_precedes_generic_seedless_discography():
    disk = release("Nagart — Дискография", "discography", 0)
    album = release("Nagart — Вы это слышали? [MP3]", "album", 3)
    record = recording("Пугало", "Вы это слышали?")
    index = IndexProbe({"Nagart Пугало": [disk], "Nagart Вы это слышали?": [album]})

    choices = release_choices(index, record, set(), allow_unconfirmed=True)

    assert [choice.release for choice in choices] == [album, disk]
    assert matching_recording_paths(record, ["07. Пугало.mp3"], album.title)


def test_unavailable_album_lookup_does_not_discard_viable_exact_result():
    exact = release("Nagart — Шкатулка", "single", 5)
    index = IndexProbe({
        "Nagart Шкатулка": [exact],
        "Nagart Vol. 3": RuntimeError("lookup timed out"),
    })

    assert [item.release for item in release_choices(index, recording("Шкатулка", "Vol. 3"), set())] == [exact]


def test_ambiguous_exact_records_still_supply_verified_album_hints(monkeypatch):
    single = CatalogRecording("Шкатулка", "Nagart", "single", album_titles=("Шкатулка",))
    album_copy = CatalogRecording("Шкатулка", "Nagart", "album", album_titles=("Vol. 3",))
    monkeypatch.setattr(catalog, "search_recordings", lambda *_args, **_kwargs: [single, album_copy])
    album = release("Nagart — Vol.3", "target", 1)
    index = IndexProbe({"Nagart Vol. 3": [album]})

    [choice] = search_choices(index, "Nagart — Шкатулка", set())

    assert choice.release == album
    assert choice.recording.mbid == ""
    assert choice.recording.album_titles == ("Шкатулка", "Vol. 3")
    assert not choice.allow_title_extension
