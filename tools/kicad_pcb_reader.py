#!/usr/bin/env python3
"""Minimal, version-tolerant reader for KiCad .kicad_pcb files (KiCad 7 - 10 s-expression format).

Pure Python (no pcbnew), so it never touches KiCad's GUI layer and reads boards written by any KiCad
version or by tools that emit KiCad format (e.g. Quilter).  Coordinates are returned in nanometres,
in KiCad's frame (y down), exactly as KiCad computes them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


class Q(str):
    """A quoted string token (distinguishes "1" from the bare atom 1)."""


def parse_sexpr(text: str):
    stack, cur, pos, n = [], [], 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"bad s-expression at offset {pos}")
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done, cur = cur, stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(Q(re.sub(r"\\(.)", lambda e: _ESC.get(e.group(1), e.group(1)), m.group(3))))
        else:
            cur.append(m.group(4))
    return cur[0]


def nm(v) -> int:
    return round(float(v) * 1_000_000)


def kids(node, tag):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == tag]


def kid(node, tag):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def rotate(x: int, y: int, deg: float) -> tuple[int, int]:
    """KiCad RotatePoint: positive angle = counter-clockwise on screen (y down)."""
    if deg % 360 == 0:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return round(x * c + y * s), round(-x * s + y * c)


@dataclass
class Pad:
    number: str
    kind: str
    shape: str
    x: int
    y: int
    angle: float
    size: tuple[int, int]
    drill: tuple[int, int] | None
    layers: tuple[str, ...]
    net: str


@dataclass
class Footprint:
    ref: str
    value: str
    fpid: str
    x: int
    y: int
    angle: float
    layer: str
    locked: bool
    attrs: tuple[str, ...]
    pads: list[Pad] = field(default_factory=list)
    props: dict = field(default_factory=dict)
    zones: list = field(default_factory=list)  # footprint rule areas / zones (board coordinates)
    @property
    def flipped(self) -> bool:
        return self.layer == "B.Cu"


@dataclass
class Track:
    kind: str  # segment | arc
    start: tuple[int, int]
    end: tuple[int, int]
    mid: tuple[int, int] | None
    width: int
    layer: str
    net: str
    locked: bool


@dataclass
class Via:
    x: int
    y: int
    size: int
    drill: int
    layers: tuple[str, ...]
    net: str
    locked: bool
    kind: str


@dataclass
class Zone:
    layers: tuple[str, ...]
    net: str
    rule_area: bool
    outlines: list[list[tuple[int, int]]]
    name: str
    priority: int
    opts: dict = field(default_factory=dict)  # fill settings: min_thickness, clearance, connect, fill, ...


@dataclass
class Shape:
    kind: str
    layer: str
    pts: list[tuple[int, int]]
    width: int


@dataclass
class Board:
    version: int
    layers: dict
    nets: list[str]
    footprints: list[Footprint]
    tracks: list[Track]
    vias: list[Via]
    zones: list[Zone]
    shapes: list[Shape]

    def footprint(self, ref):
        return next((f for f in self.footprints if f.ref == ref), None)


def _xy(node):
    return (nm(node[1]), nm(node[2]))


def _at(node):
    at = kid(node, "at")
    if at is None:
        return 0, 0, 0.0
    return nm(at[1]), nm(at[2]), float(at[3]) if len(at) > 3 and not isinstance(at[3], list) else 0.0


def _yes(node, tag):
    k = kid(node, tag)
    if k is not None:
        return len(k) < 2 or k[1] in ("yes", "true")
    return tag in node[1:]


class _NetNames:
    def __init__(self, root):
        self.by_code = {}
        for n in kids(root, "net"):
            if len(n) >= 3:
                self.by_code[str(n[1])] = str(n[2])

    def name(self, node) -> str:
        n = kid(node, "net")
        if n is None:
            return ""
        if len(n) >= 3:
            return str(n[2])
        if len(n) == 2:
            v = n[1]
            if isinstance(v, Q):  # KiCad 10 style (net "NAME")
                return str(v)
            return self.by_code.get(str(v), "")
        return ""


def _layers(node) -> tuple[str, ...]:
    k = kid(node, "layers")
    if k is not None:
        return tuple(str(x) for x in k[1:])
    k = kid(node, "layer")
    return (str(k[1]),) if k is not None else ()


def _arc_geom(a, m, b):
    """Circle through start a, any point m on the arc, end b -> (cx, cy, r, start angle, signed sweep) or None."""
    (ax, ay), (mx, my), (bx, by) = a, m, b
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-9:
        return None
    cx = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / d
    cy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / d
    a0, am, a1 = (math.atan2(p[1] - cy, p[0] - cx) for p in (a, m, b))
    sweep = (a1 - a0) % math.tau
    if (am - a0) % math.tau > sweep:  # mid is not on the positive-angle path: go the other way
        sweep -= math.tau
    return cx, cy, math.hypot(ax - cx, ay - cy), a0, sweep


def arc_true_mid(a, m, b) -> tuple[int, int]:
    """The point halfway along the arc.  KiCad files may carry any point of the arc as 'mid' (tools that
    write arcs need not normalise it), so arcs are compared by this canonical midpoint."""
    g = _arc_geom(a, m, b)
    if g is None:
        return m
    cx, cy, r, a0, sweep = g
    return round(cx + r * math.cos(a0 + sweep / 2)), round(cy + r * math.sin(a0 + sweep / 2))


def arc_points(a, m, b, max_seg_deg: float = 5.0) -> list[tuple[int, int]]:
    """Polyline through the arc start a -> mid m -> end b (inclusive), at most max_seg_deg per segment."""
    g = _arc_geom(a, m, b)
    if g is None:
        return [a, b]
    cx, cy, r, a0, sweep = g
    n = max(2, math.ceil(abs(math.degrees(sweep)) / max_seg_deg))
    return [a] + [(round(cx + r * math.cos(a0 + sweep * i / n)), round(cy + r * math.sin(a0 + sweep * i / n)))
                  for i in range(1, n)] + [b]


def _pts(node) -> list[tuple[int, int]]:
    """(pts (xy ..) (arc (start ..) (mid ..) (end ..)) ..) -> points (arcs as polylines)."""
    out: list[tuple[int, int]] = []
    for p in (node[1:] if node is not None else []):
        if isinstance(p, list) and p and p[0] == "xy":
            out.append(_xy(p))
        elif isinstance(p, list) and p and p[0] == "arc":
            pts = arc_points(*(_xy(kid(p, t)) for t in ("start", "mid", "end")))
            out.extend(pts[1:] if out and out[-1] == pts[0] else pts)
    return out


def _zone_opts(node) -> dict:
    """Copper-fill settings of a zone (all lengths in nm)."""
    o: dict = {}
    k = kid(node, "min_thickness")
    if k is not None:
        o["min_thickness"] = nm(k[1])
    cp = kid(node, "connect_pads")
    if cp is not None:
        mode = next((str(t) for t in cp[1:] if not isinstance(t, list)), "thermal")
        o["connect"] = {"yes": "solid", "no": "none", "thru_hole_only": "thru_hole_only"}.get(mode, "thermal")
        c = kid(cp, "clearance")
        if c is not None:
            o["clearance"] = nm(c[1])
    f = kid(node, "fill")
    if f is not None:
        o["fill"] = "hatch" if kid(f, "mode") is not None and str(kid(f, "mode")[1]) == "hatch" else "solid"
        for t in ("thermal_gap", "thermal_bridge_width", "hatch_thickness", "hatch_gap"):
            k = kid(f, t)
            if k is not None:
                o[t] = nm(k[1])
        k = kid(f, "island_removal_mode")
        o["island_removal"] = int(k[1]) if k is not None else 0
    a = kid(node, "attr")
    if a is not None and kid(a, "teardrop") is not None:
        o["teardrop"] = True
    ko = kid(node, "keepout")
    if ko is not None:  # rule area restrictions: {"tracks": "not_allowed", "vias": ..., ...}
        o["keepout"] = {str(k[0]): str(k[1]) for k in ko[1:] if isinstance(k, list) and len(k) > 1}
    return o


def load_board(path: str | Path) -> Board:
    root = parse_sexpr(Path(path).read_text(encoding="utf-8"))
    if root[0] != "kicad_pcb":
        raise ValueError(f"{path} is not a .kicad_pcb file")
    names = _NetNames(root)
    ver = int(kid(root, "version")[1]) if kid(root, "version") else 0
    layers = {}
    if kid(root, "layers"):
        for l in kid(root, "layers")[1:]:
            if isinstance(l, list):
                layers[str(l[1])] = {"id": str(l[0]), "type": str(l[2]), "user": str(l[3]) if len(l) > 3 else ""}
    fps, tracks, vias, zones, shapes = [], [], [], [], []
    for node in root[1:]:
        if not isinstance(node, list) or not node:
            continue
        tag = node[0]
        if tag in ("footprint", "module"):
            fps.append(_footprint(node, names))
        elif tag in ("segment", "arc"):
            mid = kid(node, "mid")
            s, e = _xy(kid(node, "start")), _xy(kid(node, "end"))
            tracks.append(Track(tag, s, e, arc_true_mid(s, _xy(mid), e) if mid else None,
                                nm(kid(node, "width")[1]), str(kid(node, "layer")[1]), names.name(node),
                                _yes(node, "locked")))
        elif tag == "via":
            x, y, _a = _at(node)
            kind = next((str(t) for t in node[1:] if t in ("blind", "micro", "buried")), "through")
            vias.append(Via(x, y, nm(kid(node, "size")[1]), nm(kid(node, "drill")[1]), _layers(node),
                            names.name(node), _yes(node, "locked"), kind))
        elif tag == "zone":
            zones.append(_zone(node, names))
        elif tag.startswith("gr_") and tag not in ("gr_text", "gr_text_box"):
            shapes.append(_shape(node))
    return Board(ver, layers, [str(n[2]) for n in kids(root, "net") if len(n) >= 3], fps, tracks, vias, zones, shapes)


def _zone(node, names) -> Zone:
    outs = [_pts(kid(poly, "pts")) for poly in kids(node, "polygon")]
    nn = kid(node, "net_name")
    net = str(nn[1]) if nn is not None and len(nn) > 1 else names.name(node)
    nm_ = kid(node, "name")
    pr = kid(node, "priority")
    return Zone(_layers(node), net, kid(node, "keepout") is not None, outs,
                str(nm_[1]) if nm_ else "", int(pr[1]) if pr else 0, _zone_opts(node))


def _shape(node) -> Shape:
    kind = node[0][3:]
    pts = []
    for t in ("start", "mid", "center", "end"):
        k = kid(node, t)
        if k is not None:
            pts.append(_xy(k))
    if kid(node, "pts") is not None:
        pts = _pts(kid(node, "pts"))
    elif kind == "arc" and len(pts) == 3:
        pts[1] = arc_true_mid(*pts)
    st = kid(node, "stroke")
    w = kid(st, "width") if st is not None else kid(node, "width")
    layer = kid(node, "layer")
    return Shape(kind, str(layer[1]) if layer else "", pts, nm(w[1]) if w else 0)


def _footprint(node, names) -> Footprint:
    x, y, ang = _at(node)
    props = {}
    for p in kids(node, "property"):
        props[str(p[1])] = str(p[2]) if len(p) > 2 else ""
    for t in kids(node, "fp_text"):  # KiCad <= 7
        if t[1] in ("reference", "value"):
            props.setdefault(t[1].capitalize(), str(t[2]))
    attr = kid(node, "attr")
    fp = Footprint(props.get("Reference", ""), props.get("Value", ""), str(node[1]), x, y, ang,
                   str(kid(node, "layer")[1]), _yes(node, "locked"),
                   tuple(str(a) for a in attr[1:]) if attr else (), props=props)
    for p in kids(node, "pad"):
        px, py, pang = _at(p)
        dx, dy = rotate(px, py, ang)
        size = kid(p, "size")
        dr = kid(p, "drill")
        drill = None
        if dr is not None:
            vals = [v for v in dr[1:] if not isinstance(v, list)]
            if vals and vals[0] == "oval":
                drill = (nm(vals[1]), nm(vals[2]) if len(vals) > 2 else nm(vals[1]))
            elif vals:
                drill = (nm(vals[0]), nm(vals[0]))
        fp.pads.append(Pad(str(p[1]), str(p[2]), str(p[3]), x + dx, y + dy, pang,
                           (nm(size[1]), nm(size[2])) if size else (0, 0), drill, _layers(p), names.name(p)))
    fp.zones = [_zone(z, names) for z in kids(node, "zone")]
    return fp
