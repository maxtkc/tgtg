import json
import os
from unittest.mock import MagicMock

import pytest
import responses
from prometheus_client import REGISTRY

from tgtg_scanner.errors import TgtgAPIError
from tgtg_scanner.models import Config
from tgtg_scanner.models.scan_health import ScanHealth
from tgtg_scanner.models.vpn import Gluetun, Vpn, VpnError, describe_exit
from tgtg_scanner.scanner import Scanner

URL = "http://gluetun:8000"


@pytest.fixture
def fresh_metrics():
    """Unregister tgtg collectors so each Scanner can register its own."""
    for collector, names in list(REGISTRY._collector_to_names.items()):
        if any(name.startswith("tgtg_") for name in names):
            REGISTRY.unregister(collector)


def _ip(ip: str, city: str = "New York City", country: str = "United States") -> dict:
    return {"public_ip": ip, "city": city, "country": country}


def test_scan_health_reports_once():
    health = ScanHealth(threshold=3)
    assert health.record_failure("403") is False
    assert health.record_failure("403") is False
    assert health.record_failure("403") is True
    assert health.record_failure("403") is False
    assert health.failures == 4
    assert health.record_success() is True
    assert health.record_success() is False
    assert health.record_failure("conn") is False
    assert health.record_success() is False


@responses.activate
def test_gluetun_requests():
    responses.get(f"{URL}/v1/publicip/ip", json=_ip("1.1.1.1"))
    status = responses.put(f"{URL}/v1/vpn/status", json={"outcome": "ok"})
    settings = responses.put(f"{URL}/v1/vpn/settings", body="settings updated")
    gluetun = Gluetun(URL, "key")

    assert gluetun.public_ip()["public_ip"] == "1.1.1.1"
    assert responses.calls[0].request.headers["X-API-Key"] == "key"

    gluetun.reconnect()
    assert [json.loads(c.request.body) for c in status.calls] == [{"status": "stopped"}, {"status": "running"}]

    gluetun.set_location("Germany", "Berlin")
    gluetun.set_location("Germany")
    bodies = [json.loads(c.request.body) for c in settings.calls]
    assert bodies[0] == {"provider": {"server_selection": {"countries": ["Germany"], "cities": ["Berlin"]}}}
    assert bodies[1]["provider"]["server_selection"]["cities"] == []


@responses.activate
def test_gluetun_error():
    responses.put(f"{URL}/v1/vpn/settings", status=400, body="country 'Atlantis' is not valid")
    with pytest.raises(VpnError, match="Atlantis"):
        Gluetun(URL, "key").set_location("Atlantis")


def test_vpn_switch_waits_for_new_ip(mocker):
    mocker.patch("tgtg_scanner.models.vpn.time.sleep")
    gluetun = MagicMock()
    gluetun.public_ip.side_effect = [_ip("1.1.1.1"), _ip("1.1.1.1"), _ip("2.2.2.2", "Berlin", "Germany")]
    on_switch = MagicMock()
    vpn = Vpn(gluetun, ["Germany"], on_switch=on_switch)

    assert vpn.switch("Germany") == "Berlin, Germany (2.2.2.2)"
    gluetun.set_location.assert_called_once_with("Germany", None)
    gluetun.reconnect.assert_not_called()
    on_switch.assert_called_once()


def test_vpn_switch_accepts_same_ip_after_timeout(mocker):
    mocker.patch("tgtg_scanner.models.vpn.time.sleep")
    mocker.patch("tgtg_scanner.models.vpn.time.monotonic", side_effect=[0, 1, 1000])
    gluetun = MagicMock()
    gluetun.public_ip.return_value = _ip("1.1.1.1")
    vpn = Vpn(gluetun, [])
    assert vpn.switch() == "New York City, United States (1.1.1.1)"
    gluetun.reconnect.assert_called_once()


def test_describe_exit():
    assert describe_exit({"public_ip": "1.1.1.1"}) == "1.1.1.1"


def test_scanner_block_notice_and_recovery(mocker, tmp_path, monkeypatch, fresh_metrics):
    monkeypatch.setenv("TGTG_TOKEN_PATH", str(tmp_path))
    config = Config()
    config.gluetun_api_key = "key"
    scanner = Scanner(config)
    scanner.notifiers = mocker.MagicMock()
    mocker.patch.object(scanner, "_save_tokens")
    mocker.patch.object(scanner.vpn, "current", return_value="New York City, United States (1.1.1.1)")
    get_favorites = mocker.patch.object(scanner.tgtg_client, "get_favorites", side_effect=TgtgAPIError(403, b"captcha"))
    errors = scanner.metrics.api_errors.labels("403")
    before = errors._value.get()

    for _ in range(4):
        scanner._job()

    assert errors._value.get() - before == 4
    scanner.notifiers.send_notice.assert_called_once()
    text = scanner.notifiers.send_notice.call_args.args[0]
    assert "403" in text and "New York City" in text
    assert scanner.notifiers.send_notice.call_args.kwargs["vpn_buttons"] is True

    get_favorites.side_effect = None
    get_favorites.return_value = []
    scanner._job()
    assert scanner.notifiers.send_notice.call_count == 2
    assert scanner.notifiers.send_notice.call_args.args[0] == "TGTG scans work again."
    assert scanner.metrics.last_scan_success._value.get() > 0


def test_scanner_reset_datadome_parks_cookie(tmp_path, monkeypatch, fresh_metrics):
    monkeypatch.setenv("TGTG_TOKEN_PATH", str(tmp_path))
    scanner = Scanner(Config())
    scanner.tgtg_client.cookie = "datadome=abc"
    scanner.tgtg_client.session.cookies.set("datadome", "abc")
    (tmp_path / "datadome").write_text("abc")

    scanner._reset_datadome()

    assert scanner.tgtg_client.cookie is None
    assert "datadome" not in scanner.tgtg_client.session.cookies
    assert not (tmp_path / "datadome").exists()
    assert len(list(tmp_path.glob("datadome.bak-*"))) == 1


def test_vpn_mode_toggles_proxy_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://gluetun:8888")
    monkeypatch.delenv("https_proxy", raising=False)
    gluetun = MagicMock()
    gluetun.public_ip.return_value = _ip("1.1.1.1")
    on_switch = MagicMock()
    vpn = Vpn(gluetun, [], on_switch=on_switch, token_path=str(tmp_path))
    assert vpn.mode == "vpn"

    assert vpn.set_mode("direct") == "direct (home IP)"
    assert "HTTPS_PROXY" not in os.environ
    assert json.loads((tmp_path / "vpn.json").read_text()) == {"mode": "direct"}
    on_switch.assert_called_once()
    vpn.set_mode("direct")
    on_switch.assert_called_once()
    gluetun.public_ip.assert_not_called()

    # A restart keeps direct mode, using the proxy from the environment to switch back
    monkeypatch.setenv("HTTPS_PROXY", "http://gluetun:8888")
    restarted = Vpn(gluetun, [], on_switch=on_switch, token_path=str(tmp_path))
    assert restarted.mode == "direct"
    assert "HTTPS_PROXY" not in os.environ

    assert restarted.set_mode("vpn") == "New York City, United States (1.1.1.1)"
    assert os.environ["HTTPS_PROXY"] == "http://gluetun:8888"
    assert json.loads((tmp_path / "vpn.json").read_text()) == {"mode": "vpn"}
    assert on_switch.call_count == 2


def test_vpn_mode_without_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    vpn = Vpn(MagicMock(), [])
    assert vpn.configured is False
    with pytest.raises(VpnError, match="HTTPS_PROXY"):
        vpn.set_mode("direct")


def test_vpn_switch_from_direct_restores_proxy(mocker, monkeypatch):
    mocker.patch("tgtg_scanner.models.vpn.time.sleep")
    monkeypatch.setenv("HTTPS_PROXY", "http://gluetun:8888")
    gluetun = MagicMock()
    gluetun.public_ip.side_effect = [_ip("1.1.1.1"), _ip("2.2.2.2", "Berlin", "Germany")]
    vpn = Vpn(gluetun, [])
    vpn._apply("direct")

    assert vpn.switch("Germany") == "Berlin, Germany (2.2.2.2)"
    assert vpn.mode == "vpn"
    assert os.environ["HTTPS_PROXY"] == "http://gluetun:8888"


def test_scanner_block_notice_direct(mocker, tmp_path, monkeypatch, fresh_metrics):
    monkeypatch.setenv("TGTG_TOKEN_PATH", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://gluetun:8888")
    (tmp_path / "vpn.json").write_text('{"mode": "direct"}')
    config = Config()
    config.gluetun_api_key = "key"
    scanner = Scanner(config)
    current = mocker.patch.object(scanner.vpn.gluetun, "public_ip")
    scanner.health.record_failure("403")

    text = scanner._block_message()
    assert "direct (home IP)" in text and "/vpn on" in text
    current.assert_not_called()
