"""Service contracts for catalogue search and discovery."""

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from radio_wave import catalog


SEED_A = "8a49dba0-253a-4535-b87f-78bb035336ce"
SEED_B = "f3bba4cd-8018-468b-902e-bc8f029593e5"
TRACK_A = "cd8cfc02-31af-4b53-9410-50f852ec88bb"
TRACK_B = "0026cc05-be60-4723-a399-e69e53c7e327"
TRACK_C = "b350a371-25ea-410d-8c3e-c138503a7cee"


def neighbour(seed, track, title, artist="Кино"):
    return {
        "reference_mbid": seed,
        "recording_mbid": track,
        "recording_name": title,
        "artist_credit_name": artist,
        "score": 10,
    }


class CatalogTests(unittest.TestCase):
    def setUp(self):
        catalog._similar_by_seed.cache_clear()
        self.addCleanup(catalog._similar_by_seed.cache_clear)
        catalog._recording_language.cache_clear()
        self.addCleanup(catalog._recording_language.cache_clear)

    def test_cyrillic_normalization(self):
        self.assertEqual(catalog.normalize("  ЁлКА\tВ\u00a0ЛЕСУ  "), "елка в лесу")

    def test_mixed_cyrillic_search_parses_artist_credit_and_version(self):
        valid = {
            "id": TRACK_A,
            "title": "Группа крови",
            "artist-credit": [
                {"name": "Кино", "joinphrase": " feat. "},
                {"artist": {"name": "Гость"}},
            ],
            "length": 280000,
            "disambiguation": "концерт",
        }
        with patch.object(catalog, "_mb_query", return_value={
            "recordings": [
                valid, valid, {"id": "wrong"}, {**valid, "id": TRACK_B, "video": True}
            ],
        }) as request:
            results = catalog.search_recordings("Кино — Группа крови")
        request.assert_called_once_with({
            "query": 'recording:"Группа крови" AND artist:"Кино"',
            "fmt": "json",
            "limit": 15,
        })
        self.assertEqual(results, [
            catalog.CatalogRecording("Группа крови", "Кино feat. Гость", TRACK_A, 280000, "концерт")
        ])

    def test_blank_does_not_call_musicbrainz(self):
        with patch.object(catalog, "_mb_query") as request:
            self.assertEqual(catalog.search_recordings("  "), [])
        request.assert_not_called()

    def test_single_word_search_covers_artist_and_track(self):
        with patch.object(catalog, "_mb_query", return_value={"recordings": []}) as request:
            catalog.search_recordings("Кино")
        self.assertEqual(
            request.call_args.args[0]["query"], 'recording:"Кино"^4 OR artist:"Кино"',
        )

    def test_musicbrainz_waits_between_request_starts(self):
        with (
            patch.object(catalog, "_get_json", return_value={"recordings": []}),
            patch.object(catalog, "_mb_last_request", 100.0),
            patch.object(catalog, "monotonic", side_effect=[100.25, 101.0]),
            patch.object(catalog, "sleep") as wait,
        ):
            self.assertEqual(catalog.search_recordings("Кино"), [])
        wait.assert_called_once_with(0.75)

    def test_recording_language_uses_exact_credited_lyrics(self):
        response = {
            "id": TRACK_A,
            "relations": [{
                "type": "performance", "target-type": "work", "attributes": [],
                "work": {"id": TRACK_B, "languages": ["eng"], "language": "eng"},
            }],
        }
        with patch.object(catalog, "_mb_query", return_value=response) as request:
            self.assertEqual(catalog.recording_language(TRACK_A), "en")
            self.assertEqual(catalog.recording_language(TRACK_A), "en")
            self.assertIsNone(catalog.recording_language("not-a-mbid"))
        request.assert_called_once_with(
            {"inc": "work-rels", "fmt": "json"}, url=f"{catalog._MUSICBRAINZ}/{TRACK_A}",
        )

    def test_recording_language_fetches_work_when_link_has_no_language_fields(self):
        recording = {
            "id": TRACK_A,
            "relations": [{
                "type": "performance", "target-type": "work", "attributes": [],
                "work": {"id": TRACK_B, "title": "Группа крови"},
            }],
        }
        with patch.object(catalog, "_mb_query", side_effect=[
            recording, {"id": TRACK_B, "languages": ["rus"]},
        ]) as request:
            self.assertEqual(catalog.recording_language(TRACK_A), "ru")
        self.assertEqual(request.call_args.kwargs["url"], f"{catalog._MUSICBRAINZ_WORK}/{TRACK_B}")

    def test_recording_language_preserves_exact_french_with_coarse_compatibility(self):
        for alias in ("fr", "fra", "fre"):
            response = {
                "id": TRACK_A,
                "relations": [{
                    "type": "performance", "target-type": "work",
                    "work": {"id": TRACK_B, "languages": [alias]},
                }],
            }
            catalog._recording_language.cache_clear()
            with self.subTest(alias=alias), patch.object(catalog, "_mb_query", return_value=response) as request:
                self.assertEqual(catalog.recording_language_code(TRACK_A), "fr")
                self.assertEqual(catalog.recording_language(TRACK_A), "other")
                request.assert_called_once()

    def test_recording_language_ignores_instrumental_ambiguous_and_merged_records(self):
        def relation(languages, attributes=()):
            return {
                "type": "performance", "target-type": "work", "attributes": list(attributes),
                "work": {"id": TRACK_B, "languages": languages},
            }
        cases = [
            {"id": TRACK_A, "relations": [relation(["rus", "eng"])]},
            {"id": TRACK_A, "relations": [relation(["eng"], ("instrumental",))]},
            {"id": TRACK_A, "relations": [relation(["zxx"])]},
            {"id": TRACK_A, "relations": [relation(["deu", "fra"])]},
            {"id": TRACK_A, "relations": [relation(["eng"]), relation(["rus"])]},
            {"id": TRACK_B, "relations": [relation(["eng"])]},
        ]
        for response in cases:
            with self.subTest(response=response), patch.object(catalog, "_mb_query", return_value=response):
                catalog._recording_language.cache_clear()
                self.assertIsNone(catalog.recording_language(TRACK_A))

    def test_neighbours_batch_dedupe_interleave_and_cache(self):
        with patch.object(catalog, "_get_json", return_value=[
            neighbour(SEED_A, TRACK_A, "Группа крови"),
            neighbour(SEED_A, TRACK_C, "Перемен"),
            neighbour(SEED_B, TRACK_B, "Звезда"),
            neighbour(SEED_B, TRACK_A, "Группа крови"),
            neighbour(SEED_B, SEED_A, "Исключённая исходная запись"),
            neighbour(SEED_A, "invalid", "Исключённый идентификатор"),
        ]) as request:
            results = catalog.similar_recordings([SEED_A, SEED_B, SEED_A, "invalid"])
            shorter = catalog.similar_recordings([SEED_A, SEED_B], limit=1)
        request.assert_called_once_with(catalog._LISTENBRAINZ, {
            "recording_mbids": (SEED_A, SEED_B),
            "algorithm": catalog._SIMILARITY_ALGORITHM,
        })
        self.assertEqual([item.mbid for item in results], [TRACK_A, TRACK_B, TRACK_C])
        self.assertEqual(shorter, results[:1])

    def test_repeated_query_parameters_and_request_timeout(self):
        with patch.object(catalog, "urlopen", side_effect=TimeoutError) as call:
            with self.assertRaises(catalog.CatalogError):
                catalog.similar_recordings([SEED_A, SEED_B])
        request = call.call_args.args[0]
        self.assertEqual(call.call_args.kwargs["timeout"], 6)
        self.assertEqual(
            parse_qs(urlparse(request.full_url).query)["recording_mbids"],
            [SEED_A, SEED_B],
        )

    def test_bad_response_is_distinct_from_no_matches(self):
        with patch.object(catalog, "_get_json", return_value={"status": "invalid"}):
            with self.assertRaises(catalog.CatalogError):
                catalog.similar_recordings([SEED_A])


if __name__ == "__main__":
    unittest.main()
