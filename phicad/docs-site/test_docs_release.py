#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 2 of the License, or (at your
# option) any later version.
#
"""Fast, offline release checks for the MkDocs source tree.

This deliberately does not invoke MkDocs or fetch anything.  It catches the cheap
failures which otherwise only show up after a release build: a stale nav entry, a
missing image, a video with no accessible fallback, or a page which accidentally
contains a machine-local path.  The generated MCP reference is excluded from the
copy/link checks because it is executable documentation and contains intentional
paths and examples which are not rendered as release prose.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


SITE_ROOT = Path(__file__).resolve().parent
DOCS_ROOT = SITE_ROOT / "docs"
MKDOCS_PATH = SITE_ROOT / "mkdocs.yml"
GENERATED_MCP_DOC = Path("reference/mcp-tools-generated.md")

# These are the release-one surfaces described in /tmp/phicad-release-one-plan.md.
# Keep the paths here explicit: a renamed page or hook should make the release
# test fail with the new contract visible to the next maintainer.
REQUIRED_PAGES = {
    "landing page": DOCS_ROOT / "index.md",
    "feedback page": DOCS_ROOT / "guides/feedback.md",
    "feedback admin page": DOCS_ROOT / "reference/feedback-admin.md",
    "support page": SITE_ROOT.parent / "support/README.md",
}
REQUIRED_HOOKS = {
    "extra_css": (Path("assets/stylesheets/landing.css"), Path("assets/stylesheets/feedback.css")),
    "extra_javascript": (
        Path("assets/javascripts/landing.js"),
        Path("assets/javascripts/feedback.js"),
    ),
}

# A generated MCP page is full of command paths and code-shaped prose.  It is
# still required by nav parsing, but is not a useful signal for these checks.
GENERATED_DOC_NAMES = {GENERATED_MCP_DOC}

_HTML_ATTRIBUTE_RE = re.compile(
    r"\b(?P<attribute>href|src|poster)\s*=\s*"
    r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_HTML_SRCSET_RE = re.compile(
    r"\bsrcset\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?P<image>!)?\[[^\]]*\]\(\s*"
    r"(?:<(?P<angle>[^>\n]+)>|(?P<bare>[^)\s]+))",
)
_HTML_TAG_RE = re.compile(r"<(?P<tag>img|picture|source|video)\b(?P<attributes>[^>]*)>", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*[\"']?(?P<url>[^)\"']+)[\"']?\s*\)", re.IGNORECASE)
_PLACEHOLDER_PATTERNS = (
    (re.compile(r"\b(?:TODO|FIXME|TBD)\b", re.IGNORECASE), "unfinished marker"),
    (re.compile(r"\bplaceholder\b", re.IGNORECASE), "placeholder text"),
    (re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE), "Lorem ipsum text"),
    (re.compile(r"\bcoming\s+soon\b", re.IGNORECASE), "coming-soon text"),
    (re.compile(r"\bunder\s+construction\b", re.IGNORECASE), "under-construction text"),
    (
        re.compile(
            r"\b(?:your|insert|replace|enter|add|fill\s+in)\s+"
            r"(?:text|copy|content|value|details?|description|name)\s+here\b",
            re.IGNORECASE,
        ),
        "instructional placeholder text",
    ),
)
_LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:/private)?/tmp(?:/|\b)")
_BRAND_TYPO_RE = re.compile(r"\bExoWatt\b")


class _Reference:
    """One source-level link or asset reference with a useful error location."""

    def __init__(self, path: Path, line: int, value: str, kind: str, image: bool = False):
        self.path = path
        self.line = line
        self.value = value
        self.kind = kind
        self.image = image

    def describe(self) -> str:
        return f"{_display_path(self.path)}:{self.line}: {self.kind} {self.value!r}"


def _display_path(path: Path) -> str:
    """Return a repository-relative path for actionable test output."""

    try:
        return path.relative_to(SITE_ROOT.parent.parent).as_posix()
    except ValueError:
        try:
            return path.relative_to(SITE_ROOT).as_posix()
        except ValueError:
            return str(path)


def _is_generated(path: Path) -> bool:
    try:
        return path.relative_to(DOCS_ROOT).as_posix() in {p.as_posix() for p in GENERATED_DOC_NAMES}
    except ValueError:
        return False


def _source_files(suffixes: tuple[str, ...] = (".md", ".html")) -> list[Path]:
    return sorted(
        path
        for suffix in suffixes
        for path in DOCS_ROOT.rglob(f"*{suffix}")
        if path.is_file() and not _is_generated(path)
    )


def _blank(text: str) -> str:
    """Blank text while preserving newlines, so diagnostics retain line numbers."""

    return "".join("\n" if character == "\n" else " " for character in text)


def _without_fenced_code(text: str) -> str:
    """Remove Markdown fenced code blocks without shifting subsequent lines."""

    output: list[str] = []
    fence: tuple[str, int] | None = None

    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(?P<marker>`{3,}|~{3,})", line)
        if fence is None:
            if marker:
                marker_text = marker.group("marker")
                fence = (marker_text[0], len(marker_text))
                output.append(_blank(line))
            else:
                output.append(line)
            continue

        output.append(_blank(line))
        if marker:
            marker_text = marker.group("marker")
            if marker_text[0] == fence[0] and len(marker_text) >= fence[1]:
                fence = None

    return "".join(output)


def _without_non_rendered_code(text: str) -> str:
    """Remove comments, HTML code blocks, and inline code from release checks."""

    text = _without_fenced_code(text)
    for pattern in (
        r"<!--.*?-->",
        r"<pre\b.*?</pre\s*>",
        r"<code\b.*?</code\s*>",
        r"`{1,3}[^`\n]*`{1,3}",
    ):
        text = re.sub(pattern, lambda match: _blank(match.group(0)), text, flags=re.IGNORECASE | re.DOTALL)
    return text


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _attribute_value(match: re.Match[str]) -> str:
    return next(
        value for value in (match.group("double"), match.group("single"), match.group("bare")) if value is not None
    )


def _references(path: Path, text: str) -> list[_Reference]:
    references: list[_Reference] = []

    for match in _HTML_ATTRIBUTE_RE.finditer(text):
        value = _attribute_value(match).strip()
        if value:
            references.append(
                _Reference(
                    path,
                    _line_number(text, match.start()),
                    value,
                    f"HTML {match.group('attribute').lower()} reference",
                )
            )

    for match in _MARKDOWN_LINK_RE.finditer(text):
        value = (match.group("angle") or match.group("bare") or "").strip()
        if value:
            references.append(
                _Reference(
                    path,
                    _line_number(text, match.start()),
                    value,
                    "Markdown image" if match.group("image") else "Markdown link",
                    image=bool(match.group("image")),
                )
            )

    # Mark HTML image attributes separately so an ordinary external link is not
    # mistaken for an external runtime image.
    for tag_match in _HTML_TAG_RE.finditer(text):
        tag = tag_match.group("tag").lower()
        in_picture = bool(
            tag == "source"
            and re.search(r"<picture\b", text[: tag_match.start()], re.IGNORECASE)
            and text[: tag_match.start()].lower().rfind("<picture")
            > text[: tag_match.start()].lower().rfind("</picture")
        )
        for attr_match in _HTML_ATTRIBUTE_RE.finditer(tag_match.group("attributes")):
            attribute = attr_match.group("attribute").lower()
            is_image = (tag == "img" and attribute == "src") or (tag == "video" and attribute == "poster") or (
                in_picture and attribute in {"src", "poster"}
            )
            if not is_image:
                continue
            value = _attribute_value(attr_match).strip()
            if value:
                references.append(
                    _Reference(
                        path,
                        _line_number(text, tag_match.start()),
                        value,
                        f"HTML {tag} image reference",
                        image=True,
                    )
                )

    unique: dict[tuple[int, str], _Reference] = {}
    for reference in references:
        key = (reference.line, reference.value)
        if key not in unique or reference.image:
            unique[key] = reference
    return list(unique.values())


def _is_absolute_url(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme) or value.startswith("//")


def _local_target(source: Path, value: str) -> Path | None:
    """Resolve a source-relative reference, or return None for a URL/fragment."""

    parsed = urlsplit(value)
    if not parsed.path or parsed.scheme or value.startswith("//"):
        return None

    relative_path = Path(unquote(parsed.path))
    target = (DOCS_ROOT / relative_path.lstrip("/")) if parsed.path.startswith("/") else source.parent / relative_path
    target = target.resolve()

    def candidates_for(candidate: Path) -> list[Path]:
        candidates = [candidate]
        if candidate.suffix == ".html":
            candidates.append(candidate.with_suffix(".md"))
        elif not candidate.suffix:
            candidates.extend((candidate.with_suffix(".md"), candidate / "index.md"))
        return candidates

    candidates = candidates_for(target)
    found = next((candidate for candidate in candidates if candidate.is_file()), None)
    if found is not None:
        return found

    # Raw HTML in a Markdown page is resolved by the browser from the built
    # page URL, not from the Markdown source directory.  MkDocs renders
    # guides/foo.md at /guides/foo/, so support that intentional form too.
    try:
        source_relative = source.relative_to(DOCS_ROOT.resolve())
    except ValueError:
        source_relative = None
    if source_relative is not None:
        route_dir = DOCS_ROOT / source_relative.with_suffix("")
        if source_relative.name == "index.md":
            route_dir = source.parent
        route_target = (route_dir / relative_path).resolve()
        route_found = next((candidate for candidate in candidates_for(route_target) if candidate.is_file()), None)
        if route_found is not None:
            return route_found

    # Returning the original target preserves a useful "missing local target"
    # diagnostic, including when a ../ link accidentally escapes docs/.
    return target


def _nav_targets(config: str) -> list[str]:
    """Extract Markdown targets from the nav block without requiring PyYAML."""

    lines = config.splitlines()
    nav_line = next((index for index, line in enumerate(lines) if re.match(r"^\s*nav\s*:", line)), None)
    if nav_line is None:
        return []

    nav_indent = len(lines[nav_line]) - len(lines[nav_line].lstrip())
    targets: list[str] = []
    for line in lines[nav_line + 1 :]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= nav_indent:
            break
        match = re.search(r":\s*[\"']?(?P<target>[^\"'#\s]+\.md)[\"']?\s*(?:#.*)?$", line)
        if match:
            targets.append(match.group("target"))

    return targets


def _config_list(config: str, key: str) -> list[str]:
    """Read a simple scalar/list key from mkdocs.yml's local YAML surface."""

    lines = config.splitlines()
    key_index = next((index for index, line in enumerate(lines) if re.match(rf"^\s*{re.escape(key)}\s*:", line)), None)
    if key_index is None:
        return []

    key_line = lines[key_index]
    remainder = key_line.split(":", 1)[1].strip()
    if remainder.startswith("[") and remainder.endswith("]"):
        return [item.strip().strip("\"'") for item in remainder[1:-1].split(",") if item.strip()]
    if remainder and not remainder.startswith("#"):
        return [remainder.strip("\"'")]

    values: list[str] = []
    key_indent = len(key_line) - len(key_line.lstrip())
    for line in lines[key_index + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= key_indent:
            break
        match = re.match(r"^\s*-\s*[\"']?(?P<value>[^\"'#\s]+)[\"']?\s*(?:#.*)?$", line)
        if match:
            values.append(match.group("value"))
    return values


def _caption_linkage(video_body: str) -> bool:
    """Return whether a video exposes captions or a linked text transcript."""

    for track in re.finditer(r"<track\b(?P<attributes>[^>]*)>", video_body, re.IGNORECASE):
        attributes = track.group("attributes")
        kind = re.search(r"\bkind\s*=\s*[\"']([^\"']+)[\"']", attributes, re.IGNORECASE)
        source = re.search(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", attributes, re.IGNORECASE)
        if kind and source and kind.group(1).lower() in {"caption", "captions", "subtitle", "subtitles"}:
            return True

    for anchor in re.finditer(
        r"<a\b(?P<attributes>[^>]*)>(?P<label>.*?)</a\s*>", video_body, re.IGNORECASE | re.DOTALL
    ):
        attributes = anchor.group("attributes")
        label = html.unescape(re.sub(r"<[^>]+>", " ", anchor.group("label")))
        href = re.search(r"\bhref\s*=\s*[\"']([^\"']+)[\"']", attributes, re.IGNORECASE)
        if href and re.search(
            r"caption|subtitle|transcript|text version|accessible", f"{label} {href.group(1)}", re.IGNORECASE
        ):
            return True

    # A Markdown transcript link immediately after the video is the common
    # fallback form.  It is intentionally checked by label and destination,
    # rather than accepting an unrelated nearby navigation link.
    return bool(
        re.search(
            r"\[[^\]]*(?:caption|subtitle|transcript|text version|accessible)[^\]]*\]\([^)]*\)"
            r"|\[[^\]]+\]\([^)]*(?:caption|subtitle|transcript|text version|accessible)[^)]*\)",
            video_body,
            re.IGNORECASE,
        )
    )


def _nearby_caption_linkage(text: str, end: int) -> bool:
    """Accept a transcript link in the short explanation immediately after a video."""

    nearby = text[end : end + 1200]
    heading = re.search(r"\n\s*#{1,6}\s", nearby)
    if heading:
        nearby = nearby[: heading.start()]
    return _caption_linkage(nearby)


def _video_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for match in re.finditer(r"<video\b[^>]*>(?P<body>.*?)</video\s*>", text, re.IGNORECASE | re.DOTALL):
        body = match.group("body")
        fallback = re.sub(r"<(?:source|track)\b[^>]*>\s*", " ", body, flags=re.IGNORECASE)
        fallback = html.unescape(re.sub(r"<[^>]+>", " ", fallback))
        fallback = " ".join(fallback.split())
        location = f"{_display_path(path)}:{_line_number(text, match.start())}"
        if not fallback:
            errors.append(f"{location}: video has no fallback text; add a browser-unavailable message")
        if not _caption_linkage(body) and not _nearby_caption_linkage(text, match.end()):
            errors.append(
                f"{location}: video has no caption track or linked transcript; add a "
                '<track kind="captions"> or a nearby local transcript link'
            )
    return errors


def _external_image_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for reference in _references(path, text):
        if reference.image and _is_absolute_url(reference.value) and not reference.value.startswith("data:"):
            errors.append(
                f"{reference.describe()}: external runtime image URL; copy the asset into docs/assets "
                "and use a local reference"
            )

    for tag_match in _HTML_TAG_RE.finditer(text):
        tag = tag_match.group("tag").lower()
        in_picture = bool(
            tag == "source"
            and re.search(r"<picture\b", text[: tag_match.start()], re.IGNORECASE)
            and text[: tag_match.start()].lower().rfind("<picture")
            > text[: tag_match.start()].lower().rfind("</picture")
        )
        if tag != "img" and not in_picture:
            continue
        for srcset in _HTML_SRCSET_RE.finditer(tag_match.group("attributes")):
            value = next(
                candidate
                for candidate in (srcset.group("double"), srcset.group("single"), srcset.group("bare"))
                if candidate is not None
            )
            for image_url in (candidate.strip().split()[0] for candidate in value.split(",") if candidate.strip()):
                if _is_absolute_url(image_url) and not image_url.startswith("data:"):
                    errors.append(
                        f"{_display_path(path)}:{_line_number(text, tag_match.start())}: external runtime image URL "
                        f"{image_url!r} in srcset; copy the asset into docs/assets"
                    )

    return errors


def _policy_errors() -> list[str]:
    errors: list[str] = []
    for path in _source_files((".md", ".html", ".css", ".js")):
        text = _without_non_rendered_code(path.read_text(encoding="utf-8", errors="replace"))
        for pattern, label in _PLACEHOLDER_PATTERNS:
            for match in pattern.finditer(text):
                errors.append(
                    f"{_display_path(path)}:{_line_number(text, match.start())}: {label} "
                    f"({match.group(0)!r}); replace it with release copy"
                )
        for match in _BRAND_TYPO_RE.finditer(text):
            errors.append(
                f"{_display_path(path)}:{_line_number(text, match.start())}: brand spelling "
                "'ExoWatt' is invalid; use 'Exowatt'"
            )
        for match in re.finditer(r"file:///", text, re.IGNORECASE):
            errors.append(
                f"{_display_path(path)}:{_line_number(text, match.start())}: accidental file:/// reference; "
                "use a repository-relative path"
            )
        for match in _LOCAL_PATH_RE.finditer(text):
            errors.append(
                f"{_display_path(path)}:{_line_number(text, match.start())}: accidental /tmp reference; "
                "use a repository-relative or documented example path"
            )
    for path in _source_files((".css",)):
        text = _without_non_rendered_code(path.read_text(encoding="utf-8", errors="replace"))
        for match in _CSS_URL_RE.finditer(text):
            value = match.group("url").strip()
            if _is_absolute_url(value) and not value.startswith("data:"):
                errors.append(
                    f"{_display_path(path)}:{_line_number(text, match.start())}: external runtime asset URL "
                    f"{value!r}; keep release CSS assets local"
                )
    return errors


def _reference_errors() -> list[str]:
    errors: list[str] = []
    for path in _source_files():
        text = _without_non_rendered_code(path.read_text(encoding="utf-8", errors="replace"))
        for reference in _references(path, text):
            value = reference.value.strip()
            if value.startswith("#") or _is_absolute_url(value):
                continue
            target = _local_target(path, value)
            if target is None or not target.is_file():
                errors.append(
                    f"{reference.describe()}: missing local target; add the file under docs/ or change the link"
                )
    return errors


class DocsReleaseRegressionTest(unittest.TestCase):
    """Source-only release checks; each failure names the file and repair."""

    def test_mkdocs_nav_targets_exist(self) -> None:
        self.assertTrue(MKDOCS_PATH.is_file(), f"missing MkDocs config: {MKDOCS_PATH}")
        targets = _nav_targets(MKDOCS_PATH.read_text(encoding="utf-8", errors="replace"))
        self.assertTrue(targets, f"mkdocs.yml has no parseable nav targets: {_display_path(MKDOCS_PATH)}")

        missing = []
        for target in targets:
            # MkDocs resolves nav paths from its docs_dir (the sibling docs/
            # directory), not from the directory containing mkdocs.yml.
            resolved = _local_target(DOCS_ROOT / "__nav__.md", target)
            if resolved is None or not resolved.is_file():
                missing.append(f"{target!r} (add docs/{target} or fix the nav entry)")
        self.assertFalse(missing, "mkdocs.yml nav has missing targets:\n- " + "\n- ".join(missing))

    def test_local_markdown_and_html_references_exist(self) -> None:
        errors = _reference_errors()
        self.assertFalse(errors, "broken local docs references:\n- " + "\n- ".join(errors))

    def test_videos_have_fallback_and_caption_linkage(self) -> None:
        errors: list[str] = []
        for path in _source_files():
            text = _without_non_rendered_code(path.read_text(encoding="utf-8", errors="replace"))
            errors.extend(_video_errors(path, text))
        self.assertFalse(errors, "inaccessible video markup:\n- " + "\n- ".join(errors))

    def test_release_pages_and_local_hooks_exist(self) -> None:
        missing_pages = [
            f"{label}: {_display_path(path)}"
            for label, path in REQUIRED_PAGES.items()
            if not path.is_file()
        ]
        self.assertFalse(
            missing_pages,
            "release-one docs/support pages are missing:\n- "
            + "\n- ".join(missing_pages)
            + "\nAdd the planned landing, feedback, admin, and support pages.",
        )

        config = MKDOCS_PATH.read_text(encoding="utf-8", errors="replace")
        missing_hooks = []
        for key, relatives in REQUIRED_HOOKS.items():
            configured = _config_list(config, key)
            for relative in relatives:
                file_path = DOCS_ROOT / relative
                if not file_path.is_file():
                    missing_hooks.append(f"docs/{relative} (create the local hook file)")
                if relative.as_posix() not in configured:
                    missing_hooks.append(f"mkdocs.yml {key}: {relative} (register the local hook)")
        self.assertFalse(
            missing_hooks,
            "release-one local CSS/JS hooks are missing or unregistered:\n- " + "\n- ".join(missing_hooks),
        )

    def test_landing_page_keeps_an_explicit_scroll_contract(self) -> None:
        home = (DOCS_ROOT / "index.md").read_text(encoding="utf-8")
        styles = (DOCS_ROOT / "assets/stylesheets/landing.css").read_text(encoding="utf-8")
        script = (DOCS_ROOT / "assets/javascripts/landing.js").read_text(encoding="utf-8")
        required = {
            "top anchor": ('id="top"', home),
            "visible scroll affordance": ("Scroll to explore", home),
            "scroll progress element": ("product-nav-progress", home),
            "return-to-top link": ('href="#top"', home),
            "keyboard-scrollable section navigation": ('aria-label="PhiCad page sections" tabindex="0"', home),
            "document scroll container": ("overflow-y: auto", styles),
            "reduced-motion fallback": ("scroll-behavior: auto", styles),
            "live scroll progress": ("--landing-scroll-progress", script),
            "scroll listener": ('addEventListener("scroll"', script),
        }
        missing = [label for label, (needle, text) in required.items() if needle not in text]
        self.assertFalse(
            missing,
            "landing page lost its scroll contract:\n- "
            + "\n- ".join(missing)
            + "\nKeep the homepage document-scrollable with visible progress and a reduced-motion fallback.",
        )

    def test_release_copy_has_no_unsafe_or_placeholder_content(self) -> None:
        errors = _policy_errors()
        for path in _source_files():
            text = _without_non_rendered_code(path.read_text(encoding="utf-8", errors="replace"))
            errors.extend(_external_image_errors(path, text))
        self.assertFalse(errors, "docs release content violations:\n- " + "\n- ".join(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
