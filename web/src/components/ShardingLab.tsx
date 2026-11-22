// Sharding: one table split across several databases three ways, each question routed to the shards that can
// answer it, and what that costs.
import { useState } from "react";
import { api, type ShardingResult } from "../api";
import { formatCount } from "../format";
import { useLab } from "../useLab";
import { BarCompare } from "./BarCompare";

const CLASS: Record<string, string> = { "hash-id": "eng-btree", "range-id": "eng-lsm", "hash-customer": "eng-third" };

const WHY: Record<string, string> = {
  point:
    "Sharded by id, the router computes the one shard holding it. Sharded by customer, the id says nothing about where the order is: ask every shard.",
  customer:
    "Only hashing customers keeps a customer's orders together. Its one shard does all the work, so the busiest shard reads more, but the total is lowest and the other shards stay free.",
  range:
    "Ranges keep neighbouring ids on one shard (two at a boundary). Hashing scatters them, so every shard is asked and each reads a little.",
  total:
    "A question about everything asks every shard whatever the key. Each sums its part and the router adds the parts: the work is split, so the busiest shard reads a fraction.",
};

export function ShardingLab({ sessionId }: { sessionId: string | null }) {
  const [shards, setShards] = useState(4);
  const [rows, setRows] = useState(4000);
  const { result, running, error, run } = useLab(sessionId, api.shardingLab);

  return (
    <div className="engine-lab">
      <header className="lab-head">
        <h2>Sharding</h2>
        <p className="muted">
          One table of orders split across several databases, three ways: by a hash of the order id, by ranges of ids,
          and by a hash of the customer. A router sends each question only to the shards that can hold the answer. Which
          questions stay on one shard depends entirely on the shard key.
        </p>
      </header>

      <section className="lab-controls" aria-label="Cluster">
        <label>
          <span>
            Shards <b>{shards}</b>
          </span>
          <input type="range" min={2} max={8} step={1} value={shards} onChange={(e) => setShards(Number(e.target.value))} />
        </label>
        <label>
          <span>
            Orders <b>{formatCount(rows)}</b>
          </span>
          <input type="range" min={1000} max={10000} step={1000} value={rows} onChange={(e) => setRows(Number(e.target.value))} />
        </label>
        <button className="button primary" onClick={() => void run({ shards, rows })} disabled={running || !sessionId}>
          {running ? (
            <>
              <span className="spinner" aria-hidden="true" /> Sharding {formatCount(rows)} orders three ways…
            </>
          ) : (
            "Shard and ask"
          )}
        </button>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}
      {!result && !error && <p className="muted lab-empty">Pick a cluster size and run it.</p>}
      {result && <ShardingReport result={result} />}
    </div>
  );
}

function ShardBars({ counts, className }: { counts: number[]; className: string }) {
  const top = Math.max(...counts, 1);
  return (
    <span className="shard-bars" role="img" aria-label={`per shard: ${counts.map(formatCount).join(", ")}`}>
      {counts.map((c, i) => (
        <i key={i} className={className} style={{ height: `${Math.max(3, (c / top) * 100)}%` }} title={`shard ${i + 1}: ${formatCount(c)}`} />
      ))}
    </span>
  );
}

function ShardingReport({ result }: { result: ShardingResult }) {
  const first = result.strategies[0]!.key;
  const newOrders = result.inserts[first]!.reduce((a, b) => a + b, 0);
  return (
    <div className="lab-report">
      <div className="amp-grid">
        {result.questions.map((q) => (
          <div key={q.key} className="access-card">
            <BarCompare
              title={q.title}
              hint="Shards asked per question (fewer is better)."
              best="lowest"
              bars={result.strategies.map((s) => ({
                key: s.key,
                label: s.label,
                className: CLASS[s.key]!,
                value: q.per[s.key]!.shards,
                note: `${formatCount(q.per[s.key]!.pages)} pages in all, ${formatCount(q.per[s.key]!.busiest)} on the busiest shard`,
              }))}
            />
            <p className="muted small access-why">{WHY[q.key]}</p>
          </div>
        ))}
      </div>
      <section className="lab-chart" aria-label="Where the rows are">
        <h3>Where the rows are, and where new orders land</h3>
        <table className="shard-table">
          <thead>
            <tr>
              <th scope="col">Shard key</th>
              <th scope="col">Rows per shard</th>
              <th scope="col">{formatCount(newOrders)} new orders</th>
            </tr>
          </thead>
          <tbody>
            {result.strategies.map((s) => (
              <tr key={s.key}>
                <th scope="row">
                  <b>{s.label}</b>
                  <span className="muted small"> {s.about}</span>
                </th>
                <td>
                  <ShardBars counts={result.balance[s.key]!} className={CLASS[s.key]!} />
                </td>
                <td>
                  <ShardBars counts={result.inserts[s.key]!} className={CLASS[s.key]!} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          Every key spreads the existing rows evenly. New orders get ever larger ids, so with ranges they all land on the
          last shard: a hot spot that takes every write while the others idle. Hashing spreads them.
        </p>
      </section>
    </div>
  );
}
