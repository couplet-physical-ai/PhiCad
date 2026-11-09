---
id: 0007-linux-stock-footprint-paths
title: Resolve stock footprint libraries on Linux without changing macOS precedence
status: done
priority: 70
tier: standard
scope:
  - phicad/synth/**
  - phicad/bin/phicad
  - phicad/testing.md
  - phicad/build-linux.md
  - phicad/build-attestations/linux-synth-library-paths.json
  - phicad/loop/backlog/0007-linux-stock-footprint-paths.md
gates:
  - python3 phicad/synth/test_library_paths.py
requires:
  - $PHICAD_BUILD/pcbnew/_pcbnew.kiface
  - $PHICAD_BUILD/eeschema/_eeschema.kiface
timeout_min: 45
max_attempts: 2
branch: fix/synth-stock-footprint-roots
source: phicad/build-attestations/linux-synth-sensor-node.json; phicad/synth/library.py STOCK_DIR
---

## Definition of done

Repair the known Linux synth failure recorded by item 0001: `Library.default()` only
searches the macOS stock footprint root and project libraries. Keep project-local libraries
first, retain the existing macOS stock location, and add documented, deterministic Linux
stock-library discovery and an explicit environment override. Inspect the existing symbol
lookup conventions before choosing override names. Missing roots must give actionable errors,
not silently resolve a similarly named footprint from an unrelated location.

Add `phicad/synth/test_library_paths.py` using temporary library fixtures and mocked platform/
environment values, with Linux and macOS cases, precedence, explicit override, absent roots,
and paths containing spaces. Wire the narrow stdlib test into `phicad check fast`. Do not
require installed libraries or the host platform for the path-regression tests.

Run the sensor-node synth command with the host's existing fork CLI and installed libraries
if available. Write a NEW `phicad/build-attestations/linux-synth-library-paths.json` with exact
command, source revision, binary version, library roots, observed stage verdicts and failures.
Preserve the original failed attestation. A missing package or a later unrelated pipeline
failure must be reported honestly; do not claim DRC/ERC passed unless the actual gates did.
Do not install system packages, rebuild/reconfigure the shared C++ tree, change the example's
circuit, or fabricate passing evidence. Update affected synth/Linux/testing documentation.

Use only the owned worktree and temporary outputs. The shared PHICAD_BUILD and PHICAD_KICAD_CLI
are read-only. No hardware, credentials, gateway limits, or public publishing operations.
Commit coherent verified checkpoints; the controller will review the branch and handle PRs.
