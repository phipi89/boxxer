"""Matching telescoping lids preserve construction and regenerate fit once."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import ezdxf

from boxxer.cli import interactive_session
from boxxer.panelgraph import PanelGraph, SVG
from tests.test_features import graph_from_shapes, tray_shapes
from tests.test_interactions import flap_receiver
from tests.test_corners import rounded_tray
from tests.test_symmetry import cyclic_corner
from tests.test_reinforcements import reinforced_box


class MatchingLidTests(unittest.TestCase):
    def test_plain_tray_has_two_nets_and_base_stays_requested_size(self):
        source = graph_from_shapes(tray_shapes())
        generated = source.with_lid()
        self.assertEqual(len(source.components), 1)
        self.assertEqual(len(generated.components), 2)
        output = io.BytesIO()
        report = generated.compile(output, thickness=.4, lid_clearance=.2, finger_cutout="opposing")
        self.assertEqual(report["cut_contours"], 2)
        self.assertEqual(report["generated_lid"]["base"], "face")
        def hinge(first, second):
            return next(row["length"] for row in report["folds"] if set(row["panels"]) == {first, second})
        self.assertAlmostEqual(hinge("face", "north"), 40)
        self.assertAlmostEqual(hinge("lid-face", "lid-north"), 41.2)
        self.assertTrue(all(cut["panel"].startswith("lid-") for cut in report["finger_cutouts"]))
        again = io.BytesIO()
        restored = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        restored.compile(again)
        self.assertEqual(output.getvalue(), again.getvalue())
        with self.assertRaisesRegex(ValueError, "existing lid"):
            restored.with_lid()

    def test_cyclic_tray_copies_angles_openings_and_two_ply_gap(self):
        source = PanelGraph("generated.svg", _source_bytes=cyclic_corner())
        generated = source.with_lid(lid_height=3).resized(width=28, length=24, base_height=7, lid_height=3)
        self.assertEqual(len(generated.panels), 26)
        self.assertEqual(sum(len(panel.internal) for panel in generated.panels), 8)
        self.assertEqual(len(generated.opening_interactions), 8)
        # Compare angle counts instead of assuming component ordering.
        for angle in (-180, 90, 180):
            self.assertEqual(sum(seam.angle == angle for seam in generated.seams),
                             2*sum(seam.angle == angle for seam in source.seams))
        output = io.BytesIO()
        report = generated.compile(output, thickness=.1)
        self.assertEqual(report["cut_contours"], 2)
        self.assertTrue(all(row["gap_layers"] == 2 for row in report["lid_fit"]["walls"]))
        self.assertTrue(all(abs(row["midplane_offset"]-.3) < 1e-7 for row in report["lid_fit"]["walls"]))
        reimported = PanelGraph("compiled.svg", _source_bytes=output.getvalue())
        self.assertEqual(len(reimported.components), 2)
        self.assertEqual(len(reimported.opening_interactions), 8)
        reimported.compile(io.BytesIO())

    def test_locking_flap_and_corner_effects_survive_copy(self):
        for source in (flap_receiver(), PanelGraph("generated.svg", _source_bytes=rounded_tray())):
            generated = source.with_lid()
            original = source.report()["editable_corners"]
            copied = generated.report()["editable_corners"]
            self.assertEqual(len(copied), len(original)*2)
            for panel in source.panels:
                for cut in panel.internal:
                    twins = [shape for owner in generated.panels for shape in owner.internal
                             if shape.id in (cut.id, "lid-"+cut.id)]
                    self.assertEqual(len(twins), 2)
                    self.assertTrue(all(abs(shape.polygon.area-cut.polygon.area) < 1e-6 for shape in twins))
            generated.compile(io.BytesIO(), thickness=.1)

    def test_page_overflow_reports_without_scaling(self):
        source = graph_from_shapes([
            ("rect", {"id": identifier, "x": str(x), "y": str(y), "width": str(w), "height": str(h)})
            for identifier, x, y, w, h in [("face", 30, 30, 100, 100), ("north", 30, 10, 100, 20),
                                          ("south", 30, 130, 100, 20), ("left", 10, 30, 20, 100),
                                          ("right", 130, 30, 20, 100)]])
        report = source.with_lid().compile(io.BytesIO(), thickness=.4)
        self.assertTrue(report["page_overflow"])
        row = next(row for row in report["folds"] if set(row["panels"]) == {"face", "north"})
        self.assertAlmostEqual(row["length"], 100)

    def test_reinforced_tray_copies_returns_and_fits_distinct_heights(self):
        root = ET.fromstring(reinforced_box())
        group = root.find(f"{{{SVG}}}g")
        for node in list(group):
            if node.get("id", "").startswith("lid"):
                group.remove(node)
        source = PanelGraph("generated.svg", _source_bytes=ET.tostring(root))
        generated = source.with_lid().resized(width=80, length=60, base_height=25, lid_height=15)
        self.assertEqual(len(generated.reinforcements), 6)
        for prefix, depth in (("", 12.5), ("lid-", 7.5)):
            panel = next(panel for panel in generated.panels if panel.id == prefix+"return-left")
            self.assertAlmostEqual(panel.polygon.bounds[2]-panel.polygon.bounds[0], depth)
        output = io.BytesIO()
        report = generated.compile(output, thickness=.4)
        self.assertEqual(report["cut_contours"], 2)
        self.assertTrue(all(row["gap_layers"] == 1 for row in report["lid_fit"]["walls"]))
        again = io.BytesIO()
        PanelGraph("compiled.svg", _source_bytes=output.getvalue()).compile(again)
        self.assertEqual(output.getvalue(), again.getvalue())

    def test_cli_and_interactive_replay_produce_matching_outputs(self):
        with tempfile.TemporaryDirectory(prefix="boxxer matching lids ") as directory:
            source, output, editable = Path(directory)/"tray.svg", Path(directory)/"pair.svg", Path(directory)/"editable.svg"
            source.write_bytes(graph_from_shapes(tray_shapes()).source_bytes)
            answers = iter(["y", "60", "50", "15", "10", ".4", "", ".2", "opposing", str(output), str(editable), "y"])
            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            self.assertEqual(report["cut_contours"], 2)
            self.assertIn("--with-lid", report["equivalent_command"])
            expected = output.read_bytes()
            args = shlex.split(report["equivalent_command"])
            process = subprocess.run([sys.executable, "-m", "boxxer", *args[3:]], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(output.read_bytes(), expected)
            self.assertEqual(len(PanelGraph(editable).components), 2)
            dxf = Path(directory)/"pair.dxf"
            process = subprocess.run([sys.executable, "-m", "boxxer", "compile", str(source), "--with-lid",
                                      "--lid-height", "8", "--thickness", ".4", "--output", str(dxf), "--json"],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["cut_contours"], 2)
            self.assertEqual(ezdxf.readfile(dxf).units, 4)

    def test_existing_box_is_not_cloned(self):
        existing = PanelGraph("generated.svg", _source_bytes=reinforced_box())
        with self.assertRaisesRegex(ValueError, "without an existing lid"):
            existing.with_lid()


if __name__ == "__main__":
    unittest.main()
