# Release-one readiness

This is the release owner's **go/no-go runbook** for the first distributed PhiCad desktop build.
It records what can be prepared in source and what still requires a reviewer, a signing identity, or
explicit release permission. A green source branch is not a downloadable desktop release.

Status checked: **2026-09-17**. Current decision: **NO-GO for publication**.

## Decision summary

| Gate | Current evidence | Verdict |
| --- | --- | --- |
| Fork-layer source gates | Local integration commit `b599e387c3f0` passes 24 fast gates plus the separately executed pinned Ruff gate | Prepared locally; not pushed, reviewed, or landed |
| Agent View authentication | The token-disclosure and one-board/one-server/one-tab guards are present in the local integration | Release blocker until reviewed and landed |
| Phi credential handoff | Public `phi auth token --json` handoff and `client=phicad` attribution are present in the local integration | Release blocker until reviewed and landed |
| Verify/Circuit JSON architecture | The #113/#114 stack is assembled in the local integration | Not reviewed or landed |
| Product site and feedback | Assembled and validated in the local integration | Not reviewed, landed, or deployed |
| macOS application bundle | A complete arm64 Release build from clean local commit `b599e387c3f0` stages relocatably and passes strict ad-hoc signature verification, but its CLI cannot launch in the current broken login/bootstrap environment | Packaging prepared; runtime and distribution gates remain blocked |
| Developer ID signature | Current application is ad-hoc signed and has no Team Identifier | **Blocked** |
| Apple notarization/Gatekeeper | No notarized/stapled artifact; Gatekeeper rejects the current bundle | **Blocked** |
| Downloadable release asset | Existing prereleases have no application assets | **Blocked** |
| Desktop update channel | No signed feed/updater exists | Deferred until signed artifacts exist |
| Explicit approval | The release owner has not approved publication | **Blocked by design** |

## Pull-request integration order

Do not replace review with a bulk merge. The dependency-safe order is:

1. **#113 — Verify V1 and PhiCad 2 architecture** (`docs/phicad-2-architecture`,
   `d46fb75583`).
2. **#114 — Circuit JSON apply path** (`feat/circuit-json-apply`, `6e5492348c`), which
   contains #113 and remains draft until its own review is complete.
3. **#115 — application presence and Agent View token/reuse guards**
   (`fix/app-presence-guards`, `76ddbd0ab6`). The unauthenticated-page fix is a release blocker.
4. **#116 — Phi-only per-person credentials and PhiCad spend attribution**
   (`feat/phi-baked-in`, `469cac52c3`).
5. **Release-one readiness** — product site, feedback operations, privacy-safe support bundle,
   docs, and version reporting. This branch is stacked on #116 and should target `dev` after #116
   lands.

#115 and #116 both change `phicad/bin/phicad` and Agent View documentation. Git's three-way
preview reports real conflict markers; a reviewer must preserve **both** sets of gates and doctor
behavior. Never resolve the conflict by dropping either security test. Local branch
`feat/release-one-integrated` demonstrates one conflict resolution through `b599e387c3f0`, but it is
not a substitute for a pushed branch, review, required CI, or a landed `dev` commit.

## Local integration preparation evidence

Clean local integration commit `b599e387c3f0` has passed the checks that do not require landing the
PR stack, a functioning macOS login bootstrap, or a signed distribution identity:

- `phicad --version` regression tests: **5 passed**;
- support-bundle privacy/canary tests: **10 passed**;
- feedback-widget behavior and privacy tests: **7 passed**;
- documentation source release tests: **6 passed**, including the document-scroll contract;
- schema checks in the automation/MCP environment: **5 schemas passed** structural validation and
  **4 shipped instances passed** full `jsonschema` validation, including the synthetic feedback
  record;
- `phicad check fast`: **24 gates passed**, 1 explicitly reported Ruff skip, 0 failed, 0 broken;
- pinned Ruff **0.16.6** run separately across `phicad/`: passed with no findings;
- the Agent View gate ran **143 cases**, with 10 expected display/host-specific skips in the
  headless shell;
- the pinned TypeScript engine suites passed **14/14** core checks and **46/46** hardening checks;
- strict `mkdocs` build: completed with **zero warnings**;
- rendered landing-page QA at **1500 × 950** and **485 × 900**: no horizontal overflow, every
  landing image loaded with alternative text or an explicit decorative treatment, the local video
  exposed controls and transcript fallback, and the page had one main landmark and no unnamed
  buttons;
- real document scrolling was exercised in an embedded browser: the page moved from `scrollY=0` to `scrollY=900`,
  updated its progress indicator, exposed desktop/mobile section navigation and active state, and
  retained reduced-motion and no-JavaScript fallbacks; and
- feedback-dialog QA at both widths: contained by the viewport, focus entered the modal, Escape
  closed it, and focus returned to the trigger.

Key light-surface and action color pairs meet WCAG AA for normal text: dark red on Paper
(**7.59:1**), Neutral on Paper (**6.64:1**), Carbon on Orange (**7.25:1**), Carbon on Red
(**5.04:1**), and white on the feedback action red (**7.38:1**). This is preparation evidence for
this local branch, not permission to publish it. Native-dependent TypeScript service checks report
**23 passed, 5 failed, 1 skipped**, and apply checks report **13 passed, 2 failed, 1 skipped** because
the native CLI cannot produce artifacts in the current shell. Those are open gates, not waived
failures. The complete integration and native evidence below is still required at the final landed
candidate commit.

## Source gates before approval

Run from a clean integration worktree at the exact candidate commit:

```bash
phicad/bin/phicad status
python3 phicad/bin/check_fork_boundary.py --base origin/dev --head HEAD
phicad/bin/phicad check fast
phicad/bin/phicad check
python3 -m unittest discover -s phicad/plugins/ai-view -p 'test_*.py' -v
build/mcp-venv/bin/python phicad/mcp/test_registry.py
build/mcp-venv/bin/python phicad/mcp/test_tools.py --quick
```

Also require, with exact logs attached to the release evidence:

- strict documentation build;
- `ruff check --no-cache phicad` with the repository's pinned Ruff version;
- gateway/model self-tests and one live Phi completion without exposing the credential;
- TS engine core, service, and hardening suites;
- native schematic, PCB, and 3D renders;
- native DRC on the release smoke design;
- staging verification that the bundled Agent View includes reviewed runtime files and excludes
  tests; and
- `git diff --check` plus a clean `git status --short`.

A source-only GitHub Actions run does not replace the native macOS evidence.

## Desktop artifact gate

The existing command:

```bash
PHICAD_BUILD=build/mac-release phicad/bin/phicad install
```

cleans a disposable install prefix, stages `PhiCad.app`, checks its structure/signature, and runs
its CLI outside build-tree mode. That is necessary, but the current smoke explicitly does **not**
prove Developer ID signing, notarization, quarantine/Gatekeeper launch, DMG behavior, Intel macOS,
Windows, or Linux.

The latest local packaging check is still not shipping evidence. From clean local source commit
`b599e387c3f0`, a complete arm64 Release build succeeded and `phicad install` produced a 352,176 KiB
staged application with all six kifaces, a real `SharedSupport` directory and bundled AI View, a
materialized ngspice library and nine codemodel files, no broken symlinks, and no non-system absolute
dylib dependencies. `codesign --verify --deep --strict` accepts the ad-hoc nested signatures.

The final CLI smoke then exits by signal 6 before emitting a version because this shell cannot
register a macOS application with AppKit/HIServices. The staged app is still ad-hoc signed, has no
Team Identifier, and Gatekeeper rejects it. `phicad version --json` therefore correctly reports
`release: unreleased`, `commit: b599e387c3f0`, `dirty: false`, and `native_version: null`. Repeat the
runtime tests from a valid logged-in desktop account before treating this as a native candidate.

That exact binary also records a minimum macOS version of **26.0** and SDK **26.5**. Its
`CFBundleShortVersionString` and `CFBundleVersion` still contain Git-describe strings rather than an
approved conventional numeric release/build pair. Neither value is a support policy: choose and
test the minimum macOS version, architecture set, marketing version, and build number explicitly.
The current upstream CMake path forcibly resets signing settings to ad-hoc defaults, so passing a
Developer ID with `-D` is not a working signing path. A reviewed post-staging signer or equivalent
release automation is required before notarization.

Before publication, the release owner needs all of the following:

- [ ] an approved minimum macOS version, architecture set, numeric marketing version, and build number;
- [ ] a maintainer-controlled Apple Developer ID Application identity;
- [ ] a reviewed signing path that does not get reset to ad-hoc CMake defaults;
- [ ] hardened-runtime entitlements reviewed for PhiCad and its six kifaces;
- [ ] every nested executable/library signed in the correct order;
- [ ] `codesign --verify --deep --strict --verbose=2 PhiCad.app` passes;
- [ ] a notarization submission succeeds and the ticket is stapled;
- [ ] `spctl --assess --type execute --verbose=4 PhiCad.app` accepts the quarantined artifact;
- [ ] a versioned arm64 asset is produced with SHA-256 and byte size recorded;
- [ ] the asset is installed and launched on a clean supported Mac account; and
- [ ] the GitHub release page names supported hardware/OS, known limitations, checksums, and
  rollback instructions.

Do not ship an unsigned auto-updater. A TS-engine dependency update may be handled through its
package/lockfile review before desktop OTA exists; the desktop channel must authenticate signed
artifacts and resist downgrade/replay.

## Version identity

`phicad --version` reports the exact source tag (or `unreleased`), commit, dirty state, and runnable
native CLI version. This makes a support report attributable; it does **not** choose a release name.
The release owner must create and approve the product version/tag before packaging, and the same
identifier must appear in the app metadata, archive name, checksum manifest, release notes, and
feedback diagnostics.

## Documentation and feedback gate

Before deployment:

- [ ] the strict docs build has no warnings, orphan pages, broken local assets, accidental
  machine-local prose references, or external runtime images;
- [ ] the responsive landing page is visually checked at desktop and narrow widths, including the
  documentation shell's available color schemes;
- [ ] videos have controls, captions or transcript links, and readable fallback text;
- [ ] the feedback dialog is keyboard operable and sends no background request;
- [ ] canonical type, triage, workflow, priority, privacy, improvement, and release-blocker labels
  exist in the private repository;
- [ ] a test report can be reviewed locally, opened in GitHub, triaged, assigned, and closed;
- [ ] improvement consent is unchecked by default and remains separate from diagnostics consent;
- [ ] support-bundle canary tests prove credentials, raw design files, prompts, and transcripts are
  excluded; and
- [ ] retention/deletion, security escalation, and agent-processing rules are approved.

See [Feedback data, diagnostics, and privacy](feedback-privacy.md) and the
[feedback administration guide](feedback-admin.md).

## Publication sequence—only after explicit approval

1. Land reviewed PRs into `dev`; resolve the #115/#116 overlap while preserving every test.
2. Re-run the complete source/native/docs evidence above at the exact `origin/dev` commit.
3. Tag the approved immutable commit with the approved version.
4. Build, sign, notarize, staple, quarantine-test, and checksum the desktop artifact.
5. Draft release notes and upload assets without marking the release public.
6. Verify asset download, checksum, signature, launch, Agent View authentication, and rollback on a
   clean machine.
7. Ask the release owner for the final go/no-go with the evidence links and remaining limitations.
8. Only after **GO**, run `phicad/bin/phicad release` to fast-forward `master` from validated
   `origin/dev`, then publish the prepared release.

## Rollback

A release is not ready without the previous known-good asset and tag. Record both before
publication. If a critical defect appears:

1. stop distribution or mark the release unavailable;
2. publish the known-good signed asset rather than rewriting an existing tag;
3. open a `release-blocker` issue with impact, affected versions, and owner;
4. fix through a reviewed branch into `dev`;
5. issue a new, monotonically newer signed version; and
6. retain the minimal incident/release record while deleting sensitive diagnostics on the privacy
   schedule.

Never force-push `master`, move a published version tag, or silently replace an asset under the
same name.
