"""Generate an original, nonfunctional EAGLE conversion fixture. No external libraries or designs."""

from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
NETS = ("NET_A", "NET_B", "NET_C", "NET_D")
LAYERS = {
    1: "Top", 16: "Bottom", 17: "Pads", 18: "Vias", 19: "Unrouted", 20: "Dimension",
    21: "tPlace", 22: "bPlace", 23: "tOrigins", 24: "bOrigins", 25: "tNames", 26: "bNames",
    27: "tValues", 28: "bValues", 29: "tStop", 30: "bStop", 31: "tCream", 32: "bCream",
    39: "tKeepout", 40: "bKeepout", 41: "tRestrict", 42: "bRestrict", 43: "vRestrict",
    44: "Drills", 45: "Holes", 46: "Milling", 47: "Measures", 48: "Document",
    51: "tDocu", 52: "bDocu", 91: "Nets", 92: "Busses", 93: "Pins", 94: "Symbols",
    95: "Names", 96: "Values",
}


def add(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})


def library():
    lib = ET.Element("library", name="bridge-demo")
    add(lib, "description").text = "Original synthetic test geometry; not an electronic product."
    packages = add(lib, "packages")
    for kind in ("SMD", "THT"):
        pkg = add(packages, "package", name=f"DEMO_{kind}")
        for name, x in (("1", -2.54), ("2", 2.54)):
            if kind == "SMD":
                add(pkg, "smd", name=name, x=x, y=0, dx=1.5, dy=2, layer=1)
            else:
                add(pkg, "pad", name=name, x=x, y=0, drill=0.8, diameter=1.8, shape="round")
        add(pkg, "wire", x1=-1, y1=-1, x2=1, y2=-1, width=0.15, layer=21)
        add(pkg, "wire", x1=1, y1=-1, x2=1, y2=1, width=0.15, layer=21)
        add(pkg, "wire", x1=1, y1=1, x2=-1, y2=1, width=0.15, layer=21)
        add(pkg, "wire", x1=-1, y1=1, x2=-1, y2=-1, width=0.15, layer=21)
        add(pkg, "text", x=-1, y=2, size=1, layer=25).text = ">NAME"
        add(pkg, "text", x=-1, y=-3, size=1, layer=27).text = ">VALUE"
        if kind == "SMD":
            add(pkg, "wire", x1=-1, y1=3.5, x2=2, y2=4, width=0.4, layer=41)
    symbols = add(lib, "symbols")
    sym = add(symbols, "symbol", name="PASSIVE")
    add(sym, "wire", x1=-2.54, y1=-1.27, x2=2.54, y2=-1.27, width=0.254, layer=94)
    add(sym, "wire", x1=2.54, y1=-1.27, x2=2.54, y2=1.27, width=0.254, layer=94)
    add(sym, "wire", x1=2.54, y1=1.27, x2=-2.54, y2=1.27, width=0.254, layer=94)
    add(sym, "wire", x1=-2.54, y1=1.27, x2=-2.54, y2=-1.27, width=0.254, layer=94)
    add(sym, "pin", name="A", x=-5.08, y=0, visible="off", length="short", direction="pas")
    add(sym, "pin", name="B", x=5.08, y=0, visible="off", length="short", direction="pas", rot="R180")
    add(sym, "text", x=-2.54, y=2.54, size=1.27, layer=95).text = ">NAME"
    add(sym, "text", x=-2.54, y=-3.81, size=1.27, layer=96).text = ">VALUE"
    ds = add(lib, "devicesets")
    for kind in ("SMD", "THT"):
        devset = add(ds, "deviceset", name=f"DEMO_{kind}", prefix="R", uservalue="yes")
        add(add(devset, "gates"), "gate", name="G$1", symbol="PASSIVE", x=0, y=0)
        dev = add(add(devset, "devices"), "device", name="", package=f"DEMO_{kind}")
        cs = add(dev, "connects")
        for pin, pad in (("A", 1), ("B", 2)):
            add(cs, "connect", gate="G$1", pin=pin, pad=pad)
        add(add(dev, "technologies"), "technology", name="")
    return lib


def document(kind):
    root = ET.Element("eagle", version="9.6.2")
    drawing = add(root, "drawing")
    add(add(drawing, "settings"), "setting", alwaysvectorfont="no")
    add(drawing, "grid", distance=0.1, unitdist="mm", unit="mm", style="lines",
        multiple=1, display="no", altdistance=0.01, altunitdist="mm", altunit="mm")
    layers = add(drawing, "layers")
    for n, name in LAYERS.items():
        add(layers, "layer", number=n, name=name, color=4 if n == 1 else 1,
            fill=1, visible="yes", active="yes")
    body = add(drawing, kind)
    add(body, "plain")
    add(body, "libraries").append(deepcopy(library()))
    add(body, "attributes")
    add(body, "variantdefs")
    add(add(body, "classes"), "class", number=0, name="default", width=0, drill=0)
    return root, body


def generate():
    sch, schematic = document("schematic")
    brd, board = document("board")
    plain = board.find("plain")
    for a, b in (((0, 0), (80, 0)), ((80, 0), (80, 60)), ((80, 60), (0, 60)), ((0, 60), (0, 0))):
        add(plain, "wire", x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=0, layer=20)
    rules = add(board, "designrules", name="Synthetic demo")
    for name, value in (("layerSetup", "(1*16)"), ("mdWireWire", "0.2mm"),
                        ("msWidth", "0.2mm"), ("msDrill", "0.3mm"),
                        ("rlMinViaOuter", "0.15mm"), ("rlMinPadTop", "0.25mm"),
                        ("rlMinPadBottom", "0.25mm")):
        add(rules, "param", name=name, value=value)
    add(board, "autorouter")
    elements, signals = add(board, "elements"), add(board, "signals")
    parts = add(schematic, "parts")
    sheet = add(add(schematic, "sheets"), "sheet")
    add(sheet, "plain")
    instances = add(sheet, "instances")
    add(sheet, "busses")
    nets = add(sheet, "nets")
    net_nodes = {n: add(nets, "net", **{"name": n, "class": 0}) for n in NETS}
    sig_nodes = {n: add(signals, "signal", name=n) for n in NETS}
    for i in range(12):
        name, kind = f"R{i + 1}", "THT" if i % 3 == 0 else "SMD"
        x, y = 25.4 + (i % 4) * 30.48, 101.6 - (i // 4) * 25.4
        add(parts, "part", name=name, library="bridge-demo", deviceset=f"DEMO_{kind}",
            device="", value=f"test-{i + 1:02}")
        add(instances, "instance", part=name, gate="G$1", x=x, y=y, smashed="no")
        el = add(elements, "element", name=name, library="bridge-demo", package=f"DEMO_{kind}",
                 value=f"test-{i + 1:02}", x=-15 - (i % 3) * 15, y=(i // 3) * 12,
                 smashed="yes", rot="R0")
        for attr, dy, layer in (("NAME", 2, 25), ("VALUE", -3, 27)):
            add(el, "attribute", name=attr, x=float(el.get("x")), y=float(el.get("y")) + dy,
                size=1, layer=layer)
        for j, (pin, dx) in enumerate((("A", -5.08), ("B", 5.08))):
            net = NETS[(i + j) % 4]
            add(sig_nodes[net], "contactref", element=name, pad=j + 1)
            seg = add(net_nodes[net], "segment")
            add(seg, "pinref", part=name, gate="G$1", pin=pin)
            end = x + dx + (-5.08 if j == 0 else 5.08)
            add(seg, "wire", x1=x + dx, y1=y, x2=end, y2=y, width=0.1524, layer=91)
            add(seg, "label", x=end, y=y, size=1.27, layer=95)
    for suffix, root in (("sch", sch), ("brd", brd)):
        ET.indent(root)
        data = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'
        data += ET.tostring(root, encoding="unicode") + "\n"
        (HERE / f"roundtrip-demo.{suffix}").write_text(data, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    generate()
