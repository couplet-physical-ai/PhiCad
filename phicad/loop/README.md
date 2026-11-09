# PhiCad continuous development loop (`phicad/loop/`)

An unattended loop that keeps improving the fork on the Linux build host. One **iteration**:

```
sync own clone to origin/dev
  → pick one backlog item (priority, dependencies, requirements, attempts left)
  → fresh worktree off origin/dev on loop/<item>-<run>
  → phi implements it (one bounded brief; runner `phi`)
  → the loop judges: scope globs · check_fork_boundary.py · phicad check fast · item gates
  → push the branch (always — a rejected attempt is evidence)
  → PR into dev if the gates passed (or queue it in the outbox when the host has no `gh` login)
  → runs/<run>/, runs.jsonl, state.json, STATUS.md
```

A systemd **user timer** fires an iteration a fixed pause after the previous one ended
(default 30 min). **Nothing here merges** — a human (or the laptop-side `ship-from`
with `--auto-merge`, which still waits for the required CI check) does that.

First host: `build-host-01` (ssh alias `build-host`), 2026-09-22, sharing the box with the
Phase 1 checkout `~/src/PhiCad` and the Studio thread — see "Host etiquette".

## Why it is built this way

- **The model never judges itself.** Every decision about what may reach `dev` runs in
  `loop.py` *after* phi has finished: changed paths must match the item's `scope` globs, no
  upstream KiCad file may change, `phicad check fast` must be green, the item's own `gates`
  must exit 0. The implementing agent is told exactly this in the brief, so it can run the
  same commands itself before stopping — but it cannot alter the verdict.
- **phi, an agent harness, is the only harness; the person's own credential is the only
  credential.** Runner `phi` is `phi -p --mode json --model <tier model> --no-session
  --append-system-prompt loop-contract.md <brief>`, using the credential phi already holds on
  the host (sign in to phi there once). The loop never reads `~/.pi/agent/auth.json` and never
  puts a credential in argv or a log (`assert_no_secret`, `redact`).
- **One branch per attempt, never reused, never force-pushed.** `loop/<item>-<run_id>`; the
  worktree directory `wt/<item>` is recycled, the branch is not. A rejected branch stays on
  origin for a human to read.
- **The backlog is code.** Items are Markdown files under `backlog/`; adding, reprioritising or
  retiring work is a pull request. The loop syncs its clone before every pick, so a backlog
  change (or a fix to `loop.py` itself) takes effect at the next firing — the loop improves
  with `dev`.
- **Stdlib only.** `loop.py` and `test_loop.py` need nothing but python ≥ 3.11 and git, so
  `phicad check fast` runs the tests on every PR and any host can run an iteration without a
  venv.

## Backlog item format

`backlog/<id>.md` — frontmatter (a fixed subset of YAML: `key: value` and `- item` lists) plus
the brief in Markdown. The id is the file name.

```markdown
---
title: Run the official KLC checkers before lib-librarian may call a part ready
status: open            # open | in-review | done | blocked | retired
priority: 10            # lower runs first
tier: standard          # cheap | standard | strong  → model (see "Models")
scope:                  # globs every changed path must match; default phicad/**, AGENTS.md
  - phicad/parts/**
  - phicad/testing.md
gates:                  # extra shell commands run in the worktree; must exit 0
  - test -f phicad/parts/klc_check.py
depends_on: []          # ids that must be done (in the backlog, or merged per state.json)
requires: []            # paths ($VAR expanded) that must exist first, e.g. a kiface still building
timeout_min: 45         # wall clock for the AI step (capped by PHICAD_LOOP_MAX_MIN)
max_attempts: 2         # then the item is parked (state.json `exhausted`)
source: the OSS integration landscape review (unpublished proposal) (row 7)
---

## Definition of done
…concrete, verifiable, with file names…
```

`python3 phicad/loop/loop.py validate` checks the directory (unique ids, known statuses and
tiers, dependencies exist, no cycles, repo-relative scopes); `check fast` runs the same check
through `test_loop.py`, so a broken item cannot merge.

**Lifecycle.** The loop flips the item to `status: done` (with `branch:`) *on the loop branch*
when the gates pass, so the merge itself closes the item in the backlog. Until then
`state.json` remembers the pending branch and the item is not picked again; if the branch is
deleted from origin without merging (a rejected PR), the item becomes eligible again with one
attempt used. Pick order: `control/NEXT` (an item id) if eligible, else lowest `priority`, then
id. Items whose `requires:` are missing are skipped *without* being marked blocked — that is
how board-loading items wait for `_pcbnew.kiface` on a host that is still building it.

## Models

Tier → model reference (`provider/model[:thinking]`), overridable with
`PHICAD_LOOP_MODELS='cheap=…,standard=…,strong=…'`:

| tier | default | intended for |
| --- | --- | --- |
| cheap | `gateway/fast:high` | docs, small helpers, platform shims |
| standard | `gateway/strong:high` | a new helper + tests + docs in one area |
| strong | `gateway/frontier:max` | cross-module changes, replacing a dependency |

All are neutral tier aliases on the configured gateway that phi resolves (see
`phicad/agents/README.md`); `phicad models check` on the host verifies them.

**Liveness probe.** Before the AI step the loop runs one tool-less `phi -p --model <ref>
"Reply with exactly TIER_OK"` (120 s, `PHICAD_LOOP_PROBE_TIMEOUT`). A model that stays silent
is skipped for the nearest tier that answers (higher tier preferred on ties); the attempt is
recorded with `model_wanted`, `model` and every probe. If no tier answers the iteration ends as
`runner-error` without consuming an attempt. Why: one gateway alias was once seen to sit silent for
minutes on every host while the other aliases answered in 1–3 s — `phicad models check` cannot
see that (it only checks the alias is served), and an unattended loop would otherwise burn the
whole item budget on nothing. `PHICAD_LOOP_PROBE=0` disables it.

**Spend caps.** The phi runner's JSON event stream is metered while it runs; an AI step that
crosses `PHICAD_LOOP_MAX_COST_USD` (default 25) is killed and recorded as `budget-exceeded` —
its commits stay on the local branch for a human, nothing is gated or pushed. Before starting an
item the loop sums the cost of runs recorded in the last 24 h and stops at
`PHICAD_LOOP_DAILY_CAP_USD` (default 100; `once --item <id>` bypasses it). Only `message_end`
events are billed: phi repeats a message's running usage in every `message_update` (thousands
per run) and again in `turn_end`, which is how the first day's records showed a $3.35 run as
$263 — `runs.jsonl` entries before 2026-09-22 20:00 UTC carry that inflated `ai.usage`; the
true first-day total was $10.07 for six runs. `STATUS.md` shows per-run cost and the 24 h total.

## Files

| file | role |
| --- | --- |
| `loop.py` | the engine: `once`, `pick`, `status [--json]`, `validate [dir]`, `ship-from <host> [--auto-merge]` |
| `loop-contract.md` | appended to phi's system prompt for the implementing agent (the operating rules) |
| `backlog/*.md` | the queue |
| `test_loop.py` | 33 tests; run by `phicad check fast` |
| `install-remote.sh` | laptop → host installer (layout, own clone, env, timer, smoke) |
| `systemd/` | the user service + timer the installer copies |

Runtime layout on the host, `~/phicad-loop/` (0700):

```
repo/            the loop's own blob:none clone, checked out at PHICAD_LOOP_BRANCH
wt/<item>/       the current attempt's worktree (recycled per item)
runs/<run_id>/   brief.md, phi-events.jsonl, phi-stderr.log, gates.log, loop.log, summary.json, pr-body.md
runs.jsonl       one JSON line per iteration            state.json   attempts, pending/merged branches
STATUS.md        human page (items, skips, recent runs)  journal.log  systemd stdout/stderr
outbox/          PRs the host could not open (no gh login) — shipped by `ship-from` from the laptop
control/         PAUSE · BREAKER · NEXT                   env          configuration (0600)
                                                         loop.lock    one iteration at a time
```

## Operating it

```bash
# install / reconfigure (laptop). While this directory is on a feature branch, track it:
phicad/loop/install-remote.sh build-host --branch feat/dev-loop \
    --kicad-cli /home/user/src/PhiCad/build/linux-dev/kicad/kicad-cli \
    --build /home/user/src/PhiCad/build/linux-dev
phicad/loop/install-remote.sh build-host            # after merge: back to dev
phicad/loop/install-remote.sh build-host --run-once # one iteration now, in the foreground

# steer (host)
touch ~/phicad-loop/control/PAUSE          # stop picking; rm to resume
echo 0004-model-viewer-3d-panel > ~/phicad-loop/control/NEXT   # take this next (if eligible)
rm ~/phicad-loop/control/BREAKER           # after reading why 3 iterations in a row failed
python3 ~/phicad-loop/repo/phicad/loop/loop.py status
systemctl --user list-timers phicad-loop.timer; journalctl --user -u phicad-loop -n 50

# ship queued PRs (laptop — where gh is logged in)
python3 phicad/loop/loop.py ship-from build-host              # open PRs for outbox entries
python3 phicad/loop/loop.py ship-from build-host --auto-merge # + GitHub auto-merge (waits for CI)
```

Outcomes recorded per run: `pr-opened`, `pushed-pr-queued`, `pushed-pr-failed`, `rejected`
(gates failed; branch pushed), `no-change` (agent made no commits), `agent-blocked`
(`.phicad-loop/blocked.md`, no commits), `timeout` (AI step killed at the wall clock; whatever
was committed is still gated and pushed), `budget-exceeded` (AI step killed at the spend cap;
nothing pushed), `runner-error`. Three failing iterations in a row
open the **breaker** (`control/BREAKER`); `once --item <id>` bypasses the breaker and the pick
order for a deliberate retry. A dry run (`once --dry-run`) prints the brief and consumes no
attempt.

## Explicitly authorized run windows

An owner can authorize a fixed window of at most six hours without the loop's dollar caps.
This does not change gateway/account quotas or replace the person's existing Phi credential.
`run_window.py install --authorized-at <UTC> --expires-at <UTC>` accepts absolute timestamps
in `YYYY-MM-DDTHH:MM:SSZ` form. It refuses an existing authorization rather than silently
renewing it, and leaves the loop paused. Do not shift the deadline to the time installation
finishes.

Install and verify on the loop host while its service is inactive. The installer pins the
runner under `control/window-runner/`, writes a private `control/run-window.json`, installs a
service start guard, and enables `phicad-loop-expiry.timer` with a persistent absolute expiry.
Check the unit definitions with `systemd-analyze --user verify`, the actual deadline with
`systemctl --user list-timers phicad-loop-expiry.timer`, and user lingering with `loginctl`.
Verify whole-cgroup termination with an independent test service before resuming paid work.
Only after those checks and the code's gates pass, remove `control/PAUSE` and start the loop
timer/service. Installation itself never starts an iteration.

During the active window, runner `phi` overrides both loop dollar caps in memory. The normal
environment file and its limits stay unchanged; accounting remains informational. Other runners
are refused for this mode. Probe and Phi timeouts are also bounded by the deadline. At expiry,
the independent host service writes `PAUSE`, stops the scheduler, and immediately kills all
processes in the loop service's control group, including descendants with separate sessions.
It stops the service without deleting its worktrees: committed and uncommitted disk work is
preserved, but an interrupted process cannot promise an additional autosave commit.

After expiry, the start guard and `PAUSE` prevent scheduled or manual `once --item` restarts.
The uncapped override is gone; normal configured caps were never rewritten. A later run needs
new explicit authorization and deliberate reconfiguration. Do not remove the authorization
record or pause simply because the next day has started. To verify the actual stop, inspect
`phicad-loop-expiry.service`'s journal and the timer/service state, confirm an empty loop
control group and the persistent pause, and report the worktree state. An installed timer is
not evidence that the future stop has already occurred.

## Host etiquette (shared machine)

The loop runs under `nice -n 15 ionice -c3` (systemd `Nice=`/`IOSchedulingClass=` plus the
same prefix on every gate and phi process). It never builds C++ (`loop-contract.md` forbids it
and the brief says so). It reads another checkout's `kicad-cli` only through
`PHICAD_KICAD_CLI`/`PHICAD_BUILD`, read-only, with `KICAD_RUN_FROM_BUILD_DIR=1`; the owner of
that build tree is told before it is deleted or reconfigured. Its clone, worktrees, and state are all under `~/phicad-loop/`.

## Limits and next steps

- No auto-merge from the host. `ship-from --auto-merge` on the laptop is the only merge path
  and it still waits for the required `fork layer (no C++ build)` check.
- The host has no `gh` login by design (deploy key only) — PRs are queued, not opened, until
  `ship-from` runs on the laptop or a GitHub App token is provisioned for the loop.
- Items are executed one at a time per host. Parallel items would need per-item build/venv
  isolation the fork does not have yet.
- The backlog is seeded from `the OSS integration landscape review (unpublished proposal)`;
  new items should cite their source the same way.
