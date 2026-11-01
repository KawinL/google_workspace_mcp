"""Calendar writes never email attendees beyond WORKSPACE_MCP_CALENDAR_SEND_UPDATES."""

import logging
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gcalendar.calendar_helpers import (
    SEND_UPDATES_ENV_VAR,
    get_configured_send_updates,
    resolve_send_updates,
)
from gcalendar.calendar_tools import (
    _create_focus_time_event_impl,
    _create_ooo_event_impl,
    _delete_focus_time_event_impl,
    _delete_ooo_event_impl,
    _update_focus_time_event_impl,
    _update_ooo_event_impl,
    manage_event,
)

EMAIL = "user@example.com"
_NY = "America/New_York"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(SEND_UPDATES_ENV_VAR, raising=False)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service(get_result=None):
    service = Mock()
    events = service.events.return_value
    events.get.return_value.execute.return_value = get_result or {}
    for method in ("insert", "patch"):
        getattr(events, method).return_value.execute.return_value = {
            "id": "evt1",
            "summary": "Sync",
            "htmlLink": "link",
            "start": {"dateTime": "2026-05-11T12:00:00Z"},
            "end": {"dateTime": "2026-05-11T13:00:00Z"},
        }
    events.delete.return_value.execute.return_value = None
    return service, events


_RSVP_EVENT = {
    "id": "evt1",
    "summary": "Sync",
    "attendees": [{"email": EMAIL, "self": True, "responseStatus": "needsAction"}],
    "organizer": {"email": "org@example.com"},
}

# action -> (manage_event kwargs, events() method written, get() result)
_MANAGE_EVENT_WRITES = {
    "create": (
        {
            "summary": "Sync",
            "start_time": "2026-05-11T12:00:00Z",
            "end_time": "2026-05-11T13:00:00Z",
            "attendees": ["guest@example.com"],
        },
        "insert",
        None,
    ),
    "update": ({"event_id": "evt1", "summary": "Renamed"}, "patch", None),
    "delete": ({"event_id": "evt1"}, "delete", None),
    "rsvp": ({"event_id": "evt1", "response": "accepted"}, "patch", _RSVP_EVENT),
}


async def _manage_event(action, **extra):
    kwargs, method, get_result = _MANAGE_EVENT_WRITES[action]
    service, events = _service(get_result)
    await _unwrap(manage_event)(
        service=service,
        user_google_email=EMAIL,
        action=action,
        **kwargs,
        **extra,
    )
    return getattr(events, method).call_args.kwargs["sendUpdates"]


# -- configuration ----------------------------------------------------------


def test_default_is_none():
    assert get_configured_send_updates() == "none"
    assert resolve_send_updates() == "none"


@pytest.mark.parametrize("value", ["all", "externalOnly", "none"])
def test_env_flag_sets_default(monkeypatch, value):
    monkeypatch.setenv(SEND_UPDATES_ENV_VAR, value)
    assert resolve_send_updates() == value


def test_invalid_env_value_falls_back_to_none(monkeypatch, caplog):
    monkeypatch.setenv(SEND_UPDATES_ENV_VAR, "everyone")
    with caplog.at_level(logging.WARNING):
        assert resolve_send_updates() == "none"
    assert SEND_UPDATES_ENV_VAR in caplog.text


@pytest.mark.parametrize(
    "configured,requested,expected",
    [
        ("none", "all", "none"),
        ("none", "externalOnly", "none"),
        ("externalOnly", "all", "externalOnly"),
        ("externalOnly", "none", "none"),
        ("all", "externalOnly", "externalOnly"),
        ("all", "all", "all"),
    ],
)
def test_requested_value_is_clamped(monkeypatch, configured, requested, expected):
    monkeypatch.setenv(SEND_UPDATES_ENV_VAR, configured)
    assert resolve_send_updates(requested) == expected


def test_clamping_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_send_updates("all") == "none"
    assert "clamping to 'none'" in caplog.text


def test_invalid_requested_value_rejected():
    with pytest.raises(UserInputError, match="Invalid send_updates"):
        resolve_send_updates("invalid")


# -- manage_event write paths -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(_MANAGE_EVENT_WRITES))
async def test_manage_event_sends_none_by_default(action):
    assert await _manage_event(action) == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(_MANAGE_EVENT_WRITES))
async def test_manage_event_clamps_caller_value(action, caplog):
    with caplog.at_level(logging.WARNING):
        assert await _manage_event(action, send_updates="all") == "none"
    assert "clamping" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(_MANAGE_EVENT_WRITES))
async def test_manage_event_honors_env_flag(monkeypatch, action):
    monkeypatch.setenv(SEND_UPDATES_ENV_VAR, "externalOnly")
    assert await _manage_event(action) == "externalOnly"
    assert await _manage_event(action, send_updates="all") == "externalOnly"
    assert await _manage_event(action, send_updates="none") == "none"


@pytest.mark.asyncio
async def test_this_and_following_split_sends_none():
    series = {
        "id": "series1",
        "summary": "Weekly",
        "attendees": [{"email": "guest@example.com"}],
        "start": {"dateTime": "2026-01-05T12:00:00-05:00", "timeZone": _NY},
        "end": {"dateTime": "2026-01-05T13:00:00-05:00", "timeZone": _NY},
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    }
    instance = {
        "id": "series1_20260511T160000Z",
        "recurringEventId": "series1",
        "originalStartTime": {"dateTime": "2026-05-11T12:00:00-04:00"},
        "start": {"dateTime": "2026-05-11T12:00:00-04:00"},
        "end": {"dateTime": "2026-05-11T13:00:00-04:00"},
    }
    service, events = _service()
    events.get.return_value.execute.side_effect = [instance, series]
    await _unwrap(manage_event)(
        service=service,
        user_google_email=EMAIL,
        action="update",
        event_id=instance["id"],
        summary="Renamed",
        send_updates="all",
        this_and_following=True,
    )
    assert events.patch.call_args.kwargs["sendUpdates"] == "none"
    assert events.insert.call_args.kwargs["sendUpdates"] == "none"


# -- Out of Office / Focus Time write paths ---------------------------------

_OOO = {"id": "evt1", "eventType": "outOfOffice", "outOfOfficeProperties": {}}
_FOCUS = {"id": "evt1", "eventType": "focusTime", "focusTimeProperties": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "impl,kwargs,method,get_result",
    [
        (
            _create_ooo_event_impl,
            {"start_time": "2026-05-11", "end_time": "2026-05-12", "timezone": _NY},
            "insert",
            None,
        ),
        (
            _update_ooo_event_impl,
            {"event_id": "evt1", "summary": "Away"},
            "patch",
            _OOO,
        ),
        (_delete_ooo_event_impl, {"event_id": "evt1"}, "delete", _OOO),
        (
            _create_focus_time_event_impl,
            {
                "start_time": "2026-05-11T09:00:00Z",
                "end_time": "2026-05-11T11:00:00Z",
            },
            "insert",
            None,
        ),
        (
            _update_focus_time_event_impl,
            {"event_id": "evt1", "summary": "Deep work"},
            "patch",
            _FOCUS,
        ),
        (_delete_focus_time_event_impl, {"event_id": "evt1"}, "delete", _FOCUS),
    ],
)
async def test_status_event_writes_send_none(impl, kwargs, method, get_result):
    service, events = _service(get_result)
    await impl(service=service, user_google_email=EMAIL, **kwargs)
    assert getattr(events, method).call_args.kwargs["sendUpdates"] == "none"
