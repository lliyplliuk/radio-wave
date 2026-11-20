import pytest

from radio_wave.ranker import TasteProfile, rank


def recording(track_id, *, artist="", genre="", language="unknown", at=0):
    return dict(id=track_id, artist=artist, genre=genre, language=language, at=at)


def by_id(options, **kwargs):
    return {item.id: item for item in rank(options, **kwargs)}


def test_early_next_is_half_negative_language_signal_and_does_not_ban_song():
    skips = [recording(f"old-{number}", language="en", at=number) for number in range(20)]
    songs = [recording("other-en", language="en"), recording("unknown"), skips[0]]

    soft = by_id(songs, early_skips=skips)
    hard = by_id(songs, disliked=skips)

    assert soft["other-en"].language_bias == pytest.approx(hard["other-en"].language_bias / 2)
    assert soft["unknown"].language_bias == 0
    assert not soft["old-0"].excluded
    assert hard["old-0"].excluded


def test_early_next_downweights_similar_artist_and_genre_without_banning():
    skipped = recording("skipped", artist="Группа", genre="Рок")
    candidates = [skipped, recording("similar", artist="Группа", genre="Рок"), recording("different")]
    profile = TasteProfile(catalogue_weight=0, language_weight=0)

    scores = by_id(candidates, early_skips=[skipped], profile=profile)

    assert scores["similar"].score == -.5 * (profile.artist_weight + profile.genre_weight)
    assert scores["different"].score == 0
    assert all(not item.excluded for item in scores.values())


def test_explicit_votes_override_weak_event():
    skipped = recording("skipped", artist="Группа", genre="Рок", language="en")
    options = [skipped, recording("similar", artist="Группа", genre="Рок", language="en")]
    assert rank(options, liked=[skipped], early_skips=[skipped]) == rank(options, liked=[skipped])
    assert rank(options, disliked=[skipped], early_skips=[skipped]) == rank(options, disliked=[skipped])
