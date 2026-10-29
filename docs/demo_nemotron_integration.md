# Demo Script: Nemotron + Nebius Token Factory Integration Walkthrough

**Target duration: ~60 seconds** (can be spliced into the main 3-min demo)

---

## 🎬 Scene 1: The API Key in `.env` (5 seconds)

**Action:** Open `.env` in editor

**Narration:**
> "Everything starts with the Nebius Token Factory API key. Here in `.env`, the `NEBIUS_API_KEY` authenticates every model call. The base URL points to Token Factory's OpenAI-compatible endpoint."

**Show on screen:**
```bash
NEBIUS_API_KEY=v1.CmQKHHN0...  # your key
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
```

---

## 🎬 Scene 2: Config Reads the Key (5 seconds)

**Action:** Open `sentinel/config.py` → `Settings` class

**Narration:**
> "The `Settings` class loads the key and defines the three Nemotron tiers: Nano for triage, Super for diagnosis, Ultra for genuinely ambiguous cases. All model IDs are configurable via env vars."

**Show on screen:**
```python
# sentinel/config.py lines 20-34
api_key: str = os.getenv("NEBIUS_API_KEY", "").strip()
base_url: str = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")

model_nano: str = os.getenv("NEMOTRON_NANO", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
model_super: str = os.getenv("NEMOTRON_SUPER", "nvidia/nemotron-3-super-120b-a12b")
model_ultra: str = os.getenv("NEMOTRON_ULTRA", "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1")
embed_model: str = os.getenv("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")

@property
def online(self) -> bool:
    return bool(self.api_key)
```

---

## 🎬 Scene 3: Nemotron Client — Handling Reasoning Models (10 seconds)

**Action:** Open `sentinel/nemotron.py` → `NemotronClient._extract` and `complete_json`

**Narration:**
> "Critical detail: Nemotron models are *reasoning models*. They stream chain-of-thought into `reasoning_content` and often leave `content` empty. Our `_extract` reads both. Also, JSON comes wrapped in prose — so `_first_json_object` brace-matches and we retry once with a repair prompt at temperature 0."

**Show on screen:**
```python
# sentinel/nemotron.py lines 131-140
@staticmethod
def _extract(message: Any) -> str:
    content = (getattr(message, "content", None) or "").strip()
    if content:
        return content
    reasoning = (getattr(message, "reasoning_content", None) or "").strip()
    if reasoning:
        log.debug("Falling back to reasoning_content (content was empty)")
    return reasoning
```

---

## 🎬 Scene 4: Three-Tier Routing in Action (15 seconds)

**Action:** Open `sentinel/reasoning.py` → `pick_deep_tier`

**Narration:**
> "This is the architecture — model routing in eight lines. Nano triages every anomaly (cheap, fast). Only real anomalies escalate. Super handles most diagnoses. Ultra wakes *only* when: critical + low confidence, OR ≥3 correlated signals, OR no memory match. The dashboard makes this visible — per-tier calls, latency, cost."

**Show on screen:**
```python
# sentinel/reasoning.py lines 114-122
def pick_deep_tier(anomaly, triage, memory_hits) -> str:
    if triage.severity == Severity.critical and triage.confidence < 0.6:
        return "ultra"
    if len(anomaly.signals) >= 3:
        return "ultra"
    if not memory_hits and triage.category in {"unknown", "deploy_regression"}:
        return "ultra"
    return "super"
```

---

## 🎬 Scene 5: Dashboard — Live Proof (15 seconds)

**Action:** Switch to browser at `http://localhost:8080`

**Narration:**
> "The dashboard proves it. Top-right: 'Nemotron online' — not a stub. The **Model Routing** table shows live per-tier metrics: calls, avg latency, cost in USD. Every incident card shows which tier diagnosed it. This isn't claimed — it's measured."

**Show on screen:**
- Header chip: `Nemotron online` (not "offline model")
- Model Routing table with Nano/Super/Ultra rows
- Incident card showing `tier: super` or `tier: ultra` in timeline

---

## 🎬 Scene 6: Offline Fallback (5 seconds)

**Action:** Stop server, unset `NEBIUS_API_KEY`, restart

**Narration:**
> "No key? No problem. Sentinel degrades gracefully to a deterministic offline stub — every response tagged `offline: true` in the UI. The demo never dies."

**Show on screen:**
- Header chip: `live · offline model`
- Incident cards tagged with `offline: true` badge

---

## 🎬 Scene 7: Cost Ledger — Transparency (5 seconds)

**Action:** Point at Model Routing table cost column

**Narration:**
> "Rough public pricing baked in: Nano ~$0.10/$0.40 per 1M tokens, Super ~$0.35/$1.40, Ultra ~$0.90/$2.60. The ledger shows *actual* spend per incident — so you can prove the routing saves money."

**Show on screen:**
```python
# sentinel/nemotron.py lines 32-36
PRICE_PER_MTOK = {
    "nano": (0.10, 0.40),
    "super": (0.35, 1.40),
    "ultra": (0.90, 2.60),
}
```

---

## 📋 Quick Commands for Recording

```bash
# Terminal 1: Sample service
cd sentinel && venv/Scripts/activate && python -m sample_service.app

# Terminal 2: Sentinel (with Nemotron)
cd sentinel && venv/Scripts/activate && python -m sentinel.server

# Terminal 3: Inject fault to trigger full pipeline
cd sentinel && venv/Scripts/activate && python scripts/inject_fault.py --type n_plus_one

# To demo offline fallback:
# 1. Ctrl+C sentinel
# 2. $env:NEBIUS_API_KEY=""  (or edit .env)
# 3. Restart sentinel
# 4. Inject fault again
```

---

## 🎯 Key Talking Points for Judges

| Point | Evidence |
|-------|----------|
| **Runs on Token Factory** | `NEBIUS_BASE_URL` + live "Nemotron online" badge |
| **Uses Nemotron 3 Nano/Super/Ultra** | Config shows all three model IDs |
| **Handles reasoning models correctly** | `_extract` reads `reasoning_content` |
| **Robust JSON parsing** | `_first_json_object` + repair turn |
| **Model routing = architecture** | `pick_deep_tier()` + dashboard ledger |
| **Cost transparency** | Per-tier USD ledger in UI |
| **Graceful degradation** | Offline stub with `offline: true` tags |

---

## 🎥 Recording Tips

1. **Record at 1080p+** — code must be readable
2. **Zoom in on code** — use editor zoom (Ctrl+Shift+=)
3. **Narrate *while* clicking** — don't separate action from explanation
4. **Show the dashboard live** — the ledger updating in real-time is powerful
5. **Keep it moving** — 60 seconds max for this segment