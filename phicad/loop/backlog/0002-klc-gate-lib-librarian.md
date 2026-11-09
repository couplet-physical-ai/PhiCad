---
id: 0002-klc-gate-lib-librarian
title: Run the official KLC checkers on every generated symbol/footprint before lib-librarian may call a part ready
status: done
priority: 10
tier: standard
scope:
  - phicad/bin/phicad
  - phicad/parts/**
  - phicad/agents/lib-librarian.md
  - phicad/testing.md
  - phicad/docs-site/docs/**
timeout_min: 45
max_attempts: 2
source: the OSS integration landscape review (unpublished proposal) ("Top recommendations", row 7)
branch: loop/0002-klc-gate-lib-librarian-20260922162402
---

## Definition of done

A stdlib-only helper `phicad/parts/klc_check.py` that runs the KiCad Library Convention
checkers from `kicad-library-tools` (`klc-check/check_symbol.py`, `klc-check/check_footprint.py`)
as **subprocesses** against a symbol library / footprint directory and returns a structured
verdict (`{"tool": ..., "checked": n, "violations": [{"file", "rule", "severity", "message"}],
"ok": bool}`), plus:

1. A CLI (`python3 phicad/parts/klc_check.py <path.kicad_sym|footprint.pretty> [--json]`) that
   exits 0 when clean, 1 when violations are found, 2 when the checkers are not installed —
   and in that case prints the one command that installs them into `build/venv`
   (`pip install` from the GitLab repo, or `git clone` under `build/`), never vendoring them.
2. The `lib-librarian` agent's "Verification gate" section (`phicad/agents/lib-librarian.md`)
   requires a clean `klc_check.py` run before a part is declared ready, with the command spelled
   out.
3. A test `phicad/parts/test_klc_check.py` that needs neither the checkers nor a network:
   feed the parser a captured sample of checker output (put the fixture text in the test file)
   and assert the structured verdict; assert the exit-code contract (0/1/2) with a fake
   subprocess runner. Wire it into `phicad/bin/phicad check fast` next to the other parts/synth
   token tests and add the row to `phicad/testing.md` §3a.
4. `phicad/parts/README.md` documents the gate and the licence position: kicad-library-tools is
   GPL-3.0 and is called as a subprocess only, never imported or copied into the tree.

## Facts

- `phicad/bin/phicad` is in scope only for the one `run_gate` line that wires the new test into `check fast`.

- The checkers live at https://gitlab.com/kicad/libraries/kicad-library-tools (`klc-check/`),
  Python, own parsers, no SWIG. Their output format is one line per violation with the rule id
  (e.g. `S4.1`, `F5.2`); read the scripts to confirm before writing the parser.
- Network on this host must strip the LLM-gateway proxy variables before any `git clone` or
  `pip`; if the network is unavailable, still land the helper, the test with fixtures, and the
  docs — exit code 2 covers "not installed".
- `phicad/parts/fetch_part.py` is the producer of the assets this gate checks; do not change
  its interface in this item.
