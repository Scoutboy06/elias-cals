import httpx
import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar

import transforms
from adapter import adapt, create_app

FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
X-WR-CALNAME:Original
BEGIN:VTIMEZONE
TZID:Europe/Stockholm
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:stable-123
DTSTAMP:20261001T120000Z
DTSTART;TZID=Europe/Stockholm:20261023T100000
DTEND;TZID=Europe/Stockholm:20261023T120000
RRULE:FREQ=WEEKLY;COUNT=3
EXDATE;TZID=Europe/Stockholm:20261030T100000
SUMMARY;LANGUAGE=en:TDA123 Lecture
LOCATION:HB1
DESCRIPTION:First line\\nSecond line
X-CUSTOM:Keep me
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:stable-123
DTSTAMP:20261001T120000Z
RECURRENCE-ID;TZID=Europe/Stockholm:20261106T100000
DTSTART;TZID=Europe/Stockholm:20261106T130000
DTEND;TZID=Europe/Stockholm:20261106T150000
SUMMARY:Exception
END:VEVENT
END:VCALENDAR
"""


def test_default_transform_preserves_calendar():
    assert Calendar.from_ical(adapt(FEED)) == Calendar.from_ical(FEED)


def test_custom_fields_and_parameters(monkeypatch):
    monkeypatch.setattr(transforms, "TITLE_REPLACEMENTS", {"TDA123": "📚 Algorithms"})
    monkeypatch.setattr(transforms, "LOCATION_REPLACEMENTS", {"HB1": "Hörsal HB1"})
    monkeypatch.setattr(transforms, "format_description", lambda value, event: value + "\nExtra")
    calendar = Calendar.from_ical(adapt(FEED))
    event = calendar.walk("VEVENT")[0]
    assert str(event["SUMMARY"]) == "📚 Algorithms Lecture"
    assert event["SUMMARY"].params["LANGUAGE"] == "en"
    assert str(event["LOCATION"]) == "Hörsal HB1"
    assert str(event["DESCRIPTION"]).endswith("\nExtra")
    assert event["UID"] == "stable-123"
    assert event["X-CUSTOM"] == "Keep me"
    assert len(calendar.walk("VTIMEZONE")) == 1
    assert len(event.walk("VALARM")) == 1
    assert "RECURRENCE-ID" in calendar.walk("VEVENT")[1]


def test_filter_and_calendar_hook(monkeypatch):
    monkeypatch.setattr(transforms, "include_event", lambda event: event["SUMMARY"] != "Exception")

    def rename(calendar):
        calendar["X-WR-CALNAME"] = "Custom"
        return calendar

    monkeypatch.setattr(transforms, "transform_calendar", rename)
    calendar = Calendar.from_ical(adapt(FEED))
    assert len(calendar.walk("VEVENT")) == 1
    assert calendar["X-WR-CALNAME"] == "Custom"


def test_id_url_and_cache():
    requests = []

    def upstream(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=FEED)

    with TestClient(create_app(upstream_transport=httpx.MockTransport(upstream))) as client:
        response = client.get("/adapter/timeedit", params={"id": "public.abc123"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert response.headers["x-feed-stale"] == "false"
        assert client.get("/adapter/timeedit", params={"id": "public.abc123"}).status_code == 200
        assert len(requests) == 1
        assert requests[0] == "https://cloud.timeedit.net/chalmers/web/public/abc123.ics"
        other = "https://cloud.timeedit.net/other/web/student/def456.ics"
        assert client.get("/adapter/timeedit", params={"url": other}).status_code == 200
        assert requests[-1] == other
        assert (
            client.get(
                "/adapter/timeedit", params={"id": "public.ghi789", "tenant": "other"}
            ).status_code
            == 200
        )
        assert requests[-1] == "https://cloud.timeedit.net/other/web/public/ghi789.ics"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"id": "bad"},
        {"id": "public.abc", "url": "https://cloud.timeedit.net/a/web/public/b.ics"},
        {"id": "public.abc", "tenant": "../evil"},
        {"url": "http://cloud.timeedit.net/a/web/public/b.ics"},
        {"url": "https://example.com/a/web/public/b.ics"},
        {"url": "https://cloud.timeedit.net.evil.com/a/web/public/b.ics"},
        {"url": "https://cloud.timeedit.net/a/web/public/b.ics?redirect=evil"},
        {"url": "https://cloud.timeedit.net/a/web/public/../b.ics"},
        {"url": "https://[invalid"},
    ],
)
def test_reject_invalid_sources(params):
    def unexpected(request):
        pytest.fail("Invalid input must not make an upstream request")

    with TestClient(create_app(upstream_transport=httpx.MockTransport(unexpected))) as client:
        assert client.get("/adapter/timeedit", params=params).status_code == 400


def test_stale_fallback(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")
    responses = iter([httpx.Response(200, content=FEED), httpx.Response(503)])
    transport = httpx.MockTransport(lambda request: next(responses))
    with TestClient(create_app(upstream_transport=transport)) as client:
        params = {"id": "public.abc"}
        first = client.get("/adapter/timeedit", params=params)
        second = client.get("/adapter/timeedit", params=params)
        assert first.status_code == second.status_code == 200
        assert second.content == first.content
        assert second.headers["x-feed-stale"] == "true"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not a calendar"),
        httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    ],
)
def test_bad_upstream(response):
    transport = httpx.MockTransport(lambda request: response)
    with TestClient(create_app(upstream_transport=transport)) as client:
        assert client.get("/adapter/timeedit", params={"id": "public.abc"}).status_code == 502


def test_transformation_error_is_not_a_partial_feed(monkeypatch):
    def broken(event):
        raise ValueError("bad custom transformation")

    monkeypatch.setattr(transforms, "transform_event", broken)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=FEED))
    with TestClient(create_app(upstream_transport=transport)) as client:
        response = client.get("/adapter/timeedit", params={"id": "public.abc"})
        assert response.status_code == 500
        assert "transforms.py" in response.json()["detail"]
