import type { ReactNode } from "react";
import { Advisor, healthScore, type Comparison } from "../advisor/Advisor";
import type { AdviceFix, EngineState, QueryResult } from "../api";
import { Explain } from "../explain/Explain";
import { QueryFlow } from "../flow/QueryFlow";
import type { FeedState } from "../feed";
import type { InspectorTab } from "../learn/lessons";
import type { Outcome } from "../learn/outcome";
import { EventLog } from "./EventLog";
import { PageMap } from "./PageMap";
import { ErrorBox, Results, type RunError } from "./Results";
import { WalTape } from "./WalTape";

const TABS: { id: InspectorTab; label: string }[] = [
  { id: "explain", label: "What happened" },
  { id: "results", label: "Results" },
  { id: "flow", label: "Plan" },
  { id: "advisor", label: "Advisor" },
  { id: "pages", label: "Pages" },
  { id: "log", label: "Log" },
  { id: "events", label: "Events" },
];

interface Props {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  outcome: Outcome | null;
  error: RunError | null;
  state: EngineState | null;
  feed: FeedState;
  flashMs: number;
  pending: number;
  onSkip: () => void;
  owners: Map<number, { tree: string; color: number; leaf: boolean }>;
  comparison: Comparison | null;
  onApply: (fix: AdviceFix, result: QueryResult) => Promise<void>;
  onFix: (fixed: string, rerun: string) => void;
  busy: boolean;
}

export function Inspector(props: Props) {
  const { tab, onTab, outcome, error, state, feed, flashMs, pending, onSkip, owners } = props;
  const advice = outcome?.results.flatMap((r) => r.advice) ?? [];
  const problems = advice.filter((a) => a.level !== "good").length;
  const errorBox = error && <ErrorBox {...error} onFix={props.onFix} />;
  let body: ReactNode;
  switch (tab) {
    case "explain":
      body = errorBox || (
        <>
          {problems > 0 && (
            <button className={`advisor-strip ${healthScore(advice) < 80 ? "warn" : "tip"}`} onClick={() => onTab("advisor")}>
              <span aria-hidden="true">✚</span> The advisor has {problems} suggestion{problems > 1 ? "s" : ""} for this statement →
            </button>
          )}
          <Explain outcome={outcome} />
        </>
      );
      break;
    case "results":
      body = errorBox || (outcome ? <Results results={outcome.results} /> : <Empty what="results" />);
      break;
    case "flow":
      body = outcome ? (
        <QueryFlow key={outcome.id} results={outcome.results} spilled={outcome.stats.sort_spills > 0} />
      ) : (
        <Empty what="the query plan" />
      );
      break;
    case "advisor":
      body = errorBox || <Advisor outcome={outcome} comparison={props.comparison} onApply={props.onApply} busy={props.busy} />;
      break;
    case "pages":
      body = state ? (
        <PageMap
          state={state}
          pageFlash={feed.pageFlash}
          flashMs={flashMs}
          owners={owners}
          treeNames={state.trees.filter((t) => t.kind !== "catalog").map((t) => t.name)}
        />
      ) : null;
      break;
    case "log":
      body = (
        <WalTape frames={feed.wal} committedOnDisk={state?.wal.committed_frames ?? 0} checkpointAt={feed.checkpointAt} flashMs={flashMs} />
      );
      break;
    case "events":
      body = <EventLog log={feed.log} counters={feed.counters} pending={pending} onSkip={onSkip} />;
      break;
  }
  return (
    <aside className="inspector glass" aria-label="Inspector">
      <div className="inspector-tabs" role="tablist" aria-label="Inspector">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`inspector-tab ${tab === t.id ? "active" : ""}`}
            onClick={() => onTab(t.id)}
          >
            {t.label}
            {t.id === "events" && pending > 0 && <span className="badge">{pending > 999 ? "999+" : pending}</span>}
            {t.id === "advisor" && problems > 0 && <span className="badge warn">{problems}</span>}
          </button>
        ))}
      </div>
      <div className="inspector-body" role="tabpanel">
        {body}
      </div>
    </aside>
  );
}

function Empty({ what }: { what: string }) {
  return (
    <div className="empty-state">
      <p>Run a statement to see {what} here.</p>
    </div>
  );
}
