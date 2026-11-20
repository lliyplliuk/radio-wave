"""Тесты сервиса: импорт каталога, метаданные, лайки, настройки."""

import json
import os
import struct

import pytest

from radio_wave.library import Catalogue
from radio_wave.service import RadioService
from radio_wave.state import AppState


def write_id3_mp3(path, artist="Ария", title="Тест", genre="Рок", language="rus"):
    """Минимальный mp3 с ID3v2.3 — проверяем чтение тегов."""
    frames = []
    for frame_id, value in (
        (b"TIT2", title), (b"TPE1", artist), (b"TCON", genre), (b"TLAN", language),
    ):
        payload = b"\x00" + value.encode("utf-8")
        frames.append(frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload)
    body = b"".join(frames)
    header = b"ID3" + bytes([3, 0, 0]) + bytes([
        (len(body) >> 21) & 0x7F, (len(body) >> 14) & 0x7F, (len(body) >> 7) & 0x7F, len(body) & 0x7F,
    ])
    with open(path, "wb") as fh:
        fh.write(header + body + b"\xff\xfb" + b"\x00" * 64)


@pytest.fixture()
def service(tmp_path, monkeypatch):
    for kind in ("CACHE", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{kind}_HOME", str(tmp_path / kind))
    state = AppState(str(tmp_path / "state.json"))
    catalogue = Catalogue(str(tmp_path / "catalogue.json"))
    return RadioService(state=state, catalogue=catalogue, player=None, torrents=None)


def test_first_run_without_library(service):
    assert service.first_run is True


def test_import_reads_tags_and_marks_russian(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    write_id3_mp3(str(folder / "song.mp3"))
    added = service.import_paths([str(folder)])
    assert added == 1
    track = service.tracks()[0]
    assert track["artist"] == "Ария"
    assert track["title"] == "Тест"
    assert track["genre"] == "Рок"
    assert track["language"] == "ru"
    assert service.first_run is False


def test_non_audio_files_are_ignored(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(b"not audio")
    (folder / "readme.txt").write_text("нет")
    assert service.import_paths([str(folder)]) == 0


def test_bulk_mark_russian(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    write_id3_mp3(str(folder / "song.mp3"), language="eng")
    service.import_paths([str(folder)], mark_russian=True)
    assert service.tracks()[0]["language"] == "ru"
    assert service.tracks()[0]["lang_source"] == "bulk"


def test_edit_metadata_and_language(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    write_id3_mp3(str(folder / "song.mp3"))
    service.import_paths([str(folder)])
    track_id = service.tracks()[0]["id"]
    assert service.update_track(track_id, artist="Новый", genre="Джаз", language="other")
    track = service.catalogue.get(track_id)
    assert track["artist"] == "Новый"
    assert track["language"] == "other"
    assert track["lang_source"] == "manual"


def test_like_is_persisted(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    write_id3_mp3(str(folder / "song.mp3"))
    service.import_paths([str(folder)])
    service.queue.current = service.tracks()[0]
    service.like_current()
    with open(service.state.path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert len(data["likes"]) == 1
    assert data["likes"][0]["artist"] == "Ария"


def test_current_like_toggle_is_persisted_and_clears_feedback(service, tmp_path):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    write_id3_mp3(first, title="Первый")
    write_id3_mp3(second, title="Второй")
    service.import_paths([str(first), str(second)])
    chosen, other = service.tracks()
    assert not service.current_liked()
    service.toggle_like_current()
    assert AppState(service.state.path).likes == []

    service.queue.current = chosen
    service.dislike_current()
    service.queue.current = chosen
    service.toggle_like_current()
    assert service.current_liked()
    assert not AppState(service.state.path).is_disliked(chosen["id"])
    assert AppState(service.state.path).is_liked(chosen["id"])

    service.queue.current = other
    assert not service.current_liked()
    service.queue.current = chosen
    assert service.current_liked()
    service.toggle_like_current()
    assert not service.current_liked()
    assert AppState(service.state.path).likes == []
    assert service.queue._last_liked == []


def test_dislike_is_persisted(service, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    write_id3_mp3(str(folder / "song.mp3"))
    service.import_paths([str(folder)])
    service.queue.current = service.tracks()[0]
    service.dislike_current()
    assert len(service.state.dislikes) == 1
    assert service.state.likes == []


def test_language_weight_persists(service):
    service.set_taste(language_weight=0.0)
    assert service.state.taste["language_weight"] == 0.0


def test_taste_knobs_are_saved(service):
    service.set_taste(artist_weight=2.0)
    assert service.taste["artist_weight"] == 2.0
    assert os.path.isfile(service.state.path)
