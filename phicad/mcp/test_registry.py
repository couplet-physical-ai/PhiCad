#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Contract test for the tool registry and the phicad-tool CLI.

WHAT IT ENFORCES (these are the rules a new tool must satisfy to land)
  1. every registered tool has a docstring, and a first line that is a real summary;
  2. every parameter and the return value are fully type-hinted (FastMCP builds the client
     schema from the annotations — an unhinted parameter reaches a chat client as untyped);
  3. no duplicate tool names, and no name that a shell would mangle;
  4. every parameter appears in the JSON Schema FastMCP will publish;
  5. every MUTATING tool's docstring says where its bytes go (that is the only thing an agent
     which cannot see the filesystem has to decide whether to snapshot first);
  6. the CLI works end to end: `list --json`, `describe`, `call`, exit codes, docs rendering;
  7. the HAND-WRITTEN docs stay consistent with the live surface (see below).

THE DOC-CONSISTENCY GATES (7) AND WHY THEY EXIST
  A surface audit found all three of these defects in the checked-in docs, each one invisible to
  every other gate:
    a. `docs-site/docs/reference/mcp-tools.md` hand-documented a fraction of the tools with
       `### tool_name(...)` sections; four tool-adding commits never touched it, and three
       "Added in this release" headings outlived their release by weeks.  The generated page has a
       byte-exact staleness check and that page had none, so per-tool prose is now BANNED there.
    b. `phicad/mcp/README.md` documented `--section repo-build-state`, which errors: the real key
       is `host-integration-repo-state`.  Every `--section X` token in the README must be a live
       section key.
    c. the same docs claimed a tool count ("126 tools", "all 126") that the next tool-adding
       commit falsified.  Any such claim must match the registry, so the honest choices are "no
       number" or "the right number" — nothing in between survives CI.

No pytest (the MCP venv has no test framework and must not grow one for this): plain asserts
and a tiny runner, so the failure message is the thing you need to fix.

Run:  build/mcp-venv/bin/python phicad/mcp/test_registry.py
      phicad/bin/phicad-tool registry-test
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

MCP_DIR = Path(__file__).resolve().parent
REPO_ROOT = MCP_DIR.parent.parent
PHICAD_TOOL = REPO_ROOT / "phicad" / "bin" / "phicad-tool"
FAB_JOBSET = MCP_DIR / "examples" / "fab_pack.kicad_jobset"

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# authlib installs its own "always" filter at import time, so import it before silencing.
try:
    import authlib.deprecate                       # noqa: F401
except Exception:                                  # noqa: BLE001
    pass
warnings.simplefilter("ignore", DeprecationWarning)

import registry                                                            # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> bool:
    """Record one assertion. Returns the condition so a test can bail out on a hard failure."""
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
    return bool(condition)


def section(title: str) -> None:
    print(f"\n-- {title}")


# --------------------------------------------------------------------------------------
# registry contract
# --------------------------------------------------------------------------------------

# Tools whose parameters are legitimately loose, with the reason. Empty today; the mechanism
# exists so a future exception is a documented line here, not a weakened rule.
HINT_EXEMPT: dict[str, str] = {}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def test_names(specs: list[registry.ToolSpec]) -> None:
    section("names are unique and shell-safe")
    names = [s.name for s in specs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    check(not dupes, f"duplicate tool names: {dupes}")
    bad = [n for n in names if not _NAME_RE.match(n)]
    check(not bad, f"tool names must be lower_snake_case (a shell argument): {bad}")
    print(f"   {len(names)} tools, all unique, all lower_snake_case")


def test_docstrings(specs: list[registry.ToolSpec]) -> None:
    section("every tool has a usable docstring")
    missing = [s.name for s in specs if not s.doc.strip()]
    check(not missing, f"tools without a docstring: {missing}")
    stubby = [s.name for s in specs if len(s.summary) < 20]
    check(not stubby, f"tools whose first docstring line is not a summary: {stubby}")
    unpunctuated = [s.name for s in specs
                    if s.summary and s.summary[-1] not in ".:!?)`\"'"]
    # A summary cut mid-sentence usually means the docstring's first line wrapped; that shows
    # up truncated in every client's tool list, so it is worth failing on.
    check(not unpunctuated,
          f"summary line looks truncated (no terminal punctuation): {unpunctuated}")
    shortest = min(len(s.summary) for s in specs)
    print(f"   {len(specs)} docstrings, shortest summary {shortest} chars")


def test_type_hints(specs: list[registry.ToolSpec]) -> None:
    section("every parameter and return value is type-hinted")
    unhinted: list[str] = []
    for s in specs:
        if s.name in HINT_EXEMPT:
            continue
        for p in s.params:
            if not p.annotation:
                unhinted.append(f"{s.name}({p.name})")
    check(not unhinted, f"parameters without a type hint: {unhinted}")
    no_return = [s.name for s in specs if not s.returns]
    check(not no_return, f"tools without a return annotation: {no_return}")
    wrong_return = [f"{s.name} -> {s.returns}" for s in specs
                    if s.returns and "dict" not in s.returns]
    check(not wrong_return,
          f"tools must return a dict (never raise across the tool boundary): {wrong_return}")
    print(f"   {sum(len(s.params) for s in specs)} parameters, all hinted; "
          f"{len(specs)} dict returns")


def test_schemas(specs: list[registry.ToolSpec]) -> None:
    section("the published JSON schema covers every parameter")
    gaps: list[str] = []
    for s in specs:
        props = (s.parameters.get("properties") or {})
        for p in s.params:
            if p.name not in props:
                gaps.append(f"{s.name}({p.name})")
            elif not registry.schema_types(props[p.name]):
                gaps.append(f"{s.name}({p.name}) has no type in the schema")
    check(not gaps, f"parameters missing from the FastMCP schema: {gaps}")
    for s in specs:
        required = set(s.parameters.get("required") or ())
        declared = {p.name for p in s.params if p.required}
        check(required <= declared,
              f"{s.name}: schema requires {sorted(required - declared)} but the signature "
              f"gives them defaults")
    print(f"   {len(specs)} schemas consistent with their signatures")


def test_mutating_documented(specs: list[registry.ToolSpec]) -> None:
    section("every mutating tool documents what it writes")
    silent: list[str] = []
    for s in specs:
        if not s.mutates:
            continue
        ok, _ = registry.documents_writes(s.name, s.doc, [p.name for p in s.params])
        if not ok:
            silent.append(s.name)
    check(not silent,
          f"mutating tools whose docstring never says what they write: {silent} "
          f"(say where the bytes land, or name the destination parameter out_*)")
    mutating = [s.name for s in specs if s.mutates]
    print(f"   {len(mutating)} mutating tools, all documented: {', '.join(mutating)}")


def test_sections(specs: list[registry.ToolSpec]) -> None:
    section("every tool lands in a real section")
    orphans = [s.name for s in specs if s.section_source == "unknown"]
    check(not orphans,
          f"tools with no banner comment above them (add one, or move them to a tools_*.py "
          f"module): {orphans}")
    keys = {s.section_key for s in specs}
    check("" not in keys, "a section produced an empty key")
    print(f"   {len(keys)} sections: {', '.join(sorted(keys))}")


def test_callables(specs: list[registry.ToolSpec]) -> None:
    section("every spec carries the real function the server registered")
    for s in specs:
        if not check(callable(s.fn), f"{s.name}: spec.fn is not callable"):
            continue
        check(s.fn.__name__ == s.name or s.name in getattr(s.fn, "__qualname__", ""),
              f"{s.name}: spec.fn is {getattr(s.fn, '__name__', '?')}")
    print(f"   {len(specs)} callables resolved (this is what `phicad-tool call` invokes)")


def test_renderers(specs: list[registry.ToolSpec]) -> None:
    section("renderers produce complete output")
    payload = json.loads(registry.render_json(specs))
    check(payload["tool_count"] == len(specs), "render_json lost tools")
    check(len(payload["tools"]) == len(specs), "render_json tool list length mismatch")
    check(all("fn" not in t for t in payload["tools"]),
          "render_json leaked a callable (it must be JSON-serialisable)")
    md = registry.render_markdown(specs)
    check(md.startswith("# MCP tool reference (generated)"),
          "the markdown page must announce that it is generated")
    check("phicad-tool docs" in md, "the page must name the command that regenerates it")
    absent = [s.name for s in specs if f"### {s.name}\n" not in md]
    check(not absent, f"tools missing from the markdown page: {absent}")
    # Python-Markdown's TOC (used by MkDocs) and GitHub preserve identifier underscores.
    # The previous generator changed only index links to hyphens, breaking tool navigation.
    heading_ids = set(re.findall(r"^### ([a-z][a-z0-9_]*)$", md, re.MULTILINE))
    index_ids = re.findall(r"^\| \[`[a-z][a-z0-9_]*`\]\(#([^)]+)\)", md, re.MULTILINE)
    check(len(index_ids) == len(specs), "tool index lost links")
    check(set(index_ids) == heading_ids, "tool index anchors do not match rendered heading IDs")
    print(f"   render_json {len(registry.render_json(specs))} bytes, "
          f"render_markdown {len(md)} bytes, all {len(specs)} tools present")


def test_coercion() -> None:
    section("schema-driven argument coercion")
    import phicad_tool

    cases: list[tuple[str, dict[str, Any], Any]] = [
        ("true", {"type": "boolean"}, True),
        ("FALSE", {"type": "boolean"}, False),
        ("no", {"type": "boolean"}, False),
        ("1", {"type": "boolean"}, True),
        ("42", {"type": "integer"}, 42),
        ("-3", {"type": "integer"}, -3),
        ("1.5", {"type": "number"}, 1.5),
        # A string parameter is never JSON-decoded: a board path that looks like JSON must
        # survive verbatim.
        ("123", {"type": "string"}, "123"),
        ("[1]", {"type": "string"}, "[1]"),
        ("/tmp/b.kicad_pcb", {"type": "string"}, "/tmp/b.kicad_pcb"),
        ('["A","B"]', {"type": "array", "items": {"type": "string"}}, ["A", "B"]),
        ("A,B", {"type": "array", "items": {"type": "string"}}, ["A", "B"]),
        ('{"g0":"/x"}', {"type": "object"}, {"g0": "/x"}),
        # Optional parameters: the null branch accepts the word, the other branch wins first.
        ("/x", {"anyOf": [{"type": "string"}, {"type": "null"}]}, "/x"),
        ("null", {"anyOf": [{"type": "integer"}, {"type": "null"}]}, None),
        ("7", {"anyOf": [{"type": "integer"}, {"type": "null"}]}, 7),
    ]
    for raw, schema, want in cases:
        got = phicad_tool.coerce_value(raw, schema)
        check(got == want and type(got) is type(want),
              f"coerce_value({raw!r}, {schema}) = {got!r} ({type(got).__name__}), "
              f"want {want!r} ({type(want).__name__})")
    for raw, schema in (("maybe", {"type": "boolean"}), ("x", {"type": "integer"}),
                        ("x", {"type": "number"})):
        try:
            phicad_tool.coerce_value(raw, schema)
            check(False, f"coerce_value({raw!r}, {schema}) should have raised")
        except phicad_tool.CoercionError:
            check(True, "")
    print(f"   {len(cases)} coercions + 3 rejections behave as specified")


def test_kwargs_building(specs: list[registry.ToolSpec]) -> None:
    section("kwargs assembly rejects bad input and honours --json/--arg precedence")
    import phicad_tool

    spec = next(s for s in specs if s.name == "pcb_drc")
    kw = phicad_tool.build_kwargs(spec, ["board_path=/x.kicad_pcb", "severity_all=false"],
                                  None, False)
    check(kw == {"board_path": "/x.kicad_pcb", "severity_all": False}, f"--arg only: {kw}")
    kw = phicad_tool.build_kwargs(spec, ["severity_all=true"],
                                  '{"board_path": "/y.kicad_pcb", "severity_all": false}', False)
    check(kw == {"board_path": "/y.kicad_pcb", "severity_all": True},
          f"--arg must override --json: {kw}")
    for pairs, blob, why in (
        (["nope=1"], None, "unknown parameter"),
        (["board_path"], None, "missing ="),
        ([], "not json", "invalid JSON"),
        ([], "[1,2]", "JSON array instead of object"),
        ([], None, "missing required parameter"),
    ):
        try:
            phicad_tool.build_kwargs(spec, pairs, blob, False)
            check(False, f"build_kwargs should have rejected: {why}")
        except phicad_tool.CoercionError:
            check(True, "")
    print("   5 rejections + 2 precedence cases behave as specified")


# --------------------------------------------------------------------------------------
# CLI, for real, through the bash entry point
# --------------------------------------------------------------------------------------


def run_cli(*args: str, stdin: str | None = None,
            timeout: int = 300) -> subprocess.CompletedProcess:
    """Run phicad/bin/phicad-tool as a subprocess (the way an agent or CI would)."""
    env = dict(os.environ)
    env.pop("PYTHONWARNINGS", None)
    return subprocess.run(
        ["bash", str(PHICAD_TOOL), *args],
        capture_output=True, text=True, timeout=timeout, input=stdin, env=env,
        cwd=str(REPO_ROOT),
    )


def test_cli_list() -> None:
    section("phicad-tool list / list --json / --section / --mutating")
    p = run_cli("list")
    check(p.returncode == 0, f"list exited {p.returncode}: {p.stderr[-400:]}")
    check("pcb_drc" in p.stdout, "list did not mention pcb_drc")
    check(not p.stderr.strip(),
          f"list wrote to stderr (a clean run must be silent there): {p.stderr[-300:]}")

    p = run_cli("list", "--json")
    check(p.returncode == 0, f"list --json exited {p.returncode}: {p.stderr[-400:]}")
    try:
        payload = json.loads(p.stdout)
    except ValueError as exc:
        check(False, f"list --json did not print JSON: {exc}; head={p.stdout[:200]!r}")
        return
    check(payload["tool_count"] >= 40, f"expected the full surface, got {payload['tool_count']}")
    check(any(t["name"] == "pcb_drc" for t in payload["tools"]), "pcb_drc missing from --json")

    p = run_cli("list", "--section", "gates")
    check(p.returncode == 0 and "pcb_drc" in p.stdout and "board_summary" not in p.stdout,
          f"--section gates did not filter: {p.stdout[:300]!r}")
    p = run_cli("list", "--mutating")
    check(p.returncode == 0 and "autoroute_freerouting" in p.stdout
          and "pcb_drc " not in p.stdout,
          f"--mutating did not filter: {p.stdout[:300]!r}")
    p = run_cli("list", "--section", "definitely-not-a-section")
    check(p.returncode == 2, f"an unknown --section must exit 2, got {p.returncode}")
    check("sections:" in p.stderr, "an unknown --section should list the real keys")
    print(f"   list ok ({payload['tool_count']} tools), --json parses, both filters narrow")


def test_cli_describe() -> None:
    section("phicad-tool describe")
    p = run_cli("describe", "pcb_drc")
    check(p.returncode == 0, f"describe exited {p.returncode}: {p.stderr[-300:]}")
    for want in ("board_path", "severity_all", "json schema", "Run design-rule check"):
        check(want in p.stdout, f"describe pcb_drc omitted {want!r}")
    p = run_cli("describe", "no_such_tool")
    check(p.returncode == 2, f"describe of an unknown tool must exit 2, got {p.returncode}")
    p = run_cli("describe", "pcb_drc", "--json")
    try:
        spec = json.loads(p.stdout)
        check(spec["name"] == "pcb_drc" and spec["parameters"]["properties"]["board_path"],
              "describe --json is missing the schema")
    except ValueError as exc:
        check(False, f"describe --json is not JSON: {exc}")
    print("   describe prints signature + schema + doc; unknown tool exits 2")


def test_cli_call() -> None:
    section("phicad-tool call — exit codes and result shape")
    p = run_cli("call", "phicad_status")
    check(p.returncode == 0, f"call phicad_status exited {p.returncode}: {p.stderr[-400:]}")
    try:
        res = json.loads(p.stdout)
    except ValueError as exc:
        check(False, f"call did not print JSON: {exc}; head={p.stdout[:200]!r}")
        return
    check(res.get("ok") is True, f"phicad_status returned ok={res.get('ok')}")
    check(res.get("repo_root"), "phicad_status did not report repo_root")

    # ok=false -> exit 1 (a real tool failure, not a usage error)
    p = run_cli("call", "board_summary", "--arg", "board_path=/tmp/definitely-missing.kicad_pcb",
                "--raw")
    check(p.returncode == 1, f"a failing tool must exit 1, got {p.returncode}")
    check(json.loads(p.stdout).get("ok") is False, "expected ok=false")
    check("\n" not in p.stdout.strip(), "--raw must print one line")

    # usage errors -> exit 2
    for args, why in (
        (("call", "not_a_tool"), "unknown tool"),
        (("call", "pcb_drc"), "missing required parameter"),
        (("call", "pcb_drc", "--arg", "bogus=1"), "unknown parameter"),
        (("call", "pcb_drc", "--arg", "board_path=/x", "--arg", "severity_all=perhaps"),
         "uncoercible value"),
        (("call", "pcb_drc", "--json", "{oops"), "malformed JSON"),
    ):
        p = run_cli(*args)
        check(p.returncode == 2, f"{why} must exit 2, got {p.returncode} ({' '.join(args)})")

    # --stdin and --project-snapshot plumbing
    p = run_cli("call", "pcb_drc", "--dry-run", "--stdin", "--project-snapshot",
                stdin='{"board_path": "/tmp/x.kicad_pcb"}')
    check(p.returncode == 0, f"--stdin dry run exited {p.returncode}: {p.stderr[-300:]}")
    check(json.loads(p.stdout)["arguments"]["board_path"] == "/tmp/x.kicad_pcb",
          "--stdin arguments did not arrive")
    print("   ok=true -> 0, ok=false -> 1, five usage errors -> 2, --stdin/--raw/--dry-run ok")


def test_cli_snapshot_env() -> None:
    section("--project-snapshot sets PHICAD_TOOL_SNAPSHOT for the tool call")
    import phicad_tool

    import contextlib
    import io

    argv = ["call", "phicad_status", "--dry-run", "--project-snapshot"]
    args = phicad_tool.build_parser().parse_args(argv)
    os.environ.pop("PHICAD_TOOL_SNAPSHOT", None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):        # the dry-run JSON is not part of this report
        rc = phicad_tool.cmd_call(args)
    check(rc == 0, f"dry-run call returned {rc}: {buf.getvalue()[:200]}")
    check(os.environ.get("PHICAD_TOOL_SNAPSHOT") == "1",
          "--project-snapshot did not export PHICAD_TOOL_SNAPSHOT=1")
    os.environ.pop("PHICAD_TOOL_SNAPSHOT", None)
    print("   PHICAD_TOOL_SNAPSHOT=1 plumbed through to the tool call")


def test_cli_docs(specs: list[registry.ToolSpec]) -> None:
    section("phicad-tool docs")
    stdout_run = run_cli("docs")
    page = stdout_run.stdout
    check(stdout_run.returncode == 0, f"docs exited {stdout_run.returncode}: "
                                      f"{stdout_run.stderr[-300:]}")
    check(page.startswith("# MCP tool reference (generated)"),
          "docs did not print the generated header")
    check("phicad-tool docs" in page, "docs page must name the command that regenerates it")
    missing = [s.name for s in specs if f"### {s.name}\n" not in page]
    check(not missing, f"docs page is missing tools: {missing}")

    target = Path(os.environ.get("TMPDIR", "/tmp")) / "phicad-tool-docs-test.md"
    target.unlink(missing_ok=True)
    file_run = run_cli("docs", "-o", str(target))
    check(file_run.returncode == 0, f"docs -o exited {file_run.returncode}: "
                                    f"{file_run.stderr[-300:]}")
    check(target.exists() and target.stat().st_size > 10000,
          f"docs -o did not write a real file: {target}")
    if target.exists():
        check(target.read_text() == page, "docs -o and docs > file disagree")

    # The checked-in page must be byte-identical to what the registry renders now: that is the
    # whole point of generating it, and this is the check that catches a tool landing without
    # its docs being regenerated.
    generated = REPO_ROOT / registry_docs_target()
    if check(generated.exists(),
             f"the checked-in generated page is missing — run "
             f"`phicad/bin/phicad-tool docs -o` ({generated})"):
        check(generated.read_text() == page,
              f"{generated} is STALE — regenerate it with `phicad/bin/phicad-tool docs -o`")
    # chars, not bytes: the em-dashes in the banner titles make the UTF-8 file larger.
    print(f"   docs renders {len(page)} chars for {len(specs)} tools; "
          f"-o wrote {target.stat().st_size if target.exists() else 0} bytes, identical text; "
          f"checked-in page current")
    target.unlink(missing_ok=True)


def registry_docs_target() -> str:
    import phicad_tool

    return phicad_tool.DOCS_TARGET


# --------------------------------------------------------------------------------------
# The hand-written docs, checked against the live surface
# --------------------------------------------------------------------------------------

#: The four hand-written pages that describe this surface.  The generated page is excluded on
#: purpose: it has its own byte-exact staleness check in test_cli_docs().
HAND_WRITTEN_DOCS = (
    "phicad/mcp/README.md",
    "phicad/ai-interfacing.md",
    "phicad/docs-site/docs/reference/mcp-tools.md",
    "phicad/docs-site/docs/guides/driving-phicad.md",
)

#: The page that must stay NARRATIVE: no per-tool sections, because they cannot be gated.
NARRATIVE_PAGE = "phicad/docs-site/docs/reference/mcp-tools.md"

#: The page whose `--section X` tokens must all be real section keys.
SECTION_MAP_PAGE = "phicad/mcp/README.md"

#: A count claim about a DIFFERENT tool surface, with the reason it is not ours.  A new entry
#: here is a documented line, not a weakened rule: `(file, text that must appear on the line)`.
COUNT_CLAIM_EXEMPT: dict[tuple[str, str], str] = {
    ("phicad/ai-interfacing.md", "kicad-analysis"):
        "counts the separate kicad-analysis MCP server, not this surface",
    ("phicad/ai-interfacing.md", "KiCAD-MCP-Server"):
        "counts the stable-KiCad SWIG server, not this surface",
}

#: `### tool_name(` or `#### tool_name(` — a per-tool section heading.
_TOOL_HEADING_RE = re.compile(r"^#{3,4}\s+`?(?P<name>[a-z][a-z0-9_]*)\s*\(", re.M)
#: A `--section <key>` token as a reader would type it (backticks/quotes optional).  `--sections`
#: is a different flag (test_handshake's check listing) and is deliberately not matched.
_SECTION_TOKEN_RE = re.compile(r"--section(?![a-z])[=\s]+`?[\"']?(?P<key>[a-z][a-z0-9-]{2,})")
#: Every way these docs have claimed a size: "126 tools", "**126 tools in 16 sections**",
#: "all 126", "126 of them", "126 in 16 sections", "the same 126 tools".
_COUNT_CLAIM_RE = re.compile(
    r"(?:\*\*)?(?P<n>\d{2,4})(?:\*\*)?\s*(?:\*\*)?"
    r"(?:[a-z][a-z-]*\s+){0,2}(?:tools?\b|of them\b|in \d+ sections?\b)"
    r"|\ball\s+(?:\*\*)?(?P<m>\d{2,4})\b",
    re.I)


def _doc_text(rel: str) -> tuple[Path, str]:
    path = REPO_ROOT / rel
    return path, (path.read_text(encoding="utf-8") if path.exists() else "")


def test_docs_no_per_tool_prose(specs: list[registry.ToolSpec]) -> None:
    section("the narrative page carries NO per-tool sections")
    path, text = _doc_text(NARRATIVE_PAGE)
    if not check(text, f"{NARRATIVE_PAGE} is missing"):
        return
    known = {s.name for s in specs}
    headings = []
    for match in _TOOL_HEADING_RE.finditer(text):
        name = match.group("name")
        line = text[:match.start()].count("\n") + 1
        # Only real tool names: a heading like `### coerce_value(...)` about the CLI is prose.
        if name in known or "_" in name:
            headings.append(f"{NARRATIVE_PAGE}:{line}: ### {name}(")
    check(not headings,
          "per-tool sections are banned on the narrative page (they rot: it documented ~29 of "
          f"{len(specs)} tools and three shipped releases as 'Added in this release'). Document "
          f"the tool's docstring instead — it renders into mcp-tools-generated.md: {headings}")
    check("mcp-tools-generated.md" in text,
          f"{NARRATIVE_PAGE} must link the generated per-tool reference prominently")
    print(f"   {NARRATIVE_PAGE}: 0 per-tool headings, delegates to the generated page")


def test_docs_section_keys(specs: list[registry.ToolSpec]) -> None:
    section("every documented --section key exists in the registry")
    path, text = _doc_text(SECTION_MAP_PAGE)
    if not check(text, f"{SECTION_MAP_PAGE} is missing"):
        return
    keys = {s.section_key for s in specs}
    # Placeholders a reader is meant to substitute, not keys to resolve.
    placeholders = {"key", "section", "name", "s"}
    bad: list[str] = []
    seen: set[str] = set()
    for match in _SECTION_TOKEN_RE.finditer(text):
        token = match.group("key")
        if token in placeholders or token in keys:
            seen.add(token)
            continue
        line = text[:match.start()].count("\n") + 1
        bad.append(f"{SECTION_MAP_PAGE}:{line}: --section {token}")
    check(not bad,
          f"documented --section keys that the registry does not have: {bad} — real keys: "
          f"{', '.join(sorted(keys))}")
    # The table is the map of the surface, so every live section must appear in it.
    missing = sorted(k for k in keys if f"`{k}`" not in text)
    check(not missing,
          f"{SECTION_MAP_PAGE}'s section table is missing live sections: {missing}")
    print(f"   {len(seen)} --section tokens resolve; all {len(keys)} live sections are in the table")


def test_docs_tool_counts(specs: list[registry.ToolSpec]) -> None:
    section("no hand-written doc claims a tool count the registry disagrees with")
    live = len(specs)
    wrong: list[str] = []
    claims = 0
    for rel in HAND_WRITTEN_DOCS:
        path, text = _doc_text(rel)
        if not check(text, f"{rel} is missing"):
            continue
        for match in _COUNT_CLAIM_RE.finditer(text):
            raw = match.group("n") or match.group("m")
            line_no = text[:match.start()].count("\n") + 1
            line = text.splitlines()[line_no - 1]
            exempt = next((why for (f, marker), why in COUNT_CLAIM_EXEMPT.items()
                           if f == rel and marker in line), None)
            if exempt:
                continue
            claims += 1
            if int(raw) != live:
                wrong.append(f"{rel}:{line_no}: claims {raw}, the registry has {live} "
                             f"({match.group(0).strip()!r})")
    check(not wrong,
          "stale tool counts in the hand-written docs — delete the number (say "
          "`phicad tool list --json | jq .tool_count`) or fix it: " + "; ".join(wrong))
    print(f"   {claims} count claim(s) across {len(HAND_WRITTEN_DOCS)} docs, all == {live}")


def test_cli_passthrough() -> None:
    section("phicad-tool doctor / help pass through")
    p = run_cli("doctor", "--help")
    check(p.returncode == 0, f"doctor --help exited {p.returncode}: {p.stderr[-300:]}")
    check("doctor" in (p.stdout + p.stderr).lower(), "doctor --help printed nothing useful")
    p = run_cli()
    check(p.returncode == 2, f"no command must exit 2 with usage, got {p.returncode}")
    check("call" in p.stdout, "bare invocation should print the command list")
    print("   doctor forwards its flags; bare invocation prints usage and exits 2")


def test_doctor_dangling_paths() -> None:
    """`check_other_servers` must flag real drift and stay quiet about the rest.

    A check that cries wolf gets ignored, which costs more than not having it: it reported a
    dangling path for a URL (splitting `http://host:7125` on ":" yields the head `//host`) and
    for an audit log the server creates on first write. Both were permanent, so "1 warn" was
    the steady state and a REAL dangling path would have looked like the usual noise.
    """
    section("doctor: dangling-path audit of other servers")
    import tempfile

    import doctor

    root = Path(tempfile.mkdtemp(prefix="phicad-doctor-"))
    (root / "kicad").mkdir()                    # parent exists; the .app inside does not
    cases = {
        # The incident this check was written for: env pointing into a retired worktree.
        "drift": {"command": "/bin/sh",
                  "env": {"KICAD_APP_PATH": str(root / "kicad" / "PhiCad.app")}},
        "url": {"command": "/bin/sh", "env": {"PRINTER": "http://192.0.2.10:7125"}},
        "log_ok": {"command": "/bin/sh", "env": {"AUDIT": str(root / "audit.jsonl")}},
        "log_bad": {"command": "/bin/sh", "env": {"AUDIT": str(root / "gone" / "a.jsonl")}},
    }
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": cases}), encoding="utf-8")

    class _Rep:
        def add(self, *args: Any, **kwargs: Any) -> None:
            pass

    out: dict[str, Any] = {}
    doctor.check_other_servers(_Rep(), out, config)
    flagged = " ".join((out.get("other_servers") or {}).get("problems") or [])
    check("drift" in flagged,
          "a missing .app whose PARENT exists must still be flagged — that is the real "
          "config-drift incident this check exists for")
    check("url" not in flagged,
          "a URL must not be read as a path (`http://host:7125` -> head `//host`)")
    check("log_ok" not in flagged,
          "a file the server creates on first write is not dangling when its directory exists")
    check("log_bad" in flagged,
          "a writable file in a MISSING directory must still be flagged")
    print("   flags real drift, ignores URLs and creatable logs, still catches a missing dir")


# --------------------------------------------------------------------------------------
# shipped fabrication gate
# --------------------------------------------------------------------------------------

# Jobset severity is the report-severity bitmask from include/widgets/report_severity.h,
# not an enum ordinal.  JOB_RC serializes m_exitCodeViolations as "fail_on_error";
# unknown settings are ignored, so pin both the registered name and the fail-closed value.
FAB_DRC_SEVERITY = 0x20 | 0x10 | 0x04


def test_fab_jobset_gate() -> None:
    """Keep the shipped DRC job configured as a real, fail-closed fabrication gate."""
    section("starter fabrication jobset explicitly fails on actionable DRC findings")
    if not check(FAB_JOBSET.exists(), f"starter fabrication jobset is missing: {FAB_JOBSET}"):
        return
    payload = json.loads(FAB_JOBSET.read_text(encoding="utf-8"))
    jobs = [job for job in payload.get("jobs", [])
            if isinstance(job, dict) and job.get("type") == "pcb_drc"]
    if not check(len(jobs) == 1, f"starter fabrication jobset must contain one pcb_drc job: {jobs}"):
        return
    settings = jobs[0].get("settings") or {}
    severity = settings.get("severity")
    check(severity == FAB_DRC_SEVERITY,
          f"starter DRC severity must be the error|warning|exclusion bitmask "
          f"({FAB_DRC_SEVERITY}), got {severity!r}")
    check(settings.get("fail_on_error") is True,
          "starter DRC job must set the registered fail_on_error setting to true")
    check("exit_code_violations" not in settings,
          "starter DRC job uses unknown exit_code_violations setting; KiCad ignores it")
    check(settings.get("format") == "json", "starter DRC job must emit a machine-readable report")
    check(settings.get("output_filename") == "drc.json",
          "starter DRC job must keep the report at fab/drc.json for post-run auditing")
    print(f"   fail_on_error=true, severity={severity} (error|warning|exclusion), output=drc.json")


#: Files `phicad/bin/phicad` runs with bare `python3` -- on macOS that is the system 3.9.6,
#: not the MCP venv.  3.10-only syntax here is a runtime crash for every `phicad project`
#: user, and nothing else in the tree catches it.
PY39_FILES = ("phicad/pylib/*.py", "phicad/mcp/doctor.py", "phicad/mcp/register.py",
              "phicad/mcp/sch_worker.py", "phicad/mcp/session_worker.py")


def test_py39_boundary() -> None:
    """Reject 3.10-only syntax in the files run by bare python3.

    `ast.parse(feature_version=(3, 9))` needs no 3.9 interpreter, so this gate also works
    from the 3.13 MCP venv and on a Linux runner whose `python3` is 3.12 -- unlike
    `python3 -m py_compile`, which only tests whatever interpreter happens to be first.
    """
    section("python 3.9 syntax boundary (files run by bare python3)")
    files: list[Path] = []
    for pattern in PY39_FILES:
        parent, _, name = pattern.rpartition("/")
        base = REPO_ROOT / parent
        files.extend(sorted(base.glob(name)) if "*" in name else [base / name])
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        if not check(path.exists(), f"{rel}: listed as py3.9-compatible but is missing"):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(rel), feature_version=(3, 9))
        except SyntaxError as exc:
            check(False, f"{rel}:{exc.lineno}: not valid python 3.9 -- {exc.msg}. "
                         f"`phicad/bin/phicad` runs this file with bare python3 (system 3.9.6)")
    check(len(files) >= 13, f"expected at least 13 py3.9 files, found {len(files)}")
    print(f"   {len(files)} files parse as python 3.9")


# Everything reaches PhiCad as a `kicad-cli` subprocess or as protobuf IPC to a
# `kicad-cli api-server`.  There is no third channel, and in particular no GUI automation:
# a synthetic click or keystroke is unreproducible, untestable in CI, races the user's own
# pointer, and breaks on any layout change.  The dirs below are the agent-facing surface
# (MCP tools, the shared registry behind `phicad-tool`, the CLI helpers, the synth
# pipeline); the ai-view GUI plugin is deliberately out of scope -- it may raise its own
# editor window with `/usr/bin/open`, which is a window-manager call, not screen control.
GUI_AUTOMATION_DIRS = ("phicad/mcp", "phicad/pylib", "phicad/synth")
# Split so this file does not match its own scan -- the tokens are assembled at runtime.
# (Skipping this file instead would leave the one file that names them all unchecked.)
GUI_AUTOMATION_TOKENS = tuple(a + b for a, b in (
    ("osa", "script"), ("apple", "script"), ("system ", "events"), ("pyauto", "gui"),
    ("cli", "click"), ("screen", "capture"), ("py", "nput"), ("xdo", "tool"),
    ("robot", "js"), ("axui", "element"), ("cgevent", "post")))


def test_no_gui_automation() -> None:
    """Keep screen control out of the MCP/CLI surface.

    Greps rather than imports, because the point is that no such dependency exists to be
    imported.  Scans source text only, so a token named in a docstring to say "we do NOT do
    this" would still trip it -- that is intentional: the explanation belongs in the docs,
    not in the code that would look like the thing it disclaims.
    """
    section("no GUI automation in the MCP/CLI surface")
    scanned = 0
    for rel_dir in GUI_AUTOMATION_DIRS:
        base = REPO_ROOT / rel_dir
        if not check(base.is_dir(), f"{rel_dir}: expected directory is missing"):
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            lowered = path.read_text(encoding="utf-8", errors="replace").lower()
            rel = path.relative_to(REPO_ROOT)
            for token in GUI_AUTOMATION_TOKENS:
                check(token not in lowered,
                      f"{rel}: contains {token!r}. The MCP/CLI surface reaches PhiCad only "
                      f"via a kicad-cli subprocess or protobuf IPC -- never by driving the "
                      f"screen. A missing capability needs a new tool or CLI flag, which is "
                      f"reviewable; a synthetic click is not.")
    check(scanned >= 25, f"expected to scan at least 25 python files, scanned {scanned}")
    print(f"   {scanned} files carry no GUI-automation call")


# --------------------------------------------------------------------------------------


def main() -> int:
    print(f"registry contract test\nrepo   : {REPO_ROOT}\npython : {sys.executable}")
    if not PHICAD_TOOL.exists():
        print(f"FAIL: {PHICAD_TOOL} missing")
        return 2
    specs = registry.collect(registry.load_server_module(MCP_DIR))
    print(f"tools  : {len(specs)}")

    tests: list[Callable[[], None]] = [
        lambda: test_names(specs),
        lambda: test_docstrings(specs),
        lambda: test_type_hints(specs),
        lambda: test_schemas(specs),
        lambda: test_mutating_documented(specs),
        lambda: test_sections(specs),
        lambda: test_callables(specs),
        lambda: test_renderers(specs),
        test_coercion,
        lambda: test_kwargs_building(specs),
        test_cli_list,
        test_cli_describe,
        test_cli_call,
        test_cli_snapshot_env,
        lambda: test_cli_docs(specs),
        test_cli_passthrough,
        test_doctor_dangling_paths,
        lambda: test_docs_no_per_tool_prose(specs),
        lambda: test_docs_section_keys(specs),
        lambda: test_docs_tool_counts(specs),
        test_fab_jobset_gate,
        test_py39_boundary,
        test_no_gui_automation,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:                                       # noqa: BLE001
            import traceback

            FAILURES.append(f"{getattr(t, '__name__', 'test')} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print(f"\n{'=' * 78}")
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
        for f in FAILURES:
            print(f"  * {f}")
        return 1
    print(f"all {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
