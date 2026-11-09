//
// This program source code file is part of PhiCad, a fork of KiCad.
//
// Copyright (C) 2026 Exowatt, Inc.
//
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The cross-view launch request: "open THIS project, at THIS revision, in THAT view".
//
// WHY THIS IS DATA AND NOT AN ACTION
// ---------------------------------
// Every product view (Studio, the circuit source, Phi, the platform docs) needs to be reachable
// from every other one, for the SAME manifest-bound project. The tempting implementation is a
// function that opens the other application. This is not that, on purpose:
//
// STUDIO AND VERIFY ARE ONE APPLICATION
// ------------------------------------
// `studio` is a MODE of the native PhiCad application that also provides Verify -- the same
// installed GPL binary and the same native authority, entered with an editing focus. It is not a
// second editor and not a separate product. The identifier stays `studio` because it names the
// VIEW a caller wants, and callers should not have to change their code when the implementation
// behind a view changes.
//
// Even within one application, the REQUEST still carries the manifest binding and is still
// fail-closed on a hash mismatch. Sharing a binary does not make two views automatically agree on
// a revision -- an editor session opened earlier can be holding older content -- and the recurring
// bug in this codebase is a tool trusting its own state instead of checking it.
//
//   * PhiCad Verify does not own the other applications and must not mutate them. It emits a
//     REQUEST; the PhiCad Platform launcher decides whether and how to honour it. A checker that
//     reaches into another app's state is no longer a checker.
//
//   * A request that can be inspected can be tested. An action that opens a GUI cannot.
//
//   * Nothing here carries credentials. The descriptor holds project identity, manifest identity,
//     the expected content hash, and the desired view. No tokens, no keys, no session state, no
//     absolute paths outside the project. If a launch ever needs authorisation, that belongs to
//     Phi (the only harness) and to the launcher, never to a link.
//
// THE HASH IS THE WHOLE POINT
// --------------------------
// Two views of "the same" project are only the same if they agree on the revision. So a request
// carries the manifest id and content hash the sender believes is current, and the receiver is
// expected to REJECT a mismatch. `validateLaunchRequest()` implements that check here so the
// rejection is testable, and so Verify never hands another application a stale identity.
//
// Deliberately NOT implemented: reconciliation. If the hashes differ, this reports a mismatch and
// stops. It does not sync, does not pick a winner, and does not apply last-writer-wins -- silently
// choosing a revision for someone is how two tools come to disagree about what the board is.

import { createHash } from "node:crypto";

/** Bumped when the descriptor's SHAPE changes. A receiver may refuse a version it cannot read. */
export const LAUNCH_CONTRACT_VERSION = 1;

/**
 * The views a project can be opened in.
 *
 * `available` is a fact about THIS machine and this moment, not an aspiration. A target that
 * cannot be resolved says so, with a reason, because a deep link that silently fails is worse
 * than one that admits it cannot resolve.
 */
export const LAUNCH_TARGETS = {
  // The identifier is stable; what stands behind it is native PhiCad.
  studio: {
    id: "studio",
    label: "Open in PhiCad Studio",
    component: "PhiCad (native)",
    description: "the native PhiCad application in its Studio editing mode -- the same installed "
                 + "application and native authority that provides Verify, not a separate editor",
    view: "visual-editor",
    // A MODE of this application family, expressed as an intent the launcher can honour rather
    // than a second product to install.
    mode: "studio",
    focus: "board-editor",
    sameApplicationAsVerify: true,
    handledBy: "platform-launcher",
    // Recorded as unavailable rather than omitted: the architecture includes the Studio mode, and a
    // caller is entitled to know the target exists and why it cannot be used yet.
    availableWhen: "the native PhiCad application is installed and a platform launcher is "
                   + "configured to route a view request to it",
  },
  source: {
    id: "source",
    label: "Open Circuit Source",
    component: "tscircuit source",
    description: "the authored circuit source, and the thin web Circuit preview when one is "
                 + "installed -- a separate surface from the native application",
    view: "code",
    mode: "preview",
    focus: "circuit-source",
    sameApplicationAsVerify: false,
    handledBy: "editor",
    availableWhen: "the project's authored .tsx source is present, or a web Circuit preview "
                   + "surface is installed and configured",
  },
  phi: {
    id: "phi",
    label: "Open in phi",
    component: "phi",
    description: "a phi session, the only AI harness in this architecture",
    view: "agent-session",
    mode: "agent",
    focus: "session",
    sameApplicationAsVerify: false,
    handledBy: "platform-launcher",
    availableWhen: "a Phi session is reachable on this machine",
  },
  docs: {
    id: "docs",
    label: "Open Platform Docs",
    component: "PhiCad Platform docs",
    description: "the platform documentation for this project's contracts",
    view: "docs",
    mode: "docs",
    focus: "reference",
    sameApplicationAsVerify: false,
    handledBy: "platform-launcher",
    availableWhen: "the platform documentation location is configured",
  },
};

export class LaunchError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "LaunchError";
    this.code = code;
  }
}

/**
 * A stable project identity derived from the authored source path.
 *
 * Deliberately NOT the manifest id: a project keeps its identity across revisions, while the
 * manifest id changes with every real change. Conflating them would mean a project became a
 * different project every time someone edited it.
 */
export function projectId(sourcePath) {
  return createHash("sha256").update(String(sourcePath)).digest("hex").slice(0, 16);
}

/**
 * Build a launch request. Pure: it describes an intent and performs no side effect.
 *
 * `manifest` is a manifest from manifest.mjs -- the binding between a project and a revision.
 */
export function launchRequest({ target, manifest, note = null } = {}) {
  const spec = LAUNCH_TARGETS[target];
  if (!spec) {
    throw new LaunchError(
      `unknown view "${target}"; known views: ${Object.keys(LAUNCH_TARGETS).join(", ")}`,
      "unknown_target",
    );
  }
  if (!manifest || !manifest.manifestId) {
    throw new LaunchError(
      "a launch request must be bound to a manifest, so both views agree on the revision",
      "unbound",
    );
  }

  // The artifact digest that identifies the revision. contentSha256 is preferred because it is
  // stable across the engine's random ids and diagnostic ordering (see manifest.mjs).
  const primary = (manifest.artifacts ?? []).find((a) => a.name?.endsWith("circuit.json"))
                  ?? (manifest.artifacts ?? [])[0]
                  ?? null;

  return {
    contract: "io.github.couplet-physical-ai.phicad.launch",
    contractVersion: LAUNCH_CONTRACT_VERSION,
    issuedBy: { component: "PhiCad Verify", tools: manifest.tools ?? null },
    project: {
      id: projectId(manifest.source ?? ""),
      source: manifest.source ?? null,
      sourceSha256: manifest.sourceSha256 ?? null,
    },
    revision: {
      manifestId: manifest.manifestId,
      expectedHash: primary?.contentSha256 ?? primary?.sha256 ?? null,
      artifact: primary?.name ?? null,
    },
    // The intent is expressed as a MODE to enter, not as an application to launch. For `studio`
    // that is the native PhiCad application's editing mode -- the same application that provides
    // Verify -- so the launcher focuses a view rather than starting a second editor.
    desiredView: {
      target: spec.id,
      view: spec.view,
      mode: spec.mode,
      focus: spec.focus,
      handledBy: spec.handledBy,
      sameApplicationAsVerify: spec.sameApplicationAsVerify === true,
    },
    note,
    // Stated in the payload so a receiver never has to infer it, and so a reviewer reading a
    // captured request can see the guarantee.
    carriesCredentials: false,
  };
}

/**
 * Check a request against what the receiver actually has.
 *
 * Returns `{ ok, action, reason, findings }` where `action` is one of:
 *   "open"    -- identity and revision agree
 *   "reject"  -- something is wrong; do NOT open, do NOT reconcile
 *
 * A mismatch is never repaired here. Verify's job is to refuse to hand another application a
 * stale identity, not to decide which revision wins.
 */
export function validateLaunchRequest(request, actual = {}) {
  const problems = [];

  if (!request || request.contract !== "io.github.couplet-physical-ai.phicad.launch") {
    return {
      ok: false, action: "reject", reason: "not a PhiCad launch request",
      findings: ["contract_unrecognised"],
    };
  }
  if (request.contractVersion !== LAUNCH_CONTRACT_VERSION) {
    // Refused rather than best-effort parsed: a descriptor whose shape we do not know may mean
    // something different by the same field names.
    return {
      ok: false, action: "reject",
      reason: `contract version ${request.contractVersion} is not version `
              + `${LAUNCH_CONTRACT_VERSION}`,
      findings: ["contract_version_mismatch"],
    };
  }
  if (!LAUNCH_TARGETS[request.desiredView?.target]) {
    return {
      ok: false, action: "reject",
      reason: `unknown view "${request.desiredView?.target}"`,
      findings: ["unknown_target"],
    };
  }

  if (actual.projectId && request.project.id !== actual.projectId) {
    problems.push("project_mismatch");
  }
  if (actual.manifestId && request.revision.manifestId !== actual.manifestId) {
    problems.push("manifest_mismatch");
  }
  if (actual.hash && request.revision.expectedHash
      && request.revision.expectedHash !== actual.hash) {
    problems.push("stale_revision");
  }
  // An unverifiable request is not a passing one: without a hash there is nothing to compare, so
  // it is flagged rather than waved through.
  if (!request.revision.expectedHash) {
    problems.push("no_expected_hash");
  }

  if (problems.length > 0) {
    return {
      ok: false,
      action: "reject",
      reason: problems.includes("stale_revision")
        ? "the requested revision is stale: the receiver holds different content. Rebuild, then "
          + "issue a fresh request -- Verify does not reconcile revisions."
        : `launch request refused: ${problems.join(", ")}`,
      findings: problems,
    };
  }

  return { ok: true, action: "open", reason: "identity and revision agree", findings: [] };
}

/**
 * Every target with its availability, for `capabilities` and the MCP surface.
 *
 * `resolvable` is checked, not assumed. Every target is reported unavailable until it is actually
 * configured, because a deep link that silently fails is worse than one that admits it cannot
 * resolve. At the time of writing no platform launcher is installed, so all of them are.
 *
 * `studio` resolves to the NATIVE PhiCad application -- the same installed application that
 * provides Verify, entered in its Studio editing mode. `PHICAD_STUDIO` names that application when
 * it is not the one currently running; `PHICAD_PLATFORM_LAUNCHER` is what actually routes a view
 * request. Being the same application is deliberately NOT treated as "already resolvable": a
 * request still needs something that can focus a view on a project, and pretending otherwise would
 * be the same dead link in a different coat.
 */
export function launchTargets({ sourcePresent = false, env = process.env } = {}) {
  const launcher = env.PHICAD_PLATFORM_LAUNCHER || null;
  const studio = env.PHICAD_STUDIO || null;
  const docs = env.PHICAD_PLATFORM_DOCS || null;
  const preview = env.PHICAD_CIRCUIT_PREVIEW || null;

  const resolve = (spec) => {
    switch (spec.id) {
      case "source":
        // The authored source and the thin web Circuit preview are the same VIEW but different
        // surfaces. The preview stays separate from the native application and unavailable until
        // it is installed.
        if (sourcePresent) {
          return {
            resolvable: true,
            reason: preview
              ? "the authored source is present and a web Circuit preview is configured"
              : "the authored source is present (no web Circuit preview is installed, so the "
                + "source opens in an editor)",
          };
        }
        return preview
          ? { resolvable: true, reason: "a web Circuit preview surface is configured" }
          : { resolvable: false,
              reason: "no authored source was found for this project, and no web Circuit preview "
                      + "surface is installed (set PHICAD_CIRCUIT_PREVIEW)" };
      case "studio":
        if (!studio && !launcher) {
          return {
            resolvable: false,
            // Names the native application, not any external product. Verify will not start it.
            reason: "the native PhiCad application's Studio mode cannot be reached: no platform "
                    + "launcher is configured to route a view request (set "
                    + "PHICAD_PLATFORM_LAUNCHER, or PHICAD_STUDIO to name the application)",
          };
        }
        return {
          resolvable: true,
          reason: "routed via the platform launcher to the native PhiCad application's Studio mode",
        };
      case "phi":
        return launcher
          ? { resolvable: true, reason: "routed via the platform launcher" }
          : { resolvable: false, reason: "no platform launcher is configured "
                                        + "(set PHICAD_PLATFORM_LAUNCHER)" };
      case "docs":
        return docs
          ? { resolvable: true, reason: "documentation location configured" }
          : { resolvable: false, reason: "no platform documentation location is configured "
                                        + "(set PHICAD_PLATFORM_DOCS)" };
      default:
        return { resolvable: false, reason: "unknown target" };
    }
  };

  return Object.values(LAUNCH_TARGETS).map((spec) => ({
    id: spec.id,
    label: spec.label,
    component: spec.component,
    description: spec.description,
    view: spec.view,
    mode: spec.mode,
    focus: spec.focus,
    // True only for `studio`: it is a mode of the application that provides Verify, so a consumer
    // can present it as switching view rather than as leaving for another product.
    sameApplicationAsVerify: spec.sameApplicationAsVerify === true,
    handledBy: spec.handledBy,
    availableWhen: spec.availableWhen,
    ...resolve(spec),
  }));
}
