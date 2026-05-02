# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-05-02

### Changed

- `source.thread` and `source.process` now ship as `{ id, name }` objects
  instead of opaque `repr()` strings, so they're searchable as structured
  fields in Better Stack.
- `source.file` extracts just the filename instead of the full Loguru
  `RecordFile` repr.

### Added

- README example payload showing the JSON shape Better Stack receives.
- GitHub Actions workflow scaffold (Python 3.9–3.12) — manual trigger only
  for now.

## [0.1.0] — 2026-05-02

### Added

- `BetterStackSink`: thread-safe queue + background flusher.
- Bind/contextualize fields surface as structured `context.*` on each record.
- Exception tracebacks ship with the originating message.
- Graceful shutdown via `close()` / `register_atexit()`.
- Tests stand up a real HTTP server on localhost (no `urllib` mocking).
