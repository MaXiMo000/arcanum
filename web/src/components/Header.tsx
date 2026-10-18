import type { Connection, Speed } from "../feed";

export type View = "2d" | "3d";

interface Props {
  view: View;
  onView: (view: View) => void;
  speed: Speed;
  onSpeed: (speed: Speed) => void;
  connection: Connection;
  inTransaction: boolean;
  onCrash: () => void;
  crashDisabled: boolean;
  theme: "light" | "dark";
  onTheme: () => void;
}

const SPEEDS: { value: Speed; label: string; hint: string }[] = [
  { value: "instant", label: "Instant", hint: "Show the end state straight away" },
  { value: "normal", label: "Normal", hint: "Replay events quickly" },
  { value: "slow", label: "Slow motion", hint: "Replay a few events per second" },
];

export function Header({ view, onView, speed, onSpeed, connection, inTransaction, onCrash, crashDisabled, theme, onTheme }: Props) {
  return (
    <header className="app-header">
      <div className="brand">
        <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
          <rect width="32" height="32" rx="7" className="brand-tile" />
          <g fill="none" className="brand-mark" strokeWidth="2.2" strokeLinecap="round">
            <rect x="11" y="5" width="10" height="6" rx="1.5" />
            <rect x="3.5" y="21" width="9" height="6" rx="1.5" />
            <rect x="19.5" y="21" width="9" height="6" rx="1.5" />
            <path d="M14 11 8 21M18 11l6 10" />
          </g>
        </svg>
        <div>
          <h1>Arcanum</h1>
          <p className="tagline">a database built from scratch, shown working</p>
        </div>
      </div>
      <div className="header-controls">
        <span className={`status-pill ${connection}`} role="status">
          <i aria-hidden="true" />
          {connection === "live" ? "live" : connection === "connecting" ? "connecting" : "offline"}
        </span>
        {inTransaction && (
          <span className="status-pill txn" role="status" title="A BEGIN is waiting for COMMIT or ROLLBACK">
            transaction open
          </span>
        )}
        <div className="segmented" role="radiogroup" aria-label="View">
          {(["2d", "3d"] as const).map((v) => (
            <button
              key={v}
              role="radio"
              aria-checked={view === v}
              aria-label={v === "2d" ? "2D view" : "3D view"}
              className={view === v ? "active" : ""}
              onClick={() => onView(v)}
            >
              {v.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="segmented" role="radiogroup" aria-label="Playback speed">
          {SPEEDS.map((s) => (
            <button
              key={s.value}
              role="radio"
              aria-checked={speed === s.value}
              className={speed === s.value ? "active" : ""}
              aria-label={s.label}
              title={s.hint}
              onClick={() => onSpeed(s.value)}
            >
              {s.label}
            </button>
          ))}
        </div>
        <button className="icon-button" onClick={onTheme} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
          {theme === "dark" ? "☀" : "☾"}
        </button>
        <button className="button danger" onClick={onCrash} disabled={crashDisabled}>
          Pull the plug
        </button>
      </div>
    </header>
  );
}
