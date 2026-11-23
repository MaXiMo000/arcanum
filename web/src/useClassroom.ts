// Classroom mode: teach (every statement you run, and the lesson step you are on, is posted to the
// classroom; students' questions come back) or follow (the teacher's statements, results and lesson step
// arrive live). Kept for this tab, so a reload resumes it.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ClassLesson, type ClassPost, type ClassQuestion, type QueryResult } from "./api";
import { readJson, tab, writeStorage } from "./storage";

const CLASS_KEY = "arcanum.classroom";
const STUDENTS_POLL_MS = 10_000; // how often a teacher's student count and questions refresh
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
  questions: ClassQuestion[];
}

export interface Following {
  role: "student";
  code: string;
  title: string;
  posts: ClassPost[];
  status: "connecting" | "live" | "ended" | "lost" | "unknown";
  /** The lesson step the teacher is on. */
  lesson: ClassLesson | null;
  /** Whether this student's lesson panel moves with the teacher's. */
  lockstep: boolean;
}

export type Classroom = Teaching | Following | null;

function saved(): Classroom {
  const value = readJson(tab, CLASS_KEY) as { role?: string; code?: string; token?: string; title?: string } | null;
  if (value?.role === "teacher" && value.code && value.token)
    return { role: "teacher", code: value.code, token: value.token, title: value.title ?? "", students: 0, questions: [] };
  if (value?.role === "student" && value.code) return joining(value.code);
  return null;
}

function joining(code: string): Following {
  return { role: "student", code, title: "", posts: [], status: "connecting", lesson: null, lockstep: true };
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
        update({ role: "teacher", code: opened.code, token: opened.token, title: opened.title, students: 0, questions: [] });
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      }
    },
    [update],
  );

  const follow = useCallback(
    (code: string) => {
      setError(null);
      update(joining(code.trim().toUpperCase()));
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

  /** Tell students which lesson step the teacher is on (null: none); sent only when it changes. */
  const sentLesson = useRef<string | null>(null);
  const publishLesson = useCallback(async (lesson: ClassLesson | null) => {
    const current = roomRef.current;
    const key = lesson ? `${lesson.id}/${lesson.step}` : "";
    if (current?.role !== "teacher" || sentLesson.current === `${current.code}:${key}`) return;
    sentLesson.current = `${current.code}:${key}`;
    await api.classLesson(current.code, current.token, lesson).catch(() => undefined);
  }, []);

  /** A student's question, for the teacher's eyes only. */
  const ask = useCallback(async (text: string, name: string) => {
    const current = roomRef.current;
    if (current?.role !== "student") return "You are not in a class.";
    try {
      await api.askClass(current.code, text, name);
      return null;
    } catch (e) {
      return e instanceof ApiError ? e.message : String(e);
    }
  }, []);

  const dismiss = useCallback(async (id: number) => {
    const current = roomRef.current;
    if (current?.role !== "teacher") return;
    setRoom((r) => (r?.role === "teacher" ? { ...r, questions: r.questions.filter((q) => q.id !== id) } : r));
    await api.dismissQuestion(current.code, current.token, id).catch(() => undefined);
  }, []);

  const setLockstep = useCallback((lockstep: boolean) => {
    setRoom((r) => (r?.role === "student" ? { ...r, lockstep } : r));
  }, []);

  // Teacher: keep the student count and the questions fresh.
  const teachingCode = room?.role === "teacher" ? room.code : null;
  const teachingToken = room?.role === "teacher" ? room.token : undefined;
  useEffect(() => {
    if (!teachingCode) return;
    const poll = () =>
      api
        .getClass(teachingCode, teachingToken)
        .then((c) => setRoom((r) => (r?.role === "teacher" ? { ...r, students: c.students, questions: c.questions ?? [] } : r)))
        .catch((e) => e instanceof ApiError && e.status === 404 && update(null));
    void poll();
    const timer = window.setInterval(poll, STUDENTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [teachingCode, teachingToken, update]);

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

    let closedWith: number | null = null; // the server's reason, sent as a message too (proxies may drop close codes)
    const connect = () => {
      closedWith = null;
      socket = new WebSocket(api.classLiveUrl(followingCode));
      socket.onmessage = (message) => {
        const data = JSON.parse(message.data as string);
        if (data.type === "closed") {
          closedWith = data.code;
        } else if (data.type === "joined") {
          tries = 0;
          set({ title: data.title, posts: data.history, status: "live", lesson: data.lesson });
        } else if (data.type === "lesson") {
          set({ lesson: data.lesson });
        } else if (data.type === "post") {
          setRoom((r) =>
            r?.role === "student" && r.code === followingCode ? { ...r, posts: [...r.posts, data.post].slice(-MAX_POSTS) } : r,
          );
        }
      };
      socket.onclose = (event) => {
        if (stopped) return;
        const code = closedWith ?? event.code;
        if (code === 4410) return set({ status: "ended" });
        if (code === 4404) return set({ status: "unknown" });
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

  return { room, error, teach, follow, leave, publish, publishLesson, ask, dismiss, setLockstep };
}
