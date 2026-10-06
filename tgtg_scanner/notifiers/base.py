import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from queue import Queue

from tgtg_scanner.models import Config, Cron, Favorites, Item, Reservations
from tgtg_scanner.models.reservations import Reservation
from tgtg_scanner.models.travel import Travel
from tgtg_scanner.models.vpn import Vpn

log = logging.getLogger("tgtg")


@dataclass
class Notice:
    """Plain status message from the scanner, optionally with VPN switch buttons."""

    text: str
    vpn_buttons: bool = False


class Notifier(ABC):
    """Base Notifier."""

    travel: Travel | None = None
    vpn: Vpn | None = None

    @abstractmethod
    def __init__(self, config: Config, reservations: Reservations, favorites: Favorites):
        self.config = config
        self.enabled = False
        self.reservations = reservations
        self.favorites = favorites
        self.cron = Cron()
        self.thread = threading.Thread(target=self._run)
        self.queue: Queue[Item | Reservation | Notice | None] = Queue()

    @property
    def name(self):
        """Get notifier name."""
        return self.__class__.__name__

    def _run(self) -> None:
        """Run notifier."""
        self.config.set_locale()
        while True:
            try:
                item = self.queue.get()
                if item is None:
                    break
                if isinstance(item, Notice):
                    continue
                log.debug("Sending %s Notification", self.name)
                self._send(item)
            except KeyboardInterrupt:
                pass
            except Exception as exc:
                log.error("Failed sending %s: %s", self.name, exc)

    def start(self) -> None:
        """Run notifier in thread."""
        if self.enabled:
            log.debug("Starting %s Notifier thread", self.name)
            self.thread.start()

    def send(self, item: Item | Reservation) -> None:
        """Send notification."""
        if not isinstance(item, (Item, Reservation)):
            log.error("Invalid item type: %s", type(item))
            return
        if self.enabled and self.cron.is_now:
            self.queue.put(item)
            if not self.thread.is_alive():
                log.debug("%s Notifier thread is dead. Restarting", self.name)
                self.thread = threading.Thread(target=self._run)
                self.start()

    def send_notice(self, notice: Notice) -> None:  # noqa: B027
        """Send a status notice. Ignored by notifiers that do not support it."""

    @abstractmethod
    def _send(self, item: Item | Reservation) -> None:
        """Send Item information."""

    def stop(self) -> None:
        """Stop notifier."""
        if self.thread.is_alive():
            log.debug("Stopping %s Notifier thread", self.name)
            self.queue.put(None)
            self.thread.join()
            log.debug("%s Notifier thread stopped", self.name)

    @abstractmethod
    def __repr__(self) -> str:
        pass
