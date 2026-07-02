# What Makes Building a Code Orchestrator Actually Hard

And what ensures 4k LOC output with minimal iterations at high quality.

---

## Part 1: The Hard Problems (things that will bite you)

### 1. Provider Wiring — 4-6 providers, exponential failure combinations

You're wiring Claude CLI, Codex CLI, Cerebras API, Anthropic API, OpenAI API, OpenRouter. Each has:
- Different auth (CLI subscription vs API key vs free tier)
- Different rate limit shapes (per-minute, per-hour, 5-hour rolling, 7-day rolling)
- Different failure modes (HTTP 429, HTTP 401, subprocess timeout, silent hang, partial output)
- Different output formats (stream-json, plain text, JSON tool calls)
- Different capability levels (Opus can architect, Cerebras can't write code)

The COMBINATIONS are what kill you:
- Claude CLI rate-limited + Codex CLI working → route to codex
- Claude CLI rate-limited + Codex CLI rate-limited + Cerebras working → route to cerebras, BUT cerebras can't write code (only reviews) → round fails → retry on... what? Both capable lanes are dead.
- Provider A returns a 429 with `retry-after: 60s` → wait and retry? Or route to B? What if B is also 429?
- Provider A returns partial output (the LLM started writing but got cut off mid-function) → is this a success or failure? Can you resume?
- Provider A hangs (subprocess never returns, no error, no output) → how long do you wait? 1 min? 5 min? The V1 answer was 50 min for heavy agents. That's too long.

**What you actually need to build:**
- A provider registry with 3 states: `available`, `exhausted(resets_at)`, `failed(reason)`
- A `pick_provider(agent_role)` function that walks the agent's preference list, skips exhausted/failed, returns the first available
- A `mark_exhausted(provider, resets_at_unix)` call that fires when ANY round gets a rate-limit response with a timestamp
- A fallback chain: primary fails → secondary → tertiary → `no_capable_provider` error (fail fast, don't retry endlessly)
- A per-provider output parser (Claude stream-json ≠ Codex plain text ≠ Cerebras JSON tool protocol)

### 2. CLI Session Spawning — subprocesses are NOT agents

This is the #1 thing people underestimate. You're not calling an API and getting a response. You're spawning a **subprocess** that:
- Takes 5-30 seconds just to start
- Runs for 1-15 minutes
- Writes files to disk AS it runs (not after)
- Can hang indefinitely if the provider stops responding
- Can OOM if it reads a large file
- Can be killed by the OS (Windows sleep, OOM killer, antivirus)
- Produces output in a streaming format that you have to parse incrementally
- Uses its OWN tool-calling system (Claude Code's built-in tools) that you don't control

**What you actually need to build:**
- `subprocess.Popen` with stdout piped to a FILE (not memory — a 15-min session produces hundreds of MB of stream-json; `proc.communicate()` OOMs)
- A per-round watchdog thread that kills the subprocess after N seconds of no output
- An idle monitor that checks if the subprocess has produced any new output in the last 60s (catches hangs that don't crash)
- Output parsing that handles: complete output, truncated output (turn cap hit), rate-limit mid-stream (Claude Pro 5-hour bucket), subprocess crash (non-zero exit, no output), and timeout (killed by watchdog)
- A prompt file written to disk BEFORE the subprocess starts (forensics — you need to know what the agent saw when it produced bad code)
- An output file written after (or during, for streaming) the subprocess runs
- Cleanup: kill the subprocess tree on timeout, not just the parent pid (Windows subprocess trees don't die cleanly with `proc.kill()`)

### 3. Agent State Across Rounds — they forget everything

Each round is a fresh CLI session. The agent doesn't remember what it did in round 1 when it starts round 2. The context window is brand new.

**What this means practically:**
- Round 1: Backend_Dev writes `main.py`, `models/note.py`, `routers/notes.py`. All correct.
- Round 2 (REFINE fix): Backend_Dev gets a prompt saying "fix the import error in models/note.py". It reads the file, sees its own code, but has NO MEMORY of why it wrote it that way. It might "fix" it by rewriting the whole file differently.
- Round 3: Backend_Dev gets another fix. It reads the file again. Sees the Round 2 version. Makes a third version that conflicts with Round 1's `main.py` (which still imports the Round 1 shape).

**What you actually need to build:**
- Previous round output injection (truncated to ~3K chars) so the agent knows what it did last round
- Wave summary injection so the agent knows what OTHER agents did
- File-on-disk as the source of truth (the agent can READ its own prior output from the filesystem — teach it to do this in the prompt)
- Objection/feedback injection: if QUALITY_GATE found a problem, inject the EXACT error message into the agent's next prompt, not a generic "fix the errors"

### 4. Phase Transitions — when to advance, when to loop, when to abort

The state machine seems simple (INTAKE → PLAN → BUILD → VERIFY → DELIVER) but the edge cases are where builds die:

- VERIFY fails → go back to BUILD? Or REFINE? How many times? V1 looped up to 6 times (streak cap). That's 60-90 min of grinding.
- BUILD produces an app that imports but has wrong API shapes → is this VERIFY-fail (code doesn't work) or PLAN-fail (contract was wrong)? V1 always blamed BUILD. Sometimes it was the contract.
- VERIFY passes but the code is ugly (no error handling, no logging, `# TODO` comments) → does VERIFY catch this? In V1, no — VERIFY only checks "does it run," not "is it good."
- BUILD times out (provider exhaustion) before all agents finish → is this a VERIFY fail? A BUILD fail? A provider fail? V1 conflated all three into `force_terminal:plan_quota`.

**What you actually need to build:**
- Clear phase-exit criteria (not "did enough happen" but "did THIS SPECIFIC thing succeed"):
  - PLAN exits when `CONTRACT_FROZEN.json` exists and has ≥1 endpoint
  - BUILD exits when `import_smoke=passed` (the app is importable)
  - VERIFY exits when `all gates passed AND runtime_smoke=ok`
  - REFINE exits when verify passes OR streak cap hit
- A `force_terminal` path that's honest about WHY it fired (provider death ≠ code quality ≠ wallclock exceeded) and doesn't conflate them into one score
- State persistence after EVERY phase transition (not just at the end) so a crash at any point can resume

### 5. Output Quality — the gap between "runs" and "good"

Getting the code to IMPORT is step 1. Getting it to be GOOD is where 80% of the effort goes:

**Things agents consistently get wrong (from 35 builds of evidence):**
- Write `from alembic import command` in `main.py` (shadows the pip package)
- Use `Mapped[Optional[str]]` on Python 3.14 (crashes SQLAlchemy's Union type handler)
- Write two migrations that both create the same table
- Write `response.json()["detail"]["loc"]` when FastAPI returns `detail` as a list, not dict
- Use `@pytest.fixture` on `async def` (produces async_generator that pytest can't unwrap)
- Use `subprocess.run(check=True)` without `capture_output=True` (loses diagnostic stderr)
- Include `cd backend && pytest` in recipes (doubles to `backend/backend/` in the app convention)
- Leave `debug_notes.py`, `test_db_setup.py` scratch files at the backend root
- Write a health endpoint that returns `{"status": "ok"}` without checking the database
- Write a global exception handler with `# Log the exception here` but never actually log

**What you actually need to build:**
- Deterministic repairs for the KNOWN defect classes (the 6 proven repairs from V1)
- NN-rules in the agent spec that cite specific examples of each mistake
- A rubric that checks FUNCTION not presence (runtime_smoke is the floor; a functional rubric that verifies actual endpoint behavior is the ceiling)

### 6. Concurrency — parallel agents writing to the same filesystem

When 2-3 agents run in parallel (ThreadPoolExecutor), they write files to the same `code_dir/`. Problems:

- Agent A writes `models/note.py` with `id: UUID`. Agent B writes the same file with `id: int`. Agent B finishes last → `id: int` wins. No merge, no conflict detection, no error.
- Agent A writes `__init__.py` that imports `from .notes import router`. Agent B hasn't written `notes.py` yet (still mid-round). If ANYTHING reads `__init__.py` between A finishing and B finishing → ImportError.
- Agent A runs `ruff --fix` which reformats files Agent B is currently writing → race condition on file content.

**What you actually need to build:**
- Non-overlapping `can_write` scopes (NO two agents write to the same glob)
- Scope enforcer in `enforce` mode: post-wave diff → revert any writes outside scope
- Sequential waves for agents with overlapping concerns (Backend_Dev in wave 1, then QA_Lead in wave 2 — not both in wave 1)
- File-level checksums before/after each wave to detect silent overwrites

### 7. Token Economics — you're burning money and need to know where

A single build can burn $5-50 in API costs (or equivalent subscription time). Without tracking:
- You don't know which agent burned the most
- You don't know which phase burned the most
- You don't know if a free-tier provider round was counted at full cost
- You can't set a budget cap that means anything

**What you actually need to build:**
- Per-round token counter (input + output tokens from the provider response)
- Per-provider weight (cerebras=0.05, codex on subscription=0.05, paid API=1.0)
- Per-phase budget caps that trigger "budget low" before the budget is exhausted (so quality agents still get to run)
- A quality-tier reservation (reserve 25% for QA/review agents so BUILD agents can't eat the whole budget)

---

## Part 2: The Minimal System That Produces 4k LOC at High Quality

### Your simplified pipeline

```
INTAKE → PLAN → BUILD → VERIFY/REFINE → DELIVER
```

3-4 agents: Architect, Backend_Dev, Frontend_Dev, (optional DevOps_Engineer)

### The 12 things that ensure quality output with minimal iterations

#### Before any agent runs:

**1. Structured contract emission (not free-form)**

The #1 cause of wasted REFINE iterations is agents building the WRONG thing. Not buggy code — code that implements a different API shape than what the plan describes.

Use StructuredOutput (or equivalent) at the PLAN phase exit to emit a machine-readable contract:
```json
{
  "endpoints": [
    {"method": "POST", "path": "/api/notes", "request_model": "NoteCreate", "response_model": "NoteOut", "status": 201},
    {"method": "GET", "path": "/api/notes", "request_model": null, "response_model": "list[NoteOut]", "query_params": ["limit", "offset", "q"]}
  ],
  "models": [
    {"name": "Note", "fields": [{"name": "id", "type": "int", "primary_key": true}, {"name": "title", "type": "str", "max_length": 200}]}
  ]
}
```

Every agent gets this JSON injected into its prompt. Every agent builds TO this contract. If the produced code doesn't match the contract, the preflight gate catches it BEFORE REFINE — not after 3 rounds of agents arguing about API shapes.

**Impact**: eliminates the 12-17 contract mismatches per build that V1 burned 20-30% of REFINE tokens fixing.

**2. Non-overlapping file ownership**

Before BUILD, assign every file path to exactly one agent:
```
Backend_Dev:  backend/app/**, backend/tests/**
Frontend_Dev: frontend/src/**, frontend/package.json
Architect:    PLAN.md, ARCHITECTURE.md, DECISIONS.md (read-only during BUILD)
DevOps_Eng:   Dockerfile, docker-compose.yml, .env.example
```

NO overlaps. Enforce with post-wave revert. If Backend_Dev touches `frontend/src/App.tsx`, that write is reverted.

**Impact**: eliminates the silent last-writer-wins file collision that produced inconsistent code in V1.

**3. Repair stack runs BEFORE verification (not after)**

```python
def _phase_deliver(self):
    repair_all(code_dir)      # line 1: fix known defect classes
    smoke = run_smoke(code_dir)  # line 2: verify the repaired code
    verdict = stamp(smoke)     # line 3: stamp the honest result
```

Not the other way around. V1's 14-line ordering mistake cost 2 entire builds.

**Impact**: eliminates the "repair would have fixed it but never got to fire" failure mode.

#### During BUILD:

**4. Evidence-backed NN-rules in the prompt (not generic instructions)**

Don't say "write good code." Say:

```yaml
- id: "NN-5: NEVER import alembic.command in app code"
  rule: "from alembic import command in main.py shadows the pip package..."
  why: "Build 5+8 died from this. Two builds wasted."
  example_wrong: "from alembic import command"
  example_right: "# Use subprocess for migrations, never import alembic in app"
```

15-20 of these per agent, each one backed by a specific historical failure.

**Impact**: prevents the agent from repeating the exact mistakes that killed prior builds. V1's Backend_Dev stopped writing `from alembic import command` after NN-BE-5 was added.

**5. Contract injection into every agent's prompt**

The agent must SEE the contract (endpoints, schemas, field types) in its prompt, not just in a file it might-or-might-not read.

Two paths (belt and suspenders):
- Full contract in the prompt's knowledge section (6K char cap)
- Per-role slice (only the endpoints/models this agent owns, 2.5K cap)

**Impact**: without this, agents hallucinate API shapes from the plan text. With it, V1's contract mismatches dropped from 12-17 per build to near-zero on builds where injection was confirmed active.

**6. Import gate at BUILD exit**

Before advancing from BUILD to VERIFY:
```python
result = subprocess.run([python, "-c", "from app.main import app"], cwd=backend_dir)
if result.returncode != 0:
    # don't advance. dispatch the owning agent with the exact error.
    return False
```

If the app doesn't import, nothing downstream works. Catch it HERE, not in VERIFY (where it becomes one of 10 failures and the agent doesn't know which to fix first).

**Impact**: V1's Builds 5-8 reached DELIVER with unimportable apps. After IMPORT_GATE (Build 9+), no build ever shipped unimportable code.

**7. Per-round hard timeout + idle detection**

```python
# Don't let an agent round run forever
future = executor.submit(agent.run_round, prompt)
try:
    result = future.result(timeout=400)  # 6.7 minutes max
except TimeoutError:
    kill_subprocess_tree(agent.pid)
    result = RoundResult(success=False, error="timeout")
```

Plus idle detection: if the subprocess has produced zero new bytes in 60 seconds, it's hung. Kill it. Don't wait for the full timeout.

**Impact**: V1's Build 9 had Backend_Dev hang for 25 minutes on a dead provider. The watchdog would have caught it at 60 seconds.

#### During VERIFY/REFINE:

**8. Three-gate verification floor**

```python
gates = [
    py_compile_all(code_dir),           # can it parse?
    import_smoke(code_dir, venv),       # can it import?
    runtime_smoke(code_dir, project_dir) # does import + alembic + pytest pass?
]
verified = all(g.passed for g in gates)
```

All three must pass. Any failure is a `verified_path=False`. No grading curve. No "8.31 organic but gated." Either it works or it doesn't.

**Impact**: eliminates the "PASS on code that doesn't import" failure class that V1's reality audit exposed.

**9. Route REFINE to file owner, not gate owner**

When pytest fails on `test_migrations.py:42`:
- The GATE that failed = pytest (owned by QA_Lead)
- The FILE that's broken = `alembic/versions/9e5496ed3e44.py` (owned by Backend_Dev or Database_Engineer)

Route to the FILE owner. QA_Lead can't fix alembic migrations.

```python
def route_refine(failure):
    broken_file = extract_file_from_traceback(failure.detail)
    owning_agent = scope_registry.owner_of(broken_file)
    return owning_agent  # not the gate owner
```

**Impact**: eliminates the 60+ min of wasted QA_Lead rounds that V1 burned routing by gate ownership.

**10. Streak cap with early exit (not grind)**

If 3 consecutive REFINE rounds produce zero net improvement (same failures, same files), stop. Don't grind to 6. The agent is stuck and more rounds won't unstick it.

```python
if streak >= 3:
    # run repair_all one final time
    repair_all(code_dir)
    # run verification one final time
    smoke = run_smoke(code_dir)
    # stamp whatever we have. don't loop.
    return finalize(smoke)
```

Check the streak BEFORE dispatching the next round, not after (V1 checked after, which meant one extra wasted round every time).

**Impact**: saves 30-60 min of grinding per build. V1's B16 ground through 13 REFINE iterations.

#### At DELIVER:

**11. One score, one verdict, no contradiction**

```python
verdict = {
    "verified_path": smoke.status == "ok" and all_gates_passed,
    "score": functional_rubric_score,    # one number
    "rescue_reason": rescue_reason or None,  # separate field, not baked into score
}
```

V1 showed `f500_organic=8.28` AND `final_score=3.99` on the same build. Both correct, both contradictory. One number.

**Impact**: every operator/reviewer understands the verdict without mental inversion.

**12. Post-build invariant assertions**

After every build, before the next one:

```python
def assert_invariants():
    # no env var set without a reader
    for var in launcher_envvars():
        assert var in source_readers(), f"{var} set but never read"

    # no two repairs target the same file
    assert no_overlapping_repair_ownership()

    # no two agents can_write the same glob
    assert no_overlapping_agent_scopes()

    # repair_all is called BEFORE verification in _phase_deliver
    assert source_line_of("repair_all") < source_line_of("run_smoke") in deliver_phase

    # every repair that fired 0 times in last 5 builds is flagged
    for repair in repair_registry:
        if repair.fire_count_last_5_builds == 0:
            log.warn(f"{repair.name} hasn't fired in 5 builds — consider opt-out")
```

**Impact**: catches the class of bugs that took V1 months to find (theater env vars, fighting repairs, wrong ordering, dead repairs).

---

## Summary: the minimum viable quality stack

```
┌─────────────────────────────────────────────────────┐
│  BEFORE agents run                                  │
│  [1] Structured contract (JSON, not markdown)       │
│  [2] Non-overlapping file ownership (enforced)      │
│  [3] Repair BEFORE verify (ordering)                │
├─────────────────────────────────────────────────────┤
│  DURING BUILD                                       │
│  [4] NN-rules with build-failure evidence           │
│  [5] Contract injected into every prompt            │
│  [6] Import gate at BUILD exit                      │
│  [7] Per-round timeout + idle detection             │
├─────────────────────────────────────────────────────┤
│  DURING VERIFY/REFINE                               │
│  [8] Three-gate floor (compile + import + smoke)    │
│  [9] Route REFINE to file owner, not gate owner     │
│  [10] Streak cap at 3 with early exit               │
├─────────────────────────────────────────────────────┤
│  AT DELIVER                                         │
│  [11] One score, no contradiction                   │
│  [12] Post-build invariant assertions               │
└─────────────────────────────────────────────────────┘
```

Miss any ONE of these 12 and you'll hit the exact failure mode V1 hit. All 12 together is what turns a 31:1 waste ratio into a system that ships 4k+ LOC of verified code in 2-3 iterations.
