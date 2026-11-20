// Shareable links. A link carries the playground (its SQL, both sessions) and the statements that
// changed the database since it opened, compressed into the URL fragment. The fragment never leaves
// the browser, so nothing is stored on a server; opening the link replays the statements on a fresh
// database of the visitor's own, then fills in the editors.
import type { SessionName } from "./api";

const PREFIX = "#share=";
const VERSION = 1;
export const MAX_LINK = 60_000; // characters; longer links are unreliable to paste and share
const MAX_STATEMENTS = 200;

export interface SharedState {
  v: number;
  sql: string;
  sqlB: string;
  two: boolean;
  /** Statements to replay first, in order, each in the session that ran it. */
  setup: [SessionName, string][];
}

function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text: string): Uint8Array<ArrayBuffer> {
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
}

async function transform(bytes: Uint8Array<ArrayBuffer>, stream: CompressionStream | DecompressionStream): Promise<Uint8Array<ArrayBuffer>> {
  const out = new Blob([bytes]).stream().pipeThrough(stream);
  return new Uint8Array(await new Response(out).arrayBuffer());
}

export async function shareLink(state: Omit<SharedState, "v">): Promise<string> {
  const json = new TextEncoder().encode(JSON.stringify({ v: VERSION, ...state }));
  const packed = await transform(json, new CompressionStream("deflate-raw"));
  return `${window.location.origin}${window.location.pathname}${PREFIX}${toBase64Url(packed)}`;
}

/** The shared state in the current URL, if any. Anything malformed is ignored: links are untrusted. */
export async function readShared(): Promise<SharedState | null> {
  const hash = window.location.hash;
  if (!hash.startsWith(PREFIX) || hash.length > MAX_LINK) return null;
  try {
    const json = await transform(fromBase64Url(hash.slice(PREFIX.length)), new DecompressionStream("deflate-raw"));
    const data: unknown = JSON.parse(new TextDecoder().decode(json));
    return valid(data) ? data : null;
  } catch {
    return null;
  }
}

/** Drop the fragment, so reloading the page does not replay the link again. */
export function clearShared() {
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

function valid(data: unknown): data is SharedState {
  if (typeof data !== "object" || data === null) return false;
  const d = data as Record<string, unknown>;
  return (
    d.v === VERSION &&
    typeof d.sql === "string" &&
    typeof d.sqlB === "string" &&
    typeof d.two === "boolean" &&
    Array.isArray(d.setup) &&
    d.setup.length <= MAX_STATEMENTS &&
    d.setup.every((s) => Array.isArray(s) && s.length === 2 && (s[0] === "A" || s[0] === "B") && typeof s[1] === "string")
  );
}
