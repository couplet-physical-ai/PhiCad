# Feedback support contract

This directory contains the contract for PhiCad feedback records. It is deliberately not a
feedback service, queue worker, database, or upload endpoint. The site prepares a human-reviewable
GitHub issue body; a maintainer or agent may later normalize its redacted fields and diagnostic
references into this record. A local support bundle has its own manifest and is never silently
converted into feedback. A report leaves the user's machine only after the user has reviewed it
and explicitly consented to the GitHub submission. The private
`couplet-physical-ai/PhiCad` GitHub repository is the durable source of feedback.

## Files and ownership

| File | Contract | Owner |
| --- | --- | --- |
| [`feedback-schema.json`](feedback-schema.json) | Versioned JSON Schema for a normalized maintainer/agent record. Site issue text and reviewed bundle references are inputs, not implicit schema instances. | The support/feedback maintainers; producer changes must be validated against it. |
| [`feedback-example.json`](feedback-example.json) | Redacted, synthetic record validated by the schema gate; it contains no report from a person or product design. | The support/feedback maintainers; keep it representative and privacy-safe. |
| [`../docs-site/docs/reference/feedback-admin.md`](../docs-site/docs/reference/feedback-admin.md) | Maintainer and agent operating procedure: queues, triage, privacy, priority, release blockers, and handoff. | PhiCad maintainers and the release owner. |

The schema owns field names, types, redaction gates, consent semantics, and the prohibition on
secrets and raw design content. The admin page owns GitHub labels and the human/agent lifecycle.
Neither file implies that a dynamic dashboard or backend exists. The admin page is a static,
maintainer-facing set of links and commands around GitHub.

## Version 1.0 boundary

`feedback-schema.json` has `$id` `urn:phicad:feedback-schema:1.0` and requires
`schema_version: "1.0"`. This is the feedback-record version; an archive manifest may carry its
own support-bundle format version and must not be treated as this record. A valid record contains,
at minimum:

- product and PhiCad version, platform, and named workflow;
- a redacted reproduction, expected result, and actual result;
- provenance for the site, local support bundle, agent, or maintainer that emitted it;
- a redaction status/profile and references to separately stored diagnostics; and
- three separate consent decisions: feedback submission, diagnostics, and the unchecked
  improvement-report opt-in.

The record is intentionally closed (`additionalProperties: false`). It has no fields for secrets,
credentials, raw board/schematic/layout data, prompts, transcripts, arbitrary environment values,
or attachments. Diagnostic values are references only. A producer must stop locally when
`redaction_status` is `not_run`, `in_progress`, or `failed`; only `passed` may accompany a true
`consent.diagnostics` value, and a false diagnostics consent requires an empty
`diagnostics_refs` list.

## Schema evolution

1. **Patch changes (`1.0.x`)** fix wording or schema metadata without changing validation.
2. **Minor changes (`1.x`)** may add optional fields or enum values only when existing 1.0
   producers remain valid and the privacy boundary is unchanged. The new field must be documented
   here and on the admin page if it affects triage.
3. **Major changes (`2.0`, etc.)** are required for removing or renaming fields, changing a
   field's meaning/type, making an optional field required, weakening redaction, or changing
   consent semantics. Preserve the old versioned schema for existing records; add the new schema
   as a separately versioned file rather than silently changing the meaning of `1.0`.
4. Every schema change must update the producer and its validation fixture in the same change,
   state the migration impact in the change description, and include a backwards-compatibility
   note. Never make a consumer guess which version it received.
5. A new field must have an owner, a maximum size, a redaction rule, and a reason it cannot be
   represented by an existing field. Do not add a catch-all `metadata`, `payload`, or attachment
   field to bypass the closed schema.

Consent is not retroactive: changing a schema version must not turn an old `false` consent into
`true`. A report marked as an improvement report is valid only when its separate
`consent.improvement_report` value is explicitly `true`.

## Local validation

From the repository root, parse every published schema and validate the synthetic feedback record
without any network access. Full instance validation runs when `jsonschema` is installed:

```bash
python3 phicad/bin/check_schemas.py
```

If the optional `jsonschema` Python package is installed, validate the schema's own structure as
well:

```bash
python3 - <<'PY'
import json
from pathlib import Path

from jsonschema import Draft202012Validator

path = Path("phicad/support/feedback-schema.json")
schema = json.loads(path.read_text())
Draft202012Validator.check_schema(schema)
print(f"valid Draft 2020-12 schema: {path}")
PY
```

The admin procedure contains the proxy-safe GitHub commands. Do not add a network call or a
credential to this directory to make validation convenient.
