#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""phicad/loop/loop.py — the PhiCad continuous development loop.

One *iteration* = pick one backlog item -> implement it with phi in a fresh worktree off
``origin/dev`` -> run the fork's own gates -> push the branch -> open (or queue) a pull
request into ``dev`` -> record what happened.  A systemd user timer on the Linux host runs
``loop.py once`` on a schedule (``phicad/loop/install-remote.sh``); nothing here merges.

Why a loop and not "an agent that keeps going": every step that decides whether work may
reach ``dev`` — scope, fork boundary, ``phicad check fast``, the item's own gates — runs in
this deterministic script, *after* the model has finished and cannot argue with the result.
The model only ever sees one bounded brief, one worktree, one branch.  What it produces is
pushed under ``loop/<item>-<stamp>`` whether or not it passed, so nothing is lost and a
rejected attempt can be read; only a passing attempt becomes a pull request.

The AI step runs through phi, an agent harness, and nothing else: ``phi -p`` (runner
``phi``), using the credential phi already holds for that person; this file never reads
``~/.pi/agent/auth.json`` and never puts a credential in argv or a log.

Stdlib only, so ``phicad check fast`` can run ``test_loop.py`` on any host without a venv.
Backlog items are Markdown files with a small frontmatter (see ``README.md``) — a reviewable,
versioned queue: adding, reprioritising or retiring work is a pull request like any other.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from run_window import deadline as window_deadline

__all__ = [
    "Item", "parse_item", "render_item", "load_backlog", "validate_backlog", "pick_item",
    "paths_in_scope", "parse_check_summary", "compose_brief", "Config",
]

STATUSES = ("open", "in-review", "done", "blocked", "retired")
TIERS = ("cheap", "standard", "strong")
DEFAULT_MODELS = {
    "cheap": "gateway/fast:high",
    "standard": "gateway/strong:high",
    "strong": "gateway/frontier:max",
}
DEFAULT_SCOPE = ("phicad/**", "AGENTS.md")
LIST_KEYS = ("scope", "gates", "depends_on", "requires", "tags")
INT_KEYS = ("priority", "timeout_min", "max_attempts")
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
# Argument shapes that must never reach a subprocess argv or a log line.
SECRET_PATTERNS = (re.compile(r"(?i)bearer\s+\S+"), re.compile(r"(?i)sk-[a-z0-9_-]{8,}"))
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")


# ------------------------------------------------------------------ backlog items


@dataclass
class Item:
    """One backlog entry: ``phicad/loop/backlog/<id>.md`` = frontmatter + brief."""

    id: str
    title: str
    status: str = "open"
    priority: int = 100
    tier: str = "standard"
    scope: tuple = DEFAULT_SCOPE
    gates: tuple = ()
    depends_on: tuple = ()
    requires: tuple = ()
    tags: tuple = ()
    timeout_min: int = 40
    max_attempts: int = 2
    source: str = ""
    branch: str = ""
    pr: str = ""
    blocked_reason: str = ""
    body: str = ""
    path: Path | None = None
    extra: dict = field(default_factory=dict)
    digest: str = ""          # sha1 of the file text: a revised item is a new item (see reset_revised)

    def model(self, models: dict) -> str:
        return models.get(self.tier, models["standard"])


def _split_list(raw: str) -> tuple:
    raw = raw.strip()
    if not raw or raw == "[]":
        return ()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return tuple(p.strip().strip("'\"") for p in raw.split(",") if p.strip())


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the ``---`` block: ``key: value`` lines and ``key:`` + ``- item`` lists.

    Deliberately not YAML: the stdlib has no YAML parser and this subset is all a backlog
    item needs.  Unknown keys are kept verbatim in ``extra`` so a round trip loses nothing.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("no frontmatter block (--- ... ---) at the top of the file")
    meta: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and line.lstrip().startswith("- ") and key:
            meta[key] = tuple(meta.get(key, ())) + (line.lstrip()[2:].strip().strip("'\""),)
            continue
        if ":" not in line:
            raise ValueError(f"frontmatter line is not 'key: value': {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        meta[key] = _split_list(value) if key in LIST_KEYS else value
        if key in LIST_KEYS and value == "":
            meta[key] = ()
    return meta, m.group(2)


def parse_item(text: str, path: Path | None = None) -> Item:
    meta, body = parse_frontmatter(text)
    known = {f for f in Item.__dataclass_fields__ if f not in ("body", "path", "extra")}
    item_id = str(meta.pop("id", "") or (path.stem if path else "")).strip()
    if not item_id:
        raise ValueError("item has no id and no file name to derive one from")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", item_id):
        raise ValueError(f"item id {item_id!r} must be lowercase [a-z0-9._-]")
    kwargs: dict = {"id": item_id, "path": path, "body": body.strip("\n") + "\n",
                    "digest": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]}
    extra: dict = {}
    for k, v in meta.items():
        if k not in known:
            extra[k] = v
            continue
        if k in INT_KEYS:
            try:
                v = int(v)
            except ValueError as exc:
                raise ValueError(f"{item_id}: {k} must be an integer, got {v!r}") from exc
        if k in LIST_KEYS and isinstance(v, str):
            v = _split_list(v)
        kwargs[k] = v
    kwargs["extra"] = extra
    if "title" not in kwargs:
        raise ValueError(f"{item_id}: missing title")
    item = Item(**kwargs)
    if item.status not in STATUSES:
        raise ValueError(f"{item_id}: status {item.status!r} not in {STATUSES}")
    if item.tier not in TIERS:
        raise ValueError(f"{item_id}: tier {item.tier!r} not in {TIERS}")
    if not item.scope:
        item.scope = DEFAULT_SCOPE
    return item


def render_item(item: Item) -> str:
    """Serialise an item back to its file form (status flips are the only writes we do)."""
    lines = ["---", f"id: {item.id}", f"title: {item.title}", f"status: {item.status}",
             f"priority: {item.priority}", f"tier: {item.tier}"]
    for k in ("scope", "gates", "depends_on", "requires", "tags"):
        vals = getattr(item, k)
        if k in ("scope",) or vals:
            lines.append(f"{k}:")
            lines.extend(f"  - {v}" for v in vals)
    lines.append(f"timeout_min: {item.timeout_min}")
    lines.append(f"max_attempts: {item.max_attempts}")
    for k in ("source", "branch", "pr", "blocked_reason"):
        if getattr(item, k):
            lines.append(f"{k}: {getattr(item, k)}")
    for k, v in item.extra.items():
        lines.append(f"{k}: {v}" if not isinstance(v, tuple) else f"{k}: [{', '.join(v)}]")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + item.body.strip("\n") + "\n"


def load_backlog(backlog_dir: Path) -> list[Item]:
    items = []
    for p in sorted(Path(backlog_dir).glob("*.md")):
        if p.name.upper() == "README.MD":
            continue
        items.append(parse_item(p.read_text(encoding="utf-8"), p))
    return items


def validate_backlog(items: list[Item]) -> list[str]:
    """Return human-readable problems; an empty list means the backlog is consistent."""
    problems = []
    ids = [i.id for i in items]
    for dup in sorted({x for x in ids if ids.count(x) > 1}):
        problems.append(f"duplicate id: {dup}")
    known = set(ids)
    for it in items:
        if it.path is not None and it.path.stem != it.id:
            problems.append(f"{it.id}: file name {it.path.name} does not match id")
        for dep in it.depends_on:
            if dep not in known:
                problems.append(f"{it.id}: depends_on unknown item {dep!r}")
        if it.timeout_min <= 0 or it.timeout_min > 240:
            problems.append(f"{it.id}: timeout_min {it.timeout_min} outside 1..240")
        if it.max_attempts <= 0:
            problems.append(f"{it.id}: max_attempts must be >= 1")
        if not it.body.strip():
            problems.append(f"{it.id}: empty brief")
        for g in it.scope:
            if g.startswith("/") or g.startswith(".."):
                problems.append(f"{it.id}: scope glob {g!r} must be repo-relative")
    # cycle check (duplicates merge their edges so a dup cannot hide a cycle)
    graph: dict = {}
    for i in items:
        graph.setdefault(i.id, set()).update(set(i.depends_on) & known)
    seen: dict = {}

    def visit(n, stack):
        if seen.get(n) == "done":
            return
        if n in stack:
            problems.append("dependency cycle: " + " -> ".join(stack + [n]))
            return
        for d in graph.get(n, ()):
            visit(d, stack + [n])
        seen[n] = "done"

    for n in graph:
        visit(n, [])
    return problems


def requirements_missing(item: Item, env: dict | None = None) -> list[str]:
    """``requires:`` entries are paths (``$VAR`` expanded) that must exist first —
    e.g. a kiface the host is still building.  Missing ones make the item ineligible today
    without marking it blocked."""
    env = dict(os.environ if env is None else env)
    missing = []
    for raw in item.requires:
        p = Path(os.path.expandvars(os.path.expanduser(raw.replace("$HOME", env.get("HOME", "~")))))
        # expandvars uses os.environ; honour an explicit env for tests
        for k, v in env.items():
            p = Path(str(p).replace(f"${k}", v).replace(f"${{{k}}}", v))
        if not p.exists():
            missing.append(str(p))
    return missing


def reset_revised(items: list[Item], state: dict) -> list[str]:
    """An item whose file changed since its last attempt gets its attempts back.

    Why: the first real iteration (2026-09-22) ended `agent-blocked` because the item's scope
    contradicted its definition of done. Fixing the item is a one-line PR; the loop should then
    simply try again, not wait for someone to hand-edit state.json. A pending pull request is
    left alone — it belongs to the old text and is judged on its own."""
    revised = []
    for it in items:
        st = state.get("items", {}).get(it.id)
        if not st or not st.get("digest") or st["digest"] == it.digest or st.get("pending_branch"):
            continue
        if it.status != "open":        # the merged status flip changes the text too; nothing to retry
            continue
        st["attempts"] = 0
        st.pop("exhausted", None)
        st["revised_at"] = utcnow()
        st["previous_digest"] = st["digest"]
        st["digest"] = it.digest
        revised.append(it.id)
    return revised


def pick_item(items: list[Item], state: dict, *, prefer: str = "", env: dict | None = None,
              merged: set | None = None) -> tuple[Item | None, list[str]]:
    """Choose the next item.  Returns (item or None, reasons for every skipped item).

    Eligible = status open, every dependency done (in the backlog or merged per ``state``),
    no attempt with a pull request still pending, attempts left, requirements present.
    ``prefer`` (the ``control/NEXT`` file) wins when eligible; then lowest priority, then id.
    ``merged`` is the set of item ids whose loop branch is already an ancestor of dev.
    """
    merged = merged or set()
    reasons: list[str] = []
    done_ids = {i.id for i in items if i.status == "done"} | merged
    eligible: list[Item] = []
    for it in items:
        st = state.get("items", {}).get(it.id, {})
        if it.id in merged:
            reasons.append(f"{it.id}: merged into {st.get('merged_branch') or 'dev'}")
            continue
        if it.status != "open":
            reasons.append(f"{it.id}: status {it.status}")
            continue
        missing_deps = [d for d in it.depends_on if d not in done_ids]
        if missing_deps:
            reasons.append(f"{it.id}: waiting on {', '.join(missing_deps)}")
            continue
        if st.get("pending_branch") and it.id not in merged:
            reasons.append(f"{it.id}: pull request pending on {st['pending_branch']}")
            continue
        if st.get("exhausted"):
            reasons.append(f"{it.id}: parked after {st.get('last_outcome', 'failure')} — "
                           "revise the item text to retry")
            continue
        if st.get("attempts", 0) >= it.max_attempts:
            reasons.append(f"{it.id}: {st['attempts']} attempts used (max {it.max_attempts})")
            continue
        miss = requirements_missing(it, env)
        if miss:
            reasons.append(f"{it.id}: requires missing {', '.join(miss)}")
            continue
        eligible.append(it)
    if not eligible:
        return None, reasons
    if prefer:
        for it in eligible:
            if it.id == prefer:
                return it, reasons
        reasons.append(f"{prefer}: preferred but not eligible")
    eligible.sort(key=lambda i: (i.priority, i.id))
    return eligible[0], reasons


# ------------------------------------------------------------------ pure checks


def paths_in_scope(paths: list[str], scope: tuple) -> list[str]:
    """Return the changed paths that match none of the scope globs (``**`` = any depth)."""
    out = []
    for p in paths:
        ok = False
        for g in scope:
            if g.endswith("/**"):
                base = g[:-3]
                if p == base or p.startswith(base + "/"):
                    ok = True
                    break
            if fnmatch.fnmatchcase(p, g) or fnmatch.fnmatchcase(p, g.replace("**", "*")):
                ok = True
                break
        if not ok:
            out.append(p)
    return out


CHECK_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed, (?P<skipped>\d+) skipped, (?P<failed>\d+) failed, (?P<broken>\d+) broken")


def parse_check_summary(output: str) -> dict | None:
    """The last ``N passed, N skipped, N failed, N broken`` line of ``phicad check``."""
    found = None
    for m in CHECK_SUMMARY_RE.finditer(output):
        found = {k: int(v) for k, v in m.groupdict().items()}
    return found


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def assert_no_secret(argv: list[str]) -> None:
    for a in argv:
        for pat in SECRET_PATTERNS:
            if pat.search(a):
                raise RuntimeError("refusing to pass a credential-shaped argument on a command line")


# ------------------------------------------------------------------ configuration


@dataclass
class Config:
    """Everything an iteration needs, from environment variables with safe defaults."""

    home: Path
    repo: Path
    branch: str = "dev"
    runner: str = "phi"
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    kicad_cli: str = ""
    build: str = ""
    breaker: int = 3
    max_min: int = 45
    nice: tuple = ("nice", "-n", "15")
    phi_bin: str = "phi"
    dry_run: bool = False
    probe: bool = True
    probe_timeout: int = 120
    max_cost_usd: float = 25.0      # one AI step; the phi runner is killed when it crosses this
    poll_sec: float = 15.0          # how often the running phi step is checked against the caps
    daily_cap_usd: float = 100.0    # rolling 24 h over recorded runs; nothing starts above it

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        home = Path(env.get("PHICAD_LOOP_HOME") or Path(env.get("HOME", "~")).expanduser() / "phicad-loop")
        repo = Path(env.get("PHICAD_LOOP_REPO") or home / "repo")
        models = dict(DEFAULT_MODELS)
        for part in env.get("PHICAD_LOOP_MODELS", "").split(","):
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip() in TIERS and v.strip():
                    models[k.strip()] = v.strip()
        nice = ("nice", "-n", "15") if shutil.which("nice") else ()
        if shutil.which("ionice") and sys.platform.startswith("linux"):
            nice = nice + ("ionice", "-c3")
        return cls(
            home=home, repo=repo,
            branch=env.get("PHICAD_LOOP_BRANCH", "dev"),
            runner=env.get("PHICAD_LOOP_RUNNER", "phi"),
            models=models,
            kicad_cli=env.get("PHICAD_KICAD_CLI", ""),
            build=env.get("PHICAD_BUILD", ""),
            breaker=int(env.get("PHICAD_LOOP_BREAKER", "3")),
            max_min=int(env.get("PHICAD_LOOP_MAX_MIN", "45")),
            nice=nice,
            phi_bin=env.get("PHICAD_LOOP_PHI", "phi"),
            probe=env.get("PHICAD_LOOP_PROBE", "1") not in ("0", "no", "false"),
            probe_timeout=int(env.get("PHICAD_LOOP_PROBE_TIMEOUT", "120")),
            max_cost_usd=float(env.get("PHICAD_LOOP_MAX_COST_USD", "25")),
            daily_cap_usd=float(env.get("PHICAD_LOOP_DAILY_CAP_USD", "100")),
        )

    # layout
    @property
    def backlog_dir(self) -> Path:
        return self.repo / "phicad" / "loop" / "backlog"

    @property
    def contract(self) -> Path:
        return self.repo / "phicad" / "loop" / "loop-contract.md"

    @property
    def worktrees(self) -> Path:
        return self.home / "wt"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def outbox(self) -> Path:
        return self.home / "outbox"

    @property
    def control(self) -> Path:
        return self.home / "control"

    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    def ensure_layout(self) -> None:
        for d in (self.home, self.worktrees, self.runs, self.outbox, self.control):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)


def load_state(cfg: Config) -> dict:
    if cfg.state_file.exists():
        try:
            return json.loads(cfg.state_file.read_text())
        except json.JSONDecodeError:
            pass
    return {"items": {}, "runs": [], "consecutive_failures": 0}


def save_state(cfg: Config, state: dict) -> None:
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, cfg.state_file)


class Lock:
    """One iteration at a time per host; a lock whose pid is dead is stale and taken over."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (ValueError, OSError):
                    pid = 0
                if pid and _pid_alive(pid):
                    raise SystemExit(f"another iteration is running (pid {pid}); lock {self.path}")
                self.path.unlink(missing_ok=True)
        raise SystemExit(f"could not take lock {self.path}")

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ------------------------------------------------------------------ subprocess + git


def clean_env(extra: dict | None = None) -> dict:
    """The child environment: proxies stripped (they point at the LLM gateway and break git),
    ~/.local/bin on PATH (non-interactive ssh has no profile), the build overrides passed on."""
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    home = env.get("HOME", str(Path.home()))
    env["PATH"] = f"{home}/.local/bin:" + env.get("PATH", "/usr/bin:/bin")
    env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: int | None = None,
        check: bool = False, log: Path | None = None) -> subprocess.CompletedProcess:
    assert_no_secret(cmd)
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env or clean_env(),
                          capture_output=True, text=True, timeout=timeout)
    if log is not None:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"$ {shlex.join(cmd)}\n{proc.stdout}{proc.stderr}[exit {proc.returncode}]\n\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{shlex.join(cmd)} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return proc


def git(repo: Path, *args: str, check: bool = True, log: Path | None = None, timeout: int = 600):
    return run(["git", "-C", str(repo), *args], check=check, log=log, timeout=timeout)


def sync_repo(cfg: Config, log: Path) -> str:
    """Bring the loop's own checkout to origin/<branch> (this updates the loop code and the
    backlog themselves — the loop improves with dev).  Returns the new HEAD."""
    git(cfg.repo, "fetch", "--prune", "origin", log=log)
    git(cfg.repo, "checkout", "-q", "-B", cfg.branch, f"origin/{cfg.branch}", log=log)
    return git(cfg.repo, "rev-parse", "HEAD", log=log).stdout.strip()


def merged_items(cfg: Config, state: dict) -> set:
    """Items whose pending loop branch is now an ancestor of origin/<branch> (merged).

    The pushed tip SHA is tested first: GitHub deletes the head branch on merge, so by the time
    the loop looks, `origin/<branch>` is usually gone while the commit itself is reachable from
    `dev`. Only when the SHA is not an ancestor AND the branch is gone was the attempt rejected."""
    merged = set()
    for item_id, st in state.get("items", {}).items():
        br = st.get("pending_branch")
        if not br:
            continue
        sha = st.get("pending_sha")
        if sha and git(cfg.repo, "merge-base", "--is-ancestor", sha, f"origin/{cfg.branch}",
                       check=False).returncode == 0:
            merged.add(item_id)
            continue
        r = git(cfg.repo, "merge-base", "--is-ancestor", f"origin/{br}", f"origin/{cfg.branch}", check=False)
        if r.returncode == 0:
            merged.add(item_id)
        elif r.returncode != 1:
            # the branch is gone from origin without merging: the attempt was rejected
            st.pop("pending_branch", None)
            st.pop("pending_sha", None)
            st["last_outcome"] = "branch-removed"
    for item_id in merged:
        st = state["items"][item_id]
        st["merged_branch"] = st.pop("pending_branch")
        st["merged_at"] = utcnow()
    return merged


def new_worktree(cfg: Config, item: Item, run_id: str, log: Path) -> tuple[Path, str]:
    """Fresh worktree off origin/<branch> on ``loop/<item>-<run_id>`` — one branch per attempt,
    never reused, never force-pushed; the previous attempt's worktree directory is recycled."""
    wt = cfg.worktrees / item.id
    if wt.exists():
        git(cfg.repo, "worktree", "remove", "--force", str(wt), check=False, log=log)
        shutil.rmtree(wt, ignore_errors=True)
    git(cfg.repo, "worktree", "prune", log=log)
    branch = f"loop/{item.id}-{run_id}"
    git(cfg.repo, "worktree", "add", "-q", "-b", branch, str(wt), f"origin/{cfg.branch}", log=log)
    # The agent's scratch files (.phicad-loop/pr-body.md, blocked.md) must never be committed,
    # and `git status` in the worktree must stay clean for the agent: exclude them per-worktree.
    exclude = Path(git(wt, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    if not exclude.is_absolute():
        exclude = wt / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        fh.write(".phicad-loop/\n")
    (wt / ".phicad-loop").mkdir(exist_ok=True)
    return wt, branch


def changed_paths(wt: Path, base: str) -> list[str]:
    out = git(wt, "diff", "--name-only", f"{base}...HEAD").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_count(wt: Path, base: str) -> int:
    return int(git(wt, "rev-list", "--count", f"{base}..HEAD").stdout.strip() or 0)


def worktree_dirty(wt: Path) -> bool:
    return bool(git(wt, "status", "--porcelain", "--untracked-files=all").stdout.strip())


# ------------------------------------------------------------------ the brief


def compose_brief(item: Item, *, worktree: Path, branch: str, base: str, model: str,
                  gates: list[str], kicad_cli: str = "") -> str:
    """The single user message the implementing agent receives.  Everything the model must
    know that is not in the repo's AGENTS.md: the item, the exact worktree/branch, the scope
    it must stay inside, the gates that will be run on its output, and how to finish."""
    scope = "\n".join(f"  - {g}" for g in item.scope)
    gate_lines = "\n".join(f"  - `{g}`" for g in gates) or "  - (none beyond the standard gates)"
    cli_note = (f"\nA built fork `kicad-cli` is available at `{kicad_cli}` (set as PHICAD_KICAD_CLI; "
                "call it with KICAD_RUN_FROM_BUILD_DIR=1)." if kicad_cli else
                "\nNo built kicad-cli is available in this environment; do not attempt a C++ build.")
    return f"""# PhiCad continuous development loop — item `{item.id}`

## {item.title}

{item.body.strip()}

## Where you are

- Worktree: `{worktree}` (branch `{branch}`, based on `{base}`). Work ONLY here.
- Model: `{model}`. Tier: {item.tier}. Wall-clock budget: {item.timeout_min} min — plan for it.
- Allowed paths (every changed file must match one of these globs; anything else fails the run):
{scope}{cli_note}

## Gates that will be run on your branch after you finish (make them pass before you stop)

  - `phicad/bin/phicad check fast`
  - `python3 phicad/bin/check_fork_boundary.py --base {base} --committed-only`
{gate_lines}

## How to finish

1. Commit small, as you go (component-prefixed subject, `Assisted-by: phi ({model}) via phicad loop` trailer).
   Leave the working tree clean — uncommitted work is autosaved with a flag and counts against you.
2. Update the documentation your change invalidates (README/docs-site page), in the same branch.
3. Write `.phicad-loop/pr-body.md` in the worktree: first line = PR title, blank line, then the body
   (what changed, why, how it was verified, limitations). It is used verbatim for the pull request.
4. Do NOT push, do NOT open a PR, do NOT merge, do NOT touch `master` or `dev`; the loop does the rest.
5. If the item cannot be done as written, write the reason to `.phicad-loop/blocked.md` and stop
   without committing half work.
"""


# ------------------------------------------------------------------ runners


PROBE_PROMPT = "Reply with exactly TIER_OK and nothing else."


def probe_model(cfg: Config, model: str, env: dict, run_dir: Path, timeout: int = 120) -> tuple[bool, float]:
    """One tool-less `phi -p` call: does this model reference answer at all right now?

    Why: a gateway alias has been seen to sit silent for minutes on every host
    while the other aliases answered in seconds. `phicad models check` cannot see that (it only
    verifies the alias is served), and an unattended loop that trusts the tier table would burn
    the whole item budget on nothing. Returns (answered, seconds).
    """
    argv = [cfg.phi_bin, "-p", "--mode", "json", "--model", model, "--no-session", "--no-tools", PROBE_PROMPT]
    assert_no_secret(argv)
    events = run_dir / f"probe-{model.replace('/', '_').replace(':', '_')}.jsonl"
    end = window_deadline(cfg.home)
    if end is not None:
        timeout = min(timeout, max(0.0, end - time.time()))
    started = time.time()
    with open(events, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(list(cfg.nice) + argv, cwd=str(run_dir), env=env, stdout=out,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            return False, round(time.time() - started, 1)
    text, _ = summarize_phi_events(events)
    return "TIER_OK" in text, round(time.time() - started, 1)


def choose_model(cfg: Config, item: Item, env: dict, run_dir: Path) -> tuple[str, list[dict]]:
    """The item's tier model if it answers the probe, else the nearest tier that does
    (higher tier preferred on ties). Returns (model or "", probe records)."""
    tiers = list(TIERS)
    want = tiers.index(item.tier)
    order = sorted(tiers, key=lambda t: (abs(tiers.index(t) - want), -tiers.index(t)))
    tried: list[dict] = []
    seen: set = set()
    for tier in order:
        model = cfg.models[tier]
        if model in seen:
            continue
        seen.add(model)
        if not cfg.probe:
            return model, tried
        ok, secs = probe_model(cfg, model, env, run_dir, timeout=cfg.probe_timeout)
        tried.append({"tier": tier, "model": model, "answered": ok, "seconds": secs})
        if ok:
            return model, tried
    return "", tried


def run_phi(cfg: Config, item: Item, brief: str, model: str, wt: Path, run_dir: Path,
            env: dict) -> dict:
    """Runner ``phi``: one non-interactive phi turn in the worktree.  The credential is
    whatever phi already holds for this user — nothing is passed on the command line."""
    events = run_dir / "phi-events.jsonl"
    argv = [cfg.phi_bin, "-p", "--mode", "json", "--model", model, "--no-session",
            "--append-system-prompt", str(cfg.contract), brief]
    assert_no_secret(argv)
    (run_dir / "phi-argv.txt").write_text(shlex.join(argv[:-1]) + " <brief>\n")
    timeout = min(item.timeout_min, cfg.max_min) * 60
    end = window_deadline(cfg.home)
    if end is not None:
        timeout = min(timeout, max(0.0, end - time.time()))
    started = time.time()
    outcome = "finished"
    meter = CostMeter(events)
    with open(events, "w", encoding="utf-8") as out, open(run_dir / "phi-stderr.log", "w") as err:
        proc = subprocess.Popen(list(cfg.nice) + argv, cwd=str(wt), env=env, stdout=out, stderr=err,
                                text=True, start_new_session=True)
        rc = None
        while rc is None:
            try:
                rc = proc.wait(timeout=min(cfg.poll_sec, max(0.2, timeout - (time.time() - started))))
            except subprocess.TimeoutExpired:
                if time.time() - started >= timeout:
                    outcome = "timeout"
                elif meter.feed() > cfg.max_cost_usd:
                    outcome = "budget-exceeded"
                else:
                    continue
                try:
                    os.killpg(proc.pid, 15)
                    rc = proc.wait(timeout=30)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    os.killpg(proc.pid, 9)
                    rc = -9
    final_text, usage = summarize_phi_events(events)
    if usage.get("cost_total", 0.0) > cfg.max_cost_usd:
        outcome = "budget-exceeded"
    killed = outcome in ("timeout", "budget-exceeded")
    return {"runner": "phi", "outcome": outcome if rc == 0 or killed else f"exit-{rc}",
            "exit": rc, "seconds": round(time.time() - started), "final_text": final_text[-4000:],
            "usage": usage, "cost_usd": round(usage.get("cost_total", 0.0), 4)}


def _usage_event(ev: dict) -> dict | None:
    """The assistant message whose usage counts, or None.

    ``phi --mode json`` streams one ``message_start``, thousands of ``message_update`` and one
    ``message_end`` per assistant message, each carrying the *running* usage of that message,
    and ``turn_end`` repeats it once more.  Only ``message_end`` is billed here; counting every
    event inflated one 50-message run from $3.35 to $263.  An untyped stream (plain message
    dumps) is counted once per assistant message.
    """
    kind = ev.get("type")
    if kind is not None and kind != "message_end":
        return None
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
    return msg if msg.get("role") == "assistant" else None


def _event_cost(msg: dict) -> float:
    u = msg.get("usage")
    if not isinstance(u, dict):
        return 0.0
    c = u.get("cost")
    if isinstance(c, dict):
        return float(c.get("total", 0) or 0)
    return float(c) if isinstance(c, (int, float)) else 0.0


class CostMeter:
    """Running USD total of a live ``phi --mode json`` stream, read incrementally.

    The stream of a long run reaches ~100 MB, so each tick reads only what was appended since
    the last one; a trailing partial line is kept until its newline arrives.
    """

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.cost = 0.0

    def feed(self) -> float:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return self.cost
        self.offset += len(chunk)
        lines = (self.partial + chunk.decode("utf-8", errors="replace")).split("\n")
        self.partial = lines.pop()
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = _usage_event(ev) if isinstance(ev, dict) else None
            if msg is not None:
                self.cost += _event_cost(msg)
        return self.cost


def summarize_phi_events(path: Path) -> tuple[str, dict]:
    """Best-effort read of ``phi --mode json`` output: last assistant text + token usage."""
    final = ""
    usage: dict = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return final, usage
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        msg = _usage_event(ev)
        if msg is None:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            final = content
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                final = "\n".join(texts)
        u = msg.get("usage")
        if isinstance(u, dict):
            for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                v = u.get(k)
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
            usage["cost_total"] = usage.get("cost_total", 0.0) + _event_cost(msg)
    return final, usage


def spent_last_24h(state: dict, now: float | None = None) -> float:
    """USD recorded for runs that started in the last 24 hours (``runs.jsonl`` mirror in state)."""
    now = time.time() if now is None else now
    total = 0.0
    for r in state.get("runs", []):
        at = r.get("at") or ""
        try:
            t = _dt.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        if now - t.timestamp() <= 86400:
            total += float(r.get("cost_usd") or 0)
    return total


# ------------------------------------------------------------------ one iteration


def gates_for(item: Item) -> list[str]:
    return list(item.gates)


def run_gates(cfg: Config, item: Item, wt: Path, base: str, run_dir: Path, env: dict) -> dict:
    """The deterministic judge: scope, fork boundary, ``check fast``, the item's own gates."""
    log = run_dir / "gates.log"
    results: dict = {"ok": True, "checks": []}

    def record(name: str, ok: bool, detail: str = ""):
        results["checks"].append({"name": name, "ok": ok, "detail": detail[-800:]})
        if not ok:
            results["ok"] = False

    paths = changed_paths(wt, base)
    results["changed_paths"] = paths
    off = paths_in_scope(paths, item.scope)
    record("scope", not off, "out of scope: " + ", ".join(off) if off else f"{len(paths)} path(s) in scope")
    r = run(["python3", "phicad/bin/check_fork_boundary.py", "--base", base, "--committed-only"],
            cwd=wt, env=env, log=log, timeout=300)
    record("fork boundary", r.returncode == 0, (r.stdout + r.stderr).strip())
    r = run(list(cfg.nice) + ["phicad/bin/phicad", "check", "fast"], cwd=wt, env=env, log=log, timeout=1800)
    summary = parse_check_summary(r.stdout + r.stderr) or {}
    ok = r.returncode == 0 and summary.get("failed", 1) == 0 and summary.get("broken", 1) == 0
    record("phicad check fast", ok, json.dumps(summary) if summary else (r.stdout + r.stderr)[-600:])
    for g in gates_for(item):
        try:
            r = run(list(cfg.nice) + ["bash", "-lc", g], cwd=wt, env=env, log=log, timeout=1800)
            record(f"gate: {g}", r.returncode == 0, (r.stdout + r.stderr).strip())
        except subprocess.TimeoutExpired:
            record(f"gate: {g}", False, "timed out after 1800 s")
    return results


def read_pr_body(wt: Path, item: Item, run_id: str, gates: dict) -> tuple[str, str]:
    body_file = wt / ".phicad-loop" / "pr-body.md"
    title = f"phicad: {item.title}"
    body = ""
    if body_file.exists():
        text = body_file.read_text(encoding="utf-8").strip()
        first, _, rest = text.partition("\n")
        if first.strip():
            title = first.strip().lstrip("# ").strip()
        body = rest.strip()
    checks = "\n".join(f"- {'✅' if c['ok'] else '❌'} {c['name']}" for c in gates.get("checks", []))
    body += (f"\n\n---\n*Produced by the PhiCad continuous development loop* — item `{item.id}`, run `{run_id}`.\n"
             f"Gates run by the loop after the agent finished:\n{checks}\n")
    return title, body.strip()


def flip_status(wt: Path, item: Item, branch: str, log: Path) -> None:
    """On the branch, mark the item done so the merge itself closes it in the backlog."""
    target = wt / "phicad" / "loop" / "backlog" / f"{item.id}.md"
    if not target.exists():
        raise FileNotFoundError(f"backlog entry {target} is missing on the branch")
    fresh = parse_item(target.read_text(encoding="utf-8"), target)
    fresh.status = "done"
    fresh.branch = branch
    target.write_text(render_item(fresh), encoding="utf-8")
    git(wt, "add", str(target.relative_to(wt)), log=log)
    git(wt, "-c", "user.name=PhiCad loop", "-c", "user.email=phicad-loop@invalid", "commit", "-q", "-m",
        f"phicad: loop — mark {item.id} done\n\nThe backlog entry closes when this branch merges into dev.",
        log=log)


def autosave(wt: Path, item: Item, model: str, log: Path) -> bool:
    if not worktree_dirty(wt):
        return False
    # .phicad-loop/ is excluded per-worktree (new_worktree), so `add -A` cannot pick it up.
    git(wt, "add", "-A", log=log)
    git(wt, "-c", "user.name=PhiCad loop", "-c", "user.email=phicad-loop@invalid", "commit", "-q", "-m",
        f"phicad: {item.id} — autosaved uncommitted work\n\nThe implementing agent left these changes "
        f"uncommitted; the loop committed them so nothing is lost.\n\nAssisted-by: phi ({model}) via phicad loop",
        log=log)
    return True


def gh_available(env: dict) -> bool:
    if not shutil.which("gh", path=env.get("PATH")):
        return False
    r = run(["gh", "auth", "status"], env=env, timeout=30)
    return r.returncode == 0


def open_pr(wt: Path, branch: str, base: str, title: str, body: str, run_dir: Path, env: dict) -> str:
    body_file = run_dir / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    r = run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file)],
            cwd=wt, env=env, timeout=120, log=run_dir / "ship.log")
    if r.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {r.stderr[-500:]}")
    return r.stdout.strip().splitlines()[-1]


def write_status(cfg: Config, state: dict, items: list[Item], reasons: list[str], picked: str, note: str) -> None:
    lines = [f"# PhiCad dev loop — {cfg.home.name} on {os.uname().nodename}", "",
             f"Updated {utcnow()} · runner `{cfg.runner}` · branch `{cfg.branch}` · repo `{cfg.repo}`", "",
             f"**Last iteration:** {note}", ""]
    if (cfg.control / "PAUSE").exists():
        lines += ["**PAUSED** — `control/PAUSE` exists.", ""]
    if (cfg.control / "BREAKER").exists():
        why = (cfg.control / "BREAKER").read_text().strip()
        lines += [f"**BREAKER OPEN** — {why} (remove `control/BREAKER` to resume)", ""]
    lines += ["| item | status | prio | tier | attempts | pending branch / merged |",
              "| --- | --- | --- | --- | --- | --- |"]
    for it in sorted(items, key=lambda i: (i.priority, i.id)):
        st = state.get("items", {}).get(it.id, {})
        br = st.get("pending_branch") or (f"merged: {st['merged_branch']}" if st.get("merged_branch") else "")
        mark = " ◀" if it.id == picked else ""
        lines.append(f"| `{it.id}`{mark} | {it.status} | {it.priority} | {it.tier} | {st.get('attempts', 0)} | {br} |")
    if reasons:
        lines += ["", "Skipped this round:", ""] + [f"- {r}" for r in reasons]
    runs = state.get("runs", [])[-10:]
    if runs:
        lines += ["", f"Recent runs (${spent_last_24h(state):.2f} in the last 24 h):", "",
                  "| run | item | outcome | gates | cost | pr |", "| --- | --- | --- | --- | --- | --- |"]
        for r in reversed(runs):
            lines.append(f"| {r['run_id']} | `{r['item']}` | {r['outcome']} | {r.get('gates', '')} | "
                         f"${float(r.get('cost_usd') or 0):.2f} | {r.get('pr', '')} |")
    (cfg.home / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_run(cfg: Config, state: dict, rec: dict) -> None:
    keep = ("run_id", "item", "outcome", "gates", "pr", "branch", "at", "cost_usd")
    state.setdefault("runs", []).append({k: rec.get(k) for k in keep})
    state["runs"] = state["runs"][-200:]
    with open(cfg.home / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    save_state(cfg, state)


def iterate(cfg: Config, *, item_id: str = "", runner: str = "") -> int:
    cfg.ensure_layout()
    runner = runner or cfg.runner
    with Lock(cfg.home / "loop.lock"):
        try:
            end = window_deadline(cfg.home)
        except (ValueError, KeyError, OSError) as exc:
            (cfg.control / "PAUSE").touch()
            print(f"run window blocked: {exc}")
            return 2
        if end is not None:
            if runner != "phi":
                print("run window requires runner phi")
                return 2
            # Scoped to this invocation only. Normal env/default caps are never rewritten.
            cfg = replace(cfg, max_cost_usd=float("inf"), daily_cap_usd=float("inf"))
        if (cfg.control / "PAUSE").exists():
            print("paused: control/PAUSE exists")
            return 0
        if (cfg.control / "BREAKER").exists() and not item_id:
            print("breaker open: " + (cfg.control / "BREAKER").read_text().strip())
            return 0
        run_id = stamp()
        run_dir = cfg.runs / run_id
        n = 0
        while run_dir.exists():          # two iterations inside one second (tests, manual re-runs)
            n += 1
            run_id = f"{stamp()}-{n}"
            run_dir = cfg.runs / run_id
        run_dir.mkdir(parents=True)
        log = run_dir / "loop.log"
        state = load_state(cfg)
        head = sync_repo(cfg, log)
        items = load_backlog(cfg.backlog_dir)
        problems = validate_backlog(items)
        if problems:
            print("backlog invalid:\n  " + "\n  ".join(problems))
            return 2
        merged = merged_items(cfg, state)
        for rid in reset_revised(items, state):
            print(f"{rid}: item text changed since its last attempt — attempts reset")
        env = clean_env({"PHICAD_KICAD_CLI": cfg.kicad_cli, "PHICAD_BUILD": cfg.build, "PHICAD_LOOP_RUN": run_id})
        prefer = item_id or ((cfg.control / "NEXT").read_text().strip() if (cfg.control / "NEXT").exists() else "")
        if item_id:
            picked = next((i for i in items if i.id == item_id), None)
            reasons = [] if picked else [f"{item_id}: not in backlog"]
        else:
            picked, reasons = pick_item(items, state, prefer=prefer, env=env, merged=merged)
        if picked is None:
            note = "nothing eligible"
            write_status(cfg, state, items, reasons, "", note)
            save_state(cfg, state)
            print(note + ("\n  " + "\n  ".join(reasons) if reasons else ""))
            return 0
        spent = spent_last_24h(state)
        if spent >= cfg.daily_cap_usd and not item_id:
            note = f"daily budget reached: ${spent:.2f} recorded in the last 24 h (cap ${cfg.daily_cap_usd:.2f})"
            write_status(cfg, state, items, reasons, "", note)
            save_state(cfg, state)
            print(note)
            return 0
        if (cfg.control / "NEXT").exists() and picked.id == prefer:
            (cfg.control / "NEXT").unlink()
        model = picked.model(cfg.models)
        st = state.setdefault("items", {}).setdefault(picked.id, {"attempts": 0})
        if cfg.dry_run:
            # no attempt consumed, no run recorded: only the merged-branch bookkeeping above persists
            save_state(cfg, state)
            shutil.rmtree(run_dir, ignore_errors=True)
            print(f"[dry-run] would take {picked.id} ({picked.tier} -> {model}) "
                  f"attempt {st.get('attempts', 0) + 1}/{picked.max_attempts} runner={runner}")
            print(compose_brief(picked, worktree=cfg.worktrees / picked.id, branch="loop/<dry-run>",
                                base=f"origin/{cfg.branch}", model=model, gates=gates_for(picked),
                                kicad_cli=cfg.kicad_cli))
            return 0
        st["attempts"] = st.get("attempts", 0) + 1
        st["last_run"] = run_id
        st["digest"] = picked.digest
        save_state(cfg, state)
        print(f"[{run_id}] item {picked.id} ({picked.tier} -> {model}) "
              f"attempt {st['attempts']}/{picked.max_attempts} runner={runner}")
        wanted = model
        model, probes = choose_model(cfg, picked, env, run_dir)
        rec: dict = {"run_id": run_id, "item": picked.id, "base_head": head, "model": model or wanted,
                     "model_wanted": wanted, "probes": probes, "runner": runner, "at": utcnow()}
        if not model:
            # every tier's model is silent: an infrastructure failure, not the item's fault
            st["attempts"] -= 1
            rec.update({"outcome": "runner-error", "error": "no model answered the liveness probe",
                        "branch": "", "gates": "", "pr": ""})
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            if state["consecutive_failures"] >= cfg.breaker:
                (cfg.control / "BREAKER").write_text(
                    f"{state['consecutive_failures']} consecutive failing iterations (last: no model answered)\n")
            (run_dir / "summary.json").write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
            record_run(cfg, state, rec)
            write_status(cfg, state, items, reasons, picked.id, f"run {run_id}: no model answered the probe")
            print(f"[{run_id}] {picked.id}: runner-error — no model answered the probe "
                  + ", ".join(f"{p['model']}={'ok' if p['answered'] else 'silent'}" for p in probes))
            return 0
        if model != wanted:
            print(f"[{run_id}] {wanted} did not answer the probe; using {model}")
        wt, branch = new_worktree(cfg, picked, run_id, log)
        base = f"origin/{cfg.branch}"
        brief = compose_brief(picked, worktree=wt, branch=branch, base=base, model=model,
                              gates=gates_for(picked), kicad_cli=cfg.kicad_cli)
        (run_dir / "brief.md").write_text(brief, encoding="utf-8")
        rec.update({"branch": branch, "worktree": str(wt)})
        try:
            ai = run_phi(cfg, picked, brief, model, wt, run_dir, env)
        except Exception as exc:  # infrastructure failure of the runner itself
            ai = {"runner": runner, "outcome": "runner-error", "error": redact(str(exc))[:800]}
        rec["ai"] = ai
        rec["cost_usd"] = ai.get("cost_usd", 0.0)
        blocked = wt / ".phicad-loop" / "blocked.md"
        rec["autosaved"] = autosave(wt, picked, model, log)
        n = commit_count(wt, base)
        rec["commits"] = n
        outcome = "rejected"
        pr = ""
        if ai.get("outcome") == "budget-exceeded":
            # Work killed mid-flight is never shipped, even if what exists would pass the gates:
            # the WIP is autosaved on the local branch for a human to look at.
            outcome = "budget-exceeded"
        elif blocked.exists() and n == 0:
            outcome = "agent-blocked"
            rec["blocked_reason"] = blocked.read_text(encoding="utf-8")[:2000]
        elif n == 0:
            outcome = "no-change"
        else:
            gates = run_gates(cfg, picked, wt, base, run_dir, env)
            rec["gates_detail"] = gates
            failed = [c["name"] for c in gates["checks"] if not c["ok"]]
            rec["gates"] = "pass" if gates["ok"] else "FAIL: " + ", ".join(failed)
            if gates["ok"]:
                flip_status(wt, picked, branch, log)
            # push whatever exists — a rejected branch is evidence, not garbage
            r = git(wt, "push", "-q", "origin", branch, check=False, log=log)
            rec["pushed"] = r.returncode == 0
            if gates["ok"] and r.returncode == 0:
                title, body = read_pr_body(wt, picked, run_id, gates)
                rec["pr_title"] = title
                if gh_available(env):
                    try:
                        pr = open_pr(wt, branch, cfg.branch, title, body, run_dir, env)
                        outcome = "pr-opened"
                    except RuntimeError as exc:
                        rec["ship_error"] = str(exc)
                        outcome = "pushed-pr-failed"
                else:
                    (cfg.outbox / f"{branch.replace('/', '__')}.json").write_text(json.dumps(
                        {"branch": branch, "base": cfg.branch, "title": title, "body": body, "item": picked.id,
                         "run_id": run_id, "at": utcnow()}, indent=2), encoding="utf-8")
                    outcome = "pushed-pr-queued"
                st["pending_branch"] = branch
                st["pending_sha"] = git(wt, "rev-parse", "HEAD").stdout.strip()
            elif not gates["ok"]:
                outcome = "rejected"
        rec["outcome"] = outcome
        rec["pr"] = pr
        st["last_outcome"] = outcome
        if outcome in ("rejected", "runner-error", "no-change", "agent-blocked", "pushed-pr-failed"):
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        else:
            state["consecutive_failures"] = 0
        if state["consecutive_failures"] >= cfg.breaker:
            (cfg.control / "BREAKER").write_text(
                f"{state['consecutive_failures']} consecutive failing iterations "
                f"(last: {picked.id} {outcome} run {run_id})\n")
        shipped = outcome in ("pr-opened", "pushed-pr-queued")
        if outcome == "agent-blocked" or (st["attempts"] >= picked.max_attempts and not shipped):
            st["exhausted"] = True
        (run_dir / "summary.json").write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        record_run(cfg, state, rec)
        note = f"run {run_id}: `{picked.id}` → **{outcome}**"
        if rec.get("gates"):
            note += f" ({rec['gates']})"
        if pr:
            note += f" {pr}"
        write_status(cfg, state, items, reasons, picked.id, note)
        print(f"[{run_id}] {picked.id}: {outcome} commits={n} gates={rec.get('gates', '-')} "
              f"branch={branch} pr={pr or '-'}")
        return 0


# ------------------------------------------------------------------ laptop side: ship queued PRs


def ship_from(host: str, *, auto_merge: bool = False, repo_slug: str = "couplet-physical-ai/PhiCad",
              loop_home: str = "~/phicad-loop", base: str = "") -> int:
    """Open pull requests for branches the remote loop pushed but could not ship (no `gh`
    login on the host).  Runs where `gh` IS logged in — the laptop — and marks each outbox
    entry shipped on the host so it is not opened twice.

    `base` overrides the base branch the host recorded: while the host tracks a feature branch
    (installer `--branch`), the work it produces should still be reviewed against `dev`, stacked
    on that feature branch's own pull request."""
    env = clean_env()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host]
    list_cmd = f'cd {loop_home}/outbox 2>/dev/null && for f in *.json; do [ -f "$f" ] && cat "$f" && echo; done'
    r = run(ssh + [list_cmd], env=env, timeout=60)
    entries = []
    for chunk in r.stdout.split("\n}\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not chunk.endswith("}"):
            chunk += "\n}"
        try:
            entries.append(json.loads(chunk))
        except json.JSONDecodeError:
            continue
    if not entries:
        print("outbox empty")
        return 0
    for e in entries:
        body = Path(os.environ.get("TMPDIR", "/tmp")) / f"phicad-loop-{e['branch'].replace('/', '__')}.md"
        body.write_text(e["body"], encoding="utf-8")
        exists = run(["gh", "pr", "list", "--repo", repo_slug, "--head", e["branch"], "--state", "all",
                      "--json", "url", "-q", ".[0].url"], env=env, timeout=60)
        url = exists.stdout.strip()
        if not url:
            cr = run(["gh", "pr", "create", "--repo", repo_slug, "--base", base or e.get("base", "dev"),
                      "--head", e["branch"], "--title", e["title"], "--body-file", str(body)], env=env, timeout=120)
            if cr.returncode != 0:
                print(f"{e['branch']}: gh pr create failed: {cr.stderr.strip()[-300:]}")
                continue
            url = cr.stdout.strip().splitlines()[-1]
        print(f"{e['branch']}: {url}")
        if auto_merge:
            mr = run(["gh", "pr", "merge", url, "--repo", repo_slug, "--merge", "--auto", "--delete-branch"],
                     env=env, timeout=120)
            print(f"  auto-merge {'enabled' if mr.returncode == 0 else 'refused: ' + mr.stderr.strip()[-200:]}")
        fname = f"{e['branch'].replace('/', '__')}.json"
        mark = (f"cd {loop_home}/outbox && mkdir -p shipped && python3 -c \"import json;p='{fname}';"
                f"d=json.load(open(p));d['pr']='{url}';json.dump(d,open('shipped/'+p,'w'),indent=2)\" "
                f"&& rm -f {fname}")
        run(ssh + [mark], env=env, timeout=60)
    return 0


# ------------------------------------------------------------------ CLI


def cmd_status(cfg: Config, as_json: bool) -> int:
    cfg.ensure_layout()
    state = load_state(cfg)
    items = load_backlog(cfg.backlog_dir) if cfg.backlog_dir.exists() else []
    if as_json:
        print(json.dumps({"home": str(cfg.home), "repo": str(cfg.repo), "runner": cfg.runner,
                          "paused": (cfg.control / "PAUSE").exists(), "breaker": (cfg.control / "BREAKER").exists(),
                          "items": [{"id": i.id, "status": i.status, "priority": i.priority, "tier": i.tier,
                                     **state.get("items", {}).get(i.id, {})} for i in items],
                          "runs": state.get("runs", [])[-10:]}, indent=2))
        return 0
    if (cfg.home / "STATUS.md").exists():
        print((cfg.home / "STATUS.md").read_text())
    else:
        write_status(cfg, state, items, [], "", "no iteration yet")
        print((cfg.home / "STATUS.md").read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loop.py", description=__doc__.split("\n\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("once", help="run one iteration (pick, implement, gate, push, ship, record)")
    s.add_argument("--item", default="", help="force this backlog item (bypasses pick order and the breaker)")
    s.add_argument("--runner", choices=("phi",), default="", help="override PHICAD_LOOP_RUNNER")
    s.add_argument("--dry-run", action="store_true", help="pick and print the brief; touch nothing")
    sub.add_parser("pick", help="print the item the next iteration would take")
    s2 = sub.add_parser("status", help="print STATUS.md (or --json)")
    s2.add_argument("--json", action="store_true")
    s3 = sub.add_parser("validate", help="validate the backlog directory (default: this checkout's)")
    s3.add_argument("backlog", nargs="?", default="")
    s4 = sub.add_parser("ship-from", help="laptop side: open PRs for branches queued in the host's outbox")
    s4.add_argument("host")
    s4.add_argument("--auto-merge", action="store_true", help="enable GitHub auto-merge (waits for required checks)")
    s4.add_argument("--repo", default="couplet-physical-ai/PhiCad")
    s4.add_argument("--loop-home", default="~/phicad-loop")
    s4.add_argument("--base", default="", help="override the base branch the host recorded (e.g. dev while the host "
                    "tracks a feature branch)")
    a = ap.parse_args(argv)
    if a.cmd == "validate":
        here = Path(__file__).resolve().parent / "backlog"
        items = load_backlog(Path(a.backlog) if a.backlog else here)
        problems = validate_backlog(items)
        for p in problems:
            print("problem:", p)
        print(f"{len(items)} item(s), {len(problems)} problem(s)")
        return 1 if problems else 0
    if a.cmd == "ship-from":
        return ship_from(a.host, auto_merge=a.auto_merge, repo_slug=a.repo, loop_home=a.loop_home, base=a.base)
    cfg = Config.from_env()
    if a.cmd == "status":
        return cmd_status(cfg, a.json)
    if a.cmd == "pick":
        cfg.ensure_layout()
        items = load_backlog(cfg.backlog_dir)
        picked, reasons = pick_item(items, load_state(cfg), env=clean_env())
        print(picked.id if picked else "(nothing eligible)")
        for r in reasons:
            print("  skip:", r)
        return 0
    cfg.dry_run = a.dry_run
    return iterate(cfg, item_id=a.item, runner=a.runner)


if __name__ == "__main__":
    sys.exit(main())
