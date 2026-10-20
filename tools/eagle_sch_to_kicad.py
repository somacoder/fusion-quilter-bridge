#!/usr/bin/env python3
"""EAGLE 9.x / Fusion 360 Electronics schematic (.sch) -> KiCad 9 schematic (.kicad_sch).

KiCad's own EAGLE *schematic* importer only exists inside the Eeschema GUI (kicad-cli cannot
load .sch files), so this headless converter fills that gap.  It is deterministic, uses only the
Python standard library and is validated by tools/verify_forward.py against KiCad's own netlister.

Outputs (in --out-dir):
  <project>.kicad_sch                 root schematic (plus <project>_sheetN.kicad_sch if multi-sheet)
  <project>-eagle-import.kicad_sym    symbol library (same symbols as embedded in the schematic)
  sym-lib-table                       project symbol library table
  <project>.eagle_map.json            symbol UUID paths / fields / expected nets for the board step
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagle_common import (  # noqa: E402
    Sym,
    arc_mid_and_center,
    det_uuid,
    dumps,
    eagle_overbar_to_kicad,
    eagle_pin_display_name,
    kicad_net_name,
    kicad_ref_map,
    parse_rot,
    rotate,
    strip_html,
)

SCH_VERSION = 20250114
SYM_VERSION = 20241209
GENERATOR = "eagle_sch_to_kicad"
GENERATOR_VERSION = "9.0"

PIN_ETYPE = {
    "nc": "no_connect", "in": "input", "out": "output", "io": "bidirectional",
    "oc": "open_collector", "hiz": "tri_state", "pas": "passive", "pwr": "power_in", "sup": "power_out",
}
PIN_STYLE = {"none": "line", "dot": "inverted", "clk": "clock", "dotclk": "inverted_clock"}
PIN_LEN = {"point": 0.0, "short": 2.54, "middle": 5.08, "long": 7.62}
ALIGN = {
    "bottom-left": ["left", "bottom"], "bottom-center": ["bottom"], "bottom-right": ["right", "bottom"],
    "center-left": ["left"], "center": [], "center-right": ["right"],
    "top-left": ["left", "top"], "top-center": ["top"], "top-right": ["right", "top"],
}
MANDATORY = {"reference", "value", "footprint", "datasheet", "description"}
PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]
GRID = 2.54
MARGIN = 12.7
TOL = 1e-3


def r4(v: float) -> float:
    return round(v, 4) + 0.0


def pkey(p) -> tuple:
    return (round(p[0], 3) + 0.0, round(p[1], 3) + 0.0)


def find(el, path, **attrs):
    for c in el.findall(path):
        if all(c.get(k) == v for k, v in attrs.items()):
            return c
    raise KeyError(f"{path} {attrs} not found")


def fnum(el, k, d=0.0) -> float:
    v = el.get(k)
    return float(v) if v is not None else d


def sanitize_item_name(name: str) -> str:
    return re.sub(r'[\s:/\\"]', "_", name)


def font(h, hide_size=False):
    s = 0 if hide_size else h
    return ["font", ["size", r4(s), r4(s * (0.85 if s else 1))]]


def effects(h, justify=None, hide=False):
    e = ["effects", font(h)]
    if justify:
        e.append(["justify", *[Sym(j) for j in justify]])
    if hide:
        e.append(["hide", Sym("yes")])
    return e


def prop(name, value, x, y, angle=0, h=1.27, justify=None, hide=False, show_name=False):
    n = ["property", name, value, ["at", r4(x), r4(y), angle]]
    if show_name:
        n.append(["show_name", Sym("yes")])
    n.append(effects(h, justify, hide))
    return n


def stroke(w=0.0, kind="default"):
    return ["stroke", ["width", r4(w)], ["type", Sym(kind)]]


def fill(kind="none"):
    return ["fill", ["type", Sym(kind)]]


def readable(deg: float, justify: list, spin: bool, mirror: bool):
    """Mimic EAGLE's 'keep text readable' rule and map to a KiCad angle (0/90) + justification."""
    j = list(justify)
    if mirror:
        j = [{"left": "right", "right": "left"}.get(x, x) for x in j]
    deg = deg % 360
    if not spin and 90 < deg <= 270:
        deg -= 180
        j = [{"left": "right", "right": "left", "top": "bottom", "bottom": "top"}.get(x, x) for x in j]
    deg = int(round(deg)) % 360
    if deg not in (0, 90):
        deg = 0
    return deg, j


def point_on_segment_interior(p, a, b) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < TOL * TOL:
        return False
    cross = (px - ax) * dy - (py - ay) * dx
    if abs(cross) / math.sqrt(L2) > TOL:
        return False
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    return TOL < t * math.sqrt(L2) < math.sqrt(L2) - TOL


def planarize(wires, extra_points):
    """Split wires at every endpoint/pin/junction lying on their interior and drop duplicate pieces.
    EAGLE tolerates overlapping collinear wires and T-joints mid-wire; KiCad only joins at endpoints."""
    pts = {pkey(p): p for a, b in wires for p in (a, b)}
    pts.update((pkey(p), p) for p in extra_points)
    out, seen = [], set()
    for a, b in wires:
        inner = sorted((p for p in pts.values() if point_on_segment_interior(p, a, b)),
                       key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
        chain = [a, *inner, b]
        for u, v in zip(chain, chain[1:]):
            key = frozenset((pkey(u), pkey(v)))
            if len(key) == 2 and key not in seen:
                seen.add(key)
                out.append((u, v))
    return out


class InstXform:
    """EAGLE instance placement; matches KiCad's (at x y angle)(mirror y) semantics:
    rotate CCW by angle, then mirror horizontally about the symbol origin."""

    def __init__(self, x, y, rot):
        self.x, self.y = x, y
        self.mirror, self.spin, self.deg = parse_rot(rot)
        if abs(self.deg / 90 - round(self.deg / 90)) > 1e-6:
            raise ValueError(f"schematic instance rotation {rot} is not a multiple of 90")
        self.deg = int(round(self.deg)) % 360

    def pt(self, lx, ly):
        x, y = rotate(lx, ly, self.deg)
        if self.mirror:
            x = -x
        return self.x + x, self.y + y

    def vec(self, vx, vy):
        x, y = rotate(vx, vy, self.deg)
        return (-x if self.mirror else x), y

# --------------------------------------------------------------------------- graphics
LINE_STYLE = {"continuous": "solid", "longdash": "dash", "shortdash": "dash", "dashdot": "dash_dot"}


def clean_text(t: str) -> str:
    return eagle_overbar_to_kicad(t or "").replace("~{}", "")


def arc_points(x1, y1, x2, y2, curve, n=8):
    (mx, my), (cx, cy) = arc_mid_and_center(x1, y1, x2, y2, curve)
    pts = []
    for k in range(1, n):
        vx, vy = rotate(x1 - cx, y1 - cy, curve * k / n)
        pts.append((cx + vx, cy + vy))
    return pts


def graphic_nodes(parent, T, ctx: str, seed: str, skip_text=True):
    """EAGLE primitives -> KiCad shapes.  ctx is 'lib' (symbol body, y-up) or 'sch' (sheet)."""
    out, warns = [], []
    for i, c in enumerate(parent):
        uid = [["uuid", det_uuid(seed, i)]] if ctx == "sch" else []
        tag = c.tag
        if tag == "wire":
            x1, y1, x2, y2 = (fnum(c, k) for k in ("x1", "y1", "x2", "y2"))
            st = stroke(fnum(c, "width"), LINE_STYLE.get(c.get("style") or "continuous", "solid"))
            curve = fnum(c, "curve")
            if abs(curve) > 1e-9:
                (mx, my), _ = arc_mid_and_center(x1, y1, x2, y2, curve)
                out.append(["arc", ["start", *T(x1, y1)], ["mid", *T(mx, my)], ["end", *T(x2, y2)], st,
                            fill("none"), *uid])
            else:
                node = ["polyline", ["pts", ["xy", *T(x1, y1)], ["xy", *T(x2, y2)]], st]
                if ctx == "lib":
                    node.append(fill("none"))
                out.append(node + uid)
        elif tag in ("rectangle", "frame"):
            x1, y1, x2, y2 = (fnum(c, k) for k in ("x1", "y1", "x2", "y2"))
            _, _, deg = parse_rot(c.get("rot"))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            hw, hh = abs(x2 - x1) / 2, abs(y2 - y1) / 2
            filled = tag == "rectangle"
            if abs(deg % 90) > 1e-6:
                corners = [rotate(dx, dy, deg) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh))]
                out.append(["polyline", ["pts", *[["xy", *T(cx + px, cy + py)] for px, py in corners]],
                            stroke(0), fill("outline" if filled else "none"), *uid])
                continue
            if int(round(deg)) % 180 == 90:
                hw, hh = hh, hw
            out.append(["rectangle", ["start", *T(cx - hw, cy + hh)], ["end", *T(cx + hw, cy - hh)],
                        stroke(0 if filled else 0.254), fill("outline" if filled else "none"), *uid])
            if tag == "frame":
                warns.append("EAGLE frame converted to a plain rectangle (no zone letters/numbers)")
        elif tag == "circle":
            x, y, r, w = fnum(c, "x"), fnum(c, "y"), fnum(c, "radius"), fnum(c, "width")
            out.append(["circle", ["center", *T(x, y)], ["radius", r4(r)], stroke(w),
                        fill("outline" if w == 0 else "none"), *uid])
        elif tag == "polygon":
            vs = c.findall("vertex")
            pts = []
            for k, v in enumerate(vs):
                x, y = fnum(v, "x"), fnum(v, "y")
                pts.append((x, y))
                cv = fnum(v, "curve")
                if abs(cv) > 1e-9:
                    nv = vs[(k + 1) % len(vs)]
                    pts.extend(arc_points(x, y, fnum(nv, "x"), fnum(nv, "y"), cv))
            pts.append(pts[0])
            out.append(["polyline", ["pts", *[["xy", *T(x, y)] for x, y in pts]], stroke(fnum(c, "width")),
                        fill("outline"), *uid])
        elif tag == "text" and not skip_text:
            out.append(text_node(c, T, ctx, uid))
        elif tag in ("pin", "text", "description", "attribute"):
            continue
        elif tag in ("dimension", "hole", "spline"):
            warns.append(f"EAGLE <{tag}> graphics not converted")
    return out, warns


def text_node(c, T, ctx, uid, xf: InstXform | None = None, content=None):
    x, y = fnum(c, "x"), fnum(c, "y")
    mirror, spin, deg = parse_rot(c.get("rot"))
    h = fnum(c, "size", 1.778)
    ang, just = readable(deg, ALIGN[c.get("align") or "bottom-left"], spin, mirror)
    txt = clean_text(content if content is not None else c.text)
    if ctx == "lib":
        return ["text", txt, ["at", *T(x, y), ang * 10], effects(h, just)]
    return ["text", txt, ["exclude_from_sim", Sym("no")], ["at", *T(x, y), ang], effects(h, just), *uid]


# --------------------------------------------------------------------------- library symbols
def symbol_extent_points(sym):
    pts = []
    for c in sym:
        if c.tag in ("wire", "rectangle", "frame"):
            pts += [(fnum(c, "x1"), fnum(c, "y1")), (fnum(c, "x2"), fnum(c, "y2"))]
        elif c.tag == "circle":
            x, y, r = fnum(c, "x"), fnum(c, "y"), fnum(c, "radius")
            pts += [(x - r, y - r), (x + r, y + r)]
        elif c.tag == "polygon":
            pts += [(fnum(v, "x"), fnum(v, "y")) for v in c.findall("vertex")]
        elif c.tag in ("text", "pin"):
            pts.append((fnum(c, "x"), fnum(c, "y")))
    return pts or [(0.0, 0.0)]


def build_lib_symbol(name, nick, ds, dev, gates, fp_lib, warnings):
    """One KiCad symbol per EAGLE deviceset+device (gates -> units, multi-pad pins -> stacked pins)."""
    connects = defaultdict(list)
    for c in dev.findall("connects/connect"):
        connects[(c.get("gate"), c.get("pin"))].extend((c.get("pad") or "").split())
    pkg = dev.get("package")
    footprint = f"{fp_lib}:{pkg}" if pkg else ""
    ident = lambda x, y: (r4(x), r4(y))  # noqa: E731  (EAGLE symbol space == KiCad library space, y-up)
    bodies, pin_meta, vis, gate_units = [], OrderedDict(), [], {}
    ref_text = val_text = None
    for ui, (gname, _gate, sym) in enumerate(gates, 1):
        gate_units[gname] = ui
        shapes, w = graphic_nodes(sym, ident, "lib", "")
        warnings.extend(f"{ds.get('name')}: {m}" for m in w)
        texts = []
        for t in sym.findall("text"):
            raw = (t.text or "").strip()
            up = raw.upper()
            if up == ">NAME":
                ref_text = ref_text if ref_text is not None else t
                continue
            if up == ">VALUE":
                val_text = val_text if val_text is not None else t
                continue
            content = None
            if raw.startswith(">"):
                content = {"PART": "${REFERENCE}", "GATE": gname}.get(up[1:], "${" + raw[1:] + "}")
            texts.append(text_node(t, ident, "lib", [], content=content))
        pins = []
        for p in sym.findall("pin"):
            pname = p.get("name")
            _, _, pdeg = parse_rot(p.get("rot"))
            pdeg = int(round(pdeg)) % 360
            px, py = fnum(p, "x"), fnum(p, "y")
            etype = PIN_ETYPE.get(p.get("direction") or "io", "bidirectional")
            style = PIN_STYLE.get(p.get("function") or "none", "line")
            length = PIN_LEN.get(p.get("length") or "long", 7.62)
            v = p.get("visible") or "both"
            show_name, show_num = v in ("both", "pin"), v in ("both", "pad")
            pads = connects.get((gname, pname), [])
            if not pads:
                if pkg:
                    warnings.append(f"{ds.get('name')}{dev.get('name')}: pin {gname}.{pname} has no pad")
                pads = [eagle_pin_display_name(pname)]
            ox, oy = rotate(-1.0, 0.0, pdeg)
            pin_meta[(gname, pname)] = {"pos": (px, py), "out": (ox, oy), "numbers": pads, "unit": ui,
                                        "direction": p.get("direction") or "io"}
            vis.append((show_name, show_num))
            for si, num in enumerate(pads):
                pins.append(["pin", Sym(etype), Sym(style), ["at", r4(px), r4(py), pdeg], ["length", r4(length)],
                             ["name", eagle_pin_display_name(pname), ["effects", font(1.27, not show_name or si > 0)]],
                             ["number", num, ["effects", font(1.27, not show_num or si > 0)]]])
        bodies.append(["symbol", f"{name}_{ui}_1", *shapes, *texts, *pins])

    def field_at(t, fname, value):
        if t is None:
            return prop(fname, value, 0, 0, 0, 1.778, None, hide=True)
        mirror, spin, deg = parse_rot(t.get("rot"))
        ang, just = readable(deg, ALIGN[t.get("align") or "bottom-left"], spin, mirror)
        return prop(fname, value, fnum(t, "x"), fnum(t, "y"), ang, fnum(t, "size", 1.778), just)

    head = []
    if vis and all(not n for _, n in vis):
        head.append(["pin_numbers", ["hide", Sym("yes")]])
    if vis and all(not n for n, _ in vis):
        head.append(["pin_names", ["hide", Sym("yes")]])
    yes = lambda b: Sym("yes" if b else "no")  # noqa: E731
    head += [["exclude_from_sim", Sym("no")], ["in_bom", yes(bool(pkg))], ["on_board", yes(bool(pkg))]]
    props = [
        field_at(ref_text, "Reference", ds.get("prefix") or ""),
        field_at(val_text, "Value", ds.get("name")),
        prop("Footprint", footprint, 0, 0, hide=True),
        prop("Datasheet", "", 0, 0, hide=True),
        prop("Description", strip_html(ds.findtext("description")), 0, 0, hide=True),
    ]
    swappable = len({g[2].get("name") for g in gates}) == 1 and all(
        int(g[1].get("swaplevel") or 0) > 0 for g in gates)
    if len(gates) > 1 and not swappable:
        props.append(["property", "ki_locked", "", ["at", 0, 0, 0], effects(1.27)])
    tail = [*bodies, ["embedded_fonts", Sym("no")]]
    meta = {"pins": pin_meta, "ref_text": ref_text, "val_text": val_text, "footprint": footprint,
            "package": pkg, "gate_units": gate_units, "symbols": {g[0]: g[2] for g in gates}}
    lib_node = ["symbol", name, *head, *props, *tail]
    sch_node = ["symbol", f"{nick}:{name}", *head, *props, *tail]
    return name, lib_node, sch_node, meta


# --------------------------------------------------------------------------- converter
class SchematicConverter:
    def __init__(self, sch_path: Path, brd_path: Path | None, project: str):
        self.root = ET.parse(sch_path).getroot()
        self.sch = self.root.find("drawing/schematic")
        if self.sch is None:
            raise SystemExit(f"{sch_path} is not an EAGLE schematic")
        self.project = project
        self.symlib = f"{project}-eagle-import"
        self.fplib = project
        self.libs = {(l.get("name"), l.get("urn") or ""): l for l in self.sch.findall("libraries/library")}
        self.parts = OrderedDict((p.get("name"), p) for p in self.sch.findall("parts/part"))
        self.board = {}
        if brd_path:
            b = ET.parse(brd_path).getroot()
            self.board = {e.get("name"): e for e in b.findall("drawing/board/elements/element")}
        self.refs = kicad_ref_map(list(self.parts) + list(self.board))
        self.warnings: list[str] = []
        self.pin_off_wire: list[str] = []
        self.libsyms: "OrderedDict[tuple, tuple]" = OrderedDict()
        self.names: dict[str, tuple] = {}
        self.root_uuid = det_uuid("root", project)
        self.components: "OrderedDict[str, dict]" = OrderedDict()
        self.expected = defaultdict(set)
        self.files: dict[str, list] = OrderedDict()
        if self.sch.find("modules/module") is not None:
            raise SystemExit("EAGLE design blocks/modules (hierarchical schematics) are not supported yet")

    # ---------------------------------------------------------------- lookups
    def lib_for(self, name, urn):
        if (name, urn or "") in self.libs:
            return self.libs[(name, urn or "")]
        cands = [l for (n, _u), l in self.libs.items() if n == name]
        if len(cands) == 1:
            return cands[0]
        raise KeyError(f"library {name} ({urn}) not found in schematic")

    def device(self, part):
        lib = self.lib_for(part.get("library"), part.get("library_urn"))
        ds = find(lib, "devicesets/deviceset", name=part.get("deviceset"))
        dev = find(ds, "devices/device", name=part.get("device") or "")
        gates = [(g.get("name"), g, find(lib, "symbols/symbol", name=g.get("symbol")))
                 for g in ds.findall("gates/gate")]
        key = (lib.get("name"), lib.get("urn") or "", ds.get("name"), dev.get("name") or "")
        if key not in self.libsyms:
            base = sanitize_item_name(ds.get("name") + (dev.get("name") or ""))
            name, n = base, 1
            while name in self.names and self.names[name] != key:
                n += 1
                name = f"{base}_{sanitize_item_name(lib.get('name'))}" if n == 2 else f"{base}_{n}"
            self.names[name] = key
            self.libsyms[key] = build_lib_symbol(name, self.symlib, ds, dev, gates, self.fplib, self.warnings)
        return ds, dev, gates, self.libsyms[key]

    def part_fields(self, part, ds, dev):
        attrs = OrderedDict()
        tech = part.get("technology") or ""
        for t in dev.findall("technologies/technology"):
            if (t.get("name") or "") == tech:
                for a in t.findall("attribute"):
                    attrs[a.get("name")] = a.get("value") or ""
        for a in part.findall("attribute"):
            attrs[a.get("name")] = a.get("value") or ""
        be = self.board.get(part.get("name"))
        if be is not None:
            for a in be.findall("attribute"):
                n = a.get("name")
                if n not in attrs and a.get("value") is not None and n.upper() not in ("NAME", "VALUE"):
                    attrs[n] = a.get("value")
        if be is not None and be.get("value") is not None:
            value = be.get("value")
        elif part.get("value") is not None:
            value = part.get("value")
        else:
            value = ds.get("name").replace("*", tech)
            value = value.replace("?", dev.get("name") or "") if "?" in value else value + (dev.get("name") or "")
        datasheet, description = "", strip_html(ds.findtext("description"))
        fields = OrderedDict()
        for n, v in attrs.items():
            ln = n.lower()
            if ln in ("name", "value"):
                continue
            if ln == "datasheet":
                datasheet = v
                continue
            if ln == "description":
                description = v or description
                continue
            fields[f"EAGLE_{n}" if ln in MANDATORY else n] = v
        return value, datasheet, description, fields

    # ---------------------------------------------------------------- sheets
    def convert(self):
        sheets = self.sch.findall("sheets/sheet")
        root_file = f"{self.project}.kicad_sch"
        if len(sheets) == 1:
            self.files[root_file] = self.convert_sheet(0, sheets[0], [], root_file, "/", is_root=True)
        else:
            sheet_nodes = []
            for i, sh in enumerate(sheets):
                fname = f"{self.project}_sheet{i + 1}.kicad_sch"
                title = strip_html(sh.findtext("description")) or f"Sheet{i + 1}"
                title = re.sub(r"[/:]", "_", title.splitlines()[0])[:40]
                su = det_uuid("sheet", i)
                self.files[fname] = self.convert_sheet(i, sh, [su], fname, f"/{title}/")
                x, y = 25.4 + (i % 4) * 63.5, 25.4 + (i // 4) * 30.48
                sheet_nodes.append([
                    "sheet", ["at", x, y], ["size", 50.8, 15.24], ["exclude_from_sim", Sym("no")],
                    ["in_bom", Sym("yes")], ["on_board", Sym("yes")], ["dnp", Sym("no")],
                    ["fields_autoplaced", Sym("yes")], stroke(0.1524, "solid"), ["fill", ["color", 0, 0, 0, 0.0]],
                    ["uuid", su],
                    prop("Sheetname", title, x, r4(y - 0.7116), 0, 1.27, ["left", "bottom"]),
                    prop("Sheetfile", fname, x, r4(y + 15.24 + 0.5874), 0, 1.27, ["left", "top"]),
                    ["instances", ["project", self.project, ["path", f"/{self.root_uuid}", ["page", str(i + 2)]]]],
                ])
            self.files[root_file] = self.file_node(self.root_uuid, ["paper", "A4"], [], sheet_nodes, is_root=True)
        return self.files

    def file_node(self, file_uuid, paper, lib_nodes, body, is_root):
        node = ["kicad_sch", ["version", SCH_VERSION], ["generator", GENERATOR],
                ["generator_version", GENERATOR_VERSION], ["uuid", file_uuid], paper,
                ["title_block", ["title", self.project],
                 ["comment", 1, "Converted from EAGLE/Fusion 360 by eagle_sch_to_kicad"]],
                ["lib_symbols", *lib_nodes], *body]
        if is_root:
            node.append(["sheet_instances", ["path", "/", ["page", "1"]]])
        node.append(["embedded_fonts", Sym("no")])
        return node

    def convert_sheet(self, idx, sheet, sheet_uuids, fname, sheetname, is_root=False):
        # ---- pass 1: placements + bounding box in EAGLE coordinates (y-up)
        placed, pts = [], []
        for inst in sheet.findall("instances/instance"):
            part = self.parts[inst.get("part")]
            ds, dev, gates, (sname, _ln, snode, meta) = self.device(part)
            gname = inst.get("gate")
            xf = InstXform(fnum(inst, "x"), fnum(inst, "y"), inst.get("rot"))
            placed.append((inst, part, ds, dev, sname, snode, meta, xf, gname))
            pts += [xf.pt(x, y) for x, y in symbol_extent_points(meta["symbols"][gname])]
            pts += [(fnum(a, "x"), fnum(a, "y")) for a in inst.findall("attribute") if a.get("x") is not None]
        for w in sheet.iter("wire"):
            pts += [(fnum(w, "x1"), fnum(w, "y1")), (fnum(w, "x2"), fnum(w, "y2"))]
        for tag in ("rectangle", "frame"):
            for r in sheet.iter(tag):
                pts += [(fnum(r, "x1"), fnum(r, "y1")), (fnum(r, "x2"), fnum(r, "y2"))]
        for c in sheet.iter("circle"):
            pts += [(fnum(c, "x") - fnum(c, "radius"), fnum(c, "y") - fnum(c, "radius")),
                    (fnum(c, "x") + fnum(c, "radius"), fnum(c, "y") + fnum(c, "radius"))]
        pts += [(fnum(v, "x"), fnum(v, "y")) for v in sheet.iter("vertex")]
        for t in list(sheet.iter("text")) + list(sheet.iter("label")) + list(sheet.iter("junction")):
            x, y, s = fnum(t, "x"), fnum(t, "y"), fnum(t, "size", 1.778)
            pts.append((x, y))
            if t.tag == "text" and not t.get("rot"):
                longest = max((len(line) for line in (t.text or "").splitlines()), default=0)
                pts.append((x + longest * s * 0.8, y + s))
        xs, ys = [p[0] for p in pts] or [0.0], [p[1] for p in pts] or [0.0]
        xoff = math.ceil((MARGIN - min(xs)) / GRID) * GRID
        yoff = math.ceil((max(ys) + MARGIN) / GRID) * GRID
        width, height = max(xs) + xoff + MARGIN, yoff - min(ys) + MARGIN
        paper = next((["paper", n] for n, pw, ph in PAPERS if width <= pw and height <= ph),
                     ["paper", "User", r4(width), r4(height)])

        def T(x, y):
            return r4(x + xoff), r4(yoff - y)

        # ---- symbols
        yes = lambda b: Sym("yes" if b else "no")  # noqa: E731
        sheet_path = "/" + "/".join([self.root_uuid] + sheet_uuids)
        used, symbols, pin_world, placed_meta = OrderedDict(), [], {}, {}
        for inst, part, ds, dev, sname, snode, meta, xf, gname in placed:
            ref = self.refs[part.get("name")]
            placed_meta[ref] = meta
            used[sname] = snode
            unit = meta["gate_units"][gname]
            value, datasheet, description, fields = self.part_fields(part, ds, dev)
            su = det_uuid("symbol", ref, gname)
            X, Y = T(xf.x, xf.y)
            smashed = {a.get("name").upper(): a for a in inst.findall("attribute") if a.get("x") is not None}

            def fld(fname_, val, key, lib_text, hidden_default):
                a = smashed.get(key)
                hide, show_name = hidden_default, False
                if a is not None:
                    mirror, spin, deg = parse_rot(a.get("rot"))
                    ang, just = readable(deg, ALIGN[a.get("align") or "bottom-left"], spin, mirror)
                    disp = a.get("display") or "value"
                    hide, show_name = disp == "off", disp in ("name", "both")
                    pos, h = T(fnum(a, "x"), fnum(a, "y")), fnum(a, "size", 1.778)
                elif lib_text is not None and not hidden_default:
                    mirror, spin, deg = parse_rot(lib_text.get("rot"))
                    ang, just = readable(deg + xf.deg, ALIGN[lib_text.get("align") or "bottom-left"], spin,
                                         mirror ^ xf.mirror)
                    pos, h = T(*xf.pt(fnum(lib_text, "x"), fnum(lib_text, "y"))), fnum(lib_text, "size", 1.778)
                else:
                    ang, just, pos, h, hide = 0, None, (X, Y), 1.27, True
                if xf.deg in (90, 270):
                    ang = 90 - ang  # KiCad turns field text together with the symbol
                return prop(fname_, val, pos[0], pos[1], ang, h, just, hide, show_name)

            props = [fld("Reference", ref, "NAME", meta["ref_text"], False),
                     fld("Value", value, "VALUE", meta["val_text"], False),
                     prop("Footprint", meta["footprint"], X, Y, hide=True),
                     prop("Datasheet", datasheet, X, Y, hide=True),
                     prop("Description", description, X, Y, hide=True)]
            props += [fld(n, v, n.upper(), None, True) for n, v in fields.items()]
            pins = []
            for (g, p), pm in meta["pins"].items():
                if g != gname:
                    continue
                pin_world[(ref, g, p)] = (xf.pt(*pm["pos"]), xf.vec(*pm["out"]), pm["numbers"])
                pins += [["pin", num, ["uuid", det_uuid("pin", ref, g, p, num)]] for num in pm["numbers"]]
            node = ["symbol", ["lib_id", f"{self.symlib}:{sname}"], ["at", X, Y, xf.deg]]
            if xf.mirror:
                node.append(["mirror", Sym("y")])
            node += [["unit", unit], ["exclude_from_sim", Sym("no")], ["in_bom", yes(meta["package"])],
                     ["on_board", yes(meta["package"])], ["dnp", Sym("no")], ["uuid", su], *props, *pins,
                     ["instances", ["project", self.project,
                                    ["path", sheet_path, ["reference", ref], ["unit", unit]]]]]
            symbols.append(node)
            if ref not in self.components or unit < self.components[ref]["unit"]:
                self.components[ref] = {
                    "unit": unit, "path": "/" + "/".join(sheet_uuids + [su]), "sheetname": sheetname,
                    "sheetfile": fname, "value": value, "footprint": meta["footprint"], "package": meta["package"],
                    "datasheet": datasheet, "description": description, "fields": fields,
                    "lib_id": f"{self.symlib}:{sname}", "on_board": bool(meta["package"])}

        # ---- nets: EAGLE connectivity is by segment membership + net name; KiCad's is geometric.
        # Every segment gets a global label (EAGLE net names are global across sheets, and global labels
        # give un-prefixed net names identical to the board's), pins not touched by a wire get a label,
        # and T-joints get junctions.
        wires_out, labels_out, junc_out = [], [], []
        net_pts, net_wires, referenced = defaultdict(set), defaultdict(list), set()
        for net in sheet.findall("nets/net"):
            kname = kicad_net_name(net.get("name"))
            for si, seg in enumerate(net.findall("segment")):
                ws = []
                for w in seg.findall("wire"):
                    a, b = (fnum(w, "x1"), fnum(w, "y1")), (fnum(w, "x2"), fnum(w, "y2"))
                    if abs(fnum(w, "curve")) > 1e-9:
                        self.warnings.append(f"net {kname}: curved net wire drawn straight (connectivity unchanged)")
                    if pkey(a) != pkey(b):
                        ws.append((a, b))
                pins = []
                for pr in seg.findall("pinref"):
                    k = (self.refs.get(pr.get("part"), pr.get("part")), pr.get("gate"), pr.get("pin"))
                    if k not in pin_world:
                        self.warnings.append(f"ERROR: net {kname}: pinref {k} has no placed gate on this sheet")
                        continue
                    referenced.add(k)
                    pos, out, nums = pin_world[k]
                    pins.append((pos, out))
                    self.expected[kname].update((k[0], n) for n in nums)
                raw_ends = {pkey(p) for a, b in ws for p in (a, b)}
                ws = planarize(ws, [p for p, _ in pins] +
                               [(fnum(j, "x"), fnum(j, "y")) for j in seg.findall("junction")])
                ends, degree = OrderedDict(), defaultdict(int)
                for a, b in ws:
                    for p, o in ((a, b), (b, a)):
                        degree[pkey(p)] += 1
                        ends.setdefault(pkey(p), (p, o))
                for p, _ in pins:
                    degree[pkey(p)] += 1
                junc = OrderedDict((pkey((fnum(j, "x"), fnum(j, "y"))), (fnum(j, "x"), fnum(j, "y")))
                                   for j in seg.findall("junction"))
                for k, p in [(k, v[0]) for k, v in ends.items()] + [(pkey(p), p) for p, _ in pins]:
                    if degree[k] >= 3 or any(point_on_segment_interior(p, a, b) for a, b in ws):
                        junc[k] = p
                labels = OrderedDict()
                for lab in seg.findall("label"):
                    lp, size = (fnum(lab, "x"), fnum(lab, "y")), max(fnum(lab, "size", 1.778), 0.8)
                    if ends:
                        p, o = min(ends.values(), key=lambda e: math.hypot(e[0][0] - lp[0], e[0][1] - lp[1]))
                        labels.setdefault(pkey(p), (p, (p[0] - o[0], p[1] - o[1]), size))
                    elif pins:
                        labels.setdefault(pkey(pins[0][0]), (pins[0][0], pins[0][1], size))
                if not labels:
                    pin_keys = {pkey(p) for p, _ in pins}
                    free = [v for k, v in ends.items() if degree[k] == 1 and k not in pin_keys] or list(ends.values())
                    if free:
                        p, o = free[0]
                        labels[pkey(p)] = (p, (p[0] - o[0], p[1] - o[1]), 1.27)
                    elif pins:
                        labels[pkey(pins[0][0])] = (pins[0][0], pins[0][1], 1.27)
                for p, out in pins:
                    k = pkey(p)
                    if ws and k not in raw_ends:
                        self.pin_off_wire.append(f"{kname}@{k}")
                    if k not in ends and k not in labels and k not in junc:
                        labels[k] = (p, out, 1.27)
                for i, (a, b) in enumerate(ws):
                    wires_out.append(["wire", ["pts", ["xy", *T(*a)], ["xy", *T(*b)]], stroke(0),
                                      ["uuid", det_uuid("wire", fname, kname, si, i)]])
                    net_wires[kname].append((a, b))
                for k, p in junc.items():
                    junc_out.append(["junction", ["at", *T(*p)], ["diameter", 0], ["color", 0, 0, 0, 0],
                                     ["uuid", det_uuid("junction", fname, kname, si, k)]])
                for k, (p, d, size) in labels.items():
                    ang = (0 if d[0] >= 0 else 180) if abs(d[0]) >= abs(d[1]) else (90 if d[1] > 0 else 270)
                    just = ["left"] if ang in (0, 90) else ["right"]
                    X, Y = T(*p)
                    labels_out.append([
                        "global_label", kname, ["shape", Sym("passive")], ["at", X, Y, ang],
                        ["fields_autoplaced", Sym("yes")], effects(size, just),
                        ["uuid", det_uuid("label", fname, kname, si, k)],
                        ["property", "Intersheetrefs", "${INTERSHEET_REFS}", ["at", X, Y, ang],
                         effects(size, just, hide=True)]])
                net_pts[kname].update(ends.keys())
                net_pts[kname].update(labels.keys())
                net_pts[kname].update(pkey(p) for p, _ in pins)

        # ---- EAGLE implicitly joins an unwired "pwr" pin to the global net named after the pin
        # (the board file carries that net); make it explicit with a global label on the pin.
        for (ref, g, p), (pos, out, nums) in pin_world.items():
            if (ref, g, p) in referenced or placed_meta[ref]["pins"][(g, p)]["direction"] != "pwr":
                continue
            kname = kicad_net_name(eagle_pin_display_name(p))
            referenced.add((ref, g, p))
            self.expected[kname].update((ref, n) for n in nums)
            ang = (0 if out[0] >= 0 else 180) if abs(out[0]) >= abs(out[1]) else (90 if out[1] > 0 else 270)
            X, Y = T(*pos)
            just = ["left"] if ang in (0, 90) else ["right"]
            labels_out.append([
                "global_label", kname, ["shape", Sym("passive")], ["at", X, Y, ang],
                ["fields_autoplaced", Sym("yes")], effects(1.27, just),
                ["uuid", det_uuid("implicit-pwr", fname, ref, g, p)],
                ["property", "Intersheetrefs", "${INTERSHEET_REFS}", ["at", X, Y, ang],
                 effects(1.27, just, hide=True)]])
            net_pts[kname].add(pkey(pos))
            self.warnings.append(f"{ref}.{p}: EAGLE implicit power connection made explicit as global label '{kname}'")

        # ---- safety: nothing may touch geometrically unless EAGLE says it is the same net
        for na, keys in net_pts.items():
            for nb, wl in net_wires.items():
                if na == nb:
                    continue
                for k in keys:
                    if any(k in (pkey(a), pkey(b)) or point_on_segment_interior(k, a, b) for a, b in wl):
                        self.warnings.append(f"ERROR: net {na} touches net {nb} at {k}; KiCad would merge them")
        for k, (pos, _o, _n) in pin_world.items():
            if k in referenced:
                continue
            pk = pkey(pos)
            for n, keys in net_pts.items():
                if pk in keys or any(point_on_segment_interior(pk, a, b) for a, b in net_wires[n]):
                    self.warnings.append(f"ERROR: unconnected pin {k} touches net {n}; KiCad would connect it")

        # ---- free graphics / text
        gfx = []
        plain = sheet.find("plain")
        if plain is not None:
            nodes, w = graphic_nodes(plain, T, "sch", f"plain|{fname}", skip_text=False)
            gfx += nodes
            self.warnings += w
        for bi, bus in enumerate(sheet.findall("busses/bus")):
            for gi, seg in enumerate(bus.findall("segment")):
                gfx += graphic_nodes(seg, T, "sch", f"bus|{fname}|{bi}|{gi}")[0]
            self.warnings.append(f"bus {bus.get('name')} drawn as graphics (EAGLE busses carry no connectivity)")

        body = [*junc_out, *wires_out, *labels_out, *gfx, *symbols]
        file_uuid = self.root_uuid if is_root else det_uuid("file", fname)
        return self.file_node(file_uuid, paper, list(used.values()), body, is_root)

    # ---------------------------------------------------------------- output
    def write(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []

        def emit(name, text):
            path = out_dir / name
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)

        for fname, node in self.files.items():
            emit(fname, dumps(node) + "\n")
        emit(f"{self.symlib}.kicad_sym", dumps(["kicad_symbol_lib", ["version", SYM_VERSION], ["generator", GENERATOR],
                                               ["generator_version", GENERATOR_VERSION],
                                               *[v[1] for v in self.libsyms.values()]]) + "\n")
        emit("sym-lib-table", dumps(["sym_lib_table", ["version", 7],
                                     ["lib", ["name", self.symlib], ["type", "KiCad"],
                                      ["uri", "${KIPRJMOD}/" + f"{self.symlib}.kicad_sym"], ["options", ""],
                                      ["descr", "Symbols converted from EAGLE/Fusion 360"]]]) + "\n")
        mapping = {
            "project": self.project, "root_uuid": self.root_uuid, "symbol_lib": self.symlib,
            "footprint_lib": self.fplib, "components": self.components,
            "refs": {e: k for e, k in self.refs.items() if e != k},
            "expected_nets": {n: sorted(map(list, v)) for n, v in sorted(self.expected.items())},
        }
        emit(f"{self.project}.eagle_map.json", json.dumps(mapping, indent=1, ensure_ascii=False) + "\n")
        return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert an EAGLE 9.x / Fusion 360 schematic to KiCad 9.")
    ap.add_argument("sch", type=Path, help="EAGLE .sch exported from Fusion 360 (File > Export > EAGLE 9.x)")
    ap.add_argument("--brd", type=Path, help="matching EAGLE .brd (component values/attributes are taken from it)")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--project", help="KiCad project name (default: schematic file name)")
    args = ap.parse_args(argv)
    conv = SchematicConverter(args.sch, args.brd, args.project or args.sch.stem)
    conv.convert()
    for path in conv.write(args.out_dir):
        print(f"wrote {path}")
    for w in dict.fromkeys(conv.warnings):
        print(("" if w.startswith("ERROR") else "WARNING: ") + w)
    if conv.pin_off_wire:
        print(f"NOTE: {len(conv.pin_off_wire)} pin(s) not at a wire end in their EAGLE segment; joined by label "
              f"instead (first: {conv.pin_off_wire[0]})")
    print(f"{len(conv.components)} components, {len(conv.libsyms)} library symbols, {len(conv.expected)} nets")
    return 1 if any(w.startswith("ERROR") for w in conv.warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
