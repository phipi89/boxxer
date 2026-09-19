import json
import math
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from boxxer import Design
from inspection import inspect_design, mm, repair_fit
from terminal import layout_diagram, render_report

ROOT = Path(__file__).parent


class InspectionTests(unittest.TestCase):
    def repaired(self, name="connected.svg"):
        d = Design(ROOT / name)
        d.fix_scores(align_reliefs=True, max_move=0.6)
        return d

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "boxxer.py"), *map(str, args)], capture_output=True, text=True)

    def test_connected_names_and_problem_groups(self):
        report = inspect_design(self.repaired())
        self.assertEqual(report["status"], "fails")
        self.assertEqual([i["kind"] for i in report["issues"]], ["lid_ceiling", "lid_side"])
        self.assertAlmostEqual(report["issues"][0]["distance_mm"], 0.2503)
        self.assertAlmostEqual(report["issues"][1]["distance_mm"], 0.00009)
        model = report["assembly"]
        names = {p["name"] for p in model["panels"]}
        self.assertIn("Base bottom", names)
        self.assertIn("Lid top", names)
        self.assertIn("Shared back / hinge panel", names)
        self.assertEqual(len(names), 15)
        self.assertEqual(sum(p["kind"] == "tab" for p in model["panels"]), 6)
        self.assertTrue(all("name" in c for c in model["folds"]))
        text = render_report(report)
        self.assertIn("Lid right wall overlaps Base right wall by 0.00009 mm", text)
        self.assertNotIn("panel-", text)
        self.assertNotIn("mm³", text)
        self.assertIn("mm³", render_report(report, details=True))

    def test_directional_overlap_for_known_parallel_contacts(self):
        report = inspect_design(self.repaired("drafts1.svg"), thickness=0.52)
        model = report["assembly"]
        contact = model["tab_contacts"][0]
        collision = next(c for c in model["interferences"] if set(c["panels"]) == {contact["tab"], contact["wall"]})
        self.assertAlmostEqual(collision["overlap_depth"], 0.02)
        self.assertEqual(collision["axis"], "y")
        issue = next(i for i in report["issues"] if contact["crease"] in i["creases"])
        self.assertIn("crease", issue["title"])
        self.assertIn("0.02 mm", issue["message"])
        self.assertIn("required for contact: 0.52", issue["details"][0])

    def test_diagram_includes_every_tab_and_crease_error(self):
        for source in ("connected.svg", "drafts1.svg"):
            report = inspect_design(self.repaired(source), thickness=0.52)
            for width in (88, 66, 42):
                diagram = layout_diagram(report["assembly"], report["issues"], width=width)
                # Panel labels may wrap across box lines. All distinguishing
                # role/corner labels and the glue-tab kind remain visible.
                for p in report["assembly"]["panels"]:
                    if p["kind"] == "tab":
                        self.assertIn(p["direction"], diagram)
                if width >= 66:
                    self.assertEqual(len(re.findall(r"\btab\b", diagram)), 6 if source == "connected.svg" else 8)
                else:
                    # In connection view a panel is boxed exactly once; link
                    # labels may repeat names, but no panel may be omitted.
                    self.assertEqual(sum(line.startswith("┌") for line in diagram.splitlines()), len(report["assembly"]["panels"]))
            contact_issue = next(i for i in report["issues"] if i["kind"] == "tab_contact")
            diagram = layout_diagram(report["assembly"], report["issues"], width=88)
            marker = f"[{contact_issue['id']}]"
            self.assertTrue(any(marker in line and "┄" in line for line in diagram.splitlines()))
            ascii_diagram = render_report(report, ascii_only=True, width=42)
            self.assertTrue(ascii_diagram.isascii())

    def test_default_inspection_checks_assembly_and_flat_is_explicit(self):
        source = ROOT / "examples/connected-fixed.svg"
        normal = self.cli("inspect", source, "--json")
        legacy = self.cli("inspect", source, "--folded", "--json")
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(json.loads(normal.stdout), json.loads(legacy.stdout))
        self.assertEqual(json.loads(normal.stdout)["status"], "fails")
        check = self.cli("check", source)
        self.assertEqual(check.returncode, 1)
        flat = self.cli("inspect", source, "--flat")
        self.assertIn("ASSEMBLY NOT CHECKED", flat.stdout)
        self.assertNotIn("PASSES", flat.stdout)
        self.assertNotIn("3D assembly checked", flat.stdout)
        self.assertEqual(self.cli("check", source, "--flat").returncode, 0)
        self.assertEqual(self.cli("inspect", source, "--flat", "--folded").returncode, 2)

    def test_blocked_assembly_is_not_a_pass(self):
        report = inspect_design(Design(ROOT / "connected.svg"))
        self.assertEqual(report["status"], "not_checked")
        self.assertEqual(report["checks"]["assembly"], "not_checked")
        self.assertIn("assembly_blocked", [i["kind"] for i in report["issues"]])
        text = render_report(report)
        self.assertIn("Assembly not checked", text)
        self.assertIn("Base left-back glue tab crease", text)
        self.assertNotIn("path5-", text)
        self.assertEqual(self.cli("check", ROOT / "connected.svg").returncode, 1)

    def test_fit_repair_only_changes_supported_dimension_and_verifies(self):
        d = self.repaired()
        dimensions = [(p.width, p.length, p.height) for p in d.panels]
        r = repair_fit(d, 0.5, top_clearance=0.2)
        self.assertEqual(dimensions, [(p.width, p.length, p.height) for p in d.panels])
        self.assertEqual(len(r["changes"]), 1)
        self.assertEqual(r["changes"][0]["parameter"], "connector_depth")
        self.assertAlmostEqual(d.connector_depth, 15.4503)
        report = inspect_design(d, 0.5)
        self.assertAlmostEqual(report["assembly"]["lid_fit"]["rim_to_ceiling"], 0.2)
        self.assertNotIn("lid_ceiling", [i["kind"] for i in report["issues"]])
        self.assertFalse(r["complete"])  # Horizontal misregistration remains.
        self.assertEqual([i["kind"] for i in report["issues"]], ["lid_side"])
        self.assertEqual(repair_fit(d, 0.5, top_clearance=0.2)["changes"], [])

    def test_fix_post_check_and_fit_option(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixed.svg"
            logfile = Path(directory) / "fixes.json"
            cmd = ("fix", ROOT / "connected.svg", "--align-reliefs", "--max-move", "0.6",
                   "--output", output, "--report", logfile, "--json")
            scores_only = self.cli(*cmd)
            self.assertEqual(scores_only.returncode, 1)
            data = json.loads(scores_only.stdout)
            self.assertTrue(data["repair"]["complete"])
            self.assertEqual(data["status"], "fails")
            self.assertAlmostEqual(Design(output).connector_depth, 15)
            fit = self.cli(*cmd, "--fit", "--top-clearance", "0.2")
            self.assertEqual(fit.returncode, 1, fit.stderr)
            data = json.loads(fit.stdout)
            self.assertEqual(data, json.loads(logfile.read_text()))
            self.assertFalse(data["fit_repair"]["complete"])
            self.assertAlmostEqual(Design(output).connector_depth, 15.4503)
            self.assertEqual(self.cli(*cmd, "--top-clearance", "0.2").returncode, 2)
            for invalid in ("-1", "nan", "inf"):
                self.assertEqual(self.cli(*cmd, "--fit", "--top-clearance", invalid).returncode, 2)

    def test_fit_repair_refuses_to_claim_pose_changes_as_svg_fixes(self):
        d = self.repaired("drafts1.svg")
        with self.assertRaisesRegex(ValueError, "connected"):
            repair_fit(d, 0.5, top_clearance=0.2)
        self.assertEqual(repair_fit(d, 0.5)["changes"], [])
        result = repair_fit(Design(ROOT / "connected.svg"), 0.5)
        self.assertFalse(result["complete"])
        self.assertEqual(result["changes"], [])

    def test_fit_repair_completes_when_side_clearance_already_exists(self):
        d = self.repaired()
        cutoff = d.lid.bounds[3]
        center = (d.lid.bounds[0]+d.lid.bounds[2])/2
        # Independent design edit: give the lid 0.2 mm additional clearance on
        # each side, keeping the base and tab offsets unchanged.
        for shape in d.shapes:
            shape.points = [(x+math.copysign(0.2, x-center) if y <= cutoff+1e-6 else x, y)
                            for x, y in shape.points]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wider.svg"
            output = Path(directory) / "fitted.svg"
            d.write(source)
            result = self.cli("fix", source, "--align-reliefs", "--fit", "--top-clearance", "0.2", "--output", output, "--json")
            self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
            report = json.loads(result.stdout)
            self.assertTrue(report["fit_repair"]["complete"])
            self.assertEqual(report["issues"], [])
            self.assertEqual(inspect_design(Design(output))["status"], "passes")

    def test_small_distances_remain_visible_and_zero_is_not_negative(self):
        self.assertEqual(mm(-1e-12), "0")
        self.assertEqual(mm(0.00009), "0.00009")
        self.assertEqual(mm(0.02), "0.02")
        self.assertEqual(mm(0.2503), "0.2503")


if __name__ == "__main__":
    unittest.main()
