# Milestone 2.5 + 3 Spec — Skill Router + Provider Routing

## System 1: Skill Router (~200-300 LOC)

Skill pool: `alirezarezvani/claude-skills` (clone to `./skills-pool` if not present).

### Skill assignment config (`skill_assignments.yaml`):
```yaml
planner:
  - engineering/software-architect
  - engineering/backend-development
builder:
  - engineering/backend-development
  - engineering/database-engineer
  - engineering/devops
verifier:
  - security/security-auditor
  - engineering/code-reviewer
```

### Skill loading (match Claude Code's mechanism):
1. On session start: scan assigned skill dirs, extract ONLY YAML frontmatter (name + description) from each SKILL.md. Inject index into system prompt (~100 tokens/skill). Full body NOT loaded.
2. Model calls `read_file("./skills-pool/.../SKILL.md")` to load full body on demand.
3. If body references other files → model calls `read_file` again. Progressive disclosure.
4. If body references scripts → model calls `bash_run("python ./skills/.../script.py")`. Script code never enters context, only output.

### Hard constraint: 
No agent's total skill content (frontmatter + loaded bodies) exceeds 5000 tokens. Router tracks count, refuses loads past budget: "Skill budget exceeded. Use loaded skills only."

### Files:
- `echara/skills/router.py` — config loader, frontmatter index builder, token budget tracker
- `echara/skills/loader.py` — reads SKILL.md, extracts frontmatter, returns full body on demand
- `skill_assignments.yaml`

---

## System 2: Provider Routing (~300-400 LOC)

### Five providers, two categories:

**Category A — CLI (subprocess):**
- `claude_code` → `claude` CLI
- `codex` → `codex` CLI

**Category B — API (via LiteLLM):**
- `chatgpt` → OpenAI API
- `anthropic` → Anthropic API  
- `openrouter` → OpenRouter API

### Provider config (`provider_config.yaml`):
```yaml
providers:
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-4-20250514"
  chatgpt:
    api_key: "${OPENAI_API_KEY}"
    model: "gpt-4o-mini"
  openrouter:
    api_key: "${OPENROUTER_API_KEY}"
    model: "anthropic/claude-sonnet-4-20250514"
  claude_code:
    type: cli
    command: "claude"
  codex:
    type: cli
    command: "codex"

role_assignment:
  planner: "anthropic"
  builder: "claude_code"
  verifier: "chatgpt"

fallback_order: [anthropic, chatgpt, openrouter]
```

### Tool-calling harness (Category B API models only):
CLI tools have built-in file access. API models need the harness:
```
Tools: read_file, write_file, list_dir, bash_run, done
Loop: send tools → model calls tool → execute → return result → repeat until 'done' or max_iterations(30)
```
Use OpenAI-compatible function-calling spec. LiteLLM normalizes across providers.

### Fallback: 
On failure (429/500/502/503/timeout>120s/connection error): log, try next in fallback_order, retry once. All fail → raise `AllProvidersExhausted`. Never retry indefinitely.

### Skill-provider integration:
- CLI tools: write `.echara/skills_index.md` into project dir. CLI model reads natively.
- API models: frontmatter index in system prompt. Full bodies via `read_file` tool. Router restricts `read_file` to assigned skill dirs + project dir.

### Files:
- `echara/providers/base.py` — ProviderBase abstract class
- `echara/providers/cli_adapter.py` — subprocess wrapper
- `echara/providers/api_adapter.py` — LiteLLM wrapper
- `echara/providers/router.py` — config reader, adapter instantiation
- `echara/providers/tool_harness.py` — tool loop for API models
- `provider_config.yaml`

---

## Dependencies
```bash
pip install litellm pyyaml tiktoken
```

---

## Tests (ALL REQUIRED — do not skip)

### `echara/tests/test_skill_router.py`:
- `test_frontmatter_extraction` — real skill from pool, extracts name+desc, under 200 tokens
- `test_role_skill_assignment` — config loads correctly, no role gets >4 skills
- `test_token_budget_enforcement` — load until >5000 tokens, assert refusal
- `test_skill_index_generation` — builder index has frontmatter only (no body), under 500 tokens
- `test_full_body_load_on_demand` — load full SKILL.md, assert budget tracker updated

### `echara/tests/test_providers.py`:
- `test_config_loading` — 5 providers defined, 3 roles mapped, fallback has 2+ entries
- `test_adapter_instantiation` — CLI→CliAdapter, API→ApiAdapter, unknown→ValueError
- `test_fallback_on_failure` — mock primary fail, assert fallback used, failure logged
- `test_all_providers_exhausted` — all fail → AllProvidersExhausted with details
- `test_role_routing` — planner→anthropic adapter, builder→claude_code adapter

### `echara/tests/test_tool_harness.py`:
- `test_read_file` — temp file, read it, assert content matches
- `test_write_file` — write to path, assert file exists with content
- `test_list_dir` — temp dir with 3 files, assert all listed
- `test_bash_run` — echo hello, assert "hello" in result
- `test_bash_run_timeout` — sleep 30 with timeout=2, assert timeout error
- `test_done_stops_loop` — model calls done first turn, loop exits after 1
- `test_max_iterations_guard` — model never calls done, exits at max, warning logged
- `test_tool_loop_produces_output` — write_file→bash_run→done sequence, assert file+output

### `echara/tests/test_integration_skill_provider.py`:
- `test_skill_loaded_into_api_session` — mock LLM echoes system prompt, assert frontmatter present, body absent
- `test_cli_skill_index_written` — .echara/skills_index.md exists with correct frontmatter

### `echara/tests/test_full_system.py`:
- `test_end_to_end_mock` — all roles get correct provider + skill index under 500 tokens + tools registered

---

## Execution order
1. Install deps
2. Build System 1 → run test_skill_router.py → all pass
3. Build System 2 → run test_providers.py + test_tool_harness.py → all pass
4. Build integration + full system tests → all pass
5. `pytest echara/tests/ -v --tb=short` → report final count

---

## Hard constraints
- Production code (excl tests/config) under 800 LOC. Over 800 = overbuilding.
- Do not modify Milestone 1/2 code without documenting why in DECISIONS.md.
- Do not add agents, phases, or orchestration logic.
- Mock all LLM responses in tests. No real API calls.
- Do not stop until all tests pass. Stuck >3 attempts on one test → document in DECISIONS.md, move on, return later.

## Output
When done, write `MILESTONE_2_5_AND_3_REPORT.md`: files created, LOC count, full pytest -v output, decisions made, known limitations.
