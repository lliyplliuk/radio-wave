import json

from radio_wave.queue import RadioQueue
from radio_wave.ranker import TasteProfile, rank
from radio_wave.state import AppState
from radio_wave.tags import normalize_language


def track(track_id, language):
    return {"id": track_id, "language": language, "genre": "рок"}


def english_dislikes(count):
    return [dict(track(f"disliked-{number}", "en"), at=number) for number in range(count)]


def by_id(tracks, **kwargs):
    return {item.id: item for item in rank(tracks, **kwargs)}


def test_vocal_language_codes_and_ambiguous_values():
    assert [normalize_language(value) for value in [
        "ru-RU", "rus", "en_GB", "eng", "English", "deu", "other",
        "", "und", "mul", "rus/eng", "rus\0eng",
    ]] == ["ru", "ru", "en", "en", "en", "other", "other",
          "unknown", "unknown", "unknown", "unknown", "unknown"]


def test_default_language_bias_is_neutral():
    scores = by_id([track(lang, lang) for lang in ("ru", "en", "other", "unknown")])
    assert all(not item.excluded for item in scores.values())
    assert all(item.language_bias == 0 for item in scores.values())


def test_english_dislikes_are_specific_and_zero_weight_disables_language_signal():
    rejected = english_dislikes(17)
    candidates = [track(lang, lang) for lang in ("ru", "en", "other", "unknown")] + [rejected[0]]
    scores = by_id(candidates, disliked=rejected)
    assert scores["en"].language_bias < -1.5
    assert scores["other"].language_bias == scores["unknown"].language_bias == 0
    assert scores[rejected[0]["id"]].excluded

    no_language_taste = by_id(candidates, disliked=rejected, profile=TasteProfile(language_weight=0))
    assert all(item.language_bias == 0 for item in no_language_taste.values())
    assert no_language_taste[rejected[0]["id"]].excluded


def test_positive_feedback_recovers_english_without_teaching_unknown():
    rejected = english_dislikes(18)
    candidates = [track("en", "en"), track("unknown", "unknown")]
    before = by_id(candidates, disliked=rejected)
    completed = [dict(track("finished", "eng"), at=19)]
    afterward = by_id(candidates, disliked=rejected, finished_listens=completed)
    likes = [dict(track(f"liked-{n}", "en"), at=n) for n in range(20, 24)]
    likes.append(dict(track("unknown-like", "unknown"), at=24))
    recovered = by_id(candidates, disliked=rejected, liked=likes, finished_listens=completed)

    assert before["en"].language_bias < afterward["en"].language_bias < recovered["en"].language_bias
    assert recovered["en"].language_bias > -0.5
    assert recovered["unknown"].language_bias == 0


def test_fifteen_consecutive_english_dislikes_outweigh_an_older_english_preference():
    history = [dict(track(f"liked-{n}", "en"), at=n) for n in range(-50, 0)]
    candidate = [track("current", "en")]

    old_bias = by_id(candidate, liked=history)["current"].language_bias
    new_bias = by_id(candidate, liked=history, disliked=english_dislikes(15))["current"].language_bias

    assert old_bias > 1
    assert new_bias < -1.5


def test_heavy_english_catalogue_presents_fewer_but_some_english_tracks_after_dislikes():
    candidates = [track(f"{number:02d}", "en") for number in range(50)]
    candidates += [track(f"other-{number}", "other") for number in range(6)]
    candidates += [track("unknown", "unknown")]

    def heard_languages(disliked):
        queue = RadioQueue()
        queue.fill(disliked=disliked)
        queue.set_catalogue(candidates)
        return [queue.next_track()["language"] for _ in range(24)]

    before = heard_languages([])
    after = heard_languages(english_dislikes(18))

    assert before.count("en") >= 17
    assert 0 < after.count("en") <= 7
    assert "unknown" in after


def test_state_persists_feedback_language_chronology_and_migrates_old_settings(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "settings": {"russian_only": True, "taste": {"genre_weight": 2}},
        "likes": [{"id": "old", "at": 1}],
    }))
    state = AppState(str(path))
    assert "russian_only" not in state.settings
    assert state.taste["language_weight"] == 1
    assert state.taste["genre_weight"] == 2
    assert state.update_feedback_language(track("old", "eng"))
    state.dislike(track("different", "eng"))
    state.remember(track("started", "ru"))
    state.remember_finished(track("done", "rus"))

    saved = AppState(str(path))
    assert saved.likes == [{"id": "old", "at": 1, "language": "en", "language_code": "en"}]
    assert saved.dislikes[0]["language"] == "en"
    assert saved.dislikes[0]["at"] > 1
    assert [item["id"] for item in saved.finished_listens] == ["done"]
    assert saved.finished_listens[0]["language"] == "ru"
    assert saved.history[0]["id"] == "started"
