# The round-trip demonstrator

An original **80 x 60 mm, two-layer** synthetic fixture with **12 parts, 24 pads
and four nets**. It mixes through-hole and surface-mount packages, smashed labels,
and asymmetric package restriction wires.

**Not a working circuit. Do not manufacture it.** Its routing simulator deliberately
creates geometric stress cases and may create shorts and clearance violations.
Passing conversion checks demonstrates representational agreement, not electrical
correctness or manufacturability.

All CAD here is newly generated from [generate.py](generate.py), using only Python's
standard library. No third-party package library or research design is required.

From the repository root, on Windows with KiCad 9 and 10 installed:

```powershell
.\Fusion_to_Quilter.cmd --input examples\roundtrip-demo
.\Selftest_Roundtrip.cmd --name roundtrip-demo
```

The second command tests unchanged return, simulated placement/routing, and a
second forward/return pass. It exercises rotations, flips, tracks, arcs, vias,
solid and hatched pours, removal/replacement of pours, and smashed text movement.
It is a **simulator**, not an actual Quilter job or execution in Fusion.

To regenerate the two source files:

```powershell
python examples\roundtrip-demo\generate.py
```

Generated outputs and logs remain ignored. Share only this synthetic fixture when
reporting a problem.
