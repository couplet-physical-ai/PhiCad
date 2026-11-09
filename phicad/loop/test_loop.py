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

"""Tests for the continuous development loop (stdlib only; run by ``phicad check fast``).

Two kinds: pure unit tests of the decisions the loop makes (item format, picking order,
scope, gate parsing, secret discipline) and one end-to-end iteration
against a scratch bare repository with a fake ``phi`` and stubbed gates — proving the
mechanics (worktree, autosave, status flip, push, outbox, state) without a model, a network,
or a real ``check fast``.  Gate tallies are never pinned (they change with every gate added).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import loop  # noqa: E402

SAMPLE = textwrap.dedent("""\
    ---
    id: 0042-sample
    title: Do a sample thing
    status: open
    priority: 7
    tier: cheap
    scope:
      - phicad/parts/**
      - phicad/testing.md
    gates: [test -f x, "echo ok"]
    depends_on: []
    requires:
      - $PHICAD_BUILD/pcbnew/_pcbnew.kiface
    timeout_min: 30
    max_attempts: 3
    source: somewhere.md
    owner: nobody
    ---

    ## Definition of done

    Something concrete.
    """)


class ItemFormat(unittest.TestCase):
    def test_parse_all_field_shapes(self):
        it = loop.parse_item(SAMPLE, Path("/x/phicad/loop/backlog/0042-sample.md"))
        self.assertEqual(it.id, "0042-sample")
        self.assertEqual(it.priority, 7)
        self.assertEqual(it.tier, "cheap")
        self.assertEqual(it.scope, ("phicad/parts/**", "phicad/testing.md"))
        self.assertEqual(it.gates, ("test -f x", "echo ok"))
        self.assertEqual(it.depends_on, ())
        self.assertEqual(it.requires, ("$PHICAD_BUILD/pcbnew/_pcbnew.kiface",))
        self.assertEqual(it.max_attempts, 3)
        self.assertEqual(it.extra, {"owner": "nobody"})
        self.assertIn("Something concrete.", it.body)

    def test_render_round_trip(self):
        it = loop.parse_item(SAMPLE, Path("/x/0042-sample.md"))
        again = loop.parse_item(loop.render_item(it), Path("/x/0042-sample.md"))
        for f in ("id", "title", "status", "priority", "tier", "scope", "gates", "requires", "timeout_min",
                  "max_attempts", "source", "extra"):
            self.assertEqual(getattr(it, f), getattr(again, f), f)
        self.assertEqual(it.body.strip(), again.body.strip())

    def test_id_from_filename_and_defaults(self):
        it = loop.parse_item("---\ntitle: T\n---\nbody\n", Path("/b/0001-x.md"))
        self.assertEqual(it.id, "0001-x")
        self.assertEqual(it.status, "open")
        self.assertEqual(it.scope, loop.DEFAULT_SCOPE)
        self.assertEqual(it.tier, "standard")

    def test_rejects_bad_values(self):
        for bad, msg in (
            ("---\ntitle: T\nstatus: wat\n---\nb\n", "status"),
            ("---\ntitle: T\ntier: huge\n---\nb\n", "tier"),
            ("---\ntitle: T\npriority: soon\n---\nb\n", "integer"),
            ("---\nstatus: open\n---\nb\n", "title"),
            ("no frontmatter\n", "frontmatter"),
            ("---\ntitle: T\nid: Bad Id\n---\nb\n", "lowercase"),
        ):
            with self.assertRaises(ValueError, msg=bad) as cm:
                loop.parse_item(bad, Path("/b/0001-x.md"))
            self.assertIn(msg, str(cm.exception))

    def test_model_by_tier(self):
        it = loop.parse_item("---\ntitle: T\ntier: strong\n---\nb\n", Path("/b/0001-x.md"))
        self.assertEqual(it.model(loop.DEFAULT_MODELS), loop.DEFAULT_MODELS["strong"])


class RealBacklog(unittest.TestCase):
    """The seed backlog shipped in this directory must always validate — CI guards it."""

    def test_shipped_backlog_is_consistent(self):
        items = loop.load_backlog(HERE / "backlog")
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(loop.validate_backlog(items), [])
        for it in items:
            self.assertTrue(it.path.stem == it.id)
            self.assertIn("## Definition of done", it.body, it.id)
            for g in it.scope:
                self.assertTrue(g.startswith(("phicad/", "AGENTS.md")), f"{it.id}: {g} escapes the fork layer")


class Validation(unittest.TestCase):
    def mk(self, id_, **kw):
        it = loop.Item(id=id_, title=id_, body="x\n", path=Path(f"/b/{id_}.md"))
        for k, v in kw.items():
            setattr(it, k, v)
        return it

    def test_detects_duplicates_unknown_deps_cycles_and_names(self):
        a = self.mk("a", depends_on=("b",))
        b = self.mk("b", depends_on=("a",))
        dup = self.mk("a")
        ghost = self.mk("c", depends_on=("nope",))
        bad_name = self.mk("d")
        bad_name.path = Path("/b/other.md")
        problems = loop.validate_backlog([a, b, dup, ghost, bad_name])
        joined = "\n".join(problems)
        self.assertIn("duplicate id: a", joined)
        self.assertIn("unknown item 'nope'", joined)
        self.assertIn("cycle", joined)
        self.assertIn("does not match id", joined)

    def test_clean_backlog_has_no_problems(self):
        self.assertEqual(loop.validate_backlog([self.mk("a"), self.mk("b", depends_on=("a",))]), [])


class Picking(unittest.TestCase):
    def mk(self, id_, **kw):
        it = loop.Item(id=id_, title=id_, body="x\n")
        for k, v in kw.items():
            setattr(it, k, v)
        return it

    def test_priority_then_id_and_dependencies(self):
        items = [self.mk("b", priority=10), self.mk("a", priority=10), self.mk("c", priority=1, depends_on=("a",))]
        picked, reasons = loop.pick_item(items, {"items": {}}, env={})
        self.assertEqual(picked.id, "a")           # c waits on a; a before b by id
        self.assertTrue(any(r.startswith("c: waiting on a") for r in reasons))
        items[1].status = "done"
        picked, _ = loop.pick_item(items, {"items": {}}, env={})
        self.assertEqual(picked.id, "c")

    def test_pending_pr_attempts_requires_and_prefer(self):
        items = [self.mk("a", priority=1), self.mk("b", priority=2), self.mk("c", priority=3, requires=("$X/missing",))]
        state = {"items": {"a": {"pending_branch": "loop/a-1"}, "b": {"attempts": 2}}}
        items[1].max_attempts = 2
        picked, reasons = loop.pick_item(items, state, env={"X": "/nonexistent"})
        self.assertIsNone(picked)
        self.assertIn("a: pull request pending on loop/a-1", reasons)
        self.assertIn("b: 2 attempts used (max 2)", reasons)
        self.assertTrue(any(r.startswith("c: requires missing") for r in reasons))
        # merged branch frees the item's dependants; prefer picks a lower-priority eligible item
        items.append(self.mk("d", priority=9))
        items.append(self.mk("e", priority=8, depends_on=("a",)))
        picked, _ = loop.pick_item(items, state, env={"X": "/nonexistent"}, merged={"a"})
        self.assertEqual(picked.id, "e")
        picked, _ = loop.pick_item(items, state, prefer="d", env={"X": "/nonexistent"}, merged={"a"})
        self.assertEqual(picked.id, "d")

    def test_revised_item_gets_its_attempts_back(self):
        a = loop.parse_item("---\ntitle: A\n---\nold text\n", Path("/b/a.md"))
        state = {"items": {"a": {"attempts": 2, "exhausted": True, "digest": a.digest,
                                 "last_outcome": "agent-blocked"}}}
        self.assertEqual(loop.reset_revised([a], state), [])          # unchanged text: nothing happens
        a2 = loop.parse_item("---\ntitle: A\n---\nnew text\n", Path("/b/a.md"))
        self.assertNotEqual(a.digest, a2.digest)
        self.assertEqual(loop.reset_revised([a2], state), ["a"])
        st = state["items"]["a"]
        self.assertEqual(st["attempts"], 0)
        self.assertNotIn("exhausted", st)
        self.assertEqual(st["digest"], a2.digest)
        self.assertEqual(st["previous_digest"], a.digest)
        picked, _ = loop.pick_item([a2], state, env={})
        self.assertEqual(picked.id, "a")
        # an unchanged item that ended agent-blocked is parked even with attempts left
        parked = {"items": {"a": {"attempts": 1, "exhausted": True, "digest": a2.digest,
                                  "last_outcome": "agent-blocked"}}}
        picked, reasons = loop.pick_item([a2], parked, env={})
        self.assertIsNone(picked)
        self.assertTrue(any("parked after agent-blocked" in r for r in reasons))
        # a pending pull request pins the old attempt: no reset
        state = {"items": {"a": {"attempts": 1, "digest": a.digest, "pending_branch": "loop/a-1"}}}
        self.assertEqual(loop.reset_revised([a2], state), [])
        self.assertEqual(state["items"]["a"]["attempts"], 1)

    def test_requires_present_makes_eligible(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "thing.kiface").write_text("")
            it = self.mk("a", requires=("$B/thing.kiface",))
            self.assertEqual(loop.requirements_missing(it, {"B": td}), [])
            picked, _ = loop.pick_item([it], {"items": {}}, env={"B": td})
            self.assertEqual(picked.id, "a")


class PureChecks(unittest.TestCase):
    def test_scope_globs(self):
        scope = ("phicad/parts/**", "phicad/testing.md", "AGENTS.md")
        inside = ["phicad/parts/a.py", "phicad/parts/deep/b.py", "phicad/testing.md", "AGENTS.md"]
        self.assertEqual(loop.paths_in_scope(inside, scope), [])
        outside = ["pcbnew/board.cpp", "phicad/mcp/server.py"]
        self.assertEqual(loop.paths_in_scope(outside, scope), outside)
        self.assertEqual(loop.paths_in_scope(["phicad/partsX/a.py"], ("phicad/parts/**",)), ["phicad/partsX/a.py"])

    def test_check_summary(self):
        out = "noise\n25 passed, 1 skipped, 0 failed, 0 broken\nmore\n3 passed, 0 skipped, 1 failed, 0 broken\n"
        self.assertEqual(loop.parse_check_summary(out), {"passed": 3, "skipped": 0, "failed": 1, "broken": 0})
        self.assertIsNone(loop.parse_check_summary("nothing here"))

    def test_secret_discipline(self):
        with self.assertRaises(RuntimeError):
            loop.assert_no_secret(["curl", "-H", "Authorization: Bearer abc123def456"])
        with self.assertRaises(RuntimeError):
            loop.assert_no_secret(["x", "sk-abcdefghijklmnop"])
        loop.assert_no_secret(["phi", "-p", "--model", "gateway/fast:high", "do the thing"])
        self.assertNotIn("abc123", loop.redact("Authorization: Bearer abc123def456 tail"))

    def test_brief_carries_the_facts(self):
        it = loop.parse_item(SAMPLE, Path("/x/0042-sample.md"))
        brief = loop.compose_brief(it, worktree=Path("/w/0042"), branch="loop/0042-sample-1", base="origin/dev",
                                   model="gateway/fast:high", gates=list(it.gates), kicad_cli="/b/kicad-cli")
        for needle in ("loop/0042-sample-1", "origin/dev", "phicad/parts/**", "`test -f x`", "gateway/fast:high",
                       "check_fork_boundary.py --base origin/dev", ".phicad-loop/pr-body.md", "/b/kicad-cli",
                       "Do a sample thing"):
            self.assertIn(needle, brief)
        self.assertNotIn("Bearer", brief)

    def test_config_from_env(self):
        env = {"HOME": "/h", "PHICAD_LOOP_MODELS": "cheap=gateway/bulk, strong=other/review:max,bogus=x",
               "PHICAD_LOOP_BRANCH": "dev", "PHICAD_LOOP_RUNNER": "phi", "PHICAD_KICAD_CLI": "/b/kicad-cli"}
        cfg = loop.Config.from_env(env)
        self.assertEqual(cfg.home, Path("/h/phicad-loop"))
        self.assertEqual(cfg.repo, Path("/h/phicad-loop/repo"))
        self.assertEqual(cfg.models["cheap"], "gateway/bulk")
        self.assertEqual(cfg.models["strong"], "other/review:max")
        self.assertEqual(cfg.models["standard"], loop.DEFAULT_MODELS["standard"])
        self.assertEqual(cfg.runner, "phi")
        self.assertEqual(cfg.backlog_dir, Path("/h/phicad-loop/repo/phicad/loop/backlog"))

    def test_clean_env_strips_proxies_and_sets_build_dir_flag(self):
        old = dict(os.environ)
        try:
            os.environ["HTTPS_PROXY"] = "https://llm.example"
            os.environ["https_proxy"] = "https://llm.example"
            os.environ.pop("PHICAD_BUILD", None)     # `phicad check` exports it; the test wants a bare shell
            env = loop.clean_env({"PHICAD_KICAD_CLI": "/b/kicad-cli", "PHICAD_BUILD": ""})
        finally:
            os.environ.clear()
            os.environ.update(old)
        # assert on names only: a failure must never print the inherited environment
        names = set(env)
        self.assertFalse("HTTPS_PROXY" in names, "proxy variable not stripped")
        self.assertFalse("https_proxy" in names, "proxy variable not stripped")
        self.assertEqual(env["KICAD_RUN_FROM_BUILD_DIR"], "1")
        self.assertEqual(env["PHICAD_KICAD_CLI"], "/b/kicad-cli")
        self.assertFalse("PHICAD_BUILD" in names, "an empty override must not create the variable")
        self.assertTrue(env["PATH"].split(":")[0].endswith("/.local/bin"))


@unittest.skipUnless(shutil.which("sh"), "sh required")
class ModelProbe(unittest.TestCase):
    """A silent model must not eat the item budget: probe, then fall back to the nearest tier."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="phicad-loop-probe-"))
        # the fake answers for models containing 'good', answers wrongly for 'wrong', hangs otherwise
        _write_exec(self.td / "phi", textwrap.dedent("""\
            #!/bin/sh
            case "$*" in
              *good*)  echo '{"type":"message_end","message":{"role":"assistant","content":"TIER_OK"}}' ;;
              *wrong*) echo '{"type":"message_end","message":{"role":"assistant","content":"nope"}}' ;;
              *)       sleep 30 ;;
            esac
            """))
        self.cfg = loop.Config(home=self.td / "home", repo=self.td / "repo", phi_bin=str(self.td / "phi"), nice=(),
                               probe_timeout=2)
        self.env = {**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_probe_outcomes(self):
        ok, _ = loop.probe_model(self.cfg, "x/good", self.env, self.td, timeout=5)
        self.assertTrue(ok)
        ok, _ = loop.probe_model(self.cfg, "x/wrong", self.env, self.td, timeout=5)
        self.assertFalse(ok)
        ok, secs = loop.probe_model(self.cfg, "x/silent", self.env, self.td, timeout=1)
        self.assertFalse(ok)
        self.assertLess(secs, 10)      # killed at the timeout, not after the fake's 30 s sleep

    def test_fallback_order_and_exhaustion(self):
        item = loop.Item(id="i", title="i", body="x\n", tier="standard")
        self.cfg.models = {"cheap": "x/good-cheap", "standard": "x/silent-std", "strong": "x/good-strong"}
        model, tried = loop.choose_model(self.cfg, item, self.env, self.td)
        self.assertEqual(model, "x/good-strong")             # nearest tier, higher preferred on the tie
        self.assertEqual([t["model"] for t in tried], ["x/silent-std", "x/good-strong"])
        self.cfg.models = {"cheap": "x/silent-1", "standard": "x/silent-2", "strong": "x/wrong"}
        model, tried = loop.choose_model(self.cfg, item, self.env, self.td)
        self.assertEqual(model, "")
        self.assertEqual(len(tried), 3)
        self.cfg.probe = False
        model, tried = loop.choose_model(self.cfg, item, self.env, self.td)
        self.assertEqual((model, tried), ("x/silent-2", []))  # probing disabled: trust the table


class WindowHost(unittest.TestCase):
    def test_installer_pins_watchdog_and_does_not_resume_or_change_env(self):
        import run_window
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "phicad-loop"
            home.mkdir()
            (home / "env").write_text("PHICAD_LOOP_MAX_COST_USD=12\n")
            now = time.time()
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            start = time.strftime(fmt, time.gmtime(now - 60))
            end = time.strftime(fmt, time.gmtime(now + 60))
            with patch.object(run_window.Path, "home", return_value=Path(td)), \
                    patch.object(run_window, "systemctl") as ctl, \
                    patch.object(run_window.subprocess, "run") as run:
                run.return_value.returncode = 3
                run_window.install(home, start, end)
                calls = [c.args for c in ctl.call_args_list]
                self.assertEqual(calls[-1], ("enable", "--now", "phicad-loop-expiry.timer"))
                self.assertNotIn(("start", "phicad-loop.service"), calls)
                with self.assertRaises(ValueError):
                    run_window.install(home, start, end)
            units = Path(td) / ".config/systemd/user"
            timer = (units / "phicad-loop-expiry.timer").read_text()
            self.assertIn("Persistent=true", timer)
            self.assertIn(end.replace("T", " ").replace("Z", " UTC"), timer)
            dropin = (units / "phicad-loop.service.d/90-authorized-window.conf").read_text()
            self.assertIn("KillMode=control-group", dropin)
            self.assertIn("ExecCondition=", dropin)
            self.assertTrue((home / "control/PAUSE").exists())
            self.assertEqual((home / "env").read_text(), "PHICAD_LOOP_MAX_COST_USD=12\n")
            with self.assertRaises(ValueError):
                run_window.deadline(home, now=now + 120)

    def test_expiry_pauses_and_kills_even_session_descendants(self):
        import run_window
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "control").mkdir()
            with patch.object(run_window, "systemctl") as ctl, \
                    patch.object(run_window.subprocess, "run") as run:
                run_window.expire(home)
                self.assertTrue((home / "control/PAUSE").exists())
                self.assertEqual(ctl.call_args_list[0].args, ("stop", "phicad-loop.timer"))
                argv = run.call_args.args[0]
                self.assertIn("--signal=SIGKILL", argv)
                self.assertIn("--kill-whom=all", argv)
                self.assertEqual(ctl.call_args_list[-1].args, ("stop", "phicad-loop.service"))


class PhiEvents(unittest.TestCase):
    def test_summarize_events(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td, "ev.jsonl")
            p.write_text("\n".join([
                "not json",
                json.dumps({"type": "message_end", "message": {"role": "user", "content": "hi"}}),
                json.dumps({"type": "message_end", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "first"}],
                    "usage": {"input": 10, "output": 5, "cost": {"total": 0.01}}}}),
                json.dumps({"type": "message_end", "message": {
                    "role": "assistant", "content": "final words",
                    "usage": {"input": 20, "output": 7, "cost": {"total": 0.02}}}}),
            ]))
            text, usage = loop.summarize_phi_events(p)
            self.assertEqual(text, "final words")
            self.assertEqual(usage["input"], 30)
            self.assertEqual(usage["output"], 12)
            self.assertAlmostEqual(usage["cost_total"], 0.03)

    def test_only_message_end_is_billed(self):
        # phi streams the running usage of an in-flight message in every message_update (thousands
        # per run) and repeats it in turn_end; billing all of them turned a $3.35 run into $263.
        def ev(kind, cost):
            return json.dumps({"type": kind, "message": {"role": "assistant", "content": "x",
                                                        "usage": {"totalTokens": 100, "cost": {"total": cost}}}})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td, "ev.jsonl")
            p.write_text("\n".join([ev("message_start", 0.1), ev("message_update", 0.5), ev("message_update", 0.9),
                                    ev("message_end", 1.0), ev("turn_end", 1.0),
                                    ev("message_start", 0.2), ev("message_update", 1.5), ev("message_end", 2.0),
                                    ev("turn_end", 2.0)]) + "\n")
            _, usage = loop.summarize_phi_events(p)
            self.assertAlmostEqual(usage["cost_total"], 3.0)
            self.assertEqual(usage["totalTokens"], 200)
            # the live meter agrees, and reads incrementally across a partial trailing line
            meter = loop.CostMeter(p)
            self.assertAlmostEqual(meter.feed(), 3.0)
            with open(p, "a") as fh:
                fh.write(ev("message_end", 4.0)[:-5])            # partial line: not billed yet
            self.assertAlmostEqual(meter.feed(), 3.0)
            with open(p, "a") as fh:
                fh.write(ev("message_end", 4.0)[-5:] + "\n")     # completed: billed once
            self.assertAlmostEqual(meter.feed(), 7.0)
            self.assertAlmostEqual(meter.feed(), 7.0)

    def test_spent_last_24h_ignores_old_runs(self):
        now = 1_800_000_000.0
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        import datetime as dt
        recent = dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).strftime(fmt)
        old = dt.datetime.fromtimestamp(now - 90000, dt.timezone.utc).strftime(fmt)
        state = {"runs": [{"at": recent, "cost_usd": 2.5}, {"at": old, "cost_usd": 50},
                          {"at": recent, "cost_usd": None}, {"at": "garbage", "cost_usd": 9}]}
        self.assertAlmostEqual(loop.spent_last_24h(state, now=now), 2.5)


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def _origin(td: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=td / "origin.git", capture_output=True, text=True).stdout


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(shutil.which("git"), "git required")
class EndToEndIteration(unittest.TestCase):
    """One real iteration against a scratch origin: fake phi commits a change and writes the
    PR body; stubbed gates pass; no gh -> the branch is pushed and the PR is queued."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="phicad-loop-e2e-"))
        origin = self.td / "origin.git"
        seed = self.td / "seed"
        _git("init", "-q", "--bare", "-b", "dev", str(origin), cwd=self.td)
        _git("init", "-q", "-b", "dev", str(seed), cwd=self.td)
        # a minimal fork layer: stub gates + the loop's own files + one backlog item
        _write_exec(seed / "phicad/bin/phicad",
                    "#!/bin/sh\necho 'stub gates'\necho '1 passed, 0 skipped, 0 failed, 0 broken'\n")
        _write_exec(seed / "phicad/bin/check_fork_boundary.py",
                    "#!/usr/bin/env python3\nprint('fork boundary ok (stub)')\n")
        (seed / "phicad/loop").mkdir(parents=True)
        shutil.copy(HERE / "loop.py", seed / "phicad/loop/loop.py")
        shutil.copy(HERE / "run_window.py", seed / "phicad/loop/run_window.py")
        shutil.copy(HERE / "loop-contract.md", seed / "phicad/loop/loop-contract.md")
        (seed / "phicad/loop/backlog").mkdir()
        (seed / "phicad/loop/backlog/0001-hello.md").write_text(textwrap.dedent("""\
            ---
            title: Add a hello file
            priority: 1
            tier: cheap
            scope:
              - phicad/hello/**
            gates:
              - test -f phicad/hello/HELLO.md
            ---

            ## Definition of done

            `phicad/hello/HELLO.md` exists.
            """))
        (seed / "phicad/loop/backlog/0002-later.md").write_text(textwrap.dedent("""\
            ---
            title: Later
            priority: 2
            depends_on:
              - 0001-hello
            ---

            ## Definition of done

            after hello.
            """))
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "seed", cwd=seed)
        _git("remote", "add", "origin", str(origin), cwd=seed)
        _git("push", "-q", "origin", "dev", cwd=seed)
        self.home = self.td / "loop-home"
        _git("clone", "-q", "-b", "dev", str(origin), str(self.home / "repo"), cwd=self.td)
        # fake phi: records argv, makes the change the item asks for, commits, writes the PR body,
        # emits one assistant event.  It must never see a credential-shaped argument.
        self.bin = self.td / "bin"
        # the host has gh installed but not logged in: `gh auth status` fails -> outbox path
        _write_exec(self.bin / "gh", "#!/bin/sh\necho 'You are not logged into any GitHub hosts.' >&2\nexit 1\n")
        _write_exec(self.bin / "phi", textwrap.dedent("""\
            #!/bin/sh
            printf '%s\\n' "$@" >> "$PHICAD_LOOP_TEST_ARGV"
            case "$*" in *TIER_OK*)   # the liveness probe: answer, touch nothing
              echo '{"type":"message_end","message":{"role":"assistant","content":"TIER_OK"}}'; exit 0 ;;
            esac
            mkdir -p phicad/hello && echo '# hello' > phicad/hello/HELLO.md
            git add phicad/hello/HELLO.md
            git -c user.name=fake -c user.email=fake@x commit -q -m 'phicad: add hello' -m 'Assisted-by: fake'
            mkdir -p .phicad-loop && printf 'phicad: add hello\\n\\nBody here.\\n' > .phicad-loop/pr-body.md
            echo 'stray' > phicad/hello/stray.txt
            echo '{"type":"message_end","message":{"role":"assistant","content":"done","usage":{"input":1,"output":1}}}'
            """))
        self.env_backup = dict(os.environ)
        os.environ["PHICAD_LOOP_TEST_ARGV"] = str(self.td / "argv.txt")
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        os.environ.pop("PHICAD_KICAD_CLI", None)
        self.cfg = loop.Config(home=self.home, repo=self.home / "repo", branch="dev", runner="phi",
                               phi_bin=str(self.bin / "phi"), nice=())

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.td, ignore_errors=True)

    def test_full_iteration_pushes_flips_and_queues(self):
        rc = loop.iterate(self.cfg)
        self.assertEqual(rc, 0)
        state = json.loads((self.home / "state.json").read_text())
        st = state["items"]["0001-hello"]
        self.assertEqual(st["attempts"], 1)
        self.assertTrue(st["pending_branch"].startswith("loop/0001-hello-"))
        self.assertEqual(st["last_outcome"], "pushed-pr-queued")
        branch = st["pending_branch"]
        # pushed to origin, with the agent's commit, the autosave, and the status flip
        log = _origin(self.td, "log", "--format=%s", branch)
        self.assertIn("phicad: add hello", log)
        self.assertIn("autosaved uncommitted work", log)
        self.assertIn("mark 0001-hello done", log)
        flipped = _origin(self.td, "show", f"{branch}:phicad/loop/backlog/0001-hello.md")
        self.assertIn("status: done", flipped)
        self.assertIn(f"branch: {branch}", flipped)
        # .phicad-loop scratch never reaches the branch
        tree = _origin(self.td, "ls-tree", "-r", "--name-only", branch)
        self.assertNotIn(".phicad-loop", tree)
        self.assertIn("phicad/hello/stray.txt", tree)
        # outbox entry for the laptop side, PR body from the agent + gate table
        outbox = list((self.home / "outbox").glob("*.json"))
        self.assertEqual(len(outbox), 1)
        entry = json.loads(outbox[0].read_text())
        self.assertEqual(entry["title"], "phicad: add hello")
        self.assertIn("Body here.", entry["body"])
        self.assertIn("phicad check fast", entry["body"])
        # the fake phi saw a clean argv: probe first (tool-less), then prompt-mode json run, no credential
        argv = (self.td / "argv.txt").read_text()
        self.assertIn("--no-tools\n" + loop.PROBE_PROMPT, argv)
        self.assertIn("--mode\njson", argv)
        self.assertIn(loop.DEFAULT_MODELS["cheap"], argv)
        self.assertNotIn("Bearer", argv)
        self.assertIn("phicad/hello/**", argv)          # scope reached the brief
        # records + status page
        self.assertTrue((self.home / "STATUS.md").exists())
        self.assertIn("pushed-pr-queued", (self.home / "STATUS.md").read_text())
        runs = [json.loads(line) for line in (self.home / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(runs[-1]["item"], "0001-hello")
        self.assertEqual(runs[-1]["gates"], "pass")
        self.assertEqual(runs[-1]["model"], loop.DEFAULT_MODELS["cheap"])
        self.assertTrue(runs[-1]["probes"][0]["answered"])
        # second iteration: 0001 has a PR pending, 0002 waits on it -> nothing eligible, no new run
        rc = loop.iterate(self.cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(len([json.loads(line) for line in (self.home / "runs.jsonl").read_text().splitlines()]), 1)
        self.assertIn("pull request pending", (self.home / "STATUS.md").read_text())
        # "merge" the branch on origin AND delete it, as GitHub's delete-branch-on-merge does
        # -> still recognised as merged (by SHA), item done, dependant becomes eligible
        subprocess.run(["git", "update-ref", "refs/heads/dev", branch], cwd=self.td / "origin.git", check=True)
        subprocess.run(["git", "update-ref", "-d", f"refs/heads/{branch}"], cwd=self.td / "origin.git", check=True)
        cfg2 = loop.Config(**{**vars(self.cfg), "dry_run": True})
        rc = loop.iterate(cfg2)
        self.assertEqual(rc, 0)
        state = json.loads((self.home / "state.json").read_text())
        self.assertIn("merged_branch", state["items"]["0001-hello"])
        self.assertNotIn("pending_branch", state["items"]["0001-hello"])
        self.assertNotEqual(state["items"]["0001-hello"].get("last_outcome"), "branch-removed")
        # a dry run consumes no attempt and records no run
        self.assertEqual(state["items"].get("0002-later", {}).get("attempts", 0), 0)
        self.assertEqual(len(state["runs"]), 1)
        items = loop.load_backlog(self.home / "repo/phicad/loop/backlog")
        picked, _ = loop.pick_item(items, state, env={}, merged={"0001-hello"})
        self.assertEqual(picked.id, "0002-later")

    def window(self, start, end):
        self.cfg.control.mkdir(parents=True, exist_ok=True)
        (self.cfg.control / "run-window.json").write_text(json.dumps({
            "authorized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)), "mode": "uncapped"}))

    def test_expired_window_blocks_manual_item_and_adds_pause(self):
        self.window(time.time() - 3600, time.time() - 60)
        self.assertEqual(loop.iterate(self.cfg, item_id="0001-hello"), 2)
        self.assertTrue((self.cfg.control / "PAUSE").exists())
        self.assertFalse((self.td / "argv.txt").exists())
        self.assertFalse((self.home / "runs.jsonl").exists())

    def test_active_window_removes_caps_only_for_its_invocation(self):
        self.window(time.time() - 60, time.time() + 3600)
        phi = self.bin / "phi"
        phi.write_text(phi.read_text().replace('"input":1,"output":1',
                                              '"input":1,"output":1,"cost":{"total":26.0}'))
        cfg = loop.Config(**{**vars(self.cfg), "daily_cap_usd": 0.0})
        self.assertEqual(loop.iterate(cfg), 0)
        rec = json.loads((self.home / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["outcome"], "pushed-pr-queued")
        self.assertAlmostEqual(rec["cost_usd"], 26.0)
        self.assertEqual(cfg.max_cost_usd, 25.0)
        self.assertEqual(cfg.daily_cap_usd, 0.0)

    def test_window_rejects_extension_and_other_runners(self):
        self.window(time.time() - 60, time.time() + 21600)
        self.assertEqual(loop.iterate(self.cfg), 2)
        (self.cfg.control / "PAUSE").unlink()
        self.window(time.time() - 60, time.time() + 3600)
        self.assertEqual(loop.iterate(self.cfg, runner="other"), 2)
        self.assertFalse((self.td / "argv.txt").exists())

    def test_budget_exceeded_before_first_poll_ships_nothing(self):
        # A completed process must still be checked before its commits can be shipped.
        phi = self.bin / "phi"
        phi.write_text(phi.read_text().replace('"input":1,"output":1',
                                              '"input":1,"output":1,"cost":{"total":1.2}'))
        cfg = loop.Config(**{**vars(self.cfg), "max_cost_usd": 1.0, "poll_sec": 60.0})
        self.assertEqual(loop.iterate(cfg), 0)
        run = json.loads((self.home / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual(run["ai"]["exit"], 0)
        self.assertEqual(run["outcome"], "budget-exceeded")
        self.assertAlmostEqual(run["cost_usd"], 1.2)
        self.assertGreater(run["commits"], 0)
        self.assertFalse(run.get("pushed"))
        self.assertEqual(_git("ls-remote", "--heads", "origin", "loop/*", cwd=self.home / "repo"), "")

    def test_budget_exceeded_kills_phi_and_ships_nothing(self):
        # a phi that bills a lot, commits, then hangs: the meter kills it and the loop must not ship
        _write_exec(self.bin / "phi", textwrap.dedent("""\
            #!/bin/sh
            case "$*" in *TIER_OK*)
              echo '{"type":"message_end","message":{"role":"assistant","content":"TIER_OK"}}'; exit 0 ;;
            esac
            mkdir -p phicad/hello && echo '# hello' > phicad/hello/HELLO.md
            git add phicad/hello/HELLO.md
            git -c user.name=fake -c user.email=fake@x commit -q -m 'phicad: add hello' -m 'Assisted-by: fake'
            u='"usage":{"cost":{"total":0.6}}'
            printf '{"type":"message_update","message":{"role":"assistant","content":"..",%s}}\\n' "$u"
            printf '{"type":"message_end","message":{"role":"assistant","content":"pricey",%s}}\\n' "$u"
            printf '{"type":"message_end","message":{"role":"assistant","content":"pricier",%s}}\\n' "$u"
            sleep 60
            """))
        cfg = loop.Config(**{**vars(self.cfg), "max_cost_usd": 1.0, "poll_sec": 0.3})
        t0 = time.time()
        rc = loop.iterate(cfg)
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 40, "the hung phi must be killed by the meter, not the timeout")
        run = json.loads((self.home / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual(run["ai"]["outcome"], "budget-exceeded")
        self.assertEqual(run["outcome"], "budget-exceeded")
        self.assertAlmostEqual(run["cost_usd"], 1.2)          # message_update not billed
        self.assertEqual(run["commits"], 1)                    # the WIP commit exists locally ...
        self.assertFalse(run.get("pushed"))                    # ... but nothing was pushed
        self.assertEqual(_git("ls-remote", "--heads", "origin", "loop/*", cwd=self.home / "repo"), "")
        state = json.loads((self.home / "state.json").read_text())
        self.assertEqual(state["items"]["0001-hello"]["attempts"], 1)
        self.assertNotIn("pending_branch", state["items"]["0001-hello"])
        self.assertIn("$1.20", (self.home / "STATUS.md").read_text())
        # the rolling cap then stops the next iteration before it spends anything
        cfg_capped = loop.Config(**{**vars(cfg), "daily_cap_usd": 1.0})
        self.assertEqual(loop.iterate(cfg_capped), 0)
        self.assertEqual(len((self.home / "runs.jsonl").read_text().splitlines()), 1)
        self.assertIn("daily budget reached", (self.home / "STATUS.md").read_text())

    def test_rejected_out_of_scope_is_pushed_but_not_queued(self):
        # the fake phi writes phicad/hello/**; narrow the item's scope so the change is out of scope
        item = self.home / "repo/phicad/loop/backlog/0001-hello.md"
        text = item.read_text().replace("  - phicad/hello/**", "  - phicad/elsewhere/**")
        item.write_text(text)
        _git("commit", "-q", "-am", "narrow scope", cwd=self.home / "repo")
        _git("push", "-q", "origin", "dev", cwd=self.home / "repo")
        rc = loop.iterate(self.cfg)
        self.assertEqual(rc, 0)
        state = json.loads((self.home / "state.json").read_text())
        st = state["items"]["0001-hello"]
        self.assertEqual(st["last_outcome"], "rejected")
        self.assertNotIn("pending_branch", st)
        self.assertEqual(list((self.home / "outbox").glob("*.json")), [])
        run = json.loads((self.home / "runs.jsonl").read_text().splitlines()[-1])
        self.assertTrue(run["pushed"])
        self.assertIn("scope", run["gates"])
        heads = _origin(self.td, "branch", "--list", "loop/*")
        self.assertIn("loop/0001-hello-", heads)
        self.assertEqual(state["consecutive_failures"], 1)

    def test_pause_and_breaker_stop_iterations(self):
        (self.home / "control").mkdir(parents=True)
        (self.home / "control/PAUSE").write_text("")
        self.assertEqual(loop.iterate(self.cfg), 0)
        self.assertFalse((self.home / "runs.jsonl").exists())
        (self.home / "control/PAUSE").unlink()
        (self.home / "control/BREAKER").write_text("3 consecutive failing iterations")
        self.assertEqual(loop.iterate(self.cfg), 0)
        self.assertFalse((self.home / "runs.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
