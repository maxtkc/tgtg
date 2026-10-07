import datetime

from tgtg_scanner.models.item import Item
from tgtg_scanner.models.stock_monitor import PriceFloors, StockMonitor


def _item(item_id: str, available: int, price_minor: int = 499) -> Item:
    return Item(
        {
            "items_available": available,
            "display_name": f"Shop {item_id}",
            "favorite": True,
            "item": {
                "item_id": item_id,
                "item_price": {"code": "EUR", "minor_units": price_minor, "decimals": 2},
                "item_value": {"code": "EUR", "minor_units": 1500, "decimals": 2},
            },
            "store": {"store_name": "Shop"},
        }
    )


def test_no_notify_on_first_sight():
    monitor = StockMonitor()
    assert monitor.observe(_item("1", 2)) is False
    assert monitor.state["1"].items_available == 2


def test_notify_on_stock_available():
    monitor = StockMonitor()
    monitor.observe(_item("1", 0))
    assert monitor.observe(_item("1", 3)) is True


def test_no_notify_when_already_in_stock():
    monitor = StockMonitor()
    monitor.observe(_item("1", 2))
    assert monitor.observe(_item("1", 5)) is False


def test_price_drop_ignored_without_flag():
    monitor = StockMonitor(price_monitoring=False)
    monitor.observe(_item("1", 1, price_minor=500))
    assert monitor.observe(_item("1", 1, price_minor=400)) is False


def test_notify_on_price_drop_to_new_floor():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 1, price_minor=750))
    assert monitor.observe(_item("1", 1, price_minor=600)) is True
    assert monitor.observe(_item("1", 1, price_minor=500)) is True


def test_dynamic_restock_above_floor_is_silent():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 1, price_minor=500))
    monitor.observe(_item("1", 0, price_minor=750))
    assert monitor.observe(_item("1", 1, price_minor=750)) is False
    assert monitor.observe(_item("1", 1, price_minor=500)) is True


def test_fixed_price_bag_notifies_on_restock():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 0, price_minor=750))
    assert monitor.observe(_item("1", 2, price_minor=750)) is True


def test_restock_at_floor_after_drop_while_sold_out():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 0, price_minor=750))
    monitor.observe(_item("1", 0, price_minor=500))
    assert monitor.observe(_item("1", 1, price_minor=500)) is True


def test_no_notify_on_stock_change_at_floor():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 3, price_minor=500))
    assert monitor.observe(_item("1", 2, price_minor=500)) is False


def test_notify_each_time_floor_is_reached():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 1, price_minor=500))
    monitor.observe(_item("1", 1, price_minor=750))
    assert monitor.observe(_item("1", 1, price_minor=500)) is True
    monitor.observe(_item("1", 1, price_minor=750))
    assert monitor.observe(_item("1", 1, price_minor=500)) is True


def test_cent_rounding_counts_as_floor():
    monitor = StockMonitor(price_monitoring=True)
    monitor.observe(_item("1", 0, price_minor=495))
    assert monitor.observe(_item("1", 1, price_minor=500)) is True


def test_first_sight_above_known_floor_is_silent():
    floors = PriceFloors()
    floors.update(_item("1", 1, price_minor=500))
    monitor = StockMonitor(price_monitoring=True, notify_on_first_sight=True, floors=floors)
    assert monitor.observe(_item("1", 1, price_minor=750)) is False


def test_floors_persist(tmp_path):
    PriceFloors(str(tmp_path)).update(_item("1", 1, price_minor=500))
    floors = PriceFloors(str(tmp_path))
    assert floors.get("1") == 5 / 15
    assert floors.at_floor(_item("1", 1, price_minor=750)) is False


def test_floor_expires_when_not_reached():
    floors = PriceFloors()
    start = datetime.date(2026, 10, 1)
    floors.update(_item("1", 1, price_minor=500), today=start)
    floors.update(_item("1", 1, price_minor=750), today=start + datetime.timedelta(days=7))
    assert floors.get("1") == 5 / 15
    floors.update(_item("1", 1, price_minor=750), today=start + datetime.timedelta(days=8))
    assert floors.get("1") == 7.5 / 15


def test_floor_refreshed_when_reached():
    floors = PriceFloors()
    start = datetime.date(2026, 10, 1)
    floors.update(_item("1", 1, price_minor=500), today=start)
    floors.update(_item("1", 1, price_minor=500), today=start + datetime.timedelta(days=6))
    floors.update(_item("1", 1, price_minor=750), today=start + datetime.timedelta(days=12))
    assert floors.get("1") == 5 / 15
