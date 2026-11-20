from radio_wave.candidate_window import candidate_window
from radio_wave.language import genre_language
from radio_wave.language_codes import normalize_language_code
from radio_wave.queue import RadioQueue
from radio_wave.ranker import TasteProfile, genre_fit, genre_suppressed, rank, suppressed_genres


def chanson(track_id, language, **extra):
    return dict(id=track_id, artist=track_id, genre="Chanson", language=language, **extra)


RUSSIAN_DISLIKES = [chanson(f"unliked-{index}", "ru") for index in range(11)]


def test_disliked_russian_chanson_does_not_suppress_french_or_ambiguous_chanson():
    tracks = [
        chanson("russian", "ru"), chanson("french", "other", language_code="fra"),
        chanson("unknown", "unknown"), chanson("other", "other"),
    ]
    profile = TasteProfile(language_weight=0)
    fit = genre_fit([], profile, disliked=RUSSIAN_DISLIKES)
    assert fit(tracks[0]) < -1
    assert all(fit(track) == 0 for track in tracks[1:])
    assert not suppressed_genres([], RUSSIAN_DISLIKES, profile)
    options = {item.id: item for item in rank(tracks, profile=profile, disliked=RUSSIAN_DISLIKES)}
    assert all(options[track["id"]].score > options["russian"].score for track in tracks[1:])
    assert not any(item.excluded for item in options.values())


def test_french_chanson_likes_match_exact_aliases_but_do_not_boost_other_known_languages():
    liked = [chanson("liked", "other", language_code="fre")]
    french = chanson("a-french", "other", language_code="fr")
    german = chanson("b-german", "other", language_code="deu")
    ambiguous = chanson("c-ambiguous", "other")
    russian = chanson("d-russian", "ru")
    profile = TasteProfile(language_weight=0)
    fit = genre_fit(liked, profile, disliked=RUSSIAN_DISLIKES)
    assert fit(french) > fit(ambiguous) >= fit(german) == 0 > fit(russian)
    queue = RadioQueue(profile=profile)
    queue.fill(liked=liked, disliked=RUSSIAN_DISLIKES)
    queue.set_catalogue([russian, german, ambiguous, french])
    assert queue.next_track()["id"] == french["id"]


def test_exact_tags_win_over_cautious_french_title_inference():
    title = {"title": "Je t’aime"}
    assert genre_language(title) == ("fr", 0.6)
    assert genre_language(dict(title, language="other", language_code="fra-CA")) == ("fr", 1.0)
    assert genre_language(dict(title, language="eng")) == ("en", 1.0)
    assert genre_language({"language": "other", "title": "Chanson"}) == ("unknown", 0.0)
    assert genre_language({"title": "Любовь / La vie en rose"}) == ("unknown", 0.0)
    assert [normalize_language_code(code) for code in ("fre", "FR", "fra", "fra/en", "und", "other")] == [
        "fr", "fr", "fr", "", "", "",
    ]


def test_candidate_projection_keeps_french_chanson_while_russian_chanson_is_available():
    russian = [chanson(f"ru-{index}", "ru", source="torrent", ready=True) for index in range(20)]
    french = [chanson(f"fr-{index}", "other", language_code="fre", source="torrent", ready=True) for index in range(6)]
    profile = TasteProfile(language_weight=0)
    fit = genre_fit([], profile, disliked=RUSSIAN_DISLIKES)
    veto = genre_suppressed([], RUSSIAN_DISLIKES, profile, fit=fit)
    visible = candidate_window(
        [*russian, *french], is_automatic=lambda _: True, is_unheard=lambda _: True,
        taste_score=fit, is_genre_suppressed=veto, upcoming_ids=[russian[0]["id"]], limit=6,
    )
    assert visible == [russian[0], *french[:5]]
    assert not genre_suppressed([], RUSSIAN_DISLIKES, TasteProfile(genre_weight=0))(russian[0])
