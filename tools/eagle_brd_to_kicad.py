#!/usr/bin/env python3
"""EAGLE 9.x / Fusion 360 Electronics board (.brd) -> KiCad 9 board (.kicad_pcb) + project.

Run with KiCad 9's Python (it provides `pcbnew`):
  "%LOCALAPPDATA%\\Programs\\KiCad\\9.0\\bin\\python.exe" eagle_brd_to_kicad.py board.brd --map X.eagle_map.json --out-dir DIR

* Footprints are converted by KiCad's own EAGLE library importer (pcbnew's EAGLE plugin, library mode)
  from the libraries embedded in the .brd.  (KiCad 9's whole-board EAGLE importer aborts outside the GUI;
  KiCad 10's `kicad-cli pcb import` is used by verify_forward.py as an independent reference instead.)
* Placement, nets, tracks, vias, polygons, keepouts, holes, text and outline are rebuilt with the pcbnew API
  using the same coordinate convention as KiCad's importer (x, -y).
* EAGLE DRC (clearances, widths, drills, restring -> real pad/via diameters, mask/cream frames, net classes)
  is applied so KiCad / Quilter see the same manufacturing geometry Fusion 360 will.
* Symbol links (paths), fields and footprint IDs come from eagle_sch_to_kicad's eagle_map.json, so
  `kicad-cli pcb drc --schematic-parity` passes.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path

import pcbnew

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eagle_common import (Rules, arc_mid_and_center, clear_dir, dim, kicad_net_name, kicad_ref_map,  # noqa: E402
                          mm_to_nm, outline_spurs, parse_rot)
from custom_pad_copper import merge_pad_copper

FROM_MM = mm_to_nm  # pcbnew.FromMM truncates the float product (64.77 mm -> 64769999 nm)


class KiCadLog:
    """KiCad's EAGLE reader reports notes through wxLog, which shows a message box per note in a
    script. Collect them instead (summarised at the end of the run) so the run never blocks on a dialog."""

    def __init__(self):
        self.messages: list[tuple[int, str]] = []
        self._target = None
        try:
            import wx

            outer = self

            class _Collect(wx.Log):
                def DoLogTextAtLevel(self, level, msg):
                    outer.messages.append((int(level), re.sub(r"^\d\d:\d\d:\d\d: ", "", msg)))

            self._target = _Collect()
            wx.Log.SetActiveTarget(self._target)
            # KiCad's internal debug checks (wxASSERT) would otherwise open a "wxWidgets Debug Alert" box
            # and block an unattended run
            wx.DisableAsserts()
        except Exception as e:  # wxPython missing: KiCad may show its notes as dialogs
            print(f"WARNING: could not redirect KiCad's log ({e}); KiCad notes may appear as dialogs")

    def summary(self) -> list[str]:
        try:
            import wx

            wx.Log.FlushActive()
        except Exception:
            pass
        skipped, other = defaultdict(int), OrderedDict()
        for level, msg in self.messages:
            m = re.match(r"Ignoring an? (\w+) since Eagle layer '([^']*)' \((\d+)\) was not mapped", msg)
            if m:
                lay = int(m.group(3))
                if not (lay == 46 or (lay in RESTRICT and m.group(1) == "wire")):  # re-created: package_extras
                    skipped[(lay, m.group(2))] += 1
            else:
                other[msg] = level
        out = []
        if skipped:
            parts = ", ".join(f"{name} ({n}): {c}" for (n, name), c in sorted(skipped.items()))
            out.append(f"NOTE: KiCad's EAGLE library reader left out package drawing items on layers it does not "
                       f"map (documentation only; the original Fusion design keeps them): {parts}")
        for msg, level in other.items():
            out.append(("WARNING: KiCad: " if level <= 2 else "NOTE: KiCad: ") + msg)
        return out


KICAD_LOG = KiCadLog()

# EAGLE layer number -> KiCad layer (non-copper), following KiCad's own EAGLE importer
NONCOPPER = {
    20: pcbnew.Edge_Cuts, 46: pcbnew.Edge_Cuts,
    21: pcbnew.F_SilkS, 22: pcbnew.B_SilkS, 25: pcbnew.F_SilkS, 26: pcbnew.B_SilkS,
    27: pcbnew.F_Fab, 28: pcbnew.B_Fab, 51: pcbnew.F_Fab, 52: pcbnew.B_Fab,
    29: pcbnew.F_Mask, 30: pcbnew.B_Mask, 33: pcbnew.F_Mask, 34: pcbnew.B_Mask,
    31: pcbnew.F_Paste, 32: pcbnew.B_Paste, 35: pcbnew.F_Adhes, 36: pcbnew.B_Adhes,
    39: pcbnew.F_CrtYd, 40: pcbnew.B_CrtYd,
    47: pcbnew.Cmts_User, 48: pcbnew.Cmts_User, 49: pcbnew.Cmts_User, 50: pcbnew.Cmts_User,
}
SKIP_LAYERS = {17, 18, 19, 23, 24, 37, 38, 44, 45}  # pads/vias/airwires/origins/test/drills: derived data
MILL_SCRATCH = pcbnew.User_9  # milling cut-outs wait here until checked against the board outline
RESTRICT = {41: "top", 42: "bottom", 43: "vias"}
H_ALIGN = {"left": pcbnew.GR_TEXT_H_ALIGN_LEFT, "center": pcbnew.GR_TEXT_H_ALIGN_CENTER,
           "right": pcbnew.GR_TEXT_H_ALIGN_RIGHT}
V_ALIGN = {"bottom": pcbnew.GR_TEXT_V_ALIGN_BOTTOM, "center": pcbnew.GR_TEXT_V_ALIGN_CENTER,
           "top": pcbnew.GR_TEXT_V_ALIGN_TOP}


def fnum(el, k, d=0.0) -> float:
    v = el.get(k)
    return d if v is None or v == "" else float(v)


def P(x: float, y: float) -> pcbnew.VECTOR2I:
    """EAGLE (mm, y-up) -> KiCad (nm, y-down); identical convention to KiCad's EAGLE importer."""
    return pcbnew.VECTOR2I(FROM_MM(x), FROM_MM(-y))


def copy_outline(zone, ps, i):
    """Copy polygon i (outline + holes) of `ps` into the zone's own outline. (ZONE.SetOutline takes
    ownership of a raw pointer that Python later frees, so it must not be used from Python.)"""
    dst = zone.Outline()
    src = ps.Outline(i)
    dst.NewOutline()
    k = dst.OutlineCount() - 1
    for j in range(src.PointCount()):
        p = src.CPoint(j)
        dst.Append(p.x, p.y, k)
    for h in range(ps.HoleCount(i)):
        hole = ps.CHole(i, h)
        dst.NewHole(k)
        for j in range(hole.PointCount()):
            p = hole.CPoint(j)
            dst.Append(p.x, p.y, k, h)


class FootprintSource:
    """Converts packages embedded in the .brd with KiCad's own EAGLE library importer."""

    def __init__(self, root, workdir: Path):
        self.root, self.workdir = root, workdir
        self.io = pcbnew.PCB_IO_MGR.PluginFind(pcbnew.PCB_IO_MGR.EAGLE)
        self.files: dict[tuple, str] = {}
        drawing = root.find("drawing")
        for i, lib in enumerate(drawing.findall("board/libraries/library")):
            key = (lib.get("name"), lib.get("urn") or "")
            e = ET.Element("eagle", root.attrib)
            d = ET.SubElement(e, "drawing")
            for tag in ("settings", "grid", "layers"):
                if drawing.find(tag) is not None:
                    d.append(drawing.find(tag))
            lb = ET.SubElement(d, "library", {"name": lib.get("name")})
            pk = lib.find("packages")
            if pk is not None:
                lb.append(pk)
            path = workdir / f"lib{i}.lbr"
            ET.ElementTree(e).write(path, encoding="utf-8", xml_declaration=True)
            self.files[key] = str(path)
        self.packages = {(l.get("name"), l.get("urn") or "", p.get("name")): p
                         for l in drawing.findall("board/libraries/library") for p in l.findall("packages/package")}

    def lib_key(self, name, urn):
        if (name, urn or "") in self.files:
            return (name, urn or "")
        c = [k for k in self.files if k[0] == name]
        if len(c) == 1:
            return c[0]
        raise KeyError(f"board library {name} ({urn}) not found")

    def load(self, lib, urn, package):
        key = self.lib_key(lib, urn)
        fp = self.io.FootprintLoad(self.files[key], package)
        if fp is None:
            raise KeyError(f"package {package} not found in {lib}")
        return pcbnew.FOOTPRINT(fp), self.packages[(key[0], key[1], package)]


class BoardBuilder:
    def __init__(self, brd: Path, mapping: dict | None, project: str, workdir: Path):
        self.root = ET.parse(brd).getroot()
        self.b_el = self.root.find("drawing/board")
        if self.b_el is None:
            raise SystemExit(f"{brd} is not an EAGLE board")
        self.mapping = mapping or {"components": {}}
        self.refs = kicad_ref_map(e.get("name") for e in self.b_el.findall("elements/element"))
        self.refs.update(self.mapping.get("refs") or {})
        self.project = project
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.milling_groups: list[tuple] = []
        self.rules = Rules(self.b_el)
        self.src = FootprintSource(self.root, workdir)
        self.board = pcbnew.CreateEmptyBoard()
        self.nets: dict[str, pcbnew.NETINFO_ITEM] = {}
        self.layer_names = {int(l.get("number")): l.get("name") for l in self.root.findall("drawing/layers/layer")}
        self.copper = self._copper_map()
        self.lib_fps: "OrderedDict[str, tuple]" = OrderedDict()
        self.elements_info: dict[str, dict] = {}

    # ------------------------------------------------------------------ layers
    def _copper_map(self) -> dict[int, int]:
        setup = self.rules.p.get("layerSetup") or "(1*16)"
        order = list(dict.fromkeys(int(n) for n in re.findall(r"\d+", setup) if 1 <= int(n) <= 16))
        if 1 not in order:
            order.insert(0, 1)
        if 16 not in order:
            order.append(16)
        order = [1] + [n for n in order if n not in (1, 16)] + [16]
        self.board.SetCopperLayerCount(len(order))
        cmap = {1: pcbnew.F_Cu, 16: pcbnew.B_Cu}
        for i, n in enumerate(order[1:-1]):
            cmap[n] = getattr(pcbnew, f"In{i + 1}_Cu")
        for n, lid in cmap.items():
            name = self.layer_names.get(n)
            if name and n not in (1, 16):
                self.board.SetLayerName(lid, name)
        return cmap

    def layer(self, n: int):
        if n in self.copper:
            return self.copper[n]
        if n in NONCOPPER:
            return NONCOPPER[n]
        if n in SKIP_LAYERS or n in RESTRICT:
            return None
        if 2 <= n <= 15:
            self.warnings.append(f"items on unused copper layer {n} ({self.layer_names.get(n)}) moved to Cmts.User")
        else:
            self.warnings.append(f"EAGLE layer {n} ({self.layer_names.get(n)}) mapped to Cmts.User")
        return pcbnew.Cmts_User

    # ------------------------------------------------------------------ nets / rules
    def net(self, name: str):
        return self.kicad_net(kicad_net_name(name))

    def kicad_net(self, kname: str):
        if kname not in self.nets:
            n = pcbnew.NETINFO_ITEM(self.board, kname)
            self.board.Add(n)
            self.nets[kname] = n
        return self.nets[kname]

    def assign_schematic_only_nets(self, sch: Path):
        """Give pads of unconnected schematic pins KiCad's own 'unconnected-(...)' nets, exactly as
        'Update PCB from Schematic' would, so the board is in full schematic parity."""
        from verify_forward import kicad_sch_nets  # noqa: E402
        exe = Path(sys.executable).with_name("kicad-cli.exe" if sys.platform == "win32" else "kicad-cli")
        cli = str(exe) if exe.is_file() else shutil.which("kicad-cli")
        if not cli:
            self.warnings.append("kicad-cli not found: unconnected pins keep no net (parity check will list them)")
            return
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
        for name, nodes in kicad_sch_nets(cli, sch).items():
            kname = name.replace("/", "{slash}")  # the XML netlist is unescaped; board net names are escaped
            for ref, pin in sorted(nodes):
                for p in (fps[ref].Pads() if ref in fps else []):
                    if p.GetNumber() != pin:
                        continue
                    if p.GetNetCode() <= 0:
                        p.SetNet(self.kicad_net(kname))
                        self.counts["schematic-only nets"] += 1
                    elif p.GetNetname() != kname:
                        self.warnings.append(f"ERROR: pad {ref}.{pin} is on {p.GetNetname()} in the board "
                                             f"but on {kname} in the schematic")

    def apply_rules(self):
        r, ds = self.rules, self.board.GetDesignSettings()
        mm = FROM_MM
        clearance = max(r.clear.values())
        ds.m_MinClearance = mm(min(r.clear.values()))
        ds.m_TrackMinWidth = mm(r.min_width)
        ds.m_MinThroughDrill = mm(r.min_drill)
        ds.m_HoleToHoleMin = mm(r.hole_hole)
        ds.m_HoleClearance = mm(min(r.clear.values()))
        ds.m_CopperEdgeClearance = mm(r.copper_dim)
        ds.m_ViasMinAnnularWidth = mm(r.get("rlMinViaOuter", 0.2032))
        ds.m_ViasMinSize = mm(r.min_drill + 2 * r.get("rlMinViaOuter", 0.2032))
        ds.m_SolderMaskExpansion = 0  # EAGLE stop/cream frames are applied per pad (they depend on pad size)
        ds.m_SolderPasteMargin = 0
        # board thickness: copper of used layers + isolation between consecutive used layers
        cu = [dim(v) for v in (r.p.get("mtCopper") or "").split()]
        iso = [dim(v) for v in (r.p.get("mtIsolate") or "").split()]
        used = sorted(self.copper)
        if cu and iso:
            t = sum(cu[n - 1] for n in used if n - 1 < len(cu))
            t += sum(iso[a - 1] for a, _b in zip(used, used[1:]) if a - 1 < len(iso))
            if t > 0:
                ds.SetBoardThickness(mm(t))
        ns = ds.m_NetSettings
        classes = {c.get("number"): c for c in self.b_el.findall("classes/class")}
        self.class_names = {}
        rules_text = []
        for num, c in sorted(classes.items(), key=lambda kv: int(kv[0])):
            name = "Default" if num == "0" else (c.get("name") or f"class{num}")
            self.class_names[num] = name
            nc = ns.GetDefaultNetclass() if num == "0" else pcbnew.NETCLASS(name)
            width = max(dim(c.get("width")), r.min_width)
            drill = dim(c.get("drill")) or r.min_drill
            own = [dim(x.get("value")) for x in c.findall("clearance") if x.get("class") == num]
            nc.SetClearance(mm(max([clearance] + own)))
            nc.SetTrackWidth(mm(width))
            nc.SetViaDrill(mm(drill))
            nc.SetViaDiameter(mm(r.via_diameter(drill, 0)))
            if num != "0":
                ns.SetNetclass(name, nc)
            for x in c.findall("clearance"):
                other = x.get("class")
                if other != num and dim(x.get("value")) > 0:
                    rules_text.append((name, other, dim(x.get("value"))))
        self.dru = ["(version 1)",
                    f'(rule "EAGLE copper to NPTH hole (mdCopperDimension)"\n'
                    f'  (condition "A.Pad_Type == \'NPTH, mechanical\'")\n'
                    f'  (constraint hole_clearance (min {r.copper_dim:g}mm)))']
        for a, other, v in rules_text:
            b = self.class_names.get(other, f"class{other}")
            self.dru.append(f'(rule "EAGLE clearance {a} - {b}"\n'
                            f'  (condition "A.NetClass == \'{a}\' && B.NetClass == \'{b}\'")\n'
                            f'  (constraint clearance (min {v:g}mm)))')

    # ------------------------------------------------------------------ elements
    def fix_pads(self, fp, pkg):
        """Apply EAGLE board DRC to library pads: restring (real copper diameter) and stop/cream frames."""
        r = self.rules
        eagle = {e.get("name"): e for e in pkg if e.tag in ("pad", "smd")}
        for pad in fp.Pads():
            e = eagle.get(pad.GetNumber())
            if e is None:
                continue
            if e.tag == "pad":
                drill = fnum(e, "drill")
                d = r.pad_diameter(drill, fnum(e, "diameter"))
                shape = r.pad_shape(e.get("shape"), e.get("first") == "yes")
                lib_shape = e.get("shape") or "round"
                if shape != lib_shape:
                    ALL = pcbnew.F_Cu
                    if shape == "round":
                        pad.SetShape(ALL, pcbnew.PAD_SHAPE_CIRCLE)
                    elif shape == "square":
                        pad.SetShape(ALL, pcbnew.PAD_SHAPE_RECTANGLE)
                    else:
                        pad.SetShape(ALL, pcbnew.PAD_SHAPE_CHAMFERED_RECT)
                        pad.SetChamferPositions(ALL, pcbnew.RECT_CHAMFER_ALL)
                        pad.SetChamferRectRatio(ALL, 1 - math.sqrt(0.5))
                    pad.SetOffset(ALL, pcbnew.VECTOR2I(0, 0))
                    pad.SetSize(ALL, pcbnew.VECTOR2I(FROM_MM(d), FROM_MM(d)))
                    self.pad_fixes += 1
                elif shape in ("long", "offset"):
                    elong = r.ratio("psElongationLong" if shape == "long" else "psElongationOffset", 100)
                    want = pcbnew.VECTOR2I(FROM_MM(d * (100 + elong) / 100), FROM_MM(d))
                    if pad.GetSize(pcbnew.F_Cu) != want:
                        pad.SetSize(pcbnew.F_Cu, want)
                        if shape == "offset":
                            pad.SetOffset(pcbnew.F_Cu, pcbnew.VECTOR2I(round((want.x - want.y) / 2), 0))
                        self.pad_fixes += 1
                else:
                    size = pad.GetSize(pcbnew.F_Cu)
                    old = min(size.x, size.y)
                    if old > 0 and abs(pcbnew.ToMM(old) - d) > 1e-4:
                        s = FROM_MM(d) / old
                        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(round(size.x * s), round(size.y * s)))
                        self.pad_fixes += 1
                if e.get("stop", "yes") != "no":
                    pad.SetLocalSolderMaskMargin(FROM_MM(r.stop_margin(d)))
            else:
                dx, dy = fnum(e, "dx"), fnum(e, "dy")
                ratio = r.smd_round_ratio(dx, dy, fnum(e, "roundness"))
                if ratio is not None and ratio > 0:
                    pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_ROUNDRECT)
                    pad.SetRoundRectRadiusRatio(pcbnew.F_Cu, ratio)
                if e.get("stop", "yes") != "no":
                    pad.SetLocalSolderMaskMargin(FROM_MM(r.stop_margin(min(dx, dy))))
                if e.get("cream", "yes") != "no":
                    pad.SetLocalSolderPasteMargin(FROM_MM(r.cream_margin(min(dx, dy))))

    def field_text(self, fp, field, attr, default_layer):
        """Position a Reference/Value field from a smashed EAGLE attribute (cosmetic)."""
        lay = self.layer(int(attr.get("layer"))) if attr.get("layer") else default_layer
        field.SetLayer(lay if lay is not None else default_layer)
        self.style_text(field, attr)
        field.SetVisible(attr.get("display", "value") != "off")

    def place_elements(self):
        comps = self.mapping.get("components", {})
        self.pad_fixes = 0
        for e in self.b_el.findall("elements/element"):
            eagle_name = e.get("name")
            name = self.refs.get(eagle_name, eagle_name)
            fp, pkg = self.src.load(e.get("library"), e.get("library_urn"), e.get("package"))
            self.board.Add(fp)
            self.counts["package milling items"] += self.package_extras(fp, pkg, name)
            comp = comps.get(name)
            fpid = (comp or {}).get("footprint") or f"{self.project}:{e.get('package')}"
            nick, item = fpid.split(":", 1) if ":" in fpid else (self.project, fpid)
            fp.SetFPID(pcbnew.LIB_ID(nick, item))
            self.fix_pads(fp, pkg)
            self.counts["custom copper pads"] += merge_pad_copper(fp)
            if fpid not in self.lib_fps:
                lib_copy, _ = self.src.load(e.get("library"), e.get("library_urn"), e.get("package"))
                lib_copy.SetFPID(pcbnew.LIB_ID(nick, item))
                self.package_extras(lib_copy, pkg, name, track=False)
                self.fix_pads(lib_copy, pkg)
                merge_pad_copper(lib_copy)
                self.lib_fps[fpid] = (item, lib_copy)
            fp.SetReference(name)
            # empty values become "~", exactly what KiCad's netlister / Update PCB from Schematic produce
            fp.SetValue((comp["value"] if comp else e.get("value")) or "~")
            fp.SetPosition(P(fnum(e, "x"), fnum(e, "y")))
            mirror, spin, deg = parse_rot(e.get("rot"))
            if mirror:  # same convention as KiCad's EAGLE importer: orientation = 180 - deg after flip
                fp.SetOrientation(pcbnew.EDA_ANGLE(deg + 180.0, pcbnew.DEGREES_T))
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
            else:
                fp.SetOrientation(pcbnew.EDA_ANGLE(deg, pcbnew.DEGREES_T))
            attrs = {a.get("name").upper(): a for a in e.findall("attribute")}
            side = 1 if mirror else 0
            for key, field, layers in (("NAME", fp.Reference(), (pcbnew.F_SilkS, pcbnew.B_SilkS)),
                                       ("VALUE", fp.Value(), (pcbnew.F_Fab, pcbnew.B_Fab))):
                a = attrs.get(key)
                if a is not None and a.get("x") is not None and e.get("smashed") == "yes":
                    self.field_text(fp, field, a, layers[side])
            if e.get("locked") == "yes":
                fp.SetLocked(True)
            if e.get("populate") == "no":
                fp.SetDNP(True)
            if comp:
                fp.SetPath(pcbnew.KIID_PATH(comp["path"]))
                fp.SetSheetname(comp.get("sheetname") or "Root")
                fp.SetSheetfile(comp.get("sheetfile") or "")
                extra = OrderedDict([("Datasheet", comp.get("datasheet") or ""),
                                     ("Description", comp.get("description") or "")])
                extra.update(comp.get("fields") or {})
            else:
                has_pads = any(p.GetNumber() for p in fp.Pads())
                if comps and has_pads:
                    self.warnings.append(f"ERROR: element {name} has pads but no schematic part")
                if not has_pads:  # logos, fiducial-like artwork: board-only, as in EAGLE
                    fp.SetBoardOnly(True)
                    fp.SetExcludedFromBOM(True)
                    fp.SetExcludedFromPosFiles(True)
                extra = OrderedDict((a.get("name"), a.get("value") or "") for a in e.findall("attribute")
                                    if a.get("name").upper() not in ("NAME", "VALUE"))
            for k, v in extra.items():
                fld = fp.GetFieldByName(k) if fp.HasFieldByName(k) else None
                if fld is None:
                    fld = pcbnew.PCB_FIELD(fp, fp.GetNextFieldId(), k)
                    fld.SetLayer(pcbnew.B_Fab if mirror else pcbnew.F_Fab)
                    fld.SetPosition(fp.GetPosition())
                    fp.AddField(fld)
                fld.SetText(v)
                fld.SetVisible(False)
            self.elements_info[name] = {
                "eagle_name": eagle_name,
                "package": e.get("package"), "library": e.get("library"), "library_urn": e.get("library_urn"),
                "rot": e.get("rot") or "R0", "x": fnum(e, "x"), "y": fnum(e, "y"),
                "pads": [p.GetNumber() for p in fp.Pads() if p.GetNumber()]}

    # ------------------------------------------------------------------ geometry helpers
    @staticmethod
    def arc_points(x1, y1, x2, y2, curve, max_seg_deg=5.0):
        """Polyline approximation (EAGLE coords) of a curved edge, excluding the start point."""
        if not curve:
            return [(x2, y2)]
        _mid, (cx, cy) = arc_mid_and_center(x1, y1, x2, y2, curve)
        n = max(2, int(math.ceil(abs(curve) / max_seg_deg)))
        a0 = math.atan2(y1 - cy, x1 - cx)
        rad = math.hypot(x1 - cx, y1 - cy)
        pts = [(cx + rad * math.cos(a0 + math.radians(curve) * i / n),
                cy + rad * math.sin(a0 + math.radians(curve) * i / n)) for i in range(1, n)]
        return pts + [(x2, y2)]

    def poly_points(self, poly_el):
        verts = poly_el.findall("vertex")
        pts = []
        for i, v in enumerate(verts):
            nxt = verts[(i + 1) % len(verts)]
            x1, y1, x2, y2 = fnum(v, "x"), fnum(v, "y"), fnum(nxt, "x"), fnum(nxt, "y")
            if not pts:
                pts.append((x1, y1))
            pts += self.arc_points(x1, y1, x2, y2, fnum(v, "curve"))
        if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
            pts.pop()
        return pts

    @staticmethod
    def text_orient(rot, align):
        """EAGLE keeps non-spin text readable: angles in (90, 270] are drawn turned by 180 deg with the
        anchor justification inverted.  Returns (mirror, kicad_angle, h, v)."""
        mirror, spin, deg = parse_rot(rot)
        al = align or "bottom-left"
        v, h = ("center", "center") if al == "center" else al.split("-")
        if not spin and 90 < deg <= 270:
            deg -= 180
            h = {"left": "right", "right": "left"}.get(h, h)
            v = {"top": "bottom", "bottom": "top"}.get(v, v)
        return mirror, (-deg if mirror else deg), h, v

    def make_shape(self, el, layer):
        """EAGLE wire/rectangle/circle/polygon element -> PCB_SHAPE (board coordinates)."""
        s = pcbnew.PCB_SHAPE(self.board)
        s.SetLayer(layer)
        w = fnum(el, "width")
        tag = el.tag
        if tag == "wire":
            x1, y1, x2, y2 = (fnum(el, k) for k in ("x1", "y1", "x2", "y2"))
            curve = fnum(el, "curve")
            if curve:
                mid, _c = arc_mid_and_center(x1, y1, x2, y2, curve)
                s.SetShape(pcbnew.SHAPE_T_ARC)
                s.SetArcGeometry(P(x1, y1), P(*mid), P(x2, y2))
            else:
                s.SetShape(pcbnew.SHAPE_T_SEGMENT)
                s.SetStart(P(x1, y1))
                s.SetEnd(P(x2, y2))
        elif tag == "circle":
            s.SetShape(pcbnew.SHAPE_T_CIRCLE)
            s.SetCenter(P(fnum(el, "x"), fnum(el, "y")))
            s.SetEnd(P(fnum(el, "x") + fnum(el, "radius"), fnum(el, "y")))
            s.SetFilled(w == 0)
        elif tag == "rectangle":
            x1, y1, x2, y2 = (fnum(el, k) for k in ("x1", "y1", "x2", "y2"))
            _m, _s, deg = parse_rot(el.get("rot"))
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            th = math.radians(deg)
            pts = [(cx + (x - cx) * math.cos(th) - (y - cy) * math.sin(th),
                    cy + (x - cx) * math.sin(th) + (y - cy) * math.cos(th)) for x, y in corners]
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetPolyPoints([P(x, y) for x, y in pts])
            s.SetFilled(True)
            w = 0
        elif tag == "polygon":
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetPolyPoints([P(x, y) for x, y in self.poly_points(el)])
            s.SetFilled(True)
        else:
            raise ValueError(tag)
        s.SetWidth(FROM_MM(w))
        return s

    def eagle_polygon_set(self, el):
        """EAGLE polygon -> SHAPE_POLY_SET of its real extent: EAGLE draws the contour with a pen of
        `width`, so copper reaches width/2 beyond the vertices (KiCad's importer does the same)."""
        ps = pcbnew.SHAPE_POLY_SET()
        ps.NewOutline()
        for x, y in self.poly_points(el):
            v = P(x, y)
            ps.Append(v.x, v.y)
        w = fnum(el, "width")
        if w > 0:
            ps.Inflate(FROM_MM(w / 2), pcbnew.CORNER_STRATEGY_ALLOW_ACUTE_CORNERS, FROM_MM(0.005), True)
        return ps

    def milling_region(self, el) -> pcbnew.SHAPE_POLY_SET:
        """Area removed by one milling (layer 46) cutter path of width w: the path swept by the cutter
        (the stroke of the wire/circle), with any island it encloses (which falls out) included."""
        s = self.make_shape(el, pcbnew.Edge_Cuts)
        if el.tag == "circle":
            s.SetFilled(True)
        ps = pcbnew.SHAPE_POLY_SET()
        s.TransformShapeToPolygon(ps, pcbnew.Edge_Cuts, 0, FROM_MM(0.005), pcbnew.ERROR_OUTSIDE)
        return ps

    def add_milling_group(self, els, parent, label, track=True) -> int:
        """Milling paths of one owner (the board, or a not-yet-placed footprint in package coordinates so the
        cut-outs move/rotate/mirror with the part) -> closed cut-out contours.  Overlapping cutter paths are
        merged so the contours never intersect each other.  Tracked groups are drawn on a scratch layer
        until finalize_milling() has checked them against the board outline."""
        union = pcbnew.SHAPE_POLY_SET()
        for el in els:
            union.BooleanAdd(self.milling_region(el))
        lw = self.board.GetDesignSettings().GetLineThickness(pcbnew.Edge_Cuts)
        shapes = []
        for i in range(union.OutlineCount()):  # holes dropped: islands enclosed by a cut fall out
            o = union.Outline(i)
            s = pcbnew.PCB_SHAPE(parent)
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetLayer(MILL_SCRATCH if track else pcbnew.Edge_Cuts)
            s.SetPolyPoints([o.CPoint(j) for j in range(o.PointCount())])
            s.SetFilled(False)
            s.SetWidth(lw)
            parent.Add(s)
            shapes.append(s)
        if track and shapes:
            self.milling_groups.append((parent, shapes, label))
        return len(shapes)

    def package_extras(self, fp, pkg, label, track=True) -> int:
        """Package items KiCad's EAGLE library reader drops but that matter for manufacturing and routing:
          * milling (slots / cut-outs, layer 46) -> footprint Edge.Cuts;
          * t/b/vRestrict (41-43) wires -> footprint rule areas, so the router keeps copper out as EAGLE does
            (KiCad's reader makes rule areas from restrict rectangles, circles and polygons itself).
        Call before the footprint is placed: both then follow the part's position, rotation and side."""
        cutter, n = [], 0
        for el in pkg:
            if el.tag not in ("wire", "circle", "rectangle", "polygon"):
                continue
            if el.tag == "wire" and el.get("layer", "").isdigit() and int(el.get("layer")) in RESTRICT:
                if fnum(el, "width") > 0:
                    k = self.add_rule_area(el, int(el.get("layer")),
                                           f"EAGLE {RESTRICT[int(el.get('layer'))]} restrict", parent=fp)
                    if track:
                        self.counts["package rule areas"] += k
                continue
            if el.get("layer") != "46":
                continue
            if el.tag in ("wire", "circle") and fnum(el, "width") > 0:
                cutter.append(el)
            else:  # zero-width contour: the drawn line is the board edge itself, as for plain items
                s = self.make_shape(el, pcbnew.Edge_Cuts)
                s.SetFilled(False)
                s.SetWidth(self.board.GetDesignSettings().GetLineThickness(pcbnew.Edge_Cuts))
                fp.Add(s)
                n += 1
        if cutter:
            n += self.add_milling_group(cutter, fp, label, track)
        return n

    def repair_outline(self):
        """Make the EAGLE Dimension outline usable by KiCad (and Quilter) without changing the board shape:
          * exact duplicate Edge.Cuts drawings -> one (KiCad's file writer drops the copies as well);
          * dead-end spurs (an open chain hanging off the outline) bound no area, EAGLE and fabs ignore
            them, but KiCad cannot build a board outline while they exist -> dropped, with a note.
        A real gap (an open chain with two free ends) is left alone; KiCad's DRC reports it."""
        def xy(p):
            return p.x, p.y

        def sig(d):
            s = (d.ShowShape(), xy(d.GetStart()), xy(d.GetEnd()), d.GetWidth())
            if d.GetShape() == pcbnew.SHAPE_T_ARC:
                s += (xy(d.GetArcMid()),)
            elif d.GetShape() == pcbnew.SHAPE_T_POLY:
                o = d.GetPolyShape().COutline(0)
                s += tuple(xy(o.CPoint(j)) for j in range(o.PointCount()))
            return s

        seen, dup = set(), 0
        for d in list(self.board.GetDrawings()):
            if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts:
                k = sig(d)
                if k in seen:
                    self.board.Remove(d)
                    dup += 1
                seen.add(k)
        if dup:
            self.counts["edge"] -= dup
            self.notes.append(f"{dup} exact duplicate outline item(s) in the EAGLE Dimension layer kept once")
        edges, owner = {}, {}
        for d in self.board.GetDrawings():
            if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts \
                    and d.GetShape() in (pcbnew.SHAPE_T_SEGMENT, pcbnew.SHAPE_T_ARC):
                edges[d.m_Uuid.AsString()] = (xy(d.GetStart()), xy(d.GetEnd()))
                owner[d.m_Uuid.AsString()] = d
        for fp in self.board.GetFootprints():  # footprint edges take part in the outline, never removed
            for d in fp.GraphicalItems():
                if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts \
                        and d.GetShape() in (pcbnew.SHAPE_T_SEGMENT, pcbnew.SHAPE_T_ARC):
                    edges["fp:" + d.m_Uuid.AsString()] = (xy(d.GetStart()), xy(d.GetEnd()))
        spurs = [owner[i] for i in outline_spurs(edges) if i in owner]
        for d in spurs:
            a, b = d.GetStart(), d.GetEnd()
            self.notes.append(f"dead-end outline item dropped: {d.ShowShape().lower()} ({a.x / 1e6:g}, {-a.y / 1e6:g})-"
                              f"({b.x / 1e6:g}, {-b.y / 1e6:g}) mm, {d.GetLength() / 1e6:.3f} mm long, bounds no area; "
                              f"KiCad cannot build the board outline with it (the Fusion design is unchanged)")
            self.board.Remove(d)
            self.counts["edge"] -= 1

    def finalize_milling(self):
        """Resolve milling cut-outs against the board outline, which KiCad (and Quilter) need as a set of
        non-intersecting closed contours.  Cut-outs inside the board, or belonging to a part that is still
        off the board, stay closed contours.  Cut-outs crossing the board edge (edge notches, card-edge
        keys) are subtracted from the outline, which is then redrawn as polygons."""
        if not self.milling_groups:
            return
        outline = pcbnew.SHAPE_POLY_SET()
        ok = self.board.GetBoardPolygonOutlines(outline)
        tol = float(FROM_MM(0.01)) ** 2
        crossing, keep = [], set()
        for parent, shapes, label in self.milling_groups:
            if ok:
                region = pcbnew.SHAPE_POLY_SET()
                for s in shapes:
                    region.BooleanAdd(s.GetPolyShape())
                outside = pcbnew.SHAPE_POLY_SET(region)
                outside.BooleanSubtract(outline)
                inside = pcbnew.SHAPE_POLY_SET(region)
                inside.BooleanIntersection(outline)
                if outside.Area() > tol and inside.Area() > tol:
                    crossing.append((parent, shapes, label, region))
                    continue
            for s in shapes:
                s.SetLayer(pcbnew.Edge_Cuts)
                keep.add(s.m_Uuid.AsString())
        if not ok:
            self.warnings.append("board outline is not a closed contour; milling cut-outs were not checked against it")
        if not crossing:
            return
        board_shape = pcbnew.SHAPE_POLY_SET(outline)
        for parent, shapes, label, region in crossing:
            board_shape.BooleanSubtract(region)
            for s in shapes:
                parent.Remove(s)
        # every other Edge.Cuts contour went into `outline`; replace them all by the merged result
        moved = []
        for d in list(self.board.GetDrawings()):
            if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts \
                    and d.m_Uuid.AsString() not in keep:
                self.board.Remove(d)
        for fp in self.board.GetFootprints():
            for d in list(fp.GraphicalItems()):
                if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts \
                        and d.m_Uuid.AsString() not in keep:
                    fp.Remove(d)
                    moved.append(fp.GetReference())
        lw = self.board.GetDesignSettings().GetLineThickness(pcbnew.Edge_Cuts)

        def contour(chain):
            s = pcbnew.PCB_SHAPE(self.board)
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetLayer(pcbnew.Edge_Cuts)
            s.SetPolyPoints([chain.CPoint(j) for j in range(chain.PointCount())])
            s.SetFilled(False)
            s.SetWidth(lw)
            self.board.Add(s)

        for i in range(board_shape.OutlineCount()):
            contour(board_shape.Outline(i))
            for h in range(board_shape.HoleCount(i)):
                contour(board_shape.Hole(i, h))
        self.notes.append("milling of " + ", ".join(c[2] for c in crossing) + " crosses the board edge: cut out of "
                          "the board outline (Edge.Cuts redrawn as polygons; these parts should stay where they are)")
        if moved:
            self.notes.append("Edge.Cuts drawings of " + ", ".join(sorted(set(moved))) +
                              " merged into the board outline")
        check = pcbnew.SHAPE_POLY_SET()
        if not self.board.GetBoardPolygonOutlines(check):
            self.warnings.append("board outline is still not a valid closed contour after merging milling")

    def explode_edge_polys(self):
        """Closed polygons on Edge.Cuts -> chains of line segments with the same vertices.  KiCad 9's DRC
        intermittently rejects a board whose outline contains a polygon ('malformed outline, no edges
        found'; ~1 run in 4, never with segments, fixed in KiCad 10), and segments are the outline form
        every tool understands.  Runs last, after the milling cut-outs have been resolved as polygons."""
        def explode(parent, s):
            ps = s.GetPolyShape()
            for i in range(ps.OutlineCount()):
                chain = ps.Outline(i)
                pts = [chain.CPoint(j) for j in range(chain.PointCount())]
                for a, b in zip(pts, pts[1:] + pts[:1]):
                    if a == b:
                        continue
                    seg = pcbnew.PCB_SHAPE(parent)
                    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
                    seg.SetLayer(pcbnew.Edge_Cuts)
                    seg.SetStart(a)
                    seg.SetEnd(b)
                    seg.SetWidth(s.GetWidth())
                    parent.Add(seg)
            parent.Remove(s)

        owners = [(self.board, list(self.board.GetDrawings()))]
        owners += [(fp, list(fp.GraphicalItems())) for fp in self.board.GetFootprints()]
        owners += [(fp, list(fp.GraphicalItems())) for _item, fp in self.lib_fps.values()]
        for parent, items in owners:
            for d in items:
                if d.Type() == pcbnew.PCB_SHAPE_T and d.GetLayer() == pcbnew.Edge_Cuts \
                        and d.GetShape() == pcbnew.SHAPE_T_POLY and not d.IsFilled():
                    explode(parent, d)

    def add_rule_area(self, el, eagle_layer, name, parent=None) -> int:
        """tRestrict / bRestrict / vRestrict item -> KiCad rule area (same semantics as KiCad's importer),
        on the board or, for package items, in the (not yet placed) footprint `parent`."""
        parent = parent or self.board
        if el.tag == "polygon":
            ps = self.eagle_polygon_set(el)
        else:
            shape = self.make_shape(el, pcbnew.F_Cu)
            ps = pcbnew.SHAPE_POLY_SET()
            shape.TransformShapeToPolygon(ps, pcbnew.F_Cu, 0, FROM_MM(0.005), pcbnew.ERROR_INSIDE)
        n = 0
        for i in range(ps.OutlineCount()):
            z = pcbnew.ZONE(parent)
            z.SetIsRuleArea(True)
            z.SetZoneName(name)
            if eagle_layer == 43:
                z.SetLayerSet(pcbnew.LSET.AllCuMask(self.board.GetCopperLayerCount()))
                z.SetDoNotAllowVias(True)
                z.SetDoNotAllowTracks(False)
                z.SetDoNotAllowCopperPour(False)
                z.SetDoNotAllowPads(False)
            else:
                z.SetLayer(pcbnew.F_Cu if eagle_layer == 41 else pcbnew.B_Cu)
                z.SetDoNotAllowVias(True)
                z.SetDoNotAllowTracks(True)
                z.SetDoNotAllowCopperPour(True)
                z.SetDoNotAllowPads(True)
            z.SetDoNotAllowFootprints(False)
            copy_outline(z, ps, i)
            parent.Add(z)
            n += 1
            if parent is self.board:
                self.counts["rule areas"] += 1
        return n

    def add_zone(self, el, layer, net, eagle_layer, key=None):
        pts = self.poly_points(el)
        if len(pts) < 3:
            self.warnings.append(f"polygon with <3 vertices on layer {eagle_layer} skipped")
            return
        pour = el.get("pour") or "solid"
        z = pcbnew.ZONE(self.board)
        z.SetLayer(layer)
        if pour == "cutout":
            z.SetIsRuleArea(True)
            z.SetDoNotAllowCopperPour(True)
            z.SetDoNotAllowTracks(False)
            z.SetDoNotAllowVias(False)
            z.SetDoNotAllowPads(False)
            z.SetDoNotAllowFootprints(False)
            z.SetZoneName("EAGLE cutout")
        else:
            if net is not None:
                z.SetNet(net)
            width = fnum(el, "width")
            z.SetMinThickness(FROM_MM(max(width, 0.0254)))
            if el.get("isolate"):
                z.SetLocalClearance(FROM_MM(fnum(el, "isolate")))
            rank = int(el.get("rank") or 1)
            z.SetAssignedPriority(max(0, 7 - rank))  # EAGLE: rank 1 wins; KiCad: higher priority wins
            if el.get("thermals", "yes") == "no":
                z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
            else:
                z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
                z.SetThermalReliefGap(FROM_MM(self.rules.get("slThermalIsolate", 0.254)))
                z.SetThermalReliefSpokeWidth(FROM_MM(max(width, self.rules.min_width)))
            z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER if el.get("orphans") == "yes"
                                   else pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
            if pour == "hatch":
                spacing = fnum(el, "spacing", 1.27)
                z.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
                z.SetHatchThickness(FROM_MM(max(width, 0.0254)))
                z.SetHatchGap(FROM_MM(max(spacing - width, 0.0254)))
                z.SetHatchOrientation(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        ps = self.eagle_polygon_set(el)
        for i in range(ps.OutlineCount()):
            copy_outline(z, ps, i)
        self.board.Add(z)
        self.counts["zones"] += 1
        if key is not None and pour != "cutout":
            # lets kicad_to_fusion.py recognise this pour in the routed board and keep the EAGLE original
            o = z.Outline()
            self.zone_records.append(dict(key, layer=eagle_layer, outlines=[
                [[o.COutline(i).CPoint(j).x, o.COutline(i).CPoint(j).y] for j in range(o.COutline(i).PointCount())]
                for i in range(o.OutlineCount())]))

    # ------------------------------------------------------------------ signals
    def add_signals(self):
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
        tent_limit = self.rules.get("mlViaStopLimit", 0.0)
        self.board_tent = tent_limit >= self.rules.min_drill
        for sig in self.b_el.findall("signals/signal"):
            name = sig.get("name")
            net = self.net(name)
            self.net_map[net.GetNetname()] = name
            cls = sig.get("class") or "0"
            if cls != "0" and cls in self.class_names:
                self.net_classes[net.GetNetname()] = self.class_names[cls]
            for cr in sig.findall("contactref"):
                fp = fps.get(self.refs.get(cr.get("element"), cr.get("element")))
                hits = [p for p in fp.Pads() if p.GetNumber() == cr.get("pad")] if fp else []
                if not hits:
                    self.warnings.append(f"ERROR: {name}: pad {cr.get('element')}.{cr.get('pad')} not found")
                for p in hits:
                    p.SetNet(net)
            for w in sig.findall("wire"):
                n = int(w.get("layer"))
                if n not in self.copper:
                    if n != 19:
                        self.warnings.append(f"signal {name}: wire on non-copper layer {n} skipped")
                    continue
                x1, y1, x2, y2 = (fnum(w, k) for k in ("x1", "y1", "x2", "y2"))
                curve = fnum(w, "curve")
                if curve:
                    t = pcbnew.PCB_ARC(self.board)
                    mid, _c = arc_mid_and_center(x1, y1, x2, y2, curve)
                    t.SetStart(P(x1, y1))
                    t.SetMid(P(*mid))
                    t.SetEnd(P(x2, y2))
                else:
                    t = pcbnew.PCB_TRACK(self.board)
                    t.SetStart(P(x1, y1))
                    t.SetEnd(P(x2, y2))
                t.SetWidth(FROM_MM(fnum(w, "width")))
                t.SetLayer(self.copper[n])
                t.SetNet(net)
                if w.get("locked") == "yes" or sig.get("locked") == "yes":
                    t.SetLocked(True)
                self.board.Add(t)
                self.counts["tracks"] += 1
            for v in sig.findall("via"):
                a, b = (int(s) for s in (v.get("extent") or "1-16").split("-"))
                ca = [n for n in sorted(self.copper) if a <= n <= b]
                via = pcbnew.PCB_VIA(self.board)
                via.SetPosition(P(fnum(v, "x"), fnum(v, "y")))
                drill = fnum(v, "drill")
                via.SetDrill(FROM_MM(drill))
                via.SetWidth(FROM_MM(self.rules.via_diameter(drill, fnum(v, "diameter"))))
                if ca and (ca[0], ca[-1]) != (min(self.copper), max(self.copper)):
                    via.SetViaType(pcbnew.VIATYPE_BLIND_BURIED)
                    via.SetLayerPair(self.copper[ca[0]], self.copper[ca[-1]])
                else:
                    via.SetViaType(pcbnew.VIATYPE_THROUGH)
                    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                tented = drill <= tent_limit + 1e-9
                if tented != self.board_tent:
                    mode = pcbnew.TENTING_MODE_TENTED if tented else pcbnew.TENTING_MODE_NOT_TENTED
                    via.SetFrontTentingMode(mode)
                    via.SetBackTentingMode(mode)
                via.SetNet(net)
                if v.get("locked") == "yes":
                    via.SetLocked(True)
                self.board.Add(via)
                self.counts["vias"] += 1
            for i, poly in enumerate(sig.findall("polygon")):
                n = int(poly.get("layer"))
                if n in self.copper:
                    self.add_zone(poly, self.copper[n], net, n, {"signal": name, "index": i})
                elif n in RESTRICT:
                    self.add_rule_area(poly, n, f"EAGLE {RESTRICT[n]} restrict")
                else:
                    self.warnings.append(f"signal {name}: polygon on non-copper layer {n} skipped")

    # ------------------------------------------------------------------ plain (board-level) items
    def add_text(self, el, layer):
        t = pcbnew.PCB_TEXT(self.board)
        t.SetText((el.text or "").replace("\r\n", "\n"))
        t.SetLayer(layer)
        self.style_text(t, el)
        self.board.Add(t)
        self.counts["texts"] += 1

    def style_text(self, t, el):
        size = fnum(el, "size", 1.778)
        thick = size * fnum(el, "ratio", 8) / 100.0
        t.SetPosition(P(fnum(el, "x"), fnum(el, "y")))
        h_ = FROM_MM(max(size - thick, 0.1))
        t.SetTextSize(pcbnew.VECTOR2I(h_, h_))
        t.SetTextThickness(FROM_MM(max(thick, 0.01)))
        mirror, ang, h, v = self.text_orient(el.get("rot"), el.get("align"))
        t.SetTextAngle(pcbnew.EDA_ANGLE(ang, pcbnew.DEGREES_T))
        t.SetMirrored(mirror)
        t.SetHorizJustify(H_ALIGN[h])
        t.SetVertJustify(V_ALIGN[v])

    def add_hole(self, el):
        drill = fnum(el, "drill")
        ref = f"HOLE{len(self.holes) + 1}"
        fp = pcbnew.FOOTPRINT(self.board)
        fp.SetFPID(pcbnew.LIB_ID("", f"EAGLE_HOLE_{drill:g}mm"))
        fp.SetReference(ref)
        fp.Reference().SetVisible(False)
        fp.SetValue(f"HOLE {drill:g}mm")
        fp.Value().SetVisible(False)
        pos = P(fnum(el, "x"), fnum(el, "y"))
        fp.SetPosition(pos)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(FROM_MM(drill), FROM_MM(drill)))
        pad.SetDrillSize(pcbnew.VECTOR2I(FROM_MM(drill), FROM_MM(drill)))
        pad.SetLayerSet(pad.UnplatedHoleMask())
        pad.SetNumber("")
        pad.SetLocalSolderMaskMargin(FROM_MM(self.rules.stop_margin(drill)))
        fp.Add(pad)
        pad.SetPosition(pos)
        fp.SetBoardOnly(True)
        fp.SetExcludedFromBOM(True)
        fp.SetExcludedFromPosFiles(True)
        fp.SetLocked(True)
        self.board.Add(fp)
        self.holes[ref] = {"x": fnum(el, "x"), "y": fnum(el, "y"), "drill": drill}

    def add_dimension(self, el, layer):
        x1, y1, x2, y2, x3, y3 = (fnum(el, k) for k in ("x1", "y1", "x2", "y2", "x3", "y3"))
        kind = el.get("dtype") or "parallel"
        if kind in ("horizontal", "vertical"):
            # measure only the x (or y) distance, as EAGLE does
            x2, y2 = (x2, y1) if kind == "horizontal" else (x1, y2)
        elif kind != "parallel":
            self.warnings.append(f"{kind} dimension converted as an aligned dimension")
        if math.hypot(x2 - x1, y2 - y1) < 1e-6:
            return
        d = pcbnew.PCB_DIM_ALIGNED(self.board, pcbnew.PCB_DIM_ALIGNED_T)
        d.SetLayer(layer)
        d.SetStart(P(x1, y1))
        d.SetEnd(P(x2, y2))
        # signed distance of the text point from the measured line, in KiCad coordinates
        ax, ay, bx, by, cx, cy = x1, -y1, x2, -y2, x3, -y3
        L = math.hypot(bx - ax, by - ay)
        h = ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / L
        d.SetHeight(FROM_MM(h))
        d.Update()
        exp = pcbnew.VECTOR2I(FROM_MM(ax - (by - ay) / L * h), FROM_MM(ay + (bx - ax) / L * h))
        if (d.GetCrossbarStart() - exp).EuclideanNorm() > FROM_MM(0.01):
            d.SetHeight(FROM_MM(-h))
            d.Update()
        d.SetLineThickness(FROM_MM(fnum(el, "width", 0.13) or 0.13))
        size = fnum(el, "textsize", 1.27)
        d.SetTextSize(pcbnew.VECTOR2I(FROM_MM(size), FROM_MM(size)))
        self.board.Add(d)
        self.counts["dimensions"] += 1

    def add_plain(self):
        plain = self.b_el.find("plain")
        plain_milling = []
        plain_polys = 0
        for el in (list(plain) if plain is not None else []):
            n = int(el.get("layer") or 0)
            tag = el.tag
            if tag == "polygon":
                plain_polys += 1
            if tag == "hole":
                self.add_hole(el)
                continue
            if tag == "frame":
                self.warnings.append("drawing frame on the board skipped")
                continue
            if tag in ("wire", "rectangle", "circle", "polygon") and n in RESTRICT:
                self.add_rule_area(el, n, f"EAGLE {RESTRICT[n]} restrict")
                continue
            if tag == "polygon" and n in self.copper:
                self.add_zone(el, self.copper[n], None, n, {"signal": "", "index": plain_polys - 1})
                continue
            if n == 46 and tag in ("wire", "circle") and fnum(el, "width") > 0:
                plain_milling.append(el)
                continue
            layer = self.layer(n)
            if layer is None:
                self.warnings.append(f"plain {tag} on EAGLE layer {n} skipped")
                continue
            if tag in ("wire", "rectangle", "circle", "polygon"):
                s = self.make_shape(el, layer)
                if tag in ("wire", "circle") and fnum(el, "width") == 0 and not s.IsFilled():
                    s.SetWidth(self.board.GetDesignSettings().GetLineThickness(layer))
                self.board.Add(s)
                self.counts["edge" if layer == pcbnew.Edge_Cuts else "graphics"] += 1
            elif tag == "text":
                self.add_text(el, layer)
            elif tag == "dimension":
                self.add_dimension(el, layer)
            else:
                self.warnings.append(f"plain <{tag}> not supported, skipped")
        if plain_milling:
            self.counts["milling cut-outs"] += self.add_milling_group(plain_milling, self.board, "the board")

    # ------------------------------------------------------------------ build / save
    def build(self, sch: Path | None = None):
        self.counts = defaultdict(int)
        self.zone_records: list[dict] = []
        self.net_map: dict[str, str] = {}
        self.net_classes: dict[str, str] = {}
        self.holes: dict[str, dict] = {}
        self.apply_rules()
        self.place_elements()
        self.add_signals()
        self.add_plain()
        self.repair_outline()
        self.finalize_milling()
        self.explode_edge_polys()
        if sch is not None:
            self.assign_schematic_only_nets(sch)
        ns = self.board.GetDesignSettings().m_NetSettings
        for net, cls in sorted(self.net_classes.items()):
            ns.SetNetclassPatternAssignment(net, cls)
        ds = self.board.GetDesignSettings()
        ds.m_TentViasFront = self.board_tent
        ds.m_TentViasBack = self.board_tent
        self.board.BuildConnectivity()

    def save(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        pcb = out_dir / f"{self.project}.kicad_pcb"
        if not pcbnew.SaveBoard(str(pcb), self.board):
            raise SystemExit(f"KiCad failed to save {pcb}")
        written = [pcb, out_dir / f"{self.project}.kicad_pro"]
        dru = out_dir / f"{self.project}.kicad_dru"
        dru.write_text("\n".join(self.dru) + "\n", encoding="utf-8", newline="\n")
        written.append(dru)
        lib = out_dir / f"{self.project}.pretty"
        if lib.exists():
            clear_dir(lib)
        kio = pcbnew.PCB_IO_MGR.PluginFind(pcbnew.PCB_IO_MGR.KICAD_SEXP)
        lib.mkdir(exist_ok=True)  # a KiCad .pretty library is a plain folder (KiCad 9's SWIG FootprintLibCreate is broken)
        for fpid, (item, fp) in self.lib_fps.items():
            if fpid.split(":", 1)[0] == self.project:
                fp.SetFPID(pcbnew.LIB_ID("", item))
                kio.FootprintSave(str(lib), fp)
        written.append(lib)
        table = out_dir / "fp-lib-table"
        table.write_text(
            "(fp_lib_table\n  (version 7)\n"
            f'  (lib (name "{self.project}")(type "KiCad")(uri "${{KIPRJMOD}}/{self.project}.pretty")'
            '(options "")(descr "Footprints converted from EAGLE/Fusion 360"))\n)\n',
            encoding="utf-8", newline="\n")
        written.append(table)
        bmap = {
            "project": self.project,
            "copper": {str(n): pcbnew.BOARD.GetStandardLayerName(l) for n, l in sorted(self.copper.items())},
            "nets": self.net_map, "net_classes": self.net_classes,
            "elements": self.elements_info, "holes": self.holes, "zones": self.zone_records,
        }
        bm = out_dir / f"{self.project}.board_map.json"
        bm.write_text(json.dumps(bmap, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        written.append(bm)
        return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert an EAGLE 9.x / Fusion 360 board to a KiCad 9 board + project.")
    ap.add_argument("brd", type=Path, help="EAGLE .brd exported from Fusion 360")
    ap.add_argument("--map", type=Path, help="<project>.eagle_map.json written by eagle_sch_to_kicad.py")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--project", help="KiCad project name (default: from --map, else board file name)")
    ap.add_argument("--sch", type=Path, help="converted .kicad_sch (default: <project>.kicad_sch next to --map); "
                                              "used to give unconnected pins KiCad's 'unconnected-(...)' nets")
    args = ap.parse_args(argv)
    mapping = json.loads(args.map.read_text(encoding="utf-8")) if args.map else None
    project = args.project or (mapping or {}).get("project") or args.brd.stem
    sch = args.sch or (args.map.parent / f"{project}.kicad_sch" if args.map else None)
    if sch is not None and not sch.is_file():
        print(f"WARNING: {sch} not found; skipping schematic-only nets")
        sch = None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        bb = BoardBuilder(args.brd, mapping, project, Path(td))
        bb.build(sch)
        written = bb.save(args.out_dir)
    for p in written:
        print(f"wrote {p}")
    for w in dict.fromkeys(bb.warnings):
        print(("" if w.startswith("ERROR") else "WARNING: ") + w)
    for note in bb.notes:
        print("NOTE: " + note)
    for line in KICAD_LOG.summary():
        print(line)
    c = bb.counts
    print(f"{len(bb.elements_info)} footprints ({bb.pad_fixes} pad sizes set from EAGLE restring rules), "
          f"{len(bb.nets)} nets ({c['schematic-only nets']} unconnected-pin nets from the schematic), "
          f"{c['tracks']} tracks, {c['vias']} vias, {c['zones']} zones, "
          f"{c['rule areas']} board + {c['package rule areas']} package rule areas, {c['edge']} outline segments, "
          f"{len(bb.holes)} holes, "
          f"{c['milling cut-outs']} board + {c['package milling items']} package milling cut-outs, "
          f"{c['graphics']} graphics, {c['texts']} texts, {c['dimensions']} dimensions")
    return 1 if any(w.startswith("ERROR") for w in bb.warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
