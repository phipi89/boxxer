"""Partial hinges and positional repair have independent tolerances."""
import io
import tempfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import parse_path

from boxxer.panelgraph import DEFAULT_NEAR, SVG, PanelGraph, polygon_for
from boxxer.prepare import prepare
from tests.test_fingercuts import enclosure


def partial_box(connected=True, gap=.056):
    root = ET.fromstring(enclosure(connected=connected))
    for identifier in ("lid-west", "lid-east"):
        wall = root.find(f".//{{{SVG}}}rect[@id='{identifier}']")
        wall.set("height", str(float(wall.get("height"))-gap))
    return ET.tostring(root)


class PartialAttachmentTests(unittest.TestCase):
    def test_default_and_stored_repair_settings(self):
        source = PanelGraph("generated.svg", _source_bytes=partial_box())
        self.assertEqual(DEFAULT_NEAR, .1)
        self.assertEqual(source.near, .1)
        custom = PanelGraph("generated.svg", near=.025, _source_bytes=partial_box())
        output = io.BytesIO()
        custom.compile(output, thickness=0)
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        self.assertEqual(restored.near, .025)
        override = PanelGraph("compiled.svg", near=.1, _source_bytes=output.getvalue())
        self.assertEqual(override.near, .1)

    def test_connected_and_detached_shells_accept_partial_wall_hinges(self):
        for connected in (True, False):
            for gap in (.02, .056, 2):
                with self.subTest(connected=connected, gap=gap):
                    source = PanelGraph("generated.svg", _source_bytes=partial_box(connected, gap))
                    self.assertEqual(source.structure["status"], "inferred")
                    model = source._resize_structure()
                    self.assertEqual(model["status"], "supported")
                    seam = next(s for s in source.seams if {s.panel_a, s.panel_b} == {"lid-principal", "lid-west"})
                    self.assertAlmostEqual(seam.length, 80-gap)
                    interval = seam.interval_a if seam.panel_a == "lid-principal" else seam.interval_b
                    self.assertAlmostEqual(abs(interval[1]-interval[0]), (80-gap)/80)
                    same_size = source.resized(width=100, length=80, base_height=20, lid_height=20)
                    same_size.compile(io.BytesIO(), thickness=0)
                    changed = source.resized(width=90, length=100, base_height=25, lid_height=25)
                    changed.compile(io.BytesIO(), thickness=.4)
                    left = next(p for p in changed.panels if p.id == "lid-west")
                    face = next(p for p in changed.panels if p.id == "lid-principal")
                    self.assertAlmostEqual(face.polygon.bounds[3]-left.polygon.bounds[3], gap)

    def test_short_exact_and_near_hinges_and_mid_edge_flaps(self):
        for length, offset in ((.05, 0), (.05, .05), (6, 0)):
            root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
            ET.SubElement(root, f"{{{SVG}}}rect", id="parent", x="0", y="0", width="10", height="10")
            ET.SubElement(root, f"{{{SVG}}}rect", id="flap", x=str(10+offset), y="2", width="5", height=str(length))
            source = PanelGraph("generated.svg", snap_near=True, _source_bytes=ET.tostring(root))
            self.assertEqual(len(source.seams), 1)
            seam = source.seams[0]
            self.assertAlmostEqual(seam.length, length)
            self.assertAlmostEqual(seam.interval_a[0], .2)
            self.assertAlmostEqual(seam.interval_a[1], (2+length)/10)
            output = io.BytesIO()
            source.compile(output, thickness=0)
            cut = ET.fromstring(output.getvalue()).find(f".//{{{SVG}}}path[@id='cut-0']")
            self.assertTrue(polygon_for(parse_path(cut.get("d"))).is_valid)

    def test_repair_limit_does_not_connect_larger_gaps(self):
        for offset, expected in ((.08, 1), (.12, 0)):
            root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
            ET.SubElement(root, f"{{{SVG}}}rect", id="one", x="0", y="0", width="10", height="10")
            ET.SubElement(root, f"{{{SVG}}}rect", id="two", x=str(10+offset), y="2", width="5", height="6")
            graph = PanelGraph("generated.svg", snap_near=True, _source_bytes=ET.tostring(root))
            self.assertEqual(len(graph.seams), expected)

    def test_larger_repair_range_does_not_hide_real_area_overlap(self):
        root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
        for identifier in ("one", "two"):
            ET.SubElement(root, f"{{{SVG}}}rect", id=identifier, x="10", y="10", width=".04", height=".2")
        graph = PanelGraph("generated.svg", _source_bytes=ET.tostring(root))
        self.assertEqual(len(graph.overlaps), 1)
        with self.assertRaisesRegex(ValueError, "overlap"):
            graph.compile(io.BytesIO(), thickness=0)

    def test_prepare_uses_shared_default_without_extending_relief(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"source.svg"
            source.write_bytes(partial_box(gap=.056))
            data, report = prepare(source)
            self.assertEqual(report["preset"]["near_distance"], .1)
            ready = PanelGraph("prepared.svg", _source_bytes=data)
            wall = next(p for p in ready.panels if p.id == "lid-west")
            face = next(p for p in ready.panels if p.id == "lid-principal")
            self.assertAlmostEqual(face.polygon.bounds[3]-wall.polygon.bounds[3], .056)


if __name__ == "__main__":
    unittest.main()
