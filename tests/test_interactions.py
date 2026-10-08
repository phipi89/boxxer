"""Receiving openings follow actual folded engagement, not their aspect ratio."""
import io
import unittest
import xml.etree.ElementTree as ET

import numpy as np
from shapely.affinity import affine_transform
from svgpathtools import Arc, parse_path

from boxxer.panelgraph import BOXXER, SVG, PanelGraph
from tests.test_features import graph_from_shapes
from tests.test_features import tray_shapes
from tests.test_flaps import flap_shapes
from tests.test_symmetry import cyclic_corner


def flap_receiver(*, fixed=False):
    shapes = flap_shapes(14, 14)[:-1]
    opening = {"id": "slit", "x": "91", "y": "61", "width": "3", "height": "1", "rx": ".5"}
    if fixed:
        opening[f"{{{BOXXER}}}feature"] = "fixed"
    return graph_from_shapes(shapes+[("rect", opening)])


def opening(graph, identifier):
    return next(shape for panel in graph.panels for shape in panel.internal if shape.id == identifier)


class InteractionTests(unittest.TestCase):
    def test_regular_flap_width_changes_keep_corner_offset(self):
        source = flap_receiver()
        self.assertEqual(source.opening_interactions["slit"]["structure"], ["tab-north"])
        target = source.resized(width=60, length=60)
        receiver = next(panel for panel in target.panels if panel.id == "east")
        slot = opening(target, "slit")
        self.assertAlmostEqual(slot.polygon.centroid.y-receiver.polygon.bounds[1], 11.5)
        self.assertAlmostEqual(slot.polygon.bounds[0]-receiver.polygon.bounds[0], 1)
        self.assertAlmostEqual(slot.polygon.bounds[2]-slot.polygon.bounds[0], 3)

    def test_height_scales_flap_reach_and_slot_follows_free_edge(self):
        source = flap_receiver()
        target = source.resized(height=10)
        flap = next(panel for panel in target.panels if panel.id == "tab-north")
        self.assertAlmostEqual(flap.polygon.bounds[2]-flap.polygon.bounds[0], 28)
        slot = opening(target, "slit")
        self.assertAlmostEqual(slot.polygon.centroid.y, 75.5)
        self.assertAlmostEqual(slot.polygon.bounds[2]-slot.polygon.bounds[0], 8)
        self.assertAlmostEqual(slot.polygon.bounds[3]-slot.polygon.bounds[1], 1)
        self.assertTrue(all(abs(segment.radius.real-.5) < 1e-8 for segment in slot.path if isinstance(segment, Arc)))
        receiver = next(panel for panel in target.panels if panel.id == "east")
        relative = np.linalg.inv(target._folded_transforms[receiver.id])@target._folded_transforms[flap.id]
        footprint = affine_transform(flap.polygon, [relative[0, 0], relative[0, 1], relative[1, 0], relative[1, 1],
                                                   relative[0, 3], relative[1, 3]])
        self.assertAlmostEqual(footprint.bounds[3]-slot.polygon.centroid.y, 2.5)
        restored = target.resized(height=5)
        for before, after in zip(opening(source, "slit").path.bbox(), opening(restored, "slit").path.bbox()):
            self.assertAlmostEqual(before, after)

    def test_gusset_slot_follows_triangle_on_height_but_not_box_width(self):
        source = PanelGraph("generated.svg", _source_bytes=cyclic_corner())
        binding = source.opening_interactions["slot"]
        self.assertEqual(binding["kind"], "gusset")
        self.assertEqual(len(binding["structure"]), 2)
        before = opening(source, "slot")
        wider = source.resized(width=28, length=24)
        wall = next(panel for panel in wider.panels if panel.id == "top")
        self.assertAlmostEqual(opening(wider, "slot").polygon.bounds[0]-wall.polygon.bounds[0], 1)
        taller = source.resized(height=7)
        wall = next(panel for panel in taller.panels if panel.id == "top")
        slot = opening(taller, "slot")
        self.assertGreater(slot.polygon.bounds[0]-wall.polygon.bounds[0], 1)
        self.assertAlmostEqual(slot.polygon.bounds[2]-slot.polygon.bounds[0], .5)
        self.assertAlmostEqual(slot.polygon.bounds[3]-slot.polygon.bounds[1],
                               before.polygon.bounds[3]-before.polygon.bounds[1]+3)

    def test_gusset_opening_moves_with_finite_thickness_hinges(self):
        source = PanelGraph("generated.svg", _source_bytes=cyclic_corner())
        results = []
        for thickness in (0, .4):
            output = io.BytesIO()
            report = source.compile(output, thickness=thickness)
            node = ET.fromstring(output.getvalue()).find(f".//{{{SVG}}}path[@id='internal-slot']")
            bounds = parse_path(node.get("d")).bbox()
            results.append(bounds[0]-report["output_translation"][0])
        self.assertAlmostEqual(results[1]-results[0], -.8)

    def test_ambiguity_is_reported_instead_of_choosing_a_flap(self):
        shapes = flap_shapes(20, 20)+[("rect", {"id": "opening", "x": "91", "y": "64", "width": "3", "height": "1"})]
        source = graph_from_shapes(shapes)
        self.assertTrue(source.interaction_warnings)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            source.resized(height=10)

    def test_fixed_override_suppresses_engagement(self):
        source = flap_receiver(fixed=True)
        self.assertEqual(source.opening_interactions, {})
        target = source.resized(height=10)
        slot = opening(target, "slit")
        self.assertAlmostEqual(slot.polygon.bounds[2]-slot.polygon.bounds[0], 3)

    def test_empty_region_of_engaging_part_is_not_a_match(self):
        shapes = flap_shapes(14, 14)[:-1]+[
            ("rect", {"id": "slit", "x": "91", "y": "61", "width": "3", "height": "1"}),
            ("rect", {"id": "flap-hole", "x": "100.8", "y": "45.8", "width": "1.4", "height": "3.4"})]
        source = graph_from_shapes(shapes)
        self.assertNotIn("slit", source.opening_interactions)

    def test_rigid_locking_notch_moves_with_flap_and_drives_opening(self):
        shapes = [shape for shape in flap_shapes(14, 14)[:-1] if shape[1]["id"] != "tab-north"]
        shapes.extend([
            ("path", {"id": "tab-north", "d": "M90 45 H104 V47 H102 V47.5 H104 V49.5 H90 Z"}),
            ("rect", {"id": "slit", "x": "91", "y": "61", "width": "3", "height": "1"})])
        source = graph_from_shapes(shapes)
        target = source.resized(height=10)
        flap = next(panel for panel in target.panels if panel.id == "tab-north")
        notch = next(segment for segment in flap.path if abs(segment.start.real-116) < 1e-8 and
                     abs(segment.end.real-116) < 1e-8)
        self.assertAlmostEqual(notch.length(), .5)
        self.assertAlmostEqual(opening(target, "slit").polygon.centroid.y, 75.5)

    def test_binding_survives_rotation_and_compiled_resize_roundtrip(self):
        source = flap_receiver()
        root = ET.Element(f"{{{SVG}}}svg", width="300mm", height="300mm", viewBox="0 0 300 300")
        for panel in source.panels:
            for identifier, path in [(panel.id, panel.path)]+[(cut.id, cut.path) for cut in panel.internal]:
                ET.SubElement(root, f"{{{SVG}}}path", id=identifier, d=path.reversed().d(), transform="rotate(90)")
        rotated = PanelGraph("generated.svg", _source_bytes=ET.tostring(root))
        self.assertEqual(rotated.opening_interactions["slit"]["structure"], ["tab-north"])
        resized = rotated.resized(height=10)
        output = io.BytesIO()
        resized.compile(output, thickness=.4)
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        self.assertEqual(restored.opening_interactions["slit"]["structure"], ["tab-north"])
        restored.resized(height=5).compile(io.BytesIO(), thickness=.4)

    def test_tall_two_shell_roles_do_not_depend_on_panel_area(self):
        from tests.test_fingercuts import enclosure
        source = PanelGraph("generated.svg", _source_bytes=enclosure(connected=False))
        target = source.resized(width=26, length=200, height=30)
        self.assertEqual(target.structure["status"], "inferred")
        self.assertEqual(target.structure["top"], "lid-principal")
        self.assertEqual(target.structure["bottom"], "base-principal")
        target.compile(io.BytesIO(), thickness=.4)

    def test_single_return_flap_engagement_uses_its_180_degree_fold(self):
        source = graph_from_shapes(tray_shapes(20)+[
            ("rect", {"id": "return", "x": "50", "y": "20", "width": "40", "height": "10"}),
            ("path", {"id": "return-fold", "d": "M50 30 H90 Z"}),
            ("rect", {"id": "slit", "x": "60", "y": "32", "width": "1", "height": "5"})])
        self.assertEqual(source.opening_interactions["slit"]["structure"], ["return"])
        target = source.resized(height=30)
        part = next(panel for panel in target.panels if panel.id == "return")
        self.assertAlmostEqual(part.polygon.bounds[3]-part.polygon.bounds[1], 15)
        slot = opening(target, "slit")
        self.assertAlmostEqual(slot.polygon.bounds[1], 22)
        self.assertAlmostEqual(slot.polygon.bounds[3], 32)
        target.compile(io.BytesIO(), thickness=.4)


if __name__ == "__main__":
    unittest.main()
