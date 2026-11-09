#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Pre-DRC geometric check for a generated board.

DRC is the authority, but it costs ~25 s per run and reports violations in board
coordinates that are tedious to map back to the generator's intent.  This reads the same
data the generator emitted and answers, in well under a second and in the generator's own
vocabulary:

  * which track pairs on different nets come closer than the clearance
  * which tracks pass through a hole (NPTH/PTH) without belonging to it
  * which tracks leave the board outline (copper-to-edge clearance)
  * which track endpoints do not land on a pad or another track (dangling)

Run it after every generator edit; only run DRC once this is clean.  Usage:

    python3 check_routing.py [board.kicad_pcb]
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CLEARANCE = 0.2      # mm, Default netclass
HOLE_CLEAR = 0.25    # mm, rules.min_hole_clearance
EDGE_CLEAR = 0.3     # mm, rules.min_copper_edge_clearance
SILK_CLEAR = 0.05    # mm, generous: silk warnings are cosmetic, so only flag real overlap
MASK_EXPANSION = 0.05   # mm, board (pad_to_mask_clearance): aperture grows this much per side
MASK_MIN_WEB = 0.1      # mm, thinnest mask web a fab will reliably hold between apertures
GLYPH_H = 0.8        # mm, refdes font size emitted by footprint()
GLYPH_ADV = 0.62     # mm, horizontal advance per character at that size
EPS = 1e-9


@dataclass(frozen=True)
class Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    net: str
    layer: str
    width: float

    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass(frozen=True)
class Hole:
    x: float
    y: float
    d: float
    ref: str
    net: str = ""    # a track on the hole's own net is connecting to it, not violating it


def seg_distance(a: Seg, b: Seg) -> float:
    """Minimum distance between the centre lines of two segments."""
    def pt_seg(px, py, x1, y1, x2, y2) -> float:
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        if L2 < EPS:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    # Proper segment intersection means distance 0.
    d1 = (a.x2 - a.x1, a.y2 - a.y1)
    d2 = (b.x2 - b.x1, b.y2 - b.y1)
    denom = d1[0] * d2[1] - d1[1] * d2[0]

    if abs(denom) > EPS:
        t = ((b.x1 - a.x1) * d2[1] - (b.y1 - a.y1) * d2[0]) / denom
        u = ((b.x1 - a.x1) * d1[1] - (b.y1 - a.y1) * d1[0]) / denom
        if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
            return 0.0

    return min(
        pt_seg(a.x1, a.y1, b.x1, b.y1, b.x2, b.y2),
        pt_seg(a.x2, a.y2, b.x1, b.y1, b.x2, b.y2),
        pt_seg(b.x1, b.y1, a.x1, a.y1, a.x2, a.y2),
        pt_seg(b.x2, b.y2, a.x1, a.y1, a.x2, a.y2),
    )


def parse(path: Path) -> tuple[list[Seg], list[Hole], list[tuple[float, float]]]:
    text = path.read_text()

    segs: list[Seg] = []
    for m in re.finditer(
        r'\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)'
        r'\s*\(width ([-\d.]+)\)\s*\(layer "([^"]+)"\)(?:[^)]*)\s*\(net "([^"]*)"\)',
        text,
    ):
        x1, y1, x2, y2, w, layer, net = m.groups()
        segs.append(Seg(float(x1), float(y1), float(x2), float(y2), net, layer,
                        float(w)))

    holes: list[Hole] = []
    for fm in re.finditer(r'\(footprint "[^"]+"(.*?)\n\t\)', text, re.S):
        body = fm.group(1)
        tm = re.search(r'\(translate ([-\d.]+) ([-\d.]+)\)', body)
        rm = re.search(r'\(property "Reference" "([^"]+)"', body)
        if not tm:
            continue
        ox, oy = float(tm.group(1)), float(tm.group(2))
        ref = rm.group(1) if rm else "?"

        # Split the body on pad boundaries so a pad's drill and net cannot be picked up
        # from a neighbouring pad by a greedy match.
        for chunk in re.split(r'(?=\(pad ")', body)[1:]:
            am = re.search(r'\(at ([-\d.]+) ([-\d.]+)', chunk)
            dm = re.search(r'\(drill ([\d.]+)\)', chunk)
            nm = re.search(r'\(net "([^"]*)"\)', chunk)
            if not am or not dm:
                continue
            px, py = float(am.group(1)), float(am.group(2))
            holes.append(Hole(ox + px, oy + py, float(dm.group(1)), ref,
                              nm.group(1) if nm else ""))

    edges: list[tuple[float, float]] = []
    for em in re.finditer(
        r'\(gr_line\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)'
        r'[^)]*\)\s*\(layer "Edge.Cuts"', text
    ):
        edges.append((float(em.group(1)), float(em.group(2))))

    return segs, holes, edges


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    size: float
    drill: float
    net: str


def parse_vias(path: Path) -> list[Via]:
    """Vias with pad size, drill and net.  Vias were entirely absent from the clearance
    checks, so a track crossing a via of another net read as clean."""
    text = path.read_text()
    out: list[Via] = []
    for m in re.finditer(
        r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)\s+\(size ([\d.]+)\)\s+\(drill ([\d.]+)\)'
        r'.*?\(net "([^"]*)"\)', text, re.S
    ):
        x, y, size, drill, net = m.groups()
        out.append(Via(float(x), float(y), float(size), float(drill), net))
    return out


def seg_box_distance(s: Seg, box: Box) -> float:
    """Distance from a segment's centreline to an axis-aligned box (0 if it enters it)."""
    x1, y1, x2, y2 = box
    edges = [
        Seg(x1, y1, x2, y1, "", s.layer, 0), Seg(x2, y1, x2, y2, "", s.layer, 0),
        Seg(x2, y2, x1, y2, "", s.layer, 0), Seg(x1, y2, x1, y1, "", s.layer, 0),
    ]
    # Either endpoint inside the box means the segment overlaps it.
    for (px, py) in ((s.x1, s.y1), (s.x2, s.y2)):
        if x1 <= px <= x2 and y1 <= py <= y2:
            return 0.0
    return min(seg_distance(s, e) for e in edges)


def parse_zones(path: Path) -> list[tuple[str, str, list[tuple[float, float]]]]:
    """Copper zones as ``(name, net, outline points)``."""
    text = path.read_text()
    out: list[tuple[str, str, list[tuple[float, float]]]] = []

    for zm in re.finditer(r'\(zone\s+(.*?)\n\t\)', text, re.S):
        zb = zm.group(1)
        nm = re.search(r'\(net "([^"]*)"\)', zb)
        am = re.search(r'\(name "([^"]*)"\)', zb)
        # Match an explicit run of (xy ...) elements.  A lazy (.*?)\)\s*\) eats the last
        # point's closing paren and silently drops a corner.
        pm = re.search(r'\(polygon\s+\(pts\s+((?:\(xy [-\d.]+ [-\d.]+\)\s*)+)', zb, re.S)
        if not pm:
            continue
        pts = [(float(x), float(y))
               for x, y in re.findall(r'\(xy ([-\d.]+) ([-\d.]+)\)', pm.group(1))]
        out.append((am.group(1) if am else "?", nm.group(1) if nm else "", pts))

    return out


def parse_outline(path: Path) -> list[tuple[float, float]]:
    """Edge.Cuts as an ordered point ring, walked from the unordered gr_line soup.

    Returns [] if the outline is not a single closed loop, so callers skip the check rather
    than reporting nonsense against a partial ring.
    """
    text = path.read_text()
    segs = [(float(a), float(b), float(c), float(d)) for a, b, c, d in re.findall(
        r'\(gr_line\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
        r'.*?\(layer "Edge\.Cuts"\)', text, re.S)]
    if not segs:
        return []

    def key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 3), round(y, 3))

    adj: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for x1, y1, x2, y2 in segs:
        adj.setdefault(key(x1, y1), []).append(key(x2, y2))
        adj.setdefault(key(x2, y2), []).append(key(x1, y1))

    if any(len(v) != 2 for v in adj.values()):
        return []       # not a clean closed ring

    start = next(iter(adj))
    ring = [start]
    prev, cur = None, start
    while True:
        nxt = next((p for p in adj[cur] if p != prev), None)
        if nxt is None or nxt == start:
            break
        ring.append(nxt)
        prev, cur = cur, nxt

    return ring if len(ring) == len(adj) else []


def point_in_poly(pt: tuple[float, float], poly: list[tuple[float, float]],
                  margin: float = 0.0) -> bool:
    """Ray-cast containment, additionally requiring `margin` clearance from every edge."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    if not inside:
        return False

    if margin > 0:
        for i in range(n):
            a = poly[i]
            b = poly[(i + 1) % n]
            if seg_distance(Seg(x, y, x, y, "", "", 0),
                            Seg(a[0], a[1], b[0], b[1], "", "", 0)) < margin - 1e-9:
                return False
    return True


@dataclass
class Pad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    w: float                # copper extent, already rotated
    h: float
    layers: str             # raw (layers ...) string
    drill: float = 0.0

    def box(self, grow: float = 0.0) -> Box:
        return (self.x - self.w / 2 - grow, self.y - self.h / 2 - grow,
                self.x + self.w / 2 + grow, self.y + self.h / 2 + grow)

    def on_front(self) -> bool:
        return self.drill > 0 or "F.Cu" in self.layers or "*.Cu" in self.layers

    def on_back(self) -> bool:
        return self.drill > 0 or "B.Cu" in self.layers or "*.Cu" in self.layers


def parse_pads(path: Path) -> list[Pad]:
    """Every pad with its net, rotated copper extent, layer set and drill."""
    text = path.read_text()
    out: list[Pad] = []

    for fm in re.finditer(r'\(footprint "[^"]+"(.*?)\n\t\)', text, re.S):
        fb = fm.group(1)
        tm = re.search(r'\(translate ([-\d.]+) ([-\d.]+)\)', fb)
        if not tm:
            continue
        ox, oy = float(tm.group(1)), float(tm.group(2))
        rmatch = re.search(r'\(rotate ([-\d.]+)\)', fb)
        frot = float(rmatch.group(1)) if rmatch else 0.0
        refm = re.search(r'\(property "Reference" "([^"]+)"', fb)
        ref = refm.group(1) if refm else "?"

        for chunk in re.split(r'(?=\(pad ")', fb)[1:]:
            nummatch = re.match(r'\(pad "([^"]*)"', chunk)
            am = re.search(r'\(at ([-\d.]+) ([-\d.]+)\s*([-\d.]*)\)', chunk, re.S)
            sm = re.search(r'\(size ([\d.]+) ([\d.]+)\)', chunk, re.S)
            if not (am and sm):
                continue
            a = math.radians(frot)
            lx, ly = float(am.group(1)), float(am.group(2))
            px = ox + lx * math.cos(a) + ly * math.sin(a)
            py = oy - lx * math.sin(a) + ly * math.cos(a)
            pw, ph = float(sm.group(1)), float(sm.group(2))
            if (float(am.group(3) or 0) + frot) % 180 == 90:
                pw, ph = ph, pw
            lm = re.search(r'\(layers([^)]*)\)', chunk)
            dm = re.search(r'\(drill ([\d.]+)\)', chunk)
            nm = re.search(r'\(net "([^"]*)"\)', chunk)
            out.append(Pad(ref, nummatch.group(1) if nummatch else "?",
                           nm.group(1) if nm else "", px, py, pw, ph,
                           lm.group(1) if lm else "", float(dm.group(1)) if dm else 0.0))

    return out


Box = tuple[float, float, float, float]     # x1, y1, x2, y2


def boxes_overlap(a: Box, b: Box, gap: float = 0.0) -> bool:
    """True when two axis-aligned boxes come within `gap` of each other."""
    return not (a[2] + gap < b[0] or b[2] + gap < a[0]
                or a[3] + gap < b[1] or b[3] + gap < a[1])


def parse_refs(path: Path) -> tuple[list[tuple[str, Box]], list[tuple[str, Box]]]:
    """Bounding boxes of silkscreen reference designators, and of all pads.

    Returns ``(refs, pads)`` as ``(reference, box)`` pairs in board coordinates.  The refdes
    box is the text extent: `GLYPH_ADV` per character wide, `GLYPH_H` tall, centred on the
    label position (KiCad centre-justifies by default), swapped when the label is rotated 90.
    Both the footprint transform and the label's own offset are applied.
    """
    text = path.read_text()
    refs: list[tuple[str, Box]] = []
    pads: list[tuple[str, Box]] = []

    for fm in re.finditer(r'\(footprint "[^"]+"(.*?)\n\t\)', text, re.S):
        body = fm.group(1)
        tm = re.search(r'\(translate ([-\d.]+) ([-\d.]+)\)', body)
        if not tm:
            continue
        ox, oy = float(tm.group(1)), float(tm.group(2))
        frot = float((re.search(r'\(rotate ([-\d.]+)\)', body) or [0, "0"])[1])

        def place(lx: float, ly: float) -> tuple[float, float]:
            """Footprint-local -> board, applying the footprint rotation."""
            a = math.radians(frot)
            return (ox + lx * math.cos(a) + ly * math.sin(a),
                    oy - lx * math.sin(a) + ly * math.cos(a))

        # The Reference property, on silk.  Only the first (at ...) inside the property block.
        pm = re.search(r'\(property "Reference" "([^"]+)"\s*\(at ([-\d.]+) ([-\d.]+)'
                       r'\s*([-\d.]*)\)', body)
        if pm:
            ref = pm.group(1)
            lx, ly = float(pm.group(2)), float(pm.group(3))
            trot = float(pm.group(4) or 0)
            cx, cy = place(lx, ly)
            w, h = GLYPH_ADV * len(ref), GLYPH_H
            if (trot + frot) % 180 == 90:
                w, h = h, w
            refs.append((ref, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)))
        else:
            ref = "?"

        for chunk in re.split(r'(?=\(pad ")', body)[1:]:
            am = re.search(r'\(at ([-\d.]+) ([-\d.]+)', chunk)
            sm = re.search(r'\(size ([\d.]+) ([\d.]+)\)', chunk)
            if not am or not sm:
                continue
            cx, cy = place(float(am.group(1)), float(am.group(2)))
            pw, ph = float(sm.group(1)) / 2, float(sm.group(2)) / 2
            pads.append((ref, (cx - pw, cy - ph, cx + pw, cy + ph)))

    return refs, pads


def main() -> int:
    default = Path(__file__).with_name("teensy-sensor-suite.kicad_pcb")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    segs, holes, _ = parse(path)

    print(f"parsed {len(segs)} segments, {len(holes)} holes from {path.name}")

    problems = 0

    # 1. track-to-track clearance between different nets on the same layer
    pairs: dict[tuple[str, str], int] = {}
    for i, a in enumerate(segs):
        for b in segs[i + 1:]:
            if a.net == b.net or a.layer != b.layer:
                continue
            need = CLEARANCE + (a.width + b.width) / 2
            if seg_distance(a, b) < need - 1e-6:
                key = tuple(sorted((a.net, b.net)))
                pairs[key] = pairs.get(key, 0) + 1

    if pairs:
        print(f"\nCLEARANCE/SHORT between nets ({sum(pairs.values())} pairs):")
        for (n1, n2), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
            print(f"   {n:4d}  {n1} <-> {n2}")
        problems += sum(pairs.values())

        # Where, exactly.  Guessing which corridor is at fault from net names alone is what
        # made the hand-tuning loop slow; print the offending geometry.
        print("   worst offenders:")
        worst = []
        for i, a in enumerate(segs):
            for b in segs[i + 1:]:
                if a.net == b.net or a.layer != b.layer:
                    continue
                need = CLEARANCE + (a.width + b.width) / 2
                d = seg_distance(a, b)
                if d < need - 1e-6:
                    worst.append((d, a, b))
        for d, a, b in sorted(worst, key=lambda t: t[0])[:6]:
            print(f"      {d:.3f}mm {a.layer}: {a.net} "
                  f"({a.x1:.1f},{a.y1:.1f})-({a.x2:.1f},{a.y2:.1f})  vs  {b.net} "
                  f"({b.x1:.1f},{b.y1:.1f})-({b.x2:.1f},{b.y2:.1f})")

    # 2. tracks passing through a hole they do not own
    hits: dict[str, int] = {}
    for s in segs:
        for h in holes:
            if h.net and h.net == s.net:
                continue    # the track is connecting to this pad, not violating it
            need = HOLE_CLEAR + h.d / 2 + s.width / 2
            d = seg_distance(s, Seg(h.x, h.y, h.x, h.y, "", s.layer, 0))
            if d < need - 1e-6:
                key = f"{s.net} through {h.ref}"
                hits[key] = hits.get(key, 0) + 1

    if hits:
        print(f"\nHOLE CLEARANCE ({len(hits)} cases):")
        for k, n in sorted(hits.items(), key=lambda kv: -kv[1])[:12]:
            print(f"   {n:4d}  {k}")
        problems += len(hits)

    # 2b. track vs PAD COPPER, and track vs VIA.  These were the checker's two biggest blind
    #     spots: it compared tracks to other tracks and to drilled holes, but never to pad
    #     copper or to via barrels, so a track crossing a pad of another net -- an outright
    #     short -- read as CLEAN.  Real DRC reported 11 shorting_items that all landed here.
    all_pads = parse_pads(path)
    vias = parse_vias(path)
    short_hits: dict[str, int] = {}
    worst: list[tuple[float, str]] = []

    for s in segs:
        for p in all_pads:
            if p.net and s.net and p.net == s.net:
                continue
            if not ((s.layer == "F.Cu" and p.on_front())
                    or (s.layer == "B.Cu" and p.on_back())):
                continue
            d = seg_box_distance(s, p.box())
            need = CLEARANCE + s.width / 2
            if d < need - 1e-9:
                key = f"{s.net or '-'} <-> {p.ref}.{p.number} ({p.net or '-'}) pad"
                short_hits[key] = short_hits.get(key, 0) + 1
                worst.append((d - need,
                              f"short by {need - d:.3f}mm ({d:.3f} of {need:.3f}) "
                              f"{s.layer}: {s.net} track "
                              f"({s.x1:.2f},{s.y1:.2f})-({s.x2:.2f},{s.y2:.2f}) vs "
                              f"{p.ref}.{p.number} [{p.net or '-'}] at ({p.x:.2f},{p.y:.2f})"))

        for v in vias:
            if v.net and s.net and v.net == s.net:
                continue        # a via on the same net is the intended connection
            d = seg_distance(s, Seg(v.x, v.y, v.x, v.y, "", s.layer, 0))
            need = CLEARANCE + s.width / 2 + v.size / 2
            if d < need - 1e-9:
                key = f"{s.net or '-'} <-> via [{v.net or '-'}]"
                short_hits[key] = short_hits.get(key, 0) + 1
                worst.append((d - need,
                              f"short by {need - d:.3f}mm ({d:.3f} of {need:.3f}) "
                              f"{s.layer}: {s.net} track "
                              f"({s.x1:.2f},{s.y1:.2f})-({s.x2:.2f},{s.y2:.2f}) vs "
                              f"via [{v.net}] at ({v.x:.2f},{v.y:.2f})"))

    # via vs pad, and via vs via
    for i, v in enumerate(vias):
        for p in all_pads:
            if v.net and p.net and v.net == p.net:
                continue
            d = seg_box_distance(Seg(v.x, v.y, v.x, v.y, "", "F.Cu", 0), p.box())
            if d < CLEARANCE + v.size / 2 - 1e-9:
                key = f"via [{v.net or '-'}] <-> {p.ref}.{p.number} ({p.net or '-'}) pad"
                short_hits[key] = short_hits.get(key, 0) + 1
        for w in vias[i + 1:]:
            if v.net and w.net and v.net == w.net:
                continue
            d = math.dist((v.x, v.y), (w.x, w.y))
            if d < CLEARANCE + v.size / 2 + w.size / 2 - 1e-9:
                key = f"via [{v.net or '-'}] <-> via [{w.net or '-'}]"
                short_hits[key] = short_hits.get(key, 0) + 1

    if short_hits:
        print(f"\nCOPPER SHORT / CLEARANCE vs PADS+VIAS ({len(short_hits)} pair(s)):")
        for k, n in sorted(short_hits.items(), key=lambda kv: -kv[1])[:12]:
            print(f"   {n:4d}  {k}")
        if worst:
            # Sorted by how far SHORT of the requirement each is, not by raw distance: sorting
            # by distance mixed real violations in with pairs that merely happened to be close,
            # so the list could not be read as "these are the problems".
            print("   worst offenders:")
            for _, line in sorted(worst)[:6]:
                print(f"      {line}")
        problems += len(short_hits)

    # 3. silkscreen reference designators overlapping pads or each other.  DRC calls these
    #    silk_over_copper / silk_overlap.  A refdes is approximated by the bounding box of
    #    its text: 0.8 mm glyphs, ~0.62 mm advance per character.
    refs, pads = parse_refs(path)
    silk_hits: list[str] = []

    for i, (r1, b1) in enumerate(refs):
        for r2, b2 in refs[i + 1:]:
            if boxes_overlap(b1, b2, SILK_CLEAR):
                silk_hits.append(f"refdes {r1} overlaps refdes {r2}")

    for r, b in refs:
        for pref, pb in pads:
            if boxes_overlap(b, pb, SILK_CLEAR):
                silk_hits.append(f"refdes {r} over a pad of {pref}")
                break

    if silk_hits:
        print(f"\nSILKSCREEN ({len(silk_hits)} cases):")
        for h in silk_hits[:14]:
            print(f"      {h}")
        problems += len(silk_hits)

    # 4. copper zone outlines must stay inside the board edge.  This check exists because a
    #    pour that overhung the board's chamfers survived every other check here: clearance,
    #    holes and silk all ignore zones, and DRC was unavailable.  A zone crossing the edge
    #    is a copper_edge_clearance error.
    zones = parse_zones(path)
    edge_poly = parse_outline(path)
    zone_hits: list[str] = []

    if edge_poly:
        for zname, znet, zpts in zones:
            outside = [p for p in zpts if not point_in_poly(p, edge_poly, EDGE_CLEAR)]
            if outside:
                x, y = outside[0]
                zone_hits.append(
                    f"zone {zname!r} (net {znet}) has {len(outside)} of {len(zpts)} "
                    f"outline point(s) within {EDGE_CLEAR} mm of / outside the board edge, "
                    f"first at ({x:.2f},{y:.2f})")

    if zone_hits:
        print(f"\nZONE OUTLINE ({len(zone_hits)} case(s)):")
        for h in zone_hits:
            print(f"      {h}")
        problems += len(zone_hits)

    # 4b. tracks and pads must also honour copper-to-edge clearance.  The routing plans march
    #     drop columns outward from an anchor, so with enough crossings the last column walks
    #     off the board -- which happened: the 5th drop column landed exactly on the west edge.
    #     Such a board still opens and still exports, and is only wrong at the fab.
    if edge_poly:
        edge_hits: list[str] = []

        for s in segs:
            for (x, y) in ((s.x1, s.y1), (s.x2, s.y2)):
                if not point_in_poly((x, y), edge_poly, EDGE_CLEAR + s.width / 2):
                    edge_hits.append(f"track {s.net} on {s.layer} reaches ({x:.2f},{y:.2f})")
                    break

        for h in holes:
            if not point_in_poly((h.x, h.y), edge_poly, EDGE_CLEAR + h.d / 2):
                edge_hits.append(
                    f"pad {h.ref} (net {h.net or '-'}) at ({h.x:.2f},{h.y:.2f})")

        if edge_hits:
            print(f"\nEDGE CLEARANCE ({len(edge_hits)} case(s)):")
            for h in edge_hits[:12]:
                print(f"      {h}")
            problems += len(edge_hits)

    # 4c. a net should have ONE track width.  A mixed width is legal, so DRC says nothing, but
    #     it almost always means a call site forgot to pass one and silently took a default --
    #     which is how the SDA/SCL trunks ended up 0.3 mm while every tap into them was 0.25.
    widths: dict[str, set[float]] = {}
    for s in segs:
        widths.setdefault(s.net, set()).add(s.width)

    mixed = {n: w for n, w in widths.items() if len(w) > 1}
    if mixed:
        print(f"\nMIXED TRACK WIDTH ({len(mixed)} net(s)):")
        for n, w in sorted(mixed.items()):
            print(f"      {n}: {sorted(w)} mm")
        problems += len(mixed)

    # 4d. solder mask bridges.  Each aperture is the pad grown by MASK_EXPANSION; when two
    #     apertures of DIFFERENT nets merge, the mask web between them disappears and solder
    #     can bridge the pads in reflow.  DRC calls this solder_mask_bridge, and it is the
    #     one advisory class here that is a genuine manufacturing risk rather than cosmetics,
    #     so it is worth checking rather than downgrading blindly.
    pads_full = parse_pads(path)
    bridges: list[tuple[float, str]] = []

    for i, p in enumerate(pads_full):
        for q in pads_full[i + 1:]:
            if p.net and q.net and p.net == q.net:
                continue        # same net: a merged aperture is harmless
            if not ((p.on_front() and q.on_front()) or (p.on_back() and q.on_back())):
                continue        # not on a shared copper layer
            gap = max(b[0] - a[1] for a, b in (
                ((p.x - p.w / 2, p.x + p.w / 2), (q.x - q.w / 2, q.x + q.w / 2)),
                ((q.x - q.w / 2, q.x + q.w / 2), (p.x - p.w / 2, p.x + p.w / 2)),
                ((p.y - p.h / 2, p.y + p.h / 2), (q.y - q.h / 2, q.y + q.h / 2)),
                ((q.y - q.h / 2, q.y + q.h / 2), (p.y - p.h / 2, p.y + p.h / 2))))
            web = gap - 2 * MASK_EXPANSION
            if web < MASK_MIN_WEB - 1e-9:
                bridges.append((web, f"{p.ref}.{p.number} ({p.net or '-'}) "
                                     f"<-> {q.ref}.{q.number} ({q.net or '-'})"))

    if bridges:
        bridges.sort()
        print(f"\nSOLDER MASK BRIDGE ({len(bridges)} pair(s)):")
        for web, desc in bridges[:12]:
            print(f"      web {web:+.3f} mm  {desc}")
        problems += len(bridges)

    # 5. zero-length / stub segments (DRC calls these dangling)
    tiny = [s for s in segs if s.length() < 0.05]
    if tiny:
        print(f"\nZERO-LENGTH segments: {len(tiny)}")
        problems += len(tiny)

    print(f"\n{'CLEAN' if not problems else f'{problems} geometric problems'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
