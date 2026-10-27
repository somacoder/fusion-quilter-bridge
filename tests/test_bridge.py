"""Fast public regression checks; no KiCad installation or private designs required."""

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from eagle_common import Rules, det_uuid, mm_to_nm, parse_rot
from kicad_to_fusion import Patch
from browser_download import browser_code, validate_archive
from selftest_roundtrip import ScriptRunner, compare_rest, compare_script
from verify_forward import copper_layer_map, zone_outline_key

DEMO = ROOT / "examples" / "roundtrip-demo"
BOARD = DEMO / "roundtrip-demo.brd"


class PublicFixtureTests(unittest.TestCase):
    def test_pour_thermal_rule_matching_is_explicit_and_uniform(self):
        patch = Patch.__new__(Patch)
        patch.board_el = ET.parse(BOARD).getroot().find("drawing/board")
        patch.rules = Rules(patch.board_el)
        patch.k = SimpleNamespace(zones=[SimpleNamespace(rule_area=False,
                                   opts={"connect": "thermal", "thermal_gap": 25400})])
        before = dict(patch.rules.p)
        patch.notes = []
        patch.pour_rules_changed = False
        patch.match_pour_rules()
        self.assertTrue(patch.pour_rules_changed)
        self.assertEqual(patch.rules.get("slThermalIsolate"), 0.0254)
        for key in before.keys() - {"slThermalIsolate"}:
            self.assertEqual(before[key], patch.rules.p[key])
        patch.k.zones.append(SimpleNamespace(rule_area=False,
                                            opts={"connect": "thermal", "thermal_gap": 254000}))
        with self.assertRaisesRegex(ValueError, "per-zone"):
            patch.match_pour_rules()
    def test_browser_download_archive_validation(self):
        def archive(names):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as target:
                for name in names:
                    target.writestr(name, "(kicad_pcb)")
            return stream.getvalue()
        validate_archive(archive(["candidate.kicad_pcb", "candidate.kicad_pro"]))
        for names in ([], ["candidate.kicad_pro"], ["a.kicad_pcb", "b.kicad_pcb"]):
            with self.assertRaises(ValueError):
                validate_archive(archive(names))
        with self.assertRaises(zipfile.BadZipFile):
            validate_archive(b"not a zip")
        code = browser_code("http://127.0.0.1:8769/example")
        self.assertIn('"http://127.0.0.1:8769/example"', code)
        self.assertNotIn("ENDPOINT", code)

    def test_explicit_via_rule_matching_preserves_pads(self):
        patch = Patch.__new__(Patch)
        patch.board_el = ET.parse(BOARD).getroot().find("drawing/board")
        patch.board_el.find("designrules/param[@name='rlMinViaOuter']").set("value", "0.254mm")
        patch.rules = Rules(patch.board_el)
        patch.k = SimpleNamespace(vias=[SimpleNamespace(size=700700, drill=350350),
                                       SimpleNamespace(size=897573, drill=547223)])
        before = dict(patch.rules.p)
        patch.notes = []
        patch.via_rules_changed = False
        patch.match_via_rules()
        self.assertTrue(patch.via_rules_changed)
        self.assertEqual(patch.rules.get("rlMinViaOuter"), 0.175175)
        self.assertEqual(patch.rules.ratio("rvViaOuter", 1), 0)
        for key in before.keys() - {"rlMinViaOuter", "rvViaOuter"}:
            self.assertEqual(before[key], patch.rules.p[key])
        for via in patch.k.vias:
            self.assertAlmostEqual(patch.rules.via_diameter(via.drill / 1e6, via.size / 1e6),
                                   via.size / 1e6)

    def test_invalid_annulus_is_rejected(self):
        patch = Patch.__new__(Patch)
        patch.k = SimpleNamespace(vias=[SimpleNamespace(size=300000, drill=350000)])
        with self.assertRaisesRegex(ValueError, "annular ring"):
            patch.match_via_rules()

    def test_zone_outline_closure_and_winding_are_equivalent(self):
        ring = [(0, 0), (1000000, 0), (1000000, 1000000), (0, 1000000)]
        expected = zone_outline_key([ring])
        self.assertEqual(expected, zone_outline_key([ring + ring[:1]]))
        self.assertEqual(expected, zone_outline_key([ring[2:] + ring[:2]]))
        self.assertEqual(expected, zone_outline_key([ring[::-1]]))
        self.assertEqual(expected, zone_outline_key([[ring[0]] + ring]))

    def test_zone_outline_preserves_edges_and_holes(self):
        ring = [(0, 0), (1000000, 0), (1000000, 1000000), (0, 1000000)]
        crossed = [ring[0], ring[2], ring[1], ring[3]]
        self.assertNotEqual(zone_outline_key([ring]), zone_outline_key([crossed]))
        self.assertNotEqual(zone_outline_key([ring]), zone_outline_key([ring, ring]))

    def test_identical_outer_pours_do_not_collapse_layer_mapping(self):
        votes = {"F.Cu": {"F.Cu": 1, "B.Cu": 1},
                 "B.Cu": {"F.Cu": 1, "B.Cu": 1},
                 "In1.Cu": {"In2.Cu": 3}}
        self.assertEqual(copper_layer_map(votes),
                         {"F.Cu": "F.Cu", "B.Cu": "B.Cu", "In1.Cu": "In2.Cu"})

    def test_exact_dimensions_and_net_membership(self):
        board = ET.parse(BOARD).getroot().find("drawing/board")
        sch = ET.parse(DEMO / "roundtrip-demo.sch").getroot().find("drawing/schematic")
        self.assertEqual(len(board.findall("elements/element")), 12)
        self.assertEqual(len(board.findall("signals/signal/contactref")), 24)
        signals = {s.get("name"): {(r.get("element"), r.get("pad")) for r in s.findall("contactref")}
                   for s in board.findall("signals/signal")}
        nets = {n.get("name"): {(r.get("part"), {"A": "1", "B": "2"}[r.get("pin")])
                                for r in n.findall("segment/pinref")}
                for n in sch.findall("sheets/sheet/nets/net")}
        self.assertEqual(signals, nets)
        self.assertEqual(len(signals), 4)
        self.assertEqual({(w.get("x1"), w.get("y1")) for w in board.findall("plain/wire")},
                         {("0", "0"), ("80", "0"), ("80", "60"), ("0", "60")})

    def test_fixture_regenerates_byte_identically(self):
        spec = importlib.util.spec_from_file_location("fixture_generator", DEMO / "generate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            module.HERE = Path(tmp)
            module.generate()
            for suffix in ("sch", "brd"):
                name = f"roundtrip-demo.{suffix}"
                self.assertEqual((DEMO / name).read_bytes(), (Path(tmp) / name).read_bytes())

    def test_numeric_and_identity_helpers(self):
        self.assertEqual(mm_to_nm("64.77"), 64770000)
        self.assertEqual(mm_to_nm("-0.001"), -1000)
        self.assertEqual(parse_rot("MR90"), (True, False, 90.0))
        self.assertEqual(det_uuid("test", 1), det_uuid("test", 1))
        self.assertNotEqual(det_uuid("test", 1), det_uuid("test", 2))


class ScriptRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def script(self, commands):
        path = self.dir / "commands.scr"
        path.write_text("GRID MM FINEST;\n" + commands + "\nGRID LAST;\n", encoding="utf-8")
        return path

    def test_identity(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(compare_script(self.script("RATSNEST;"), BOARD, BOARD))
            self.assertTrue(compare_rest(BOARD, BOARD))

    def test_one_micron_placement_error_is_caught(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(compare_script(self.script("MOVE 'R1' (-14.999 0);"), BOARD, BOARD))

    def test_hidden_origin_pick_is_caught_even_when_visibility_restored(self):
        runner = ScriptRunner(BOARD).run(self.script("DISPLAY -23;\nMOVE 'R1' (10 10);\nDISPLAY 23;"))
        self.assertTrue(any("hidden" in e for e in runner.errors), runner.errors)

    def test_unrestored_visibility_is_caught(self):
        runner = ScriptRunner(BOARD).run(self.script("DISPLAY -23;"))
        self.assertTrue(any("not restored" in e for e in runner.errors), runner.errors)

    def test_missing_ripup_is_caught(self):
        tree = ET.parse(BOARD)
        signal = tree.getroot().find("drawing/board/signals/signal")
        ET.SubElement(signal, "wire", x1="-17.54", y1="0", x2="0", y2="0", width="0.25", layer="1")
        board = self.dir / "wired.brd"
        tree.write(board, encoding="utf-8")
        runner = ScriptRunner(board).run(self.script("MOVE 'R1' (10 10);"))
        self.assertTrue(any("would drag copper" in e for e in runner.errors), runner.errors)
        good = ScriptRunner(board).run(self.script("RIPUP 'NET_A';\nMOVE 'R1' (10 10);"))
        self.assertEqual(good.errors, [])

    def test_changed_library_is_caught(self):
        tree = ET.parse(BOARD)
        tree.getroot().find("drawing/board/libraries/library/packages/package/smd").set("dx", "9")
        path = self.dir / "changed.brd"
        tree.write(path, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(compare_rest(BOARD, path))

    def test_unknown_command_is_caught(self):
        runner = ScriptRunner(BOARD).run(self.script("NOT_A_COMMAND;"))
        self.assertTrue(any("unknown command" in e for e in runner.errors), runner.errors)

    def test_drc_load_applies_emitted_rules(self):
        dru = self.dir / "test.dru"
        dru.write_text("rlMinViaOuter = 0.175175mm\nrvViaOuter = 0\n", encoding="utf-8")
        runner = ScriptRunner(BOARD).run(self.script(f"DRC LOAD '{dru}';"))
        self.assertEqual(runner.errors, [])
        self.assertEqual(Rules(runner.board).get("rlMinViaOuter"), 0.175175)


if __name__ == "__main__":
    unittest.main()
