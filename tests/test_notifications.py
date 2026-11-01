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


def test_publish_captures_the_tabs_log_lines_for_the_details():
    from app import logs

    started = 1000.0
    logs.log("Search", "INFO", "scan started")
    logs.log("Search", "WARN", "movie.mkv: ffprobe timed out")
    notifications.publish(
        "warning",
        "Media scan complete with warnings",
        "1 probed, 1 could not be probed",
        tab="Search",
        started=started,
    )

    event = notifications.get(notifications.latest_id())
    assert event is not None
    assert event["tab"] == "Search"
    assert [line["message"] for line in event["details"]] == [
        "scan started",
        "movie.mkv: ffprobe timed out",
    ]
    assert [line["level"] for line in event["details"]] == ["INFO", "WARN"]


def test_list_events_omit_details_but_report_what_they_contain(monkeypatch):
    from app import logs

    monkeypatch.setattr(notifications, "DETAIL_WINDOW_SECONDS", 10**9)
    logs.log("Search", "INFO", "started")
    logs.log("Search", "WARN", "one subtitle missing")
    logs.log("Search", "ERROR", "one file failed")
    event_id = notifications.publish("warning", "Scan done", tab="Search")

    items, _latest, _more = notifications.query()
    item = next(e for e in items if e["id"] == event_id)

    assert "details" not in item
    assert item["detail_count"] == 3
    assert item["warning_count"] == 2
    assert item["error_count"] == 1

    full = notifications.get(event_id)
    assert [line["message"] for line in full["details"]] == [
        "started",
        "one subtitle missing",
        "one file failed",
    ]


def test_details_are_persisted_with_the_event():
    from app import logs

    logs.log("Backup", "INFO", "Backup created: nightly.zip (1024 bytes)")
    event_id = notifications.publish("success", "Backup complete", tab="Backup")

    notifications._reset_for_tests()

    event = notifications.get(event_id)
    assert event is not None
    assert event["details"][0]["message"].startswith("Backup created")


def test_get_respects_the_event_owner():
    event_id = notifications.publish("success", "Private", user="alice")
    assert notifications.get(event_id, "alice") is not None
    assert notifications.get(event_id, "bob") is None
    assert notifications.get("not-a-number") is None


def test_captured_details_keep_warnings_and_the_summary_within_the_limit():
    from app import logs

    for index in range(20):
        logs.log("Search", "INFO", f"file {index}")
        if index == 5:
            logs.log("Search", "WARN", "file 5 failed")

    lines = logs.recent("Search", limit=5)

    messages = [line["message"] for line in lines]
    assert len(lines) == 5
    assert "file 5 failed" in messages
    assert messages[-1] == "file 19"


async def test_detail_endpoint_returns_log_lines_and_404s():
    from app import logs

    logs.log("Search", "WARN", "movie.mkv: ffprobe timed out")
    event_id = notifications.publish("warning", "Scan complete with warnings", tab="Search")

    response = await notifications.api_notification(str(event_id), user="alice")
    data = json.loads(response.body)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert data["item"]["details"][0]["message"] == "movie.mkv: ffprobe timed out"

    missing = await notifications.api_notification("999999", user="alice")
    assert missing.status_code == 404
