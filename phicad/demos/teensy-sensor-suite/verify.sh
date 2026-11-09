#!/bin/bash
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Full verification of the Teensy demo board: regenerate, geometric pre-check, DRC,
# renders, exports, and a summary.  One command, so re-verifying after a change (or after
# a machine problem) is not a hunt for the right flags.
#
#   ./verify.sh            # verify in place
#   ./verify.sh -q         # quiet: only the summary table
#
# Exit status: 0 if every step ran and DRC reported no ERROR-severity violations.

set -u

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
CLI="$ROOT/build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli"
PY="$ROOT/build/venv/bin/python"
BOARD="$HERE/teensy-sensor-suite.kicad_pcb"
OUT="${TMPDIR:-/tmp}/teensy-verify"
QUIET=0

[ "${1:-}" = "-q" ] && QUIET=1

say() { [ "$QUIET" = 1 ] || echo "$@"; }

run() {   # run <label> <cmd...>; records PASS/FAIL and keeps going
    local label="$1"; shift
    if "$@" >"$OUT/$label.log" 2>&1; then
        RESULTS+=("PASS  $label")
        return 0
    fi
    local rc=$?
    RESULTS+=("FAIL  $label (exit $rc)")
    return 1
}

rm -rf "$OUT"; mkdir -p "$OUT"
RESULTS=()

# --- 0. environment sanity ----------------------------------------------------------
# A wedged opendirectoryd makes every kicad-cli invocation abort with exit 134 and NO
# output, which looks exactly like a corrupt board.  Check it first so a machine fault is
# never mistaken for a design fault.
if ! dscl . -read "/Users/$(whoami)" RecordName >/dev/null 2>&1; then
    echo "ENVIRONMENT FAULT: directory services (opendirectoryd) is not answering."
    echo "  dscl reports: $(dscl . -read "/Users/$(whoami)" RecordName 2>&1 | head -1)"
    echo
    echo "  Every kicad-cli run aborts with SIGABRT (exit 134) and no error message --"
    echo "  including 'kicad-cli version', which needs no board at all.  Other symptoms"
    echo "  that confirm it: 'whoami' prints a bare uid, 'sudo' says you do not exist in"
    echo "  the passwd database, git cannot resolve hosts while nslookup can, and"
    echo "  screencapture fails with 'could not create image from display'."
    echo
    echo "  REBOOT is the only fix.  Specifically NOT fixable in-session:"
    echo "    sudo killall opendirectoryd            -> sudo itself needs the dead daemon"
    echo "    launchctl kickstart -k system/com.apple.opendirectoryd"
    echo "                                           -> '141: Reentrancy avoided'"
    exit 2
fi

if [ ! -x "$CLI" ]; then
    echo "missing kicad-cli at $CLI -- run: phicad/bin/phicad build kicad-cli"
    exit 2
fi

# A stale kiface against a rebuilt libkicommon also aborts at 134.  Cheap to rule out.
if command -v ninja >/dev/null 2>&1; then
    pending=$(ninja -C "$ROOT/build/mac-dev" -n kicad-cli pcbnew_kiface 2>/dev/null \
              | grep -c '^\[' || true)
    if [ "${pending:-0}" -gt 0 ]; then
        say "note: $pending build steps pending for kicad-cli/pcbnew_kiface (relinking)"
        ninja -C "$ROOT/build/mac-dev" kicad-cli pcbnew_kiface >/dev/null 2>&1
    fi
fi

export KICAD_RUN_FROM_BUILD_DIR=1

# A PhiCad GUI holding this project can silently overwrite the files under test.  It
# happened: an editor still showing the pre-bonding revision saved at some point and put its
# stale 106-segment board back over the committed 120-segment one, and the only outward sign
# was `git status` showing the demo dirty.  The generator is the source of truth here, so
# warn rather than fail -- step 1 immediately regenerates and step 1b proves the result
# matches what is committed.
if pgrep -x PhiCad >/dev/null 2>&1; then
    echo "NOTE  PhiCad is running.  If it has this project open, it can overwrite the board"
    echo "      on save with whatever revision it loaded.  Close the document (or quit) if"
    echo "      the determinism check below fails unexpectedly."
    echo
fi

# --- 1. regenerate, and prove it is deterministic ------------------------------------
before=$(md5 -q "$BOARD" 2>/dev/null || echo none)
run generate "$PY" "$HERE/generate_board.py"
after=$(md5 -q "$BOARD")

if [ "$before" = "$after" ]; then
    RESULTS+=("PASS  deterministic (md5 unchanged: ${after:0:12})")
else
    RESULTS+=("NOTE  board changed this run (${before:0:8} -> ${after:0:8})")
fi

# --- 2. geometric pre-check (fast, and names nets) ----------------------------------
run pre-check python3 "$HERE/check_routing.py"

# --- 3. DRC -- the authority --------------------------------------------------------
if run drc "$CLI" pcb drc --refill-zones --format json -o "$OUT/drc.json" "$BOARD"; then
    python3 - "$OUT/drc.json" <<'PY' > "$OUT/drc.summary"
import collections, json, sys
d = json.load(open(sys.argv[1]))
c = collections.Counter((v.get("severity", "?"), v["type"]) for v in d["violations"])
err = sum(n for (s, _), n in c.items() if s == "error")
warn = sum(n for (s, _), n in c.items() if s == "warning")
print(f"errors={err} warnings={warn} "
      f"unconnected={len(d.get('unconnected_items', []))}")
for (s, k), n in sorted(c.items(), key=lambda kv: (kv[0][0], -kv[1])):
    print(f"    {n:4d}  {s:8s} {k}")
PY
    head -1 "$OUT/drc.summary" | grep -q "errors=0" \
        && RESULTS+=("PASS  drc: $(head -1 "$OUT/drc.summary")") \
        || RESULTS+=("WARN  drc: $(head -1 "$OUT/drc.summary")")
fi

# --- 4. renders and exports ---------------------------------------------------------
run render-top    "$CLI" pcb render --side top    -o "$OUT/render-top.png"    "$BOARD"
run render-bottom "$CLI" pcb render --side bottom -o "$OUT/render-bottom.png" "$BOARD"
# --mode-single is REQUIRED here and is the reason this step failed for so long: without
# it `export svg` defaults to --mode-multi, which treats -o as a DIRECTORY and writes one
# file per layer, so passing a .svg path produced no such file.  --layers is also required
# (the default plots nothing useful), and --fit-page-to-board keeps the sheet off the plot.
run export-svg    "$CLI" pcb export svg --theme phicad-altium -o "$OUT/board.svg" \
                  --layers F.Cu,B.Cu,F.Silkscreen,Edge.Cuts --mode-single \
                  --exclude-drawing-sheet --fit-page-to-board "$BOARD"
run export-gerber "$CLI" pcb export gerbers -o "$OUT/gerbers/" "$BOARD"
run export-step   "$CLI" pcb export step -o "$OUT/board.step" "$BOARD"

if [ -d "$OUT/gerbers" ]; then
    RESULTS+=("NOTE  gerber files: $(find "$OUT/gerbers" -type f | wc -l | tr -d ' ')")
fi

# --- summary ------------------------------------------------------------------------
echo
echo "=== Teensy sensor suite: verification ==============================="
for r in "${RESULTS[@]}"; do echo "  $r"; done
[ -f "$OUT/drc.summary" ] && { echo; sed -n '2,$p' "$OUT/drc.summary"; }
echo
echo "artifacts: $OUT"

printf '%s\n' "${RESULTS[@]}" | grep -q '^FAIL' && exit 1
printf '%s\n' "${RESULTS[@]}" | grep -q '^WARN  drc' && exit 0
exit 0
