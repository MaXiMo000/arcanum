"""Recursive-descent SQL parser: text -> syntax tree (see nodes.py).

Expression precedence, loosest first:
    OR, AND, NOT, comparisons (= != < <= > >= IS IN BETWEEN LIKE), ||, + -, * / %, unary - +

Set operations: INTERSECT binds tighter than UNION and EXCEPT, as in standard SQL.
"""

from ..errors import SqlError
from . import nodes as n
from .lexer import EOF, FLOAT, INT, OP, QUOTED, STRING, WORD, tokenize

# Words that are never a bare name. Other keywords (KEY, INDEX, ROWS, FIRST, ...)
# only mean something in one spot, so they stay usable as table and column names.
RESERVED = frozenset(["ALL", "AND", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CROSS", "DESC", "DISTINCT", "ELSE", "END", "EXCEPT", "EXISTS", "FALSE", "FROM", "FULL", "GROUP", "HAVING", "IN", "INNER", "INTERSECT", "IS", "JOIN", "LATERAL", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER", "RIGHT", "SELECT", "SET", "THEN", "TRUE", "UNION", "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH"])

TYPES = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "REAL": "REAL", "FLOAT": "REAL", "DOUBLE": "REAL",
    "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT",
    "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
}

_QUERY_START = ("SELECT", "WITH", "VALUES")
_COMPARISONS = ("=", "!=", "<>", "<", "<=", ">", ">=")
_UNSUPPORTED_CREATE = ("VIEW", "TRIGGER", "PROCEDURE", "FUNCTION", "SCHEMA", "DATABASE", "SEQUENCE", "TYPE")


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
        if self.starts_query() or self.is_op("("):
            return self.query()
        tok = self.tok
        word = self.accept_kw("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "BEGIN", "COMMIT",
                              "ROLLBACK", "EXPLAIN", "ANALYZE")
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
        if word in ("BEGIN", "COMMIT", "ROLLBACK"):
            self.accept_kw("TRANSACTION")
            return {"BEGIN": n.Begin, "COMMIT": n.Commit, "ROLLBACK": n.Rollback}[word]()
        if word == "EXPLAIN":
            analyze = bool(self.accept_kw("ANALYZE"))
            return n.Explain(self.statement(), analyze)
        if word == "ANALYZE":
            return n.Analyze(self.ident("a table name") if self.is_name() else None)
        raise self.error(f"expected a statement, got {self.describe(tok)}", tok)

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident("a table name")
        columns = self.paren_names() if self.is_op("(") and not self.is_kw(*_QUERY_START, tok=self.peek()) else None
        if not (self.starts_query() or self.is_op("(")):
            raise self.error(f"expected VALUES or SELECT, got {self.describe()}")
        return n.Insert(table, self.query(), columns)

    def update(self):
        table = self.ident("a table name")
        self.expect_kw("SET")

        def assignment():
            column = self.ident("a column name")
            self.expect_op("=")
            return column, self.expr()

        assignments = self.comma_list(assignment)
        where = self.expr() if self.accept_kw("WHERE") else None
        return n.Update(table, assignments, where)

    def delete(self):
        self.expect_kw("FROM")
        table = self.ident("a table name")
        where = self.expr() if self.accept_kw("WHERE") else None
        return n.Delete(table, where)

    def create(self):
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

    def create_table(self):
        if_not_exists = self.if_not_exists()
        name = self.ident("a table name")
        self.expect_op("(")
        columns, primary_key = [], []
        while True:
            tok = self.tok
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                if primary_key:
                    raise self.error("table has more than one primary key", tok)
                primary_key = self.paren_names()
            else:
                column, is_pk = self.column_def()
                if is_pk:
                    if primary_key:
                        raise self.error("table has more than one primary key", tok)
                    primary_key = [column.name]
                columns.append(column)
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if not columns:
            raise self.error("a table needs at least one column")
        return n.CreateTable(name, columns, primary_key, if_not_exists)

    def column_def(self):
        column = n.ColumnDef(self.ident("a column name"), self.type_name())
        is_pk = False
        while True:
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                is_pk = True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                column.not_null = True
            elif self.accept_kw("NULL"):
                pass
            elif self.accept_kw("UNIQUE"):
                column.unique = True
            elif self.accept_kw("DEFAULT"):
                column.default = self.unary()
            else:
                return column, is_pk

    def type_name(self):
        tok = self.tok
        if tok.kind != WORD or tok.value.upper() not in TYPES:
            supported = "INTEGER, REAL, TEXT and BOOLEAN"
            raise self.error(f"expected a type ({supported}), got {self.describe()}")
        self.advance()
        if self.accept_op("("):  # VARCHAR(255) and similar: the size is accepted and ignored
            self.comma_list(self.expect_int)
            self.expect_op(")")
        return TYPES[tok.value.upper()]

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
        word = self.accept_kw("TABLE", "INDEX")
        if not word:
            raise self.error(f"expected TABLE or INDEX after DROP, got {self.describe()}")
        if_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            if_exists = True
        if word == "TABLE":
            return n.DropTable(self.ident("a table name"), if_exists)
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
        if self.accept_kw("LIMIT"):
            limit = self.expr()
        if self.accept_kw("OFFSET"):
            offset = self.expr()
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
        distinct = self.accept_kw("DISTINCT", "ALL") == "DISTINCT"
        columns = self.comma_list(self.select_item)
        from_ = self.from_clause() if self.accept_kw("FROM") else None
        where = self.expr() if self.accept_kw("WHERE") else None
        group_by = []
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            group_by = self.comma_list(self.expr)
        having = self.expr() if self.accept_kw("HAVING") else None
        if self.is_kw("WINDOW"):
            raise self.unsupported("named windows")
        return n.Select(columns, from_, where, group_by, having, distinct)

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
        if self.is_kw("LATERAL"):
            raise self.unsupported("LATERAL")
        if self.accept_op("("):
            if not self.starts_query():
                raise self.unsupported("parenthesized joins")
            query = self.query()
            self.expect_op(")")
            tok = self.tok
            alias = self.alias()
            if alias is None:
                raise self.error("a subquery in FROM needs an alias, e.g. (SELECT ...) AS sub", tok)
            return n.SubqueryRef(query, alias)
        pos = self.tok.pos
        return n.TableRef(self.ident("a table name"), self.alias(), pos)

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
            if op := self.accept_op(*_COMPARISONS):
                pos = self.prev_pos
                left = n.Binary("!=" if op == "<>" else op, left, self.concat(), pos)
            elif self.accept_kw("IS"):
                negated = bool(self.accept_kw("NOT"))
                self.expect_kw("NULL")
                left = n.IsNull(left, negated)
            else:
                negated = self.is_kw("NOT") and self.is_kw("IN", "BETWEEN", "LIKE", tok=self.peek())
                if negated:
                    self.advance()
                if self.accept_kw("IN"):
                    left = self.in_rest(left, negated)
                elif self.accept_kw("BETWEEN"):
                    low = self.concat()
                    self.expect_kw("AND")
                    left = n.Between(left, low, self.concat(), negated)
                elif self.accept_kw("LIKE"):
                    left = n.Like(left, self.concat(), negated)
                else:
                    return left

    def in_rest(self, left, negated):
        self.expect_op("(")
        if self.starts_query():
            query = self.query()
            self.expect_op(")")
            return n.InSubquery(left, query, negated)
        items = self.comma_list(self.expr)
        self.expect_op(")")
        return n.InList(left, items, negated)

    def concat(self):
        left = self.additive()
        while self.accept_op("||"):
            pos = self.prev_pos
            left = n.Binary("||", left, self.additive(), pos)
        return left

    def additive(self):
        left = self.multiplicative()
        while op := self.accept_op("+", "-"):
            pos = self.prev_pos
            left = n.Binary(op, left, self.multiplicative(), pos)
        return left

    def multiplicative(self):
        left = self.unary()
        while op := self.accept_op("*", "/", "%"):
            pos = self.prev_pos
            left = n.Binary(op, left, self.unary(), pos)
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
        return self.primary()

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
            self.expect_op(")")
            return expr
        word = self.accept_kw("NULL", "TRUE", "FALSE", "CASE", "CAST", "EXISTS")
        if word in ("NULL", "TRUE", "FALSE"):
            return n.Literal({"NULL": None, "TRUE": True, "FALSE": False}[word])
        if word == "CASE":
            return self.case()
        if word == "CAST":
            self.expect_op("(")
            expr = self.expr()
            self.expect_kw("AS")
            type_ = self.type_name()
            self.expect_op(")")
            return n.Cast(expr, type_)
        if word == "EXISTS":
            self.expect_op("(")
            query = self.query()
            self.expect_op(")")
            return n.Exists(query)
        if tok.kind == WORD and self.is_name() and self.peek().kind == OP and self.peek().value == "(":
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
        if self.accept_op("*"):
            call.star = True
        elif not self.is_op(")"):
            call.distinct = self.accept_kw("DISTINCT", "ALL") == "DISTINCT"
            call.args = self.comma_list(self.expr)
        self.expect_op(")")
        if self.accept_kw("OVER"):
            call.over = self.window_spec()
        return call

    def window_spec(self):
        if not self.is_op("("):
            raise self.unsupported("named windows")
        self.advance()
        spec = n.WindowSpec()
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
