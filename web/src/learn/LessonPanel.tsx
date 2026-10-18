import { useRef, useState } from "react";
import type { CrashSummary, EngineState } from "../api";
import { RichText } from "./RichText";
import { LESSONS, type Lesson, type Step } from "./lessons";
import type { CheckContext, Outcome } from "./outcome";
import type { Progress } from "./progress";

interface Props {
  progress: Progress;
  onOpen: (lesson: string | null, step?: number) => void;
  onPassed: (lesson: string, step: string) => void;
  run: (sql: string, step: Step) => Promise<Outcome | null>;
  crash: () => Promise<CrashSummary | null>;
  state: EngineState | null;
  onTryInPlayground: (sql: string) => void;
  busy: boolean;
}

type Status = { kind: "idle" } | { kind: "running" } | { kind: "passed"; message: string } | { kind: "failed"; message: string };

export function LessonPanel(props: Props) {
  const { progress, onOpen } = props;
  const lesson = LESSONS.find((l) => l.id === progress.lesson);
  const lessonsDone = LESSONS.filter((l) => l.steps.every((s) => progress.done.includes(`${l.id}/${s.id}`))).length;

  if (!lesson) {
    return (
      <section className="side glass" aria-label="Course">
        <header className="course-head">
          <p className="eyebrow">Interactive course</p>
          <h2>How a database really works</h2>
          <p className="muted">
            {LESSONS.length} short lessons. Every step runs real SQL on your own database and checks what the engine actually did.
          </p>
          <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={LESSONS.length} aria-valuenow={lessonsDone}>
            <i style={{ width: `${(lessonsDone / LESSONS.length) * 100}%` }} />
          </div>
          <p className="muted small">
            {lessonsDone} of {LESSONS.length} lessons complete
          </p>
        </header>
        <ol className="lesson-list">
          {LESSONS.map((l, i) => {
            const done = l.steps.filter((s) => progress.done.includes(`${l.id}/${s.id}`)).length;
            const complete = done === l.steps.length;
            return (
              <li key={l.id}>
                <button className={`lesson-card ${complete ? "complete" : ""}`} onClick={() => onOpen(l.id, complete ? 0 : done)}>
                  <span className="lesson-number" aria-hidden="true">
                    {complete ? "✓" : i + 1}
                  </span>
                  <span className="lesson-text">
                    <strong>{l.title}</strong>
                    <span>{l.summary}</span>
                    <span className="lesson-meta">
                      {l.minutes} min · {done}/{l.steps.length} steps
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </section>
    );
  }
  return <LessonView key={lesson.id} lesson={lesson} {...props} />;
}

function LessonView({ lesson, progress, onOpen, onPassed, run, crash, state, onTryInPlayground, busy }: Props & { lesson: Lesson }) {
  const index = Math.min(progress.step, lesson.steps.length - 1);
  const step = lesson.steps[index]!;
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const outcomes = useRef(new Map<string, Outcome>());
  const number = LESSONS.indexOf(lesson) + 1;
  const passedBefore = progress.done.includes(`${lesson.id}/${step.id}`);

  const go = (next: number) => {
    setStatus({ kind: "idle" });
    if (next >= lesson.steps.length) onOpen(null);
    else onOpen(lesson.id, next);
  };

  const attempt = async () => {
    setStatus({ kind: "running" });
    let outcome: Outcome | null = null;
    let summary: CrashSummary | null = null;
    if (step.sql) outcome = await run(step.sql, step);
    if (step.action === "crash") summary = await crash();
    if ((step.sql && !outcome) || (step.action === "crash" && !summary)) {
      setStatus({ kind: "failed", message: "That did not run. See the error on the right, then try again." });
      return;
    }
    if (outcome) outcomes.current.set(step.id, outcome);
    const ctx: CheckContext = {
      outcome,
      crash: summary,
      state: outcome?.state ?? state,
      earlier: (id) => outcomes.current.get(id),
    };
    if (!step.check || step.check(ctx)) {
      onPassed(lesson.id, step.id);
      setStatus({ kind: "passed", message: step.success?.(ctx) ?? "Done." });
    } else {
      setStatus({ kind: "failed", message: step.hint ?? "The engine did not do what this step expects. Run it again." });
    }
  };

  const interactive = Boolean(step.sql || step.action);
  const canAdvance = !interactive || status.kind === "passed" || passedBefore;

  return (
    <section className="side glass" aria-label={`Lesson ${number}: ${lesson.title}`}>
      <nav className="crumbs">
        <button className="link" onClick={() => onOpen(null)}>
          ← All lessons
        </button>
        <span className="muted small">
          Lesson {number} of {LESSONS.length}
        </span>
      </nav>
      <h2 className="lesson-title">{lesson.title}</h2>
      <ol className="step-dots" aria-label="Steps">
        {lesson.steps.map((s, i) => (
          <li key={s.id}>
            <button
              className={`dot ${i === index ? "current" : ""} ${progress.done.includes(`${lesson.id}/${s.id}`) ? "done" : ""}`}
              aria-label={`Step ${i + 1}: ${s.title}`}
              aria-current={i === index ? "step" : undefined}
              onClick={() => go(i)}
            />
          </li>
        ))}
      </ol>
      <article className="step" aria-live="polite">
        <p className="eyebrow">
          Step {index + 1} of {lesson.steps.length}
        </p>
        <h3>{step.title}</h3>
        {step.body.map((paragraph, i) => (
          <p key={i}>
            <RichText text={paragraph} />
          </p>
        ))}
        {step.sql && <pre className="step-sql">{step.sql}</pre>}
        {interactive && (
          <div className="step-actions">
            <button
              className={`button ${step.action === "crash" ? "danger" : "primary"}`}
              onClick={() => void attempt()}
              disabled={busy || status.kind === "running"}
            >
              {status.kind === "running" ? "Running…" : step.action === "crash" ? "Pull the plug" : status.kind === "idle" ? "Run it" : "Run again"}
            </button>
            {step.sql && (
              <button className="button ghost" onClick={() => onTryInPlayground(step.sql!)}>
                Edit in playground
              </button>
            )}
          </div>
        )}
        {status.kind === "passed" && (
          <div className="callout success" role="status">
            <strong>Got it.</strong> <RichText text={status.message} />
          </div>
        )}
        {status.kind === "failed" && (
          <div className="callout warning" role="status">
            <RichText text={status.message} />
          </div>
        )}
      </article>
      <footer className="step-nav">
        <button className="button ghost" onClick={() => go(index - 1)} disabled={index === 0}>
          Back
        </button>
        <button className="button primary" onClick={() => go(index + 1)} disabled={!canAdvance}>
          {index === lesson.steps.length - 1 ? "Finish lesson" : "Next step"}
        </button>
      </footer>
    </section>
  );
}
