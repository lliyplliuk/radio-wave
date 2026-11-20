import json

import pytest

from radio_wave.library import Catalogue
from radio_wave.state import AppState
from radio_wave.tags import read_tags
from test_service import write_id3_mp3


@pytest.mark.parametrize("code", ["fra", "fre", "fr"])
def test_audio_and_catalogue_keep_exact_french_code(tmp_path, code):
    path = tmp_path / "chanson.mp3"
    write_id3_mp3(path, language=code)
    assert read_tags(path)["language"] == "other"
    assert read_tags(path)["language_code"] == "fr"

    catalogue = Catalogue(str(tmp_path / "catalog.json"))
    [track] = catalogue.scan_path(str(path))
    assert (track["language"], track["language_code"], track["lang_source"]) == ("other", "fr", "tag")
    catalogue.update(track["id"], title="Nouveau titre", language="other")
    assert track["language_code"] == "fr"
    catalogue.update(track["id"], language="en")
    assert (track["language"], track["language_code"]) == ("en", "en")
    catalogue.update(track["id"], language="unknown")
    assert "language_code" not in track
    catalogue.update(track["id"], language="other", language_code="fra")
    assert Catalogue(catalogue.path).get(track["id"])["language_code"] == "fr"


def test_old_other_only_recovers_existing_unambiguous_audio_tags(tmp_path):
    french = tmp_path / "french.mp3"
    untagged = tmp_path / "untagged.mp3"
    write_id3_mp3(french, language="fre")
    untagged.write_bytes(b"audio")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([
        {"id": "tagged", "language": "other", "lang_source": "tag", "path": str(french)},
        {"id": "manual", "language": "other", "lang_source": "manual", "path": str(french)},
        {"id": "und", "language": "other", "lang_source": "tag", "path": str(untagged)},
        {"id": "missing", "language": "other", "lang_source": "tag", "path": str(tmp_path / "gone.mp3")},
    ]))

    catalogue = Catalogue(str(path))
    assert catalogue.get("tagged")["language_code"] == "fr"
    assert all("language_code" not in catalogue.get(item) for item in ("manual", "und", "missing"))
    french.unlink()
    assert Catalogue(str(path)).get("tagged")["language_code"] == "fr"


@pytest.mark.parametrize(("codes", "expected"), [
    (["fre", "fra"], {"language": "other", "language_code": "fr"}),
    (["fre", "eng"], {"language": "unknown"}),
])
def test_vorbis_language_comments_distinguish_aliases_from_multiple_languages(tmp_path, codes, expected):
    path = tmp_path / "chanson.flac"
    comments = b"".join(len(item).to_bytes(4, "little") + item for item in (
        f"LANGUAGE={code}".encode() for code in codes
    ))
    block = b"\x00" * 4 + len(codes).to_bytes(4, "little") + comments
    path.write_bytes(b"fLaC\x84" + len(block).to_bytes(3, "big") + block)
    assert read_tags(path) == expected


def test_feedback_and_history_retain_exact_language_and_original_time(tmp_path):
    state = AppState(str(tmp_path / "state.json"))
    track = {"id": "french", "title": "Chanson", "genre": "chanson", "language": "other", "language_code": "fra"}
    state.like(track)
    state.remember(track)
    state.remember_finished(track)
    before = AppState(state.path)
    entries = [before.likes[0], before.history[0], before.finished_listens[0]]
    assert all(item["language_code"] == "fr" and item["language"] == "other" for item in entries)
    original_times = [item["at"] for item in entries]

    assert before.update_feedback_language({**track, "language_code": "deu"})
    reloaded = AppState(state.path)
    updated = [reloaded.likes[0], reloaded.history[0], reloaded.finished_listens[0]]
    assert [(item["language_code"], item["at"]) for item in updated] == [("de", when) for when in original_times]


def test_batch_feedback_upgrade_uses_only_confirmed_catalogue_codes(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    legacy = {"id": "known", "language": "other", "at": 1}
    missing = {"id": "missing", "language": "other", "at": 2}
    unrelated = {"id": "known", "language": "ru", "at": 3}
    path.write_text(json.dumps({"likes": [legacy, missing], "history": [legacy, unrelated]}))
    state = AppState(str(path))
    save = state.save
    writes = []

    def observed_save():
        writes.append(1)
        save()

    monkeypatch.setattr(state, "save", observed_save)
    tracks = [
        {"id": "known", "language": "other", "language_code": "fre", "lang_source": "tag"},
        {"id": "missing", "language": "other", "title": "La vie en rose"},
    ]
    assert state.restore_feedback_language_codes(tracks)
    assert not state.restore_feedback_language_codes(tracks)
    assert writes == [1]
    reread = AppState(str(path))
    enriched = {**legacy, "language_code": "fr"}
    assert reread.likes == [enriched, missing]
    assert reread.history == [enriched, unrelated]
