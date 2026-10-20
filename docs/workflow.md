# Workflow reference

[Overview](../README.md) · [Validation](validation.md)

## Forward

The Windows launcher uses KiCad 9's bundled Python:

```powershell
.\Fusion_to_Quilter.cmd --input input --name my-board
```

Explicit source paths are also supported:

```powershell
.\Fusion_to_Quilter.cmd --sch input\my-board.sch --brd input\my-board.brd
```

Under `to_quilter\<name>`:

| Path | Meaning |
| :--- | :--- |
| `upload` | Board, project settings and all schematic sheets to upload individually. |
| `fusion_source` | Exact EAGLE source snapshot used to construct the return patch. |
| `*.board_map.json`, `*.eagle_map.json` | Name, layer and identity mappings. Keep these locally. |
| `*.pretty`, symbol library, library tables | Locally generated, self-contained KiCad libraries. |
| `logs` | Conversion and verification output. May contain sensitive design details. |

**Re-running replaces generated contents for that design.** Archive a completed
run before sending another job if you may need the old mapping or source snapshot.
Do not change the live Fusion design while a job is in progress.

The schematic converter is original Python code. It is not an invocation of
KiCad's GUI-only EAGLE schematic importer. Board construction uses KiCad's
`pcbnew` API and official EAGLE footprint reader. Verification combines source
XML comparisons with KiCad's netlister and board importer.

## Quilter

Create a job in [Quilter](https://app.quilter.ai), upload the individual generated
files, inspect its preview and constraints, run the job, and choose a candidate.
Verify outline dimensions, mounting positions and intended locked parts there.
Download its KiCad board or candidate ZIP.

No public API integration is implemented here. Do not assume that a Copilot,
Fusion or GitHub login provides access to Quilter. Uploading a board discloses
that design to Quilter under its own service terms.

## Return

The default is the newest KiCad board or ZIP in `from_quilter`. To avoid ambiguity:

```powershell
.\Quilter_to_Fusion.cmd from_quilter\candidate.kicad_pcb --name my-board
```

Use `--input <folder>` if the current Fusion export is stored elsewhere; that
folder is compared with the source snapshot to warn about intervening changes.
The tool cannot detect unsaved edits in a running Fusion session.

Two output forms go to `to_fusion`:

* `.scr`: placement and copper commands for the original open PCB. This avoids
  intentionally rebuilding the schematic or library objects.
* `.brd`: the exported board XML patched with the corresponding changes. Opening
  it as a separate design does not recreate the original cloud design/history.

The importer check compares the patched board to the candidate; a difference
returns failure. If KiCad 10 is absent, normal conversion reports a verification
skip. For full verification install it; do not treat a skip as a pass.
The self-test requires the independent importer.

Back up the design before applying the script, inspect warnings, and run the
printed `SCRIPT` command in the original Fusion PCB. The script includes
RATSNEST; run Fusion DRC and inspect the result before any manufacturing export.
No Fusion GUI execution has been automated or certified by the test suite.

### Return limitations

* The original outline and drawing content are retained, not adopted from Quilter.
* Original locked parts are not moved; warnings identify them.
* Free holes can be patched in the `.brd`; their script counterpart needs manual
  attention. The two outputs are not guaranteed equivalent for unsupported cases.
* Fusion may enlarge vias that violate its annular-ring/restring limits.
* Router rule areas, teardrops, holes in pours and some KiCad pad-connection modes
  do not translate exactly. Known cases produce warnings.
* Polygon width corresponds to minimum thickness or hatch width, depending on
  fill style. Inspect clearances and filled geometry after Fusion repours.
* The script restores the grid and source-export layer visibility, not necessarily
  unsaved changes to visibility in the current editor. Wire bend remains straight;
  optimizing is set on at the end.
* Geometry comparisons have tolerances. They do not prove arbitrary metadata,
  3D assemblies or cloud-linked objects survived a real editor operation.

## Automation and diagnostics

Set `QUILTER_NOPAUSE=1` when calling launchers from another script. Check exit
codes and logs; never proceed after a failure merely because output files exist.
The Python entry points accept `--help` and explicit input/output directories.
Windows is the supported workflow; other platforms have not been integration-tested.

KiCad reader notes are collected into logs. wxWidgets assertion dialogs are
disabled in the headless paths, and KiCad 10 is used through its CLI rather than
its Python module. Disabling a dialog is not evidence of data fidelity: the
comparisons remain the acceptance criteria.
