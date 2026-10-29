# Sentinel — AI SRE Copilot

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![NVIDIA Nemotron](https://img.shields.io/badge/Powered%20by-NVIDIA%20Nemotron-76B900.svg)](https://www.nvidia.com/en-us/research/nemotron/)
[![Nebius Token Factory](https://img.shields.io/badge/Served%20on-Nebius%20Token%20Factory-00D4AA.svg)](https://tokenfactory.nebius.com)

> **An agent that watches a backend service, catches anomalies before they become outages, explains the root cause in plain language, and proposes a fix that a human approves before anything runs.**

Built for the **Nebius × NVIDIA Global AI Hackathon** — **Coding and Agentic Engineering Track**.

---

## 🎯 The Problem

On-call engineers spend the first 10–15 minutes of any incident doing archaeology: What changed? Is it us or upstream? Did this happen before? Sentinel compresses that window — by the time a human opens their laptop, it has already read the logs, checked what happened the last time this shape of failure appeared, written a diagnosis, and staged a fix behind an approval button.

---

## ✨ What It Does

| Stage | Model | Purpose |
|-------|-------|---------|
| **1. Watch** | — | Target service streams structured logs/metrics to `/ingest` |
| **2. Detect** | — | Robust statistics (median/MAD z-scores, monotonic drift) flag error spikes, latency jumps, memory growth, query fan-out. *No LLM — detection must be cheap, fast, deterministic* |
| **3. Triage** | **Nemotron Nano** | Cheap real-or-noise call. Most anomalies stop here |
| **4. Recall** | — | Vector-store RAG matches against past incidents |
| **5. Diagnose** | **Nemotron Super / Ultra** | Structured root-cause analysis + one remediation from allowlist. **Ultra only when genuinely ambiguous** (low-confidence critical, ≥3 correlated signals, or no memory match) |
| **6. Approve** | Human | Fix shown as literal command with blast radius & rollback. *Nothing executes until a human clicks Approve* |
| **7. Execute & Verify** | — | Sandbox run → reads service health back to confirm signals recovered |
| **8. Remember** | — | Resolved incident written back to vector store — next occurrence diagnosed faster |

---

## 🏗 Architecture

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
                          ◄── approval gate ──►         human decides
                                      │
                     sandboxed executor + verify        did it actually work?
                                      │
                         write back to memory           next time is faster
```

Full architecture walkthrough: [`docs/architecture.md`](docs/architecture.md)

---

## 🎬 Demo Video

**[▶ Watch the demo on YouTube](https://youtu.be/a0OrVer6hx4)**

The demo shows:
- **0:00–0:20** — Problem statement & baseline learning
- **0:20–0:40** — Fault injection (`n_plus_one` query fan-out)
- **0:40–1:20** — Detection & Nano triage (cost/latency visible in dashboard)
- **1:20–2:00** — Memory recall & Super diagnosis (root cause + proposed fix)
- **2:00–2:35** — Approval gate → sandbox execution → verification
- **2:35–2:50** — Memory write-back; chat confirms faster next time

---

## 🔑 Key Innovations

### 1. Model Routing IS the Architecture
Anomalies fire far more often than incidents happen. Running a 253B reasoning model on every blip is slow and wasteful. Nano filters; Super/Ultra reason. The escalation rule lives in one function (`pick_deep_tier`) and the dashboard shows per-tier call count, latency, and spend — **the claim is visible, not asserted**.

### 2. The Model Never Writes a Command
It picks a name from a registry in [`sentinel/actions.py`](sentinel/actions.py) and fills typed parameters. Anything it invents fails validation before reaching the sandbox, and a human still has to approve. [`tests/test_executor.py`](tests/test_executor.py) is the proof.

### 3. A Fix Isn't Done When the Command Exits 0
After execution, Sentinel reads the service's own health endpoint and reports whether the signals actually recovered. An incident only closes — and only enters memory — when they do.

### 4. Read-Only Source Support (AWS CloudWatch)
Sentinel can observe external infrastructure (AWS CloudWatch metrics & logs) without ever calling a mutating AWS API. Incidents from read-only sources are diagnosed and escalated as tickets — **never remediated**. This is a deliberate architectural boundary for production safety.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- A **Nebius Token Factory API key** from [tokenfactory.nebius.com](https://tokenfactory.nebius.com)

### Installation

```bash
git clone https://github.com/your-username/sentinel.git
cd sentinel
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # Add your NEBIUS_API_KEY
python scripts/setup_vector_store.py --reset
```

### Running (Two Terminals)

**Terminal 1 — The service being watched:**
```bash
python sample_service/app.py
# Runs on http://localhost:8000
```

**Terminal 2 — Sentinel (agent + dashboard):**
```bash
python -m sentinel.server
# Runs on http://localhost:8080
```

Open **http://localhost:8080** — give it ~90 seconds to learn a baseline, then break something:

```bash
python scripts/inject_fault.py --type memory_leak
python scripts/inject_fault.py --type dependency_timeout
python scripts/inject_fault.py --type n_plus_one
python scripts/inject_fault.py --clear    # reset all faults
```

Watch the incident stream: detection → triage → recall → diagnosis → approval → execution → verification → memory write-back.

---

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NEBIUS_API_KEY` | *required* | Without it, Sentinel runs on a deterministic offline stub |
| `NEBIUS_BASE_URL` | `https://api.tokenfactory.nebius.com/v1/` | Token Factory endpoint |
| `NEMOTRON_NANO` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Triage model |
| `NEMOTRON_SUPER` | `nvidia/nemotron-3-super-120b-a12b` | Diagnosis model |
| `NEMOTRON_ULTRA` | `nvidia/Llama-3_1-Nemotron-Ultra-253B-v1` | Escalation model |
| `AUTO_APPROVE` | `false` | Set `true` only for fully autonomous demo |
| `SANDBOX_MODE` | `local` | `contree` = Nebius Sandboxes (VM-isolated, forkable) |
| `SENTINEL_AWS_ENABLED` | `false` | Enable AWS CloudWatch source |
| `SENTINEL_AWS_MODE` | `mock` | `mock` or `live` (requires boto3 + credentials) |
| `DETECT_COOLDOWN_SECONDS` | `45` | Minimum gap between pages for same fingerprint |

Full config in [`sentinel/config.py`](sentinel/config.py).

---

## 🧪 Tests

```bash
pytest -q
```

The detector and executor tests run without any network access.

---

## 📁 Project Layout

```
sentinel/
├── config.py           # env-driven settings
├── models.py           # pydantic contracts every stage speaks
├── bus.py              # in-process pub/sub → dashboard websocket
├── nemotron.py         # Token Factory client, JSON recovery, cost ledger
├── detector.py         # statistical anomaly detection
├── memory.py           # SQLite + embeddings vector store (RAG)
├── reasoning.py        # triage, escalation routing, diagnosis, chat
├── actions.py          # the remediation allowlist — the safety boundary
├── sandbox.py          # Nebius Sandboxes backend + local fallback
├── executor.py         # approval gate, execution, verification, audit log
├── pipeline.py         # the orchestrator
├── server.py           # FastAPI: ingest + API + websocket + dashboard
├── offline.py          # deterministic fallbacks so demo never dies
├── prompts/            # triage.txt, diagnose.txt, chat.txt, postmortem.txt
└── web/index.html      # the on-call dashboard
sample_service/         # the FastAPI app we break on purpose
scripts/                # inject_fault.py, setup_vector_store.py
incidents/              # seed incidents + generated postmortems
tickets/                # escalation tickets for read-only sources
tests/                  # detector, reasoning, executor, memory
docs/                   # architecture.md, demo_script.md
```

---

## 🔒 Safety & Security

- **No shell injection** — model output → allowlist lookup → typed params → argv list → sandbox (no `shell=True`)
- **Human-in-the-loop** — every remediation requires explicit approval (unless `AUTO_APPROVE=true`)
- **Read-only sources** — AWS CloudWatch incidents are diagnosed but never acted on
- **Audit log** — every execution recorded with incident ID, action, params, approver, result
- **Sandbox isolation** — Nebius Sandboxes support Git-like forking; failed verification discards the branch

---

## 💡 NVIDIA Nemotron Usage

| Model | Role | Why This Tier |
|-------|------|---------------|
| **Nemotron 3 Nano (30B)** | Triage | High volume, near-binary decision, wrong answers recoverable (fallback = escalate) |
| **Nemotron 3 Super (120B)** | Diagnosis | Long context (signals + logs + memory + actions), needs real reasoning, runs once/incident |
| **Nemotron Ultra (253B)** | Escalation | Only for genuinely ambiguous cases: critical + low confidence, ≥3 correlated signals, no memory match |

**Token Factory integration:**
- All inference via Nebius Token Factory OpenAI-compatible endpoint
- Handles Nemotron reasoning models correctly (reads both `content` and `reasoning_content`)
- Robust JSON extraction with brace-matching + single repair turn
- Per-call cost ledger displayed live in dashboard

---

## ☁️ Nebius Services Used

| Service | How It's Used |
|---------|---------------|
| **Token Factory** | Serves NVIDIA Nemotron Nano/Super/Ultra + Qwen3-Embedding-8B |
| **Nebius Sandboxes** (`contree` mode) | VM-isolated remediation execution with fork/rollback |
| **Serverless Endpoints** (optional) | Can serve the dashboard API if deployed |

---

## 📝 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Acknowledgments

- **NVIDIA** for the Nemotron model family — the routing architecture only works because the tiers have genuinely different capability/cost profiles
- **Nebius** for Token Factory and Sandboxes — running open models on independent infrastructure is exactly what this project demonstrates
- The open-source community for FastAPI, Pydantic, and the Python async ecosystem

---

## 📬 Contact

Built by **Sarthak Gupta** for the Nebius × NVIDIA Global AI Hackathon.

- GitHub: [@your-username](https://github.com/your-username)
- Project: [https://github.com/your-username/sentinel](https://github.com/your-username/sentinel)

---

*If you're judging this — thank you for your time. The dashboard at `localhost:8080` is the best way to see the agent think. The `docs/demo_script.md` has the exact 3-minute flow if you want to reproduce it.*