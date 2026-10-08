"""Move the VPN exit through the gluetun control server."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("tgtg")

VPN_FILE = "vpn.json"
PROXY_VARS = ("HTTPS_PROXY", "https_proxy")


class VpnError(Exception):
    pass


@dataclass
class VpnRequest:
    """Telegram button payload: a new server in ``country``, or the same country when None.

    ``mode`` ("vpn" or "direct") switches the route instead.
    """

    country: str | None = None
    mode: str | None = None


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
    """Switches the VPN exit and runs ``on_switch`` once the new exit is up.

    Also routes TGTG traffic through the proxy from ``HTTPS_PROXY`` ("vpn") or direct ("direct")
    by setting or clearing the proxy variables, which requests reads on every call. The mode is
    persisted to ``<token_path>/vpn.json``.
    """

    WAIT_TIMEOUT = 90
    WAIT_STEP = 3

    def __init__(
        self,
        gluetun: Gluetun,
        countries: list[str],
        on_switch: Callable[[], None] | None = None,
        token_path: str | None = None,
    ):
        self.gluetun = gluetun
        self.countries = countries
        self.on_switch = on_switch
        self._lock = threading.Lock()
        self._proxy = {var: os.environ[var] for var in PROXY_VARS if os.environ.get(var)}
        self._path = Path(token_path, VPN_FILE) if token_path else None
        self.mode = "vpn"
        if self._load_mode() == "direct" and self._proxy:
            self._apply("direct")
            log.info("VPN: sending TGTG traffic direct (saved mode)")

    @property
    def configured(self) -> bool:
        """True when a proxy is set in the environment at startup."""
        return bool(self._proxy)

    def _load_mode(self) -> str | None:
        if self._path is None or not self._path.is_file():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8")).get("mode")
        except (OSError, ValueError, AttributeError) as err:
            log.error("Error loading VPN mode - %s", err)
            return None

    def _save_mode(self) -> None:
        if self._path is None:
            return
        try:
            self._path.write_text(json.dumps({"mode": self.mode}), encoding="utf-8")
        except OSError as err:
            log.error("Error saving VPN mode - %s", err)

    def _apply(self, mode: str) -> None:
        for var, value in self._proxy.items():
            if mode == "direct":
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self.mode = mode

    def _set_mode(self, mode: str) -> bool:
        """Apply and persist ``mode``. Returns False when it was already set."""
        if not self._proxy:
            raise VpnError("no VPN proxy (HTTPS_PROXY) is configured")
        if mode == self.mode:
            return False
        self._apply(mode)
        self._save_mode()
        log.info("VPN: TGTG traffic now goes %s", "through the VPN" if mode == "vpn" else "direct")
        return True

    def set_mode(self, mode: str) -> str:
        """Switch between "vpn" and "direct", resetting DataDome on a change. Returns the route."""
        with self._lock:
            if self._set_mode(mode) and self.on_switch is not None:
                self.on_switch()
        try:
            return self.current()
        except VpnError as err:
            return f"VPN ({err})"

    def current(self) -> str:
        if self.mode == "direct":
            return "direct (home IP)"
        return describe_exit(self.gluetun.public_ip())

    def switch(self, country: str | None = None, city: str | None = None) -> str:
        """New server in the same country when ``country`` is None. Returns the new exit.

        Switches back to VPN mode first when direct.
        """
        with self._lock:
            if self.mode == "direct":
                self._set_mode("vpn")
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
