"""Random SQL for the crash fuzzer: well-formed statements over every feature, and mangled ones.

Gen(rng).select() and Gen(rng).write() return one statement each; mutate() drops, duplicates or swaps
words to reach the parser's and binder's error paths. SETUP creates the schema they use.
"""

SETUP = """
CREATE TABLE c (id SERIAL PRIMARY KEY, name TEXT NOT NULL, city TEXT, born DATE, score NUMERIC(6,2) CHECK (score >= 0),
                tags TEXT[], doc JSONB, active BOOLEAN DEFAULT true, seen TIMESTAMP DEFAULT now());
CREATE TABLE o (id INTEGER PRIMARY KEY, cid INTEGER REFERENCES c ON DELETE CASCADE, amount REAL, qty INTEGER,
                note TEXT, UNIQUE (cid, note));
CREATE INDEX o_cid ON o (cid);
CREATE INDEX c_city ON c (city);
INSERT INTO c (name, city, born, score, tags, doc) VALUES
 ('ann', 'oslo', '1990-01-02', 10.5, '{a,b}', '{"k": 1, "l": [1, 2]}'),
 ('bo', NULL, NULL, NULL, NULL, NULL),
 ('cy', 'lima', '2001-12-31', 0, '{}', '[1, "x", null]'),
 ('di', 'oslo', '1985-06-15', 99.99, '{b,c,NULL}', '{"k": {"m": true}}');
INSERT INTO o VALUES (1, 1, 10.5, 2, 'x'), (2, 1, NULL, NULL, NULL), (3, 3, -1, 5, 'y'), (4, 4, 1e10, 0, 'z');
CREATE VIEW v AS SELECT c.name, count(o.id) AS n, sum(o.amount) AS total FROM c LEFT JOIN o ON o.cid = c.id GROUP BY c.name;
"""

COLS = {"c": ["id", "name", "city", "born", "score", "tags", "doc", "active", "seen"],
        "o": ["id", "cid", "amount", "qty", "note"], "v": ["name", "n", "total"]}
LITS = ["1", "0", "-1", "2.5", "NULL", "'x'", "''", "'oslo'", "true", "false", "DATE '2020-02-29'", "'2020-01-01'",
        "INTERVAL '1 day'", "'{1,2}'", "ARRAY[1,2]", "'{\"k\": 1}'::jsonb", "9223372036854775807", "1e308",
        "'1 month'", "now()", "CURRENT_DATE", "ARRAY['a']", "'[1]'", "0.1::numeric", "(SELECT 1)",
        "(SELECT max(id) FROM o)", "'a''b'"]
FUNCS = ["abs", "round", "length", "lower", "upper", "coalesce", "nullif", "greatest", "least", "concat",
         "date_trunc", "date_part", "to_char", "jsonb_typeof", "array_length", "cardinality", "to_jsonb",
         "string_to_array", "array_to_string", "split_part", "lpad", "repeat", "regexp_replace", "age",
         "jsonb_build_object", "jsonb_array_length", "trunc", "ceil", "mod", "sqrt", "left", "right",
         "array_append", "array_position", "strpos", "initcap", "make_date", "to_date", "jsonb_set"]
AGGS = ["count", "sum", "avg", "min", "max", "string_agg", "array_agg", "jsonb_agg", "bool_and", "bool_or"]
OPS = ["+", "-", "*", "/", "%", "||", "=", "<>", "<", ">=", "->", "->>", "@>", "&&", "?", "~", "LIKE", "ILIKE",
       "#>", "AND", "OR"]


class Gen:
    def __init__(self, rng):
        self.r = rng

    def col(self, t):
        return f"{t}.{self.r.choice(COLS[t])}" if self.r.random() < 0.5 else self.r.choice(COLS[t])

    def expr(self, t, depth=0):
        r = self.r
        k = r.random()
        if depth > 3 or k < 0.3:
            return r.choice(LITS) if r.random() < 0.4 else self.col(t)
        if k < 0.5:
            return f"({self.expr(t, depth + 1)} {r.choice(OPS)} {self.expr(t, depth + 1)})"
        if k < 0.65:
            n = r.randint(0, 3)
            return f"{r.choice(FUNCS)}({', '.join(self.expr(t, depth + 1) for _ in range(n))})"
        if k < 0.7:
            return f"CASE WHEN {self.expr(t, depth + 1)} THEN {self.expr(t, depth + 1)} ELSE {self.expr(t, depth + 1)} END"
        if k < 0.75:
            return f"CAST({self.expr(t, depth + 1)} AS {r.choice(['INTEGER', 'TEXT', 'NUMERIC(5,1)', 'DATE', 'JSONB', 'INTEGER[]', 'BOOLEAN', 'REAL', 'TIMESTAMP', 'INTERVAL'])})"
        if k < 0.8:
            return f"{self.expr(t, depth + 1)} {r.choice(['IS NULL', 'IS NOT NULL', 'IN (1, 2, NULL)', 'BETWEEN 1 AND 3', '= ANY(ARRAY[1,2])', '[1]'])}"
        if k < 0.85:
            return f"EXTRACT({r.choice(['year', 'dow', 'epoch', 'nope'])} FROM {self.expr(t, depth + 1)})"
        if k < 0.9:
            return f"EXISTS (SELECT 1 FROM o WHERE o.cid = {self.col(t)})"
        return f"{self.expr(t, depth + 1)}::{r.choice(['text', 'int', 'jsonb', 'date', 'numeric', 'text[]'])}"

    def select(self):
        r = self.r
        t = r.choice(["c", "o", "v"])
        items = [self.expr(t) for _ in range(r.randint(1, 3))]
        if r.random() < 0.3:
            delimiter = ", ','" if r.random() < 0.3 else ""
            order = " ORDER BY " + self.col(t) if r.random() < 0.3 else ""
            keep = " FILTER (WHERE " + self.expr(t) + ")" if r.random() < 0.2 else ""
            items.append(f"{r.choice(AGGS)}({self.expr(t)}{delimiter}{order}){keep}")
        if r.random() < 0.15:
            items.append(f"row_number() OVER (PARTITION BY {self.col(t)} ORDER BY {self.col(t)})")
        sql = f"SELECT {'DISTINCT ' if r.random() < 0.1 else ''}{', '.join(items)} FROM {t}"
        if r.random() < 0.2:
            sql += r.choice([" JOIN o ON o.cid = c.id" if t == "c" else "", ", LATERAL unnest(ARRAY[1,2]) AS u(x)",
                             " CROSS JOIN generate_series(1, 3) g", " LEFT JOIN LATERAL (SELECT 1 AS one) s ON true"])
        if r.random() < 0.6:
            sql += f" WHERE {self.expr(t)}"
        if r.random() < 0.3:
            g = self.col(t)
            sql += r.choice([f" GROUP BY {g}", f" GROUP BY ROLLUP ({g})", f" GROUP BY CUBE ({g}, {self.col(t)})",
                             f" GROUP BY GROUPING SETS (({g}), ())"])
            if r.random() < 0.3:
                sql += f" HAVING count(*) > {r.randint(0, 2)}"
        if r.random() < 0.4:
            sql += f" ORDER BY {r.choice(['1', self.col(t)])}{r.choice(['', ' DESC', ' NULLS FIRST', ' DESC NULLS LAST'])}"
        if r.random() < 0.3:
            sql += f" LIMIT {r.randint(0, 5)}{' OFFSET 1' if r.random() < 0.3 else ''}"
        if r.random() < 0.1:
            sql = r.choice(["SELECT DISTINCT ON (1) * FROM (", "WITH w AS (", "SELECT * FROM ("]) + sql + (
                ") x" if not sql.startswith("WITH") else "")
            if sql.startswith("WITH"):
                sql += ") SELECT * FROM w"
        return sql

    def write(self):
        r = self.r
        k = r.random()
        if k < 0.3:
            n = r.randint(1, 3)
            vals = ", ".join(f"({', '.join(self.r.choice(LITS) for _ in range(2))})" for _ in range(n))
            tail = r.choice(["", " ON CONFLICT DO NOTHING", " ON CONFLICT (id) DO UPDATE SET amount = excluded.amount + 1",
                             " RETURNING *", " RETURNING id, amount * 2"])
            return f"INSERT INTO o (id, cid) VALUES {vals}{tail}"
        if k < 0.45:
            return f"INSERT INTO c (name, city, score, tags, doc) VALUES ({r.choice(LITS)}, {r.choice(LITS)}, {r.choice(LITS)}, {r.choice(LITS)}, {r.choice(LITS)}){r.choice(['', ' RETURNING *'])}"
        if k < 0.6:
            t = r.choice(["c", "o"])
            col = r.choice(COLS[t][1:])
            frm = " FROM c" if t == "o" and r.random() < 0.3 else ""
            return f"UPDATE {t} SET {col} = {self.expr(t)}{frm} WHERE {self.expr(t)}{r.choice(['', ' RETURNING *'])}"
        if k < 0.7:
            t = r.choice(["c", "o"])
            return f"DELETE FROM {t} WHERE {self.expr(t)}{r.choice(['', ' RETURNING id'])}"
        if k < 0.8:
            return r.choice(["BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT s", "ROLLBACK TO s", "RELEASE s",
                             "BEGIN ISOLATION LEVEL SERIALIZABLE", "BEGIN ISOLATION LEVEL REPEATABLE READ"])
        if k < 0.9:
            return r.choice([
                "ALTER TABLE o ADD COLUMN extra TEXT DEFAULT 'q'", "ALTER TABLE o DROP COLUMN extra",
                "ALTER TABLE o RENAME COLUMN note TO memo", "ALTER TABLE o RENAME COLUMN memo TO note",
                "ALTER TABLE o ALTER COLUMN qty TYPE NUMERIC(8,1)", "ALTER TABLE o ALTER COLUMN qty TYPE INTEGER",
                "ALTER TABLE o ADD CONSTRAINT pos CHECK (qty >= 0)", "ALTER TABLE o DROP CONSTRAINT pos",
                "ALTER TABLE c ALTER COLUMN city SET NOT NULL", "ALTER TABLE c ALTER COLUMN city DROP NOT NULL",
                "CREATE INDEX IF NOT EXISTS o_note ON o (note)", "DROP INDEX IF EXISTS o_note",
                "CREATE OR REPLACE VIEW w2 AS SELECT * FROM o", "DROP VIEW IF EXISTS w2", "ANALYZE",
                "ALTER TABLE o RENAME TO o2", "ALTER TABLE o2 RENAME TO o", "DROP TABLE o",
                f"INSERT INTO c (name, doc) VALUES ('big', '{{\"t\": \"{'x' * r.randint(100, 9000)}\"}}')",
                f"UPDATE c SET name = repeat('y', {r.randint(1, 20000)}) WHERE id = {r.randint(1, 5)}",
            ])
        return "EXPLAIN " + self.select()


def mutate(rng, sql):
    toks = sql.split(" ")
    for _ in range(rng.randint(1, 3)):
        if not toks:
            break
        i = rng.randrange(len(toks))
        op = rng.random()
        if op < 0.4:
            del toks[i]
        elif op < 0.7:
            toks.insert(i, toks[rng.randrange(len(toks))])
        else:
            j = rng.randrange(len(toks))
            toks[i], toks[j] = toks[j], toks[i]
    return " ".join(toks)
