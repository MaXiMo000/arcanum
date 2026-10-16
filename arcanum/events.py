"""A small synchronous event bus.

The engine emits an event for each internal step (a page read, a node split,
a cache eviction) so a UI can animate what the database is doing. With no
subscribers, emitting costs one loop over an empty list.
"""


class EventBus:
    def __init__(self):
        self._subscribers = []

    def subscribe(self, fn):
        """Call fn(kind, data) for every event. Returns a function that unsubscribes."""
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    def emit(self, kind, **data):
        for fn in self._subscribers:
            fn(kind, data)
