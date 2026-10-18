// Course and challenge progress, remembered in this browser (and only here).
import { useCallback, useEffect, useState } from "react";

const KEY = "arcanum.progress.v1";

export interface Progress {
  /** "lessonId/stepId" of every step whose check passed. */
  done: string[];
  /** Challenge ids, with when they were first completed. */
  challenges: Record<string, number>;
  lesson: string | null;
  step: number;
}

const EMPTY: Progress = { done: [], challenges: {}, lesson: null, step: 0 };

function load(): Progress {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return EMPTY;
    const data = JSON.parse(raw) as Partial<Progress>;
    return {
      done: Array.isArray(data.done) ? data.done.filter((x) => typeof x === "string") : [],
      challenges: data.challenges && typeof data.challenges === "object" ? data.challenges : {},
      lesson: typeof data.lesson === "string" ? data.lesson : null,
      step: typeof data.step === "number" ? data.step : 0,
    };
  } catch {
    return EMPTY; // unavailable or corrupt storage: start fresh
  }
}

export function useProgress() {
  const [progress, setProgress] = useState<Progress>(load);

  useEffect(() => {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(progress));
    } catch {
      // Progress is a convenience; the course works without saving it.
    }
  }, [progress]);

  const markStep = useCallback((lessonId: string, stepId: string) => {
    const key = `${lessonId}/${stepId}`;
    setProgress((p) => (p.done.includes(key) ? p : { ...p, done: [...p.done, key] }));
  }, []);

  const unlock = useCallback((ids: string[]) => {
    setProgress((p) => {
      const fresh = ids.filter((id) => !(id in p.challenges));
      if (!fresh.length) return p;
      const now = Date.now();
      return { ...p, challenges: { ...p.challenges, ...Object.fromEntries(fresh.map((id) => [id, now])) } };
    });
  }, []);

  const open = useCallback((lesson: string | null, step = 0) => setProgress((p) => ({ ...p, lesson, step })), []);

  const reset = useCallback(() => setProgress(EMPTY), []);

  return { progress, markStep, unlock, open, reset };
}
