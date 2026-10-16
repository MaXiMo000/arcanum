import random

import pytest

from arcanum.storage import BTree, DuplicateKeyError, Pager, encode


def key(n):
    return encode((n,))


@pytest.fixture
def pager(tmp_path):
    with Pager(tmp_path / "t.db", pool_size=32) as p:
        yield p


def test_empty_tree(pager):
    tree = BTree.create(pager)
    assert tree.get(key(1)) is None
    assert list(tree.scan()) == []
    assert not tree.delete(key(1))
    assert tree.verify() == 0


def test_insert_get_and_duplicates(pager):
    tree = BTree.create(pager, max_keys=4)
    for n in range(100):
        tree.insert(key(n), b"v%d" % n)
    assert tree.get(key(42)) == b"v42"
    with pytest.raises(DuplicateKeyError):
        tree.insert(key(42), b"again")
    tree.insert(key(42), b"new", replace=True)
    assert tree.get(key(42)) == b"new"
    assert tree.verify() == 100


def test_range_scans(pager):
    tree = BTree.create(pager, max_keys=4)
    for n in random.Random(1).sample(range(200), 200):
        tree.insert(key(n), b"")
    keys = lambda **kw: [k for k, _ in tree.scan(**kw)]
    assert keys() == [key(n) for n in range(200)]
    assert keys(lo=key(10), hi=key(15)) == [key(n) for n in range(10, 15)]
    assert keys(lo=key(10), hi=key(15), lo_inclusive=False, hi_inclusive=True) == [key(n) for n in range(11, 16)]
    assert keys(hi=key(3)) == [key(0), key(1), key(2)]
    assert keys(lo=key(197)) == [key(197), key(198), key(199)]


def test_splits_emit_events(pager):
    events = []
    pager.events.subscribe(lambda kind, data: events.append(kind))
    tree = BTree.create(pager, max_keys=3)
    for n in range(4):
        tree.insert(key(n), b"")
    assert "node_split" in events and "root_grow" in events


@pytest.mark.parametrize("max_keys,value_size", [(3, 0), (4, 8), (None, 300), (None, 0)])
def test_random_operations_match_a_dict(pager, max_keys, value_size):
    """Thousands of random inserts and deletes, checked against a plain dict."""
    r = random.Random(max_keys or value_size)
    tree = BTree.create(pager, max_keys=max_keys)
    model = {}
    for step in range(4000):
        k = key(r.randrange(1500))
        if r.random() < 0.6:
            v = r.randbytes(r.randint(0, value_size))
            tree.insert(k, v, replace=True)
            model[k] = v
        else:
            assert tree.delete(k) == (k in model)
            model.pop(k, None)
        if step % 500 == 0:
            assert tree.verify() == len(model)
    assert tree.verify() == len(model)
    assert list(tree.scan()) == sorted(model.items())
    for k in list(model):
        assert tree.get(k) == model[k]


def test_delete_everything_frees_pages(pager):
    tree = BTree.create(pager, max_keys=4)
    for n in range(500):
        tree.insert(key(n), b"x" * 20)
    pages_used = pager.page_count
    for n in random.Random(3).sample(range(500), 500):
        assert tree.delete(key(n))
    assert tree.verify() == 0
    root = tree._load(tree.root)
    assert root.leaf and not root.keys
    for n in range(500):  # refilling reuses the freed pages instead of growing the file
        tree.insert(key(n), b"x" * 20)
    assert pager.page_count == pages_used


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "t.db"
    with Pager(path, pool_size=8) as pager:  # small pool forces evictions mid-build
        tree = BTree.create(pager)
        for n in range(3000):
            tree.insert(key(n), b"row %d" % n)
        root = tree.root
    with Pager(path) as pager:
        tree = BTree(pager, root)
        assert tree.verify() == 3000
        assert tree.get(key(2999)) == b"row 2999"


def test_rejects_oversized_entries(pager):
    tree = BTree.create(pager)
    with pytest.raises(ValueError):
        tree.insert(key(1), b"x" * 2000)
