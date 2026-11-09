# Feedback data, diagnostics, and privacy

Release one treats feedback as product data with an audit trail—not as permission to collect a
person's workstation. This page defines the data boundary for the documentation feedback control,
GitHub issue queue, support bundles, maintainers, and agents.

## Release-one data flow

1. The documentation site builds a report draft **in the browser**.
2. Nothing is posted to PhiCad or GitHub until the reporter chooses **Continue to GitHub**.
3. GitHub displays the full draft. The reporter can edit or abandon it before submitting.
4. A submitted issue enters the private `couplet-physical-ai/PhiCad` repository and inherits that
   repository's access controls and audit history.
5. Maintainers triage the issue in GitHub. There is no second feedback database and no anonymous
   upload endpoint in release one.

The documentation control may add the current page URL, browser user-agent, viewport size,
language, time zone, and UTC time to a draft. It does not read cookies, local storage, clipboard,
filesystem contents, prompts, transcripts, credentials, or environment variables.

## Data classes

| Class | Examples | Default handling |
| --- | --- | --- |
| Reporter-authored text | summary, steps, expected and actual behavior, desired outcome | included only after GitHub submission |
| Coarse environment facts | PhiCad release/commit, native version, OS, CPU architecture, workflow | useful for triage; avoid names and account identifiers |
| Diagnostics | bounded command outcome flags and strictly parsed version/source identifiers; no raw command output | optional; attach only after previewing |
| Project metadata | explicit project file names, sizes, and SHA-256 hashes | excluded by default; metadata only when `--project` is supplied |
| Project contents | schematic, PCB, Circuit JSON, net names, geometry, fabrication data | never collected by the default support bundle |
| Secrets and identity material | tokens, cookies, credentials, private keys, arbitrary environment values | forbidden; redact and rotate immediately if exposed |
| Improvement eligibility | explicit consent to use sanitized report text for evaluation or improvement | unchecked by default; separate from submitting feedback |

A request for “all diagnostics” means all **whitelisted, relevant, sanitized facts** in the support
bundle—not an unrestricted disk, process, browser, network, or conversation capture.

## Support bundles

Generate a local previewable archive with:

```bash
phicad support bundle
```

Add `--project PATH` only when project-level metadata is necessary. The collector accepts only a
recognizable KiCad/PhiCad project; filesystem roots, the home directory, and unrelated directories
are refused. It then adds names, sizes, and SHA-256 hashes under independent directory, entry,
file, and byte bounds; it still does not copy project contents. The bundle never invokes
`phi auth token`, reads Phi's credential store, collects prompt/transcript history, or copies
arbitrary logs. Review every archive before attaching it.

## Three separate choices

Submitting a report, attaching diagnostics, and allowing improvement use are different decisions:

1. **Submit feedback** — the reporter explicitly submits the GitHub issue.
2. **Share diagnostics** — the reporter explicitly generates, reviews, and attaches a support
   bundle.
3. **Allow improvement use** — an unchecked control permits sanitized report text to become
   eligible for a future, human-reviewed evaluation or improvement dataset.

The third choice does not include attachments, project data, credentials, or sensitive design
information. Release one performs **no automatic training export**. Consent marks eligibility; a
maintainer must still review redaction, purpose, and provenance before any later use.

## Access and audit

Feedback is visible only to collaborators who can access the private PhiCad repository. GitHub
records issue authorship, edits, comments, labels, assignment, and closure. Agents use the same
queue and must identify automated triage comments. They may summarize or classify a report, but
must not download sensitive attachments into unrelated worktrees or copy report data into prompts
that are broader than the triage task.

The [feedback administration guide](feedback-admin.md) defines the lifecycle, labels, release
blocker criteria, and agent handoff format.

## Retention and deletion

Release-one defaults are deliberately bounded:

- issue text and its triage history are reviewed for deletion **24 months after closure**;
- support bundles and other diagnostic attachments are removed no later than **90 days after the
  issue is resolved**, and sooner when they are no longer needed;
- an attachment containing a secret or unrelated personal/project data is removed immediately;
  the affected credential owner is told to rotate the credential;
- a minimal release-blocker record may be retained with the corresponding release evidence after
  diagnostic attachments are removed; and
- sanitized improvement excerpts are not created without explicit consent and provenance. A
  withdrawal stops future use and triggers deletion from any PhiCad-maintained evaluation set.

A reporter can request correction, redaction, consent withdrawal, or deletion by commenting on the
issue. If the request itself is sensitive, contact a repository owner privately and cite only the
issue number. GitHub backups and organization-wide retention controls remain governed by GitHub
and the operator's policy; PhiCad does not claim it can erase those systems independently.

## Security reports

Do not put an exploitable vulnerability, credential, or sensitive proof-of-concept into a normal
feedback issue. Use the repository's private security-advisory flow:

[Open PhiCad security advisories](https://github.com/couplet-physical-ai/PhiCad/security/advisories){ .md-button }

If that flow is unavailable to the reporter, contact a PhiCad repository owner privately. A
maintainer who finds a security report in the normal queue must stop agent processing, remove
sensitive content where possible, and move the conversation to the private security workflow.

## Abuse protection

Release one accepts no anonymous form posts. GitHub authentication, private-repository membership,
and GitHub's own rate limits are the first abuse boundary. The feedback control also enforces
bounded field lengths and does not upload files. Maintainers may lock abusive threads, revoke
repository access through normal organization controls, and preserve only the minimum audit record.

## Agent rules

An agent processing feedback must:

1. read the issue without expanding unrelated attachments;
2. check for secrets, personal data, proprietary design content, and security signals before
   summarizing;
3. apply `privacy-review` and stop when redaction is uncertain;
4. treat improvement consent as false unless the report says true explicitly;
5. preserve issue URL, state changes, owner, and next action in handoffs; and
6. never infer permission to train on attachments or design data from permission to use sanitized
   report text.
