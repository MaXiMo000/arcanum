"""Error doctor: explains a failed statement in plain words and, when the intent
is clear, returns the whole script with the mistake corrected.

diagnose() returns {"hint", "fix", "fix_label", "pos", "end"}: fix is the
corrected SQL or None, and pos/end mark the characters at fault (or None).
It never raises: an error it does not recognise gets None.
"""

import logging
import re
from difflib import get_close_matches

from ..errors import ConstraintError, SqlError
from ..sql.lexer import EOF, OP, QUOTED, STRING, WORD, tokenize
from ..sql.parser import RESERVED, TYPES
from .expr import AGGREGATES, FUNCTIONS, WINDOW_ONLY

KEYWORDS = RESERVED | {
    "ANALYZE", "BEGIN", "BOOLEAN", "COMMIT", "CREATE", "DEFAULT", "DELETE", "DROP", "EXPLAIN", "IF", "INDEX",
    "INSERT", "INTEGER", "INTO", "KEY", "PARTITION", "PRIMARY", "RANGE", "REAL", "RECURSIVE", "ROLLBACK", "ROWS",
    "TABLE", "TEXT", "UNIQUE", "UPDATE"}
FUNCTION_NAMES = set(FUNCTIONS) | AGGREGATES | WINDOW_ONLY | {"COALESCE", "IFNULL", "NULLIF", "CAST"}
CUTOFF = 0.75  # how similar a name must be to count as a likely typo
SHORT_CUTOFF = 0.6  # names of four characters or fewer: one wrong letter is already a big share
MAX_LISTED = 12  # names listed in a hint when nothing is close
_CLAUSE_END = {"ORDER", "HAVING", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT", "WINDOW"}
_SMART_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "`": '"'})


def diagnose(error, catalog, sql):
    try:
        return _Doctor(error, catalog, sql).run()
    except Exception:
        # A hint is optional and the original error still stands; keep the fault in the log.
        logging.getLogger(__name__).warning("diagnosing %r failed", getattr(error, "message", error), exc_info=True)
        return None


class _Doctor:
    def __init__(self, error, catalog, sql):
        self.error, self.catalog, self.sql = error, catalog, sql
        self.message = getattr(error, "message", str(error))
        self.pos = getattr(error, "pos", None)
        try:
            self.tokens = tokenize(sql)
        except SqlError:
            self.tokens = None
        self.i = self.token_index(self.pos)
        self.tables = {name.lower(): table for name, table in catalog.tables.items()}
        self.columns = {c.name.lower() for t in catalog.tables.values() for c in t.columns}

    # ---- answers -------------------------------------------------------------------------

    def answer(self, hint, fix=None, label=None, span=None):
        pos, end = span if span else self.span(self.i)
        return {"hint": hint, "fix": fix if fix != self.sql else None, "fix_label": label if fix else None,
                "pos": pos, "end": end}

    def replace(self, index, text):
        start, end = self.span(index)
        return self.sql[:start] + text + self.sql[end:]

    def run(self):
        m = self.message
        if isinstance(self.error, ConstraintError):
            return self.constraint()
        for pattern, handler in (
            (r"table (\w+) does not exist", self.unknown_table),
            (r"column (?:(\w+)\.)?(\w+) does not exist(?: in table (\w+))?", self.unknown_column),
            (r"function (\w+) does not exist", self.unknown_function),
            (r"column (\w+) must appear in GROUP BY", self.not_grouped),
            (r"column reference (\w+) is ambiguous", self.ambiguous),
            (r"unterminated (string|quoted identifier)", self.unterminated),
            (r"unexpected character (.+)", self.bad_character),
            (r"expected a type \(", self.bad_type),
            (r"expected (.+), got (.+)", self.unexpected),
        ):
            match = re.match(pattern, m)
            if match:
                return handler(*match.groups())
        return self.plain(m)

    def plain(self, m):
        hints = (
            ("aggregate functions are not allowed in WHERE", "WHERE filters single rows before any grouping, "
             "so it cannot see count() or sum(). Filter on aggregates with HAVING, after GROUP BY."),
            ("cannot compare", "Text and numbers never compare equal. Quote text values ('5'), and compare "
             "number columns with numbers."),
            ("INSERT has", "The column list and each VALUES row must have the same length. " + self.insert_columns()),
            ("but the value is", "The value's type does not fit the column. Quote text, leave numbers bare, "
             "and use TRUE or FALSE for BOOLEAN."),
            ("missing FROM entry", "That table is not in FROM. If you gave it an alias (FROM orders o), "
             "refer to it by the alias."),
            ("there is no transaction", "COMMIT and ROLLBACK end a transaction; start one with BEGIN first."),
            ("already in progress", "Finish the open transaction with COMMIT or ROLLBACK before a new BEGIN."),
            ("requires an OVER clause", "Window functions run over a window: e.g. row_number() OVER (ORDER BY id)."),
            ("appears more than once in FROM", "Give each copy its own alias: FROM t a JOIN t b ON a.id = b.parent."),
            ("SELECT * needs a FROM", "* means every column of the FROM tables; add FROM table_name."),
        )
        for needle, hint in hints:
            if needle in m:
                return self.answer(hint)
        return None

    def constraint(self):
        m = self.message
        if "duplicate key" in m:
            return self.answer("A row with that primary key already exists. Choose an unused key (SELECT max(id) "
                               "FROM the table shows the largest), or change the existing row with UPDATE.")
        if "duplicate value" in m:
            return self.answer("A UNIQUE index already holds that value; each value may appear only once.")
        if "violates NOT NULL" in m:
            return self.answer("That column is NOT NULL: give it a value in the INSERT, or set a DEFAULT.")
        return None

    def unknown_table(self, name):
        match = _close(name, self.catalog.tables)
        if match:
            fix = self.replace(self.i, match) if self.is_word(self.i, name) else None
            return self.answer(f"There is no table {name}. Did you mean {match}?", fix, f"Use {match}")
        listed = ", ".join(sorted(self.catalog.tables)[:MAX_LISTED]) or "none yet"
        return self.answer(f"There is no table {name}. Tables in this database: {listed}.")

    def unknown_column(self, qualifier, name, table):
        index = self.find_word(name)
        if index is not None and self.tokens[index].kind == QUOTED:
            value = self.tokens[index].value.replace("'", "''")
            return self.answer(f'"{name}" in double quotes is a column name. For the text value, use single '
                               f"quotes: '{name}'.", self.replace(index, f"'{value}'"), "Use single quotes",
                               self.span(index))
        owner = self.tables.get((table or qualifier or "").lower())
        candidates = [c.name for c in owner.columns] if owner else self.columns_in_query()
        match = _close(name, candidates)
        span = self.span(index) if index is not None else (None, None)
        if match:
            fix = self.replace(index, match) if index is not None else None
            return self.answer(f"There is no column {name}. Did you mean {match}?", fix, f"Use {match}", span)
        where = f" in {owner.name}" if owner else ""
        listed = ", ".join(sorted(set(candidates))[:MAX_LISTED])
        return self.answer(f"There is no column {name}{where}." + (f" Columns: {listed}." if listed else ""), span=span)

    def unknown_function(self, name):
        match = _close(name.upper(), FUNCTION_NAMES)
        if not match:
            return self.answer(f"Arcanum has no function {name}. Available: "
                               f"{', '.join(sorted(f.lower() for f in FUNCTION_NAMES))}.")
        return self.answer(f"There is no function {name}. Did you mean {match.lower()}?",
                           self.replace(self.i, match.lower()) if self.is_word(self.i, name) else None,
                           f"Use {match.lower()}")

    def not_grouped(self, name):
        hint = (f"With GROUP BY or an aggregate, each output row stands for a group, so {name} must be "
                f"one of the grouping columns or inside an aggregate such as max({name}).")
        if self.tokens is None or sum(t.kind == OP and t.value == ";" for t in self.tokens[:-2]):
            return self.answer(hint)
        group = self.find_sequence("GROUP", "BY")
        if group is not None:
            end = self.clause_end(group + 2)
            at = self.span(end - 1)[1]
            return self.answer(hint, self.sql[:at] + f", {name}" + self.sql[at:], f"Add {name} to GROUP BY")
        at = self.span(self.clause_end(0) - 1)[1]
        return self.answer(hint, self.sql[:at] + f" GROUP BY {name}" + self.sql[at:], f"GROUP BY {name}")

    def ambiguous(self, name):
        owners = [t.name for t in self.catalog.tables.values()
                  if t.name.lower() in self.words() and any(c.name.lower() == name.lower() for c in t.columns)]
        listed = " or ".join(f"{t}.{name}" for t in owners) or f"table.{name}"
        return self.answer(f"More than one table in FROM has a column {name}. Say which one: {listed}.")

    def unterminated(self, what):
        if what == "string":
            return self.answer("A text value was opened with ' and never closed.", self.sql.rstrip() + "'",
                               "Close the quote", (self.pos, len(self.sql)))
        return self.answer('A name was opened with " and never closed. Text values use single quotes.',
                           span=(self.pos, len(self.sql)))

    def bad_character(self, char):
        fixed = self.sql.translate(_SMART_QUOTES)
        if fixed != self.sql:
            return self.answer("Curly quotes and backticks are not SQL quotes. Text uses straight single quotes "
                               "('text'); names use double quotes.", fixed, "Use straight quotes",
                               (self.pos, self.pos + 1))
        if char.strip("'") == "=":
            return self.answer("Comparison is a single = in SQL.", span=(self.pos, self.pos + 1))
        return self.answer(f"{char} is not part of SQL syntax here.", span=(self.pos, self.pos + 1))

    def bad_type(self):
        tok = self.token(self.i)
        match = tok and tok.kind == WORD and _close(tok.value.upper(), TYPES)
        if match:
            return self.answer(f"Did you mean {match}?", self.replace(self.i, match), f"Use {match}")
        return self.answer("Column types are INTEGER, REAL, TEXT and BOOLEAN (common synonyms like INT, "
                           "VARCHAR and FLOAT work too).")

    def unexpected(self, expected, got):
        tok, prev = self.token(self.i), self.token(self.i - 1)
        if tok is None:
            return None
        if prev is not None and prev.kind == OP and prev.value == "," and (tok.kind != WORD or tok.value.upper() in KEYWORDS):
            return self.answer("There is a comma with nothing after it.", self.replace(self.i - 1, ""),
                               "Remove the comma", self.span(self.i - 1))
        if prev is not None and prev.kind == OP and prev.value == "=" and tok.kind == OP and tok.value == "=":
            return self.answer("Equality is a single = in SQL (not ==).", self.replace(self.i, ""), "Use =")
        for index in (self.i, self.i - 1):
            keyword = self.misspelled_keyword(index)
            if keyword:
                word = self.tokens[index].value
                return self.answer(f"{word} is not a keyword. Did you mean {keyword}?", self.replace(index, keyword),
                                   f"Use {keyword}", self.span(index))
        if tok.kind == WORD and tok.value.lower() in self.tables and (prev is None or prev.value.upper() not in KEYWORDS):
            at = self.span(self.i)[0]
            return self.answer(f"{tok.value} is a table: did you forget FROM before it?",
                               self.sql[:at] + "FROM " + self.sql[at:], "Add FROM")
        run = self.column_run(self.i)
        if len(run) > 1:
            fixed = self.sql
            for index in reversed(run[:-1]):
                at = self.span(index)[1]
                fixed = fixed[:at] + "," + fixed[at:]
            return self.answer("Select-list items are separated by commas. Without one, the second name is "
                               "read as an alias of the first.", fixed, "Add the commas",
                               (self.span(run[0])[0], self.span(run[-1])[1]))
        if expected == "')'" and tok.kind != EOF:
            at = self.span(self.i - 1)[1]
            return self.answer("A parenthesis is still open here.", self.sql[:at] + ")" + self.sql[at:],
                               "Close the parenthesis")
        if tok.kind == EOF:
            depth = sum(t.value == "(" for t in self.tokens if t.kind == OP) - sum(
                t.value == ")" for t in self.tokens if t.kind == OP)
            if depth > 0:
                return self.answer(f"{depth} parenthesis{'es' if depth > 1 else ''} never closed.",
                                   self.sql.rstrip() + ")" * depth, "Close the parenthesis")
            last = prev.value if prev is not None else ""
            return self.answer(f"The statement ends too early: after {last!r} it needs {expected}.")
        return None

    # ---- tokens --------------------------------------------------------------------------

    def token_index(self, pos):
        if self.tokens is None or pos is None:
            return None
        for i, tok in enumerate(self.tokens):
            if tok.pos >= pos:
                return i
        return len(self.tokens) - 1

    def token(self, index):
        if self.tokens is None or index is None or not 0 <= index < len(self.tokens):
            return None
        return self.tokens[index]

    def span(self, index):
        tok = self.token(index)
        if tok is None:
            return (self.pos, None) if self.pos is not None else (None, None)
        if tok.kind in (QUOTED, STRING):
            quote, end = self.sql[tok.pos], tok.pos + 1
            while True:
                end = self.sql.find(quote, end) + 1
                if not self.sql.startswith(quote, end):
                    return tok.pos, end
                end += 1
        return tok.pos, tok.pos + len(tok.value)

    def is_word(self, index, name):
        tok = self.token(index)
        return tok is not None and tok.kind in (WORD, QUOTED) and tok.value.lower() == name.lower()

    def find_word(self, name):
        """The token naming `name`, preferring the error position and what follows it."""
        if self.tokens is None:
            return None
        start = self.i or 0
        order = list(range(start, len(self.tokens))) + list(range(start))
        return next((i for i in order if self.is_word(i, name)), None)

    def find_sequence(self, *words):
        for i in range(len(self.tokens) - len(words)):
            if all(self.tokens[i + j].kind == WORD and self.tokens[i + j].value.upper() == w for j, w in enumerate(words)):
                return i
        return None

    def clause_end(self, start):
        """Index of the first token after `start` that ends the current clause at nesting depth 0."""
        depth = 0
        for i in range(start, len(self.tokens)):
            tok = self.tokens[i]
            if tok.kind == OP and tok.value == "(":
                depth += 1
            elif tok.kind == OP and tok.value == ")":
                if depth == 0:
                    return i
                depth -= 1
            elif depth == 0 and (tok.kind == EOF or (tok.kind == OP and tok.value == ";")
                                 or (tok.kind == WORD and tok.value.upper() in _CLAUSE_END)):
                return i
        return len(self.tokens) - 1

    def words(self):
        return {t.value.lower() for t in self.tokens or () if t.kind in (WORD, QUOTED)}

    def columns_in_query(self):
        named = [t for name, t in self.tables.items() if name in self.words()]
        return [c.name for t in (named or self.catalog.tables.values()) for c in t.columns]

    def misspelled_keyword(self, index):
        tok = self.token(index)
        if tok is None or tok.kind != WORD or len(tok.value) < 3:
            return None
        word = tok.value.lower()
        if tok.value.upper() in KEYWORDS or word in self.tables or word in self.columns:
            return None
        return _close(tok.value.upper(), KEYWORDS, strict=True)

    def column_run(self, index):
        """Indexes of consecutive bare column names ending at index (a select list missing its commas)."""
        run = []
        while (tok := self.token(index)) is not None and tok.kind == WORD and tok.value.lower() in self.columns:
            run.insert(0, index)
            index -= 1
        return run

    def insert_columns(self):
        m = re.search(r"(?i)INSERT\s+INTO\s+(\w+)", self.sql)
        table = self.tables.get(m.group(1).lower()) if m else None
        return f"{table.name} has: {', '.join(c.name for c in table.columns)}." if table else ""


def _close(name, candidates, strict=False):
    cutoff = CUTOFF if strict or len(name) > 4 else SHORT_CUTOFF
    found = get_close_matches(name.lower(), {c.lower(): c for c in candidates}, n=1, cutoff=cutoff)
    if not found:
        return None
    return next(c for c in candidates if c.lower() == found[0])
