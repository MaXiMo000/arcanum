// Start a classroom or join one; while teaching, the code to share and how many students are following.
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Classroom } from "../useClassroom";

interface Props {
  room: Classroom;
  error: string | null;
  onTeach: (title: string) => Promise<void>;
  onFollow: (code: string) => void;
  onLeave: () => Promise<void>;
  onClose: () => void;
}

const CODE = /^[A-HJ-NP-Z2-9]{6}$/;

export function classLink(code: string) {
  return `${window.location.origin}${window.location.pathname}?class=${code}`;
}

export function ClassDialog({ room, error, onTeach, onFollow, onLeave, onClose }: Props) {
  const [title, setTitle] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);

  useEffect(() => {
    dialog.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const teach = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await onTeach(title);
    setBusy(false);
  };
  const follow = (e: FormEvent) => {
    e.preventDefault();
    onFollow(code.trim().toUpperCase().replace(/[\s-]/g, ""));
    onClose();
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  const normalized = code.trim().toUpperCase().replace(/[\s-]/g, "");

  return (
    <div className="overlay" role="presentation" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="crash-dialog class-dialog" role="dialog" aria-modal="true" aria-labelledby="class-title" tabIndex={-1} ref={dialog}>
        <div className="class-dialog-head">
          <h2 id="class-title">Classroom</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {error && (
          <p className="error-box small" role="alert">
            {error}
          </p>
        )}

        {room?.role === "teacher" && (
          <>
            <p className="muted">
              Every statement you run, with its first rows, appears live on your students' screens. They can open it in their
              own playground and run it on their own database.
            </p>
            <div className="class-code" aria-label={`Classroom code ${room.code}`}>
              {room.code.slice(0, 3)}
              <span aria-hidden="true">·</span>
              {room.code.slice(3)}
            </div>
            <p className="class-meta">
              {room.title && <b>{room.title} · </b>}
              {room.students} student{room.students === 1 ? "" : "s"} following
            </p>
            <div className="dialog-actions">
              <button className="button ghost" onClick={() => void copy(classLink(room.code))}>
                {copied ? "Link copied" : "Copy the join link"}
              </button>
              <button className="button danger" onClick={() => void onLeave().then(onClose)}>
                End the class
              </button>
              <button className="button ghost" onClick={onClose}>
                Close
              </button>
            </div>
          </>
        )}

        {room?.role === "student" && (
          <>
            <p className="muted">
              You are following <b>{room.title || room.code}</b>. The teacher's statements appear in the panel on the left.
            </p>
            <div className="dialog-actions">
              <button className="button danger" onClick={() => void onLeave().then(onClose)}>
                Leave the class
              </button>
              <button className="button ghost" onClick={onClose}>
                Close
              </button>
            </div>
          </>
        )}

        {room === null && (
          <div className="class-choices">
            <form className="class-choice" onSubmit={follow}>
              <h3>Join a class</h3>
              <p className="muted small">Type the code your teacher shows.</p>
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="e.g. K7M2QX" maxLength={9} autoComplete="off"
                spellCheck={false} aria-label="Classroom code" className="class-input" />
              <button className="button primary" disabled={!CODE.test(normalized)}>
                Join
              </button>
            </form>
            <form className="class-choice" onSubmit={(e) => void teach(e)}>
              <h3>Teach a class</h3>
              <p className="muted small">Get a code; what you run is shown live to everyone who joins.</p>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (optional)" maxLength={80}
                aria-label="Class title" className="class-input title" />
              <button className="button primary" disabled={busy}>
                {busy ? "Opening…" : "Start teaching"}
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
