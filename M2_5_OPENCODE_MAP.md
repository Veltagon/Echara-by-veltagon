# M2.5 — opencode → ECHARA harness (DONE)

Raw API models (Cerebras, etc.) expose only a chat-completions endpoint — no
filesystem, no shell, no tool loop. opencode solves this by owning the loop and
handing the model tools. We ported that core (not the Effect/Bun/TUI layers) to
a ~450-LOC Python harness. CLI agents (claude/codex) keep their own loop via the
M2 `providers/` path; this is the parallel path for keyed APIs.

## What each piece became

| opencode component | ECHARA file | Notes |
|---|---|---|
| `tool/external-directory.ts` `assertExternalDirectory` | `harness/safety.py::clamp_path` | realpath subpath check; the one trust boundary |
| `tool/{read,write,edit}.ts` | `harness/tools.py` | read_file (line window), write_file, edit_file (unique-string replace) |
| `tool/{glob,grep}.ts` | `harness/tools.py` | stdlib `glob` + `re`/`fnmatch`, no ripgrep dep |
| `tool/shell.ts` (timeout/tree-kill) | `harness/tools.py::bash_run`, `::powershell_run` | shared `_exec` helper; reuses M2's `providers.base._kill_tree`. PowerShell = registry/COM/native-module reach cmd/sh lack |
| `tool/webfetch.ts` | `harness/tools.py::webfetch` | urllib + regex HTML→text, browser UA |
| (opencode has none) | `harness/tools.py::web_search` | DuckDuckGo HTML scrape, zero-dep, no key — discover URLs, not just fetch known ones |
| `skill/discovery.ts` + `skill/index.ts` | `harness/skills.py` | frontmatter index via `yaml.safe_load`; `stage()` copies skill folders into the workspace so `references/` (read_file) and `scripts/` (bash_run) are reachable — the full drill-down, not just the top SKILL.md |
| `tool/skill.ts` | `harness/tools.py::load_skill` | loads full SKILL.md body on demand (convenience alongside read_file) |
| `tool/registry.ts` | `harness/registry.py` | name → (OpenAI schema, fn) in one table |
| `session/system.ts` | `harness/prompt.py` | environment + skill index + persona |
| `session/prompt.ts::runLoop` + `processor.ts` | `harness/loop.py` | non-stream `chat.completions` loop, 25-round cap |
| `provider/provider.ts` BUNDLED_PROVIDERS | `providers/openai_compat.py` | one class, one config row per upstream |
| `session/llm.ts streamText` | — | dropped; replaced by the stdlib openai SDK |

## Tool surface (12)

read_file, write_file, edit_file, list_dir, glob, grep, bash_run,
**powershell_run**, **web_search**, **webfetch**, load_skill, done.

## Access posture (deliberate, not an oversight)

M2 runs claude/codex with `--dangerously-skip-permissions` /
`--dangerously-bypass-approvals-and-sandbox` — unrestricted. The harness drives
weakly-aligned *open* models and this repo's `.env` holds live keys, so the
default is tighter, but escapable:

- File tools (read/write/edit/list/glob/grep) clamp to the workspace by default.
- `bash_run` / `powershell_run` are **never** clamped — real shells can `cd`
  anywhere. The clamp was only ever a guard on the *structured* file tools.
- `run_harness_agent.py --full-access` (Context `allow_outside_workspace=True`)
  drops the file-tool clamp → M2-equivalent posture. One flag.

## Deviations (ponytail)

- **Reused, didn't rebuild.** Shell tools share one `_exec` + M2's `_kill_tree`;
  frontmatter uses installed `yaml`; key loading uses installed `dotenv`; web
  tools use stdlib `urllib`. No new deps.
- **Two stop conditions.** Loop ends when the model emits no tool_calls *or*
  calls `done` — small models often just stop instead of calling done.
- **`reasoning or content`.** Some models put text in `.reasoning` with
  `content=None`; the loop reads whichever is set.
- **Browser User-Agent on web tools.** Default Python-urllib UA gets 403'd
  (Cloudflare 1010) — the same trap that masked Cerebras as dead.
- **Skipped `` !`command` `` substitution.** The guide lists preprocessing
  `` !`git diff` `` → command output in SKILL.md, but zero skills in the repo use
  it and opencode's loader doesn't do it either. YAGNI — add a substitution pass
  in `load_skill`/`stage` if a skill ever needs it.

## Provider reality (probed 2026-07-01, not trusted from the wind-down doc)

Every `.env` key was probed live. Cloudflare 403s the default `Python-urllib`
User-Agent (`error 1010`) — **the harness must use the openai SDK, not urllib.**

| provider | status |
|---|---|
| **Cerebras** | **LIVE.** Models: `gemma-4-31b`, `gpt-oss-120b`, `zai-glm-4.7`. Only `gemma-4-31b` emits real `tool_calls`; the other two route text to `.reasoning` with `content=None`. `.env`'s `qwen-3-235b...` is gone. |
| airforce | 403 (Cloudflare 1010) |
| z.ai | 429 — insufficient balance |
| Vercel AI Gateway | 403 — requires a credit card on file |
| Hugging Face | 402 — monthly credits depleted |

Registered `cerebras_gemma` in `providers/__init__.py::HARNESS_PROVIDERS`. Other
upstreams are one config row away once a key is funded.

## Resilience & input hardening (review round)

- **API errors never crash the run.** SDK client set with `max_retries=4` +
  `timeout=120` (owns backoff, honors Retry-After); `loop.py` wraps
  `complete()` in try/except → terminal failure ends with `stop_reason="error"`
  and a report, not a stack trace.
- **Untrusted tool args validated.** `read_file` offset/limit go through
  `_get_int` (bad/out-of-range → `_err`, not an exception); `write_file`
  tolerates `content: null` and reports real UTF-8 byte count; `glob` capped at
  200 like `grep`; `powershell_run` adds `-ExecutionPolicy Bypass` for `.ps1`.
- **Deferred (ponytail):** multi-key rotation from the comma-separated `.env`
  lists — single-key by design here; failover is M3 provider-routing's job.

## Review round 2

- **`bash_run` no longer a lie.** Uses real `bash` when on PATH (POSIX works),
  else the platform shell; `<environment>` in the system prompt states the
  *actual* active shell and warns when it's cmd.exe. `active_bash_shell()`.
- **`.env` hygiene.** Added `.gitignore` (`.env` excluded — no accidental
  secret push) + `.env.example`; removed the dead `qwen` model line and
  documented that gemma-4-31b is pinned in code.
- **Harness seam for the orchestrator.** Extracted `run_harness_agent.run_harness()`
  — a caller passes a provider object + task and gets a report; also writes
  `SYSTEM_PROMPT.md` forensics. The M1 orchestrator's BUILD phase can call it
  once **M3** defines provider routing — deliberately NOT wired into
  `phases.phase_build` now (that's M3/M4 scope; wiring it early would bake in
  routing decisions M3 owns).
- **Integration test.** `test_integration_full_assembly` runs the WHOLE harness
  via `run_harness()` with a scripted provider and asserts cross-component
  wiring — a staged skill reference is readable *through the loop*, not just by a
  direct tool call. Components passing ≠ assembly passing; this proves the latter.

## Verification

- `python tests_m25_harness.py` → **43/43** (clamp escapes, full-access toggle,
  read_file/write_file/glob edge cases, API-error-ends-cleanly, registry shape,
  full loop with a scripted fake provider, round cap). Web-tool parsers tested
  offline; powershell_run / web_search / webfetch smoke-tested live (free).
- `python tests_hardening.py` → **21/21** (M2 path untouched).
- **Live E2E (gemma-4-31b), all clean:**
  - tool loop: wrote `hello.py`, ran it (`exit 0`, `hello echara`), `done` — 3 rounds.
  - new tools: model picked `powershell_run` → `Windows_NT`.
  - skill index → `load_skill("senior-backend")` → named `scripts/api_scaffolder.py`.
  - full drill-down: `list_dir` → `read_file references/api_design_patterns.md`
    → quoted `## 1. REST vs GraphQL Decision`. Proves staged references are reachable.

## Files

```
harness/{__init__,safety,tools,skills,registry,prompt,loop}.py
providers/openai_compat.py          providers/__init__.py (HARNESS_PROVIDERS, additive)
run_harness_agent.py                tests_m25_harness.py
```
