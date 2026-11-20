from base64 import b32encode
from dataclasses import replace
from html import escape
from urllib.parse import parse_qs, urlsplit

import pytest

from radio_wave import torrent_index as index
from radio_wave.catalog import CatalogRecording
from radio_wave.online_radio import _starting_choices, release_choices


HASH = "a0" * 20
OTHER_HASH = "ba" * 20
MAGNET = f"magnet:?xt=urn:btih:{HASH}"


def torznab_item(title, magnet=MAGNET, *, source="https://tracker.example/42", seeders=0, category="3000"):
    magnet_attr = f'<torznab:attr name="magneturl" value="{escape(magnet)}"/>' if magnet else ""
    return f"""
    <item>
      <title>{escape(title)}</title>
      <guid>{escape(source)}</guid>
      <category>{category}</category>
      <enclosure url="https://untrusted.example/download/42" type="application/x-bittorrent"/>
      {magnet_attr}
      <torznab:attr name="seeders" value="{seeders}"/>
      <torznab:attr name="category" value="{category}"/>
    </item>
    """


def feed(*entries):
    return (
        '<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
        + "".join(entries) + "</channel></rss>"
    ).encode("utf-8")


def test_torznab_requires_sound_evidence_despite_mirror_category():
    data = feed(
        torznab_item("Кино — Группа крови [FLAC]"),
        torznab_item("Кино — Группа крови [x264, MP3]"),
        torznab_item("Кино — Группа крови [FLAC]", category="3030"),
        torznab_item("Кино — Группа крови [FLAC]", magnet=None),
        torznab_item("Кино — Группа крови", seeders=100),
    )
    assert index._parse_jacred(data) == [
        index.TorrentRelease(
            "Кино — Группа крови [FLAC]", MAGNET, None, 0,
            "https://tracker.example/42", True, "jacred",
        )
    ]


def test_public_torznab_search_query_no_key_and_merged_seed_rank(monkeypatch):
    valid = feed(torznab_item("Кино — Группа крови [FLAC]", seeders=23))
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return valid

    old = index.TorrentRelease(
        "Кино — Группа крови [MP3]", f"magnet:?xt=urn:btih:{OTHER_HASH}", None,
        4, "https://rutor.info/torrent/45", True,
    )
    monkeypatch.setattr(index, "_lookup", lambda _path: [old])
    monkeypatch.setattr(index, "_request", request)
    found = index.search_audio("Кино — Группа крови")
    parts = urlsplit(calls[0][0])
    assert (parts.hostname, parts.path) == ("jac.black", "/torznab/api")
    assert parse_qs(parts.query) == {"t": ["music"], "cat": ["3000"], "q": ["Кино Группа крови"]}
    assert len(calls) == 1
    assert [item.provider for item in found] == ["jacred", "rutor"]
    assert [item.seeders for item in found] == [23, 4]


def test_merge_deduplicates_uppercase_hex_and_base32_and_source():
    base32 = b32encode(bytes.fromhex(HASH)).decode()
    first = index.TorrentRelease("Кино MP3", MAGNET, None, 7, "https://rutor.info/torrent/12")
    second = index.TorrentRelease(
        "Кино FLAC", f"magnet:?xt=URN:BTIH:{base32}", None, 12,
        "https://tracker.example/30", provider="jacred",
    )
    third = replace(second, magnet=f"magnet:?xt=urn:btih:{OTHER_HASH}", seeders=9)
    assert index._deduplicate([first, second, third]) == [second]


def test_torznab_conflicting_hashes_and_multiple_categories():
    base32 = b32encode(bytes.fromhex(HASH)).decode()
    equal = f"{MAGNET}&xt=urn:btih:{base32}&dn=Тест"
    conflict = f"{MAGNET}&xt=urn:btih:{OTHER_HASH}"
    ambiguous_category = torznab_item("Кино FLAC").replace(
        '<torznab:attr name="category" value="3000"/>',
        '<torznab:attr name="category" value="2040"/><torznab:attr name="category" value="3000"/>',
    )
    results = index._parse_jacred(feed(
        torznab_item("Кино FLAC", equal), torznab_item("Кино FLAC", conflict), ambiguous_category,
    ))
    assert len(results) == 1
    assert parse_qs(urlsplit(results[0].magnet).query) == {"xt": [f"urn:btih:{HASH}"], "dn": ["Тест"]}


def test_public_index_failure_keeps_available_provider(monkeypatch):
    def unavailable(_query):
        raise index.TorrentIndexError("public endpoint unavailable")

    rutor = index.TorrentRelease("Кино FLAC", MAGNET, None, 7, "https://rutor.info/torrent/12")
    jacred = replace(rutor, source_url="https://tracker.example/30", provider="jacred")
    monkeypatch.setattr(index, "_lookup", lambda _path: [rutor])
    monkeypatch.setattr(index, "_jacred_search", unavailable)
    assert index.search_audio("Кино") == [rutor]
    monkeypatch.setattr(index, "_lookup", unavailable)
    monkeypatch.setattr(index, "_jacred_search", lambda _query: [jacred])
    assert index.search_audio("Кино") == [jacred]
    monkeypatch.setattr(index, "_jacred_search", unavailable)
    with pytest.raises(index.TorrentIndexError):
        index.search_audio("Кино")


def test_jacred_unknown_seeds_are_last_and_used_if_browse_fails():
    jacred = index.TorrentRelease(
        "Кино — Группа крови FLAC", MAGNET, None, 0, "https://tracker.example/42", True, "jacred",
    )
    rutor = index.TorrentRelease(
        "Кино — Группа крови MP3", f"magnet:?xt=urn:btih:{OTHER_HASH}", None,
        3, "https://rutor.info/torrent/12", True,
    )

    class PublicIndex:
        def latest_audio(self):
            raise index.TorrentIndexError("browse is offline")

        def search_audio(self, query):
            return [jacred] if query == "Кино Группа крови" else [jacred, rutor]

    provider = PublicIndex()
    wanted = CatalogRecording("Группа крови", "Кино", "")
    assert [choice.release for choice in release_choices(provider, wanted, set())] == [rutor, jacred]
    assert [choice.release for choice in _starting_choices(provider, {rutor.source_url}, 0)] == [jacred]
