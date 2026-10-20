#!/usr/bin/env python3
"""Fusion 360 -> Quilter in one step.

  input\\<name>.sch + input\\<name>.brd        Fusion 360 Electronics: File > Export > EAGLE 9.x (schematic and board)
    -> to_quilter\\<name>\\                    complete KiCad 9 project (open it in KiCad to inspect, if you like)
    -> to_quilter\\<name>\\upload\\            just the files to drag into Quilter (.kicad_pcb, .kicad_sch, .kicad_pro)

Steps
  1. eagle_sch_to_kicad.py   schematic + symbols, net names, values and attributes
  2. eagle_brd_to_kicad.py   board: footprints (from the board's own EAGLE libraries), outline, stack-up,
                             design rules, net classes, existing copper, texts and graphics
  3. verify_forward.py       netlist == EAGLE schematic == EAGLE board, KiCad schematic parity, closed outline,
                             and (if KiCad 10 is installed) geometry against KiCad's official EAGLE importer
  4. Quilter readiness       one closed Edge.Cuts outline, which parts Quilter will place / keep fixed

The exported .brd/.sch are copied to to_quilter\\<name>\\fusion_source\\; quilter_to_fusion.py uses that exact
copy to build the return script, so the result always matches what was sent to Quilter.

Run with KiCad 9's python (pcbnew); Fusion_to_Quilter.cmd finds it for you.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from eagle_common import clear_dir  # noqa: E402
from kicad_pcb_reader import arc_points, kid, kids, load_board, parse_sexpr  # noqa: E402

NOISE = ("duplicate image handler", "memory leak")


def step(title: str, args: list, log: Path) -> int:
    print(f"\n--- {title}")
    p = subprocess.run([sys.executable, "-u", *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="replace")
    out = [l for l in p.stdout.splitlines() if l.strip() and not any(n in l for n in NOISE)]
    log.write_text("\n".join(out) + "\n", encoding="utf-8")
    for l in out:
        print("    " + l)
    return p.returncode


def find_pair(args) -> tuple[Path, Path]:
    if args.brd or args.sch:
        brd = args.brd or args.sch.with_suffix(".brd")
        sch = args.sch or brd.with_suffix(".sch")
    else:
        brds = sorted(args.input.glob("*.brd"), key=lambda p: p.stat().st_mtime, reverse=True)
        brds = [b for b in brds if b.with_suffix(".sch").exists() and (not args.name or b.stem == args.name)]
        if not brds:
            raise SystemExit(f"no <name>.brd + <name>.sch pair found in {args.input}\n"
                             "Export both from Fusion 360 (File > Export > EAGLE 9.x) into that folder.")
        if len(brds) > 1:
            print(f"NOTE: several designs in {args.input}; using the newest, {brds[0].stem} "
                  f"(choose another with --name): {', '.join(b.stem for b in brds)}")
        brd, sch = brds[0], brds[0].with_suffix(".sch")
    for f in (sch, brd):
        if not f.exists():
            raise SystemExit(f"not found: {f}")
    return sch, brd


def outline_segments(board):
    segs = []
    for s in board.shapes:
        if s.layer != "Edge.Cuts":
            continue
        if s.kind == "line":
            segs.append((s.pts[0], s.pts[1]))
        elif s.kind == "arc":
            pts = arc_points(*s.pts)
            segs += list(zip(pts, pts[1:]))
        elif s.kind == "rect":
            (x1, y1), (x2, y2) = s.pts
            c = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            segs += [(c[i - 1], c[i]) for i in range(4)]
        elif s.kind == "circle":
            (cx, cy), p = s.pts
            r = math.dist((cx, cy), p)
            c = [(cx + r * math.cos(math.tau * i / 72), cy + r * math.sin(math.tau * i / 72)) for i in range(72)]
            segs += [(c[i - 1], c[i]) for i in range(72)]
        elif s.kind in ("poly", "polygon") and len(s.pts) > 2:
            segs += [(s.pts[i - 1], s.pts[i]) for i in range(len(s.pts))]
    return segs


def inside(pt, segs) -> bool:
    x, y, odd = pt[0], pt[1], False
    for (x1, y1), (x2, y2) in segs:
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            odd = not odd
    return odd


def readiness(pcb: Path) -> bool:
    """Quilter's input rules (docs.quilter.ai > Prepare your input board file)."""
    print("\n--- Quilter readiness")
    board = load_board(pcb)
    ok = True
    segs = outline_segments(board)
    if not segs:
        print("    FAIL  no board outline on Edge.Cuts - draw the board outline (Dimension layer) in Fusion")
        return False
    xs = [p[0] for s in segs for p in s]
    ys = [p[1] for s in segs for p in s]
    print(f"    PASS  board outline on Edge.Cuts: {(max(xs) - min(xs)) / 1e6:g} x {(max(ys) - min(ys)) / 1e6:g} mm "
          f"({len(segs)} edge segments; closed and valid per KiCad DRC above)")
    root = parse_sexpr(pcb.read_text(encoding="utf-8"))
    fp_edges = [str(kid(fp, "property")[2]) if kid(fp, "property") else "?" for fp in kids(root, "footprint")
                if any(isinstance(n, list) and kid(n, "layer") is not None and str(kid(n, "layer")[1]) == "Edge.Cuts"
                       for n in fp[1:])]
    if fp_edges:
        print(f"    NOTE  {len(fp_edges)} footprint(s) carry Edge.Cuts cut-outs (EAGLE milling): {fp_edges[:10]}; "
              "Quilter's docs say extra shapes on the outline layer can confuse its parser - check its preview")
    fixed = sorted(f.ref for f in board.footprints if inside((f.x, f.y), segs))
    free = sorted(f.ref for f in board.footprints if f.ref not in fixed)
    print(f"    INFO  {len(free)} part(s) outside the outline -> Quilter places them"
          + (f": {', '.join(free)}" if len(free) <= 40 else ""))
    if fixed:
        print(f"    INFO  {len(fixed)} part(s) inside the outline -> Quilter keeps them fixed (pre-placed): "
              f"{', '.join(fixed)}")
    else:
        print("    TIP   nothing is pre-placed. Quilter cannot tell connectors from other parts; to fix the position "
              "of connectors, mounting holes etc., move them onto the board in Fusion before exporting")
    if board.tracks or board.vias:
        print(f"    INFO  {len(board.tracks)} track(s) / {len(board.vias)} via(s) already routed -> kept by Quilter")
    named = [v["user"] for k, v in board.layers.items() if k.endswith(".Cu") and v.get("user")]
    if named:
        print(f"    INFO  copper layer names {named}: Quilter recognises plane layers named GND/ground or PWR/power")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=ROOT / "input", help="folder with the Fusion EAGLE export")
    ap.add_argument("--name", help="design name when the input folder holds several")
    ap.add_argument("--sch", type=Path, help="EAGLE .sch (instead of --input)")
    ap.add_argument("--brd", type=Path, help="EAGLE .brd (instead of --input)")
    ap.add_argument("--out", type=Path, default=ROOT / "to_quilter", help="output folder (default: to_quilter)")
    ap.add_argument("--no-verify", action="store_true", help="skip the verification step")
    a = ap.parse_args(argv)
    if importlib.util.find_spec("pcbnew") is None:
        raise SystemExit("Run this with KiCad 9's python (it provides pcbnew), e.g. via Fusion_to_Quilter.cmd")

    sch, brd = find_pair(a)
    name = brd.stem
    proj = a.out / name
    print(f"Fusion export : {sch}\n                {brd}\nKiCad project : {proj}")
    if proj.exists():
        mine = any(proj.glob("*.board_map.json")) or (proj / "fusion_source").is_dir()
        if not mine and any(proj.iterdir()):
            raise SystemExit(f"{proj} exists and was not made by this tool; move it away first")
        clear_dir(proj)  # keeps the folder itself: Explorer, editors and Dropbox often hold it open
    src = proj / "fusion_source"
    src.mkdir(parents=True, exist_ok=True)
    for f in (sch, brd):
        shutil.copy2(f, src / f.name)
    sch, brd = src / sch.name, src / brd.name
    logs = proj / "logs"
    logs.mkdir(exist_ok=True)

    if step("1/4 schematic", [TOOLS / "eagle_sch_to_kicad.py", sch, "--brd", brd, "--out-dir", proj,
                              "--project", name], logs / "schematic.log"):
        raise SystemExit("schematic conversion failed")
    emap = proj / f"{name}.eagle_map.json"
    if step("2/4 board", [TOOLS / "eagle_brd_to_kicad.py", brd, "--map", emap, "--out-dir", proj],
            logs / "board.log"):
        raise SystemExit("board conversion failed")
    pcb, ksch = proj / f"{name}.kicad_pcb", proj / f"{name}.kicad_sch"
    ok = True
    if not a.no_verify:
        ok = step("3/4 verification", [TOOLS / "verify_forward.py", "--sch", ksch, "--map", emap, "--eagle-brd", brd,
                                       "--pcb", pcb, "--official-import"], logs / "verify.log") == 0
    ok &= readiness(pcb)

    up = proj / "upload"
    up.mkdir(exist_ok=True)
    sheets = sorted(proj.glob("*.kicad_sch"))
    for f in [pcb, proj / f"{name}.kicad_pro", *sheets]:
        shutil.copy2(f, up / f.name)

    print(f"\n{'READY' if ok else 'CHECK THE FAILURES ABOVE'}: upload these {2 + len(sheets)} files to Quilter "
          f"(app.quilter.ai > new job > upload; individual files, not a zip):\n    {up}")
    for f in sorted(up.iterdir()):
        print(f"      {f.name}")
    print(f"When Quilter is done, download the chosen candidate's KiCad board into\n    {ROOT / 'from_quilter'}\n"
          "and run Quilter_to_Fusion.cmd.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
