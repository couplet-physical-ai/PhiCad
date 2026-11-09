#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The web view's token gate, including the shell page.

This exists because of a real leak: `GET /` was served unauthenticated on the reasoning that
"the page itself is harmless without the token". It was not harmless -- the page **carries**
the token (it embeds it as `const TOKEN` so the front end can call the guarded routes), so an
unauthenticated fetch of `/` handed the credential over, and every "guarded" route below was
then reachable with it. Measured on the live server before the fix: an unauthenticated
`GET /` returned a byte-identical 12,353-byte page including the live token, which then read
`/api/status` and pulled an 80 KB board render.

The invariant these tests pin is therefore narrower than "routes are guarded":

    a caller who cannot present the token learns NOTHING -- not the token, not the board name,
    not the board path, not whether a board exists at all.

A test that only checked `/api/*` would have passed throughout the vulnerability, which is why
the shell page gets its own cases here.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import web_view                                             # noqa: E402

BOARD_NAME = "authprobe"


def _minimal_board(tmp: Path) -> Path:
    """The smallest board kicad-cli will accept; no renders are taken in these tests."""
    path = tmp / f"{BOARD_NAME}.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "phicad-test")\n'
        "  (general (thickness 1.6))\n"
        "  (paper \"A4\")\n"
        "  (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal))\n"
        ")\n"
    )
    return path


class TokenGateTests(unittest.TestCase):
    """Every response to an unauthenticated caller, checked for disclosure."""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory()
        board = _minimal_board(Path(cls._tmp.name))
        cls.server, cls.url = web_view.serve(board, port=0)
        cls.token = cls.url.split("token=", 1)[1]
        cls.base = cls.url.split("/?", 1)[0]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _get(self, path: str, timeout: int = 30):
        """Return (status, body) for a GET, treating an HTTP error as a normal outcome."""
        try:
            resp = urllib.request.urlopen(self.base + path, timeout=timeout)
            return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    # -- the shell page ---------------------------------------------------

    def test_the_page_is_refused_without_a_token(self):
        status, body = self._get("/")
        self.assertEqual(status, 403)
        self.assertNotIn(self.token, body)

    def test_the_page_is_refused_with_a_wrong_token(self):
        status, body = self._get("/?token=bogus")
        self.assertEqual(status, 403)
        self.assertNotIn(self.token, body)

    def test_the_refusal_does_not_name_the_board(self):
        """The board name and path are as sensitive as the token: they say what is open."""
        _, body = self._get("/")
        self.assertNotIn(BOARD_NAME, body)

    def test_the_authorized_page_still_carries_the_token(self):
        """The fix must not break the front end, which reads the token out of the page."""
        status, body = self._get(f"/?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(self.token, body)
        self.assertIn(BOARD_NAME, body)

    def test_a_token_in_the_header_authorizes_the_page(self):
        """The gate accepts the header form too, so a fetch() front end is not locked out."""
        req = urllib.request.Request(self.base + "/",
                                     headers={"X-PhiCad-Token": self.token})
        resp = urllib.request.urlopen(req, timeout=30)
        self.assertEqual(resp.status, 200)
        self.assertIn(self.token, resp.read().decode("utf-8", "replace"))

    # -- the data routes --------------------------------------------------

    def test_data_routes_are_refused_without_a_token(self):
        for route in ("/api/status", "/api/chat", "/api/context", "/api/render?kind=pcb"):
            with self.subTest(route=route):
                status, _ = self._get(route)
                self.assertEqual(status, 403)

    def test_data_routes_answer_a_legitimate_caller(self):
        """The guard must not have been tightened into uselessness."""
        for route in (f"/api/status?ipc=0&token={self.token}",
                      f"/api/chat?token={self.token}",
                      f"/api/context?token={self.token}"):
            with self.subTest(route=route):
                status, _ = self._get(route, timeout=90)
                self.assertEqual(status, 200)

    def test_no_response_to_an_unauthenticated_caller_contains_the_token(self):
        """The whole-surface version of the claim, so a new unguarded route cannot regress it."""
        for route in ("/", "/?token=bogus", "/api/status", "/api/chat", "/nonexistent"):
            with self.subTest(route=route):
                _, body = self._get(route)
                self.assertNotIn(self.token, body)


class SingletonTests(unittest.TestCase):
    """One board, one server.

    An agent session may reopen the panel after every generation. Before this, each launch
    bound a fresh port, so the same board ended up served by several processes at once (three in
    seconds, measured) -- the user cannot tell which tab is current, and every abandoned one still
    holds a live token. These tests pin reuse AND the cases where reuse must NOT happen, because
    handing back a dead URL is worse than binding a new port.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.board = _minimal_board(self.root)
        # Redirect the record + last-board state into the temp dir so the real config is untouched.
        self._prev = os.environ.get("PHICAD_WEB_VIEW_LAST_BOARD")
        os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = str(self.root / "state" / "last-board.txt")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("PHICAD_WEB_VIEW_LAST_BOARD", None)
        else:
            os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = self._prev
        self._tmp.cleanup()

    def test_no_record_means_no_reuse(self):
        self.assertIsNone(web_view.find_live_server(self.board))

    def test_a_live_server_is_found_and_reused(self):
        server, url = web_view.serve(self.board, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            web_view._record_server(self.board, url, server.server_address[1])
            found = web_view.find_live_server(self.board)
            self.assertIsNotNone(found)
            self.assertEqual(found["url"], url)
        finally:
            server.shutdown()
            server.server_close()

    def test_a_dead_server_is_not_reused_and_its_record_is_dropped(self):
        """The record is a hint, never an authority -- a dead port must fall through."""
        server, url = web_view.serve(self.board, port=0)
        port = server.server_address[1]
        server.server_close()                       # nothing is listening now
        web_view._record_server(self.board, url, port)
        self.assertIsNone(web_view.find_live_server(self.board, timeout=1.0))
        self.assertFalse(web_view._server_record_path(self.board).exists(),
                         "a stale record must be deleted, not left to mislead the next launch")

    def test_an_unrelated_server_on_the_port_is_not_mistaken_for_ours(self):
        """A recycled port serving something else must not be adopted as an Agent View."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Impostor(BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                body = b"<html>not the agent view</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):              # noqa: A003
                pass

        impostor = ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
        thread = threading.Thread(target=impostor.serve_forever, daemon=True)
        thread.start()
        try:
            port = impostor.server_address[1]
            web_view._record_server(self.board, f"http://127.0.0.1:{port}/?token=x", port)
            self.assertIsNone(web_view.find_live_server(self.board, timeout=2.0))
        finally:
            impostor.shutdown()
            impostor.server_close()

    def test_every_name_for_one_board_shares_one_record(self):
        """.kicad_pro / .kicad_sch / the directory must key to the same server, or reuse fails."""
        pro = self.board.with_suffix(".kicad_pro")
        pro.write_text("{}")
        # Compare against the RESOLVED board: on macOS the temp dir is /var/... which resolves
        # to /private/var/..., and the record is keyed by the resolved path. Using the
        # unresolved one here compared two different keys and failed -- correctly.
        key = web_view._server_record_path(web_view.resolve_board(self.board))
        for alias in (pro, self.board.parent, self.board):
            with self.subTest(alias=alias.name):
                self.assertEqual(web_view._server_record_path(web_view.resolve_board(alias)), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
