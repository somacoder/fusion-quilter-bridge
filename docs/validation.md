# Reproducible evidence

The evidence below uses only the [original public fixture](../examples/roundtrip-demo).
It was reproduced locally on **2026-10-20**, on Windows with **KiCad 9.0.8 and
10.0.7**. These are tested versions, not a claim about all future releases.

## Integration scenarios

| Scenario | Observed result | Scope |
| :--- | :--- | :--- |
| Forward | PASS | 4 nets; 12 parts; 24 pads; 8 package restriction wires; closed 80 × 60 mm outline; schematic parity. |
| Identity | PASS | No placement/copper edits; canonical XML equals the source export. |
| First layout | PASS | 12 parts, 2 flipped; 47 straight tracks; 7 vias; 3 pours; 24 smashed texts. |
| Second pass | PASS | Routed board reconverts; 4 parts edited; 43 straight tracks + 2 arcs; 3 vias; 3 pours after removal/replacement. |

For each return, KiCad 10's EAGLE importer agreed with the candidate's footprint,
pad, track, via, outline and supported pour geometry. The script model agreed
with the patched board. Non-layout XML, libraries, rules and net membership were
checked for preservation.

Importer comparisons generally use micrometre-scale geometry tolerance.
Smashed text comparisons in the script model allow 5 nm for numerical rounding.
Canonical XML equality ignores serialization syntax differences, not design data.

## Run it yourself

```powershell
.\Fusion_to_Quilter.cmd --input examples\roundtrip-demo
.\Selftest_Roundtrip.cmd --name roundtrip-demo
python -m unittest discover -s tests -v
python tools\check_release.py
```

Inspect `to_quilter\roundtrip-demo\logs\verify.log` and `selftest.log`.
Use `--keep` on the self-test to preserve its temporary candidates and return
files for inspection. Runtime logs are intentionally not distributed, since logs
from real use may contain local usernames, paths and private design details.

The fast tests include canonical example regeneration, exact net membership,
one-micrometre movement detection, hidden-layer selection, missing RIPUP, changed
library geometry, unknown commands and layer visibility restoration.

## What PASS does not mean

* The simulated routing is not an actual Quilter result.
* The generated script has not been executed inside Fusion in this test.
* The synthetic circuit is not electrically meaningful or safe to manufacture.
* DRC violations caused by intentionally crude simulated routing are expected.
* Branding approval, code ownership, regulatory compliance and manufacturing
  suitability are not determined by software tests.

GitHub Actions runs the standard-library tests and release inventory check.
KiCad integration tests are run separately; CI intentionally does not imply they
ran on every commit.
