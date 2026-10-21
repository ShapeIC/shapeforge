"""Operation-local source snapshots and diagnostics; never serialized to projects."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
import json
import os

_current = ContextVar("shapeforge_runtime", default=None)


class Sources:
    def __init__(self, previous=None, refresh=False):
        self.entries = dict(previous or {})
        self.refresh, self.seen = refresh, set()
        self.counts, self.seconds = {}, {}
        self.owner = None

    def load(self, key, loader):
        if key not in self.entries or self.refresh and key not in self.seen:
            with phase("source_read"):
                value = loader()
            self.entries[key] = value
        self.seen.add(key)
        return self.entries[key]


def current():
    return _current.get()


def count(name, amount=1):
    context = current()
    if context is not None:
        context.counts[name] = context.counts.get(name, 0) + amount


@contextmanager
def phase(name):
    start = perf_counter()
    try:
        yield
    finally:
        context = current()
        if context is not None:
            context.seconds[name] = context.seconds.get(name, 0) + perf_counter() - start


@contextmanager
def scope(session=None, refresh=False, publish=False):
    existing = current()
    if existing is not None:
        yield existing
        return
    context = Sources(getattr(session, "_sources", None), refresh)
    context.owner = session
    token = _current.set(context)
    try:
        yield context
        if publish and session is not None:
            session._sources = context.entries
    finally:
        _current.reset(token)


def operation(refresh=False):
    def decorate(function):
        @wraps(function)
        def call(session, *args, **kwargs):
            with scope(session, refresh=refresh, publish=True) as context:
                started = perf_counter()
                result = function(session, *args, **kwargs)
                if context.owner is session:
                    context.seconds["operation"] = perf_counter() - started
                    session.performance = {"counts": dict(context.counts), "seconds": dict(context.seconds)}
                return result
        return call
    return decorate


def provider(function):
    @wraps(function)
    def call(*args, **kwargs):
        with scope(refresh=True):
            return function(*args, **kwargs)
    return call


def loaded(key, loader):
    context = current()
    return loader() if context is None else context.load(key, loader)


def cache_key(value, identity=()):
    environment = loaded(("environment",), lambda: {name: os.environ.get(name) for name in (
        "PDK_ROOT", "SHAPEFORGE_CELLKIT_ROOT", "SHAPEFORGE_PDK")})
    return json.dumps([value, identity, environment], sort_keys=True, allow_nan=False)
