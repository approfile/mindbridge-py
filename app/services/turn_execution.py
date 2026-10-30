"""Bounded per-process admission and worker ownership; no unbounded to_thread."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
import threading

from app.services.observability import log_event


class TurnRejected(Exception):
    def __init__(self, code, status=429):
        self.code = code; self.status = status


class TurnRuntime:
    def __init__(self, settings):
        self.limit = settings.turn_max_inflight
        self.user_limit = settings.turn_max_inflight_per_user
        self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix='mindbridge-turn')
        self.lock = threading.RLock(); self.leases = set(); self.users = {}; self.sessions = {}
        self.closing = False

    def admit(self, control):
        with self.lock:
            if self.closing or len(self.leases) >= self.limit:
                raise TurnRejected('TURN_CAPACITY_EXCEEDED')
            lease = TurnLease(self, control); self.leases.add(lease); control.lease = lease
            return lease

    def stats(self):
        with self.lock:
            return {'active': len(self.leases), 'limit': self.limit, 'closing': self.closing}

    def close(self):
        with self.lock:
            self.closing = True
            for lease in list(self.leases):
                lease.control.stop('SERVER_SHUTDOWN')
                task = lease.http_task
                if task and not task.done() and not task.get_loop().is_closed():
                    task.get_loop().call_soon_threadsafe(task.cancel)
            if not self.leases:
                self.executor.shutdown(wait=False)


class TurnLease:
    def __init__(self, runtime, control):
        self.runtime = runtime; self.control = control; self.context = copy_context()
        self.user_id = None; self.session_key = None; self.pending = 0
        self.http_done = False; self.http_task = None; self.released = False
        self.finalization_required = False; self.finalizer = None; self.finalizer_started = False
        self.finalized = False; self.finished = Future()

    def bind_user(self, user_id, session_id=None):
        with self.runtime.lock:
            if self.user_id is None:
                if self.runtime.users.get(user_id, 0) >= self.runtime.user_limit:
                    raise TurnRejected('USER_TURN_CAPACITY_EXCEEDED')
                self.user_id = user_id; self.runtime.users[user_id] = self.runtime.users.get(user_id, 0) + 1
            elif self.user_id != user_id:
                raise RuntimeError('Turn lease principal mismatch')
            if session_id:
                self.bind_session(session_id)

    def bind_session(self, session_id):
        with self.runtime.lock:
            key = (self.user_id, session_id)
            owner = self.runtime.sessions.get(key)
            if owner is not None and owner is not self:
                raise TurnRejected('SESSION_TURN_IN_PROGRESS', 409)
            if self.session_key and self.session_key != key:
                raise RuntimeError('Turn lease session mismatch')
            self.session_key = key; self.runtime.sessions[key] = self

    def submit(self, function):
        with self.runtime.lock:
            self.control.check()
            self.pending += 1
            try:
                future = self.runtime.executor.submit(copy_context().run, function)
            except BaseException:
                self.pending -= 1; raise
            future.add_done_callback(self._work_done)
            return future

    def external_started(self):
        """Track FastAPI's sync authentication worker before it opens SQL."""
        with self.runtime.lock:
            self.control.check()
            if self.released:
                raise RuntimeError('Turn lease already released')
            self.pending += 1

    def external_finished(self):
        with self.runtime.lock:
            self.pending -= 1; self._maybe_finalize(); self._maybe_release()

    def _work_done(self, future):
        # Retrieve abandoned errors too: disconnects must not leave unobserved
        # futures or release capacity while a blocking operation still runs.
        if not future.cancelled():
            future.exception()
        with self.runtime.lock:
            self.pending -= 1
            self._maybe_finalize()
            self._maybe_release()

    def finish(self, finalizer):
        with self.runtime.lock:
            self.finalization_required = True
            if self.finalizer is None:
                context = copy_context()
                self.finalizer = lambda: context.run(finalizer)
            self._maybe_finalize()
            return self.finished

    def _maybe_finalize(self):
        if self.finalizer is None or self.finalizer_started or self.pending:
            return
        self.finalizer_started = True; self.pending += 1
        future = self.runtime.executor.submit(self.finalizer)
        future.add_done_callback(self._finalized)

    def _finalized(self, future):
        with self.runtime.lock:
            self.pending -= 1; self.finalized = True
            error = future.exception() if not future.cancelled() else RuntimeError('Finalizer cancelled')
            if error is None: self.finished.set_result(None)
            else: self.finished.set_exception(error)
            self._maybe_release()

    def http_finished(self, task=None):
        if task and not task.cancelled():
            task.exception()
        with self.runtime.lock:
            self.http_done = True; self._maybe_release()

    def _maybe_release(self):
        if self.released or not self.http_done or self.pending or (self.finalization_required and not self.finalized):
            return
        self.released = True; self.runtime.leases.discard(self)
        if self.user_id is not None:
            count = self.runtime.users[self.user_id] - 1
            if count: self.runtime.users[self.user_id] = count
            else: self.runtime.users.pop(self.user_id)
        if self.session_key: self.runtime.sessions.pop(self.session_key, None)
        self.context.copy().run(log_event, 'turn.capacity_released', status=self.control.reason or 'COMPLETED')
        if self.runtime.closing and not self.runtime.leases:
            self.runtime.executor.shutdown(wait=False)


async def await_owned(future):
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    return await asyncio.shield(wrapped)


_RUNTIMES = {}; _LOCK = threading.Lock()


def get_turn_runtime(settings):
    key = (settings.turn_max_inflight, settings.turn_max_inflight_per_user)
    with _LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None or runtime.closing:
            runtime = _RUNTIMES[key] = TurnRuntime(settings)
        return runtime
