import io
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import Arc, parse_path

from boxxer.panelgraph import BOXXER, SVG, PanelGraph


def graph_from_shapes(shapes):
    root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
    for tag, attributes in shapes:
        ET.SubElement(root, f"{{{SVG}}}{tag}", **attributes)
    return PanelGraph("generated.svg", _source_bytes=ET.tostring(root))


def tray_shapes(height=10):
    return [("rect", {"id": name, "x": str(x), "y": str(y), "width": str(w), "height": str(h)})
            for name, x, y, w, h in [
                ("face", 50, 50, 40, 30), ("north", 50, 50-height, 40, height),
                ("east", 90, 50, height, 30), ("south", 50, 80, 40, height),
                ("west", 50-height, 50, height, 30)]]


class FeatureTests(unittest.TestCase):
    def test_slot_keeps_width_caps_and_end_insets(self):
        for rounded in (False, True):
            with self.subTest(rounded=rounded):
                attributes = {"id": "slot", "x": "60", "y": "42", "width": "1", "height": "6"}
                if rounded:
                    attributes["rx"] = "0.5"
                graph = graph_from_shapes(tray_shapes()+[("rect", attributes)])
                resized = graph.resized(width=60, height=20)
                wall = next(panel for panel in resized.panels if panel.id == "north")
                slot = wall.internal[0]
                x0, x1, y0, y1 = slot.path.bbox()
                self.assertAlmostEqual(x1-x0, 1)
                self.assertAlmostEqual(y1-y0, 16)
                self.assertAlmostEqual(y0-wall.path.bbox()[2], 2)
                self.assertAlmostEqual(wall.path.bbox()[3]-y1, 2)
                self.assertAlmostEqual((x0+x1)/2, 55.75)
                if rounded:
                    self.assertTrue(all(abs(segment.radius.real-0.5) < 1e-8
                                        for segment in slot.path if isinstance(segment, Arc)))
                resized.compile(io.BytesIO(), thickness=0.5)
                restored = resized.resized(width=40, height=10)
                original_slot = next(shape for panel in graph.panels for shape in panel.internal)
                restored_slot = next(shape for panel in restored.panels for shape in panel.internal)
                for before, after in zip(original_slot.path.bbox(), restored_slot.path.bbox()):
                    self.assertAlmostEqual(before, after)
                with self.assertRaisesRegex(ValueError, "slot is too short"):
                    graph.resized(height=4)

    def test_slot_anchors_follow_rotated_and_reversed_panel_paths(self):
        shapes = tray_shapes()+[("rect", {"id": "slot", "x": "60", "y": "42",
                                         "width": "1", "height": "6", "rx": "0.5"})]
        source = graph_from_shapes(shapes)
        for angle in (90, 180, 270):
            with self.subTest(angle=angle):
                root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
                for panel in source.panels:
                    for name, path in [(panel.id, panel.path)]+[(shape.id, shape.path) for shape in panel.internal]:
                        ET.SubElement(root, f"{{{SVG}}}path", id=name, d=path.reversed().d(),
                                      transform=f"rotate({angle})")
                graph = PanelGraph("rotated.svg", _source_bytes=ET.tostring(root))
                resized = graph.resized(height=20)
                slot = next(shape for panel in resized.panels for shape in panel.internal)
                x0, x1, y0, y1 = slot.path.bbox()
                self.assertAlmostEqual(min(x1-x0, y1-y0), 1)
                self.assertAlmostEqual(max(x1-x0, y1-y0), 16)

    def test_fixed_override_survives_source_and_compiled_round_trip(self):
        graph = graph_from_shapes(tray_shapes()+[("rect", {
            "id": "decoration", "x": "60", "y": "42", "width": "1", "height": "6",
            f"{{{BOXXER}}}feature": "fixed"})])
        resized = graph.resized(height=20)
        output = io.BytesIO()
        resized.compile(output)
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue()).resized(height=15)
        shape = next(shape for panel in restored.panels for shape in panel.internal)
        self.assertEqual(shape.feature, "fixed")
        x0, x1, y0, y1 = shape.path.bbox()
        self.assertAlmostEqual(x1-x0, 1)
        self.assertAlmostEqual(y1-y0, 6)

    def test_finger_cutout_retains_radius_and_relative_edge_position(self):
        for center in (60, 70):
            with self.subTest(center=center):
                shapes = [shape for shape in tray_shapes() if shape[1]["id"] != "north"]
                shapes.append(("path", {"id": "north", "d":
                    f"M50 40 H{center-2} A2 2 0 0 0 {center+2} 40 H90 V50 H50 Z"}))
                resized = graph_from_shapes(shapes).resized(width=60, height=20)
                north = next(panel for panel in resized.panels if panel.id == "north")
                arc = next(segment for segment in north.path if isinstance(segment, Arc))
                self.assertAlmostEqual(arc.radius.real, 2)
                self.assertAlmostEqual(arc.radius.imag, 2)
                self.assertAlmostEqual((arc.start.real+arc.end.real)/2, 40+(center-50)*1.5)
                resized.compile(io.BytesIO())

    def test_straight_notch_is_a_rigid_edge_feature(self):
        shapes = [shape for shape in tray_shapes() if shape[1]["id"] != "east"]
        shapes.append(("path", {"id": "east", "d":
            "M90 50 H100 V60 H98 V62 H100 V80 H90 Z"}))
        resized = graph_from_shapes(shapes).resized(length=60)
        east = next(panel for panel in resized.panels if panel.id == "east")
        notch_floor = next(segment for segment in east.path if
                           abs(segment.start.real-98) < 1e-8 and abs(segment.end.real-98) < 1e-8)
        self.assertAlmostEqual(notch_floor.length(), 2)
        self.assertAlmostEqual((notch_floor.start.imag+notch_floor.end.imag)/2, 72)
        resized.compile(io.BytesIO())

    def test_lid_fit_preserves_hole_size_and_anchors_slot_ends(self):
        source = Path(__file__).resolve().parents[1] / "presets" / "separate-lid-box.svg"
        tree = ET.fromstring(PanelGraph(source).source_bytes)
        ET.SubElement(tree, f"{{{SVG}}}circle", id="hole", cx="100", cy="90", r="2")
        ET.SubElement(tree, f"{{{SVG}}}rect", id="slot", x="60", y="70", width="1", height="40", rx="0.5")
        graph = PanelGraph("lid.svg", _source_bytes=ET.tostring(tree))
        output = io.BytesIO()
        report = graph.compile(output, thickness=0.5)
        root = ET.fromstring(output.getvalue())
        hole = parse_path(root.find(f".//{{{SVG}}}path[@id='internal-hole']").get("d"))
        slot = parse_path(root.find(f".//{{{SVG}}}path[@id='internal-slot']").get("d"))
        self.assertAlmostEqual(hole.bbox()[1]-hole.bbox()[0], 4)
        self.assertAlmostEqual(hole.bbox()[3]-hole.bbox()[2], 4)
        self.assertAlmostEqual(slot.bbox()[1]-slot.bbox()[0], 1)
        self.assertAlmostEqual(slot.bbox()[3]-slot.bbox()[2], 42)
        recompiled = io.BytesIO()
        PanelGraph("compiled.svg", _source_bytes=output.getvalue()).compile(recompiled)
        self.assertEqual(output.getvalue(), recompiled.getvalue())
        self.assertEqual(report["cut_contours"], 2)


if __name__ == "__main__":
    unittest.main()
