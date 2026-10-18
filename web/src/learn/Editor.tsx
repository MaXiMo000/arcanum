// SQL editor: a textarea over a highlighted copy of its text, with the doctor's error span underlined
// and completion of table names, column names and keywords.
import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { TableInfo } from "../api";

const KEYWORDS = `SELECT FROM WHERE GROUP BY HAVING ORDER LIMIT OFFSET JOIN LEFT RIGHT FULL INNER OUTER CROSS ON USING
AS AND OR NOT NULL IS IN EXISTS BETWEEN LIKE DISTINCT UNION ALL INTERSECT EXCEPT WITH RECURSIVE CASE WHEN THEN
ELSE END ASC DESC NULLS FIRST LAST INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE INDEX UNIQUE PRIMARY KEY
DROP IF DEFAULT BEGIN COMMIT ROLLBACK EXPLAIN ANALYZE OVER PARTITION ROWS RANGE PRECEDING FOLLOWING CURRENT ROW
UNBOUNDED TRUE FALSE CAST INTEGER REAL TEXT BOOLEAN`.split(/\s+/);
const FUNCTIONS = `count sum avg min max abs length lower upper trim ltrim rtrim replace substr round coalesce ifnull
nullif row_number rank dense_rank lag lead first_value last_value`.split(/\s+/);
const KEYWORD_SET = new Set(KEYWORDS);
const MAX_SUGGESTIONS = 6;
const INDENT = "  ";

const TOKEN = /(--[^\n]*)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|([\s\S])/g;

interface Segment {
  text: string;
  cls: string;
}

function highlight(sql: string): Segment[] {
  const out: Segment[] = [];
  for (const m of sql.matchAll(TOKEN)) {
    const [text, comment, string, quoted, number, word] = m;
    let cls = "";
    if (comment) cls = "tok-comment";
    else if (string) cls = "tok-string";
    else if (quoted) cls = "tok-name";
    else if (number) cls = "tok-number";
    else if (word) {
      const next = sql.slice(m.index + text.length).trimStart()[0];
      cls = KEYWORD_SET.has(word.toUpperCase()) ? "tok-keyword" : next === "(" ? "tok-function" : "tok-ident";
    }
    out.push({ text, cls });
  }
  return out;
}

/** Split segments at the error span so it can be underlined. */
function mark(segments: Segment[], from: number, to: number): Segment[] {
  const out: Segment[] = [];
  let at = 0;
  for (const s of segments) {
    const start = at;
    const end = at + s.text.length;
    at = end;
    if (end <= from || start >= to) {
      out.push(s);
      continue;
    }
    const a = Math.max(from, start) - start;
    const b = Math.min(to, end) - start;
    if (a > 0) out.push({ text: s.text.slice(0, a), cls: s.cls });
    out.push({ text: s.text.slice(a, b), cls: `${s.cls} squiggle` });
    if (b < s.text.length) out.push({ text: s.text.slice(b), cls: s.cls });
  }
  return out;
}

function splitAt(segments: Segment[], at: number): [Segment[], Segment[]] {
  const before: Segment[] = [];
  const after: Segment[] = [];
  let pos = 0;
  for (const s of segments) {
    const end = pos + s.text.length;
    if (end <= at) before.push(s);
    else if (pos >= at) after.push(s);
    else {
      before.push({ text: s.text.slice(0, at - pos), cls: s.cls });
      after.push({ text: s.text.slice(at - pos), cls: s.cls });
    }
    pos = end;
  }
  return [before, after];
}

const render = (segments: Segment[]) =>
  segments.map((s, i) => (
    <span key={i} className={s.cls || undefined}>
      {s.text}
    </span>
  ));

interface Props {
  id: string;
  value: string;
  onChange: (sql: string) => void;
  onRun: () => void;
  tables: TableInfo[];
  /** Characters to underline, as offsets into value. */
  error: { pos: number; end: number } | null;
}

export function Editor({ id, value, onChange, onRun, tables, error }: Props) {
  const area = useRef<HTMLTextAreaElement>(null);
  const mirror = useRef<HTMLPreElement>(null);
  const caretMark = useRef<HTMLSpanElement>(null);
  const [caret, setCaret] = useState<number | null>(null); // set while completing
  const [choice, setChoice] = useState(0);
  const [popup, setPopup] = useState<{ left: number; top: number } | null>(null);

  const names = useMemo(() => {
    const all = new Set<string>();
    for (const t of tables) {
      all.add(t.name);
      for (const c of t.columns) all.add(c.name);
    }
    return [...all, ...FUNCTIONS, ...KEYWORDS];
  }, [tables]);

  const prefix = caret === null ? "" : (/[A-Za-z_][A-Za-z0-9_]*$/.exec(value.slice(0, caret))?.[0] ?? "");
  const suggestions =
    prefix.length < 2
      ? []
      : names
          .filter((n) => n.toLowerCase().startsWith(prefix.toLowerCase()) && n.toLowerCase() !== prefix.toLowerCase())
          .filter((n, i, all) => all.indexOf(n) === i)
          .slice(0, MAX_SUGGESTIONS);
  const open = suggestions.length > 0 && popup !== null;

  const segments = useMemo(() => {
    const base = highlight(value);
    return error && error.end > error.pos ? mark(base, error.pos, error.end) : base;
  }, [value, error]);

  // Place the completion list under the caret, measured from a marker in the mirrored text.
  useLayoutEffect(() => {
    if (caret === null || !caretMark.current || !area.current) {
      setPopup(null);
      return;
    }
    const el = area.current;
    setPopup({ left: caretMark.current.offsetLeft - el.scrollLeft, top: caretMark.current.offsetTop + 20 - el.scrollTop });
  }, [caret, value]);

  const accept = (name: string) => {
    if (caret === null) return;
    const start = caret - prefix.length;
    const keyword = KEYWORD_SET.has(name);
    const text = keyword && prefix === prefix.toLowerCase() ? name.toLowerCase() : name;
    onChange(value.slice(0, start) + text + value.slice(caret));
    const at = start + text.length;
    setCaret(null);
    requestAnimationFrame(() => area.current?.setSelectionRange(at, at));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setChoice((c) => (c + (e.key === "ArrowDown" ? 1 : suggestions.length - 1)) % suggestions.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.ctrlKey && !e.metaKey)) {
        e.preventDefault();
        accept(suggestions[Math.min(choice, suggestions.length - 1)]!);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setCaret(null);
        return;
      }
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      setCaret(null);
      onRun();
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      const el = e.currentTarget;
      const { selectionStart: start, selectionEnd: end } = el;
      onChange(`${value.slice(0, start)}${INDENT}${value.slice(end)}`);
      requestAnimationFrame(() => el.setSelectionRange(start + INDENT.length, start + INDENT.length));
    }
  };

  const syncScroll = () => {
    if (mirror.current && area.current) {
      mirror.current.scrollTop = area.current.scrollTop;
      mirror.current.scrollLeft = area.current.scrollLeft;
    }
  };

  const [before, after] = caret === null ? [segments, []] : splitAt(segments, caret);
  return (
    <div className="editor-wrap">
      <pre className="editor-mirror" ref={mirror} aria-hidden="true">
        {render(before)}
        {caret !== null && <span ref={caretMark} />}
        {render(after)}
        {"\n"}
      </pre>
      <textarea
        id={id}
        ref={area}
        className="editor"
        value={value}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        aria-autocomplete="list"
        aria-controls={open ? `${id}-suggestions` : undefined}
        aria-expanded={open}
        onChange={(e) => {
          onChange(e.target.value);
          setChoice(0);
          const typed = (e.nativeEvent as InputEvent).inputType?.startsWith("insert");
          setCaret(typed ? e.target.selectionStart : null);
        }}
        onKeyDown={onKeyDown}
        onScroll={syncScroll}
        onBlur={() => setCaret(null)}
        onClick={() => setCaret(null)}
        rows={8}
      />
      {open && (
        <ul className="completions" id={`${id}-suggestions`} role="listbox" style={popup}>
          {suggestions.map((s, i) => (
            <li
              key={s}
              role="option"
              aria-selected={i === choice}
              className={i === choice ? "active" : ""}
              onMouseDown={(e) => {
                e.preventDefault();
                accept(s);
              }}
            >
              <span className={KEYWORD_SET.has(s) ? "tok-keyword" : FUNCTIONS.includes(s) ? "tok-function" : "tok-ident"}>{s}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
