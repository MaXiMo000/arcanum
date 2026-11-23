import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Comparison } from "./advisor/Advisor";
import {
  api,
  ApiError,
  type AdviceFix,
  type CrashSummary,
  type EngineState,
  type ImportRequest,
  type QueryReply,
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
import { Labs } from "./components/Labs";
import { TopBar, type Mode, type View } from "./components/TopBar";
import { EngineMap, type MapFocus } from "./components/EngineMap";
import { EXAMPLES } from "./examples";
import { FLASH_MS, replayFrame, useEngineFeed } from "./feed";
import { CHALLENGES } from "./learn/challenges";
import { LessonPanel } from "./learn/LessonPanel";
import { LESSONS, type InspectorTab, type Step } from "./learn/lessons";
import { plural, type CheckContext, type Outcome, type Waiting } from "./learn/outcome";
import { PlayPanel, SECOND_SESSION_SQL } from "./learn/PlayPanel";
import { MAX_LINK, clearShared, readShared, shareLink, type SharedState } from "./share";
import { useProgress } from "./learn/progress";
import type { PageOwner } from "./format";
import type { StageProps } from "./stage";
import { local, readStorage, writeStorage } from "./storage";
import { CHANGES, useSession } from "./useSession";
import { useClassroom } from "./useClassroom";
import { usePlanHistory } from "./flow/planHistory";
import { ClassDialog } from "./components/ClassDialog";
import { ClassFeed } from "./components/ClassFeed";

const THEME_KEY = "arcanum.theme";
const VIEW_KEY = "arcanum.view.v2"; // v2: the engine map became the default view
const MODE_KEY = "arcanum.mode";
const INTRO_KEY = "arcanum.intro-seen";
const EXPLAIN_PREFIX = /^\s*EXPLAIN\s+(ANALYZE\s+)?/i;
const SCHEMA_CHANGE = /^\s*(CREATE|DROP|ALTER)\b/i; // a fix for one of these runs once, with no before/after
const WAIT_POLL_MS = 400; // how often a waiting statement's ticket is checked

// The 3D view (three.js) is a separate download, fetched the first time someone opens it.
const Vault = lazy(() => import("./vault/Vault"));

/** The tree a query read first (an index scan reads the index's own tree), so the map can follow it. */
function treeRead(results: QueryResult[]): string | null {
  for (const result of results) {
    const stack = result.plan ? [result.plan] : [];
    while (stack.length) {
      const node = stack.shift()!;
      if (node.operator.startsWith("TableScan")) return node.label.replace(/^TableScan(Backward)? /, "");
      const m = /^(?:IndexScan|IndexOnlyScan)(?:Backward)? (\S+) using (.+?) \(/.exec(node.label);
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

export default function App() {
  const { sessionId, idRef, state, setState, startError, start: startSession, lost: onSessionGone, refresh, remember, unreplayable, replay: replaySetup, setup } =
    useSession();
  const [selectedTree, setSelectedTree] = useState<string | null>(null); // set to the first table on load
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [runError, setRunError] = useState<RunError | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [tab, setTab] = useState<InspectorTab>("explain");
  const [playSql, setPlaySql] = useState(EXAMPLES[0]!.sql);
  const [playSqlB, setPlaySqlB] = useState(SECOND_SESSION_SQL);
  const [twoSessions, setTwoSessions] = useState(false);
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
  const classroom = useClassroom();
  const plans = usePlanHistory();
  const [classOpen, setClassOpen] = useState(false);
  // On phones and portrait tablets one part of the shell shows at a time (CSS ignores this when wider).
  const [pane, setPane] = useState<"side" | "stage" | "inspector">("side");
  const publish = useRef(classroom.publish);
  publish.current = classroom.publish;

  // Teaching: students see which lesson step the teacher is on.
  const { publishLesson } = classroom;
  const teaching = classroom.room?.role === "teacher";
  useEffect(() => {
    if (teaching) void publishLesson(mode === "learn" && progress.lesson ? { id: progress.lesson, step: progress.step } : null);
  }, [teaching, mode, progress.lesson, progress.step, publishLesson]);

  // Following in lockstep: the lesson panel moves with the teacher's.
  const following = classroom.room?.role === "student" ? classroom.room : null;
  const teacherLesson = following?.lockstep && following.lesson ? following.lesson : null;
  useEffect(() => {
    if (!teacherLesson || !LESSONS.some((l) => l.id === teacherLesson.id)) return;
    setMode("learn");
    open(teacherLesson.id, teacherLesson.step);
    setPane("side");
  }, [teacherLesson?.id, teacherLesson?.step, open]); // eslint-disable-line react-hooks/exhaustive-deps -- the step, not the object

  // A join link (?class=CODE) follows that class, once; the parameter is then removed from the address.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("class");
    if (!code) return;
    params.delete("class");
    const rest = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${rest ? `?${rest}` : ""}${window.location.hash}`);
    if (/^[A-Za-z0-9]{6}$/.test(code)) classroom.follow(code);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on load
  }, []);

  // Show the first table once the database is there; later tables do not steal the view.
  useEffect(() => {
    if (state) setSelectedTree((name) => name ?? firstTable(state));
  }, [state]);

  const outcomeIds = useRef(0);
  const toastIds = useRef(0);
  const completedRef = useRef(progress.challenges);
  completedRef.current = progress.challenges;

  /** Open a shared link: replay its statements on this fresh database, then fill in the playground. */
  const openShared = useCallback(
    async (shared: SharedState) => {
      const failed = await replaySetup(shared.setup);
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
    [replaySetup],
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
    async (
      sql: string,
      focus?: InspectorTab,
      session: SessionName = "A",
      /** Sends the work instead of running `sql` (a CSV import); `sql` then only describes it. */
      send?: (id: string) => Promise<QueryReply>,
    ): Promise<Outcome | ApiError | Waiting | null> => {
      const id = idRef.current;
      if (!id) return null;
      setRunError(null);
      setComparison(null);

      const fail = (e: unknown): ApiError => {
        if (e instanceof ApiError && e.status === 404) onSessionGone();
        const error = e instanceof ApiError ? e : new ApiError(0, { type: "Error", message: String(e) });
        setRunError({ error, sql });
        setPane((p) => (p === "stage" ? "inspector" : p)); // the doctor's diagnosis is in the inspector
        void publish.current(sql, [], `${error.body.type}: ${error.message}`);
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
        void publish.current(sql, results);
        plans.record(next);
        if (results.some((r) => CHANGES.test(r.status))) {
          if (send) unreplayable();
          else remember([[session, sql]]);
        }
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
        let reply = await (send ? send(id) : api.query(id, sql, session));
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
              if (cancelled.current.delete(ticket)) reply = await api.cancel(id, ticket);
              else reply = await api.ticket(id, ticket);
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
    [mode, judge, onSessionGone, refresh, remember, unreplayable, plans.record],
  );

  const runQuery = useCallback(
    async (sql: string, focus?: InspectorTab, session: SessionName = "A"): Promise<Outcome | null> => {
      let result = await execute(sql, focus, session);
      if (result && "waiting" in result) result = await result.done;
      return result instanceof ApiError ? null : result;
    },
    [execute],
  );

  const importCsv = useCallback(
    (request: ImportRequest, session: SessionName) =>
      execute(`-- CSV import into ${request.table}`, "results", session, (id) => api.importCsv(id, request, session)),
    [execute],
  );

  /** Run the advisor's fix, measuring the statement before and after it. */
  const applyFix = useCallback(
    async (fix: AdviceFix, result: QueryResult) => {
      const statement = result.statement.replace(EXPLAIN_PREFIX, "");
      setMeasuring(true);
      try {
        if (SCHEMA_CHANGE.test(statement)) {
          await runQuery(fix.sql, "advisor"); // a fix for a schema change (DROP INDEX): nothing to re-run and compare
          return;
        }
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

  const runReported = useCallback((sql: string) => void runQuery(sql, "advisor"), [runQuery]);

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
    const map = new Map<number, PageOwner>();
    state?.trees.forEach((tree, i) => {
      const color = tree.kind === "catalog" ? 7 : (i - 1) % 7;
      for (const node of tree.nodes) map.set(node.page, { tree: tree.name, color, leaf: node.leaf });
      for (const page of tree.toast_pages ?? []) map.set(page, { tree: tree.name, color, leaf: false, overflow: true });
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

  const stageProps: StageProps | null = state && {
    trees: state.trees,
    selected: selectedTree,
    onSelect: setSelectedTree,
    feed: shown,
    flashMs,
    state,
    owners,
  };
  const stage =
    view === "lab" ? (
      <Labs sessionId={sessionId} limits={state?.limits.labs ?? null} />
    ) : stageProps && view === "3d" ? (
      <Suspense fallback={<div className="stage-loading">Loading the 3D view…</div>}>
        <Vault {...stageProps} onUnavailable={use2d} />
      </Suspense>
    ) : stageProps ? (
      <EngineMap {...stageProps} focus={mapFocus} marker={replay?.marker ?? null} />
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
        classLabel={
          classroom.room?.role === "teacher"
            ? `Teaching ${classroom.room.code} · ${classroom.room.students}${classroom.room.questions.length ? ` · ${classroom.room.questions.length} ?` : ""}`
            : classroom.room?.role === "student"
              ? `Class ${classroom.room.code}`
              : null
        }
        onClass={() => setClassOpen(true)}
      />
      {classOpen && (
        <ClassDialog
          room={classroom.room}
          error={classroom.error}
          onTeach={classroom.teach}
          onFollow={classroom.follow}
          onLeave={classroom.leave}
          onDismiss={classroom.dismiss}
          onClose={() => setClassOpen(false)}
        />
      )}
      <div className="shell" data-pane={pane}>
        <div className="side-column">
        {classroom.room?.role === "student" && (
          <ClassFeed
            room={classroom.room}
            busy={!state || measuring}
            onOpen={(sql) => {
              setPlaySql(sql);
              setMode("play");
            }}
            onRun={(sql) => {
              setPlaySql(sql);
              setMode("play");
              setPane("stage");
              void runQuery(sql);
            }}
            onLeave={() => void classroom.leave()}
            onAsk={classroom.ask}
            onLockstep={classroom.setLockstep}
            onGoTo={(lesson) => {
              setMode("learn");
              open(lesson.id, lesson.step);
            }}
          />
        )}
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
            onRun={(sql, session) => {
              setPane("stage"); // on a phone, show the engine at work
              return execute(sql, undefined, session);
            }}
            onCancel={cancelWait}
            two={twoSessions}
            onTwo={setTwoSessions}
            sqlB={playSqlB}
            onSqlB={setPlaySqlB}
            onShare={() => void share()}
            sessions={state?.sessions ?? null}
            maxSqlBytes={state?.limits.max_sql_bytes ?? null}
            sessionId={sessionId}
            onImport={importCsv}
            maxImportBytes={state?.limits.max_import_bytes ?? null}
            disabled={!state || measuring}
            completed={progress.challenges}
            tables={state?.tables ?? []}
            error={errorSpan}
          />
        )}
        </div>
        <main className="stage glass" aria-label="The database">
          {stage}
          <StoryBar
            outcome={outcome}
            onExplain={() => {
              setTab("explain");
              setPane("inspector");
            }}
          />
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
          onRun={runReported}
          report={state?.indexes ?? null}
          planHistory={plans.history}
          onFix={fixError}
          busy={!state || measuring || crashOpen}
        />
      </div>
      <nav className="pane-nav" aria-label="Show">
        {(
          [
            ["side", mode === "learn" ? "Lesson" : "SQL", "✎"],
            ["stage", view === "lab" ? "Labs" : "Database", "◈"],
            ["inspector", "Details", "☰"],
          ] as const
        ).map(([id, label, icon]) => (
          <button key={id} className={pane === id ? "active" : ""} aria-pressed={pane === id} onClick={() => {
            setPane(id);
            window.scrollTo({ top: 0 });
          }}>
            <span aria-hidden="true">{icon}</span>
            {label}
            {id === "inspector" && runError && <i className="pane-alert" aria-label="an error to read" />}
          </button>
        ))}
      </nav>
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
