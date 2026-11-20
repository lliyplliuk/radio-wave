"""Acquisition uses durable feedback when it asks for new sources."""

import pytest

from radio_wave import catalog
from radio_wave import online_radio
from radio_wave.online_radio import OnlineChoice, acquisition_taste, radio_choices
from radio_wave.ranker import TasteProfile, genre_suppressed, suppressed_genres
from radio_wave.torrent_index import TorrentRelease


class Index:
    def __init__(self, results, latest=()):
        self.results = results
        self.latest = list(latest)
        self.calls = []

    def search_audio(self, query):
        self.calls.append(query)
        return self.results.get(query, [])

    def latest_audio(self):
        self.calls.append("latest")
        return self.latest


def release(title, seeds):
    key = title.split()[0]
    return TorrentRelease(title, None, "https://fixture.example/download/" + key, seeds, "https://fixture.example/" + key)


def feedback(genre, name, count, language="ru"):
    return [
        {"id": f"{genre}:{name}:{index}", "genre": genre, "artist": name, "language": language, "at": index}
        for index in range(count)
    ]


def next_sources(index, *, favourites=(), taste=None, rotation=1, language_target=None, genre_target=None, artist_fallback=None):
    return radio_choices(
        index, favourites, set(), set(), set(), rotation, lambda: False,
        taste=taste, language_target=language_target, genre_target=genre_target, artist_fallback=artist_fallback,
    )


def test_rock_target_searches_liked_artist_before_popularity(monkeypatch):
    likes = feedback("RusRock", "Кино", 9) + feedback("Alternative Rock", "Кино", 4) + feedback("Поп", "Поп-группа", 2)
    taste = acquisition_taste(likes, genre_weight=0.8)
    rock = release("Кино — Проверенный альбом [FLAC]", 9)
    pop = release("Поп — Хиты сезона", 90)
    index = Index({"Кино": [rock]}, [pop])
    monkeypatch.setattr(catalog, "search_recordings", lambda *_args, **_kwargs: [])

    [choice] = next_sources(index, favourites=likes, taste=taste, genre_target="rock", language_target="ru")

    assert choice.release == rock
    assert choice.genre_hint == "rock" and choice.offer
    assert index.calls == ["Кино"]


def test_foreign_target_preserves_positive_rock_preference():
    likes = feedback("Рок", "Кино", 13)
    foreign = release("Nirvana — Nevermind", 6)
    local = release("Кино — Альбом", 9)
    index = Index({"Nirvana": [foreign], "Кино": [local]})

    taste = acquisition_taste(likes, genre_weight=0.8)
    [overseas] = next_sources(index, taste=taste, rotation=5, language_target="foreign", genre_target="rock")
    assert overseas.release == foreign
    assert overseas.genre_hint == "rock"
    assert index.calls == ["Nirvana"]

    index.calls.clear()
    [domestic] = next_sources(index, taste=taste, rotation=5, language_target="ru", genre_target="rock")
    assert domestic.release == local
    assert index.calls == ["Кино"]


def test_strong_chanson_dislikes_avoid_marked_releases_only_when_alternatives_exist():
    dislikes = feedback("Шансон", "Сборник", 11, language="unknown")
    genres = suppressed_genres([], dislikes, TasteProfile())
    assert "chanson" in genres
    taste = acquisition_taste([], genre_weight=0.8, avoided_genres=genres)
    chanson = release("Шансон — Большой хит", 80)
    other = release("Другой сборник — Новинки", 2)

    choices = next_sources(Index({}, [chanson, other]), taste=taste)
    assert [choice.release for choice in choices] == [other]
    choices = next_sources(Index({}, [chanson]), taste=taste)
    assert [choice.release for choice in choices] == [chanson]


def test_no_feedback_leaves_russian_startup_preference():
    domestic = release("Кино — Хиты", 8)
    foreign = release("Nirvana — Nevermind", 80)
    sources = next_sources(Index({}, [foreign, domestic]))

    assert sources[0].release == domestic


def test_russian_chanson_dislikes_do_not_veto_french_or_unknown_release_language():
    dislikes = feedback("Шансон", "Артист", 18, language="ru")
    profile = TasteProfile()
    taste = acquisition_taste(
        [], genre_weight=profile.genre_weight, avoided_genres=suppressed_genres([], dislikes, profile),
        genre_denied=genre_suppressed([], dislikes, profile),
    )
    russian = release("Русский шансон — Хиты", 90)
    assert [choice.release for choice in next_sources(Index({}, [russian]), taste=taste)] == [russian]
    for compatible in (
        release("French chanson — Best of", 5),
        release("Chanson — Best of", 5),
        release("Joe Dassin – Chanson (Russian Edition)", 5),
    ):
        assert [choice.release for choice in next_sources(Index({}, [russian, compatible]), taste=taste)] == [compatible]


@pytest.mark.parametrize(("side", "genre", "query"), [
    ("ru", "pop", "Русский поп"), ("foreign", "pop", "Pop"),
    ("ru", "chanson", "Русский шансон"), ("foreign", "chanson", "French chanson"),
    ("ru", "jazz", "Джаз"), ("foreign", "jazz", "Jazz"),
])
def test_genre_source_requires_evidence_and_one_rock_like_does_not_preempt_it(side, genre, query):
    neutral = release("Unlabeled — Album", 100)
    desired = release(f"{query} — Альбом", 4)
    index = Index({query: [neutral, desired, release("Iggy Pop — Discography", 100)]})
    taste = acquisition_taste(feedback("Rock", "Кино", 1), genre_weight=0.8)
    [choice] = next_sources(index, taste=taste, language_target=side, genre_target=genre)
    assert index.calls == [query]
    assert choice.release == desired and choice.genre_hint == genre and choice.language_target == side


def test_russian_rock_sources_rotate_away_from_recent_and_queued_kino():
    names = ["Кино", "Алиса", "Машина времени", "Сектор Газа"]
    results = {name: [release(f"{name} [Дискография]", 5), release(f"Каверы — {name} (AI)", 100)] for name in names}
    mixed_case = {"КИНО": 12}
    selected = []
    for name in names[1:]:
        index = Index(results)
        taste = acquisition_taste(feedback("Rock", "Кино", 8), genre_weight=0.8, artist_exposure=mixed_case)
        [choice] = next_sources(index, taste=taste, rotation=0, language_target="ru", genre_target="rock")
        assert index.calls == [name]
        assert choice.release == results[name][0]
        selected.append(name)
        mixed_case[name] = 6
    assert selected == names[1:]


def test_empty_artist_only_search_falls_through_to_ordinary_index():
    normal = release("Unlabeled — Playlist", 8)
    index = Index({}, [normal])
    [choice] = next_sources(index, rotation=0, artist_fallback=("rock", "ru"))
    assert index.calls[:2] == ["Кино", "latest"]
    assert choice.release == normal and not choice.genre_hint and not choice.language_target


@pytest.mark.parametrize("available", [True, False])
def test_artist_rotation_gets_one_search_before_similar_recording_by_a_repeat(monkeypatch, available):
    repeated = catalog.CatalogRecording("Солнечная песня", "Кино", "kino-recording")
    familiar = release("Кино — Другой альбом", 12)
    fresh = release("Алиса — Новый альбом", 6)
    index = Index({"Алиса": [fresh] if available else []})
    taste = acquisition_taste([], genre_weight=0.8, artist_exposure={"Кино": 5})
    searched = []

    def resolve(_index, recording, *_args, **_kwargs):
        searched.append(recording.artist)
        return [OnlineChoice(familiar, recording=recording)]

    monkeypatch.setattr(online_radio, "release_choices", resolve)
    [choice] = radio_choices(
        index, (), set(), set(), set(), 0, lambda: False, suggestions=[repeated],
        taste=taste, language_target="ru", artist_fallback=("rock", "ru"),
    )

    assert index.calls == ["Алиса"]
    assert choice.language_target == "ru"
    assert choice.release == (fresh if available else familiar)
    assert searched == ([] if available else ["Кино"])
