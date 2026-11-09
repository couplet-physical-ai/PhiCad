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
"""Shared tool registry for the phicad MCP surface — ONE description of every tool.

WHY THIS EXISTS
    The MCP server is the definition of the tool surface, but three other consumers need the
    same list: the command line (`phicad-tool`, so an agent without an MCP client can call any
    tool with identical semantics), the generated reference page (so the docs cannot drift from
    the code), and the registry self-test (so a new tool cannot land without a docstring, type
    hints or a note about what it writes).  Anything that hard-codes a second copy of the list
    rots within a week; this module derives everything from the live FastMCP instance.

WHAT IT PRODUCES
    `collect(server_module)` -> list[ToolSpec], one per registered tool, carrying:
      name, section (the banner comment the tool sits under in server.py, or the tools_*.py
      module it came from), module/file/line, the inspect signature, the JSON parameter schema
      FastMCP built from the annotations, the full docstring, and a `mutates` verdict.
    `render_markdown(specs)` / `render_json(specs)` turn that into the reference page and into
    machine-readable output for `phicad-tool list --json`.

HOW THE SCHEMA IS OBTAINED
    Not by re-deriving it: FastMCP already builds it.  `await mcp.get_tools()` returns
    {name: FunctionTool}, and each tool carries `.parameters` (the JSON Schema sent to clients)
    and `.fn` (the undecorated python function — which is also what `phicad-tool call` invokes,
    so the CLI and an MCP client run the exact same code path).

IMPORTING THE SERVER IS SAFE
    server.py builds the FastMCP object and resolves paths at import time but starts nothing
    until main(); it does not import kipy (that only ever runs as a subprocess under
    build/venv/bin/python).  So this module can import it, and must be run under
    build/mcp-venv/bin/python (fastmcp lives there).

Run:  build/mcp-venv/bin/python phicad/mcp/registry.py [--json]
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

MCP_DIR = Path(__file__).resolve().parent
REPO_ROOT = MCP_DIR.parent.parent

# --------------------------------------------------------------------------------------
# The mutation heuristic
#
# A tool "mutates" when it can change a file that already existed (as opposed to writing a
# fresh artifact under the MCP out dir) or change live session/project state.  The registry
# needs to say so without executing anything, so this is name- and docstring-based on purpose;
# it is the input to `phicad-tool list --mutating`, to the docs page's warning column, and to
# test_registry.py's rule that a mutating tool must document what it writes.
# --------------------------------------------------------------------------------------

# Exact prefixes. Kept explicit rather than clever: a wrong guess here is either a missing
# warning in the docs or a false alarm, and both are worse than a long list.
_MUTATING_PREFIXES = (
    "session_add", "session_update", "session_delete", "session_set", "session_move",
    "session_rotate", "session_batch", "session_refill", "session_revert",
    "autoroute_",
    "project_snapshot", "project_restore", "project_undo", "project_tag",
    "sch_set", "sch_add", "sch_delete", "sch_place",
    "pcb_upgrade", "pcb_import",
)

_MUTATING_EXACT = ("swarm_merge", "synth_build")

# `*_import*` anywhere in the name (pcb_import_ses, sch_import_netlist, …).
_MUTATING_CONTAINS = ("_import",)

_MUTATING_DOC_MARKERS = ("in place",)


def classify_mutating(name: str, doc: str) -> tuple[bool, str | None]:
    """Decide whether a tool can modify pre-existing files or live state.

    Returns (mutates, reason) where reason names the rule that fired, so a surprising verdict
    can be traced to one line of this module instead of guessed at.
    """
    for prefix in _MUTATING_PREFIXES:
        if name.startswith(prefix):
            return True, f"name starts with {prefix!r}"
    if name in _MUTATING_EXACT:
        return True, f"{name!r} is a known writer"
    for frag in _MUTATING_CONTAINS:
        if frag in name:
            return True, f"name contains {frag!r}"
    low = (doc or "").lower()
    for marker in _MUTATING_DOC_MARKERS:
        if marker in low:
            return True, f"docstring mentions {marker!r}"
    return False, None


# Vocabulary that counts as "this docstring says where the bytes go". A mutating tool has to
# tell the caller what it writes — an agent that cannot see the file system decides whether to
# snapshot first from the docstring alone. Either a write verb, or a named destination
# parameter (out_board / out_dir / output), satisfies the rule.
_WRITE_WORDS = (
    "write", "wrote", "written", "writes", "in place", "in-place",
    "save", "saves", "saved", "overwrit", "rewrit", "modif", "mutat",
    "output", "outputs", "creates", "created", "applies", "applied", "apply",
    "replace", "merge", "commit", "compile", "snapshot", "restore",
)

_DEST_PARAM_HINTS = ("out", "output", "dest", "target")


def documents_writes(name: str, doc: str, param_names: Iterable[str]) -> tuple[bool, str | None]:
    """Does this tool say where its bytes land? (mutating tools must; test_registry enforces it)

    Returns (documented, evidence). Evidence is the word or parameter that satisfied the rule,
    so a failing self-test can point at the fix instead of just saying "add some words".
    """
    low = (doc or "").lower()
    for word in _WRITE_WORDS:
        if word in low:
            return True, f"docstring says {word!r}"
    for pname in param_names:
        head = pname.lower().split("_", 1)[0]
        if head in _DEST_PARAM_HINTS:
            return True, f"destination parameter {pname!r}"
    return False, None


# --------------------------------------------------------------------------------------
# Section discovery — the banner comments in server.py are the section names
# --------------------------------------------------------------------------------------

# A banner is a comment block fenced by two rule lines:
#     # ---------------------------------------
#     # Gates — DRC / ERC
#     # ---------------------------------------
_RULE_RE = re.compile(r"^#\s*-{10,}\s*$")
_COMMENT_RE = re.compile(r"^#\s?(.*)$")


def _banners(path: Path) -> list[tuple[int, str]]:
    """Every (line_number, title) banner in a source file, in file order.

    A single rule line with no text under it (server.py uses one as a plain separator before
    the shutdown helpers) yields nothing, so it cannot claim the tools that follow it.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        if not _RULE_RE.match(lines[i].strip()):
            i += 1
            continue
        start = i + 1                      # 1-based line number of the opening rule
        text: list[str] = []
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if _RULE_RE.match(stripped):   # closing rule -> a real banner
                break
            m = _COMMENT_RE.match(stripped)
            if not m:                      # ran out of comment: it was just a separator
                j = -1
                break
            text.append(m.group(1).rstrip())
            j += 1
        if j > 0 and text:
            title = next((t for t in text if t.strip()), "")
            if title:
                out.append((start, title.strip().rstrip(".")))
            i = j + 1
        else:
            i += 1
    return out


# A banner headline can be a whole sentence ("Semantic diffs — exit code 5 means 'documents
# differ', not an error"), which is exactly right above the code and far too long for a table
# cell or a section heading. Past this many characters, keep only the part before the first
# em-dash / dash / parenthesis and carry the rest as the section's note.
_TITLE_MAX = 56


def split_title(title: str) -> tuple[str, str]:
    """(section title, note) for a banner headline — short titles keep their whole line."""
    t = (title or "").strip().rstrip(".")
    if len(t) <= _TITLE_MAX:
        return t, ""
    parts = re.split(r"\s+[\u2014\u2013]\s+|\s+-\s+|\s*\(", t, maxsplit=1)
    head = parts[0].strip()
    note = t[len(head):].lstrip(" \u2014\u2013-(").strip()
    # The split may have eaten an opening paren; only drop the closing one if nothing inside
    # the note needs it, so "(subprocess only)" does not become "(subprocess only".
    if note.endswith(")") and note.count("(") < note.count(")"):
        note = note[:-1].strip()
    return (head or t), note


def section_key(section: str) -> str:
    """Short kebab handle for a section, for `phicad-tool list --section`.

    A banner headline is prose ("Semantic diffs — exit code 5 means 'documents differ', not an
    error"), which is right for the docs page and useless as a command-line filter. The key is
    the part before the first em-dash / dash / parenthesis, kebab-cased: "semantic-diffs".
    """
    head = split_title(section)[0]
    head = re.split(r"\s+[\u2014\u2013]\s+|\s+-\s+|\s*\(", head, maxsplit=1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", head.strip().lower()).strip("-")
    return slug or "uncategorised"


def _section_from_module(module_name: str) -> str:
    """Section name for a tools_*.py module: 'tools_autoroute' -> 'Autoroute'."""
    stem = module_name.rsplit(".", 1)[-1]
    if stem.startswith("tools_"):
        stem = stem[len("tools_"):]
    return stem.replace("_", " ").strip().capitalize()


# --------------------------------------------------------------------------------------
# ToolSpec
# --------------------------------------------------------------------------------------


@dataclasses.dataclass
class ParamSpec:
    """One tool parameter as both python and JSON Schema see it."""

    name: str
    annotation: str
    default: Any
    required: bool
    schema: dict[str, Any]

    @property
    def type_hint(self) -> str:
        """Short type label for the docs table (from the annotation, schema as fallback)."""
        if self.annotation:
            return self.annotation
        return json_type_label(self.schema)


@dataclasses.dataclass
class ToolSpec:
    """Everything the CLI, the docs page and the self-test need about one MCP tool."""

    name: str
    section: str
    section_note: str                    # the rest of a long banner headline, if any
    section_key: str                     # short kebab handle, for --section
    section_source: str                  # "banner" | "module" | "unknown"
    module: str
    file: str
    line: int
    signature: str
    returns: str
    params: list[ParamSpec]
    parameters: dict[str, Any]           # the JSON Schema FastMCP sends to clients
    doc: str
    summary: str
    mutates: bool
    mutate_reason: str | None

    # The live callable. Excluded from json/markdown output; `phicad-tool call` uses it.
    fn: Any = dataclasses.field(default=None, repr=False, compare=False)

    @property
    def sync(self) -> bool:
        return not inspect.iscoroutinefunction(self.fn)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (no callables, no Paths)."""
        return {
            "name": self.name,
            "section": self.section,
            "section_note": self.section_note,
            "section_key": self.section_key,
            "section_source": self.section_source,
            "module": self.module,
            "file": self.file,
            "line": self.line,
            "signature": self.signature,
            "returns": self.returns,
            "mutates": self.mutates,
            "mutate_reason": self.mutate_reason,
            "summary": self.summary,
            "doc": self.doc,
            "params": [
                {
                    "name": p.name,
                    "annotation": p.annotation,
                    "required": p.required,
                    "default": p.default,
                    "schema": p.schema,
                }
                for p in self.params
            ],
            "parameters": self.parameters,
        }


def json_type_label(schema: dict[str, Any]) -> str:
    """Human label for a JSON Schema fragment ('string', 'string|null', 'array[string]')."""
    if not isinstance(schema, dict):
        return "any"
    if "anyOf" in schema:
        return "|".join(json_type_label(s) for s in schema["anyOf"])
    typ = schema.get("type")
    if typ == "array":
        items = schema.get("items")
        return f"array[{json_type_label(items)}]" if isinstance(items, dict) else "array"
    if isinstance(typ, list):
        return "|".join(str(t) for t in typ)
    return str(typ or "any")


def schema_types(schema: dict[str, Any]) -> list[str]:
    """Flat list of JSON Schema type tokens for a parameter, null last.

    `phicad-tool call` coerces a --arg string with this: the first non-null type wins, and a
    remaining "string" branch is what makes an unparsable value fall back to the raw string.
    """
    out: list[str] = []

    def walk(s: Any) -> None:
        if not isinstance(s, dict):
            return
        for branch in s.get("anyOf") or ():
            walk(branch)
        typ = s.get("type")
        if isinstance(typ, list):
            out.extend(str(t) for t in typ)
        elif typ:
            out.append(str(typ))
        if "enum" in s and not typ:
            out.append("string")

    walk(schema)
    seen: list[str] = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return [t for t in seen if t != "null"] + (["null"] if "null" in seen else [])


# --------------------------------------------------------------------------------------
# collect()
# --------------------------------------------------------------------------------------


def _server_mcp(server_module: Any) -> Any:
    mcp = getattr(server_module, "mcp", None)
    if mcp is None:
        raise RuntimeError(f"{server_module!r} has no `mcp` FastMCP instance")
    return mcp


async def collect_async(server_module: Any) -> list[ToolSpec]:
    """Build the ToolSpec list from a live FastMCP instance (async: get_tools() is a coroutine)."""
    mcp = _server_mcp(server_module)
    tools: dict[str, Any] = await mcp.get_tools()
    banner_cache: dict[str, list[tuple[int, str]]] = {}
    specs: list[ToolSpec] = []

    for name, tool in tools.items():
        callable_fn = getattr(tool, "fn", None) or tool
        # Introspect the ORIGINAL function (source file, line, banner section) but keep the
        # registered callable for `.fn`: server.py wraps mutating tools with the project-git
        # journal hook, and calling the unwrapped function would silently skip journaling —
        # so `phicad-tool call` would not log what the MCP client does log.
        fn = inspect.unwrap(callable_fn)
        doc = inspect.cleandoc(fn.__doc__ or getattr(tool, "description", "") or "")
        try:
            src_file = Path(inspect.getsourcefile(fn) or "").resolve()
        except (TypeError, OSError):
            src_file = Path("")
        try:
            line = inspect.getsourcelines(fn)[1]
        except (TypeError, OSError):
            line = 0
        module_name = getattr(fn, "__module__", "?")

        if src_file.name == "server.py":
            if str(src_file) not in banner_cache:
                banner_cache[str(src_file)] = _banners(src_file)
            section, source = _pick_banner(banner_cache[str(src_file)], line)
        elif src_file.name.startswith("tools_") or module_name.startswith("tools_"):
            section, source = _section_from_module(module_name), "module"
        else:
            section, source = "Uncategorised", "unknown"

        sig = inspect.signature(fn)
        schema = dict(getattr(tool, "parameters", None) or {})
        # NOTE inspect renders the annotations of a `from __future__ import annotations` module
        # as quoted strings ('str'), which is noise in a reference page — format them by hand.
        props: dict[str, Any] = dict(schema.get("properties") or {})
        required = set(schema.get("required") or ())
        params: list[ParamSpec] = []
        for pname, p in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            params.append(
                ParamSpec(
                    name=pname,
                    annotation=_unquote(_annotation_str(p.annotation)),
                    default=_jsonable(p.default) if p.default is not inspect._empty else None,
                    required=(p.default is inspect._empty) or pname in required,
                    schema=props.get(pname, {}),
                )
            )
        mutates, reason = classify_mutating(name, doc)
        title, note = split_title(section)
        specs.append(
            ToolSpec(
                name=name,
                section=title,
                section_note=note,
                section_key=section_key(section),
                section_source=source,
                module=module_name,
                file=_rel(src_file),
                line=line,
                signature=_format_signature(name, sig),
                returns=_unquote(_annotation_str(sig.return_annotation)),
                params=params,
                parameters=schema,
                doc=doc,
                summary=summarize(doc),
                mutates=mutates,
                mutate_reason=reason,
                fn=callable_fn,
            )
        )

    # Stable, useful order: section as first seen in the source, then by definition line.
    order: dict[str, int] = {}
    for s in sorted(specs, key=lambda s: (s.file, s.line)):
        order.setdefault(s.section, len(order))
    specs.sort(key=lambda s: (order.get(s.section, 99), s.file, s.line, s.name))
    return specs


def collect(server_module: Any) -> list[ToolSpec]:
    """Sync wrapper around collect_async (raises if called from inside a running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(collect_async(server_module))
    raise RuntimeError("collect() called from a running event loop — use collect_async()")


def _pick_banner(banners: list[tuple[int, str]], line: int) -> tuple[str, str]:
    """The last banner defined above `line` — i.e. the section the function sits under."""
    best: tuple[int, str] | None = None
    for start, title in banners:
        if start < line and (best is None or start > best[0]):
            best = (start, title)
    return (best[1], "banner") if best else ("Uncategorised", "unknown")


def _format_signature(name: str, sig: inspect.Signature) -> str:
    """`pcb_drc(board_path: str, severity_all: bool = True)` — no quotes, no return annotation."""
    parts: list[str] = []
    for pname, p in sig.parameters.items():
        text = pname
        ann = _unquote(_annotation_str(p.annotation))
        if ann:
            text += f": {ann}"
        if p.default is not inspect._empty:
            # python repr, not JSON: the signature reads as python (None, True), while the
            # parameter tables and --json output stay JSON (null, true).
            default = repr(_jsonable(p.default))
            text += f" = {default}" if ann else f"={default}"
        parts.append(text)
    return f"{name}({', '.join(parts)})"


def _unquote(text: str) -> str:
    """Strip the quotes inspect puts around a stringified annotation."""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def _annotation_str(annotation: Any) -> str:
    """Annotation as source text. server.py uses `from __future__ import annotations`, so these
    are already strings; anything else is formatted the way inspect prints it."""
    if annotation is inspect._empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def summarize(doc: str) -> str:
    """One-line summary of a tool: the first SENTENCE, not the first line.

    Docstring first lines wrap at the column limit (`pcb_diff` opens with a sentence spread
    over two lines), so taking `doc.splitlines()[0]` yields "...`differs` is True when they are
    not identical" — a fragment that reads as a truncation everywhere it is shown. Join the
    first paragraph, then cut at the first sentence end.
    """
    if not doc:
        return ""
    para: list[str] = []
    for line in doc.splitlines():
        if not line.strip():
            break
        para.append(line.strip())
    text = " ".join(para)
    # ".kicad_pcb" / "e.g." must not end the sentence: require whitespace-or-end after the stop.
    m = re.search(r"[.!?](?=\s|$)", text)
    return (text[: m.end()] if m else text).strip()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def load_server_module(mcp_dir: Path | None = None) -> Any:
    """Import phicad/mcp/server.py as a module (no side effects beyond building FastMCP).

    The server is not a package, and it imports its own siblings (`import tools_autoroute`), so
    its directory has to be on sys.path — exactly what the server does for itself at runtime.
    """
    d = str((mcp_dir or MCP_DIR).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import server                                      # noqa: PLC0415  (deliberate late import)

    return server


# --------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------


def sections(specs: Iterable[ToolSpec]) -> list[str]:
    """Section names in presentation order, de-duplicated."""
    out: list[str] = []
    for s in specs:
        if s.section not in out:
            out.append(s.section)
    return out


def render_json(specs: Iterable[ToolSpec], *, indent: int | None = 2) -> str:
    """The whole registry as JSON (what `phicad-tool list --json` prints)."""
    lst = list(specs)
    payload = {
        "tool_count": len(lst),
        "sections": [
            {"key": section_key(s), "title": s,
             "tools": [t.name for t in lst if t.section == s]}
            for s in sections(lst)
        ],
        "tools": [s.as_dict() for s in lst],
    }
    return json.dumps(payload, indent=indent, default=str)


GENERATED_HEADER = """# MCP tool reference (generated)

!!! warning "Generated file — do not edit by hand"
    This page is rendered from the live FastMCP registry by

    ```bash
    phicad/bin/phicad-tool docs > phicad/docs-site/docs/reference/mcp-tools-generated.md
    ```

    Every entry below is the tool's real signature, JSON Schema and docstring as the server
    registers it, so the page cannot drift from `phicad/mcp/server.py`. Edit the tool's
    docstring instead. Hand-written narrative belongs in
    [MCP tools](mcp-tools.md).

Every tool listed here is callable three ways with identical semantics:

| Surface | How |
| --- | --- |
| MCP client (chat) | `phicad` over stdio: `build/mcp-venv/bin/python phicad/mcp/server.py` |
| Shell / agent | `phicad/bin/phicad-tool call <tool> --arg key=value` |
| Python | `registry.collect(server)` -> `spec.fn(**kwargs)` |

`ok` in every result dict is the verdict: `true` means the tool did what it says. Tools marked
**writes** can change files that already existed or live session state; everything else either
reads, or writes a new artifact under the MCP output directory.
"""


def render_markdown(specs: Iterable[ToolSpec]) -> str:
    """The full reference page: header, index table, then one block per tool."""
    lst = list(specs)
    out: list[str] = [GENERATED_HEADER, ""]
    out.append(f"**{len(lst)} tools** in {len(sections(lst))} sections.")
    out.append("")

    # Index
    out.append("## Index")
    out.append("")
    out.append("| Tool | Section | Writes | Summary |")
    out.append("| --- | --- | --- | --- |")
    for s in lst:
        # Tool names are lowercase identifiers. Markdown TOC slugs preserve underscores;
        # replacing them with hyphens links to a heading that the renderer never emits.
        anchor = s.name
        out.append(
            f"| [`{s.name}`](#{anchor}) | {_md(s.section)} | "
            f"{'**yes**' if s.mutates else 'no'} | {_md(s.summary)} |"
        )
    out.append("")

    for section in sections(lst):
        members = [t for t in lst if t.section == section]
        out.append(f"## {section}")
        out.append("")
        note = next((t.section_note for t in members if t.section_note), "")
        if note:
            out.append(f"*{note}*")
            out.append("")
        for s in members:
            out.extend(_render_tool(s))
    return "\n".join(out).rstrip() + "\n"


def _render_tool(s: ToolSpec) -> list[str]:
    out: list[str] = [f"### {s.name}", ""]
    out.append("```python")
    out.append(f"{s.signature} -> {s.returns or 'dict'}")
    out.append("```")
    out.append("")
    flags = [f"`{s.file}:{s.line}`"]
    if s.mutates:
        flags.append(f"**writes in place / changes state** ({s.mutate_reason})")
    out.append(" · ".join(flags))
    out.append("")
    if s.doc:
        out.append(s.doc)
        out.append("")
    if s.params:
        out.append("| Parameter | Type | Required | Default |")
        out.append("| --- | --- | --- | --- |")
        for p in s.params:
            default = "—" if p.required else f"`{json.dumps(p.default, default=str)}`"
            out.append(
                f"| `{p.name}` | `{_md(p.type_hint)}` | "
                f"{'yes' if p.required else 'no'} | {default} |"
            )
        out.append("")
    out.append("=== \"Shell\"")
    out.append("")
    out.append("    ```bash")
    out.append(f"    phicad/bin/phicad-tool call {s.name}{_example_args(s)}")
    out.append("    ```")
    out.append("")
    return out


def _example_args(s: ToolSpec) -> str:
    parts = [f" --arg {p.name}=<{_md(p.type_hint)}>" for p in s.params if p.required]
    return "".join(parts)


def _md(text: str) -> str:
    """Escape the few characters that break a Markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    specs = collect(load_server_module())
    if "--json" in args:
        print(render_json(specs))
    elif "--markdown" in args or "--docs" in args:
        print(render_markdown(specs), end="")
    else:
        for s in specs:
            mark = "W" if s.mutates else " "
            print(f"{mark} {s.name:26} {s.section_key:22} {s.summary[:60]}")
        print(f"\n{len(specs)} tools, {len(sections(specs))} sections "
              f"({sum(1 for s in specs if s.mutates)} write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
