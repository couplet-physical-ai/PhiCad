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
"""Ship the telemetry file to InfluxDB, for Grafana to draw.

    phicad telemetry ship --dry-run        # show the line protocol, send nothing
    phicad telemetry ship                  # send what has not been sent yet

Shipping is a SEPARATE STEP, not a sink on the write path.  A design session must
never wait on a metrics endpoint, and a laptop that is offline, on a plane, or
outside the VPN must still record everything and catch up later.  The file stays
the source of truth; this is a reader over it, and it is resumable — a cursor
records the byte offset reached in each daily file, so re-running ships only what
is new and an interrupted run resumes rather than duplicating.

CARDINALITY, which is the thing that goes wrong
-----------------------------------------------
InfluxDB builds an index entry per unique combination of tag values.  Putting a
UUID in a tag creates one series per session forever: the index outgrows the data,
queries slow to a crawl, and the fix is a rewrite.  So:

    TAGS    event, source, outcome, command, backend  -- closed vocabularies,
            a few dozen combinations in total
    FIELDS  session_id, run_id, attempt, every duration and byte count

`session_id` and `run_id` are the fields you most want to group by, and they are
exactly the ones that must not be tags.  Grafana can still filter on a field; it
is the index that cannot afford them.

Configuration, all by environment, nothing in the repo
-----------------------------------------------------
    PHICAD_INFLUX_URL      https://influx.internal.example
    PHICAD_INFLUX_ORG      example
    PHICAD_INFLUX_BUCKET   phicad
    PHICAD_INFLUX_TOKEN    a write token, or
    PHICAD_INFLUX_TOKEN_FILE  a path to one (preferred: a token in the environment
                              is visible in `ps` output and shell history)

There is no default host.  A wrong endpoint that silently accepts writes is worse
than a missing one, so this refuses to guess.
"""

from __future__ import annotations

import calendar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MEASUREMENT = "phicad"

# Closed vocabularies only.  Anything unbounded belongs in a field; see the note above.
#
# `failed_stage` is derived, not recorded: the record carries `failed_stages`, a list.
# A list cannot be a tag without one series per COMBINATION of gates, but the compiler
# stops at the first failing gate, so the first name is both the useful one and a
# closed vocabulary of about fourteen.  That single name is what makes "which gate is
# failing builds this week" a one-line Grafana query instead of a text search.
#
# `model` and `provider` are the identity of the model that answered a turn, as
# the provider itself reported it (chat_backend.TurnUsage) — a vocabulary the
# size of the gateway's alias list, and THE group-by for cost questions:
# "spend per model this week" must be a tag scan, not a field search.  Points
# shipped before this key existed simply lack the tag; Flux group() treats the
# missing column as empty, so old data stays queryable.
TAG_KEYS = ("event", "source", "outcome", "command", "backend", "failed_stage",
            "model", "provider")

# Written next to the logs, not in the repo.
CURSOR_NAME = ".influx-cursor.json"

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
               "ALL_PROXY", "all_proxy")


class ShipError(RuntimeError):
    """Configuration is missing or the write was rejected."""


# --------------------------------------------------------------------- encoding

def _esc_tag(value: str) -> str:
    """Tag keys, tag values and field keys: comma, equals and space are separators."""
    return (str(value).replace("\\", "\\\\").replace(",", r"\,")
            .replace("=", r"\=").replace(" ", r"\ "))


def _esc_measurement(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(",", r"\,").replace(" ", r"\ ")


def _field(value: Any) -> Optional[str]:
    """Render one field value, or None for something line protocol cannot carry."""
    if isinstance(value, bool):
        # Checked before int: bool is a subclass of int, and True would otherwise
        # ship as 1i and lose the fact that it was a flag.
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        # failed_stages is a list; a comma-joined string keeps it greppable in
        # Grafana without inventing one series per combination of gates.
        return _field(",".join(str(v) for v in value)) if value else None
    return None


def to_nanos(ts: str) -> int:
    """`2026-09-06T22:41:17Z` -> epoch nanoseconds.

    The record's own timestamp, not now(): a file shipped a day late must land on
    the day the work actually happened, or every dashboard reads as a spike at the
    moment someone ran the shipper.
    """
    try:
        parsed = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return int(time.time() * 1_000_000_000)
    return int(calendar.timegm(parsed)) * 1_000_000_000


def line(record: Dict[str, Any], measurement: str = MEASUREMENT) -> Optional[str]:
    """One record -> one line of InfluxDB line protocol, or None if unusable."""
    tags: List[str] = []
    fields: List[str] = []

    record = dict(record)
    first_failed = (record.get("failed_stages") or [None])[0]
    if isinstance(first_failed, str) and first_failed:
        record["failed_stage"] = first_failed

    for key, value in sorted(record.items()):
        if key == "ts":
            continue
        if key in TAG_KEYS:
            if value not in (None, ""):
                tags.append(f"{_esc_tag(key)}={_esc_tag(value)}")
            continue
        rendered = _field(value)
        if rendered is not None:
            fields.append(f"{_esc_tag(key)}={rendered}")
    if not fields:
        # A point with no field is rejected by Influx; drop it here with the reason
        # visible rather than let the whole batch fail on one bad record.
        return None
    head = _esc_measurement(measurement) + ("," + ",".join(tags) if tags else "")
    return f"{head} {','.join(fields)} {to_nanos(record.get('ts', ''))}"


# ---------------------------------------------------------------------- config

class Config:
    def __init__(self, url: str, org: str, bucket: str, token: str) -> None:
        self.url, self.org, self.bucket, self.token = url.rstrip("/"), org, bucket, token

    @property
    def write_url(self) -> str:
        return (f"{self.url}/api/v2/write?org={urllib.parse.quote(self.org)}"
                f"&bucket={urllib.parse.quote(self.bucket)}&precision=ns")

    def redacted(self) -> str:
        return f"{self.url} org={self.org} bucket={self.bucket} token=<{len(self.token)} chars>"


def load_config(require_token: bool = True) -> Config:
    url = os.environ.get("PHICAD_INFLUX_URL", "").strip()
    org = os.environ.get("PHICAD_INFLUX_ORG", "").strip()
    bucket = os.environ.get("PHICAD_INFLUX_BUCKET", "").strip()

    token = os.environ.get("PHICAD_INFLUX_TOKEN", "").strip()
    token_file = os.environ.get("PHICAD_INFLUX_TOKEN_FILE", "").strip()
    if not token and token_file:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ShipError(f"cannot read PHICAD_INFLUX_TOKEN_FILE {path}: {exc}") from None
        mode = path.stat().st_mode & 0o077
        if mode:
            raise ShipError(f"{path} is group/world readable ({oct(path.stat().st_mode & 0o777)});"
                            " chmod 600 it")

    missing = [name for name, value in (("PHICAD_INFLUX_URL", url),
                                        ("PHICAD_INFLUX_ORG", org),
                                        ("PHICAD_INFLUX_BUCKET", bucket)) if not value]
    if require_token and not token:
        missing.append("PHICAD_INFLUX_TOKEN or PHICAD_INFLUX_TOKEN_FILE")
    if missing:
        raise ShipError("not configured; set " + ", ".join(missing)
                        + "\n(there is no default endpoint: a wrong one that silently"
                          " accepts writes is worse than a missing one)")
    return Config(url, org, bucket, token)


# ---------------------------------------------------------------------- cursor

def cursor_path(log_dir: Path) -> Path:
    return log_dir / CURSOR_NAME


def read_cursor(log_dir: Path) -> Dict[str, int]:
    try:
        data = json.loads(cursor_path(log_dir).read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.get("offsets", {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def write_cursor(log_dir: Path, offsets: Dict[str, int]) -> None:
    path = cursor_path(log_dir)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"version": 1, "offsets": offsets}, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)          # atomic: a torn cursor would re-ship a whole day
    except OSError:
        tmp.unlink(missing_ok=True)


def pending(log_dir: Path, offsets: Dict[str, int]) -> Iterable[Tuple[Path, int, List[str]]]:
    """(file, new_offset, lines) for everything not yet shipped.

    Reads by byte offset rather than line count so a file being appended to while
    this runs is picked up exactly once: whatever was complete at read time ships,
    and the rest is next run's problem.
    """
    for path in sorted(log_dir.glob("*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = offsets.get(path.name, 0)
        if start >= size:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                chunk = handle.read()
        except OSError:
            continue
        # Ship only whole lines; a partial trailing line means the writer is mid-append.
        end = chunk.rfind("\n")
        if end < 0:
            continue
        whole = chunk[:end + 1]
        yield path, start + len(whole.encode("utf-8")), whole.splitlines()


# ----------------------------------------------------------------------- write

def post(config: Config, body: str, timeout: float = 30.0) -> None:
    request = urllib.request.Request(
        config.write_url, data=body.encode("utf-8"), method="POST",
        headers={"Authorization": f"Token {config.token}",
                 "Content-Type": "text/plain; charset=utf-8"})
    # The LLM proxy fronts an LLM gateway and rejects ordinary HTTP; an internal
    # metrics host is reached directly, the same way phicad's other network commands do.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status not in (200, 204):
                raise ShipError(f"influx returned {response.status}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise ShipError(f"influx rejected the write ({exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise ShipError(f"cannot reach {config.url}: {exc.reason}") from None
