"""The syntax tree produced by the parser.

Nodes that name something (columns, tables, functions, operators) carry
the offset of their token in `pos` for error messages; it is ignored by ==.
Unquoted identifiers are lower-cased; quoted identifiers keep their exact
spelling. Function names are upper-cased. Type names are normalised to
INTEGER, REAL, TEXT or BOOLEAN.
"""

from dataclasses import dataclass, field

# ---- expressions -------------------------------------------------------------


@dataclass(slots=True)
class Literal:
    value: object  # None, bool, int, float or str


@dataclass(slots=True)
class ColumnRef:
    name: str
    table: str | None = None
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


@dataclass(slots=True)
class Star:
    """`*` or `t.*` in a select list."""
    table: str | None = None
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


@dataclass(slots=True)
class Unary:
    op: str  # "-", "+" or "NOT"
    operand: object
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


@dataclass(slots=True)
class Binary:
    op: str  # OR AND = != < <= > >= || + - * / %
    left: object
    right: object
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


@dataclass(slots=True)
class IsNull:
    expr: object
    negated: bool = False


@dataclass(slots=True)
class Between:
    expr: object
    low: object
    high: object
    negated: bool = False


@dataclass(slots=True)
class InList:
    expr: object
    items: list
    negated: bool = False


@dataclass(slots=True)
class InSubquery:
    expr: object
    query: "Query"
    negated: bool = False


@dataclass(slots=True)
class Like:
    expr: object
    pattern: object
    negated: bool = False


@dataclass(slots=True)
class Exists:
    query: "Query"


@dataclass(slots=True)
class Subquery:
    """A subquery used as a single value."""
    query: "Query"


@dataclass(slots=True)
class Case:
    operand: object  # CASE x WHEN ... ; None for CASE WHEN cond ...
    whens: list  # [(condition or value, result)]
    else_: object = None


@dataclass(slots=True)
class Cast:
    expr: object
    type: str


@dataclass(slots=True)
class FrameBound:
    kind: str  # UNBOUNDED PRECEDING, PRECEDING, CURRENT ROW, FOLLOWING, UNBOUNDED FOLLOWING
    offset: object = None  # for PRECEDING / FOLLOWING


@dataclass(slots=True)
class Frame:
    mode: str  # ROWS or RANGE
    start: FrameBound
    end: FrameBound


@dataclass(slots=True)
class WindowSpec:
    partition_by: list = field(default_factory=list)
    order_by: list = field(default_factory=list)
    frame: Frame | None = None


@dataclass(slots=True)
class Call:
    name: str
    args: list = field(default_factory=list)
    distinct: bool = False
    star: bool = False  # COUNT(*)
    over: WindowSpec | None = None
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


# ---- queries -------------------------------------------------------------------


@dataclass(slots=True)
class OrderItem:
    expr: object
    desc: bool = False
    nulls_first: bool | None = None  # None: the default for the direction


@dataclass(slots=True)
class SelectItem:
    expr: object
    alias: str | None = None


@dataclass(slots=True)
class TableRef:
    name: str
    alias: str | None = None
    pos: int | None = field(default=None, compare=False, repr=False)  # offset in the SQL text


@dataclass(slots=True)
class SubqueryRef:
    query: "Query"
    alias: str


@dataclass(slots=True)
class Join:
    kind: str  # INNER, LEFT, RIGHT, FULL, CROSS
    left: object
    right: object
    on: object = None
    using: list | None = None


@dataclass(slots=True)
class Select:
    columns: list  # [SelectItem]
    from_: object = None
    where: object = None
    group_by: list = field(default_factory=list)
    having: object = None
    distinct: bool = False


@dataclass(slots=True)
class Values:
    rows: list  # [[expr]]


@dataclass(slots=True)
class SetOp:
    op: str  # UNION, INTERSECT, EXCEPT
    all: bool
    left: object
    right: object


@dataclass(slots=True)
class CTE:
    name: str
    query: "Query"
    columns: list | None = None


@dataclass(slots=True)
class With:
    recursive: bool
    ctes: list


@dataclass(slots=True)
class Query:
    """A full query: Select, Values or SetOp body plus ordering and limits."""
    body: object
    with_: With | None = None
    order_by: list = field(default_factory=list)
    limit: object = None
    offset: object = None


# ---- statements ----------------------------------------------------------------


@dataclass(slots=True)
class ColumnDef:
    name: str
    type: str
    not_null: bool = False
    unique: bool = False
    default: object = None


@dataclass(slots=True)
class CreateTable:
    name: str
    columns: list
    primary_key: list = field(default_factory=list)  # column names; empty means an implicit row id
    if_not_exists: bool = False


@dataclass(slots=True)
class DropTable:
    name: str
    if_exists: bool = False


@dataclass(slots=True)
class CreateIndex:
    name: str
    table: str
    columns: list
    unique: bool = False
    if_not_exists: bool = False


@dataclass(slots=True)
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass(slots=True)
class Insert:
    table: str
    source: Query  # VALUES or SELECT
    columns: list | None = None


@dataclass(slots=True)
class Update:
    table: str
    assignments: list  # [(column, expr)]
    where: object = None


@dataclass(slots=True)
class Delete:
    table: str
    where: object = None


@dataclass(slots=True)
class Begin:
    pass


@dataclass(slots=True)
class Commit:
    pass


@dataclass(slots=True)
class Rollback:
    pass


@dataclass(slots=True)
class Explain:
    statement: object
    analyze: bool = False


@dataclass(slots=True)
class Analyze:
    table: str | None = None
