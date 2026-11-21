"""Crash fuzzing: any statement, valid or mangled, either works or fails with an ArcanumError.

An exception of any other type (TypeError, KeyError, RecursionError...) would reach a user as an internal
error, so it is a bug, whatever the SQL. Each seed is deterministic; a failure prints the statement.
"""

import random

import pytest
from sqlgen import SETUP, Gen, mutate

from arcanum import Database
from arcanum.engine.executor import QueryLimits
from arcanum.errors import ArcanumError

STATEMENTS = 1500


@pytest.mark.parametrize("seed", range(3))
def test_no_statement_crashes_the_engine(tmp_path, seed):
    rng = random.Random(seed)
    gen = Gen(rng)
    with Database(tmp_path / "f.db", max_keys=4,
                  limits=QueryLimits(memory_rows=20_000, recursive_rows=2_000, timeout=10)) as db:
        db.execute_script(SETUP)
        sessions = [db, db.connect("b")]
        for _ in range(STATEMENTS):
            sql = gen.select() if rng.random() < 0.55 else gen.write()
            if rng.random() < 0.25:
                sql = mutate(rng, sql)
            try:
                rng.choice(sessions).execute(sql)
            except ArcanumError:
                pass
            except Exception as e:
                pytest.fail(f"{type(e).__name__}: {e} in: {sql}")
