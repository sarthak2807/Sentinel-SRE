# Architecture

## The loop

```
 sample-service                     Sentinel (one process)
 ──────────────                     ────────────────────────────────────────
 every request  ──► POST /ingest ──► detector.observe()
 every 2s heartbeat                      │
                                         │  every 2s
                                         ▼
                                   detector.evaluate()
                                         │  Anomaly | None
                                         ▼
                              reasoning.triage_anomaly()          Nemotron Nano
                                         │
                            is_real=false ──► dismissed, nobody paged
                                         │
                                         ▼
                            memory.search_for_anomaly()           embeddings + cosine
                                         │
                                         ▼
                              reasoning.diagnose()                Super or Ultra
                                         │  Diagnosis + ProposedFix
                                         ▼
                              executor.preview()                  allowlist + validation
                                         │
                                  status = awaiting_approval
                                         │
                              ◄── human clicks Approve ──►
                                         │
                              executor.execute()                  sandbox
                                         │
                              executor._verify()                  read health back
                                         │
                              memory.remember_resolved()          write back
```

Every transition publishes to `bus`, and the dashboard's websocket is a pure
subscriber. The UI holds no state the agent does not have.

## Why detection has no model in it

A model in the detection path would mean one inference call per telemetry batch.
At even modest traffic that is thousands of calls per hour to answer a question
statistics already answers well, and it makes the demo non-deterministic.

The detector uses median and MAD rather than mean and standard deviation,
because one eight-second GC pause would otherwise inflate the baseline spread
enough to hide the real incident that follows it. `test_single_outlier_does_not_page`
covers exactly that case.

Memory leaks never spike, so they get their own check: split the baseline window
in half, compare medians, and require the second half to be monotonic. Without
that, a leak is invisible to a z-score until the OOM kill.

## Model routing

| Stage | Tier | Why |
|---|---|---|
| Triage | Nano | High volume, low stakes, near-binary decision. Wrong answers are recoverable because the fallback is to escalate. |
| Diagnosis | Super | Long context (signals + logs + retrieved incidents + action menu), needs real reasoning, runs once per real incident. |
| Diagnosis | Ultra | Only when the case is ambiguous: low-confidence critical triage, three or more correlated signals, or nothing useful in memory. |
| Chat | Super | Interactive latency matters; the state is already structured. |

`pick_deep_tier()` is the whole escalation policy, in eight lines, so it can be
tuned and unit-tested rather than buried in a prompt.

## Working with Nemotron reasoning models

The Nemotron models on Token Factory are reasoning models. They emit their chain
of thought into `message.reasoning_content` and will leave `message.content`
empty — especially under a tight `max_tokens`. Code that reads only `.content`
gets silent blanks. `NemotronClient._extract` reads both, preferring `content`.

For the same reason, JSON cannot be trusted to arrive clean. `_first_json_object`
brace-matches (string-aware, so `{p95}` inside a sentence does not break it) and
falls back to a single repair turn at temperature 0. Everything parsed then goes
through a pydantic model, so a malformed field degrades instead of crashing the
incident.

## The safety boundary

```
model output ──► {"action": "set_timeout", "params": {"seconds": 2}}
                         │
                 REGISTRY lookup            unknown name → rejected
                         │
                 action.validate(params)    unknown or wrong-typed param → rejected
                         │
                 action.build(params)       argv list, never a shell string
                         │
                 human approval             recorded with the approver's name
                         │
                 sandbox.run(argv)          no shell=True, 20s cap, output truncated
                         │
                 audit.log                  append-only record of what ran
```

The model's output is treated as a request, not an instruction. Adding a new
capability means adding an `Action` to `sentinel/actions.py`, which is a code
review.

## Sandbox backends

`SANDBOX_MODE=contree` runs remediation inside a Nebius Token Factory Sandbox.
Sandboxes support Git-like forking, so Sentinel forks before it applies a change:
if verification fails, the branch is discarded rather than leaving a half-applied
mutation behind.

`SANDBOX_MODE=local` runs the same argv in a capped subprocess. Both return the
same `ExecutionResult`, and the dashboard shows which one ran.

## Incident memory

SQLite plus a float32 blob per row, cosine similarity in numpy. At hackathon
scale the corpus is a few hundred documents; a vector database would add a
container and a failure mode without changing a single retrieval result. The
interface is two methods, so swapping in Qdrant later touches one file.

The part that matters is the write-back. `remember_resolved()` turns every
closed incident into a retrievable document, which is what makes the second
occurrence of a fault faster to diagnose than the first — and that is a
demonstrable claim, not a hand-wave.

## Failure modes and what happens

| Failure | Behaviour |
|---|---|
| No API key | Deterministic offline stub, every response tagged `offline: true` in the UI |
| Token Factory unreachable mid-incident | Three retries with backoff, then offline stub, incident continues |
| Model returns unparseable JSON | One repair turn, then the stage fails with the incident left open |
| Model proposes an unknown action | Rejected at `executor.preview()`, incident stays open, trace records why |
| Sandbox unavailable | Falls back to the local sandbox rather than losing the diagnosis |
| Service health unreachable during verify | `verified = null`, incident does not close |
