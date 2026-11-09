# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""The agent suite's model tiers, checked against the live LLM gateway.

`phicad models` runs this.  Three jobs, all in service of one policy: **the
gateway is the source of truth for what models exist, cost, and can do** —
never a table copied into a doc, and never a marketing name baked into code
(see phicad/integration/README.md, "Model adapters and future GPT releases").

  list      what the gateway serves right now: alias, price, context, output
            cap, description — with the aliases the agent suite actually uses
            marked.  New model families (a GPT-6 line, say) show up here the
            day the gateway adds them, priced and described.
  check     every `model:` ref in phicad/agents/*.md resolves against the
            gateway, and its thinking suffix is legal for that model.  Exit
            code says pass/fail, so it can gate a PR.  `check --offline`
            applies only the rules that need no gateway (suffix vocabulary,
            no-reasoning models) — that subset runs in `phicad check fast`
            and CI, where no credential exists.
  retarget  rewrite agent frontmatter alias -> alias (provider and thinking
            suffix preserved), for the day a new family replaces a tier:
            `phicad models retarget fast=fast-6 strong=strong-6 --dry-run`.

The gateway answers on the same host the LLM proxy env points at, so
requests here must NOT be routed through that proxy env; run through
`phicad`'s noproxy wrapper (bin/phicad strips the vars before exec'ing us)
or a clean environment.

Auth comes through phi's public credential-handoff contract (`phi auth token --json`).
This module never reads phi's store directly, and never writes, caches, logs, or prints a key.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# The gateway is configured, not baked in: PHICAD_LLM_GATEWAY is the base URL of an
# OpenAI/LiteLLM-compatible gateway, and PHICAD_LLM_PROVIDER the phi provider id that holds the
# user's login for it (default "gateway"). Unset/empty PHICAD_LLM_GATEWAY disables every live
# model feature with a clear error; nothing is contacted.
#
# Redirecting the variable cannot leak the user's key to another host: the credential comes
# only from phi's public handoff contract (`phi auth token --json`), and it is used only if
# phi itself reports the SAME provider and baseUrl. `PHICAD_LLM_AUTH_FILE` stays inert (a
# credential file is never read) -- see `ignored_gateway_overrides()`.
GATEWAY = (os.environ.get("PHICAD_LLM_GATEWAY") or "").strip().rstrip("/")
PROVIDER = (os.environ.get("PHICAD_LLM_PROVIDER") or "gateway").strip()
PHI_AUTH_TOKEN_MIN_VERSION = "0.83.0-exo.45"
CLIENT_TAG = "client=phicad"

#: Formerly-honoured overrides, now inert. Named so `check` and the tests agree.
IGNORED_GATEWAY_ENV = ("PHICAD_LLM_AUTH_FILE",)


def ignored_gateway_overrides(env: dict | None = None) -> List[str]:
    """Which inert gateway overrides are set, so callers can say they did nothing."""
    src = os.environ if env is None else env
    return [k for k in IGNORED_GATEWAY_ENV if (src.get(k) or "").strip()]

# phi's thinking-level vocabulary (suffix after ':' in a model ref).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# Aliases with no reasoning support: a thinking suffix on these is at best
# ignored and at worst a hard request error, so `check` rejects it.
NO_REASONING = ("bulk",)

_MODEL_LINE = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)


def repo_root() -> Path:
    """The checkout containing this file (…/phicad/pylib/gateway_models.py)."""
    return Path(__file__).resolve().parent.parent.parent


def _phi_executable(explicit: Optional[str] = None) -> Optional[str]:
    """The one credential broker: phi on PATH, then its documented install path."""
    if explicit:
        return explicit
    found = shutil.which("phi")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "phi"
    return str(fallback) if os.access(fallback, os.X_OK) else None


def _api_key(
        phi_executable: Optional[str] = None,
        *,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        timeout_s: float = 10.0) -> str:
    """The signed-in user's gateway key, through phi's public contract.

    Captured into one protected pipe and returned only to the request that needs it. The token is
    never placed in argv or an environment variable, and no failure includes stdout/stderr: even
    a broken or hostile broker cannot make a credential appear in diagnostics.
    """
    if not GATEWAY:
        raise RuntimeError(
            "no LLM gateway configured; set PHICAD_LLM_GATEWAY (and PHICAD_LLM_PROVIDER) "
            "to enable live models")
    executable = _phi_executable(phi_executable)
    if executable is None:
        raise RuntimeError(
            "phi is not installed; install phi and sign in before using live models")

    invoke = runner or subprocess.run
    argv = [executable, "auth", "token", "--json"]
    try:
        result = invoke(argv, capture_output=True, text=True, check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("phi auth token timed out; the credential handoff did not complete") \
            from exc
    except OSError as exc:
        raise RuntimeError(f"could not run phi auth token ({type(exc).__name__})") from exc

    if result.returncode != 0:
        reasons = {
            1: ("this phi does not provide the stable auth-token contract; run `phi update self` "
                f"(PhiCad requires phi >= {PHI_AUTH_TOKEN_MIN_VERSION})"),
            2: "phi is not signed in to the gateway; sign in with phi first",
            3: "phi's credential store is malformed or unreadable; repair the phi login",
            4: "phi refused credential output because stdout was a TTY (PhiCad uses a pipe)",
        }
        reason = reasons.get(result.returncode,
                             f"phi auth token failed with exit status {result.returncode}")
        raise RuntimeError(reason)

    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("phi auth token returned malformed JSON; update or repair phi") from exc

    if (payload.get("schemaVersion") != 1
            or payload.get("provider") != PROVIDER
            or payload.get("baseUrl") != GATEWAY):
        raise RuntimeError("phi auth token returned an unsupported credential contract")
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("phi auth token returned no gateway key; sign in with phi first")
    return token.strip()


def fetch_model_groups(timeout_s: float = 20.0) -> Dict[str, Dict[str, Any]]:
    """Live `{alias: info}` from the gateway, or raise RuntimeError with a reason."""
    key = _api_key()
    req = urllib.request.Request(
        f"{GATEWAY}/model_group/info",
        headers={"Authorization": f"Bearer {key}",
                 # LiteLLM's request-tag contract. The key identifies the person; this tag lets
                 # gateway spend views also group calls made on PhiCad's behalf.
                 "x-litellm-tags": CLIENT_TAG})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.load(resp)
    except Exception as exc:
        raise RuntimeError(
            f"gateway unreachable at {GATEWAY} ({type(exc).__name__}: {exc}); "
            "note this host must be contacted directly, not through the "
            "HTTPS_PROXY that points at it") from exc
    return {m["model_group"]: m for m in payload.get("data", [])}


def parse_ref(ref: str) -> Tuple[str, str, Optional[str]]:
    """'gateway/fast:max' -> ('gateway', 'fast', 'max'); provider/suffix optional."""
    provider, _, rest = ref.rpartition("/")
    alias, _, suffix = rest.partition(":")
    return provider, alias, (suffix or None)


def agent_refs(root: Optional[Path] = None) -> Dict[str, str]:
    """`{agent-file-name: model-ref}` for every agent definition with a model."""
    agents_dir = (root or repo_root()) / "phicad" / "agents"
    refs: Dict[str, str] = {}
    for path in sorted(agents_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        match = _MODEL_LINE.search(path.read_text())
        if match:
            refs[path.name] = match.group(1)
    return refs


def check(refs: Dict[str, str],
          groups: Optional[Dict[str, Dict[str, Any]]]) -> List[str]:
    """Problems as human-readable strings; empty means every ref is sound.

    ``groups=None`` means "no gateway answer available": alias existence is
    then not judged at all — an offline run must never fail a ref the gateway
    would have vouched for, only refs that are wrong by local rules alone.
    """
    problems: List[str] = []
    for name, ref in sorted(refs.items()):
        _, alias, suffix = parse_ref(ref)
        if groups is not None and alias not in groups:
            problems.append(f"{name}: alias '{alias}' is not served by the "
                            f"gateway (ref: {ref})")
        if suffix is not None and suffix not in THINKING_LEVELS:
            problems.append(f"{name}: unknown thinking level ':{suffix}' "
                            f"(ref: {ref})")
        if suffix is not None and alias in NO_REASONING:
            problems.append(f"{name}: '{alias}' has no reasoning support — "
                            f"drop ':{suffix}' (ref: {ref})")
    return problems


def retarget(mapping: Dict[str, str], root: Optional[Path] = None,
             dry_run: bool = False) -> List[str]:
    """Rewrite frontmatter `model:` aliases per `mapping`; returns change lines."""
    changes: List[str] = []
    agents_dir = (root or repo_root()) / "phicad" / "agents"
    for path in sorted(agents_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text()
        match = _MODEL_LINE.search(text)
        if not match:
            continue
        ref = match.group(1)
        provider, alias, suffix = parse_ref(ref)
        if alias not in mapping:
            continue
        new_alias = mapping[alias]
        new_ref = (f"{provider}/" if provider else "") + new_alias \
            + (f":{suffix}" if suffix else "")
        changes.append(f"{path.name}: {ref} -> {new_ref}")
        if not dry_run:
            path.write_text(text[:match.start(1)] + new_ref + text[match.end(1):])
    return changes


def _fmt_price(info: Dict[str, Any]) -> str:
    i = (info.get("input_cost_per_token") or 0) * 1e6
    o = (info.get("output_cost_per_token") or 0) * 1e6
    return f"{i:7.2f}/{o:8.2f}"


def cmd_list(argv: List[str]) -> int:
    groups = fetch_model_groups()
    used = {parse_ref(r)[1] for r in agent_refs().values()}
    print(f"{'alias':22s} {'$in/$out per 1M':16s} {'ctx':>9s} {'out':>8s}  agents")
    for alias in sorted(groups):
        info = groups[alias]
        ctx = info.get("max_input_tokens") or 0
        out = info.get("max_output_tokens") or 0
        mark = "  <-- agent suite" if alias in used else ""
        print(f"{alias:22s} {_fmt_price(info):16s} {int(ctx):>9d} {int(out or 0):>8d}{mark}")
    missing = sorted(used - set(groups))
    if missing:
        print(f"\nWARNING: agent refs not served by the gateway: {', '.join(missing)}")
        return 1
    return 0


def cmd_check(argv: List[str]) -> int:
    offline = "--offline" in argv
    refs = agent_refs()
    groups = None if offline else fetch_model_groups()
    problems = check(refs, groups)
    for name, ref in sorted(refs.items()):
        status = "FAIL" if any(p.startswith(name) for p in problems) else "ok"
        print(f"{status:4s} {name:24s} {ref}")
    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    print(f"{len(refs)} agent refs, {len(problems)} problem(s)"
          + (" [offline rules only]" if offline else ""))
    return 1 if problems else 0


def cmd_retarget(argv: List[str]) -> int:
    dry = "--dry-run" in argv
    pairs = [a for a in argv if "=" in a]
    if not pairs:
        print("usage: gateway_models.py retarget OLD=NEW [OLD=NEW ...] [--dry-run]",
              file=sys.stderr)
        return 2
    mapping = dict(p.split("=", 1) for p in pairs)
    # refuse to retarget onto an alias the gateway does not serve
    groups = fetch_model_groups()
    unknown = sorted(set(mapping.values()) - set(groups))
    if unknown:
        print(f"refusing: target alias(es) not on the gateway: {', '.join(unknown)}",
              file=sys.stderr)
        return 1
    changes = retarget(mapping, dry_run=dry)
    for line in changes:
        print(("DRY-RUN " if dry else "") + line)
    if not changes:
        print("no agent refs matched the mapping")
    elif not dry:
        print("rewritten; now run: phicad models check, then commit the diff. "
              "Grep docs for stale mentions: rg -n '"
              + "|".join(sorted(mapping)) + "' phicad/agents/README.md phicad/docs-site")
    return 0


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "list"
    try:
        if cmd == "list":
            return cmd_list(argv[1:])
        if cmd == "check":
            return cmd_check(argv[1:])
        if cmd == "retarget":
            return cmd_retarget(argv[1:])
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
