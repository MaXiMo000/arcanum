// B+tree versus LSM tree: the same workload on both engines, compared by the I/O each one does, with the
// LSM tree's levels replayed flush by flush and compaction by compaction.
import { useEffect, useRef, useState } from "react";
import { api, type LabParams, type LabResult, type LsmMoment, type LsmShape } from "../api";
import { formatCount } from "../format";
import { LabTour } from "./LabTour";
import { useLab } from "../useLab";
import { BarCompare, type Bar } from "./BarCompare";
import { LineChart, type Series } from "./LineChart";

const DEFAULTS: LabParams = { keys: 5000, order: "random", updates: 0.5, value_bytes: 64, memory_pages: 16 };
const PLAY_MS = 450;

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(n < 10 * 1024 * 1024 ? 2 : 1)} MB`;
}

const times = (a: number, b: number) => (Math.max(a, b) / Math.max(Math.min(a, b), 1e-9)).toFixed(1);

const TOUR: { title: string; body: string; params: Partial<LabParams> }[] = [
  { title: "Random keys", params: { order: "random", updates: 0.5 },
    body: "Writes land all over the key space. The B+tree logs a whole page for almost every one; the LSM tree only appends, so it writes several times fewer bytes." },
  { title: "Sequential keys", params: { order: "sequential", updates: 0 },
    body: "Keys arrive in order and fill one page at a time, so the B+tree's page writes are cheap and its advantage on reads shows: one tree, one descent." },
  { title: "Overwrite everything", params: { order: "random", updates: 1 },
    body: "Every key is written twice. Old versions pile up in the LSM tree's levels until compaction drops them: watch space amplification and the compactions." },
  { title: "More memory", params: { order: "random", updates: 0.5, memory_pages: 64 },
    body: "A bigger memtable means fewer, larger flushes, and a bigger buffer pool means fewer B+tree page writes. Compare with the first step." },
];

export function EngineLab({ sessionId }: { sessionId: string | null }) {
  const [params, setParams] = useState<LabParams>(DEFAULTS);
  const { result, running, error, run } = useLab(sessionId, api.lab);
  const set = <K extends keyof LabParams>(key: K, value: LabParams[K]) => setParams((p) => ({ ...p, [key]: value }));

  const operations = params.keys + Math.floor(params.keys * params.updates);
  return (
    <div className="engine-lab">
      <header className="lab-head">
        <h2>B+tree vs LSM tree</h2>
        <p className="muted">
          Two ways to store sorted keys. A B+tree changes pages in place; an LSM tree (RocksDB, Cassandra) only appends and
          merges files later. Run one workload on both, on equal memory, and count every byte each one reads and writes.
        </p>
      </header>

      <LabTour busy={running || !sessionId} steps={TOUR.map(({ title, body, params: changes }) => ({
        title,
        body,
        run: () => {
          const next = { ...DEFAULTS, ...changes };
          setParams(next);
          return run(next);
        },
      }))} />

      <section className="lab-controls" aria-label="Workload">
        <label>
          <span>
            Keys <b>{formatCount(params.keys)}</b>
          </span>
          <input type="range" min={500} max={10000} step={500} value={params.keys} onChange={(e) => set("keys", Number(e.target.value))} />
        </label>
        <label>
          <span>
            Overwrites <b>{Math.round(params.updates * 100)}%</b>
          </span>
          <input type="range" min={0} max={1} step={0.1} value={params.updates} onChange={(e) => set("updates", Number(e.target.value))} />
        </label>
        <label>
          <span>
            Value size <b>{params.value_bytes} B</b>
          </span>
          <input type="range" min={16} max={256} step={16} value={params.value_bytes} onChange={(e) => set("value_bytes", Number(e.target.value))} />
        </label>
        <label>
          <span>
            Memory <b>{params.memory_pages} pages</b> ({params.memory_pages * 4} KB)
          </span>
          <input type="range" min={8} max={64} step={8} value={params.memory_pages} onChange={(e) => set("memory_pages", Number(e.target.value))} />
        </label>
        <div className="lab-order" role="radiogroup" aria-label="Key order">
          {(["random", "sequential"] as const).map((order) => (
            <button key={order} role="radio" aria-checked={params.order === order} className={params.order === order ? "on" : ""} onClick={() => set("order", order)}>
              {order === "random" ? "Random keys" : "Sequential keys"}
            </button>
          ))}
        </div>
        <button className="button primary" onClick={() => void run(params)} disabled={running || !sessionId}>
          {running ? (
            <>
              <span className="spinner" aria-hidden="true" /> Running {formatCount(operations)} writes on both…
            </>
          ) : (
            "Run both engines"
          )}
        </button>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}
      {!result && !error && (
        <p className="muted lab-empty">
          Each engine gets {formatCount(operations)} writes in commits of 64, then 1,000 lookups and 20 range scans. Pick a
          workload and run it.
        </p>
      )}
      {result && <LabReport result={result} />}
    </div>
  );
}

function LabReport({ result }: { result: LabResult }) {
  const { btree, lsm, workload } = result;
  return (
    <div className="lab-report">
      <p className="lab-takeaway">
        {lsm.write_amp < btree.write_amp ? (
          <>
            The LSM tree wrote <b>{times(btree.write_amp, lsm.write_amp)}× fewer bytes</b>.{" "}
          </>
        ) : (
          <>
            The B+tree wrote <b>{times(btree.write_amp, lsm.write_amp)}× fewer bytes</b>.{" "}
          </>
        )}
        Every B+tree commit logs each page it changed in full, so {workload.order} keys
        {workload.order === "random" ? " that land on different pages cost a whole page each" : " that fill one page at a time stay cheap"}.
        The LSM tree logs only the entries, then rewrites them during compaction ({lsm.compactions} compactions).{" "}
        {Math.abs(btree.lookup_reads - lsm.lookup_reads) <= 0.25 * Math.max(btree.lookup_reads, lsm.lookup_reads, 0.1)
          ? "Lookups cost about the same"
          : lsm.lookup_reads > btree.lookup_reads
            ? `Lookups cost the LSM tree ${times(lsm.lookup_reads, btree.lookup_reads)}× more reads, since a key may sit in any level`
            : `Lookups cost the B+tree ${times(btree.lookup_reads, lsm.lookup_reads)}× more reads`}
        : Bloom filters let the LSM tree skip {formatCount(lsm.bloom_skips)} files without reading them.
      </p>

      <div className="amp-grid">
        <Amp
          title="Write amplification"
          unit="×"
          hint="bytes written to storage per byte of data written"
          a={btree.write_amp}
          b={lsm.write_amp}
          notes={[
            `${bytes(btree.log_bytes)} log + ${bytes(btree.data_bytes)} data, ${btree.checkpoints} checkpoints`,
            `${bytes(lsm.log_bytes)} log + ${bytes(lsm.data_bytes)} files, ${lsm.flushes} flushes`,
          ]}
        />
        <Amp
          title="Read amplification"
          unit=" blocks"
          hint="disk reads per point lookup (a fifth of them for missing keys)"
          a={btree.lookup_reads}
          b={lsm.lookup_reads}
          notes={[`height ${btree.height}, ${btree.pages} pages`, `${lsm.levels} levels, ${bytes(lsm.filter_memory)} of filters and indexes in memory`]}
        />
        <Amp title="Range scan" unit=" blocks" hint="disk reads per scan of 100 keys" a={btree.scan_reads} b={lsm.scan_reads} />
        <Amp
          title="Space amplification"
          unit="×"
          hint="bytes on storage per byte of live data"
          a={btree.space_amp}
          b={lsm.space_amp}
          notes={[`${bytes(btree.disk_bytes)}: pages are rarely full`, `${bytes(lsm.disk_bytes)}: compaction drops old versions`]}
        />
      </div>

      <WrittenChart result={result} />
      <LsmReplay result={result} />
    </div>
  );
}

/** The B+tree and the LSM tree on one measure (lower is better). */
function Amp({ title, unit, hint, a, b, notes }: { title: string; unit: string; hint: string; a: number; b: number; notes?: string[] }) {
  const bars: Bar[] = [
    { key: "btree", label: "B+tree", className: "eng-btree", value: a, note: notes?.[0] },
    { key: "lsm", label: "LSM tree", className: "eng-lsm", value: b, note: notes?.[1] },
  ];
  return <BarCompare title={title} hint={hint} unit={unit} bars={bars} best="lowest" />;
}

function WrittenChart({ result }: { result: LabResult }) {
  const series: Series[] = [
    { key: "btree", label: "B+tree", className: "eng-btree", points: result.series.map((s) => [s[0], s[1]]) },
    { key: "lsm", label: "LSM tree", className: "eng-lsm", points: result.series.map((s) => [s[0], s[2]]) },
  ];
  const total = result.series[result.series.length - 1]?.[0] ?? 0;
  return (
    <LineChart title="Bytes written as the workload runs" series={series} xLabel={`writes done (of ${formatCount(total)})`}
      formatX={(v) => `${formatCount(v)} writes`} formatY={(v) => bytes(Math.round(v))}>
      <p className="muted small">
        The B+tree climbs steadily: each commit logs whole pages. The LSM tree climbs in steps: each flush writes a file,
        each compaction rewrites files into the next level.
      </p>
    </LineChart>
  );
}

function describe(m: LsmMoment): string {
  const d = m.detail;
  if (m.kind === "flush") return `Flush: the full memtable became file #${d.table} in level 0 (${bytes(d.bytes ?? 0)}).`;
  if (m.kind === "move") return `Move: file #${d.table} went down from L${d.from} to L${d.to} without being rewritten, since nothing there overlaps its keys.`;
  const merged = (d.inputs?.length ?? 0) + (d.overlapping?.length ?? 0);
  return `Compaction L${d.from} → L${d.to}: ${merged} file${merged === 1 ? "" : "s"} (${d.inputs?.length} from L${d.from}, ${d.overlapping?.length} overlapping in L${d.to}) merged into ${d.outputs?.length}: read ${bytes(d.bytes_in ?? 0)}, wrote ${bytes(d.bytes_out ?? 0)}.`;
}

function LsmReplay({ result }: { result: LabResult }) {
  const moments = result.shapes;
  const [index, setIndex] = useState(moments.length); // moments.length = the final state
  const [playing, setPlaying] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    setIndex(moments.length);
    setPlaying(false);
  }, [moments]);
  useEffect(() => {
    if (!playing) return;
    timer.current = window.setInterval(() => {
      setIndex((i) => {
        if (i >= moments.length) {
          setPlaying(false);
          return i;
        }
        return i + 1;
      });
    }, PLAY_MS);
    return () => window.clearInterval(timer.current ?? undefined);
  }, [playing, moments.length]);

  const moment = index < moments.length ? moments[index] : null;
  const shape: LsmShape = moment ?? result.final;
  const fresh = new Set(moment ? (moment.kind === "compaction" ? moment.detail.outputs : [moment.detail.table]) : []);
  const depth = Math.max(...moments.map((m) => m.levels.length), result.final.levels.length);

  return (
    <section className="lsm-replay" aria-label="The LSM tree's levels">
      <div className="lab-chart-head">
        <h3>Inside the LSM tree</h3>
        <span className="muted small">key space 0 – {formatCount(result.key_space - 1)} →</span>
      </div>
      <div className="lsm-levels">
        <div className="lsm-level">
          <span className="lsm-label">
            Memtable
            <small>
              {bytes(shape.memtable.bytes)} / {bytes(shape.memtable.limit)}
            </small>
          </span>
          <span className="lsm-track memtable">
            <i style={{ width: `${Math.min(100, (shape.memtable.bytes / shape.memtable.limit) * 100)}%` }} />
          </span>
        </div>
        {Array.from({ length: depth }, (_, level) => {
          const files = shape.levels[level] ?? [];
          const size = files.reduce((n, f) => n + f.bytes, 0);
          const cap = shape.capacity[level];
          return (
            <div key={level} className="lsm-level">
              <span className="lsm-label">
                L{level}
                <small>
                  {level === 0 ? `${files.length} file${files.length === 1 ? "" : "s"}, overlapping` : `${bytes(size)}${cap ? ` / ${bytes(cap)}` : ""}`}
                </small>
              </span>
              <span className="lsm-track" style={{ height: level === 0 ? `${Math.max(1, files.length) * 14 + 8}px` : undefined }}>
                {files.map((f, i) => (
                  <i
                    key={f.number}
                    className={fresh.has(f.number) ? "fresh" : ""}
                    title={`file #${f.number}: keys ${formatCount(f.first)}–${formatCount(f.last)}, ${formatCount(f.entries)} entries, ${bytes(f.bytes)}`}
                    style={{
                      left: `${(f.first / result.key_space) * 100}%`,
                      width: `max(3px, ${((f.last - f.first + 1) / result.key_space) * 100}%)`,
                      top: level === 0 ? `${4 + i * 14}px` : undefined,
                    }}
                  />
                ))}
              </span>
            </div>
          );
        })}
      </div>
      <div className="lsm-scrub">
        <button className="button ghost small" onClick={() => {
          if (index >= moments.length) setIndex(0);
          setPlaying((p) => !p);
        }} aria-label={playing ? "Pause" : "Play the flushes and compactions"}>
          {playing ? "❚❚" : "▶"}
        </button>
        <input type="range" min={0} max={moments.length} value={index} aria-label="Moment"
          onChange={(e) => {
            setPlaying(false);
            setIndex(Number(e.target.value));
          }} />
        <span className="muted small">
          {index < moments.length ? `${index + 1} / ${moments.length}` : "end"}
        </span>
      </div>
      <p className="lsm-caption" aria-live="polite">
        {moment
          ? `After ${formatCount(moment.op)} writes. ${describe(moment)}`
          : `The end state, after ${formatCount(result.workload.operations)} writes, ${result.lsm.flushes} flushes, ${result.lsm.compactions} compactions and ${result.lsm.moves} moves. Press play to watch it build up.`}
      </p>
    </section>
  );
}

