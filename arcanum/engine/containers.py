"""Arrays and JSONB at run time: operators, functions and the set-returning functions used in FROM.

Arrays are one-dimensional tuples, indexed from 1 as in SQL. JSONB values are
values.Jsonb; operators follow PostgreSQL: -> and ->> take a key or an index,
#> and #>> a path, @> tests containment, ? tests for a key.
"""

import datetime
import itertools
from decimal import Decimal

from ..errors import ExecutionError
from ..values import Jsonb, dumps

# ---- arrays ------------------------------------------------------------------------------------


def subscript(array, index):
    """array[i], 1-based; out of range is NULL."""
    if isinstance(array, Jsonb):
        return json_get(array, index)
    return array[index - 1] if 1 <= index <= len(array) else None


def contains(a, b):
    """a @> b: every element of b is in a (arrays), or b's structure is inside a (JSONB)."""
    if isinstance(a, Jsonb):
        return _json_contains(a.value, b.value, top=True)
    present = {x for x in a if x is not None}
    return all(x is not None and x in present for x in b)


def overlaps(a, b):
    """a && b: the arrays share an element."""
    present = {x for x in a if x is not None}
    return any(x is not None and x in present for x in b)


def concat(a, b):
    """|| on arrays (array || array, array || element, element || array) and on JSONB."""
    if isinstance(a, Jsonb) or isinstance(b, Jsonb):
        return json_concat(a, b)
    if isinstance(a, tuple) and isinstance(b, tuple):
        return a + b
    return (*a, b) if isinstance(a, tuple) else (a, *b)


def array_length(array, dimension):
    if dimension != 1:
        return None  # one-dimensional arrays only
    return len(array) or None


def array_remove(array, value):
    return tuple(x for x in array if x != value)


def array_position(array, value):
    return next((i + 1 for i, x in enumerate(array) if x == value), None)


def array_to_string(array, separator, null=None):
    from .expr import to_text  # noqa: PLC0415 (expr imports this module)

    return separator.join(to_text(x) if x is not None else null for x in array if x is not None or null is not None)


def string_to_array(text, separator, null=None):
    parts = list(text) if separator is None else text.split(separator) if separator else [text]
    return tuple(None if null is not None and p == null else p for p in parts) if text else ()


def any_compare(op, value, array, all_):
    """value op ANY (array) / ALL (array), with SQL's three-valued logic."""
    saw_null = value is None
    for item in array:
        if item is None or value is None:
            saw_null = True
            continue
        result = op(value, item)
        if result and not all_:
            return True
        if not result and all_:
            return False
    if saw_null and array:
        return None
    return all_


# ---- JSONB -------------------------------------------------------------------------------------


def to_json_value(v):
    """A SQL value as a JSON value (for to_jsonb, jsonb_build_object, jsonb_agg)."""
    if v is None or isinstance(v, (bool, int, float, Decimal, str)):
        return v
    if isinstance(v, Jsonb):
        return v.value
    if isinstance(v, tuple):
        return [to_json_value(x) for x in v]
    if isinstance(v, datetime.datetime):
        return v.isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    return str(v)


def to_jsonb(v):
    return Jsonb(to_json_value(v))


def json_get(j, key):
    """j -> key: an object's field, or an array's element (from 0; negative counts from the end)."""
    value = j.value
    if isinstance(key, str):
        return Jsonb(value[key]) if isinstance(value, dict) and key in value else None
    if isinstance(value, list) and -len(value) <= key < len(value):
        return Jsonb(value[key])
    return None


def json_get_text(j, key):
    """j ->> key: the same, as text (a JSON string unquoted, JSON null as SQL NULL)."""
    found = json_get(j, key)
    return None if found is None else as_text(found)


def as_text(j):
    if j.value is None:
        return None
    return j.value if isinstance(j.value, str) else j.text


def json_path(j, path):
    """j #> '{a,0,b}': follow a path of keys and indexes."""
    for step in path:
        if j is None or step is None:
            return None
        if isinstance(j.value, list):
            try:
                step = int(step)
            except ValueError:
                return None
        j = json_get(j, step)
    return j


def json_path_text(j, path):
    found = json_path(j, path)
    return None if found is None else as_text(found)


def json_has(j, key):
    """j ? key: a top-level key, or a string element of a top-level array."""
    value = j.value
    if isinstance(value, dict):
        return key in value
    if isinstance(value, list):
        return key in [x for x in value if isinstance(x, str)]
    return value == key


def json_has_any(j, keys):
    return any(k is not None and json_has(j, k) for k in keys)


def json_has_all(j, keys):
    return all(k is not None and json_has(j, k) for k in keys)


def _json_contains(a, b, top=False):
    if isinstance(a, dict) and isinstance(b, dict):
        return all(k in a and _json_contains(a[k], v) for k, v in b.items())
    if isinstance(a, list):
        if isinstance(b, list):
            return all(any(_json_contains(x, y) for x in a) for y in b)
        return top and not isinstance(b, dict) and any(_json_contains(x, b) for x in a)
    if isinstance(a, dict) or isinstance(b, (dict, list)):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


def json_concat(a, b):
    a = a if isinstance(a, Jsonb) else to_jsonb(a)
    b = b if isinstance(b, Jsonb) else to_jsonb(b)
    if isinstance(a.value, dict) and isinstance(b.value, dict):
        return Jsonb({**a.value, **b.value})
    left = a.value if isinstance(a.value, list) else [a.value]
    right = b.value if isinstance(b.value, list) else [b.value]
    return Jsonb(left + right)


def json_delete(j, key):
    """j - key (or index)."""
    value = j.value
    if isinstance(key, str):
        if isinstance(value, dict):
            return Jsonb({k: v for k, v in value.items() if k != key})
        if isinstance(value, list):
            return Jsonb([x for x in value if x != key])
        raise ExecutionError("cannot delete a key from a JSON scalar")
    if not isinstance(value, list):
        raise ExecutionError("cannot delete an index from a JSON value that is not an array")
    index = key if key >= 0 else len(value) + key
    return Jsonb([x for i, x in enumerate(value) if i != index])


def json_set(j, path, new, create=True):
    """jsonb_set(j, '{a,b}', new): j with the value at path replaced (or created)."""
    if not path:
        return j

    def put(node, steps):
        step, rest = steps[0], steps[1:]
        if isinstance(node, dict):
            if step not in node and (rest or not create):
                return node
            return {**node, step: put(node[step], rest) if rest else new.value}
        if isinstance(node, list):
            try:
                index = int(step)
            except ValueError:
                raise ExecutionError(f"path element {step!r} is not an integer, but the value there is an array") \
                    from None
            if -len(node) <= index < len(node):
                out = list(node)
                out[index] = put(node[index], rest) if rest else new.value
                return out
            if rest or not create:
                return node
            return [new.value, *node] if index < 0 else [*node, new.value]
        return node

    return Jsonb(put(j.value, list(path)))


def json_typeof(j):
    v = j.value
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float, Decimal)):
        return "number"
    return {str: "string", list: "array", dict: "object"}[type(v)]


def json_array_length(j):
    if not isinstance(j.value, list):
        raise ExecutionError("cannot get the array length of a JSON value that is not an array")
    return len(j.value)


def json_pretty(j):
    def render(v, depth):
        pad = "    " * (depth + 1)
        if isinstance(v, dict) and v:
            keys = sorted(v, key=lambda k: (len(k.encode()), k.encode()))
            inner = ",\n".join(f"{pad}{dumps(k)}: {render(v[k], depth + 1)}" for k in keys)
            return "{\n" + inner + "\n" + "    " * depth + "}"
        if isinstance(v, list) and v:
            return "[\n" + ",\n".join(pad + render(x, depth + 1) for x in v) + "\n" + "    " * depth + "]"
        return dumps(v)
    return render(j.value, 0)


def json_strip_nulls(j):
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return Jsonb(strip(j.value))


def build_object(*args):
    if len(args) % 2:
        raise ExecutionError("jsonb_build_object needs an even number of arguments: key, value, key, value, ...")
    out = {}
    for key, value in zip(args[::2], args[1::2], strict=True):
        if key is None:
            raise ExecutionError("jsonb_build_object: a key cannot be NULL")
        out[key if isinstance(key, str) else dumps(to_json_value(key)).strip('"')] = to_json_value(value)
    return Jsonb(out)


def build_array(*args):
    return Jsonb([to_json_value(a) for a in args])


def json_scalar(j, target):
    """(j)::integer and friends: a JSON number or boolean as a SQL value."""
    v = j.value
    kind = json_typeof(j)
    if target == "BOOLEAN":
        if kind != "boolean":
            raise ExecutionError(f"cannot cast jsonb {kind} to type boolean")
        return v
    if kind != "number":
        raise ExecutionError(f"cannot cast jsonb {kind} to type {target.lower()}")
    if target == "INTEGER":
        return int(Decimal(v).to_integral_value()) if not isinstance(v, int) else v
    if target == "REAL":
        return float(v)
    return Decimal(v) if not isinstance(v, Decimal) else v


# ---- functions that return rows (used in FROM) --------------------------------------------------


def _series(start, stop, step=None):
    if isinstance(start, (datetime.date, datetime.datetime)):
        from .datetimes import Interval, plus  # noqa: PLC0415

        if step is None:
            raise ExecutionError("generate_series over timestamps needs an interval step")
        if not (step.months or step.days or step.micros):
            raise ExecutionError("generate_series: the step cannot be zero")
        current, stop = plus(start, Interval()), plus(stop, Interval())  # dates count as midnight
        forward = plus(current, step) > current
        while (current <= stop) if forward else (current >= stop):
            yield (current,)
            current = plus(current, step)
        return
    step = 1 if step is None else step
    if step == 0:
        raise ExecutionError("generate_series: the step cannot be zero")
    current = start
    while (current <= stop) if step > 0 else (current >= stop):
        yield (current,)
        current += step


def _unnest(*arrays):
    yield from itertools.zip_longest(*arrays)


def _elements(j, text=False):
    if not isinstance(j.value, list):
        raise ExecutionError("cannot extract elements from a JSON value that is not an array")
    for v in j.value:
        item = Jsonb(v)
        yield (as_text(item),) if text else (item,)


def _each(j, text=False):
    if not isinstance(j.value, dict):
        raise ExecutionError("cannot deconstruct a JSON value that is not an object")
    for k in sorted(j.value, key=lambda k: (len(k.encode()), k.encode())):
        item = Jsonb(j.value[k])
        yield (k, as_text(item) if text else item)


def _keys(j):
    if not isinstance(j.value, dict):
        raise ExecutionError("cannot get the keys of a JSON value that is not an object")
    for k in sorted(j.value, key=lambda k: (len(k.encode()), k.encode())):
        yield (k,)


def _split(text, pattern):
    from .functions import compile_regex  # noqa: PLC0415 (functions imports this module)

    yield from ((part,) for part in compile_regex(pattern, 0).split(text))


# name -> (rows function, column names, column types given the argument types)
TABLE_FUNCTIONS = {
    "UNNEST": (_unnest, None, None),  # one column per array, typed by the arrays' elements
    "GENERATE_SERIES": (_series, ["generate_series"], None),
    "JSONB_ARRAY_ELEMENTS": (_elements, ["value"], ["JSONB"]),
    "JSONB_ARRAY_ELEMENTS_TEXT": (lambda j: _elements(j, True), ["value"], ["TEXT"]),
    "JSONB_EACH": (_each, ["key", "value"], ["TEXT", "JSONB"]),
    "JSONB_EACH_TEXT": (lambda j: _each(j, True), ["key", "value"], ["TEXT", "TEXT"]),
    "JSONB_OBJECT_KEYS": (_keys, ["jsonb_object_keys"], ["TEXT"]),
    "REGEXP_SPLIT_TO_TABLE": (_split, ["regexp_split_to_table"], ["TEXT"]),
}
for _name in [n for n in TABLE_FUNCTIONS if n.startswith("JSONB_")]:
    TABLE_FUNCTIONS["JSON_" + _name[6:]] = TABLE_FUNCTIONS[_name]
