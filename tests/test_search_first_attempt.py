import io
import json
import os
from threading import Event
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from radio_wave import catalog


_NAGART = catalog.CatalogRecording("Мёртвая Алёна", "Nagart", "5fad0af1-2bcf-4644-b6e8-2d8e60ee0f2c")


@pytest.fixture(autouse=True)
def reset_musicbrainz_throttle(monkeypatch):
    monkeypatch.setattr(catalog, "_mb_retry_at", float("-inf"))
    monkeypatch.setattr(catalog, "_mb_last_request", float("-inf"))


def test_temporary_503_is_retried_once_through_musicbrainz_throttle(monkeypatch):
    requests = []
    waits = []
    traces = []

    def urlopen(request, *, timeout):
        requests.append(timeout)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 503, "temporary", {}, None)
        row = dict(
            id=_NAGART.mbid, title=_NAGART.title,
            **{"artist-credit": [{"name": _NAGART.artist}]},
        )
        return io.BytesIO(json.dumps({"recordings": [row]}).encode())

    monkeypatch.setattr(catalog, "urlopen", urlopen)
    monkeypatch.setattr(catalog, "sleep", waits.append)
    monkeypatch.setattr(catalog, "trace", lambda event, **fields: traces.append((event, fields)))

    assert catalog.search_recordings("nagart") == [_NAGART]
    assert requests == [6, 6]
    assert len(waits) == 1 and 0 < waits[0] <= 1
    assert [(event, fields.get("outcome") or fields.get("http_status")) for event, fields in traces] == [
        ("musicbrainz_search_started", None), ("musicbrainz_search_retry", 503),
        ("musicbrainz_search_result", "found"),
    ]


@pytest.mark.parametrize(("status", "headers", "tries"), ((400, {}, 1), (503, {"Retry-After": "5"}, 1), (503, {}, 2)))
def test_musicbrainz_failure_is_bounded_and_honors_server_delay(monkeypatch, status, headers, tries):
    attempted = []

    def urlopen(request, **_kwargs):
        attempted.append(status)
        raise HTTPError(request.full_url, status, "error", headers, None)

    monkeypatch.setattr(catalog, "urlopen", urlopen)
    monkeypatch.setattr(catalog, "sleep", lambda _delay: None)
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.search_recordings("nagart")
    assert caught.value.kind == "http" and caught.value.status == status
    assert len(attempted) == tries


def test_musicbrainz_recording_retains_real_distinct_release_labels(monkeypatch):
    monkeypatch.setattr(catalog, "_mb_query", lambda _parameters: {
        "recordings": [{
            "id": _NAGART.mbid, "title": _NAGART.title,
            "artist-credit": [{"name": _NAGART.artist}],
            "releases": [{"title": "Дискография"}, {"title": "дискография"},
                         None, {"title": "Секреты оборотня"}],
        }],
    })
    [recording] = catalog.search_recordings("nagart")
    assert recording.album_titles == ("Дискография", "Секреты оборотня")


def test_empty_musicbrainz_uses_song_catalogue_without_fabricating_mbid(monkeypatch):
    def response(url, parameters):
        if url == catalog._MUSICBRAINZ:
            return {"recordings": []}
        assert url == catalog._ITUNES
        assert parameters["entity"] == "song" and parameters["term"] == "Nagart"
        song = {
            "kind": "song", "trackName": "Картины", "artistName": "Nagart",
            "collectionName": "Vol. 3", "trackTimeMillis": 192_000,
            "previewUrl": "https://media.invalid/preview", "trackId": 765,
        }
        return {"results": [
            song, {**song, "collectionName": "vol. 3"}, {**song, "collectionName": "Концерт"},
            {**song, "artistName": "Другая группа"}, {**song, "kind": "music-video"},
        ]}

    monkeypatch.setattr(catalog, "_get_json", response)
    assert catalog.search_picker_recordings(" Nagart ") == [
        catalog.CatalogRecording("Картины", "Nagart", "", 192_000, album_titles=("Vol. 3", "Концерт")),
    ]


def test_retry_after_cooldown_skips_later_musicbrainz_requests_without_sleep(monkeypatch):
    requests = []
    now = [100.0]

    def urlopen(request, **_kwargs):
        provider = urlsplit(request.full_url).hostname
        requests.append(provider)
        if provider == "musicbrainz.org":
            raise HTTPError(request.full_url, 503, "busy", {"Retry-After": "5"}, None)
        return io.BytesIO(b'{"results":[]}')

    monkeypatch.setattr(catalog, "urlopen", urlopen)
    monkeypatch.setattr(catalog, "monotonic", lambda: now[0])

    assert catalog.search_picker_recordings("Первый") == []
    now[0] += 4
    assert catalog.search_picker_recordings("Второй") == []
    now[0] += 2
    assert catalog.search_picker_recordings("Третий") == []
    assert requests == ["musicbrainz.org", "itunes.apple.com", "itunes.apple.com",
                        "musicbrainz.org", "itunes.apple.com"]


@pytest.fixture
def picker(tmp_path, monkeypatch, request):
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("нет графического дисплея")
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from radio_wave import catalog_dialog

    if not Gtk.init_check():
        pytest.skip("GTK4 не может открыть дисплей")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    local_case = getattr(request, "param", 0)
    local = [{"id": "local", "artist": "Nagart", "title": "Картины" if local_case == "missing" else "Локальная запись"}] if local_case else []
    parent = Gtk.Window()
    window = catalog_dialog.show_catalog(
        parent, lambda: local, on_play=lambda _track: None,
        on_play_recording=lambda _recording: None, is_available=lambda _track: local_case != "missing",
        on_play_search=lambda _query: None,
    )
    layout = window.get_child()
    header = layout.get_first_child()
    entry = header.get_first_child()
    status = header.get_next_sibling()
    results = status.get_next_sibling().get_child()
    if isinstance(results, Gtk.Viewport):
        results = results.get_child()

    def settle(condition):
        ctx = GLib.MainContext.default()
        until = time.monotonic() + 2
        while time.monotonic() < until:
            while ctx.pending():
                ctx.iteration(False)
            if condition():
                return
            time.sleep(0.005)
        pytest.fail("Нет ожидаемого результата GTK")

    yield catalog_dialog, entry, status, results, settle
    if window.get_visible():
        window.close()
    parent.destroy()


def test_first_submit_ignores_redundant_same_text_signal_and_duplicate_enter(picker, monkeypatch):
    dialog, entry, status, results, settle = picker
    pending, release = Event(), Event()
    queries = []

    def search(query):
        queries.append(query)
        pending.set()
        release.wait(2)
        return [_NAGART]

    monkeypatch.setattr(dialog, "search_picker_recordings", search)
    entry.set_text("nagart")
    entry.emit("activate")
    assert pending.wait(1)
    try:
        entry.emit("changed")
        entry.emit("activate")
        assert status.get_text() == "Ищем песни…"
        assert not results.get_row_at_index(0)
    finally:
        release.set()
    settle(lambda: results.get_row_at_index(0) is not None)
    result = results.get_row_at_index(0).get_child().get_first_child().get_first_child()
    assert result.get_label() == "Nagart — Мёртвая Алёна"
    assert queries == ["nagart"]


@pytest.mark.parametrize("picker", [0, 1], indirect=True)
def test_first_submit_503_uses_independent_catalogue_and_keeps_local_rows(picker, monkeypatch, request):
    _dialog, entry, status, results, settle = picker
    local_count = request.node.callspec.params["picker"]
    providers = []

    def urlopen(request, **_kwargs):
        url = urlsplit(request.full_url)
        providers.append(url.hostname)
        if url.hostname == "musicbrainz.org":
            raise HTTPError(request.full_url, 503, "busy", {"Retry-After": "5"}, None)
        assert parse_qs(url.query)["term"] == ["nagart"]
        return io.BytesIO(json.dumps({"results": [{
            "kind": "song", "artistName": "Nagart", "trackName": "Картины", "collectionName": "Vol. 3",
        }]}).encode())

    monkeypatch.setattr(catalog, "urlopen", urlopen)
    entry.set_text("nagart")
    entry.emit("activate")
    if local_count:
        assert results.get_row_at_index(0) is not None
    settle(lambda: results.get_row_at_index(local_count) is not None)
    first = results.get_row_at_index(0).get_child().get_first_child().get_first_child()
    if local_count:
        assert first.get_label() == "Nagart — Локальная запись"
    recording = results.get_row_at_index(local_count).get_child().get_first_child().get_first_child()
    assert recording.get_label() == "Nagart — Картины"
    assert recording.get_next_sibling().get_label() == "Vol. 3"
    assert not status.get_text()
    assert providers == ["musicbrainz.org", "itunes.apple.com"]


@pytest.mark.parametrize("picker", ["missing"], indirect=True)
def test_missing_local_duplicate_is_enriched_with_matching_remote_album(picker, monkeypatch):
    dialog, entry, _status, results, settle = picker
    remote = catalog.CatalogRecording("Картины", "Nagart", "", album_titles=("Vol. 3",))
    monkeypatch.setattr(dialog, "search_picker_recordings", lambda _query: [remote])
    entry.set_text("nagart")
    entry.emit("activate")

    def ready_label():
        labels = results.get_row_at_index(0).get_child().get_first_child()
        return labels.get_first_child().get_next_sibling()

    settle(lambda: ready_label() is not None)
    assert ready_label().get_text() == "Vol. 3"
    assert results.get_row_at_index(1) is None


def test_changed_query_submits_once_even_while_previous_result_is_in_flight(picker, monkeypatch):
    dialog, entry, status, results, settle = picker
    pending, release = Event(), Event()
    queries = []

    def search(query):
        queries.append(query)
        if query == "Старый":
            pending.set()
            release.wait(2)
            return [catalog.CatalogRecording("Устаревший ответ", "Другой", "old")]
        return [_NAGART]

    monkeypatch.setattr(dialog, "search_picker_recordings", search)
    entry.set_text("Старый")
    entry.emit("activate")
    assert pending.wait(1)
    try:
        entry.set_text("nagart")
        entry.emit("activate")
        assert status.get_text() == "Ищем песни…"
    finally:
        release.set()
    settle(lambda: results.get_row_at_index(0) is not None)
    result = results.get_row_at_index(0).get_child().get_first_child().get_first_child()
    assert result.get_label() == "Nagart — Мёртвая Алёна"
    assert queries == ["Старый", "nagart"]


def test_empty_search_and_network_failure_have_distinct_statuses(picker, monkeypatch):
    dialog, entry, status, results, settle = picker

    def search(query):
        if query == "Ошибка":
            raise catalog.CatalogError("offline", kind="network")
        return []

    monkeypatch.setattr(dialog, "search_picker_recordings", search)
    entry.set_text("Пустой запрос")
    entry.emit("activate")
    settle(lambda: status.get_text() == "В музыкальном каталоге песни не найдены.")
    assert results.get_row_at_index(0) is not None  # Отдельная попытка через поиск аудио.
    entry.set_text("Ошибка")
    entry.emit("activate")
    settle(lambda: status.get_text() == "Каталог песен временно недоступен.")
    assert results.get_row_at_index(0) is not None
