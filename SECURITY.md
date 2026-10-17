# Security

## Reporting a problem

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository rather than opening a public issue. Include steps to reproduce and the
version or commit you tested.

## Scope

Arcanum is an educational database engine. It is not meant to store sensitive data in production.

## Protections

**Isolation**

- Each session has its own database file and its own engine process, behind an unguessable session id.
- The engine runs SQL only against its own files. There are no functions that read or write other files,
  run programs or open network connections. SQL is parsed into a typed syntax tree, never assembled by
  string concatenation, and the engine refuses database files it does not recognise.
- The server and its engine processes exchange JSON only, never pickled objects.
- Session files are readable only by the server's user, are deleted when the session ends, and are
  cleared at startup.

**Resource limits** (all configurable, see `.env.example`)

- Per-client request rate, session creation rate and number of sessions; per-client and global
  WebSocket connections; total connections.
- Statement run time (an over-running engine process is stopped and restarted, and its database recovers
  from the log), statements running at once, rows a statement may hold in memory, temporary sort space,
  recursive query size, returned rows, SQL length, request body size, and database size.
- The parser refuses statements nested beyond a fixed depth, and `LIKE` runs in polynomial time whatever
  the pattern.

**No leaks**

- Unexpected errors return a generic message with a reference; details stay in the server log.
- Invalid requests are not echoed back. Access logs are off by default because URLs carry session ids.
- Responses send `nosniff`, `no-referrer`, frame denial and a strict Content Security Policy for the UI;
  CORS allows only configured origins and never credentials. WebSockets from other origins are refused.

## What the application cannot do on its own

No application can absorb a volumetric denial-of-service attack: a large flood fills the network link
before the server sees a request. A public deployment should sit behind a reverse proxy or CDN that
terminates TLS, filters floods and enforces connection limits (for example Cloudflare, or Caddy / nginx
with rate limiting), with `ARCANUM_TRUSTED_PROXIES` set to the proxy's address so per-client limits see
real client addresses, and `ARCANUM_HSTS=1`.
