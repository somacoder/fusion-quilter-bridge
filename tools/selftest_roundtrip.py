#!/usr/bin/env python3
"""Self-test of the round trip on your own design, without Quilter.

Run with KiCad 9's Python (it provides pcbnew) after Fusion_to_Quilter:
  Selftest_Roundtrip.cmd          (or: <KiCad 9>\\bin\\python.exe tools\\selftest_roundtrip.py [--name <design>])

Everything happens in a scratch folder: input\\, to_quilter\\, from_quilter\\ and to_fusion\\ are not changed (the
log is written to to_quilter\\<name>\\logs\\selftest.log).  A stand-in for Quilter, built on pcbnew, produces
layouts that change everything a placer/router changes:

  1. identity      the board sent to Quilter comes back unchanged -> no edits, the .brd equals the Fusion export
  2. first layout  every part placed on the board (rotated, some flipped to the bottom), every net routed with
                   tracks, arcs and vias, three pours (solid / hatched, thermals / solid, orphans on / off)
  3. re-route      the routed .brd goes forward again (all forward checks, now on a routed board); then parts are
                   moved, rotated and flipped, nets ripped up and re-routed, a pour removed and another added

Each returned layout is checked three ways:
  * quilter_to_fusion's own check: KiCad's official EAGLE importer (KiCad 10) reads the new .brd, and it must
    equal the simulated layout (parts, pads, tracks, vias, outline, pours);
  * the .scr is executed by an EAGLE script interpreter on the exported .brd, and the result must equal the new
    .brd (parts, smashed texts, tracks, vias, pours), with the layers, grid and visibility restored at the end;
  * nothing else in the .brd changed: libraries, design rules, layers, drawings, attributes, net membership.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from eagle_common import mm_to_nm, parse_rot  # noqa: E402

MM = 1_000_000
COPPER = range(1, 17)
MIRROR_LAYER = {21: 22, 25: 26, 27: 28, 29: 30, 31: 32, 33: 34, 35: 36, 37: 38, 39: 40, 41: 42, 51: 52}
MIRROR_LAYER.update({b: t for t, b in list(MIRROR_LAYER.items())})


# ====================================================================== stand-in for Quilter (pcbnew)
def _pcbnew():
    import pcbnew

    try:  # KiCad reports notes through wxLog, which would show message boxes in a script
        import wx

        class _Quiet(wx.Log):
            def DoLogTextAtLevel(self, level, msg):
                pass

        _pcbnew.quiet = _Quiet()
        wx.Log.SetActiveTarget(_pcbnew.quiet)
        wx.DisableAsserts()  # no "wxWidgets Debug Alert" boxes
    except Exception:
        pass
    return pcbnew


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


class Router:
    """Deterministic 'router': chains the pads of a net with L-shaped tracks (some corners as arcs) and puts a via
    where the two pads are on different sides.  Geometry, not DRC, is what the round trip has to carry."""

    def __init__(self, pcbnew, board, seed=0):
        self.p, self.b, self.k = pcbnew, board, seed
        self.stats = Counter()

    def v(self, x, y):
        return self.p.VECTOR2I(int(x), int(y))

    def seg(self, a, c, layer, w, net):
        if a == c:
            return
        t = self.p.PCB_TRACK(self.b)
        t.SetStart(self.v(*a)), t.SetEnd(self.v(*c)), t.SetWidth(w), t.SetLayer(layer), t.SetNet(net)
        self.b.Add(t)
        self.stats["tracks"] += 1

    def arc(self, a, m, c, layer, w, net):
        t = self.p.PCB_ARC(self.b)
        t.SetStart(self.v(*a)), t.SetMid(self.v(*m)), t.SetEnd(self.v(*c)), t.SetWidth(w), t.SetLayer(layer)
        t.SetNet(net)
        self.b.Add(t)
        self.stats["arcs"] += 1

    def via(self, at, net):
        small = self.stats["vias"] % 4 == 3  # smaller than Fusion's restring rules: expect the 'enlarged' warning
        v = self.p.PCB_VIA(self.b)
        v.SetPosition(self.v(*at)), v.SetLayerPair(self.p.F_Cu, self.p.B_Cu), v.SetNet(net)
        v.SetDrill(300_000 if small else 350_000)
        v.SetWidth(600_000 if small else 800_000)
        self.b.Add(v)
        self.stats["vias"] += 1

    def side(self, pad):
        f, b = pad.IsOnLayer(self.p.F_Cu), pad.IsOnLayer(self.p.B_Cu)
        if f and b:
            return self.p.B_Cu if pad.GetParentFootprint().IsFlipped() else self.p.F_Cu
        return self.p.F_Cu if f else self.p.B_Cu

    def route(self, name):
        net = self.b.FindNet(name)
        pads = sorted((p for fp in self.b.GetFootprints() for p in fp.Pads()
                       if p.GetNetname() == name and p.GetAttribute() != self.p.PAD_ATTRIB_NPTH),
                      key=lambda p: (_natural(p.GetParentFootprint().GetReference()), _natural(p.GetNumber())))
        if len(pads) < 2:
            return
        def xy(pad):
            return pad.GetPosition().x, pad.GetPosition().y

        order, rest = [pads[0]], pads[1:]
        while rest:  # nearest neighbour chain
            last = xy(order[-1])
            nxt = min(rest, key=lambda p: math.dist(xy(p), last))
            rest.remove(nxt)
            order.append(nxt)
        w = 500_000 if len(pads) >= 4 else 250_000
        for pa, pb in zip(order, order[1:]):
            a, c = (pa.GetPosition().x, pa.GetPosition().y), (pb.GetPosition().x, pb.GetPosition().y)
            la, lc = self.side(pa), self.side(pb)
            self.k += 1
            if la == lc:
                dx, dy, r = c[0] - a[0], c[1] - a[1], 1_000_000
                if self.k % 3 == 1 and abs(dx) > 3 * r and abs(dy) > 3 * r:
                    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
                    p1, p2 = (c[0] - sx * r, a[1]), (c[0], a[1] + sy * r)
                    cx, cy = c[0] - sx * r, a[1] + sy * r
                    m = (round(cx + sx * r / math.sqrt(2)), round(cy - sy * r / math.sqrt(2)))
                    self.seg(a, p1, la, w, net), self.arc(p1, m, p2, la, w, net), self.seg(p2, c, la, w, net)
                else:
                    self.seg(a, (c[0], a[1]), la, w, net), self.seg((c[0], a[1]), c, la, w, net)
            else:
                mx = round((a[0] + c[0]) / 2 / 10_000) * 10_000
                if abs(mx - a[0]) < 1_000_000:
                    mx = a[0] + 2_000_000
                self.seg(a, (mx, a[1]), la, w, net)
                self.via((mx, a[1]), net)
                self.seg((mx, a[1]), (mx, c[1]), lc, w, net), self.seg((mx, c[1]), c, lc, w, net)


def _signal_nets(board):
    count = Counter(p.GetNetname() for fp in board.GetFootprints() for p in fp.Pads())
    return sorted((n for n in count if n and not n.startswith("unconnected-") and count[n] >= 2),
                  key=lambda n: (-count[n], n))


def _pour(pcbnew, board, net, layer, pts, name, priority=0, hatch=False, solid=False, orphans=False):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNet(board.FindNet(net))
    ol = z.Outline()
    ol.NewOutline()
    for x, y in pts:
        ol.Append(int(x), int(y))
    z.SetZoneName(name)
    z.SetAssignedPriority(priority)
    z.SetMinThickness(254_000)
    z.SetLocalClearance(300_000)
    if hatch:
        z.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
        z.SetHatchThickness(300_000)
        z.SetHatchGap(500_000)
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL if solid else pcbnew.ZONE_CONNECTION_THERMAL)
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER if orphans else pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    board.Add(z)


def _save(pcbnew, board, dst: Path):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    dst.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(dst), board)


def sim_first_layout(src: Path, dst: Path) -> str:
    p = _pcbnew()
    b = p.LoadBoard(str(src))
    bb = b.GetBoardEdgesBoundingBox()
    x0, y0, x1, y1 = bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()
    fps = sorted(b.GetFootprints(), key=lambda f: _natural(f.GetReference()))
    cols = max(1, math.ceil(math.sqrt(len(fps) * (x1 - x0) / max(y1 - y0, 1))))
    rows = math.ceil(len(fps) / cols)
    flipped = 0
    for i, fp in enumerate(fps):
        r, c = divmod(i, cols)
        x = round((x0 + (2 * c + 1) * (x1 - x0) / (2 * cols)) / 10_000) * 10_000
        y = round((y0 + (2 * r + 1) * (y1 - y0) / (2 * rows)) / 10_000) * 10_000
        fp.SetPosition(p.VECTOR2I(x, y))
        fp.SetOrientationDegrees((0, 90, 180, 270, 45, 0, 30)[i % 7])
        if i % 5 == 2:
            fp.SetLayerAndFlip(p.B_Cu)
            flipped += 1
    router = Router(p, b)
    nets = _signal_nets(b)
    for n in nets:
        router.route(n)
    m = 1_000_000
    pours = nets[:3]
    if pours:
        _pour(p, b, pours[0], p.B_Cu, [(x0 + m, y0 + m), (x1 - m, y0 + m), (x1 - m, y1 - m), (x0 + m, y1 - m)],
              pours[0])
    if len(pours) > 1:  # chamfered, hatched, higher priority
        xa, xb, ya, yb, ch = x0 + 2 * m, x0 + (x1 - x0) // 3, y0 + 2 * m, y1 - 2 * m, 5 * m
        _pour(p, b, pours[1], p.F_Cu, [(xa + ch, ya), (xb, ya), (xb, yb), (xa, yb), (xa, ya + ch)], pours[1],
              priority=1, hatch=True)
    if len(pours) > 2:
        xa, xb = x1 - (x1 - x0) // 3, x1 - 2 * m
        _pour(p, b, pours[2], p.F_Cu, [(xa, y0 + 2 * m), (xb, y0 + 2 * m), (xb, y1 - 2 * m), (xa, y1 - 2 * m)],
              pours[2], solid=True, orphans=True)
    _save(p, b, dst)
    s = router.stats
    return (f"{len(fps)} parts placed on the board ({flipped} flipped to the bottom, rotations 0/90/180/270/45/30), "
            f"{len(nets)} nets routed: {s['tracks']} tracks, {s['arcs']} arcs, {s['vias']} vias; "
            f"{len(pours)} pours ({', '.join(pours)})")


def sim_reroute(src: Path, dst: Path) -> str:
    p = _pcbnew()
    b = p.LoadBoard(str(src))
    fps = sorted(b.GetFootprints(), key=lambda f: _natural(f.GetReference()))
    pick = [fps[i % len(fps)] for i in (1, 4, 7, 10)]
    pick[0].Move(p.VECTOR2I(3_000_000, -2_000_000))
    pick[1].SetOrientationDegrees(pick[1].GetOrientationDegrees() + 90)
    pick[2].SetLayerAndFlip(p.F_Cu if pick[2].IsFlipped() else p.B_Cu)
    pick[3].Move(p.VECTOR2I(-1_500_000, 2_500_000))
    pick[3].SetOrientationDegrees(pick[3].GetOrientationDegrees() + 30)
    nets = _signal_nets(b)
    touched = {pad.GetNetname() for fp in pick for pad in fp.Pads()} & set(nets)
    extra = next((n for n in nets[3:] if n not in touched), None)  # an untouched net, re-routed anyway
    reroute = sorted(touched | ({extra} if extra else set()))
    gone = Counter()
    for t in list(b.GetTracks()):
        if t.GetNetname() in reroute:
            gone["vias" if t.GetClass() == "PCB_VIA" else "tracks"] += 1
            b.Remove(t)
    router = Router(p, b, seed=1)
    for n in reroute:
        router.route(n)
    removed = None
    if len(nets) > 1:
        removed = next((z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname() == nets[1]), None)
        if removed is not None:
            b.Remove(removed)
    bb = b.GetBoardEdgesBoundingBox()
    cx, cy, m = bb.GetCenter().x, bb.GetCenter().y, 1_000_000
    new = nets[3] if len(nets) > 3 else None
    if new:
        _pour(p, b, new, p.F_Cu, [(cx - 15 * m, cy - 10 * m), (cx + 15 * m, cy - 10 * m), (cx + 15 * m, cy + 10 * m),
                                  (cx - 15 * m, cy + 10 * m)], new, priority=2)
    _save(p, b, dst)
    s = router.stats
    return (f"moved {pick[0].GetReference()}, rotated {pick[1].GetReference()}, flipped {pick[2].GetReference()}, "
            f"moved+rotated {pick[3].GetReference()}; ripped up {len(reroute)} nets ({gone['tracks']} tracks, "
            f"{gone['vias']} vias) and re-routed them ({s['tracks']} tracks, {s['arcs']} arcs, {s['vias']} vias); "
            f"pour {nets[1] if removed is not None else '-'} removed, pour {new or '-'} added")


# ====================================================================== EAGLE script interpreter
def _nm(el, k) -> int:
    v = el.get(k)
    return mm_to_nm(v) if v not in (None, "") else 0


def _F(mirror: bool, deg: float):
    """EAGLE placement: rotate CCW by deg, then mirror x."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((-c, s), (s, c)) if mirror else ((c, -s), (s, c))


def _mul(A, B):
    return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(2)) for j in range(2)) for i in range(2))


def _T(A):
    return ((A[0][0], A[1][0]), (A[0][1], A[1][1]))


def _decompose(M) -> tuple[bool, float]:
    mirror = M[0][0] * M[1][1] - M[0][1] * M[1][0] < 0
    R = _mul(((-1, 0), (0, 1)), M) if mirror else M
    return mirror, round(math.degrees(math.atan2(R[1][0], R[0][0])), 6) % 360.0


def _rot(rot) -> tuple[bool, bool, float]:
    m, s, d = parse_rot(rot)
    return m, s, round(d, 6) % 360.0


def _rot_str(m, s, d) -> str:
    d = round(d, 6) % 360.0
    return ("S" if s else "") + ("M" if m else "") + "R" + (f"{d:.6f}".rstrip("0").rstrip(".") or "0")


def _commands(text: str) -> list[str]:
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    cmds, buf, quoted = [], [], False
    for ch in body:
        if ch == "'":
            quoted = not quoted
        if ch == ";" and not quoted:
            cmds.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        raise ValueError(f"unterminated command: {''.join(buf).strip()[:60]}")
    return [c for c in cmds if c]


_TOK = re.compile(r"'((?:[^']|'')*)'|\(([^)]*)\)|(\S+)")


def _tokens(cmd: str):
    out = []
    for m in _TOK.finditer(cmd):
        if m.group(1) is not None:
            out.append(("name", m.group(1).replace("''", "'")))
        elif m.group(2) is not None:
            out.append(("pt", m.group(2).split()))
        else:
            out.append(("word", m.group(3)))
    return out


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


class ScriptRunner:
    """Executes the commands kicad_to_fusion writes, with EAGLE's semantics, on the XML of a .brd:
    GRID, DISPLAY, SET, RIPUP, MOVE, ROTATE, LAYER, WIRE, CHANGE, VIA, POLYGON, DELETE (S x y), RATSNEST.
    Things that would go wrong in Fusion are reported as errors: RIPUP/MOVE/DELETE on hidden layers, MOVE
    dragging copper that was not ripped up, DELETE that would not pick exactly one polygon."""

    def __init__(self, brd: Path):
        self.tree = ET.parse(brd)
        root = self.tree.getroot()
        self.board = root.find("drawing/board")
        self.start_visible = {int(l.get("number")) for l in root.findall("drawing/layers/layer")
                              if l.get("visible") == "yes"}
        self.visible = set(self.start_visible)
        self.elements = {e.get("name"): e for e in self.board.findall("elements/element")}
        self.signals = {s.get("name"): s for s in self.board.findall("signals/signal")}
        self.layer = None
        self.drill = None
        self.chg = {}
        self.grid = []
        self.errors: list[str] = []
        self.count = Counter()

    def err(self, msg):
        self.errors.append(msg)

    def run(self, scr: Path):
        for cmd in _commands(scr.read_text(encoding="utf-8")):
            t = _tokens(cmd)
            verb = t[0][1].upper()
            self.count[verb] += 1
            getattr(self, "do_" + verb.lower(), self.unknown)(t[1:], cmd)
        if self.grid[:1] != ["MM FINEST"] or self.grid[-1:] != ["LAST"]:
            self.err(f"grid not set to finest first and restored last: {self.grid}")
        if self.visible != self.start_visible:
            self.err(f"visible layers not restored: {sorted(self.visible ^ self.start_visible)} differ")
        return self

    def unknown(self, _t, cmd):
        self.err(f"unknown command: {cmd[:60]}")

    def do_grid(self, t, _c):
        self.grid.append(" ".join(w for _k, w in t).upper())

    def do_set(self, _t, _c):
        pass

    def do_ratsnest(self, _t, _c):
        pass

    def do_display(self, t, _c):
        for _k, w in t:
            if w.upper() == "NONE":
                self.visible = set()
            elif w.isdigit():
                self.visible.add(int(w))
            elif w.startswith("-") and w[1:].isdigit():
                self.visible.discard(int(w[1:]))

    def do_layer(self, t, _c):
        self.layer = int(t[0][1])

    def do_change(self, t, _c):
        key, val = t[0][1].upper(), t[1][1]
        if key == "DRILL":
            self.drill = mm_to_nm(val)
        else:
            self.chg[key] = val

    def do_ripup(self, t, cmd):
        for _k, name in t:
            sig = self.signals.get(name)
            if sig is None:
                self.err(f"RIPUP: no signal {name}")
                continue
            for w in sig.findall("wire"):
                n = int(w.get("layer"))
                if n in COPPER:
                    if n in self.visible:
                        sig.remove(w)
                    else:
                        self.err(f"RIPUP {name}: wire on hidden layer {n} stays")
            for v in sig.findall("via"):
                if 18 in self.visible:
                    sig.remove(v)
                else:
                    self.err(f"RIPUP {name}: vias layer hidden")

    def place(self, el, x, y, rot):
        ox, oy = _nm(el, "x"), _nm(el, "y")
        om, _os, od = _rot(el.get("rot"))
        nm_, _ns, nd = _rot(rot)
        Fo, Fn = _F(om, od), _F(nm_, nd)
        R = _mul(Fn, _T(Fo))  # old placement -> new placement
        for a in el.findall("attribute"):
            if a.get("x") is None:
                continue
            vx, vy = _nm(a, "x") - ox, _nm(a, "y") - oy
            a.set("x", str((x + R[0][0] * vx + R[0][1] * vy) / MM))
            a.set("y", str((y + R[1][0] * vx + R[1][1] * vy) / MM))
            tm, ts, td = _rot(a.get("rot"))
            m2, d2 = _decompose(_mul(R, _F(tm, td)))
            a.set("rot", _rot_str(m2, ts, d2))
            if m2 != tm and a.get("layer"):
                a.set("layer", str(MIRROR_LAYER.get(int(a.get("layer")), int(a.get("layer")))))
        el.set("x", str(x / MM))
        el.set("y", str(y / MM))
        el.set("rot", rot)

    def _check_pick(self, el, verb):
        origin = 24 if _rot(el.get("rot"))[0] else 23
        if origin not in self.visible:
            self.err(f"{verb} {el.get('name')}: origin layer {origin} hidden, the part cannot be picked")
        for name, sig in self.signals.items():
            if any(cr.get("element") == el.get("name") for cr in sig.findall("contactref")) and \
                    (sig.find("via") is not None or any(int(w.get("layer")) in COPPER for w in sig.findall("wire"))):
                self.err(f"{verb} {el.get('name')}: would drag copper of signal {name}, which was not ripped up")

    def do_move(self, t, _c):
        el = self.elements.get(t[0][1])
        if el is None:
            return self.err(f"MOVE: no element {t[0][1]}")
        self._check_pick(el, "MOVE")
        x, y = (mm_to_nm(v) for v in t[1][1])
        self.place(el, x, y, el.get("rot") or "R0")

    def do_rotate(self, t, _c):
        rot, name = t[0][1], t[1][1]
        el = self.elements.get(name)
        if el is None or not rot.startswith("="):
            return self.err(f"ROTATE: unsupported form {rot} {name}")
        self._check_pick(el, "ROTATE")
        self.place(el, _nm(el, "x"), _nm(el, "y"), rot[1:])

    def _parent(self, name):
        if name is None:
            return self.board.find("plain")
        sig = self.signals.get(name)
        if sig is None:
            self.err(f"no signal {name}")
        return sig

    def do_wire(self, t, cmd):
        name = t[0][1] if t[0][0] == "name" else None
        rest = t[1:] if name is not None else t
        width, pts, curve = mm_to_nm(rest[0][1]), [], None
        for k, v in rest[1:]:
            if k == "pt":
                pts.append(tuple(mm_to_nm(c) for c in v))
            elif re.fullmatch(r"[+-][0-9.]+", v):
                curve = float(v)
        if len(pts) != 2:
            return self.err(f"WIRE with {len(pts)} points: {cmd[:60]}")
        w = ET.SubElement(self._parent(name), "wire", {
            "x1": str(pts[0][0] / MM), "y1": str(pts[0][1] / MM), "x2": str(pts[1][0] / MM),
            "y2": str(pts[1][1] / MM), "width": str(width / MM), "layer": str(self.layer)})
        if curve:
            w.set("curve", repr(curve))

    def do_via(self, t, _c):
        name, size, shape, ext, pt = t[0][1], t[1][1], t[2][1], t[3][1], t[4][1]
        if self.drill is None:
            self.err("VIA before CHANGE DRILL")
        attrs = {"x": str(mm_to_nm(pt[0]) / MM), "y": str(mm_to_nm(pt[1]) / MM), "extent": ext,
                 "drill": str((self.drill or 0) / MM), "diameter": str(mm_to_nm(size) / MM)}
        if shape.lower() != "round":
            attrs["shape"] = shape.lower()
        ET.SubElement(self._parent(name), "via", attrs)

    def do_polygon(self, t, cmd):
        name = t[0][1] if t[0][0] == "name" else None
        rest = t[1:] if name is not None else t
        pts = [tuple(mm_to_nm(c) for c in v) for k, v in rest[1:] if k == "pt"]
        if len(pts) < 4 or pts[0] != pts[-1]:
            return self.err(f"POLYGON not closed: {cmd[:60]}")
        c = self.chg
        attrs = {"width": str(mm_to_nm(rest[0][1]) / MM), "layer": str(self.layer),
                 "isolate": c.get("ISOLATE", "0"), "rank": c.get("RANK", "1"), "pour": c.get("POUR", "SOLID").lower(),
                 "thermals": "yes" if c.get("THERMALS", "ON").upper() == "ON" else "no",
                 "orphans": "yes" if c.get("ORPHANS", "OFF").upper() == "ON" else "no"}
        if attrs["pour"] == "hatch":
            attrs["spacing"] = c.get("SPACING", "")
        poly = ET.SubElement(self._parent(name), "polygon", attrs)
        for x, y in pts[:-1]:
            ET.SubElement(poly, "vertex", {"x": str(x / MM), "y": str(y / MM)})

    def do_delete(self, t, cmd):
        pt = t[0][1]
        if pt[0].upper() != "S":
            return self.err(f"DELETE without Shift: {cmd[:60]}")
        p = (mm_to_nm(pt[1]), mm_to_nm(pt[2]))
        hits, others = [], []
        parents = [*self.signals.values(), self.board.find("plain")]
        for par in parents:
            for w in par.findall("wire"):
                if int(w.get("layer")) in self.visible:
                    d = _seg_dist(p, (_nm(w, "x1"), _nm(w, "y1")), (_nm(w, "x2"), _nm(w, "y2"))) - _nm(w, "width") / 2
                    others.append(d)
            if 18 in self.visible:
                others += [math.dist(p, (_nm(v, "x"), _nm(v, "y"))) for v in par.findall("via")]
            for poly in par.findall("polygon"):
                if int(poly.get("layer")) not in self.visible:
                    continue
                vs = [(_nm(v, "x"), _nm(v, "y")) for v in poly.findall("vertex")]
                d = min(_seg_dist(p, vs[i - 1], vs[i]) for i in range(len(vs))) - _nm(poly, "width") / 2
                if d <= 1_000:
                    hits.append((par, poly))
                else:
                    others.append(d)
        for e in self.elements.values():
            if (24 if _rot(e.get("rot"))[0] else 23) in self.visible:
                others.append(math.dist(p, (_nm(e, "x"), _nm(e, "y"))))
        near = [d for d in others if d < 250_000]
        if len(hits) != 1 or near:
            return self.err(f"DELETE {pt}: {len(hits)} polygon(s) on the point and {len(near)} other object(s) "
                            "within 0.25 mm")
        hits[0][0].remove(hits[0][1])


# ====================================================================== comparisons
def _norm_rot(rot):
    m, s, d = _rot(rot)
    return m, s, round(d, 4) % 360.0


def _poly_key(p):
    vs = [(_nm(v, "x"), _nm(v, "y"), round(float(v.get("curve") or 0), 3)) for v in p.findall("vertex")]
    i = vs.index(min(vs))
    hatch = (p.get("pour") or "solid") == "hatch"
    return (int(p.get("layer")), _nm(p, "width"), _nm(p, "isolate"), int(p.get("rank") or 1), p.get("pour") or "solid",
            p.get("thermals") or "yes", p.get("orphans") or "no", _nm(p, "spacing") if hatch else 0,
            tuple(vs[i:] + vs[:i]))


def _wire_key(w):
    a, b, c = (_nm(w, "x1"), _nm(w, "y1")), (_nm(w, "x2"), _nm(w, "y2")), round(float(w.get("curve") or 0), 3)
    if b < a:
        a, b, c = b, a, -c
    return a, b, c, _nm(w, "width"), int(w.get("layer"))


def copper_state(board) -> dict:
    out = {}
    for par in [*board.findall("signals/signal"), board.find("plain")]:
        if par is None:
            continue
        name = par.get("name", "(no signal)")
        out[name] = (Counter(_wire_key(w) for w in par.findall("wire") if int(w.get("layer")) in COPPER),
                     Counter((_nm(v, "x"), _nm(v, "y"), _nm(v, "drill"), _nm(v, "diameter"), v.get("extent") or "1-16",
                              v.get("shape") or "round") for v in par.findall("via")),
                     Counter(_poly_key(p) for p in par.findall("polygon") if int(p.get("layer")) in COPPER))
    return out


def placement_state(board) -> dict:
    out = {}
    for e in board.findall("elements/element"):
        texts = sorted((a.get("name"), _nm(a, "x"), _nm(a, "y"), _norm_rot(a.get("rot")), a.get("layer"))
                       for a in e.findall("attribute") if a.get("x") is not None)
        out[e.get("name")] = (_nm(e, "x"), _nm(e, "y"), _norm_rot(e.get("rot")), texts)
    return out


def _texts_close(a, b, tol=5) -> bool:  # smashed texts: float math in two implementations, allow 5 nm
    return len(a) == len(b) and all(
        x[0] == y[0] and x[4] == y[4] and abs(x[1] - y[1]) <= tol and abs(x[2] - y[2]) <= tol and
        x[3][:2] == y[3][:2] and abs((x[3][2] - y[3][2] + 180) % 360 - 180) < 1e-3 for x, y in zip(a, b))


def compare_script(scr: Path, sent_brd: Path, new_brd: Path) -> bool:
    runner = ScriptRunner(sent_brd).run(scr)
    got_b, want_b = runner.board, ET.parse(new_brd).getroot().find("drawing/board")
    bad = list(runner.errors)
    gp, wp = placement_state(got_b), placement_state(want_b)
    for k in sorted(set(gp) | set(wp)):
        g, w = gp.get(k), wp.get(k)
        if g is None or w is None or g[:2] != w[:2] or g[2][:2] != w[2][:2] or \
                abs((g[2][2] - w[2][2] + 180) % 360 - 180) > 1e-3 or not _texts_close(g[3], w[3]):
            bad.append(f"part {k}: script gives {g}, .brd has {w}")
    gc, wc = copper_state(got_b), copper_state(want_b)
    for k in sorted(set(gc) | set(wc)):
        for what, g, w in zip(("tracks", "vias", "pours"), gc.get(k, ({}, {}, {})), wc.get(k, ({}, {}, {}))):
            if Counter(g) != Counter(w):
                bad.append(f"signal {k}: {what} differ (script {sum(Counter(g).values())}, "
                           f".brd {sum(Counter(w).values())})")
    cmds = ", ".join(f"{v} {k}" for k, v in sorted(runner.count.items(), key=lambda kv: -kv[1]))
    if bad:
        print(f"FAIL  .scr executed on the exported .brd != new .brd: {len(bad)} difference(s)")
        for b in bad[:20]:
            print("      " + b)
        return False
    n_tr = sum(sum(c[0].values()) for c in wc.values())
    n_v = sum(sum(c[1].values()) for c in wc.values())
    n_p = sum(sum(c[2].values()) for c in wc.values())
    n_t = sum(len(v[3]) for v in wp.values())
    print(f"PASS  .scr executed on the exported .brd == new .brd: {len(wp)} parts, {n_t} smashed texts, {n_tr} tracks, "
          f"{n_v} vias, {n_p} pours; grid, layers and visibility restored; no hidden-layer pick, no dragged copper, "
          f"every DELETE picks exactly one pour  [{cmds}]")
    return True


def _canon(el) -> str:
    el = ET.fromstring(ET.tostring(el))
    for x in el.iter():
        x.text = (x.text or "").strip() or None
        x.tail = None
    return ET.tostring(el, encoding="unicode")


def compare_rest(sent_brd: Path, new_brd: Path) -> bool:
    """Everything the return path must not touch is unchanged."""
    a, b = ET.parse(sent_brd).getroot(), ET.parse(new_brd).getroot()
    bad = []
    if a.attrib != b.attrib:
        bad.append("<eagle> attributes")
    da, db = a.find("drawing"), b.find("drawing")
    for ca, cb in zip(da, db):
        if ca.tag != "board" and _canon(ca) != _canon(cb):
            bad.append(f"drawing/{ca.tag}")
    ba, bb = da.find("board"), db.find("board")
    if [c.tag for c in ba] != [c.tag for c in bb]:
        bad.append("board sections")
    for ca, cb in zip(ba, bb):
        if ca.tag in ("elements", "signals"):
            continue
        if ca.tag == "plain":
            ca, cb = (ET.fromstring(ET.tostring(c)) for c in (ca, cb))
            for c in (ca, cb):
                for x in [x for x in c if x.tag in ("wire", "polygon") and int(x.get("layer")) in COPPER]:
                    c.remove(x)
        if _canon(ca) != _canon(cb):
            bad.append(f"board/{ca.tag}")
    ea = {e.get("name"): e for e in ba.findall("elements/element")}
    eb = {e.get("name"): e for e in bb.findall("elements/element")}
    if list(ea) != list(eb):
        bad.append("element list")
    for n, e in ea.items():
        f = eb.get(n)
        if f is None:
            continue
        if {k: v for k, v in e.attrib.items() if k not in ("x", "y", "rot")} != \
                {k: v for k, v in f.attrib.items() if k not in ("x", "y", "rot")}:
            bad.append(f"element {n}: attributes other than position/rotation")
        ka, kb = list(e), list(f)
        if [c.tag for c in ka] != [c.tag for c in kb] or any(
                {k: v for k, v in x.attrib.items() if k not in ("x", "y", "rot", "layer")} !=
                {k: v for k, v in y.attrib.items() if k not in ("x", "y", "rot", "layer")} for x, y in zip(ka, kb)):
            bad.append(f"element {n}: attributes/variants")
    sa = {s.get("name"): s for s in ba.findall("signals/signal")}
    sb = {s.get("name"): s for s in bb.findall("signals/signal")}
    if list(sa) != list(sb):
        bad.append("signal list")
    for n, s in sa.items():
        t = sb.get(n)
        if t is None:
            continue
        if s.attrib != t.attrib:
            bad.append(f"signal {n}: attributes (net class)")
        if sorted(_canon(c) for c in s.findall("contactref")) != sorted(_canon(c) for c in t.findall("contactref")):
            bad.append(f"signal {n}: pad connections")
        if sorted(_canon(w) for w in s.findall("wire") if int(w.get("layer")) not in COPPER) != \
                sorted(_canon(w) for w in t.findall("wire") if int(w.get("layer")) not in COPPER):
            bad.append(f"signal {n}: non-copper wires")
    if bad:
        print(f"FAIL  parts of the .brd the return path must not touch changed: {bad[:10]}")
        return False
    print(f"PASS  everything else in the .brd is unchanged: {len(list(db))} drawing sections, libraries, design rules, "
          f"layers, drawings, {len(ea)} part definitions/attributes, {len(sa)} signals with their pad connections")
    return True


# ====================================================================== driver
def section(title):
    print(f"\n=== {title}")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def return_and_check(tq: Path, pcb: Path, out: Path, name: str, inbox: Path) -> bool:
    import quilter_to_fusion

    rc = quilter_to_fusion.main([str(pcb), "--name", name, "--to-quilter", str(tq), "--from-quilter", str(inbox),
                                 "--out", str(out), "--input", str(tq / name / "fusion_source")])
    print()
    ok = rc == 0
    print(f"{'PASS' if ok else 'FAIL'}  Quilter_to_Fusion exit code {rc}")
    sent = tq / name / "fusion_source" / f"{name}.brd"
    scr, brd = out / f"{pcb.stem}.scr", out / f"{pcb.stem}.brd"
    ok &= compare_script(scr, sent, brd)
    ok &= compare_rest(sent, brd)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="design in to_quilter (default: the only one)")
    ap.add_argument("--to-quilter", type=Path, default=ROOT / "to_quilter")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder")
    a = ap.parse_args(argv)
    from verify_forward import find_kicad10_cli

    if find_kicad10_cli() is None:
        raise SystemExit("The round-trip self-test requires KiCad 10's CLI for independent verification. "
                         "Install KiCad 10 or set KICAD10_CLI to its kicad-cli executable.")
    projs = sorted(p.parent for p in a.to_quilter.glob("*/*.board_map.json"))
    if a.name:
        projs = [p for p in projs if p.name == a.name]
    if len(projs) != 1:
        raise SystemExit(f"run Fusion_to_Quilter first, or choose a design with --name (found {[p.name for p in projs]})")
    proj = projs[0]
    name = proj.name
    log = proj / "logs" / "selftest.log"
    log.parent.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="quilter_selftest_"))
    results = {}
    with open(log, "w", encoding="utf-8") as fh:
        out = sys.stdout
        sys.stdout = Tee(out, fh)
        try:
            print(f"Self-test of the round trip for {name} (scratch folder {scratch})")
            tq1 = scratch / "to_quilter"
            shutil.copytree(proj, tq1 / name, ignore=shutil.ignore_patterns("upload", "logs"))
            sent_pcb = tq1 / name / f"{name}.kicad_pcb"
            sent_brd = tq1 / name / "fusion_source" / f"{name}.brd"

            section("1. identity: the board sent to Quilter comes back unchanged")
            inbox = scratch / "from_quilter_1"
            inbox.mkdir()
            same = inbox / f"{name}_unchanged.kicad_pcb"
            shutil.copy2(sent_pcb, same)
            ok = return_and_check(tq1, same, scratch / "to_fusion_1", name, inbox)
            scr_lines = [l for l in (scratch / "to_fusion_1" / f"{same.stem}.scr").read_text().splitlines()
                         if l.strip() and not l.startswith("#")]
            edits = [l for l in scr_lines if not re.match(r"(GRID|DISPLAY|SET|RATSNEST)\b", l)]
            back = scratch / "to_fusion_1" / f"{same.stem}.brd"
            same_xml = ET.canonicalize(from_file=str(back)) == ET.canonicalize(from_file=str(sent_brd))
            if edits or not same_xml:
                ok = False
                print(f"FAIL  unchanged board: {len(edits)} edit command(s); .brd XML identical to the export: {same_xml}")
            else:
                print("PASS  unchanged board: the script makes no edits and the .brd is XML-identical to the export "
                      "(canonical XML; only the spacing inside some empty tags differs)")
            results["1. identity"] = ok

            section("2. first layout: placement from scratch, full routing, pours")
            inbox = scratch / "from_quilter_2"
            lay1 = inbox / f"{name}_layout1.kicad_pcb"
            print("simulated Quilter result: " + sim_first_layout(sent_pcb, lay1))
            results["2. first layout"] = return_and_check(tq1, lay1, scratch / "to_fusion_2", name, inbox)

            section("3. second pass: the routed board goes forward again, then is edited and comes back")
            import fusion_to_quilter

            src2 = scratch / "fusion_export_2"
            src2.mkdir()
            shutil.copy2(scratch / "to_fusion_2" / f"{lay1.stem}.brd", src2 / f"{name}.brd")
            shutil.copy2(tq1 / name / "fusion_source" / f"{name}.sch", src2 / f"{name}.sch")
            tq2 = scratch / "to_quilter_2"
            rc = fusion_to_quilter.main(["--sch", str(src2 / f"{name}.sch"), "--brd", str(src2 / f"{name}.brd"),
                                         "--out", str(tq2)])
            ok = rc == 0
            print(f"\n{'PASS' if ok else 'FAIL'}  Fusion_to_Quilter on the routed board: exit code {rc}")
            inbox = scratch / "from_quilter_3"
            lay2 = inbox / f"{name}_layout2.kicad_pcb"
            print("simulated Quilter result: " + sim_reroute(tq2 / name / f"{name}.kicad_pcb", lay2))
            ok &= return_and_check(tq2, lay2, scratch / "to_fusion_3", name, inbox)
            results["3. second pass"] = ok
        finally:
            section("summary")
            for k, v in results.items():
                print(f"{'PASS' if v else 'FAIL'}  {k}")
            all_ok = len(results) == 3 and all(results.values())
            print("ALL ROUND-TRIP TESTS PASSED" if all_ok else "ROUND-TRIP TESTS FAILED")
            print(f"log: {log}")
            sys.stdout = out
            if a.keep:
                print(f"scratch kept: {scratch}")
            else:
                shutil.rmtree(scratch, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
