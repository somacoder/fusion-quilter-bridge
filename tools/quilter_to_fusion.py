#!/usr/bin/env python3
"""Quilter -> Fusion 360 in one step.

  from_quilter\\...\\<board>.kicad_pcb   the layout candidate downloaded from Quilter (default: the newest .kicad_pcb
                                       or .zip in from_quilter; .zip downloads are unpacked automatically)
    -> to_fusion\\<board>.scr           EAGLE script for the board open in Fusion 360 (recommended): updates the
                                       design in place, keeping the schematic link, libraries and history
    -> to_fusion\\<board>.brd           the same changes applied to the .brd that was sent to Quilter (alternative)

Uses to_quilter\\<name>\\ written by fusion_to_quilter.py (board map + the exact .brd that was converted).
If KiCad 10 is installed the result is verified: KiCad's official EAGLE importer reads the new .brd and its
placement, pads, tracks, vias, outline and pours are compared with Quilter's board.

Pure Python 3.9+ (no KiCad needed except for the optional verification); Quilter_to_Fusion.cmd runs it.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import kicad_to_fusion  # noqa: E402
from verify_forward import find_kicad10_cli, official_geometry  # noqa: E402


def routed_board(arg: Path | None, inbox: Path) -> Path:
    if arg is None:
        cands = [p for p in inbox.rglob("*") if p.suffix.lower() in (".kicad_pcb", ".zip")
                 and "_unzipped" not in p.parts]
        if not cands:
            raise SystemExit(f"no .kicad_pcb (or .zip) found in {inbox}\n"
                             "Download the chosen layout candidate from Quilter into that folder.")
        arg = max(cands, key=lambda p: p.stat().st_mtime)
    if arg.suffix.lower() == ".zip":
        dest = arg.parent / "_unzipped" / arg.stem
        with zipfile.ZipFile(arg) as z:
            z.extractall(dest)
        pcbs = sorted(dest.rglob("*.kicad_pcb"))
        if len(pcbs) != 1:
            raise SystemExit(f"{arg.name} holds {len(pcbs)} .kicad_pcb files ({', '.join(p.name for p in pcbs)}); "
                             "pass the one you want: quilter_to_fusion.py <path>")
        print(f"unpacked {arg.name}")
        arg = pcbs[0]
    if not arg.exists():
        raise SystemExit(f"not found: {arg}")
    return arg


def project(sent: Path, name: str | None, pcb: Path) -> Path:
    projs = [p.parent for p in sent.glob("*/*.board_map.json")]
    if name:
        projs = [p for p in projs if p.name == name]
    elif len(projs) > 1:
        projs = [p for p in projs if pcb.stem.lower().startswith(p.name.lower())] or projs
    if len(projs) != 1:
        raise SystemExit(f"cannot tell which design {pcb.name} belongs to; found {[p.name for p in projs]} in "
                         f"{sent}. Use --name <design>." if projs else
                         f"no converted design in {sent}; run Fusion_to_Quilter first")
    return projs[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("routed", type=Path, nargs="?", help="Quilter's .kicad_pcb or .zip (default: newest in from_quilter)")
    ap.add_argument("--name", help="design name (folder in to_quilter) when there are several")
    ap.add_argument("--from-quilter", type=Path, default=ROOT / "from_quilter")
    ap.add_argument("--to-quilter", type=Path, default=ROOT / "to_quilter")
    ap.add_argument("--out", type=Path, default=ROOT / "to_fusion")
    ap.add_argument("--input", type=Path, default=ROOT / "input",
                    help="folder with the current Fusion export (to warn if it changed since it was sent)")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)

    pcb = routed_board(a.routed, a.from_quilter)
    proj = project(a.to_quilter, a.name, pcb)
    name = proj.name
    brd = proj / "fusion_source" / f"{name}.brd"
    bmap = proj / f"{name}.board_map.json"
    if not brd.exists():
        raise SystemExit(f"{brd} is missing; re-run Fusion_to_Quilter for this design")
    current = a.input / brd.name
    if current.exists() and current.read_bytes() != brd.read_bytes():
        print(f"WARNING: {current} differs from the board that was sent to Quilter. The script below is made for "
              "the board as sent; if the Fusion design changed since (parts, nets), re-run the round trip.")
    out_name = pcb.stem if pcb.stem != name else f"{name}_routed"
    print(f"Quilter board : {pcb}\nFusion design : {name} (as sent: {brd})\nOutput        : {a.out}\n")

    rc = kicad_to_fusion.main([str(pcb), "--brd", str(brd), "--map", str(bmap), "--out-dir", str(a.out),
                               "--name", out_name])
    if rc:
        return rc
    ok = True
    if not a.no_verify:
        cli10 = find_kicad10_cli()
        if cli10 is None:
            print("\nSKIP  verification needs KiCad 10 (kicad-cli pcb import)")
        else:
            print("\n--- verification: KiCad's official EAGLE importer reads the new .brd; compared with Quilter's board")
            ok = official_geometry(cli10, a.out / f"{out_name}.brd", pcb)
            if not ok:
                print("      (differences that match a WARNING above are items Fusion cannot take, by design)")
    scr = a.out / f"{out_name}.scr"
    print(f"""
Next, in Fusion 360:
  1. Open the design's PCB (the same board that was exported), then Automation > Run Script (or type in the command
     line):  SCRIPT '{scr}'
  2. Check the result: RATSNEST, then DRC. One Undo per command if needed.
Alternative: {a.out / f'{out_name}.brd'} is the exported board with the same changes (File > Open in Fusion).""")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
