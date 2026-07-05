# M5 Eval Ladder — run order (infrastructure A–G complete)

Phases A–G are implemented, unit-tested (92 pytest + 26 hardening + 43 harness,
all green), and pushed. This is the **stop point**: the ladder below runs LIVE
builds that burn the scarce Opus quota and, from E1 up, take many hours each, so
it is run deliberately — not folded into the build session.

## How a scale build runs

```bash
# 1. Draft the architecture (Opus, 1 session) — stops at the approval gate.
ECHARA_BUILDS_ROOT=Z:/echara_builds \
  python orchestrator.py --prompt "<the app>"

# 2. Review Z:/echara_builds/<build>/ARCHITECTURE.md, MODULES.json,
#    CONVENTIONS.md, SEAMS.json. Then approve — this runs planning + build +
#    verify unattended, parking through quota walls:
ECHARA_BUILDS_ROOT=Z:/echara_builds ECHARA_WAIT_ON_EXHAUST=1 \
  python orchestrator.py --approve

# 3. If a crash/kill happens, just re-run step 2's command — waves already on
#    disk are skipped (BUILD_PROGRESS.json), so it resumes where it stopped.
```

Knobs: `ECHARA_WAVE_MODEL` (default `sonnet`), `ECHARA_WAIT_ON_EXHAUST=1`
(park-and-wait, 8h cap), `ECHARA_BUILDS_ROOT` (→ `Z:/echara_builds`, off OneDrive).

## The ladder

| Rung | Scope | Prompt sketch | Gate |
|---|---|---|---|
| **E0** | ~15k backend | the bookmark-manager prompt (already passed pre-M5) | regression: DoD clean, quality not worse than pre-M5 |
| **E1** | ~8–10k fullstack | bookmarks + tags + auth + a small React admin UI | DoD (backend + tsc + vite build) clean |
| **E2** | ~18–20k fullstack | above + roles, activity feed, richer UI | DoD clean |
| **E3** | 25–30k fullstack | ERP-lite: inventory / orders / invoicing / customers / RBAC / audit-log + React admin | DoD clean **twice** ⇒ milestone done |

Between rungs: add repairs / NN-rules **only** from observed failures (house
doctrine — the frontend repairs section is intentionally empty until a real eval
produces one). Record `DELIVERY_REPORT.md` each run.

## Verifying a delivered build

`DELIVERY_REPORT.md` (auto-written at DELIVER) has LOC-by-module, the DoD
verification summary, per-module seam status, session/quota stats, and a journal
digest. For behavior, run the contract-matched post-hoc probe against the build's
verify interpreter (read `VERIFICATION_REPORT.json:interpreter` — do NOT assume
`.verify_venv`; on Py3.14 a pydantic-pin can force the orchestrator-python
fallback).

## Budget reality (from the plan's session math)

E3 ≈ 70–90 CLI sessions ≈ 15–18h compute ≈ 20–24h calendar with Sonnet tiering +
parking. Both subscription lanes (claude 5h window, codex credits) cap after
2–3 scale builds; the park loop + wave-skip resume are what make it survivable
unattended. Do not start E3 without fresh weekly Opus budget.

## Deferred (optional, non-blocking)

- **Drift auditor (§3.5)**: a periodic gemma pass appending advisory flags to
  `AUDIT_FLAGS.md`. Skipped to conserve tokens; the DoD is fully deterministic
  without it. Add if a scale eval shows convention drift the seam check misses.
