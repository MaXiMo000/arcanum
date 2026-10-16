class SqlError(Exception):
    """A problem in the SQL text, reported with its line and column."""

    def __init__(self, message, sql, pos):
        self.message = message
        self.sql = sql
        self.pos = pos
        self.line = sql.count("\n", 0, pos) + 1
        self.col = pos - (sql.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"line {self.line}, col {self.col}: {message}")

    def excerpt(self):
        """The offending line with a caret under the error position."""
        start = self.sql.rfind("\n", 0, self.pos) + 1
        end = self.sql.find("\n", self.pos)
        line = self.sql[start:] if end < 0 else self.sql[start:end]
        return f"{line}\n{' ' * (self.col - 1)}^"
