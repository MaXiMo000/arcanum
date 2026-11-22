// What a student sees of the class: the lesson step the teacher is on (followed automatically unless turned
// off), the teacher's latest statement with its results, earlier ones below, buttons to open or run any of
// them on the student's own database, and a box to send the teacher a question.
import { useState, type FormEvent } from "react";
import type { ClassLesson, ClassPost } from "../api";
import { formatValue } from "../format";
import { LESSONS } from "../learn/lessons";
import type { Following } from "../useClassroom";

interface Props {
  room: Following;
  onOpen: (sql: string) => void;
  onRun: (sql: string) => void;
  onLeave: () => void;
  /** Sends a question to the teacher; resolves to an error message, or null when sent. */
  onAsk: (text: string, name: string) => Promise<string | null>;
  onLockstep: (lockstep: boolean) => void;
  onGoTo: (lesson: ClassLesson) => void;
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

function Post({ post, onOpen, onRun, busy }: { post: ClassPost } & Pick<Props, "onOpen" | "onRun" | "busy">) {
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

function TeacherLesson({ room, onLockstep, onGoTo }: Pick<Props, "room" | "onLockstep" | "onGoTo">) {
  const lesson = room.lesson && LESSONS.find((l) => l.id === room.lesson!.id);
  if (!room.lesson || !lesson) return null;
  const step = lesson.steps[room.lesson.step];
  return (
    <div className="class-lesson">
      <p className="small">
        The teacher is on <b>{lesson.title}</b>
        {step ? `, step ${room.lesson.step + 1}: ${step.title}` : ""}.
      </p>
      <div className="class-post-actions">
        <label className="import-check small">
          <input type="checkbox" checked={room.lockstep} onChange={(e) => onLockstep(e.target.checked)} /> Follow along
        </label>
        {!room.lockstep && (
          <button className="button ghost small" onClick={() => onGoTo(room.lesson!)}>
            Go there
          </button>
        )}
      </div>
    </div>
  );
}

function Ask({ onAsk }: Pick<Props, "onAsk">) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const send = async (e: FormEvent) => {
    e.preventDefault();
    setSending(true);
    const failed = await onAsk(text.trim(), name.trim());
    setSending(false);
    setNote(failed ?? "Sent. Only the teacher sees it.");
    if (!failed) setText("");
  };
  return (
    <details className="class-ask">
      <summary className="muted small">Ask the teacher a question</summary>
      <form onSubmit={(e) => void send(e)}>
        <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={300} rows={2}
          placeholder="Why did this use a hash join?" aria-label="Your question" />
        <div className="class-post-actions">
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} placeholder="Your name (optional)"
            aria-label="Your name" />
          <button className="button primary small" disabled={sending || !text.trim()}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
        {note && <p className="muted small" role="status">{note}</p>}
      </form>
    </details>
  );
}

export function ClassFeed({ room, onOpen, onRun, onLeave, onAsk, onLockstep, onGoTo, busy }: Props) {
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
      <TeacherLesson room={room} onLockstep={onLockstep} onGoTo={onGoTo} />
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
      {room.status === "live" && <Ask onAsk={onAsk} />}
    </section>
  );
}
