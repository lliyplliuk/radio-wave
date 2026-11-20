import json

import pytest

from radio_wave.library import Catalogue
from radio_wave.ranker import genre_family, rank, track_genre_family
from radio_wave.state import AppState
from radio_wave.tags import is_corrupted_text, normalize_language, read_tags


MBID = "b2f71618-a599-44f9-b072-fbdbd97f5b41"


def syncsafe(size):
    return bytes((size >> offset) & 0x7F for offset in (21, 14, 7, 0))


def frame(major, name, raw):
    if major == 2:
        return name + len(raw).to_bytes(3, "big") + raw
    size = syncsafe(len(raw)) if major == 4 else len(raw).to_bytes(4, "big")
    return name + size + b"\0\0" + raw


def tag(tmp_path, major, frames, tail=b""):
    path = tmp_path / "track.mp3"
    body = b"".join(frame(major, name, raw) for name, raw in frames)
    path.write_bytes(b"ID3" + bytes((major, 0, 0)) + syncsafe(len(body)) + body + tail)
    return path


def encode(text, method, codec, bom=b""):
    return bytes((method,)) + bom + text.encode(codec)


@pytest.mark.parametrize(("major", "method", "codec", "bom"), [
    (3, 1, "utf-16-le", b"\xff\xfe"),
    (3, 1, "utf-16-be", b"\xfe\xff"),
    (4, 2, "utf-16-be", b""),
    (4, 3, "utf-8", b""),
])
def test_cyrillic_id3_text_frames_reproduce_played_mp3(tmp_path, major, method, codec, bom):
    path = tag(tmp_path, major, [
        (b"TPE1", encode("001. Татьяна Куртукова\0", method, codec, bom)),
        (b"TIT2", encode("Синяя вода", method, codec, bom)),
        (b"TCON", encode("Поп", method, codec, bom)),
        (b"TLAN", encode("rus", method, codec, bom)),
    ])
    assert read_tags(path) == {
        "artist": "001. Татьяна Куртукова",
        "title": "Синяя вода",
        "genre": "Поп",
        "language": "ru",
        "language_code": "ru",
    }


@pytest.mark.parametrize(("method", "codec", "bom", "second_bom"), [
    (0, "latin-1", b"", b""),
    (1, "utf-16-le", b"\xff\xfe", b""),
    (1, "utf-16-le", b"\xff\xfe", b"\xff\xfe"),
    (1, "utf-16-be", b"\xfe\xff", b"\xfe\xff"),
    (2, "utf-16-be", b"", b""),
    (3, "utf-8", b"", b""),
])
def test_txxx_description_terminator_uses_frame_encoding(tmp_path, method, codec, bom, second_bom):
    raw = encode("MusicBrainz Recording ID\0", method, codec, bom) + second_bom + MBID.encode(codec)
    path = tag(tmp_path, 4, [(b"TXXX", raw)])
    assert read_tags(path) == {"mbid": MBID}


def test_legacy_latin_and_mislabeled_cyrillic(tmp_path):
    path = tag(tmp_path, 3, [
        (b"TPE1", encode("Björk", 0, "latin-1")),
        (b"TIT2", encode("Свет", 0, "cp1251")),
        (b"TCON", encode("Поп", 0, "utf-8")),
    ])
    assert read_tags(path) == {"artist": "Björk", "title": "Свет", "genre": "Поп"}


def test_corrupt_frames_fall_back_to_valid_fields(tmp_path):
    tail = b"TAG" + b"Old title".ljust(30, b"\0") + b"Old artist".ljust(30, b"\0") + b"\0" * 64 + b"\xff"
    path = tag(tmp_path, 3, [
        (b"TPE1", b"\x01\xff\xfeBroken!"),
        (b"TIT2", b"\x03Bad\x01text"),
        (b"TIT2", b"\x03Good title"),
    ], tail)
    assert read_tags(path) == {"artist": "Old artist", "title": "Good title"}
    old_mojibake = encode("001. Татьяна Куртукова", 1, "utf-16-le", b"\xff\xfe").decode("cp1251")
    assert is_corrupted_text(old_mojibake)
    assert is_corrupted_text("Tag with \ufffd")
    assert not is_corrupted_text("Татьяна Куртукова")


def test_id3v24_multiple_languages_remain_ambiguous(tmp_path):
    path = tag(tmp_path, 4, [(b"TLAN", b"\x03rus\0eng\0")])
    assert normalize_language(read_tags(path)["language"]) == "unknown"


def test_v22_short_frame_headers(tmp_path):
    path = tag(tmp_path, 2, [(b"TT2", encode("Синяя вода", 1, "utf-16-le", b"\xff\xfe"))])
    assert read_tags(path) == {"title": "Синяя вода"}


def test_id3v1_freeform_comment_is_not_a_genre(tmp_path):
    path = tmp_path / "untagged.mp3"
    footer = b"TAG" + b"Song".ljust(30, b"\0") + b"\0" * 64
    path.write_bytes(footer + b"Some freeform comment".ljust(30, b"\0") + b"\x11")
    assert read_tags(path) == {"title": "Song", "genre": "Rock"}


@pytest.mark.parametrize(("major", "raw", "label"), [
    (3, b"(17)", "Rock"),
    (3, b"(4)Eurodisco", "Eurodisco"),
    (3, b"(17)(102)", "Rock / Chanson"),
    (3, b"(102)", "Chanson"),
    (4, b"8\0Rock", "Jazz / Rock"),
    (3, b"Club 17", "Club 17"),
])
def test_tcon_numbers_resolve_without_overwriting_text(tmp_path, major, raw, label):
    path = tag(tmp_path, major, [(b"TCON", b"\x03" + raw), (b"TIT2", b"\x03" + b"17")])
    assert read_tags(path)["genre"] == label
    assert read_tags(path)["title"] == "17"


def test_saved_genre_code_is_readable_and_matches_freeform_rock(tmp_path):
    old = {"id": "legacy", "genre": "(17)", "language": "ru", "path": "/cache/old.mp3"}
    bespoke = {"id": "manual", "genre": "Club 17", "edited": ["genre"]}
    filename = tmp_path / "catalog.json"
    filename.write_text(json.dumps({"tracks": [old, bespoke]}))
    assert Catalogue(str(filename)).get("legacy")["genre"] == "Rock"
    assert Catalogue(str(filename)).get("manual")["genre"] == "Club 17"

    assert genre_family(old["genre"]) == "rock"
    assert rank([old])[0].reason == "популярный жанр в коллекции: Rock"
    chosen = rank([{"id": "fresh", "genre": "Rock", "language": "ru"}], liked=[old])[0]
    assert chosen.similar and "жанр: Rock" in chosen.reason


def test_literal_numeric_genres_are_not_reinterpreted_on_load_or_ranking(tmp_path):
    catalogue = Catalogue(str(tmp_path / "catalog.json"))
    path = tag(tmp_path, 3, [(b"TCON", b"\x03((17)")])
    assert read_tags(path)["genre"] == "(17)"
    [escaped] = catalogue.scan_path(str(path))
    catalogue.add_external({"id": "vorbis", "genre": "17", "path": "/music/record.flac"})
    catalogue.add_external({"id": "edited", "genre": "(17)", "path": "/music/record.mp3", "edited": ["genre"]})

    reloaded = Catalogue(catalogue.path)
    state = AppState(str(tmp_path / "state.json"))
    for ident, label in ((escaped["id"], "(17)"), ("vorbis", "17"), ("edited", "(17)")):
        track = reloaded.get(ident)
        assert track["genre"] == label
        assert track_genre_family(track) == label
        assert rank([track])[0].reason.endswith(label)
        state.like(track)

    assert not rank([{"id": "rock", "genre": "Rock"}], liked=AppState(state.path).likes)[0].similar
