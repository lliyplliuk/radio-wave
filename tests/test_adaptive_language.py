import pytest

from radio_wave.queue import RadioQueue
from radio_wave.ranker import TasteProfile, language_mix


def song(track_id, language, **extra):
    return dict(id=track_id, title=track_id, artist=track_id, genre="Rock", language=language, **extra)


@pytest.mark.parametrize("preferred,expected", [(None, 6), ("ru", 8), ("en", 4)])
def test_adaptive_mix_plays_both_sides_in_proportion_to_feedback(preferred, expected):
    alternatives = [song(f"{language}-{index:02}", language) for language in ("ru", "en") for index in range(20)]
    likes = [song(f"like-{index}", preferred) for index in range(4)] if preferred else []
    queue = RadioQueue()
    queue.fill(liked=likes)
    queue.set_catalogue(alternatives)
    played = [queue.next_track()["language"] for _ in range(12)]
    assert played.count("ru") == pytest.approx(expected, abs=1)
    assert 0 < played.count("en") < 12


@pytest.mark.parametrize("present,missing", [("ru", "foreign"), ("en", "ru")])
def test_available_side_does_not_hide_shortage_of_the_other_side(present, missing):
    queue = RadioQueue()
    queue.set_catalogue([song(f"song-{index}", present) for index in range(12)])
    assert queue.missing_language() == missing
    assert queue.next_track()["language"] == present
    assert queue.missing_language(skip={missing}) is None


def test_zero_weight_disables_target():
    queue = RadioQueue(profile=TasteProfile(language_weight=0))
    queue.set_catalogue([song("one", "ru")])
    assert not queue.language_reserve_targets()
    assert queue.missing_language() is None


def test_equal_feedback_and_unknown_leave_the_prior_neutral():
    ru = song("russian", "ru")
    english = song("english", "en")
    other = song("french", "other")
    unknown = song("unknown", "unknown")
    assert language_mix([ru, english, unknown], [unknown]).russian_share == 0.5
    assert language_mix([ru, other]).russian_share == 0.5
    assert language_mix([unknown] * 20).reserve_targets(4) == {"ru": 2, "foreign": 2}
    assert 0.5 < language_mix([ru], weight=0.5).russian_share < language_mix([ru]).russian_share < 0.65
    assert language_mix(finished_listens=[ru]).russian_share < language_mix([ru]).russian_share
    assert language_mix(disliked=[ru]).russian_share < language_mix(early_skips=[ru]).russian_share < 0.5


@pytest.mark.parametrize("rejected,opposite,side", [("ru", "en", "foreign"), ("en", "ru", "ru")])
def test_heavy_negative_feedback_symmetrically_suppresses_reserve_but_allows_fallback(rejected, opposite, side):
    dislikes = [song(f"disliked-{index}", rejected) for index in range(18)]
    queue = RadioQueue()
    queue.fill(disliked=dislikes)
    assert queue.language_reserve_targets() == {side: 4}
    queue.set_catalogue([song("ready", rejected), song("pending", opposite, ready=False)])
    assert queue.next_track()["language"] == rejected
    assert queue.missing_language() == side


def test_can_stage_both_language_sides_without_replacing_published_tracks():
    ready = [song(f"unknown-{index}", "unknown") for index in range(8)]
    pending = [song("pending-ru", "ru", ready=False), song("pending-fr", "other", ready=False)]
    queue = RadioQueue()
    queue.set_catalogue(ready)
    published = [item["id"] for item in queue.upcoming[:4]]
    queue.pin_visible(published)
    queue.set_catalogue([*ready, *pending])

    assert all(queue.offer_recovery(track["id"]) for track in pending)
    assert set(queue.recovery_ids()) == {track["id"] for track in pending}
    assert [item["id"] for item in queue.upcoming[:4]] == published
    assert {track["id"] for track in pending} <= {item["id"] for item in queue.upcoming[4:]}
