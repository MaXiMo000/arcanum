import type { Outcome } from "../learn/outcome";
import { narrate } from "./narrate";

const ICONS = { read: "◎", write: "✎", log: "≋", tree: "⟁", plan: "⚙", ok: "✓", muted: "·" } as const;

export function Explain({ outcome }: { outcome: Outcome | null }) {
  if (!outcome) {
    return (
      <div className="empty-state">
        <p className="empty-title">Nothing has run yet</p>
        <p>Run a statement, from a lesson or the playground, and this tab explains what the engine did and why.</p>
      </div>
    );
  }
  return (
    <div className="explain">
      <pre className="explain-sql">{outcome.sql.trim()}</pre>
      <ol className="story">
        {narrate(outcome).map((line, i) => (
          <li key={i} className={`story-line tone-${line.tone}`}>
            <span className="story-icon" aria-hidden="true">
              {ICONS[line.tone]}
            </span>
            <div>
              <p>{line.text}</p>
              {line.why && <p className="story-why">{line.why}</p>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
