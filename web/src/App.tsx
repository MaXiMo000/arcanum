import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type CrashSummary, type EngineState } from "./api";
import { CrashOverlay } from "./components/CrashOverlay";
import { EventLog } from "./components/EventLog";
import { Header } from "./components/Header";
import { PageMap } from "./components/PageMap";
import { QueryPanel } from "./components/QueryPanel";
import { TreeView } from "./components/TreeView";
import { WalTape } from "./components/WalTape";
import { FLASH_MS, useEngineFeed } from "./feed";

const SESSION_KEY = "arcanum.session";
const THEME_KEY = "arcanum.theme";

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

const session = () => window.sessionStorage;
const local = () => window.localStorage;

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [state, setState] = useState<EngineState | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [selectedTree, setSelectedTree] = useState<string | null>(null); // null: the first table
  const [crashOpen, setCrashOpen] = useState(false);
  const [crashSummary, setCrashSummary] = useState<CrashSummary | null>(null);
  const [crashError, setCrashError] = useState<string | null>(null);
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const saved = readStorage(local, THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  const idRef = useRef<string | null>(null);
  const refreshing = useRef<Promise<void> | null>(null);
  const refreshAgain = useRef(false);
  const starting = useRef<Promise<void> | null>(null);

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
      setState(await api.state(created.id));
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

  const runQuery = useCallback(
    async (sql: string) => {
      const id = idRef.current;
      if (!id) throw new ApiError(0, { type: "NoSession", message: "no session yet" });
      try {
        const { results } = await api.query(id, sql);
        return results;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) onSessionGone();
        throw e;
      } finally {
        void refresh();
      }
    },
    [refresh, onSessionGone],
  );

  const crash = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    setCrashSummary(null);
    setCrashError(null);
    setCrashOpen(true);
    startCrash();
    try {
      setCrashSummary(await api.crash(id, true));
    } catch (e) {
      setCrashError(e instanceof ApiError ? e.message : "the crash demo failed");
      clearCrash();
    } finally {
      void refresh();
    }
  }, [refresh, startCrash, clearCrash]);

  const closeCrash = useCallback(() => {
    setCrashOpen(false);
    clearCrash();
  }, [clearCrash]);

  const owners = useMemo(() => {
    const map = new Map<number, { tree: string; color: number; leaf: boolean }>();
    state?.trees.forEach((tree, i) => {
      const color = tree.kind === "catalog" ? 7 : (i - 1) % 7;
      for (const node of tree.nodes) map.set(node.page, { tree: tree.name, color, leaf: node.leaf });
    });
    return map;
  }, [state]);

  if (startError && !state) {
    return (
      <main className="centered">
        <div className="panel start-error" role="alert">
          <h1>Arcanum</h1>
          <p>Could not start a session: {startError}.</p>
          <button className="button primary" onClick={() => void startSession(false)}>
            Try again
          </button>
        </div>
      </main>
    );
  }

  return (
    <>
      <Header
        speed={speed}
        onSpeed={setSpeed}
        connection={connection}
        inTransaction={state?.in_transaction ?? false}
        onCrash={() => void crash()}
        crashDisabled={!sessionId || crashOpen}
        theme={theme}
        onTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
      />
      <main className="workspace">
        <div className="column left">
          <QueryPanel disabled={!state} maxSqlBytes={state?.limits.max_sql_bytes ?? null} onRun={runQuery} />
        </div>
        <div className="column center">
          {state ? (
            <TreeView
              trees={state.trees}
              selected={selectedTree}
              onSelect={setSelectedTree}
              nodeFlash={feed.nodeFlash}
              flashMs={flashMs}
            />
          ) : (
            <section className="panel tree-panel loading" aria-busy="true">
              Opening a database…
            </section>
          )}
        </div>
        <div className="column right">
          {state && (
            <PageMap
              state={state}
              pageFlash={feed.pageFlash}
              flashMs={flashMs}
              owners={owners}
              treeNames={state.trees.filter((t) => t.kind !== "catalog").map((t) => t.name)}
            />
          )}
          <WalTape
            frames={feed.wal}
            committedOnDisk={state?.wal.committed_frames ?? 0}
            checkpointAt={feed.checkpointAt}
            flashMs={flashMs}
          />
          <EventLog log={feed.log} counters={feed.counters} pending={pending} onSkip={skip} />
        </div>
      </main>
      {crashOpen && <CrashOverlay phase={feed.crash} summary={crashSummary} error={crashError} onClose={closeCrash} />}
    </>
  );
}
