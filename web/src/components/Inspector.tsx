import type { ReactNode } from "react";
import { Advisor, healthScore, type Comparison } from "../advisor/Advisor";
import { IndexReport } from "../advisor/IndexReport";
import type { AdviceFix, IndexReport as Report, QueryResult } from "../api";
import { Explain } from "../explain/Explain";
import { QueryFlow } from "../flow/QueryFlow";
import { queryKey, type PlanHistory } from "../flow/planHistory";
import { PlanHistoryView } from "../flow/PlanHistoryView";
import type { FeedState } from "../feed";
import type { InspectorTab } from "../learn/lessons";
import type { Outcome } from "../learn/outcome";
import { EventLog } from "./EventLog";
import { ErrorBox, Results, type RunError } from "./Results";

const TABS: { id: InspectorTab; label: string }[] = [
  { id: "explain", label: "What happened" },
  { id: "results", label: "Results" },
  { id: "flow", label: "Plan" },
  { id: "advisor", label: "Advisor" },
  { id: "events", label: "Events" },
];

interface Props {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  outcome: Outcome | null;
  error: RunError | null;
  feed: FeedState;
  pending: number;
  onSkip: () => void;
  comparison: Comparison | null;
  onApply: (fix: AdviceFix, result: QueryResult) => Promise<void>;
  /** Run one statement the index report suggests. */
  onRun: (sql: string) => void;
  report: Report | null;
  /** Earlier plans of each query, for the Plan tab's history. */
  planHistory: PlanHistory;
  onFix: (fixed: string, rerun: string) => void;
  busy: boolean;
}

export function Inspector(props: Props) {
  const { tab, onTab, outcome, error, feed, pending, onSkip } = props;
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
        <>
          <QueryFlow key={outcome.id} results={outcome.results} spilled={outcome.stats.sort_spills > 0} />
          {(() => {
            const planned = outcome.results.filter((r) => r.plan);
            const last = planned[planned.length - 1];
            const runs = last && props.planHistory[queryKey(last.statement)];
            return runs ? <PlanHistoryView key={`${queryKey(last.statement)}:${runs.length}`} runs={runs} /> : null;
          })()}
        </>
      ) : (
        <Empty what="the query plan" />
      );
      break;
    case "advisor":
      body = errorBox || (
        <>
          <Advisor outcome={outcome} comparison={props.comparison} onApply={props.onApply} busy={props.busy} />
          {props.report && <IndexReport report={props.report} onRun={props.onRun} busy={props.busy} />}
        </>
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
