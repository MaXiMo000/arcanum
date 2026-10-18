import { headline } from "../explain/narrate";
import type { Outcome } from "../learn/outcome";

export function StoryBar({ outcome, onExplain }: { outcome: Outcome | null; onExplain: () => void }) {
  if (!outcome) {
    return (
      <div className="storybar" aria-live="polite">
        <span className="muted">Run a statement to see the engine at work.</span>
      </div>
    );
  }
  const first = outcome.sql.trim().split(/\s+/).slice(0, 8).join(" ");
  return (
    <div className="storybar" aria-live="polite">
      <code className="storybar-sql">{first.length < outcome.sql.trim().length ? `${first} …` : first}</code>
      <span className="storybar-stats">{headline(outcome)}</span>
      <button className="link small" onClick={onExplain}>
        What happened →
      </button>
    </div>
  );
}
