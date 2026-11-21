// A line chart for the labs: several series over one shared x axis, labelled at their ends, with a
// crosshair and tooltip that follow the pointer.
import { useState, type PointerEvent, type ReactNode } from "react";

export interface Series {
  key: string;
  label: string;
  /** Sets the series colour through --eng (see .eng-* in styles.css). */
  className: string;
  /** [x, y] points; every series shares the first series' x values. */
  points: [number, number][];
  dashed?: boolean;
}

interface Props {
  title: string;
  series: Series[];
  xLabel: string;
  formatX: (x: number) => string;
  formatY: (y: number) => string;
  /** The top of the y axis; the largest value by default. */
  yMax?: number;
  /** Log scale on x, for values spanning orders of magnitude; each x value gets a tick label. */
  logX?: boolean;
  /** A vertical marker at this x. */
  mark?: number;
  children?: ReactNode;
}

const W = 640;
const H = 220;
const PAD = { l: 52, r: 76, t: 14, b: 30 };
const LABEL_GAP = 12; // the least vertical space between two end labels
const TICK_GAP = 28; // the least horizontal space between two x tick labels

/** End-label positions, pushed apart where lines end close together. */
function spread(ys: number[]): number[] {
  const order = ys.map((y, i) => [y, i] as const).sort((a, b) => a[0] - b[0]);
  const out = [...ys];
  let previous = -Infinity;
  for (const [y, i] of order) {
    out[i] = Math.max(y, previous + LABEL_GAP);
    previous = out[i]!;
  }
  return out;
}

/** Which x values get a tick label: the last always, others only with room beside their neighbours. */
function xTicks(positions: number[]): number[] {
  const shown: number[] = [];
  const last = positions.length - 1;
  positions.forEach((p, k) => {
    const room = (q: number | undefined) => q === undefined || Math.abs(q - p) >= TICK_GAP;
    if (room(positions[shown[shown.length - 1] ?? -1]) && (k === last || room(positions[last]))) shown.push(k);
  });
  return shown;
}

export function LineChart({ title, series, xLabel, formatX, formatY, yMax, logX, mark, children }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const xs = series[0]?.points.map((p) => p[0]) ?? [];
  if (!xs.length) return null;
  const scale = (v: number) => (logX ? Math.log(Math.max(v, 1e-9)) : v);
  const [x0, x1] = [scale(xs[0]!), scale(xs[xs.length - 1]!)];
  const top = yMax ?? Math.max(...series.flatMap((s) => s.points.map((p) => p[1])), 1e-9);
  const x = (v: number) => PAD.l + ((scale(v) - x0) / (x1 - x0 || 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) => H - PAD.b - (v / top) * (H - PAD.t - PAD.b);
  const path = (s: Series) => s.points.map((p, k) => `${k ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  const ends = spread(series.map((s) => y(s.points[s.points.length - 1]?.[1] ?? 0) + 4));
  const at = hover === null ? null : xs[hover]!;

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    for (let k = 1; k < xs.length; k++) if (Math.abs(x(xs[k]!) - px) < Math.abs(x(xs[best]!) - px)) best = k;
    setHover(best);
  };

  const last = xs.length - 1;
  const summary = series.map((s) => `${s.label} ${formatY(s.points[last]?.[1] ?? 0)}`).join(", ");
  return (
    <section className="lab-chart" aria-label={title}>
      <div className="lab-chart-head">
        <h3>{title}</h3>
        <span className="legend">
          {series.map((s) => (
            <span key={s.key}>
              <i className={`${s.className}${s.dashed ? " dashed" : ""}`} /> {s.label}
            </span>
          ))}
        </span>
      </div>
      <div className="lab-chart-box">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, at ${formatX(xs[last]!)}: ${summary}`}
          onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
          {ticks.map((t) => (
            <g key={t} className="grid">
              <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
              <text x={PAD.l - 8} y={y(t) + 4} textAnchor="end">
                {formatY(t)}
              </text>
            </g>
          ))}
          {logX &&
            xTicks(xs.map(x)).map((k) => (
              <text key={xs[k]} x={x(xs[k]!)} y={H - PAD.b + 13} textAnchor="middle">
                {xs[k]}
              </text>
            ))}
          <text className="axis-label" x={(PAD.l + W - PAD.r) / 2} y={H - 4} textAnchor="middle">
            {xLabel}
          </text>
          {mark !== undefined && <line className="mark" x1={x(mark)} x2={x(mark)} y1={PAD.t} y2={H - PAD.b} />}
          {series.map((s, i) => (
            <g key={s.key} className={s.className}>
              <path d={path(s)} className={`line${s.dashed ? " dashed" : ""}`} />
              <text className="end-label" x={W - PAD.r + 6} y={ends[i]}>
                {s.label}
              </text>
            </g>
          ))}
          {at !== null && (
            <g className="crosshair">
              <line x1={x(at)} x2={x(at)} y1={PAD.t} y2={H - PAD.b} />
              {series.map((s) => (
                <circle key={s.key} cx={x(at)} cy={y(s.points[hover!]?.[1] ?? 0)} r={4} className={s.className} />
              ))}
            </g>
          )}
        </svg>
        {at !== null && (
          <div className="chart-tip" style={{ left: `${(x(at) / W) * 100}%` }}>
            <b>{formatX(at)}</b>
            {series.map((s) => (
              <span key={s.key}>
                <i className={s.className} /> {s.label} {formatY(s.points[hover!]?.[1] ?? 0)}
              </span>
            ))}
          </div>
        )}
      </div>
      {children}
    </section>
  );
}
