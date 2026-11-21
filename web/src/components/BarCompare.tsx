// One measure compared across a few contenders as horizontal bars, the best one ticked.

export interface Bar {
  key: string;
  label: string;
  /** Sets the bar colour through --eng (see .eng-* in styles.css). */
  className: string;
  value: number;
  /** The value as shown; the number itself by default. */
  shown?: string;
  note?: string;
}

interface Props {
  title: string;
  hint: string;
  unit?: string;
  bars: Bar[];
  /** Which end of the scale wins. */
  best: "lowest" | "highest";
  /** The bar length for a full track; the largest value by default. */
  max?: number;
}

export function BarCompare({ title, hint, unit = "", bars, best, max }: Props) {
  const values = bars.map((b) => b.value);
  const top = max ?? Math.max(...values, 1e-9);
  const winner = best === "lowest" ? Math.min(...values) : Math.max(...values);
  const tied = values.every((v) => v === winner);
  return (
    <section className="amp-card" aria-label={title}>
      <h3>{title}</h3>
      <p className="muted small">{hint}</p>
      {bars.map((bar) => (
        <div key={bar.key} className={`amp-row ${bar.value === winner && !tied ? "best" : ""}`}>
          <span className="amp-name">{bar.label}</span>
          <span className="amp-track">
            <i className={bar.className} style={{ width: `${Math.max(2, (bar.value / top) * 100)}%` }} />
          </span>
          <span className="amp-value">
            {bar.shown ?? bar.value}
            {unit}
          </span>
          {bar.note && <span className="amp-note muted small">{bar.note}</span>}
        </div>
      ))}
    </section>
  );
}
