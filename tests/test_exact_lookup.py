from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from radio_wave import catalog, torrent_index
from radio_wave.online_radio import matching_recording_paths, release_choices, search_choices
from radio_wave.torrent_index import TorrentRelease


EXACT = catalog.CatalogRecording("Мёртвая Алёна", "Nagart", "5fad0af1-2bcf-4644-b6e8-2d8e60ee0f2c")
TYPED = catalog.CatalogRecording("Алёна", "Nagart", "")


def release(name, seeders, provider="rutor"):
    return TorrentRelease(
        name, "magnet:?xt=urn:btih:" + "0" * 40, None, seeders,
        f"https://index.example/{name}", provider=provider,
    )


class Index:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search_audio(self, text):
        self.queries.append(text)
        return self.results.get(text, [])


def test_named_query_resolves_unique_canonical_title_and_tries_zero_seed_release_last(monkeypatch):
    monkeypatch.setattr(catalog, "search_recordings", lambda title, artist: [EXACT])
    wrong_album = release("Nagart — 13 этаж", 1)
    right_album = release("Nagart — Секреты оборотня", 0)
    right_discography = release("Nagart — Дискография", 0)
    index = Index({"Nagart": [right_album, wrong_album, right_discography]})

    choices = search_choices(index, "Nagart - Алёна", set())

    assert index.queries == ["Nagart Мёртвая Алёна", "Nagart"]
    assert [choice.release for choice in choices] == [wrong_album, right_album, right_discography]
    assert all(choice.recording == EXACT and not choice.allow_title_extension for choice in choices)
    assert matching_recording_paths(EXACT, ["Nagart - Мертвая Алёна.mp3"], right_album.title)


def test_only_explicit_named_lookup_relaxes_unknown_rutor_availability():
    unknown = release("Nagart — неизвестное число сидов", 0)
    jacred = release("Nagart — Jacred", 0, "jacred")
    index = Index({"Nagart": [unknown, jacred]})

    assert [choice.release for choice in release_choices(index, EXACT, set())] == [jacred]
    assert [choice.release for choice in release_choices(
        index, EXACT, set(), allow_unconfirmed=True,
    )] == [unknown, jacred]


def test_metadata_failure_yields_resolvable_artist_title_without_an_mbid(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise catalog.CatalogError("temporarily unavailable")

    monkeypatch.setattr(catalog, "search_recordings", unavailable)
    album = release("Nagart — Секреты оборотня", 0)
    [choice] = search_choices(Index({"Nagart": [album]}), "Nagart — Алёна", set())
    assert choice.recording == TYPED
    assert choice.allow_title_extension
    assert matching_recording_paths(
        choice.recording,
        ["01 - Запасной трек.mp3", "05 - Мёртвая Алёна.mp3"],
        album.title,
        allow_title_extension=choice.allow_title_extension,
    ) == [("05 - Мёртвая Алёна.mp3", catalog.CatalogRecording("Мёртвая Алёна", "Nagart", ""))]


def test_exact_filename_wins_over_extension_even_when_extension_comes_first():
    paths = ["Nagart - Мёртвая Алёна.mp3", "Nagart - Алёна.mp3"]
    assert matching_recording_paths(TYPED, paths, "Nagart", allow_title_extension=True) == [
        ("Nagart - Алёна.mp3", TYPED),
    ]


def test_expansion_rejects_unproven_artist_substring_versions_and_ambiguity():
    for paths, album in (
        (["Дельфин - Мёртвая Алёна.mp3"], "Nagart — Сборник"),
        (["05 - Мёртвая Алёна.mp3"], "Другой исполнитель"),
        (["Nagart - Мёртвая Алёнушка.mp3"], "Nagart"),
        (["Nagart - Мёртвая Алёна (live).mp3"], "Nagart"),
        (["Nagart - Мёртвая Алёна.mp3"], "Nagart — Концерт"),
        (["Nagart - Мёртвая Алёна.mp3", "Nagart - Привет Алёна.mp3"], "Nagart"),
    ):
        assert matching_recording_paths(TYPED, paths, album, allow_title_extension=True) == []


def test_ambiguous_musicbrainz_expansions_do_not_relax_file_matching(monkeypatch):
    other = catalog.CatalogRecording("Здравствуй, Алёна", "Nagart", "another")
    monkeypatch.setattr(catalog, "search_recordings", lambda title, artist: [EXACT, other])
    [choice] = search_choices(Index({"Nagart": [release("Nagart", 0)]}), "Nagart - Алёна", set())
    assert choice.recording == TYPED
    assert not choice.allow_title_extension


@pytest.mark.parametrize("first_failed", [False, True])
def test_exact_cancellation_after_running_request_skips_mirror_jacred_and_album_queries(monkeypatch, first_failed):
    started, proceed, cancelled = Event(), Event(), Event()
    called = []
    named = catalog.CatalogRecording(EXACT.title, EXACT.artist, EXACT.mbid, album_titles=("Альбом",))
    monkeypatch.setattr(catalog, "search_recordings", lambda *_args, **_kwargs: [named])

    def request(url, **_kwargs):
        called.append(url)
        started.set()
        assert proceed.wait(2)
        if first_failed:
            raise torrent_index.TorrentIndexError("provider unavailable")
        return b""

    monkeypatch.setattr(torrent_index, "_request", request)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(search_choices, torrent_index, "Nagart — Мёртвая Алёна", set(), cancelled=cancelled.is_set)
        try:
            assert started.wait(2)
            cancelled.set()
        finally:
            proceed.set()
        assert future.result(timeout=2) == []
    assert len(called) == 1
    assert called[0].startswith("https://rutor.info/search/")
