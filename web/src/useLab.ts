// Running a lab: one request at a time, its result, and a readable error.
import { useCallback, useState } from "react";
import { ApiError } from "./api";

export function useLab<P, R>(sessionId: string | null, request: (id: string, params: P) => Promise<R>) {
  const [result, setResult] = useState<R | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (params: P) => {
      if (!sessionId) return;
      setRunning(true);
      setError(null);
      try {
        setResult(await request(sessionId, params));
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setRunning(false);
      }
    },
    [sessionId, request],
  );
  return { result, running, error, run };
}

/** The largest values this server allows for a lab's size fields (limits.labs in the session's limits). */
export type LabLimits = Record<string, number> | null | undefined;

/** A slider's top: the server's limit for `field` when lower than `max`, kept on the slider's step grid. */
export function labMax(limits: LabLimits, field: string, min: number, max: number, step: number): number {
  const cap = limits?.[field];
  if (cap === undefined || cap >= max) return max;
  return Math.max(min, min + Math.floor((cap - min) / step) * step);
}

/** params with every size field brought within the server's limits. */
export function capParams<P extends object>(params: P, limits: LabLimits): P {
  if (!limits) return params;
  const out = { ...params } as Record<string, unknown>;
  for (const [field, cap] of Object.entries(limits)) {
    if (typeof out[field] === "number" && (out[field] as number) > cap) out[field] = cap;
  }
  return out as P;
}
