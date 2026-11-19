import type { SessionName, SessionTxn } from "../api";
import type { Connection, Speed } from "../feed";

export type View = "2d" | "3d";
export type Mode = "learn" | "play";

interface Props {
  mode: Mode;
  onMode: (mode: Mode) => void;
  view: View;
  onView: (view: View) => void;
  speed: Speed;
  onSpeed: (speed: Speed) => void;
  connection: Connection;
  sessions: Record<SessionName, SessionTxn> | null;
  onCrash: () => void;
  crashDisabled: boolean;
  theme: "light" | "dark";
  onTheme: () => void;
  onAbout: () => void;
}

function Segmented<T extends string>({ label, value, options, onChange }: {
  label: string;
  value: T;
  options: { value: T; label: string; hint?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={value === o.value}
          aria-label={o.label}
          title={o.hint}
          className={value === o.value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function TopBar(p: Props) {
  return (
    <header className="topbar">
      <button className="brand" onClick={p.onAbout} aria-label="About Arcanum">
        <svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true">
          <defs>
            <linearGradient id="brand-gradient" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="#ffd36b" />
              <stop offset="0.55" stopColor="#ff6fd0" />
              <stop offset="1" stopColor="#6d8bff" />
            </linearGradient>
          </defs>
          <rect width="32" height="32" rx="9" className="brand-tile" />
          <g fill="none" stroke="url(#brand-gradient)" strokeWidth="2.3" strokeLinecap="round">
            <path d="M16 5 22 11 16 17 10 11Z" />
            <path d="M9 19 13 23 9 27 5 23ZM23 19 27 23 23 27 19 23Z" />
            <path d="M13 15 10 20M19 15l3 5" />
          </g>
        </svg>
        <span className="brand-name">Arcanum</span>
      </button>
      <Segmented<Mode>
        label="Mode"
        value={p.mode}
        onChange={p.onMode}
        options={[
          { value: "learn", label: "Learn", hint: "Guided lessons" },
          { value: "play", label: "Play", hint: "Write any SQL and take on challenges" },
        ]}
      />
      <div className="topbar-spacer" />
      <span className={`status-pill ${p.connection}`} role="status" title="Live connection to your database">
        <i aria-hidden="true" />
        {p.connection === "live" ? "live" : p.connection}
      </span>
      {Object.entries(p.sessions ?? {}).map(([name, s]) =>
        s.xid === null ? null : (
          <span
            key={name}
            className={`status-pill txn s-${name}`}
            role="status"
            title={`Session ${name} has transaction ${s.xid} open (${s.isolation}); it is waiting for COMMIT or ROLLBACK`}
          >
            {name} · txn {s.xid}
          </span>
        ),
      )}
      <Segmented<View>
        label="View"
        value={p.view}
        onChange={p.onView}
        options={[
          { value: "2d", label: "Map", hint: "The engine map: tree, buffer pool and disk" },
          { value: "3d", label: "3D", hint: "The tree in 3D" },
        ]}
      />
      <Segmented<Speed>
        label="Playback speed"
        value={p.speed}
        onChange={p.onSpeed}
        options={[
          { value: "instant", label: "Instant", hint: "Show the end state straight away" },
          { value: "normal", label: "Normal", hint: "Replay events quickly" },
          { value: "slow", label: "Slow", hint: "Slow motion: a few events per second" },
        ]}
      />
      <button className="icon-button" onClick={p.onTheme} aria-label={`Switch to ${p.theme === "dark" ? "light" : "dark"} theme`}>
        {p.theme === "dark" ? "☀" : "☾"}
      </button>
      <button className="button danger plug" onClick={p.onCrash} disabled={p.crashDisabled}>
        <span aria-hidden="true">⏻</span> Pull the plug
      </button>
    </header>
  );
}
