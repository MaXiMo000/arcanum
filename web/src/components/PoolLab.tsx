// Buffer pool replacement policies: one recorded page-access trace replayed through LRU, Clock and 2Q,
// with Belady's optimal policy as the ceiling no real policy can reach.
import { useState } from "react";
import { api, type PoolLabParams, type PoolLabResult, type PoolPolicy } from "../api";
import { formatCount } from "../format";
import { useLab } from "../useLab";
import { BarCompare } from "./BarCompare";
import { LineChart } from "./LineChart";

const DEFAULTS: PoolLabParams = { workload: "hot-scan", pool_pages: 32, rows: 4000, lookups: 1500 };
const POOL_SIZES = [4, 8, 16, 32, 64, 128, 256, 512];
const WORKLOADS: { value: PoolLabParams["workload"]; label: string }[] = [
  { value: "hot", label: "Hot keys" },
  { value: "hot-scan", label: "Hot keys + scans" },
  { value: "loop", label: "Repeated scan" },
  { value: "index", label: "Index lookups" },
];
const POLICIES: { key: PoolPolicy; label: string; className: string; dashed?: boolean }[] = [
  { key: "lru", label: "LRU", className: "eng-btree" },
  { key: "clock", label: "Clock", className: "eng-lsm" },
  { key: "2q", label: "2Q", className: "eng-third" },
  { key: "optimal", label: "Optimal", className: "eng-reference", dashed: true },
];
const REAL = POLICIES.filter((p) => p.key !== "optimal");

const percent = (rate: number) => `${(rate * 100).toFixed(rate > 0 && rate < 0.1 ? 1 : 0)}%`;

export function PoolLab({ sessionId }: { sessionId: string | null }) {
  const [params, setParams] = useState<PoolLabParams>(DEFAULTS);
  const { result, running, error, run } = useLab(sessionId, api.poolLab);
  const set = <K extends keyof PoolLabParams>(key: K, value: PoolLabParams[K]) => setParams((p) => ({ ...p, [key]: value }));

  return (
    <div className="engine-lab">
      <header className="lab-head">
        <h2>Buffer pool policies</h2>
        <p className="muted">
          The buffer pool keeps recently used pages in memory; when it is full, a replacement policy picks the page to
          drop. Run real queries, record every page the engine asks for, and replay that trace through each policy the
          pager can use. Every miss is a disk read.
        </p>
      </header>

      <section className="lab-controls" aria-label="Workload">
        <div className="lab-order lab-wide" role="radiogroup" aria-label="Workload">
          {WORKLOADS.map((w) => (
            <button key={w.value} role="radio" aria-checked={params.workload === w.value}
              className={params.workload === w.value ? "on" : ""} onClick={() => set("workload", w.value)}>
              {w.label}
            </button>
          ))}
        </div>
        <label>
          <span>
            Pool <b>{params.pool_pages} pages</b> ({params.pool_pages * 4} KB)
          </span>
          <input type="range" min={0} max={POOL_SIZES.length - 1} step={1} value={Math.max(0, POOL_SIZES.indexOf(params.pool_pages))}
            onChange={(e) => set("pool_pages", POOL_SIZES[Number(e.target.value)]!)} />
        </label>
        <label>
          <span>
            Rows <b>{formatCount(params.rows)}</b>
          </span>
          <input type="range" min={500} max={20000} step={500} value={params.rows} onChange={(e) => set("rows", Number(e.target.value))} />
        </label>
        <label>
          <span>
            Lookups <b>{formatCount(params.lookups)}</b>
          </span>
          <input type="range" min={100} max={5000} step={100} value={params.lookups} onChange={(e) => set("lookups", Number(e.target.value))} />
        </label>
        <button className="button primary" onClick={() => void run(params)} disabled={running || !sessionId}>
          {running ? (
            <>
              <span className="spinner" aria-hidden="true" /> Recording and replaying…
            </>
          ) : (
            "Run every policy"
          )}
        </button>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}
      {!result && !error && (
        <p className="muted lab-empty">{WORKLOADS.find((w) => w.value === params.workload)?.label}: pick a pool size and run it.</p>
      )}
      {result && <PoolReport result={result} />}
    </div>
  );
}

function PoolReport({ result }: { result: PoolLabResult }) {
  const { policies, sweep, workload } = result;
  const ranked = [...REAL].sort((a, b) => policies[b.key].hit_rate - policies[a.key].hit_rate);
  const best = ranked[0]!;
  const worst = ranked[ranked.length - 1]!;
  const optimal = policies.optimal;
  const saved = policies[worst.key].misses - policies[best.key].misses;

  return (
    <div className="lab-report">
      <p className="lab-takeaway">
        {workload.about}{" "}
        {saved > 0 ? (
          <>
            With {workload.pool_pages} of the {formatCount(workload.distinct_pages)} pages in memory, <b>{best.label}</b> hit{" "}
            {percent(policies[best.key].hit_rate)} of {formatCount(workload.accesses)} page requests and saved{" "}
            <b>{formatCount(saved)} disk reads</b> over {worst.label}.{" "}
          </>
        ) : (
          <>LRU, Clock and 2Q all hit {percent(policies[best.key].hit_rate)} of {formatCount(workload.accesses)} page requests. </>
        )}
        Knowing the future, the optimal policy would hit {percent(optimal.hit_rate)}
        {optimal.misses < policies[best.key].misses
          ? `, with ${formatCount(policies[best.key].misses - optimal.misses)} fewer reads than the best real policy.`
          : ": the best real policy matched it."}
      </p>

      <div className="amp-grid">
        <BarCompare
          title="Hit rate"
          hint="page requests served from memory"
          best="highest"
          max={1}
          bars={POLICIES.map((p) => ({
            key: p.key,
            label: p.label,
            className: p.className,
            value: policies[p.key].hit_rate,
            shown: percent(policies[p.key].hit_rate),
            note: `${formatCount(policies[p.key].misses)} disk reads`,
          }))}
        />
        <section className="amp-card" aria-label="The trace">
          <h3>The trace</h3>
          <p className="muted small">what the recorded workload asked of the pool</p>
          <dl className="lab-facts">
            <dt>Page requests</dt>
            <dd>{formatCount(workload.accesses)}</dd>
            <dt>Distinct pages</dt>
            <dd>{formatCount(workload.distinct_pages)}</dd>
            <dt>Table pages</dt>
            <dd>{formatCount(workload.table_pages)}</dd>
            <dt>Pool</dt>
            <dd>
              {workload.pool_pages} pages ({percent(Math.min(1, workload.pool_pages / Math.max(1, workload.distinct_pages)))} of the distinct pages)
            </dd>
          </dl>
        </section>
      </div>

      <LineChart
        title="Hit rate as the workload runs"
        series={POLICIES.map((p) => ({ ...p, points: policies[p.key].series }))}
        xLabel={`page requests (of ${formatCount(workload.accesses)})`}
        formatX={(v) => `${formatCount(v)} requests`}
        formatY={percent}
        yMax={1}
      >
        <p className="muted small">
          Over the most recent twentieth of the trace. A full scan shows as a dip: LRU and Clock let its pages push the hot
          ones out, while 2Q sends first-time pages through a small queue of their own.
        </p>
      </LineChart>

      <LineChart
        title="Hit rate against pool size"
        series={POLICIES.map((p) => ({ ...p, points: sweep.sizes.map((size, i) => [size, sweep.hit_rates[p.key][i] ?? 0]) }))}
        xLabel="pool size in pages (log scale)"
        formatX={(v) => `${formatCount(v)} pages`}
        formatY={percent}
        yMax={1}
        logX
        mark={workload.pool_pages}
      >
        <p className="muted small">
          The same trace at every size, up to one big enough for all {formatCount(workload.distinct_pages)} pages, where every
          policy meets: only the first use of each page misses. The upright line marks the pool you chose.
        </p>
      </LineChart>
    </div>
  );
}
