"""Preset preparation using generated sketches and symmetry constraints."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from boxxer.panelgraph import PanelGraph, SVG
from boxxer.prepare import prepare
from boxxer.presetmeta import read_metadata
from tests.test_corners import rounded_tray
from tests.test_fingercuts import tray
from tests.test_folds import quarter
from tests.test_symmetry import cyclic_corner, rotation_marker


class PrepareTests(unittest.TestCase):
    def prepare_source(self, source, **options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"sketch.svg"
            path.write_bytes(source)
            return prepare(path, **options)

    def test_rotational_seed_stays_compact_and_requires_no_repairs(self):
        root = ET.fromstring(cyclic_corner())
        marker = root.find(f".//{{{SVG}}}path[@id='rotation']")
        marker.set("d", rotation_marker(center=20.0002+20.0003j))
        # A deliberately slightly inaccurate diagonal must retain closed folds.
        root.find(f".//{{{SVG}}}path[@id='return']").set("d", "M10 6 V10 L6 6.000001 Z")
        data, report = self.prepare_source(ET.tostring(root), thickness=.4)
        self.assertGreater(report["repaired_seams"], 0)
        self.assertLessEqual(report["maximum_vertex_shift"], .01)
        prepared = ET.fromstring(data)
        self.assertEqual(len(prepared.findall(f".//{{{SVG}}}path")), len(root.findall(f".//{{{SVG}}}path")))
        self.assertIsNotNone(prepared.find(f".//{{{SVG}}}path[@id='rotation']"))
        graph = PanelGraph("prepared.svg", _source_bytes=data)
        self.assertEqual(graph.near_seams, [])
        self.assertEqual(graph.contact_warnings, [])
        self.assertEqual(len(graph.panels), 13)
        self.assertEqual(graph.preset_metadata["status"], "ready")
        self.assertEqual(graph.preset_defaults, {"thickness": .4})
        result = graph.resized(width=50, length=50, height=50).compile(io.BytesIO(), finger_cutout="opposing")
        self.assertEqual(result["metadata"]["thickness"], .4)
        self.assertEqual(len(result["finger_cutouts"]), 2)

    def test_mirror_axes_and_translucent_construction_are_preserved(self):
        root = ET.fromstring(quarter())
        root.find(f".//{{{SVG}}}path[@id='mirror-x']").set("d", "M20.0002 0 V40")
        construction = ET.SubElement(root, f"{{{SVG}}}path", id="construction", d="invalid", opacity=".3")
        data, report = self.prepare_source(ET.tostring(root))
        prepared = ET.fromstring(data)
        self.assertEqual(prepared.find(f".//{{{SVG}}}path[@id='construction']").attrib, construction.attrib)
        self.assertEqual(report["preset"]["mirror_axes"], 2)
        self.assertEqual(PanelGraph("prepared.svg", _source_bytes=data).near_seams, [])

    def test_repeated_preparation_is_idempotent_and_metadata_detects_edits(self):
        root = ET.fromstring(tray())
        root.find(f".//{{{SVG}}}rect[@id='east']").set("x", "150.0002")
        data, _ = self.prepare_source(ET.tostring(root), thickness=.4)
        again, _ = self.prepare_source(data)
        self.assertEqual(data, again)
        root = ET.fromstring(data)
        ET.indent(root)
        self.assertEqual(read_metadata(ET.fromstring(ET.tostring(root)))["status"], "ready")
        node = root.find(f".//{{{SVG}}}path[@id='east']")
        node.set("transform", "translate(.1,0)")
        graph = PanelGraph("edited.svg", _source_bytes=ET.tostring(root))
        self.assertEqual(graph.preset_metadata["status"], "stale")
        self.assertEqual(graph.preset_defaults, {})
        self.assertTrue(any("stale" in warning for warning in graph.report()["warnings"]))

    def test_repair_keeps_transforms_and_editable_corner_radius(self):
        root = ET.fromstring(rounded_tray())
        east = root.find(f".//{{{SVG}}}path[@id='east']")
        east.set("transform", "translate(.0002,0)")
        data, _ = self.prepare_source(ET.tostring(root))
        prepared = ET.fromstring(data)
        self.assertEqual(prepared.find(f".//{{{SVG}}}path[@id='east']").get("transform"), "translate(.0002,0)")
        graph = PanelGraph("prepared.svg", _source_bytes=data)
        self.assertEqual(graph.near_seams, [])
        self.assertAlmostEqual(graph.report()["editable_corners"][0]["radius"], 5)
        graph.compile(io.BytesIO())

    def test_partial_fold_and_receiving_slot_keep_ownership_and_width(self):
        from tests.test_flaps import flap_shapes
        root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
        group = ET.SubElement(root, f"{{{SVG}}}g", transform="translate(5,10)")
        shapes = flap_shapes(14, 14)
        shapes[-1][1]["x"] = "90.0002"
        shapes[-1][1]["fill"] = "#eaeaea"
        shapes.append(("rect", {"id": "hole", "x": "60", "y": "60", "width": ".001", "height": "1"}))
        for tag, attributes in shapes:
            ET.SubElement(group, f"{{{SVG}}}{tag}", attributes)
        data, _ = self.prepare_source(ET.tostring(root))
        graph = PanelGraph("prepared.svg", _source_bytes=data)
        self.assertEqual(graph.near_seams, [])
        hole = next(shape for panel in graph.panels for shape in panel.internal if shape.id == "hole")
        self.assertAlmostEqual(hole.polygon.bounds[2]-hole.polygon.bounds[0], .001)
        self.assertTrue(next(panel for panel in graph.panels if panel.id == "tab-south").outside_hint)
        self.assertEqual(ET.fromstring(data).find(f"{{{SVG}}}g").get("transform"), "translate(5,10)")

    def test_wrong_fold_angles_still_fail_readiness(self):
        root = ET.fromstring(cyclic_corner())
        root.find(f".//{{{SVG}}}path[@id='diagonal']").set("d", "M10 10 L6 6")
        with self.assertRaisesRegex(ValueError, "fold closure"):
            self.prepare_source(ET.tostring(root))

    def test_cli_in_place_and_failed_preparation_leave_source_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"sketch.svg"
            source.write_bytes(tray())
            command = [sys.executable, "-m", "boxxer", "prepare", str(source), "--in-place", "--thickness", ".4", "--json"]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "ready")
            self.assertEqual(PanelGraph(source).preset_defaults["thickness"], .4)
            invalid = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 H20"/></svg>'
            source.write_bytes(invalid)
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_bytes(), invalid)


if __name__ == "__main__":
    unittest.main()
