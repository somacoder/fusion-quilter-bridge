#!/usr/bin/env python3
"""Independent verification of the Fusion/EAGLE -> KiCad conversion.

Uses KiCad's own engine (kicad-cli) and the original EAGLE XML as the judges:
  * schematic netlist exported by kicad-cli  ==  EAGLE schematic pinrefs (eagle_map.json expected_nets)
  * schematic netlist                         ==  EAGLE board <contactref>s (every placed element / pad)
  * KiCad board (if given): every pad net     ==  EAGLE board <contactref>s
  * every EAGLE signal wire / via / polygon exists on the KiCad board (geometry, width/drill, layer, net)
  * kicad-cli pcb drc --schematic-parity      ->  no parity violations, and a valid closed board outline
  * --official-import: placement, pads, tracks, vias, copper zones, rule areas and outline equal KiCad 10's
    official EAGLE importer (known, documented importer differences are reported as INFO); with EAGLE
    milling the outline must equal the official outline minus the milled areas
Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagle_common import Rules, arc_mid_and_center, kicad_net_name, kicad_ref_map, outline_spurs, parse_rot  # noqa: E402
from kicad_pcb_reader import load_board  # noqa: E402


def find_kicad_cli(explicit: str | None = None) -> str:
    cands = [explicit, os.environ.get("KICAD_CLI"), shutil.which("kicad-cli")]
    for ver in ("9.0", "10.0"):
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
            sub = "Programs\\KiCad" if base == os.environ.get("LOCALAPPDATA") else "KiCad"
            cands.append(os.path.join(base, sub, ver, "bin", "kicad-cli.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("kicad-cli not found; install KiCad 9+ or set KICAD_CLI")


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p.stdout


KICAD_ESCAPES = {"{slash}": "/", "{backslash}": "\\", "{lt}": "<", "{gt}": ">", "{colon}": ":",
                 "{dblquote}": '"', "{quote}": "'", "{space}": " ", "{tab}": "\t"}


def unescape(name: str) -> str:
    for k, v in KICAD_ESCAPES.items():
        name = name.replace(k, v)
    return name


def partition(nets: dict) -> dict:
    """{net: {(ref,pad)}} without empty / single-pad unconnected nets -> comparable dict."""
    return {unescape(n): frozenset(v) for n, v in nets.items() if v and not n.startswith("unconnected-")}


def kicad_sch_nets(cli: str, sch: Path, refs: set | None = None) -> dict:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "net.xml"
        run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(sch)])
        root = ET.parse(out).getroot()
    nets = {}
    for n in root.find("nets"):
        nodes = {(x.get("ref"), x.get("pin")) for x in n.findall("node")}
        if refs is not None:
            nodes = {x for x in nodes if x[0] in refs}
        nets[n.get("name")] = nodes
    return nets


def eagle_board_nets(brd: Path, refs: dict | None = None) -> tuple[dict, set, set]:
    """-> (nets, elements, pad-less elements that EAGLE allows on the board only, e.g. logos), with element
    names translated to KiCad references (kicad_ref_map + the converter's recorded renames)."""
    root = ET.parse(brd).getroot()
    names = [e.get("name") for e in root.findall("drawing/board/elements/element")]
    rmap = kicad_ref_map(names)
    rmap.update(refs or {})
    nets = defaultdict(set)
    for s in root.findall("drawing/board/signals/signal"):
        for c in s.findall("contactref"):
            nets[kicad_net_name(s.get("name"))].add((rmap.get(c.get("element"), c.get("element")), c.get("pad")))
    padded = set()
    for lib in root.findall("drawing/board/libraries/library"):
        for pkg in lib.findall("packages/package"):
            if pkg.find("pad") is not None or pkg.find("smd") is not None:
                padded.add((lib.get("name"), lib.get("urn") or "", pkg.get("name")))
    elements, padless = set(), set()
    for e in root.findall("drawing/board/elements/element"):
        ref = rmap.get(e.get("name"), e.get("name"))
        elements.add(ref)
        if (e.get("library"), e.get("library_urn") or "", e.get("package")) not in padded:
            padless.add(ref)
    return nets, elements, padless


def kicad_board_nets(pcb: Path) -> dict:
    nets = defaultdict(set)
    for fp in load_board(pcb).footprints:
        for pad in fp.pads:
            if pad.net and pad.number:
                nets[pad.net].add((fp.ref, pad.number))
    return nets


def compare(label: str, got: dict, want: dict) -> bool:
    g, w = partition(got), partition(want)
    if g == w:
        print(f"PASS  {label}: {len(g)} nets identical (names and members)")
        return True
    print(f"FAIL  {label}: {len(g)} vs {len(w)} nets")
    by_members_w = {v: k for k, v in w.items()}
    for n, v in sorted(g.items()):
        if w.get(n) == v:
            continue
        if v in by_members_w:
            print(f"   name differs: {n!r} vs {by_members_w[v]!r}")
        else:
            print(f"   {n}: got {sorted(v)}; want {sorted(w.get(n, []))}")
    for n in sorted(set(w) - set(g)):
        if w[n] not in g.values():
            print(f"   missing net {n}: {sorted(w[n])}")
    return False


def find_kicad10_cli(explicit: str | None = None) -> str | None:
    cands = [explicit, os.environ.get("KICAD10_CLI")]
    for base, sub in ((os.environ.get("LOCALAPPDATA", ""), "Programs\\KiCad"), (os.environ.get("ProgramFiles", ""), "KiCad")):
        for ver in ("10.0", "11.0"):
            cands.append(os.path.join(base, sub, ver, "bin", "kicad-cli.exe"))
    return next((c for c in cands if c and os.path.isfile(c)), None)


def _um(p):
    return (round(p[0] / 1000), round(p[1] / 1000))


def zone_outline_key(outlines):
    rings = []
    for outline in outlines:
        points = []
        for point in map(_um, outline):
            if not points or point != points[-1]:
                points.append(point)
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if points:
            variants = []
            for ring in (points, points[::-1]):
                start = min(ring)
                variants.extend(tuple(ring[i:] + ring[:i])
                                for i, point in enumerate(ring) if point == start)
            rings.append(min(variants))
    return tuple(sorted(rings))


def _mm_nm(v) -> int:
    return round(float(v) * 1e6)


def _copper_layer_names(board) -> list[str]:
    return [k for k in board.layers if k.endswith(".Cu")]


def eagle_copper_check(brd: Path, ours) -> bool:
    """Direct, importer-independent check: every EAGLE signal wire / via / polygon of the .brd exists
    in our board with identical geometry, width, drill and net, on the KiCad layer that carries the
    same EAGLE layer (F.Cu = Top, B.Cu = Bottom, inner layers by their EAGLE layer name)."""
    root = ET.parse(brd).getroot()
    ename = {int(l.get("number")): l.get("name") for l in root.findall("drawing/layers/layer")}
    kl = {"F.Cu": 1, "B.Cu": 16}
    for k, v in ours.layers.items():
        if k.startswith("In") and k.endswith(".Cu"):
            n = next((num for num, nme in ename.items() if 1 < num < 16 and nme == v["user"]), None)
            if n is not None:
                kl[k] = n
    want_t, want_v, want_z = defaultdict(int), defaultdict(int), defaultdict(int)
    for s in root.findall("drawing/board/signals/signal"):
        net = unescape(kicad_net_name(s.get("name")))
        for w in s.findall("wire"):
            n = int(w.get("layer"))
            if not 1 <= n <= 16:
                continue
            a = (_mm_nm(w.get("x1")), -_mm_nm(w.get("y1")))
            b = (_mm_nm(w.get("x2")), -_mm_nm(w.get("y2")))
            if a == b:
                continue
            want_t[(tuple(sorted([_um(a), _um(b)])), round(_mm_nm(w.get("width")) / 1000), n, net)] += 1
        for v in s.findall("via"):
            want_v[(_um((_mm_nm(v.get("x")), -_mm_nm(v.get("y")))), round(_mm_nm(v.get("drill")) / 1000),
                    v.get("extent"), net)] += 1
        for p in s.findall("polygon"):
            n = int(p.get("layer"))
            if 1 <= n <= 16 and p.get("pour") != "cutout":
                want_z[(n, net)] += 1
    got_t, got_v, got_z = defaultdict(int), defaultdict(int), defaultdict(int)
    for t in ours.tracks:
        got_t[(tuple(sorted([_um(t.start), _um(t.end)])), round(t.width / 1000), kl.get(t.layer), unescape(t.net))] += 1
    used = sorted({k[2] for k in want_t} | {int(e) for k in want_v for e in (k[2] or "1-16").split("-")})
    for v in ours.vias:
        ls = sorted(kl.get(l, -1) for l in v.layers) if v.layers and "*.Cu" not in v.layers else []
        ext = f"{ls[0]}-{ls[-1]}" if len(ls) == 2 else "1-16"
        got_v[(_um((v.x, v.y)), round(v.drill / 1000), ext, unescape(v.net))] += 1
    want_v = {(p, d, e if e != f"{used[0]}-{used[-1]}" else "1-16", n): c for (p, d, e, n), c in want_v.items()} \
        if used else want_v
    for z in ours.zones:
        if not z.rule_area and len(z.layers) == 1:
            got_z[(kl.get(z.layers[0]), unescape(z.net))] += 1
    ok = True
    for label, want, got in (("tracks", want_t, got_t), ("vias", want_v, got_v), ("copper polygons", want_z, got_z)):
        miss = {k: c - got.get(k, 0) for k, c in want.items() if got.get(k, 0) < c}
        extra = {k: c - want.get(k, 0) for k, c in got.items() if want.get(k, 0) < c}
        if miss or extra:
            ok = False
            print(f"FAIL  {label} vs EAGLE board: {sum(miss.values())} missing, {sum(extra.values())} unexpected")
            for k in list(miss)[:4]:
                print("     missing:   ", k)
            for k in list(extra)[:4]:
                print("     unexpected:", k)
        else:
            print(f"PASS  {label} vs EAGLE board: {sum(want.values())} identical (geometry, width/drill, "
                  f"EAGLE layer, net)")
    return ok


TAU = 2 * math.pi


def _circumcenter(a, m, b):
    (ax, ay), (bx, by), (cx, cy) = a, m, b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    s1, s2, s3 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    return ((s1 * (by - cy) + s2 * (cy - ay) + s3 * (ay - by)) / d,
            (s1 * (cx - bx) + s2 * (ax - cx) + s3 * (bx - ax)) / d)


def _arc(a, m, b):
    """Arc through start/mid/end -> ("arc", center, radius, start angle, signed sweep)."""
    c = _circumcenter(a, m, b)
    if c is None:
        return ("seg", a, b)
    ang = [math.atan2(p[1] - c[1], p[0] - c[0]) for p in (a, m, b)]
    ccw = (ang[1] - ang[0]) % TAU <= (ang[2] - ang[0]) % TAU
    sweep = (ang[2] - ang[0]) % TAU if ccw else -((ang[0] - ang[2]) % TAU)
    return ("arc", c, math.dist(a, c), ang[0], sweep)


def _shape_prims(s) -> list:
    """kicad_pcb_reader Shape -> drawing primitives (segments, arcs, circles) in nm."""
    p = s.pts
    if s.kind in ("line", "segment") and len(p) >= 2:
        return [("seg", p[0], p[1])]
    if s.kind == "arc" and len(p) >= 3:
        return [_arc(p[0], p[1], p[2])]
    if s.kind == "circle" and len(p) >= 2:
        return [("circle", p[0], math.dist(p[0], p[1]))]
    if s.kind == "rect" and len(p) >= 2:
        (x1, y1), (x2, y2) = p[0], p[1]
        p = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return [("seg", p[i], p[(i + 1) % len(p)]) for i in range(len(p))] if len(p) >= 2 else []


def _prim_dist(q, pr) -> float:
    if pr[0] == "seg":
        (ax, ay), (bx, by) = pr[1], pr[2]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((q[0] - ax) * dx + (q[1] - ay) * dy) / L2))
        return math.hypot(q[0] - ax - t * dx, q[1] - ay - t * dy)
    if pr[0] == "circle":
        return abs(math.dist(q, pr[1]) - pr[2])
    _, c, r, t0, sw = pr
    t = math.atan2(q[1] - c[1], q[0] - c[0])
    rel = (t - t0) % TAU if sw >= 0 else (t0 - t) % TAU
    if rel <= abs(sw):
        return abs(math.dist(q, c) - r)
    ends = [(c[0] + r * math.cos(a), c[1] + r * math.sin(a)) for a in (t0, t0 + sw)]
    return min(math.dist(q, e) for e in ends)


def _prim_samples(pr, step):
    if pr[0] == "seg":
        (ax, ay), (bx, by) = pr[1], pr[2]
        n = max(1, math.ceil(math.hypot(bx - ax, by - ay) / step))
        return [(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n + 1)]
    if pr[0] == "circle":
        c, r = pr[1], pr[2]
        n = max(16, math.ceil(TAU * r / step))
        return [(c[0] + r * math.cos(TAU * i / n), c[1] + r * math.sin(TAU * i / n)) for i in range(n)]
    _, c, r, t0, sw = pr
    n = max(4, math.ceil(abs(sw) * r / step))
    return [(c[0] + r * math.cos(t0 + sw * i / n), c[1] + r * math.sin(t0 + sw * i / n)) for i in range(n + 1)]


def _prim_bbox(pr):
    if pr[0] == "seg":
        (ax, ay), (bx, by) = pr[1], pr[2]
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
    c, r = pr[1], pr[2]
    return c[0] - r, c[1] - r, c[0] + r, c[1] + r


class _Near:
    """Distance from a point to the nearest of many primitives (1 mm bucket grid; inf beyond `reach`)."""

    def __init__(self, prims, reach, cell=1_000_000):
        self.cell, self.grid = cell, defaultdict(list)
        for pr in prims:
            x1, y1, x2, y2 = _prim_bbox(pr)
            for i in range(int((x1 - reach) // cell), int((x2 + reach) // cell) + 1):
                for j in range(int((y1 - reach) // cell), int((y2 + reach) // cell) + 1):
                    self.grid[(i, j)].append(pr)

    def __call__(self, q):
        cand = self.grid.get((int(q[0] // self.cell), int(q[1] // self.cell)), ())
        return min((_prim_dist(q, pr) for pr in cand), default=math.inf)


def eagle_element_T(e):
    """EAGLE element -> function mapping package (x, y) to board (x, y) in mm.  EAGLE rotates first, then
    mirrors x for 'M' parts (the convention of KiCad's importer, which pads are checked against)."""
    mirror, _spin, deg = parse_rot(e.get("rot"))
    ex, ey, th = float(e.get("x")), float(e.get("y")), math.radians(deg)

    def T(x, y):
        rx, ry = x * math.cos(th) - y * math.sin(th), x * math.sin(th) + y * math.cos(th)
        return ex + (-rx if mirror else rx), ey + ry
    return T, mirror


def eagle_milling_paths(root) -> list:
    """EAGLE Milling (layer 46) cutter paths with a width, plain and inside placed packages, in KiCad board
    coordinates (nm): [(primitive, cutter radius, is_circle)].  A milling circle removes its whole disc."""
    out = []

    def add(el, T, mirror):
        w = float(el.get("width") or 0) * 1e6
        if w <= 0 or el.tag not in ("wire", "circle"):
            return
        k = lambda x, y: (x * 1e6, -y * 1e6)  # noqa: E731
        if el.tag == "circle":
            out.append((("circle", k(*T(float(el.get("x")), float(el.get("y")))), float(el.get("radius")) * 1e6),
                        w / 2, True))
            return
        a = T(float(el.get("x1")), float(el.get("y1")))
        b = T(float(el.get("x2")), float(el.get("y2")))
        curve = float(el.get("curve") or 0) * (-1 if mirror else 1)
        if curve:
            mid, _c = arc_mid_and_center(a[0], a[1], b[0], b[1], curve)
            pr = _arc(k(*a), k(*mid), k(*b))
        else:
            pr = ("seg", k(*a), k(*b))
        out.append((pr, w / 2, False))

    board = root.find("drawing/board")
    for el in board.findall("plain/*"):
        if el.get("layer") == "46":
            add(el, lambda x, y: (x, y), False)
    pk = {}
    for lib in board.findall("libraries/library"):
        for p in lib.findall("packages/package"):
            pk[(lib.get("name"), lib.get("urn") or "", p.get("name"))] = p
            pk.setdefault((lib.get("name"), None, p.get("name")), p)
    for e in board.findall("elements/element"):
        p = pk.get((e.get("library"), e.get("library_urn") or "", e.get("package")))
        if p is None:
            p = pk.get((e.get("library"), None, e.get("package")))
        items = [x for x in (p if p is not None else []) if x.get("layer") == "46"]
        if not items:
            continue
        T, mirror = eagle_element_T(e)
        for x in items:
            add(x, T, mirror)
    return out


def _in_outlines(q, outlines, tol) -> bool:
    """q inside one of the outlines (even-odd) or within tol of its boundary."""
    for o in outlines:
        inside = False
        for i in range(len(o)):
            (x1, y1), (x2, y2) = o[i], o[(i + 1) % len(o)]
            if (y1 > q[1]) != (y2 > q[1]) and q[0] < x1 + (q[1] - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
            if _prim_dist(q, ("seg", (x1, y1), (x2, y2))) <= tol:
                return True
        if inside:
            return True
    return False


def package_restrict_check(brd: Path, ours, refs: dict | None = None, tol=10_000) -> bool:
    """Keep-outs drawn in library packages (tRestrict 41 / bRestrict 42 / vRestrict 43) must be rule areas of
    their footprint: sample points of every EAGLE item, mapped through the element's position, rotation and
    mirror, lie in a rule area of that footprint on the right copper side with EAGLE's restrictions, exactly
    one rule area per item.  KiCad's library reader makes these rule areas for restrict rectangles, circles
    and polygons but drops restrict wires (re-created by the converter)."""
    root = ET.parse(brd).getroot()
    board = root.find("drawing/board")
    names = [e.get("name") for e in board.findall("elements/element")]
    rmap = kicad_ref_map(names)
    rmap.update(refs or {})
    pk = {}
    for lib in board.findall("libraries/library"):
        for p in lib.findall("packages/package"):
            pk[(lib.get("name"), lib.get("urn") or "", p.get("name"))] = p
            pk.setdefault((lib.get("name"), None, p.get("name")), p)
    fps = {f.ref: f for f in ours.footprints}
    cu = _copper_layer_names(ours)
    want_ko = {41: {"tracks", "vias", "pads", "copperpour"}, 42: {"tracks", "vias", "pads", "copperpour"},
               43: {"vias"}}
    n_items, n_parts, bad = 0, 0, []
    for e in board.findall("elements/element"):
        p = pk.get((e.get("library"), e.get("library_urn") or "", e.get("package")))
        if p is None:
            p = pk.get((e.get("library"), None, e.get("package")))
        items = [x for x in (p if p is not None else []) if x.tag in ("wire", "circle", "rectangle", "polygon")
                 and x.get("layer") in ("41", "42", "43")]
        ref = rmap.get(e.get("name"), e.get("name"))
        fp = fps.get(ref)
        zones = [z for z in (fp.zones if fp else []) if z.rule_area]
        if not items and not zones:
            continue
        n_parts += 1
        Te, mirror = eagle_element_T(e)

        def T(x, y):
            bx, by = Te(x, y)
            return bx * 1e6, -by * 1e6

        hit, n_el = set(), 0
        for it in items:
            n = int(it.get("layer"))
            f = lambda k: float(it.get(k) or 0)  # noqa: E731
            if it.tag == "wire":
                if f("width") <= 0:
                    continue
                pts = [(f("x1"), f("y1")), (f("x2"), f("y2"))]
                pts.append(arc_mid_and_center(*pts[0], *pts[1], f("curve"))[0] if f("curve")
                           else ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2))
            elif it.tag == "circle":
                r = f("radius") * (0.9 if f("width") <= 0 else 1)
                pts = [(f("x") + r * math.cos(a * TAU / 8), f("y") + r * math.sin(a * TAU / 8)) for a in range(8)]
                if f("width") <= 0:
                    pts.append((f("x"), f("y")))
            elif it.tag == "rectangle":
                cx, cy = (f("x1") + f("x2")) / 2, (f("y1") + f("y2")) / 2
                rr = math.radians(parse_rot(it.get("rot"))[2])
                pts = [(cx, cy)] + [(cx + (x - cx) * math.cos(rr) - (y - cy) * math.sin(rr),
                                     cy + (x - cx) * math.sin(rr) + (y - cy) * math.cos(rr))
                                    for x, y in ((f("x1"), f("y1")), (f("x2"), f("y1")),
                                                 (f("x2"), f("y2")), (f("x1"), f("y2")))]
            else:
                pts = [(float(v.get("x")), float(v.get("y"))) for v in it.findall("vertex")]
            n_items += 1
            n_el += 1
            if n == 43:
                layer_ok = lambda z: "*.Cu" in z.layers or set(cu) <= set(z.layers)  # noqa: E731
            else:
                side = "F.Cu" if (n == 41) != mirror else "B.Cu"
                layer_ok = lambda z, side=side: tuple(z.layers) == (side,)  # noqa: E731
            cand = [i for i, z in enumerate(zones) if layer_ok(z) and
                    {k for k, v in z.opts.get("keepout", {}).items() if v == "not_allowed"} == want_ko[n]]
            for q in (T(*pt) for pt in pts):
                inside = [i for i in cand if _in_outlines(q, zones[i].outlines, tol)]
                if not inside:
                    bad.append(f"{ref}: {it.tag} on layer {n} at ({q[0] / 1e6:.3f}, {q[1] / 1e6:.3f}) mm is not "
                               f"in a matching footprint rule area")
                    break
                hit.update(inside)
        for i, z in enumerate(zones):
            if i not in hit:
                bad.append(f"{ref}: footprint rule area on {','.join(z.layers)} has no EAGLE package restrict item")
        if len(zones) != n_el:
            bad.append(f"{ref}: {len(zones)} footprint rule area(s) for {n_el} EAGLE package restrict item(s)")
    if bad:
        print(f"FAIL  package keep-outs (t/b/vRestrict) vs EAGLE board: {len(bad)} problem(s)")
        for b in bad[:6]:
            print("     ", b)
        return False
    if n_items:
        print(f"PASS  package keep-outs (t/b/vRestrict) vs EAGLE board: {n_items} item(s) in {n_parts} part(s), one "
              f"footprint rule area each at the EAGLE position, side and layer (restrict wires, which KiCad's "
              f"reader drops, are re-created)")
    return True


def milling_outline_check(ref, ours, mill, tol=15_000, step=100_000):
    """Our Edge.Cuts must equal the official outline with the milled areas removed:
      * every point of our Edge.Cuts lies on the official outline or on a cut boundary, never inside a cut;
      * every point of the official outline lies on ours or inside a cut.
    tol 15 um covers KiCad's 5 um arc-to-polygon error on both sides.  -> (ok, n_points, bad examples)"""
    A = [pr for s in ref.shapes if s.layer == "Edge.Cuts" for pr in _shape_prims(s)]
    B = [pr for s in ours.shapes if s.layer == "Edge.Cuts" for pr in _shape_prims(s)]
    near_a, near_b = _Near(A, tol), _Near(B, tol)

    def cut(q):  # signed distance to the milled area (negative inside)
        return min(((math.dist(q, pr[1]) - pr[2] - rad) if circ else (_prim_dist(q, pr) - rad)
                    for pr, rad, circ in mill), default=math.inf)

    bad, n = [], 0
    for pr in B:
        for q in _prim_samples(pr, step):
            n += 1
            c = cut(q)
            if c < -tol or (abs(c) > tol and near_a(q) > tol):
                bad.append(("ours", round(q[0] / 1e6, 3), round(q[1] / 1e6, 3)))
    for pr in A:
        for q in _prim_samples(pr, step):
            n += 1
            if cut(q) > tol and near_b(q) > tol:
                bad.append(("official", round(q[0] / 1e6, 3), round(q[1] / 1e6, 3)))
    return not bad, n, bad


def copper_layer_map(votes):
    mapping = {layer: max(candidates, key=candidates.get)
               for layer, candidates in votes.items() if candidates}
    # Outer layers are invariant; identical top/bottom pours must not collapse them.
    mapping.update({"F.Cu": "F.Cu", "B.Cu": "B.Cu"})
    return mapping


def official_geometry(cli10: str, brd: Path, pcb: Path) -> bool:
    """Import the original .brd with KiCad's official EAGLE importer (kicad-cli >= 10) and compare
    footprint placement/pads, outline, tracks, vias and copper zones with our board (read with
    kicad_pcb_reader). Known, documented importer differences are reported as INFO, not hidden:
      * net spelling: KiCad's importer turns EAGLE's '~' into an overbar '~{..}'; we keep EAGLE's text;
      * copper layer numbering: KiCad numbers every *active* EAGLE layer in XML order, we follow the
        physical stack from the EAGLE layer setup -> compared through the inferred 1:1 layer mapping;
      * non-copper polygons: KiCad makes zones, we make filled graphic polygons;
      * Milling (layer 46): KiCad drops it, we cut it out on Edge.Cuts."""
    tol = 1000  # 1 um
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / brd.name
        shutil.copy2(brd, src)  # the importer writes a .kicad_dru next to its input
        ref_path = Path(td) / "official.kicad_pcb"
        run([cli10, "pcb", "import", "--format", "eagle", "-o", str(ref_path), str(src)])
        ref, ours = load_board(ref_path), load_board(pcb)
        from custom_pad_copper import verified_custom_pads
        custom_pads = verified_custom_pads(brd, pcb)
    ok = True
    R = {f.ref: f for f in ref.footprints}
    O = {f.ref: f for f in ours.footprints}
    bad, size_diff, n_pads, unconnected = [], 0, 0, 0
    pairs = defaultdict(set)
    for refdes in sorted(set(R) | set(O)):
        if refdes not in R or refdes not in O:
            bad.append(f"{refdes}: only in {'official import' if refdes in R else 'our board'}")
            continue
        fr, fo = R[refdes], O[refdes]
        if abs(fr.x - fo.x) > tol or abs(fr.y - fo.y) > tol or fr.flipped != fo.flipped:
            bad.append(f"{refdes}: position/side differ")
        rem = list(fo.pads)
        for p in fr.pads:
            n_pads += 1
            hit = next((q for q in rem if q.number == p.number and abs(q.x - p.x) <= tol and abs(q.y - p.y) <= tol),
                       None)
            if hit is None:
                bad.append(f"{refdes} pad {p.number!r}: no pad at the official position")
                continue
            rem.remove(hit)
            shape = p.shape if (refdes, p.number) in custom_pads else hit.shape
            if (shape, hit.kind, hit.drill, set(hit.layers)) != (p.shape, p.kind, p.drill, set(p.layers)) \
                    or abs((hit.angle - p.angle + 90) % 180 - 90) > 0.01:
                bad.append(f"{refdes} pad {p.number!r}: shape/type/drill/layers/angle differ "
                           f"{(hit.shape, hit.kind, hit.drill, hit.layers, hit.angle)} vs "
                           f"{(p.shape, p.kind, p.drill, p.layers, p.angle)}")
            if hit.size != p.size:
                size_diff += 1
            if not p.net and hit.net.startswith("unconnected-"):
                unconnected += 1
            elif p.net or hit.net:
                pairs[p.net].add(hit.net)
        bad += [f"{refdes} pad {q.number!r}: extra pad" for q in rem]
    net_map, inv = {}, defaultdict(set)
    for o, s in pairs.items():
        if len(s) > 1:
            bad.append(f"official net {o!r} is split into {sorted(s)} on our board")
        net_map[o] = sorted(s)[0]
        for x in s:
            inv[x].add(o)
    bad += [f"our net {x!r} merges official nets {sorted(s)}" for x, s in inv.items() if len(s) > 1]
    if bad:
        ok = False
        print(f"FAIL  footprints vs KiCad official EAGLE import: {len(bad)} difference(s)")
        for b in bad[:25]:
            print("    ", b)
    else:
        print(f"PASS  footprints vs KiCad official EAGLE import: {len(R)} footprints, {n_pads} pads at identical "
              f"positions/orientation/side/shape/drill/layers; nets correspond 1:1")
    if size_diff:
        print(f"INFO  {size_diff} pad size(s) differ from KiCad's importer by design: EAGLE restring/'use diameter' "
              f"rules give the real copper size Fusion 360 manufactures")
    if custom_pads:
        print(f"PASS  {len(custom_pads)} custom pads: copper equals EAGLE pad + package polygon unions")
    renamed = sorted((o, x) for o, x in net_map.items() if unescape(o) != unescape(x))
    if renamed:
        ex = ", ".join(f"{o!r}->{x!r}" for o, x in renamed[:3])
        print(f"INFO  {len(renamed)} net name(s) spelled differently, same members (official->ours: {ex}); "
              f"we keep EAGLE's exact names")
    if unconnected:
        print(f"INFO  {unconnected} unconnected pin(s) carry KiCad 'unconnected-(...)' nets, exactly as "
              f"'Update PCB from Schematic' assigns them")

    def nmap(n):
        return unescape(net_map.get(n, n))

    def tkey(t, net):
        return tuple(sorted([_um(t.start), _um(t.end)])), _um(t.mid) if t.mid else None, t.width, net

    def zkey(z, net):
        return net, z.rule_area, zone_outline_key(z.outlines)

    ours_cu = _copper_layer_names(ours)
    votes = defaultdict(lambda: defaultdict(int))
    by_key = defaultdict(list)
    for t in ours.tracks:
        by_key[tkey(t, unescape(t.net))].append(t.layer)
    for t in ref.tracks:
        for l in by_key.get(tkey(t, nmap(t.net)), []):
            votes[t.layer][l] += 1
    zby = defaultdict(list)
    for z in ours.zones:
        if len(z.layers) == 1:
            zby[zkey(z, unescape(z.net))].append(z.layers[0])
    for z in ref.zones:
        if len(z.layers) == 1:
            for l in zby.get(zkey(z, nmap(z.net)), []):
                votes[z.layers[0]][l] += 1
    lmap = copper_layer_map(votes)
    if len(set(lmap.values())) != len(lmap):
        ok = False
        print(f"FAIL  copper layers: no 1:1 correspondence with the official import: {lmap}")
    elif any(k != v for k, v in lmap.items()):
        moved = ", ".join(f"{k}->{v}" for k, v in sorted(lmap.items()) if k != v)
        print(f"INFO  copper layer numbering differs from KiCad's importer ({moved}); ours follows the EAGLE "
              f"layer setup (physical stack order, {len(ours_cu)} copper layers) and is checked by EAGLE layer "
              f"name above")

    def zlayers(layers, board):
        cu = set(_copper_layer_names(board))
        if "*.Cu" in layers or (len(layers) > 1 and cu <= set(layers)):
            return ("ALL.Cu",)
        return tuple(sorted(lmap.get(l, l) if board is ref else l for l in layers))

    def items(b):
        o = b is ref
        rules = Rules(ET.parse(brd).getroot().find("drawing/board"))
        tr = {(tkey(t, nmap(t.net) if o else unescape(t.net)), lmap.get(t.layer, t.layer) if o else t.layer)
              for t in b.tracks}
        vi = {(_um((v.x, v.y)), v.drill,
               round(rules.via_diameter(v.drill / 1e6, v.size / 1e6) * 1e6) if o else v.size,
               zlayers(v.layers, b), nmap(v.net) if o else unescape(v.net)) for v in b.vias}
        edge = {("line", tuple(sorted((_um(pr[1]), _um(pr[2]))))) if pr[0] == "seg" else
                (s.kind, tuple(sorted(_um(p) for p in s.pts)))
                for s in b.shapes if s.layer == "Edge.Cuts" for pr in _shape_prims(s)}
        zo = {(zlayers(z.layers, b),) + zkey(z, nmap(z.net) if o else unescape(z.net)) for z in b.zones
              if z.rule_area or any(l.endswith(".Cu") for l in z.layers)}
        return {"tracks": tr, "vias": vi, "outline": edge, "copper zones/rule areas": zo}

    ref_edges = {i: (s.pts[0], s.pts[-1]) for i, s in enumerate(ref.shapes)
                 if s.layer == "Edge.Cuts" and s.kind in ("line", "segment", "arc")}
    spurs = outline_spurs(ref_edges)
    if spurs:
        print(f"INFO  {len(spurs)} dead-end item(s) in the EAGLE Dimension outline (bound no area; KiCad cannot "
              f"build an outline with them) are dropped by the converter: "
              + "; ".join(f"{ref.shapes[i].kind} " + "-".join(f"({x / 1e6:g}, {y / 1e6:g})" for x, y in
                                                            (ref.shapes[i].pts[0], ref.shapes[i].pts[-1]))
                          for i in sorted(spurs)))
        ref.shapes = [s for i, s in enumerate(ref.shapes) if i not in spurs]
    IR, IO = items(ref), items(ours)
    root = ET.parse(brd).getroot()
    mill = eagle_milling_paths(root)
    for k in IR:
        only_o, only_r = IO[k] - IR[k], IR[k] - IO[k]
        if not only_o and not only_r:
            print(f"PASS  {k} vs KiCad official EAGLE import: {len(IR[k])} identical")
        elif k == "outline" and mill:
            good, n, bad = milling_outline_check(ref, ours, mill)
            if good:
                print(f"PASS  outline == KiCad official EAGLE import minus the {len(mill)} EAGLE Milling (46) cutter "
                      f"path(s) ({n} points checked, 15 um tolerance)")
                print("INFO  KiCad's importer drops the Milling layer; we cut it out of Edge.Cuts so Quilter sees "
                      "the real board shape")
            else:
                ok = False
                print(f"FAIL  outline vs official import minus milling: {len(bad)} of {n} points off, e.g. {bad[:4]}")
        else:
            ok = False
            print(f"FAIL  {k} vs KiCad official EAGLE import: {len(only_o)} only ours, {len(only_r)} only official")
            for x in sorted(only_o, key=str)[:4]:
                print("     ours:    ", str(x)[:300])
            for x in sorted(only_r, key=str)[:4]:
                print("     official:", str(x)[:300])
    other = sum(1 for z in ref.zones if not z.rule_area and not any(l.endswith(".Cu") for l in z.layers))
    if other:
        print(f"INFO  KiCad's importer makes {other} zone(s) from non-copper EAGLE polygons/rectangles; we keep "
              f"them as filled graphic shapes on the same layers")
    return ok

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sch", type=Path, required=True, help="converted root .kicad_sch")
    ap.add_argument("--map", type=Path, required=True, help="<project>.eagle_map.json from eagle_sch_to_kicad")
    ap.add_argument("--eagle-brd", type=Path, help="original EAGLE .brd")
    ap.add_argument("--pcb", type=Path, help="converted .kicad_pcb (needs KiCad's python for pcbnew)")
    ap.add_argument("--kicad-cli")
    ap.add_argument("--official-import", action="store_true",
                    help="also compare --pcb geometry with KiCad 10's official EAGLE board importer "
                         "(run this script with KiCad 10's python)")
    args = ap.parse_args(argv)
    cli = find_kicad_cli(args.kicad_cli)
    mapping = json.loads(args.map.read_text(encoding="utf-8"))
    ok = True

    expected = {n: {tuple(x) for x in v} for n, v in mapping["expected_nets"].items()}
    sch_nets = kicad_sch_nets(cli, args.sch)
    ok &= compare("KiCad schematic netlist == EAGLE schematic", sch_nets, expected)

    if args.eagle_brd:
        brd_nets, elements, padless = eagle_board_nets(args.eagle_brd, mapping.get("refs"))
        ok &= compare("KiCad schematic netlist == EAGLE board", kicad_sch_nets(cli, args.sch, elements), brd_nets)
        on_board = {r for r, c in mapping["components"].items() if c["on_board"]}
        board_only = elements - on_board
        if on_board - elements or board_only - padless:
            ok = False
            print(f"FAIL  parts: schematic-only {sorted(on_board - elements)}, "
                  f"board-only with pads {sorted(board_only - padless)}")
        else:
            extra = f" (+{len(board_only)} pad-less board-only items, e.g. logos)" if board_only else ""
            print(f"PASS  parts: {len(on_board)} schematic parts with packages == board elements{extra}")
        if mapping.get("refs"):
            ex = ", ".join(f"{e}->{k}" for e, k in list(mapping["refs"].items())[:5])
            print(f"INFO  {len(mapping['refs'])} EAGLE name(s) given a trailing number, as KiCad's own EAGLE "
                  f"importer does (KiCad treats references without one as unannotated): {ex}")
        if args.pcb:
            ok &= compare("KiCad board pads == EAGLE board", kicad_board_nets(args.pcb), brd_nets)
            ours = load_board(args.pcb)
            ok &= eagle_copper_check(args.eagle_brd, ours)
            ok &= package_restrict_check(args.eagle_brd, ours, mapping.get("refs"))

    if args.pcb:
        with tempfile.TemporaryDirectory() as td:
            rpt = Path(td) / "drc.json"
            run([cli, "pcb", "drc", "--schematic-parity", "--format", "json", "--severity-all",
                 "-o", str(rpt), str(args.pcb)])
            drc = json.loads(rpt.read_text(encoding="utf-8"))
        parity = drc.get("schematic_parity", [])
        if parity:
            ok = False
            print(f"FAIL  kicad-cli schematic parity: {len(parity)} issue(s)")
            for v in parity[:20]:
                print("   ", v["type"], v["description"], "|", "; ".join(i["description"] for i in v["items"]))
        else:
            print("PASS  kicad-cli pcb drc --schematic-parity: board and schematic agree")
        kinds = defaultdict(int)
        for v in drc.get("violations", []) + drc.get("unconnected_items", []):
            kinds[(v["severity"], v["type"])] += 1
        bad_outline = [v for v in drc.get("violations", []) if v["type"] == "invalid_outline"]
        shorts = [v for v in drc.get("violations", []) if v["type"] == "shorting_items"]
        if shorts:
            ok = False
            print(f"FAIL  board DRC: {len(shorts)} copper shorts (not waived as unrouted connections)")
            for v in shorts[:20]:
                print("   ", v["description"], "|", "; ".join(i["description"] for i in v["items"]))
        if bad_outline:
            ok = False
            print(f"FAIL  board outline: KiCad cannot build a closed, non-intersecting outline (Quilter needs one): "
                  + "; ".join(sorted({v['description'] for v in bad_outline})))
        if kinds:
            print("INFO  board DRC (expected before routing):", dict(kinds))

    if args.official_import and args.pcb and args.eagle_brd:
        cli10 = find_kicad10_cli()
        if cli10 is None:
            print("SKIP  official-import comparison needs KiCad 10+ (kicad-cli pcb import)")
        else:
            ok &= official_geometry(cli10, args.eagle_brd, args.pcb)

    print("ALL CHECKS PASSED" if ok else "VERIFICATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
