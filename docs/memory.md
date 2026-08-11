# Fleet — Memory

Per REDESIGN.md §77. Covers `memory/` as it actually exists after Phases
6 and 7. Precise claim, per §79: Fleet provides infrastructure for
storing and retrieving agent memory under explicit resource budgets — it
does not "solve agent memory" as a general capability.

## Working memory

Short-lived, represents current task state (§15's example: current
objective, current files, current failure). `MemoryKind.WORKING`.
Supports an optional `ttl_seconds` at write time
(`MemoryManager.record_working()`), which sets `expires_at`;
`MemoryStore.purge_expired()` removes stale working memory. Nothing
calls `purge_expired()` automatically — no background task runs it; a
caller (or a future scheduled job) has to invoke it.

## Episodic memory

Longer-lived, records specific useful past events (§16's example: "test
failed because token expiration was checked twice"). `MemoryKind.
EPISODIC`, never expires (`expires_at` stays `None`). Written explicitly
by the caller — Fleet does not auto-extract memories from arbitrary
text or tool output (§14's constraint, upheld throughout: no automatic
memory extraction anywhere in this codebase).

## Semantic memory

**Deferred** (`REDESIGN.md` §0.2) — not partially built, not scheduled
for a later phase within this effort. `MemoryKind` only has `WORKING`
and `EPISODIC`. No embeddings, no vector index, no similarity search
dependency anywhere in `memory/`.

## Retrieval

`memory/retrieval.py` (Phase 7) — `retrieve_relevant_memories()`
composes ranking and budgeting over an already-fetched candidate pool
(`MemoryManager.list_for_workflow()` does the actual DB read).
`select_within_budget()` greedy-packs by score/token-cost density,
mirroring `context/selection.py`'s approach exactly and reusing
`context.models.estimate_tokens()` rather than a second heuristic.
`to_context_items()` is the "inject" step — converts selected
`MemoryItem`s into `ContextItem`s (`type=MEMORY`).

`MemoryManager.get_relevant_memories()`/`get_relevant_context()` expose
the full retrieve→rank→budget(→inject) pipeline as manager methods.
**Not called by any live route** — same status as context selection,
artifacts, and compression (`docs/context.md`). The pipeline is real and
integration-tested against a live Postgres instance; nothing in
`gateway/routes.py` invokes it yet.

## Ranking

`memory/ranking.py` — REDESIGN.md §21's formula, `memory_score =
relevance + importance + recency + access_frequency`, as a weighted sum
(equal weights by default; §21 doesn't commit to specific values the way
§10 does for context selection). `recency` is normalized from
`last_used_at`, not `created_at` — decay is about staleness of *usage*.
`relevance` uses `lexical_relevance()`: deterministic word-overlap
(Jaccard similarity) between a query string and the memory's content —
explicitly not semantic search, since embeddings are deferred. This is
the same "deterministic and measurable first" rule §10 established for
context selection, applied here because memory retrieval needs *some*
relevance signal and none was stored at write time (relevance is
request-specific, so §20 doesn't list it as stored metadata the way
`importance` is).

## Lifecycle

`created_at`, `last_used_at` (updated by `MemoryStore.get()`, alongside
`access_count`), `expires_at` (working memory only). No richer state
machine than that — no `ARCHIVED`/`SUMMARIZED` states like
`docs/architecture.md` notes context items also lack.

## Privacy, isolation, deletion

**Not enforced.** REDESIGN.md §68/§69 describe scoped memory isolation
(global/organization/agent/workflow) and explicit anti-cross-contamination
guarantees ("memory belonging to Agent A must not accidentally be
returned to Agent B"). What actually exists: `list_for_workflow()`
filters by the `workflow_id` string a caller passes in — there's no
enforcement preventing a caller from passing a `workflow_id` it doesn't
"own." Isolation today is "callers are expected to pass the right ID,"
not an enforced boundary.

Deletion: `MemoryStore.delete(item_id)` removes a single item by ID.
There is **no bulk "delete everything for this workflow"** operation —
`REDESIGN.md` §70's "a workflow should be able to delete its associated
state" isn't fully implemented; a caller would have to list then delete
each item individually. `ContextStore` (a separate subsystem) does have
`clear_workflow()`; `MemoryStore` doesn't have the equivalent yet.

## What this means in practice, today

An application calling `memory_manager.record_working(...)` or
`record_episodic(...)` directly (as a Python library, in-process — since
nothing exposes this over HTTP yet) gets real, durable, Postgres-backed
storage with real ranking and budgeting on retrieval. What it does not
get: automatic capture from conversation, enforced multi-tenant
isolation, semantic search, or a way to fully erase a workflow's memory
in one call. All of that is either explicitly deferred or a documented,
honest gap — not a hidden one.
