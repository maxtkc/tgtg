"""Travel mode: scan around a shared location instead of (or as well as) favorites."""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("tgtg")

TRAVEL_FILE = "travel.json"


@dataclass
class TravelState:
    latitude: float | None = None
    longitude: float | None = None
    radius: int = 5
    min_rating: float = 4.5
    until: str | None = None
    enabled: bool = False


class Travel:
    """Thread-safe travel mode state, shared by the scanner and the Telegram bot.

    Persisted to ``<token_path>/travel.json`` when a token path is set.
    """

    def __init__(self, radius: int = 5, min_rating: float = 4.5, token_path: str | None = None):
        self._lock = threading.Lock()
        self._path = Path(token_path, TRAVEL_FILE) if token_path else None
        self._state = TravelState(radius=radius, min_rating=min_rating)
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = TravelState(**{k: v for k, v in data.items() if k in TravelState.__dataclass_fields__})
            log.info("Loaded travel mode state: %s", self.describe())
        except (OSError, ValueError, TypeError) as err:
            log.error("Error loading travel mode state - %s", err)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.write_text(json.dumps(asdict(self._state)), encoding="utf-8")
        except OSError as err:
            log.error("Error saving travel mode state - %s", err)

    def start(self, radius: int | None = None, min_rating: float | None = None, days: float | None = None) -> None:
        """Enable travel mode. Scanning begins once a location is set."""
        with self._lock:
            if radius is not None:
                self._state.radius = radius
            if min_rating is not None:
                self._state.min_rating = min_rating
            self._state.until = (
                (datetime.datetime.now() + datetime.timedelta(days=days)).isoformat(timespec="minutes") if days else None
            )
            self._state.enabled = True
            self._save()

    def set_location(self, latitude: float, longitude: float) -> None:
        """Set the search center and enable travel mode."""
        with self._lock:
            self._state.latitude = latitude
            self._state.longitude = longitude
            self._state.enabled = True
            self._save()

    def stop(self) -> None:
        """Disable travel mode, keeping the last settings."""
        with self._lock:
            self._state.enabled = False
            self._state.until = None
            self._save()

    @property
    def has_location(self) -> bool:
        return self._state.latitude is not None and self._state.longitude is not None

    @property
    def is_active(self) -> bool:
        """True when enabled, located and not expired. Expiry disables travel mode."""
        with self._lock:
            if not self._state.enabled:
                return False
            if self._state.until and datetime.datetime.fromisoformat(self._state.until) < datetime.datetime.now():
                log.info("Travel mode expired")
                self._state.enabled = False
                self._state.until = None
                self._save()
                return False
        return self.has_location

    @property
    def state(self) -> TravelState:
        with self._lock:
            return TravelState(**asdict(self._state))

    def describe(self) -> str:
        s = self.state
        if not s.enabled:
            return "Travel mode is off."
        where = f"{s.latitude:.4f}, {s.longitude:.4f}" if self.has_location else "waiting for a location"
        until = f" until {s.until.replace('T', ' ')}" if s.until else ""
        return f"Travel mode on{until}: {where}, radius {s.radius} km, rating >= {s.min_rating}."
