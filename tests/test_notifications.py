import json

from app import notifications


def test_publish_and_query_are_ordered_and_per_user():
    first = notifications.publish("success", "Track edit complete", "movie.mkv", user="alice")
    second = notifications.publish("error", "Track edit failed", "permission denied", user="bob")
    third = notifications.publish("info", "Scheduled task", "backup done")

    items, latest, more = notifications.query(user="alice")

    assert [item["id"] for item in items] == [first, third]
    assert latest == third
    assert more is False
    assert items[0]["kind"] == "success"
    assert items[1]["kind"] == "info"


def test_after_returns_only_newer_visible_events():
    first = notifications.publish("success", "One", user="alice")
    notifications.publish("error", "Hidden", user="bob")
    third = notifications.publish("warning", "Three", user="alice")

    items, latest, more = notifications.query(after=first, user="alice")

    assert [item["id"] for item in items] == [third]
    assert latest >= third
    assert more is False


def test_query_reports_more_and_limits_pages():
    ids = [
        notifications.publish("info", f"Event {index}")
        for index in range(5)
    ]

    first_page, latest, more = notifications.query(after=0, limit=2)
    assert [item["id"] for item in first_page] == ids[:2]
    assert latest == ids[-1]
    assert more is True

    second_page, _latest, more = notifications.query(after=first_page[-1]["id"], limit=2)
    assert [item["id"] for item in second_page] == ids[2:4]
    assert more is True

    last_page, _latest, more = notifications.query(after=second_page[-1]["id"], limit=2)
    assert [item["id"] for item in last_page] == ids[4:]
    assert more is False

    newest, _latest, more = notifications.query(limit=2)
    assert [item["id"] for item in newest] == ids[-2:]
    assert more is True


def test_unknown_kind_and_non_string_user_are_normalized():
    event_id = notifications.publish("loud", "Title", "Message", user=object())

    items, _latest, _more = notifications.query()

    assert items[-1]["id"] == event_id
    assert items[-1]["kind"] == "info"
    assert items[-1]["user"] is None


def test_store_survives_a_reload():
    event_id = notifications.publish("success", "Persisted", "still here", user="alice")

    # Drop the in-memory copy and make the next read load the JSON file again.
    notifications._reset_for_tests()

    items, latest, _more = notifications.query(user="alice")
    assert items[-1]["id"] == event_id
    assert items[-1]["message"] == "still here"
    assert latest == event_id


def test_store_is_bounded(monkeypatch):
    monkeypatch.setattr(notifications, "MAX_EVENTS", 3)
    ids = [notifications.publish("info", f"Event {index}") for index in range(5)]

    items, _latest, _more = notifications.query()

    assert [item["id"] for item in items] == ids[-3:]


async def test_api_returns_items_and_head():
    event_id = notifications.publish("success", "Done", "all good", user="alice")

    response = await notifications.api_notifications(after=0, limit=50, user="alice")
    data = json.loads(response.body)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert data["ok"] is True
    assert data["latest"] == event_id
    assert data["more"] is False
    assert data["items"][-1]["title"] == "Done"
