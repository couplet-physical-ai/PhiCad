#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Read back what the agent suite recorded.

    phicad telemetry                 # last 7 days
    phicad telemetry --days 30
    phicad telemetry --json          # the same numbers, for a dashboard
    phicad telemetry --where         # just say where the files are

Durations are reported as median and p90, not as a mean.  One 300-second timeout
in a set of twenty two-second turns moves a mean by fifteen seconds and tells you
nothing about what a turn is normally like; the median stays put and the p90 is
where the timeout shows up, which is the pair worth having.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telemetry  # noqa: E402


def read(days: int) -> Iterator[Dict[str, Any]]:
    """Every record from the last `days` daily files, oldest first.

    A corrupt line is skipped rather than fatal: a record half-written when the
    editor was killed should cost that record and nothing else.
    """
    now = time.time()
    wanted = {time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)) for i in range(days)}
    directory = telemetry.log_dir()
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        if path.stem not in wanted:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def quantile(values: List[float], q: float) -> float:
    """Nearest-rank quantile.  No interpolation: these are measurements, not a model."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[rank]


def summarise(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    turns = [r for r in records if r.get("event") == "ai.turn"]
    runs = [r for r in records if r.get("event") == "synth.run"]

    def durations(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        ms = [float(r["duration_ms"]) for r in rows if isinstance(r.get("duration_ms"), (int, float))]
        return {"median_ms": round(quantile(ms, 0.5)), "p90_ms": round(quantile(ms, 0.9))}

    ok_turns = [r for r in turns if r.get("outcome") == "ok"]
    sessions = {r.get("session_id") for r in turns if r.get("session_id")}

    failed_stage_counts: Counter = Counter()
    for r in runs:
        for name in r.get("failed_stages") or []:
            failed_stage_counts[name] += 1

    # A repair loop checks the same design once per attempt, so one user action can file
    # eight failures on its way to a design that ends up valid.  Counting attempts flat
    # would headline three self-repairing designs as "failed 24, ok 3".  Runs carrying a
    # run_id are folded into one cycle, judged by its LAST attempt; runs without one are
    # standalone and counted as themselves.
    cycles: Dict[str, List[Dict[str, Any]]] = {}
    standalone: List[Dict[str, Any]] = []
    for r in runs:
        run_id = r.get("run_id")
        if run_id:
            cycles.setdefault(str(run_id), []).append(r)
        else:
            standalone.append(r)

    def last_attempt(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return max(rows, key=lambda r: (r.get("attempt") or 0, r.get("ts") or ""))

    cycle_outcomes = Counter(last_attempt(rows)["outcome"] for rows in cycles.values()
                             if rows and "outcome" in last_attempt(rows))
    repaired = sum(1 for rows in cycles.values()
                   if last_attempt(rows).get("outcome") == "ok" and len(rows) > 1)

    unknown = {r.get("schema") for r in records} - {telemetry.SCHEMA, None}

    return {
        "records": len(records),
        "unknown_schema_versions": sorted(v for v in unknown if v is not None),
        "ai_turns": {
            "total": len(turns),
            "sessions": len(sessions),
            "outcomes": dict(Counter(r.get("outcome", "?") for r in turns).most_common()),
            "backends": dict(Counter(r.get("backend", "?") for r in turns).most_common()),
            # Which surface the turn came from: the chat pane, or the automatic repair
            # loop.  Without this the loop's turns -- usually the majority -- are
            # indistinguishable from someone typing.
            "sources": dict(Counter(r.get("source", "chat_pane") for r in turns).most_common()),
            "with_board_context": sum(1 for r in turns if r.get("has_context")),
            **durations(turns),
            "prompt_bytes": durations_field(turns, "prompt_bytes"),
            "reply_bytes": durations_field(ok_turns, "reply_bytes"),
            # Usage as the provider reported it (phi --mode json); absent on turns
            # whose backend reported nothing, so "measured" counts those that did.
            "usage": usage_rollup(turns),
        },
        "synth_runs": {
            "total": len(runs),
            "attempt_outcomes": dict(Counter(r.get("outcome", "?") for r in runs).most_common()),
            "commands": dict(Counter(r.get("command", "?") for r in runs).most_common()),
            "repair_cycles": len(cycles),
            "cycle_outcomes": dict(cycle_outcomes.most_common()),
            "self_repaired": repaired,
            "standalone": len(standalone),
            **durations(runs),
            "failing_stages": dict(failed_stage_counts.most_common()),
        },
    }


def durations_field(rows: List[Dict[str, Any]], field: str) -> Dict[str, float]:
    vals = [float(r[field]) for r in rows if isinstance(r.get(field), (int, float))]
    return {"median": round(quantile(vals, 0.5)), "p90": round(quantile(vals, 0.9))}


def usage_rollup(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cost and tokens summed over the turns that reported them, split by model.

    Every number is a sum of provider-reported values — nothing is estimated for
    the turns that reported nothing, and `measured` says how representative the
    totals are.  Per-model rows are what makes tier decisions data: the same
    stage on two models becomes two lines you can compare.
    """
    measured = [r for r in turns if isinstance(r.get("cost_usd"), (int, float))
                or isinstance(r.get("tokens_in"), (int, float))]
    by_model: Dict[str, Dict[str, Any]] = {}
    for r in measured:
        key = str(r.get("model") or "?")
        row = by_model.setdefault(key, {"turns": 0, "tokens_in": 0, "tokens_out": 0,
                                        "tokens_reasoning": 0, "cost_usd": 0.0})
        row["turns"] += 1
        for f in ("tokens_in", "tokens_out", "tokens_reasoning"):
            if isinstance(r.get(f), (int, float)):
                row[f] += int(r[f])
        if isinstance(r.get("cost_usd"), (int, float)):
            row["cost_usd"] = round(row["cost_usd"] + float(r["cost_usd"]), 6)
    return {
        "measured": len(measured),
        "unmeasured": len(turns) - len(measured),
        "cost_usd": round(sum(row["cost_usd"] for row in by_model.values()), 6),
        "by_model": dict(sorted(by_model.items(),
                                key=lambda kv: -kv[1]["cost_usd"])),
    }


def human(summary: Dict[str, Any], days: int) -> str:
    out: List[str] = []
    ai, syn = summary["ai_turns"], summary["synth_runs"]
    out.append(f"telemetry, last {days} day(s) — {summary['records']} record(s) "
               f"in {telemetry.log_dir()}")
    if summary["unknown_schema_versions"]:
        out.append(f"  NOTE: records from schema {summary['unknown_schema_versions']} "
                   f"(this reader knows {telemetry.SCHEMA}); some fields may be summarised wrong")

    out.append("")
    out.append(f"AI turns    {ai['total']} across {ai['sessions']} session(s), "
               f"{ai['with_board_context']} with live board context")
    if ai["total"]:
        out.append(f"  duration  median {ai['median_ms']:,} ms, p90 {ai['p90_ms']:,} ms")
        out.append(f"  prompt    median {ai['prompt_bytes']['median']:,} B, "
                   f"p90 {ai['prompt_bytes']['p90']:,} B")
        out.append(f"  reply     median {ai['reply_bytes']['median']:,} B, "
                   f"p90 {ai['reply_bytes']['p90']:,} B   (successful turns only)")
        out.append("  outcomes  " + ", ".join(f"{k} {v}" for k, v in ai["outcomes"].items()))
        out.append("  source    " + ", ".join(f"{k} {v}" for k, v in ai["sources"].items()))
        out.append("  backend   " + ", ".join(f"{k} {v}" for k, v in ai["backends"].items()))
        usage = ai["usage"]
        if usage["measured"]:
            out.append(f"  cost      ${usage['cost_usd']:.4f} over {usage['measured']} "
                       f"measured turn(s)"
                       + (f" ({usage['unmeasured']} reported no usage)"
                          if usage["unmeasured"] else ""))
            for name, row in usage["by_model"].items():
                out.append(f"    {name:14s} {row['turns']:3d} turn(s)  "
                           f"in {row['tokens_in']:,} / out {row['tokens_out']:,} "
                           f"/ think {row['tokens_reasoning']:,} tok  "
                           f"${row['cost_usd']:.4f}")

    out.append("")
    out.append(f"synth runs  {syn['total']} invocation(s)")
    if syn["total"]:
        out.append(f"  duration  median {syn['median_ms']:,} ms, p90 {syn['p90_ms']:,} ms")
        out.append("  command   " + ", ".join(f"{k} {v}" for k, v in syn["commands"].items()))
        if syn["repair_cycles"]:
            out.append(f"  cycles    {syn['repair_cycles']} repair cycle(s), "
                       f"{syn['standalone']} standalone — judged on the last attempt: "
                       + ", ".join(f"{k} {v}" for k, v in syn["cycle_outcomes"].items()))
            out.append(f"            {syn['self_repaired']} design(s) failed a gate and "
                       f"then repaired themselves")
        out.append("  attempts  " + ", ".join(f"{k} {v}"
                                              for k, v in syn["attempt_outcomes"].items())
                   + ("   (a repair cycle files one per attempt)"
                      if syn["repair_cycles"] else ""))
        if syn["failing_stages"]:
            out.append("  failing   " + ", ".join(f"{k} x{v}"
                                                  for k, v in syn["failing_stages"].items()))
        else:
            out.append("  failing   none")

    if not summary["records"]:
        out.append("")
        out.append("Nothing recorded yet. Telemetry is "
                   + ("on" if telemetry.enabled() else "OFF (PHICAD_TELEMETRY)")
                   + "; it fills as the AI View pane is used.")
    return "\n".join(out)


def ship(argv: List[str]) -> int:
    """Send anything not yet sent to InfluxDB, for Grafana to draw."""
    import telemetry_influx as influx

    ap = argparse.ArgumentParser(prog="phicad telemetry ship",
                                 description=influx.__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the line protocol and send nothing (no token needed)")
    ap.add_argument("--batch", type=int, default=1000, help="lines per HTTP write")
    ap.add_argument("--reset", action="store_true",
                    help="forget the cursor and re-ship every file from the start")
    args = ap.parse_args(argv)

    log_dir = telemetry.log_dir()
    if not log_dir.is_dir():
        print(f"nothing to ship: {log_dir} does not exist")
        return 0

    # A dry run needs no configuration at all: its whole purpose is to show what would
    # be sent, which is exactly what you want to look at BEFORE you have an endpoint.
    config = None
    if not args.dry_run:
        try:
            config = influx.load_config()
        except influx.ShipError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"==> {config.redacted()}")

    offsets = {} if args.reset else influx.read_cursor(log_dir)
    total = skipped = 0
    for path, new_offset, raw in influx.pending(log_dir, offsets):
        lines: List[str] = []
        for text in raw:
            text = text.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                skipped += 1
                continue
            encoded = influx.line(record)
            if encoded is None:
                skipped += 1
                continue
            lines.append(encoded)

        if args.dry_run:
            for encoded in lines:
                print(encoded)
            total += len(lines)
            continue

        try:
            for start in range(0, len(lines), max(1, args.batch)):
                influx.post(config, "\n".join(lines[start:start + args.batch]))
        except influx.ShipError as exc:
            # Leave the cursor where it was: the next run re-sends this file from the
            # last confirmed point.  Influx writes are idempotent for identical
            # measurement+tags+timestamp, so a repeat overwrites rather than doubles.
            influx.write_cursor(log_dir, offsets)
            print(f"error: {exc}", file=sys.stderr)
            return 1

        offsets[path.name] = new_offset
        total += len(lines)

    if not args.dry_run:
        influx.write_cursor(log_dir, offsets)
        print(f"shipped {total} point(s)"
              + (f", skipped {skipped} unusable record(s)" if skipped else ""))
    elif not total:
        print("nothing pending")
    return 0


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ship":
        return ship(argv[1:])

    ap = argparse.ArgumentParser(prog="phicad telemetry", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="phicad telemetry ship [--dry-run]   "
                                        "send to InfluxDB for Grafana")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--where", action="store_true", help="print the log location and exit")
    args = ap.parse_args(argv)

    if args.where:
        import telemetry_influx as influx
        print(f"directory : {telemetry.log_dir()}")
        print(f"today     : {telemetry.current_log()}")
        print(f"enabled   : {'yes' if telemetry.enabled() else 'no (PHICAD_TELEMETRY)'}")
        try:
            print(f"influx    : {influx.load_config(require_token=False).redacted()}")
        except influx.ShipError:
            print("influx    : not configured (PHICAD_INFLUX_URL/ORG/BUCKET/TOKEN)")
        return 0

    summary = summarise(list(read(max(1, args.days))))
    print(json.dumps(summary, indent=2) if args.as_json else human(summary, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
