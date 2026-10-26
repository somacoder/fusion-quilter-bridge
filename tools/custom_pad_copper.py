"""Preserve EAGLE polygon-pad copper as electrically connected KiCad custom pads."""
import pcbnew
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def copper_polygons(fp):
    return [s for s in fp.GraphicalItems()
            if isinstance(s, pcbnew.PCB_SHAPE) and s.GetShape() == pcbnew.SHAPE_T_POLY
            and s.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu) and s.IsFilled()]


def shape_polygon(shape):
    polygon = pcbnew.SHAPE_POLY_SET()
    shape.TransformShapeToPolygon(polygon, shape.GetLayer(), 0, 1000, pcbnew.ERROR_INSIDE)
    return polygon


def owners(fp, shape):
    polygon = shape_polygon(shape)
    return [p for p in fp.Pads() if p.IsOnLayer(shape.GetLayer())
            and polygon.Collide(p.GetEffectivePolygon(shape.GetLayer()))]


def merge_pad_copper(fp):
    """Only merge a filled polygon when it contains exactly one simple SMD anchor."""
    count = 0
    for shape in copper_polygons(fp):
        matches = owners(fp, shape)
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError("Ambiguous copper polygon in %s touches pads %s" %
                             (fp.GetReference(), [p.GetNumber() for p in matches]))
        pad = matches[0]
        layer = shape.GetLayer()
        if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD or pad.GetShape(layer) not in (
                pcbnew.PAD_SHAPE_RECTANGLE, pcbnew.PAD_SHAPE_CIRCLE):
            raise ValueError("Unsupported polygon pad anchor %s.%s" %
                             (fp.GetReference(), pad.GetNumber()))
        polygon = shape_polygon(shape)
        remainder = pcbnew.SHAPE_POLY_SET(pad.GetEffectivePolygon(layer))
        remainder.BooleanSubtract(polygon)
        if remainder.Area() > 1:
            raise ValueError("Copper polygon does not contain pad anchor %s.%s" %
                             (fp.GetReference(), pad.GetNumber()))
        polygon.Move(-pad.GetPosition())
        polygon.Rotate(pcbnew.EDA_ANGLE(-pad.GetOrientationDegrees(), pcbnew.DEGREES_T),
                       pcbnew.VECTOR2I(0, 0))
        anchor = pad.GetShape(layer)
        pad.SetShape(layer, pcbnew.PAD_SHAPE_CUSTOM)
        pad.SetAnchorPadShape(layer, anchor)
        pad.AddPrimitivePoly(layer, polygon, 0, True)
        fp.Remove(shape)
        count += 1
    return count


def verified_custom_pads(reference_path, actual_path):
    """Compare custom-pad copper with the original EAGLE pad + graphic unions."""
    from eagle_brd_to_kicad import FootprintSource, P
    from eagle_common import parse_rot

    root = ET.parse(reference_path).getroot()
    refs = {}
    reference = pcbnew.CreateEmptyBoard()
    with tempfile.TemporaryDirectory() as td:
        source = FootprintSource(root, Path(td))
        for element in root.findall("drawing/board/elements/element"):
            fp, _ = source.load(element.get("library"), element.get("library_urn"), element.get("package"))
            reference.Add(fp)
            fp.SetPosition(P(float(element.get("x")), float(element.get("y"))))
            mirror, _, degrees = parse_rot(element.get("rot"))
            fp.SetOrientation(pcbnew.EDA_ANGLE(degrees + (180 if mirror else 0), pcbnew.DEGREES_T))
            if mirror:
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
            refs[element.get("name")] = fp
    actual = pcbnew.LoadBoard(str(actual_path))
    if actual is None:
        raise ValueError("Cannot read converted board for custom-pad geometry verification")
    from eagle_common import kicad_ref_map
    refs = {kicad_ref_map(refs)[name]: fp for name, fp in refs.items()}
    required = set()
    for name, fp in refs.items():
        fp.SetReference(name)
        for graphic in copper_polygons(fp):
            matches = owners(fp, graphic)
            if len(matches) > 1:
                raise ValueError("Ambiguous reference copper polygon in %s" % name)
            if matches:
                required.add((name, matches[0].GetNumber()))
    verified = set()
    for fp in actual.GetFootprints():
        for pad in fp.Pads():
            layers = [layer for layer in (pcbnew.F_Cu, pcbnew.B_Cu)
                      if pad.IsOnLayer(layer) and pad.GetShape(layer) == pcbnew.PAD_SHAPE_CUSTOM]
            if not layers:
                continue
            ref = refs[fp.GetReference()]
            candidates = [p for p in ref.Pads() if p.GetNumber() == pad.GetNumber()
                          and (p.GetPosition() - pad.GetPosition()).EuclideanNorm() <= 1000]
            if len(candidates) != 1:
                raise ValueError("Cannot identify reference custom pad %s.%s" %
                                 (fp.GetReference(), pad.GetNumber()))
            original = candidates[0]
            for layer in layers:
                expected = pcbnew.SHAPE_POLY_SET(original.GetEffectivePolygon(layer))
                graphics = [s for s in copper_polygons(ref) if s.GetLayer() == layer
                            and any(p == original for p in owners(ref, s))]
                if not graphics:
                    raise ValueError("Custom pad has no source copper polygon: %s.%s" %
                                     (fp.GetReference(), pad.GetNumber()))
                for graphic in graphics:
                    if len(owners(ref, graphic)) != 1:
                        raise ValueError("Ambiguous reference copper polygon")
                    expected.BooleanAdd(shape_polygon(graphic))
                got = pcbnew.SHAPE_POLY_SET(pad.GetEffectivePolygon(layer))
                missing = pcbnew.SHAPE_POLY_SET(expected)
                missing.BooleanSubtract(got)
                extra = pcbnew.SHAPE_POLY_SET(got)
                extra.BooleanSubtract(expected)
                # Polygon arithmetic rounds integer-nm coordinates at intersections.
                if missing.Area() + extra.Area() > 10000:
                    raise ValueError("Custom-pad copper changed: %s.%s (difference %.0f nm^2)" %
                                     (fp.GetReference(), pad.GetNumber(), missing.Area() + extra.Area()))
            verified.add((fp.GetReference(), pad.GetNumber()))
    if verified != required:
        raise ValueError("Custom-pad membership mismatch: missing %s, unexpected %s" %
                         (sorted(required - verified), sorted(verified - required)))
    return verified
