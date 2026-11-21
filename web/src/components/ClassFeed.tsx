// What a student sees of the class: the teacher's latest statement with its results, earlier ones below,
// and buttons to open or run any of them on the student's own database.
import type { ClassPost } from "../api";
import { formatValue } from "../format";
import type { Following } from "../useClassroom";

interface Props {
  room: Following;
  onOpen: (sql: string) => void;
  onRun: (sql: string) => void;
  onLeave: () => void;
  busy: boolean;
}

const STATUS: Record<Following["status"], string> = {
  connecting: "connecting…",
  live: "live",
  ended: "the teacher ended the class",
  lost: "connection lost",
  unknown: "no class has this code",
};

function time(at: number) {
  return new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Post({ post, onOpen, onRun, busy }: { post: ClassPost } & Omit<Props, "room" | "onLeave">) {
  const result = post.results.find((r) => r.columns.length) ?? post.results[post.results.length - 1];
  return (
    <div className="class-post">
      <pre className="fix-sql">{post.sql}</pre>
      {post.error ? (
        <p className="error-box small">{post.error}</p>
      ) : (
        <p className="muted small">{post.results.map((r) => r.status).join(" · ")}</p>
      )}
      {result && result.columns.length > 0 && (
        <div className="import-sample">
          <table>
            <thead>
              <tr>
                {result.columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((v, j) => (
                    <td key={j}>{formatValue(v)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {result && result.row_total > result.rows.length && (
        <p className="muted small">
          {result.rows.length} of {result.row_total} rows
        </p>
      )}
      <div className="class-post-actions">
        <button className="button primary small" onClick={() => onRun(post.sql)} disabled={busy}>
          Run it on my database
        </button>
        <button className="button ghost small" onClick={() => onOpen(post.sql)}>
          Open in the playground
        </button>
      </div>
    </div>
  );
}

export function ClassFeed({ room, onOpen, onRun, onLeave, busy }: Props) {
  const posts = [...room.posts].reverse();
  const [latest, ...earlier] = posts;
  return (
    <section className="class-feed glass" aria-label="Your class" aria-live="polite">
      <header className="class-feed-head">
        <span className={`status-pill ${room.status === "live" ? "live" : ""}`}>
          <i aria-hidden="true" />
          {STATUS[room.status]}
        </span>
        <b>{room.title || `Class ${room.code}`}</b>
        <button className="button ghost small" onClick={onLeave}>
          Leave
        </button>
      </header>
      {latest ? (
        <>
          <p className="eyebrow">The teacher ran, at {time(latest.at)}</p>
          <Post post={latest} onOpen={onOpen} onRun={onRun} busy={busy} />
        </>
      ) : (
        <p className="muted small">Waiting for the teacher's first statement.</p>
      )}
      {earlier.length > 0 && (
        <details className="class-earlier">
          <summary className="muted small">
            {earlier.length} earlier statement{earlier.length === 1 ? "" : "s"}
          </summary>
          {earlier.map((post) => (
            <div key={post.seq}>
              <p className="eyebrow">{time(post.at)}</p>
              <Post post={post} onOpen={onOpen} onRun={onRun} busy={busy} />
            </div>
          ))}
        </details>
      )}
    </section>
  );
}
