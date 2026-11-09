#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Widen tracks headlessly and PROVE the change survived to disk.

WHY THIS SHAPE
    "The API returned OK" is not evidence. The only proof a headless edit worked is to
    close the session, start a FRESH server, re-open the saved file, and re-read the
    value. This script does exactly that, in three phases:

        phase 1  session A: open, record widths, widen the selected tracks, save
        phase 2  session A is torn down (server killed, socket removed)
        phase 3  session B (brand new server): re-open the SAVED file, re-read, compare

    Phase 3 needs a second server, not just a second get_board(): a server accepts only
    ONE DOCUMENT PER TYPE, so re-opening a .kicad_pcb in the same server fails with
    "a document of this type is already open".

WHAT IS VERIFIED WORKING (measured on build/mac-dev, PhiCad 10.99.0)
    Track width edits apply and persist. Confirmed 0.8 mm -> 0.85 mm and 0.999 mm round
    trips, checked by grepping the saved .kicad_pcb, not just by re-reading over IPC.

FOOTPRINT CAVEAT — READ THIS BEFORE EDITING FOOTPRINTS
    pcbnew/api/api_handler_board.cpp (~line 445) does NOT update footprints in place.
    PCB_FOOTPRINT_T and PCB_GROUP_T take a commit->Remove() + commit->Add() path, because
    the commit system cannot handle a footprint whose children were swapped out - the
    comment there is explicit that the editor VIEW would keep stale cached geometry for
    the footprint's children.

    What that means in practice, measured rather than assumed (--with-footprint-probe
    re-runs the experiment):
      * A headless footprint position edit DOES reach the file. Moving C1 by +2 mm and
        saving produced `(transform (translate 112.49 78.867) ...)` on disk, verified after
        the server exited. A footprint FIELD edit (value 100uF -> PHICAD_TEST_470uF) also
        persisted, the footprint's UUID was preserved, and the board's pad count was
        unchanged (247 before and after), so Remove()+Add() did not drop children here.
      * So the "footprint updates are silently discarded headlessly" claim did NOT
        reproduce on this build. The failure that looks like a silent discard is usually
        kipy's MISSING ReferencePoint WRAPPER: get_footprints() raises
        NotImplementedError on boards whose footprints hold `(point ...)` children, which
        reads like a broken write when it is really a broken read.
        session_lifecycle.install_fork_compat() fixes that.
      * The real risk stays: Remove()+Add() replaces the object, so anything the proto
        round trip does not carry is reconstructed, not preserved, and a live GUI attached
        to the same board can show stale child geometry. Prefer track/via/zone edits for
        unattended automation, and always verify footprint edits on disk the way this
        script does.

USAGE
    # widen track index 0 by 0.05 mm
    build/venv/bin/python phicad/mcp/examples/session_edit_track.py --index 0

    # widen every track on a net to an absolute width
    build/venv/bin/python phicad/mcp/examples/session_edit_track.py --net GND --width-mm 0.6

    --delta-mm D            add D mm to current width (default 0.05)
    --width-mm W            set absolute width instead
    --board PATH            source board (default demos/pic_programmer)
    --with-footprint-probe  also try a footprint move and report honestly what persisted
    --keep-temp             keep the staged copy so you can inspect the file yourself

    The board is always staged to a temp copy first: opening + saving upgrades a file to
    this fork's version (20260831), which stable KiCad 10.0 then refuses to open. Nothing
    under demos/ or qa/ is ever touched.

EXIT CODES
    0 change verified on disk, 1 session/selection failure, 3 change did NOT persist
    (the interesting failure), 2 unexpected, 130 interrupted.

Verified against build/mac-dev, PhiCad 10.99.0, on demos/pic_programmer: one track by
index and all 40 VCC tracks, both confirmed after a full server restart.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_lifecycle import (  # noqa: E402
    REPO_ROOT,
    HeadlessSession,
    SessionError,
    stage_fixture,
)

NM_PER_MM = 1e6


def mm(value_nm: int) -> float:
    return round(value_nm / NM_PER_MM, 4)


def select_tracks(board, index: Optional[int], net: Optional[str]) -> list:
    """Return the tracks to edit, by index or by net name."""
    tracks = board.get_tracks()
    if not tracks:
        raise SessionError("this board has no tracks to widen")

    if net is not None:
        chosen = [t for t in tracks if t.net.name == net]
        if not chosen:
            names = sorted({t.net.name for t in tracks if t.net.name})
            raise SessionError(
                f"no tracks on net {net!r}. Nets that have tracks:\n"
                f"  {', '.join(names[:25])}{' ...' if len(names) > 25 else ''}"
            )
        return chosen

    idx = 0 if index is None else index
    if idx < 0 or idx >= len(tracks):
        raise SessionError(f"track index {idx} out of range (board has {len(tracks)})")
    return [tracks[idx]]


def footprint_probe(board, session_label: str) -> Optional[dict]:
    """Move one footprint +1 mm in X and report what the server said. Honest, not hopeful.

    Returns the bookkeeping needed for the phase-3 disk check, or None if the board has
    no footprints.
    """
    footprints = board.get_footprints()
    if not footprints:
        return None
    fp = footprints[0]
    ref = fp.reference_field.text.value
    before = (fp.position.x, fp.position.y)

    from kipy.geometry import Vector2

    fp.position = Vector2.from_xy(before[0] + 1_000_000, before[1])
    commit = board.begin_commit()
    returned = board.update_items(fp)
    board.push_commit(commit, f"{session_label}: move {ref} +1mm")

    reported = (returned[0].position.x, returned[0].position.y) if returned else None
    reread = [f for f in board.get_footprints() if f.reference_field.text.value == ref]
    in_session = (reread[0].position.x, reread[0].position.y) if reread else None

    print(f"   footprint probe: {ref}")
    print(f"     before        {mm(before[0]):.3f} mm")
    print(f"     server echoed {mm(reported[0]):.3f} mm" if reported
          else "     server echoed nothing")
    print(f"     re-read live  {mm(in_session[0]):.3f} mm" if in_session
          else "     re-read failed")
    return {"reference": ref, "before_x": before[0], "want_x": before[0] + 1_000_000}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Widen track(s) headlessly, then prove the change persisted on disk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--board",
        default="demos/pic_programmer/pic_programmer.kicad_pcb",
        help="source board; relative paths resolve against the repo root "
             "(default: %(default)s)",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--index", type=int,
                          help="widen the track at this index (default: 0)")
    selector.add_argument("--net", help="widen every track on this net")
    amount = parser.add_mutually_exclusive_group()
    amount.add_argument("--delta-mm", type=float, default=0.05,
                        help="add this many mm to the current width (default: %(default)s)")
    amount.add_argument("--width-mm", type=float,
                        help="set this absolute width in mm instead of a delta")
    parser.add_argument("--with-footprint-probe", action="store_true",
                        help="also attempt a footprint move and report what persisted")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the staged copy for inspection")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="server readiness timeout (default: %(default)s)")
    args = parser.parse_args()

    work_dir: Optional[Path] = None
    try:
        board_path = stage_fixture(args.board, keep=args.keep_temp)
        work_dir = board_path.parent
        print(f"[stage]   {args.board}")
        print(f"[stage]   -> {board_path}   (temp copy; the original is never touched)")

        # ---------------- phase 1: edit and save in session A ----------------
        print("\n=== phase 1: session A - open, widen, save ===")
        expected: list[tuple[str, int, int]] = []   # (track id, before_nm, after_nm)
        fp_probe: Optional[dict] = None

        with HeadlessSession(name=f"edit-a-{board_path.stem}",
                             ready_timeout=args.timeout) as sess_a:
            print(f"[server]  pid {sess_a.proc.pid if sess_a.proc else '?'} "
                  f"on {sess_a.socket_url}")
            board = sess_a.open_board(board_path)
            print(f"[open]    {board.name}: {len(board.get_tracks())} tracks")

            targets = select_tracks(board, args.index, args.net)
            how = (f"net {args.net!r}" if args.net
                   else f"index {0 if args.index is None else args.index}")
            print(f"[select]  {len(targets)} track(s) by {how}")

            commit = board.begin_commit()
            for track in targets:
                before = track.width
                after = (int(args.width_mm * NM_PER_MM) if args.width_mm is not None
                         else before + int(args.delta_mm * NM_PER_MM))
                if after <= 0:
                    raise SessionError(f"refusing a non-positive width: {after} nm")
                track.width = after
                expected.append((track.id.value, before, after))
            returned = board.update_items(targets)
            board.push_commit(commit, "session_edit_track: widen")
            print(f"[edit]    update_items accepted {len(returned)} item(s)")
            for tid, before, after in expected[:6]:
                print(f"           {tid[:8]}  {mm(before):.4f} mm -> {mm(after):.4f} mm")
            if len(expected) > 6:
                print(f"           ... {len(expected) - 6} more")

            if args.with_footprint_probe:
                fp_probe = footprint_probe(board, "session A")

            sess_a.save(board)
            print("[save]    SaveDocument ok")

        print("[stop]    session A torn down (server killed, socket removed)")

        # ---------------- phase 3: verify in a brand new session B ----------------
        print("\n=== phase 3: session B - FRESH server, re-open the saved file ===")
        want = {tid: after for tid, _before, after in expected}
        with HeadlessSession(name=f"edit-b-{board_path.stem}",
                             ready_timeout=args.timeout) as sess_b:
            print(f"[server]  pid {sess_b.proc.pid if sess_b.proc else '?'} "
                  f"on {sess_b.socket_url}")
            board_b = sess_b.open_board(board_path)
            on_disk = {t.id.value: t.width for t in board_b.get_tracks()}

            matched, mismatched, missing = [], [], []
            for tid, target in want.items():
                if tid not in on_disk:
                    missing.append(tid)
                elif on_disk[tid] == target:
                    matched.append(tid)
                else:
                    mismatched.append((tid, target, on_disk[tid]))

            print(f"[verify]  re-read {len(on_disk)} tracks from the saved file")
            for tid in matched[:6]:
                print(f"           OK       {tid[:8]}  = {mm(want[tid]):.4f} mm")
            for tid, target, got in mismatched[:6]:
                print(f"           MISMATCH {tid[:8]}  want {mm(target):.4f} mm, "
                      f"got {mm(got):.4f} mm")
            for tid in missing[:6]:
                print(f"           MISSING  {tid[:8]}  (id not present after reload)")

            fp_disk_ok: Optional[bool] = None
            if fp_probe:
                found = [f for f in board_b.get_footprints()
                         if f.reference_field.text.value == fp_probe["reference"]]
                if found:
                    got_x = found[0].position.x
                    fp_disk_ok = got_x == fp_probe["want_x"]
                    print(f"[verify]  footprint {fp_probe['reference']} on disk: "
                          f"{mm(got_x):.3f} mm "
                          f"(wanted {mm(fp_probe['want_x']):.3f} mm) -> "
                          f"{'PERSISTED' if fp_disk_ok else 'DISCARDED'}")
                else:
                    print(f"[verify]  footprint {fp_probe['reference']} vanished after reload")
                    fp_disk_ok = False

        print("[stop]    session B torn down")

        # ---------------- verdict ----------------
        print(f"\n{'=' * 70}")
        if mismatched or missing:
            print(f"RESULT: track change did NOT fully persist "
                  f"({len(matched)} ok, {len(mismatched)} wrong, {len(missing)} missing)")
            print(f"{'=' * 70}")
            return 3
        print(f"RESULT: verified on disk - {len(matched)} track(s) kept the new width "
              f"across a full server restart")
        if fp_probe:
            verdict = ("position edit ALSO persisted" if fp_disk_ok
                       else "position edit was DISCARDED")
            print(f"        footprint probe: {verdict}"
                  f"  (see the docstring caveat either way)")
        print(f"{'=' * 70}")
        return 0

    except SessionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nFAIL: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if work_dir and not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
        elif work_dir:
            print(f"[clean]   kept {work_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
