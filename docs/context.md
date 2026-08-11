# Fleet — Context

Per REDESIGN.md §76. Covers `context/` as it actually exists after
Phases 3, 4, 5, and 8 of the redesign.

## Why context becomes a bottleneck

An agent workflow accumulates conversation history, tool outputs, files,
and errors over many steps. Eventually that exceeds what a model can
accept — REDESIGN.md §2's example: 80k tokens of accumulated context
against a 32k model window. Something has to decide what's actually
worth sending. Fleet's answer is to treat context as a resource with an
explicit budget, not an unbounded log that gets truncated arbitrarily
when it overflows.

## Context budget

A target token count (`CONTEXT_BUDGET_DEFAULT`, default 8192) that
`context/selection.py`'s policies pack candidate context into. **As of
Phase 14, this setting is dead config** — nothing in the live request
path calls `select_context()` or reads `CONTEXT_BUDGET_DEFAULT`; only
`ContextManager.total_tokens()` (an unbudgeted running count) feeds live
routing decisions (Phase 9). Budgeting is real, tested, and demonstrated
in `benchmarks/experiments/context_budgeting.py` — with real, measured
results in `docs/experiments.md` — but not reachable through the API.

## Context item model

`ContextItem` (`context/models.py`): `id`, `type`, `content`,
`token_count` (auto-computed via `estimate_tokens()` — a documented
~4-chars/token heuristic, not a real tokenizer), `created_at`,
`last_accessed_at`, `importance`, `relevance`, `source`, `agent_id`,
`workflow_id`, `artifact_id` (set when this item is a reference to a
large externalized artifact rather than raw content). `ContextType`:
`conversation`, `file`, `tool_result`, `error`, `memory`, `summary`,
`instruction`, `task_state`.

Storage is `ContextStore` (`context/store.py`) — in-memory, scoped by
`workflow_id`, **no expiry, no size cap**. This is a known, accepted
limitation (§14 of `docs/architecture.md`), not an oversight: Phase 3
deliberately deferred durability to Phase 6 (which gave memory Postgres,
not context — context stayed in-memory by design, since it's meant to be
workflow-scoped and short-lived, not permanent).

## Selection policies

`context/selection.py`, five policies exactly per REDESIGN.md §11:

| Policy | Behavior |
|---|---|
| `full` | Baseline — chronological, oldest first, until budget runs out. What a naive non-Fleet system does. |
| `recent` | Most-recent-first until budget runs out. |
| `relevance` | Highest-`relevance`-first until budget runs out. |
| `budget_aware` | Greedy pack by `importance / token_count` density. |
| `hybrid` (recommended) | Greedy pack by weighted `relevance + recency + importance`, normalized within the candidate set, divided by `token_count`. |

The weighted formula omits §10's `workflow_weight * workflow_match`
term deliberately: every candidate already comes pre-scoped to one
workflow via `ContextStore.list_for_workflow()`, so that term would
always be a constant 1.0 — carrying it as dead weight would be
misleading, not more correct.

Packing is greedy (first-fit-decreasing by the policy's key) per §12's
explicit instruction not to build a full knapsack solver "unless
benchmarking shows it is necessary." It hasn't.

**Real, measured result** (`docs/experiments.md`, Experiment 1): against
a 21-item pool (20 irrelevant filler items + 1 relevant file), `hybrid`
with a 400-token budget used 39% fewer actual tokens than `full` while
still correctly solving the task — the mechanism works as designed, in a
real run against a real model, just not one triggered by an actual HTTP
request yet.

## Compression

`context/compression.py` (Phase 8) — the one place `context/` calls the
inference layer. `compress_items()` takes a group of `ContextItem`s and
an injected `Summarizer` (async `str -> str` callable), returning a
`CompressionResult` with `tokens_before`/`tokens_after`/`tokens_saved`.
`llm_summarizer()` is the real implementation, routing through
`router.load_balancer` with the same `pick_worker()`-outside-try/except
fix pattern used everywhere else in this codebase. `ContextManager.
compress_old_context()` replaces the oldest items for a workflow with a
single summary — explicit, caller-invoked, never automatic.

Live-verified once, manually, outside the test suite: `llm_summarizer()`
produced a coherent real summary from real conversation-shaped text via
a real Ollama call (see the Phase 8 entry in `docs/migration-plan.md`).
Like selection, **not called by any live route**.

## Artifacts

`context/artifacts.py` (Phase 5) — large content (`file`, `tool_output`,
`log`, `document`) gets stored as an `Artifact` with a deterministic,
keyword-based summary (`summarize_text()` — no LLM call, mirrors §33's
worked example), and only a small `ContextItem` reference
(`type=SUMMARY`, carrying `artifact_id`) goes into context. The full
artifact stays retrievable by ID or line-range excerpt
(`ArtifactStore.get_excerpt()`). Tested to guarantee reference items are
at least 10x smaller than the artifact they point to. Same status as
selection and compression: real, tested, **not live-wired**.

## Caching and invalidation

REDESIGN.md §26/§27 describe an optional context cache with TTL and
explicit invalidation (e.g. when an agent modifies a file it previously
read). **Not built.** No phase in the 15-phase checklist (§72) actually
assigns this — §26/§27 exist in the architecture narrative but were
never itemized as a deliverable, so unlike other gaps in this document,
this isn't "built but not wired," it's simply not implemented at all.

## Benchmarks

`benchmarks/experiments/context_budgeting.py` — see `docs/experiments.md`
Experiment 1 for the full writeup, real results, and limitations.
`scripts/benchmark_context_selection.py` (Phase 4) is a lighter,
purely-synthetic mechanics check (does packing respect the budget, how
many tokens does each policy save) — a sanity check for the algorithm,
not the task-success experiment.
