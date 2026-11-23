"""A small synchronous event bus.

The engine emits an event for each internal step (a page read, a node split,
a cache eviction) so a UI can animate what the database is doing. With no
subscribers, emitting costs one loop over an empty list.
"""


import contextlib


class EventBus:
    def __init__(self):
        self._subscribers = []
        self._muted = 0

    def subscribe(self, fn):
        """Call fn(kind, data) for every event. Returns a function that unsubscribes."""
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    def emit(self, kind, **data):
        if self._muted:
            return
        for fn in self._subscribers:
            fn(kind, data)

    @contextlib.contextmanager
    def muted(self):
        """Emit nothing inside the block."""
        self._muted += 1
        try:
            yield
        finally:
            self._muted -= 1
