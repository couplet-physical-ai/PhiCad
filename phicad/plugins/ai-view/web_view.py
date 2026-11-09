#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Agent View as a local web app, so it can run in any browser (including an agent host's).

Why this exists alongside ``ai_view_gui.py``:

Agent View's desktop UI is Tk, which a browser cannot host, so a browser-hosted Agent View
needs an HTML front end.  Rather than fork the logic, this module reuses the exact
modules the Tk app uses -- ``host_bridge`` for the shared phi session, ``board_context`` for
live IPC state, ``board_views`` for renders, ``project_history`` for Git -- none of which
import tkinter.  The two front ends are therefore skins over one implementation, and a fix to
the bridge or the renderers shows up in both.

Design constraints worth keeping:

* **Loopback only, single-origin, token-gated.**  This serves a local board and can drive a
  real agent turn, so it binds 127.0.0.1, mints a per-run token, and rejects any request that
  does not carry it.  A stray page in the same browser must not be able to poke it.
* **Standard library only.**  It has to run under the same interpreter as the plugin
  (``build/venv``) with no extra dependency; ``http.server`` is enough for one local reader.
* **The browser never becomes a second writer.**  Turns go out through ``HostChatSession``,
  exactly like the Tk app, preserving the single-writer rule the whole integration depends
  on.  (This build ships an inert ``host_bridge``, so the web view never holds a session.)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_views                                          # noqa: E402
import host_bridge                                         # noqa: E402
import project_history                                      # noqa: E402

try:                                                        # optional: only for live IPC
    import board_context                                    # noqa: E402
except Exception:                                           # noqa: BLE001
    board_context = None                                    # type: ignore[assignment]


DEFAULT_PORT = int(os.environ.get("PHICAD_WEB_VIEW_PORT", "0"))
RENDER_TTL_S = 30.0
STATIC_DIR = Path(__file__).resolve().parent / "static"
MODEL_VIEWER_BUNDLE = STATIC_DIR / "model-viewer.min.js"


class ViewError(RuntimeError):
    """A request could not be served for a reason worth showing the user."""


# --------------------------------------------------------------------------- state


@dataclass
class Session:
    """Everything one served board needs, shared by all requests."""

    board: Path
    token: str
    renders: dict[str, tuple[float, str]] = field(default_factory=dict)
    chat: Optional[host_bridge.HostChatSession] = None
    log: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False

    @property
    def project_dir(self) -> Path:
        return self.board.parent

    @property
    def paths(self) -> board_views.Paths:
        return board_views.Paths(board=str(self.board))

    def descriptor(self) -> Optional[host_bridge.BridgeDescriptor]:
        """Re-read every time: the user may connect or disconnect while this is open."""
        try:
            return host_bridge.load(self.board)
        except Exception:                                   # noqa: BLE001
            return None

    def chat_session(self) -> host_bridge.HostChatSession:
        descriptor = self.descriptor()
        if descriptor is None:
            raise ViewError(
                "This board is not attached to a shared phi session. Chat from the "
                "desktop Agent View panel instead.")
        # Rebuild when the descriptor changes (reconnect, new terminal) so a stale handle
        # never silently swallows turns.
        current = self.chat.descriptor if self.chat else None
        if (current is None or current.session_id != descriptor.session_id
                or current.terminal_handle != descriptor.terminal_handle):
            self.chat = host_bridge.HostChatSession(descriptor)
        return self.chat

    def history(self) -> Path:
        return self.project_dir


# --------------------------------------------------------------------------- payloads


def _git_status(session: Session) -> dict:
    try:
        history = project_history.ProjectHistory(session.project_dir)
        status = history.status()
        return {"enabled": status.enabled, "clean": status.clean,
                "branch": status.branch, "head": status.head,
                "display": status.display()}
    except Exception as exc:                                # noqa: BLE001
        return {"enabled": False, "clean": True, "display": f"Git unavailable: {exc}"}


def _bridge_status(session: Session) -> dict:
    descriptor = session.descriptor()
    if descriptor is None:
        return {"connected": False,
                "display": "No shared session"}
    return {"connected": True, "display": descriptor.display(),
            "session_id": descriptor.session_id,
            "terminal": descriptor.terminal_handle,
            "model": host_bridge.model_report(descriptor)}


def _ipc_status() -> dict:
    """Live IPC is a bonus, not a requirement: the board file alone drives every view."""
    if board_context is None:
        return {"connected": False, "display": "kipy unavailable in this interpreter"}
    try:
        board_context.install_fork_compat()
        kicad = board_context.connect(timeout_ms=4000)
        ctx = board_context.collect_board_context(kicad)
        return {"connected": True,
                "display": f"IPC connected · {ctx.kicad_version}",
                "board": ctx.board_name, "digest": ctx.ai_digest()}
    except Exception as exc:                                # noqa: BLE001
        return {"connected": False,
                "display": f"IPC not connected ({type(exc).__name__})",
                "detail": str(exc)[:200]}


def _status_payload(session: Session, with_ipc: bool = True) -> dict:
    return {
        "ok": True,
        "board": session.board.name,
        "board_path": str(session.board),
        "project_dir": str(session.project_dir),
        "bridge": _bridge_status(session),
        "git": _git_status(session),
        "ipc": _ipc_status() if with_ipc else {"connected": False, "display": "(skipped)"},
        "busy": session.busy,
    }


def _render(session: Session, kind: str, variant: str = "") -> str:
    """Render on demand with a short TTL, so repeated tab clicks are instant but not stale."""
    key = f"{kind}:{variant}"
    now = time.monotonic()
    cached = session.renders.get(key)
    if cached and now - cached[0] < RENDER_TTL_S and os.path.isfile(cached[1]):
        return cached[1]

    out_dir = Path(tempfile.gettempdir()) / "phicad-web-view"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = str(out_dir / f"{session.board.stem}-{kind}{('-' + variant) if variant else ''}.png")
    paths = session.paths

    if kind == "pcb":
        layers = {"front": board_views.LAYERS_FRONT, "back": board_views.LAYERS_BACK,
                  "": board_views.LAYERS_BOTH,
                  "both": board_views.LAYERS_BOTH}.get(variant, board_views.LAYERS_BOTH)
        board_views.render_pcb(paths, out, layers=layers)
    elif kind == "3d":
        board_views.render_3d(paths, out, side=variant or "top")
    elif kind == "schematic":
        if not paths.has_schematic:
            raise ViewError("This project has no schematic file to render.")
        board_views.render_schematic(paths, out)
    else:
        raise ViewError(f"unknown view: {kind}")

    session.renders[key] = (now, out)
    return out


def _board_digest(board: Path) -> str:
    """Hash the saved board that determines a GLB, without trusting timestamps."""
    digest = hashlib.sha256()
    with board.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_path(session: Session) -> Path:
    """The content-addressed GLB path for the board as it exists right now."""
    out_dir = Path(tempfile.gettempdir()) / "phicad-web-view"
    board_key = hashlib.sha256(str(session.board).encode("utf-8")).hexdigest()[:12]
    return out_dir / f"{session.board.stem}-{board_key}-{_board_digest(session.board)}.glb"


def _export_model(session: Session) -> Path:
    """Export the current board to GLB once, using the same argv as pcb_export_3d."""
    out = _model_path(session)
    if out.is_file() and out.stat().st_size:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    # tools_cli.pcb_export_3d constructs this exact GLB command. Keep this small web
    # surface independent of the MCP server while sharing board_views' executable,
    # timeout, build-tree environment, and user-facing failures.
    try:
        argv = [board_views.find_kicad_cli(), "pcb", "export", "glb",
                "-o", str(out), "--force", str(session.board)]
        board_views._run(argv)
    except board_views.RenderError as exc:
        try:
            out.unlink()
        except OSError:
            pass
        raise ViewError(f"Interactive 3D export failed: {exc}") from exc
    if not out.is_file() or not out.stat().st_size:
        raise ViewError("Interactive 3D export failed: kicad-cli wrote no GLB file.")
    return out


def _chat_payload(session: Session, limit: int = 40) -> dict:
    """The shared transcript, as the browser should show it."""
    status = _bridge_status(session)
    if not status["connected"]:
        return {"ok": True, "connected": False, "messages": [],
                "display": status["display"]}
    descriptor = session.descriptor()
    assert descriptor is not None
    try:
        messages = host_bridge.session_messages(descriptor.session_file)
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "connected": True, "messages": [],
                "display": status["display"], "error": str(exc)}
    shown = [{"role": m.role, "text": m.text} for m in messages[-limit:]
             if m.role in ("user", "assistant") and m.text.strip()]
    return {"ok": True, "connected": True, "messages": shown,
            "display": status["display"], "total": len(messages)}


def _ask(session: Session, prompt: str) -> dict:
    """One shared turn: send through the host session, wait, return the reply the session recorded."""
    with session.lock:
        if session.busy:
            raise ViewError("A turn is already in flight; wait for it to finish.")
        session.busy = True
    try:
        chat = session.chat_session()
        context = None
        ipc = _ipc_status()
        if ipc.get("connected") and ipc.get("digest"):
            context = ipc["digest"]
        reply = chat.ask(prompt, context)
        return {"ok": True, "reply": reply, "git": _git_status(session)}
    finally:
        session.busy = False


# --------------------------------------------------------------------------- HTTP


def _page(token: str, board: str, board_path: str = "") -> str:
    """One self-contained page; no external assets, so it works offline in the webview."""
    return (_PAGE_TEMPLATE
            .replace("__TOKEN__", token)
            .replace("__BOARD_PATH__", html.escape(board_path or board))
            .replace("__BOARD__", html.escape(board)))


class Handler(BaseHTTPRequestHandler):
    server_version = "PhiCadAgentView/1.0"
    session: Session                                        # set on the server instance

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:         # noqa: A003
        if os.environ.get("PHICAD_WEB_VIEW_DEBUG"):
            sys.stderr.write("web-view: " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page may only talk to itself; it must not be embeddable or reachable
        # cross-origin from anything else the browser has open.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # blob: is required: renders are fetched with the auth header and handed to <img>
        # through URL.createObjectURL, which produces a blob: URL.  Omitting it makes every
        # view silently decode to a 0x0 image.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: blob:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline' blob:; worker-src blob:")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _unauthorized_page(self) -> None:
        """Refuse the shell page without disclosing the board or the token.

        Deliberately says nothing about what is being served -- not the board name, not the
        path, not whether a board exists at all. The whole point of the refusal is to reveal
        nothing to a caller who cannot already authenticate.
        """
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<title>PhiCad Agent View</title>"
            "<style>body{font:14px -apple-system,sans-serif;margin:3rem auto;max-width:32rem;"
            "color:#1a1a1f}code{background:#f2f2f4;padding:.1rem .3rem;border-radius:3px}</style>"
            "<h1>Not authorized</h1>"
            "<p>This view requires the single-use token minted when the server started.</p>"
            "<p>Open the URL printed by <code>phicad web-view</code>, which already carries it.</p>"
        ).encode("utf-8")
        self._send(HTTPStatus.FORBIDDEN, body, "text/html; charset=utf-8")

    def _authorized(self, query: dict) -> bool:
        """Constant-time token check, from either the header or the query string."""
        want = self.session.token
        got = self.headers.get("X-PhiCad-Token") or (query.get("token", [""])[0])
        return bool(got) and secrets.compare_digest(got, want)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:                               # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if route == "/":
            # The page is NOT harmless without the token, because it carries the token: it
            # embeds it as `const TOKEN` so the front end can call the guarded routes. Serving
            # it unauthenticated therefore HANDS the credential to the caller, and the guard on
            # every data route below is worth nothing against anyone who fetched `/` first.
            # Observed: an unauthenticated GET / returned a byte-identical page including the
            # live token, which then read /api/status and pulled an 80 KB board render.
            #
            # So `/` is gated like everything else. The cost is that the URL must carry the
            # token -- which is exactly how the printed URL is
            # already formed, so no caller changes.
            if not self._authorized(query):
                self._unauthorized_page()
                return
            body = _page(self.session.token, self.session.board.name,
                         str(self.session.board)).encode("utf-8")
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return

        if not self._authorized(query):
            self._json({"ok": False, "error": "unauthorized"}, HTTPStatus.FORBIDDEN)
            return

        try:
            if route == "/api/status":
                self._json(_status_payload(
                    self.session, with_ipc=query.get("ipc", ["1"])[0] != "0"))
            elif route == "/api/chat":
                self._json(_chat_payload(self.session))
            elif route == "/api/render":
                kind = query.get("kind", ["pcb"])[0]
                variant = query.get("variant", [""])[0]
                path = _render(self.session, kind, variant)
                data = Path(path).read_bytes()
                ctype = mimetypes.guess_type(path)[0] or "image/png"
                self._send(HTTPStatus.OK, data, ctype)
            elif route == "/api/model":
                path = _model_path(self.session)
                if not path.is_file():
                    self._json({"ok": False, "error": "interactive 3D model not exported"},
                               HTTPStatus.NOT_FOUND)
                else:
                    self._send(HTTPStatus.OK, path.read_bytes(), "model/gltf-binary")
            elif route == "/static/model-viewer.min.js":
                self._send(HTTPStatus.OK, MODEL_VIEWER_BUNDLE.read_bytes(),
                           "text/javascript; charset=utf-8")
            elif route == "/api/context":
                self._json({"ok": True, "ipc": _ipc_status()})
            else:
                self._json({"ok": False, "error": f"no route {route}"},
                           HTTPStatus.NOT_FOUND)
        except ViewError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:                            # noqa: BLE001
            if os.environ.get("PHICAD_WEB_VIEW_DEBUG"):
                traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:                              # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        if not self._authorized(query):
            self._json({"ok": False, "error": "unauthorized"}, HTTPStatus.FORBIDDEN)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._json({"ok": False, "error": "body was not JSON"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            if route == "/api/model":
                model = _export_model(self.session)
                self._json({"ok": True, "digest": model.stem.rsplit("-", 1)[-1]})
            elif route == "/api/ask":
                prompt = str(payload.get("prompt") or "").strip()
                if not prompt:
                    raise ViewError("Type something to send to the shared session.")
                self._json(_ask(self.session, prompt))
            elif route == "/api/snapshot":
                history = project_history.ProjectHistory(self.session.project_dir)
                message = str(payload.get("message")
                              or "phicad: snapshot from Agent View")
                committed = history.snapshot(message)
                self._json({"ok": True, "committed": committed,
                            "git": _git_status(self.session)})
            else:
                self._json({"ok": False, "error": f"no route {route}"},
                           HTTPStatus.NOT_FOUND)
        except ViewError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except host_bridge.BridgeError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except Exception as exc:                            # noqa: BLE001
            if os.environ.get("PHICAD_WEB_VIEW_DEBUG"):
                traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)


# --------------------------------------------------------------- finding a board
#
# A launcher entry may run this with no path, because a command that hardcodes one board is
# only ever right for one project. Its cwd is the project checkout, so the board is
# discovered from there.

_SKIP_DIRS = {".git", ".history", "node_modules", "__pycache__", ".venv", "venv",
              "build", "cmake-build", "dist", ".cache", "thirdparty", "qa"}
_MAX_DEPTH = 3
_LAST_BOARD_FILE = "web-view-last-board.txt"


def _last_board_path() -> Path:
    """Where the most recently served board is remembered.

    Same config root as the desktop panel's layout state, so a sandboxed run
    (`KICAD_CONFIG_HOME`) gets its own and never scribbles on the real one.
    """
    override = os.environ.get("PHICAD_WEB_VIEW_LAST_BOARD", "").strip()
    if override:
        return Path(override).expanduser()
    root = os.environ.get("KICAD_CONFIG_HOME", "").strip()
    if not root:
        if sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Preferences/kicad")
        elif sys.platform.startswith("win"):
            root = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "kicad")
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME", "").strip() or \
                os.path.expanduser("~/.config")
            root = os.path.join(xdg, "kicad")
    return Path(root) / "phicad" / _LAST_BOARD_FILE


# --------------------------------------------------------------------------- singleton
#
# One board must have at most ONE server. Without this, every launch bound a fresh port, so
# repeatedly opening the panel (which is what an agent session does, once per generation)
# left a pile of stale servers each showing a frozen snapshot of the same board -- three
# processes on three ports was reproducible in seconds. The user then has to guess which tab is
# current, and every dead one still holds a live token.
#
# The record is a small JSON file per board, keyed by the board's resolved path, holding the
# url/port/pid. It is a HINT, never an authority: a recorded server is only reused after it
# answers an authenticated request, so a crashed process, a recycled pid, or a port taken over by
# something else all fall through to a fresh bind instead of handing back a dead URL.

_SERVERS_DIR = "web-view-servers"


def _server_record_path(board: Path) -> Path:
    """One record per board, named by a hash of its path so any board name is filesystem-safe."""
    import hashlib

    digest = hashlib.sha256(str(board).encode("utf-8")).hexdigest()[:16]
    return _last_board_path().parent / _SERVERS_DIR / f"{digest}.json"


def _record_server(board: Path, url: str, port: int) -> None:
    """Best-effort note that this process serves this board."""
    try:
        path = _server_record_path(board)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "port": port, "pid": os.getpid(),
                                    "board": str(board)}), encoding="utf-8")
    except OSError:
        pass                    # a launch must never fail because the hint could not be saved


def _forget_server(board: Path) -> None:
    try:
        _server_record_path(board).unlink()
    except OSError:
        pass


def find_live_server(board: Path, timeout: float = 2.0) -> Optional[dict]:
    """Return a recorded server for this board that is actually answering, else None.

    Liveness is proved by an authenticated request, not by the record existing and not by the
    port being open: a recycled pid or an unrelated process on the port must not be mistaken for
    our server. A stale record is deleted so it cannot mislead the next launch either.
    """
    import urllib.error
    import urllib.request

    path = _server_record_path(board)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    url = record.get("url") or ""
    if not url:
        _forget_server(board)
        return None

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
        # Confirm it is a PhiCad Agent View for THIS board, not merely something on the port.
        if response.status == 200 and "PhiCad Agent View" in body:
            return record
    except (urllib.error.URLError, OSError, ValueError):
        pass

    _forget_server(board)
    return None


def remember_board(board: Path) -> None:
    """Best-effort: record the board so a later no-argument launch can reuse it."""
    try:
        path = _last_board_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(board), encoding="utf-8")
    except OSError:
        pass                    # a launch must never fail because a hint could not be saved


def _recall_board() -> Optional[Path]:
    try:
        raw = _last_board_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    candidate = Path(raw)
    return candidate if raw and candidate.is_file() else None


def _boards_under(start: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Boards at or below `start`, skipping VCS/build/sample trees.

    Bounded on purpose: an unbounded walk of a KiCad checkout finds hundreds of fixture
    boards under `qa/data` and takes seconds, and neither is useful here.
    """
    found: list[Path] = []
    start = start.resolve()
    for current, dirs, files in os.walk(start):
        depth = len(Path(current).relative_to(start).parts)
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        found.extend(Path(current) / name for name in files if name.endswith(".kicad_pcb"))
        if len(found) > 24:                 # enough to know it is ambiguous
            break
    return sorted(found)


def discover_board(start: Optional[Path] = None) -> tuple[Path, str]:
    """Find the board to open when none was named.  Returns (board, how_it_was_chosen).

    Order is most-specific-first, and an ambiguous directory is an ERROR rather than a
    guess: opening the wrong board looks like a broken panel, and the user cannot tell
    from the render which project they got. The one exception is the remembered board,
    which is only consulted when the tree gives no single answer -- and the panel header
    always names the board it opened, so the choice is visible either way.
    """
    start = (start or Path.cwd()).resolve()

    here = sorted(start.glob("*.kicad_pcb"))
    if len(here) == 1:
        return here[0], "cwd"
    if len(here) > 1:
        names = ", ".join(p.name for p in here[:6])
        raise ViewError(f"{len(here)} boards in {start}: {names} -- name the one you want")

    nested = _boards_under(start)
    if len(nested) == 1:
        return nested[0], "search"

    # Ambiguity inside THIS tree outranks the remembered board. Falling back here would
    # silently open a board from a different project while the one you are looking at has
    # candidates of its own -- the panel would look like it ignored you.
    if len(nested) > 1:
        shown = ", ".join(str(p.relative_to(start)) for p in nested[:6])
        more = " ..." if len(nested) > 6 else ""
        raise ViewError(f"several boards under {start}: {shown}{more} -- name the one "
                        f"you want")

    # Nothing here at all: reuse the last board rather than dead-ending.
    remembered = _recall_board()
    if remembered is not None:
        return remembered, "last-used"

    raise ViewError(f"no .kicad_pcb found in or under {start} (searched {_MAX_DEPTH} levels) "
                    f"-- open a project first, or name a board")


def resolve_board(board: Path | str) -> Path:
    """Map anything nameable to the one board path everything downstream is keyed off.

    Extracted from `serve` because the singleton check has to agree with it exactly: the record
    is keyed by the resolved board, so `foo.kicad_pro`, `foo.kicad_sch`, `foo.kicad_pcb` and the
    containing directory must all resolve to the same key or the same board gets two servers.
    """
    resolved = Path(board).expanduser().resolve()
    if resolved.is_dir():
        found = sorted(resolved.glob("*.kicad_pcb"))
        if not found:
            raise ViewError(f"no .kicad_pcb found in {resolved}")
        resolved = found[0]
    elif resolved.suffix in (".kicad_sch", ".kicad_pro"):
        # The plugin is launchable from the schematic editor and the project manager, so the
        # path handed over can name the schematic or the project. Everything downstream
        # (board_views.Paths, the PCB and 3D renders, the IPC digest) is keyed off the board
        # and derives the schematic from it -- given a .kicad_sch we would try to load a
        # schematic as a board and fail with "Failed to load board" on two of the six views.
        sibling = resolved.with_suffix(".kicad_pcb")
        if sibling.is_file():
            resolved = sibling
        else:
            found = sorted(resolved.parent.glob("*.kicad_pcb"))
            if not found:
                raise ViewError(f"no .kicad_pcb beside {resolved.name} in {resolved.parent}")
            resolved = found[0]
    if not resolved.is_file():
        raise ViewError(f"board not found: {resolved}")
    return resolved


def serve(board: Path | str, port: int = DEFAULT_PORT,
          token: Optional[str] = None) -> tuple[ThreadingHTTPServer, str]:
    """Bind a loopback server for one board.  Returns (server, url)."""
    resolved = resolve_board(board)

    session = Session(board=resolved, token=token or secrets.token_urlsafe(24))

    class Bound(Handler):
        pass

    Bound.session = session
    server = ThreadingHTTPServer(("127.0.0.1", port), Bound)
    host, bound_port = server.server_address[:2]
    url = f"http://127.0.0.1:{bound_port}/?token={session.token}"
    return server, url


# --------------------------------------------------------------------------- front end

_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhiCad Agent View — __BOARD__</title>
<script>
// Override model-viewer's optional decoder defaults before importing it. KiCad GLBs do not
// use these codecs, but a future compressed asset must fail locally rather than contact a CDN.
window.ModelViewerElement = {
  dracoDecoderLocation: "/static/model-viewer-decoders/",
  ktx2TranscoderLocation: "/static/model-viewer-decoders/",
  lottieLoaderLocation: "/static/model-viewer-decoders/LottieLoader.js"
};
</script>
<script type="module" src="/static/model-viewer.min.js?token=__TOKEN__"></script>
<style>
  :root {
    --bg:#151718; --panel:#1d2021; --line:#2e3234; --ink:#e8eaed; --dim:#9aa0a6;
    --accent:#2dd4bf; --good:#5cc389; --bad:#e5716b;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--font);
       height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--panel);
         border-bottom:1px solid var(--line);flex-wrap:wrap;flex:0 0 auto}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:0 0 auto}
  .dot.on{background:var(--good)} .dot.off{background:var(--bad)}
  .pill{font-size:12px;color:var(--dim);white-space:nowrap}
  .pill b{color:var(--ink);font-weight:600}
  .grow{flex:1 1 auto}
  button{background:#2a2e30;color:var(--ink);border:1px solid var(--line);border-radius:6px;
         padding:5px 11px;font:13px var(--font);cursor:pointer}
  button:hover:not(:disabled){background:#343a3c}
  button:disabled{opacity:.45;cursor:default}
  button.sel{background:var(--accent);border-color:var(--accent);color:#042f2e;font-weight:600}
  main{flex:1 1 auto;display:flex;min-height:0}
  #views{flex:1 1 60%;display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
  .tabs{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line);flex:0 0 auto;
        align-items:center;flex-wrap:wrap}
  #stage{flex:1 1 auto;overflow:auto;display:flex;align-items:center;justify-content:center;
         background:#0d0f10;padding:12px;min-height:0}
  #stage img{max-width:100%;max-height:100%;object-fit:contain}
  #stage model-viewer{width:100%;height:100%;min-height:320px;background:#0d0f10}
  #stage pre{margin:0;padding:12px;color:var(--dim);font:12px/1.6 ui-monospace,Menlo,monospace;
             white-space:pre-wrap;align-self:flex-start;width:100%}
  #chat{flex:1 1 40%;display:flex;flex-direction:column;min-width:320px;min-height:0}
  #chat h2{margin:0;padding:9px 12px;font-size:12px;font-weight:600;color:var(--dim);
           text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--line)}
  #log{flex:1 1 auto;overflow-y:auto;padding:10px 12px;min-height:0}
  .msg{margin-bottom:12px}
  .who{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);
       margin-bottom:3px;font-weight:600}
  .msg.user .who{color:var(--accent)}
  .body{white-space:pre-wrap;word-wrap:break-word;background:var(--panel);
        border:1px solid var(--line);border-radius:8px;padding:8px 10px}
  .msg.user .body{background:#132826;border-color:#1f4a45}
  .note{color:var(--dim);font-size:13px;padding:10px 12px}
  .note code{background:var(--panel);padding:2px 6px;border-radius:4px;color:var(--ink)}
  form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);flex:0 0 auto}
  textarea{flex:1 1 auto;resize:none;background:#0f1112;color:var(--ink);
           border:1px solid var(--line);border-radius:8px;padding:8px 10px;
           font:13px var(--font);min-height:60px}
  #err{display:none;padding:8px 12px;background:#3b1f1d;color:#ffbfbb;font-size:13px;
       border-top:1px solid #5a2b28;flex:0 0 auto}
</style></head><body>

<header>
  <span class="dot" id="d-ipc"></span><span class="pill" id="s-board" title="__BOARD_PATH__"><b>__BOARD__</b></span>
  <span class="pill" id="s-ipc">checking…</span>
  <span class="pill">·</span><span class="pill" id="s-bridge">checking…</span>
  <span class="pill">·</span><span class="pill" id="s-git">checking…</span>
  <span class="grow"></span>
  <button id="b-snap" title="Commit the project's current state">Snapshot</button>
  <button id="b-refresh">Refresh</button>
</header>

<main>
  <section id="views">
    <div class="tabs" id="tabs">
      <button data-kind="pcb" data-variant="both" class="sel">PCB</button>
      <button data-kind="pcb" data-variant="front">Front</button>
      <button data-kind="pcb" data-variant="back">Back</button>
      <button data-kind="schematic" data-variant="">Schematic</button>
      <button data-kind="3d" data-variant="top">3D</button>
      <button data-kind="3d" data-variant="interactive">3D interactive</button>
      <button data-kind="context" data-variant="">Context</button>
    </div>
    <div id="stage"><span class="note">Loading view…</span></div>
  </section>

  <section id="chat">
    <h2>Agent session</h2>
    <div id="log"><div class="note">Loading conversation…</div></div>
    <div id="err"></div>
    <form id="f">
      <textarea id="p" placeholder="Ask about or change this board. Enter sends."></textarea>
      <button type="submit" id="b-send">Send</button>
    </form>
  </section>
</main>

<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
let busy = false, view = {kind:"pcb", variant:"both"}, lastCount = -1;

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: {"X-PhiCad-Token": TOKEN, "Content-Type": "application/json"}
  }, opts || {}));
  const ctype = res.headers.get("Content-Type") || "";
  if (!ctype.includes("json")) return res;
  const data = await res.json();
  if (!data.ok && data.error) throw new Error(data.error);
  return data;
}
function showErr(msg) {
  const el = $("err");
  if (!msg) { el.style.display = "none"; return; }
  el.textContent = msg; el.style.display = "block";
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("s-board").innerHTML = "<b>" + s.board + "</b>";
    $("s-ipc").textContent = s.ipc.display;
    $("d-ipc").className = "dot " + (s.ipc.connected ? "on" : "off");
    const b = s.bridge;
    $("s-bridge").textContent = b.connected
      ? b.display + (b.model && b.model.connected_as
          ? " (connected as " + b.model.connected_as.id + ")" : "")
      : b.display;
    $("s-git").textContent = s.git.display;
  } catch (e) { showErr("Status failed: " + e.message); }
}

async function refreshChat() {
  try {
    const c = await api("/api/chat");
    const log = $("log");
    if (!c.connected) {
      log.innerHTML = '<div class="note">This board is not attached to a shared phi '
        + 'session, so chat is not available here.<br><br>The views, context and '
        + 'snapshots still work; chat from the desktop Agent View panel.</div>';
      lastCount = -1; return;
    }
    if (c.messages.length === lastCount) return;   // avoid clobbering scroll on every poll
    lastCount = c.messages.length;
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    log.innerHTML = c.messages.map(m =>
      '<div class="msg ' + m.role + '"><div class="who">'
      + (m.role === "user" ? "You" : "Agent")
      + '</div><div class="body"></div></div>').join("")
      || '<div class="note">The shared session has no messages yet.</div>';
    // Assign text through .textContent so board text can never inject markup.
    log.querySelectorAll(".msg .body").forEach((el, i) => el.textContent = c.messages[i].text);
    if (atBottom) log.scrollTop = log.scrollHeight;
  } catch (e) { showErr("Chat failed: " + e.message); }
}

async function loadView() {
  const stage = $("stage");
  stage.innerHTML = '<span class="note">Rendering…</span>';
  try {
    if (view.kind === "context") {
      const c = await api("/api/context");
      const pre = document.createElement("pre");
      pre.textContent = c.ipc.digest
        || (c.ipc.display + "\n\n" + (c.ipc.detail || "")
            + "\n\nOpen this board in PhiCad and start the API server to see live context:\n"
            + "  phicad api-server <board>");
      stage.innerHTML = ""; stage.appendChild(pre);
      return;
    }
    if (view.kind === "3d" && view.variant === "interactive") {
      const exported = await api("/api/model", {method: "POST", body: "{}"});
      const model = document.createElement("model-viewer");
      model.setAttribute("camera-controls", "");
      model.setAttribute("auto-rotate", "");
      model.setAttribute("shadow-intensity", "1");
      model.setAttribute("alt", "Interactive 3D model of the board");
      model.src = "/api/model?token=" + encodeURIComponent(TOKEN)
                + "&digest=" + encodeURIComponent(exported.digest);
      model.addEventListener("error", () => {
        stage.innerHTML = "";
        const n = document.createElement("span"); n.className = "note";
        n.textContent = "The GLB was exported, but the interactive viewer could not load it.";
        stage.appendChild(n);
      });
      stage.innerHTML = ""; stage.appendChild(model);
      return;
    }
    const url = "/api/render?kind=" + view.kind + "&variant=" + view.variant
              + "&token=" + encodeURIComponent(TOKEN) + "&t=" + Date.now();
    const probe = await fetch(url, {headers: {"X-PhiCad-Token": TOKEN}});
    if (!probe.ok) {
      let msg = "render failed";
      try { msg = (await probe.json()).error || msg; } catch (e) {}
      stage.innerHTML = "";
      const n = document.createElement("span"); n.className = "note"; n.textContent = msg;
      stage.appendChild(n); return;
    }
    const blob = await probe.blob();
    const img = document.createElement("img");
    img.src = URL.createObjectURL(blob);
    stage.innerHTML = ""; stage.appendChild(img);
  } catch (e) {
    stage.innerHTML = "";
    const n = document.createElement("span"); n.className = "note";
    n.textContent = "Render failed: " + e.message; stage.appendChild(n);
  }
}

$("tabs").addEventListener("click", ev => {
  const btn = ev.target.closest("button"); if (!btn) return;
  [...$("tabs").children].forEach(b => b.classList.remove("sel"));
  btn.classList.add("sel");
  view = {kind: btn.dataset.kind, variant: btn.dataset.variant || ""};
  loadView();
});

$("f").addEventListener("submit", async ev => {
  ev.preventDefault();
  const box = $("p"), text = box.value.trim();
  if (!text || busy) return;
  busy = true; showErr("");
  $("b-send").disabled = true; $("b-send").textContent = "Working…";
  box.value = "";
  try {
    await api("/api/ask", {method: "POST", body: JSON.stringify({prompt: text})});
    lastCount = -1;
    await refreshChat(); await refreshStatus(); await loadView();
  } catch (e) {
    showErr(e.message); box.value = text;
  } finally {
    busy = false; $("b-send").disabled = false; $("b-send").textContent = "Send";
  }
});

$("p").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("f").requestSubmit(); }
});

$("b-refresh").addEventListener("click", () => {
  lastCount = -1; refreshStatus(); refreshChat(); loadView();
});

$("b-snap").addEventListener("click", async () => {
  showErr("");
  try {
    const r = await api("/api/snapshot", {method: "POST", body: JSON.stringify({})});
    $("s-git").textContent = r.git.display
      + (r.committed ? "" : " (nothing to commit)");
  } catch (e) { showErr(e.message); }
});

refreshStatus(); refreshChat(); loadView();
// Poll for turns typed in the shared session, so both windows stay in step.
setInterval(() => { if (!busy) { refreshChat(); refreshStatus(); } }, 4000);
</script></body></html>
"""


# --------------------------------------------------------------------------- CLI


def _open_in_browser(url: str) -> dict:
    """Show the page in the system browser."""
    import webbrowser

    return {"ok": bool(webbrowser.open(url)), "browser": "system"}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="phicad web-view",
        description="Serve Agent View as a local web app for a browser.")
    parser.add_argument("board", nargs="?", default=None,
                        help="board (.kicad_pcb), schematic (.kicad_sch), project "
                             "(.kicad_pro) or project directory. Omit to discover one "
                             "from the current directory")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port to bind (default: an unused one)")
    parser.add_argument("--open", action="store_true",
                        help="open the page in the system browser")
    parser.add_argument("--print-url", action="store_true",
                        help="print the URL as JSON and keep serving")
    parser.add_argument("--once", action="store_true",
                        help="bind, report, and exit without serving (for checks)")
    parser.add_argument("--new", action="store_true",
                        help="force a second server for this board instead of reusing the "
                             "running one (rarely wanted; the default is one per board)")
    args = parser.parse_args(argv)

    # Resolve before serving so a discovery failure reports the reason, not a bind error.
    try:
        if args.board:
            target, source = Path(args.board).expanduser(), "argument"
        else:
            target, source = discover_board()

        # One board, one server. An agent session may reopen the panel after every
        # generation, so without this the ports pile up and the user cannot tell which tab is
        # live. Reuse is conditional on the recorded server actually answering.
        resolved_target = resolve_board(target)
        if not args.new and args.port == DEFAULT_PORT:
            existing = find_live_server(resolved_target)
            if existing:
                reused = {"ok": True, "url": existing["url"], "board": str(resolved_target),
                          "board_source": source, "port": existing["port"],
                          "reused": True, "pid": existing.get("pid")}
                if args.open:
                    reused["opened"] = _open_in_browser(existing["url"])
                if args.print_url or args.open or args.once:
                    print(json.dumps(reused, indent=2), flush=True)
                else:
                    print(f"Agent View already serving this board at {existing['url']}\n"
                          f"(reusing pid {existing.get('pid')}; --new forces another)",
                          flush=True)
                return 0

        server, url = serve(target, port=args.port)
    except (ViewError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2

    board = resolve_board(target)
    remember_board(board)
    result: dict[str, Any] = {"ok": True, "url": url, "reused": False,
                              "board": str(board),
                              "board_source": source,
                              "port": server.server_address[1]}
    if args.once:
        server.server_close()
        print(json.dumps(result, indent=2))
        return 0

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Recorded only once actually serving, so a record never points at a server that never ran.
    _record_server(board, url, server.server_address[1])

    if args.open:
        result["opened"] = _open_in_browser(url)
    if args.print_url or args.open:
        print(json.dumps(result, indent=2), flush=True)
    else:
        print(f"Agent View serving at {url}\n"
              "Ctrl-C to stop.", flush=True)

    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        _forget_server(board)   # so the next launch binds fresh rather than probing a dead port
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
