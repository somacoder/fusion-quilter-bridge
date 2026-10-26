"""Synthetic custom-pad tests; run with KiCad 9 Python, skipped without pcbnew."""
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
try:
    import pcbnew
except ImportError:
    pcbnew = None

if pcbnew is not None:
    from custom_pad_copper import merge_pad_copper, shape_polygon


@unittest.skipIf(pcbnew is None, "KiCad pcbnew required")
class CustomPadTests(unittest.TestCase):
    def test_outer_ground_layer_name_is_preserved(self):
        from eagle_brd_to_kicad import BoardBuilder
        root = Path(__file__).resolve().parents[1]
        tree = ET.parse(root / "examples" / "roundtrip-demo" / "roundtrip-demo.brd")
        tree.getroot().find("drawing/layers/layer[@number='16']").set("name", "GND")
        with tempfile.TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "ground.brd"
            tree.write(board_path)
            builder = BoardBuilder(board_path, None, "ground", Path(tmp))
            self.assertEqual(builder.board.GetLayerName(pcbnew.B_Cu), "GND")
            self.assertEqual(builder.copper[16], pcbnew.B_Cu)

    def footprint(self):
        board = pcbnew.CreateEmptyBoard()
        fp = pcbnew.FOOTPRINT(board)
        board.Add(fp)
        fp.SetReference("U1")
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
        pad.SetSize(pcbnew.VECTOR2I(500000, 500000))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        fp.Add(pad)
        polygon = pcbnew.SHAPE_POLY_SET()
        polygon.NewOutline()
        for x, y in ((-1000000, -700000), (1300000, -700000),
                     (1000000, 800000), (-1000000, 800000)):
            polygon.Append(x, y)
        shape = pcbnew.PCB_SHAPE(fp)
        shape.SetShape(pcbnew.SHAPE_T_POLY)
        shape.SetLayer(pcbnew.F_Cu)
        shape.SetPolyShape(polygon)
        shape.SetFilled(True)
        shape.SetWidth(10000)
        fp.Add(shape)
        return board, fp, pad, shape

    def test_copper_union_preserved_after_move_rotation_and_flip(self):
        for rotation, flip in ((0, False), (30, False), (90, True), (270, True)):
            with self.subTest(rotation=rotation, flip=flip):
                board, fp, pad, shape = self.footprint()
                expected = shape_polygon(shape)
                expected.BooleanAdd(pcbnew.SHAPE_POLY_SET(pad.GetEffectivePolygon(pcbnew.F_Cu)))
                self.assertEqual(merge_pad_copper(fp), 1)
                got = pcbnew.SHAPE_POLY_SET(pad.GetEffectivePolygon(pcbnew.F_Cu))
                missing = pcbnew.SHAPE_POLY_SET(expected)
                missing.BooleanSubtract(got)
                extra = pcbnew.SHAPE_POLY_SET(got)
                extra.BooleanSubtract(expected)
                self.assertLessEqual(missing.Area() + extra.Area(), 10000)
                area = got.Area()
                fp.SetPosition(pcbnew.VECTOR2I(12000000, 7000000))
                fp.SetOrientation(pcbnew.EDA_ANGLE(rotation, pcbnew.DEGREES_T))
                if flip:
                    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
                layer = pcbnew.B_Cu if flip else pcbnew.F_Cu
                self.assertLess(abs(pad.GetEffectivePolygon(layer).Area() - area), 10000000)
                self.assertEqual(merge_pad_copper(fp), 0)

    def test_ambiguous_polygon_rejected(self):
        board, fp, pad, shape = self.footprint()
        second = pcbnew.PAD(pad)
        second.SetNumber("2")
        fp.Add(second)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            merge_pad_copper(fp)

    def test_unattached_copper_is_not_assigned_to_a_pad(self):
        board, fp, pad, shape = self.footprint()
        shape.Move(pcbnew.VECTOR2I(5000000, 0))
        self.assertEqual(merge_pad_copper(fp), 0)
        self.assertEqual(pad.GetShape(pcbnew.F_Cu), pcbnew.PAD_SHAPE_RECTANGLE)

    def test_partially_contained_anchor_rejected(self):
        board, fp, pad, shape = self.footprint()
        pad.SetSize(pcbnew.VECTOR2I(3000000, 3000000))
        with self.assertRaisesRegex(ValueError, "does not contain"):
            merge_pad_copper(fp)


if __name__ == "__main__":
    unittest.main()
