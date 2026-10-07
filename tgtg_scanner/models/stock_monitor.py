"""Detect stock / price changes between scan cycles."""

from __future__ import annotations

import logging

from tgtg_scanner.models.item import Item

log = logging.getLogger("tgtg")

# Dynamic bags step down from 1/2 of value to 1/3. Price drops only notify at the
# bottom step; 0.34 allows for cent rounding.
PRICE_DROP_MAX_RATIO = 0.34


class StockMonitor:
    """Tracks previous item snapshots and decides when to notify."""

    def __init__(self, *, price_monitoring: bool = False, notify_on_first_sight: bool = False) -> None:
        self.price_monitoring = price_monitoring
        self.notify_on_first_sight = notify_on_first_sight
        self.state: dict[str, Item] = {}

    def observe(self, item: Item) -> bool:
        """Update state for ``item``. Return True if a notification should be sent."""
        previous = self.state.get(item.item_id)
        notify = False

        if previous is None:
            notify = self.notify_on_first_sight and item.items_available > 0
        else:
            item._previous_price = previous._price
            if previous.items_available != item.items_available:
                log.info(
                    "%s - amount changed from %s to %s",
                    item.display_name,
                    previous.items_available,
                    item.items_available,
                )
                if previous.items_available == 0 and item.items_available > 0:
                    notify = True
            if previous.price != item.price:
                log.info(
                    "%s - price changed from %s to %s",
                    item.display_name,
                    previous.price,
                    item.price,
                )
                if (
                    self.price_monitoring
                    and item.items_available > 0
                    and item._price < previous._price
                    and self._at_price_floor(item)
                ):
                    notify = True

        self.state[item.item_id] = item
        return notify

    @staticmethod
    def _at_price_floor(item: Item) -> bool:
        """True if the price is at most PRICE_DROP_MAX_RATIO of the value, or the value is unknown."""
        if item._value <= 0:
            return True
        return item._price <= PRICE_DROP_MAX_RATIO * item._value
