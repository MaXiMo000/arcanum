"""Buffer pool replacement policies: which cached page to evict when the pool is full.

The pager tells its policy about every page it caches or touches (touch) and
every page that leaves the pool (remove), and asks it for a victim when it needs
a free slot. Each policy also lists the pages in the order it would evict them,
so the pool can be drawn.

- LRU evicts the least recently used page. Simple and good for skewed access, but
  one large scan pushes every hot page out ("sequential flooding").
- Clock approximates LRU with one reference bit per page and a hand that sweeps
  the slots, clearing bits until it finds a page not used since its last visit.
  PostgreSQL's buffer manager works this way.
- 2Q (Johnson and Shasha, 1994) admits a new page to a small FIFO queue first; only
  a page used again after leaving it (remembered in a "ghost" list of ids) joins
  the main LRU queue. A scan passes through the FIFO without displacing hot pages.

Belady's optimal policy (evict the page used furthest in the future) needs the
future, so it only exists in the buffer pool lab, which replays recorded traces.
"""

from collections import OrderedDict


class ReplacementPolicy:
    name = ""

    def __init__(self, capacity):
        self.capacity = capacity

    def touch(self, page):
        """page was read or written: it is in the pool now."""
        raise NotImplementedError

    def remove(self, page, evicted=False):
        """page left the pool (evicted, when the policy chose it as the victim)."""
        raise NotImplementedError

    def victim(self):
        """The page to evict next (it stays until remove is called)."""
        raise NotImplementedError

    def eviction_order(self):
        """Cached pages, the next victim first."""
        raise NotImplementedError


class LRU(ReplacementPolicy):
    name = "lru"

    def __init__(self, capacity):
        super().__init__(capacity)
        self.pages = OrderedDict()  # least recently used first

    def touch(self, page):
        self.pages[page] = None
        self.pages.move_to_end(page)

    def remove(self, page, evicted=False):
        self.pages.pop(page, None)

    def victim(self):
        return next(iter(self.pages))

    def eviction_order(self):
        return list(self.pages)


class Clock(ReplacementPolicy):
    name = "clock"

    def __init__(self, capacity):
        super().__init__(capacity)
        self.slots = []  # page in each slot of the ring, or None when free
        self.free = []  # free slots
        self.where = {}  # page -> slot
        self.referenced = {}  # page -> its reference bit
        self.hand = 0

    def touch(self, page):
        if page not in self.where:
            if self.free:
                slot = self.free.pop()
            else:
                slot = len(self.slots)
                self.slots.append(None)
            self.slots[slot], self.where[page] = page, slot
        self.referenced[page] = True

    def remove(self, page, evicted=False):
        slot = self.where.pop(page, None)
        if slot is not None:
            self.slots[slot] = None
            self.free.append(slot)
            del self.referenced[page]

    def victim(self):
        # At most two sweeps: the first clears every bit it passes, the second must find a page.
        for _ in range(2 * len(self.slots) + 1):
            self.hand %= len(self.slots)
            page = self.slots[self.hand]
            if page is not None:
                if not self.referenced[page]:
                    return page
                self.referenced[page] = False
            self.hand += 1
        raise RuntimeError("the pool is empty")

    def eviction_order(self):
        n = len(self.slots)
        ring = [self.slots[(self.hand + i) % n] for i in range(n)] if n else []
        cold = [p for p in ring if p is not None and not self.referenced[p]]
        return cold + [p for p in ring if p is not None and self.referenced[p]]


class TwoQ(ReplacementPolicy):
    name = "2q"
    IN_SHARE, GHOST_SHARE = 0.25, 0.5  # the paper's recommended sizes, as shares of the pool

    def __init__(self, capacity):
        super().__init__(capacity)
        self.a1_in = OrderedDict()  # first-time pages, FIFO
        self.a1_out = OrderedDict()  # ids of pages evicted from a1_in (no data): a "ghost" list
        self.am = OrderedDict()  # pages used again after a1_in: LRU
        self.in_limit = max(1, int(capacity * self.IN_SHARE))
        self.ghost_limit = max(1, int(capacity * self.GHOST_SHARE))

    def touch(self, page):
        if page in self.am:
            self.am.move_to_end(page)
        elif page in self.a1_in:
            pass  # repeated use within the FIFO counts as one (correlated references)
        elif page in self.a1_out:
            del self.a1_out[page]
            self.am[page] = None  # seen before, and again: it is hot
        else:
            self.a1_in[page] = None

    def remove(self, page, evicted=False):
        if self.a1_in.pop(page, False) is None and evicted:
            self.a1_out[page] = None
            while len(self.a1_out) > self.ghost_limit:
                self.a1_out.popitem(last=False)
        self.am.pop(page, None)

    def victim(self):
        if self.a1_in and (len(self.a1_in) > self.in_limit or not self.am):
            return next(iter(self.a1_in))
        return next(iter(self.am))

    def eviction_order(self):
        if len(self.a1_in) > self.in_limit or not self.am:
            return list(self.a1_in) + list(self.am)
        return list(self.am) + list(self.a1_in)


POLICIES = {policy.name: policy for policy in (LRU, Clock, TwoQ)}


def make_policy(name, capacity):
    try:
        return POLICIES[name](capacity)
    except KeyError:
        raise ValueError(f"unknown buffer pool policy {name!r}; choose one of {', '.join(POLICIES)}") from None
