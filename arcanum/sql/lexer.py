"""SQL tokenizer: text -> list of tokens.

Words are not classified as keywords here. The parser decides from context,
so a keyword that only matters in one place (KEY, ROWS, FIRST) can still be
used as a table or column name.
"""

import re
from dataclasses import dataclass

from ..errors import SqlError

WORD, QUOTED, INT, FLOAT, STRING, OP, EOF = "word", "quoted", "int", "float", "string", "op", "eof"

# Longest first, so "<=" wins over "<".
_OPS = ("<=", ">=", "<>", "!=", "||", "=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ",", ".", ";")
_NUMBER = re.compile(r"(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    pos: int  # offset into the SQL text


def tokenize(sql: str) -> list[Token]:
    tokens, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise SqlError("unterminated comment", sql, i)
            i = end + 2
        elif c.isalpha() or c == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            tokens.append(Token(WORD, sql[start:i], start))
        elif "0" <= c <= "9" or (c == "." and "0" <= sql[i + 1:i + 2] <= "9"):
            m = _NUMBER.match(sql, i)
            end = m.end()
            if end < n and (sql[end].isalnum() or sql[end] == "_"):
                raise SqlError(f"invalid number {sql[i:end + 1]!r}", sql, i)
            text = m.group()
            tokens.append(Token(FLOAT if any(ch in text for ch in ".eE") else INT, text, i))
            i = end
        elif c in "'\"":
            value, end = _quoted(sql, i)
            tokens.append(Token(STRING if c == "'" else QUOTED, value, i))
            i = end
        else:
            op = next((op for op in _OPS if sql.startswith(op, i)), None)
            if op is None:
                raise SqlError(f"unexpected character {c!r}", sql, i)
            tokens.append(Token(OP, op, i))
            i += len(op)
    tokens.append(Token(EOF, "", n))
    return tokens


def _quoted(sql, start):
    """Read a '...' string or "..." identifier; a doubled quote stands for itself."""
    quote, i, parts = sql[start], start + 1, []
    while True:
        end = sql.find(quote, i)
        if end < 0:
            what = "string" if quote == "'" else "quoted identifier"
            raise SqlError(f"unterminated {what}", sql, start)
        parts.append(sql[i:end])
        if sql.startswith(quote, end + 1):
            parts.append(quote)
            i = end + 2
        else:
            return "".join(parts), end + 1
