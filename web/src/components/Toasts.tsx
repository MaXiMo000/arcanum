import { useEffect } from "react";

export interface Toast {
  id: number;
  title: string;
  body: string;
}

export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  useEffect(() => {
    if (!toasts.length) return;
    const timer = window.setTimeout(() => onDismiss(toasts[0]!.id), 6000);
    return () => window.clearTimeout(timer);
  }, [toasts, onDismiss]);

  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className="toast" role="status">
          <span className="toast-star" aria-hidden="true">
            ★
          </span>
          <div>
            <strong>{t.title}</strong>
            <p>{t.body}</p>
          </div>
          <button className="icon-button small" onClick={() => onDismiss(t.id)} aria-label="Dismiss">
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
