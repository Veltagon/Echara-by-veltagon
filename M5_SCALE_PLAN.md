# M5 SCALE PLAN — 25–30k LOC fullstack builds

Status: approved plan, not yet implemented. Supersedes guide.md's original 4–6k M5 target (already surpassed: 15k backend builds work today). Decisions locked with the user 2026-07-04: **fullstack scope**, **human gates at plan + delivery**, **tiered models (Sonnet waves / Opus for architect + integration + fixes)**.

---

## 0. Definition of done at scale

A 25–30k build is **delivered** iff, in one final verification run:

- Backend: import smoke + alembic upgrade + full pytest all pass (existing DEFINITION_OF_DONE.md).
- Frontend: `tsc --noEmit` passes and `vite build` exits 0.
- Seam conformance: every module's actual exports match SEAMS.json (deterministic AST check).
- Per-module gates passed during the build (wave gates + module integration).

Still true: no LLM scores, no refine loops, no jury reviews. Two human gates: **approve the architecture before any code**, **review the delivery report at the end**.

---

## 1. Why the current architecture breaks between 15k and 30k

Core insight: the wave design (fresh CLI session per wave) already avoids in-session compaction — the quality cliff Anthropic documents never happens because no session lives that long. The scaling problem is the inverse: **each fresh session knows less and less about the growing codebase.** Everything below follows from that.

Confirmed breaking points (file:line from live code review + adversarial stress-test):

| # | Breakage | Where |
|---|---|---|
| 1 | Wave context grows linearly: full PLAN.md + full contract embedded in EVERY wave prompt; at 250 files the 8 relevant files drown mid-prompt (lost-in-the-middle) | `agents/builder.py:105-112` `_base_context` |
| 2 | "READ earlier waves for interfaces" with no index at 180+ files → sessions guess signatures → **this is the drift mechanism** | `agents/builder.py:115-125` |
| 3 | ONE global integration pass must fix a 30k codebase + 500-test suite in a single session — hard ceiling, breaks first | `agents/builder.py:128-135` |
| 4 | Verification end-loaded: a wave-3 interface bug poisons waves 4–25 before detection | pipeline shape |
| 5 | **Silent empty build**: `_implementation_order` regexes match only `code/backend/...`; a module plan using other roots yields `files=[]`, which dispatches a wave with an EMPTY file list — no guard; and `validate_plan`'s `0 < n_files < 20` means zero matches *passes* validation | `agents/builder.py:89-91,171,183`; `agents/planner.py:136` |
| 6 | `validate_plan` 20-file floor rejects legitimate 15-file module plans → burns retry + claude fallback | `agents/planner.py:135-140` |
| 7 | Pytest timeout 300s false-fails healthy big suites: NN-AUTH-1 real bcrypt (~0.5s/hash) × the planner's auth matrix (4 cases/protected endpoint) ≈ 100–150s of bcrypt alone before 500 other tests | `agents/verifier.py:138`; `agents/builder.py:61-64`; `agents/planner.py:75-78` |
| 8 | Failure routing impossible: streams capped at 800 chars **at capture** — at 500 tests the tail is summary counters, not tracebacks; `failure_summary`'s 4000-char cap is unreachable | `agents/verifier.py:27,56-57,173-177` |
| 9 | Lane permadeath: ONE non-ok result (incl. a single 300s idle blip) kills the lane for the whole build; over 80 sessions P(≥1 blip)≈1 → whole build silently degrades to codex | `agents/builder.py:186,204` |
| 10 | No claude quota detection at all (only codex parses rate limits) + config pins Opus (`claude-opus-4-7[1M]`) — quota hit reads as hard failure → permadeath | `providers/claude_code.py` (no run() override); `providers/codex.py:119-151` |
| 11 | Forensics collapse: BUILDER_PROMPT.md overwritten (keeps only last of 40+ prompts); log filenames second-resolution → collisions | `agents/builder.py:184`; `providers/base.py:166` |
| 12 | stdin prompt written BEFORE idle watcher starts → wedged child hangs orchestrator with no kill path (the "few KB" comment is already false: current prompts are 25–50KB) | `providers/base.py:201-219` |
| 13 | One planner session can't produce a coherent 250-file manifest (observed: even gemma's 50-file plans need retries) | `agents/planner.py` one-shot design |
| 14 | Availability registry is process-memory only — resume after exit forgets a known 4h codex bench (acceptable, but don't build parking on it surviving restarts) | `providers/availability.py:15` |
| 15 | OneDrive hosts `builds/` — 30k LOC × fsync barrier loops under active sync ⇒ transient PermissionError flakes | `agents/repairs.py:530-540`; repo path |
| 16 | Session output ceiling: one CLI session wraps up ~800 LOC regardless of instructions (own eval data; matches literature ~3k output tokens stable) | DECISIONS.md observation |

---

## 2. Research grounding

- **Context rot is a gradient**: recall precision degrades as context grows, well before the window limit; n² attention dilution. Compaction is the WEAKEST tool ("overly aggressive compaction can result in the loss of subtle but critical context") — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).
- **Winning patterns**: structured note-taking (external files reloaded into fresh contexts), sub-agents with clean windows returning distilled summaries, just-in-time retrieval over pre-loading, small permanent core (the CLAUDE.md pattern) — same source + [memory tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool).
- **For codebases the second brain is deterministic**: [Aider's repo map](https://aider.chat/docs/repomap.html) — extracted signatures fitted to a ~1k-token budget beat semantic retrieval; the index is derived from disk so it can never be hallucinated. Repo-sketch → per-file fill → verify is the converged shape ([CodeTeam](https://arxiv.org/pdf/2606.22082)).
- **Long-session contamination**: models perpetuate their own earlier mistakes in context ("context contamination", [SlopCodeBench](https://arxiv.org/pdf/2603.24755)); output quality stable only to ~3k tokens/response — fresh-session waves are the right call, keep them.
- **Quota reality (2026)**: Claude Pro/Max 5h rolling caps (doubled May 2026) + weekly caps incl. Opus-hours budgets — [usage limits](https://support.claude.com/en/articles/9797557-usage-limit-best-practices). Opus on Pro is the scarce resource; a 27k build is 70–90 sessions ≈ 15–18h compute. Tiering + park-and-wait are mandatory, not optional.
- **Contract-first integration**: machine-checkable seam contracts + incremental per-change verification beat end-loaded integration ([contract-first guide](https://dev.to/wallaceespindola/contract-first-integration-a-practical-guide-for-developers-49nf)).

---

## 3. Target architecture

### 3.1 The second brain = deterministic disk artifacts (never hallucinated, regenerated, or summarized by a model)

All artifacts live in `build_dir/`:

| Artifact | Producer | Consumer | Purpose |
|---|---|---|---|
| `ARCHITECTURE.md` | Architect (Opus, 1 session) | human gate, planners | module list, boundaries, rationale |
| `MODULES.json` | Architect | planner/builder loops | `[{name, kind: backend\|frontend, loc_budget≤3000, depends_on[], path_root}]`, acyclic |
| `SEAMS.json` | Architect | wave prompts, seam checker | cross-module exports w/ signatures — the ONLY inter-module knowledge models get |
| `CONVENTIONS.md` | Architect | EVERY prompt, verbatim | ~1k tokens: error pattern, DI, naming, service/router split, NFRs. Never summarized — constraints survive by re-reading, not remembering |
| `PLAN_<module>.md` + contract slice | per-module planner (gemma→claude fallback) | that module's waves | 10–25 file manifest + implementation order |
| `interfaces/<module>.md` | **deterministic** `agents/interfaces.py` (stdlib `ast` for .py; regex export extractor for .ts/.tsx) | wave prompts | accurate signatures of everything built so far; regenerated after every wave |
| `code/frontend/src/api/{types.ts, client.ts}` | **deterministic** `agents/contract_codegen.py` from CONTRACT_REGISTRY.json | frontend waves | the backend↔frontend seam is machine-generated — a model never hand-writes it |
| `BUILD_JOURNAL.md` | each wave appends 3–5 lines | later wave prompts (tail ~30 lines) | decisions/deviations/warnings — Anthropic's structured note-taking |
| `BUILD_PROGRESS.json` | builder, write-through after EVERY session | resume + budgets | `{module: {waves_done, gate_fixes, integration_fixes, seams_ok, integrated}, global_fix_budget_used}` — **file-based, NOT in ProjectState** (state saves only at phase boundaries; a mid-BUILD crash would lose it, and threading state into the builder breaks the stub-test signatures) |
| `BUILD_METRICS.json` | builder | tuning + delivery report | per-session lane/tier/duration/outcome |
| `DELIVERY_REPORT.md` | DELIVER phase | human gate 2 | LOC by module, verification summary, journal digest, seam status, session/quota stats |

### 3.2 Flat wave context (~6–8k tokens, constant from wave 1 to wave 40)

Wave prompt = CONVENTIONS.md + seams slice (only the modules this one depends on, per MODULES.json) + own `interfaces/<module>.md` + own PLAN_<module>.md + this wave's file list + journal tail. **The full global plan is never embedded again.** Frontend waves additionally reference the generated api client (NN rule: import from it, never re-declare types).

### 3.3 Pipeline shape — PHASES LIST UNCHANGED (stress-test verdict: Option A)

`INTAKE → PLAN → BUILD → REPAIR → VERIFY → DELIVER` stays exactly as-is in `state.py`/`orchestrator.py`/`phases.py`. All loops live INSIDE the agent functions, invisible to the orchestrator and the 8 stub-based pipeline tests:

- **PLAN** (`run_planner` rewritten): if no ARCHITECTURE.md → run Architect (claude Opus); validate hard (modules 8–16, budgets ≤3k, deps acyclic via `graphlib.TopologicalSorter`, seams complete); then raise `ApprovalPending` (new exception) unless `state.approved`. Orchestrator catches it → saves state, prints "review ARCHITECTURE.md / CONVENTIONS.md / MODULES.json, then `python orchestrator.py --approve`", exits 0. On resume with `--approve`: architect skipped (files exist), per-module planners run (gemma primary, claude fallback, validator parameterized per module: path-prefix from MODULES.json + floor `max(6, loc_budget//400)` — fixes breakages #5/#6).
- **BUILD** (`run_builder` rewritten): modules in topological order; per module: waves (Sonnet) → after each wave `py_compile` + import smoke (backend) / `tsc --noEmit` (frontend) + regenerate interfaces → on gate failure ONE wave-scoped fix (Opus), then continue (gate is a tripwire, not a convergence loop) → module integration pass (Opus, module's own tests only) → `repair_all` (idempotent) → deterministic seam check → ≤1 seam fix. Hard guard: `if not files: raise BuildDispatchFailed` (kills breakage #5). Budgets enforced from BUILD_PROGRESS.json, **persisted before dispatch**: ≤1 gate-fix/wave, ≤2 integration-fixes + 1 seam-fix/module lifetime, global fix budget ~25 — exhausted budgets let VerifyFailed propagate into the normal MAX_RETRIES backstop (prevents the 205-session worst case; happy path ≈ 68 sessions).
- **REPAIR**: unchanged (global `repair_all`; per-module calls during BUILD are safe — idempotent by contract; barrier semantics preserved).
- **VERIFY** (`verifier.py` extended): backend import + alembic + pytest with `--junitxml` (structured per-test failures into VERIFICATION_REPORT.json — fixes breakage #8) and timeout scaled by collected count (`60 + 2×n_tests`, cap 1800 — fixes #7); frontend leg when `code/frontend` exists: `npm install` (cached node_modules) + `tsc --noEmit` + `vite build`. On retry, the builder re-reads VERIFICATION_REPORT.json and routes fixes per-module from the junitxml file map (`last_error` stays the human summary — no signature changes).
- **DELIVER**: unchanged + DELIVERY_REPORT.md (human gate 2).

### 3.4 Quota survival + provider hardening

- **Claude limit parser** (~25 lines): `run()` override in `ClaudeCodeProvider` mirroring codex's — scan output tail for limit messages → `availability.mark_exhausted("claude", reset_ts)`. Without it, parking has nothing to wait on (fixes #10).
- **Lane cooldown, not permadeath**: dead only after 2–3 consecutive failures; single blips → timed cooldown via the existing registry (fixes #9).
- **Park-and-wait in the BUILDER lane loop** (~15 lines): all lanes unavailable with finite `resets_at` → persist progress, sleep until earliest reset (cap ~8h total, knob `ECHARA_WAIT_ON_EXHAUST=1`), retry same wave. No known reset → `BuildDispatchFailed` exactly as today (keeps `test_dispatch_failure_stamps_clean_verdict` intact).
- **Model tiering**: `ClaudeCodeProvider` gains a `model` param → `--model sonnet|opus` in argv. Waves=Sonnet, architect/integration/fix=Opus (~60–70% Opus-burn cut). Codex = spot-fix fallback lane only (single slot + 10s spacing can't absorb 50 sessions).
- **Small hardenings**: start idle watcher BEFORE the stdin write (#12); microsecond log timestamps (#11); per-wave prompt files `BUILDER_PROMPT_<module>_w<N>.md` (#11); EACCES retry in fsync loops + recommend excluding `builds/` from OneDrive sync (#15).
- **INTAKE preflight**: node/npm on PATH when the prompt implies frontend; fail fast with a clear message.

### 3.5 Optional (non-blocking) drift auditor

Every ~4 waves, gemma (free) reads CONVENTIONS.md + own-module interfaces + new signatures → appends flags to `AUDIT_FLAGS.md`, consumed by later prompts. Never blocks; DoD stays deterministic.

---

## 4. Budget & wall-clock (27k LOC, from the stress-test's session math)

| Item | Sessions | Time |
|---|---|---|
| Architect (Opus) | 1 | ~15 min |
| Module planners (gemma, free) | 12 harness runs | ~40 min |
| Waves (27k ÷ ~800 LOC/session) | ~34 | ~6–7 h |
| Wave-gate fixes (~25%) | ~8 | ~1 h |
| Module integrations | 12 | ~3.5 h |
| Integration + seam fixes | ~9 | ~1.5 h |
| REPAIR/VERIFY cycles (deterministic) | 0 | ~1 h |
| VERIFY-routed fixes | ~6 | ~1 h |
| **Total** | **~70–90 CLI sessions** | **~15–18 h compute** |

Calendar: ~20–24h with Sonnet tiering + parking; 1.5–3 days if run all-Opus without pacing (quota lockouts every 10–20 heavy sessions). Worst-case session count without budget discipline: ~205 — the budgets in §3.3 exist to make that impossible.

---

## 5. Implementation phases (each independently shippable; 122 existing tests stay green; new tests additive in the `tests/test_m4_pipeline.py` stub style)

**Phase A — index layer + flat context** (highest ROI, no pipeline change)
New `agents/interfaces.py` (ast signatures for .py, regex exports for .ts/.tsx; seam-conformance checker). Builder: replace `_base_context` full-plan embed with the §3.2 assembly; regenerate interfaces after each wave; `if not files: raise`. Tests: unit (extraction on fixture files, seam mismatch detection), context-size assertion. Validate: re-run the existing 15k-class eval — quality must not regress, prompt sizes flat.

**Phase B — wave gates + journal + progress file + forensics**
Per-wave py_compile/import gate + single fix dispatch; BUILD_JOURNAL.md append + tail injection; BUILD_PROGRESS.json write-through (budgets persisted pre-dispatch); per-wave prompt files; microsecond log names; watcher-before-stdin reorder. Reuse `verifier._provision_venv` at the first gate (venv then reused by VERIFY via the existing stamp check).

**Phase C — hierarchical planning + approval gate**
New `agents/architect.py` (Opus; ARCHITECTURE.md/MODULES.json/SEAMS.json/CONVENTIONS.md; hard validation + one error-fed retry). `planner.py` → per-module loop with parameterized validator. `ApprovalPending` exception + `--approve` flag + `state.approved` field (load-compatible default). Toposort module order. Tests: approval-flow walk, module-plan validation, resume mid-planning.

**Phase D — module integration + failure routing + verifier scaling**
Per-module integration prompts (scoped test commands); junitxml in verifier + structured failures in VERIFICATION_REPORT.json; builder retry branch routes per-module from the report; pytest timeout scaling; lifetime budgets enforced. Tests: routing unit tests (junitxml fixture → module map), scaled-timeout unit.

**Phase E — quota survival + tiering**
Claude limit parser; lane cooldown; park loop; `--model` tiering; knobs (`ECHARA_WAIT_ON_EXHAUST`, `ECHARA_WAVE_MODEL`). Tests: parser cases (real limit strings), cooldown-not-death, park-with-finite-reset stub.

**Phase F — fullstack leg**
New `agents/contract_codegen.py` (CONTRACT_REGISTRY.json → types.ts + client.ts, golden-file tested). Frontend NN-rules block (vite+react+ts pinned stack; import only the generated client; `VITE_API_URL` env; real package.json deps). Verifier frontend checks (npm install cached / tsc / vite build) + node preflight in INTAKE. Frontend repairs section starts EMPTY — populated only from observed eval failures (house doctrine).

**Phase G — metrics, delivery report, eval ladder**
BUILD_METRICS.json; DELIVERY_REPORT.md in DELIVER. Ladder: **E0** existing 15k backend eval (regression, after A–D) → **E1** ~8–10k fullstack (e.g., bookmarks+tags+auth+React UI) → **E2** ~18–20k fullstack → **E3** 25–30k (ERP-lite: inventory/orders/invoicing/customers/RBAC/audit-log + React admin). Between rungs: add repairs/NN-rules only from observed failures; re-run until DoD clean; record metrics. E3 clean twice ⇒ milestone done.

---

## 6. Top risks (ranked, from the adversarial review)

1. Claude quota + lane permadeath compound → build silently degrades to codex. *Parser + cooldown + park (Phase E).*
2. Pytest 300s timeout false-fails healthy 600-test suites (bcrypt math). *Scaled timeout (Phase D).*
3. Failure routing has no data (800-char caps). *junitxml (Phase D).*
4. Retry multiplication (3 global × per-module fixes ≈ 72 tail-chase sessions). *Lifetime budgets in BUILD_PROGRESS.json, persisted pre-dispatch (Phase B/D).*
5. Empty/foreign-path module plans dispatch empty file lists silently. *Path validation + hard guard (Phase A/C).*
6. `validate_plan` floor rejects module plans, burning fallbacks. *Parameterized validator (Phase C).*
7. Mid-BUILD progress loss on crash. *Write-through BUILD_PROGRESS.json (Phase B).*
8. Seam drift between independently-planned modules. *Toposort order + deterministic seam check + generated frontend client (Phases C/F).*
9. Codex can't absorb systemic fallback (single slot, 10s spacing). *Codex = spot-fix lane; park for claude instead (Phase E).*
10. Forensics/environment flakes at 80 sessions (prompt overwrite, log collisions, OneDrive fsync). *Phase B hardenings + sync exclusion.*

---

## 7. What NOT to build

- No vector DB / embeddings / RAG memory — deterministic indexes beat semantic search on a codebase the orchestrator itself wrote.
- No in-session compaction of any kind — fresh-session waves already sidestep the degradation cliff.
- No LLM judge scores, no refine loops (DEFINITION_OF_DONE doctrine unchanged).
- No Playwright/browser e2e yet; no Docker; no CI generation (FUTURE.md).
- No speculative frontend repairs — evidence-first, same as every existing repair.
- No dynamic phase machine — the flat PHASES list survives contact with 30k LOC (verified against every pipeline test).
