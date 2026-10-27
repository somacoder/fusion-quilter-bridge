#!/usr/bin/env python3
"""Bring a placed / routed KiCad board (e.g. Quilter's result) back into Fusion 360 Electronics.

Inputs
  * the routed .kicad_pcb (Quilter returns the board in the format it was given)
  * the original EAGLE .brd exported from Fusion 360 (the one that was converted for Quilter)
  * <project>.board_map.json written by eagle_brd_to_kicad.py at that time

Outputs (both carry exactly the same changes)
  <name>.scr  EAGLE script for the board that is already open in Fusion 360 (Automation > Run Script).
              It updates the existing design in place, so the schematic link, libraries, attributes,
              3D/PCB link and history all stay as they are.
  <name>.brd  the original .brd with the same changes applied; everything else in the file (libraries,
              schematic-related data, layers, design rules, graphics, texts) is kept as exported.

What a place-and-route tool changes is transferred: part position / rotation / side (smashed name and
value texts move with their part), copper tracks and arcs, vias and copper pours.  Items that are unchanged
are kept as the original EAGLE objects.  Board outline, silkscreen and other drawings are not taken from
the routed board (Quilter does not change them); differences in connectivity are reported, never applied.

Pure Python 3.9+ standard library; does not need KiCad.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagle_common import Rules, arc_mid_and_center, mm_to_nm, parse_rot, rotate  # noqa: E402
from kicad_pcb_reader import load_board  # noqa: E402

NM = 1_000_000
TOP_BOTTOM = {21: 22, 25: 26, 27: 28, 29: 30, 31: 32, 33: 34, 35: 36, 37: 38, 39: 40, 41: 42, 51: 52}
TOP_BOTTOM.update({b: t for t, b in list(TOP_BOTTOM.items())})


def mirror_layer(n: int) -> int:
    if 1 <= n <= 16:
        return 17 - n
    return TOP_BOTTOM.get(n, n)


def fmt(v_nm: float) -> str:
    """nm -> EAGLE mm string."""
    s = f"{v_nm / NM:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def fdeg(d: float) -> str:
    d = round(d % 360.0, 6) % 360.0
    s = f"{d:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def enm(el, k) -> int:
    v = el.get(k)
    return mm_to_nm(v) if v not in (None, "") else 0


def to_eagle(p) -> tuple[int, int]:
    """KiCad nm (y down) -> EAGLE nm (y up)."""
    return p[0], -p[1]


def rot_str(mirror: bool, spin: bool, deg: float) -> str:
    return ("S" if spin else "") + ("M" if mirror else "") + "R" + fdeg(deg)


def frame(mirror: bool, deg: float):
    """EAGLE placement matrix (y up): rotate CCW by deg, then mirror x if mirrored (as KiCad's importer)."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((-c, s), (s, c)) if mirror else ((c, -s), (s, c))


def arc_curve(a, m, b) -> float:
    """Signed sweep (degrees, CCW positive, y up) of the arc a -> m -> b."""
    (ax, ay), (mx, my), (bx, by) = a, m, b
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-6:
        return 0.0
    cx = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / d
    cy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / d
    a0, am, a1 = (math.atan2(p[1] - cy, p[0] - cx) for p in (a, m, b))
    sweep = (a1 - a0) % math.tau
    if (am - a0) % math.tau > sweep:
        sweep -= math.tau
    return math.degrees(sweep)


def signed_area(pts) -> float:
    return sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts))) / -2.0


def shrink(pts, d):
    """Offset a simple polygon inwards by d (mitred corners, limited to 4 d)."""
    pts = [p for i, p in enumerate(pts) if p != pts[i - 1]]
    if len(pts) < 3 or d <= 0:
        return pts
    if signed_area(pts) < 0:
        pts = pts[::-1]
    n, out = len(pts), []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        u = (p1[0] - p0[0], p1[1] - p0[1])
        v = (p2[0] - p1[0], p2[1] - p1[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        n1 = (-u[1] / lu, u[0] / lu)  # left normal = inside for CCW
        n2 = (-v[1] / lv, v[0] / lv)
        bis = (n1[0] + n2[0], n1[1] + n2[1])
        cos_half = math.sqrt(max((1 + n1[0] * n2[0] + n1[1] * n2[1]) / 2, 0.0))
        lb = math.hypot(*bis)
        if lb < 1e-9 or cos_half < 0.25:
            k, bis = 4 * d, (n1 if lb < 1e-9 else (bis[0] / lb, bis[1] / lb))
        else:
            k, bis = d / cos_half, (bis[0] / lb, bis[1] / lb)
        out.append((round(p1[0] + bis[0] * k), round(p1[1] + bis[1] * k)))
    return out


def path_pts(a, b, curve: float, steps: int = 16):
    """Points along an EAGLE wire / polygon edge from a to b; arcs (curve = CCW sweep in degrees) are sampled."""
    if not curve:
        return [a, b]
    _m, (cx, cy) = arc_mid_and_center(a[0], a[1], b[0], b[1], curve)
    return [(cx + vx, cy + vy) for vx, vy in (rotate(a[0] - cx, a[1] - cy, curve * k / steps)
                                              for k in range(steps + 1))]


def seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def point_in(pt, poly) -> bool:
    x, y, inside = pt[0], pt[1], False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i - 1], poly[i]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def q(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


class Patch:
    def __init__(self, pcb: Path, brd: Path, bmap: dict):
        self.k = load_board(pcb)
        self.brd = brd
        self.text = brd.read_text(encoding="utf-8")
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        self.root = ET.fromstring(self.text, parser=parser)
        self.board_el = self.root.find("drawing/board")
        self.rules = Rules(self.board_el)
        self.bmap = bmap
        user = {v.get("user"): k for k, v in self.k.layers.items() if v.get("user")}
        self.cu = {name if name in self.k.layers else user.get(name, name): int(n)
                   for n, name in bmap["copper"].items()}  # KiCad layer -> EAGLE layer
        self.nets = bmap["nets"]  # KiCad net -> EAGLE signal
        self.signals = {s.get("name"): s for s in self.board_el.findall("signals/signal")}
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.stats = Counter()
        self.scr: list[str] = []
        self.moved: set[str] = set()
        self.place_lines: list[str] = []
        self.via_rules_changed = False
        self.pour_rules_changed = False

    def match_pour_rules(self):
        gaps = {z.opts["thermal_gap"] for z in self.k.zones
                if not z.rule_area and z.opts.get("connect", "thermal") == "thermal"
                and "thermal_gap" in z.opts}
        if not gaps:
            return
        if len(gaps) != 1:
            raise ValueError("Different per-zone thermal gaps cannot share one Fusion thermal-isolate rule")
        gap = gaps.pop()
        if gap <= 0:
            raise ValueError("Cannot preserve a non-positive thermal gap")
        if abs(self.rules.get("slThermalIsolate", 0.254) * NM - gap) <= 1:
            return
        rules = self.board_el.find("designrules")
        if rules is None:
            raise ValueError("Cannot adjust missing Fusion design rules")
        param = rules.find("param[@name='slThermalIsolate']")
        if param is None:
            param = ET.SubElement(rules, "param", name="slThermalIsolate")
        param.set("value", fmt(gap) + "mm")
        self.rules = Rules(self.board_el)
        self.pour_rules_changed = True
        self.notes.append(f"Global thermal-isolate rule changed to {fmt(gap)} mm to match the routed zones. "
                          "Repour and inspect thermal connections before manufacture.")

    def match_via_rules(self):
        if not self.k.vias:
            return
        rings = [(v.size - v.drill) / 2 for v in self.k.vias]
        if min(rings) <= 0:
            raise ValueError("Cannot preserve vias with a non-positive annular ring")
        if not any(self.rules.via_diameter(v.drill / NM, v.size / NM) * NM > v.size + 1
                   for v in self.k.vias):
            return
        rules = self.board_el.find("designrules")
        if rules is None:
            raise ValueError("Cannot adjust missing Fusion design rules")
        values = {"rlMinViaOuter": fmt(min(rings)) + "mm", "rvViaOuter": "0"}
        for key, value in values.items():
            param = next((p for p in rules.findall("param") if p.get("name") == key), None)
            if param is None:
                param = ET.SubElement(rules, "param", name=key)
            param.set("value", value)
        self.rules = Rules(self.board_el)
        self.via_rules_changed = True
        self.notes.append(f"Via-only restring rules changed to preserve routed diameters: "
                          f"minimum outer annulus {fmt(min(rings))} mm, ratio 0. "
                          "Confirm this annulus with the fabricator before manufacture.")

    # ------------------------------------------------------------------ helpers
    def signal_of(self, net: str, what: str):
        """KiCad net -> EAGLE signal name ('' = no net), or None when the item must be skipped."""
        if not net:
            return ""
        if net in self.nets:
            return self.nets[net]
        if net.startswith("unconnected-("):
            self.warnings.append(f"{what} on single-pin net {net} skipped")
        else:
            self.warnings.append(f"{what} on net {net}, which is not in the Fusion design, skipped")
        return None

    def eagle_layer(self, layer: str, what: str):
        n = self.cu.get(layer)
        if n is None:
            self.warnings.append(f"{what} on KiCad layer {layer}, which has no EAGLE copper layer, skipped")
        return n

    def append(self, parent, el):
        """Append in EAGLE's layout: one element per line, no indentation."""
        if len(parent):
            parent[-1].tail = "\n"
        else:
            parent.text = "\n"
        el.tail = "\n"
        parent.append(el)

    def remove(self, parent, el):
        kids = list(parent)
        i = kids.index(el)
        if i > 0:
            kids[i - 1].tail = el.tail
        elif len(kids) == 1:
            parent.text = None
        parent.remove(el)

    # ------------------------------------------------------------------ parts
    def placement(self):
        elements = {e.get("name"): e for e in self.board_el.findall("elements/element")}
        holes = self.bmap.get("holes", {})
        moves, flips = [], 0
        for fp in self.k.footprints:
            info = self.bmap["elements"].get(fp.ref)
            x, y = to_eagle((fp.x, fp.y))
            if info is None:
                if fp.ref in holes:
                    self.move_hole(fp.ref, holes[fp.ref], x, y)
                else:
                    self.warnings.append(f"footprint {fp.ref} is not part of the Fusion design; not transferred")
                continue
            el = elements.get(info["eagle_name"])
            if el is None:
                self.warnings.append(f"element {info['eagle_name']} not found in {self.brd.name}")
                continue
            om, ospin, odeg = parse_rot(el.get("rot"))
            nmir = fp.flipped
            ndeg = (180.0 - fp.angle) % 360.0 if nmir else fp.angle % 360.0
            ox, oy = enm(el, "x"), enm(el, "y")
            rot = rot_str(nmir, ospin, ndeg)
            if (x, y) == (ox, oy) and rot == rot_str(om, ospin, odeg):
                self.stats["parts unchanged"] += 1
                continue
            if el.get("locked") == "yes":
                self.warnings.append(f"{info['eagle_name']} is locked in Fusion but was moved by the router; "
                                     "left where it is")
                continue
            self.move_attributes(el, (ox, oy, om, odeg), (x, y, nmir, ndeg))
            el.set("x", fmt(x))
            el.set("y", fmt(y))
            if el.get("rot") is not None or rot != "R0":
                el.set("rot", rot)
            flips += nmir != om
            self.moved.add(info["eagle_name"])
            moves.append(f"MOVE {q(info['eagle_name'])} ({fmt(x)} {fmt(y)});")
            moves.append(f"ROTATE ={rot} {q(info['eagle_name'])};")
        self.stats["parts placed"] = len(moves) // 2
        self.stats["parts changed side"] = flips
        if moves:
            self.place_lines = ["", "# ---- part placement (smashed name/value texts follow their parts)"] + moves

    def move_attributes(self, el, old, new):
        ox, oy, om, odeg = old
        nx, ny, nmir, ndeg = new
        fo, fn = frame(om, odeg), frame(nmir, ndeg)
        for a in el.findall("attribute"):
            if a.get("x") is None:
                continue
            vx, vy = enm(a, "x") - ox, enm(a, "y") - oy
            lx, ly = fo[0][0] * vx + fo[1][0] * vy, fo[0][1] * vx + fo[1][1] * vy  # F_old^T v
            a.set("x", fmt(nx + fn[0][0] * lx + fn[0][1] * ly))
            a.set("y", fmt(ny + fn[1][0] * lx + fn[1][1] * ly))
            tm, ts, tdeg = parse_rot(a.get("rot"))
            rel = om != tm
            a.set("rot", rot_str(nmir != rel, ts, tdeg + (ndeg - odeg) * (-1 if rel else 1)))
            if nmir != om and a.get("layer"):
                a.set("layer", str(mirror_layer(int(a.get("layer")))))

    def move_hole(self, ref, info, x, y):
        ox, oy = round(info["x"] * NM), round(info["y"] * NM)
        if (x, y) == (ox, oy):
            return
        for h in self.board_el.findall("plain/hole"):
            if (enm(h, "x"), enm(h, "y")) == (ox, oy) and abs(float(h.get("drill")) - info["drill"]) < 1e-9:
                h.set("x", fmt(x))
                h.set("y", fmt(y))
                self.notes.append(f"hole {ref} moved; the script cannot move holes, use the .brd or move it by hand")
                return

    # ------------------------------------------------------------------ copper
    def wire_key(self, sig, layer, a, b, width):
        return sig, layer, frozenset((a, b)), width

    def routing(self):
        copper = set(self.cu.values())
        # originals: signal wires / vias on copper
        orig_w = defaultdict(list)
        orig_v = defaultdict(list)
        for name, sig in self.signals.items():
            for w in sig.findall("wire"):
                n = int(w.get("layer"))
                if n not in copper:
                    continue
                a, b = (enm(w, "x1"), enm(w, "y1")), (enm(w, "x2"), enm(w, "y2"))
                mid = None
                if float(w.get("curve") or 0):
                    m, _c = arc_mid_and_center(*(float(w.get(k)) for k in ("x1", "y1", "x2", "y2")),
                                               float(w.get("curve")))
                    mid = (round(m[0] * NM), round(m[1] * NM))
                orig_w[self.wire_key(name, n, a, b, enm(w, "width"))].append((w, sig, mid))
            for v in sig.findall("via"):
                ext = tuple(int(t) for t in (v.get("extent") or "1-16").split("-"))
                orig_v[(name, enm(v, "x"), enm(v, "y"), enm(v, "drill"))].append((v, sig, ext))

        new_w, new_v, kept = [], [], set()
        for t in self.k.tracks:
            sig = self.signal_of(t.net, "track")
            n = self.eagle_layer(t.layer, "track")
            if sig is None or n is None:
                continue
            a, b = to_eagle(t.start), to_eagle(t.end)
            if a == b:
                self.stats["zero-length tracks skipped"] += 1
                continue
            mid = to_eagle(t.mid) if t.kind == "arc" and t.mid else None
            cands = orig_w.get(self.wire_key(sig, n, a, b, t.width), [])
            hit = next((c for c in cands if id(c[0]) not in kept and
                        ((c[2] is None and mid is None) or
                         (c[2] is not None and mid is not None and math.dist(c[2], mid) < 2000))), None)
            if hit is not None:
                kept.add(id(hit[0]))
                continue
            curve = arc_curve(a, mid, b) if mid is not None else 0.0
            new_w.append((sig, n, a, b, t.width, curve))
        for v in self.k.vias:
            sig = self.signal_of(v.net, "via")
            if sig is None:
                continue
            x, y = to_eagle((v.x, v.y))
            ls = [self.cu[l] for l in v.layers if l in self.cu]
            ext = (min(ls), max(ls)) if v.kind != "through" and len(ls) >= 2 else (min(copper), max(copper))
            cands = orig_v.get((sig, x, y, v.drill), [])
            hit = next((c for c in cands if id(c[0]) not in kept), None)
            if hit is not None:
                kept.add(id(hit[0]))
                continue
            new_v.append((sig, x, y, v.drill, v.size, ext))

        removed_sigs = set()
        for group in (orig_w, orig_v):
            for items in group.values():
                for el, sig, _x in items:
                    if id(el) not in kept:
                        self.remove(sig, el)
                        removed_sigs.add(sig.get("name"))
                        self.stats["tracks removed" if el.tag == "wire" else "vias removed"] += 1
                    else:
                        self.stats["tracks kept" if el.tag == "wire" else "vias kept"] += 1

        # .brd
        for sig, n, a, b, width, curve in new_w:
            w = ET.Element("wire", {"x1": fmt(a[0]), "y1": fmt(a[1]), "x2": fmt(b[0]), "y2": fmt(b[1]),
                                    "width": fmt(width), "layer": str(n)})
            if curve:
                w.set("curve", f"{curve:.6f}".rstrip("0").rstrip("."))
            self.append(self.signals[sig] if sig else self.plain(), w)
        for sig, x, y, drill, size, ext in new_v:
            if not sig:
                self.warnings.append(f"via without a net at ({fmt(x)} {fmt(y)}) skipped (EAGLE vias need a signal)")
                continue
            v = ET.Element("via", {"x": fmt(x), "y": fmt(y), "extent": f"{ext[0]}-{ext[1]}", "drill": fmt(drill),
                                   "diameter": fmt(size)})
            self.append(self.signals[sig], v)
            eagle_dia = self.rules.via_diameter(drill / NM, size / NM) * NM
            if eagle_dia > size + 1000:
                self.stats["vias enlarged by Fusion restring rules"] += 1
        self.stats["tracks added"] = len(new_w)
        self.stats["vias added"] = sum(1 for v in new_v if v[0])
        if self.stats["vias enlarged by Fusion restring rules"]:
            self.warnings.append(f"{self.stats['vias enlarged by Fusion restring rules']} new via(s) are smaller than "
                                 "Fusion's restring design rules allow; Fusion will draw them larger - run DRC")

        # script order: rip up -> move parts -> draw.  MOVE drags every wire that ends on a pad of the part, so all
        # signals with copper on a moved part are ripped up too; ripped signals are redrawn completely (kept items
        # included) from the patched .brd, which keeps the script and the .brd identical.
        on_moved = {s for s, el in self.signals.items()
                    if any(cr.get("element") in self.moved for cr in el.findall("contactref"))
                    and (el.find("via") is not None
                         or any(int(w.get("layer")) in copper for w in el.findall("wire")))}
        redraw = sorted(s for s in removed_sigs | on_moved if s)
        lines = []
        if redraw:
            lines += ["", "# ---- signals that are re-routed or attached to moved parts: rip up the old copper "
                          "(redrawn below, unchanged pieces included)"]
            lines += [f"RIPUP {q(s)};" for s in redraw]
            redraw_w = [(s, int(w.get("layer")), (enm(w, "x1"), enm(w, "y1")), (enm(w, "x2"), enm(w, "y2")),
                         enm(w, "width"), float(w.get("curve") or 0)) for s in redraw
                        for w in self.signals[s].findall("wire") if int(w.get("layer")) in copper]
            redraw_v = [(s, enm(v, "x"), enm(v, "y"), enm(v, "drill"),
                         enm(v, "diameter") or round(self.rules.via_diameter(enm(v, "drill") / NM, 0) * NM),
                         tuple(int(t) for t in (v.get("extent") or "1-16").split("-")))
                        for s in redraw for v in self.signals[s].findall("via")]
            new_w = [w for w in new_w if w[0] not in redraw] + redraw_w
            new_v = [v for v in new_v if v[0] not in redraw] + redraw_v
        lines += self.place_lines
        self.script_copper(lines, new_w, new_v)

    def plain(self):
        p = self.board_el.find("plain")
        if p is None:
            p = ET.SubElement(self.board_el, "plain")
        return p

    def script_copper(self, lines, wires, vias):
        if wires:
            lines += ["", "# ---- tracks"]
            layer, width = None, None
            for sig, n, a, b, w, curve in sorted(wires, key=lambda t: (t[1], t[0], t[4])):
                if n != layer:
                    lines.append(f"LAYER {n};")
                    layer = n
                c = f" {curve:+.6f}".rstrip("0").rstrip(".") if curve else ""
                name = f"{q(sig)} " if sig else ""
                lines.append(f"WIRE {name}{fmt(w)} ({fmt(a[0])} {fmt(a[1])}){c} ({fmt(b[0])} {fmt(b[1])});")
        vias = [v for v in vias if v[0]]
        if vias:
            lines += ["", "# ---- vias"]
            drill = None
            for sig, x, y, d, size, ext in sorted(vias, key=lambda t: (t[3], t[0])):
                if d != drill:
                    lines.append(f"CHANGE DRILL {fmt(d)};")
                    drill = d
                lines.append(f"VIA {q(sig)} {fmt(size)} round {ext[0]}-{ext[1]} ({fmt(x)} {fmt(y)});")
        self.scr += lines

    # ------------------------------------------------------------------ pours
    def needed_layers(self) -> str:
        """Layers that must be visible for the script's RIPUP / MOVE / WIRE / VIA commands."""
        return " ".join(str(n) for n in sorted(set(self.cu.values()) | {17, 18, 19, 23, 24}))

    def delete_point(self, poly, all_polys, clear=500_000):
        """A point on the outline of `poly` at least `clear` away from every other object on its layer (in the
        board as the script leaves it before the pours), so DELETE (S x y) can only pick this polygon."""
        n = int(poly.get("layer"))
        segs = []  # (a, b, half width)

        def wire(a, b, curve, half):
            pts = path_pts(a, b, curve)
            segs.extend((p, r, half) for p, r in zip(pts, pts[1:]))

        def outline(p):
            vs = p.findall("vertex")
            for j, v in enumerate(vs):
                w = vs[(j + 1) % len(vs)]
                yield (enm(v, "x"), enm(v, "y")), (enm(w, "x"), enm(w, "y")), float(v.get("curve") or 0)

        for parent in [*self.signals.values(), self.plain()]:
            for w in parent.findall("wire"):
                if int(w.get("layer")) == n:
                    wire((enm(w, "x1"), enm(w, "y1")), (enm(w, "x2"), enm(w, "y2")), float(w.get("curve") or 0),
                         enm(w, "width") / 2)
            for v in parent.findall("via"):
                lo, hi = (int(t) for t in (v.get("extent") or "1-16").split("-"))
                if lo <= n <= hi:
                    r = max(enm(v, "diameter"), enm(v, "drill") * 2) / 2
                    segs.append(((enm(v, "x"), enm(v, "y")), (enm(v, "x"), enm(v, "y")), r))
        pl = self.plain()
        for r in pl.findall("rectangle"):
            if int(r.get("layer")) == n:
                x1, y1, x2, y2 = enm(r, "x1"), enm(r, "y1"), enm(r, "x2"), enm(r, "y2")
                c = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
                segs.extend((c[j - 1], c[j], 0) for j in range(4))
        for c in pl.findall("circle"):
            if int(c.get("layer")) == n:
                x, y, rad = enm(c, "x"), enm(c, "y"), enm(c, "radius")
                wire((x + rad, y), (x - rad, y), 180, enm(c, "width") / 2)
                wire((x - rad, y), (x + rad, y), 180, enm(c, "width") / 2)
        for t in pl.findall("text"):
            if int(t.get("layer")) == n:
                segs.append(((enm(t, "x"), enm(t, "y")), (enm(t, "x"), enm(t, "y")), enm(t, "size")))
        for p in all_polys:
            if p is not poly and int(p.get("layer")) == n:
                for a, b, curve in outline(p):
                    wire(a, b, curve, enm(p, "width") / 2)
        own = [(enm(v, "x"), enm(v, "y")) for v in poly.findall("vertex")]
        edges = sorted(outline(poly), key=lambda e: -math.dist(e[0], e[1]))
        for a, b, curve in edges:
            for t in (0.5, 0.25, 0.75):
                pts = path_pts(a, b, curve, steps=4) if curve else [a, b]
                p = (pts[2] if t == 0.5 else pts[1] if t == 0.25 else pts[3]) if curve else \
                    (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                p = (round(p[0]), round(p[1]))
                if min((math.dist(p, v) for v in own), default=clear) < clear:
                    continue
                if all(seg_dist(p, s, e) - h >= clear for s, e, h in segs):
                    return p
        return None

    def pours(self):
        records = self.bmap.get("zones", [])
        used = set()
        orig_polys = {name: sig.findall("polygon") for name, sig in self.signals.items()}
        orig_polys[""] = self.plain().findall("polygon")

        def um_set(outlines):
            return frozenset((round(x / 10_000), round(y / 10_000)) for o in outlines for x, y in o)

        rec_keys = [(r["signal"], r["layer"], um_set(r["outlines"])) for r in records]
        new = []
        for z in self.k.zones:
            if z.rule_area:
                if not z.name.startswith("EAGLE "):
                    self.warnings.append(f"keep-out / rule area '{z.name or '(unnamed)'}' from the router not "
                                         "transferred (add it in Fusion if it is needed)")
                continue
            layers = [l for l in z.layers if l in self.cu] if "*.Cu" not in z.layers else list(self.cu)
            if not layers:
                continue
            if z.opts.get("teardrop"):
                self.stats["teardrops skipped (Fusion has its own teardrop command)"] += 1
                continue
            sig = self.signal_of(z.net, "copper pour")
            if sig is None:
                continue
            key = um_set(z.outlines)
            for l in layers:
                n = self.cu[l]
                i = next((i for i, rk in enumerate(rec_keys) if i not in used and rk == (sig, n, key)), None)
                if i is not None:
                    used.add(i)
                    continue
                new.append((sig, n, z))
        all_polys = [p for ps in orig_polys.values() for p in ps]
        dels = defaultdict(list)
        for i, r in enumerate(records):
            if i in used:
                self.stats["pours kept"] += 1
                continue
            parent = self.signals.get(r["signal"]) if r["signal"] else self.plain()
            polys = orig_polys.get(r["signal"], [])
            if parent is not None and r["index"] < len(polys):
                el = polys[r["index"]]
                pick = self.delete_point(el, all_polys)
                self.remove(parent, el)
                self.stats["pours removed"] += 1
                if pick is None:
                    self.warnings.append(f"copper pour of {r['signal'] or '(no signal)'} on layer {r['layer']} is "
                                         "not in the routed board; removed in the .brd - delete it by hand when "
                                         "using the script (no unambiguous point on its outline to select it)")
                else:
                    dels[int(el.get("layer"))].append((r["signal"], pick))
        lines = []
        if dels:
            lines += ["", "# ---- copper pours the router removed: Shift+click on the outline deletes the whole "
                          "polygon; only its layer is shown so nothing else can be picked"]
            for n in sorted(dels):
                lines.append(f"DISPLAY NONE ?? {n};")
                for sig, (x, y) in dels[n]:
                    lines += [f"# {sig or '(no signal)'}", f"DELETE (S {fmt(x)} {fmt(y)});"]
            lines.append(f"DISPLAY ?? {self.needed_layers()};")
        for sig, n, z in new:
            o = z.opts
            if o.get("connect", "thermal") == "thermal":
                gap = o.get("thermal_gap")
                if gap is not None and abs(gap - self.rules.get("slThermalIsolate", 0.254) * NM) > 1000:
                    self.warnings.append(f"pour of {sig}: thermal gap differs from Fusion's global rule; "
                                         "use --match-pour-rules for a common gap or inspect in Fusion")
                spoke = o.get("thermal_bridge_width")
                actual_spoke = max(o.get("min_thickness", 254_000), round(self.rules.min_width * NM))
                if spoke is not None and abs(spoke - actual_spoke) > 1000:
                    self.warnings.append(f"pour of {sig}: KiCad thermal spoke width {fmt(spoke)} mm "
                                         f"differs from Fusion's derived {fmt(actual_spoke)} mm; "
                                         "inspect pad connections after repour")
            fill_hatch = o.get("fill") == "hatch"
            width = o.get("hatch_thickness" if fill_hatch else "min_thickness", 254_000)
            rank = min(6, max(1, 7 - z.priority))
            attrs = {"width": fmt(width), "layer": str(n), "isolate": fmt(o.get("clearance", 0)),
                     "rank": str(rank)}
            if fill_hatch:
                attrs["pour"] = "hatch"
                attrs["spacing"] = fmt(width + o.get("hatch_gap", 0))
            if o.get("connect") == "solid":
                attrs["thermals"] = "no"
            elif o.get("connect") == "none":
                self.warnings.append(f"pour of {sig}: KiCad 'no pad connection' has no EAGLE equivalent; "
                                     "thermals used")
            if o.get("island_removal") == 1:
                attrs["orphans"] = "yes"
            outs = [[to_eagle(p) for p in ol] for ol in z.outlines if len(ol) >= 3]
            for j, ol in enumerate(outs):
                if any(point_in(ol[0], other) for k_, other in enumerate(outs) if k_ != j):
                    self.warnings.append(f"pour of {sig}: a hole in the KiCad zone outline was not transferred")
                    continue
                pts = shrink(ol, width / 2)
                poly = ET.Element("polygon", dict(attrs))
                for x, y in pts:
                    self.append(poly, ET.Element("vertex", {"x": fmt(x), "y": fmt(y)}))
                self.append(self.signals[sig] if sig else self.plain(), poly)
                self.stats["pours added"] += 1
                pour = "HATCH" if fill_hatch else "SOLID"
                lines += [f"CHANGE ISOLATE {attrs['isolate']};", f"CHANGE RANK {rank};", f"CHANGE POUR {pour};",
                          f"CHANGE THERMALS {'OFF' if attrs.get('thermals') == 'no' else 'ON'};",
                          f"CHANGE ORPHANS {'ON' if attrs.get('orphans') == 'yes' else 'OFF'};"]
                if fill_hatch:
                    lines.append(f"CHANGE SPACING {attrs['spacing']};")
                coords = " ".join(f"({fmt(x)} {fmt(y)})" for x, y in pts + pts[:1])
                name = f"{q(sig)} " if sig else ""
                lines += [f"LAYER {n};", f"POLYGON {name}{attrs['width']} {coords};"]
        if lines:
            self.scr += ["", "# ---- copper pours (filled by RATSNEST below)"] + lines

    # ------------------------------------------------------------------ connectivity check
    def check_nets(self):
        orig = set()
        for name, sig in self.signals.items():
            for cr in sig.findall("contactref"):
                orig.add((cr.get("element"), cr.get("pad"), name))
        to_eagle_name = {r: i["eagle_name"] for r, i in self.bmap["elements"].items()}
        routed = set()
        for fp in self.k.footprints:
            if fp.ref not in to_eagle_name:
                continue
            for p in fp.pads:
                if p.net and not p.net.startswith("unconnected-(") and p.number:
                    routed.add((to_eagle_name[fp.ref], p.number, self.nets.get(p.net, p.net)))
        diff = (routed - orig) | (orig - routed)
        if diff:
            self.warnings.append(f"{len(diff)} pad/net assignment(s) differ from the Fusion design (not applied), "
                                 f"e.g. {sorted(diff)[:4]}")

    # ------------------------------------------------------------------ output
    def run(self):
        self.check_nets()
        self.placement()
        self.routing()
        self.pours()

    def write(self, out_dir: Path, name: str, source: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        head = self.text[:self.text.index("<eagle")]
        body = ET.tostring(self.root, encoding="unicode")
        brd = out_dir / f"{name}.brd"
        brd.write_text(head + body + "\n", encoding="utf-8", newline="\n")
        layers = self.root.findall("drawing/layers/layer")
        shown = " ".join(l.get("number") for l in layers if l.get("visible") == "yes")
        needed = self.needed_layers()
        scr = out_dir / f"{name}.scr"
        hdr = [f"# Generated by kicad_to_fusion.py from {source.name}",
               f"# Apply to the board of the original Fusion 360 design ({self.brd.stem}):",
               "#   open the PCB document, then Automation > Run Script (or type: SCRIPT <path to this file>).",
               "# Part placement, tracks, vias and copper pours are updated in place; one Undo step per command.",
               "# RIPUP and MOVE only act on visible layers, so copper and origin layers are shown while it runs;",
               "# the finest grid keeps typed coordinates exact.  Grid and visible layers are restored at the end.",
               "", "GRID MM FINEST;", f"DISPLAY ?? {needed};", "SET WIRE_BEND 2;", "SET OPTIMIZING OFF;"]
        if self.via_rules_changed or self.pour_rules_changed:
            dru = out_dir / f"{name}.dru"
            params = self.board_el.findall("designrules/param")
            dru.write_text("\n".join(f"{p.get('name')} = {p.get('value')}" for p in params) + "\n",
                           encoding="utf-8")
            hdr += ["# Load explicitly matched design rules before drawing copper.",
                    f"DRC LOAD {q(str(dru.resolve()))};"]
        tail = ["", "SET OPTIMIZING ON;", "RATSNEST;"] + ([f"DISPLAY NONE ?? {shown};"] if shown else []) + \
               ["GRID LAST;", ""]
        scr.write_text("\n".join(hdr + self.scr + tail), encoding="utf-8", newline="\r\n")
        return brd, scr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcb", type=Path, help="routed .kicad_pcb (e.g. downloaded from Quilter)")
    ap.add_argument("--brd", type=Path, required=True, help="original EAGLE .brd exported from Fusion 360")
    ap.add_argument("--map", type=Path, required=True, help="<project>.board_map.json from eagle_brd_to_kicad.py")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--name", help="output base name (default: <brd name>_routed)")
    ap.add_argument("--match-via-rules", action="store_true",
                    help="explicitly adjust via-only restring rules to preserve routed via diameters")
    ap.add_argument("--match-pour-rules", action="store_true",
                    help="match a common zone thermal gap using Fusion's global thermal-isolate rule")
    a = ap.parse_args(argv)
    p = Patch(a.pcb, a.brd, json.loads(a.map.read_text(encoding="utf-8")))
    if a.match_via_rules:
        p.match_via_rules()
    if a.match_pour_rules:
        p.match_pour_rules()
    p.run()
    brd, scr = p.write(a.out_dir, a.name or f"{a.brd.stem}_routed", a.pcb)
    for w in p.warnings:
        print("WARNING: " + w)
    for n in p.notes:
        print("NOTE: " + n)
    s = p.stats
    print(f"parts: {s['parts placed']} placed/moved ({s['parts changed side']} changed side), "
          f"{s['parts unchanged']} unchanged; tracks: +{s['tracks added']} -{s['tracks removed']} "
          f"={s['tracks kept']}; vias: +{s['vias added']} -{s['vias removed']} ={s['vias kept']}; "
          f"pours: +{s['pours added']} -{s['pours removed']} ={s['pours kept']}"
          + (f"; {s['zero-length tracks skipped']} zero-length tracks skipped" if s['zero-length tracks skipped']
             else "")
          + "".join(f"; {v} {k}" for k, v in s.items() if k.startswith("teardrops")))
    print(f"wrote {scr}\nwrote {brd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
