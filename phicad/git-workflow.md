# Git workflow

How work moves through this fork: branch per change, small commits, push constantly, keep `dev`
current. The goal is that `origin/dev` is always the truth and no work sits on a laptop; `master`
is the release branch that fast-forwards onto validated `dev` states.

This page covers **tempo and mechanics only**. It deliberately does not restate:

- branch roles, the two-worktree layout, upstream syncing → [README.md](README.md)
- "never commit on `master`", "keep PhiCad files in `phicad/`" → this page and [README.md](README.md)
- commit subject conventions, changelog tags, `Assisted-by:` → [testing.md](testing.md) §6

## The loop

```bash
git fetch origin                                   # always start from the real dev
git switch -c fix/drc-zone-crash origin/dev        # branch per change, off dev
# … edit, build narrowly, test narrowly …
phicad/bin/phicad fmt                              # changed lines only
git add -p && git commit                           # small, self-contained
git push -u origin fix/drc-zone-crash              # push the first commit immediately
# … more commits, push after each …
gh pr create --base dev                            # PR into dev, keep it small
```

Never branch off another feature branch unless the work genuinely depends on it. Stacked branches
turn one review into three.

## Branch naming

`<type>/<short-kebab-summary>` — no personal prefixes, no ticket-only names.

| Prefix | Use for |
| --- | --- |
| `fix/` | bug fix in existing behavior |
| `feat/` | new capability |
| `chore/` | docs, tooling, wrapper, CI, housekeeping |
| `refactor/` | behavior-preserving restructuring |
| `test/` | tests/fixtures only |
| `sync/` | upstream merge resolution when it needs review |

Name the *change*, not the file: `fix/drc-zone-crash`, not `fix/drc-cpp`. If you cannot name the
branch in a few words, it is more than one change — split it.

## Commit granularity

One logical change per commit. Prefer many small commits over one large one, **even inside a single
task** — this repo is a 2 GB fork of upstream and large blob commits make both review and upstream
merges impractical.

- Separate mechanical from semantic: a rename/move commit, then the behavior commit.
- Never mix a code fix with a docs change. They are different PRs.
- A commit should build. Do not commit a known-broken intermediate state to "fix it in the next one".
- Use `git add -p` to keep unrelated hunks out of the commit you are writing.
- Not yet pushed and the history is messy? Reshape it (`reset --mixed`, `commit --amend`, interactive
  rebase). Once pushed to a shared branch, only add new commits.

## When to push

Push after **every** meaningful commit — not at the end of the session. An unpushed commit is
invisible to the other agents and humans working this repo, and `Main` being stale is what makes
merges expensive.

- Push the first commit of a branch as soon as it exists (`git push -u`), so the branch is visible
  early.
- Never leave a session with local commits unpushed.
- `master` is push-only-after-fast-forward from `dev` (`phicad release`); see [README.md](README.md).

## Pull requests

- **Small.** A PR should be reviewable in one sitting. If the diff spans unrelated concerns, split it.
- One concern per PR. The fix and the doc describing it are two PRs.
- Body must state: the problem, the cause, the fix, and **the verification you actually ran**
  (real command output, not "should work").
- Target `dev`. Never target `master` — `master` only moves by `phicad release`.
- **CI must be green.** `dev` is a protected branch requiring the `fork layer (no C++ build)`
  check (`.github/workflows/phicad-check.yml`), so GitHub refuses the merge while it is red:
  `the base branch policy prohibits the merge`. Run `phicad check` locally first — it is the
  same gate, so a local pass means the PR will pass. Force-pushes and deletion of `dev` are
  blocked for the same reason.
  - The rule is deliberately **not** "strict" (a PR need not be rebased onto the newest `dev`
    before merging): `dev` moves several times an hour here, and strict mode would force a
    re-run and re-merge on every landing, serialising everyone behind the queue.
  - Admins are deliberately **not** included, so a genuine emergency can still land with
    `gh pr merge --admin`. That is an escape hatch, not a workflow — say why in the PR.
  - `master` is intentionally left unprotected: `phicad release` pushes to it directly, and a
    required check there would break the release cut.
- `delete_branch_on_merge` and auto-merge are enabled on the repo, so merged branches clean
  themselves up — do not hand-delete or leave stale branches around.
- Retire the local branch after merge: `git switch dev && git pull && git branch -d <branch>`.

## Fork/upstream path boundary

A fork-only PR may change `phicad/**` and three narrow integration points:
`AGENTS.md`, `pyproject.toml`, and `.github/workflows/phicad-check.yml`. The required CI job
compares the pull request tree with the event's exact base commit and rejects every other path.
Both sides of a rename are checked, so moving an upstream-owned file under `phicad/` is not a
way around the boundary.

Run the same checker before committing; unlike CI, the local form also includes tracked
working-tree changes and untracked, non-ignored files:

```bash
python3 phicad/bin/check_fork_boundary.py --base origin/dev
```

The checker exits 2, not 0, when either revision or the Git checkout cannot be inspected. CI
fetches only the exact base commit in addition to its one-commit checkout; it does not fetch the
multi-gigabyte history. Upstream synchronization remains the separate `phicad sync` workflow and
does not make an upstream implementation path valid in a fork-only PR.

### One-time exceptions (content-pinned)

Occasionally the fork has to carry a fix in an upstream file before upstream does (2026-09-22:
`kicad-cli` segfaulting at exit after `sch erc` on a schematic with no `.kicad_pro`, PR #121).
That is not a reason to widen the boundary. With the owner's explicit decision, the fix is
admitted through `phicad/fork-boundary-exceptions.json`: one entry per path, naming the exact
**blob id** of the fixed file, the PR, the reason, who authorized it and in what words, and where
the fix goes upstream. The checker admits exactly that content and nothing else — a further edit
to the file, or upstream's own version arriving through `phicad sync`, changes the blob and the
path is forbidden again until the entry is deliberately revised or removed. Because the list lives
under `phicad/`, every change to it is a reviewable diff in the PR that needs it, and CI prints
`exception applied: <path> (blob …, PR #n, authorized by …)` so the audit trail is in the run log.
A list the checker cannot trust (missing keys, a short blob, an entry for an already-allowed path,
a duplicate) is exit 2 — a broken gate, never a pass. `phicad/bin/test_check_gates.py` pins all of
this. When upstream ships the fix, `phicad sync` brings their version, the entry is dropped in the
same PR, and the boundary is whole again.

## Releases (`dev` → `master`)

`master` is what a fresh clone gets, so it only receives **validated** `dev` states:

1. `dev` is green: the areas touched since the last release built narrowly and their `qa_*`
   suites passed (full builds are ~3.5 h — gate on the narrow targets you touched, not the world).
   For the fork layer that is one command — run the **full** `phicad check` (not `fast`) in a
   worktree with the venvs and a built `kicad-cli`, so the MCP registry and synth selftest run
   too; CI only ever runs the `fast` subset. A release cut usually also promotes other people's
   merged work, so validate the tip you are about to publish, not just your own commits.
2. `phicad release` — fast-forwards `master` to `origin/dev` in the release checkout
   (`~/Documents/PhiCad-release`) and pushes. It refuses non-fast-forward moves; if `master`
   cannot ff, something was committed on it directly — fix that first.
3. Tag notable releases if wanted: `git tag -a exo-vN -m "..." && git push origin exo-vN`
   (optional — the branch pointer is the release record).

## Worktree lifecycle

Feature work happens in a worktree off `dev`, so several changes (and several agents) can be in
flight without fighting over one checkout.

```bash
git worktree add ~/Documents/PhiCad/wt-<name> -b <type>/<name> origin/dev   # create
git worktree list                                                           # what exists
git worktree remove ~/Documents/PhiCad/wt-<name>                            # retire after merge
```

Standing worktree: `~/Documents/PhiCad-release` (release, `master`) — `phicad release` requires it
clean. Everything else under `~/Documents/PhiCad/<Topic>` is a topic worktree branched off `dev`;
run `git worktree list` for the current set rather than trusting a list in a document. (The old
`Dev`/`Main` worktrees from the 2026-09-02 rename no longer exist — do not recreate them, and never
point a tool at another worktree's `build/mac-dev`.)

Retire worktrees once merged — each extra checkout is a full KiCad source tree, and a
configured one carries a ~2 GB `build/` tree with it.

**All worktrees share one `.git`.** Everything below follows from that.

## Working alongside another agent

More than one agent works this repo at once. Before doing git work, check whether someone else
holds a worktree:

```bash
git worktree list                 # look for another worktree of this repo
git -C <that-worktree> status --short
```

A staged change you did not make means a peer is mid-flight. Then:

- **Never** run a write-y git command (`add`, `commit`, `checkout`, `switch`, `stash`, `reset`) in
  another agent's worktree. It corrupts their in-flight index.
- Read-only inspection (`status`, `diff`, `log`, `ls-tree`) is fine and is the fastest way to see
  what they are doing.
- Split ownership by branch/worktree and say so explicitly to the other agent (or its human).
- If a file is owned by another agent, do not edit it in parallel. Send them the fix, or take the
  file on a branch and tell them to leave it alone while the PR is open.

## Agents: commit small as you go, PR per segment

This applies to agent sessions and subagent waves exactly as written above, and it is the rule
most often broken, because an agent holds the whole task in context and *feels* like one unit of
work. It is not. **Commit at every working checkpoint; open a PR per coherent segment.**

What this means concretely inside one long session:

```bash
git switch -c feat/board-routing-plan origin/dev
# ... get pad-centre parsing working ...
git add -p && git commit -m "phicad: read demo pad centres from the footprint libraries"
git push -u origin feat/board-routing-plan     # push the FIRST commit, not the last
# ... get the corridor plan working ...
git add -p && git commit -m "phicad: route the demo board from a declared corridor plan"
git push
# ... segment done ...
gh pr create --base dev --fill
# next segment gets its OWN branch off origin/dev, not a commit on this one
```

Checkpoint whenever any of these becomes true — do not wait for the task to end:

- a check that was failing now passes (a DRC/QA count dropped, a test went green)
- a subagent wave landed and you have verified its output
- you are about to try something that could make things worse (then the checkpoint is your undo)
- you learned a durable fact worth writing down (the commit body is where it belongs)
- roughly every 30–60 minutes of work, regardless

Why this is not bureaucracy, in this repo specifically:

- **A 2 GB fork makes big commits unreviewable.** "Everything I did today" cannot be reviewed,
  cannot be bisected, and cannot be reverted without losing the good parts with the bad.
- **Agent work needs an undo.** When a wave produces a silently wrong result (a board that opens
  but is misrouted, a refdes offset that moves labels onto pads), the fix is
  `git revert <one commit>` — only if that commit exists.
- **`git bisect` is the cheapest debugger available.** It needs many small commits to work.
- **Unpushed work is invisible.** A peer agent cannot see it, and a crashed session loses it.
- **The PR is where a human gets a say.** Landing a whole feature directly on `dev` skips review
  entirely; a segment-sized PR is small enough to actually read.

> **Recorded because it was violated.** The 2026-09-02 rebrand/AI-layer/demo-board work went in
> as 14 large commits pushed straight to `dev`, no branches and no PRs. It should have been
> roughly six PRs — rebrand, icon identity, kipy regen + MCP session control, GUI `OpenDocument`,
> theme + AI View plugin, demo board — each with its small commits underneath. The cost was real:
> a mid-work board generator bug (pad numbers are strings, so nets silently attached to nothing)
> sat inside a commit large enough that reverting it would have taken the working parts too, and
> no reviewer saw any of it before it reached `dev`. Do not copy that history as a pattern.

Subagents must not share a branch. Give each task its own branch off `origin/dev` (or have it
return a patch and commit it yourself). Two agents committing to one branch race on the index,
and `--ff-only` merges then fail for everyone.

## Two traps this repo already hit

**1. `phicad/CMakePresets.json` is gitignored.** `.gitignore:122` is the bare pattern
`CMakePresets.json`, which matches at *any* depth. `git add phicad/` silently skips it and
`phicad bootstrap` then breaks for the next clone.

```bash
git add -f phicad/CMakePresets.json
```

**2. Never edit the upstream `.gitignore`** (or any upstream file) to solve a PhiCad problem.
It buys a permanent merge conflict with upstream. `git add -f` is the conflict-free fix. This
generalizes: keep PhiCad changes inside `phicad/` (AGENTS.md rule 3).

After committing a force-added or otherwise unusual file, verify it actually landed:

```bash
git ls-tree -r HEAD -- phicad/
```

Do not trust that a commit contains what its message claims.
