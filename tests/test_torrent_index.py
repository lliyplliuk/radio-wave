from unittest.mock import patch

import pytest

from radio_wave import torrent_index as index


HASH = "5" * 40


@pytest.fixture(autouse=True)
def only_rutor(monkeypatch):
    monkeypatch.setattr(index, "_jacred_search", lambda _query: [])


def release(title, seeders=7, *, identifier="123", hash_value=HASH, download_host="d.rutor.info"):
    magnet = f'<a href="magnet:?xt=urn:btih:{hash_value}&amp;dn=fixture"><img /></a>' if hash_value else ""
    return f"""
    <tr class="gai">
      <td>10&nbsp;Окт&nbsp;26</td>
      <td>
        <a class="downgif" href="//{download_host}/download/{identifier}"><img /></a>
        {magnet}
        <a href="/torrent/{identifier}/fixture">{title}</a>
      </td>
      <td><span class="green"><img alt="S" />&nbsp;{seeders}</span><span class="red">99</span></td>
    </tr>
    """


def listing(*rows):
    return ('<div id="index"><table>' + "".join(rows) + "</table></div>").encode("utf-8")


def test_search_cyrillic_scopes_to_audio_and_preserves_links():
    html = listing(
        release("Кино - <b>Группа крови</b> &amp; концерт", 12),
        release("Другой автор - Группа крови", 90, hash_value="1" * 40),
    )
    with patch.object(index, "_request", return_value=html) as request:
        found = index.search_audio("Кино — Группа крови")
    url = request.call_args.args[0]
    assert url.startswith("https://rutor.info/search/0/2/100/2/")
    assert "%D0%9A%D0%B8%D0%BD%D0%BE%20" in url
    assert found == [index.TorrentRelease(
        "Кино - Группа крови & концерт",
        f"magnet:?xt=urn:btih:{HASH}&dn=fixture",
        "https://d.rutor.info/download/123",
        12,
        "https://rutor.info/torrent/123/fixture",
        True,
    )]


def test_browse_favors_active_russian_candidates_and_deduplicates():
    html = listing(
        release("Кино - Группа крови", 4),
        release("VA - Русский хит", 7, identifier="789"),
        release("Different Artist - Track", 100, hash_value="1" * 40),
        release("Сплин - Резонанс", 0, hash_value="a" * 40),
    )
    with patch.object(index, "_request", return_value=html) as request:
        found = index.latest_audio(limit=2)
    assert request.call_args.args[0] == "https://rutor.info/browse/0/2/0/2"
    assert [item.title for item in found] == ["VA - Русский хит", "Different Artist - Track"]


def test_index_html_layout_error_falls_back_without_external_mirrors():
    with patch.object(index, "_request", side_effect=[b"<html>challenge</html>", listing()]) as request:
        assert index.search_audio("Кино") == []
    assert request.call_count == 2
    assert request.call_args.args[0].startswith("https://rutor.is/")


def test_download_only_entries_require_trusted_origin_and_valid_path():
    html = listing(
        release("Мельница - Дороги", hash_value=None),
        release("Мельница - Клон", hash_value=None, download_host="evil.example"),
    ).decode()
    found = index._parse_releases(html, "https://rutor.info")
    assert [item.title for item in found] == ["Мельница - Дороги"]
    assert found[0].magnet is None


def test_malformed_href_and_huge_count_do_not_drop_valid_results():
    html = listing(
        release("Кино - Группа крови", "1" * 5000, download_host="[broken"),
        release("Земфира - Спасибо", 8, hash_value="8" * 40),
    ).decode()
    releases = index._parse_releases(html, "https://rutor.info")
    assert [item.seeders for item in releases] == [0, 8]
    assert releases[0].torrent_url is None


def test_torrent_metadata_request_and_unexpected_content():
    item = index.TorrentRelease(
        "Test", None, "https://d.rutor.info/download/42", 4, "https://rutor.info/torrent/42",
    )
    with patch.object(index, "_request", return_value=b"d4:infodee") as request:
        assert index.fetch_torrent(item) == b"d4:infodee"
    assert request.call_args.args == (item.torrent_url,)
    assert request.call_args.kwargs["path"].fullmatch("/download/42")
    with patch.object(index, "_request", return_value=b"<!doctype html>"):
        with pytest.raises(index.TorrentIndexError, match="другого формата"):
            index.fetch_torrent(item)


def test_unsafe_download_urls_are_rejected_before_open():
    with patch.object(index, "build_opener") as opener:
        for url in (
            "http://d.rutor.info/download/42",
            "https://d.rutor.info@localhost/download/42",
            "https://d.rutor.info:1234/download/42",
            "https://d.rutor.info/anything",
        ):
            with pytest.raises(index.TorrentIndexError, match="Недопустимый адрес"):
                index.fetch_torrent(index.TorrentRelease("Test", None, url, 0, ""))
    opener.assert_not_called()
