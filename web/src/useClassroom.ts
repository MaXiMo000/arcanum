// Classroom mode: teach (every statement you run is posted to the classroom) or follow (a teacher's
// statements and results arrive live). Kept for this tab, so a reload resumes it.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ClassPost, type QueryResult } from "./api";
import { readJson, tab, writeStorage } from "./storage";

const CLASS_KEY = "arcanum.classroom";
const STUDENTS_POLL_MS = 15_000; // how often a teacher's student count refreshes
const RETRIES = 5; // reconnect attempts after a dropped connection, with growing waits
const MAX_POSTS = 50;
const PREVIEW_ROWS = 20; // rows of each result sent to students
const MAX_CELL = 200; // characters of each value sent to students

export interface Teaching {
  role: "teacher";
  code: string;
  token: string;
  title: string;
  students: number;
}

export interface Following {
  role: "student";
  code: string;
  title: string;
  posts: ClassPost[];
  status: "connecting" | "live" | "ended" | "lost" | "unknown";
}

export type Classroom = Teaching | Following | null;

function saved(): Classroom {
  const value = readJson(tab, CLASS_KEY) as { role?: string; code?: string; token?: string; title?: string } | null;
  if (value?.role === "teacher" && value.code && value.token)
    return { role: "teacher", code: value.code, token: value.token, title: value.title ?? "", students: 0 };
  if (value?.role === "student" && value.code)
    return { role: "student", code: value.code, title: "", posts: [], status: "connecting" };
  return null;
}

function store(room: Classroom) {
  writeStorage(tab, CLASS_KEY, room && JSON.stringify(room.role === "teacher"
    ? { role: room.role, code: room.code, token: room.token, title: room.title }
    : { role: room.role, code: room.code }));
}

/** A result as students see it: the first rows, long values shortened. */
function summarize(result: QueryResult) {
  return {
    status: result.status.slice(0, 100),
    columns: result.columns.slice(0, 30).map((c) => c.slice(0, 100)),
    rows: result.rows.slice(0, PREVIEW_ROWS).map((row) =>
      row.slice(0, 30).map((v) => (typeof v === "string" && v.length > MAX_CELL ? `${v.slice(0, MAX_CELL - 1)}…` : v)),
    ),
    row_total: result.row_total,
  };
}

export function useClassroom() {
  const [room, setRoom] = useState<Classroom>(saved);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0); // a new join reconnects even to the same code
  const roomRef = useRef(room);
  roomRef.current = room;

  const update = useCallback((next: Classroom) => {
    setRoom(next);
    store(next);
  }, []);

  const teach = useCallback(
    async (title: string) => {
      setError(null);
      try {
        const opened = await api.openClass(title);
        update({ role: "teacher", code: opened.code, token: opened.token, title: opened.title, students: 0 });
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      }
    },
    [update],
  );

  const follow = useCallback(
    (code: string) => {
      setError(null);
      update({ role: "student", code: code.trim().toUpperCase(), title: "", posts: [], status: "connecting" });
      setAttempt((a) => a + 1);
    },
    [update],
  );

  const leave = useCallback(async () => {
    const current = roomRef.current;
    update(null);
    if (current?.role === "teacher") await api.endClass(current.code, current.token).catch(() => undefined);
  }, [update]);

  /** Post what the teacher just ran; students see it at once. */
  const publish = useCallback(async (sql: string, results: QueryResult[], failure?: string) => {
    const current = roomRef.current;
    if (current?.role !== "teacher") return;
    try {
      const reply = await api.postClass(current.code, current.token, {
        sql: sql.slice(0, 20_000),
        results: results.slice(0, 10).map(summarize),
        error: failure ? failure.slice(0, 500) : null,
      });
      setRoom((r) => (r?.role === "teacher" ? { ...r, students: reply.students } : r));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        update(null); // the classroom has gone (the server restarted, or it was ended elsewhere)
        setError("The classroom has closed.");
      }
    }
  }, [update]);

  // Teacher: keep the student count fresh.
  const teachingCode = room?.role === "teacher" ? room.code : null;
  useEffect(() => {
    if (!teachingCode) return;
    const poll = () =>
      api
        .getClass(teachingCode)
        .then((c) => setRoom((r) => (r?.role === "teacher" ? { ...r, students: c.students } : r)))
        .catch((e) => e instanceof ApiError && e.status === 404 && update(null));
    void poll();
    const timer = window.setInterval(poll, STUDENTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [teachingCode, update]);

  // Student: a live connection, reconnected a few times if it drops.
  const followingCode = room?.role === "student" ? room.code : null;
  useEffect(() => {
    if (!followingCode) return;
    let socket: WebSocket | null = null;
    let tries = 0;
    let timer: number | undefined;
    let stopped = false;
    const set = (patch: Partial<Following>) =>
      setRoom((r) => (r?.role === "student" && r.code === followingCode ? { ...r, ...patch } : r));

    const connect = () => {
      socket = new WebSocket(api.classLiveUrl(followingCode));
      socket.onmessage = (message) => {
        const data = JSON.parse(message.data as string);
        if (data.type === "joined") {
          tries = 0;
          set({ title: data.title, posts: data.history, status: "live" });
        } else if (data.type === "post") {
          setRoom((r) =>
            r?.role === "student" && r.code === followingCode ? { ...r, posts: [...r.posts, data.post].slice(-MAX_POSTS) } : r,
          );
        }
      };
      socket.onclose = (event) => {
        if (stopped) return;
        if (event.code === 4410) return set({ status: "ended" });
        if (event.code === 4404) return set({ status: "unknown" });
        if (tries >= RETRIES) return set({ status: "lost" });
        set({ status: "connecting" });
        timer = window.setTimeout(connect, 1000 * 2 ** tries++);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, [followingCode, attempt]);

  return { room, error, teach, follow, leave, publish };
}
