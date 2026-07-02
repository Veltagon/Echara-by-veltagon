# DECISIONS — Milestone 2.5 + 3 (skill router + provider routing)

Autonomous build (user unavailable). Every non-obvious call is recorded here.

## Skill pool & IDs

- **Cloned** `alirezarezvani/claude-skills` → `skills-pool/` (per spec).
- **Real content lives at `skills-pool/engineering-team/skills/<id>/SKILL.md`.**
  The flat `.gemini/skills/*/SKILL.md` entries are git-symlinks that Windows
  checked out as *text stubs* (file body = the link target path), so they are
  unusable as skill content. Pool root is therefore the `engineering-team`
  path (33 real, frontmatter-backed skills). `skills/router.py::DEFAULT_POOL_ROOT`.
- **Skill IDs are flat names**, not the spec's `engineering/backend-development`
  form — those paths don't exist in the real repo. Mapped role→skill to real
  skills (`skill_assignments.yaml`):
  - planner: senior-architect, senior-backend
  - builder: senior-backend, senior-data-engineer, senior-devops
  - verifier: senior-security, code-reviewer

## Reuse over rebuild (ponytail)

- `skills/loader.py` reuses `harness.skills._parse_frontmatter` — one parser.
- `providers/tool_harness.py` reuses `harness.tools` executors + `harness.registry`
  schemas; it only adds the 5-tool subset wiring + the bounded loop.
- `providers/cli_adapter.py` delegates to the hardened M2 `PROVIDERS`
  (ClaudeCodeProvider/CodexProvider) — no duplicate subprocess handling.
- Token counting uses `tiktoken` cl100k_base as a cross-model proxy (budgeting
  doesn't need per-model exactness).

## Existing-code changes (spec: must document)

1. **`providers/base.py`** — appended `ProviderBase` (abstract role adapter).
   Purely additive; the M2 `Provider` class is untouched. Kept here because the
   spec names `providers/base.py` as its home.
2. **`providers/__init__.py`** — added `ProviderBase` to the base import and
   appended `CliAdapter/ApiAdapter/ProviderRouter/AllProvidersExhausted` exports
   *after* `PROVIDERS` is defined (ordering matters — see #3). Additive.
3. **`harness/tools.py`** — moved `from providers.base import _kill_tree` from
   module top to a lazy import inside `_exec()`. **Why:** the new routing
   adapters make `providers/__init__` import `harness.tools`; the top-level
   `harness.tools → providers` edge then formed a circular import
   (`harness.tools → providers pkg init → api_adapter → harness.tools`). Lazy
   import keeps `harness.tools` standalone and breaks the cycle. Behaviour
   identical; verified by the M2.5 suite (43/43) and a live E2E.

## Interpretations

- **Test path:** spec says `echara/tests/`; the project root *is* `echara/`, so
  tests live in `tests/` and run as `pytest tests/ -v`. `conftest.py` puts the
  repo root on `sys.path`.
- **Dependencies:** `pip install litellm tiktoken` (pyyaml/pytest already
  present). This upgraded `openai` 1.39→2.30 as a transitive dep; re-ran the M2
  (21/21) and M2.5 (43/43) suites — no regression.
- **litellm is lazy-imported** in `api_adapter.py` (only for real calls). All
  tests inject a mock `complete_fn`, so the suite passes without any network and
  even if litellm were absent.
- **Fallback semantics:** each provider in `fallback_order` is tried once; any
  exception is logged and routing moves to the next; all-fail raises
  `AllProvidersExhausted` carrying `{provider: error}`. Bounded — never loops.
- **Unknown provider type:** a provider with `type` set to anything other than
  `cli` (and no API `model`) raises `ValueError` in `make_adapter`.

## Not done (out of scope, per spec "infrastructure only")

- No wiring of routing/skills into the M1 orchestrator phases (no agents/phases
  added). The seam exists (`run_harness_agent.run_harness`, `ProviderRouter`,
  `SkillRouter`) for M4 to consume.
- No real API calls anywhere in tests.

## Blockers

None. No test needed >3 attempts. The one real defect found mid-build (the
circular import above) was root-caused and fixed, not worked around.
