"""Verify service teardown finishes independent steps after a backend exception."""

from types import SimpleNamespace

from radio_wave.service import RadioService, _OnlineRequest


def test_service_cancels_worker_detaches_player_and_saves_even_after_errors(tmp_path, monkeypatch, caplog):
    for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    service = RadioService(online=False)
    stages = []

    def fail_player():
        stages.append("player")
        raise RuntimeError("decoder")

    def fail_state():
        stages.append("state")
        raise OSError("write")

    service.player = SimpleNamespace(detach_callbacks=lambda: stages.append("detach"), shutdown=fail_player)
    service.torrents = SimpleNamespace(shutdown=lambda: stages.append("torrents"))
    monkeypatch.setattr(service.state, "save", fail_state)
    monkeypatch.setattr(service.catalogue, "save", lambda: stages.append("catalogue"))
    request = _OnlineRequest(True)
    request.future = service._executor.submit(request.cancelled.wait, 2)
    service._online = request

    service.begin_shutdown()
    assert request.cancelled.is_set()
    assert stages == ["detach"]
    service.quit()
    service.quit()
    assert stages == ["detach", "player", "torrents", "state", "catalogue"]
    assert request.future.done()
    assert "stage=player error_class=RuntimeError" in caplog.text
    assert "stage=state error_class=OSError" in caplog.text
