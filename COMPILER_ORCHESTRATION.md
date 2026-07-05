# COMPILER_ORCHESTRATION.md
## Production Specification: 6-Lane Multi-Agent Compilation of a 40,000-LOC Codebase
### Context-Isolated, DAG-Routed, Self-Healing, Token-Bounded

Document class: mission-critical operational specification.
Applies to: Parent Orchestrator (`orchestrator.py`, tier 0), Feature Sub-Orchestrators
(tier 1, deterministic loops inside `agents/builder.py`), Leaf Workers (tier 2, model
invocations). All constants in this document are normative; a component that exceeds a
ceiling defined here is defective by definition.

All constants below are normative STARTING CALIBRATIONS, not axioms — see
Section 0.4. Where any claim in Sections 1–5 conflicts with Section 0, Section 0
governs (it is the reality layer added after the first live fullstack builds).

Global constants (referenced by symbol throughout):

| Symbol | Value | Meaning |
|---|---|---|
| `N` | 6 | concurrent worker lanes |
| `S` | 40,000 | target codebase size, LOC |
| `TAU` | 13 | tokens per LOC of source (measured, Python/TS mixed) |
| `TAU_GEN` | 15 | budgeted output tokens per generated LOC (incl. retries amortized) |
| `L_BATCH` | 1,050 | max LOC per leaf batch (4 files × ~260 LOC) |
| `F_BATCH` | 4 | max files per leaf batch |
| `C_PREFIX` | 2,750 | frozen cached system-prefix tokens |
| `ALPHA` | 0.10 | cache-hit input price multiplier |
| `V_MAX` | 4,500 | max volatile (uncached) tokens per invocation |
| `K_DEP` | 4 | max dependency modules visible to one batch |
| `IFC_MAX` | 3,000 | max interface-closure tokens per invocation |
| `LES_MAX` | 600 | max lesson-ledger tokens per invocation |
| `B_GATE` | 1 | gate-fix budget per wave |
| `B_INTEG` | 2 | integration-fix budget per module |
| `B_SEAM` | 1 | seam-fix budget per module |
| `B_GLOBAL` | 25 | global fix-pass budget per build |
| `R_MAX` | 3 | global VERIFY retry backstop |

---

# SECTION 0: OPERATING REALITY, ADOPTION SEQUENCE & AMENDMENTS

Normative and governing. Added 2026-07-05 after E0/E1 (the first fullstack
builds) and a live provider-capability audit, to reconcile the idealized model
in Sections 1–5 with what the system actually is. Where this section conflicts
with a claim below, **this section wins.**

## 0.1 The binding constraint is provider quota, not compute

Sections 1–2 model a compute/coordination-bound machine: `N`=6 workers holding
context, lock tables, merge mutexes. The bottleneck observed on every real build
is **provider quota**. The two CLI lanes (claude, codex) cap after 2–3 scale
builds; E1 (~15k LOC) burned a full claude 5-hour window *plus* codex's daily
credits and still parked mid-build. Therefore:

- **`N`=6 is an upper bound gated by live availability, not a guarantee.**
  Effective N = lanes with quota *right now*, frequently 1–2.
- The scheduler MUST treat "no live lane" as a first-class state — park-and-wait
  until the earliest quota reset, then cross-provider overflow to cheaper lanes —
  **not** an error. This quota-survival machinery (reset parsing, lane cooldown,
  park, overflow) is the load-bearing path to 40k and is *under*-specified by
  §4's brake taxonomy, which models only CODE failures. It is real code today:
  `agents/builder.py` park loop + `_OVERFLOW_LANES`.

## 0.2 The provider fleet (measured 2026-07-05) fills the lanes

LEAF_GEN's `api_single_turn` transport and the whole N-lane model REQUIRE a fleet
of API build lanes. Audited with run-and-verify (code correctness) **and** the
harness tool-calling gate:

- **Lane-eligible (correct code + real `tool_calls`):** claude CLI, codex CLI,
  `cerebras/gpt-oss-120b`, `cerebras/gemma-4-31b`, `hf/Qwen2.5-Coder-32B`,
  `hf/DeepSeek-V3`, `nvidia/deepseek-v4-flash`. The 5 API lanes are wired into
  `providers.HARNESS_PROVIDERS` and used as overflow in `agents/builder.py`.
- **Code-capable but NOT lane-eligible:** `nvidia/mistral-nemotron` emits tool
  calls as *text*; `cerebras/zai-glm-4.7` routes content into `reasoning`. Both
  fail the tool-calling gate — never route a build wave to them.
- **Unfunded (billing, not capability):** z.ai, api.airforce, Vercel gateway.

**Caching caveat (amends §1.2):** the `ALPHA` cached-prefix term assumes prompt
caching. Anthropic/OpenAI cache; **Cerebras / HF-router / NVIDIA NIM caching is
unconfirmed.** Until measured (0.5), budget the cheap lanes at `ALPHA = 1.0` (no
cache) and read the 22.8-tok/LOC figure as a best case for the CLI lanes only.

## 0.3 Single-turn LEAF_GEN is a candidate, not the default

`api_single_turn, max_turns: 1, "no filesystem"` (§1.3, §3.2) bets that a strong
model one-shots a 4-file batch from a frozen work order. The PROVEN builder today
is **multi-turn** tool-calling (claude/codex CLI sessions; the harness loop for
API lanes — verified building real multi-file modules with passing tests). Adopt
single-turn only after an A/B on real modules shows equal quality at lower cost.
Until then LEAF_GEN runs multi-turn through the harness; keep the malformed-output
guard (§4.3), but relax "no filesystem" to "workspace-clamped filesystem."

## 0.4 Ceilings are calibration knobs, not correctness oracles

Amendment to "a component that exceeds a ceiling is defective by definition": the
numeric ceilings in §1.3 are **starting calibrations tuned from measured
`BUILD_METRICS.json` distributions**, not axioms. A breach LOGS and TRIMS first
(as §1.3's volatile algorithm already does) and hard-rejects only when trimming
cannot satisfy it. Model/quota reality drifts — leave the knob, don't weld it.

## 0.5 Adoption sequence — build to observed gaps, not all ten sections

This spec is ~10× the current implementation. Do NOT implement it wholesale
(YAGNI). Each step is justified by a real gap and validated before the next:

1. **DONE** — multi-provider fleet + overflow lanes (`agents/builder.py`
   `_OVERFLOW_LANES`, per-wave family rotation). The enabling substrate.
2. **DONE** — concurrent multi-lane module router (`agents/builder.py`,
   `ECHARA_CONCURRENCY`, default 1). Independent modules of a topological layer
   build in PARALLEL, each on its own lane; extra workers spill onto the API
   fleet instead of queueing on the 2 CLI lanes; `progress.py` is lock-guarded so
   the shared ledger/budget can't be corrupted. Module-granularity (worktree-free
   — path_roots are disjoint); file-level §2.3 worktrees remain deferred. Proven
   deterministically (parallel builds + dependency order); needs a LIVE multi-lane
   run to validate at scale (per step 5).
3. **DONE** — §4.2 contract-based failure classifier (`agents/classifier.py`),
   wired into the builder's multi-module retry branch. Each VERIFY failure is
   classified against `SEAMS.json` into LOCAL_BUG / INTERFACE_BREACH /
   UPSTREAM_HALLUCINATION and the fix routes to the real culprit — the PROVIDER
   on a breach, the CONSUMER (test owner) otherwise — with the state + reason in
   the fix prompt. Conservative: only a high-confidence breach re-routes off the
   owner. Module-granularity; signature-mismatch call-site resolution needs the
   file DAG (§2.1, deferred) and falls back to LOCAL.
4. **NEXT** — minimal §5 lesson ledger (append-only `LESSONS.jsonl` → builder
   prompt), NOT the full promotion pipeline yet.
5. **Instrument before trusting the math:** extend `BUILD_METRICS.json` to log
   per-invocation input/output/cache tokens; run E1/E2; CONFIRM the flat curve
   and whether the fleet caches BEFORE building §1.3's REJECT enforcement.

DEFER until 2–5 prove out and quota supports concurrency: single-turn LEAF_GEN,
6-lane git-worktree parallelism (§2.3), sub-file lock ranges.

## 0.6 Known-fragile pieces to harden before trusting

- **TS extractor (§3.1.2) is regex-based** and WILL miss generics/overloads/
  decorators — the weakest link in the "un-hallucinatable seam" on the frontend.
  Prefer the TypeScript compiler (`tsc --emitDeclarationOnly` / language service)
  for frontend-heavy builds before relying on §3 for TS.
- **6 worktrees + `merge-tree --write-tree` (§2.3)** need a git-version gate
  (≥2.38) and Windows/OneDrive shakeout — MAX_PATH already bit us (see
  `phases._prune_nested_code` / `_rmtree_long`). Keep `LANES_ROOT` off OneDrive
  and short.
- **Output tokens dominate and are excluded from §1.2's headline** (~600k at
  15/LOC for 40k). `build_ceiling` budgets 1.2M output — quote input+output
  together, never input-only.

## 0.7 Repo layout (2026-07-05)

Root holds only entrypoints + the two governing docs: `orchestrator.py`,
`state.py`, `phases.py`, `run_harness_agent.py`, `conftest.py`, `CLAUDE.md`,
`COMPILER_ORCHESTRATION.md`. Docs → `docs/`; config → `config/`
(`provider_config.yaml`, `skill_assignments.yaml`). The legacy M2 manual-dispatch
path (`agent.py`, `run_agent.py`) and the stale `guide.md` were deleted —
`agents.builder` + `orchestrator` are the pipeline. This spec replaces `guide.md`
as the build design of record.

---

# SECTION 1: MATHEMATICAL TOKEN MODELING & CONTEXT BUDGETING

## 1.1 The naive model (monolithic context, N agents, I iterations)

Definitions:

- `S_i` — codebase size in LOC at iteration `i` (monotone non-decreasing, `S_I = S`).
- `P` — fixed prompt overhead per invocation (system prompt, instructions), tokens.
- `E_i` — error/log payload at iteration `i`, tokens.
- `TAU` — tokens per LOC.

In the naive architecture every agent re-reads the monolithic codebase every iteration
(directly, or effectively via multi-turn tool-use re-reads):

$$
T_{\text{naive}}(N, S, I) \;=\; \sum_{i=1}^{I} N \cdot \big( \tau S_i + P + E_i \big)
\;\le\; N \cdot I \cdot \big( \tau S + P + \bar{E} \big)
$$

Iteration count is not independent of size: each iteration lands a bounded net
increment `dS` of working code (measured single-session ceiling ≈ 800 LOC; batch
planning raises it to `L_BATCH`), so `I = S / dS`. Substituting:

$$
T_{\text{naive}}(S) \;=\; \frac{N\tau}{\Delta S}\, S^2 \;+\; \frac{N(P+\bar{E})}{\Delta S}\, S
\;=\; O(N \cdot S^2)
$$

**The naive model is quadratic in codebase size.** Concrete evaluation at the target
scale, `N = 6`, `TAU = 13`, `S = 40,000`, `dS = 1,000` (hence `I = 40`), `P = 2,000`:

```
T_naive = 6 × 40 × (13 × 40,000 + 2,000)
        = 240 × 522,000
        = 125,280,000 input tokens        (125.3M; output tokens excluded)
        = 3,132 input tokens burned per delivered LOC
```

This is the observed failure mode at 25k LOC: token spend and lost-in-the-middle
recall degradation grow together, because both are functions of context size.

## 1.2 The Context-Isolated model (AST interface boundaries)

Per-invocation cost decomposition. A worker invocation `k` receives:

1. `C_PREFIX` — frozen system prefix (rules + conventions), byte-identical across the
   entire build, billed at `ALPHA` via prompt caching.
2. `S_leaf(k)` — the strict file constraint: plan slices for the ≤ `F_BATCH` files the
   worker owns, plus (fix-class only) the current contents of those same files. Bounded
   by `F_BATCH` and `L_BATCH`, **not** by `S`.
3. `I_interfaces(k)` — the shallow dependency schema layer: AST-extracted public
   signatures of the declared-import closure only (Section 3). Bounded by
   `K_DEP × per-module signature budget`, trimmed to `IFC_MAX`, **not** a function of `S`.
4. `Λ` — lesson injection, ≤ `LES_MAX` (Section 5).
5. `Π` — contract slice (endpoints/types this batch implements), ≤ 1,200 tokens.

$$
T_{\text{inv}}(k) \;=\; \alpha\, C_{\text{prefix}} \;+\; \tau\, S_{\text{leaf}}(k)
\;+\; \sum_{d \in D(k)} \sigma X_d \;+\; \Lambda \;+\; \Pi
$$

Boundedness of every term with respect to `S`:

| Term | Bound | Why it does not grow with S |
|---|---|---|
| `ALPHA × C_PREFIX` | 275 effective | frozen at build start; cache refreshed by every dispatch (lane cadence < TTL) |
| `TAU × S_leaf` | ≤ 1,600 | plan slices capped at 40 lines/file × 4 files; fix-class own-file reads capped by `L_BATCH` |
| `Σ σ X_d` | ≤ `IFC_MAX` = 3,000 | closure pruning: a 4-file batch imports from ≤ `K_DEP` modules regardless of how many modules exist; extractor compression ratio ρ ≈ 0.04 tokens-of-signature per token-of-source; hard trim at 3,000 |
| `Λ` | ≤ 600 | top-12 selection, hard cap |
| `Π` | ≤ 1,200 | only this batch's contract rows |

Therefore:

$$
T_{\text{inv}} \;\le\; T_{\max} \;=\; 275 + 4{,}500 \;=\; 4{,}775 \text{ effective input tokens}
\qquad\text{and}\qquad
\frac{d\,T_{\text{inv}}}{d\,S} = 0
$$

Per-invocation cost is **O(1) in S** — a flat curve. Total build cost is the number of
invocations times a constant. Generation batches `B(S) = ceil(S / L_BATCH)`; fix
invocations are empirically `F(S) ≤ φ · B(S)` with `φ ≈ 0.55` (gate fixes ~20% of
waves + `B_INTEG`/`B_SEAM` module passes + VERIFY-routed fixes, all budget-capped by
Section 4.3 so `φ` cannot drift upward):

$$
T_{\text{build}}(S) \;=\; B(S)\,T_{\text{gen}} + F(S)\,T_{\text{fix}} + T_{\text{plan}}
\;=\; O(S) \text{ total} , \quad O(1) \text{ per invocation}
$$

At any instant at most `N` workers hold context simultaneously, so peak live context is

$$
T_{\text{live}}(S) \;=\; O\!\big( N \cdot ( S_{\text{leaf}} + \textstyle\sum I_{\text{interfaces}} ) \big)
$$

which is the required flat, sub-linear scaling law: linear total (unavoidable — you
must emit `S` lines), **constant per step, constant live footprint**, versus quadratic
total and linear-in-S live footprint for the naive model.

Concrete evaluation at `S = 40,000` (`B = 38` generation batches; 21 fix/integration
sessions at ~30,000 effective tokens each because they run interactive multi-turn;
architect + 14 planners ≈ 100,000):

```
T_build = 38 × 4,775  +  21 × 30,000  +  100,000
        = 181,450     +  630,000      +  100,000
        = 911,450 effective input tokens                (0.91M)
        = 22.8 input tokens per delivered LOC

Naive:      125.3M     3,132 tok/LOC     baseline
This spec:    0.91M       22.8 tok/LOC   −99.3% vs naive, −87% vs current 25k shape
```

## 1.3 Token Budget Allocator — concrete JSON

The allocator is a single file, `TOKEN_BUDGET.json`, loaded by tier 0 at BUILD start
and enforced by the dispatch layer on **every** invocation. Exceeding a ceiling is a
dispatch-time error, never a warning.

Formal schema (JSON Schema draft 2020-12):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "echara://schemas/token_budget.v1.json",
  "title": "TokenBudgetAllocator",
  "type": "object",
  "required": ["version", "build_ceiling", "invocation_classes", "serialization_triggers", "compression_policy"],
  "additionalProperties": false,
  "properties": {
    "version": { "type": "integer", "const": 1 },
    "build_ceiling": {
      "type": "object",
      "required": ["max_total_input_tokens", "max_total_output_tokens", "max_wall_clock_seconds", "on_trip"],
      "additionalProperties": false,
      "properties": {
        "max_total_input_tokens": { "type": "integer", "minimum": 1 },
        "max_total_output_tokens": { "type": "integer", "minimum": 1 },
        "max_wall_clock_seconds": { "type": "integer", "minimum": 1 },
        "on_trip": { "type": "string", "enum": ["FAIL_VERDICT_WITH_FORENSICS"] }
      }
    },
    "invocation_classes": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": {
        "type": "object",
        "required": ["transport", "max_input_tokens", "max_cached_prefix_tokens", "max_volatile_tokens", "max_output_tokens", "max_turns", "max_invocations_per_module", "on_ceiling_exceeded"],
        "additionalProperties": false,
        "properties": {
          "transport": { "type": "string", "enum": ["api_single_turn", "cli_session"] },
          "max_input_tokens": { "type": "integer", "minimum": 1 },
          "max_cached_prefix_tokens": { "type": "integer", "minimum": 0 },
          "max_volatile_tokens": { "type": "integer", "minimum": 1 },
          "max_output_tokens": { "type": "integer", "minimum": 1 },
          "max_turns": { "type": "integer", "minimum": 1 },
          "max_invocations_per_module": { "type": "integer", "minimum": 0 },
          "on_ceiling_exceeded": { "type": "string", "enum": ["REJECT_BEFORE_DISPATCH", "KILL_AND_ESCALATE"] }
        }
      }
    },
    "serialization_triggers": {
      "type": "object",
      "required": ["session_context_high_water_tokens", "action_on_high_water", "state_files", "resume_protocol"],
      "additionalProperties": false,
      "properties": {
        "session_context_high_water_tokens": { "type": "integer", "minimum": 1 },
        "action_on_high_water": { "type": "string", "enum": ["SERIALIZE_STATE_AND_RESPAWN"] },
        "state_files": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
        "resume_protocol": { "type": "string" }
      }
    },
    "compression_policy": {
      "type": "object",
      "required": ["allowed_classes", "max_compressions_per_session", "min_retained_fraction", "protected_blocks", "forbidden_classes"],
      "additionalProperties": false,
      "properties": {
        "allowed_classes": { "type": "array", "items": { "type": "string" } },
        "max_compressions_per_session": { "type": "integer", "minimum": 0 },
        "min_retained_fraction": { "type": "number", "minimum": 0, "maximum": 1 },
        "protected_blocks": { "type": "array", "items": { "type": "string" } },
        "forbidden_classes": { "type": "array", "items": { "type": "string" } }
      }
    }
  }
}
```

Normative instance shipped with this specification:

```json
{
  "version": 1,
  "build_ceiling": {
    "max_total_input_tokens": 2500000,
    "max_total_output_tokens": 1200000,
    "max_wall_clock_seconds": 86400,
    "on_trip": "FAIL_VERDICT_WITH_FORENSICS"
  },
  "invocation_classes": {
    "LEAF_GEN": {
      "transport": "api_single_turn",
      "max_input_tokens": 7250,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 4500,
      "max_output_tokens": 16000,
      "max_turns": 1,
      "max_invocations_per_module": 12,
      "on_ceiling_exceeded": "REJECT_BEFORE_DISPATCH"
    },
    "GATE_FIX": {
      "transport": "cli_session",
      "max_input_tokens": 12000,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 9250,
      "max_output_tokens": 24000,
      "max_turns": 12,
      "max_invocations_per_module": 6,
      "on_ceiling_exceeded": "KILL_AND_ESCALATE"
    },
    "MODULE_INTEGRATE": {
      "transport": "cli_session",
      "max_input_tokens": 16000,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 13250,
      "max_output_tokens": 40000,
      "max_turns": 25,
      "max_invocations_per_module": 2,
      "on_ceiling_exceeded": "KILL_AND_ESCALATE"
    },
    "SEAM_FIX": {
      "transport": "cli_session",
      "max_input_tokens": 12000,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 9250,
      "max_output_tokens": 24000,
      "max_turns": 12,
      "max_invocations_per_module": 1,
      "on_ceiling_exceeded": "KILL_AND_ESCALATE"
    },
    "VERIFY_FIX": {
      "transport": "cli_session",
      "max_input_tokens": 16000,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 13250,
      "max_output_tokens": 40000,
      "max_turns": 25,
      "max_invocations_per_module": 2,
      "on_ceiling_exceeded": "KILL_AND_ESCALATE"
    },
    "ARCHITECT": {
      "transport": "cli_session",
      "max_input_tokens": 20000,
      "max_cached_prefix_tokens": 0,
      "max_volatile_tokens": 20000,
      "max_output_tokens": 60000,
      "max_turns": 30,
      "max_invocations_per_module": 0,
      "on_ceiling_exceeded": "KILL_AND_ESCALATE"
    },
    "MODULE_PLANNER": {
      "transport": "api_single_turn",
      "max_input_tokens": 9000,
      "max_cached_prefix_tokens": 2750,
      "max_volatile_tokens": 6250,
      "max_output_tokens": 12000,
      "max_turns": 1,
      "max_invocations_per_module": 3,
      "on_ceiling_exceeded": "REJECT_BEFORE_DISPATCH"
    }
  },
  "serialization_triggers": {
    "session_context_high_water_tokens": 90000,
    "action_on_high_water": "SERIALIZE_STATE_AND_RESPAWN",
    "state_files": [
      "BUILD_PROGRESS.json",
      "BUILD_METRICS.json",
      "LESSONS.jsonl",
      "interfaces/<module>.md",
      "FILE_DAG.json"
    ],
    "resume_protocol": "Kill session; persist per-module status + fingerprint_history + budgets to BUILD_PROGRESS.json (write-through, lockfile-guarded); respawn a FRESH session whose volatile context is rebuilt exclusively from the state_files listed above. No transcript carry-over of any kind."
  },
  "compression_policy": {
    "allowed_classes": ["MODULE_INTEGRATE", "VERIFY_FIX"],
    "max_compressions_per_session": 1,
    "min_retained_fraction": 0.40,
    "protected_blocks": [
      "NN_RULES",
      "CONVENTIONS",
      "WORK_ORDER.batch_files",
      "WORK_ORDER.visible_interfaces",
      "CURRENT_FAILURE_SET"
    ],
    "forbidden_classes": ["LEAF_GEN", "GATE_FIX", "SEAM_FIX", "MODULE_PLANNER", "ARCHITECT"]
  }
}
```

Enforcement algorithm (runs before and after every dispatch):

```python
def enforce_budget(invocation, budget, ledger):
    cls = budget["invocation_classes"][invocation.klass]
    # 1. Pre-dispatch static checks (REJECT means: orchestrator bug, not model bug)
    if invocation.rendered_prefix_tokens > cls["max_cached_prefix_tokens"]:
        raise BudgetViolation("prefix over ceiling", invocation)
    if invocation.rendered_volatile_tokens > cls["max_volatile_tokens"]:
        invocation.trim_volatile(order=["visible_interfaces", "lessons", "plan_slice"])
        if invocation.rendered_volatile_tokens > cls["max_volatile_tokens"]:
            raise BudgetViolation("volatile over ceiling after trim", invocation)
    if ledger.count(invocation.klass, invocation.module) >= cls["max_invocations_per_module"] \
            and cls["max_invocations_per_module"] > 0:
        raise BudgetExhausted(invocation.klass, invocation.module)
    if ledger.total_input + invocation.estimated_input > budget["build_ceiling"]["max_total_input_tokens"]:
        raise BuildCeilingTripped("input", ledger.total_input)
    # 2. Dispatch with hard runtime guards
    result = dispatch(invocation,
                      max_output=cls["max_output_tokens"],
                      max_turns=cls["max_turns"],
                      kill_on_context_tokens=budget["serialization_triggers"]["session_context_high_water_tokens"])
    # 3. Post-dispatch accounting (write-through BEFORE result is consumed)
    ledger.append(invocation.klass, invocation.module,
                  result.input_tokens, result.output_tokens, result.outcome)
    ledger.flush_to_disk("BUILD_METRICS.json")
    return result
```

---

# SECTION 2: THE DAG DECOMPOSITION ENGINE

## 2.1 DAG Construction Engine — complete algorithm

Input: `REQUIREMENTS.md` (raw multi-module system requirement specification).
Output: `FILE_DAG.json` — every file, class, and public interface to be created,
with dependencies, batches, and worker assignments. **No code is generated until this
artifact validates.**

```python
# ============================================================================
# DAG CONSTRUCTION ENGINE — tier 0, deterministic except two validated LLM calls
# ============================================================================
import graphlib, hashlib, json, re
from pathlib import Path

MODULE_COUNT_MIN, MODULE_COUNT_MAX = 8, 16
LOC_BUDGET_MAX      = 3000
FANIN_CAP           = 6        # max dependents per module (core exempt)
DEPTH_CAP           = 5        # longest dependency chain
F_BATCH             = 4
LOC_CONSERVATION    = 1.2      # sum(est_loc) <= loc_budget * 1.2

def construct_dag(requirements_md: str, build_dir: Path) -> dict:
    spec     = parse_requirements(requirements_md)                    # step 1
    modules  = architect_decompose(spec, build_dir)                   # step 2 (LLM, validated)
    validate_module_graph(modules)                                    # step 3
    seams    = load_seams(build_dir / "SEAMS.json")                   # step 4
    plans    = plan_all_modules(modules, seams, build_dir)            # step 5 (LLM, validated)
    nodes, edges = compile_file_dag(modules, plans, seams)            # step 6
    validate_file_dag(nodes, edges, seams, modules)                   # step 7
    batches  = schedule_batches(nodes, edges)                         # step 8
    assign_workers(batches, n_workers=6)                              # step 9
    dag = {"nodes": nodes, "edges": edges, "batches": batches,
           "toposort": [b["batch_id"] for b in batches]}
    (build_dir / "FILE_DAG.json").write_text(json.dumps(dag, indent=2))
    return dag

# ---- step 1: deterministic requirements parse ------------------------------
def parse_requirements(md: str) -> dict:
    sections = {}
    current = "UNSECTIONED"
    for line in md.splitlines():
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        if h:
            current = h.group(2).strip()
            sections[current] = []
        else:
            sections.setdefault(current, []).append(line)
    features, constraints = [], []
    for title, body in sections.items():
        text = "\n".join(body)
        for item in re.findall(r"(?m)^\s*(?:[-*]|\d+\.)\s+(.+)$", text):
            bucket = constraints if re.search(
                r"(?i)\b(must not|shall not|constraint|non-functional|nfr|limit)\b", item
            ) else features
            bucket.append({"section": title, "text": item.strip()})
    if not features:
        raise SpecError("REQUIREMENTS.md yields zero feature bullets — not plannable")
    return {"sections": sections, "features": features, "constraints": constraints}

# ---- step 2: architect (single Opus session), output schema-validated ------
def architect_decompose(spec: dict, build_dir: Path) -> list[dict]:
    # One LLM call. Prompt = features + constraints + the MODULES.json schema below.
    # Retried at most once with the exact validation error appended.
    for attempt in (1, 2):
        raw = dispatch_architect(spec, build_dir)          # writes MODULES.json, SEAMS.json,
        modules = json.loads((build_dir / "MODULES.json").read_text())  # CONVENTIONS.md, ARCHITECTURE.md
        errs = module_schema_errors(modules)
        if not errs:
            return modules
        if attempt == 2:
            raise SpecError("architect output invalid twice: " + "; ".join(errs))
        feed_back_errors(errs)

def module_schema_errors(modules: list[dict]) -> list[str]:
    errs = []
    if not (MODULE_COUNT_MIN <= len(modules) <= MODULE_COUNT_MAX):
        errs.append(f"module count {len(modules)} outside [{MODULE_COUNT_MIN},{MODULE_COUNT_MAX}]")
    names = [m.get("name") for m in modules]
    if len(names) != len(set(names)):
        errs.append("duplicate module names")
    for m in modules:
        for key in ("name", "kind", "loc_budget", "depends_on", "path_root"):
            if key not in m:
                errs.append(f"{m.get('name','?')}: missing {key}")
        if m.get("loc_budget", 0) > LOC_BUDGET_MAX:
            errs.append(f"{m['name']}: loc_budget {m['loc_budget']} > {LOC_BUDGET_MAX}")
        if m.get("kind") not in ("backend", "frontend"):
            errs.append(f"{m['name']}: kind must be backend|frontend")
        for d in m.get("depends_on", []):
            if d not in names:
                errs.append(f"{m['name']}: depends_on unknown module {d}")
    return errs

# ---- step 3: module-graph structural validation ----------------------------
def validate_module_graph(modules: list[dict]) -> None:
    by_name = {m["name"]: m for m in modules}
    # 3a. acyclicity (raises CycleError with the cycle path)
    ts = graphlib.TopologicalSorter({m["name"]: set(m["depends_on"]) for m in modules})
    order = list(ts.static_order())
    # 3b. pairwise-disjoint path_roots (the ownership axiom)
    roots = sorted(m["path_root"].rstrip("/") + "/" for m in modules)
    for a, b in zip(roots, roots[1:]):
        if b.startswith(a):
            raise SpecError(f"path_root overlap: {a} is a prefix of {b}")
    # 3c. fan-in cap
    fanin = {n: 0 for n in by_name}
    for m in modules:
        for d in m["depends_on"]:
            fanin[d] += 1
    for name, count in fanin.items():
        if count > FANIN_CAP and name != "core":
            raise SpecError(f"module {name}: fan-in {count} > {FANIN_CAP} — split it")
    # 3d. depth cap (longest chain via DP over topological order)
    depth = {n: 1 for n in by_name}
    for name in order:
        for d in by_name[name]["depends_on"]:
            depth[name] = max(depth[name], depth[d] + 1)
    worst = max(depth.values())
    if worst > DEPTH_CAP:
        raise SpecError(f"dependency depth {worst} > {DEPTH_CAP} — flatten the chain")

# ---- step 5: per-module planners (schema-validated, path-prefix enforced) --
def plan_all_modules(modules, seams, build_dir) -> dict:
    plans = {}
    for m in modules:
        for attempt in (1, 2, 3):
            plan = dispatch_planner(m, seams_slice(seams, m), build_dir)
            errs = plan_errors(plan, m)
            if not errs:
                plans[m["name"]] = plan
                break
            if attempt == 3:
                raise SpecError(f"module {m['name']}: plan invalid 3x: {errs}")
            feed_back_errors(errs)
    return plans

def plan_errors(plan: dict, module: dict) -> list[str]:
    errs = []
    floor = max(6, module["loc_budget"] // 400)
    if not (floor <= len(plan["files"]) <= 30):
        errs.append(f"file count {len(plan['files'])} outside [{floor},30]")
    for f in plan["files"]:
        if not f["path"].startswith(module["path_root"]):
            errs.append(f"{f['path']} outside path_root {module['path_root']}")
        if not re.search(r"\.\w+$", f["path"]):
            errs.append(f"{f['path']}: no file extension")
        if "declared_imports" not in f or "exports" not in f:
            errs.append(f"{f['path']}: missing declared_imports or exports")
    total = sum(f.get("est_loc", 0) for f in plan["files"])
    if total > module["loc_budget"] * LOC_CONSERVATION:
        errs.append(f"est LOC {total} > budget {module['loc_budget']} x {LOC_CONSERVATION}")
    return errs

# ---- step 6: compile the file-level DAG ------------------------------------
def compile_file_dag(modules, plans, seams):
    by_name = {m["name"]: m for m in modules}
    nodes, path_owner = {}, {}
    for mname, plan in plans.items():
        for f in plan["files"]:
            if f["path"] in path_owner:
                raise SpecError(f"{f['path']} claimed by {path_owner[f['path']]} AND {mname}")
            path_owner[f["path"]] = mname
            nodes[f["path"]] = {
                "node_id": "n_" + hashlib.sha1(f["path"].encode()).hexdigest()[:10],
                "file_path": f["path"],
                "module": mname,
                "kind": by_name[mname]["kind"],
                "est_loc": f["est_loc"],
                "strict_import_dependencies": sorted(f["declared_imports"]),
                "exported_public_interfaces": f["exports"],
                "purpose": f["purpose"],
                "test_matrix": f.get("test_matrix", []),
                "wave_batch": None,
                "assigned_worker_id": None,
                "status": "PLANNED"
            }
    import_index = build_import_index(nodes)   # dotted import path -> file_path
    edges = []
    for path, node in nodes.items():
        for imp in node["strict_import_dependencies"]:
            target = import_index.get(imp)
            if target is None:
                raise SpecError(f"{path}: declared import {imp} maps to no planned file")
            if target != path:
                edges.append([path, target])
    return nodes, edges

def build_import_index(nodes: dict) -> dict:
    index = {}
    for path in nodes:
        if path.endswith(".py"):
            dotted = (path.replace("code/backend/", "")
                          .removesuffix(".py").removesuffix("/__init__")
                          .replace("/", "."))
            index[dotted] = path
        elif re.search(r"\.(ts|tsx)$", path):
            bare = re.sub(r"\.(ts|tsx)$", "", path.replace("code/frontend/", ""))
            index[bare] = path
            index["@/" + bare.removeprefix("src/")] = path
    return index

# ---- step 7: file-DAG validation --------------------------------------------
def validate_file_dag(nodes, edges, seams, modules) -> None:
    # 7a. acyclic at file granularity
    graph = {p: set() for p in nodes}
    for src, dst in edges:
        graph[src].add(dst)
    list(graphlib.TopologicalSorter(graph).static_order())   # CycleError names the cycle
    # 7b. cross-module edges must be licensed by SEAMS.json
    for src, dst in edges:
        m_src, m_dst = nodes[src]["module"], nodes[dst]["module"]
        if m_src == m_dst:
            continue
        depends = {m["name"]: m["depends_on"] for m in modules}
        if m_dst not in depends[m_src]:
            raise SpecError(f"{src} -> {dst}: {m_src} does not declare dependency on {m_dst}")
        wanted = nodes[dst]["exported_public_interfaces"]
        licensed = {e["symbol"] for e in seams.get(m_dst, [])}
        used = {e["symbol"] for e in wanted if e["symbol"] in licensed}
        if not used:
            raise SpecError(f"{src} -> {dst}: no SEAMS.json entry of {m_dst} licenses this edge; "
                            f"planner must route through a declared seam or architect must add one")

# ---- step 8: batch scheduling (Kahn layering, batch-size capped) ------------
def schedule_batches(nodes, edges):
    indeg = {p: 0 for p in nodes}
    rev = {p: [] for p in nodes}
    for src, dst in edges:            # src depends on dst => dst must be scheduled first
        indeg[src] += 1
        rev[dst].append(src)
    ready = sorted(p for p, d in indeg.items() if d == 0)
    scheduled, batches, counter = set(), [], {}
    while ready:
        layer, ready = ready, []
        by_module = {}
        for p in layer:
            by_module.setdefault(nodes[p]["module"], []).append(p)
        for mname, paths in sorted(by_module.items()):
            for i in range(0, len(paths), F_BATCH):
                counter[mname] = counter.get(mname, 0) + 1
                bid = f"{mname}.b{counter[mname]}"
                chunk = paths[i:i + F_BATCH]
                for p in chunk:
                    nodes[p]["wave_batch"] = bid
                batches.append({"batch_id": bid, "module": mname, "files": chunk,
                                "est_loc": sum(nodes[p]["est_loc"] for p in chunk)})
        for p in layer:
            scheduled.add(p)
            for dependent in rev[p]:
                indeg[dependent] -= 1
                if indeg[dependent] == 0:
                    ready.append(dependent)
        ready.sort()
    if len(scheduled) != len(nodes):
        raise SpecError("scheduler stalled — unscheduled nodes imply an undetected cycle")
    return batches

# ---- step 9: worker assignment (module-affine round robin) ------------------
def assign_workers(batches, n_workers: int) -> None:
    module_lane, next_lane = {}, 0
    for b in batches:
        if b["module"] not in module_lane:
            module_lane[b["module"]] = f"w{(next_lane % n_workers) + 1}"
            next_lane += 1
        b["assigned_worker_id"] = module_lane[b["module"]]
```

## 2.2 DAG node schema (normative) and instance

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "echara://schemas/dag_node.v1.json",
  "title": "FileDagNode",
  "type": "object",
  "required": ["node_id", "file_path", "module", "kind", "est_loc",
               "strict_import_dependencies", "exported_public_interfaces",
               "purpose", "wave_batch", "assigned_worker_id", "status"],
  "additionalProperties": false,
  "properties": {
    "node_id":   { "type": "string", "pattern": "^n_[0-9a-f]{10}$" },
    "file_path": { "type": "string", "pattern": "^code/(backend|frontend)/" },
    "module":    { "type": "string" },
    "kind":      { "type": "string", "enum": ["backend", "frontend"] },
    "est_loc":   { "type": "integer", "minimum": 5, "maximum": 600 },
    "strict_import_dependencies": {
      "type": "array", "items": { "type": "string" }, "uniqueItems": true
    },
    "exported_public_interfaces": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["symbol", "kind", "signature"],
        "additionalProperties": false,
        "properties": {
          "symbol":    { "type": "string" },
          "kind":      { "type": "string", "enum": ["function", "class", "constant", "type", "router"] },
          "signature": { "type": "string", "maxLength": 400 }
        }
      }
    },
    "purpose":     { "type": "string", "maxLength": 300 },
    "test_matrix": { "type": "array", "items": { "type": "string",
                     "enum": ["success", "validation_error", "unauthenticated",
                              "not_found", "ownership_violation", "unit"] } },
    "wave_batch":         { "type": ["string", "null"] },
    "assigned_worker_id": { "type": ["string", "null"], "pattern": "^w[1-6]$" },
    "status": { "type": "string",
                "enum": ["PLANNED", "DISPATCHED", "GATED", "INTEGRATED", "FAILED"] }
  }
}
```

Instance:

```json
{
  "node_id": "n_f3a91c07d2",
  "file_path": "code/backend/app/orders/service.py",
  "module": "orders",
  "kind": "backend",
  "est_loc": 220,
  "strict_import_dependencies": [
    "app.core.db",
    "app.customers.schemas",
    "app.inventory.service",
    "app.orders.models",
    "app.orders.schemas"
  ],
  "exported_public_interfaces": [
    { "symbol": "create_order",  "kind": "function",
      "signature": "def create_order(db: Session, customer_id: int, lines: list[OrderLineIn]) -> OrderOut" },
    { "symbol": "confirm_order", "kind": "function",
      "signature": "def confirm_order(db: Session, order_id: int, actor_id: int) -> OrderOut" },
    { "symbol": "cancel_order",  "kind": "function",
      "signature": "def cancel_order(db: Session, order_id: int, actor_id: int) -> OrderOut" }
  ],
  "purpose": "Order lifecycle service: create/confirm/cancel with stock reservation via the inventory seam",
  "test_matrix": ["success", "validation_error", "not_found", "ownership_violation", "unit"],
  "wave_batch": "orders.b2",
  "assigned_worker_id": "w3",
  "status": "PLANNED"
}
```

## 2.3 Race-Condition & Git Worktree Isolation Protocol

### 2.3.1 Exact terminal orchestration (build start)

```bash
# tier 0, once per build --------------------------------------------------------
git -C "$REPO" checkout -b "integration/${BUILD_ID}" main
git -C "$REPO" commit --allow-empty -m "build ${BUILD_ID}: integration base"

# six isolated lanes ------------------------------------------------------------
for W in w1 w2 w3 w4 w5 w6; do
  git -C "$REPO" branch "lane/${BUILD_ID}/${W}" "integration/${BUILD_ID}"
  git -C "$REPO" worktree add "${LANES_ROOT}/${W}" "lane/${BUILD_ID}/${W}"
done
# LANES_ROOT is OUTSIDE any cloud-synced directory (OneDrive fsync flake guard),
# and short (Windows MAX_PATH guard — see phases.py:_rmtree_long).

# lane merge-back (per completed module, serialized through one merge mutex) ----
git -C "$REPO" merge-tree --write-tree "integration/${BUILD_ID}" "lane/${BUILD_ID}/${W}" \
  > "${BUILD_DIR}/merge_preflight_${W}.txt"
#   any "CONFLICT" line in preflight output => ownership violation => lane FAILED,
#   merge NOT attempted (a conflict is defined as a partitioner bug, not a situation
#   to resolve).
git -C "$REPO" checkout "integration/${BUILD_ID}"
git -C "$REPO" merge --no-ff -m "merge ${MODULE} from ${W}" "lane/${BUILD_ID}/${W}"

# lane teardown ------------------------------------------------------------------
git -C "$REPO" worktree remove --force "${LANES_ROOT}/${W}"
git -C "$REPO" branch -D "lane/${BUILD_ID}/${W}"
git -C "$REPO" worktree prune
```

### 2.3.2 The lock table

`WRITE_LOCKS.json` at the build root, mutated only under an OS lockfile
(`WRITE_LOCKS.json.lock`, acquired with `msvcrt.locking` on Windows / `fcntl.flock`
on POSIX, 5 s timeout, exponential backoff x3):

```json
{
  "version": 1,
  "locks": [
    { "lock_id": "L-orders-b2",
      "worker_id": "w3",
      "module": "orders",
      "paths": [
        { "file_path": "code/backend/app/orders/service.py", "range": [1, -1] },
        { "file_path": "code/backend/app/orders/schemas.py", "range": [1, -1] }
      ],
      "acquired_at": "2026-07-05T14:02:11",
      "expires_at":  "2026-07-05T14:27:11" }
  ]
}
```

`range: [1, -1]` means whole-file. **Policy: every model-writable file is granted as a
whole-file range.** Sub-file ranges exist in the schema for exactly one file class —
deterministically generated shared files (`requirements.txt`, `app/main.py` router
registry, the alembic chain) whose per-module fragments occupy disjoint generated
blocks — and those are written by tier-0 code, never by a model. Two agents never
co-edit one file: a file needing two writers is mis-factored or becomes generated.

### 2.3.3 State isolation algorithm — step by step

```
STEP 0  PRECONDITION. FILE_DAG.json validated (Section 2.1 step 7): every file has
        exactly one owner module; module path_roots are pairwise disjoint; every
        batch is a subset of one module's files. Disjointness is therefore a
        THEOREM about the lock table, not a hope: two live locks can only
        intersect if validation was bypassed.

STEP 1  ACQUIRE. Before dispatching batch B on worker W:
          with oslock("WRITE_LOCKS.json.lock"):
              table = load("WRITE_LOCKS.json")
              for existing in table.locks:
                  for (p1, r1) in existing.paths:
                      for (p2, r2) in B.paths_with_ranges:
                          if p1 == p2 and ranges_intersect(r1, r2):
                              raise LockCollision(existing.lock_id, B.batch_id)
              table.locks.append(lock_record(B, W, ttl_minutes=25))
              save("WRITE_LOCKS.json")
        LockCollision is a HARD ERROR (partitioner bug) — logged, batch not
        dispatched, build fails fast with both claimants named.

        def ranges_intersect(r1, r2):
            a1, b1 = r1[0], (10**9 if r1[1] == -1 else r1[1])
            a2, b2 = r2[0], (10**9 if r2[1] == -1 else r2[1])
            return a1 <= b2 and a2 <= b1

STEP 2  EXECUTE. Worker W runs inside its own worktree LANES_ROOT/W. It has no
        handle to any other worktree, to the integration branch, or to the repo
        root: LEAF_GEN workers have NO filesystem at all (single API turn; the
        work order is their entire universe); CLI-session workers are launched
        with cwd = LANES_ROOT/W and the work-order prompt.

STEP 3  INTERCEPT (pre-commit write-collision interception).
          changed = git -C LANES_ROOT/W status --porcelain --untracked-files=all
          granted = set(p for (p, r) in B.paths_with_ranges)
          violations = [path for (status, path) in parse(changed) if path not in granted]
          if violations:
              git -C LANES_ROOT/W checkout -- <violations>          # tracked files
              rm <violations that are untracked>                    # new stray files
              ledger_append(lesson tags=["protocol"], symptom="wrote outside grant",
                            files=violations, worker=W)             # Section 5
              gate B normally (its own files may still be good)
        Nothing a worker does outside its grant can ever reach a commit.

STEP 4  GATE + COMMIT. Wave gate (Section 4) runs inside the lane. On pass:
          git -C LANES_ROOT/W add <granted paths only>
          git -C LANES_ROOT/W commit -m "batch ${B.batch_id}"

STEP 5  RELEASE. Under the OS lockfile, remove B's lock record. Locks also expire
        by TTL: a wedged worker's lock is reaped by the scheduler after
        expires_at + 60 s AND subprocess kill confirmation (never reap a lock
        whose process is still alive).

STEP 6  MERGE. When a module's final batch is INTEGRATED and seams_ok:
        merge-tree preflight, then --no-ff merge (commands in 2.3.1), serialized
        through a single merge mutex so integration-branch updates are atomic.
        Post-merge: regenerate interfaces/<module>.md on the integration branch;
        dependent lanes created AFTER this point fork from the updated branch and
        therefore see the frozen, integrated dependency — never a moving target.
```

Concurrency safety summary: worker↔worker isolation by disjoint worktrees + disjoint
lock grants; worker↔orchestrator isolation by lockfile-guarded JSON write-through;
lane↔integration isolation by merge mutex + preflight. There is no shared mutable
state anywhere in the system that is not behind exactly one of those three guards.

---

# SECTION 3: AST INTERFACE-ONLY ISOLATION ENGINE

## 3.1 AST Dependency Extractor Engine — complete algorithm

Contract: input = one source file; output = its public interface map with **zero
implementation content**: no function bodies, no local variables, no private symbols,
no comments. Deterministic, stdlib-only, zero model tokens to produce.

### 3.1.1 Python extractor (stdlib `ast`)

```python
import ast

def extract_python_interface(source: str, module_dotted: str) -> list[str]:
    """Public interface of one Python file. Returns signature lines only.
    Scrubs: bodies, locals, private names (_-prefixed), comments, decorators
    except routing-relevant ones, default-value expressions beyond literals."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [f"# {module_dotted}: UNPARSEABLE (syntax error) — treat as no exports"]
    out: list[str] = [f"# module {module_dotted}"]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            out.append(_signature_of(node, indent=""))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            bases = ", ".join(_safe_unparse(b) for b in node.bases)
            out.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
            emitted = 0
            for member in node.body:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name) \
                        and not member.target.id.startswith("_"):
                    out.append(f"    {member.target.id}: {_safe_unparse(member.annotation)}")
                    emitted += 1
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and not member.name.startswith("_"):
                    out.append(_signature_of(member, indent="    "))
                    emitted += 1
            if emitted == 0:
                out.append("    pass")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    literal = _literal_or_type(node)
                    out.append(f"{t.id} = {literal}" if literal else t.id)
    return out

def _signature_of(node, indent: str) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = []
    a = node.args
    for arg in getattr(a, "posonlyargs", []):
        args.append(_fmt_arg(arg))
    if getattr(a, "posonlyargs", []):
        args.append("/")
    defaults_pad = len(a.args) - len(a.defaults)
    for i, arg in enumerate(a.args):
        d = a.defaults[i - defaults_pad] if i >= defaults_pad else None
        args.append(_fmt_arg(arg) + (f" = {_literal_only(d)}" if d is not None else ""))
    if a.vararg:
        args.append("*" + _fmt_arg(a.vararg))
    elif a.kwonlyargs:
        args.append("*")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        args.append(_fmt_arg(arg) + (f" = {_literal_only(d)}" if d is not None else ""))
    if a.kwarg:
        args.append("**" + _fmt_arg(a.kwarg))
    ret = f" -> {_safe_unparse(node.returns)}" if node.returns is not None else ""
    return f"{indent}{prefix} {node.name}({', '.join(args)}){ret}"

def _fmt_arg(arg) -> str:
    if arg.annotation is not None:
        return f"{arg.arg}: {_safe_unparse(arg.annotation)}"
    return arg.arg

def _literal_only(expr) -> str:
    # Defaults are kept ONLY when they are literals; call/attribute defaults leak
    # implementation and are replaced by an opaque marker.
    if isinstance(expr, ast.Constant):
        return repr(expr.value)
    return "<computed>"

def _literal_or_type(node) -> str:
    if isinstance(node, ast.AnnAssign):
        return f": {_safe_unparse(node.annotation)}"
    if isinstance(node.value, ast.Constant):
        return repr(node.value.value)
    return ""

def _safe_unparse(expr) -> str:
    try:
        return ast.unparse(expr)
    except Exception:
        return "object"
```

### 3.1.2 TypeScript extractor (regex + brace scrubber, no compiler dependency)

```python
import re

TS_EXPORT_PATTERNS = [
    re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?function\s+\w+\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^({\n]+)?"),
    re.compile(r"^export\s+(?:default\s+)?(?:abstract\s+)?class\s+\w+(?:<[^>]*>)?(?:\s+extends\s+[\w.<>,\s]+)?(?:\s+implements\s+[\w.<>,\s]+)?"),
    re.compile(r"^export\s+interface\s+\w+(?:<[^>]*>)?(?:\s+extends\s+[\w.<>,\s]+)?"),
    re.compile(r"^export\s+type\s+\w+(?:<[^>]*>)?\s*="),
    re.compile(r"^export\s+(?:const|let)\s+\w+\s*(?::\s*[^=\n]+)?"),
    re.compile(r"^export\s+enum\s+\w+"),
]

def extract_ts_interface(source: str, rel_path: str) -> list[str]:
    """Public surface of one .ts/.tsx file: export declarations with bodies scrubbed.
    interface/type/enum bodies are KEPT (they are pure shape, zero implementation);
    function and class bodies are REMOVED via brace matching; class public method
    signatures are re-emitted from the scrubbed class body."""
    lines = _strip_comments(source).splitlines()
    out = [f"// file {rel_path}"]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        matched = next((p for p in TS_EXPORT_PATTERNS if p.match(line)), None)
        if matched is None:
            i += 1
            continue
        block, i = _consume_block(lines, i)
        if re.match(r"^export\s+(interface|type|enum)\b", block):
            out.append(_compact_ws(block))               # shape-only: keep whole body
        elif re.match(r"^export\s+(?:default\s+)?(?:abstract\s+)?class\b", block):
            header = block[: block.find("{")].strip()
            out.append(header + " {")
            for m in re.finditer(
                r"(?m)^\s*(?:public\s+)?(?:static\s+)?(?:async\s+)?"
                r"(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(:\s*[^({\n]+)?\s*\{", block):
                name = m.group(1)
                if name.startswith("_") or name in ("constructor",) or \
                   re.search(r"\bprivate\b", block[max(0, m.start() - 40): m.start()]):
                    continue
                out.append(f"  {name}({m.group(2).strip()}){(m.group(3) or '').strip()}")
            out.append("}")
        else:                                             # function / const / let
            sig = block.split("{")[0].split("=>")[0].rstrip(" =")
            out.append(_compact_ws(sig))
    return out

def _consume_block(lines: list[str], start: int) -> tuple[str, int]:
    """Return the full statement beginning at lines[start] (balanced braces or
    up to the terminating semicolon/newline for brace-less statements)."""
    buf, depth, opened, i = [], 0, False, start
    while i < len(lines):
        buf.append(lines[i])
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        i += 1
        if opened and depth == 0:
            break
        if not opened and buf[-1].rstrip().endswith((";",)):
            break
        if not opened and len(buf) >= 3:                  # brace-less one-liner guard
            break
    return "\n".join(buf), i

def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(?m)//[^\n]*$", "", src)

def _compact_ws(s: str) -> str:
    return re.sub(r"\n\s*", " ", s.strip())
```

### 3.1.3 Closure assembly (what one worker is allowed to see)

```python
def visible_interfaces(dag: dict, batch: dict, iface_store: dict) -> dict[str, list[str]]:
    """iface_store: module_dotted_path -> extracted signature lines, regenerated
    from disk after every merged batch. Selection = declared-import closure of
    THIS batch only — never 'all dependencies of the module'."""
    wanted: set[str] = set()
    for path in batch["files"]:
        wanted.update(dag["nodes"][path]["strict_import_dependencies"])
    own_module = batch["module"]
    for path, node in dag["nodes"].items():
        if node["module"] == own_module and node["status"] in ("GATED", "INTEGRATED"):
            wanted.add(dotted_of(path))                   # own already-built files
    result, spent = {}, 0
    for dotted in sorted(wanted):
        block = iface_store.get(dotted, [])
        cost = token_estimate(block)
        if spent + cost > 3000:                           # IFC_MAX hard trim
            result[dotted] = [f"# trimmed for budget — symbols: " +
                              ", ".join(first_symbols(block, 12))]
            continue
        result[dotted] = block
        spent += cost
    return result
```

## 3.2 Prompt injection — the exact wrapper

Raw text of a complete LEAF_GEN dispatch. Agent A (worker `w2`) previously built
`inventory`; its extracted interface bridges to Agent B (worker `w3`) building
`orders`. Not one line of `inventory`'s implementation appears.

```
================================ SYSTEM (cached prefix, frozen at build start) =====
You are a leaf code generator inside a compiler-style orchestration. You produce
complete, production-quality source files and NOTHING else.

OUTPUT PROTOCOL (violation = rejected pass):
- For each file in BATCH_FILES, emit exactly one fenced block:
  ```path=<file_path>
  <complete file content>
  ```
- No prose before, between, or after blocks. No partial files. No TODOs, no stubs.

[NN_RULES — 1,200 tokens, verbatim from agents/builder.py:_NN_RULES]
[CONVENTIONS.md — 1,000 tokens, verbatim, frozen at human gate 1]
<<cache_control breakpoint>>
================================ USER (volatile work order) ========================
WORK_ORDER orders.b2.try1 | build build_20260705_101530_112233 | module orders

BATCH_FILES (you own exactly these; you cannot see or touch anything else):
1. code/backend/app/orders/service.py  (~220 LOC)
   PURPOSE: Order lifecycle service: create/confirm/cancel with stock reservation.
   PLAN:
   - create_order(db, customer_id, lines) -> OrderOut: validate lines non-empty,
     resolve customer via customers seam, reserve stock via inventory seam
     (reserve_stock), persist Order + OrderLine rows, return OrderOut.
   - confirm_order(db, order_id, actor_id) -> OrderOut: 404 if missing, 403 if
     actor is not owner, transition DRAFT->CONFIRMED, error ORD_STATE if not DRAFT.
   - cancel_order(db, order_id, actor_id) -> OrderOut: release reserved stock via
     release_stock, transition to CANCELLED, same guards as confirm.
   TESTS REQUIRED: success, validation_error, not_found, ownership_violation, unit.
2. code/backend/app/orders/schemas.py  (~90 LOC)
   PURPOSE: Pydantic schemas: OrderLineIn, OrderCreate, OrderOut, OrderLineOut.
   PLAN: OrderOut.status is the str enum value; OrderOut.model_config enables
   from_attributes; lines nested as list[OrderLineOut].

VISIBLE INTERFACES (accurate, extracted from code already on disk — import from
these EXACTLY; nothing else exists in your universe):
--- app.core.db ---
def get_session() -> Iterator[Session]
class Base(DeclarativeBase):
    pass
--- app.inventory.service ---
def reserve_stock(db: Session, sku: str, qty: int) -> Reservation
def release_stock(db: Session, reservation_id: int) -> None
class Reservation(Base):
    id: int
    sku: str
    qty: int
--- app.customers.schemas ---
class CustomerOut(BaseModel):
    id: int
    email: str
--- app.orders.models ---
class Order(Base):
    id: int
    customer_id: int
    status: OrderStatus
class OrderLine(Base):
    id: int
    order_id: int
    sku: str
    qty: int
class OrderStatus(str, Enum):
    DRAFT = 'DRAFT'
    CONFIRMED = 'CONFIRMED'
    CANCELLED = 'CANCELLED'

CONTRACT SLICE (implement exactly; source of truth CONTRACT_REGISTRY.json):
POST /orders          request=OrderCreate   response=OrderOut   status=201
POST /orders/{id}/confirm                   response=OrderOut   status=200

LESSONS (operational guardrails from prior passes — obey):
- L-0142 [sqlalchemy] Use Session.get(Model, id), not Query.get (removed in 2.0).
- L-0159 [orders] OrderStatus persists as VARCHAR; compare with .value in raw SQL.
- L-0161 [backend] Services return schema objects (model_validate), never live ORM
  instances; routers must not touch lazy relationships.

FORBIDDEN: paths outside BATCH_FILES; new dependencies; any import not present in
VISIBLE INTERFACES. You have no filesystem: VISIBLE INTERFACES is complete.
====================================================================================
```

The bridge property, stated precisely: `orders.b2` may call `reserve_stock` because
(a) `FILE_DAG.json` records the edge `orders/service.py -> inventory/service.py`,
(b) that edge is licensed by an `inventory` entry in `SEAMS.json`, and (c) the
extractor projected `inventory/service.py` down to three signature lines. Agent B
compiles against Agent A's surface exactly the way a linker uses a symbol table —
possession of the implementation is neither needed nor possible.

---

# SECTION 4: THE SELF-HEALING TDD FAILURE CLASSIFICATION MACHINE

## 4.1 Error lifecycle — state-mutation diagram

```
                             ┌────────────────────────────────────────────────┐
                             │                DETECTORS (deterministic)        │
                             │  wave gate: exists + py_compile / tsc --noEmit │
                             │  module gate: scoped pytest / vitest            │
                             │  seam gate: AST exports vs SEAMS.json           │
                             │  VERIFY: import smoke + alembic + full pytest   │
                             │          --junitxml + tsc + vite build          │
                             └───────────────┬────────────────────────────────┘
                                             │ raw error artifact
                                             ▼
                                   ┌──────────────────┐
                                   │  E_CAPTURED      │  parse trace/log →
                                   │                  │  structured failure set F
                                   └───────┬──────────┘
                                           │ fingerprint(F); breaker consult (4.3)
                          brake tripped ◄──┤
                                           ▼
                                   ┌──────────────────┐
                                   │  E_CLASSIFIED    │  classifier (4.2) →
                                   └───┬────┬────┬────┘  exactly one state
                        STATE_LOCAL_BUG│    │    │STATE_UPSTREAM_HALLUCINATION
                                       │    │STATE_INTERFACE_BREACH
                     ┌─────────────────┘    └───────────────┐
                     ▼                  ▼                    ▼
             ┌──────────────┐  ┌────────────────────┐  ┌─────────────────────┐
             │ E_ROUTED_    │  │ E_ROUTED_CONTRACT  │  │ E_ROUTED_RESYNC     │
             │ LOCAL        │  │ halt module branch;│  │ rollback worker     │
             │ re-invoke    │  │ escalate to sub-   │  │ output (git checkout│
             │ owning worker│  │ orchestrator of    │  │ granted paths);     │
             │ w/ exact log │  │ PROVIDER module;   │  │ regenerate iface    │
             │ snippet      │  │ seam-fix dispatch; │  │ closure from disk;  │
             │ (budget      │  │ re-run consumers'  │  │ re-init worker with │
             │  B_INTEG)    │  │ failing tests      │  │ fresh work order    │
             │              │  │ (budget B_SEAM)    │  │ (counts as retry)   │
             └──────┬───────┘  └─────────┬──────────┘  └──────────┬──────────┘
                    │ fix pass           │ fix pass               │ regen pass
                    ▼                    ▼                        ▼
                                 ┌──────────────────┐
                                 │  E_REVERIFY      │  re-run ONLY the detecting
                                 │                  │  gate, scoped
                                 └────┬──────┬──────┘
                            all clear │      │ still failing
                                      ▼      ▼
                            ┌───────────┐  ┌────────────────────────────────┐
                            │ E_RESOLVED│  │ fingerprint check:             │
                            │ lesson    │  │  same fp → ESCALATE one tier   │
                            │ appended  │  │  A/B/A cycle → HARD_BRAKE      │
                            │ (Sec. 5)  │  │  budget spent → VERIFY backstop│
                            └───────────┘  │  R_MAX exhausted → FAILED      │
                                           │  VERDICT + DEBUG_STATE dump    │
                                           └────────────────────────────────┘
```

## 4.2 Error Root-Cause Classifier — complete execution algorithm

```python
# ============================================================================
# ERROR ROOT-CAUSE CLASSIFIER — tier 0/1, deterministic, zero model tokens
# Input : raw stack trace | compiler error text | linter log | junitxml failures
# Output: (STATE, routing) where STATE in {STATE_LOCAL_BUG,
#          STATE_INTERFACE_BREACH, STATE_UPSTREAM_HALLUCINATION}
# ============================================================================
import re

RE_PY_FRAME  = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
RE_PY_EXC    = re.compile(r'^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt))\s*:?\s*(?P<msg>.*)$')
RE_PY_IMPORT = re.compile(r"(?:ModuleNotFoundError|ImportError).*?(?:No module named|cannot import name)\s+'(?P<sym>[\w.]+)'(?:\s+from\s+'(?P<src>[\w.]+)')?")
RE_PY_ATTR   = re.compile(r"AttributeError: (?:module )?'?(?P<obj>[\w.]+)'? (?:object )?has no attribute '(?P<attr>\w+)'")
RE_PY_SIG    = re.compile(r"TypeError: (?P<func>[\w.]+)\(\) (?:got an unexpected keyword argument '(?P<kw>\w+)'|takes (?P<takes>[\w\s]+) positional arguments? but (?P<given>\d+))")
RE_TS_ERROR  = re.compile(r'^(?P<path>[^\s(]+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$')
TS_HALLUCINATION_CODES = {"TS2307", "TS2305", "TS2339", "TS2551", "TS2724"}
TS_SIGNATURE_CODES     = {"TS2345", "TS2554", "TS2322", "TS2769"}

STATE_LOCAL_BUG               = "STATE_LOCAL_BUG"
STATE_INTERFACE_BREACH        = "STATE_INTERFACE_BREACH"
STATE_UPSTREAM_HALLUCINATION  = "STATE_UPSTREAM_HALLUCINATION"

def classify(raw: str, failing_test_path: str | None,
             dag: dict, seams: dict, iface_index: dict,
             module_of_path) -> dict:
    # ---- phase 1: PARSE the artifact into (frames, exception, boundary_symbol)
    frames, exc_type, exc_msg = [], None, raw.strip().splitlines()[-1] if raw.strip() else ""
    for line in raw.splitlines():
        f = RE_PY_FRAME.match(line)
        if f:
            frames.append({"path": f["path"], "line": int(f["line"]), "func": f["func"]})
        ts = RE_TS_ERROR.match(line)
        if ts:
            frames.append({"path": ts["path"], "line": int(ts["line"]), "func": "<ts>",
                           "ts_code": ts["code"], "ts_msg": ts["msg"]})
        e = RE_PY_EXC.match(line)
        if e:
            exc_type, exc_msg = e["type"], e["msg"]

    # ---- phase 2: OWNERSHIP — innermost project frame and test owner
    project_frames = [f for f in frames if is_project_path(f["path"])]
    raise_frame  = project_frames[-1] if project_frames else None
    raise_module = module_of_path(raise_frame["path"]) if raise_frame else None
    test_module  = module_of_path(failing_test_path) if failing_test_path else raise_module

    # ---- phase 3: SYMBOL EXTRACTION at the boundary
    missing_symbol, provider_guess = None, None
    m = RE_PY_IMPORT.search(raw)
    if m:
        missing_symbol, provider_guess = m["sym"], m["src"] or m["sym"].rsplit(".", 1)[0]
    m = RE_PY_ATTR.search(raw)
    if m and missing_symbol is None:
        missing_symbol, provider_guess = m["attr"], m["obj"]
    m = RE_PY_SIG.search(raw)
    signature_mismatch = m["func"] if m else None
    for f in frames:
        code = f.get("ts_code")
        if code in TS_HALLUCINATION_CODES and missing_symbol is None:
            sym = re.search(r"'([^']+)'", f["ts_msg"])
            missing_symbol = sym.group(1) if sym else f["ts_msg"][:60]
            provider_guess = ts_import_source(f)      # resolve from the file's import lines
        if code in TS_SIGNATURE_CODES and signature_mismatch is None:
            signature_mismatch = f["path"] + ":" + str(f["line"])

    # ---- phase 4: DECISION TREE (order is normative) ------------------------
    # 4a. Purely intra-module: raise and test in the same module, no cross symbol
    if missing_symbol is None and signature_mismatch is None \
            and raise_module is not None and raise_module == test_module:
        return _route_local(test_module, raw)

    # 4b. A symbol is missing/unknown → arbitrate against the CONTRACT
    if missing_symbol is not None:
        provider_module = module_of_dotted(provider_guess, dag) if provider_guess else None
        licensed = {e["symbol"] for e in seams.get(provider_module, [])} if provider_module else set()
        actually_exported = symbol_in_index(missing_symbol, provider_guess, iface_index)
        if provider_module is None or missing_symbol.split(".")[-1] not in licensed:
            # Provider never promised it: the CONSUMER invented the dependency.
            return _route_hallucination(consumer=test_module or raise_module,
                                        symbol=missing_symbol, provider=provider_guess, raw=raw)
        if not actually_exported:
            # Promised in SEAMS.json, absent on disk: the PROVIDER broke contract.
            return _route_breach(provider=provider_module, symbol=missing_symbol, raw=raw)
        # Promised AND exported, yet unresolved at runtime → environment/import
        # wiring inside the consumer (wrong path style, missing __init__): local.
        return _route_local(test_module or raise_module, raw)

    # 4c. Signature mismatch → compare call-site expectation vs extracted truth
    if signature_mismatch is not None:
        callee_module = module_of_callsite(signature_mismatch, frames, dag, module_of_path)
        if callee_module is not None and callee_module != (test_module or raise_module):
            declared = seam_signature(callee_module, signature_mismatch, seams)
            actual   = index_signature(callee_module, signature_mismatch, iface_index)
            if declared is not None and actual is not None and declared != actual:
                return _route_breach(provider=callee_module,
                                     symbol=signature_mismatch, raw=raw)
            # Contract and disk agree → the CALLER is calling it wrong.
            return _route_hallucination(consumer=test_module or raise_module,
                                        symbol=signature_mismatch,
                                        provider=callee_module, raw=raw)
        return _route_local(test_module or raise_module, raw)

    # 4d. Cross-module raise without symbol evidence (semantic failure in the
    # provider's code while honoring its signature) → local bug AT THE RAISE SITE.
    if raise_module is not None and test_module is not None and raise_module != test_module:
        return _route_local(raise_module, raw)

    # 4e. No project frames at all (env, collection error, root conftest)
    return {"state": STATE_LOCAL_BUG, "scope": "__unrouted__",
            "action": "WHOLE_BUILD_FIX_SESSION", "error_snippet": tail(raw, 4000)}

# ---- routing actions --------------------------------------------------------
def _route_local(module: str, raw: str) -> dict:
    return {"state": STATE_LOCAL_BUG, "scope": module,
            "action": "REINVOKE_OWNING_WORKER",
            "invocation_class": "VERIFY_FIX", "budget": "B_INTEG(module)<=2",
            "context": "module work-order context + this exact snippet ONLY",
            "error_snippet": tail(raw, 4000)}

def _route_breach(provider: str, symbol: str, raw: str) -> dict:
    return {"state": STATE_INTERFACE_BREACH, "scope": provider,
            "action": "HALT_CONSUMER_BRANCHES_THEN_SEAM_FIX",
            "steps": [
                f"1. mark every unmerged batch of modules depending on {provider} HELD",
                f"2. dispatch SEAM_FIX to {provider}: 'your exports must match SEAMS.json "
                f"exactly; violated symbol: {symbol}' (budget B_SEAM=1)",
                "3. regenerate interfaces/<provider>.md from disk",
                "4. IF the provider CANNOT honor the contract: SEAMS.json is immutable "
                "mid-build — fail fast with an architect-level report naming the seam; "
                "contract rewrites re-enter at human gate 1 and re-propagate down the "
                "DAG as new work orders for every consumer node of that seam",
                "5. release HELD batches; re-run ONLY consumers' failing tests"],
            "error_snippet": tail(raw, 4000)}

def _route_hallucination(consumer: str, symbol: str, provider, raw: str) -> dict:
    return {"state": STATE_UPSTREAM_HALLUCINATION, "scope": consumer,
            "action": "ROLLBACK_AND_RESYNC",
            "steps": [
                f"1. git checkout -- <granted paths of the offending batch> (rollback)",
                f"2. regenerate the interface closure for the batch from CURRENT disk",
                f"3. re-render the work order; the fresh VISIBLE INTERFACES section now "
                f"proves {symbol!r} does not exist on {provider!r}",
                "4. re-initialize the worker (one fresh LEAF_GEN dispatch; counts "
                "against the batch retry budget of 1)"],
            "error_snippet": tail(raw, 4000)}
```

Classification invariants: (1) `SEAMS.json` is the immutable referee — fault is
assigned from the contract, never from "whoever ran last", so consumer/provider
blame cannot oscillate; (2) every route names a budget; (3) the classifier consumes
zero model tokens.

## 4.3 Hard-Brake Circuit Breaker — exact algorithms

**Fingerprint.**

```python
import hashlib, re

def fingerprint(failure_set: list[dict]) -> str:
    """Stable identity of a failure state. Normalization strips everything that
    varies between identical failures: line numbers, addresses, tmp paths,
    timestamps, object ids, durations."""
    def norm(s: str) -> str:
        s = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
        s = re.sub(r"line \d+", "line N", s)
        s = re.sub(r"[/\\](?:tmp|Temp)[/\\][^\s'\"]+", "/TMP", s)
        s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?", "TS", s)
        s = re.sub(r"\b\d+\.\d+s\b", "DUR", s)
        return s
    parts = sorted(
        f"{f['test_id']}|{f['exc_type']}|{norm(f['message'])[:160]}|"
        f"{f.get('innermost_frame', {}).get('path','')}:{f.get('innermost_frame', {}).get('func','')}"
        for f in failure_set)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()
```

**Breaker state and rules** (persisted to `BUILD_PROGRESS.json` **before** every fix
dispatch, so a crash cannot double-spend):

```python
BRAKE_REASONS = ("FP_REPEAT", "FP_OSCILLATION", "NON_MONOTONE", "TOKEN_METER",
                 "PASS_METER", "WALL_CLOCK", "MALFORMED_OUTPUT_REPEAT")

def consult_breaker(module: str, prog: dict, metrics: dict,
                    new_fp: str, new_fail_count: int, budget: dict) -> str | None:
    st = prog["modules"][module]
    hist = st.setdefault("fingerprint_history", [])

    # RULE 1 — repetition: same fingerprint twice in a row => the fix did nothing.
    if hist and hist[-1] == new_fp:
        st["escalation_tier"] = st.get("escalation_tier", 0) + 1
        # tiers: 0 worker -> 1 module fix -> 2 whole-build fix -> 3 FAILED
        if st["escalation_tier"] >= 3:
            return "FP_REPEAT"

    # RULE 2 — oscillation: A/B/A within the last 4 entries => two fixes are
    # undoing each other. Immediate module brake; other lanes continue.
    window = (hist + [new_fp])[-4:]
    if len(window) >= 3 and any(window[i] == window[i + 2] != window[i + 1]
                                for i in range(len(window) - 2)):
        return "FP_OSCILLATION"

    # RULE 3 — monotone progress: failing count must strictly decrease across
    # consecutive fix rounds for the same module.
    prev = st.get("last_fail_count")
    if prev is not None and new_fail_count >= prev:
        st["stall_rounds"] = st.get("stall_rounds", 0) + 1
        if st["stall_rounds"] >= 2:
            return "NON_MONOTONE"
    else:
        st["stall_rounds"] = 0

    # RULE 4 — token meter:   spent(m) <= 3 * loc_budget(m) * TAU_GEN
    ceiling = 3 * st["loc_budget"] * 15
    if metrics["module_tokens"][module] > ceiling:
        return "TOKEN_METER"

    # RULE 5 — pass meter: budgets B_GATE/B_INTEG/B_SEAM/B_GLOBAL already
    # decremented pre-dispatch; a route with an exhausted budget falls through
    # to the VERIFY backstop (R_MAX = 3), never silently re-dispatches.
    if prog["global_fix_used"] >= 25:
        return "PASS_METER"

    # RULE 6 — wall clock (build-level, checked here for locality)
    if metrics["build_elapsed_sec"] > 86400:
        return "WALL_CLOCK"

    hist.append(new_fp)
    st["last_fail_count"] = new_fail_count
    return None
```

**Kill sequence** (a brake is a scheduled outcome, not an exception):

```python
def hard_brake(module: str, reason: str, prog, metrics, failure_set, build_dir):
    kill_subprocess_tree(module)                       # SIGTERM, 10 s grace, SIGKILL
    release_locks(module)                              # Section 2.3.3 step 5
    prog["modules"][module]["status"] = "FAILED"
    prog["modules"][module]["brake_reason"] = reason
    save_write_through(prog, build_dir)                # BEFORE anything else
    debug = {
        "module": module,
        "brake_reason": reason,
        "fingerprint_history": prog["modules"][module]["fingerprint_history"],
        "escalation_tier": prog["modules"][module].get("escalation_tier", 0),
        "budgets": {"gate": prog["modules"][module].get("gate_fixes", 0),
                    "integration": prog["modules"][module].get("integration_fixes", 0),
                    "seam": prog["modules"][module].get("seam_fixes", 0),
                    "global_used": prog["global_fix_used"]},
        "tokens": {"module_input": metrics["module_tokens"][module],
                   "module_ceiling": 3 * prog["modules"][module]["loc_budget"] * 15,
                   "build_input_total": metrics["total_input"]},
        "last_failure_set": failure_set,
        "last_classifications": prog["modules"][module].get("classification_log", []),
        "held_dependents": [m for m, s in prog["modules"].items()
                            if module in s.get("depends_on", []) and s["status"] != "INTEGRATED"],
        "resume_hint": "fix root cause, delete brake_reason, re-run orchestrator to resume this module only"
    }
    (build_dir / f"DEBUG_STATE_{module}.json").write_text(json.dumps(debug, indent=2))
    # Other lanes CONTINUE: one poisoned module must not spend thirteen modules'
    # budget. The failed module surfaces in DELIVERY_REPORT.md at human gate 2.
```

Malformed-output guard (LEAF_GEN specific): a response violating the output protocol
(unparseable fences, wrong paths, truncation) counts as one lane failure; exactly one
retry with the parse error prepended; second violation escalates the batch to a
GATE_FIX session. `MALFORMED_OUTPUT_REPEAT` trips the brake on the third occurrence
per module.

---

# SECTION 5: PERSISTENT COMPANION MEMORY & LESSON LEDGER

## 5.1 The `AGENTS.md` ledger — absolute structural standard

Two coupled files, one logical ledger:

- **`LESSONS.jsonl`** — machine substrate. Append-only, one JSON object per line,
  never edited, never compacted, never summarized by a model. Correction = new record
  with `supersedes`; retraction = new record with `retracts`. Appends go through the
  same OS lockfile discipline as Section 2.3.2 (multi-lane safe: append is the only
  write operation; there is no read-modify-write).
- **`AGENTS.md`** — human view, regenerated deterministically from `LESSONS.jsonl`
  after every append (never hand-edited; the JSONL is truth).

`AGENTS.md` normative layout:

```markdown
# AGENTS.md — Companion Lesson Ledger (generated; edit LESSONS.jsonl workflow only)
Build: build_20260705_101530_112233 | Records: 47 active, 6 superseded, 2 retracted

## Index by tag
| tag | active lessons |
|---|---|
| sqlalchemy | L-0142, L-0161 |
| orders     | L-0159, L-0161 |
| protocol   | L-0163 |
| deps       | L-0140 |
| frontend   | L-0155, L-0158 |

## Lessons
### L-0161 · orders · MODULE_INTEGRATE · 2026-07-05T14:22:31
**Symptom:** DetachedInstanceError accessing relationship after session close in
service-layer return.
**Fix:** Return schema objects (model_validate) from services, never live ORM
instances; routers must not touch lazy relationships.
**Tags:** sqlalchemy, session, backend · **Paths:** code/backend/app/**/service.py
**Evidence:** fixed app/orders/tests/test_service.py::test_confirm_order at
lane/orders@4e2a91c · **Hits:** 3 · **TTL:** 8 builds
<!-- lesson:{"id":"L-0161","status":"active"} -->
```

`LESSONS.jsonl` record — normative schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "echara://schemas/lesson.v1.json",
  "title": "MicroLesson",
  "type": "object",
  "required": ["id", "ts", "build_id", "module", "phase", "tags", "path_glob",
               "symptom", "fix", "evidence", "hits", "supersedes", "retracts", "ttl_builds"],
  "additionalProperties": false,
  "properties": {
    "id":       { "type": "string", "pattern": "^L-\\d{4,}$" },
    "ts":       { "type": "string", "format": "date-time" },
    "build_id": { "type": "string" },
    "module":   { "type": "string" },
    "phase":    { "type": "string",
                  "enum": ["BATCH_GEN", "WAVE_GATE", "MODULE_INTEGRATE",
                           "SEAM_CHECK", "VERIFY", "PROTOCOL"] },
    "tags":     { "type": "array", "items": { "type": "string" },
                  "minItems": 1, "maxItems": 5 },
    "path_glob": { "type": "string" },
    "symptom":  { "type": "string", "maxLength": 240 },
    "fix":      { "type": "string", "maxLength": 240 },
    "evidence": {
      "type": "object",
      "required": ["fingerprint", "failing_before", "passing_after", "fix_commit"],
      "additionalProperties": false,
      "properties": {
        "fingerprint":    { "type": "string" },
        "failing_before": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
        "passing_after":  { "type": "boolean", "const": true },
        "fix_commit":     { "type": "string" }
      }
    },
    "hits":       { "type": "integer", "minimum": 0 },
    "supersedes": { "type": ["string", "null"] },
    "retracts":   { "type": ["string", "null"] },
    "ttl_builds": { "type": "integer", "minimum": 1, "maximum": 20 }
  }
}
```

Hard rules: `symptom` + `fix` together ≤ 60 tokens (injected verbatim); `tags` from
the controlled vocabulary (framework names, module names, `protocol`, `deps`,
`frontend`, `backend`); `evidence.passing_after` MUST be `true` — a lesson is written
by tier-1 code only after the re-gate passes. A worker may propose one `LESSON:` line
in its final output; tier 1 validates it against schema and evidence before appending.
No passing re-gate, no record: the ledger stores facts, not opinions.

## 5.2 Dynamic Prompt Compiler — complete algorithm

Runs immediately before every worker invocation; deterministic; no embeddings, no
model calls; total added cost ≤ `LES_MAX` = 600 volatile tokens.

```python
# ============================================================================
# DYNAMIC PROMPT COMPILER — lesson selection & injection
# ============================================================================
import fnmatch, json, re

CONTROLLED_FRAMEWORK_TAGS = {
    "fastapi", "sqlalchemy", "pydantic", "alembic", "pytest", "bcrypt",
    "react", "vite", "typescript", "vitest", "axios"
}
MAX_LESSONS, MAX_TOKENS = 12, 600

def compile_lessons(work_order: dict, dag: dict, ledger_path: str,
                    current_build_id: str, error_context: str = "") -> list[str]:
    # ---- 1. load active records (drop superseded / retracted / expired) -----
    records, superseded, retracted = [], set(), set()
    for line in open(ledger_path, encoding="utf-8"):
        r = json.loads(line)
        if "hit" in r:                                   # hit-count event record
            continue
        if r["supersedes"]: superseded.add(r["supersedes"])
        if r["retracts"]:   retracted.add(r["retracts"])
        records.append(r)
    active = [r for r in records
              if r["id"] not in superseded and r["id"] not in retracted
              and build_age(r["build_id"], current_build_id) <= r["ttl_builds"]]

    # ---- 2. derive the query signal from THIS invocation ---------------------
    batch_paths = [f["path"] for f in work_order["batch_files"]]
    signal_tags = {work_order["module"], work_order["kind"]}
    for path in batch_paths:                             # AST/import-derived tags
        node = dag["nodes"][path]
        for imp in node["strict_import_dependencies"]:
            root = imp.split(".")[0].split("/")[0].lower()
            if root in CONTROLLED_FRAMEWORK_TAGS:
                signal_tags.add(root)
        for token in re.split(r"[/_.]", path.lower()):
            if token in CONTROLLED_FRAMEWORK_TAGS:
                signal_tags.add(token)
    if error_context:                                    # fix-class invocations
        for tag in CONTROLLED_FRAMEWORK_TAGS:
            if tag in error_context.lower():
                signal_tags.add(tag)

    # ---- 3. deterministic scoring --------------------------------------------
    def score(r: dict) -> tuple:
        s  = 3 * len(set(r["tags"]) & signal_tags)
        s += 2 if any(fnmatch.fnmatch(p, r["path_glob"]) for p in batch_paths) else 0
        s += 1 if r["build_id"] == current_build_id else 0
        s += min(r["hits"], 3)
        return (s, r["ts"])                              # ties -> newest wins

    ranked = sorted((r for r in active if score(r)[0] > 0), key=score, reverse=True)

    # ---- 4. token-capped selection -------------------------------------------
    selected, spent = [], 0
    for r in ranked[: MAX_LESSONS * 2]:
        line = f"- {r['id']} [{','.join(r['tags'][:2])}] {r['symptom']} -> {r['fix']}"
        cost = token_estimate(line)
        if len(selected) >= MAX_LESSONS or spent + cost > MAX_TOKENS:
            break
        selected.append((r["id"], line))
        spent += cost

    # ---- 5. hit accounting (append-only; feeds promotion, Section 5.3) -------
    with oslock(ledger_path + ".lock"), open(ledger_path, "a", encoding="utf-8") as fh:
        for lesson_id, _ in selected:
            fh.write(json.dumps({"hit": lesson_id, "ts": now_iso()}) + "\n")

    return [line for _, line in selected]   # rendered into the WORK_ORDER "LESSONS" block
```

## 5.3 Promotion pipeline (how lessons become law without prompt growth)

```
after each build, at DELIVER:
  candidates = active global lessons with hits >= 3 across >= 2 distinct builds
  DELIVERY_REPORT.md gains a "PROMOTION CANDIDATES" table:
      id | symptom | fix | builds affected | evidence links
  human gate 2 approves a candidate ->
      preference order (strongest first):
        1. a deterministic repair in agents/repairs.py     (code beats prompt)
        2. a rule line in NN_RULES / CONVENTIONS.md        (law beats memory)
      then append {"retracts": "<id>"} to GLOBAL_LESSONS.jsonl
      (it is law now; injecting it too would double-spend tokens)
```

The cached system prefix is frozen at human gate 1 and can only change **between**
builds through this pipeline. Nothing enters permanent context without first living
in the volatile section, proving recurrence with evidence, and passing a human gate —
which is the mechanical guarantee behind Section 1's claim that `C_PREFIX` is a
constant, and therefore behind the entire flat-curve token model.

---

# APPENDIX: CROSS-SECTION INVARIANT TABLE

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Per-invocation input ≤ 4,775 effective tokens, independent of S | §1.3 allocator, REJECT_BEFORE_DISPATCH |
| 2 | Every file has exactly one owner; lock grants cannot intersect | §2.1 step 7, §2.3.3 step 1 |
| 3 | A merge conflict is a partitioner bug, never a resolvable event | §2.3.1 merge-tree preflight |
| 4 | Workers never see peer implementation — signatures only | §3.1 extractor, LEAF_GEN has no filesystem |
| 5 | Every cross-module import is licensed by SEAMS.json before code exists | §2.1 step 7b |
| 6 | Fault is assigned by the immutable contract, not by execution order | §4.2 phases 4b/4c |
| 7 | Every fix route decrements a persisted budget before dispatch | §4.3 rule 5, write-through |
| 8 | No identical failure state is ever re-dispatched at the same tier | §4.3 rules 1–3 |
| 9 | A lesson without passing-after evidence cannot enter the ledger | §5.1 hard rules |
| 10 | Permanent prompt content changes only between builds, human-gated | §5.3 |
