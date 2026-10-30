"""One monotonic turn budget and cooperative stop signal, including workers.

Threads are not killed. Already-started commits may finish; the admission lease
is retained until owned work and telemetry finalization have actually ended.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import threading
import time

_CURRENT = ContextVar('mindbridge_turn_control', default=None)


class TurnStopped(BaseException):
    # Must not be swallowed by model/rewriter `except Exception` fallbacks.
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class TurnControl:
    def __init__(self, seconds, io_limit, observation=None, elapsed=0.):
        self.started = time.monotonic() - elapsed
        self.deadline = self.started + seconds
        self.seconds = seconds; self.io_limit = io_limit
        self.observation = observation
        self.reason = None; self.lock = threading.RLock()
        self.lease = None; self.risk = None; self.terminal_sent = False

    def remaining(self):
        return max(0., self.deadline - time.monotonic())

    def stop(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason
        if self.observation:
            self.observation.stream_status = self.status()

    def check(self):
        if not self.reason and self.remaining() <= 0:
            self.stop('TURN_TIMEOUT')
        if self.reason:
            raise TurnStopped(self.reason)

    def status(self):
        return {'TURN_TIMEOUT': 'turn_timeout', 'CLIENT_DISCONNECTED': 'cancelled',
                'SERVER_SHUTDOWN': 'shutdown', 'REQUEST_TOO_LARGE': 'request_too_large',
                'TURN_FAILED': 'failed'}.get(self.reason, 'in_progress')

    def note_risk(self, risk):
        order = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3}
        if order.get(risk, 0) > order.get(self.risk, 0):
            self.risk = risk

    def snapshot(self):
        return {'schemaVersion': 1, 'budgetSeconds': self.seconds,
                'elapsedMs': round((time.monotonic() - self.started) * 1000, 3),
                'reasonCode': self.reason or 'COMPLETED', 'remainingMs': round(self.remaining() * 1000, 3),
                'scope': 'request plus tracked worker finalization',
                'riskKnown': self.risk, 'uninterruptibleWorkMayFinish': True}


def current_turn():
    return _CURRENT.get()


@contextmanager
def turn_scope(control):
    token = _CURRENT.set(control)
    try:
        yield control
    finally:
        _CURRENT.reset(token)


def checkpoint():
    control = current_turn()
    if control:
        control.check()


def bounded_timeout(default):
    control = current_turn()
    if control is None:
        return default
    control.check()
    return max(.001, min(default, control.io_limit, control.remaining()))


async def within_turn(awaitable):
    control = current_turn()
    if control is None:
        return await awaitable
    try:
        async with asyncio.timeout(control.remaining()):
            return await awaitable
    except TimeoutError:
        if control.remaining() <= 0:
            control.stop('TURN_TIMEOUT')
            raise TurnStopped(control.reason)
        raise
