// Raft replication: three nodes, each with its own database, kept in agreement by a replicated log of SQL
// statements. Step or play the simulated clock, write through the leader, read from any node, and cut the
// network or crash nodes to watch elections, stale reads and log repair.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, type ClusterNode, type ClusterReply, type ClusterRequest, type ClusterState, type NodeName } from "../api";
import { formatValue } from "../format";
import { LabTour, type TourStep } from "./LabTour";

const NAMES: NodeName[] = ["n1", "n2", "n3"];
const SPOT: Record<NodeName, [number, number]> = { n1: [160, 46], n2: [52, 214], n3: [268, 214] };
const R = 36;
const TICK_MS = 160; // playback: one simulated tick per frame
const TOUR_TICK_MS = 70; // a tour step's ticks, drawn quicker
const BATCH = 10; // ticks fetched per request while playing
const TIMEOUT_MAX = 20; // the longest election timeout, for the timer ring
const WRITES = [
  { label: "Ada pays 10", sql: "UPDATE accounts SET balance = balance - 10 WHERE id = 1" },
  { label: "Grace gets 10", sql: "UPDATE accounts SET balance = balance + 10 WHERE id = 2" },
  { label: "Add Barbara", sql: "INSERT INTO accounts VALUES (4, 'Barbara', 100)" },
];
const READ_SQL = "SELECT id, owner, balance FROM accounts ORDER BY id";
const KIND: Record<string, string> = {
  vote: "RequestVote",
  vote_reply: "vote",
  append: "AppendEntries",
  append_reply: "append reply",
};

const termHue = (term: number) => `hsl(${(term * 67) % 360} 62% 56%)`;

export function RaftLab({ sessionId }: { sessionId: string | null }) {
  const [reply, setReply] = useState<ClusterReply | null>(null);
  const [frame, setFrame] = useState<ClusterState | null>(null); // what is drawn: one tick of a batch
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sql, setSql] = useState(WRITES[0]!.sql);
  const [readSql, setReadSql] = useState(READ_SQL);
  const [read, setRead] = useState<(ClusterReply["read"] & { node: NodeName }) | null>(null);
  const queue = useRef<ClusterState[]>([]);
  const root = useRef<HTMLDivElement>(null);

  const send = useCallback(
    async (request: ClusterRequest) => {
      if (!sessionId) return null;
      setBusy(true);
      setError(null);
      try {
        const next = await api.cluster(sessionId, request);
        setReply(next);
        if (!next.frames) {
          queue.current = [];
          setFrame(next.state);
        }
        return next;
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
        setPlaying(false);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [sessionId],
  );

  useEffect(() => {
    void send({ action: "state" });
  }, [send]);

  // Playback: draw queued ticks one per frame, fetching the next batch when the queue runs dry.
  useEffect(() => {
    if (!playing) return;
    let stopped = false;
    let fetching = false;
    const timer = window.setInterval(() => {
      if (!root.current?.offsetParent) {
        setPlaying(false); // hidden (another lab or view is showing): stop the clock
        return;
      }
      const next = queue.current.shift();
      if (next) setFrame(next);
      else if (!fetching) {
        fetching = true;
        void send({ action: "tick", ticks: BATCH }).then((r) => {
          fetching = false;
          if (!stopped && r?.frames) queue.current.push(...r.frames);
        });
      }
    }, TICK_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [playing, send]);

  // An action while playing applies to the latest state, so drop the ticks still queued for drawing.
  const act = (request: ClusterRequest) => {
    queue.current = [];
    return send(request);
  };
  const step = async () => {
    const r = await act({ action: "tick", ticks: 1 });
    if (r?.frames?.length) setFrame(r.frames[r.frames.length - 1]!);
  };
  const write = (e: FormEvent) => {
    e.preventDefault();
    void act({ action: "write", sql });
  };
  const readFrom = async (node: NodeName) => {
    const r = await act({ action: "read", node, sql: readSql });
    setRead(r?.read ? { ...r.read, node } : null);
  };

  const state = frame ?? reply?.state ?? null;
  const leader = state?.nodes.find((x) => x.up && x.role === "leader") ?? null;

  /** Run `ticks` ticks and draw them one by one; the state at the end. */
  const animate = async (ticks: number) => {
    setPlaying(false);
    const r = await act({ action: "tick", ticks });
    for (const f of r?.frames ?? []) {
      setFrame(f);
      await new Promise((done) => window.setTimeout(done, TOUR_TICK_MS));
    }
    return r?.state ?? null;
  };
  /** The leader of the majority, ticking until there is one. */
  const electLeader = async () => {
    let current = state;
    for (let i = 0; i < 4; i++) {
      const found = current?.nodes.find((n) => n.up && n.role === "leader" && current?.groups.some((g) => g.length > 1 && g.includes(n.name)));
      if (found) return found.name;
      current = await animate(20);
    }
    return null;
  };
  const stranded = useRef<NodeName | null>(null); // the leader the tour cuts off
  const tour: TourStep[] = [
    { title: "Elect a leader", body: "Followers time out (the orange rings), one stands for election, and a majority of votes makes it leader.",
      run: electLeader },
    { title: "Replicate a write", body: "The leader appends the statement and sends it in AppendEntries; once two of three nodes store it, it commits and every node applies it.",
      run: async () => {
        await electLeader();
        await act({ action: "write", sql: WRITES[0]!.sql });
        await animate(15);
      } },
    { title: "Cut off the leader", body: "Isolate the leader. The other two stop hearing heartbeats, time out and elect a new leader in a higher term. The old one still thinks it leads.",
      run: async () => {
        const name = await electLeader();
        if (!name) return;
        stranded.current = name;
        await act({ action: "partition", groups: [[name], NAMES.filter((n) => n !== name)] });
        await animate(45);
      } },
    { title: "Write to the stranded leader", body: "It accepts the write and appends it, but no majority can store it, so it never commits: an outlined entry that is never applied.",
      run: async () => {
        if (!stranded.current) return;
        await act({ action: "write", sql: WRITES[2]!.sql, node: stranded.current });
        await animate(10);
      } },
    { title: "Read from it", body: "A client still talking to the old leader gets stale data: it has not seen what the new majority committed, nor its own uncommitted write.",
      run: async () => {
        if (stranded.current) await readFrom(stranded.current);
      } },
    { title: "Heal the network", body: "The old leader hears the higher term, steps down, and the new leader overwrites its uncommitted entry: every log ends identical.",
      run: async () => {
        await act({ action: "heal" });
        await animate(30);
      } },
  ];
  return (
    <div className="engine-lab" ref={root}>
      <header className="lab-head">
        <h2>Replication with Raft</h2>
        <p className="muted">
          Three databases stay identical by agreeing on one log of statements. Writes go to the leader, which commits a
          statement once a majority store it; then every node applies it. Cut the network or crash a node and watch Raft
          elect a new leader and repair the logs.
        </p>
      </header>

      <LabTour steps={tour} busy={busy || !sessionId} />

      <section className="lab-controls raft-controls" aria-label="Clock and faults">
        <div className="raft-buttons">
          <button className="button primary" onClick={() => setPlaying((p) => !p)} disabled={!sessionId}>
            {playing ? "Pause" : "Play"}
          </button>
          <button className="button ghost" onClick={() => void step()} disabled={busy || playing || !sessionId}>
            Step one tick
          </button>
          <button className="button ghost" onClick={() => void act({ action: "heal" })} disabled={!sessionId || (state?.groups.length ?? 1) === 1}>
            Heal network
          </button>
          <button className="button ghost" onClick={() => {
            setRead(null);
            void act({ action: "reset" });
          }} disabled={!sessionId}>
            Reset
          </button>
        </div>
        <form className="raft-write" onSubmit={write}>
          <label>
            <span>Write a statement through the leader</span>
            <input value={sql} onChange={(e) => setSql(e.target.value)} maxLength={2000} spellCheck={false} aria-label="Statement to replicate" />
          </label>
          <button className="button primary" disabled={!sessionId || !sql.trim()}>
            Write
          </button>
          <div className="raft-presets">
            {WRITES.map((w) => (
              <button type="button" key={w.label} className={`chip ${sql === w.sql ? "active" : ""}`} onClick={() => setSql(w.sql)}>
                {w.label}
              </button>
            ))}
          </div>
        </form>
      </section>

      {error && (
        <p className="error-box" role="alert">
          {error}
        </p>
      )}

      {state && reply && (
        <div className="raft-grid">
          <section className="lab-chart raft-net" aria-label="The cluster">
            <div className="lab-chart-head">
              <h3>The cluster</h3>
              <span className="muted small">tick {state.now}</span>
            </div>
            <Network state={state} />
            <p className="muted small">
              {leader
                ? `${leader.name} leads term ${leader.term}.`
                : "No leader: a follower that hears nothing will time out and stand for election."}{" "}
              Dots are messages in flight; a dashed link is cut.
            </p>
            <div className="raft-node-actions">
              {state.nodes.map((node) => (
                <NodeActions key={node.name} node={node} state={state} act={act} onRead={readFrom} />
              ))}
            </div>
          </section>

          <section className="lab-chart raft-logs" aria-label="The logs">
            <div className="lab-chart-head">
              <h3>Each node's log</h3>
              <span className="muted small">filled = committed · colour = term</span>
            </div>
            {state.nodes.map((node) => (
              <LogRow key={node.name} node={node} entries={reply.entries} />
            ))}
            <label className="raft-read">
              <span>Read query (run on one node)</span>
              <input value={readSql} onChange={(e) => setReadSql(e.target.value)} maxLength={2000} spellCheck={false} />
            </label>
            {read && <ReadResult read={read} />}
          </section>

          <section className="lab-chart raft-history" aria-label="What happened">
            <h3>What happened</h3>
            <ol>
              {[...reply.history].reverse().map((h, i) => (
                <li key={`${h.tick}-${i}`}>
                  <span className="muted">{h.tick}</span> {h.text}
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </div>
  );
}

function Network({ state }: { state: ClusterState }) {
  const together = (a: NodeName, b: NodeName) => state.groups.some((g) => g.includes(a) && g.includes(b));
  const links: [NodeName, NodeName][] = [["n1", "n2"], ["n1", "n3"], ["n2", "n3"]];
  return (
    <svg viewBox="0 0 320 270" className="raft-svg" role="img"
      aria-label={state.nodes.map((x) => `${x.name}: ${x.up ? `${x.role}, term ${x.term}` : "down"}`).join("; ")}>
      {links.map(([a, b]) => (
        <line key={a + b} className={`raft-link ${together(a, b) ? "" : "cut"}`}
          x1={SPOT[a][0]} y1={SPOT[a][1]} x2={SPOT[b][0]} y2={SPOT[b][1]} />
      ))}
      {state.messages.map((m) => {
        const [ax, ay] = SPOT[m.from];
        const [bx, by] = SPOT[m.to];
        const f = Math.min(1, Math.max(0, (state.now - m.sent) / Math.max(1, m.deliver - m.sent)));
        const len = Math.hypot(bx - ax, by - ay);
        const side = m.from < m.to ? 1 : -1; // each direction on its own lane
        const ox = (-(by - ay) / len) * 7 * side;
        const oy = ((bx - ax) / len) * 7 * side;
        const reply = m.kind.endsWith("reply");
        const refused = m.granted === false || m.success === false;
        return (
          <g key={m.id} className={`raft-msg ${m.kind.startsWith("vote") ? "vote" : "append"} ${reply ? "reply" : ""} ${refused ? "refused" : ""}`}
            style={{ transform: `translate(${ax + (bx - ax) * f + ox}px, ${ay + (by - ay) * f + oy}px)` }}>
            <title>{`${KIND[m.kind]} ${m.from} → ${m.to}, term ${m.term}${m.size ? `, ${m.size} entr${m.size === 1 ? "y" : "ies"}` : ""}${refused ? ", refused" : ""}`}</title>
            <circle r={reply ? 3.5 : m.size ? 6 : 4.5} />
          </g>
        );
      })}
      {state.nodes.map((node) => {
        const [x, y] = SPOT[node.name];
        const timer = node.election_in === null ? 0 : Math.min(1, node.election_in / TIMEOUT_MAX);
        const c = 2 * Math.PI * (R + 5);
        return (
          <g key={node.name} className={`raft-node ${node.up ? node.role : "down"}`} transform={`translate(${x} ${y})`}>
            {node.up && node.election_in !== null && (
              <circle className="raft-timer" r={R + 5} strokeDasharray={`${timer * c} ${c}`} transform="rotate(-90)" />
            )}
            <circle className="raft-body" r={R} />
            <text className="raft-name" y={-8} textAnchor="middle">
              {node.name}
            </text>
            <text className="raft-role" y={7} textAnchor="middle">
              {node.up ? node.role : "down"}
            </text>
            <text className="raft-term" y={20} textAnchor="middle">
              term {node.term}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function NodeActions({ node, state, act, onRead }: {
  node: ClusterNode;
  state: ClusterState;
  act: (r: ClusterRequest) => Promise<unknown>;
  onRead: (node: NodeName) => void;
}) {
  const alone = state.groups.some((g) => g.length === 1 && g[0] === node.name);
  return (
    <div className="raft-node-row">
      <b>{node.name}</b>
      <button className="chip" onClick={() => void act({ action: node.up ? "crash" : "restart", node: node.name })}>
        {node.up ? "Crash" : "Restart"}
      </button>
      <button className="chip" disabled={alone}
        onClick={() => void act({ action: "partition", groups: [[node.name], NAMES.filter((n) => n !== node.name)] })}>
        Isolate
      </button>
      <button className="chip" disabled={!node.up} onClick={() => onRead(node.name)}>
        Read
      </button>
    </div>
  );
}

function LogRow({ node, entries }: { node: ClusterNode; entries: ClusterReply["entries"] }) {
  return (
    <div className={`raft-log ${node.up ? "" : "down"}`}>
      <span className="raft-log-name">
        {node.name}
        <small>
          commit {node.commit} · applied {node.applied}
        </small>
      </span>
      <ol className="raft-entries" aria-label={`${node.name}'s log`}>
        {node.log.map((term, i) => {
          const position = i + 1;
          const entry = entries[`${position}:${term}`];
          const committed = position <= node.commit;
          const what = entry?.sql ?? "no-op (a new leader's first entry)";
          const outcome = entry?.result && !entry.result.ok ? ` — failed on every node: ${entry.result.error}` : "";
          return (
            <li key={position} className={committed ? "committed" : ""} style={{ "--term": termHue(term) } as React.CSSProperties}
              title={`#${position}, term ${term}, ${committed ? "committed" : "not committed"}: ${what}${outcome}`}>
              {term}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function ReadResult({ read }: { read: NonNullable<ClusterReply["read"]> & { node: NodeName } }) {
  return (
    <div className="raft-result">
      <p className="muted small">
        From {read.node} ({read.role}, term {read.term}), which has applied {read.applied} of its log. A follower or a cut-off
        leader can answer with data that is out of date.
      </p>
      <table>
        <thead>
          <tr>
            {read.columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {read.rows.map((row, i) => (
            <tr key={i}>
              {row.map((v, j) => (
                <td key={j}>{formatValue(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
