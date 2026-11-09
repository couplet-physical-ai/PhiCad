# Skills — the chat-side entry points

A phi *skill* is instructions loaded into a chat session when the task matches its
description. These are the entry points to PhiCad from outside the application: a terminal
`phi`, or any host with the phicad MCP server registered.

| Skill | For |
| --- | --- |
| `phicad-design-board` | "design me a board" → a placed, routed, DRC-clean project via `phicad/synth` |

Deliberately not installed, same as `phicad/agents/`. To activate one:

```bash
mkdir -p ~/.pi/agent/skills
cp -R phicad/skills/phicad-design-board ~/.pi/agent/skills/
```

A skill and an agent do different jobs and both exist here on purpose. The skill loads into
the session the user is already talking to, so the conversation stays with them; the agent
(`phicad/agents/synth-designer.md`) runs the same work as a subagent when an orchestrator
wants it off the main thread. They share the pipeline, so neither can drift from the other.
