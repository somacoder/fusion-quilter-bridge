"""Fast public regression checks; no KiCad installation or private designs required."""

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from eagle_common import det_uuid, mm_to_nm, parse_rot
from selftest_roundtrip import ScriptRunner, compare_rest, compare_script

DEMO = ROOT / "examples" / "roundtrip-demo"
BOARD = DEMO / "roundtrip-demo.brd"


class PublicFixtureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
