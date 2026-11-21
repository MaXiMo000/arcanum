"""Raft consensus over three Arcanum databases, simulated tick by tick.

Each node keeps a log of SQL statements and its own database: the state
machine. Raft (Ongaro and Ousterhout, 2014) keeps the logs identical:

- Leader election. A follower that hears nothing from a leader for its
  election timeout (randomized, so nodes rarely time out together) becomes a
  candidate for the next term and asks the others for votes. A node grants
  one vote per term, and only to a candidate whose log is at least as
  up to date as its own; a majority makes a leader.
- Log replication. Clients write to the leader, which appends the statement
  to its log and sends it to the followers in AppendEntries messages (empty
  ones are heartbeats). A follower accepts entries only if its log matches
  the leader's at the entry just before them; otherwise the leader backs up
  and resends until the logs agree, overwriting what a follower should not have.
- Commitment. Once a majority store an entry of the leader's current term, it
  and everything before it are committed, and every node applies them to its
  database in log order. A new leader commits a no-op entry first, which also
  commits what earlier leaders left behind.

The network is simulated: messages take one to three ticks, and a partition
or a crashed node drops them. Persistent state (term, vote and log) survives a
crash; a restarted node rebuilds its database by applying its log again as
the leader tells it what is committed. Everything is deterministic for a seed.

Statements are replicated, not their effects, so each node runs every
statement itself. Statements that read the clock or random numbers would give
each node a different answer, so they are refused, as are transaction control
statements: each entry is one autocommit statement.
"""

import random

from ..engine import Database
from ..engine.functions import STABLE
from ..errors import ArcanumError, SqlError
from ..sql import nodes as n
from ..sql.lexer import tokenize
from ..sql.parser import parse_statements
from ..storage.files import CountingMemFileSystem

NODES = ("n1", "n2", "n3")
ELECTION_TIMEOUT = (10, 20)  # ticks a follower waits for a leader before standing for election
HEARTBEAT = 3  # ticks between a leader's AppendEntries to each follower
LATENCY = (1, 3)  # ticks a message takes
MAX_BATCH = 8  # entries per AppendEntries
MAX_LOG = 200  # entries a cluster keeps; a real system would snapshot and truncate
HISTORY = 60  # recent happenings kept for the viewer
NONDETERMINISTIC = {name.lower() for name in STABLE} | {"random"}
SEED_SQL = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, balance INTEGER NOT NULL);
INSERT INTO accounts VALUES (1, 'Ada', 100), (2, 'Grace', 100), (3, 'Edsger', 100);
"""


class Node:
    def __init__(self, name):
        self.name = name
        # Persistent: kept across a crash.
        self.term = 0
        self.voted_for = None
        self.log = []  # [{"term", "sql"}]; index i is log position i + 1 (1-based, as in the paper)
        # Volatile.
        self.up = True
        self.role = "follower"
        self.leader = None  # the leader this node last heard from in its term
        self.commit = 0
        self.applied = 0
        self.db = None
        self.results = {}  # log position -> what applying it did, kept for the viewer
        self.deadline = 0  # tick of the next election timeout
        self.votes = set()
        self.next_index = {}  # leader only: per follower, the next position to send
        self.match = {}  # leader only: per follower, the highest position known to be stored there
        self.heartbeat_at = {}  # leader only: per follower, the tick of the next AppendEntries

    def last(self):
        """(position, term) of the last log entry, (0, 0) for an empty log."""
        return len(self.log), (self.log[-1]["term"] if self.log else 0)

    def term_at(self, position):
        return self.log[position - 1]["term"] if position else 0


class Cluster:
    def __init__(self, seed=1, limits=None):
        self.rng = random.Random(seed)
        self.limits = limits
        self.now = 0
        self.nodes = {name: Node(name) for name in NODES}
        self.messages = []  # in flight: {"id", "kind", "from", "to", "sent", "deliver", ...}
        self.groups = [list(NODES)]  # the partition: nodes can only reach nodes in their own group
        self.history = []
        self.sequence = 0
        for node in self.nodes.values():
            self._boot(node)

    # ---- the client's view ----------------------------------------------------------------

    def leader(self):
        """The leader of the highest term among running nodes, if there is one."""
        leaders = [x for x in self.nodes.values() if x.up and x.role == "leader"]
        return max(leaders, key=lambda x: x.term, default=None)

    def write(self, sql, node=None):
        """Append a statement to a leader's log; it is applied once committed. Returns its log position."""
        check_write(sql)
        target = self.nodes[node] if node else self.leader()
        if target is None or not target.up:
            raise SqlError("no leader to write to: wait for an election, or heal the partition")
        if target.role != "leader":
            raise SqlError(f"{target.name} is not the leader; Raft sends every write through the leader"
                           + (f" ({target.leader})" if target.leader else ""))
        if len(target.log) >= MAX_LOG:
            raise SqlError(f"this cluster's log is full ({MAX_LOG} entries); reset it to start again")
        target.log.append({"term": target.term, "sql": sql.strip()})
        target.match[target.name] = len(target.log)
        self._note(f"{target.name} appended entry {len(target.log)} (term {target.term}): {_short(sql)}")
        for peer in self._peers(target):
            target.heartbeat_at[peer] = self.now  # replicate on the next tick, not the next heartbeat
        self._advance_commit(target)
        return len(target.log)

    def read(self, node, sql):
        """Run a query on one node's own database: what a client connected to that node would see."""
        statements = parse_statements(sql)
        if len(statements) != 1 or not isinstance(statements[0][0], n.Query):
            raise SqlError("a read runs exactly one SELECT")
        target = self.nodes[node]
        if not target.up:
            raise SqlError(f"{node} is down")
        result = target.db.execute(sql, max_rows=100)
        return {"columns": result.columns, "rows": result.rows, "applied": target.applied, "commit": target.commit,
                "role": target.role, "term": target.term}

    # ---- faults ----------------------------------------------------------------------------

    def partition(self, groups):
        """Split the network: each group of nodes can only reach nodes in the same group."""
        names = [name for group in groups for name in group]
        if sorted(names) != sorted(NODES):
            raise ValueError(f"a partition puts each of {', '.join(NODES)} in exactly one group")
        self.groups = [sorted(group) for group in groups if group]
        self._note("network: " + " | ".join(", ".join(group) for group in self.groups))

    def heal(self):
        self.groups = [list(NODES)]
        self._note("network healed")

    def crash(self, node):
        target = self.nodes[node]
        if target.up:
            target.up = False
            target.db = None
            self._note(f"{node} crashed: it keeps its term, vote and log, and loses everything else")

    def restart(self, node):
        target = self.nodes[node]
        if not target.up:
            target.up = True
            self._boot(target)
            self._note(f"{node} restarted as a follower; it reapplies its log as it learns what is committed")

    # ---- time ------------------------------------------------------------------------------

    def tick(self):
        """One step: deliver the messages that arrive now, then run each node's timers."""
        self.now += 1
        arriving = [m for m in self.messages if m["deliver"] <= self.now]
        self.messages = [m for m in self.messages if m["deliver"] > self.now]
        for message in arriving:
            target = self.nodes[message["to"]]
            if target.up and self._reachable(message["from"], message["to"]):
                self._receive(target, message)
        for node in self.nodes.values():
            if not node.up:
                continue
            if node.role == "leader":
                for peer in self._peers(node):
                    if self.now >= node.heartbeat_at[peer]:
                        self._append_entries(node, peer)
            elif self.now >= node.deadline:
                self._stand(node)

    # ---- Raft --------------------------------------------------------------------------------

    def _boot(self, node):
        node.role, node.leader, node.commit, node.applied, node.results = "follower", None, 0, 0, {}
        node.db = Database(node.name, fs=CountingMemFileSystem(), limits=self.limits)
        node.db.execute_script(SEED_SQL)  # the snapshot every node starts from
        self._reset_timer(node)

    def _reset_timer(self, node):
        node.deadline = self.now + self.rng.randint(*ELECTION_TIMEOUT)

    def _peers(self, node):
        return [name for name in NODES if name != node.name]

    def _reachable(self, a, b):
        return any(a in group and b in group for group in self.groups)

    def _send(self, sender, to, kind, **fields):
        self.sequence += 1
        self.messages.append({"id": self.sequence, "kind": kind, "from": sender.name, "to": to, "term": sender.term,
                              "sent": self.now, "deliver": self.now + self.rng.randint(*LATENCY), **fields})

    def _note(self, text):
        self.history.append({"tick": self.now, "text": text})
        del self.history[:-HISTORY]

    def _step_down(self, node, term):
        """A message from a later term: this node is behind, and becomes a follower in that term."""
        was_leader = node.role == "leader"
        node.term, node.voted_for, node.role, node.leader = term, None, "follower", None
        if was_leader:
            self._note(f"{node.name} saw term {term} and stepped down")
        self._reset_timer(node)

    def _stand(self, node):
        node.term += 1
        node.role, node.voted_for, node.votes, node.leader = "candidate", node.name, {node.name}, None
        self._reset_timer(node)
        self._note(f"{node.name} timed out and stands for election in term {node.term}")
        position, term = node.last()
        for peer in self._peers(node):
            self._send(node, peer, "vote", last_index=position, last_term=term)

    def _become_leader(self, node):
        node.role, node.leader = "leader", node.name
        node.next_index = {peer: len(node.log) + 1 for peer in self._peers(node)}
        node.match = dict.fromkeys(self._peers(node), 0) | {node.name: len(node.log)}
        node.heartbeat_at = dict.fromkeys(self._peers(node), self.now)
        self._note(f"{node.name} won a majority and leads term {node.term}")
        node.log.append({"term": node.term, "sql": None})  # the no-op that lets earlier entries commit
        node.match[node.name] = len(node.log)

    def _append_entries(self, leader, peer):
        start = leader.next_index[peer]
        entries = leader.log[start - 1:start - 1 + MAX_BATCH]
        self._send(leader, peer, "append", prev_index=start - 1, prev_term=leader.term_at(start - 1),
                   entries=[dict(e) for e in entries], commit=leader.commit)
        leader.heartbeat_at[peer] = self.now + HEARTBEAT

    def _receive(self, node, m):
        if m["term"] > node.term:
            self._step_down(node, m["term"])
        handler = {"vote": self._on_vote, "vote_reply": self._on_vote_reply,
                   "append": self._on_append, "append_reply": self._on_append_reply}[m["kind"]]
        handler(node, m)

    def _on_vote(self, node, m):
        position, term = node.last()
        up_to_date = (m["last_term"], m["last_index"]) >= (term, position)
        granted = m["term"] == node.term and node.voted_for in (None, m["from"]) and up_to_date
        if granted:
            node.voted_for = m["from"]
            self._reset_timer(node)
        self._send(node, m["from"], "vote_reply", granted=granted)

    def _on_vote_reply(self, node, m):
        if node.role != "candidate" or m["term"] != node.term or not m["granted"]:
            return
        node.votes.add(m["from"])
        if len(node.votes) * 2 > len(NODES):
            self._become_leader(node)

    def _on_append(self, node, m):
        if m["term"] < node.term:
            self._send(node, m["from"], "append_reply", success=False, match=0)
            return
        if node.role != "follower" or node.leader != m["from"]:
            if node.role == "candidate":
                self._note(f"{node.name} heard from leader {m['from']} and became a follower")
            node.role, node.leader = "follower", m["from"]
        self._reset_timer(node)
        prev = m["prev_index"]
        if prev > len(node.log) or node.term_at(prev) != m["prev_term"]:
            self._send(node, m["from"], "append_reply", success=False, match=0, hint=min(prev, len(node.log)))
            return
        for offset, entry in enumerate(m["entries"]):
            position = prev + 1 + offset
            if position <= len(node.log) and node.log[position - 1]["term"] != entry["term"]:
                dropped = len(node.log) - position + 1
                del node.log[position - 1:]
                self._note(f"{node.name} dropped {dropped} conflicting entr{'y' if dropped == 1 else 'ies'} "
                           f"from position {position}: the leader's log wins")
            if position > len(node.log):
                node.log.append(dict(entry))
        matched = prev + len(m["entries"])
        if m["commit"] > node.commit:
            node.commit = max(node.commit, min(m["commit"], matched))
            self._apply(node)
        self._send(node, m["from"], "append_reply", success=True, match=matched)

    def _on_append_reply(self, node, m):
        if node.role != "leader" or m["term"] != node.term:
            return
        peer = m["from"]
        if m["success"]:
            node.match[peer] = max(node.match[peer], m["match"])
            node.next_index[peer] = node.match[peer] + 1
            self._advance_commit(node)
        else:
            node.next_index[peer] = max(1, min(node.next_index[peer] - 1, m.get("hint", 0) + 1))
        if node.next_index[peer] <= len(node.log):
            node.heartbeat_at[peer] = self.now  # more to send: do not wait for the heartbeat

    def _advance_commit(self, leader):
        stored = sorted(leader.match.values(), reverse=True)
        majority = stored[len(NODES) // 2]  # the highest position a majority has stored
        # Only an entry of the current term commits by counting; earlier ones commit along with it.
        if majority > leader.commit and leader.term_at(majority) == leader.term:
            leader.commit = majority
            self._note(f"{leader.name} committed up to entry {majority}: a majority stores it")
            self._apply(leader)

    def _apply(self, node):
        while node.applied < node.commit:
            node.applied += 1
            sql = node.log[node.applied - 1]["sql"]
            if sql is None:
                continue
            try:
                result = node.db.execute(sql)
                node.results[node.applied] = {"ok": True, "rowcount": result.rowcount}
            except ArcanumError as e:  # deterministic: every node fails the same statement the same way
                node.results[node.applied] = {"ok": False, "error": str(e)}

    # ---- the viewer's picture ------------------------------------------------------------------

    def snapshot(self):
        return {
            "now": self.now,
            "nodes": [{"name": x.name, "up": x.up, "role": x.role, "term": x.term, "voted_for": x.voted_for,
                       "leader": x.leader, "commit": x.commit, "applied": x.applied,
                       "log": [e["term"] for e in x.log],
                       "election_in": max(0, x.deadline - self.now) if x.up and x.role != "leader" else None,
                       "match": dict(x.match) if x.role == "leader" else None}
                      for x in self.nodes.values()],
            "messages": [{key: m[key] for key in ("id", "kind", "from", "to", "term", "sent", "deliver")}
                         | {"size": len(m.get("entries", ())), "granted": m.get("granted"), "success": m.get("success")}
                         for m in self.messages],
            "groups": self.groups,
        }

    def entries(self):
        """The statement and outcome behind each (position, term) any node holds, for the log view."""
        out = {}
        for node in self.nodes.values():
            for position, entry in enumerate(node.log, 1):
                key = f"{position}:{entry['term']}"
                item = out.setdefault(key, {"sql": entry["sql"]})
                if position in node.results and "result" not in item:
                    item["result"] = node.results[position]
        return out


def check_write(sql):
    """Refuse what statement replication cannot replay identically on every node."""
    statements = parse_statements(sql)
    if len(statements) != 1:
        raise SqlError("each log entry is exactly one statement")
    statement = statements[0][0]
    if isinstance(statement, (n.Begin, n.Commit, n.Rollback, n.Savepoint, n.RollbackTo, n.Release)):
        raise SqlError("each log entry is one autocommit statement; transaction control is not replicated")
    if isinstance(statement, (n.Query, n.Explain)):
        raise SqlError("a write changes the database; run queries as reads on a node")
    used = {t.value.lower() for t in tokenize(sql) if t.kind == "word"} & NONDETERMINISTIC
    if used:
        raise SqlError(f"{', '.join(sorted(used))} would give each node a different value: every node runs "
                       "the statement itself, so replicated statements must be deterministic")


def _short(sql, limit=60):
    text = " ".join(sql.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
