import { useEffect, useRef } from "react";
import { LESSONS } from "../learn/lessons";

interface Props {
  onCourse: () => void;
  onPlay: () => void;
  onCrash: () => void;
  onClose: () => void;
}

const FEATURES = [
  { title: `${LESSONS.length} hands-on lessons`, body: "B+trees, indexes, the buffer pool, the optimizer, transactions, the log. Each step runs real SQL and checks the result." },
  { title: "A query advisor and an error doctor", body: "Postgres-style plan flows with real row counts, one-click fixes measured before and after, and typo fixes for broken SQL." },
  { title: "Your own live database", body: "Not a recording. Every query you write runs on a real engine built from scratch, and you see every page it touches." },
  { title: "Crash it on purpose", body: "Kill the engine in the middle of a write, then watch recovery bring back every committed row." },
];

export function Intro({ onCourse, onPlay, onCrash, onClose }: Props) {
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialog.current?.querySelector<HTMLButtonElement>(".button.primary")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="intro" role="dialog" aria-modal="true" aria-labelledby="intro-title" ref={dialog}>
      <div className="intro-inner">
        <button className="intro-close icon-button" onClick={onClose} aria-label="Close">
          ✕
        </button>
        <p className="eyebrow">A real relational database, built from scratch</p>
        <h1 id="intro-title" className="hero-title">
          See inside a database
          <br />
          <span className="gradient-text">while it thinks.</span>
        </h1>
        <p className="hero-lead">
          Run a query and watch it walk the B+tree, hit the buffer pool and write the log, page by page. Then pull the
          plug and watch it recover.
        </p>
        <div className="hero-actions">
          <button className="button primary large" onClick={onCourse}>
            Start the course
          </button>
          <button className="button ghost large" onClick={onPlay}>
            Explore freely
          </button>
          <button className="button danger-outline large" onClick={onCrash}>
            <span aria-hidden="true">⏻</span> Pull the plug
          </button>
        </div>
        <ul className="hero-features">
          {FEATURES.map((f) => (
            <li key={f.title}>
              <strong>{f.title}</strong>
              <span>{f.body}</span>
            </li>
          ))}
        </ul>
        <p className="hero-foot">Every number on screen comes from the engine itself, not from an animation script.</p>
      </div>
    </div>
  );
}
