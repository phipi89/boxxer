import math
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from boxxer import Design

ROOT = Path(__file__).parent


class BoxTests(unittest.TestCase):
    def design(self, name="connected.svg"):
        return Design(ROOT / name)

    def roundtrip(self, design):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.svg"
            design.write(path)
            return Design(path)

    def test_measurements_and_corrected_front(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            self.assertAlmostEqual(d.lid.width, 60)
            self.assertAlmostEqual(d.base.width, 59)
            self.assertAlmostEqual(d.base.height, 15, delta=0.001)
            self.assertAlmostEqual(d.base.outline.geometry.bounds[3]-d.base.bounds[3], 15, delta=0.001)

    def test_identity(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            before = [s.geometry for s in d.shapes]
            d.resize()
            for a, b in zip(before, d.shapes):
                self.assertLess(a.hausdorff_distance(b.geometry), 1e-8)

    def test_linked_height_and_connector(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            d.resize(height=25)
            after = self.roundtrip(d)
            for p in after.panels:
                self.assertAlmostEqual(p.height, 25, delta=0.001)
            self.assertEqual(after.report()["lid_minus_base"], [1, 2])
            if after.connected:
                self.assertAlmostEqual(after.base.bounds[1]-after.lid.bounds[3], 25, delta=0.001)

    def test_independent_heights(self):
        d = self.design()
        d.resize(base_height=25)
        d = self.roundtrip(d)
        self.assertAlmostEqual(d.lid.height, 15, delta=0.001)
        d.resize(height=30)
        d = self.roundtrip(d)
        self.assertAlmostEqual(d.base.height, 30, delta=0.001)
        self.assertAlmostEqual(d.lid.height, 15, delta=0.001)
        d.resize(height=35, lid_height=20)
        d = self.roundtrip(d)
        self.assertAlmostEqual(d.lid.height, 20, delta=0.001)

    def test_footprint(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            d.resize(width=80, length=50, height=25)
            d = self.roundtrip(d)
            self.assertAlmostEqual(d.base.width, 80, delta=0.001)
            self.assertAlmostEqual(d.base.length, 50, delta=0.001)
            self.assertAlmostEqual(d.lid.width, 81, delta=0.001)
            self.assertAlmostEqual(d.lid.length, 52, delta=0.001)

    def test_measures_changed_source_margins(self):
        d = self.design("drafts1.svg")
        base = d.base
        shapes = [s for s in d.shapes if s is base.outline or
                  (s.color == "red" and base.outline.geometry.distance(s.geometry) < 0.1)]
        x0, y0, x1, y1 = base.bounds
        # Simulate a source edit: narrower/shorter base, unchanged walls and lid.
        for s in shapes:
            s.points = [(x+0.5-min(1, max(0, (x-x0)/(x1-x0))),
                         y-2*min(1, max(0, (y-y0)/(y1-y0)))) for x, y in s.points]
        d = self.roundtrip(d)
        self.assertEqual(d.report()["lid_minus_base"], [2, 4])
        d.resize(width=80, length=50)
        d = self.roundtrip(d)
        self.assertAlmostEqual(d.lid.width, 82, delta=0.001)
        self.assertAlmostEqual(d.lid.length, 54, delta=0.001)

    def test_connected_tab_translation(self):
        d = self.design()
        tab = next(s for s in d.shapes if s.id == "path5-7-9")
        before = list(tab.points)
        d.resize(height=25)
        for a, b in zip(before, tab.points):
            self.assertAlmostEqual(b[1]-a[1], 10, delta=0.001)

    def test_notch_geometry_and_clearance(self):
        for height, expected_depth in ((15, 13), (25, 15)):
            d = self.design()
            d.resize(height=height)
            area = d.lid.outline.geometry.area
            info = d.notch()
            self.assertAlmostEqual(info["actual_depth"], expected_depth, delta=0.001)
            self.assertGreaterEqual(info["fold_clearance"], 2-1e-8)
            self.assertIn(" A ", d.lid.outline.output_d)
            after = self.roundtrip(d)
            removed = area-after.lid.outline.geometry.area
            expected = 25*(info["actual_depth"]-12.5)+math.pi*12.5**2/2
            self.assertAlmostEqual(removed, expected, delta=0.01)
            self.assertTrue(after.lid.outline.geometry.is_valid)

    def test_impossible_notch(self):
        d = self.design()
        d.resize(height=14)
        with self.assertRaisesRegex(ValueError, "semicircular notch"):
            d.notch()
        with self.assertRaises(ValueError):
            d.notch(width=100)

    def test_validation(self):
        d = self.design()
        self.assertEqual(len(d.report()["warnings"]), 4)
        d.resize(height=25)
        self.assertEqual(len(d.report()["warnings"]), 4)
        with self.assertRaises(ValueError):
            self.design().resize(height=-1)
        with self.assertRaises(ValueError):
            self.design().resize(width=-1)

    def test_thickness_inference_and_contact_errors(self):
        for name in ("connected.svg", "drafts1.svg"):
            analysis = self.design(name).score_analysis()
            self.assertEqual(analysis["thickness"], 0.5)
            contacts = {c["score"]: c for c in analysis["contacts"]}
            self.assertAlmostEqual(contacts["path5"]["contact_gap"], 0.100169)
            if name == "drafts1.svg":
                self.assertAlmostEqual(contacts["path5-7"]["contact_gap"], -0.09986)

    def test_thickness_is_measured_not_hardcoded(self):
        d = self.design()
        for c in d.tab_contacts():
            c["shape"].points = [(x, y+c["sign"]*0.1) for x, y in c["shape"].points]
        self.assertEqual(d.score_analysis()["thickness"], 0.6)
        for index, c in enumerate(d.tab_contacts()):
            c["shape"].points = [(x, c["anchor"]+c["sign"]*(0.2+index*0.1)) for x, _ in c["shape"].points]
        with self.assertRaisesRegex(ValueError, "majority"):
            d.score_analysis()
        self.assertEqual(d.score_analysis(0.7)["thickness"], 0.7)

    def test_full_score_and_relief_repair(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            panels_before = [list(p.shape.points) for p in d.panels]
            result = d.fix_scores(align_reliefs=True, max_move=0.6)
            self.assertTrue(result["complete"], result)
            self.assertTrue(any(c["cut_changes"] for c in result["changes"]))
            self.assertEqual(panels_before, [p.shape.points for p in d.panels])
            d = self.roundtrip(d)
            self.assertEqual(d.report(scores=True)["warnings"], [])
            self.assertTrue(all(s.geometry.is_valid for s in d.outlines))
            second = d.fix_scores(align_reliefs=True, max_move=0.6)
            self.assertTrue(second["complete"])
            self.assertEqual(second["changes"], [])

    def test_score_only_repair_is_honest_and_preserves_cuts(self):
        d = self.design()
        before = [list(s.points) for s in d.outlines]
        result = d.fix_scores()
        self.assertFalse(result["complete"])
        self.assertTrue(result["changes"])
        self.assertTrue(result["unresolved"])
        self.assertEqual(before, [s.points for s in d.outlines])

    def test_repair_movement_limit_and_explicit_thickness(self):
        d = self.design()
        before = [list(s.points) for s in d.shapes]
        result = d.fix_scores(max_move=1e-7, align_reliefs=True)
        self.assertFalse(result["complete"])
        self.assertEqual(before, [s.points for s in d.shapes])
        d = self.design()
        result = d.fix_scores(thickness=0.55, align_reliefs=True, max_move=0.7)
        self.assertTrue(result["complete"], result)
        self.assertEqual(d.score_analysis(0.55)["issues"], [])
        for value in (-1, 0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.design().fix_scores(thickness=value)
            with self.assertRaises(ValueError):
                self.design().fix_scores(max_move=value)

    def test_repaired_design_still_resizes(self):
        for name in ("connected.svg", "drafts1.svg"):
            d = self.design(name)
            d.fix_scores(align_reliefs=True, max_move=0.6)
            d.resize(height=25, width=80, length=50)
            d = self.roundtrip(d)
            self.assertEqual(d.score_analysis(0.5)["issues"], [])
            self.assertEqual(d.report()["warnings"], [])

    def test_fix_cli_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "fixed.svg"
            report = Path(directory) / "repair.json"
            source = ROOT / "connected.svg"
            source_bytes = source.read_bytes()
            command = [sys.executable, str(ROOT / "boxxer.py"), "fix", str(source),
                       "--output", str(target), "--report", str(report), "--json"]
            partial = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(partial.returncode, 1, partial.stderr)
            self.assertFalse(json.loads(partial.stdout)["repair"]["complete"])
            complete = subprocess.run(command+["--align-reliefs", "--max-move", "0.6"], capture_output=True, text=True)
            # Score repairs pass, but fix now verifies the assembled box too.
            self.assertEqual(complete.returncode, 1, complete.stderr)
            self.assertTrue(json.loads(report.read_text())["repair"]["complete"])
            self.assertEqual(json.loads(report.read_text())["status"], "fails")
            checked = subprocess.run([sys.executable, str(ROOT / "boxxer.py"), "check", str(target), "--flat", "--scores"], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout+checked.stderr)
            refused = subprocess.run(command+["--report", str(source)], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(source.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
