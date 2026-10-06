import datetime
import json

from tgtg_scanner.models import Config, Item
from tgtg_scanner.models.stock_monitor import StockMonitor
from tgtg_scanner.models.travel import Travel
from tgtg_scanner.scanner import Scanner


def _raw(item_id: str, available: int, rating: float | None, distance: float = 1234.0) -> dict:
    item: dict = {
        "item_id": item_id,
        "item_price": {"code": "EUR", "minor_units": 399, "decimals": 2},
        "item_value": {"code": "EUR", "minor_units": 1200, "decimals": 2},
    }
    if rating is not None:
        item["average_overall_rating"] = {"average_overall_rating": rating}
    return {
        "items_available": available,
        "display_name": f"Shop {item_id}",
        "distance": distance,
        "item": item,
        "store": {"store_name": "Shop"},
    }


def test_inactive_until_located(tmp_path):
    travel = Travel(token_path=str(tmp_path))
    assert travel.is_active is False
    travel.start(radius=3, min_rating=4.0)
    assert travel.is_active is False
    travel.set_location(47.99, 7.85)
    assert travel.is_active is True
    assert travel.state.radius == 3
    assert travel.state.min_rating == 4.0


def test_persisted(tmp_path):
    travel = Travel(token_path=str(tmp_path))
    travel.start(radius=2, min_rating=4.2)
    travel.set_location(1.5, 2.5)
    saved = json.loads((tmp_path / "travel.json").read_text())
    assert saved["latitude"] == 1.5
    restored = Travel(radius=9, min_rating=1.0, token_path=str(tmp_path))
    assert restored.is_active is True
    assert restored.state.radius == 2


def test_stop_and_expiry(tmp_path):
    travel = Travel(token_path=str(tmp_path))
    travel.set_location(1.0, 2.0)
    travel.stop()
    assert travel.is_active is False

    travel.start(days=1)
    assert travel.is_active is True
    travel._state.until = (datetime.datetime.now() - datetime.timedelta(minutes=1)).isoformat()
    assert travel.is_active is False
    assert travel.state.enabled is False


def test_describe():
    travel = Travel()
    assert travel.describe() == "Travel mode is off."
    travel.start(radius=4, min_rating=4.5)
    assert "waiting for a location" in travel.describe()
    travel.set_location(47.99, 7.85)
    assert "47.9900, 7.8500" in travel.describe()


def test_monitor_notifies_on_first_sight():
    monitor = StockMonitor(notify_on_first_sight=True)
    assert monitor.observe(Item(_raw("1", 2, 4.8))) is True
    assert monitor.observe(Item(_raw("1", 1, 4.8))) is False
    assert monitor.observe(Item(_raw("2", 0, 4.8))) is False
    assert monitor.observe(Item(_raw("2", 3, 4.8))) is True


def test_travel_distance():
    assert Item(_raw("1", 1, 4.0, distance=1234.0)).travel_distance == "1.2 km"
    assert Item({}).travel_distance == "-"


def test_travel_job_filters_and_skips_favorites(mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("TGTG_TOKEN_PATH", str(tmp_path))
    scanner = Scanner(Config())
    scanner.notifiers = mocker.MagicMock()
    scanner.travel.start(radius=3, min_rating=4.5)
    scanner.travel.set_location(47.99, 7.85)

    get_items = mocker.patch.object(
        scanner.tgtg_client,
        "get_items",
        return_value=[_raw("good", 2, 4.7), _raw("low", 2, 3.9), _raw("unrated", 2, None), _raw("empty", 0, 4.9)],
    )
    get_favorites = mocker.patch.object(scanner.tgtg_client, "get_favorites", return_value=[])
    mocker.patch.object(scanner, "_save_tokens")

    scanner._job()

    get_items.assert_called_once_with(favorites_only=False, latitude=47.99, longitude=7.85, radius=3, page_size=50)
    get_favorites.assert_not_called()
    sent = [call.args[0] for call in scanner.notifiers.send.call_args_list]
    assert [item.item_id for item in sent] == ["good"]
    assert sent[0]._travel is True

    scanner._job()
    assert scanner.notifiers.send.call_count == 1

    scanner.travel.stop()
    scanner._job()
    get_favorites.assert_called_once()
    assert scanner.travel_monitor.state == {}
