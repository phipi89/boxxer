"""Signed sketch annotations, mirrored nets, and independent hinge closure checks."""
import io
from contextlib import redirect_stdout
import math
from pathlib import Path as FilePath
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
from svgpathtools import CubicBezier, Line, Path, parse_path

from boxxer.foldguides import guide_for
from boxxer.panelgraph import BOXXER, SVG, PanelGraph, rotation_3d


def quarter(transform="", reverse=False):
    root = ET.Element(f"{{{SVG}}}svg", width="100mm", height="100mm", viewBox="0 0 100 100")
    group = ET.SubElement(root, f"{{{SVG}}}g", transform=transform)
    shapes = [
        ("base", "M10 10 H30 V30 H10 Z", "none"),
        ("top", "M10 6 H30 V10 H10 Z", "none"),
        ("left", "M6 10 H10 V30 H6 Z", "none"),
        ("return", "M10 6 V10 L6 6 Z", "none"),
        ("gusset", "M6 6 L10 10 H6 Z", "none"),
        ("slot", "M11 6.5 H11.5 V9 H11 Z", "none"),
        ("diagonal", "M10 10 L6 6 Z", "none"),
        ("back", "M10 6 V10 Z", "1,1"),
        ("mirror-x", "M20 0 V40", "4,2,1,2"),
        ("mirror-y", "M0 20 H40", "4,2,1,2"),
    ]
    for identifier, d, dash in reversed(shapes) if reverse else shapes:
        if reverse:
            d = parse_path(d).reversed().d()
        ET.SubElement(group, f"{{{SVG}}}path", id=identifier, d=d,
                      fill="none", stroke="#ee8822", **{"stroke-dasharray": dash})
    return ET.tostring(root)


def graph(source=None, **kwargs):
    return PanelGraph("generated.svg", _source_bytes=source or quarter(), **kwargs)


class FoldSketchTests(unittest.TestCase):
    def test_all_four_angles_and_subdivided_return(self):
        for d, magnitude in (("M0 0 H10", 90), ("M0 0 H5 H10 H5 Z", 180)):
            for dash, sign in (("none", 1), ("1,1", -1)):
                for path in (parse_path(d), parse_path(d).reversed()):
                    self.assertEqual(guide_for(path, {"stroke-dasharray": dash}, "fold").angle,
                                     sign*magnitude)
        with self.assertRaisesRegex(ValueError, "once or twice"):
            guide_for(parse_path("M0 0 H10 H5"), {}, "partial-return")

    def test_mirrors_expand_and_deduplicate_material(self):
        g = graph()
        self.assertEqual(len(g.panels), 13)
        self.assertEqual(len(g.seams), 16)
        self.assertEqual(sum(len(panel.internal) for panel in g.panels), 4)
        self.assertEqual(sorted(seam.angle for seam in g.seams), [-180]*4+[90]*8+[180]*4)
        self.assertEqual(g.contact_warnings, [])
        self.assertEqual(g.overlaps, [])
        # Explicitly expanded source must not multiply again at reimport.
        records = [(p.order, p.id, p.path, p.outside_hint, "auto", p.corner_treatments)
                   for p in g.panels]
        expanded = g._source_document(records)
        mirrored = ET.fromstring(quarter())
        for node in mirrored.findall(f".//{{{SVG}}}path"):
            if node.get("id", "").startswith("mirror-"):
                expanded.append(node)
        again = graph(ET.tostring(expanded))
        self.assertEqual(len(again.panels), 13)
        self.assertEqual(len(again.seams), 16)

    def test_unmatched_partial_and_conflicting_guides_fail(self):
        for d, dash, message in (("M70 70 H80", "none", "does not match"),
                                 ("M10 6 V8", "none", "only part"),
                                 ("M10 6 V10", "none", "Conflicting")):
            root = ET.fromstring(quarter())
            ET.SubElement(root, f"{{{SVG}}}path", id="bad", d=d, fill="none",
                          **{"stroke-dasharray": dash})
            with self.assertRaisesRegex(ValueError, message):
                graph(ET.tostring(root))

    def test_nonorthogonal_mirror_axes_fail(self):
        root = ET.fromstring(quarter())
        root.find(f".//{{{SVG}}}path[@id='mirror-y']").set("d", "M0 0 L40 40")
        with self.assertRaisesRegex(ValueError, "perpendicular"):
            graph(ET.tostring(root))

    def test_wrong_diagonal_does_not_close(self):
        root = ET.fromstring(quarter())
        root.find(f".//{{{SVG}}}path[@id='diagonal']").set("d", "M10 10 L6 6")
        g = graph(ET.tostring(root))
        self.assertTrue(any("Inconsistent fold cycle" in warning for warning in g.contact_warnings))
        with self.assertRaisesRegex(ValueError, "Inconsistent fold cycle"):
            g.compile(io.BytesIO(), thickness=0)

    def test_front_back_scores_and_embedded_source_roundtrip(self):
        output = io.BytesIO()
        g = graph()
        result = g.compile(output, thickness=.4)
        root = ET.fromstring(output.getvalue())
        front = root.find(f"{{{SVG}}}g[@id='boxxer-scores']")
        back = root.find(f"{{{SVG}}}g[@id='boxxer-scores-back']")
        self.assertEqual((len(front), len(back)), (12, 4))
        for node in list(front)+list(back):
            self.assertEqual(len(parse_path(node.get("d"))), 1)
            self.assertIn(node.get(f"{{{BOXXER}}}angle"), ("90", "180", "-180"))
        self.assertEqual(result["cut_contours"], 1)
        self.assertEqual(result["internal_cuts"], 4)
        reimported = graph(output.getvalue())
        again = io.BytesIO()
        reimported.compile(again)
        self.assertEqual(output.getvalue(), again.getvalue())

    def test_surface_hinges_close_and_separate_three_layers(self):
        g = graph()
        result = g.compile(io.BytesIO(), thickness=.4)
        plans = {row["fold"]: row for row in result["fold_stack"]}
        by_id = {panel.id: panel for panel in g.panels}
        transforms = {"base": np.eye(4)}
        queue = ["base"]
        # Independently compose actual rotations about displaced 3-D surface
        # axes, rather than inspecting the solver's reported linear residual.
        while queue:
            parent = queue.pop(0)
            for child, seam in g._adjacency()[parent]:
                shift = complex(*plans[seam.id]["shift"])
                start, end = seam.start+shift, seam.end+shift
                interior = complex(*by_id[child].polygon.representative_point().coords[0])-seam.start
                side = ((seam.end-seam.start).conjugate()*interior).imag
                angle = math.copysign(1, side)*math.radians(seam.angle)
                height = np.eye(4)
                height[2, 3] = math.copysign(.2, seam.angle)
                candidate = transforms[parent]@height@rotation_3d(start, end, angle)@np.linalg.inv(height)
                if child in transforms:
                    np.testing.assert_allclose(candidate, transforms[child], atol=1e-8)
                else:
                    transforms[child] = candidate
                    queue.append(child)
        normal = transforms["top"][:3, 2]
        # y=10 is the common unshifted plane of these three folded faces.
        worlds = []
        for identifier in ("top", "return", "gusset"):
            point = by_id[identifier].polygon.centroid
            worlds.append((transforms[identifier]@np.array([point.x, point.y, 0, 1]))[:3])
        self.assertAlmostEqual(float((worlds[1]-worlds[0])@normal), -.4)
        self.assertAlmostEqual(float((worlds[2]-worlds[0])@normal), -.8)
        horizontal = next(s for s in g.seams if {s.panel_a, s.panel_b} == {"left", "gusset"})
        vertical = next(s for s in g.seams if {s.panel_a, s.panel_b} == {"top", "return"})
        np.testing.assert_allclose(plans[horizontal.id]["shift"], [0, -.8], atol=1e-8)
        np.testing.assert_allclose(plans[vertical.id]["shift"], [-.4, 0], atol=1e-8)

    def test_offset_and_rotation_order_invariance(self):
        for transform, reverse in (("", False), ("rotate(37)", True), ("scale(-1,1)", True)):
            g = graph(quarter(transform, reverse))
            rows = g.compile(io.BytesIO(), thickness=.4, fold_offset=.1)["fold_stack"]
            distances = sorted(round(row["distance"], 8) for row in rows)
            self.assertEqual(distances, [0]*8+[.5]*4+[1.0]*4)
        with self.assertRaisesRegex(ValueError, "layers overlap"):
            graph().compile(io.BytesIO(), thickness=.4, fold_offset=-.1)

    def test_resize_preserves_gusset_angles_and_slot_widths(self):
        g = graph().resized(width=28, length=24, height=7)
        self.assertEqual(len(g.panels), 13)
        self.assertEqual(g.contact_warnings, [])
        self.assertEqual(sorted(s.angle for s in g.seams), [-180]*4+[90]*8+[180]*4)
        for panel in g.panels:
            for slot in panel.internal:
                x0, _, x1, _ = slot.polygon.bounds
                self.assertAlmostEqual(x1-x0, .5)
        g.compile(io.BytesIO(), thickness=.4)

    def test_actual_inkscape_gluefree_sketch(self):
        source = FilePath(__file__).resolve().parents[1]/"presets"/"tray_mirrored_self-locking.svg"
        g = PanelGraph(source, snap_near=True)
        self.assertEqual(len(g.panels), 13)
        self.assertEqual(len(g.seams), 16)
        self.assertEqual(len(g.mirror_axes), 2)
        resized = g.resized(width=120, length=80, height=30)
        self.assertEqual(sum(isinstance(s, CubicBezier) for p in resized.panels for s in p.path),
                         sum(isinstance(s, CubicBezier) for p in g.panels for s in p.path))
        result = resized.compile(io.BytesIO(), thickness=.4)
        self.assertEqual(result["internal_cuts"], 4)
        self.assertEqual(result["cut_contours"], 1)

    def test_generated_signed_fold_interactive_preset(self):
        from boxxer.cli import interactive_session
        with tempfile.TemporaryDirectory() as directory:
            target = FilePath(directory)/"compiled.svg"
            editable = FilePath(directory)/"editable.svg"
            (FilePath(directory)/"signed-tray.svg").write_bytes(quarter())
            answers = iter(["1", "", "120", "80", "30", ".4", "", str(target), str(editable), "y"])
            with redirect_stdout(io.StringIO()):
                result = interactive_session(directory=directory, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            self.assertEqual(result["metadata"]["thickness"], .4)
            self.assertEqual(len(PanelGraph(editable, snap_near=True).seams), 16)
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
