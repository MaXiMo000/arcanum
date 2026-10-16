"""A B+tree mapping byte-string keys to byte-string values, one node per page.

Keys compare as raw bytes; storage.codec makes that match SQL ordering. Leaves
hold the entries and are chained left to right for range scans. An internal
node with children c0..cn and separator keys k1..kn guarantees that every key
under c(i-1) is < k(i) <= every key under c(i).

The root never moves. When it splits, its contents move to two new pages and
the root page becomes their parent, so a tree is identified by its root page
for its whole life.

Node page layout (little-endian):
    u8   kind     1 = leaf, 2 = internal
    u16  count    number of keys
    u32  link     leaf: next leaf page (0 = last leaf); internal: first child
    then `count` entries:
        leaf:     u16 key length, key, u16 value length, value
        internal: u16 key length, key, u32 child to the right of the key
"""

import struct
from bisect import bisect_left, bisect_right

from .pager import PAGE_SIZE, CorruptDatabaseError

LEAF, INTERNAL = 1, 2

_NODE = struct.Struct("<BHI")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

CAPACITY = PAGE_SIZE - _NODE.size
# A split must leave entries on both sides, so one entry may use at most a quarter of a node.
MAX_ENTRY = CAPACITY // 4


class DuplicateKeyError(KeyError):
    pass


class Node:
    __slots__ = ("page", "leaf", "keys", "values", "children", "next_leaf")

    def __init__(self, page, leaf, keys=None, values=None, children=None, next_leaf=0):
        self.page = page
        self.leaf = leaf
        self.keys = keys if keys is not None else []
        self.values = values if values is not None else []  # leaves only
        self.children = children if children is not None else []  # internal nodes only
        self.next_leaf = next_leaf

    def entry_sizes(self):
        if self.leaf:
            return [4 + len(k) + len(v) for k, v in zip(self.keys, self.values)]
        return [6 + len(k) for k in self.keys]

    def size(self):
        return _NODE.size + sum(self.entry_sizes())


class BTree:
    def __init__(self, pager, root, max_keys=None):
        """Open the tree whose root lives on page `root`.

        max_keys caps the keys per node on top of the page-size limit. Small
        values make splits and merges happen after a handful of inserts, which
        is useful for demos and tests.
        """
        if max_keys is not None and max_keys < 3:
            raise ValueError("max_keys must be at least 3")
        self.pager = pager
        self.root = root
        self.max_keys = max_keys
        self.events = pager.events

    @classmethod
    def create(cls, pager, max_keys=None):
        tree = cls(pager, pager.allocate(), max_keys)
        tree._store(Node(tree.root, True))
        return tree

    # ---- lookups -----------------------------------------------------------

    def get(self, key):
        """Return the value stored under key, or None."""
        node = self._load(self.root)
        while not node.leaf:
            node = self._load(node.children[bisect_right(node.keys, key)])
        i = bisect_left(node.keys, key)
        return node.values[i] if i < len(node.keys) and node.keys[i] == key else None

    def scan(self, lo=None, hi=None, lo_inclusive=True, hi_inclusive=False):
        """Yield (key, value) pairs in key order between the optional bounds.

        The tree must not be modified while a scan is running; collect the
        keys first when deleting or updating what a scan finds.
        """
        node = self._load(self.root)
        while not node.leaf:
            node = self._load(node.children[0 if lo is None else bisect_right(node.keys, lo)])
        i = 0 if lo is None else (bisect_left if lo_inclusive else bisect_right)(node.keys, lo)
        while True:
            for key, value in zip(node.keys[i:], node.values[i:]):
                if hi is not None and (key > hi or (key == hi and not hi_inclusive)):
                    return
                yield key, value
            if not node.next_leaf:
                return
            node, i = self._load(node.next_leaf), 0

    # ---- insert ------------------------------------------------------------

    def insert(self, key, value, replace=False):
        """Store value under key. An existing key raises DuplicateKeyError unless replace is true."""
        if 6 + len(key) + len(value) > MAX_ENTRY:
            raise ValueError(f"entry of {len(key) + len(value)} bytes is too large (limit {MAX_ENTRY - 6})")
        split = self._insert(self.root, key, value, replace)
        if split:
            self._grow_root(*split)

    def _insert(self, page, key, value, replace):
        """Insert below page. Returns (separator, new right page) if page split, else None."""
        node = self._load(page)
        if node.leaf:
            i = bisect_left(node.keys, key)
            if i < len(node.keys) and node.keys[i] == key:
                if not replace:
                    raise DuplicateKeyError(key)
                node.values[i] = value
            else:
                node.keys.insert(i, key)
                node.values.insert(i, value)
        else:
            i = bisect_right(node.keys, key)
            split = self._insert(node.children[i], key, value, replace)
            if split is None:
                return None
            separator, right = split
            node.keys.insert(i, separator)
            node.children.insert(i + 1, right)
        if self._fits(node):
            self._store(node)
            return None
        return self._split(node)

    def _split(self, node):
        right = Node(self.pager.allocate(), node.leaf)
        if node.leaf:
            m = self._split_index(node.entry_sizes(), internal=False)
            right.keys, right.values = node.keys[m:], node.values[m:]
            del node.keys[m:], node.values[m:]
            right.next_leaf, node.next_leaf = node.next_leaf, right.page
            separator = right.keys[0]
        else:
            m = self._split_index(node.entry_sizes(), internal=True)
            separator = node.keys[m]
            right.keys, right.children = node.keys[m + 1:], node.children[m + 1:]
            del node.keys[m:], node.children[m + 1:]
        self._store(node)
        self._store(right)
        self.events.emit("node_split", page=node.page, new_page=right.page, leaf=node.leaf, separator=separator)
        return separator, right.page

    def _grow_root(self, separator, right):
        """The root split: move its left half to a new page and make the root their parent."""
        left = self._load(self.root)
        left.page = self.pager.allocate()
        self._store(left)
        self._store(Node(self.root, False, [separator], children=[left.page, right]))
        self.events.emit("root_grow", page=self.root)

    # ---- delete ------------------------------------------------------------

    def delete(self, key) -> bool:
        """Remove key. Returns False if it was not present."""
        if self._delete(self.root, key) is None:
            return False
        root = self._load(self.root)
        if not root.leaf and not root.keys:
            # The root's children merged into one: pull that child up into the root page.
            child = self._load(root.children[0])
            self.pager.free(child.page)
            child.page = self.root
            self._store(child)
            self.events.emit("root_shrink", page=self.root)
        return True

    def _delete(self, page, key):
        """Delete below page. Returns None if key is absent, else whether page is now underfull."""
        node = self._load(page)
        if node.leaf:
            i = bisect_left(node.keys, key)
            if i == len(node.keys) or node.keys[i] != key:
                return None
            del node.keys[i], node.values[i]
            self._store(node)
            return self._underfull(node)
        i = bisect_right(node.keys, key)
        child_underfull = self._delete(node.children[i], key)
        if not child_underfull:
            return child_underfull  # None (absent) or False (nothing to fix)
        self._rebalance(node, i)
        self._store(node)
        return self._underfull(node)

    def _rebalance(self, parent, i):
        """Fix underfull child i by merging it with a sibling, or by sharing the sibling's entries."""
        s = i if i + 1 < len(parent.children) else i - 1  # merge children s and s + 1
        left, right = self._load(parent.children[s]), self._load(parent.children[s + 1])
        if left.leaf:
            merged = Node(left.page, True, left.keys + right.keys, left.values + right.values,
                          next_leaf=right.next_leaf)
        else:
            merged = Node(left.page, False, left.keys + [parent.keys[s]] + right.keys,
                          children=left.children + right.children)
        if self._fits(merged):
            del parent.keys[s], parent.children[s + 1]
            self.pager.free(right.page)
            self._store(merged)
            self.events.emit("node_merge", page=left.page, removed_page=right.page, leaf=left.leaf)
            return
        keys = merged.keys
        if left.leaf:
            m = self._split_index(merged.entry_sizes(), internal=False)
            left.keys, left.values = keys[:m], merged.values[:m]
            right.keys, right.values = keys[m:], merged.values[m:]
            parent.keys[s] = right.keys[0]
        else:
            m = self._split_index(merged.entry_sizes(), internal=True)
            left.keys, left.children = keys[:m], merged.children[:m + 1]
            right.keys, right.children = keys[m + 1:], merged.children[m + 1:]
            parent.keys[s] = keys[m]
        self._store(left)
        self._store(right)
        self.events.emit("node_redistribute", page=left.page, sibling_page=right.page, leaf=left.leaf)

    # ---- node helpers --------------------------------------------------------

    def _fits(self, node):
        return node.size() <= CAPACITY and (self.max_keys is None or len(node.keys) <= self.max_keys)

    def _underfull(self, node):
        if self.max_keys is not None:
            return len(node.keys) < self.max_keys // 2
        return node.size() < CAPACITY // 3

    def _split_index(self, sizes, internal):
        """Where to cut entries into two halves of about equal bytes.

        Leaf: the left half keeps entries[:m]. Internal: keys[m] moves up to the
        parent, so both sides must keep at least one key.
        """
        n = len(sizes)
        lo, hi = 1, n - 2 if internal else n - 1
        if self.max_keys is not None:
            lo, hi = max(lo, n - self.max_keys - internal), min(hi, self.max_keys)
        total, run = sum(sizes), 0
        for m, size in enumerate(sizes, 1):
            run += size
            if 2 * run >= total:
                return min(max(m, lo), hi)
        return hi

    def _load(self, page):
        data = self.pager.read(page)
        kind, count, link = _NODE.unpack_from(data)
        if kind not in (LEAF, INTERNAL):
            raise CorruptDatabaseError(f"page {page} is not a B+tree node")
        keys, values, children = [], [], [link]
        pos = _NODE.size
        for _ in range(count):
            (key_len,) = _U16.unpack_from(data, pos)
            keys.append(data[pos + 2:pos + 2 + key_len])
            pos += 2 + key_len
            if kind == LEAF:
                (value_len,) = _U16.unpack_from(data, pos)
                values.append(data[pos + 2:pos + 2 + value_len])
                pos += 2 + value_len
            else:
                children.append(_U32.unpack_from(data, pos)[0])
                pos += 4
        self.events.emit("node_visit", page=page, leaf=kind == LEAF)
        if kind == LEAF:
            return Node(page, True, keys, values, next_leaf=link)
        return Node(page, False, keys, children=children)

    def _store(self, node):
        if node.leaf:
            parts = [_NODE.pack(LEAF, len(node.keys), node.next_leaf)]
            for key, value in zip(node.keys, node.values):
                parts += (_U16.pack(len(key)), key, _U16.pack(len(value)), value)
        else:
            parts = [_NODE.pack(INTERNAL, len(node.keys), node.children[0])]
            for key, child in zip(node.keys, node.children[1:]):
                parts += (_U16.pack(len(key)), key, _U32.pack(child))
        self.pager.write(node.page, b"".join(parts))

    # ---- checking ------------------------------------------------------------

    def verify(self) -> int:
        """Check every structural invariant and return the number of entries.

        Raises CorruptDatabaseError naming the first broken rule. Used by the
        tests and by crash-recovery checks.
        """
        leaves = []

        def expect(ok, message):
            if not ok:
                raise CorruptDatabaseError(message)

        def walk(page, lo, hi, depth):
            node = self._load(page)
            keys = node.keys
            expect(all(a < b for a, b in zip(keys, keys[1:])), f"page {page}: keys not strictly increasing")
            expect(all((lo is None or k >= lo) and (hi is None or k < hi) for k in keys),
                   f"page {page}: key outside the range its parent allows")
            expect(self._fits(node), f"page {page}: node is over capacity")
            if node.leaf:
                leaves.append((node, depth))
                return
            expect(len(keys) >= 1, f"page {page}: internal node without keys")
            expect(len(node.children) == len(keys) + 1, f"page {page}: child count does not match keys")
            bounds = [lo, *keys, hi]
            for j, child in enumerate(node.children):
                walk(child, bounds[j], bounds[j + 1], depth + 1)

        walk(self.root, None, None, 0)
        expect(len({depth for _, depth in leaves}) == 1, "leaves are at different depths")
        for (a, _), (b, _) in zip(leaves, leaves[1:]):
            expect(a.next_leaf == b.page, f"leaf {a.page} links to {a.next_leaf}, expected {b.page}")
        expect(leaves[-1][0].next_leaf == 0, "last leaf links to another page")
        return sum(len(node.keys) for node, _ in leaves)
