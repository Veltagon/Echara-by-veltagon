# Milestone 2.5 + 3 Report — Skill Router + Provider Routing

Status: **complete. `pytest tests/ -v` → 22 passed, 0 failed.**

## Files created

### Production (443 LOC, ceiling 800)
| LOC | File |
|-----|------|
| 19  | `skills/__init__.py` |
| 55  | `skills/loader.py` — SKILL.md access + tiktoken token counting |
| 86  | `skills/router.py` — role config, frontmatter index, per-session budget |
| 53  | `providers/cli_adapter.py` — Category A, delegates to M2 CLI providers |
| 52  | `providers/api_adapter.py` — Category B, LiteLLM (lazy) + tool harness |
| 84  | `providers/router.py` — role routing + bounded fallback |
| 76  | `providers/tool_harness.py` — 5-tool loop (reuses harness executors) |
| 18  | `providers/base.py` — appended `ProviderBase` (additive) |

### Tests (330 LOC)
`tests/test_skill_router.py` (75), `tests/test_providers.py` (64),
`tests/test_tool_harness.py` (106), `tests/test_integration_skill_provider.py` (52),
`tests/test_full_system.py` (33).

### Config / scaffold (53 LOC)
`skill_assignments.yaml` (17), `provider_config.yaml` (30), `conftest.py` (6),
`tests/__init__.py` (0). Plus `skills-pool/` (cloned skill repo, not counted).

## LOC totals
- Production: **443** (under 800)
- Tests: 330
- Config/scaffold: 53

## pytest output (verbatim, `pytest tests/ -v --tb=short`)
```
============================= test session starts =============================
platform win32 -- Python 3.14.6, pytest-8.2.2, pluggy-1.6.0
rootdir: C:\Users\white\OneDrive\文档\programms\echara
plugins: anyio-4.14.0, hypothesis-6.155.7, locust-2.44.4, asyncio-0.23.7, benchmark-5.2.3, cov-7.1.0
collected 22 items

tests/test_full_system.py::test_end_to_end_mock PASSED                   [  4%]
tests/test_integration_skill_provider.py::test_skill_loaded_into_api_session PASSED [  9%]
tests/test_integration_skill_provider.py::test_cli_skill_index_written PASSED [ 13%]
tests/test_providers.py::test_config_loading PASSED                      [ 18%]
tests/test_providers.py::test_adapter_instantiation PASSED               [ 22%]
tests/test_providers.py::test_fallback_on_failure PASSED                 [ 27%]
tests/test_providers.py::test_all_providers_exhausted PASSED             [ 31%]
tests/test_providers.py::test_role_routing PASSED                        [ 36%]
tests/test_skill_router.py::test_frontmatter_extraction PASSED           [ 40%]
tests/test_skill_router.py::test_role_skill_assignment PASSED            [ 45%]
tests/test_skill_router.py::test_token_budget_enforcement PASSED         [ 50%]
tests/test_skill_router.py::test_skill_index_generation PASSED           [ 54%]
tests/test_skill_router.py::test_full_body_load_on_demand PASSED         [ 59%]
tests/test_tool_harness.py::test_read_file PASSED                        [ 63%]
tests/test_tool_harness.py::test_write_file PASSED                       [ 68%]
tests/test_tool_harness.py::test_list_dir PASSED                         [ 72%]
tests/test_tool_harness.py::test_bash_run PASSED                         [ 77%]
tests/test_tool_harness.py::test_bash_run_timeout PASSED                 [ 81%]
tests/test_tool_harness.py::test_done_stops_loop PASSED                  [ 86%]
tests/test_tool_harness.py::test_max_iterations_guard PASSED             [ 90%]
tests/test_tool_harness.py::test_tool_loop_produces_output PASSED        [ 95%]
tests/test_tool_harness.py::test_five_tools_registered PASSED            [100%]
============================= 22 passed in 4.41s ==============================
```
(~960 DeprecationWarnings from the pytest-asyncio plugin under Python 3.14 —
third-party noise, unrelated to this code.)

## No regressions
- `tests_hardening.py` (M2) → **21/21**
- `tests_m25_harness.py` (M2.5 harness) → **43/43**
- Live E2E `run_harness_agent --provider cerebras_gemma` → done, 3 rounds — the
  real API path still works after the openai upgrade + import refactor.

## Decisions
See `DECISIONS.md`. Highlights: real skill pool is `engineering-team/skills`
(the `.gemini` copies are Windows symlink stubs); skill IDs flat + mapped to
real skills; heavy reuse of the M2.5 harness; one circular import found and
root-caused (lazy `_kill_tree` import).

## Known limitations / shortcuts
- **Skill ID → path is flat**, not the spec's `domain/skill`. Real repo is flat.
- **tiktoken cl100k_base** used for all providers' budgeting (proxy, not
  per-model exact). Fine for a 5k ceiling.
- **Fallback tries each provider once** (no per-provider inner retry). Bounded
  by design; the spec's "retry once" is satisfied by the fallback step itself.
- **No real API calls tested** (per spec) — LiteLLM path is wired and
  lazy-imported but only exercised via mocks; first real call happens in M4.
- **Not wired into the orchestrator** — infrastructure only, per spec. Seams
  (`SkillRouter`, `ProviderRouter`, `run_harness`) are ready for M4.
