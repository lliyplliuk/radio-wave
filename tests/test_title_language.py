import pytest

from radio_wave.language import effective_language
from radio_wave.ranker import TasteProfile, rank
from radio_wave.state import AppState


@pytest.mark.parametrize(("title", "expected"), [
    ("Прекрасное далёко", "ru"),
    ("Кукушка", "ru"),
    ("Компромисс", "ru"),
    ("Звезда по имени Солнце (Live)", "ru"),
    ("Never Let Me Go", "en"),
    ("With or Without You", "en"),
    ("The End", "en"),
    ("My Hero", "en"),
    ("Casualty", "en"),
    ("Cut the Bridge", "en"),
    ("Another Life", "en"),
    ("Walk Of Life", "en"),
    ("Overflow", "en"),
    ("Wrathchild", "en"),
    ("Heavy Is the Crown", "en"),
    ("Over Each Other", "en"),
    ("Let You Fade", "en"),
    ("The Ides Of March", "en"),
    ("Думи мої", "other"),
    ("Bésame mucho", "other"),
    ("東京", "other"),
    ("Go", "unknown"),
    ("Hero", "unknown"),
    ("Zvezda po imeni Solntse", "unknown"),
    ("Nirvana", "unknown"),
    ("Requiem", "unknown"),
    ("Unshatter", "unknown"),
    ("Sto Let", "unknown"),
    ("Of", "unknown"),
    ("I", "unknown"),
    ("Океан", "unknown"),
    ("123", "unknown"),
    ("Любовь / Love", "unknown"),
])
def test_title_language_without_metadata(title, expected):
    code, confidence = effective_language({"title": title, "artist": "Английский ансамбль"})
    assert code == expected
    if expected != "unknown":
        assert 0 < confidence < 1
    else:
        assert confidence == 0


@pytest.mark.parametrize("source", ["manual", "tag", "musicbrainz", "bulk"])
def test_confirmed_language_takes_precedence_over_title(source):
    assert effective_language({
        "title": "Never Let Me Go", "language": "ru", "lang_source": source,
    }) == ("ru", 1)


def test_artist_name_does_not_determine_language():
    assert effective_language({"title": "1", "artist": "Я люблю этот мир"}) == ("unknown", 0)


def test_language_guess_is_not_saved_as_confirmed_feedback(tmp_path):
    path = tmp_path / "state.json"
    track = {"id": "song", "title": "Forever Alone", "language": "unknown"}
    state = AppState(str(path))
    state.dislike(track)
    assert track["language"] == "unknown"
    state = AppState(str(path))
    assert state.dislikes[0]["language"] == "en"
    assert state.dislikes[0]["lang_source"] == "title"

    confirmed = dict(track, language="en", lang_source="musicbrainz")
    assert state.update_feedback_language(confirmed)
    assert effective_language(state.dislikes[0]) == ("en", 1)


def test_title_has_weaker_learned_bias_than_confirmed_language():
    candidates = [
        {"id": "tagged", "title": "Ночь", "language": "ru"},
        {"id": "estimate", "title": "Долгая ночь"},
        {"id": "unknown", "title": "1"},
    ]
    liked = [{"id": "feedback", "language": "ru"}]
    scores = {item.id: item for item in rank(candidates, liked=liked)}

    assert scores["tagged"].language_bias > scores["estimate"].language_bias > 0
    assert scores["unknown"].language_bias == 0
    assert all(not item.excluded for item in scores.values())


def test_repeated_guess_based_dislikes_teach_english_weakly_without_affecting_unknown():
    guessed = [
        {"id": f"old-{i}", "title": f"Never Let Me Go {i}", "at": i}
        for i in range(20)
    ]
    confirmed = [dict(item, language="en") for item in guessed]
    candidates = [
        {"id": "tagged", "title": "123", "language": "en"},
        {"id": "estimate", "title": "Never Let Me Go"},
        {"id": "unknown", "title": "1"},
        guessed[0],
    ]
    uncertain = {item.id: item for item in rank(candidates, disliked=guessed)}
    certain = {item.id: item for item in rank(candidates, disliked=confirmed)}
    no_language = rank(candidates, disliked=guessed, profile=TasteProfile(language_weight=0))

    assert certain["tagged"].language_bias < uncertain["tagged"].language_bias < 0
    assert uncertain["tagged"].language_bias < uncertain["estimate"].language_bias < 0
    assert uncertain["unknown"].language_bias == 0
    assert uncertain[guessed[0]["id"]].excluded
    assert all(item.language_bias == 0 for item in no_language)
