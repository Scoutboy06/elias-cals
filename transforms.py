"""Your customization file. Changes take effect after restart (or with --reload)."""

from collections.abc import Mapping

from icalendar import Calendar, Event


def text(event: Event, field: str, default: str = "") -> str:
    """Read a text field. Missing fields return the default."""
    return str(event.get(field, default))


def replace_text(value: str, replacements: Mapping[str, str]) -> str:
    """Apply literal replacements in insertion order."""
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def set_text(event: Event, field: str, value: str | None) -> None:
    """Set a text field while keeping its parameters; None removes the field."""
    original = event.get(field)
    params = dict(original.params) if hasattr(original, "params") else {}
    event.pop(field, None)
    if value is not None:
        event.add(field, value, parameters=params)


# Edit these mappings, or replace the format functions with any Python logic.
TITLE_REPLACEMENTS: dict[str, str] = {
    # "TDA123": "Algorithms",
    # "Lecture": "📚 Lecture",
}
LOCATION_REPLACEMENTS: dict[str, str] = {
    # "HB1": "Hörsal HB1, Chalmers",
}


def format_title(value: str, event: Event) -> str:
    return replace_text(value, TITLE_REPLACEMENTS)


def format_description(value: str, event: Event) -> str:
    # Example: return f"{text(event, 'SUMMARY')}\n\n{value}"
    return value


def format_location(value: str, event: Event) -> str:
    return replace_text(value, LOCATION_REPLACEMENTS)


def include_event(event: Event) -> bool:
    # Example: return "exam" not in text(event, "SUMMARY").lower()
    return True


def transform_event(event: Event) -> Event | None:
    """Mutate/return an event, or return None to drop it. All ICS fields are available.

    Leave UID and RECURRENCE-ID stable. DTSTAMP should not change on every request.
    Dates are available as event.decoded("DTSTART") / event.decoded("DTEND").
    When moving recurring events, account for RRULE, RDATE, EXDATE and exceptions.
    """
    if not include_event(event):
        return None

    for field, formatter in (
        ("SUMMARY", format_title),
        ("DESCRIPTION", format_description),
        ("LOCATION", format_location),
    ):
        old_value = text(event, field)
        new_value = formatter(old_value, event)
        if new_value != old_value:
            set_text(event, field, new_value)

    # Other examples:
    # set_text(event, "URL", "https://example.com/course")
    # set_text(event, "DESCRIPTION", None)  # Remove a field.
    # event.pop("CATEGORIES", None)
    # event.add("CATEGORIES", ["University"])
    # import re
    # title = re.sub(r"\s+", " ", text(event, "SUMMARY")).strip()
    # set_text(event, "SUMMARY", title)
    return event


def transform_calendar(calendar: Calendar) -> Calendar:
    """Customize feed-level metadata here; VTIMEZONE and other components survive."""
    # calendar["X-WR-CALNAME"] = "My Chalmers schedule"
    return calendar
