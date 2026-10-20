"""Shared helpers for the EAGLE/Fusion 360 <-> KiCad conversion tools (stdlib only)."""

from __future__ import annotations

import math
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path

# Fixed namespace so every UUID we generate is deterministic (re-running a conversion
# produces byte-identical output, which keeps diffs and Quilter re-uploads clean).
UUID_NS = uuid.UUID("6f1c2a9e-3b0d-4c55-9a51-5e2f0b7c1d42")


def clear_dir(folder: Path, tries: int = 10) -> None:
    """Empty a folder for regeneration.  The folder itself (and any sub-folder that stays locked) is kept:
    Explorer, editors' file watchers and Dropbox often hold a folder handle, which makes deleting it fail on
    Windows, while its files are usually only locked for a moment (sync), hence the retries."""
    for attempt in range(tries):
        busy = []
        for p in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if p.is_dir() and not p.is_symlink():
                    p.rmdir()
                else:
                    p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                if not p.is_dir():
                    busy.append(p)
        if not busy:
            return
        time.sleep(0.5)
    raise SystemExit(f"cannot delete {busy[0]} (and {len(busy) - 1} more): close the program using it and run again")


def det_uuid(*parts: object) -> str:
    return str(uuid.uuid5(UUID_NS, "|".join(str(p) for p in parts)))


# --------------------------------------------------------------------------- S-expressions
class Sym(str):
    """An unquoted S-expression token (keyword)."""


def fmt_num(v: float) -> str:
    if isinstance(v, bool):
        raise TypeError("bool is not a number token")
    if isinstance(v, int):
        return str(v)
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return f'"{s}"'


def dumps(node, indent: int = 0) -> str:
    """Serialise nested lists into KiCad-style S-expression text.

    node = [head, item, ...]; head is the keyword, items are Sym/str/int/float or nested lists.
    """
    head, items = node[0], node[1:]
    atoms = []
    children = []
    for it in items:
        if isinstance(it, list):
            children.append(it)
        elif isinstance(it, Sym):
            atoms.append(str(it))
        elif isinstance(it, str):
            atoms.append(quote(it))
        elif isinstance(it, (int, float)):
            atoms.append(fmt_num(it))
        else:
            raise TypeError(f"unsupported token {it!r}")
    pad = "\t" * indent
    line = pad + "(" + " ".join([str(head)] + atoms)
    if not children:
        return line + ")"
    # keep short coordinate lists on one line, like KiCad does for (pts (xy ..) (xy ..))
    if all(not any(isinstance(x, list) for x in c[1:]) for c in children) and head in ("pts",):
        return line + " " + " ".join(dumps(c).strip() for c in children) + ")"
    out = [line]
    for c in children:
        out.append(dumps(c, indent + 1))
    out.append(pad + ")")
    return "\n".join(out)


# --------------------------------------------------------------------------- EAGLE helpers

def outline_spurs(edges: dict, tol: int = 1000) -> set:
    """Dead-end spurs of a board outline drawn with open segments/arcs.

    edges: {id: (start, end)} in nm.  A spur is an open chain hanging off a junction (a point shared by
    3+ edge ends) that ends in a free end: it bounds no area, so EAGLE and board fabs ignore it, but KiCad
    then cannot build the board outline.  A chain with two free ends is a real gap in the outline and is
    not a spur.  -> set of edge ids"""
    def key(p):
        return round(p[0] / tol), round(p[1] / tol)

    ends: dict = {}
    for i, (a, b) in edges.items():
        if key(a) != key(b):
            ends.setdefault(key(a), []).append(i)
            ends.setdefault(key(b), []).append(i)

    def other(i, k):
        a, b = (key(p) for p in edges[i])
        return b if a == k else a

    spurs: set = set()
    for start in [k for k, v in ends.items() if len(v) == 1]:
        chain, cur, e = [], start, ends[start][0]
        while e not in chain:
            chain.append(e)
            cur = other(e, cur)
            nxt = [x for x in ends[cur] if x != e]
            if len(nxt) == 1:
                e = nxt[0]
                continue
            if len(nxt) >= 2:
                spurs.update(chain)
            break
    return spurs

def parse_rot(rot: str | None) -> tuple[bool, bool, float]:
    """EAGLE rotation string ("R90", "MR180", "SMR45", ...) -> (mirror, spin, degrees)."""
    rot = rot or "R0"
    mirror = "M" in rot
    spin = "S" in rot
    m = re.search(r"R(-?[0-9.]+)", rot)
    deg = float(m.group(1)) if m else 0.0
    return mirror, spin, deg % 360.0


def eagle_overbar_to_kicad(text: str) -> str:
    """EAGLE uses '!' to toggle an overbar; KiCad uses ~{...}."""
    if "!" not in text:
        return text
    out, buf, on, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "!":
            (buf if on else out).append("!")
            i += 2
            continue
        if ch == "!":
            if on:
                out.append("~{" + "".join(buf) + "}")
                buf = []
            on = not on
        else:
            (buf if on else out).append(ch)
        i += 1
    if on:
        out.append("~{" + "".join(buf) + "}")
    return "".join(out)


def kicad_net_name(eagle_name: str) -> str:
    """Net name as KiCad stores it (escaped) for an EAGLE net/signal name.

    The same function is used for schematic labels and board nets so they always match.
    """
    name = eagle_overbar_to_kicad(eagle_name)
    return name.replace("/", "{slash}")


def eagle_pin_display_name(pin_name: str) -> str:
    """EAGLE hides everything after '@' (used to make duplicate pin names unique)."""
    return eagle_overbar_to_kicad(pin_name.split("@", 1)[0])


def kicad_ref_map(names) -> dict:
    """EAGLE part/element name -> KiCad reference designator, using the same rules as KiCad's own
    EAGLE importers: KiCad treats a reference without a trailing number as *not annotated* (ERC error,
    renumbered by the annotator), so 'RX' -> 'RX0'; names starting with a digit or '#' get 'UNK'.
    Unchanged names map to themselves; the result is unique and deterministic for a given name set."""
    names = list(dict.fromkeys(n for n in names if n))
    out, taken = {}, set(names)
    for n in names:
        r = n
        if r[0].isdigit():
            r = "UNK" + r
        if r.startswith("#"):
            r = "UNK" + r
        if not r[-1].isdigit():
            r += "0"
        if r != n:
            while r in taken:
                r += "0"
            taken.add(r)
        out[n] = r
    return out


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    t = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    import html as _html

    t = _html.unescape(t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Counter-clockwise rotation in a y-up coordinate system."""
    if deg % 360 == 0:
        return x, y
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    if abs(c) < 1e-12:
        c = 0.0
    if abs(s) < 1e-12:
        s = 0.0
    return x * c - y * s, x * s + y * c


def arc_mid_and_center(x1, y1, x2, y2, curve_deg):
    """EAGLE wire with 'curve' (CCW sweep in degrees, y-up) -> (mid point, center)."""
    th = math.radians(curve_deg)
    dx, dy = x2 - x1, y2 - y1
    d = math.hypot(dx, dy)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    h = (d / 2) / math.tan(th / 2)
    cx, cy = mx - dy / d * h, my + dx / d * h
    vx, vy = rotate(x1 - cx, y1 - cy, curve_deg / 2)
    return (cx + vx, cy + vy), (cx, cy)


# --------------------------------------------------------------------------- EAGLE design rules
def mm_to_nm(v) -> int:
    """mm (EAGLE attribute string or float) -> integer nm exactly as KiCad's EAGLE parser (ECOORD) does it:
    the decimal digits are taken literally and anything below 1 nm is truncated toward zero
    ('64.77' -> 64770000, '0.29336875' -> 293368). Floats use their shortest repr, so 64.77 stays exact."""
    return int(Decimal(v.strip() if isinstance(v, str) else repr(float(v))).scaleb(6))


def dim(v: str | None, default=0.0) -> float:
    """EAGLE dimension string ('6mil', '0.35mm', '0.1in', '1.5') -> mm."""
    if v is None or v == "":
        return default
    m = re.fullmatch(r"\s*([-+0-9.eE]+)\s*(mil|mm|in|mic)?\s*", v)
    if not m:
        return default
    n = float(m.group(1))
    return {"mil": n * 0.0254, "in": n * 25.4, "mic": n / 1000.0}.get(m.group(2) or "mm", n)


def clamp(lo, v, hi):
    return max(lo, min(v, hi))


class Rules:
    """EAGLE <designrules> with the derived values KiCad needs."""

    def __init__(self, board_el):
        self.p = {e.get("name"): e.get("value") for e in board_el.findall("designrules/param")}
        g = self.get
        self.clear = {k: g(k, 0.1524) for k in ("mdWireWire", "mdWirePad", "mdWireVia", "mdPadPad", "mdPadVia",
                                                "mdViaVia", "mdSmdPad", "mdSmdVia", "mdSmdSmd")}
        self.copper_dim = g("mdCopperDimension", 1.016)
        self.hole_hole = g("mdDrill", 0.1524)
        self.min_width = g("msWidth", 0.1524)
        self.min_drill = g("msDrill", 0.35)
        self.use_dia = int(self.p.get("useDiameter", "13") or 13)

    def get(self, k, d=0.0):
        return dim(self.p.get(k), d)

    def ratio(self, k, d):
        try:
            return float(self.p.get(k, d))
        except (TypeError, ValueError):
            return d

    def restring(self, drill, which):  # which: PadTop / PadBottom / ViaOuter
        return clamp(self.get(f"rlMin{which}", 0.254), drill * self.ratio(f"rv{which}", 0.25),
                     self.get(f"rlMax{which}", 0.508))

    def pad_diameter(self, drill, lib_dia):
        """EAGLE: restring from rules; library diameter wins if larger and 'use diameter' is set."""
        d = drill + 2 * max(self.restring(drill, "PadTop"), self.restring(drill, "PadBottom"))
        if lib_dia > 0 and (self.use_dia & 1 or self.use_dia & 4):
            d = max(d, lib_dia)
        return d

    def via_diameter(self, drill, dia):
        return max(drill + 2 * self.restring(drill, "ViaOuter"), dia)

    def stop_margin(self, size):
        return clamp(self.get("mlMinStopFrame", 0.1016), self.ratio("mvStopFrame", 1.0) * size,
                     self.get("mlMaxStopFrame", 0.1016))

    def cream_margin(self, size):
        return -clamp(self.get("mlMinCreamFrame", 0.0), self.ratio("mvCreamFrame", 0.0) * size,
                      self.get("mlMaxCreamFrame", 0.0))

    def smd_round_ratio(self, dx, dy, lib_roundness):
        """KiCad roundrect ratio for an EAGLE SMD (library roundness % vs. design-rule corner radius),
        or None for a plain rectangle."""
        mn = min(dx, dy)
        rr = clamp(2 * self.get("srMinRoundness", 0), mn * self.ratio("srRoundness", 0.0),
                   2 * self.get("srMaxRoundness", 0))
        if not lib_roundness and rr <= 0:
            return None
        ratio = rr / mn / 2 if mn > 0 else 0.0
        return min(0.5, max(lib_roundness / 200.0, ratio))

    def pad_shape(self, lib_shape, first):
        """EAGLE pad shape after the DRC 'Shapes' overrides (-1 = as in library)."""
        names = {0: "square", 1: "round", 2: "octagon"}
        ov = int(self.ratio("psFirst", -1)) if first else -1
        if ov not in names:
            ov = int(self.ratio("psTop", -1))
        return names.get(ov, lib_shape or "round")
