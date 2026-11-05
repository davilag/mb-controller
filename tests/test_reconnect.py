"""Automatic reconnects follow clock boundaries, including transport failures."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.mbapi import websocket as ws


@pytest.mark.parametrize(('clock', 'delay'), [
    ('10:32:00', 480), ('10:38:00', 120), ('10:39:59.500000', .5),
    ('10:40:00', 600), ('23:58:00', 120),
])
def test_next_boundary(clock, delay):
    now = datetime.fromisoformat('2026-11-05T' + clock).replace(tzinfo=timezone.utc).timestamp()
    assert ws.seconds_until_reconnect(now) == delay


@pytest.mark.asyncio
async def test_watchdog_recalculates_each_arm(monkeypatch):
    watchdog = ws.ReconnectWatchdog(AsyncMock(), 600, 'test')
    monkeypatch.setattr(ws.time, 'time', lambda: 1920)  # :32
    try:
        await watchdog.trigger()
        assert watchdog.timeout == 480
        monkeypatch.setattr(ws.time, 'time', lambda: 2400)  # :40 failure
        await watchdog.trigger()
        assert watchdog.timeout == 600
    finally:
        watchdog.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [None, ConnectionResetError(), RuntimeError('transport failure')])
async def test_handler_close_and_errors_wait_for_boundary(monkeypatch, error):
    socket = object.__new__(ws.Websocket)
    socket.is_stopping = False
    socket._LOGGER = Mock()
    socket.component_reload_watcher = SimpleNamespace(trigger=AsyncMock())
    socket._websocket_handler = AsyncMock(side_effect=error)
    socket.ws_connect_retry_counter = 0
    monkeypatch.setattr(ws.time, 'time', lambda: 2280)  # :38
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(ws.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await socket._start_websocket_handler(SimpleNamespace(closed=False))
    sleep.assert_awaited_once_with(120)
    assert socket.connection_state == ws.STATE_RECONNECTING
