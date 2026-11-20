// Row versions, the way PostgreSQL keeps them: every changed row as a chain of versions, each stamped
// with the transaction that created it (xmin) and the one that deleted it (xmax), and which session's
// snapshot sees which version right now. Rows settle back to one frozen version once no snapshot needs
// the others.
import type { Dependency, EngineState, RowVersion, SessionName, TxnStatus } from "../api";
import { formatValue } from "../format";

const SHOWN = 8;

function Xid({ label, xid, status }: { label: string; xid: number | null; status: TxnStatus | null }) {
  if (xid === null) return <span className="xid none">{label} –</span>;
  if (xid === 0) return <span className="xid frozen" title="frozen: visible to every transaction">{label} frozen</span>;
  const meaning = { committed: "committed", running: "still open", aborted: "rolled back" }[status ?? "committed"];
  return (
    <span className={`xid ${status}`} title={`transaction ${xid}: ${meaning}`}>
      {label} {xid}
    </span>
  );
}

function Version({ version, current }: { version: RowVersion; current: boolean }) {
  return (
    <li className={`version ${version.seen_by.length ? "seen" : ""} ${version.status[0] === "aborted" ? "dead" : ""}`}>
      <div className="version-xids">
        <Xid label="xmin" xid={version.xmin} status={version.status[0]} />
        <Xid label="xmax" xid={version.xmax} status={version.status[1]} />
      </div>
      <code className="version-row">{version.row.map((v) => formatValue(v)).join(" · ")}</code>
      <div className="version-seen">
        {version.seen_by.map((s) => (
          <span key={s} className={`session-badge s-${s}`} title={`Session ${s}'s snapshot sees this version`}>
            <span aria-hidden="true">{s}</span> sees
          </span>
        ))}
        {!version.seen_by.length && <span className="muted small">{current ? "nobody yet" : "nobody"}</span>}
      </div>
    </li>
  );
}

/** Who waits for whom: each waiting session, the transaction it waits for and what it needs. */
function LockGraph({ waits }: { waits: EngineState["waits"] }) {
  return (
    <ul className="lock-graph" aria-label="Lock waits">
      {waits.map((w) => (
        <li key={w.ticket}>
          <span className={`session-badge s-${w.session}`}>
            <span aria-hidden="true">{w.session}</span>
            {w.xid ? ` txn ${w.xid}` : " statement"}
          </span>
          <span className="lock-arrow" aria-hidden="true">
            waits for ⟶
          </span>
          <span className={`session-badge s-${w.holder_session ?? "A"}`}>
            <span aria-hidden="true">{w.holder_session}</span> txn {w.holder}
          </span>
          <span className="muted small">
            {w.what} · {(w.waited_ms / 1000).toFixed(1)} s
          </span>
        </li>
      ))}
    </ul>
  );
}

function Txn({ xid, session }: { xid: number; session: string | null }) {
  return (
    <span className={`session-badge s-${session ?? "none"}`}>
      <span aria-hidden="true">{session ?? "?"}</span> txn {xid}
    </span>
  );
}

/** SERIALIZABLE's read/write dependencies: each arrow means "read something the other then changed", so
 * in any serial order the reader goes first. A transaction with an arrow in and an arrow out is a pivot. */
export function Dependencies({ edges, failed }: { edges: Dependency[]; failed?: boolean }) {
  const into = new Set(edges.map((e) => e.writer));
  const pivots = new Set(edges.map((e) => e.reader).filter((x) => into.has(x)));
  return (
    <ul className={`lock-graph rw-graph ${failed ? "failed" : ""}`} aria-label="Read/write dependencies">
      {edges.map((e) => (
        <li key={`${e.reader}-${e.writer}`} className={pivots.has(e.reader) || pivots.has(e.writer) ? "pivot" : ""}>
          <Txn xid={e.reader} session={e.reader_session} />
          <span className="lock-arrow" aria-hidden="true">
            rw ⟶
          </span>
          <Txn xid={e.writer} session={e.writer_session} />
          <span className="muted small">
            read {e.what} before it changed
          </span>
        </li>
      ))}
      {pivots.size > 0 && (
        <li className="muted small rw-note">
          {failed ? "Refused: " : ""}
          {pivots.size === 1 ? `transaction ${[...pivots][0]} has` : `transactions ${[...pivots].join(" and ")} each have`} a
          dependency in and one out (a pivot), so no serial order fits them all.
        </li>
      )}
    </ul>
  );
}

export function Versions({ state }: { state: EngineState }) {
  const rows = state.versions ?? [];
  const waits = state.waits ?? [];
  const edges = state.dependencies ?? [];
  const sessions = Object.entries(state.sessions ?? {}) as [SessionName, EngineState["sessions"][SessionName]][];
  return (
    <section className="map-card versions-card" aria-label="Row versions">
      <header className="map-head">
        <div>
          <h3>
            <span className="map-icon mvcc" aria-hidden="true">⧉</span> Row versions
          </h3>
          <p className="muted small">
            {rows.length
              ? "A change keeps the old version for snapshots that still see it (PostgreSQL's MVCC)."
              : "Every row has one frozen version. Open a transaction and change a row to see versions appear."}
          </p>
        </div>
        <div className="txn-strip">
          {sessions.map(([name, s]) => (
            <span key={name} className={`txn-chip s-${name}`} title={`snapshot: sees transactions below ${s.snapshot.xmax}${s.snapshot.active.length ? ` except ${s.snapshot.active.join(", ")} (still open)` : ""}`}>
              <b>{name}</b> {s.xid ? `txn ${s.xid}${s.isolation === "REPEATABLE READ" ? " · RR" : s.isolation === "SERIALIZABLE" ? " · SER" : ""}` : "idle"}
            </span>
          ))}
          <span className="txn-chip muted" title="the id the next transaction gets">next {state.next_xid}</span>
        </div>
      </header>
      {waits.length > 0 && <LockGraph waits={waits} />}
      {edges.length > 0 && <Dependencies edges={edges} />}
      {rows.length > 0 && (
        <ul className="version-rows">
          {rows.slice(0, SHOWN).map((r) => (
            <li key={`${r.table}:${r.key.join(",")}`} className="version-row-item">
              <span className="version-key">
                {r.table} <b>{r.key.map(formatValue).join(", ")}</b>
              </span>
              <ol className="version-chain">
                {r.versions.map((v, i) => (
                  <Version key={i} version={v} current={i === r.versions.length - 1} />
                ))}
              </ol>
            </li>
          ))}
          {rows.length > SHOWN && <li className="muted small">+{rows.length - SHOWN} more rows with versions</li>}
        </ul>
      )}
    </section>
  );
}
