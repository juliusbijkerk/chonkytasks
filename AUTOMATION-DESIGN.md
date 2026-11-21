# Automation creation — design notes

Captured from the chonkytasks/agent design discussion (post-ranking step).

## Next step after `rank`: compile + run the top loop

Drive via the JSON API (documented in `/llms.txt`, served by `ui/server.py`):

```
GET  /api/report.md                  # pick top opportunity by measured minutes
POST /api/compile  {"id": "opp_01"}  # blocking, up to 300s -> out/skills/opp_01/skill.json
GET  /api/skill?id=opp_01            # review the action list before trusting it
POST /api/run      {"id": "opp_01", "params": {...}, "headed": false}
GET  /api/events?since=0             # poll the run feed
```

CLI equivalent: `./pipeline/compile.py opp_01` then
`./pipeline/run.py opp_01 --dry-run` (read it), `--headed` (watch it).

Safety rule: actions marked `"confirm": true` (save/send/pay/delete) stop for a
human. The UI runs with `--yes`; agents must ask first.

## Two creation approaches

| Approach | Code | Model emits |
|---|---|---|
| Skill compiler | `pipeline/compile.py` | `skill.json` — data-only action list with semantic locators; fixed Playwright interpreter (`pipeline/run.py`) executes it. Model never writes code. |
| Mistral Agent with tools (Agents & Workflows track) | `pipeline/rank_agent.py` (SDK: `agents.Agent(model, name, instructions, tools=[...])` + `@agents.tool` + `await agent.run(...)`); `AdoptChonk (Mehdi)/backend/agent.py` (hosted `POST /v1/agents` with function tool specs, or ReAct loop via `tools=..., tool_choice="auto"`) | Tool calls; terminal `finish` tool receives the final structured result. |

Workflow version: wrap compile/skill/run as tools on one `agents.Agent`, same
pattern as `rank_agent.py`.

## Ideal agent `instructions` (technical checklist)

1. Role + goal in one line.
2. Numbered workflow: which tool first, what to do with results, when to stop.
3. Division of labor as a rule: model decides what/why, tools do all numbers
   ("never state a duration or count yourself — the tool is the only source").
4. Terminal contract: `finish(...)` docstring = exact JSON schema of the output.
5. Safety + boundaries: drafts only / confirm on writes, no credentials, start
   after login, don't invent steps.

Length rule: orchestration agents get a short prompt (~6 lines, cf.
`AGENT_INSTRUCTIONS` in AutoPilot); schema-emitting agents need the full schema
inline (cf. `SYSTEM` in `pipeline/compile.py`).
