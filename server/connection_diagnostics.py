"""Bounded, credential-free connection lifecycle evidence."""
from collections import deque
from datetime import datetime, timezone
import logging
import time

LOGGER = logging.getLogger('mb.connection')
EVENTS = {'connected', 'idle_timeout', 'remote_close', 'socket_error', 'network_error',
          'handshake_error', 'unexpected_error', 'reconnect_attempt', 'retry_scheduled', 'shutdown'}


class ConnectionDiagnostics:
    def __init__(self):
        self.events = deque(maxlen=40)
        self.sequence = 0
        self.last_connected = None
        self.last_message = None
        self.messages = 0

    def record(self, event, *, code=None, retry_at=None):
        if event not in EVENTS:
            raise ValueError('Unknown connection event')
        now = time.time()
        self.sequence += 1
        item = {'id': self.sequence, 'at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'event': event}
        if type(code) is int:
            item['code'] = code
        if event == 'connected':
            self.last_connected = now
        if event == 'idle_timeout':
            baseline = max(self.last_message or 0, self.last_connected or 0) or None
            item['silenceSeconds'] = round(now - baseline, 1) if baseline else None
            item['messagesReceived'] = self.messages
        if type(retry_at) in (float, int):
            item['retryAt'] = datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
        self.events.append(item)
        # Never log exception strings, headers, payloads, tokens, VINs or URLs.
        LOGGER.warning('Mercedes connection event: %s', item)

    def received(self):
        self.last_message = time.time()
        self.messages += 1

    def snapshot(self):
        return {'events': list(self.events), 'messagesReceived': self.messages,
                'lastConnectedAt': self.last_connected, 'lastMessageAt': self.last_message}


def exception_evidence(error):
    """Extract bounded structural facts, never exception messages or request objects."""
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        item = {"type": type(error).__name__}
        for field in ("status", "errno"):
            value = getattr(error, field, None)
            if type(value) is int:
                item[field] = value
        chain.append(item)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain
