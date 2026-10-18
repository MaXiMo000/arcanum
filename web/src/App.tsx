import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Comparison } from "./advisor/Advisor";
import { api, ApiError, type AdviceFix, type CrashSummary, type EngineState, type QueryResult } from "./api";
import { CrashOverlay } from "./components/CrashOverlay";
import { Inspector } from "./components/Inspector";
import { Intro } from "./components/Intro";
import type { RunError } from "./components/Results";
import { StoryBar } from "./components/StoryBar";
import { Toasts, type Toast } from "./components/Toasts";
import { TopBar, type Mode, type View } from "./components/TopBar";
import { TreeView } from "./components/TreeView";
import { EXAMPLES } from "./examples";
import { FLASH_MS, useEngineFeed } from "./feed";
import { CHALLENGES } from "./learn/challenges";
import { LessonPanel } from "./learn/LessonPanel";
import type { InspectorTab, Step } from "./learn/lessons";
import type { CheckContext, Outcome } from "./learn/outcome";
import { PlayPanel } from "./learn/PlayPanel";
import { useProgress } from "./learn/progress";

const SESSION_KEY = "arcanum.session";
const THEME_KEY = "arcanum.theme";
const VIEW_KEY = "arcanum.view";
const MODE_KEY = "arcanum.mode";
const INTRO_KEY = "arcanum.intro-seen";
const EXPLAIN_PREFIX = /^\s*EXPLAIN\s+(ANALYZE\s+)?/i;

// The 3D view (three.js) is a separate download, fetched the first time someone opens it.
const Vault = lazy(() => import("./vault/Vault"));

function readStorage(storage: () => Storage, key: string): string | null {
  try {
    return storage().getItem(key);
  } catch {
    return null; // storage can be unavailable (private windows, blocked site data)
  }
}

function writeStorage(storage: () => Storage, key: string, value: string | null) {
  try {
    if (value === null) storage().removeItem(key);
    else storage().setItem(key, value);
  } catch {
    // Not essential: the page works without it.
  }
}

/** The tree shown until the viewer picks one; fixed once, so tables created later do not steal the view. */
function firstTable(state: EngineState): string | null {
  return state.trees.find((t) => t.kind === "table")?.name ?? null;
}

const session = () => window.sessionStorage;
const local = () => window.localStorage;

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [state, setState] = useState<EngineState | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [selectedTree, setSelectedTree] = useState<string | null>(null); // set to the first table on load
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [runError, setRunError] = useState<RunError | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [tab, setTab] = useState<InspectorTab>("explain");
  const [playSql, setPlaySql] = useState(EXAMPLES[0]!.sql);
  const [crashOpen, setCrashOpen] = useState(false);
  const [crashSummary, setCrashSummary] = useState<CrashSummary | null>(null);
  const [crashError, setCrashError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [introOpen, setIntroOpen] = useState(() => readStorage(local, INTRO_KEY) === null);
  const [mode, setMode] = useState<Mode>(() => (readStorage(local, MODE_KEY) === "play" ? "play" : "learn"));
  const [view, setView] = useState<View>(() => (readStorage(local, VIEW_KEY) === "2d" ? "2d" : "3d"));
  const [theme, setTheme] = useState<"light" | "dark">(() => (readStorage(local, THEME_KEY) === "light" ? "light" : "dark"));
  const { progress, markStep, unlock, open } = useProgress();

  const idRef = useRef<string | null>(null);
  const refreshing = useRef<Promise<void> | null>(null);
  const refreshAgain = useRef(false);
  const starting = useRef<Promise<void> | null>(null);
  const outcomeIds = useRef(0);
  const toastIds = useRef(0);
  const completedRef = useRef(progress.challenges);
  completedRef.current = progress.challenges;

  const refresh = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    if (refreshing.current) {
      refreshAgain.current = true; // coalesce bursts into one follow-up request
      return;
    }
    refreshing.current = (async () => {
      try {
        setState(await api.state(id));
      } catch {
        // A failed refresh keeps the last picture; the next action refreshes again.
      } finally {
        refreshing.current = null;
        if (refreshAgain.current) {
          refreshAgain.current = false;
          void refresh();
        }
      }
    })();
  }, []);

  const startSession = useCallback((forceNew: boolean) => {
    // One start at a time: a second call (a remount, a lost session reported twice) joins the first.
    starting.current ??= openSession(forceNew).finally(() => {
      starting.current = null;
    });
    return starting.current;
  }, []);

  async function openSession(forceNew: boolean) {
    setStartError(null);
    const saved = forceNew ? null : readStorage(session, SESSION_KEY);
    if (saved) {
      try {
        const current = await api.state(saved);
        idRef.current = saved;
        setSessionId(saved);
        setState(current);
        setSelectedTree((name) => name ?? firstTable(current));
        return;
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 404) {
          setStartError(e instanceof ApiError ? e.message : "the server cannot be reached");
          return;
        }
      }
    }
    try {
      const created = await api.createSession();
      writeStorage(session, SESSION_KEY, created.id);
      idRef.current = created.id;
      setSessionId(created.id);
      const fresh = await api.state(created.id);
      setState(fresh);
      setSelectedTree((name) => name ?? firstTable(fresh));
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : "the server cannot be reached");
    }
  }

  useEffect(() => {
    void startSession(false);
  }, [startSession]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    writeStorage(local, THEME_KEY, theme);
  }, [theme]);
  useEffect(() => writeStorage(local, VIEW_KEY, view), [view]);
  useEffect(() => writeStorage(local, MODE_KEY, mode), [mode]);
  const use2d = useCallback(() => setView("2d"), []);

  const onSessionGone = useCallback(() => {
    writeStorage(session, SESSION_KEY, null);
    void startSession(true);
  }, [startSession]);

  const { feed, pending, speed, setSpeed, skip, connection, clearCrash, startCrash } = useEngineFeed(
    sessionId,
    onSessionGone,
    refresh,
  );
  const flashMs = FLASH_MS[speed];

  useEffect(() => {
    document.documentElement.style.setProperty("--flash-ms", `${flashMs}ms`);
  }, [flashMs]);

  const dismissToast = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), []);

  /** Award any challenge the latest outcome or crash satisfies, with a toast for each new one. */
  const judge = useCallback(
    (ctx: CheckContext) => {
      const fresh = CHALLENGES.filter((c) => !(c.id in completedRef.current) && c.check(ctx));
      if (!fresh.length) return;
      // Record them now, so a second check before the next render does not award them again.
      completedRef.current = { ...completedRef.current, ...Object.fromEntries(fresh.map((c) => [c.id, Date.now()])) };
      unlock(fresh.map((c) => c.id));
      setToasts((all) => [...all, ...fresh.map((c) => ({ id: ++toastIds.current, title: `Challenge complete: ${c.title}`, body: c.goal }))]);
    },
    [unlock],
  );

  const runQuery = useCallback(
    async (sql: string, focus?: InspectorTab): Promise<Outcome | null> => {
      const id = idRef.current;
      if (!id) return null;
      setRunError(null);
      setComparison(null);
      try {
        const { results, stats } = await api.query(id, sql);
        let after: EngineState | null = null;
        try {
          after = await api.state(id);
          setState(after);
        } catch {
          // The outcome still stands without the fresh picture.
        }
        const next: Outcome = { id: ++outcomeIds.current, sql, results, stats, state: after };
        setOutcome(next);
        setTab(focus ?? (mode === "play" ? "results" : "explain"));
        judge({ outcome: next, crash: null, state: after, earlier: () => undefined });
        return next;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) onSessionGone();
        setRunError({ error: e instanceof ApiError ? e : new ApiError(0, { type: "Error", message: String(e) }), sql });
        setTab("explain");
        return null;
      }
    },
    [mode, judge, onSessionGone],
  );

  /** Run the advisor's fix, measuring the statement before and after it. */
  const applyFix = useCallback(
    async (fix: AdviceFix, result: QueryResult) => {
      const statement = result.statement.replace(EXPLAIN_PREFIX, "");
      setMeasuring(true);
      try {
        const before = await runQuery(statement, "advisor");
        if (!before) return;
        let after: Outcome | null;
        if (fix.kind === "run") {
          if (!(await runQuery(fix.sql, "advisor"))) return;
          after = await runQuery(statement, "advisor");
        } else {
          const original = fix.original;
          if (original) setPlaySql((sql) => (sql.includes(original) ? sql.replace(original, fix.sql) : sql));
          after = await runQuery(fix.sql.replace(EXPLAIN_PREFIX, ""), "advisor");
        }
        if (!after) return;
        const measured = { label: fix.label, before, after };
        setComparison(measured);
        judge({ outcome: after, crash: null, state: after.state, earlier: () => undefined, comparison: measured });
      } finally {
        setMeasuring(false);
      }
    },
    [runQuery, judge],
  );

  /** Apply the doctor's fix: correct the editor, then run what is left of the script. */
  const fixError = useCallback(
    (fixed: string, rerun: string) => {
      const failed = runError?.sql;
      setPlaySql((sql) => (failed && sql.endsWith(failed) ? sql.slice(0, sql.length - failed.length) + fixed : fixed));
      setMode("play");
      void runQuery(rerun);
    },
    [runError, runQuery],
  );

  const crash = useCallback(async (): Promise<CrashSummary | null> => {
    const id = idRef.current;
    if (!id) return null;
    setCrashSummary(null);
    setCrashError(null);
    setCrashOpen(true);
    startCrash();
    try {
      const summary = await api.crash(id, true);
      setCrashSummary(summary);
      judge({ outcome: null, crash: summary, state, earlier: () => undefined });
      return summary;
    } catch (e) {
      setCrashError(e instanceof ApiError ? e.message : "the crash demo failed");
      clearCrash();
      return null;
    } finally {
      void refresh();
    }
  }, [refresh, startCrash, clearCrash, judge, state]);

  const closeCrash = useCallback(() => {
    setCrashOpen(false);
    clearCrash();
  }, [clearCrash]);

  const closeIntro = useCallback(() => {
    setIntroOpen(false);
    writeStorage(local, INTRO_KEY, "1");
  }, []);

  const owners = useMemo(() => {
    const map = new Map<number, { tree: string; color: number; leaf: boolean }>();
    state?.trees.forEach((tree, i) => {
      const color = tree.kind === "catalog" ? 7 : (i - 1) % 7;
      for (const node of tree.nodes) map.set(node.page, { tree: tree.name, color, leaf: node.leaf });
    });
    return map;
  }, [state]);

  // The doctor's error span, mapped into the editor text (a rerun is the tail of the script).
  const doctor = runError?.error.body.doctor;
  const errorOffset = runError && playSql.endsWith(runError.sql) ? playSql.length - runError.sql.length : -1;
  const errorSpan =
    doctor?.pos != null && errorOffset >= 0
      ? { pos: errorOffset + doctor.pos, end: errorOffset + (doctor.end ?? doctor.pos + 1) }
      : null;

  if (startError && !state) {
    return (
      <main className="centered">
        <div className="glass start-error" role="alert">
          <h1>Arcanum</h1>
          <p>Could not start a session: {startError}.</p>
          <button className="button primary" onClick={() => void startSession(false)}>
            Try again
          </button>
        </div>
      </main>
    );
  }

  const stage =
    state && view === "3d" ? (
      <Suspense fallback={<div className="stage-loading">Loading the 3D view…</div>}>
        <Vault
          trees={state.trees}
          selected={selectedTree}
          onSelect={setSelectedTree}
          feed={feed}
          flashMs={flashMs}
          state={state}
          owners={owners}
          onUnavailable={use2d}
        />
      </Suspense>
    ) : state ? (
      <TreeView trees={state.trees} selected={selectedTree} onSelect={setSelectedTree} nodeFlash={feed.nodeFlash} flashMs={flashMs} />
    ) : (
      <div className="stage-loading" aria-busy="true">
        Opening your database…
      </div>
    );

  return (
    <>
      <div className="backdrop" aria-hidden="true" />
      <TopBar
        mode={mode}
        onMode={setMode}
        view={view}
        onView={setView}
        speed={speed}
        onSpeed={setSpeed}
        connection={connection}
        inTransaction={state?.in_transaction ?? false}
        onCrash={() => void crash()}
        crashDisabled={!sessionId || crashOpen}
        theme={theme}
        onTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
        onAbout={() => setIntroOpen(true)}
      />
      <div className="shell">
        {mode === "learn" ? (
          <LessonPanel
            progress={progress}
            onOpen={open}
            onPassed={markStep}
            run={(sql: string, step: Step) => runQuery(sql, step.focus)}
            crash={crash}
            state={state}
            busy={!state || crashOpen}
            onTryInPlayground={(sql) => {
              setPlaySql(sql);
              setMode("play");
            }}
          />
        ) : (
          <PlayPanel
            sql={playSql}
            onSql={setPlaySql}
            onRun={(sql) => runQuery(sql)}
            maxSqlBytes={state?.limits.max_sql_bytes ?? null}
            disabled={!state || measuring}
            completed={progress.challenges}
            tables={state?.tables ?? []}
            error={errorSpan}
          />
        )}
        <main className="stage glass" aria-label="The database">
          {stage}
          <StoryBar outcome={outcome} onExplain={() => setTab("explain")} />
        </main>
        <Inspector
          tab={tab}
          onTab={setTab}
          outcome={outcome}
          error={runError}
          state={state}
          feed={feed}
          flashMs={flashMs}
          pending={pending}
          onSkip={skip}
          owners={owners}
          comparison={comparison}
          onApply={applyFix}
          onFix={fixError}
          busy={!state || measuring || crashOpen}
        />
      </div>
      {introOpen && (
        <Intro
          onClose={closeIntro}
          onCourse={() => {
            setMode("learn");
            open(null);
            closeIntro();
          }}
          onPlay={() => {
            setMode("play");
            closeIntro();
          }}
          onCrash={() => {
            closeIntro();
            void crash();
          }}
        />
      )}
      {crashOpen && <CrashOverlay phase={feed.crash} summary={crashSummary} error={crashError} onClose={closeCrash} />}
      <Toasts toasts={toasts} onDismiss={dismissToast} />
    </>
  );
}
