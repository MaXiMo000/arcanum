class ArcanumError(Exception):
    """Base class for every error Arcanum raises on purpose."""


class SqlError(ArcanumError):
    """A problem with the SQL itself: syntax, unknown names, type mismatches.

    When the position is known, the message starts with its line and column.
    """

    def __init__(self, message, sql=None, pos=None):
        self.message = message
        self.sql = sql
        self.pos = pos
        self.line = self.col = None
        if sql is not None and pos is not None:
            self.line = sql.count("\n", 0, pos) + 1
            self.col = pos - (sql.rfind("\n", 0, pos) + 1) + 1
            super().__init__(f"line {self.line}, col {self.col}: {message}")
        else:
            super().__init__(message)

    def excerpt(self):
        """The offending line with a caret under the error position, or "" if unknown."""
        if self.line is None:
            return ""
        start = self.sql.rfind("\n", 0, self.pos) + 1
        end = self.sql.find("\n", self.pos)
        line = self.sql[start:] if end < 0 else self.sql[start:end]
        return f"{line}\n{' ' * (self.col - 1)}^"


class ConstraintError(ArcanumError):
    """A write would break a NOT NULL, UNIQUE or PRIMARY KEY rule."""


class ExecutionError(ArcanumError):
    """A query failed while running, for example dividing by zero."""
