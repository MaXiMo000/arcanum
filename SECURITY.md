# Security

## Reporting a problem

Please report security issues privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository rather than opening a public issue. Include steps to reproduce and the
version or commit you tested.

## Scope

Arcanum is an educational database engine. It is not meant to store sensitive data in production.

- The engine validates its file header and page structure and refuses files it does not recognise.
- SQL is parsed by a hand-written parser into a typed syntax tree; queries are never built by
  string concatenation.
- The API server gives each session its own database file and engine process, behind an unguessable
  session id. Statement run time, database size, returned rows, SQL length, the number of sessions and
  idle time are all limited, and browser origins are restricted by configuration.
- The engine runs SQL only against its own files: there are no functions that read or write other files,
  run programs or open network connections.
