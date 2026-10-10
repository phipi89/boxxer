"""Physical units, colour operations, curve compatibility, and DXF/CLI consistency."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path as FilePath
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from svgpathtools import Arc, CubicBezier, QuadraticBezier, parse_path

from boxxer.cli import interactive_session
from boxxer.dxf import CURVE_TOLERANCE, flatten
from boxxer.panelgraph import PanelGraph, SVG
from tests.test_fingercuts import tray
from tests.test_symmetry import cyclic_corner


class DxfTests(unittest.TestCase):
    def test_signed_operations_have_indexed_colours_and_continuous_scores(self):
        graph = PanelGraph("generated.svg", _source_bytes=cyclic_corner())
        stream = io.StringIO()
        report = graph.compile(stream, thickness=.4, output_format="dxf")
        document = ezdxf.read(io.StringIO(stream.getvalue()))
        self.assertEqual(document.units, 4)
        self.assertEqual(document.header["$MEASUREMENT"], 1)
        expected = {"CUT": 7, "SCORE_FRONT": 1, "SCORE_BACK": 5}
        for layer, colour in expected.items():
            self.assertEqual(document.layers.get(layer).dxf.color, colour)
            for entity in document.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'):
                self.assertEqual(entity.dxf.color, colour)
                self.assertEqual(entity.dxf.linetype, "CONTINUOUS")
        self.assertEqual(len(document.modelspace().query('LWPOLYLINE[layer=="SCORE_FRONT"]')), 12)
        back = document.modelspace().query('LWPOLYLINE[layer=="SCORE_BACK"]')
        self.assertEqual(len(back), 4)
        self.assertTrue(all(len(entity) == 2 and not entity.closed for entity in back))
        cuts = document.modelspace().query('LWPOLYLINE[layer=="CUT"]')
        self.assertEqual(len(cuts), 5)
        self.assertTrue(all(entity.closed for entity in cuts))
        self.assertEqual(report["output_format"], "dxf")
        self.assertFalse(document.audit().has_errors)

    def test_opposing_finger_cuts_are_on_opposite_edges_without_bulges(self):
        graph = PanelGraph("generated.svg", _source_bytes=tray())
        stream = io.StringIO()
        graph.compile(stream, finger_cutout="opposing", output_format="dxf")
        document = ezdxf.read(io.StringIO(stream.getvalue()))
        cut = document.modelspace().query('LWPOLYLINE[layer=="CUT"]')[0]
        self.assertTrue(cut.closed)
        self.assertFalse(cut.has_arc)
        self.assertTrue(all(entity.dxftype() == "LINE" for entity in cut.virtual_entities()))
        # SVG north free edge is y=30, translated to y=10. The notch goes
        # downward to y=20; DXF reflects it to 300-20 rather than 300-0.
        points = list(cut.get_points())
        self.assertTrue(all(p[4] == 0 for p in points))
        tips = sorted(p[1] for p in points if abs(p[0]-80) < 1e-7)
        self.assertEqual(len(tips), 2)
        self.assertAlmostEqual(tips[0], 160, delta=CURVE_TOLERANCE)
        self.assertAlmostEqual(tips[1], 280, delta=CURVE_TOLERANCE)
        self.assertTrue(Polygon([(p[0], p[1]) for p in points]).is_valid)
        self.assertAlmostEqual(min(p[0] for p in points), 10)
        self.assertAlmostEqual(max(p[0] for p in points)-min(p[0] for p in points), 140)

    def test_flattened_curves_obey_chord_tolerance(self):
        curves = [CubicBezier(0j, 5+20j, 30-15j, 40+0j),
                  QuadraticBezier(0j, 20+50j, 40+0j),
                  Arc(0j, 30+10j, 31, False, True, 40+10j),
                  Arc(0j, 15+15j, 0, False, True, 20+0j),
                  Arc(20+40j, 15+15j, 0, False, False, 0+40j)]
        for curve in curves:
            points = flatten(curve)
            polyline = LineString([(p.real, p.imag) for p in points])
            maximum = max(polyline.distance(Point(curve.point(i/1000).real, curve.point(i/1000).imag))
                          for i in range(1001))
            self.assertLessEqual(maximum, CURVE_TOLERANCE+1e-8)
            self.assertEqual(points[0], curve.start)
            self.assertEqual(points[-1], curve.end)

    def test_svg_and_dxf_curve_geometry_matches_within_tolerance(self):
        from tests.test_corners import rounded_tray
        graph = PanelGraph("generated.svg", _source_bytes=rounded_tray())
        svg, dxf = io.BytesIO(), io.StringIO()
        graph.compile(svg, thickness=.4, finger_cutout="one")
        graph.compile(dxf, thickness=.4, finger_cutout="one", output_format="dxf")
        document = ezdxf.read(io.StringIO(dxf.getvalue()))
        root = ET.fromstring(svg.getvalue())
        for group_id, layer in (("boxxer-cuts", "CUT"), ("boxxer-scores", "SCORE_FRONT")):
            paths = root.find(f"{{{SVG}}}g[@id='{group_id}']").findall(f"{{{SVG}}}path")
            entities = document.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]')
            self.assertEqual(len(paths), len(entities))
            for node, entity in zip(paths, entities):
                path = parse_path(node.get("d"))
                vertices = list(entity.get_points())
                self.assertTrue(all(vertex[4] == 0 for vertex in vertices))
                self.assertEqual(entity.closed, path.isclosed())
                points = [(vertex[0], graph.page[1]-vertex[1]) for vertex in vertices]
                if entity.closed:
                    points.append(points[0])
                actual = LineString(points)
                for segment in path:
                    for index in range(201):
                        point = segment.point(index/200)
                        self.assertLessEqual(actual.distance(Point(point.real, point.imag)), CURVE_TOLERANCE+1e-8)
                    # Exact joins are retained even when a curve is flattened.
                    self.assertLess(min(abs(segment.start-complex(x, y)) for x, y in points), 1e-8)

    def test_cli_compile_and_atomic_resize_select_dxf_by_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = FilePath(directory)/"tray.svg", FilePath(directory)/"out.DXF"
            editable = FilePath(directory)/"editable.svg"
            source.write_bytes(tray())
            for operation, options in (("compile", []), ("resize", ["--width", "50", "--length", "50", "--height", "50",
                                                                  "--source-output", str(editable)])):
                process = subprocess.run([sys.executable, "-m", "boxxer", operation, str(source), "--output", str(output),
                                          "--finger-cutout", "opposing", "--json", *options], text=True, capture_output=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                report = json.loads(process.stdout)
                self.assertEqual(report["output_format"], "dxf")
                self.assertEqual(report["output"], str(output))
                document = ezdxf.readfile(output)
                self.assertEqual(document.units, 4)
            self.assertEqual(ET.parse(editable).getroot().tag, f"{{{SVG}}}svg")

    def test_interactive_dxf_command_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = FilePath(directory)/"tray.svg", FilePath(directory)/"out.dxf"
            source.write_bytes(tray())
            answers = iter(["", "50", "50", "50", ".4", "opposing", str(output), "", "y"])
            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            self.assertEqual(report["output_format"], "dxf")
            self.assertIn("out.dxf", report["equivalent_command"])
            self.assertEqual(ezdxf.readfile(output).units, 4)


if __name__ == "__main__":
    unittest.main()
