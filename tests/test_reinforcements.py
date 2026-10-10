"""Mixed 90° glue joints and 180° returns, using generated assemblies."""
import io
import unittest
import xml.etree.ElementTree as ET
from svgpathtools import Line, parse_path


from boxxer.panelgraph import PanelGraph, SVG
from boxxer.interactions import folded_transforms, planar, polygon_transform, surface_seams
from boxxer.corners import INKSCAPE, CornerTreatment, DEFAULT_SATELLITE, lpe_attributes, rounded_path
from boxxer.reinforcements import validate_layers


def reinforced_box(*, angle=180, overlap=False, transform="", top_return=False):
    root = ET.Element(f"{{{SVG}}}svg", width="300mm", height="320mm", viewBox="0 0 300 320")
    group = ET.SubElement(root, f"{{{SVG}}}g", transform=transform, fill="none", stroke="#000000")
    rectangles = [
        ("lid", 50, 50, 100, 80), ("lid-front", 50, 30, 100, 20),
        ("lid-left", 30, 50, 20, 80), ("lid-right", 150, 50, 20, 80),
        ("connector", 50, 130, 100, 20), ("base", 50, 150, 100, 80),
        ("base-left", 30, 150, 20, 80), ("base-right", 150, 150, 20, 80),
        ("base-front", 50, 230, 100, 20),
        ("lid-tab-left", 33, 30, 17, 17), ("lid-tab-right", 150, 30, 17, 17),
        ("base-tab-left", 33, 233, 17, 17), ("base-tab-right", 150, 233, 17, 17),
        ("back-tab-left", 30, 133, 17, 17), ("back-tab-right", 153, 133, 17, 17),
        ("return-left", 20, 150, 10, 80 if overlap else 62),
        ("return-right", 170, 150, 10, 62), ("return-front", 50, 250, 100, 10)]
    if top_return:
        rectangles.append(("return-lid", 20, 68, 10, 62))
    for identifier, x, y, width, height in rectangles:
        ET.SubElement(group, f"{{{SVG}}}rect", id=identifier, x=str(x), y=str(y), width=str(width), height=str(height))
    folds = [("left", "M30 150 V"+str(230 if overlap else 212)+" Z"),
             ("right", "M170 150 V212 Z"), ("front", "M50 250 H150 Z")]
    if top_return:
        folds.append(("lid", "M30 68 V130 Z"))
    for identifier, path in folds:
        ET.SubElement(group, f"{{{SVG}}}path", id="fold-return-"+identifier, d=path,
                      **{"stroke-dasharray": "1,1" if angle < 0 else "none"})
    return ET.tostring(root)


def graph(**options):
    return PanelGraph("generated.svg", _source_bytes=reinforced_box(**options))


def shaped_lid(*, fillet=True, subdivided=False, reflected=False):
    transform = "translate(250,0) scale(-1,1)" if reflected else ""
    root = ET.fromstring(reinforced_box(top_return=True, transform=transform))
    group = root.find(f"{{{SVG}}}g")
    group.remove(group.find(f"{{{SVG}}}rect[@id='lid-left']"))
    sharp = parse_path("M30 50 H50 V130 L30 126"+(" V122" if subdivided else "")+" Z")
    attributes = {"id": "lid-left", "d": sharp.d()}
    if fillet:
        treatment = CornerTreatment(3, DEFAULT_SATELLITE, (("effect", "fillet_chamfer"), ("mode", "F")))
        effect, extra = lpe_attributes({3: treatment}, sharp, "wall-corners")
        definitions = ET.SubElement(root, f"{{{SVG}}}defs")
        ET.SubElement(definitions, f"{{{INKSCAPE}}}path-effect", effect)
        attributes.update(extra)
        attributes[f"{{{INKSCAPE}}}original-d"] = sharp.d()
        attributes["d"] = rounded_path(sharp, {3: treatment}).d()
    ET.SubElement(group, f"{{{SVG}}}path", attributes)
    group.find(f"{{{SVG}}}rect[@id='return-lid']").set("height", "52")
    group.find(f"{{{SVG}}}path[@id='fold-return-lid']").set("d", "M30 68 V120 Z")
    return PanelGraph("generated.svg", _source_bytes=ET.tostring(root))


def attachment_limit(assembly, identifier, paths=None):
    parent = next(panel for panel in assembly.panels if panel.id == identifier)
    path = paths[identifier] if paths else parent.path
    boundary = rounded_path(path, parent.corner_treatments)
    minimum = min(segment.start.real for segment in path)
    maximum = max(segment.start.real for segment in path)
    limits = {}
    for segment in boundary:
        if (isinstance(segment, Line) and abs(segment.start.real-segment.end.real) < 1e-8 and
                (abs(segment.start.real-minimum) < 1e-8 or abs(segment.start.real-maximum) < 1e-8)):
            key = round(segment.start.real, 7)
            limits[key] = max(limits.get(key, -float("inf")), segment.start.imag, segment.end.imag)
    # The shaped outer edge terminates before the full-height inner edge.
    return min(limits.values())


def footprint(assembly, identifier, receiver):
    import numpy as np
    panel = next(panel for panel in assembly.panels if panel.id == identifier)
    relative = np.linalg.inv(assembly._folded_transforms[receiver])@assembly._folded_transforms[identifier]
    return polygon_transform(panel.polygon, planar(relative))


class ReinforcementTests(unittest.TestCase):
    def test_returns_do_not_disable_glue_contacts(self):
        source = graph()
        self.assertEqual(len(source.contacts), 6)
        self.assertEqual(len(source.reinforcements), 3)
        self.assertEqual(source.contact_warnings, [])
        self.assertTrue(all(binding["placement"] == "inside" for binding in source.reinforcements.values()))
        self.assertEqual(surface_seams(source), {seam.id for seam in source.seams if abs(seam.angle) == 180})

    def test_uncompensated_collision_is_resolved_in_final_assembly(self):
        source = graph()
        paths = {panel.id: panel.path for panel in source.panels}
        scores = {seam.id: seam.curve for seam in source.seams}
        with self.assertRaisesRegex(ValueError, "layers overlap"):
            validate_layers(source, paths, scores, .4, 0)
        output = io.BytesIO()
        report = source.compile(output, thickness=.4)
        self.assertEqual(len(report["compensation"]), 6)
        self.assertEqual(len(report["reinforcements"]), 3)
        self.assertTrue(all(abs(row["residual"]) < 1e-6 for row in source.compensated_contacts(source.compensation_plan(.4))))
        recompiled = io.BytesIO()
        PanelGraph("compiled.svg", _source_bytes=output.getvalue()).compile(recompiled)
        self.assertEqual(output.getvalue(), recompiled.getvalue())

    def test_return_clearance_tracks_glue_reach_on_height_and_length_changes(self):
        source = graph()
        for options in ({"height": 30}, {"length": 60}, {"width": 80, "length": 60, "height": 30}):
            target = source.resized(**options)
            lining = footprint(target, "return-left", "base-left")
            glue = footprint(target, "base-tab-left", "base-left")
            self.assertAlmostEqual(glue.bounds[1]-lining.bounds[3], 1)
            wall = next(panel for panel in target.panels if panel.id == "base-left")
            self.assertAlmostEqual(lining.bounds[0], wall.polygon.bounds[0])
            part = next(panel for panel in target.panels if panel.id == "return-left")
            self.assertAlmostEqual(part.polygon.bounds[2]-part.polygon.bounds[0],
                                   15 if options.get("height") == 30 else 10)
            target.compile(io.BytesIO(), thickness=.4)

    def test_true_overlap_is_not_hidden(self):
        with self.assertRaisesRegex(ValueError, "layers overlap"):
            graph(overlap=True).compile(io.BytesIO(), thickness=.4)

    def test_outward_returns_contribute_to_lid_gap(self):
        inward, outward = graph(), graph(angle=-180)
        self.assertEqual(outward.reinforcements["return-left"]["placement"], "outside")
        first = next(row for row in inward.lid_fit_plan(.4)["walls"] if row["side"] == "left")
        second = next(row for row in outward.lid_fit_plan(.4)["walls"] if row["side"] == "left")
        self.assertAlmostEqual(second["midplane_offset"]-first["midplane_offset"], .4)
        outward.compile(io.BytesIO(), thickness=.4)

    def test_disjoint_lid_returns_and_tabs_share_one_gap_layer(self):
        source = graph(top_return=True)
        row = next(row for row in source.lid_fit_plan(.4)["walls"] if row["side"] == "left")
        self.assertEqual(row["gap_layers"], 1)
        source.compile(io.BytesIO(), thickness=.4)

    def test_reflected_source_keeps_contacts_and_clearances(self):
        source = graph(transform="translate(250,0) scale(-1,1)")
        self.assertEqual(len(source.contacts), 6)
        target = source.resized(width=80, length=60, height=30)
        self.assertTrue(all(abs(row["clearance"]-1) < 1e-7 for row in target.resize_metadata["return_clearances"]))
        target.compile(io.BytesIO(), thickness=.4)

    def test_chamfered_filleted_and_subdivided_wall_end_clearance(self):
        for fillet, subdivided, reflected in ((False, False, False), (True, False, False),
                                              (True, True, False), (True, True, True)):
            with self.subTest(fillet=fillet, subdivided=subdivided, reflected=reflected):
                source = shaped_lid(fillet=fillet, subdivided=subdivided, reflected=reflected)
                original = footprint(source, "return-lid", "lid-left")
                gap = attachment_limit(source, "lid-left")-original.bounds[3]
                target = source.resized(width=80, length=60, height=30)
                lining = footprint(target, "return-lid", "lid-left")
                glue = footprint(target, "lid-tab-left", "lid-left")
                self.assertAlmostEqual(attachment_limit(target, "lid-left")-lining.bounds[3], gap)
                self.assertAlmostEqual(lining.bounds[1]-glue.bounds[3], 1)
                target.compile(io.BytesIO(), thickness=.4)

    def test_lid_fitting_preserves_return_gaps_and_roundtrips(self):
        import numpy as np
        source = shaped_lid()
        source_end = footprint(source, "return-lid", "lid-left").bounds[3]
        gap = attachment_limit(source, "lid-left")-source_end
        _, scores, _, _, _, _, _, paths, _ = source._compensated_geometry(.4, 0, 0)
        poses = folded_transforms(source, paths, scores, .4)
        relative = planar(np.linalg.inv(poses["lid-left"])@poses["return-lid"])
        from boxxer.interactions import polygon_for_path
        lining = polygon_transform(polygon_for_path(paths["return-lid"]), relative)
        glue_pose = planar(np.linalg.inv(poses["lid-left"])@poses["lid-tab-left"])
        glue = polygon_transform(polygon_for_path(paths["lid-tab-left"]), glue_pose)
        self.assertAlmostEqual(lining.bounds[1]-glue.bounds[3], 1)
        self.assertAlmostEqual(attachment_limit(source, "lid-left", paths)-lining.bounds[3], gap)
        output = io.BytesIO()
        source.compile(output, thickness=.4)
        again = io.BytesIO()
        PanelGraph("compiled.svg", _source_bytes=output.getvalue()).compile(again)
        self.assertEqual(output.getvalue(), again.getvalue())


if __name__ == "__main__":
    unittest.main()
