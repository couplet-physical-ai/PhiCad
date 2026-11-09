# AI interfacing

PhiCad exposes several ways for agents to inspect, edit, validate, and export electronics designs without driving the GUI.

- The fork-native IPC API provides transactional live board and schematic operations through kipy.
- `kicad-cli` handles deterministic checks, diffs, renders, exports, and jobsets.
- MCP servers provide chat-facing tools while delegating to the supported automation paths.
- Agent View keeps each board project in Git with automatic stable-save snapshots.
- S-expression libraries can help with file-level work, but require version-aware round-trip checks.
- The fork's 10.99-format boundary means stable-KiCad authoring tools must not rewrite fork files.

Source of truth: [phicad/ai-interfacing.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/ai-interfacing.md)
