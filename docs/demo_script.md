# Demo script — three minutes

Total runtime 2:50. Rehearse once; the fault timings below are real.

## Before you record

```bash
cp .env.example .env            # NEBIUS_API_KEY filled in
python scripts/setup_vector_store.py --reset
python sample_service/app.py    # terminal 1
python -m sentinel.server       # terminal 2
```

Open <http://localhost:8080> and let it run for **90 seconds** before recording.
The detector needs a baseline; firing before it has one is the only way this
demo looks bad. Confirm "Live signals" shows `latency_ms` and `memory_rss_mb`.

Have a third terminal ready for fault injection.

## 0:00 — 0:20 · The problem

On camera, on the dashboard.

> "When a service breaks at 2am, the first fifteen minutes are archaeology.
> What changed, is it us or upstream, did this happen before. Sentinel does that
> part before you've opened your laptop."

Point at the empty incident stream. It is watching, it has learned a baseline,
there is nothing wrong.

## 0:20 — 0:40 · Break it

```bash
python scripts/inject_fault.py --type n_plus_one
```

> "I've just shipped a regression — a serializer that does one query per row."

Say what to watch for: `db_calls_per_request` climbing in Live signals.

## 0:40 — 1:20 · Detection and triage

The incident card appears. Walk the stage list as it lights up.

> "Detection is plain statistics, no model — it has to be cheap enough to run on
> every batch. Then Nemotron Nano makes one cheap call: is this real or is it
> noise. Most anomalies stop there."

Point at the Model routing table: Nano's call, its latency, its cost.

## 1:20 — 2:00 · Recall and diagnosis

> "It found a similar incident from January in the vector store — same shape,
> different service. That goes into the prompt as context."

Read the root cause aloud from the card.

> "This is Nemotron Super. It didn't just name the symptom, it named the
> mechanism, and it says how confident it is."

## 2:00 — 2:35 · The approval gate

Point at the fix block. Read the literal command.

> "The model doesn't get to write a command. It picks a name from an allowlist,
> and what you see here is exactly what will run, where it will run, and how to
> roll it back. Nothing has executed yet."

Click **Approve and run**.

> "It ran in the sandbox — and then it read the service's health back. That's
> the part people skip: a fix isn't done because a command exited zero."

`db_calls_per_request` drops in Live signals. The card turns green.

## 2:35 — 2:50 · Memory closes the loop

Type into the chat: `what just happened and would you catch it faster next time?`

> "The resolved incident went back into memory. The second time this shape of
> failure appears, it's recognised, not reasoned from scratch."

End on the Model routing table.

> "Nano triaged, Super diagnosed, Ultra stayed asleep because this case wasn't
> ambiguous. That routing is the architecture."

## If something goes wrong on camera

- **No incident after 45 seconds** — the baseline is still short. Keep talking,
  it will fire. Do not inject a second fault.
- **Token Factory is slow** — the card sits on "diagnosing". Say so plainly and
  point at the stage list; recovering visibly is better than a cut.
- **Everything is offline** — the header reads "live (offline model)" and every
  card is tagged. Say it out loud; a demo that degrades honestly is a better
  story than one that fakes it.

## Fault reference

| Fault | What the detector sees | Time to fire |
|---|---|---|
| `n_plus_one` | `db_calls_per_request` 1 → ~41 | ~20s |
| `memory_leak` | `memory_rss_mb` monotonic drift | ~60s |
| `dependency_timeout` | `latency_ms` ramp, then 5xx | ~40s |
