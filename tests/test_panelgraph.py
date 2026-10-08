import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point
from svgpathtools import Arc, CubicBezier, Line, QuadraticBezier, parse_path

from boxxer import cli as boxxer
from boxxer.panelgraph import BOXXER, SVG, PanelGraph, polygon_for, sample_segment

ROOT = Path(__file__).resolve().parents[1]
SINGLE = ROOT / "drafts" / "single-shell.svg"


def svg(shapes, directory, name="source.svg"):
    path = Path(directory) / name
    root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
    for tag, attributes in shapes:
        ET.SubElement(root, f"{{{SVG}}}{tag}", **attributes)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


class PanelGraphTests(unittest.TestCase):
    def test_rotational_source_is_a_generic_panel_graph(self):
        raw = PanelGraph(SINGLE)
        self.assertEqual(len(raw.panels), 9)
        self.assertEqual(len(raw.seams), 3)
        self.assertEqual(len(raw.near_seams), 5)
        self.assertGreater(len(raw.components), 1)
        graph = PanelGraph(SINGLE, snap_near=True)
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
        graph = PanelGraph(SINGLE, snap_near=True)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.svg"
            report = graph.compile(target, thickness=0.7, fold_offset=0.1)
            root = ET.parse(target).getroot()
            model = json.loads(root.findtext(f".//{{{BOXXER}}}model"))
            self.assertEqual(model["schema"], 4)
            self.assertEqual(model["thickness"], 0.7)
            self.assertEqual(model["fold_offset"], 0.1)
            self.assertEqual(model["output_translation"], report["output_translation"])
            embedded = root.find(f".//{{{BOXXER}}}source")
            self.assertEqual(embedded.get("encoding"), "base64")
            self.assertTrue(embedded.get("sha256"))
            self.assertEqual(len(model["panels"]), 9)
            self.assertEqual(len(model["folds"]), 8)
            self.assertEqual(len(model["components"]), 1)
            cuts = root.findall(f".//{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path")
            scores = root.findall(f".//{{{SVG}}}g[@id='boxxer-scores']/{{{SVG}}}path")
            self.assertEqual([path.get("id") for path in cuts],
                             ["cut-0"]+[f"relief-{i}" for i in range(8)]+
                             [f"corner-relief-{i}" for i in range(4)])
            self.assertTrue(polygon_for(parse_path(cuts[0].get("d"))).is_valid)
            self.assertEqual(len(scores), 8)
            self.assertTrue(all(element.tag == f"{{{SVG}}}path" for element in cuts+scores))
            self.assertEqual(len(model["contacts"]), 4)
            self.assertEqual(len(model["compensation"]), 4)
            for row in model["compensation"]:
                self.assertAlmostEqual(row["distance"], 0.8)
                self.assertAlmostEqual(row["achieved_midplane_separation"], 0.8)
                self.assertAlmostEqual(row["residual"], 0, delta=1e-9)

    def test_inside_compensation_moves_scores_and_adds_reliefs(self):
        graph = PanelGraph(SINGLE, snap_near=True)
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "plain.svg"
            thick = Path(directory) / "thick.svg"
            graph.compile(plain, thickness=0)
            report = graph.compile(thick, thickness=0.5)
            self.assertEqual(len(report["compensation"]), 4)
            self.assertTrue(all(row["placement"] == "inside" for row in report["compensation"]))
            self.assertTrue(all(abs(row["residual"]) < 1e-9 for row in report["compensation"]))
            plain_root, thick_root = ET.parse(plain).getroot(), ET.parse(thick).getroot()
            for row in report["compensation"]:
                before = parse_path(plain_root.find(f".//{{{SVG}}}path[@id='{row['fold']}']").get("d"))[0]
                after = parse_path(thick_root.find(f".//{{{SVG}}}path[@id='{row['fold']}']").get("d"))[0]
                self.assertAlmostEqual(abs(after.start-before.start), row["geometric_shift_distance"])
                self.assertAlmostEqual(abs(after.end-before.end), row["geometric_shift_distance"])
            plain_cut = parse_path(plain_root.find(f".//{{{SVG}}}path[@id='cut-0']").get("d"))
            thick_cut = parse_path(thick_root.find(f".//{{{SVG}}}path[@id='cut-0']").get("d"))
            self.assertAlmostEqual(thick_cut.bbox()[0], 10)
            self.assertAlmostEqual(thick_cut.bbox()[2], 10)
            removed = polygon_for(plain_cut).area-polygon_for(thick_cut).area
            expected = sum(next(s.length for s in graph.seams if s.id == row["fold"])*row["geometric_shift_distance"]
                           for row in report["compensation"])
            # Four 3 mm wall-end remnants extend outward by half the thickness.
            expected -= 4*3*0.25
            self.assertAlmostEqual(removed, expected, delta=0.002)
            reliefs = [path for path in thick_root.findall(f".//{{{SVG}}}path")
                       if path.get("id", "").startswith("relief-")]
            self.assertEqual(report["tab_relief_cuts"], 8)
            self.assertEqual(report["corner_relief_cuts"], 4)
            self.assertEqual(report["relief_cuts"], 12)
            self.assertEqual(len(reliefs), 8)
            expected_lengths = sorted([row["geometric_shift_distance"]+abs(complex(*row["wall_edge_shift"]))
                                       for row in report["compensation"] for _ in range(2)])
            actual_lengths = sorted(parse_path(path.get("d")).length() for path in reliefs)
            for actual, expected_length in zip(actual_lengths, expected_lengths):
                self.assertAlmostEqual(actual, expected_length)
            for row in report["compensation"]:
                direction = complex(*row["direction"])
                wall_shift = complex(*row["wall_edge_shift"])
                self.assertAlmostEqual(abs(wall_shift), 0.25)
                self.assertAlmostEqual((wall_shift*direction.conjugate()).real, -0.25)

    def test_outside_compensation_moves_wall_end_outward_and_preserves_internal_slots(self):
        graph = PanelGraph(ROOT / "second.svg", snap_near=True)
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "plain.svg"
            thick = Path(directory) / "thick.svg"
            graph.compile(plain, thickness=0)
            report = graph.compile(thick, thickness=0.5, fold_offset=0.1)
            self.assertEqual(len(report["compensation"]), 4)
            self.assertEqual(report["tab_relief_cuts"], 0)
            self.assertEqual(report["corner_relief_cuts"], 4)
            self.assertTrue(all(row["placement"] == "outside" for row in report["compensation"]))
            self.assertTrue(all(abs(row["distance"]-0.6) < 1e-12 for row in report["compensation"]))
            self.assertTrue(all(abs(row["residual"]) < 1e-9 for row in report["compensation"]))
            for row in report["compensation"]:
                direction = complex(*row["direction"])
                wall_shift = complex(*row["wall_edge_shift"])
                self.assertAlmostEqual(abs(wall_shift), 0.25)
                self.assertAlmostEqual((wall_shift*direction.conjugate()).real, 0.25)
            plain_root, thick_root = ET.parse(plain).getroot(), ET.parse(thick).getroot()
            plain_cut = polygon_for(parse_path(plain_root.find(f".//{{{SVG}}}path[@id='cut-0']").get("d")))
            thick_path = parse_path(thick_root.find(f".//{{{SVG}}}path[@id='cut-0']").get("d"))
            self.assertAlmostEqual(thick_path.bbox()[0], 10)
            self.assertAlmostEqual(thick_path.bbox()[2], 10)
            thick_cut = polygon_for(thick_path)
            self.assertGreater(thick_cut.area, plain_cut.area)
            plain_internal = {p.get("id"): p.get("d") for p in plain_root.findall(f".//{{{SVG}}}path") if p.get("id", "").startswith("internal-")}
            thick_internal = {p.get("id"): p.get("d") for p in thick_root.findall(f".//{{{SVG}}}path") if p.get("id", "").startswith("internal-")}
            self.assertEqual(plain_internal, thick_internal)
            self.assertEqual(len(thick_internal), 4)
            reduced = graph.compensation_plan(0.5, -0.2)
            self.assertTrue(all(abs(row["distance"]-0.3) < 1e-12 for row in reduced))
            with self.assertRaisesRegex(ValueError, "cannot be negative"):
                graph.compensation_plan(0.5, -0.6)

    def test_internal_geometry_moves_with_compensated_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            root = ET.parse(SINGLE).getroot()
            layer = next(e for e in root if e.tag.endswith("g"))
            ET.SubElement(layer, f"{{{SVG}}}circle", id="tab-hole", cx="160", cy="31", r="1",
                          style="fill:none;stroke:#000000")
            source = Path(directory) / "with-hole.svg"
            ET.ElementTree(root).write(source)
            graph = PanelGraph(source, snap_near=True)
            owner = next(p for p in graph.panels if any(s.id == "tab-hole" for s in p.internal))
            self.assertEqual(owner.id, "rect4")
            plain, thick = Path(directory) / "plain.svg", Path(directory) / "thick.svg"
            graph.compile(plain, thickness=0)
            report = graph.compile(thick, thickness=0.5)
            before = parse_path(ET.parse(plain).find(f".//{{{SVG}}}path[@id='internal-tab-hole']").get("d"))
            after = parse_path(ET.parse(thick).find(f".//{{{SVG}}}path[@id='internal-tab-hole']").get("d"))
            shift = next(row["shift"] for row in report["compensation"] if row["tab"] == "rect4")
            self.assertAlmostEqual(after[0].start.real-before[0].start.real, shift[0])
            self.assertAlmostEqual(after[0].start.imag-before[0].start.imag, shift[1])

    def test_compensated_principal_score_corners_are_opened_to_cut_paths(self):
        fixtures = (SINGLE, ROOT / "second.svg",
                    ROOT / "drafts" / "connected.svg")
        with tempfile.TemporaryDirectory() as directory:
            for fixture in fixtures:
                graph = PanelGraph(fixture, snap_near=True)
                principals = ({graph.structure["top"], graph.structure["bottom"]}
                              if graph.structure["status"] == "inferred" else
                              {max(graph.panels, key=lambda panel: panel.polygon.area).id})
                principal_folds = {seam.id for seam in graph.seams
                                   if seam.panel_a in principals or seam.panel_b in principals}
                for thickness in (0.5, 2.0, 5.0):
                    target = Path(directory) / f"{fixture.stem}-{thickness}.svg"
                    report = graph.compile(target, thickness=thickness)
                    root = ET.parse(target).getroot()
                    cut_nodes = root.findall(f".//{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path")
                    cut_lines = [LineString([point for segment in parse_path(node.get("d"))
                                             for point in sample_segment(segment)])
                                 for node in cut_nodes]
                    scores = {node.get("id"): parse_path(node.get("d"))
                              for node in root.findall(f".//{{{SVG}}}g[@id='boxxer-scores']/{{{SVG}}}path")}
                    expected_corners = 6 if fixture.name == "connected.svg" else 4
                    self.assertEqual(report["corner_relief_cuts"], expected_corners)
                    corners = [parse_path(node.get("d")) for node in cut_nodes
                               if node.get("id", "").startswith("corner-relief-")]
                    self.assertEqual(len(corners), expected_corners)
                    score_endpoints = [endpoint for fold_id in principal_folds
                                       for endpoint in (scores[fold_id].start, scores[fold_id].end)]
                    contour_line = cut_lines[0]
                    for corner in corners:
                        self.assertLessEqual(contour_line.distance(Point(corner.start.real, corner.start.imag)), 1e-6)
                        self.assertLessEqual(min(abs(corner.end-endpoint) for endpoint in score_endpoints), 1e-6)

    def test_connected_enclosure_infers_shells_and_flap_aware_lid_fit(self):
        graph = PanelGraph(ROOT / "drafts" / "connected.svg", snap_near=True)
        self.assertEqual(graph.structure["top"], "rect1")
        self.assertEqual(graph.structure["bottom"], "rect5")
        self.assertEqual(graph.structure["shared_connector"], ["rect6"])
        self.assertEqual(graph.structure["contact_domains"]["rect6"], ["top", "bottom"])
        self.assertEqual({contact.tab: contact.receiver for contact in graph.contacts}, {
            "rect4": "rect2-3-6", "rect4-20": "rect3",
            "rect10": "rect8", "rect10-0": "rect6",
            "rect11": "rect7", "path1": "rect6",
        })
        self.assertEqual(graph.contact_warnings, [])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "connected.svg"
            report = graph.compile(target, thickness=0.5)
            fit = {row["side"]: row for row in report["lid_fit"]["walls"]}
            self.assertEqual(fit["far"]["gap_layers"], 0)
            self.assertAlmostEqual(fit["far"]["midplane_offset"], 0.5)
            self.assertEqual(fit["left"]["gap_layers"], 1)
            self.assertEqual(fit["right"]["gap_layers"], 1)
            self.assertAlmostEqual(fit["left"]["midplane_offset"], 1.0)
            self.assertAlmostEqual(fit["right"]["midplane_offset"], 1.0)
            root = ET.parse(target).getroot()
            scores = {path.get("id"): parse_path(path.get("d"))
                      for path in root.findall(f".//{{{SVG}}}g[@id='boxxer-scores']/{{{SVG}}}path")}
            self.assertAlmostEqual(scores["fold-0"].length(), 102.0, places=4)
            self.assertAlmostEqual(scores["fold-1"].length(), 100.5, places=4)
            self.assertAlmostEqual(scores["fold-2"].length(), 100.5, places=4)
            self.assertAlmostEqual(scores["fold-3"].length(), 102.0, places=4)
            self.assertAlmostEqual(scores["fold-6"].length(), 100.0, places=4)
            reported_folds = {row["id"]: row for row in report["folds"]}
            self.assertAlmostEqual(reported_folds["fold-0"]["length"], 102.0, places=4)
            self.assertTrue(reported_folds["fold-0"]["fitted"])
            self.assertFalse(reported_folds["fold-6"]["fitted"])
            self.assertTrue(all(row["verification"] == "local-contact-before-lid-fit"
                                for row in report["compensation"]))
            self.assertTrue(all("achieved_midplane_separation" not in row
                                for row in report["compensation"]))
            compensation = {row["tab"]: row for row in report["compensation"]}
            self.assertEqual(set(compensation), {
                "rect4", "rect4-20", "rect10", "rect10-0", "rect11", "path1"})
            for tab in ("rect10-0", "path1"):
                self.assertAlmostEqual(compensation[tab]["distance"], 0.5)
                self.assertAlmostEqual(abs(complex(*compensation[tab]["wall_edge_shift"])), 0.25)
            contour = parse_path(root.find(f".//{{{SVG}}}path[@id='cut-0']").get("d"))
            self.assertAlmostEqual(contour.bbox()[0], 10)
            self.assertAlmostEqual(contour.bbox()[2], 10)
            self.assertTrue(polygon_for(contour).is_valid)
            self.assertEqual(report["tab_relief_cuts"], 12)
            self.assertEqual(report["corner_relief_cuts"], 6)
            self.assertEqual(report["relief_cuts"], 18)
            clearance = graph.lid_fit_plan(0.5, 0.1)
            clear_fit = {row["side"]: row["midplane_offset"] for row in clearance["walls"]}
            self.assertEqual(clear_fit, {"far": 0.6, "left": 1.1, "right": 1.1})
            variable = graph.compensation_plan(0.8, 0.1)
            self.assertTrue(all(abs(row["distance"]-0.9) < 1e-12 for row in variable))
            self.assertTrue(all(abs(abs(complex(*row["wall_edge_shift"]))-0.4) < 1e-12
                                for row in variable))

    def test_separate_shells_infer_lid_base_fit_and_resize(self):
        working = PanelGraph(ROOT / "rotational.svg", snap_near=True)
        self.assertEqual(working.structure["layout"], "separate")
        self.assertEqual((working.structure["top"], working.structure["bottom"]), ("rect1", "rect5"))
        self.assertEqual(len(working.contacts), 8)

        source = ROOT / "presets" / "box_glued-flaps_separate-lid.svg"
        graph = PanelGraph(source)
        self.assertEqual(len(graph.components), 2)
        self.assertEqual(graph.structure["layout"], "separate")
        self.assertEqual(graph.structure["top"], "lid-face")
        self.assertEqual(graph.structure["bottom"], "base-face")
        self.assertEqual(graph.structure["shared_connector"], [])
        self.assertEqual(len(graph.contacts), 8)
        fit = graph.lid_fit_plan(0.5, 0.1)
        self.assertEqual(fit["layout"], "separate")
        self.assertIsNone(fit["connector"])
        self.assertEqual({row["side"] for row in fit["walls"]}, {"top", "right", "bottom", "left"})
        self.assertTrue(all(row["gap_layers"] == 1 for row in fit["walls"]))
        self.assertTrue(all(abs(row["midplane_offset"]-1.1) < 1e-12 for row in fit["walls"]))

        def component_bounds(candidate, panel_ids):
            panels = [panel for panel in candidate.panels if panel.id in panel_ids]
            return (min(panel.polygon.bounds[0] for panel in panels),
                    min(panel.polygon.bounds[1] for panel in panels),
                    max(panel.polygon.bounds[2] for panel in panels),
                    max(panel.polygon.bounds[3] for panel in panels))

        source_top = component_bounds(graph, graph.structure["top_shell"])
        source_bottom = component_bounds(graph, graph.structure["bottom_shell"])
        source_gap = source_bottom[1]-source_top[3]
        resized = graph.resized(width=80, length=40, base_height=15, lid_height=15)
        dimensions = resized._resize_structure()["dimensions"]
        self.assertAlmostEqual(dimensions["width"], 80)
        self.assertAlmostEqual(dimensions["length"], 40)
        self.assertAlmostEqual(dimensions["base_height"], 15)
        self.assertAlmostEqual(dimensions["lid_height"], 15)
        target_top = component_bounds(resized, resized.structure["top_shell"])
        target_bottom = component_bounds(resized, resized.structure["bottom_shell"])
        self.assertAlmostEqual(target_bottom[1]-target_top[3], source_gap, places=5)
        with self.assertRaisesRegex(ValueError, "Connector depth requires"):
            graph.resized(width=80, connector_depth=20)

        with tempfile.TemporaryDirectory() as directory:
            compiled = Path(directory) / "separate.svg"
            recompiled = Path(directory) / "recompiled.svg"
            report = resized.compile(compiled, thickness=0.5, lid_clearance=0.1)
            self.assertEqual(report["cut_contours"], 2)
            self.assertEqual(report["tab_relief_cuts"], 16)
            self.assertEqual(report["corner_relief_cuts"], 8)
            self.assertEqual(report["lid_fit"]["status"], "applied")
            PanelGraph(compiled).compile(recompiled)
            self.assertEqual([(node.get("id"), node.get("d")) for node in ET.parse(compiled).findall(f".//{{{SVG}}}path")],
                             [(node.get("id"), node.get("d")) for node in ET.parse(recompiled).findall(f".//{{{SVG}}}path")])

    def test_separate_shell_inference_rejects_ambiguous_components(self):
        def shell(prefix, x, y, width, length):
            return [
                ("rect", {"id": f"{prefix}-face", "x": str(x), "y": str(y),
                          "width": str(width), "height": str(length)}),
                ("rect", {"id": f"{prefix}-top", "x": str(x), "y": str(y-5),
                          "width": str(width), "height": "5"}),
                ("rect", {"id": f"{prefix}-right", "x": str(x+width), "y": str(y),
                          "width": "5", "height": str(length)}),
                ("rect", {"id": f"{prefix}-bottom", "x": str(x), "y": str(y+length),
                          "width": str(width), "height": "5"}),
                ("rect", {"id": f"{prefix}-left", "x": str(x-5), "y": str(y),
                          "width": "5", "height": str(length)}),
            ]

        with tempfile.TemporaryDirectory() as directory:
            unequal = PanelGraph(svg(shell("upper", 10, 10, 40, 30)+
                                     shell("lower", 10, 60, 80, 50), directory, "unequal.svg"))
            self.assertEqual(unequal.structure["status"], "none")

            open_shells = shell("upper", 10, 10, 40, 30)[:-1]+shell("lower", 10, 60, 40, 30)[:-1]
            incomplete = PanelGraph(svg(open_shells, directory, "incomplete.svg"))
            self.assertEqual(incomplete.structure["status"], "none")

            def tiny_flaps(prefix, y):
                return [
                    ("rect", {"id": f"{prefix}-face", "x": "10", "y": str(y),
                              "width": "40", "height": "30"}),
                    ("rect", {"id": f"{prefix}-top", "x": "29", "y": str(y-5),
                              "width": "2", "height": "5"}),
                    ("rect", {"id": f"{prefix}-right", "x": "50", "y": str(y+14),
                              "width": "5", "height": "2"}),
                    ("rect", {"id": f"{prefix}-bottom", "x": "29", "y": str(y+30),
                              "width": "2", "height": "5"}),
                    ("rect", {"id": f"{prefix}-left", "x": "5", "y": str(y+14),
                              "width": "5", "height": "2"}),
                ]

            short_folds = PanelGraph(svg(tiny_flaps("upper", 10)+tiny_flaps("lower", 60),
                                         directory, "short-folds.svg"))
            self.assertEqual(short_folds.structure["status"], "none")

    def test_separate_lid_fit_rejects_overlapping_output_contours(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = ET.parse(ROOT / "presets" / "box_glued-flaps_separate-lid.svg")
            for node in tree.findall(f".//{{{SVG}}}rect"):
                if node.get("id", "").startswith("base-"):
                    node.set("y", str(float(node.get("y"))-19.8))
            source = Path(directory) / "narrow-gap.svg"
            tree.write(source, encoding="utf-8", xml_declaration=True)
            graph = PanelGraph(source)
            self.assertEqual(graph.structure["layout"], "separate")
            with self.assertRaisesRegex(ValueError, "contours overlap"):
                graph.compile(Path(directory) / "unused.svg", thickness=0.5, lid_clearance=0.1)

    def test_lid_clearance_requires_inferred_enclosure(self):
        graph = PanelGraph(SINGLE, snap_near=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "requires an inferred"):
                graph.compile(Path(directory) / "unused.svg", thickness=0.5, lid_clearance=0.1)

    def test_compiled_svg_recompiles_from_embedded_source(self):
        source = ROOT / "drafts" / "connected.svg"
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.svg"
            direct = Path(directory) / "direct.svg"
            recompiled = Path(directory) / "recompiled.svg"
            PanelGraph(source, snap_near=True).compile(first, thickness=0.5)
            compiled_graph = PanelGraph(first)
            self.assertTrue(compiled_graph.input_is_compiled)
            self.assertEqual(compiled_graph.source_name, "connected.svg")
            self.assertTrue(compiled_graph.snap_near)
            compiled_graph.compile(recompiled, thickness=0.8, fold_offset=0.1, lid_clearance=0.2)
            PanelGraph(source, snap_near=True).compile(
                direct, thickness=0.8, fold_offset=0.1, lid_clearance=0.2)

            def manufacturing_paths(path):
                root = ET.parse(path).getroot()
                return [(node.get("id"), node.get("d")) for node in root.findall(f".//{{{SVG}}}path")]

            self.assertEqual(manufacturing_paths(recompiled), manufacturing_paths(direct))
            model = json.loads(ET.parse(recompiled).findtext(f".//{{{BOXXER}}}model"))
            self.assertEqual(model["thickness"], 0.8)
            self.assertEqual(model["fold_offset"], 0.1)
            self.assertEqual(model["lid_clearance"], 0.2)
            stored = Path(directory) / "stored.svg"
            PanelGraph(first).compile(stored)
            stored_model = json.loads(ET.parse(stored).findtext(f".//{{{BOXXER}}}model"))
            self.assertEqual(stored_model["thickness"], 0.5)
            no_snap = subprocess.run([
                sys.executable, "-m", "boxxer", "compile", str(first),
                "--no-snap-near", "-o", str(Path(directory) / "no-snap.svg"),
            ], capture_output=True, text=True)
            self.assertEqual(no_snap.returncode, 2)
            self.assertIn("Near-coincident", no_snap.stderr)

            invalid = Path(directory) / "invalid.svg"
            tree = ET.parse(first)
            tree.find(f".//{{{BOXXER}}}source").attrib.pop("sha256")
            tree.write(invalid, encoding="utf-8", xml_declaration=True)
            with self.assertRaisesRegex(ValueError, "requires a valid SHA-256"):
                PanelGraph(invalid)

            tree = ET.parse(first)
            model_node = tree.find(f".//{{{BOXXER}}}model")
            invalid_model = json.loads(model_node.text)
            invalid_model["schema"] = 99
            model_node.text = json.dumps(invalid_model)
            tree.write(invalid, encoding="utf-8", xml_declaration=True)
            with self.assertRaisesRegex(ValueError, "schema 99"):
                PanelGraph(invalid)

    def test_single_shell_resize_preserves_tabs_curves_and_internal_features(self):
        def tab_depth(graph, tab_id, seam_id):
            panel = next(panel for panel in graph.panels if panel.id == tab_id)
            seam = next(seam for seam in graph.seams if seam.id == seam_id)
            vector = seam.end-seam.start
            return max(abs(((complex(x, y)-seam.start)*vector.conjugate()).imag)/abs(vector)
                       for x, y in panel.polygon.exterior.coords)

        rotational = PanelGraph(SINGLE, snap_near=True)
        resized = rotational.resized(width=80, length=70, height=25)
        dimensions = resized._resize_structure()["dimensions"]
        self.assertAlmostEqual(dimensions["width"], 80)
        self.assertAlmostEqual(dimensions["length"], 70)
        self.assertAlmostEqual(dimensions["height"], 25, places=4)
        principal = resized._resize_structure()["principal"]
        principal_folds = [seam.length for seam in resized.seams
                           if principal in (seam.panel_a, seam.panel_b)]
        self.assertEqual([round(value) for value in sorted(principal_folds)], [70, 70, 80, 80])
        tab_folds = [seam.length for seam in resized.seams
                     if principal not in (seam.panel_a, seam.panel_b)]
        self.assertTrue(all(abs(value-22) < 0.001 for value in tab_folds))
        self.assertEqual(len(resized.contacts), 4)
        for contact in rotational.contacts:
            self.assertAlmostEqual(tab_depth(resized, contact.tab, contact.seam),
                                   tab_depth(rotational, contact.tab, contact.seam), places=4)

        second = PanelGraph(ROOT / "second.svg", snap_near=True)
        source_internal = {shape.id: shape.polygon.bounds for panel in second.panels for shape in panel.internal}
        source_owners = {shape.id: panel.polygon.bounds for panel in second.panels for shape in panel.internal}
        source_curves = sorted(segment.length(error=1e-6) for panel in second.panels
                               for segment in panel.path if not isinstance(segment, Line))
        resized_second = second.resized(width=125, length=90, height=30)
        target_internal = {shape.id: shape.polygon.bounds for panel in resized_second.panels for shape in panel.internal}
        target_owners = {shape.id: panel.polygon.bounds for panel in resized_second.panels for shape in panel.internal}
        target_curves = sorted(segment.length(error=1e-6) for panel in resized_second.panels
                               for segment in panel.path if not isinstance(segment, Line))
        for shape_id, bounds in source_internal.items():
            before = bounds[2]-bounds[0], bounds[3]-bounds[1]
            after_bounds = target_internal[shape_id]
            after = after_bounds[2]-after_bounds[0], after_bounds[3]-after_bounds[1]
            # These horizontal receiving slots follow the wall height, retaining
            # their width and their insets from the transverse panel edges.
            self.assertAlmostEqual(after_bounds[0]-target_owners[shape_id][0],
                                   bounds[0]-source_owners[shape_id][0], places=5)
            self.assertAlmostEqual(target_owners[shape_id][2]-after_bounds[2],
                                   source_owners[shape_id][2]-bounds[2], places=5)
            self.assertAlmostEqual(after[1], before[1])
        self.assertEqual(len(source_curves), len(target_curves))
        for before, after in zip(source_curves, target_curves):
            self.assertAlmostEqual(after, before)
        for contact in second.contacts:
            self.assertAlmostEqual(tab_depth(resized_second, contact.tab, contact.seam),
                                   tab_depth(second, contact.tab, contact.seam), places=4)
        self.assertEqual({panel.id for panel in resized_second.panels if panel.outside_hint},
                         {"rect4-20", "rect4-20-6", "path6", "path7"})
        with tempfile.TemporaryDirectory() as directory:
            report = resized_second.compile(Path(directory) / "resized.svg", thickness=0.5)
            self.assertEqual(report["internal_cuts"], 4)
            self.assertEqual(len(report["compensation"]), 4)

    def test_connected_resize_round_trips_from_embedded_source(self):
        graph = PanelGraph(ROOT / "drafts" / "connected.svg", snap_near=True)
        source_curves = sorted(segment.length(error=1e-6) for panel in graph.panels
                               for segment in panel.path if not isinstance(segment, Line))
        resized = graph.resized(width=80, length=70, height=25)
        model = resized._resize_structure()
        dimensions = model["dimensions"]
        self.assertAlmostEqual(dimensions["width"], 80)
        self.assertAlmostEqual(dimensions["length"], 70)
        self.assertAlmostEqual(dimensions["base_height"], 25, places=4)
        self.assertAlmostEqual(dimensions["lid_height"], 25, places=4)
        self.assertAlmostEqual(dimensions["connector_depth"], 25, places=4)
        target_curves = sorted(segment.length(error=1e-6) for panel in resized.panels
                               for segment in panel.path if not isinstance(segment, Line))
        for before, after in zip(source_curves, target_curves):
            self.assertAlmostEqual(after, before)
        self.assertEqual(len(resized.contacts), 6)
        independent = graph.resized(base_height=30, lid_height=15, connector_depth=18)
        independent_dimensions = independent._resize_structure()["dimensions"]
        self.assertAlmostEqual(independent_dimensions["base_height"], 30, places=4)
        self.assertAlmostEqual(independent_dimensions["lid_height"], 15, places=4)
        self.assertAlmostEqual(independent_dimensions["connector_depth"], 18, places=4)
        self.assertEqual(len(independent.contacts), 6)
        self.assertEqual(independent.contact_warnings, [])
        with tempfile.TemporaryDirectory() as directory:
            compiled = Path(directory) / "resized.svg"
            recompiled = Path(directory) / "recompiled.svg"
            report = resized.compile(compiled, thickness=0.5, lid_clearance=0.1)
            self.assertEqual(report["metadata"]["resize"]["effective_dimensions"]["width"], 80)
            self.assertEqual(report["lid_fit"]["status"], "applied")
            PanelGraph(compiled).compile(recompiled)

            def manufacturing_paths(path):
                return [(node.get("id"), node.get("d"))
                        for node in ET.parse(path).findall(f".//{{{SVG}}}path")]

            self.assertEqual(manufacturing_paths(compiled), manufacturing_paths(recompiled))

    def test_resize_cli_writes_optional_source_and_allows_a4_overflow(self):
        command = [sys.executable, "-m", "boxxer"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "compiled.svg"
            source = Path(directory) / "source.svg"
            result = subprocess.run(command+[
                "resize", str(SINGLE), "--snap-near",
                "--width", "300", "--height", "25", "--thickness", "0.5",
                "--source-output", str(source), "-o", str(output), "--json",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["page_overflow"])
            self.assertGreater(report["output_bounds"][1], 210)
            for path in (source, output):
                root = ET.parse(path).getroot()
                self.assertEqual(root.get("width"), "210mm")
                self.assertEqual(root.get("height"), "297mm")
                self.assertEqual(root.get("viewBox"), "0 0 210 297")
            model = json.loads(ET.parse(output).findtext(f".//{{{BOXXER}}}model"))
            self.assertTrue(model["page_overflow"])
            self.assertEqual(model["resize"]["requested"], {"width": 300.0, "height": 25.0})

            missing = subprocess.run(command+[
                "resize", str(SINGLE), "--snap-near", "-o", str(output),
            ], capture_output=True, text=True)
            self.assertEqual(missing.returncode, 2)
            self.assertIn("at least one target dimension", missing.stderr)

            hard_link = Path(directory) / "source-hard-link.svg"
            os.link(SINGLE, hard_link)
            unsafe = subprocess.run(command+[
                "resize", str(SINGLE), "--snap-near", "--width", "80",
                "-o", str(hard_link),
            ], capture_output=True, text=True)
            self.assertEqual(unsafe.returncode, 2)
            self.assertIn("Input and output paths must differ", unsafe.stderr)

            output.write_text("compiled sentinel")
            source.write_text("source sentinel")
            failed = subprocess.run(command+[
                "resize", str(SINGLE), "--snap-near", "--width", "80",
                "--thickness", "0.5", "--fold-offset", "-0.6",
                "--source-output", str(source), "-o", str(output),
            ], capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(output.read_text(), "compiled sentinel")
            self.assertEqual(source.read_text(), "source sentinel")
            with self.assertRaisesRegex(ValueError, "must not be directories"):
                boxxer.validate_resize_paths(SINGLE, output, Path(directory))

            graph = PanelGraph(SINGLE, snap_near=True)
            real_replace = os.replace
            failed_source_install = False

            def fail_second_install(source_path, target_path):
                nonlocal failed_source_install
                if Path(target_path) == source and not failed_source_install:
                    failed_source_install = True
                    raise OSError("simulated second-output failure")
                return real_replace(source_path, target_path)

            with mock.patch.object(boxxer.os, "replace", side_effect=fail_second_install):
                with self.assertRaisesRegex(OSError, "second-output failure"):
                    boxxer.resize_and_compile(
                        graph, SINGLE, output, source, {"width": 80},
                        thickness=0.5, fold_offset=0, lid_clearance=0)
            self.assertEqual(output.read_text(), "compiled sentinel")
            self.assertEqual(source.read_text(), "source sentinel")

    def test_inconsistent_cyclic_panel_graph_reports_closure_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = svg([
                ("rect", {"id": "a", "x": "0", "y": "0", "width": "10", "height": "10"}),
                ("rect", {"id": "b", "x": "10", "y": "0", "width": "10", "height": "10"}),
                ("rect", {"id": "c", "x": "0", "y": "10", "width": "10", "height": "10"}),
                ("rect", {"id": "d", "x": "10", "y": "10", "width": "10", "height": "10"}),
            ], directory)
            graph = PanelGraph(source)
            self.assertEqual(len(graph.seams), 4)
            self.assertEqual(graph.contacts, [])
            self.assertTrue(any("Inconsistent fold cycle" in warning for warning in graph.contact_warnings))

    def test_cli_safety_json_and_validation(self):
        command = [sys.executable, "-m", "boxxer"]
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
