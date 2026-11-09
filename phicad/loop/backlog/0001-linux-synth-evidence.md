---
id: 0001-linux-synth-evidence
title: Prove the synth pipeline end-to-end on the Linux build host and record the evidence
status: done
priority: 20
tier: standard
scope:
  - phicad/build-linux.md
  - phicad/synth/README.md
  - phicad/loop/**
  - phicad/build-attestations/**
gates:
  - test -s phicad/build-attestations/linux-synth-sensor-node.json
requires:
  - $PHICAD_BUILD/pcbnew/_pcbnew.kiface
  - $PHICAD_BUILD/eeschema/_eeschema.kiface
timeout_min: 45
max_attempts: 2
source: phicad/build-linux.md; phicad/synth/README.md ("Verified end-to-end on examples/sensor-node.json")
branch: loop/0001-linux-synth-evidence-20260922170058
---

## Definition of done

`phicad/bin/phicad synth build phicad/synth/examples/sensor-node.json -o /tmp/loop-synth-out`
runs to completion **on this Linux host with the fork's own `kicad-cli`** (the one in
`PHICAD_KICAD_CLI`), and the result is recorded in the repo:

1. A machine-readable attestation `phicad/build-attestations/linux-synth-sensor-node.json`
   with: host (`uname -srm`), `kicad-cli version` output, the commit of the checkout, the exact
   command, wall-clock seconds, and every gate verdict the synth pipeline printed (DRC
   violations/unconnected counts, ERC, parity), copied from the tool output — not typed by hand.
   Follow the shape of the existing files in `phicad/build-attestations/` if any exist; otherwise
   keep it flat JSON with those keys.
2. `phicad/build-linux.md` gains a short "Verified on Linux" section that points at the
   attestation and states what did and did not work (e.g. Freerouting present or absent —
   `~/.kicad-mcp/freerouting.jar` — and whether autorouting ran).
3. If the pipeline fails on Linux, do **not** paper over it: record the exact failing step and
   its stderr in the attestation with `"status": "failed"`, and still land the documentation.
   That is a successful iteration — the loop's job is evidence.

## Facts

- The macOS run of this example is documented in `phicad/synth/README.md` ("6 parts, 4 nets,
  2-layer, poured"); compare numbers against it and note any difference.
- `kicad-cli` needs `KICAD_RUN_FROM_BUILD_DIR=1` when run from a build tree; `phicad run` and
  the MCP server set it already. The build tree is shared with another agent: read it, never
  run `ninja` in it.
- Freerouting is Java (`default-jre-headless` is installed by the bootstrap); if the jar is
  absent the pipeline should say so — record that rather than downloading anything.
- Known Linux-only defect (2026-09-22, fix on `fix/cli-exit-default-project`, PR #121, routed via an
  upstream MR because the file is upstream KiCad): `kicad-cli sch erc` on a schematic with **no
  sibling `.kicad_pro`** writes its report correctly and then segfaults at exit (status 139). The
  binary at `PHICAD_KICAD_CLI` on build-host-01 was relinked with the fix at 16:20Z and exits 0/5;
  a plain `dev` rebuild would reintroduce it. If you still see exit 139 from that command, treat
  it as "report written, crashed on exit", say so in the attestation, and do not debug it; `pcb
  drc`, the exports, and `sch erc` with a sidecar `.kicad_pro` exit normally either way.
- Do not modify the synth pipeline code in this item; if you find a Linux-only defect, describe
  it precisely in the attestation and in `build-linux.md` so a follow-up item can fix it.
