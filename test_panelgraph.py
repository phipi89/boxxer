import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import Arc, CubicBezier, QuadraticBezier, parse_path

from panelgraph import BOXXER, SVG, PanelGraph

ROOT = Path(__file__).parent


def svg(shapes, directory, name="source.svg"):
    path = Path(directory) / name
    root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
    for tag, attributes in shapes:
        ET.SubElement(root, f"{{{SVG}}}{tag}", **attributes)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


class PanelGraphTests(unittest.TestCase):
    def test_rotational_source_is_a_generic_panel_graph(self):
        raw = PanelGraph(ROOT / "rotational.svg")
        self.assertEqual(len(raw.panels), 9)
        self.assertEqual(len(raw.seams), 3)
        self.assertEqual(len(raw.near_seams), 5)
        self.assertGreater(len(raw.components), 1)
        graph = PanelGraph(ROOT / "rotational.svg", snap_near=True)
        self.assertEqual(len(graph.panels), 9)
        self.assertEqual(len(graph.seams), 8)
        self.assertEqual(len(graph.components), 1)
        self.assertEqual(sum(len(p.internal) for p in graph.panels), 0)
        self.assertEqual(sorted(round(s.length, 2) for s in graph.seams), [17, 17, 17, 17, 100, 100, 100, 100])
        # The rotated source rectangles need no special layout rule.
        self.assertIn({"rect1", "rect2-3-6"}, [{s.panel_a, s.panel_b} for s in graph.seams])

    def test_second_preserves_slots_curves_and_outside_hints(self):
        graph = PanelGraph(ROOT / "second.svg", snap_near=True)
        self.assertEqual(len(graph.panels), 9)
        self.assertEqual(len(graph.seams), 8)
        self.assertEqual(len(graph.components), 1)
        self.assertEqual(sum(len(p.internal) for p in graph.panels), 4)
        outside = {p.id for p in graph.panels if p.outside_hint}
        self.assertEqual(outside, {"rect4-20", "rect4-20-6", "path6", "path7"})
        slots = {shape.id: panel.id for panel in graph.panels for shape in panel.internal}
        self.assertEqual(slots, {"rect5": "rect3", "rect5-5": "rect3",
                                 "rect5-5-3": "rect2-3-6", "rect5-5-2": "rect2-3-6"})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "compiled.svg"
            report = graph.compile(target, thickness=0.5)
            self.assertEqual(report["cut_contours"], 1)
            self.assertEqual(report["internal_cuts"], 4)
            root = ET.parse(target).getroot()
            paths = root.findall(f".//{{{SVG}}}path")
            internal = [p for p in paths if p.get("id", "").startswith("internal-")]
            self.assertEqual(len(internal), 4)
            self.assertTrue(all(any(isinstance(s, Arc) for s in parse_path(p.get("d"))) for p in internal))
            cut = next(p for p in paths if p.get("id") == "cut-0")
            self.assertTrue(any(isinstance(s, CubicBezier) for s in parse_path(cut.get("d"))))

    def test_near_seams_warn_then_require_explicit_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            source = svg([
                ("rect", {"id": "a", "x": "0", "y": "0", "width": "10", "height": "10"}),
                ("rect", {"id": "b", "x": "10.001", "y": "2", "width": "5", "height": "6"}),
            ], directory)
            graph = PanelGraph(source)
            self.assertEqual(len(graph.seams), 0)
            self.assertEqual(len(graph.near_seams), 1)
            self.assertAlmostEqual(graph.near_seams[0]["distance"], 0.001)
            with self.assertRaisesRegex(ValueError, "--snap-near"):
                graph.compile(Path(directory) / "refused.svg")
            repaired = PanelGraph(source, snap_near=True)
            self.assertEqual(len(repaired.seams), 1)
            self.assertTrue(repaired.seams[0].repaired)
            target = Path(directory) / "out.svg"
            result = repaired.compile(target)
            self.assertEqual(result["cut_contours"], 1)
            model = json.loads(ET.parse(target).findtext(f".//{{{BOXXER}}}model"))
            self.assertEqual(len(model["repairs"]), 1)

    def test_partial_oblique_and_curved_panels_need_no_box_template(self):
        with tempfile.TemporaryDirectory() as directory:
            oblique = svg([
                ("path", {"id": "triangle-a", "d": "M 0,0 L 10,0 L 0,10 Z"}),
                ("path", {"id": "triangle-b", "d": "M 10,0 L 10,10 L 0,10 Z"}),
            ], directory, "oblique.svg")
            graph = PanelGraph(oblique)
            self.assertEqual(len(graph.seams), 1)
            self.assertAlmostEqual(graph.seams[0].length, 10*2**0.5)
            result = graph.compile(Path(directory) / "oblique-out.svg")
            self.assertEqual(result["cut_contours"], 1)

            curved = svg([
                ("rect", {"id": "body", "x": "0", "y": "0", "width": "10", "height": "10"}),
                ("path", {"id": "rounded-wing", "d": "M 10,2 L 15,2 A 3,3 0 0 1 15,8 L 10,8 Z"}),
            ], directory, "curved.svg")
            graph = PanelGraph(curved)
            self.assertEqual(len(graph.seams), 1)
            self.assertAlmostEqual(graph.seams[0].length, 6)
            target = Path(directory) / "curved-out.svg"
            graph.compile(target)
            cut = parse_path(ET.parse(target).find(f".//{{{SVG}}}path[@id='cut-0']").get("d"))
            self.assertTrue(any(isinstance(segment, Arc) for segment in cut))

            curved_fold = svg([
                ("path", {"id": "below", "d": "M 0,0 L 10,0 Q 5,5 0,0 Z"}),
                ("path", {"id": "above", "d": "M 0,0 Q 5,5 10,0 L 10,10 L 0,10 Z"}),
            ], directory, "curved-fold.svg")
            graph = PanelGraph(curved_fold)
            self.assertEqual(len(graph.seams), 1)
            target = Path(directory) / "curved-fold-out.svg"
            graph.compile(target)
            score = parse_path(ET.parse(target).find(f".//{{{SVG}}}path[@id='fold-0']").get("d"))
            self.assertIsInstance(score[0], QuadraticBezier)

    def test_internal_geometry_belongs_to_smallest_strict_container(self):
        with tempfile.TemporaryDirectory() as directory:
            source = svg([
                ("rect", {"id": "panel", "x": "0", "y": "0", "width": "20", "height": "20"}),
                ("rect", {"id": "slot", "x": "4", "y": "4", "width": "12", "height": "12"}),
                ("circle", {"id": "detail", "cx": "10", "cy": "10", "r": "2"}),
            ], directory)
            graph = PanelGraph(source)
            self.assertEqual([p.id for p in graph.panels], ["panel"])
            # Both nested shapes ultimately remain internal to the material
            # panel; nested drawing geometry is not promoted to a new component.
            self.assertEqual({s.id for s in graph.panels[0].internal}, {"slot", "detail"})

    def test_shared_point_is_not_a_fold_and_components_remain_neutral(self):
        with tempfile.TemporaryDirectory() as directory:
            source = svg([
                ("rect", {"id": "one", "x": "0", "y": "0", "width": "10", "height": "10"}),
                ("rect", {"id": "two", "x": "10", "y": "10", "width": "5", "height": "5"}),
                ("rect", {"id": "three", "x": "30", "y": "30", "width": "5", "height": "5"}),
            ], directory)
            graph = PanelGraph(source)
            self.assertEqual(graph.seams, [])
            self.assertEqual(len(graph.components), 3)

    def test_same_side_and_area_overlaps_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = svg([
                ("rect", {"id": "one", "x": "0", "y": "0", "width": "10", "height": "10"}),
                ("rect", {"id": "two", "x": "5", "y": "0", "width": "10", "height": "10"}),
            ], directory)
            graph = PanelGraph(source, snap_near=True)
            self.assertEqual(graph.seams, [])
            self.assertEqual(len(graph.overlaps), 1)
            with self.assertRaisesRegex(ValueError, "interiors overlap"):
                graph.compile(Path(directory) / "out.svg")

    def test_metadata_and_manufacturing_output(self):
        graph = PanelGraph(ROOT / "rotational.svg", snap_near=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.svg"
            graph.compile(target, thickness=0.7, fold_offset=0.1)
            root = ET.parse(target).getroot()
            model = json.loads(root.findtext(f".//{{{BOXXER}}}model"))
            self.assertEqual(model["schema"], 1)
            self.assertEqual(model["thickness"], 0.7)
            self.assertEqual(model["fold_offset_from_midplane"], 0.1)
            self.assertEqual(len(model["panels"]), 9)
            self.assertEqual(len(model["folds"]), 8)
            self.assertEqual(len(model["components"]), 1)
            cuts = root.findall(f".//{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path")
            scores = root.findall(f".//{{{SVG}}}g[@id='boxxer-scores']/{{{SVG}}}path")
            self.assertEqual(len(cuts), 1)
            self.assertEqual(len(scores), 8)
            self.assertTrue(all(element.tag == f"{{{SVG}}}path" for element in cuts+scores))

    def test_cli_safety_json_and_validation(self):
        command = [sys.executable, str(ROOT / "boxxer.py")]
        inspected = subprocess.run(command+["inspect", str(ROOT / "second.svg"), "--snap-near", "--json"], capture_output=True, text=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(len(json.loads(inspected.stdout)["internal_geometry"]), 4)
        warning = subprocess.run(command+["inspect", str(ROOT / "second.svg")], capture_output=True, text=True)
        self.assertEqual(warning.returncode, 1)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.svg"
            refused = subprocess.run(command+["compile", str(ROOT / "second.svg"), "-o", str(target)], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            compiled = subprocess.run(command+["compile", str(ROOT / "second.svg"), "--snap-near", "-o", str(target)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue(target.exists())
        same = subprocess.run(command+["compile", str(ROOT / "second.svg"), "--snap-near", "-o", str(ROOT / "second.svg")], capture_output=True, text=True)
        self.assertEqual(same.returncode, 2)
        for invalid in ("0", "-1", "nan", "inf"):
            result = subprocess.run(command+["inspect", str(ROOT / "second.svg"), "--near-distance", invalid], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
