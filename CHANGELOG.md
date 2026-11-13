# Changelog

All notable changes to the TaskFlow demo api-service (`demo-app`) are
documented here. The format follows Keep a Changelog; versions are the
GitHub Releases of this repo, each gated by the workspace's
release-check (see `release-assistant` in ai-workspace-kit).

## [1.2.0] - 2026-11-12

### Added

- Comments (TF-106): `POST` / `GET` list / `GET` one under
  `/v1/tasks/{task_id}/comments`. Comments are append-only — an edit
  is a new revision row carrying `supersedes_id` — and a revision
  must supersede the current head of its chain (a stale supersede is
  409 `conflict`; the check-and-insert is atomic, so chains cannot
  fork). The `comments` table and its `(task_id, id)` index are
  applied by the pre-start migration, like every schema change here.
- OpenAPI spec conformance suite (TF-107): the published spec in
  `taskflow-api-docs` is exercised against the running app in CI —
  route parity, per-operation response shapes, and the documented
  error scenarios — so the spec and the implementation cannot drift
  apart silently.

### Fixed

- `/health` now probes the `comments` table as well as `tasks`: a
  database that predates the comments migration reports `degraded`
  instead of reading healthy while comment routes return 503
  (found by the PR #8 review; pinned by a legacy-schema upgrade
  test).

## [1.1.0] - 2026-11-12

First release through the workspace's release process (release-check
verdict GO; rollback tag `pre-v1.1.0`).

### Added

- SQLite-backed task store (TF-101), applied by a pre-start,
  idempotent migration; `/health` reports the contract's degraded
  shape when the store is unreachable, and store failures answer
  503 `store_unavailable` in the error envelope.
- Board pagination (TF-102): `GET /v1/tasks` accepts a `status`
  filter with cursor pagination over a `(status, id)` index.
- Idempotent weekly digest (TF-103): a `digest_sends` ledger makes
  repeat sends safe; diagnostics record sanitized error codes only.
- Central request-validation handler (TF-105): framework validation
  failures answer 422 `validation_error` in the contract envelope
  instead of FastAPI's default shape, without echoing submitted
  values.
- CI: tests, Ruff lint, and a container build with a `/health`
  smoke test on every push and pull request.
