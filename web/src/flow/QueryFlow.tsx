// The query plan three ways: a live flow diagram (rows streaming up from the scans to the result), the
// planner's decisions phase by phase, and the plan as PostgreSQL's EXPLAIN ANALYZE would print it.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Advice, PlannerDecision, QueryResult } from "../api";
import { formatCount } from "../format";
import { CandidateTable, Planner } from "./Planner";
import { explainText, flatten, toPostgres, type Family, type PgNode } from "./postgres";

const CARD_W = 236;
const CARD_H = 112;
const GAP_X = 30;
const GAP_Y = 70;
const PAD = 18;
const MISESTIMATE = 10; // estimate and actual this many times apart earns a badge
const MAX_PARTICLES = 7;
const LEVEL_RANK = { warn: 0, tip: 1, good: 2 } as const;

const ICONS: Record<Family, string> = { scan: "▤", index: "⌖", join: "⋈", aggregate: "Σ", sort: "⇅", set: "∪", output: "◇", hash: "#" };

type View = "flow" | "planner" | "explain";

interface Placed {
  node: PgNode;
  depth: number;
  x: number;
  parent: Placed | null;
}

function layout(root: PgNode): Placed[] {
  const placed: Placed[] = [];
  let slot = 0;
  const place = (node: PgNode, depth: number, parent: Placed | null): Placed => {
    const item: Placed = { node, depth, x: 0, parent };
    placed.push(item);
    const kids = node.children.map((c) => place(c, depth + 1, item));
    item.x = kids.length ? (kids[0]!.x + kids[kids.length - 1]!.x) / 2 : slot++;
    return item;
  };
  place(root, 0, null);
  return placed;
}

const flowing = (node: PgNode) => node.rows ?? node.est ?? 0;

function selfMs(node: PgNode) {
  if (node.ms === undefined || node.family === "hash") return 0;
  const below = node.children.reduce((sum, c) => sum + (c.family === "hash" ? (c.children[0]?.ms ?? 0) : (c.ms ?? 0)), 0);
  return Math.max(0, node.ms - below);
}

function misestimate(node: PgNode): number | null {
  if (node.rows === undefined || node.est === null) return null;
  const low = Math.max(1, Math.min(node.rows, node.est));
  const high = Math.max(node.rows, node.est);
  return high >= MISESTIMATE && high / low >= MISESTIMATE ? Math.round(high / low) : null;
}

interface Props {
  /** Remount (key) per outcome: the picked statement and operator start fresh. */
  results: QueryResult[];
  spilled: boolean;
}

export function QueryFlow({ results, spilled }: Props) {
  const planned = results.filter((r) => r.plan);
  const [index, setIndex] = useState(0);
  const [view, setView] = useState<View>("flow");
  const [full, setFull] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const result = planned[Math.min(index, planned.length - 1)];
  const root = useMemo(() => (result?.plan ? toPostgres(result.plan, spilled) : null), [result, spilled]);

  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  if (!result || !root) {
    return (
      <div className="empty-state">
        <p className="empty-title">No plan yet</p>
        <p>Run a SELECT (or EXPLAIN ANALYZE). Its plan appears here as PostgreSQL would show it, with every decision the planner made.</p>
      </div>
    );
  }

  const showInFlow = (id: number) => {
    const target = flatten(root).find((n) => n.ids.includes(id));
    if (target) setSelected(target.key);
    setView("flow");
  };

  const toolbar = (
    <div className="flow-toolbar">
      <div className="segmented small" role="tablist" aria-label="Plan view">
        {(
          [
            ["flow", "Flow"],
            ["planner", "How it was chosen"],
            ["explain", "EXPLAIN"],
          ] as [View, string][]
        ).map(([id, label]) => (
          <button key={id} role="tab" aria-selected={view === id} className={view === id ? "active" : ""} onClick={() => setView(id)}>
            {label}
          </button>
        ))}
      </div>
      {!full && (
        <button className="button ghost small" onClick={() => setFull(true)} aria-label="Open the plan full screen">
          ⤢ Expand
        </button>
      )}
      {full && (
        <button className="icon-button" onClick={() => setFull(false)} aria-label="Close">
          ✕
        </button>
      )}
    </div>
  );
  const picker = planned.length > 1 && (
    <div className="flow-picker" role="group" aria-label="Statement">
      {planned.map((r, i) => (
        <button key={i} className={`chip ${i === index ? "active" : ""}`} onClick={() => setIndex(i)} title={r.statement}>
          {i + 1}. {r.statement.split(/\s+/).slice(0, 4).join(" ")}
        </button>
      ))}
    </div>
  );
  const timing = <Timing result={result} root={root} />;
  let body: ReactNode;
  if (view === "flow") {
    body = <Diagram key={index} root={root} advice={result.advice} decisions={result.planner} selected={selected} onSelect={setSelected} />;
  } else if (view === "planner") {
    body = <Planner result={result} root={root} onShow={showInFlow} />;
  } else {
    body = <ExplainText text={explainText(root, result.planning_ms, result.execution_ms)} />;
  }
  const content = (
    <>
      {toolbar}
      {picker}
      {timing}
      {body}
    </>
  );
  return (
    <div className="flow">
      {content}
      {full &&
        // On <body>: the glass panels' backdrop-filter would otherwise contain a fixed overlay.
        createPortal(
          <div className="flow-modal" role="dialog" aria-modal="true" aria-label="Query plan">
            <div className="flow-modal-inner glass">{content}</div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function Timing({ result, root }: { result: QueryResult; root: PgNode }) {
  const nodes = flatten(root).filter((n) => n.family !== "hash");
  return (
    <div className="flow-timing">
      <span>
        <b>{nodes.length}</b> operators
      </span>
      {result.planning_ms != null && (
        <span>
          planning <b>{result.planning_ms.toFixed(2)} ms</b>
        </span>
      )}
      {result.execution_ms != null ? (
        <span>
          execution <b>{result.execution_ms.toFixed(2)} ms</b>
        </span>
      ) : (
        <span className="muted">estimated plan, not run (EXPLAIN ANALYZE runs it)</span>
      )}
      {root.cost !== null && (
        <span>
          total cost <b>{formatCount(root.cost)}</b>
        </span>
      )}
    </div>
  );
}

function ExplainText({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="pg-explain-wrap">
      <button
        className="button ghost small pg-copy"
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => setCopied(true));
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <pre className="pg-explain" aria-label="EXPLAIN ANALYZE output">
        {text.split("\n").map((line, i) => (
          <span key={i} className="pg-line">
            <ExplainLine line={line} />
            {"\n"}
          </span>
        ))}
      </pre>
      <p className="muted small">
        Laid out like PostgreSQL's EXPLAIN ANALYZE. Costs are Arcanum's: estimated row visits, one per row of a full scan. Postgres
        also prints a startup cost; Arcanum's executor streams every operator, so only the total is shown.
      </p>
    </div>
  );
}

function ExplainLine({ line }: { line: string }) {
  const node = /^(\s*(?:->\s+)?)([A-Z][\w ]*?(?: on \S+| using \S+ on \S+)?)(\s{2}\(.*)?$/.exec(line);
  const detail = /^(\s+)([A-Z][\w ]+?):(.*)$/.exec(line);
  if (detail && !line.includes("->")) {
    return (
      <>
        {detail[1]}
        <span className="pg-label">{detail[2]}:</span>
        <span className="pg-value">{detail[3]}</span>
      </>
    );
  }
  if (node) {
    return (
      <>
        <span className="pg-arrow">{node[1]}</span>
        <span className="pg-node">{node[2]}</span>
        <span className="pg-numbers">{node[3]}</span>
      </>
    );
  }
  return <span className="pg-value">{line}</span>;
}

function Diagram({ root, advice, decisions, selected, onSelect }: {
  root: PgNode;
  advice: Advice[];
  decisions: PlannerDecision[];
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const placed = useMemo(() => layout(root), [root]);
  const current = placed.find((p) => p.node.key === selected) ?? placed[0]!;
  const analyzed = root.rows !== undefined;
  const max = Math.max(1, ...placed.map((p) => flowing(p.node)));
  const maxCost = Math.max(1, ...placed.map((p) => p.node.cost ?? 0));
  const total = root.ms ?? 0;
  const hottest = analyzed ? placed.reduce((a, b) => (selfMs(b.node) > selfMs(a.node) ? b : a)).node.key : null;
  const notesFor = (node: PgNode) => advice.filter((a) => a.node !== null && node.ids.includes(a.node));

  const width = (Math.max(...placed.map((p) => p.x)) + 1) * (CARD_W + GAP_X) - GAP_X + PAD * 2;
  const height = (Math.max(...placed.map((p) => p.depth)) + 1) * (CARD_H + GAP_Y) - GAP_Y + PAD * 2;
  const left = (p: Placed) => PAD + p.x * (CARD_W + GAP_X);
  const top = (p: Placed) => PAD + p.depth * (CARD_H + GAP_Y);

  return (
    <>
      <div className="flow-canvas">
        <div className="flow-stage" style={{ width, height }} role="tree" aria-label="Plan operators">
          <svg width={width} height={height} className="flow-edges" aria-hidden="true">
            <defs>
              <filter id="particle-glow" x="-200%" y="-200%" width="500%" height="500%">
                <feGaussianBlur stdDeviation="2.4" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>
            {placed
              .filter((p) => p.parent)
              .map((p) => {
                const x1 = left(p) + CARD_W / 2;
                const y1 = top(p);
                const x2 = left(p.parent!) + CARD_W / 2;
                const y2 = top(p.parent!) + CARD_H;
                const mid = (y1 + y2) / 2;
                const rows = flowing(p.node);
                const share = Math.log10(rows + 1) / Math.log10(max + 1);
                const stroke = 2 + 9 * share;
                const d = `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`;
                const particles = rows > 0 ? Math.max(1, Math.round(share * MAX_PARTICLES)) : 0;
                const label = p.node.subplan ? p.node.subplan.replace(/ \(.*\)/, "") : `${formatCount(rows)} ${analyzed ? "rows" : "est"}`;
                return (
                  <g key={p.node.key} className={`edge family-${p.node.family} ${p.node.subplan ? "subquery" : ""}`}>
                    <path d={d} className="edge-halo" strokeWidth={stroke + 8} />
                    <path d={d} className="edge-base" strokeWidth={stroke} />
                    {Array.from({ length: particles }, (_, i) => (
                      <circle key={i} r={2.2 + share * 2} className="edge-particle" filter="url(#particle-glow)">
                        <animateMotion dur={`${2.4 - share}s`} begin={`${(i / particles) * (2.4 - share)}s`} repeatCount="indefinite" path={d} />
                      </circle>
                    ))}
                    <g transform={`translate(${(x1 + x2) / 2}, ${mid})`}>
                      <rect x={-label.length * 3.6 - 9} y={-11} width={label.length * 7.2 + 18} height={22} rx={11} className="edge-pill" />
                      <text textAnchor="middle" y={4} className="edge-label">
                        {label}
                      </text>
                    </g>
                  </g>
                );
              })}
          </svg>
          {placed.map((p) => {
            const node = p.node;
            const notes = notesFor(node);
            const worst = notes.reduce<Advice["level"] | null>((w, a) => (w === null || LEVEL_RANK[a.level] < LEVEL_RANK[w] ? a.level : w), null);
            const off = misestimate(node);
            const share = total > 0 ? selfMs(node) / total : 0;
            const condition = node.details.find(([label]) => label !== "Rows Removed by Filter" && label !== "Sort Method");
            const [, title = node.name, index, table] = /^(.*?)(?: using (\S+))?(?: on (\S+))?$/.exec(node.name) ?? [];
            return (
              <button
                key={node.key}
                role="treeitem"
                aria-selected={current.node.key === node.key}
                className={`flow-card family-${node.family} ${current.node.key === node.key ? "selected" : ""} ${hottest === node.key && total > 0 ? "hot" : ""} ${worst ? `note-${worst}` : ""}`}
                style={{ left: left(p), top: top(p), width: CARD_W, height: CARD_H, animationDelay: `${p.depth * 60}ms` }}
                onClick={() => onSelect(node.key)}
                title={node.name}
              >
                <span className="flow-card-head">
                  <span className="flow-icon" aria-hidden="true">
                    {ICONS[node.family]}
                  </span>
                  <span className="flow-titles">
                    <span className="flow-name">{title}</span>
                    {table && <span className="flow-relation">{index ? `${index} · ${table}` : table}</span>}
                  </span>
                  {hottest === node.key && total > 0 && <span className="flow-hot" title="Most time spent here">●</span>}
                  {worst && <span className={`flow-dot ${worst}`} aria-label={`${notes.length} advisor note${notes.length > 1 ? "s" : ""}`} />}
                </span>
                <span className="flow-detail">{condition ? `${condition[0]}: ${condition[1]}` : node.subplan ?? " "}</span>
                <span className="flow-stats">
                  <span className="flow-rows">
                    <b>{formatCount(flowing(node))}</b> {analyzed ? "rows" : "est rows"}
                  </span>
                  {analyzed && node.est !== null && <span className="muted">est {formatCount(Math.round(node.est))}</span>}
                  {off && <span className="flow-off">×{off} off</span>}
                </span>
                <span className="flow-bars" aria-hidden="true">
                  {node.cost !== null && <i className="cost" style={{ width: `${(node.cost / maxCost) * 100}%` }} />}
                  {analyzed && <i className="time" style={{ width: `${Math.min(100, share * 100)}%` }} />}
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <p className="flow-legend muted small">
        Particles = rows flowing up · edge width = row count · <i className="legend-bar cost" /> estimated cost · <i className="legend-bar time" /> own share of
        time · <span className="flow-dot warn" /> problem <span className="flow-dot tip" /> tip <span className="flow-dot good" /> good
      </p>
      <NodeDetail node={current.node} notes={notesFor(current.node)} decisions={decisions.filter((d) => d.node !== null && current.node.ids.includes(d.node))} total={total} />
    </>
  );
}

function NodeDetail({ node, notes, decisions, total }: { node: PgNode; notes: Advice[]; decisions: PlannerDecision[]; total: number }) {
  const off = misestimate(node);
  return (
    <section className="flow-detail-panel" aria-live="polite">
      <header>
        <span className={`flow-icon family-${node.family}`} aria-hidden="true">
          {ICONS[node.family]}
        </span>
        <div>
          <strong>{node.name}</strong>
          <p>{node.about}</p>
        </div>
      </header>
      {node.details.length > 0 && (
        <dl className="pg-details">
          {node.details.map(([label, value], i) => (
            <div key={i}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <dl className="flow-numbers">
        {node.rows !== undefined && (
          <div>
            <dt>Actual rows</dt>
            <dd>{node.rows.toLocaleString()}</dd>
          </div>
        )}
        {node.est !== null && (
          <div>
            <dt>Estimated rows</dt>
            <dd>{Math.round(node.est).toLocaleString()}</dd>
          </div>
        )}
        {node.cost !== null && (
          <div>
            <dt>Estimated cost</dt>
            <dd>{formatCount(node.cost)}</dd>
          </div>
        )}
        {node.ms !== undefined && (
          <div>
            <dt>Time (incl. inputs)</dt>
            <dd>{node.ms.toFixed(3)} ms</dd>
          </div>
        )}
        {node.ms !== undefined && total > 0 && (
          <div>
            <dt>Own share</dt>
            <dd>{Math.round((selfMs(node) / total) * 100)}%</dd>
          </div>
        )}
      </dl>
      {off && (
        <p className="callout warning small">
          The estimate was {off}× off. Estimates drive the join order and index choices; ANALYZE gives the planner real statistics.
        </p>
      )}
      {decisions.map((d, i) => (
        <div key={i} className="why">
          <p className="eyebrow">Why the planner chose this</p>
          <strong>{d.title}</strong>
          <p className="muted small">{d.detail}</p>
          {d.candidates.length > 0 && <CandidateTable candidates={d.candidates} />}
        </div>
      ))}
      {notes.map((a, i) => (
        <p key={i} className={`callout small advice-${a.level}`}>
          <strong>{a.title}.</strong> {a.detail}
        </p>
      ))}
    </section>
  );
}
