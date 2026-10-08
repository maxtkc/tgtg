import datetime
import logging
import sys
import time
from pathlib import Path
from random import random
from time import sleep
from typing import NoReturn

import requests
from progress.spinner import Spinner

from tgtg_scanner.errors import TgtgAPIError
from tgtg_scanner.models import (
    Config,
    Cron,
    Favorites,
    Item,
    Location,
    Metrics,
    Reservations,
)
from tgtg_scanner.models.scan_health import ScanHealth
from tgtg_scanner.models.stock_monitor import PriceFloors, StockMonitor
from tgtg_scanner.models.travel import Travel
from tgtg_scanner.models.vpn import Gluetun, Vpn, VpnError
from tgtg_scanner.notifiers import Notifiers
from tgtg_scanner.tgtg_client import BASE_URL, TgtgClient, extract_datadome, normalize_cookie, resolve_user_agent

log = logging.getLogger("tgtg")


class Activity:
    """Activity class that creates a spinner if active is True."""

    def __init__(self, active: bool):
        self.active = active
        self.spinner = None
        if self.active:
            self.spinner = Spinner("Scanning... ")

    def next(self) -> None:
        """Next function that updates the spinner."""
        if self.spinner:
            self.spinner.next()

    def flush(self) -> None:
        """Flush function that flushes the spinner."""
        if self.spinner:
            sys.stdout.write("\x1b[80D\x1b[K")
            sys.stdout.flush()


class Scanner:
    """Main Scanner class."""

    def __init__(self, config: Config):
        self.config = config
        self.metrics = Metrics(self.config.metrics_port)
        self.item_ids = {item_id for item_id in self.config.item_ids if item_id}
        self.cron = self.config.schedule_cron
        floors = PriceFloors(self.config.token_path)
        self.monitor = StockMonitor(price_monitoring=self.config.price_monitoring, floors=floors)
        self.travel = Travel(self.config.travel_radius, self.config.travel_min_rating, self.config.token_path)
        self.travel_monitor = StockMonitor(
            price_monitoring=self.config.price_monitoring, notify_on_first_sight=True, floors=floors
        )
        self.notifiers: Notifiers | None = None
        self.location: Location | None = None
        self.tgtg_client = self._build_client(config)
        self.health = ScanHealth()
        self._scan_ok = False
        self._scan_error: str | None = None
        self.vpn: Vpn | None = None
        if config.gluetun_api_key:
            self.vpn = Vpn(
                Gluetun(config.gluetun_url, config.gluetun_api_key),
                config.vpn_countries,
                on_switch=self._reset_datadome,
            )
        self.reservations = Reservations(self.tgtg_client)
        self.favorites = Favorites(self.tgtg_client)

    @staticmethod
    def _build_client(config: Config) -> TgtgClient:
        tgtg = config.tgtg
        kwargs = {
            "url": tgtg.base_url or BASE_URL,
            "email": tgtg.username,
            "access_token": tgtg.access_token,
            "refresh_token": tgtg.refresh_token,
            "cookie": normalize_cookie(tgtg.datadome),
            "timeout": tgtg.timeout,
            "access_token_lifetime": tgtg.access_token_lifetime,
            "pin_port": config.port,
            "max_polling_tries": tgtg.max_polling_tries,
            "polling_wait_time": tgtg.polling_wait_time,
        }
        user_agent = resolve_user_agent(tgtg.user_agent, tgtg.apk_version)
        if user_agent:
            kwargs["user_agent"] = user_agent
        return TgtgClient(**kwargs)

    @property
    def state(self) -> dict[str, Item]:
        """Current item state from the stock monitor."""
        return self.monitor.state

    def _item_from_api(self, data: dict) -> Item:
        return Item(data, self.location, self.config.locale, self.config.time_format)

    def _save_tokens(self) -> None:
        self.config.save_tokens(
            self.tgtg_client.access_token or "",
            self.tgtg_client.refresh_token or "",
            extract_datadome(self.tgtg_client),
        )

    def _reset_datadome(self) -> None:
        """Drop the DataDome cookie after an IP change; the client fetches a fresh one."""
        self.tgtg_client.forget_datadome()
        if self.config.token_path is None:
            return
        cookie = Path(self.config.token_path, "datadome")
        if cookie.is_file():
            parked = cookie.with_name(f"datadome.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
            cookie.rename(parked)
            log.info("Parked DataDome cookie as %s", parked.name)

    def _api_ok(self) -> None:
        self._scan_ok = True

    def _api_error(self, err: Exception) -> None:
        log.error(err)
        status = str(err.args[0]) if isinstance(err, TgtgAPIError) and err.args else "conn"
        self._scan_error = status
        self.metrics.api_errors.labels(status).inc()

    def _check_health(self) -> None:
        """Report a run of failed scans once, and its recovery once."""
        if self.notifiers is None:
            return
        if self._scan_ok:
            self.metrics.last_scan_success.set(time.time())
            if self.health.record_success():
                self.notifiers.send_notice("TGTG scans work again.")
        elif self._scan_error is not None and self.health.record_failure(self._scan_error):
            self.notifiers.send_notice(self._block_message(), vpn_buttons=self.vpn is not None)

    def _block_message(self) -> str:
        status = self.health.status
        since = f"{self.health.since:%H:%M}" if self.health.since else "?"
        if status == "403":
            text = f"TGTG is blocking this IP: {self.health.failures} scans in a row got 403, since {since}."
        elif status == "conn":
            text = f"TGTG is unreachable (connection or proxy error) for {self.health.failures} scans, since {since}."
        else:
            text = f"TGTG scans failing with HTTP {status} for {self.health.failures} scans, since {since}."
        if self.vpn is not None:
            try:
                text += f"\nVPN exit: {self.vpn.current()}"
            except VpnError as err:
                text += f"\nVPN: {err}"
            text += "\nPick a new exit below, or use /vpn."
        return text

    def _get_test_item(self) -> Item:
        """Returns an item for test notifications."""
        items = sorted(self._load_favorite_items(), key=lambda x: x.items_available, reverse=True)
        if items:
            return items[0]
        items = sorted(
            [
                self._item_from_api(item)
                for item in self.tgtg_client.get_items(
                    favorites_only=False,
                    latitude=53.5511,
                    longitude=9.9937,
                    radius=50,
                )
            ],
            key=lambda x: x.items_available,
            reverse=True,
        )
        return items[0]

    def _load_items(self) -> list[Item]:
        """Fetch configured item IDs and account favorites as Items."""
        items: list[Item] = []
        for item_id in self.item_ids:
            try:
                items.append(self._item_from_api(self.tgtg_client.get_item(item_id)))
                self._api_ok()
            except (TgtgAPIError, requests.RequestException) as err:
                self._api_error(err)
        items.extend(self._load_favorite_items())
        return items

    def _load_favorite_items(self) -> list[Item]:
        try:
            items = [self._item_from_api(item) for item in self.tgtg_client.get_favorites()]
            self._api_ok()
            return items
        except (TgtgAPIError, requests.RequestException) as err:
            self._api_error(err)
            self.metrics.get_favorites_errors.inc()
            return []

    def _job(self) -> None:
        """Job iterates over all monitored items."""
        if self.notifiers is None:
            raise RuntimeError("Notifiers not initialized!")

        self._scan_ok = False
        self._scan_error = None
        travel_active = self.travel.is_active
        if travel_active:
            self._travel_job()
        else:
            self.travel_monitor.state.clear()

        if not (travel_active and self.config.travel_skip_favorites):
            for item in self._load_items():
                if self.monitor.observe(item):
                    self._send_messages(item)
                    self.metrics.send_notifications.labels(item.item_id, item.display_name).inc()
                self.metrics.update(item)

            amounts = {item_id: item.items_available for item_id, item in self.monitor.state.items()}
            log.debug("new State: %s", amounts)
            self.reservations.make_orders(self.monitor.state, self.notifiers.send)

            if not self.monitor.state:
                log.warning("No items in observation! Did you add any favorites?")

        self._check_health()
        self._save_tokens()

    def _travel_job(self) -> None:
        """Notify on well rated bags around the travel location.

        Bags are reported when first seen in stock and when they come back into stock.
        """
        state = self.travel.state
        try:
            data = self.tgtg_client.get_items(
                favorites_only=False,
                latitude=state.latitude,
                longitude=state.longitude,
                radius=state.radius,
                page_size=50,
            )
            self._api_ok()
        except (TgtgAPIError, requests.RequestException) as err:
            self._api_error(err)
            return
        for raw in data:
            item = self._item_from_api(raw)
            if item._rating is None or item._rating < state.min_rating:
                continue
            item._travel = True
            if self.travel_monitor.observe(item):
                self._send_messages(item)
        log.debug("Travel mode: %s items, %s matching", len(data), len(self.travel_monitor.state))

    def _send_messages(self, item: Item) -> None:
        """Send notifications for Item."""
        if self.notifiers is None:
            raise RuntimeError("Notifiers not initialized!")

        log.info(
            "Sending notifications for %s - %s bags available",
            item.display_name,
            item.items_available,
        )
        self.notifiers.send(item)

    def run(self) -> NoReturn:
        """Main Loop of the Scanner."""
        # test tgtg API
        api_ok = True
        try:
            self.tgtg_client.login()
            self._save_tokens()
        except (TgtgAPIError, requests.RequestException) as err:
            # With stored tokens, start anyway so the bot can report the block and switch the VPN.
            if not (self.tgtg_client.access_token and self.tgtg_client.refresh_token):
                raise
            log.warning("TGTG login failed, starting without it: %s", err)
            api_ok = False
        # activate location service
        self.location = Location(
            self.config.location.enabled,
            self.config.location.google_maps_api_key,
            self.config.location.origin_address,
        )
        # activate and test notifiers
        if self.config.metrics:
            self.metrics.enable_metrics()
        self.notifiers = Notifiers(self.config, self.reservations, self.favorites, self.travel, self.vpn)
        self.notifiers.start()
        if api_ok and not self.config.disable_tests and self.notifiers.notifier_count > 0:
            log.info("Sending test Notifications ...")
            self.notifiers.send(self._get_test_item())
        # start scanner
        log.info("Scanner started ...")
        running = True
        if self.cron != Cron("* * * * *"):
            log.info("Active on schedule: %s", self.cron.get_description(self.config.locale))
        activity = Activity(self.config.activity and not (self.config.docker or self.config.quiet))
        while True:
            if self.cron.is_now:
                if not running:
                    log.info("Scanner reenabled by cron schedule.")
                    running = True
                try:
                    self._job()
                except Exception:
                    log.error("Job Error! - %s", sys.exc_info())
                finally:
                    sleep_time = self.config.sleep_time * (0.9 + 0.2 * random())
                    steps = max(int(sleep_time), 1)
                    for _ in range(steps):
                        activity.next()
                        sleep(sleep_time / steps)
                        activity.flush()
            elif running:
                log.info("Scanner disabled by cron schedule.")
                running = False
            else:
                sleep(60)

    def stop(self) -> None:
        """Stop scanner."""
        if self.notifiers:
            self.notifiers.stop()

    def get_credentials(self) -> dict:
        """Returns current tgtg credentials.

        Returns:
            dict: dictionary containing access token, refresh token and datadome cookie

        """
        return self.tgtg_client.get_credentials()

    def get_items(self, lat, lng, radius) -> list[dict]:
        """Get items by geographic position.

        Args:
            lat (float): latitude
            lng (float): longitude
            radius (int): radius in meter

        Returns:
            List: List of found items

        """
        return self.tgtg_client.get_items(
            favorites_only=False,
            latitude=lat,
            longitude=lng,
            radius=radius,
        )

    def get_favorites(self) -> list[dict]:
        """Returns favorites of the current tgtg account.

        Returns:
            List: List of items

        """
        return self.tgtg_client.get_favorites()

    def set_favorite(self, item_id: str) -> None:
        """Add item to favorites.

        Args:
            item_id (str): Item ID

        """
        self.tgtg_client.set_favorite(item_id=item_id, is_favorite=True)

    def unset_favorite(self, item_id: str) -> None:
        """Remove item from favorites.

        Args:
            item_id (str): Item ID

        """
        self.tgtg_client.set_favorite(item_id=item_id, is_favorite=False)

    def unset_all_favorites(self) -> None:
        """Remove all items from favorites."""
        item_ids = [item.get("item", {}).get("item_id") for item in self.get_favorites()]
        for item_id in item_ids:
            if item_id:
                self.unset_favorite(item_id)


if __name__ == "__main__":
    print("Please use __main__.py.")
