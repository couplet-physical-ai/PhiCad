#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""The BOM/Datasheets tab: what is on the board, and the paper that documents it.

WHY THIS READS THE FILE INSTEAD OF THE IPC SESSION
--------------------------------------------------
Every other pane in this window talks to the running editor over the API.  This one
does not, on purpose.  The board's own file already carries everything a BOM needs --
footprint properties hold Reference, Value, MPN, LCSC, Datasheet and Description -- and
`pylib/sexp.py` reads a 100 KB board in single-digit milliseconds.  Going over IPC for
it would put a second consumer on the session that the chat depends on, for data that is
on disk anyway.  The pane already knows the board's path (board_views.Paths), so the
whole tab stays out of the API's way.

WHAT IS AND IS NOT SHOWN
------------------------
Passives on the demo boards deliberately carry no MPN and no datasheet.  That is honest
data, not a hole to be filled: a 4k7 0603 is bought on value and tolerance, not on a
part number.  So a blank cell here means "the board does not say", and nothing in this
module ever guesses a manufacturer, a part number or a datasheet URL.

Grouped view is a BOM in the sense a purchaser means: parts with the same Value and
Footprint collapse to one line with a reference list and a quantity, and footprints the
board marks `exclude_from_bom` (mounting holes, fiducials) are left out of it -- which
is why the summary line says how many were excluded and where to see them.  "All parts"
shows every footprint on the board, excluded ones included.

THE DATASHEET FOLDER
--------------------
`<project dir>/datasheets/`, beside the board, so the files outlive this window and can
be committed with the project.  It is created when something is actually put in it (or
when Reveal is pressed), never merely by opening the tab -- a read-only view has no
business making directories in someone's project.

Nothing is downloaded on its own.  A cached file previews immediately; an uncached one
says so and waits for Fetch, because network I/O is the user's decision and a tab that
quietly reached out to two vendor sites every time it was opened would be a surprise.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

import ui_theme

TAB_LABEL = "BOM/Datasheets"

# The folder is a sibling of the board file, not a temp dir: these are project documents.
DATASHEET_DIRNAME = "datasheets"

OPEN = "/usr/bin/open"
QLMANAGE = "/usr/bin/qlmanage"

FETCH_TIMEOUT_S = 45.0
PREVIEW_TIMEOUT_S = 30.0
PDFIUM_INSTALL_HINT = (
    "No PDF preview backend is available. Install pypdfium2 with "
    "`build/venv/bin/pip install pypdfium2` and restart PhiCad."
)
# A datasheet is a few MB; anything past this is a mirror serving something else.
MAX_BYTES = 64 * 1024 * 1024
# Some vendor sites answer a bare urllib User-Agent with 403.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) PhiCad-AIView/1.0")

# Detail pane geometry.  Fixed rather than a PanedWindow sash: the table is the thing
# being read and the detail is a fixed-size inspector under it, and a sash here would
# inherit the re-pinning problem the main window's _restore_sash exists to work around.
DETAIL_H = 296
PREVIEW_W = 236


class BomError(RuntimeError):
    """The board could not be turned into a BOM.  The message is shown in the tab."""


class FetchError(RuntimeError):
    """A datasheet could not be downloaded.  The message is shown in the tab."""


class PreviewError(RuntimeError):
    """A cached datasheet could not be rendered to an image."""


# --------------------------------------------------------------------------- sexp

_SEXP = None


def _load_sexp():
    """Load the fork's own s-expression reader, by PATH rather than by name.

    It lives in `phicad/pylib/`, which `phicad ai-view-install` does not copy into the
    plugin directory -- the installed plugin is a flat copy of this one folder.  So the
    checkout is located the same way /design finds `phicad/synth`: through the
    `repo-path` marker the installer writes.

    Loaded from its file with a private module name, and NOT with `import sexp` after a
    sys.path append.  Measured: `sexp` is a plausible enough name that a stray one on
    the path wins the bare import and the tab dies on `module 'sexp' has no attribute
    'parse_file'` -- and the plugin's own sys.path already carries the launcher's
    directory and the cwd KiCad chose, neither of which this module controls.  Loading
    by path also means nothing is added to sys.path for the whole GUI process.
    """
    global _SEXP
    if _SEXP is not None:
        return _SEXP

    import importlib.util                                  # noqa: PLC0415
    import design_commands                                 # noqa: PLC0415

    root = design_commands.repo_root()
    if root is None:
        raise BomError(
            "This plugin copy cannot find its PhiCad checkout, so it cannot load the "
            "s-expression reader (phicad/pylib/sexp.py) that parses the board.\n\n"
            "Reinstall with `phicad ai-view-install` from a checkout, or set "
            "PHICAD_REPO to one.")

    path = root / "phicad" / "pylib" / "sexp.py"
    if not path.is_file():
        raise BomError(f"the board parser is missing from the checkout: {path}")
    spec = importlib.util.spec_from_file_location("_phicad_sexp", str(path))
    if spec is None or spec.loader is None:
        raise BomError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                                # noqa: BLE001
        raise BomError(f"could not load {path}: {exc}") from exc
    if not hasattr(module, "parse_file"):
        raise BomError(f"{path} is not the expected s-expression reader")
    _SEXP = module
    return module


# --------------------------------------------------------------------------- model

@dataclass
class Part:
    """One footprint on the board, as its own properties describe it."""

    reference: str
    value: str
    footprint: str                 # the library id, e.g. Capacitor_SMD:C_0603_1608Metric
    mpn: str = ""
    lcsc: str = ""
    datasheet: str = ""
    description: str = ""
    layer: str = ""
    attrs: frozenset = frozenset()

    @property
    def dnp(self) -> bool:
        return "dnp" in self.attrs

    @property
    def in_bom(self) -> bool:
        return "exclude_from_bom" not in self.attrs

    def flags(self) -> str:
        out = []
        if self.dnp:
            out.append("DNP")
        if not self.in_bom:
            out.append("no BOM")
        return ", ".join(out)


@dataclass
class BomLine:
    """One purchasable line: the parts that are the same part."""

    value: str
    footprint: str
    mpn: str = ""
    lcsc: str = ""
    datasheet: str = ""
    description: str = ""
    dnp: bool = False
    parts: List[Part] = field(default_factory=list)

    @property
    def qty(self) -> int:
        return len(self.parts)

    @property
    def references(self) -> str:
        return ", ".join(p.reference for p in self.parts)

    def flags(self) -> str:
        return "DNP" if self.dnp else ""


def _ref_key(reference: str) -> Tuple[str, int, str]:
    """Sort R2 before R10.  Plain string order puts R10 first, which reads as a bug."""
    match = re.match(r"^([^0-9]*)(\d*)", reference or "")
    prefix = match.group(1) if match else reference
    number = match.group(2) if match else ""
    return (prefix, int(number) if number else 0, reference or "")


def read_board(board_path: str) -> List[Part]:
    """Parse a .kicad_pcb into parts.  Raises BomError with something actionable."""
    sexp = _load_sexp()
    if not os.path.isfile(board_path):
        raise BomError(f"board file not found: {board_path}")
    try:
        doc = sexp.parse_file(board_path)
    except (OSError, ValueError) as exc:
        raise BomError(
            f"could not read {os.path.basename(board_path)}: {exc}\n\n"
            "The board file is unreadable or not an s-expression document. Nothing is "
            "shown rather than a partial parts list, because half a BOM is worse than "
            "none.") from exc

    parts: List[Part] = []
    for fp in sexp.children(doc, "footprint"):
        args = sexp.atoms(fp)
        parts.append(Part(
            reference=sexp.fp_property(fp, "Reference") or "",
            value=sexp.fp_property(fp, "Value") or "",
            footprint=str(args[0]) if args else "",
            mpn=sexp.fp_property(fp, "MPN") or "",
            lcsc=sexp.fp_property(fp, "LCSC") or "",
            datasheet=(sexp.fp_property(fp, "Datasheet") or "").strip(),
            description=sexp.fp_property(fp, "Description") or "",
            layer=sexp.scalar(fp, "layer") or "",
            attrs=frozenset(sexp.fp_attrs(fp)),
        ))
    parts.sort(key=lambda p: _ref_key(p.reference))
    return parts


def group_parts(parts: List[Part]) -> List[BomLine]:
    """Collapse identical parts into BOM lines, skipping the ones the board excludes.

    The key is Value + Footprint, plus MPN and DNP.  Value+Footprint is the BOM rule;
    MPN joins it because two parts that share a value and a package but were ordered
    against different part numbers are two line items to whoever buys them, and DNP
    joins it because a fitted and an unfitted part are not the same quantity of anything.
    """
    lines: Dict[tuple, BomLine] = {}
    order: List[tuple] = []
    for part in parts:
        if not part.in_bom:
            continue
        key = (part.value, part.footprint, part.mpn, part.dnp)
        line = lines.get(key)
        if line is None:
            line = BomLine(value=part.value, footprint=part.footprint, mpn=part.mpn,
                           lcsc=part.lcsc, datasheet=part.datasheet,
                           description=part.description, dnp=part.dnp)
            lines[key] = line
            order.append(key)
        # First non-empty wins for the shared fields: the grouping key does not include
        # them, so one member of a group carrying a datasheet must not be silently
        # dropped just because it was not the first one seen.
        line.lcsc = line.lcsc or part.lcsc
        line.datasheet = line.datasheet or part.datasheet
        line.description = line.description or part.description
        line.parts.append(part)
    return [lines[key] for key in order]


# ---------------------------------------------------------------------- datasheets

def datasheet_dir(project_dir: str) -> str:
    return os.path.join(project_dir, DATASHEET_DIRNAME)


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-.")
    return cleaned[:60] or "datasheet"


def cache_name(url: str, label: str = "") -> str:
    """The file a URL is cached as.

    `<label or url basename>-<8 hex of the url>.pdf`.  The readable half is there so the
    folder can be browsed by a human; the hash is there because the filename has to be
    derivable from the URL alone and two vendors happily publish different documents
    under the same basename (`datasheet.pdf`).
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    base = label or os.path.basename(urllib.parse.urlparse(url).path) or "datasheet"
    base = re.sub(r"\.pdf$", "", base, flags=re.IGNORECASE)
    return f"{_slug(base)}-{digest}.pdf"


def cached_path(project_dir: str, url: str, label: str = "") -> Optional[str]:
    """Where a URL's file is, if it has been fetched.  None means "not downloaded"."""
    # An empty project_dir would make this a RELATIVE probe against whatever directory
    # KiCad launched the plugin in, which is not a place datasheets are ever kept.
    if not url or not project_dir:
        return None
    path = os.path.join(datasheet_dir(project_dir), cache_name(url, label))
    return path if os.path.isfile(path) else None


def _opener() -> urllib.request.OpenerDirector:
    """A urllib opener with proxies switched off.

    chat_backend._child_env strips HTTPS_PROXY and its siblings from every subprocess
    because on managed machines they point at an LLM gateway that is not an HTTP proxy
    at all -- anything that honours them fails with a malformed response.  urllib reads
    the same variables, and popping them from os.environ here would change them for the
    whole GUI process, so the proxy is disabled at the handler instead.  An empty
    ProxyHandler mapping is the in-process equivalent of that env strip.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_datasheet(url: str, dest: str) -> str:
    """Download one datasheet.  Only ever called from a user action."""
    if not url.lower().startswith(("http://", "https://")):
        raise FetchError(f"not an http(s) URL, so there is nothing to fetch: {url}")
    host = urllib.parse.urlparse(url).netloc or url

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    try:
        with _opener().open(request, timeout=FETCH_TIMEOUT_S) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{host} answered {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"could not reach {host}: {exc.reason}") from exc
    except OSError as exc:
        raise FetchError(f"could not reach {host}: {exc}") from exc

    if len(body) > MAX_BYTES:
        raise FetchError(f"{host} sent more than {MAX_BYTES // (1024 * 1024)} MB; "
                         "that is not a datasheet, so nothing was saved")
    if not body.startswith(b"%PDF"):
        # A dead vendor link is usually a 200 carrying a consent wall or a search page,
        # not a 404.  Writing that to a .pdf would leave a file in the folder that reads
        # as cached and fails in every viewer afterwards, so refuse and say what came
        # back instead.
        head = body[:120].decode("utf-8", "replace").strip().replace("\n", " ")
        raise FetchError(
            f"{host} did not send a PDF (Content-Type: {ctype or 'unset'}, "
            f"{len(body)} bytes). Nothing was saved.\n\nIt starts: {head}")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Written aside and renamed: a download interrupted halfway must not leave a
    # truncated file that every later check reports as cached.
    temporary = dest + ".part"
    with open(temporary, "wb") as handle:
        handle.write(body)
    os.replace(temporary, dest)
    return dest


def _load_pdfium():
    """Import pypdfium2 only when a PDF operation actually needs it."""
    try:
        import pypdfium2 as pdfium                         # noqa: PLC0415
    except (ImportError, OSError):
        return None
    return pdfium


def preview_backend() -> Optional[str]:
    """Return the available preview backend, preferring PDFium on Linux.

    The import is deliberately behind this function: AI View remains importable with
    only the standard library and tkinter, and macOS does not need pypdfium2 when
    Quick Look is present.
    """
    qlmanage = shutil.which(QLMANAGE)
    if sys.platform.startswith("linux"):
        if _load_pdfium() is not None:
            return "pypdfium2"
        return "qlmanage" if qlmanage else None
    if qlmanage:
        return "qlmanage"
    if _load_pdfium() is not None:
        return "pypdfium2"
    return None


def _close_pdfium(value) -> None:
    """Close a pypdfium2 object when this version exposes an explicit close method."""
    close = getattr(value, "close", None)
    if close is not None:
        close()


def _render_pdfium(pdf: str, out: str, size: int) -> None:
    """Render the first PDF page through pypdfium2 and Pillow."""
    pdfium = _load_pdfium()
    if pdfium is None:
        raise PreviewError(PDFIUM_INSTALL_HINT)
    document = page = bitmap = None
    try:
        document = pdfium.PdfDocument(pdf)
        if len(document) == 0:
            raise PreviewError(f"{os.path.basename(pdf)} has no pages")
        page = document[0]
        width = float(page.get_width())
        height = float(page.get_height())
        if size <= 0 or max(width, height) <= 0:
            raise PreviewError(f"cannot render {os.path.basename(pdf)} at size {size}")
        bitmap = page.render(scale=size / max(width, height))
        try:
            image = bitmap.to_pil()
        except ImportError as exc:
            raise PreviewError(
                "pypdfium2 preview needs Pillow; run `phicad venv` and retry.") from exc
        image.save(out, format="PNG")
    except PreviewError:
        raise
    except Exception as exc:                              # noqa: BLE001 - backend boundary
        raise PreviewError(f"pypdfium2 could not render {os.path.basename(pdf)}: {exc}") from exc
    finally:
        if bitmap is not None:
            _close_pdfium(bitmap)
        if page is not None:
            _close_pdfium(page)
        if document is not None:
            _close_pdfium(document)


def datasheet_text(pdf: str, max_pages: int = 3) -> str:
    """Extract text from up to ``max_pages`` of a cached PDF with pypdfium2.

    Pages are separated by a blank line.  This is intentionally an explicit failure
    when the optional dependency is absent: callers can distinguish missing tooling
    from a datasheet whose first pages contain no selectable text.
    """
    if max_pages <= 0:
        return ""
    pdfium = _load_pdfium()
    if pdfium is None:
        raise PreviewError(PDFIUM_INSTALL_HINT)

    document = None
    pages = []
    try:
        document = pdfium.PdfDocument(pdf)
        for index in range(min(max_pages, len(document))):
            page = document[index]
            textpage = None
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                if text:
                    pages.append(text.strip())
            finally:
                if textpage is not None:
                    _close_pdfium(textpage)
                _close_pdfium(page)
    except Exception as exc:                              # noqa: BLE001 - backend boundary
        raise PreviewError(
            f"pypdfium2 could not extract text from {os.path.basename(pdf)}: {exc}") from exc
    finally:
        if document is not None:
            _close_pdfium(document)
    return "\n\n".join(page for page in pages if page)


def preview_png(pdf: str, out_dir: str, size: int = 1400) -> str:
    """Render page 1 of a cached PDF to a PNG.

    Quick Look remains the macOS backend.  Linux prefers pypdfium2, whose page render
    uses Pillow to write the PNG and scales its longest edge to ``size``.  Both imports
    and backend probes happen only when a preview is requested.
    """
    out = os.path.join(out_dir, os.path.basename(pdf) + ".png")
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(pdf):
        return out
    os.makedirs(out_dir, exist_ok=True)
    backend = preview_backend()
    if backend is None:
        raise PreviewError(PDFIUM_INSTALL_HINT)
    if backend == "pypdfium2":
        _render_pdfium(pdf, out, size)
        return out

    qlmanage = shutil.which(QLMANAGE) or QLMANAGE
    try:
        subprocess.run([qlmanage, "-t", "-s", str(size), "-o", out_dir, pdf],
                       capture_output=True, timeout=PREVIEW_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PreviewError(f"qlmanage timed out after {PREVIEW_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise PreviewError(f"could not run {qlmanage}: {exc}") from exc
    if not os.path.isfile(out):
        # qlmanage exits 0 whether or not it produced anything, so the file is the only
        # evidence that it worked.
        raise PreviewError(
            f"Quick Look produced no thumbnail for {os.path.basename(pdf)}.\n\n"
            "The file is downloaded and Open will still hand it to the OS; only the "
            "inline preview is unavailable.")
    return out


def reveal(path: str) -> None:
    """Show a file or folder in Finder.  Never raises into the Tk loop."""
    argv = [OPEN, "-R", path] if os.path.isfile(path) else [OPEN, path]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hand_to_os(target: str) -> None:
    """Open a file, or a URL, with whatever the system uses for it."""
    subprocess.Popen([OPEN, target], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def human_size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


# ------------------------------------------------------------------------ the tab

#   (id, heading, width, minwidth, anchor, stretch)
# Footprint and MPN stretch; nothing else does.  Those two hold the longest cells (a
# library id and a manufacturer part number), so they are what should absorb a maximised
# agent view -- measured, a fixed 128 px MPN column truncated "AP2112K-3.3TRG1" in a
# 1423 px table that had 500 px going spare.  The rest keep their width and go under the
# horizontal scrollbar rather than being silently cut: at the manual split the columns
# want 882 px against the 559 that pane gives.
_COLUMNS = (
    ("qty", "Qty", 40, 34, tk.CENTER, False),
    ("refs", "References", 118, 70, tk.W, False),
    ("value", "Value", 128, 70, tk.W, False),
    ("footprint", "Footprint", 236, 96, tk.W, True),
    ("mpn", "MPN", 128, 70, tk.W, True),
    ("lcsc", "LCSC", 64, 50, tk.W, False),
    ("sheet", "Datasheet", 104, 70, tk.W, False),
    ("flags", "Flags", 64, 44, tk.W, False),
)


class BomTab:
    """The BOM/Datasheets view: a parts table, and an inspector for the selected row.

    View-only by construction -- nothing here has a handle on the board, the IPC session
    or the chat.  The only things it writes are files under `<project>/datasheets/`, and
    only when the user presses Fetch.

    Reads run on the Tk thread (parsing a board is milliseconds); fetching and previewing
    run on workers and come back through the app's event queue, because a 700 KB download
    over a slow link would otherwise freeze the window.
    """

    def __init__(self, notebook: ttk.Notebook, app):
        self.app = app
        self.rendered = False          # has the board been read for the current board?
        self.busy = False
        self.parts: List[Part] = []
        self.lines: List[BomLine] = []
        self.rows: Dict[str, object] = {}     # tree iid -> BomLine | Part
        self.selected = None
        self.preview_photo = None      # keep a reference or Tk drops the image
        self.project_dir = ""

        fonts = app.fonts
        self.frame = ttk.Frame(notebook, padding=(0, 6, 0, 0))

        # ---- toolbar.  Same shape as an ImageTab's: grouping on the left, actions on
        # the right, and the readout between them where it can be clipped harmlessly.
        bar = ttk.Frame(self.frame, padding=(10, 0, 10, 0))
        bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        modes = ttk.Frame(bar)
        modes.pack(side=tk.LEFT)
        self.grouped = tk.IntVar(value=1)
        for label, value in (("Grouped", 1), ("All parts", 0)):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.grouped,
                            style="Toolbutton",
                            command=self._fill).pack(side=tk.LEFT, padx=(0, 2))

        self.controls = ttk.Frame(bar)
        self.controls.pack(side=tk.RIGHT)
        self.reload_btn = ttk.Button(self.controls, text="↻", width=2,
                                     style="Small.TButton", command=self.render)
        self.reload_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.fetch_all_btn = ttk.Button(self.controls, text="Fetch all", width=9,
                                        style="Small.TButton", command=self._fetch_all)
        self.fetch_all_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.folder_btn = ttk.Button(self.controls, text="Folder", width=7,
                                     style="Small.TButton", command=self._reveal_folder)
        self.folder_btn.pack(side=tk.LEFT)

        # Spacer, so the two button groups sit at the ends of the bar.  The counts are
        # NOT in here: measured at the manual split's 594 px the toolbar had 60 px left
        # over and clipped "13 parts · 8 BOM lines · ..." to "13 pa", which is worse than
        # absent.  They get their own full-width row below, where nothing competes.
        ttk.Frame(bar).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.status = ttk.Frame(self.frame, padding=(10, 0, 10, 6))
        self.status.pack(side=tk.TOP, fill=tk.X)
        self.summary = ttk.Label(self.status, text="", font=fonts.small, anchor="w",
                                 foreground=ui_theme.TEXT_DIM)
        self.summary.pack(side=tk.TOP, fill=tk.X)
        # Its OWN full-width row, wrapped, shown only while a fetch runs or after one
        # failed.  Sharing the counts' row cost it half its text -- "could not reach
        # www.bosch-se" -- and the reason a download failed is the whole message.
        # Successes say nothing here: the counts above already read "(2 downloaded)".
        self.detail_state = ttk.Label(self.status, text="", font=fonts.small, anchor="w",
                                      justify="left", wraplength=520,
                                      foreground=ui_theme.TEXT_FAINT)

        # ---- the message shown instead of the table (no board, unreadable board).
        self.message = ttk.Label(self.frame, text="", font=fonts.ui,
                                 foreground=ui_theme.TEXT_DIM,
                                 wraplength=480, justify="left", anchor="nw")

        # ---- detail pane, packed BEFORE the table so pack gives it its parcel first.
        # A ScrolledText-sized table packed with expand=True would otherwise swallow the
        # cavity and squeeze this to nothing -- the trap the chat's entry row documents.
        self.detail = ttk.Frame(self.frame, padding=(10, 8, 10, 6), height=DETAIL_H)
        self.detail.pack(side=tk.BOTTOM, fill=tk.X)
        self.detail.pack_propagate(False)      # honour height, not the children's wishes

        dhead = ttk.Frame(self.detail)
        dhead.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.detail_title = ttk.Label(dhead, text="", font=fonts.ui_bold)
        self.detail_title.pack(side=tk.LEFT)
        actions = ttk.Frame(dhead)
        actions.pack(side=tk.RIGHT)
        self.fetch_btn = ttk.Button(actions, text="Fetch", width=6,
                                    style="Small.TButton", command=self._fetch_selected)
        self.fetch_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.open_btn = ttk.Button(actions, text="Open", width=6,
                                   style="Small.TButton", command=self._open_selected)
        self.open_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.reveal_btn = ttk.Button(actions, text="Reveal", width=7,
                                     style="Small.TButton", command=self._reveal_selected)
        self.reveal_btn.pack(side=tk.LEFT)

        # The preview canvas carries the board background for the same reason the render
        # tabs do: a page rendered with no margin needs a surround that is clearly not
        # part of the document.
        self.preview = tk.Canvas(self.detail, width=PREVIEW_W, height=PREVIEW_W,
                                 background=ui_theme.CANVAS,
                                 highlightthickness=0, borderwidth=0)
        self.preview.pack(side=tk.RIGHT, fill=tk.Y, padx=(12, 0))
        self.preview_note = self.preview.create_text(
            PREVIEW_W // 2, 20, text="", width=PREVIEW_W - 20, anchor="n",
            justify="center", fill=ui_theme.CODE_DIM, font=fonts.small)
        self.preview.bind("<Double-Button-1>", lambda e: self._open_selected())

        # Scrolled, because the detail pane is a fixed height and a real part overflows
        # it: U1's row is nine fields, two of which (a description and a vendor URL) wrap
        # to three lines each in the manual split -- measured, the File line fell off the
        # bottom with no way to reach it.
        fields_box = ttk.Frame(self.detail)
        fields_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # width=1 so the Text asks for nothing: it wraps, so it is never clipped, and
        # Tk's default 80-column request would otherwise report a phantom clip in the
        # pane's own layout snapshot (reqw 720 against the 326 it is happily using).
        self.fields = tk.Text(fields_box, font=fonts.mono_small, wrap=tk.WORD,
                              borderwidth=0, highlightthickness=0, height=8, width=1,
                              background=ui_theme.SURFACE, foreground=ui_theme.TEXT,
                              padx=0, pady=0, state=tk.DISABLED, cursor="arrow")
        fbar = ttk.Scrollbar(fields_box, orient=tk.VERTICAL, command=self.fields.yview)
        self.fields.configure(yscrollcommand=fbar.set)
        fbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.fields.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # A wrapped value continues under the value column rather than under the key, so
        # a three-line URL still reads as one field.
        self.fields.tag_configure("row", lmargin2=96)
        self.fields.tag_configure("key", foreground=ui_theme.TEXT_DIM)
        self.fields.tag_configure("none", foreground=ui_theme.TEXT_FAINT)
        self.fields.tag_configure("link", foreground=ui_theme.ON_LIGHT_CYAN)
        self.fields.tag_configure("have", foreground=ui_theme.ON_LIGHT_GREEN)
        self.fields.tag_configure("warn", foreground=ui_theme.ON_LIGHT_RED)

        ttk.Separator(self.frame).pack(side=tk.BOTTOM, fill=tk.X)

        # ---- the table.
        self.table = ttk.Frame(self.frame, padding=(10, 0, 10, 6))
        self.table.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        style = ttk.Style(self.frame)
        # A private style name: the pane has one Treeview and configuring "Treeview"
        # itself would reach any other that a later tab adds.
        style.configure("Bom.Treeview", font=fonts.small, rowheight=21)
        style.configure("Bom.Treeview.Heading", font=fonts.small_bold)

        self.tree = ttk.Treeview(self.table, style="Bom.Treeview", selectmode="browse",
                                 show="headings",
                                 columns=[c[0] for c in _COLUMNS])
        for name, heading, width, minwidth, anchor, stretch in _COLUMNS:
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, minwidth=minwidth, anchor=anchor,
                             stretch=stretch)
        ybar = ttk.Scrollbar(self.table, orient=tk.VERTICAL, command=self.tree.yview)
        # A HORIZONTAL bar as well, which a Treeview does not get by default: the manual
        # split is narrower than this table, and without it MPN, LCSC and Datasheet are
        # off the right edge with no way to reach them -- a BOM with its part numbers
        # unreachable is not a BOM.
        xbar = ttk.Scrollbar(self.table, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        # grid, not pack: two bars on two edges of one widget is what grid is for, and
        # packing them leaves the corner ambiguous.
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.table.rowconfigure(0, weight=1)
        self.table.columnconfigure(0, weight=1)

        # Row tags say the datasheet state in colour as well as in words, so the two
        # rows that have paper are findable in a thirteen-row table at a glance.
        self.tree.tag_configure("cached", foreground=ui_theme.ON_LIGHT_GREEN)
        self.tree.tag_configure("link", foreground=ui_theme.ON_LIGHT_CYAN)
        self.tree.tag_configure("plain", foreground=ui_theme.TEXT)
        self.tree.tag_configure("dim", foreground=ui_theme.TEXT_FAINT)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-Button-1>", lambda e: self._open_selected())
        self.frame.bind("<Configure>", self._on_frame_resize)

        self._set_detail_placeholder("Select a part to see its details.")
        self._update_buttons()

    # ------------------------------------------------------------------ loading

    def render(self) -> None:
        """(Re-)read the open board.  Named to match ImageTab so the pane treats them alike."""
        if self.busy:
            return
        paths = self.app.board_paths()
        self.rendered = True
        # Cleared before anything can fail, so a stale table from the previous board is
        # never what a failure leaves on screen.
        self.parts, self.lines = [], []
        if paths is None:
            self.project_dir = ""
            # Shared with the render tabs: "open a board" is the wrong instruction when the
            # real blocker is a dead IPC client, and this pane cannot tell the difference on
            # its own.  app.no_board_message() names the actual state.
            self._show_message(self.app.no_board_message())
            self._clear_detail("No BOM: see the message on the left.")
            return

        self.project_dir = paths.project_dir
        try:
            self.parts = read_board(paths.board)
        except BomError as exc:
            self._show_message(str(exc), soft=False)
            self._clear_detail("The board could not be read.")
            return

        self.lines = group_parts(self.parts)
        self._show_table()
        self._fill()

    def _fill(self) -> None:
        """Rebuild the table from the parts already read.  No file access."""
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()

        if self.grouped.get():
            for index, line in enumerate(self.lines):
                iid = f"g{index}"
                state, tag = self._sheet_state(line.datasheet, line.mpn)
                self.tree.insert("", tk.END, iid=iid, tags=(tag,), values=(
                    line.qty, line.references, line.value, line.footprint,
                    line.mpn, line.lcsc, state, line.flags()))
                self.rows[iid] = line
        else:
            for index, part in enumerate(self.parts):
                iid = f"p{index}"
                state, tag = self._sheet_state(part.datasheet, part.mpn)
                if not part.in_bom:
                    tag = "dim"
                self.tree.insert("", tk.END, iid=iid, tags=(tag,), values=(
                    1, part.reference, part.value, part.footprint,
                    part.mpn, part.lcsc, state, part.flags()))
                self.rows[iid] = part

        self.summary.config(text=self._summary_text())
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
        else:
            self._clear_detail("This board has no footprints.")
        self._update_buttons()

    def _summary_text(self) -> str:
        excluded = sum(1 for p in self.parts if not p.in_bom)
        with_url = sum(1 for p in self.parts if p.datasheet)
        cached = sum(1 for p in self.parts
                     if p.datasheet and cached_path(self.project_dir, p.datasheet,
                                                    p.mpn or p.value))
        bits = [f"{len(self.parts)} part{'' if len(self.parts) == 1 else 's'}",
                f"{len(self.lines)} BOM line{'' if len(self.lines) == 1 else 's'}",
                f"{with_url} datasheet{'' if with_url == 1 else 's'} "
                f"({cached} downloaded)"]
        if excluded:
            # Said out loud rather than left as a discrepancy between two counts: the
            # grouped table simply does not contain these rows, and a reader counting
            # references would otherwise find parts missing with no explanation.
            bits.append(f"{excluded} excluded from BOM")
        return "  ·  ".join(bits)

    def _sheet_state(self, url: str, label: str) -> Tuple[str, str]:
        """The Datasheet cell and its row tag.  Blank means the board names no URL."""
        if not url:
            return ("", "plain")
        if cached_path(self.project_dir, url, label):
            return ("✓ downloaded", "cached")
        host = urllib.parse.urlparse(url).netloc
        return (host.replace("www.", "") or "link", "link")

    def _show_table(self) -> None:
        self.message.pack_forget()
        if not self.table.winfo_ismapped():
            # Re-packed rather than left in place: pack allocates in call order, and the
            # detail frame is already parked at the bottom, so the table lands in what is
            # left -- which is where it was.
            self.table.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _show_message(self, text: str, soft: bool = True) -> None:
        self.table.pack_forget()
        self.summary.config(text="")
        self.message.config(text=text,
                            foreground=(ui_theme.TEXT_DIM if soft
                                        else ui_theme.ON_LIGHT_RED))
        self.message.pack(fill=tk.BOTH, expand=True, padx=18, pady=16, anchor="nw")

    def _set_notice(self, text: str, colour: str = ui_theme.TEXT_DIM) -> None:
        """Show (or clear) the row under the counts.  Empty means the row disappears."""
        self.detail_state.config(text=text, foreground=colour)
        if text:
            self.detail_state.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))
        else:
            self.detail_state.pack_forget()

    def _on_frame_resize(self, event) -> None:
        self.message.configure(wraplength=max(220, event.width - 40))
        self.detail_state.configure(wraplength=max(220, event.width - 40))

    def describe(self) -> str:
        """What the tab is actually showing, in the shape ImageTab.describe() uses."""
        if not self.rendered:
            return "not read yet"
        if not self.parts:
            first = (self.message.cget("text") or "").strip().splitlines()
            return "no parts: " + (first[0] if first else "nothing read")
        mode = "grouped" if self.grouped.get() else "all parts"
        return f"{len(self.parts)} parts, {len(self.lines)} BOM lines ({mode})"

    # ----------------------------------------------------------------- selection

    def _current(self):
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def _on_select(self, _event=None) -> None:
        row = self._current()
        self.selected = row
        if row is None:
            self._clear_detail("Select a part to see its details.")
            return
        self._show_detail(row)
        self._update_buttons()

    def _selected_url(self) -> str:
        row = self.selected
        return getattr(row, "datasheet", "") if row is not None else ""

    def _selected_label(self) -> str:
        row = self.selected
        if row is None:
            return ""
        return getattr(row, "mpn", "") or getattr(row, "value", "")

    def _selected_cache(self) -> Optional[str]:
        if not (self.project_dir and self._selected_url()):
            return None
        return cached_path(self.project_dir, self._selected_url(),
                           self._selected_label())

    def _show_detail(self, row) -> None:
        if isinstance(row, BomLine):
            title = f"{row.references}  ·  {row.value}" if row.value else row.references
            refs = f"{row.references}  (qty {row.qty})"
        else:
            title = f"{row.reference}  ·  {row.value}" if row.value else row.reference
            refs = row.reference
        self.detail_title.config(text=title)

        url = row.datasheet
        cache = self._selected_cache()
        rows = [("Reference", refs, ""),
                ("Value", row.value, ""),
                ("Footprint", row.footprint, ""),
                ("MPN", row.mpn, ""),
                ("LCSC", row.lcsc, ""),
                ("Description", row.description, ""),
                ("Datasheet", url, "link" if url else "")]
        if url:
            if cache:
                rows.append(("File", f"{DATASHEET_DIRNAME}/{os.path.basename(cache)}  "
                                     f"({human_size(os.path.getsize(cache))})", "have"))
            else:
                rows.append(("File", "not downloaded yet -- press Fetch", "warn"))

        self.fields.config(state=tk.NORMAL)
        self.fields.delete("1.0", tk.END)
        for key, value, tag in rows:
            self.fields.insert(tk.END, f"{key:<12}", ("row", "key"))
            if value:
                self.fields.insert(tk.END, value + "\n", ("row", tag) if tag else "row")
            else:
                # "--" and not a guess: the board does not carry this field, and a BOM
                # that invents an MPN is worse than one that admits it has none.
                self.fields.insert(tk.END, "--\n", ("row", "none"))
        self.fields.config(state=tk.DISABLED)

        self._update_preview()
        self._update_buttons()

    def _clear_detail(self, note: str) -> None:
        self.selected = None
        self.detail_title.config(text="")
        self._set_notice("")
        self.fields.config(state=tk.NORMAL)
        self.fields.delete("1.0", tk.END)
        self.fields.config(state=tk.DISABLED)
        self._set_detail_placeholder(note)
        self._update_buttons()

    def _set_detail_placeholder(self, note: str) -> None:
        self.preview.delete("page")
        self.preview_photo = None
        self.preview.itemconfigure(self.preview_note, text=note)

    # ------------------------------------------------------------------- preview

    def _update_preview(self) -> None:
        cache = self._selected_cache()
        if not self._selected_url():
            self._set_detail_placeholder(
                "This part has no datasheet URL on the board.")
            return
        if not cache:
            self._set_detail_placeholder(
                "Not downloaded.\nPress Fetch to put it in the datasheets folder.")
            return
        self._set_detail_placeholder("rendering page 1...")
        threading.Thread(target=self._preview_worker, args=(cache,),
                         daemon=True).start()

    def _preview_worker(self, pdf: str) -> None:
        try:
            png = preview_png(pdf, os.path.join(self.app.render_dir, "datasheets"))
        except PreviewError as exc:
            message = str(exc)
            self.app.post(lambda: self._preview_failed(pdf, message))
            return
        self.app.post(lambda: self._preview_ready(pdf, png))

    def _preview_failed(self, pdf: str, message: str) -> None:
        if self._selected_cache() != pdf:
            return                     # the selection moved on while we were rendering
        self._set_detail_placeholder(message)

    def _preview_ready(self, pdf: str, png: str) -> None:
        # The user may have clicked another row while qlmanage ran; drawing the old page
        # over the new selection would be a lie about what is on screen.
        if self._selected_cache() != pdf:
            return
        try:
            from PIL import Image, ImageTk                  # noqa: PLC0415
        except ImportError:
            self._set_detail_placeholder(
                "Pillow is not installed in this interpreter, so the page cannot be "
                "shown here. The PDF is downloaded; press Open.")
            return
        try:
            image = Image.open(png)
            image.load()
        except Exception as exc:                            # noqa: BLE001
            self._set_detail_placeholder(f"could not open the preview: {exc}")
            return

        width = max(1, self.preview.winfo_width())
        height = max(1, self.preview.winfo_height())
        scale = min(width / image.width, height / image.height)
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        self.preview_photo = ImageTk.PhotoImage(image.resize(size, Image.LANCZOS))
        self.preview.delete("page")
        self.preview.itemconfigure(self.preview_note, text="")
        self.preview.create_image(width // 2, height // 2, image=self.preview_photo,
                                  tags="page")

    # -------------------------------------------------------------------- actions

    def _update_buttons(self) -> None:
        url = self._selected_url()
        cache = self._selected_cache()
        self.fetch_btn.config(state=(tk.NORMAL if url and not self.busy
                                     else tk.DISABLED))
        # Open falls back to the URL, so it is live whenever the board names one.
        self.open_btn.config(state=tk.NORMAL if (url or cache) else tk.DISABLED)
        self.reveal_btn.config(state=tk.NORMAL if cache else tk.DISABLED)
        pending = self._pending()
        self.fetch_all_btn.config(
            state=(tk.NORMAL if pending and not self.busy else tk.DISABLED))
        self.folder_btn.config(state=tk.NORMAL if self.project_dir else tk.DISABLED)

    def _pending(self) -> List[Tuple[str, str]]:
        """(url, label) for every datasheet on the board that is not downloaded yet."""
        seen = set()
        out: List[Tuple[str, str]] = []
        for part in self.parts:
            if not part.datasheet or part.datasheet in seen:
                continue
            seen.add(part.datasheet)
            label = part.mpn or part.value
            if not cached_path(self.project_dir, part.datasheet, label):
                out.append((part.datasheet, label))
        return out

    def _fetch_selected(self) -> None:
        url = self._selected_url()
        if url:
            self._start_fetch([(url, self._selected_label())])

    def _fetch_all(self) -> None:
        self._start_fetch(self._pending())

    def _start_fetch(self, wanted: List[Tuple[str, str]]) -> None:
        if self.busy or not wanted or not self.project_dir:
            return
        self.busy = True
        self._update_buttons()
        self._set_notice(f"downloading {len(wanted)}...")
        threading.Thread(target=self._fetch_worker,
                         args=(self.project_dir, list(wanted)), daemon=True).start()

    def _fetch_worker(self, project_dir: str, wanted: List[Tuple[str, str]]) -> None:
        done, failures = 0, []
        for url, label in wanted:
            dest = os.path.join(datasheet_dir(project_dir), cache_name(url, label))
            try:
                fetch_datasheet(url, dest)
                done += 1
            except FetchError as exc:
                failures.append(str(exc))
            except Exception as exc:                        # noqa: BLE001
                failures.append(f"{type(exc).__name__}: {exc}")
        self.app.post(lambda: self._fetch_done(done, failures))

    def _fetch_done(self, done: int, failures: List[str]) -> None:
        self.busy = False
        if failures:
            # The first failure in full: it names the host and what it answered, which
            # is the only thing that tells a proxy problem from a dead vendor link.
            self._set_notice(failures[0], ui_theme.ON_LIGHT_RED)
        else:
            # Nothing on success: the counts to the left are rebuilt below and already
            # read "(2 downloaded)", and a second label saying it competed with them for
            # the same row and got clipped to "2 do".
            self._set_notice("")
        # The Datasheet column and the summary both count cached files, so the table is
        # rebuilt rather than patched.
        keep = self.tree.selection()
        self._fill()
        if keep and keep[0] in self.rows:
            self.tree.selection_set(keep[0])
        self._update_buttons()

    def _open_selected(self) -> None:
        target = self._selected_cache() or self._selected_url()
        if target:
            hand_to_os(target)

    def _reveal_selected(self) -> None:
        cache = self._selected_cache()
        if cache:
            reveal(cache)

    def _reveal_folder(self) -> None:
        if not self.project_dir:
            return
        folder = datasheet_dir(self.project_dir)
        try:
            # Created here as well as on fetch: "show me the folder" is a request for it
            # to exist, and revealing a path that is not there does nothing at all.
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self._set_notice(f"cannot create {folder}: {exc}",
                             ui_theme.ON_LIGHT_RED)
            return
        reveal(folder)
