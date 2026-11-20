import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from radio_wave.__main__ import main
from radio_wave.diagnostics import configure_playback_log, playback_log, trace


@pytest.fixture
def private_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    previous = playback_log.handlers[:]
    level, propagate = playback_log.level, playback_log.propagate
    for handler in previous:
        playback_log.removeHandler(handler)
    yield Path(tmp_path) / "radio-wave/playback.log"
    for handler in playback_log.handlers[:]:
        playback_log.removeHandler(handler)
        handler.close()
    for handler in previous:
        playback_log.addHandler(handler)
    playback_log.setLevel(level)
    playback_log.propagate = propagate


def test_normal_log_keeps_only_info_and_does_not_emit_to_terminal(private_logging, capsys):
    configure_playback_log()
    trace("search_requested", query="Мёртвая Алёна")
    playback_log.info("audio_started source=torrent")
    assert capsys.readouterr().err == ""
    log = private_logging.read_text()
    assert "application_started mode=normal" in log
    assert "audio_started source=torrent" in log
    assert "search_requested" not in log


def test_developer_log_is_readable_bounded_and_redacted(private_logging, capsys):
    configure_playback_log(developer=True)
    trace(
        "search_requested",
        title="Мёртвая Алёна\nисполнитель\x1b\u202e",
        query="Nagart magnet:?xt=urn:btih:privateid https://server/secret?t=key-frag",
        detail="password=\"very private\" Authorization: Bearer private-token",
        other="prefix /home/listener/Very Private Song.mp3",
        api_key="hidden-key",
        long_title="Б" * 500,
    )
    playback_log.info("ordinary URL https://domain/token?password=hunter2")
    output = capsys.readouterr().err
    assert output == private_logging.read_text()
    assert "title=\"Мёртвая Алёна исполнитель\"" in output
    assert "query=\"Nagart [redacted-uri] [redacted-uri]\"" in output
    assert "long_title=\"" + "Б" * 179 + "…" in output
    assert len(output.splitlines()) == 3
    for secret in ("privateid", "very private", "private-token", "hidden-key", "Very Private",
                   "/home/listener", "key-frag", "hunter2", "\x1b", "\u202e"):
        assert secret not in output


def test_dev_flag_is_consumed_before_gtk_and_not_after_option_separator(private_logging, monkeypatch, capsys):
    received = []
    monkeypatch.setitem(
        sys.modules, "radio_wave.ui.app",
        SimpleNamespace(main=lambda args: received.append(args) or 0),
    )
    assert main(["--dev", "--gapplication-service"]) == 0
    assert received[-1] == ["--gapplication-service"]
    assert "mode=dev" in capsys.readouterr().err
    assert main(["--", "--dev"]) == 0
    assert received[-1] == ["--", "--dev"]
    assert "mode=normal" in private_logging.read_text()
    assert capsys.readouterr().err == ""
