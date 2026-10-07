"""Detect stock / price changes between scan cycles."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from tgtg_scanner.models.item import Item

log = logging.getLogger("tgtg")

FLOORS_FILE = "price_floors.json"
# Absorbs cent rounding between scans of the same bag.
FLOOR_TOLERANCE = 0.01
# A floor not reached again for this long is replaced by the current price.
FLOOR_MAX_AGE = datetime.timedelta(days=7)


def _ratio(item: Item) -> float | None:
    if item._value <= 0:
        return None
    return item._price / item._value


class PriceFloors:
    """Lowest price/value ratio seen per item and the last day it was seen.

    Persisted to ``<token_path>/price_floors.json`` when a token path is set.
    """

    def __init__(self, token_path: str | None = None):
        self._path = Path(token_path, FLOORS_FILE) if token_path else None
        self._floors: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._floors = {k: {"ratio": float(v["ratio"]), "seen": str(v["seen"])} for k, v in data.items()}
            log.info("Loaded price floors for %s items", len(self._floors))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as err:
            log.error("Error loading price floors - %s", err)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.write_text(json.dumps(self._floors), encoding="utf-8")
        except OSError as err:
            log.error("Error saving price floors - %s", err)

    def get(self, item_id: str) -> float | None:
        entry = self._floors.get(item_id)
        return entry["ratio"] if entry else None

    def update(self, item: Item, today: datetime.date | None = None) -> None:
        """Lower the item's floor to its current ratio, or refresh it when at the floor."""
        ratio = _ratio(item)
        if ratio is None:
            return
        today = today or datetime.date.today()
        entry = self._floors.get(item.item_id)
        if (
            entry is None
            or ratio < entry["ratio"] - FLOOR_TOLERANCE
            or today - datetime.date.fromisoformat(entry["seen"]) > FLOOR_MAX_AGE
        ):
            if entry is not None:
                log.info("%s - price floor %.3f -> %.3f of value", item.display_name, entry["ratio"], ratio)
            self._floors[item.item_id] = {"ratio": ratio, "seen": today.isoformat()}
            self._save()
        elif ratio <= entry["ratio"] + FLOOR_TOLERANCE:
            entry["ratio"] = min(entry["ratio"], ratio)
            if entry["seen"] != today.isoformat():
                entry["seen"] = today.isoformat()
                self._save()

    def at_floor(self, item: Item) -> bool:
        """True if the price is at the item's lowest seen ratio, or the value is unknown."""
        ratio = _ratio(item)
        floor = self.get(item.item_id)
        if ratio is None or floor is None:
            return True
        return ratio <= floor + FLOOR_TOLERANCE


class StockMonitor:
    """Tracks previous item snapshots and decides when to notify.

    An item is buyable when in stock and, with price monitoring, at its price
    floor. Notifies when an item becomes buyable, so a dynamic bag restocked at
    1/2 of value stays silent and notifies once it steps down to its floor.
    """

    def __init__(
        self,
        *,
        price_monitoring: bool = False,
        notify_on_first_sight: bool = False,
        floors: PriceFloors | None = None,
    ) -> None:
        self.price_monitoring = price_monitoring
        self.notify_on_first_sight = notify_on_first_sight
        self.floors = floors if floors is not None else PriceFloors()
        self.state: dict[str, Item] = {}

    def _buyable(self, item: Item) -> bool:
        if item.items_available <= 0:
            return False
        return not self.price_monitoring or self.floors.at_floor(item)

    def observe(self, item: Item) -> bool:
        """Update state for ``item``. Return True if a notification should be sent."""
        previous = self.state.get(item.item_id)
        self.state[item.item_id] = item
        if self.price_monitoring:
            self.floors.update(item)

        if previous is None:
            return self.notify_on_first_sight and self._buyable(item)

        item._previous_price = previous._price
        if previous.items_available != item.items_available:
            log.info(
                "%s - amount changed from %s to %s",
                item.display_name,
                previous.items_available,
                item.items_available,
            )
        if previous.price != item.price:
            log.info(
                "%s - price changed from %s to %s",
                item.display_name,
                previous.price,
                item.price,
            )
        return self._buyable(item) and not self._buyable(previous)
