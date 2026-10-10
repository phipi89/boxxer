"""Generated free-edge openings and material-clearance regressions."""
import io
import json
import math
from pathlib import Path as FilePath
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from shapely.geometry import LineString
from svgpathtools import Arc, Line, parse_path

from boxxer.panelgraph import SVG, PanelGraph, polygon_for, sample_segment


def tray(width=100, height=20, slot=False, rotation=""):
    root = ET.Element(f"{{{SVG}}}svg", width="300mm", height="300mm", viewBox="0 0 300 300")
    group = ET.SubElement(root, f"{{{SVG}}}g", transform=rotation)
    for identifier, x, y, w, h in (("base", 50, 50, width, width),
                                  ("north", 50, 50-height, width, height),
                                  ("south", 50, 50+width, width, height),
                                  ("west", 50-height, 50, height, width),
                                  ("east", 50+width, 50, height, width)):
        ET.SubElement(group, f"{{{SVG}}}rect", id=identifier, x=str(x), y=str(y), width=str(w), height=str(h))
    if slot:
        ET.SubElement(group, f"{{{SVG}}}rect", id="slot", x=str(50+width/2-1), y=str(50-height+8),
                      width="2", height="2")
    return ET.tostring(root)


def compile_source(source, **options):
    graph = PanelGraph("generated.svg", _source_bytes=source)
    output = io.BytesIO()
    report = graph.compile(output, **options)
    return graph, report, ET.fromstring(output.getvalue()), output.getvalue()


def enclosure(connected=True, rotation=""):
    root = ET.Element(f"{{{SVG}}}svg", width="400mm", height="400mm", viewBox="0 0 400 400")
    group = ET.SubElement(root, f"{{{SVG}}}g", transform=rotation)
    for prefix, y in (("lid", 50), ("base", 200)):
        for suffix, x, yy, width, height in (("principal", 50, y, 100, 80),
                                            ("north", 50, y-20, 100, 20),
                                            ("south", 50, y+80, 100, 20),
                                            ("west", 30, y, 20, 80),
                                            ("east", 150, y, 20, 80)):
            if connected and (prefix, suffix) in (("lid", "south"), ("base", "north")):
                continue
            ET.SubElement(group, f"{{{SVG}}}rect", id=f"{prefix}-{suffix}", x=str(x), y=str(yy),
                          width=str(width), height=str(height))
    if connected:
        ET.SubElement(group, f"{{{SVG}}}rect", id="hinge", x="50", y="130", width="100", height="70")
    return ET.tostring(root)


class FingerCutoutTests(unittest.TestCase):
    def test_default_circle_is_30_diameter_and_10_deep(self):
        graph, result, root, _ = compile_source(tray(), finger_cutout="one")
        self.assertEqual(len(result["finger_cutouts"]), 1)
        row = result["finger_cutouts"][0]
        self.assertEqual(row["panel"], "north")
        self.assertEqual(row["status"], "applied")
        self.assertFalse(row["cropped"])
        self.assertAlmostEqual(row["depth"], 10)
        self.assertAlmostEqual(row["mouth_width"], 2*math.sqrt(200))
        contour = parse_path(root.find(f"{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path").get("d"))
        arcs = [segment for segment in contour if isinstance(segment, Arc)]
        self.assertEqual(len(arcs), 1)
        self.assertAlmostEqual(arcs[0].radius.real, 15)
        self.assertAlmostEqual(max(y for _, y in sample_segment(arcs[0]))-min(arcs[0].start.imag, arcs[0].end.imag),
                               10, places=4)
        self.assertEqual(len(graph.seams), 4)

    def test_opposing_cutouts_and_page_top_selection(self):
        _, result, _, _ = compile_source(tray(), finger_cutout="opposing")
        self.assertEqual([row["panel"] for row in result["finger_cutouts"]], ["north", "south"])
        _, rotated, _, _ = compile_source(tray(rotation="rotate(90)"), finger_cutout="one")
        self.assertEqual(rotated["finger_cutouts"][0]["panel"], "west")

    def test_connected_lid_opposite_hinge_and_detached_lid_only(self):
        # A rotation makes the opening edge face sideways on the page; the
        # physical hinge, rather than page-top, still selects the lid flap.
        _, report, _, _ = compile_source(enclosure(rotation="rotate(30)"), finger_cutout="one")
        self.assertEqual([row["panel"] for row in report["finger_cutouts"]], ["lid-north"])
        _, report, _, _ = compile_source(enclosure(connected=False), finger_cutout="opposing", thickness=.4)
        self.assertEqual([row["panel"] for row in report["finger_cutouts"]], ["lid-north", "lid-south"])
        with self.assertRaisesRegex(ValueError, "two free opposite walls"):
            compile_source(enclosure(), finger_cutout="opposing")

    def test_finger_arc_and_editable_corner_radii_coexist(self):
        from boxxer.corners import INKSCAPE, CornerTreatment, DEFAULT_SATELLITE, lpe_attributes, rounded_path
        root = ET.fromstring(tray())
        group = root.find(f"{{{SVG}}}g")
        north = group.find(f"{{{SVG}}}rect[@id='north']")
        group.remove(north)
        path = parse_path("M50 30 H150 V50 H50 Z")
        treatment = CornerTreatment(4, DEFAULT_SATELLITE, (("effect", "fillet_chamfer"), ("mode", "F")))
        treatments = {0: treatment, 1: treatment}
        effect, attributes = lpe_attributes(treatments, path, "corners")
        defs = ET.SubElement(root, f"{{{SVG}}}defs")
        ET.SubElement(defs, f"{{{INKSCAPE}}}path-effect", effect)
        ET.SubElement(group, f"{{{SVG}}}path", id="north", d=rounded_path(path, treatments).d(),
                      **attributes, **{f"{{{INKSCAPE}}}original-d": path.d()})
        _, report, output, _ = compile_source(ET.tostring(root), finger_cutout="one")
        contour = output.find(f"{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path")
        radii = sorted(round(s.radius.real, 6) for s in parse_path(contour.get("d")) if isinstance(s, Arc))
        self.assertEqual(radii, [4, 4, 15])
        self.assertIsNotNone(contour.get(f"{{{INKSCAPE}}}path-effect"))
        self.assertEqual(report["finger_cutouts"][0]["status"], "applied")

    def test_small_flap_is_cropped_not_scaled(self):
        _, result, root, _ = compile_source(tray(width=20, height=8), finger_cutout="one")
        row = result["finger_cutouts"][0]
        self.assertTrue(row["cropped"])
        self.assertLessEqual(row["depth"], 5)
        self.assertLessEqual(row["mouth_width"], 14)
        self.assertGreaterEqual(row["clearance"], 3)
        contour = parse_path(root.find(f"{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path").get("d"))
        self.assertTrue(polygon_for(contour).is_valid)
        # A wider but shallow flap retains radius 15 at its clipped shoulders.
        _, shallow, root, _ = compile_source(tray(height=8), finger_cutout="one")
        arcs = [s for s in parse_path(root.find(f"{{{SVG}}}g[@id='boxxer-cuts']/{{{SVG}}}path").get("d"))
                if isinstance(s, Arc)]
        self.assertEqual(len(arcs), 2)
        self.assertTrue(all(abs(s.radius.real-15) < 1e-7 for s in arcs))
        self.assertGreaterEqual(shallow["finger_cutouts"][0]["clearance"], 3)

    def test_internal_slot_and_score_clearance(self):
        _, result, root, _ = compile_source(tray(slot=True), finger_cutout="one", thickness=.5)
        row = result["finger_cutouts"][0]
        self.assertTrue(row["cropped"])
        self.assertLessEqual(row["depth"], 5)
        paths = root.find(f"{{{SVG}}}g[@id='boxxer-cuts']").findall(f"{{{SVG}}}path")
        outer, internal = [parse_path(node.get("d")) for node in paths]
        # Measure emitted geometry independently of the planning report.
        outer_line = LineString([point for s in outer for point in sample_segment(s)])
        inner_line = LineString([point for s in internal for point in sample_segment(s)])
        self.assertGreaterEqual(outer_line.distance(inner_line), 3)

    def test_unusable_flaps_are_omitted_and_inputs_validated(self):
        for width, height in ((6, 4), (100, 3)):
            _, result, _, _ = compile_source(tray(width, height), finger_cutout="one")
            self.assertEqual(result["finger_cutouts"][0]["status"], "omitted")
        for options in ({"finger_cutout_diameter": -1}, {"finger_cutout_depth": float("nan")},
                        {"finger_cutout_depth": 16}):
            with self.assertRaises(ValueError):
                compile_source(tray(), finger_cutout="one", **options)

    def test_recompile_resize_and_disable_do_not_accumulate_notches(self):
        _, result, _, output = compile_source(tray(), finger_cutout="one")
        graph = PanelGraph("compiled.svg", _source_bytes=output)
        again = io.BytesIO()
        graph.compile(again)
        self.assertEqual(output, again.getvalue())
        resized = graph.resized(width=20, length=25, height=8)
        report = resized.compile(io.BytesIO())
        self.assertTrue(report["finger_cutouts"][0]["cropped"])
        report = graph.compile(io.BytesIO(), finger_cutout="none")
        self.assertEqual(report["finger_cutouts"], [])

    def test_cli_defaults_and_custom_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = FilePath(directory)/"source.svg", FilePath(directory)/"out.svg"
            source.write_bytes(tray())
            command = [sys.executable, "-m", "boxxer", "compile", str(source),
                       "--output", str(output), "--finger-cutout", "--json"]
            process = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["finger_cutout"]["mode"], "one")
            process = subprocess.run(command[:-1]+["opposing", "--finger-cutout-diameter", "20",
                                                 "--finger-cutout-depth", "6", "--json"],
                                     text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual(len(report["finger_cutouts"]), 2)
            self.assertEqual(report["finger_cutout"]["diameter"], 20)

    def test_cutouts_on_repaired_rotational_edges(self):
        from tests.test_symmetry import cyclic_corner, rotation_marker
        root = ET.fromstring(cyclic_corner("scale(3)"))
        root.find(f".//{{{SVG}}}path[@id='rotation']").set("d", rotation_marker(center=20.0002+20.0003j))
        graph = PanelGraph("generated.svg", _source_bytes=ET.tostring(root), snap_near=True)
        resized = graph.resized(width=70, length=50, height=15)
        report = resized.compile(io.BytesIO(), thickness=.4, finger_cutout="opposing")
        self.assertEqual([row["status"] for row in report["finger_cutouts"]], ["applied", "applied"])

    def test_interactive_cutout_options(self):
        from contextlib import redirect_stdout
        from boxxer.cli import interactive_session
        with tempfile.TemporaryDirectory() as directory:
            source, target = FilePath(directory)/"source.svg", FilePath(directory)/"out.svg"
            source.write_bytes(tray())
            answers = iter(["", "", "", "", "", "opposing", str(target), "", "y"])
            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            self.assertEqual(len(report["finger_cutouts"]), 2)


if __name__ == "__main__":
    unittest.main()
