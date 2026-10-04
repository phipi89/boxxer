import io
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import QuadraticBezier

from panelgraph import PanelGraph, SVG
from test_features import graph_from_shapes, tray_shapes


def flap_shapes(first=20, second=20):
    return tray_shapes(5)+[
        ("rect", {"id": "tab-north", "x": "90", "y": "45", "width": str(first), "height": "4.5"}),
        ("rect", {"id": "tab-south", "x": "90", "y": "80.5", "width": str(second), "height": "4.5"})]


class FlapTests(unittest.TestCase):
    def test_opposing_flaps_share_shortening_and_keep_hinges(self):
        for first, second in ((20, 20), (20, 14)):
            with self.subTest(first=first, second=second):
                graph = graph_from_shapes(flap_shapes(first, second))
                shortened = graph._shortened_flaps()
                rows = {row["tab"]: row for row in shortened.flap_adjustments}
                self.assertEqual(set(rows), {"tab-north", "tab-south"})
                amount = (first+second-30)/2
                self.assertAlmostEqual(rows["tab-north"]["depth"], first-amount, places=6)
                self.assertAlmostEqual(rows["tab-south"]["depth"], second-amount, places=6)
                self.assertEqual([(s.panel_a, s.panel_b, s.start, s.end) for s in graph.seams],
                                 [(s.panel_a, s.panel_b, s.start, s.end) for s in shortened.seams])
                self.assertIs(shortened._shortened_flaps(), shortened)

    def test_no_shortening_for_disjoint_or_opposite_side_flaps(self):
        separated = graph_from_shapes(flap_shapes(14, 14))
        self.assertIs(separated._shortened_flaps(), separated)
        shapes = flap_shapes()
        shapes[-1][1]["stroke-dasharray"] = "1,1"
        opposite = graph_from_shapes(shapes)
        self.assertIs(opposite._shortened_flaps(), opposite)

    def test_nearly_full_wall_coverage_does_not_hide_a_flap(self):
        shapes = flap_shapes(29.5, 14)[:-1]
        shapes[-1][1]["height"] = "4.9"
        graph = graph_from_shapes(shapes)
        self.assertEqual([(row.tab, row.receiver) for row in graph.contacts], [("tab-north", "east")])
        report = graph.compile(io.BytesIO(), thickness=0.5)
        self.assertEqual(len(report["compensation"]), 1)

    def test_shortening_is_rotation_and_element_order_invariant(self):
        for angle in (0, 23, 90, 180):
            for reverse in (False, True):
                with self.subTest(angle=angle, reverse=reverse):
                    shapes = flap_shapes()
                    if reverse:
                        shapes.reverse()
                    root = ET.fromstring(graph_from_shapes(shapes).source_bytes)
                    for element in root:
                        element.set("transform", f"rotate({angle})")
                    graph = PanelGraph("rotated.svg", _source_bytes=ET.tostring(root))
                    rows = graph._shortened_flaps().flap_adjustments
                    self.assertEqual(len(rows), 2)
                    for row in rows:
                        self.assertAlmostEqual(row["depth"], 15, places=6)

    def test_shrinking_receiver_shortens_flaps_and_round_trips(self):
        graph = graph_from_shapes(flap_shapes(14, 14))
        resized = graph.resized(length=20)
        self.assertEqual(len(resized.flap_adjustments), 2)
        for row in resized.flap_adjustments:
            self.assertAlmostEqual(row["depth"], 10, places=6)
        output = io.BytesIO()
        report = resized.compile(output, thickness=0.5)
        self.assertEqual(len(report["metadata"]["flap_shortening"]), 2)
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        recompiled = io.BytesIO()
        restored.compile(recompiled)
        self.assertEqual(output.getvalue(), recompiled.getvalue())

    def test_direct_compilation_preserves_original_source_and_round_trips(self):
        graph = graph_from_shapes(flap_shapes())
        output = io.BytesIO()
        report = graph.compile(output, thickness=0.5)
        self.assertEqual(len(report["flap_shortening"]), 2)
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        self.assertEqual(restored.source_bytes, graph.source_bytes)
        recompiled = io.BytesIO()
        restored.compile(recompiled)
        self.assertEqual(output.getvalue(), recompiled.getvalue())

        # Direct compilation embeds the intended source depths, allowing a later
        # enlargement to retain the full flap rather than accumulate shortening.
        expanded = restored.resized(length=50)
        self.assertEqual(expanded.flap_adjustments, [])
        for panel in expanded.panels:
            if panel.id.startswith("tab-"):
                self.assertAlmostEqual(panel.path.bbox()[1]-panel.path.bbox()[0], 20)

    def test_shortening_refuses_to_remove_internal_features(self):
        shapes = flap_shapes()+[("circle", {"id": "hole", "cx": "108", "cy": "47", "r": "0.5"})]
        graph = graph_from_shapes(shapes)
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "cut through its internal geometry"):
            graph.compile(output, thickness=0.5)
        self.assertEqual(output.getvalue(), b"")

    def test_clipped_curves_remain_native(self):
        shapes = tray_shapes(5)+[
            ("path", {"id": "tab-north", "d": "M90 45 Q130 47.25 90 49.5 Z"}),
            ("path", {"id": "tab-south", "d": "M90 80.5 Q130 82.75 90 85 Z"})]
        graph = graph_from_shapes(shapes)
        shortened = graph._shortened_flaps()
        self.assertEqual(len(shortened.flap_adjustments), 2)
        for panel in shortened.panels:
            if panel.id.startswith("tab-"):
                self.assertTrue(any(isinstance(segment, QuadraticBezier) for segment in panel.path))
                self.assertTrue(panel.polygon.is_valid)
        shortened.compile(io.BytesIO(), thickness=0.5)


if __name__ == "__main__":
    unittest.main()
