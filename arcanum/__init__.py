"""Arcanum: a relational database built from scratch, with a live view of its internals."""

from .engine import Database, QueryLimits, Result
from .errors import (
    ArcanumError,
    ConstraintError,
    DeadlockError,
    ExecutionError,
    LockTimeoutError,
    LockWaitError,
    SerializationError,
    SqlError,
    StatementTimeoutError,
)

__version__ = "0.1.0"

__all__ = ["ArcanumError", "ConstraintError", "Database", "DeadlockError", "ExecutionError", "LockTimeoutError",
           "LockWaitError", "QueryLimits", "Result", "SerializationError", "SqlError",
           "StatementTimeoutError"]
