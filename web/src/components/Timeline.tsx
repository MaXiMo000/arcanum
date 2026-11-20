// Time travel through one query: every event the engine reported for it (each page read, node visit,
// split and log write), stepped through one by one or played back, while the map shows the engine as it
// was at that moment.
import { useEffect } from "react";
import { describeEvent } from "../events";
import type { Recording } from "../feed";

const PLAY_SECONDS = 12; // a replay of any length plays back in about this long
const MIN_RATE = 3; // events per second, at least
const MAX_RATE = 60;

/** Where the replay is: the current event, the page it touched, and the nodes visited so far. */
export interface ReplayMarker {
  page: number | null;
  node: number | null;
  trail: Set<number>;
}

export function replayMarker(recording: Recording, index: number): ReplayMarker {
  const trail = new Set<number>();
  let node: number | null = null;
  recording.events.slice(0, index + 1).forEach((e) => {
    if (e.kind === "node_visit" && typeof e.page === "number") {
      trail.add(e.page);
      node = e.page;
    }
  });
  const current = recording.events[index];
  return { page: typeof current?.page === "number" ? current.page : null, node, trail };
}

interface Props {
  recording: Recording | null;
  /** The event shown, or null while showing the engine live. */
  index: number | null;
  onIndex: (index: number | null) => void;
  playing: boolean;
  onPlaying: (playing: boolean) => void;
}

export function Timeline({ recording, index, onIndex, playing, onPlaying }: Props) {
  const total = recording?.events.length ?? 0;

  useEffect(() => {
    if (!playing || !total) return;
    const rate = Math.min(MAX_RATE, Math.max(MIN_RATE, total / PLAY_SECONDS));
    const timer = window.setInterval(() => {
      const next = (index ?? -1) + 1;
      if (next >= total) {
        onPlaying(false);
        onIndex(null);
      } else {
        onIndex(next);
      }
    }, 1000 / rate);
    return () => window.clearInterval(timer);
  }, [playing, index, total, onIndex, onPlaying]);

  if (!recording || !total) return null;
  const at = index ?? total - 1;
  const event = recording.events[at]!;
  const { text, tone } = describeEvent(event);
  const step = (to: number) => {
    onPlaying(false);
    onIndex(Math.max(0, Math.min(total - 1, to)));
  };

  return (
    <div className={`timeline ${index === null ? "live" : "replaying"}`} role="group" aria-label="Replay the last query">
      <div className="timeline-controls">
        <button className="icon-button small" onClick={() => step(0)} aria-label="First event" title="First event">
          ⏮
        </button>
        <button className="icon-button small" onClick={() => step(at - 1)} aria-label="Previous event" title="Previous event">
          ◀
        </button>
        <button
          className="icon-button small play"
          onClick={() => {
            if (!playing && index === null) onIndex(0);
            onPlaying(!playing);
          }}
          aria-label={playing ? "Pause" : "Play the query back"}
          title={playing ? "Pause" : "Play back"}
        >
          {playing ? "⏸" : "▶"}
        </button>
        <button className="icon-button small" onClick={() => step(at + 1)} aria-label="Next event" title="Next event">
          ▶|
        </button>
        <button
          className="icon-button small"
          onClick={() => {
            onPlaying(false);
            onIndex(null);
          }}
          aria-label="Back to live"
          title="Back to live"
          disabled={index === null}
        >
          ⏭
        </button>
      </div>
      <input
        className="timeline-range"
        type="range"
        min={0}
        max={total - 1}
        value={at}
        onChange={(e) => step(Number(e.target.value))}
        aria-label="Event"
        aria-valuetext={`event ${at + 1} of ${total}: ${text}`}
      />
      <p className="timeline-caption small">
        {index === null ? (
          <span className="muted">
            Step through this query's {total.toLocaleString()} events{recording.truncated ? " (the first ones)" : ""}
          </span>
        ) : (
          <>
            <b>
              {at + 1}/{total.toLocaleString()}
            </b>{" "}
            <span className={`tone-${tone}`}>{text}</span>
          </>
        )}
      </p>
    </div>
  );
}
