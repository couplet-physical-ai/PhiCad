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
"""phicad-tool — every MCP tool, callable from a shell with identical semantics.

WHY
    An agent in a chat client reaches the tools over MCP; an agent in a terminal (or a CI job,
    or a human debugging one tool) has no client. Re-implementing the surface as shell commands
    would give two behaviours to keep in sync, so this front end calls the SAME python function
    the MCP server registers — `registry.collect()` hands over `tool.fn`, and this module
    invokes it in-process. There is no second code path to drift.

COMMANDS
    list [--json] [--section S] [--mutating]   what exists
    describe <tool>                            signature, JSON Schema, full docstring
    call <tool> [--arg k=v ...] [--json '{}'] [--stdin] [--raw] [--project-snapshot]
                                               run it; prints the result dict as JSON
    docs                                       render the generated reference page to stdout
    selftest [--full ...]                      run phicad/mcp/test_handshake.py (real stdio MCP)
    doctor [...]                               run phicad/mcp/doctor.py
    serve                                      run the MCP server on stdio (what a client does)

EXIT CODES  (the whole point of the CLI: a shell can branch on the verdict)
    0  the tool ran and returned ok=true            (or an informational command succeeded)
    1  the tool ran and returned ok=false           (a real EDA failure: DRC missing, no venv…)
    2  usage error                                 (unknown tool, bad --arg, bad JSON)

ARGUMENT COERCION
    `--arg key=value` values are strings on a command line, but the schema knows better. For
    each parameter the JSON Schema types decide: boolean accepts true/false/1/0/yes/no,
    integer/number parse numerically, array/object parse as JSON, and a string parameter is
    taken verbatim (never JSON-decoded — a board path is not a JSON document). Unknown keys are
    a usage error, so a typo cannot silently become a default.

Run under the MCP venv:  build/mcp-venv/bin/python phicad/mcp/phicad_tool.py <command>
Normally reached through: phicad/bin/phicad-tool <command>
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

MCP_DIR = Path(__file__).resolve().parent
REPO_ROOT = MCP_DIR.parent.parent

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import registry                                                            # noqa: E402

EXIT_OK = 0
EXIT_TOOL_FAILED = 1
EXIT_USAGE = 2

DOCS_TARGET = "phicad/docs-site/docs/reference/mcp-tools-generated.md"


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


def _load_specs() -> list[registry.ToolSpec]:
    """Import the server and collect the registry.

    The warnings filter is not cosmetic: importing fastmcp pulls in authlib, which prints two
    AuthlibDeprecationWarnings to stderr on every single invocation. On a CLI whose whole
    contract is "stdout is JSON, stderr is a problem", that trains callers to ignore stderr.
    Only this import is silenced; anything a tool itself warns about still comes through.

    authlib.deprecate has to be imported FIRST: its module body runs
    `warnings.simplefilter("always", AuthlibDeprecationWarning)`, which would otherwise be
    installed after our filter and win. Importing it up front means our "ignore" is the last
    word. Missing/renamed authlib is not an error here — then there is nothing to silence.
    """
    import warnings

    with warnings.catch_warnings():
        try:
            import authlib.deprecate            # noqa: F401  (imported for its side effect)
        except Exception:                       # noqa: BLE001
            pass
        warnings.simplefilter("ignore", DeprecationWarning)
        server = registry.load_server_module(MCP_DIR)
    return registry.collect(server)


def _find(specs: list[registry.ToolSpec], name: str) -> registry.ToolSpec | None:
    for s in specs:
        if s.name == name:
            return s
    return None


def _suggest(specs: list[registry.ToolSpec], name: str) -> list[str]:
    """Cheap did-you-mean: substring matches, then shared prefix."""
    low = name.lower()
    hits = [s.name for s in specs if low in s.name.lower()]
    if hits:
        return hits[:8]
    head = low.split("_", 1)[0]
    return [s.name for s in specs if s.name.startswith(head)][:8]


def _filter(specs: list[registry.ToolSpec], section: str | None,
            mutating: bool) -> list[registry.ToolSpec]:
    out = specs
    if section:
        want = registry.section_key(section)
        out = [s for s in out
               if s.section_key == want or want in s.section_key
               or want in registry.section_key(s.section)]
    if mutating:
        out = [s for s in out if s.mutates]
    return out


# --------------------------------------------------------------------------------------
# coercion
# --------------------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


class CoercionError(ValueError):
    """A --arg value cannot be read as the type the schema asks for."""


def coerce_value(raw: str, schema: dict[str, Any]) -> Any:
    """Turn one command-line string into the type the tool's JSON Schema declares.

    The schema is the authority, not a guess at the text: `--arg severity_all=false` must reach
    a `bool` parameter as False (the string "false" is truthy and would silently invert the
    flag), while `--arg board_path=/tmp/[1].kicad_pcb` must reach a `str` parameter unchanged
    even though it happens to look like JSON.
    """
    types = registry.schema_types(schema)
    if not types:                          # untyped / Any: JSON when it parses, else the string
        return _json_or_str(raw)
    for typ in types:
        if typ == "boolean":
            low = raw.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            continue
        if typ == "integer":
            try:
                return int(raw.strip(), 10)
            except ValueError:
                continue
        if typ == "number":
            try:
                return float(raw.strip())
            except ValueError:
                continue
        if typ in ("array", "object"):
            parsed = _json_or_none(raw)
            if (typ == "array" and isinstance(parsed, list)) or \
               (typ == "object" and isinstance(parsed, dict)):
                return parsed
            if typ == "array":
                # A bare comma list is the obvious shell spelling of a list of strings.
                return [part for part in (p.strip() for p in raw.split(",")) if part]
            continue
        if typ == "string":
            return raw
        if typ == "null":
            if raw.strip().lower() in ("null", "none", ""):
                return None
            continue
    raise CoercionError(
        f"cannot read {raw!r} as {'|'.join(types)}"
    )


def _json_or_none(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _json_or_str(raw: str) -> Any:
    parsed = _json_or_none(raw)
    return raw if parsed is None and raw.strip().lower() not in ("null", "none") else parsed


def build_kwargs(spec: registry.ToolSpec, pairs: list[str], json_blob: str | None,
                 stdin_blob: bool) -> dict[str, Any]:
    """Assemble the call kwargs from --json / --stdin (a whole object) plus --arg overrides.

    Precedence is deliberate: the JSON object provides the base (that is how a client sends
    arguments), individual --arg flags override it, so a saved payload can be tweaked from the
    command line without editing it.
    """
    schema_props: dict[str, Any] = dict((spec.parameters.get("properties") or {}))
    known = {p.name for p in spec.params}
    kwargs: dict[str, Any] = {}

    blobs: list[str] = []
    if json_blob:
        blobs.append(json_blob)
    if stdin_blob:
        text = sys.stdin.read().strip()
        if text:
            blobs.append(text)
    for blob in blobs:
        try:
            payload = json.loads(blob)
        except ValueError as exc:
            raise CoercionError(f"--json/--stdin is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise CoercionError("--json/--stdin must be a JSON object of arguments")
        kwargs.update(payload)

    for pair in pairs:
        if "=" not in pair:
            raise CoercionError(f"--arg needs key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if key not in known:
            raise CoercionError(
                f"unknown parameter {key!r} for {spec.name}; "
                f"known: {', '.join(sorted(known)) or '(none)'}"
            )
        kwargs[key] = coerce_value(raw, schema_props.get(key, {}))

    unknown = sorted(set(kwargs) - known)
    if unknown:
        raise CoercionError(
            f"unknown parameter(s) {', '.join(unknown)} for {spec.name}; "
            f"known: {', '.join(sorted(known)) or '(none)'}"
        )
    missing = sorted(p.name for p in spec.params if p.required and p.name not in kwargs)
    if missing:
        raise CoercionError(f"missing required parameter(s): {', '.join(missing)}")
    return kwargs


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    all_specs = _load_specs()
    specs = _filter(all_specs, args.section, args.mutating)
    if args.section and not specs:
        # An unknown --section is a typo, not "this section is empty": fail loudly with the
        # real keys rather than printing nothing and exiting 0.
        keys = sorted({s.section_key for s in all_specs})
        print(f"usage error: no section matches {args.section!r}\nsections: {', '.join(keys)}",
              file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(registry.render_json(specs))
        return EXIT_OK
    if not specs:
        print("no tools match", file=sys.stderr)
        return EXIT_OK
    width = max(len(s.name) for s in specs)
    current = None
    for s in specs:
        if s.section != current:
            current = s.section
            print(f"\n{s.section}  [{s.section_key}]")
        mark = "W" if s.mutates else " "
        print(f"  {mark} {s.name:<{width}}  {_clip(s.summary, 100)}")
    writes = sum(1 for s in specs if s.mutates)
    n_sec = len(registry.sections(specs))
    print(f"\n{len(specs)} tools, {n_sec} section{'' if n_sec == 1 else 's'}, {writes} write "
          f"(W = can change existing files or live session state)")
    print("describe one: phicad-tool describe <tool>")
    return EXIT_OK


def _clip(text: str, width: int) -> str:
    """Trim to a width on a word boundary, so a summary never breaks mid-word."""
    if len(text) <= width:
        return text
    cut = text[: width - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut + "…"


def cmd_describe(args: argparse.Namespace) -> int:
    specs = _load_specs()
    spec = _find(specs, args.tool)
    if spec is None:
        print(f"unknown tool {args.tool!r}", file=sys.stderr)
        hints = _suggest(specs, args.tool)
        if hints:
            print(f"did you mean: {', '.join(hints)}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(spec.as_dict(), indent=2, default=str))
        return EXIT_OK
    print(f"{spec.name}\n{'=' * len(spec.name)}")
    print(f"\n  {spec.signature} -> {spec.returns or 'dict'}")
    print(f"\n  section : {spec.section}  [{spec.section_key}]")
    print(f"  source  : {spec.file}:{spec.line}")
    print(f"  mutates : {'YES — ' + (spec.mutate_reason or '') if spec.mutates else 'no'}")
    if spec.doc:
        print("\n" + "\n".join(f"  {ln}" for ln in spec.doc.splitlines()))
    if spec.params:
        print("\n  parameters")
        for p in spec.params:
            req = "required" if p.required else f"default={p.default!r}"
            print(f"    {p.name:<18} {p.type_hint:<24} {req}")
    print("\n  json schema")
    for ln in json.dumps(spec.parameters, indent=2).splitlines():
        print(f"    {ln}")
    example = " ".join(f"--arg {p.name}=<{p.type_hint}>" for p in spec.params if p.required)
    print(f"\n  call it: phicad-tool call {spec.name} {example}".rstrip())
    return EXIT_OK


def cmd_call(args: argparse.Namespace) -> int:
    specs = _load_specs()
    spec = _find(specs, args.tool)
    if spec is None:
        print(f"unknown tool {args.tool!r}", file=sys.stderr)
        hints = _suggest(specs, args.tool)
        if hints:
            print(f"did you mean: {', '.join(hints)}", file=sys.stderr)
        return EXIT_USAGE
    try:
        kwargs = build_kwargs(spec, args.arg or [], args.json_args, args.stdin)
    except CoercionError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Plumbing only: the server-side journal hook reads this and snapshots the project before
    # a mutating tool touches it. Set in this process's env so the in-process tool call sees it.
    if args.project_snapshot:
        os.environ["PHICAD_TOOL_SNAPSHOT"] = "1"

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "tool": spec.name, "arguments": kwargs,
                          "mutates": spec.mutates}, indent=2, default=str))
        return EXIT_OK

    fn = spec.fn
    try:
        result = asyncio_run(fn(**kwargs)) if inspect.iscoroutinefunction(fn) else fn(**kwargs)
    except TypeError as exc:
        # Wrong arity/type reaching the function is a usage error, not a tool failure.
        print(f"usage error: {spec.name}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:                                     # noqa: BLE001
        # Tools are contracted never to raise; if one does, report it in the tool's own shape
        # so a caller parsing stdout still gets JSON, and exit 1 (a failure, not bad usage).
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tool": spec.name}

    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    text = (json.dumps(result, default=str) if args.raw
            else json.dumps(result, indent=2, default=str))
    print(text)
    return EXIT_OK if result.get("ok") else EXIT_TOOL_FAILED


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def cmd_docs(args: argparse.Namespace) -> int:
    specs = _load_specs()
    page = registry.render_markdown(specs)
    if args.out:
        dest = Path(args.out).expanduser()
        if not dest.is_absolute():
            dest = (REPO_ROOT / dest).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page)
        print(f"wrote {dest} ({len(page)} bytes, {len(specs)} tools)", file=sys.stderr)
    else:
        sys.stdout.write(page)
    return EXIT_OK


def _exec_python(script: Path, extra: list[str]) -> int:
    """Run one of the sibling scripts with THIS interpreter (already the MCP venv).

    exec-style: no output capture, so a long selftest streams. Returns its exit code.
    """
    if not script.exists():
        print(f"missing script: {script}", file=sys.stderr)
        return EXIT_USAGE
    proc = subprocess.run([sys.executable, str(script), *extra], cwd=str(REPO_ROOT))
    return proc.returncode


def cmd_selftest(args: argparse.Namespace) -> int:
    return _exec_python(MCP_DIR / "test_handshake.py", args.rest)


def cmd_registry_test(args: argparse.Namespace) -> int:
    return _exec_python(MCP_DIR / "test_registry.py", args.rest)


def cmd_doctor(args: argparse.Namespace) -> int:
    return _exec_python(MCP_DIR / "doctor.py", args.rest)


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server on stdio — the same command an MCP client is configured with."""
    server = MCP_DIR / "server.py"
    if not server.exists():
        print(f"missing server: {server}", file=sys.stderr)
        return EXIT_USAGE
    os.execv(sys.executable, [sys.executable, str(server), *args.rest])
    return EXIT_USAGE            # unreachable


# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phicad-tool",
        description="Call any phicad MCP tool from a shell (same functions, same semantics).",
        epilog="exit codes: 0 ok=true, 1 ok=false, 2 usage error",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    pl = sub.add_parser("list", help="list the registered tools")
    pl.add_argument("--json", action="store_true", help="full registry as JSON")
    pl.add_argument("--section", help="filter by section key (e.g. gates, autoroute, session)")
    pl.add_argument("--mutating", action="store_true",
                    help="only tools that can change existing files or live state")
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("describe", help="signature, JSON schema and docstring of one tool")
    pd.add_argument("tool")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_describe)

    pc = sub.add_parser("call", help="call one tool and print its result dict as JSON")
    pc.add_argument("tool")
    pc.add_argument("--arg", action="append", metavar="KEY=VALUE",
                    help="one argument, coerced by the tool's schema (repeatable)")
    pc.add_argument("--json", dest="json_args", metavar="JSON",
                    help="all arguments as one JSON object")
    pc.add_argument("--stdin", action="store_true",
                    help="read a JSON object of arguments from stdin")
    pc.add_argument("--raw", action="store_true", help="compact one-line JSON result")
    pc.add_argument("--dry-run", action="store_true",
                    help="print the coerced arguments and exit without calling")
    pc.add_argument("--project-snapshot", action="store_true",
                    help="set PHICAD_TOOL_SNAPSHOT=1 (server-side git journal hook)")
    pc.set_defaults(func=cmd_call)

    pdoc = sub.add_parser("docs", help=f"render the generated tool reference ({DOCS_TARGET})")
    pdoc.add_argument("-o", "--out", nargs="?", const=DOCS_TARGET,
                      help=f"write to a file instead of stdout (default {DOCS_TARGET})")
    pdoc.set_defaults(func=cmd_docs)

    pst = sub.add_parser("selftest", help="run the end-to-end MCP handshake test")
    pst.add_argument("rest", nargs=argparse.REMAINDER)
    pst.set_defaults(func=cmd_selftest)

    prt = sub.add_parser("registry-test", help="run the registry/CLI contract test")
    prt.add_argument("rest", nargs=argparse.REMAINDER)
    prt.set_defaults(func=cmd_registry_test)

    pdr = sub.add_parser("doctor", help="run the MCP environment doctor")
    pdr.add_argument("rest", nargs=argparse.REMAINDER)
    pdr.set_defaults(func=cmd_doctor)

    psv = sub.add_parser("serve", help="run the MCP server on stdio")
    psv.add_argument("rest", nargs=argparse.REMAINDER)
    psv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `phicad-tool docs | head` and `list | grep -q x` close the pipe early. That is a
        # normal shell idiom, not an error: swallow it, and take stdout out of the way so
        # python's interpreter shutdown does not try to flush it again and print its own
        # "Exception ignored" traceback.
        try:
            sys.stdout.close()
        except Exception:                                  # noqa: BLE001
            pass
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        return EXIT_OK
    except ImportError as exc:
        print(f"cannot import the MCP server ({exc}) — is this build/mcp-venv/bin/python?",
              file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
