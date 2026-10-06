"""Move the VPN exit through the gluetun control server."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

log = logging.getLogger("tgtg")


class VpnError(Exception):
    pass


@dataclass
class VpnRequest:
    """Telegram button payload: a new server in ``country``, or the same country when None."""

    country: str | None = None


class Gluetun:
    """Minimal client for the gluetun HTTP control server (``/v1``)."""

    def __init__(self, url: str, api_key: str, timeout: int = 10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # Talk to gluetun directly, never through its own proxy
        self.session.trust_env = False
        self.session.headers["X-API-Key"] = api_key

    def _request(self, method: str, path: str, json: dict | None = None) -> requests.Response:
        try:
            res = self.session.request(method, f"{self.url}/v1{path}", json=json, timeout=self.timeout)
        except requests.RequestException as err:
            raise VpnError(f"gluetun unreachable: {err}") from err
        if not res.ok:
            raise VpnError(f"gluetun {method} {path}: {res.status_code} {res.text.strip()}")
        return res

    def public_ip(self) -> dict:
        """Current exit: public_ip, country, city, ..."""
        return self._request("GET", "/publicip/ip").json()

    def reconnect(self) -> None:
        """Reconnect, which picks a new server with the current selection."""
        self._request("PUT", "/vpn/status", {"status": "stopped"})
        self._request("PUT", "/vpn/status", {"status": "running"})

    def set_location(self, country: str, city: str | None = None) -> None:
        """Select servers in a country (and city) and reconnect."""
        selection = {"countries": [country], "cities": [city] if city else []}
        self._request("PUT", "/vpn/settings", {"provider": {"server_selection": selection}})


def describe_exit(info: dict) -> str:
    place = ", ".join(part for part in (info.get("city"), info.get("country")) if part)
    return f"{place} ({info.get('public_ip', '?')})" if place else str(info.get("public_ip", "unknown"))


class Vpn:
    """Switches the VPN exit and runs ``on_switch`` once the new exit is up."""

    WAIT_TIMEOUT = 90
    WAIT_STEP = 3

    def __init__(self, gluetun: Gluetun, countries: list[str], on_switch: Callable[[], None] | None = None):
        self.gluetun = gluetun
        self.countries = countries
        self.on_switch = on_switch
        self._lock = threading.Lock()

    def current(self) -> str:
        return describe_exit(self.gluetun.public_ip())

    def switch(self, country: str | None = None, city: str | None = None) -> str:
        """New server in the same country when ``country`` is None. Returns the new exit."""
        with self._lock:
            try:
                old_ip = self.gluetun.public_ip().get("public_ip")
            except VpnError:
                old_ip = None
            if country:
                log.info("VPN: switching to %s", ", ".join(p for p in (city, country) if p))
                self.gluetun.set_location(country, city)
            else:
                log.info("VPN: reconnecting to a new server")
                self.gluetun.reconnect()
            info = self._wait_for_new_ip(old_ip)
            if self.on_switch is not None:
                self.on_switch()
            exit_ = describe_exit(info)
            log.info("VPN: exit is now %s", exit_)
            return exit_

    def _wait_for_new_ip(self, old_ip: str | None) -> dict:
        """Wait for a changed public IP; after the timeout accept an unchanged one."""
        deadline = time.monotonic() + self.WAIT_TIMEOUT
        info: dict = {}
        while True:
            time.sleep(self.WAIT_STEP)
            try:
                info = self.gluetun.public_ip()
                if info.get("public_ip") and info["public_ip"] != old_ip:
                    return info
            except VpnError:
                info = {}
            if time.monotonic() > deadline:
                if info.get("public_ip"):
                    return info
                raise VpnError("VPN did not come back up in time")
