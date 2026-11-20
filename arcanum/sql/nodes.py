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
    insensitive: bool = False  # ILIKE


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
    modifiers: tuple = ()  # NUMERIC(precision, scale)


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
    order_by: list = field(default_factory=list)  # string_agg(x, ',' ORDER BY y)
    filter: object = None  # count(*) FILTER (WHERE ...)


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
    distinct_on: list = field(default_factory=list)  # DISTINCT ON (exprs)


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
class ForeignKey:
    """FOREIGN KEY (columns) REFERENCES table (ref_columns): ref_columns empty means its primary key."""
    columns: list
    table: str
    ref_columns: list = field(default_factory=list)
    on_delete: str = "NO ACTION"  # NO ACTION, RESTRICT, CASCADE or SET NULL
    on_update: str = "NO ACTION"  # NO ACTION or RESTRICT
    name: str | None = None


@dataclass(slots=True)
class Check:
    expr: object
    name: str | None = None
    sql: str = ""  # the condition's own text, for error messages


@dataclass(slots=True)
class ColumnDef:
    name: str
    type: str
    not_null: bool = False
    unique: bool = False
    default: object = None
    identity: str | None = None  # "ALWAYS" or "BY DEFAULT" (SERIAL types are BY DEFAULT)
    modifiers: tuple = ()  # NUMERIC(precision, scale)
    default_sql: str | None = field(default=None, compare=False)  # the default as written, for ALTER TABLE


@dataclass(slots=True)
class CreateTable:
    name: str
    columns: list
    primary_key: list = field(default_factory=list)  # column names; empty means an implicit row id
    if_not_exists: bool = False
    checks: list = field(default_factory=list)  # [Check], column and table constraints alike
    foreign_keys: list = field(default_factory=list)  # [ForeignKey]
    uniques: list = field(default_factory=list)  # [(name or None, [column names])] from table constraints


@dataclass(slots=True)
class AlterAction:
    """One change of ALTER TABLE. kind: ADD COLUMN, DROP COLUMN, RENAME COLUMN, RENAME TABLE, SET DEFAULT,
    DROP DEFAULT, SET NOT NULL, DROP NOT NULL, TYPE, ADD CONSTRAINT or DROP CONSTRAINT."""
    kind: str
    column: str | None = None
    definition: object = None  # ColumnDef for ADD COLUMN; Check, ForeignKey or ("UNIQUE", name, columns) for ADD
    new_name: str | None = None
    default: object = None
    default_sql: str | None = None
    type: str | None = None
    modifiers: tuple = ()
    if_exists: bool = False  # DROP ... IF EXISTS, or IF NOT EXISTS for ADD COLUMN


@dataclass(slots=True)
class AlterTable:
    name: str
    actions: list
    if_exists: bool = False


@dataclass(slots=True)
class CreateView:
    name: str
    query: object
    columns: list | None = None  # names for the query's columns
    or_replace: bool = False


@dataclass(slots=True)
class DropView:
    name: str
    if_exists: bool = False


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
class OnConflict:
    """ON CONFLICT [(columns)] DO NOTHING | DO UPDATE SET ... [WHERE ...]."""
    columns: list | None
    assignments: list | None = None  # None: DO NOTHING
    where: object = None


@dataclass(slots=True)
class Insert:
    table: str
    source: Query  # VALUES or SELECT
    columns: list | None = None
    on_conflict: OnConflict | None = None
    returning: list | None = None  # [SelectItem]


@dataclass(slots=True)
class Update:
    table: str
    assignments: list  # [(column, expr)]
    where: object = None
    returning: list | None = None
    from_: object = None  # UPDATE ... FROM: other tables joined to each target row


@dataclass(slots=True)
class Delete:
    table: str
    where: object = None
    returning: list | None = None
    using: object = None  # DELETE ... USING: other tables joined to each target row


@dataclass(slots=True)
class Begin:
    isolation: str | None = None  # READ COMMITTED, REPEATABLE READ or SERIALIZABLE; None for the default


@dataclass(slots=True)
class Commit:
    pass


@dataclass(slots=True)
class Savepoint:
    name: str


@dataclass(slots=True)
class RollbackTo:
    name: str


@dataclass(slots=True)
class Release:
    name: str


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
