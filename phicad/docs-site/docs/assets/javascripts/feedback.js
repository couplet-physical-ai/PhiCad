/*
 * PhiCad documentation feedback handoff.
 * Copyright (C) 2026 Exowatt, Inc.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

(function (root) {
    "use strict";

    const ISSUE_URL = "https://github.com/couplet-physical-ai/PhiCad/issues/new";
    const MAX_TEXT_LENGTH = 4000;
    const MAX_TITLE_LENGTH = 120;
    const MAX_ISSUE_URL_LENGTH = 12000;

    const FEEDBACK_TYPES = {
        bug: {
            label: "Report a bug",
            titlePrefix: "Bug",
            labelValue: "feedback,needs-triage,bug"
        },
        feature: {
            label: "Request a feature",
            titlePrefix: "Feature request",
            labelValue: "feedback,needs-triage,enhancement"
        },
        improve: {
            label: "Improve this page",
            titlePrefix: "Docs improvement",
            labelValue: "feedback,needs-triage,documentation"
        }
    };

    const FIELD_NAMES = {
        bug: [
            "summary", "product_version", "surface", "workflow", "command_action",
            "frequency", "steps", "expected", "actual", "error_excerpt",
            "project_context", "support_bundle_status", "severity"
        ],
        feature: ["problem", "outcome", "value", "acceptance"],
        improve: ["improvement"]
    };

    const REQUIRED_FIELD_NAMES = {
        bug: [
            "summary", "product_version", "surface", "workflow", "frequency", "steps",
            "expected", "actual", "support_bundle_status", "severity"
        ],
        feature: ["problem", "outcome", "value", "acceptance"],
        improve: ["improvement"]
    };

    function sanitizeText(value, maxLength) {
        return String(value == null ? "" : value)
            .replace(/\u0000/g, "")
            .trim()
            .slice(0, maxLength || MAX_TEXT_LENGTH);
    }

    function cleanSingleLine(value, maxLength) {
        return sanitizeText(value, maxLength || MAX_TITLE_LENGTH).replace(/\s+/g, " ");
    }

    function textOrPlaceholder(value) {
        return sanitizeText(value) || "Not provided";
    }

    function containsSensitiveMarker(value) {
        return /(?:authorization|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|client[_ -]?secret)\s*[:=]\s*\S+|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]{12,}|-----BEGIN [^-\r\n]{1,80} PRIVATE KEY-----/i.test(
            String(value == null ? "" : value)
        );
    }

    function safePageUrl(rawUrl, fallbackUrl) {
        const fallback = fallbackUrl || "https://couplet-physical-ai.github.io/PhiCad/";

        try {
            const url = new URL(rawUrl || fallback, fallback);
            return url.origin + (url.pathname || "/");
        } catch (error) {
            try {
                const url = new URL(fallback, fallback);
                return url.origin + (url.pathname || "/");
            } catch (fallbackError) {
                return "";
            }
        }
    }

    function collectSafeMetadata(doc, location, navigator, viewport) {
        const currentLocation = location || {};
        const currentNavigator = navigator || {};
        const currentViewport = viewport || {};
        const canonicalLink = doc && doc.querySelector
            ? doc.querySelector('link[rel="canonical"]')
            : null;
        const canonicalHref = canonicalLink && canonicalLink.href
            ? canonicalLink.href
            : currentLocation.href;

        let timezone = "Not provided";

        try {
            timezone = cleanSingleLine(Intl.DateTimeFormat().resolvedOptions().timeZone, 80) || timezone;
        } catch (error) {
            timezone = "Not provided";
        }

        return {
            canonical_url: safePageUrl(canonicalHref, currentLocation.href),
            page_title: cleanSingleLine(doc && doc.title, 200),
            browser_user_agent: cleanSingleLine(currentNavigator.userAgent, 240),
            browser_language: cleanSingleLine(currentNavigator.language, 40),
            viewport: currentViewport.innerWidth && currentViewport.innerHeight
                ? currentViewport.innerWidth + "x" + currentViewport.innerHeight
                : "Not provided",
            timezone: timezone,
            captured_at_utc: new Date().toISOString()
        };
    }

    function formatMetadata(metadata) {
        const safeMetadata = metadata || {};

        return [
            "### Page and browser context",
            "- Canonical page URL: " + safePageUrl(safeMetadata.canonical_url),
            "- Page title: " + textOrPlaceholder(cleanSingleLine(safeMetadata.page_title, 200)),
            "- Browser user agent: " + textOrPlaceholder(cleanSingleLine(safeMetadata.browser_user_agent, 240)),
            "- Browser language: " + textOrPlaceholder(cleanSingleLine(safeMetadata.browser_language, 40)),
            "- Viewport: " + textOrPlaceholder(cleanSingleLine(safeMetadata.viewport, 32)),
            "- Time zone: " + textOrPlaceholder(cleanSingleLine(safeMetadata.timezone, 80)),
            "- Captured at (UTC): " + textOrPlaceholder(cleanSingleLine(safeMetadata.captured_at_utc, 40))
        ].join("\n");
    }

    function formatFields(type, fields, metadata) {
        const values = fields || {};
        const pageMetadata = metadata || {};

        if (type === "bug") {
            return [
                "### Summary\n" + textOrPlaceholder(values.summary),
                "### PhiCad version / build identity\n" + textOrPlaceholder(values.product_version),
                "### Product surface\n" + textOrPlaceholder(values.surface),
                "### Workflow\n" + textOrPlaceholder(values.workflow),
                "### Relevant command or action\n" + textOrPlaceholder(values.command_action),
                "### Reproduction frequency\n" + textOrPlaceholder(values.frequency),
                "### Steps to reproduce\n" + textOrPlaceholder(values.steps),
                "### Expected behavior\n" + textOrPlaceholder(values.expected),
                "### Actual behavior\n" + textOrPlaceholder(values.actual),
                "### Sanitized error / stack excerpt\n" + textOrPlaceholder(values.error_excerpt),
                "### Coarse project context\n" + textOrPlaceholder(values.project_context),
                "### Support bundle\n" + textOrPlaceholder(values.support_bundle_status),
                "### Severity\n" + textOrPlaceholder(values.severity)
            ].join("\n\n");
        }

        if (type === "feature") {
            return [
                "### Problem\n" + textOrPlaceholder(values.problem),
                "### Desired outcome\n" + textOrPlaceholder(values.outcome),
                "### Value\n" + textOrPlaceholder(values.value),
                "### Acceptance criteria\n" + textOrPlaceholder(values.acceptance)
            ].join("\n\n");
        }

        return [
            "### Suggested improvement\n" + textOrPlaceholder(values.improvement),
            "### Page URL\n" + safePageUrl(values.page_url || pageMetadata.canonical_url)
        ].join("\n\n");
    }

    function buildIssueUrl(type, fields, metadata) {
        const selectedType = FEEDBACK_TYPES[type] ? type : "improve";
        const selectedFields = fields || {};
        const selectedMetadata = metadata || {};
        const definition = FEEDBACK_TYPES[selectedType];
        const sensitiveField = Object.keys(selectedFields).find(function (name) {
            return name !== "improvement_consent" && containsSensitiveMarker(selectedFields[name]);
        });

        if (sensitiveField) {
            throw new TypeError("Feedback contains a credential-looking value");
        }
        const titleSource = selectedType === "bug"
            ? selectedFields.summary
            : selectedType === "feature"
                ? selectedFields.problem
                : selectedFields.improvement;
        const titleDetail = cleanSingleLine(titleSource, MAX_TITLE_LENGTH) || "User feedback";
        const title = (definition.titlePrefix + ": " + titleDetail).slice(0, MAX_TITLE_LENGTH);
        const consent = selectedFields.improvement_consent === true ? "yes" : "no";
        const pageMetadata = Object.assign({}, selectedMetadata);

        if (selectedType === "improve") {
            pageMetadata.canonical_url = safePageUrl(
                selectedMetadata.canonical_url || selectedFields.page_url
            );
        }

        const body = [
            "## " + definition.label,
            "",
            formatFields(selectedType, selectedFields, pageMetadata),
            "",
            formatMetadata(pageMetadata),
            "",
            "### Privacy choices",
            "- Feedback submission: pending explicit review and submission on GitHub",
            "- Diagnostics: no automatic attachment; selected support-bundle status: " +
                textOrPlaceholder(selectedFields.support_bundle_status),
            "- Use report text after maintainer redaction to evaluate or improve PhiCad: " + consent,
            "- Attachments, support bundles, prompts, transcripts, and sensitive design data are excluded from that consent.",
            "",
            "This issue was prepared locally by the PhiCad documentation feedback form. " +
                "The form does not attach files or send data anywhere before this GitHub page is opened."
        ].join("\n");
        const issueUrl = new URL(ISSUE_URL);

        issueUrl.searchParams.set("title", title);
        issueUrl.searchParams.set("body", body);
        issueUrl.searchParams.set("labels", definition.labelValue);

        const encodedIssueUrl = issueUrl.toString();
        if (encodedIssueUrl.length > MAX_ISSUE_URL_LENGTH) {
            throw new RangeError("Feedback draft is too long for a reviewable GitHub handoff");
        }
        return encodedIssueUrl;
    }

    function fieldValue(fieldset, name) {
        const field = fieldset && fieldset.elements
            ? fieldset.elements.namedItem(name)
            : null;
        return field ? sanitizeText(field.value) : "";
    }

    function readFields(type, fieldset, consent, pageUrl) {
        const fields = {};

        (FIELD_NAMES[type] || []).forEach(function (name) {
            fields[name] = fieldValue(fieldset, name);
        });

        fields.improvement_consent = Boolean(consent && consent.checked);

        if (type === "improve") {
            fields.page_url = sanitizeText(pageUrl);
        }

        return fields;
    }

    function focusableElements(dialog) {
        return Array.prototype.slice.call(dialog.querySelectorAll(
            "button, [href], input, select, textarea, [tabindex]:not([tabindex=\"-1\"])"
        )).filter(function (element) {
            return !element.disabled && !element.closest("[hidden]");
        });
    }

    function createMarkup(doc) {
        const dialog = doc.createElement("dialog");
        dialog.id = "phicad-feedback-dialog";
        dialog.setAttribute("role", "dialog");
        dialog.setAttribute("aria-modal", "true");
        dialog.setAttribute("aria-labelledby", "phicad-feedback-title");
        dialog.setAttribute("aria-describedby", "phicad-feedback-description");
        dialog.innerHTML = [
            '<div class="phicad-feedback-shell" role="region" aria-label="Feedback form" tabindex="0">',
            '  <header class="phicad-feedback-header">',
            '    <p class="phicad-feedback-kicker">PhiCad feedback</p>',
            '    <h2 id="phicad-feedback-title">Help us improve PhiCad</h2>',
            '    <p id="phicad-feedback-description">Choose a path and describe what you need. Your browser will open a draft GitHub issue for you to review.</p>',
            '    <button type="button" class="phicad-feedback-close" data-feedback-close aria-label="Close feedback dialog">&times;</button>',
            "  </header>",
            '  <form id="phicad-feedback-form" class="phicad-feedback-form" novalidate>',
            '    <div class="phicad-feedback-field">',
            '      <label for="phicad-feedback-type">Feedback type</label>',
            '      <select id="phicad-feedback-type" name="feedback-type">',
            '        <option value="bug">Report a bug</option>',
            '        <option value="feature">Request a feature</option>',
            '        <option value="improve">Improve this page</option>',
            "      </select>",
            "    </div>",
            '    <fieldset data-feedback-fields="bug">',
            '      <legend>Bug report</legend>',
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-summary">Summary</label>',
            '        <input id="phicad-feedback-summary" name="summary" maxlength="200" required>',
            '        <p class="phicad-feedback-help">What went wrong?</p>',
            "      </div>",
            '      <div class="phicad-feedback-two-column">',
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-product-version">PhiCad version / build identity</label>',
            '          <input id="phicad-feedback-product-version" name="product_version" maxlength="200" required>',
            '          <p class="phicad-feedback-help">Paste <code>phicad --version</code> output, a release tag, or <code>unknown</code>; do not paste paths or credentials.</p>',
            "        </div>",
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-surface">Product surface</label>',
            '          <select id="phicad-feedback-surface" name="surface" required>',
            '            <option value="">Choose a surface</option>',
            '            <option>Desktop application</option>',
            '            <option>Agent View</option>',
            '            <option>CLI / automation</option>',
            '            <option>Board synthesis</option>',
            '            <option>MCP tools</option>',
            '            <option>Documentation</option>',
            '            <option>Install / build</option>',
            '            <option>Other</option>',
            "          </select>",
            "        </div>",
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-workflow">Workflow</label>',
            '        <textarea id="phicad-feedback-workflow" name="workflow" rows="2" maxlength="1200" required></textarea>',
            '        <p class="phicad-feedback-help">Name the task you were trying to complete.</p>',
            "      </div>",
            '      <div class="phicad-feedback-two-column">',
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-command">Relevant command or action <span class="phicad-feedback-optional">Optional</span></label>',
            '          <textarea id="phicad-feedback-command" name="command_action" rows="2" maxlength="1200"></textarea>',
            "        </div>",
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-frequency">Reproduction frequency</label>',
            '          <select id="phicad-feedback-frequency" name="frequency" required>',
            '            <option value="">Choose frequency</option>',
            '            <option>Every time</option>',
            '            <option>Usually</option>',
            '            <option>Intermittent</option>',
            '            <option>Once</option>',
            '            <option>Unknown</option>',
            "          </select>",
            "        </div>",
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-steps">Steps to reproduce</label>',
            '        <textarea id="phicad-feedback-steps" name="steps" rows="4" maxlength="2400" required></textarea>',
            "      </div>",
            '      <div class="phicad-feedback-two-column">',
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-expected">Expected behavior</label>',
            '          <textarea id="phicad-feedback-expected" name="expected" rows="3" maxlength="1200" required></textarea>',
            "        </div>",
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-actual">Actual behavior</label>',
            '          <textarea id="phicad-feedback-actual" name="actual" rows="3" maxlength="2400" required></textarea>',
            "        </div>",
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-error-excerpt">Sanitized error or stack excerpt <span class="phicad-feedback-optional">Optional</span></label>',
            '        <textarea id="phicad-feedback-error-excerpt" name="error_excerpt" rows="3" maxlength="2400"></textarea>',
            '        <p class="phicad-feedback-help">Remove tokens, credentials, local paths, design data, prompts, and transcripts first.</p>',
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-project-context">Coarse project context <span class="phicad-feedback-optional">Optional</span></label>',
            '        <textarea id="phicad-feedback-project-context" name="project_context" rows="2" maxlength="1200"></textarea>',
            '        <p class="phicad-feedback-help">For example: 2-layer board, about 20 parts; no names or design contents.</p>',
            "      </div>",
            '      <div class="phicad-feedback-two-column">',
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-support-bundle">Support bundle</label>',
            '          <select id="phicad-feedback-support-bundle" name="support_bundle_status" required>',
            '            <option value="">Choose one</option>',
            '            <option>Not created</option>',
            '            <option>Created and reviewed; I may attach it in GitHub</option>',
            '            <option>Created but not reviewed; I will not attach it yet</option>',
            '            <option>Command unavailable in this checkout</option>',
            "          </select>",
            "        </div>",
            '        <div class="phicad-feedback-field">',
            '          <label for="phicad-feedback-severity">Severity</label>',
            '          <select id="phicad-feedback-severity" name="severity" required>',
            '            <option value="">Choose severity</option>',
            '            <option>Blocking: I cannot continue</option>',
            '            <option>High: a key workflow is impaired</option>',
            '            <option>Medium: there is a workaround</option>',
            '            <option>Low: minor or cosmetic</option>',
            "          </select>",
            "        </div>",
            "      </div>",
            "    </fieldset>",
            '    <fieldset data-feedback-fields="feature" hidden disabled>',
            '      <legend>Feature request</legend>',
            '      <p class="phicad-feedback-help phicad-feedback-intro">Tell us about the problem and the outcome, rather than prescribing only a solution.</p>',
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-problem">Problem</label>',
            '        <textarea id="phicad-feedback-problem" name="problem" rows="3" maxlength="4000" required></textarea>',
            '        <p class="phicad-feedback-help">Who is affected, and what is difficult today?</p>',
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-outcome">Desired outcome</label>',
            '        <textarea id="phicad-feedback-outcome" name="outcome" rows="3" maxlength="4000" required></textarea>',
            '      </div>',
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-value">Value</label>',
            '        <textarea id="phicad-feedback-value" name="value" rows="3" maxlength="4000" required></textarea>',
            '        <p class="phicad-feedback-help">What would improve if this problem were solved?</p>',
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-acceptance">Acceptance criteria</label>',
            '        <textarea id="phicad-feedback-acceptance" name="acceptance" rows="3" maxlength="4000" required></textarea>',
            '        <p class="phicad-feedback-help">How could we tell the outcome is working?</p>',
            "      </div>",
            "    </fieldset>",
            '    <fieldset data-feedback-fields="improve" hidden disabled>',
            '      <legend>Improve this page</legend>',
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-page-url">Current canonical page URL</label>',
            '        <input id="phicad-feedback-page-url" name="page-url" readonly>',
            '        <p class="phicad-feedback-help">The query string and fragment are removed before this is included.</p>',
            "      </div>",
            '      <div class="phicad-feedback-field">',
            '        <label for="phicad-feedback-improvement">What should improve?</label>',
            '        <textarea id="phicad-feedback-improvement" name="improvement" rows="5" maxlength="4000" required></textarea>',
            '        <p class="phicad-feedback-help">What was unclear, missing, or harder than it needed to be?</p>',
            "      </div>",
            "    </fieldset>",
            '    <section class="phicad-feedback-privacy" aria-labelledby="phicad-feedback-included-title">',
            '      <h3 id="phicad-feedback-included-title">Included in the draft</h3>',
            '      <p>Your answers and limited page/browser context: canonical URL without query or fragment, page title, browser user agent, language, viewport, time zone, and UTC capture time.</p>',
            '      <p>No files are attached automatically. <a href="/guides/feedback/#support-bundle">Read about an optional support bundle</a> before adding one yourself on GitHub.</p>',
            '      <p>Do not submit credentials, private design content, prompts, transcripts, or identifying local paths. <a href="/reference/feedback-privacy/">Read the privacy policy</a>.</p>',
            "    </section>",
            '    <label class="phicad-feedback-consent" for="phicad-feedback-consent">',
            '      <input type="checkbox" id="phicad-feedback-consent" name="improvement-consent">',
            '      <span>After maintainer redaction, I consent to using only the sanitized report text I entered to evaluate or improve PhiCad. This consent does not include attachments, support bundles, prompts, transcripts, or sensitive design data.</span>',
            "    </label>",
            '    <p class="phicad-feedback-error" data-feedback-error role="alert" hidden></p>',
            '    <footer class="phicad-feedback-actions">',
            '      <button type="button" class="phicad-feedback-button phicad-feedback-button--secondary" data-feedback-close>Cancel</button>',
            '      <button type="submit" class="phicad-feedback-button phicad-feedback-button--primary">Continue to GitHub</button>',
            "    </footer>",
            "  </form>",
            "</div>"
        ].join("\n");

        return dialog;
    }

    function init(doc) {
        if (!doc || !doc.body || doc.getElementById("phicad-feedback-control")) {
            return null;
        }

        const control = doc.createElement("button");
        const dialog = createMarkup(doc);
        const form = dialog.querySelector("#phicad-feedback-form");
        const typeSelect = dialog.querySelector("#phicad-feedback-type");
        const consent = dialog.querySelector("#phicad-feedback-consent");
        const error = dialog.querySelector("[data-feedback-error]");
        const pageUrl = dialog.querySelector("#phicad-feedback-page-url");
        let previouslyFocused = null;

        control.id = "phicad-feedback-control";
        control.type = "button";
        control.className = "phicad-feedback-control";
        control.setAttribute("aria-haspopup", "dialog");
        control.setAttribute("aria-controls", dialog.id);
        control.setAttribute("aria-expanded", "false");
        control.textContent = "Feedback";

        function setMode(type) {
            const selectedType = FEEDBACK_TYPES[type] ? type : "bug";

            dialog.querySelectorAll("[data-feedback-fields]").forEach(function (fieldset) {
                const isSelected = fieldset.getAttribute("data-feedback-fields") === selectedType;
                fieldset.hidden = !isSelected;
                fieldset.disabled = !isSelected;
            });

            if (selectedType === "improve") {
                pageUrl.value = collectSafeMetadata(
                    doc,
                    root.location,
                    root.navigator,
                    root
                ).canonical_url;
            }

            error.hidden = true;
            typeSelect.value = selectedType;
        }

        function close() {
            if (dialog.open && typeof dialog.close === "function") {
                dialog.close();
            } else {
                dialog.removeAttribute("open");
            }
            dialog.classList.remove("phicad-feedback-dialog--fallback");

            control.setAttribute("aria-expanded", "false");

            if (previouslyFocused && typeof previouslyFocused.focus === "function") {
                previouslyFocused.focus();
            } else {
                control.focus();
            }
        }

        function open() {
            previouslyFocused = doc.activeElement;
            setMode(typeSelect.value);

            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "open");
                dialog.classList.add("phicad-feedback-dialog--fallback");
            }

            control.setAttribute("aria-expanded", "true");
            const firstField = focusableElements(dialog)[0];

            if (firstField) {
                firstField.focus();
            }
        }

        control.addEventListener("click", open);
        typeSelect.addEventListener("change", function () {
            setMode(typeSelect.value);
        });

        dialog.querySelectorAll("[data-feedback-close]").forEach(function (closeButton) {
            closeButton.addEventListener("click", close);
        });

        dialog.addEventListener("cancel", function (event) {
            event.preventDefault();
            close();
        });

        dialog.addEventListener("click", function (event) {
            if (event.target === dialog) {
                close();
            }
        });

        dialog.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                event.preventDefault();
                close();
                return;
            }

            if (event.key !== "Tab") {
                return;
            }

            const elements = focusableElements(dialog);
            const first = elements[0];
            const last = elements[elements.length - 1];

            if (!first || !last) {
                event.preventDefault();
            } else if (event.shiftKey && doc.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && doc.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });

        form.addEventListener("submit", function (event) {
            event.preventDefault();
            error.hidden = true;

            const selectedType = typeSelect.value;
            const selectedFieldset = dialog.querySelector(
                '[data-feedback-fields="' + selectedType + '"]'
            );

            if (!form.reportValidity()) {
                return;
            }

            const fields = readFields(
                selectedType,
                selectedFieldset,
                consent,
                pageUrl.value
            );
            const missingField = (REQUIRED_FIELD_NAMES[selectedType] || []).find(function (name) {
                return !fields[name];
            });

            if (missingField) {
                const missingElement = selectedFieldset.elements.namedItem(missingField);
                error.textContent = "Please complete the " + missingField + " field.";
                error.hidden = false;
                if (missingElement && typeof missingElement.focus === "function") {
                    missingElement.focus();
                }
                return;
            }

            const sensitiveField = Object.keys(fields).find(function (name) {
                return name !== "improvement_consent" && containsSensitiveMarker(fields[name]);
            });

            if (sensitiveField) {
                const sensitiveElement = selectedFieldset.elements.namedItem(sensitiveField);
                error.textContent = "Remove the credential-looking value before continuing to GitHub.";
                error.hidden = false;
                if (sensitiveElement && typeof sensitiveElement.focus === "function") {
                    sensitiveElement.focus();
                }
                return;
            }

            const metadata = collectSafeMetadata(
                doc,
                root.location,
                root.navigator,
                root
            );

            try {
                root.location.assign(buildIssueUrl(selectedType, fields, metadata));
            } catch (submitError) {
                error.textContent = submitError instanceof RangeError
                    ? "The draft is too long for a safe GitHub handoff. Shorten the report and try again."
                    : "The GitHub draft could not be opened. Please try again.";
                error.hidden = false;
            }
        });

        doc.body.appendChild(control);
        doc.body.appendChild(dialog);
        setMode(typeSelect.value);

        return {
            control: control,
            dialog: dialog
        };
    }

    const api = {
        buildIssueUrl: buildIssueUrl,
        collectSafeMetadata: collectSafeMetadata,
        containsSensitiveMarker: containsSensitiveMarker,
        sanitizeText: sanitizeText,
        safePageUrl: safePageUrl,
        init: init
    };

    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }

    if (root) {
        root.PhiCadFeedback = api;

        if (root.document) {
            if (root.document.readyState === "loading") {
                root.document.addEventListener("DOMContentLoaded", function () {
                    init(root.document);
                }, { once: true });
            } else {
                init(root.document);
            }
        }
    }
}(typeof globalThis !== "undefined" ? globalThis : this));
