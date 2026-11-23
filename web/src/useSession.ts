// The visitor's database session: created on first visit, resumed in the same tab, replaced when the
// server has forgotten it. Also keeps the log of statements that changed the database, which a share
// link replays on a fresh session.
import { useCallback, useRef, useState } from "react";
import { api, ApiError, type EngineState, type SessionName } from "./api";
import { readJson, readStorage, tab, writeStorage } from "./storage";

const SESSION_KEY = "arcanum.session";
const SETUP_KEY = "arcanum.setup"; // statements that changed this session's database, for share links
const MAX_SETUP = 200;

/** Statements a share link must replay: the ones that change the database or a transaction. */
export const CHANGES = /^(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)/;

export interface SetupLog {
  session: string | null;
  statements: [SessionName, string][];
  /** Whether changes are missing: the log was full, or data came from an imported file. */
  full: boolean;
}

function isSetupLog(value: unknown, session: string): value is SetupLog {
  const log = value as SetupLog | null;
  return !!log && log.session === session && Array.isArray(log.statements);
}

export function useSession() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [state, setState] = useState<EngineState | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const idRef = useRef<string | null>(null);
  const setup = useRef<SetupLog>({ session: null, statements: [], full: false });
  const refreshing = useRef<Promise<void> | null>(null);
  const refreshAgain = useRef(false);
  const starting = useRef<Promise<void> | null>(null);

  /** Fetch the engine's state again; bursts of calls coalesce into one follow-up request. */
  const refresh = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;
    if (refreshing.current) {
      refreshAgain.current = true;
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

  const open = useCallback(async (forceNew: boolean) => {
    setStartError(null);
    const previous = readStorage(tab, SESSION_KEY);
    if (forceNew && previous) {
      // The tab is starting over: free its old database now (visitors may only hold a few at once).
      writeStorage(tab, SESSION_KEY, null);
      await api.deleteSession(previous).catch(() => undefined);
    }
    const saved = forceNew ? null : previous;
    if (saved) {
      try {
        const current = await api.state(saved);
        const stored = readJson(tab, SETUP_KEY);
        setup.current = isSetupLog(stored, saved) ? stored : { session: saved, statements: [], full: false };
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
      writeStorage(tab, SESSION_KEY, created.id);
      setup.current = { session: created.id, statements: [], full: false };
      writeStorage(tab, SETUP_KEY, JSON.stringify(setup.current));
      idRef.current = created.id;
      setSessionId(created.id);
      setState(await api.state(created.id));
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : "the server cannot be reached");
    }
  }, []);

  /** Open (or resume) the session. One start at a time: a second call joins the first. */
  const start = useCallback(
    (forceNew: boolean) => {
      starting.current ??= open(forceNew).finally(() => {
        starting.current = null;
      });
      return starting.current;
    },
    [open],
  );

  /** The server no longer knows this session (it expired or restarted): start a new one. */
  const lost = useCallback(() => {
    writeStorage(tab, SESSION_KEY, null);
    return start(true);
  }, [start]);

  /** Note statements that changed the database, for share links. */
  const remember = useCallback((statements: [SessionName, string][]) => {
    const log = setup.current;
    for (const statement of statements) {
      if (log.statements.length >= MAX_SETUP) log.full = true;
      else log.statements.push(statement);
    }
    writeStorage(tab, SETUP_KEY, JSON.stringify(log));
  }, []);

  /** Note a change a share link cannot replay (rows imported from a file). */
  const unreplayable = useCallback(() => {
    setup.current.full = true;
    writeStorage(tab, SETUP_KEY, JSON.stringify(setup.current));
  }, []);

  /** Run a share link's setup statements on this session. Returns how many failed. */
  const replay = useCallback(
    async (statements: [SessionName, string][]) => {
      const id = idRef.current;
      if (!id) return statements.length;
      let failed = 0;
      for (const [who, sql] of statements) {
        try {
          const reply = await api.query(id, sql, who);
          if (reply.waiting) {
            // Waiting for a lock the replay itself took cannot end on its own: give the statement up,
            // so the session is free for the next one.
            await api.cancel(id, reply.waiting.ticket).catch(() => undefined);
            failed++;
          }
        } catch {
          failed++;
        }
      }
      remember(statements);
      void refresh();
      return failed;
    },
    [refresh, remember],
  );

  return { sessionId, idRef, state, setState, startError, start, lost, refresh, remember, unreplayable, replay, setup };
}
