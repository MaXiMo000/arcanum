// The query plan as a flow diagram: operators as cards, rows flowing up the edges from the scans to the
// result, with actual counts, estimates, time and the advisor's findings on the operators they concern.
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import type { Advice, PlanNode, QueryResult } from "../api";
import { formatCount } from "../format";
import { operatorInfo, type Family } from "./operators";

const CARD_W = 212;
const CARD_H = 96;
const GAP_X = 26;
const GAP_Y = 58;
const PAD = 14;
const MISESTIMATE = 10; // estimate and actual this many times apart is worth a badge
const LEVEL_RANK = { warn: 0, tip: 1, good: 2 } as const;

const ICONS: Record<Family, string> = {
  scan: "▤",
  index: "⌖",
  join: "⋈",
  aggregate: "Σ",
  sort: "⇅",
  filter: "⧩",
  set: "∪",
  output: "◇",
};

interface Placed {
  node: PlanNode;
  depth: number;
  x: number;
  parent: Placed | null;
  subquery: string | null;
}

function layout(root: PlanNode): Placed[] {
  const placed: Placed[] = [];
  let slot = 0;
  const place = (node: PlanNode, depth: number, parent: Placed | null, subquery: string | null): Placed => {
    const item: Placed = { node, depth, x: 0, parent, subquery };
    placed.push(item);
    const kids = [
      ...node.children.map((c) => place(c, depth + 1, item, null)),
      ...node.subplans.map((s) => place(s.plan, depth + 1, item, s.correlated ? `${s.kind} subquery, per row` : `${s.kind} subquery`)),
    ];
    item.x = kids.length ? (kids[0]!.x + kids[kids.length - 1]!.x) / 2 : slot++;
    return item;
  };
  place(root, 0, null, null);
  return placed;
}

const flowing = (node: PlanNode) => node.rows ?? node.est_rows ?? 0;

function selfMs(node: PlanNode) {
  if (node.ms === undefined) return 0;
  const below = [...node.children, ...node.subplans.map((s) => s.plan)].reduce((sum, c) => sum + (c.ms ?? 0), 0);
  return Math.max(0, node.ms - below);
}

function misestimate(node: PlanNode): number | null {
  if (node.rows === undefined || node.est_rows === null) return null;
  const low = Math.max(1, Math.min(node.rows, node.est_rows));
  const high = Math.max(node.rows, node.est_rows);
  return high >= MISESTIMATE && high / low >= MISESTIMATE ? Math.round(high / low) : null;
}

interface Props {
  /** Remount (key) per outcome: the picked statement and operator start fresh. */
  results: QueryResult[];
}

export function QueryFlow({ results }: Props) {
  const planned = results.filter((r) => r.plan);
  const [index, setIndex] = useState(0);
  const [full, setFull] = useState(false);
  const result = planned[Math.min(index, planned.length - 1)];

  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  if (!result?.plan) {
    return (
      <div className="empty-state">
        <p className="empty-title">No plan yet</p>
        <p>Run a SELECT (or EXPLAIN ANALYZE) and its plan appears here as a flow: rows travel up from the scans to the result.</p>
      </div>
    );
  }

  const picker = planned.length > 1 && (
    <div className="flow-picker" role="group" aria-label="Statement">
      {planned.map((r, i) => (
        <button key={i} className={`chip ${i === index ? "active" : ""}`} onClick={() => setIndex(i)} title={r.statement}>
          {i + 1}. {r.statement.split(/\s+/).slice(0, 4).join(" ")}
        </button>
      ))}
    </div>
  );
  const diagram = <Diagram key={index} plan={result.plan} advice={result.advice} />;
  return (
    <div className="flow">
      <div className="flow-toolbar">
        {picker}
        <button className="button ghost small" onClick={() => setFull(true)} aria-label="Open the plan full screen">
          ⤢ Expand
        </button>
      </div>
      {diagram}
      {full &&
        // On <body>: the glass panels' backdrop-filter would otherwise contain a fixed overlay.
        createPortal(
        <div className="flow-modal" role="dialog" aria-modal="true" aria-label="Query plan">
          <div className="flow-modal-inner glass">
            <div className="flow-toolbar">
              <h2>Query plan</h2>
              {picker}
              <button className="icon-button" onClick={() => setFull(false)} aria-label="Close">
                ✕
              </button>
            </div>
            {diagram}
          </div>
        </div>,
        document.body,
        )}
    </div>
  );
}

function Diagram({ plan, advice }: { plan: PlanNode; advice: Advice[] }) {
  const placed = useMemo(() => layout(plan), [plan]);
  const [selected, setSelected] = useState<number>(plan.id);

  const analyzed = plan.rows !== undefined;
  const max = Math.max(1, ...placed.map((p) => flowing(p.node)));
  const total = plan.ms ?? 0;
  const hottest = analyzed ? placed.reduce((a, b) => (selfMs(b.node) > selfMs(a.node) ? b : a)).node.id : null;
  const byNode = new Map<number, Advice[]>();
  for (const a of advice) if (a.node !== null) byNode.set(a.node, [...(byNode.get(a.node) ?? []), a]);

  const width = (Math.max(...placed.map((p) => p.x)) + 1) * (CARD_W + GAP_X) - GAP_X + PAD * 2;
  const height = (Math.max(...placed.map((p) => p.depth)) + 1) * (CARD_H + GAP_Y) - GAP_Y + PAD * 2;
  const left = (p: Placed) => PAD + p.x * (CARD_W + GAP_X);
  const top = (p: Placed) => PAD + p.depth * (CARD_H + GAP_Y);
  const chosen = placed.find((p) => p.node.id === selected) ?? placed[0]!;

  return (
    <>
      <p className="flow-summary">
        {analyzed ? (
          <>
            <strong>{total.toFixed(total < 10 ? 2 : 1)} ms</strong> across {placed.length} operators · rows flow upward from the scans
          </>
        ) : (
          <>Estimated plan (not run): numbers are the optimizer's guesses. Use EXPLAIN ANALYZE for actual rows and time.</>
        )}
      </p>
      <div className="flow-canvas" role="tree" aria-label="Plan operators">
        <div className="flow-stage" style={{ width, height }}>
          <svg width={width} height={height} className="flow-edges" aria-hidden="true">
            {placed
              .filter((p) => p.parent)
              .map((p) => {
                const x1 = left(p) + CARD_W / 2;
                const y1 = top(p);
                const x2 = left(p.parent!) + CARD_W / 2;
                const y2 = top(p.parent!) + CARD_H;
                const mid = (y1 + y2) / 2;
                const rows = flowing(p.node);
                const stroke = 1.5 + 6 * (Math.log10(rows + 1) / Math.log10(max + 1));
                const d = `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`;
                return (
                  <g key={p.node.id} className={p.subquery ? "edge subquery" : "edge"}>
                    <path d={d} className="edge-base" strokeWidth={stroke} />
                    {rows > 0 && <path d={d} className="edge-flow" strokeWidth={Math.max(1.5, stroke - 1)} />}
                    <text x={(x1 + x2) / 2 + 8} y={mid + 4} className="edge-label">
                      {p.subquery ?? `${formatCount(rows)}${analyzed ? "" : " est"}`}
                    </text>
                  </g>
                );
              })}
          </svg>
          {placed.map((p) => {
            const info = operatorInfo(p.node);
            const notes = byNode.get(p.node.id) ?? [];
            const worst = notes.reduce<Advice["level"] | null>(
              (w, a) => (w === null || LEVEL_RANK[a.level] < LEVEL_RANK[w] ? a.level : w),
              null,
            );
            const off = misestimate(p.node);
            const share = total > 0 ? selfMs(p.node) / total : 0;
            return (
              <button
                key={p.node.id}
                role="treeitem"
                aria-selected={selected === p.node.id}
                className={`flow-card family-${info.family} ${selected === p.node.id ? "selected" : ""} ${hottest === p.node.id && total > 0 ? "hot" : ""} ${worst ? `note-${worst}` : ""}`}
                style={{ left: left(p), top: top(p), width: CARD_W, height: CARD_H }}
                onClick={() => setSelected(p.node.id)}
                title={p.node.label}
              >
                <span className="flow-card-head">
                  <span className="flow-icon" aria-hidden="true">
                    {ICONS[info.family]}
                  </span>
                  <span className="flow-name">{info.name}</span>
                  {worst && <span className={`flow-dot ${worst}`} aria-label={`${notes.length} advisor note${notes.length > 1 ? "s" : ""}`} />}
                </span>
                <span className="flow-detail">{info.detail || " "}</span>
                <span className="flow-stats">
                  <span>
                    <b>{formatCount(flowing(p.node))}</b> {analyzed ? "rows" : "est"}
                  </span>
                  {analyzed && p.node.est_rows !== null && <span className="muted">est {formatCount(Math.round(p.node.est_rows))}</span>}
                  {off && <span className="flow-off">×{off} off</span>}
                </span>
                {analyzed && (
                  <span className="flow-time" aria-hidden="true">
                    <i style={{ width: `${Math.min(100, share * 100)}%` }} />
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
      <NodeDetail node={chosen.node} notes={byNode.get(chosen.node.id) ?? []} total={total} />
      <p className="flow-legend muted small">
        Edge thickness = rows flowing up · bar = share of time spent in the operator itself · <span className="flow-dot warn" /> problem{" "}
        <span className="flow-dot tip" /> tip <span className="flow-dot good" /> good
      </p>
    </>
  );
}

function NodeDetail({ node, notes, total }: { node: PlanNode; notes: Advice[]; total: number }) {
  const info = operatorInfo(node);
  const off = misestimate(node);
  return (
    <section className="flow-detail-panel" aria-live="polite">
      <header>
        <strong>{info.name}</strong>
        <code>{node.label}</code>
      </header>
      <p>{info.about}</p>
      <dl className="flow-numbers">
        {node.rows !== undefined && (
          <div>
            <dt>Actual rows</dt>
            <dd>{node.rows.toLocaleString()}</dd>
          </div>
        )}
        {node.est_rows !== null && (
          <div>
            <dt>Estimated</dt>
            <dd>{Math.round(node.est_rows).toLocaleString()}</dd>
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
          The optimizer's estimate was {off}× off. Estimates drive join order and index choice; ANALYZE keeps them honest.
        </p>
      )}
      {notes.map((a, i) => (
        <p key={i} className={`callout small advice-${a.level}`}>
          <strong>{a.title}.</strong> {a.detail}
        </p>
      ))}
    </section>
  );
}
