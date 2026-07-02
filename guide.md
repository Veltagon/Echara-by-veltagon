This is file contains the steps to achive an clean, working state of echara v2
follow this guide step by without skipping anything

Mile-stone 1
bulding the phases and the agents
only building code that spaws fake agents
functions that write hardcoded files to disk. No Claude, no Codex, no API keys. Just:

orchestrator.py  →  walks INTAKE → PLAN → BUILD → VERIFY → DELIVER
state.py         →  saves/loads PROJECT_STATE.json

Each phase calls a stub function that writes a placeholder file (PLAN.md, backend/app/main.py, etc.) and advances. VERIFY checks if main.py exists and contains from fastapi import FastAPI. DELIVER stamps a verdict.

End goal of mile-stone 1 - echara can run, write hard coded thing, go thru phases, verify, deliver and could be stoppend at mi-build and can resume gracefully

mile-stone 2

running one, configured agent succesdfully
builder agent with - alirezarezvani/claude-skills (19k+ stars) — 345 production-ready skills across 17 domains, 579 Python tools, 705 reference guides, 93 agents, 99 slash commands. Organized by domain: engineering, marketing, product, security, compliance, C-level advisory, research, business operations. Ships with specialized agent personas that include identity, mission, critical rules, capabilities, workflows, communication style, and success metrics. Works across Claude Code, Codex, Gemini CLI, Cursor, and 9 more tools.

the agent get the "alirezarezvani/claude-skills" skill and we would test it one provider and then move on to other provider 

goal - the agent can produce reliable code, not drift away from the goal, producing what is instructed, especially verify its own work

mile-stone 2.5

we are gonna use the open source "opencode":
It's basically a terminal user interface where you drop in your API key. It supports a lot of providers, and then it suddenly gives the API key's model access to your whole computer and access to whatever things you can think of for an API provider model.

If we take Claude Code Terminal User Interface as an example that works as a wrapper, giving the agent the ability to do tasks on the user's computer and the models run on their private server. Claude Code isn't Opus 4.8 or Opus 4.7 or any of the models. It's just a wrapper that acts as a bridge between the user's computer and the model's server, so that's what OpenCode does.

Now, what we're going to do is we have a lot of API providers, and every one of them should be explicitly given files that are asked for. We are not going to spend time building it, but what clone open codes repository and take parts of it take its core job of giving the pure API model to do stuff all the stuff that it has so we're gonna take that we're gonna write that in then proceed with the below things 


How skills work in ECHARA (same mechanism as Claude Code):
Harness scans `./skills/` directory
- Extracts ONLY YAML frontmatter (name + description) from each SKILL.md
- Injects skill index into system prompt (~100 tokens per skill)
- Full skill body is NOT loaded yet

Model reads skill index, decides which skill matches the task
- Model calls `read_file("./skills/backend-development/SKILL.md")` to load full body
- If body references other files → model calls `read_file` again
- If body references scripts → model calls `bash_run("python ./skills/.../script.py")`
- Script code never enters context, only output does

- Harness preprocesses `!`command`` patterns in SKILL.md before returning to model
- Replaces with command output (e.g., `!`git diff HEAD`` → actual diff)

- Skills are loaded via tool calls, not system prompts
- Any model with read_file + bash_run tools can use skills identically
- SKILL.md format is provider-agnostic (open standard)
- Claude Code-specific frontmatter fields (context: fork, allowed-tools) 
  are handled by harness where needed, ignored otherwise

mile-stone 3
wring prover routing thru air llm 
applying same skill which is originally made for claude code to all the providers
if we try to put an entire skill like: alirezarezvani/claude-skill to other any other provider it would be an 10k system prompt

so we need an way to apply these "claude-only skills" to the other providers
explame -> codex has this: ./scripts/convert.sh --target codex

To route these detailed folders to providers like Cerebras, AirLLM, OpenRouter, or Vercel AI Gateway, you must bridge them using a dynamic context provider. Providers like OpenRouter and Vercel AI Gateway only supply raw LLM APIs—they do not handle local files. Your agent backend must do that work.
Instead of a generic system prompt, use a dynamic runtime file pattern (like the standard CLAUDE.md pattern used by advanced coding agents):

LLMs accessed via api key don't have the ablity to read, edit, bash, ternimal powershell or do any what an wrpper like 'claude code to its model' does
so insted of doing it our selfs we are gonna use the 'opencode' opensource repo and take the main coponents and wrie it in our code so we can give pure api models pervallage to everty that is supposed to be done

For Cerebras: Because Cerebras runs at high speeds, its context limits require attention. Do not feed it the entire 300+ skill repo at once. Use the Python filter logic above to inject only the specific skill folder matching the user's active task.For AirLLM (Local Models): Smaller open-source models (like Llama-3-8B) will experience context overload if you pass complex, nested folder instructions. If a user selects AirLLM, strip out the references/ subdirectory text and pass only the core SKILL.md file.For Vercel AI Gateway / OpenRouter: These gateways function smoothly. They accept massive multi-file system strings and route them seamlessly directly to the endpoint targets without any text truncation.

we need to engineer a way where 'how an skill is give to claude code' should be give the same way to other providers
    when an skill is added to claude code
        it doesn't recive an very long system prompt insted it recives: 📂 claude-skills/
                                                                    ├── 📄 SKILL.md          <-- The core prompt, instructions, and trigger definitions
                                                                    ├── 📂 scripts/           <-- Python tools that Claude runs automatically via CLI
                                                                    └── 📂 references/        <-- Blueprints, strict checklists, and documentation
    we need to do the same way to other provider 
    to give an claude code skill successfully to other provder we should never sqweez it in an system prompt 
    instead give it an compatable way ehre it can proccess the skill, use it 
    it should be give in folder with sub folder in an way where it can use it

    every skill, system prompt, prompt total sshould't exxced the 5-6k limit## Milestone 3

Tool-calling agent harness + provider routing

### Agent harness (~250 LOC)
Build the core tool loop that gives any API model full filesystem access:
- Tools: read_file, write_file, list_dir, bash_run, done
- Loop: send tools to model → model calls tool → execute → return result → repeat until model calls done
- Uses OpenAI-compatible function-calling spec (works with every provider)
- Skill loader integrated: frontmatter index injected at session start, 
  model reads full skills via read_file tool calls

### Provider routing via LiteLLM
- Single config file defines all providers with priority order
- Built-in fallback chains, rate limit handling, cooldowns, budget tracking
- pip install litellm, write YAML config, done

### Per-provider context limits
- Claude Code / Codex CLI (local subprocess): full skill folder, model reads directly
- API providers via harness: same mechanism — model reads skill files through 
  read_file tool, progressive disclosure keeps context lean
- Small context models (Cerebras, local LLMs): model reads only core SKILL.md, 
  skips references/ subdirectory. Enforced by harness rule: if model context 
  window < 16K tokens, intercept read_file calls to references/ and return 
  "reference not available, use core SKILL.md instructions only"

### Goal
- One agent can run on any provider with the same skill producing equivalent results
- Test: run builder agent with backend-development skill on provider A, 
  then provider B, compare output quality


mile-stone 4

now making the muilt agent system
first it was one agent now 4-5 agents..
not talking but running turn by turn
every agent has its own task to do

builder -> handels A-Z given by Architect
verifiy agent checks the code produced by builder
planner agents takes the normal plan produced by the architect and makes it more detailed, includes step by step instruction to how to achive it?
    produce a detailed technical implementation plan — file-by-file breakdown, dependency order, which files need to exist before others, which endpoints depend on which models. Not vision statements. Not market analysis. Delete those four items and replace with: "detailed file manifest, dependency graph, implementation order, contract spec (JSON endpoints + models)."

every agent will have an defaullt .md which it will read every time it recives an prompt
like how claude has 'CLAUDE.md' something like for every agent, and every agent will have one skill in common: alirezarezvani/claude-skill
Architect
     will have the common skill
     a system prompt about the architect role
                               what should it do
                               how should it do
                               what should it not do
                               rules
                               examples
    it will have: database-designer skill

verifier:
    only write test files
    verifies indivsual components
    verifies the code produced by builder
    but testing indivusial components might be good but when ran together they might break so verifier should also identify it
    it will have: skill-security-auditor

builder:
    it will reive an 500word .md with detailed things like: it should not drift from  the goal
                                                            simplicity fromcomplex things
                                                            should't put everything in one/two single files
                                                            will only do what is required will not implemnt extra things
                                                            it will recive: engineering/backend-development

mile-stone 5

verify everything works and echara could produce reliabley, good qualtiy 4k-6k loc successfuly 
with out muiltiple itterations, token exhaust
with a decent amout of time

probelms we could face:
    cil/provider hang
    agent not producing good code
    bad plan
    plan verification 

things that i missed
    we need to procced slowly with small steps about:
    phases, routing, agent hang, handoff

we need 3 clean code produced echara to declare that it is working
there are a lot of things that need to be doe in the project, fucntions, phases so to help you with some fuctions has helped in the pervious versions are in: Help.md
so make use of it 

Wrapper phase:

where everything would be wrapped in an tui like claude but not exactly that
we are gonna go over that onece all the mile stines are successfully achived

    