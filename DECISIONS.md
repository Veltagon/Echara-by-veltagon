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

## Guide-vs-spec audit (post-signoff review)

The pasted spec (`MILESTONE_2_5_3_SPEC.md`) was tighter than `guide.md`'s M3.
After building to the spec, I audited against the guide and found three gaps.
Items 1 and 3 are now closed; item 2 is explicitly deferred.

### ✅ Item 1: `references/` gate for small-context models
guide.md: *"if model context window < 16K tokens, intercept read_file calls to
references/ and return 'reference not available, use core SKILL.md instructions
only'"*. Implemented in `providers/tool_harness.py` (`SMALL_CONTEXT_FLOOR`,
`_touches_references`, `REFERENCES_REFUSAL`). `ApiAdapter` carries an optional
`context_window`; router reads it from `provider_config.yaml`. When the window
is known and < 16000, any `read_file` whose path has a `references` segment
returns the refusal string without dispatch. 4 tests
(`test_touches_references_helper`, `..._intercepts_small_context`,
`..._allowed_large_context`, `..._gate_off_when_window_unknown`).

### ✅ Item 3: cooldowns for API providers in the router
guide.md's LiteLLM bullet says *"rate limit handling, cooldowns, budget
tracking"*. `ProviderRouter.call_with_fallback` now consults
`providers.availability`: providers on cooldown are skipped without a call, and
a rate-limit-shaped failure (`_looks_rate_limited`, matches HTTP 429 /
`RateLimitError` / "rate limit" text) marks the provider exhausted for 60s
(`_DEFAULT_COOLDOWN_SEC`). Non-rate-limit errors (ConnectionError, etc.) do
NOT trigger cooldown — that's specifically the 429/quota lane. 4 tests
(signatures, skip-on-cooldown, mark-on-rate-limit incl. next-call skips it,
non-rate-limit-no-cooldown). The `providers/availability` registry (built in
M2 for codex) is now used by both the CLI path and the M3 API router.

### ⏸️ Item 2: cross-provider live comparison — DEFERRED (funded keys needed)
guide.md's M3 acceptance test: *"run builder agent with backend-development
skill on provider A, then provider B, compare output quality"*. Cannot be run
honestly today: `.env` liveness (probed 2026-07-01) — Cerebras gemma-4-31b LIVE
(harness confirmed E2E), z.ai insufficient balance, Vercel needs credit card,
Hugging Face credits depleted, airforce Cloudflare-blocked. Anthropic /
OpenAI / OpenRouter keys are placeholders in `provider_config.yaml`.
**Ready-to-run command once any second provider is funded:**

```bash
python run_harness_agent.py --provider cerebras_gemma \
  --skills-dir skills-pool/engineering-team/skills \
  --task "Read the senior-backend skill and build a minimal FastAPI note-CRUD."
# then, with a funded second provider added to HARNESS_PROVIDERS or a new
# adapter that routes via ApiAdapter, re-run against it and diff the outputs.
```

The infrastructure is ready — every layer that a live cross-provider run needs
is wired and tested with mocks. All that's missing is the second funded key.

## Blockers

None during the build. The one real defect mid-build (circular import) was
root-caused and fixed. Item 2 is not a blocker to M3 sign-off — it's an
external dependency (funded API keys) that blocks the *acceptance test*.
