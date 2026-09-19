import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import manifold3d as manifold
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
import trimesh

from boxxer import Design, SVG, path_data
from folded import Assembly, xyz

ROOT = Path(__file__).parent


class FoldedTests(unittest.TestCase):
    def repaired(self, name="drafts1.svg"):
        d = Design(ROOT / name)
        result = d.fix_scores(align_reliefs=True, max_move=0.6)
        self.assertTrue(result["complete"])
        return d

    def reimport(self, design):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.svg"
            design.write(source)
            return Design(source)

    def widen_lid(self, design, delta):
        lid = design.lid
        members = [s for s in design.shapes if s is lid.outline or
                   (s.color == "red" and lid.outline.geometry.distance(s.geometry) < 0.01)]
        cx = (lid.bounds[0]+lid.bounds[2])/2
        cy = (lid.bounds[1]+lid.bounds[3])/2
        for shape in members:
            shape.points = [(x+math.copysign(delta/2, x-cx), y+math.copysign(delta/2, y-cy))
                            for x, y in shape.points]
        return self.reimport(design)

    def test_panel_graph_and_hinge_continuity(self):
        for name, panels, folds in (("drafts1.svg", 18, 16), ("connected.svg", 15, 14)):
            assembly = Assembly(self.repaired(name))
            self.assertEqual(len(assembly.faces), panels)
            self.assertEqual(len(assembly.folds), folds)
            self.assertEqual(assembly.normalization, [])
            for fold in assembly.folds:
                a, b = assembly.faces[fold.a], assembly.faces[fold.b]
                hinge = [fold.start, fold.end]
                np.testing.assert_allclose(xyz(hinge, a.transform), xyz(hinge, b.transform), atol=1e-8)
                self.assertAlmostEqual(float(np.dot(a.normal, b.normal)), 0)
            for face in assembly.faces:
                self.assertAlmostEqual(float(np.linalg.det(face.transform[:3, :3])), 1)

    def test_separate_fit_and_layer_counts(self):
        r = Assembly(self.repaired()).report()
        self.assertEqual(r["thickness"], 0.5)
        self.assertEqual(len(r["tab_contacts"]), 8)
        self.assertTrue(all(c["status"] == "contact" for c in r["tab_contacts"]))
        self.assertEqual(r["interferences"], [])
        self.assertTrue(r["lid_fit"]["static_fit"])
        for gap in r["lid_fit"]["minimum_clearance_per_side"].values():
            self.assertAlmostEqual(gap, 0)
        self.assertAlmostEqual(r["lid_fit"]["rim_to_ceiling"], 0)
        self.assertAlmostEqual(r["part_envelopes"]["base"]["limiting_inner_plane_span"]["x"], 58.5)
        self.assertAlmostEqual(r["part_envelopes"]["base"]["limiting_inner_plane_span"]["y"], 36.5)
        for stack in r["wall_stacks"]:
            for region in stack["tab_regions"]:
                self.assertEqual(region["layers"], 2)
                self.assertAlmostEqual(region["envelope_thickness"], 1)

    def test_connected_closure_error_is_detected(self):
        a = Assembly(self.repaired("connected.svg"))
        r = a.report()
        self.assertEqual(len(r["tab_contacts"]), 6)
        self.assertFalse(r["lid_fit"]["static_fit"])
        self.assertAlmostEqual(r["lid_fit"]["rim_to_ceiling"], -0.25, delta=0.001)
        self.assertAlmostEqual(r["lid_fit"]["minimum_clearance_per_side"]["x"], -0.00009)
        self.assertAlmostEqual(r["lid_fit"]["minimum_clearance_per_side"]["y"], 1)
        lid_main = a.faces[a.roots["lid"]].id
        self.assertTrue(any(lid_main in c["panels"] for c in r["interferences"]))
        with self.assertRaisesRegex(ValueError, "hinges"):
            Assembly(self.repaired("connected.svg"), lid_lift=0.5)

    def test_thickness_changes_clearance_and_collision(self):
        design = self.repaired()
        r = Assembly(design, thickness=0.6).report()
        self.assertFalse(r["lid_fit"]["static_fit"])
        self.assertGreater(len(r["interferences"]), 0)
        self.assertTrue(all(c["status"] == "interference" for c in r["tab_contacts"]))
        self.assertAlmostEqual(r["lid_fit"]["minimum_clearance_per_side"]["x"], -0.1)
        design.fix_scores(thickness=0.6, align_reliefs=True, max_move=0.6)
        r = Assembly(design).report()
        self.assertEqual(r["thickness"], 0.6)
        self.assertTrue(all(c["status"] == "contact" for c in r["tab_contacts"]))
        self.assertFalse(r["lid_fit"]["static_fit"])

    def test_positive_clearance_and_lid_lift(self):
        d = self.widen_lid(self.repaired(), 0.4)
        r = Assembly(d, lid_lift=0.3).report()
        self.assertEqual(r["interferences"], [])
        self.assertTrue(r["lid_fit"]["static_fit"])
        for value in r["lid_fit"]["minimum_clearance_per_side"].values():
            self.assertAlmostEqual(value, 0.2)
        self.assertAlmostEqual(r["lid_fit"]["rim_to_ceiling"], 0.3)
        self.assertEqual(r["warnings"], [])

    def test_lid_above_walls_is_not_a_verified_nested_fit(self):
        r = Assembly(self.repaired(), lid_lift=50).report()
        self.assertFalse(r["lid_fit"]["static_fit"])
        self.assertTrue(all(v is None for v in r["lid_fit"]["minimum_clearance_per_side"].values()))

    def test_nested_clearance_keeps_its_sign_after_walls_pass(self):
        d = self.repaired("connected.svg")
        d.resize(connector_depth=15.5)
        cutoff = d.lid.bounds[3]
        for shape in d.shapes:
            shape.points = [(x+1 if y <= cutoff+1e-6 else x, y) for x, y in shape.points]
        r = Assembly(self.reimport(d)).report()
        self.assertAlmostEqual(r["lid_fit"]["minimum_clearance_per_side"]["x"], -0.99991)
        self.assertFalse(r["lid_fit"]["static_fit"])

    def test_notch_and_resizing_survive_folding(self):
        d = self.repaired()
        d.resize(height=25, width=80, length=50)
        before = Assembly(d).mesh("lid").volume
        d.notch()
        after = Assembly(self.reimport(d))
        expected_volume = (25*(15-12.5)+math.pi*12.5**2/2)*0.5
        self.assertAlmostEqual(before-after.mesh("lid").volume, expected_volume, delta=0.02)
        self.assertTrue(after.report()["lid_fit"]["static_fit"])
        self.assertEqual(len(after.faces), 18)

    def test_unrepaired_input_and_invalid_parameters(self):
        with self.assertRaisesRegex(ValueError, "fix"):
            Assembly(Design(ROOT / "connected.svg"))
        for value in (-1, 0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                Assembly(self.repaired(), thickness=value)
        with self.assertRaises(ValueError):
            Assembly(self.repaired(), lid_lift=-1)

    def test_connector_allowance_can_be_edited_and_preserved(self):
        d = self.repaired("connected.svg")
        d.resize(connector_depth=15.5)
        d = self.reimport(d)
        r = Assembly(d).report()
        self.assertAlmostEqual(r["lid_fit"]["rim_to_ceiling"], 0.25, delta=0.001)
        self.assertAlmostEqual(d.connector_depth, 15.5)
        allowance = d.connector_depth-d.base.height
        d.resize(height=25)
        self.assertAlmostEqual(d.connector_depth-25, allowance)
        with self.assertRaises(ValueError):
            self.repaired().resize(connector_depth=15.5)

    def test_exact_connected_fixture_with_adequate_connector(self):
        # Independent no-tab fixture: exact centered dimensions, 15 mm walls,
        # and a 15.5 mm back panel. Thickness is supplied, not inferred.
        parts = [box(100, 80, 160, 120), box(100.5, 135.5, 159.5, 173.5),
                 Polygon([(100, 120), (160, 120), (159.5, 135.5), (100.5, 135.5)]),
                 box(100, 65, 160, 80), box(85, 80.5, 100, 119.5), box(160, 80.5, 175, 119.5),
                 box(100.5, 173.5, 159.5, 188.5), box(85.5, 136, 100.5, 173), box(159.5, 136, 174.5, 173)]
        material = unary_union(parts)
        self.assertEqual(material.geom_type, "Polygon")
        root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
        ET.SubElement(root, f"{{{SVG}}}path", id="cut", stroke="black", fill="none", d=path_data(material.exterior.coords)+" Z")
        for name, x, y, w, h in (("lid", 100, 80, 60, 40), ("base", 100.5, 135.5, 59, 38)):
            ET.SubElement(root, f"{{{SVG}}}rect", id=name, x=str(x), y=str(y), width=str(w), height=str(h), stroke="red", fill="none")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exact.svg"
            ET.ElementTree(root).write(path)
            d = Design(path)
        a = Assembly(d, thickness=0.5)
        self.assertEqual(len(a.faces), 9)
        self.assertEqual(a.report()["interferences"], [])
        self.assertTrue(a.report()["lid_fit"]["static_fit"])
        self.assertAlmostEqual(a.report()["lid_fit"]["rim_to_ceiling"], 0.25)
        with self.assertRaisesRegex(ValueError, "Too few"):
            Assembly(d)

    def test_stl_exports_are_closed_and_preserve_volume(self):
        for name in ("drafts1.svg", "connected.svg"):
            a = Assembly(self.repaired(name))
            partitioned = manifold.Manifold.batch_boolean([f.solid for f in a.faces], manifold.OpType.Add)
            self.assertAlmostEqual(a.mesh().volume, partitioned.volume(), delta=1e-5)
            with tempfile.TemporaryDirectory() as directory:
                for role in (None, "base", "lid"):
                    path = Path(directory) / f"{role or 'assembly'}.stl"
                    mesh = a.mesh(role)
                    a.write_stl(path, role)
                    loaded = trimesh.load_mesh(path)
                    self.assertTrue(loaded.is_watertight)
                    self.assertTrue(loaded.is_winding_consistent)
                    self.assertAlmostEqual(loaded.volume, mesh.volume, delta=0.01)
                    np.testing.assert_allclose(loaded.bounds, mesh.bounds, atol=5e-6)

    def test_assemble_cli_and_folded_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, stl = root / "source.svg", root / "assembly.stl"
            self.repaired().write(source)
            command = [sys.executable, str(ROOT / "boxxer.py")]
            result = subprocess.run(command+["assemble", str(source), "--output", str(stl), "--base-output", str(root / "base.stl"), "--lid-output", str(root / "lid.stl"), "--json"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(stl.with_suffix(".json").read_text())
            self.assertTrue(report["assembly"]["lid_fit"]["static_fit"])
            self.assertEqual(len(report["assembly"]["panels"]), 18)
            for filename in ("assembly.stl", "base.stl", "lid.stl"):
                self.assertTrue(trimesh.load_mesh(root / filename).is_watertight)
            check = subprocess.run(command+["check", str(source), "--folded"], capture_output=True, text=True)
            self.assertEqual(check.returncode, 1)  # Zero sliding clearance is reported.
            self.widen_lid(self.repaired(), 0.4).write(source)
            check = subprocess.run(command+["check", str(source), "--folded", "--thickness", "auto"], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout+check.stderr)


if __name__ == "__main__":
    unittest.main()
