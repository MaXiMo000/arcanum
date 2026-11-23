"""Recursive-descent SQL parser: text -> syntax tree (see nodes.py).

Expression precedence, loosest first:
    OR, AND, NOT, comparisons (= != < <= > >= IS IN BETWEEN LIKE), ||, + -, * / %, unary - +

Set operations: INTERSECT binds tighter than UNION and EXCEPT, as in standard SQL.
"""

import functools

from ..errors import SqlError
from . import nodes as n
from .lexer import EOF, FLOAT, INT, OP, QUOTED, STRING, WORD, tokenize

# Words that are never a bare name. Other keywords (KEY, INDEX, ROWS, FIRST, ...)
# only mean something in one spot, so they stay usable as table and column names.
RESERVED = frozenset(["ALL", "AND", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "CONSTRAINT", "CROSS", "DESC", "DISTINCT", "ELSE", "END", "EXCEPT", "EXISTS", "FALSE", "FETCH", "FOREIGN", "FROM", "FULL", "GROUP", "HAVING", "IN", "INNER", "INTERSECT", "INTO", "IS", "JOIN", "LATERAL", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER", "REFERENCES", "RETURNING", "RIGHT", "SELECT", "SET", "THEN", "TRUE", "UNION", "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH"])

SERIALS = {"SERIAL", "BIGSERIAL", "SMALLSERIAL", "SERIAL4", "SERIAL8", "SERIAL2"}  # INTEGER with a sequence
TYPES = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "REAL": "REAL", "FLOAT": "REAL", "DOUBLE": "REAL",
    "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
    "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC",
    "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIMESTAMPTZ": "TIMESTAMP", "INTERVAL": "INTERVAL",
    "JSON": "JSONB", "JSONB": "JSONB",
}
_CONTAINER_OPS = ("||", "->", "->>", "#>", "#>>", "@>", "<@", "&&", "?", "?|", "?&")
MAX_GROUPING_SETS = 4096
# Words that are values without parentheses, as in standard SQL.
_NILADIC = ("CURRENT_DATE", "CURRENT_TIMESTAMP", "LOCALTIMESTAMP")
_TYPED_LITERALS = ("DATE", "TIMESTAMP", "TIMESTAMPTZ", "INTERVAL", "NUMERIC", "DECIMAL", "JSON", "JSONB")

_QUERY_START = ("SELECT", "WITH", "VALUES")
_COMPARISONS = ("=", "!=", "<>", "<", "<=", ">", ">=")
_UNSUPPORTED_CREATE = ("MATERIALIZED", "TRIGGER", "PROCEDURE", "FUNCTION", "SCHEMA", "DATABASE", "SEQUENCE", "TYPE")


# Deeper trees would exhaust Python's recursion limit in later stages, so they are refused here.
MAX_NESTING = 64  # parentheses, subqueries, NOT and unary minus inside each other
MAX_TREE_DEPTH = 150  # including long left-leaning chains like a + a + a + ...


def parse(sql: str):
    """Parse exactly one statement. A trailing semicolon is allowed."""
    p = _Parser(sql)
    statement = p.statement()
    p.accept_op(";")
    if p.tok.kind != EOF:
        raise p.error(f"expected end of statement, got {p.describe()}")
    _check_depth(statement, sql)
    return statement


def _check_depth(statement, sql):
    stack = [(statement, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise SqlError(f"the statement is nested too deeply (limit {MAX_TREE_DEPTH} levels)", sql,
                           getattr(node, "pos", None))
        if isinstance(node, (list, tuple)):
            stack.extend((x, depth) for x in node)
        elif hasattr(node, "__dataclass_fields__"):
            stack.extend((getattr(node, name), depth + 1) for name in node.__dataclass_fields__ if name != "pos")


def parse_script(sql: str) -> list:
    """Parse any number of semicolon-separated statements."""
    return [statement for statement, _ in parse_statements(sql)]


def parse_statements(sql: str) -> list:
    """Parse a script into (statement, source text of that statement) pairs."""
    p = _Parser(sql)
    statements = []
    while True:
        while p.accept_op(";"):
            pass
        if p.tok.kind == EOF:
            return statements
        start = p.tok.pos
        statement = p.statement()
        _check_depth(statement, sql)
        statements.append((statement, sql[start:p.tok.pos].strip()))
        if p.tok.kind != EOF:
            p.expect_op(";")


class _Parser:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.i = 0
        self.nesting = 0
        self.window_refs = []  # per SELECT being parsed: [(spec, window name, parenthesized, token)] to resolve
        self.select_into = None  # (token, table name) of a SELECT ... INTO, which makes the statement a CREATE TABLE AS

    def nest(self):
        self.nesting += 1
        if self.nesting > MAX_NESTING:
            raise self.error(f"the statement is nested too deeply (limit {MAX_NESTING} levels)")

    def unnest(self):
        self.nesting -= 1

    # ---- token helpers ---------------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.i]

    def peek(self, k=1):
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    @property
    def prev_pos(self):
        """Position of the token just consumed."""
        return self.tokens[self.i - 1].pos

    def advance(self):
        tok = self.tok
        if tok.kind != EOF:
            self.i += 1
        return tok

    def describe(self, tok=None):
        tok = tok or self.tok
        if tok.kind == EOF:
            return "end of input"
        if tok.kind == STRING:
            return f"string {tok.value!r}"
        return f"'{tok.value}'"

    def expect_kw_of(self, *words):
        word = self.accept_kw(*words)
        if not word:
            raise self.error(f"expected {' or '.join(words)}, got {self.describe()}")
        return word

    def transaction_modes(self):
        """BEGIN's optional modes, in any order and comma-separated: ISOLATION LEVEL ..., READ ONLY,
        READ WRITE and [NOT] DEFERRABLE (accepted; it only matters to PostgreSQL's read-only SERIALIZABLE)."""
        isolation, read_only = None, False
        while True:
            if self.is_kw("ISOLATION"):
                isolation = self.isolation_level()
            elif self.is_kw("READ") and self.is_kw("ONLY", "WRITE", tok=self.peek()):
                self.advance()
                read_only = self.advance().value.upper() == "ONLY"
            elif self.accept_kw("NOT"):
                self.expect_kw("DEFERRABLE")
            elif not self.accept_kw("DEFERRABLE"):
                return n.Begin(isolation, read_only)
            self.accept_op(",")

    def isolation_level(self):
        """The ISOLATION LEVEL clause of BEGIN."""
        self.expect_kw("ISOLATION")
        self.expect_kw("LEVEL")
        if self.accept_kw("READ"):
            if self.accept_kw("COMMITTED"):
                return "READ COMMITTED"
            if self.accept_kw("UNCOMMITTED"):
                return "READ COMMITTED"  # as in PostgreSQL, which never shows uncommitted rows
            raise self.error(f"expected COMMITTED, got {self.describe()}")
        if self.accept_kw("REPEATABLE"):
            self.expect_kw("READ")
            return "REPEATABLE READ"
        if self.accept_kw("SERIALIZABLE"):
            return "SERIALIZABLE"
        raise self.error(f"expected READ COMMITTED, REPEATABLE READ or SERIALIZABLE, got {self.describe()}")

    def error(self, message, tok=None):
        return SqlError(message, self.sql, (tok or self.tok).pos)

    def unsupported(self, feature, tok=None):
        return self.error(f"Arcanum doesn't support {feature} yet", tok)

    def is_kw(self, *words, tok=None):
        tok = tok or self.tok
        return tok.kind == WORD and tok.value.upper() in words

    def accept_kw(self, *words):
        if self.is_kw(*words):
            return self.advance().value.upper()
        return None

    def expect_kw(self, word):
        if not self.accept_kw(word):
            raise self.error(f"expected {word}, got {self.describe()}")

    def is_op(self, *ops):
        return self.tok.kind == OP and self.tok.value in ops

    def accept_op(self, *ops):
        if self.is_op(*ops):
            return self.advance().value
        return None

    def expect_op(self, op):
        if not self.accept_op(op):
            raise self.error(f"expected '{op}', got {self.describe()}")

    def is_name(self, tok=None):
        tok = tok or self.tok
        return tok.kind == QUOTED or (tok.kind == WORD and tok.value.upper() not in RESERVED)

    def ident(self, what="a name"):
        if not self.is_name():
            raise self.error(f"expected {what}, got {self.describe()}")
        tok = self.advance()
        return tok.value if tok.kind == QUOTED else tok.value.lower()

    def comma_list(self, item):
        items = [item()]
        while self.accept_op(","):
            items.append(item())
        return items

    def paren_names(self, what="a column name"):
        self.expect_op("(")
        names = self.comma_list(lambda: self.ident(what))
        self.expect_op(")")
        return names

    def starts_query(self):
        return self.is_kw(*_QUERY_START)

    # ---- statements ----------------------------------------------------------

    def statement(self):
        stmt = self._statement()
        if self.select_into is None:
            return stmt
        tok, name = self.select_into
        self.select_into = None
        if not isinstance(stmt, n.Query):
            raise self.error("SELECT ... INTO is only allowed as a statement of its own", tok)
        return n.CreateTableAs(name, stmt)

    def _statement(self):
        if self.starts_query() or self.is_op("("):
            return self.query()
        tok = self.tok
        word = self.accept_kw("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "BEGIN", "COMMIT",
                              "ROLLBACK", "EXPLAIN", "ANALYZE", "SAVEPOINT", "RELEASE", "START", "END", "VACUUM",
                              "TRUNCATE")
        if word == "INSERT":
            return self.insert()
        if word == "UPDATE":
            return self.update()
        if word == "DELETE":
            return self.delete()
        if word == "CREATE":
            return self.create()
        if word == "DROP":
            return self.drop()
        if word == "ALTER":
            return self.alter()
        if word == "SAVEPOINT":
            return n.Savepoint(self.ident("a savepoint name"))
        if word == "RELEASE":
            self.accept_kw("SAVEPOINT")
            return n.Release(self.ident("a savepoint name"))
        if word == "START":
            self.expect_kw("TRANSACTION")
            return self.transaction_modes()
        if word in ("BEGIN", "COMMIT", "ROLLBACK", "END"):
            self.accept_kw("TRANSACTION", "WORK")
            if word == "BEGIN":
                return self.transaction_modes()
            if word == "ROLLBACK" and self.accept_kw("TO"):
                self.accept_kw("SAVEPOINT")
                return n.RollbackTo(self.ident("a savepoint name"))
            return n.Rollback() if word == "ROLLBACK" else n.Commit()
        if word == "EXPLAIN":
            analyze = self.explain_options()
            return n.Explain(self.statement(), analyze)
        if word == "ANALYZE":
            return n.Analyze(self.ident("a table name") if self.is_name() else None)
        if word == "VACUUM":
            if self.is_op("("):
                options = self.options("VACUUM", {"FULL", "ANALYZE", "VERBOSE"})
                full, analyze = options.get("FULL", False), options.get("ANALYZE", False)
            else:
                full = bool(self.accept_kw("FULL"))
                self.accept_kw("VERBOSE")
                analyze = bool(self.accept_kw("ANALYZE"))
            return n.Vacuum(self.ident("a table name") if self.is_name() else None, full, analyze)
        if word == "TRUNCATE":
            self.accept_kw("TABLE")
            tables = self.comma_list(lambda: self.ident("a table name"))
            if self.is_kw("RESTART"):
                raise self.unsupported("TRUNCATE ... RESTART IDENTITY")
            if self.accept_kw("CONTINUE"):
                self.expect_kw("IDENTITY")
            cascade = self.accept_kw("CASCADE", "RESTRICT") == "CASCADE"
            return n.Truncate(tables, cascade)
        raise self.error(f"expected a statement, got {self.describe(tok)}", tok)

    def explain_options(self):
        """EXPLAIN's options, old style (EXPLAIN ANALYZE VERBOSE) or in parentheses; whether to run the query."""
        if self.is_op("(") and self.peek().kind == WORD and not self.peek_starts_query():
            known = {"ANALYZE", "VERBOSE", "COSTS", "BUFFERS", "TIMING", "SUMMARY", "SETTINGS", "WAL", "FORMAT"}
            return self.options("EXPLAIN", known).get("ANALYZE", False)
        analyze = bool(self.accept_kw("ANALYZE"))
        self.accept_kw("VERBOSE")
        return analyze

    def options(self, statement, known):
        """A parenthesized option list such as (ANALYZE, COSTS false, FORMAT TEXT): {OPTION: value}."""
        self.expect_op("(")
        options = {}
        while True:
            tok = self.tok
            if tok.kind != WORD:
                raise self.error(f"expected a {statement} option, got {self.describe()}")
            name = self.advance().value.upper()  # option names such as FULL may be reserved words
            if name not in known:
                raise self.error(f"unrecognized {statement} option {name.lower()}", tok)
            if name == "FORMAT":
                if self.ident("a format").upper() != "TEXT":
                    raise self.unsupported("EXPLAIN formats other than TEXT", tok)
                options[name] = "TEXT"
            elif word := self.accept_kw("TRUE", "FALSE", "ON", "OFF"):
                options[name] = word in ("TRUE", "ON")
            elif self.tok.kind == INT and self.tok.value in ("0", "1"):
                options[name] = self.advance().value == "1"
            else:
                options[name] = True
            if not self.accept_op(","):
                break
        self.expect_op(")")
        return options

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident("a table name")
        columns = self.paren_names() if self.is_op("(") and not self.is_kw(*_QUERY_START, tok=self.peek()) else None
        if self.accept_kw("DEFAULT"):
            self.expect_kw("VALUES")
            if columns:
                raise self.error("DEFAULT VALUES takes no column list")
            return self.insert_rest(table, n.Query(n.Values([[]])), [])
        if not (self.starts_query() or self.is_op("(")):
            raise self.error(f"expected VALUES, SELECT or DEFAULT VALUES, got {self.describe()}")
        return self.insert_rest(table, self.query(), columns)

    def insert_rest(self, table, source, columns):
        on_conflict = None
        if self.accept_kw("ON"):
            self.expect_kw("CONFLICT")
            target = self.paren_names() if self.is_op("(") else None
            self.expect_kw("DO")
            if self.accept_kw("NOTHING"):
                on_conflict = n.OnConflict(target)
            else:
                self.expect_kw("UPDATE")
                if target is None:
                    raise self.error("ON CONFLICT DO UPDATE needs a conflict target, such as ON CONFLICT (id)")
                self.expect_kw("SET")
                assignments = self.comma_list(self.assignment)
                on_conflict = n.OnConflict(target, assignments, self.expr() if self.accept_kw("WHERE") else None)
        return n.Insert(table, source, columns, on_conflict, self.returning())

    def assignment(self):
        column = self.ident("a column name")
        self.expect_op("=")
        return column, self.expr()

    def returning(self):
        return self.comma_list(self.select_item) if self.accept_kw("RETURNING") else None

    def update(self):
        table = self.ident("a table name")
        alias = self.target_alias()
        self.expect_kw("SET")
        assignments = self.comma_list(self.assignment)
        from_ = self.from_clause() if self.accept_kw("FROM") else None
        where = self.expr() if self.accept_kw("WHERE") else None
        return n.Update(table, assignments, where, self.returning(), from_, alias)

    def delete(self):
        self.expect_kw("FROM")
        table = self.ident("a table name")
        alias = self.target_alias()
        using = self.from_clause() if self.accept_kw("USING") else None
        where = self.expr() if self.accept_kw("WHERE") else None
        return n.Delete(table, where, self.returning(), using, alias)

    def names_then_as(self):
        """Whether a parenthesized list of bare names followed by AS comes next: CREATE TABLE t (a, b) AS ..."""
        if not self.is_op("("):
            return False
        k = 1
        while self.peek(k).kind in (WORD, QUOTED):
            after = self.peek(k + 1)
            if after.kind == OP and after.value == ")":
                return self.is_kw("AS", tok=self.peek(k + 2))
            if not (after.kind == OP and after.value == ","):
                return False
            k += 2
        return False

    def target_alias(self):
        """The optional alias after UPDATE's or DELETE's table: [AS] name."""
        if self.accept_kw("AS"):
            return self.ident("an alias")
        return self.ident("an alias") if self.is_name() else None

    def create(self):
        if self.accept_kw("OR"):
            self.expect_kw("REPLACE")
            self.expect_kw("VIEW")
            return self.create_view(or_replace=True)
        if self.accept_kw("VIEW"):
            return self.create_view()
        unique = bool(self.accept_kw("UNIQUE"))
        if self.accept_kw("INDEX"):
            if_not_exists = self.if_not_exists()
            name = self.ident("an index name")
            self.expect_kw("ON")
            table = self.ident("a table name")
            return n.CreateIndex(name, table, self.paren_names(), unique, if_not_exists)
        if unique:
            raise self.error(f"expected INDEX after UNIQUE, got {self.describe()}")
        if self.accept_kw("TABLE"):
            return self.create_table()
        if self.is_kw(*_UNSUPPORTED_CREATE):
            raise self.unsupported(f"CREATE {self.tok.value.upper()}")
        raise self.error(f"expected TABLE or INDEX after CREATE, got {self.describe()}")

    def create_view(self, or_replace=False):
        name = self.ident("a view name")
        columns = self.paren_names() if self.is_op("(") else None
        self.expect_kw("AS")
        return n.CreateView(name, self.query(), columns, or_replace)

    def create_table(self):
        if_not_exists = self.if_not_exists()
        name = self.ident("a table name")
        if self.is_kw("AS") or self.names_then_as():
            columns = self.paren_names() if self.is_op("(") else None
            self.expect_kw("AS")
            if not (self.starts_query() or self.is_op("(")):
                raise self.error(f"expected a query after AS, got {self.describe()}")
            query = self.query()
            with_data = True
            if self.accept_kw("WITH"):
                with_data = not self.accept_kw("NO")
                self.expect_kw("DATA")
            return n.CreateTableAs(name, query, columns, if_not_exists, with_data)
        self.expect_op("(")
        stmt = n.CreateTable(name, [], [], if_not_exists)
        while True:
            tok = self.tok
            constraint = self.ident("a constraint name") if self.accept_kw("CONSTRAINT") else None
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                if stmt.primary_key:
                    raise self.error("table has more than one primary key", tok)
                stmt.primary_key = self.paren_names()
            elif self.accept_kw("UNIQUE"):
                stmt.uniques.append((constraint, self.paren_names()))
            elif self.is_kw("CHECK"):
                stmt.checks.append(self.check(constraint))
            elif self.accept_kw("FOREIGN"):
                self.expect_kw("KEY")
                columns = self.paren_names()
                stmt.foreign_keys.append(self.references(columns, constraint))
            elif constraint is not None:
                raise self.error(f"expected PRIMARY KEY, UNIQUE, CHECK or FOREIGN KEY, got {self.describe()}")
            else:
                column, is_pk = self.column_def(stmt)
                if is_pk:
                    if stmt.primary_key:
                        raise self.error("table has more than one primary key", tok)
                    stmt.primary_key = [column.name]
                stmt.columns.append(column)
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if not stmt.columns:
            raise self.error("a table needs at least one column")
        return stmt

    def default_value(self):
        """A DEFAULT's expression and its text as written."""
        start = self.tok.pos
        value = self.unary()
        return value, self.sql[start:self.tok.pos].strip()

    def alter(self):
        self.expect_kw("TABLE")
        if_exists = self.if_exists()
        name = self.ident("a table name")
        return n.AlterTable(name, self.comma_list(lambda: self.alter_action(name)), if_exists)

    def if_exists(self):
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            return True
        return False

    def alter_action(self, table):
        tok = self.tok
        if self.accept_kw("ADD"):
            if self.is_kw("CONSTRAINT", "CHECK", "FOREIGN", "UNIQUE", "PRIMARY"):
                return n.AlterAction("ADD CONSTRAINT", definition=self.table_constraint())
            self.accept_kw("COLUMN")
            if_not_exists = self.if_not_exists()
            holder = n.CreateTable(table, [])
            column, is_pk = self.column_def(holder)
            if is_pk or column.unique:
                raise self.unsupported("adding a PRIMARY KEY or UNIQUE column (add the column, then ADD UNIQUE (...))",
                                       tok)
            return n.AlterAction("ADD COLUMN", column.name, (column, holder.checks, holder.foreign_keys),
                                 if_exists=if_not_exists)
        if self.accept_kw("DROP"):
            if self.accept_kw("CONSTRAINT"):
                if_exists = self.if_exists()
                return n.AlterAction("DROP CONSTRAINT", new_name=self.ident("a constraint name"), if_exists=if_exists)
            self.accept_kw("COLUMN")
            if_exists = self.if_exists()
            action = n.AlterAction("DROP COLUMN", self.ident("a column name"), if_exists=if_exists)
            self.accept_kw("RESTRICT")
            return action
        if self.accept_kw("RENAME"):
            if self.accept_kw("TO"):
                return n.AlterAction("RENAME TABLE", new_name=self.ident("a table name"))
            self.accept_kw("COLUMN")
            column = self.ident("a column name")
            self.expect_kw("TO")
            return n.AlterAction("RENAME COLUMN", column, new_name=self.ident("a column name"))
        if self.accept_kw("ALTER"):
            self.accept_kw("COLUMN")
            column = self.ident("a column name")
            if self.accept_kw("SET"):
                if self.accept_kw("DEFAULT"):
                    value, text = self.default_value()
                    return n.AlterAction("SET DEFAULT", column, default=value, default_sql=text)
                if self.accept_kw("NOT"):
                    self.expect_kw("NULL")
                    return n.AlterAction("SET NOT NULL", column)
                self.expect_kw("DATA")
                self.expect_kw("TYPE")
                return self.alter_type(column)
            if self.accept_kw("DROP"):
                if self.accept_kw("DEFAULT"):
                    return n.AlterAction("DROP DEFAULT", column)
                self.expect_kw("NOT")
                self.expect_kw("NULL")
                return n.AlterAction("DROP NOT NULL", column)
            if self.accept_kw("TYPE"):
                return self.alter_type(column)
            raise self.error(f"expected SET, DROP or TYPE, got {self.describe()}")
        raise self.error(f"expected ADD, DROP, RENAME or ALTER, got {self.describe()}")

    def alter_type(self, column):
        type_, modifiers = self.type_with_modifiers()
        if self.is_kw("USING"):
            raise self.unsupported("ALTER COLUMN ... TYPE ... USING")
        return n.AlterAction("TYPE", column, type=type_, modifiers=modifiers)

    def table_constraint(self):
        name = self.ident("a constraint name") if self.accept_kw("CONSTRAINT") else None
        tok = self.tok
        if self.is_kw("CHECK"):
            return self.check(name)
        if self.accept_kw("FOREIGN"):
            self.expect_kw("KEY")
            return self.references(self.paren_names(), name)
        if self.accept_kw("UNIQUE"):
            return ("UNIQUE", name, self.paren_names())
        if self.accept_kw("PRIMARY"):
            raise self.unsupported("adding a PRIMARY KEY to an existing table", tok)
        raise self.error(f"expected CHECK, FOREIGN KEY or UNIQUE, got {self.describe()}")

    def check(self, name):
        self.expect_kw("CHECK")
        self.expect_op("(")
        start = self.tok.pos
        expr = self.expr()
        text = self.sql[start:self.tok.pos].strip()
        self.expect_op(")")
        return n.Check(expr, name, text)

    def references(self, columns, name):
        self.expect_kw("REFERENCES")
        fk = n.ForeignKey(columns, self.ident("a table name"), self.paren_names() if self.is_op("(") else [], name=name)
        while self.accept_kw("ON"):
            event = self.accept_kw("DELETE", "UPDATE")
            if event is None:
                raise self.error(f"expected DELETE or UPDATE, got {self.describe()}")
            tok = self.tok
            if self.accept_kw("CASCADE"):
                action = "CASCADE"
            elif self.accept_kw("RESTRICT"):
                action = "RESTRICT"
            elif self.accept_kw("NO"):
                self.expect_kw("ACTION")
                action = "NO ACTION"
            elif self.accept_kw("SET"):
                if not self.accept_kw("NULL"):
                    raise self.unsupported("ON DELETE SET DEFAULT (use SET NULL or CASCADE)", tok)
                action = "SET NULL"
            else:
                raise self.error(f"expected CASCADE, RESTRICT, NO ACTION or SET NULL, got {self.describe()}")
            if event == "UPDATE":
                if action not in ("NO ACTION", "RESTRICT"):
                    raise self.unsupported(f"ON UPDATE {action} (use NO ACTION or RESTRICT)", tok)
                fk.on_update = action
            else:
                fk.on_delete = action
        return fk

    def column_def(self, table):
        name = self.ident("a column name")
        if self.tok.kind == WORD and self.tok.value.upper() in SERIALS:
            self.advance()
            column = n.ColumnDef(name, "INTEGER", not_null=True, identity="BY DEFAULT")
        else:
            type_, modifiers = self.type_with_modifiers()
            column = n.ColumnDef(name, type_, modifiers=modifiers)
        is_pk = False
        while True:
            constraint = self.ident("a constraint name") if self.accept_kw("CONSTRAINT") else None
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                is_pk = True
            elif self.is_kw("CHECK"):
                table.checks.append(self.check(constraint or f"{table.name}_{name}_check"))
            elif self.is_kw("REFERENCES"):
                table.foreign_keys.append(self.references([name], constraint))
            elif self.accept_kw("GENERATED"):
                mode = "ALWAYS" if self.accept_kw("ALWAYS") else None
                if mode is None:
                    self.expect_kw("BY")
                    self.expect_kw("DEFAULT")
                    mode = "BY DEFAULT"
                self.expect_kw("AS")
                self.expect_kw("IDENTITY")
                if column.type != "INTEGER":
                    raise self.error(f"identity column {name} must be an integer")
                column.identity, column.not_null = mode, True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                column.not_null = True
            elif self.accept_kw("NULL"):
                pass
            elif self.accept_kw("UNIQUE"):
                column.unique = True
            elif self.accept_kw("DEFAULT"):
                column.default, column.default_sql = self.default_value()
            else:
                return column, is_pk

    def type_name(self):
        return self.type_with_modifiers()[0]

    def type_with_modifiers(self):
        """A type name and its numbers: NUMERIC(10, 2) is ("NUMERIC", (10, 2)). VARCHAR(255) and TIMESTAMP(3)
        accept a size, which is ignored."""
        tok = self.tok
        if tok.kind != WORD or tok.value.upper() not in TYPES:
            supported = "INTEGER, REAL, NUMERIC, TEXT, BOOLEAN, DATE, TIMESTAMP and INTERVAL"
            raise self.error(f"expected a type ({supported}), got {self.describe()}")
        self.advance()
        word = tok.value.upper()
        if word == "DOUBLE":
            self.accept_kw("PRECISION")
        if word in ("CHAR", "CHARACTER"):
            self.accept_kw("VARYING")
        modifiers = ()
        if self.accept_op("("):
            modifiers = tuple(self.comma_list(self.expect_int))
            self.expect_op(")")
        if word == "TIMESTAMP" and self.accept_kw("WITH", "WITHOUT"):
            self.expect_kw("TIME")
            self.expect_kw("ZONE")
        type_ = TYPES[word]
        dimensions = 0
        while True:  # INTEGER[], TEXT[3], INTEGER ARRAY
            if self.accept_op("["):
                if not self.is_op("]"):
                    self.expect_int()
                self.expect_op("]")
            elif self.accept_kw("ARRAY"):
                if self.accept_op("["):
                    self.expect_int()
                    self.expect_op("]")
            else:
                break
            dimensions += 1
        if dimensions > 1:
            raise self.unsupported("arrays of more than one dimension", tok)
        if dimensions:
            if type_ in ("INTERVAL", "JSONB"):
                raise self.unsupported(f"arrays of {type_}", tok)
            return type_ + "[]", ()
        if type_ == "NUMERIC" and modifiers:
            precision, scale = (*modifiers, 0)[:2]
            if len(modifiers) > 2 or not 1 <= precision <= 1000 or not 0 <= scale <= precision:
                raise self.error(f"NUMERIC({', '.join(map(str, modifiers))}) is not a valid precision and scale", tok)
            return type_, (precision, scale)
        return type_, ()

    def expect_int(self):
        if self.tok.kind != INT:
            raise self.error(f"expected a number, got {self.describe()}")
        return int(self.advance().value)

    def if_not_exists(self):
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            return True
        return False

    def drop(self):
        word = self.accept_kw("TABLE", "INDEX", "VIEW")
        if not word:
            raise self.error(f"expected TABLE, INDEX or VIEW after DROP, got {self.describe()}")
        if_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            if_exists = True
        if word == "TABLE":
            return n.DropTable(self.ident("a table name"), if_exists)
        if word == "VIEW":
            return n.DropView(self.ident("a view name"), if_exists)
        return n.DropIndex(self.ident("an index name"), if_exists)

    # ---- queries -------------------------------------------------------------

    def query(self):
        self.nest()
        try:
            return self._query()
        finally:
            self.unnest()

    def _query(self):
        with_ = None
        if self.accept_kw("WITH"):
            recursive = bool(self.accept_kw("RECURSIVE"))
            with_ = n.With(recursive, self.comma_list(self.cte))
        body = self.set_expr()
        order_by = self.order_by() if self.accept_kw("ORDER") else []
        limit = offset = None
        seen = set()
        while self.is_kw("LIMIT", "OFFSET", "FETCH"):
            # LIMIT n and OFFSET n in either order, or the standard OFFSET n ROWS FETCH FIRST n ROWS ONLY.
            tok = self.tok
            word = self.advance().value.upper()
            clause = "OFFSET" if word == "OFFSET" else "LIMIT"
            if clause in seen:
                raise self.error(f"{'OFFSET' if clause == 'OFFSET' else 'LIMIT or FETCH'} is given twice", tok)
            seen.add(clause)
            if word == "LIMIT":
                limit = None if self.accept_kw("ALL") else self.expr()
            elif word == "OFFSET":
                offset = self.additive()
                self.accept_kw("ROW", "ROWS")
            else:
                self.expect_kw_of("FIRST", "NEXT")
                limit = n.Literal(1) if self.is_kw("ROW", "ROWS") else self.additive()
                self.expect_kw_of("ROW", "ROWS")
                if self.is_kw("WITH"):
                    raise self.unsupported("FETCH ... WITH TIES")
                self.expect_kw("ONLY")
        return n.Query(body, with_, order_by, limit, offset)

    def cte(self):
        name = self.ident("a CTE name")
        columns = self.paren_names() if self.is_op("(") else None
        self.expect_kw("AS")
        self.expect_op("(")
        query = self.query()
        self.expect_op(")")
        return n.CTE(name, query, columns)

    def set_expr(self):
        left = self.set_term()
        while op := self.accept_kw("UNION", "EXCEPT"):
            left = n.SetOp(op, self.set_quantifier(), left, self.set_term())
        return left

    def set_term(self):
        left = self.set_primary()
        while self.accept_kw("INTERSECT"):
            left = n.SetOp("INTERSECT", self.set_quantifier(), left, self.set_primary())
        return left

    def set_quantifier(self):
        if self.accept_kw("ALL"):
            return True
        self.accept_kw("DISTINCT")
        return False

    def set_primary(self):
        if self.accept_op("("):
            query = self.query()
            self.expect_op(")")
            return query
        if self.accept_kw("VALUES"):
            return n.Values(self.comma_list(self.paren_exprs))
        if self.is_kw("SELECT"):
            return self.select()
        raise self.error(f"expected SELECT, VALUES or '(', got {self.describe()}")

    def paren_exprs(self):
        self.expect_op("(")
        items = self.comma_list(self.expr)
        self.expect_op(")")
        return items

    def select(self):
        self.expect_kw("SELECT")
        self.window_refs.append([])
        distinct = self.accept_kw("DISTINCT", "ALL") == "DISTINCT"
        distinct_on = []
        if distinct and self.accept_kw("ON"):
            self.expect_op("(")
            distinct_on = self.comma_list(self.expr)
            self.expect_op(")")
        columns = self.comma_list(self.select_item)
        if self.is_kw("INTO"):
            tok = self.advance()
            if self.nesting != 1 or self.select_into is not None:
                raise self.error("SELECT ... INTO is only allowed in the outermost SELECT", tok)
            self.accept_kw("TABLE")
            self.select_into = (tok, self.ident("a table name"))
        from_ = self.from_clause() if self.accept_kw("FROM") else None
        where = self.expr() if self.accept_kw("WHERE") else None
        group_by, grouping_sets = [], None
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            group_by, grouping_sets = self.group_by()
        having = self.expr() if self.accept_kw("HAVING") else None
        named = {}
        if self.accept_kw("WINDOW"):
            for tok, name, spec in self.comma_list(self.named_window):
                if name in named:
                    raise self.error(f"window {name} is already defined", tok)
                named[name] = spec
        self.resolve_windows(named, self.window_refs.pop())
        return n.Select(columns, from_, where, group_by, having, distinct and not distinct_on, distinct_on,
                        grouping_sets)

    def select_item(self):
        if self.accept_op("*"):
            return n.SelectItem(n.Star(pos=self.prev_pos))
        pos = self.tok.pos
        if self.is_name() and (self.peek().kind, self.peek().value, self.peek(2).kind, self.peek(2).value) == (OP, ".", OP, "*"):
            table = self.ident()
            self.advance()
            self.advance()
            return n.SelectItem(n.Star(table, pos))
        return n.SelectItem(self.expr(), self.alias())

    def alias(self):
        if self.accept_kw("AS"):
            return self.ident("an alias")
        return self.ident() if self.is_name() else None

    def from_clause(self):
        item = self.join_expr()
        while self.accept_op(","):
            item = n.Join("CROSS", item, self.join_expr())
        return item

    def join_expr(self):
        left = self.table_primary()
        while True:
            if self.is_kw("NATURAL"):
                raise self.unsupported("NATURAL joins")
            if self.accept_kw("CROSS"):
                self.expect_kw("JOIN")
                left = n.Join("CROSS", left, self.table_primary())
                continue
            kind = self.accept_kw("JOIN", "INNER", "LEFT", "RIGHT", "FULL")
            if not kind:
                return left
            if kind == "JOIN":
                kind = "INNER"
            else:
                if kind != "INNER":
                    self.accept_kw("OUTER")
                self.expect_kw("JOIN")
            right = self.table_primary()
            if self.accept_kw("ON"):
                left = n.Join(kind, left, right, on=self.expr())
            elif self.accept_kw("USING"):
                left = n.Join(kind, left, right, using=self.paren_names())
            else:
                raise self.error(f"expected ON or USING after the joined table, got {self.describe()}")

    def table_primary(self):
        lateral = bool(self.accept_kw("LATERAL"))
        if self.accept_op("("):
            if not self.starts_query():
                raise self.unsupported("parenthesized joins")
            query = self.query()
            self.expect_op(")")
            tok = self.tok
            alias = self.alias()
            if alias is None:
                raise self.error("a subquery in FROM needs an alias, e.g. (SELECT ...) AS sub", tok)
            return n.SubqueryRef(query, alias, lateral, self.column_aliases())
        pos = self.tok.pos
        if self.is_name() and self.peek().kind == OP and self.peek().value == "(":
            name = self.advance().value.upper()
            self.expect_op("(")
            args = [] if self.is_op(")") else self.comma_list(self.expr)
            self.expect_op(")")
            return n.FunctionRef(name, args, self.alias(), self.column_aliases(), pos)
        if lateral:
            raise self.error(f"expected a subquery or a function after LATERAL, got {self.describe()}")
        return n.TableRef(self.ident("a table name"), self.alias(), pos)

    def column_aliases(self):
        return self.paren_names() if self.is_op("(") else None

    def group_by(self):
        """GROUP BY items. Returns (every distinct expression, the grouping sets or None). Plain items are
        in every set; ROLLUP, CUBE and GROUPING SETS multiply the sets, as in PostgreSQL."""
        sets, plain = [[]], True
        self._plain_groups = []
        for item in self.comma_list(self.grouping_item):
            plain = plain and item is None
            options = item if item is not None else None
            if options is None:
                continue
            sets = [a + b for a in sets for b in options]
            if len(sets) > MAX_GROUPING_SETS:
                raise self.error(f"too many grouping sets (the limit is {MAX_GROUPING_SETS})")
        exprs = []
        for e in self._plain_groups + [e for s in sets for e in s]:
            if e not in exprs:
                exprs.append(e)
        if plain:
            return exprs, None
        return exprs, [self._plain_groups + s for s in sets]

    def grouping_item(self):
        """None for a plain expression (remembered in _plain_groups), else the list of sets it stands for."""
        word = self.accept_kw("ROLLUP", "CUBE", "GROUPING")
        if word is None:
            if self.is_op("(") and self.peek().kind == OP and self.peek().value == ")":
                self.advance()
                self.advance()
                return [[]]  # (): the grand total
            self._plain_groups.append(self.expr())
            return None
        if word == "GROUPING":
            self.expect_kw("SETS")
            self.expect_op("(")
            sets = self.comma_list(self.grouping_set)
            self.expect_op(")")
            return sets
        self.expect_op("(")
        elements = self.comma_list(self.grouping_set)
        self.expect_op(")")
        if word == "ROLLUP":
            return [[e for part in elements[:k] for e in part] for k in range(len(elements), -1, -1)]
        if len(elements) > 12:
            raise self.error("CUBE takes at most 12 elements")
        width = len(elements)
        return [[e for i in range(width) if mask & (1 << (width - 1 - i)) for e in elements[i]]
                for mask in range((1 << width) - 1, -1, -1)]

    def grouping_set(self):
        """(a, b), (), or a single expression: a list of expressions."""
        if self.is_op("(") and not self.peek_starts_query():
            self.advance()
            items = [] if self.is_op(")") else self.comma_list(self.expr)
            self.expect_op(")")
            return items
        return [self.expr()]

    def peek_starts_query(self):
        tok = self.peek()
        return tok.kind == WORD and tok.value.upper() in _QUERY_START

    def order_by(self):
        self.expect_kw("BY")

        def item():
            expr = self.expr()
            desc = self.accept_kw("ASC", "DESC") == "DESC"
            nulls_first = None
            if self.accept_kw("NULLS"):
                word = self.accept_kw("FIRST", "LAST")
                if not word:
                    raise self.error(f"expected FIRST or LAST, got {self.describe()}")
                nulls_first = word == "FIRST"
            return n.OrderItem(expr, desc, nulls_first)

        return self.comma_list(item)

    # ---- expressions ---------------------------------------------------------

    def expr(self):
        self.nest()
        try:
            return self._expr()
        finally:
            self.unnest()

    def _expr(self):
        left = self.and_expr()
        while self.accept_kw("OR"):
            pos = self.prev_pos
            left = n.Binary("OR", left, self.and_expr(), pos)
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept_kw("AND"):
            pos = self.prev_pos
            left = n.Binary("AND", left, self.not_expr(), pos)
        return left

    def not_expr(self):
        if self.accept_kw("NOT"):
            pos = self.prev_pos
            self.nest()
            try:
                return n.Unary("NOT", self.not_expr(), pos)
            finally:
                self.unnest()
        return self.comparison()

    def comparison(self):
        left = self.concat()
        while True:
            if op := self.accept_op(*_COMPARISONS, "~", "~*", "!~", "!~*"):
                pos = self.prev_pos
                op = "!=" if op == "<>" else op
                if self.is_kw("ANY", "SOME", "ALL") and self.peek().kind == OP and self.peek().value == "(":
                    left = self.quantified(op, left)
                else:
                    left = self.row_compare(op, left, self.concat(), pos)
            elif self.accept_kw("IS"):
                negated = bool(self.accept_kw("NOT"))
                if word := self.accept_kw("TRUE", "FALSE"):
                    left = n.IsDistinct(left, n.Literal(word == "TRUE"), not negated)
                elif self.accept_kw("DISTINCT"):
                    self.expect_kw("FROM")
                    left = n.IsDistinct(left, self.concat(), negated)
                else:
                    if not self.accept_kw("UNKNOWN"):  # IS UNKNOWN: the boolean spelling of IS NULL
                        self.expect_kw("NULL")
                    left = n.IsNull(left, negated)
            else:
                negated = self.is_kw("NOT") and self.is_kw("IN", "BETWEEN", "LIKE", "ILIKE", tok=self.peek())
                if negated:
                    self.advance()
                if self.accept_kw("IN"):
                    left = self.in_rest(left, negated)
                elif self.accept_kw("BETWEEN"):
                    low = self.concat()
                    self.expect_kw("AND")
                    left = n.Between(left, low, self.concat(), negated)
                elif word := self.accept_kw("LIKE", "ILIKE"):
                    pattern = self.concat()
                    escape = self.concat() if self.accept_kw("ESCAPE") else None
                    left = n.Like(left, pattern, negated, word == "ILIKE", escape)
                else:
                    return left

    def quantified(self, op, left):
        """x op ANY (array) / ALL (array); = ANY (subquery) is IN, <> ALL (subquery) is NOT IN."""
        every = self.advance().value.upper() == "ALL"
        self.expect_op("(")
        if self.starts_query():
            query = self.query()
            self.expect_op(")")
            if (op, every) == ("=", False):
                return n.InSubquery(left, query)
            if (op, every) == ("!=", True):
                return n.InSubquery(left, query, negated=True)
            raise self.unsupported(f"{op} {'ALL' if every else 'ANY'} with a subquery (use an array, or IN)")
        right = self.expr()
        self.expect_op(")")
        return n.Quantified(op, left, right, every)

    def row_compare(self, op, left, right, pos=None):
        """left op right, where a row on either side compares field by field: (a, b) = (c, d) is a = c AND b = d,
        != is a != c OR b != d, and < <= > >= compare in order, as in PostgreSQL: (a, b) < (c, d) is
        a < c OR (a = c AND b < d)."""
        rows = [x for x in (left, right) if isinstance(x, n.RowValue)]
        if not rows:
            return n.Binary(op, left, right, pos)
        if len(rows) == 1 or len(left.items) != len(right.items):
            raise self.error("both sides of a row comparison must be rows with the same number of fields")
        pairs = list(zip(left.items, right.items, strict=True))
        if op in ("=", "!="):
            join = "AND" if op == "=" else "OR"
            return functools.reduce(lambda x, y: n.Binary(join, x, y), (n.Binary(op, a, b, pos) for a, b in pairs))
        strict = op[0]  # < or >
        result = n.Binary(op, *pairs[-1], pos)
        for a, b in reversed(pairs[:-1]):
            result = n.Binary("OR", n.Binary(strict, a, b, pos), n.Binary("AND", n.Binary("=", a, b, pos), result))
        return result

    def in_rest(self, left, negated):
        self.expect_op("(")
        if self.starts_query():
            if isinstance(left, n.RowValue):
                raise self.unsupported("row values with IN (subquery)")
            query = self.query()
            self.expect_op(")")
            return n.InSubquery(left, query, negated)
        items = self.comma_list(self.expr)
        self.expect_op(")")
        if isinstance(left, n.RowValue) or any(isinstance(item, n.RowValue) for item in items):
            # (a, b) IN ((1, 2), (3, 4)) is (a = 1 AND b = 2) OR (a = 3 AND b = 4)
            match = functools.reduce(lambda x, y: n.Binary("OR", x, y), (self.row_compare("=", left, i) for i in items))
            return n.Unary("NOT", match) if negated else match
        return n.InList(left, items, negated)

    def concat(self):
        """|| and PostgreSQL's other operators (-> ->> #> @> ? && ...), which share one precedence level."""
        left = self.additive()
        while op := self.accept_op(*_CONTAINER_OPS):
            pos = self.prev_pos
            left = n.Binary(op, left, self.additive(), pos)
        return left

    def additive(self):
        left = self.multiplicative()
        while op := self.accept_op("+", "-"):
            pos = self.prev_pos
            left = n.Binary(op, left, self.multiplicative(), pos)
        return left

    def multiplicative(self):
        left = self.exponent()
        while op := self.accept_op("*", "/", "%"):
            pos = self.prev_pos
            left = n.Binary(op, left, self.exponent(), pos)
        return left

    def exponent(self):
        left = self.unary()
        while self.accept_op("^"):  # left-associative, as in PostgreSQL: 2 ^ 3 ^ 2 = 64
            pos = self.prev_pos
            left = n.Binary("^", left, self.unary(), pos)
        return left

    def unary(self):
        if op := self.accept_op("-", "+"):
            pos = self.prev_pos
            self.nest()
            try:
                operand = self.unary()
            finally:
                self.unnest()
            if op == "-" and isinstance(operand, n.Literal) and type(operand.value) in (int, float):
                return n.Literal(-operand.value)  # so -9223372036854775808 stays in range
            return n.Unary(op, operand, pos)
        expr = self.primary()
        while True:
            if self.accept_op("::"):  # PostgreSQL's cast: x::date
                type_, modifiers = self.type_with_modifiers()
                expr = n.Cast(expr, type_, modifiers)
            elif self.accept_op("["):  # a[1], j['key']
                index = self.expr()
                self.expect_op("]")
                expr = n.Subscript(expr, index)
            else:
                return expr

    def primary(self):
        tok = self.tok
        if tok.kind == INT:
            self.advance()
            return n.Literal(int(tok.value))
        if tok.kind == FLOAT:
            self.advance()
            return n.Literal(float(tok.value))
        if tok.kind == STRING:
            self.advance()
            return n.Literal(tok.value)
        if self.accept_op("("):
            if self.starts_query():
                query = self.query()
                self.expect_op(")")
                return n.Subquery(query)
            expr = self.expr()
            if self.accept_op(","):
                expr = n.RowValue([expr, *self.comma_list(self.expr)], tok.pos)
            self.expect_op(")")
            return expr
        if tok.kind == WORD and tok.value.upper() == "ROW" and self.peek().kind == OP and self.peek().value == "(":
            self.advance()
            self.advance()
            items = [] if self.is_op(")") else self.comma_list(self.expr)
            self.expect_op(")")
            return n.RowValue(items, tok.pos)
        if tok.kind == WORD and tok.value.upper() == "ARRAY" and self.peek().kind == OP and self.peek().value == "[":
            self.advance()
            self.advance()
            items = [] if self.is_op("]") else self.comma_list(self.expr)
            self.expect_op("]")
            return n.ArrayLiteral(items)
        if tok.kind == WORD and tok.value.upper() in _TYPED_LITERALS and self.peek().kind == STRING:
            self.advance()  # DATE '2024-01-31', INTERVAL '7 days': a string read as that type
            return n.Cast(n.Literal(self.advance().value), TYPES[tok.value.upper()])
        if tok.kind == WORD and tok.value.upper() in _NILADIC and not (self.peek().kind == OP
                                                                       and self.peek().value == "("):
            self.advance()
            return n.Call(tok.value.upper(), pos=tok.pos)
        word = self.accept_kw("NULL", "TRUE", "FALSE", "CASE", "CAST", "EXISTS")
        if word in ("NULL", "TRUE", "FALSE"):
            return n.Literal({"NULL": None, "TRUE": True, "FALSE": False}[word])
        if word == "CASE":
            return self.case()
        if word == "CAST":
            self.expect_op("(")
            expr = self.expr()
            self.expect_kw("AS")
            type_, modifiers = self.type_with_modifiers()
            self.expect_op(")")
            return n.Cast(expr, type_, modifiers)
        if word == "EXISTS":
            self.expect_op("(")
            query = self.query()
            self.expect_op(")")
            return n.Exists(query)
        if (tok.kind == WORD and (self.is_name() or tok.value.upper() in ("LEFT", "RIGHT"))
                and self.peek().kind == OP and self.peek().value == "("):
            return self.call(self.advance().value.upper(), tok.pos)
        if self.is_name():
            name = self.ident()
            if self.accept_op("."):
                return n.ColumnRef(self.ident("a column name"), name, tok.pos)
            return n.ColumnRef(name, pos=tok.pos)
        raise self.error(f"expected an expression, got {self.describe()}")

    def case(self):
        operand = None if self.is_kw("WHEN") else self.expr()
        whens = []
        while self.accept_kw("WHEN"):
            condition = self.expr()
            self.expect_kw("THEN")
            whens.append((condition, self.expr()))
        if not whens:
            raise self.error(f"expected WHEN, got {self.describe()}")
        else_ = self.expr() if self.accept_kw("ELSE") else None
        self.expect_kw("END")
        return n.Case(operand, whens, else_)

    def call(self, name, pos):
        self.expect_op("(")
        call = n.Call(name, pos=pos)
        if name == "EXTRACT":  # EXTRACT(field FROM value) is date_part('field', value)
            field = self.advance()
            if field.kind not in (WORD, STRING):
                raise self.error(f"expected a field such as YEAR, got {self.describe(field)}", field)
            self.expect_kw("FROM")
            call = n.Call("DATE_PART", [n.Literal(field.value.lower()), self.expr()], pos=pos)
        elif name == "POSITION":  # POSITION(sub IN text) is strpos(text, sub)
            sub = self.concat()
            self.expect_kw("IN")
            call = n.Call("STRPOS", [self.concat(), sub], pos=pos)
        elif self.accept_op("*"):
            call.star = True
        elif not self.is_op(")"):
            call.distinct = self.accept_kw("DISTINCT", "ALL") == "DISTINCT"
            call.args = self.comma_list(self.expr)
            if self.accept_kw("ORDER"):
                call.order_by = self.order_by()
        self.expect_op(")")
        if self.accept_kw("WITHIN"):
            self.expect_kw("GROUP")
            self.expect_op("(")
            self.expect_kw("ORDER")
            if call.order_by:
                raise self.error("an aggregate cannot have both ORDER BY in its arguments and WITHIN GROUP")
            call.order_by, call.within_group = self.order_by(), True
            self.expect_op(")")
        if self.accept_kw("FILTER"):
            self.expect_op("(")
            self.expect_kw("WHERE")
            call.filter = self.expr()
            self.expect_op(")")
        if self.accept_kw("OVER"):
            call.over = self.window_spec()
        return call

    def named_window(self):
        tok = self.tok
        name = self.ident("a window name")
        self.expect_kw("AS")
        if not self.is_op("("):
            raise self.error(f"expected '(' after AS, got {self.describe()}")
        return tok, name, self.window_spec()

    def resolve_windows(self, named, refs):
        """Fill each OVER name and OVER (name ...) of one SELECT from its WINDOW clause, as PostgreSQL does:
        a definition may build on an earlier one, and a reference may add ORDER BY and a frame."""
        by_spec = {id(ref[0]): ref for ref in refs}
        done = set()

        def resolve(spec, name, parenthesized, tok, seen=()):
            if name in seen:
                raise self.error(f"window {name} refers to itself", tok)
            base = named.get(name)
            if base is None:
                raise self.error(f"window {name} does not exist", tok)
            if id(base) in by_spec and id(base) not in done:
                resolve(*by_spec[id(base)], seen=(*seen, name))
            if spec.partition_by:
                raise self.error(f"cannot override the PARTITION BY clause of window {name}", tok)
            if spec.order_by and base.order_by:
                raise self.error(f"cannot override the ORDER BY clause of window {name}", tok)
            if parenthesized and base.frame is not None:
                raise self.error(f"cannot copy window {name} because it has a frame clause", tok)
            spec.partition_by = base.partition_by
            spec.order_by = spec.order_by or base.order_by
            spec.frame = spec.frame or base.frame
            done.add(id(spec))

        for ref in refs:
            if id(ref[0]) not in done:
                resolve(*ref)

    def window_ref(self, spec, parenthesized):
        """OVER name, or OVER (name ...): spec takes the named window's clauses once the WINDOW clause is read."""
        tok = self.tok
        name = self.ident("a window name")
        if not self.window_refs:
            raise self.error(f"window {name} does not exist", tok)
        self.window_refs[-1].append((spec, name, parenthesized, tok))
        return spec

    def window_spec(self):
        spec = n.WindowSpec()
        if not self.is_op("("):
            return self.window_ref(spec, parenthesized=False)
        self.advance()
        if self.is_name() and not self.is_kw("PARTITION", "ORDER", "ROWS", "RANGE"):
            self.window_ref(spec, parenthesized=True)
        if self.accept_kw("PARTITION"):
            self.expect_kw("BY")
            spec.partition_by = self.comma_list(self.expr)
        if self.accept_kw("ORDER"):
            spec.order_by = self.order_by()
        if mode := self.accept_kw("ROWS", "RANGE"):
            if self.accept_kw("BETWEEN"):
                start = self.frame_bound()
                self.expect_kw("AND")
                end = self.frame_bound()
            else:
                start, end = self.frame_bound(), n.FrameBound("CURRENT ROW")
            spec.frame = n.Frame(mode, start, end)
        self.expect_op(")")
        return spec

    def frame_bound(self):
        if self.accept_kw("UNBOUNDED"):
            direction = self.accept_kw("PRECEDING", "FOLLOWING")
            if not direction:
                raise self.error(f"expected PRECEDING or FOLLOWING, got {self.describe()}")
            return n.FrameBound(f"UNBOUNDED {direction}")
        if self.accept_kw("CURRENT"):
            self.expect_kw("ROW")
            return n.FrameBound("CURRENT ROW")
        offset = self.additive()
        direction = self.accept_kw("PRECEDING", "FOLLOWING")
        if not direction:
            raise self.error(f"expected PRECEDING or FOLLOWING, got {self.describe()}")
        return n.FrameBound(direction, offset)
