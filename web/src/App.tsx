import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Comparison } from "./advisor/Advisor";
import {
  api,
  ApiError,
  type AdviceFix,
  type CrashSummary,
  type EngineState,
  type QueryResult,
  type QueryStats,
  type SessionName,
} from "./api";
import { CrashOverlay } from "./components/CrashOverlay";
import { Inspector } from "./components/Inspector";
import { Intro } from "./components/Intro";
import type { RunError } from "./components/Results";
import { StoryBar } from "./components/StoryBar";
import { Timeline, replayMarker } from "./components/Timeline";
import { Toasts, type Toast } from "./components/Toasts";
import { EngineLab } from "./components/EngineLab";
import { TopBar, type Mode, type View } from "./components/TopBar";
import { EngineMap, type MapFocus } from "./components/EngineMap";
import { EXAMPLES } from "./examples";
import { FLASH_MS, replayFrame, useEngineFeed } from "./feed";
import { CHALLENGES } from "./learn/challenges";
import { LessonPanel } from "./learn/LessonPanel";
import type { InspectorTab, Step } from "./learn/lessons";
import { plural, type CheckContext, type Outcome, type Waiting } from "./learn/outcome";
import { PlayPanel, SECOND_SESSION_SQL } from "./learn/PlayPanel";
import { MAX_LINK, clearShared, readShared, shareLink, type SharedState } from "./share";
import { useProgress } from "./learn/progress";

const SESSION_KEY = "arcanum.session";
const THEME_KEY = "arcanum.theme";
const VIEW_KEY = "arcanum.view.v2"; // v2: the engine map became the default view
const MODE_KEY = "arcanum.mode";
const INTRO_KEY = "arcanum.intro-seen";
const SETUP_KEY = "arcanum.setup"; // statements that changed this session's database, for share links
const MAX_SETUP = 200;
// Statuses of statements that change the database (or a transaction), which a shared link replays.
const CHANGES = /^(INSERT|UPDATE|DELETE|CREATE|DROP|ANALYZE|BEGIN|COMMIT|ROLLBACK)/;
const EXPLAIN_PREFIX = /^\s*EXPLAIN\s+(ANALYZE\s+)?/i;
const WAIT_POLL_MS = 400; // how often a waiting statement's ticket is checked

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

/** The tree a query read first (an index scan reads the index's own tree), so the map can follow it. */
function treeRead(results: QueryResult[]): string | null {
  for (const result of results) {
    const stack = result.plan ? [result.plan] : [];
    while (stack.length) {
      const node = stack.shift()!;
      if (node.operator === "TableScan") return node.label.replace(/^TableScan /, "");
      const m = /^(?:IndexScan|IndexOnlyScan) (\S+) using (.+?) \(/.exec(node.label);
      if (m) return m[2] === "primary key" ? m[1]! : m[2]!;
      stack.push(...node.children);
    }
  }
  return null;
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
  const [playSqlB, setPlaySqlB] = useState(SECOND_SESSION_SQL);
  const [twoSessions, setTwoSessions] = useState(false);
  const setup = useRef<{ session: string | null; statements: [SessionName, string][]; full: boolean }>({
    session: null,
    statements: [],
    full: false,
  });
  const [crashOpen, setCrashOpen] = useState(false);
  const [crashSummary, setCrashSummary] = useState<CrashSummary | null>(null);
  const [crashError, setCrashError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [introOpen, setIntroOpen] = useState(() => readStorage(local, INTRO_KEY) === null);
  const [mode, setMode] = useState<Mode>(() => (readStorage(local, MODE_KEY) === "play" ? "play" : "learn"));
  const [view, setView] = useState<View>(() => {
    const saved = readStorage(local, VIEW_KEY);
    return saved === "3d" || saved === "lab" ? saved : "2d";
  });
  const [mapFocus, setMapFocus] = useState<MapFocus>(null);
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
    const previous = readStorage(session, SESSION_KEY);
    if (forceNew && previous) {
      // The tab is starting over: free its old database now (visitors may only hold a few at once).
      writeStorage(session, SESSION_KEY, null);
      await api.deleteSession(previous).catch(() => undefined);
    }
    const saved = forceNew ? null : previous;
    if (saved) {
      try {
        const current = await api.state(saved);
        idRef.current = saved;
        try {
          const stored = JSON.parse(readStorage(session, SETUP_KEY) ?? "null");
          if (stored?.session === saved && Array.isArray(stored.statements)) setup.current = stored;
        } catch {
          // A damaged log only means a share link without setup.
        }
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
      setup.current = { session: created.id, statements: [], full: false };
      writeStorage(session, SETUP_KEY, JSON.stringify(setup.current));
      idRef.current = created.id;
      setSessionId(created.id);
      const fresh = await api.state(created.id);
      setState(fresh);
      setSelectedTree((name) => name ?? firstTable(fresh));
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : "the server cannot be reached");
    }
  }

  const remember = useCallback((statements: [SessionName, string][]) => {
    const log = setup.current;
    for (const statement of statements) {
      if (log.statements.length >= MAX_SETUP) log.full = true;
      else log.statements.push(statement);
    }
    writeStorage(session, SETUP_KEY, JSON.stringify(log));
  }, []);

  /** Open a shared link: replay its statements on this fresh database, then fill in the playground. */
  const openShared = useCallback(
    async (shared: SharedState) => {
      const id = idRef.current;
      if (!id) return;
      let failed = 0;
      for (const [who, sql] of shared.setup) {
        try {
          await api.query(id, sql, who);
        } catch {
          failed++;
        }
      }
      remember(shared.setup);
      void refresh();
      setPlaySql(shared.sql);
      setPlaySqlB(shared.sqlB);
      setTwoSessions(shared.two);
      setMode("play");
      setToasts((all) => [
        ...all,
        {
          id: ++toastIds.current,
          title: "Opened a shared query",
          body: shared.setup.length
            ? `${shared.setup.length - failed} of ${shared.setup.length} setup statements ran on your own fresh database. Press Run when ready.`
            : "On your own fresh database. Press Run when ready.",
        },
      ]);
    },
    [refresh, remember],
  );

  useEffect(() => {
    void (async () => {
      const shared = await readShared();
      if (!shared) {
        await startSession(false);
        return;
      }
      clearShared();
      await startSession(true); // a fresh database, so the setup replays onto exactly what the sharer started from
      await openShared(shared);
    })();
  }, [startSession, openShared]);

  const share = useCallback(async () => {
    const log = setup.current;
    let body: string;
    try {
      const link = await shareLink({ sql: playSql, sqlB: playSqlB, two: twoSessions, setup: log.statements });
      if (link.length > MAX_LINK) {
        body = "This database has changed too much to fit in a link. Share the query alone from a fresh session.";
      } else {
        await navigator.clipboard.writeText(link);
        body = log.full
          ? "Copied. Only your first changes fit; the database it opens may differ."
          : `Copied. It opens this query on a fresh database${log.statements.length ? ` after replaying your ${plural(log.statements.length, "change")}` : ""}.`;
      }
    } catch {
      body = "The link could not be copied. Your browser may block clipboard access here.";
    }
    setToasts((all) => [...all, { id: ++toastIds.current, title: "Share link", body }]);
  }, [playSql, playSqlB, twoSessions]);

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

  const { feed, pending, speed, setSpeed, skip, connection, clearCrash, startCrash, syncPool } = useEngineFeed(
    sessionId,
    onSessionGone,
    refresh,
  );
  const flashMs = FLASH_MS[speed];

  useEffect(() => {
    if (state) syncPool(state.pager.cached, state.pager.pool_size);
  }, [state, syncPool]);

  // Time travel: the stage can show the engine as it was at any event of the latest request.
  const [replayIndex, setReplayIndex] = useState<number | null>(null);
  const [replaying, setReplaying] = useState(false);
  const recording = feed.recording;
  const request = recording?.request;
  useEffect(() => {
    setReplayIndex(null);
    setReplaying(false);
  }, [request]);
  const replay = useMemo(
    () =>
      recording && replayIndex !== null
        ? { feed: replayFrame(recording, replayIndex, flashMs / 6), marker: replayMarker(recording, replayIndex) }
        : null,
    [recording, replayIndex, flashMs],
  );
  const shown = replay?.feed ?? feed;

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

  /** Run SQL in one of the two database sessions; the outcome, or the error it failed with. */
  const cancelled = useRef(new Set<string>());
  const cancelWait = useCallback((ticket: string) => cancelled.current.add(ticket), []);

  const execute = useCallback(
    async (sql: string, focus?: InspectorTab, session: SessionName = "A"): Promise<Outcome | ApiError | Waiting | null> => {
      const id = idRef.current;
      if (!id) return null;
      setRunError(null);
      setComparison(null);

      const fail = (e: unknown): ApiError => {
        if (e instanceof ApiError && e.status === 404) onSessionGone();
        const error = e instanceof ApiError ? e : new ApiError(0, { type: "Error", message: String(e) });
        setRunError({ error, sql });
        setTab("explain");
        void refresh(); // earlier statements of the script, or the transaction, may have changed things
        return error;
      };

      const finish = async ({ results, stats }: { results: QueryResult[]; stats: QueryStats }): Promise<Outcome> => {
        let after: EngineState | null = null;
        try {
          after = await api.state(id);
          setState(after);
        } catch {
          // The outcome still stands without the fresh picture.
        }
        const next: Outcome = { id: ++outcomeIds.current, session, sql, results, stats, state: after };
        if (results.some((r) => CHANGES.test(r.status))) remember([[session, sql]]);
        setOutcome(next);
        const read = treeRead(results);
        if (read && after?.trees.some((t) => t.name === read)) setSelectedTree(read);
        // Pages and the log live on the stage now: point there, and tell the story in the inspector.
        setMapFocus(focus === "pages" ? "pool" : focus === "log" ? "log" : null);
        setTab(focus && focus !== "pages" && focus !== "log" ? focus : mode === "play" ? "results" : "explain");
        judge({ outcome: next, crash: null, state: after, earlier: () => undefined });
        return next;
      };

      try {
        let reply = await api.query(id, sql, session);
        if (!reply.waiting) return await finish(reply);
        // A statement is waiting for another session's transaction: show the lock, then follow the
        // ticket until the holder finishes (or the wait is cancelled or times out).
        void refresh();
        const first = reply.waiting;
        const done = (async () => {
          try {
            while (reply.waiting) {
              await new Promise((resolve) => window.setTimeout(resolve, WAIT_POLL_MS));
              if (idRef.current !== id) return null;
              const ticket = reply.waiting.ticket;
              reply = cancelled.current.has(ticket) ? await api.cancel(id, ticket) : await api.ticket(id, ticket);
            }
            return await finish(reply);
          } catch (e) {
            return fail(e);
          }
        })();
        return { waiting: first, done };
      } catch (e) {
        return fail(e);
      }
    },
    [mode, judge, onSessionGone, refresh, remember],
  );

  const runQuery = useCallback(
    async (sql: string, focus?: InspectorTab, session: SessionName = "A"): Promise<Outcome | null> => {
      let result = await execute(sql, focus, session);
      if (result && "waiting" in result) result = await result.done;
      return result instanceof ApiError ? null : result;
    },
    [execute],
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
    view === "lab" ? (
      <EngineLab sessionId={sessionId} />
    ) : state && view === "3d" ? (
      <Suspense fallback={<div className="stage-loading">Loading the 3D view…</div>}>
        <Vault
          trees={state.trees}
          selected={selectedTree}
          onSelect={setSelectedTree}
          feed={shown}
          flashMs={flashMs}
          state={state}
          owners={owners}
          onUnavailable={use2d}
        />
      </Suspense>
    ) : state ? (
      <EngineMap
        trees={state.trees}
        selected={selectedTree}
        onSelect={setSelectedTree}
        feed={shown}
        flashMs={flashMs}
        state={state}
        owners={owners}
        focus={mapFocus}
        marker={replay?.marker ?? null}
      />
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
        sessions={state?.sessions ?? null}
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
            run={(sql: string, step: Step) => execute(sql, step.focus, step.session)}
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
            onRun={(sql, session) => execute(sql, undefined, session)}
            onCancel={cancelWait}
            two={twoSessions}
            onTwo={setTwoSessions}
            sqlB={playSqlB}
            onSqlB={setPlaySqlB}
            onShare={() => void share()}
            sessions={state?.sessions ?? null}
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
          <Timeline
            recording={recording}
            index={replayIndex}
            onIndex={setReplayIndex}
            playing={replaying}
            onPlaying={setReplaying}
          />
        </main>
        <Inspector
          tab={tab}
          onTab={setTab}
          outcome={outcome}
          error={runError}
          feed={feed}
          pending={pending}
          onSkip={skip}
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
