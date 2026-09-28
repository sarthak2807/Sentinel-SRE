# Sentinel — AI SRE Copilot

An agent that watches a backend service, catches anomalies before they become
outages, explains the root cause in plain language, and proposes a fix that a
human approves before anything runs. Built on NVIDIA Nemotron models served
through Nebius Token Factory.

Built for the Nebius × NVIDIA Global AI Hackathon — Coding and Agentic
Engineering track.

## The problem

On-call engineers spend the first 10–15 minutes of any incident just working out
what broke. Sentinel compresses that window: by the time a human opens their
laptop, it has already read the logs, checked what happened the last time this
shape of failure appeared, written a diagnosis, and staged a fix behind an
approval button.

## What it does

1. **Watches** — the target service streams structured logs and metrics to
   Sentinel's `/ingest` endpoint.
2. **Detects** — robust statistics (median/MAD z-scores, plus a monotonic-drift
   check) flag error spikes, latency jumps, memory growth and query fan-out.
   No model runs here: detection has to be cheap, fast and deterministic.
3. **Triages** — every anomaly goes to **Nemotron Nano** for a cheap real-or-noise
   call. Most anomalies stop here.
4. **Recalls** — surviving anomalies are matched against past incidents in a
   vector store, so the agent can say "this is the auth timeout from February."
5. **Diagnoses** — **Nemotron Super**, or **Ultra** when the case is genuinely
   ambiguous, produces a structured root-cause analysis and one remediation
   chosen from an allowlist.
6. **Waits** — the fix is shown as the literal command it will run, with its
   blast radius and rollback. Nothing executes until a human clicks Approve.
7. **Executes and verifies** — the approved action runs in a sandbox, then
   Sentinel reads the service's health back to check the fix actually worked.
8. **Remembers** — the resolved incident is written back into the vector store,
   so the second occurrence is diagnosed faster and with higher confidence.

## Why this shape

Three decisions are worth defending in a judging room.

**Model routing is the architecture, not a feature.** Anomalies fire far more
often than incidents happen. Running a 253B reasoning model on every blip is
both slow and wasteful, so Nano filters and Super/Ultra reason. The escalation
rule lives in one function (`pick_deep_tier`) and the dashboard shows the
per-tier call count, latency and spend, so the claim is visible rather than
asserted.

**The model never writes a command.** It picks a name from a registry in
`sentinel/actions.py` and fills typed parameters. Anything it invents fails
validation before it reaches the sandbox, and a human still has to approve.
`tests/test_executor.py` is the proof.

**A fix is not done when the command exits 0.** After execution, Sentinel reads
the service's own health endpoint and reports whether the signals actually
recovered. An incident only closes — and only enters memory — when they do.

## Architecture

```
  sample service ──logs, metrics──► /ingest
                                      │
                              anomaly detector          (statistics, no LLM)
                                      │
                         Nemotron Nano triage           real or noise?
                                      │
                        incident memory (RAG)           have we seen this?
                                      │
                 Nemotron Super / Ultra diagnosis       root cause + one fix
                                      │
                          ◄── approval gate ──►         a human decides
                                      │
                     sandboxed executor + verify        did it actually work?
                                      │
                         write back to memory           next time is faster
```

Full walkthrough in [`docs/architecture.md`](docs/architecture.md).

## Layout

```
sentinel/
├── config.py        env-driven settings
├── models.py        pydantic contracts every stage speaks
├── bus.py           in-process pub/sub → dashboard websocket
├── nemotron.py      Token Factory client, JSON recovery, cost ledger
├── detector.py      statistical anomaly detection
├── memory.py        SQLite + embeddings vector store (RAG)
├── reasoning.py     triage, escalation routing, diagnosis, chat
├── actions.py       the remediation allowlist — the safety boundary
├── sandbox.py       Nebius Sandboxes backend + local fallback
├── executor.py      approval gate, execution, verification, audit log
├── pipeline.py      the orchestrator
├── server.py        FastAPI: ingest + API + websocket + dashboard
├── offline.py       deterministic fallbacks so the demo never dies
├── prompts/         triage.txt, diagnose.txt, chat.txt
└── web/index.html   the on-call dashboard
sample_service/      the FastAPI app we break on purpose
scripts/             inject_fault.py, setup_vector_store.py
incidents/           seed incidents for the memory store
tests/               detector, reasoning, executor, memory
```

## Setup

Requires Python 3.10+ and a Nebius Token Factory API key from
[tokenfactory.nebius.com](https://tokenfactory.nebius.com).

```bash
git clone <repo-url> && cd sentinel
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add NEBIUS_API_KEY
python scripts/setup_vector_store.py
```

## Running

Two processes. Terminal one:

```bash
python sample_service/app.py      # the service being watched, :8000
```

Terminal two:

```bash
python -m sentinel.server         # agent + dashboard, :8080
```

Open <http://localhost:8080>. Give it about a minute to learn a baseline, then
break something:

```bash
python scripts/inject_fault.py --type memory_leak
python scripts/inject_fault.py --type dependency_timeout
python scripts/inject_fault.py --type n_plus_one
python scripts/inject_fault.py --clear
```

Watch the incident stream: detection, triage, recall, diagnosis, then the
approval gate. Approve the fix and watch the verification step confirm the
signals came back to baseline.

## Tests

```bash
pytest -q
```

The detector and executor tests run without any network access.

## Configuration worth knowing

| Variable | Default | What it does |
|---|---|---|
| `NEBIUS_API_KEY` | — | Without it, Sentinel runs on a deterministic offline stub and labels every response as such |
| `AUTO_APPROVE` | `false` | Set `true` only to demo fully autonomous remediation |
| `SANDBOX_MODE` | `local` | `contree` runs fixes in a Nebius Sandbox instead of a local subprocess |
| `DETECT_COOLDOWN_SECONDS` | `45` | Minimum gap between pages for the same signal fingerprint |

## Limitations

- The statistical detector has no seasonality model, so a genuine traffic pattern
  that changes hourly would need a real baseline store.
- Verification asks the service about its own health. In production you would
  re-read the same metric series the anomaly was raised on.
- The remediation allowlist is deliberately small. Growing it is a review step,
  not a configuration change.

## License

MIT — see [LICENSE](LICENSE).
