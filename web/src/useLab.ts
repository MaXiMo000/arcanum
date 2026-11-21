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
