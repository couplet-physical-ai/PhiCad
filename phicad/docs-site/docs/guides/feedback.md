# Feedback and privacy

PhiCad documentation has one feedback path for three useful outcomes:

- **Report a bug** captures build identity, product surface, workflow, reproduction frequency,
  steps, expected and actual behavior, bounded diagnostic context, support-bundle status, and
  severity.
- **Request a feature** starts with the problem, desired outcome, value, and acceptance criteria.
  It deliberately does not ask for a solution only; describing the need leaves room for the right
  design.
- **Improve this page** captures what was unclear or missing and includes the page's current
  canonical URL.

The **Feedback** control is available globally when the documentation assets are enabled. The
control opens an accessible modal dialog. It can be reached and operated with a keyboard, traps
focus while open, restores focus when closed, and closes with **Escape** or **Cancel**. On a small
screen the form becomes a single-column, full-width flow.

## How the handoff works

The form is intentionally a client-only handoff to a private GitHub repository:

1. The form runs in your browser and validates the fields locally.
2. On **Continue to GitHub**, it builds a structured URL for a new issue in
   [`couplet-physical-ai/PhiCad`](https://github.com/couplet-physical-ai/PhiCad/issues/new). The issue title,
   labels, and Markdown body are encoded as URL parameters.
3. GitHub opens the draft issue for your review. Nothing is submitted to the repository until you
   use GitHub's own final **Submit new issue** control.
4. Review every field and remove anything you do not want to share before submitting. GitHub's
   normal account, repository, and network policies then apply.

There is no PhiCad feedback endpoint or feedback database. The widget does not make a background
request, upload a file, or send anything when it opens, while you type, or while you switch between
feedback types. Building the draft URL is local; opening GitHub only happens after the explicit
**Continue to GitHub** click.

## What the widget includes

The draft contains the answers you entered and limited context generated locally:

- canonical page URL, reduced to its origin and path (query strings and fragments are removed);
- page title;
- browser user agent;
- browser language;
- viewport dimensions;
- coarse time zone; and
- UTC capture time.

The widget never reads or collects cookies, browser storage, tokens, prompts, chat transcripts,
file contents, board or schematic source, or environment values. It does not collect a referrer,
query string, or URL fragment. The page/browser context is included in the GitHub draft for
triage, not as a substitute for a support bundle.

### Separate improvement consent

The unchecked consent box is separate from the feedback submission. It asks whether the report
text that remains **after maintainer redaction** may be used to evaluate or improve PhiCad.
Leaving it unchecked is completely valid and does not prevent a bug report, feature request, or
page improvement from being opened.

That consent never includes attachments, support bundles, prompts, transcripts, or sensitive
design data. It also does not grant consent to upload local files. Review the generated GitHub
draft and the [privacy policy](../reference/feedback-privacy.md) before submitting it.

## Optional support-bundle attachment

A support bundle is optional and is never read by the feedback widget. When the PhiCad CLI is
available, create one locally with:

```bash
phicad support bundle
```

Use the command's reported output path, inspect the archive before sharing it, and remove anything
that should not leave your machine. The default support-bundle contract is redacted: it excludes
board source, schematic source, prompts, transcripts, credentials, arbitrary environment values,
and raw user files. A support bundle can still contain useful version, platform, and diagnostic
information, so **review it yourself** rather than treating the redaction as a substitute for
review.

To attach one:

1. Create and inspect the bundle locally.
2. Open the draft with **Continue to GitHub**.
3. Use GitHub's attachment control to add the reviewed archive to the draft issue.
4. Remember that the separate improvement consent does **not** include the attachment or any
   sensitive design data in it.
5. Submit the issue only after reviewing both the text and the attachment.

The widget does not know whether you attached a bundle and does not place a local path in the
issue body automatically.

## What a bug draft asks for

A bug draft includes the PhiCad version/build identity, product surface, workflow, reproduction
frequency, steps, expected and actual behavior, optional sanitized error text, coarse project
context, support-bundle status, and severity. The form refuses obvious credential-looking values
before it continues to GitHub. This is a guardrail, not a guarantee: review the draft yourself.

## For maintainers

The user flow stops at the private issue queue. Queue filters, labels, priority rules, lifecycle
targets, privacy escalation, and bounded agent handoff rules live in
[Feedback administration](../reference/feedback-admin.md). The machine-readable record contract
is `phicad/support/feedback-schema.json` in the source tree.

## Enabling the global control

The site configuration that owns the final MkDocs theme should load both assets:

```yaml
extra_css:
  - assets/stylesheets/feedback.css
extra_javascript:
  - assets/javascripts/feedback.js
```

The JavaScript creates the control and dialog after the document is ready, so no page template
markup is required. The URL-building functions are also exposed as `window.PhiCadFeedback` for
site-level tests and integration checks; they do not perform network operations themselves.
