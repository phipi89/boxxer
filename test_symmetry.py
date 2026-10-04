"""Rotation and combined symmetry rules, using generated geometry only."""
import io
import math
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import Arc, Path

from foldguides import rotation_guide_for
from panelgraph import PanelGraph, SVG
from test_folds import quarter


def rotation_marker(center=20+20j, order=4, radius=15):
    end = center+radius*complex(math.cos(2*math.pi/order), math.sin(2*math.pi/order))
    return Path(Arc(center+radius, complex(radius, radius), 0, False, True, end)).d()


def cyclic_corner(transform="", mirror=False):
    root = ET.fromstring(quarter(transform))
    group = root.find(f"{{{SVG}}}g")
    for node in list(group):
        if node.get("id", "").startswith("mirror-"):
            group.remove(node)
    ET.SubElement(group, f"{{{SVG}}}path", d=rotation_marker(), id="rotation",
                  style="fill:none;stroke:#008800;stroke-dasharray:4,2,1,2")
    if mirror:
        ET.SubElement(group, f"{{{SVG}}}path", d="M20 0 V40", id="mirror",
                      style="stroke-dasharray:4,2,1,2")
    return ET.tostring(root)


class RotationTests(unittest.TestCase):
    def test_repeat_angles_and_reversed_arcs(self):
        from svgpathtools import parse_path
        for order in (2, 3, 4, 6, 8):
            path = parse_path(rotation_marker(order=order))
            for candidate in (path, path.reversed()):
                marker = rotation_guide_for(candidate, {"stroke-dasharray": "4,2,1,2"}, "marker")
                self.assertEqual(marker.order, order)
                self.assertAlmostEqual(abs(marker.center-(20+20j)), 0)

    def test_rotated_gusset_net_resizes_and_roundtrips(self):
        graph = PanelGraph("generated.svg", _source_bytes=cyclic_corner())
        self.assertEqual(len(graph.panels), 13)
        self.assertEqual(len(graph.seams), 16)
        self.assertEqual(sum(len(panel.internal) for panel in graph.panels), 4)
        self.assertEqual(graph.contact_warnings, [])
        self.assertEqual(graph.rotational_symmetry[0]["order"], 4)
        resized = graph.resized(width=28, length=24, height=7)
        output = io.BytesIO()
        result = resized.compile(output, thickness=.4)
        self.assertEqual((result["cut_contours"], result["internal_cuts"]), (1, 4))
        again = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        self.assertEqual(len(again.panels), 13)
        self.assertEqual(sorted(s.angle for s in graph.seams), sorted(s.angle for s in again.seams))

    def test_arbitrary_rotation_and_reflection_of_marker(self):
        for transform in ("rotate(31)", "translate(50,10) scale(-1,1)", "scale(2)"):
            graph = PanelGraph("generated.svg", _source_bytes=cyclic_corner(transform))
            self.assertEqual(len(graph.panels), 13)
            self.assertEqual(graph.contact_warnings, [])
            graph.compile(io.BytesIO(), thickness=.4)

    def test_rotation_and_mirror_form_finite_group(self):
        root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
        ET.SubElement(root, f"{{{SVG}}}rect", x="32", y="22", width="2", height="3")
        ET.SubElement(root, f"{{{SVG}}}path", id="rotation", d=rotation_marker(),
                      style="stroke-dasharray:4,2,1,2")
        ET.SubElement(root, f"{{{SVG}}}path", id="mirror", d="M0 20 H40",
                      style="stroke-dasharray:4,2,1,2")
        graph = PanelGraph("generated.svg", _source_bytes=ET.tostring(root))
        self.assertEqual(len(graph.panels), 8)
        self.assertEqual(graph.overlaps, [])
        self.assertEqual(graph.compile(io.BytesIO())["cut_contours"], 8)
        root.find(f"{{{SVG}}}path[@id='mirror']").set("d", "M0 21 H40")
        with self.assertRaisesRegex(ValueError, "rotation centre"):
            PanelGraph("generated.svg", _source_bytes=ET.tostring(root))

    def test_invalid_markers_and_conflicting_fold_copies(self):
        with self.assertRaisesRegex(ValueError, "one circle"):
            PanelGraph("generated.svg", _source_bytes=cyclic_corner("scale(2,1)"))
        root = ET.fromstring(cyclic_corner())
        marker = root.find(f".//{{{SVG}}}path[@id='rotation']")
        marker.set("d", rotation_marker(order=7.5))
        with self.assertRaisesRegex(ValueError, "divide 360"):
            PanelGraph("generated.svg", _source_bytes=ET.tostring(root))
        root = ET.fromstring(cyclic_corner())
        ET.SubElement(root, f"{{{SVG}}}path", id="conflict", d="M30 10 H34")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            PanelGraph("generated.svg", _source_bytes=ET.tostring(root))


if __name__ == "__main__":
    unittest.main()
