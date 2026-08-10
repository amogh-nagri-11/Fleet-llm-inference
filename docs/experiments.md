# Fleet v2 — Experiments

Per REDESIGN.md §61: hypothesis, setup, variables, metrics, results,
limitations, conclusion for every experiment. All numbers below are from
actually running the code in `benchmarks/experiments/` on this machine —
none are invented or projected (§39/§60's standing rule, honored
throughout this whole redesign).

## Scope note — why 3 experiments, not 6

REDESIGN.md §53-58 describes six experiments. Three of them (§53 full
history vs. budgeted, §54 tool output explosion, §56 memory retrieval)
compare *context strategies applied to what's actually sent to a model*.
As of Phase 13, nothing in the live gateway request path applies context
selection, artifact summarization, or memory retrieval to outgoing
prompts — `gateway/routes.py` only records raw prompts and counts tokens
for routing (confirmed by the Phase 12 brutal audit, still true).

So three experiments are genuinely runnable, all using real code and (for
two of them) a real model:

- **Experiment 1** (§53, adapted) — runs `context/selection.py` for real
  against a real task and a real model, at the *library* level rather
  than through the gateway. This is the closest honest equivalent to §53
  available today, and directly informs the redesign's core thesis.
- **Experiment 5** (§57) — context-aware routing, which *is* live-wired
  (Phase 9) and runs through the real production code path.
- **Experiment 6** (§58) — agent bursts, fully live against the real
  gateway, reusing the Phase 12 simulator.

§54 (tool output explosion) and §56 (memory retrieval) are not run here
— both would need to fabricate a comparison of what's *not* actually
happening in production, which would misrepresent the system rather than
measure it. They become real experiments once `context/artifacts.py` and
`memory/retrieval.py` are wired into live request handling — a separate,
larger piece of work noted as a gap in the Phase 13 migration plan entry,
not attempted here.

---

## Experiment 1 — Full History vs. Budget-Aware Context

**Hypothesis.** For a task where only a small fraction of accumulated
context is actually relevant, budget-aware selection
(`context/selection.py`, Phase 4) sends far fewer tokens than dumping the
full history, without losing task success.

**Setup.** A synthetic 21-item context pool: 20 unrelated filler
Q&A turns (an agent that's been chatting about pasta recipes, CSS,
geography, etc.) plus the real, intentionally-buggy
`examples/coding_agent/sandbox_repo/calculator.py` (Phase 11's planted
bug — `add()` subtracts instead of adding). Two strategies compared:
`full` (policy, effectively unlimited budget — everything goes in) vs.
`hybrid` (policy, 400-token budget — forces real trimming). Both
strategies' selected context + a fixed task prompt ("what's wrong with
the add function?") are sent to a real `llama3:latest` via a direct
`OllamaClient` call (not through the gateway — see scope note above).

**Variables.** Selection policy, token budget.

**Metrics.** Items selected, estimated tokens (from `select_context`),
actual tokens (Ollama's real `prompt_eval_count`), latency, task success.

**Task success criterion.** Objective keyword check (REDESIGN.md §61 —
not a subjective LLM judge) on whether the model's response correctly
identifies the bug: does it mention "subtract", "minus", the wrong
operator, or similar (`benchmarks/experiments/context_budgeting.py`'s
`task_succeeded()`).

**Results** (single run, `llama3:latest`, this machine):

| Policy | Items | Tokens (est.) | Tokens (actual) | Latency | Task success |
|---|---|---|---|---|---|
| full   | 21/21 | 654 | 629 | 34.3s | True |
| hybrid | 13/21 | 399 | 396 | 22.7s | True |

Both strategies correctly diagnosed the bug. `hybrid` used **39% fewer
tokens** (396 vs. 629 actual) and was **34% faster** (22.7s vs. 34.3s) —
it excluded 8 of the 20 filler items while keeping the actually-relevant
file content, exactly the intended behavior.

**Limitations.**
- Single run, single task, single model. No statistical confidence
  interval — this is a demonstration that the mechanism works as
  designed, not a rigorous multi-trial study.
- Latency on this machine (CPU-only Ollama in WSL) is dominated by model
  load/inference time, not context-processing overhead — the token
  reduction is the more portable finding than the specific latency
  numbers.
- "Task success" is binary and single-criterion. A model could pass this
  keyword check while still producing a subtly wrong analysis elsewhere
  in its response.
- Not run through the live gateway (see scope note) — this measures
  `context/selection.py`'s real behavior, not the deployed request path.

**Conclusion.** The core mechanism works as designed: budget-aware
selection meaningfully reduces tokens sent to the model for a task with
mixed relevant/irrelevant context, without sacrificing the correct
answer, in a real (not simulated) run. This doesn't prove Fleet's overall
thesis in production, since selection isn't live-wired yet — it proves
the building block that thesis depends on actually does what it claims.

---

## Experiment 5 — Context-Aware Routing

**Hypothesis.** Given workers with different context capacities, Fleet
correctly restricts routing to workers that can actually hold a
request's context — a merely busy (but capable) worker stays eligible;
an incapable one doesn't, regardless of load.

**Setup.** Three synthetic workers matching REDESIGN.md §41's own
example exactly: Worker A (32k context, low load), Worker B (8k
context), Worker C (32k context, 50 active requests — heavily loaded).
Real `LoadBalancer`/`pick_worker()` calls (`router/load_balancer.py`) —
only the workers themselves are synthetic, since this dev environment
has exactly one real Ollama instance and §57 needs several with
different capacities.

**Variables.** Request context size (100 / 16,000 / 100,000 tokens).

**Metrics.** Eligible worker set per scenario, which worker got picked,
whether oversized requests are correctly rejected.

**Results** (this machine):

| Scenario | Eligible | Picked | Matches §41 |
|---|---|---|---|
| small (100 tok) | A, B, C | A | ✓ |
| medium (16,000 tok) | A, C | A | ✓ |
| oversized (100,000 tok) | (none) | rejected (`NoCapacityError`) | ✓ |

Worker C (heavily loaded, 50 active requests) stayed eligible for both
the small and medium scenarios — exactly per §41: filtering is on hard
capacity only, load doesn't affect eligibility.

**Limitations.** Workers are synthetic (see setup) — this validates the
routing *logic* precisely, not real multi-worker load balancing under
real concurrent traffic (that would need real hardware this environment
doesn't have).

**Conclusion.** Context-aware routing behaves exactly as REDESIGN.md §41
specifies, using the actual production code path, for every tested
scenario including the edge case (no worker has enough capacity).

---

## Experiment 6 — Agent Bursts

**Hypothesis.** As concurrent agent count increases, P95 latency and
throughput degrade in a way that reflects real queueing behind a fixed
worker pool, not silent failure or crashes.

**Setup.** Reuses `scripts/simulate.py` (Phase 12) directly — real HTTP
traffic against the real running gateway (`client/fleet_client.py`),
`mixed` workload, two scales.

**Variables.** Concurrent agent count (2, 4).

**Metrics.** Requests completed, failures, throughput, P50/P95/P99
latency.

**Results** (this machine, single real Ollama worker, 20s duration each):

| Agents | Requests | Failed | Throughput | P50 | P95 | P99 |
|---|---|---|---|---|---|---|
| 2 | 5 | 0 | 0.174 req/s | 9,935ms | 13,395ms | 13,550ms |
| 4 | 7 | 0 | 0.183 req/s | 20,756ms | 23,803ms | 23,873ms |

Zero failures at both scales — the system degrades gracefully (higher
latency) rather than dropping requests. P50 roughly doubled going from 2
to 4 agents, consistent with requests queueing behind one serially-
processing Ollama worker rather than any Fleet-side bottleneck.

**Limitations.** This is the experiment REDESIGN.md §58 most explicitly
scales to 10-500 agents against a real multi-worker fleet — this
environment has exactly one CPU-only Ollama worker in WSL, so 2 and 4
agents were chosen to keep a real run finishing in a reasonable time.
These are genuine measured numbers at this small scale, not a stand-in
for what §58's larger scenarios would show against real hardware.
Throughput is capacity-bound by the single worker, not representative of
what Fleet's scheduler would achieve with more workers available.

**Conclusion.** At the scale this environment can support, Fleet handles
concurrent agent bursts without failures, and the latency degradation
pattern is consistent with real single-worker queueing rather than a
Fleet-side defect. Confirming the same holds at REDESIGN.md's intended
scale needs a multi-worker environment this setup doesn't have.
