// How query cost grows with the data: TPC-H-style queries run at doubling sizes, each one's pages compared
// with its cost at the smallest size, against the line a cost proportional to the data would follow.
import { api, type ScalingQuery } from "../api";
import { formatCount } from "../format";
import { useLab } from "../useLab";
import { LineChart, type Series } from "./LineChart";

const GROWTH_CLASS: Record<ScalingQuery["growth"], string> = {
  logarithmic: "eng-btree",
  sublinear: "eng-third",
  linear: "eng-lsm",
};
const GROWTH_NAME: Record<ScalingQuery["growth"], string> = {
  logarithmic: "about flat (logarithmic)",
  sublinear: "slower than the data",
  linear: "with the data (linear)",
};

const factor = (v: number) => `×${v < 10 ? v.toFixed(1) : Math.round(v)}`;
const request = (id: string) => api.scalingLab(id);

export function ScalingLab({ sessionId }: { sessionId: string | null }) {
  const { result, running, error, run } = useLab(sessionId, request);

  const queries = result ? Object.entries(result.queries) : [];
  const series: Series[] = queries.map(([key, q]) => ({
    key,
    label: q.title,
    className: GROWTH_CLASS[q.growth],
    points: q.points.map((p) => [p.rows, p.pages / Math.max(1, q.points[0]!.pages)]),
  }));
  const first = queries[0]?.[1].points ?? [];
  if (first.length) {
    series.push({
      key: "data",
      label: "the data",
      className: "eng-reference",
      dashed: true,
      points: first.map((p) => [p.rows, p.rows / first[0]!.rows]),
    });
  }

  return (
    <div className="engine-lab">
      <header className="lab-head">
        <h2>How queries scale</h2>
        <p className="muted">
          A TPC-H-style database (customers, orders, line items) generated at five doubling sizes, with the same queries run
          at each. A B+tree lookup barely notices the data growing; a scan, a join or a sort does work in proportion to it.
          Cost here is pages the plan asks for, cache hits included, so it measures the plan and not the cache.
        </p>
      </header>

      <section className="lab-controls" aria-label="Run">
        <button className="button primary" onClick={() => void run(undefined)} disabled={running || !sessionId}>
          {running ? (
            <>
              <span className="spinner" aria-hidden="true" /> Generating and querying five databases…
            </>
          ) : (
            "Run the benchmark"
          )}
        </button>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}
      {!result && !error && <p className="muted lab-empty">From 1,000 to 16,000 line items; it takes a few seconds.</p>}

      {result && (
        <div className="lab-report">
          <LineChart
            title="Pages read, relative to the smallest database"
            series={series}
            xLabel="line items (log scale)"
            formatX={(v) => `${formatCount(v)} line items`}
            formatY={factor}
            logX
          >
            <p className="muted small">
              Each line divides a query's pages at every size by its pages at the smallest. Lines along the dashed one grow with
              the data; flat lines are B+tree lookups, whose height grows by one level only when the data grows about a
              hundredfold.
            </p>
          </LineChart>

          <section className="lab-chart" aria-label="The queries">
            <h3>The queries</h3>
            <table className="scaling-table">
              <thead>
                <tr>
                  <th scope="col">Query</th>
                  <th scope="col">Grows</th>
                  <th scope="col">Pages</th>
                  <th scope="col">Time at the largest</th>
                </tr>
              </thead>
              <tbody>
                {queries.map(([key, q]) => {
                  const a = q.points[0]!;
                  const b = q.points[q.points.length - 1]!;
                  return (
                    <tr key={key}>
                      <th scope="row">
                        <b>{q.title}</b>
                        <span className="muted small"> {q.about}</span>
                        <details>
                          <summary className="muted small">SQL</summary>
                          <code className="small">{q.sql}</code>
                        </details>
                      </th>
                      <td>
                        <span className={`growth ${GROWTH_CLASS[q.growth]}`}>{GROWTH_NAME[q.growth]}</span>
                        <span className="muted small"> slope {q.slope}</span>
                      </td>
                      <td>
                        {formatCount(a.pages)} → {formatCount(b.pages)}
                      </td>
                      <td>{b.ms.toFixed(1)} ms</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="muted small">
              The slope is fitted to log(pages) against log(line items): 0 means the cost does not depend on the size, 1 that it
              doubles when the data doubles.
            </p>
          </section>
        </div>
      )}
    </div>
  );
}
