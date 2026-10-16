"""Arcanum: a relational database built from scratch, with a live view of its internals."""

from .engine import Database, Result
from .errors import ArcanumError, ConstraintError, ExecutionError, SqlError

__version__ = "0.1.0"

__all__ = ["ArcanumError", "ConstraintError", "Database", "ExecutionError", "Result", "SqlError"]
