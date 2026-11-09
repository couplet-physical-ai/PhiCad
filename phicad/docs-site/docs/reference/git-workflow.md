# Git workflow

PhiCad work moves through feature branches and the `dev` integration branch before release.

- `master` is the release branch and must not receive direct feature work.
- Changes target `dev`, where upstream synchronization and review happen first.
- Feature work belongs in a worktree or branch based on the current integration tip.
- Keep commits focused, build and test narrowly, and push meaningful progress promptly.
- A validated `dev` state is promoted to `master` by fast-forward release.
- Every PR must pass the required **`fork layer (no C++ build)`** CI check — it lints and tests
  the `phicad/` layer (plus CodeQL analysis) without building the C++ tree, so it gates in
  minutes. C++ builds and QA suites remain a local responsibility (build and test narrowly).

Source of truth: [phicad/git-workflow.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/git-workflow.md)
