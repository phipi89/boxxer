import io
import unittest
import xml.etree.ElementTree as ET

from svgpathtools import Arc, Line, parse_path

from boxxer.corners import INKSCAPE
from boxxer.panelgraph import BOXXER, SVG, PanelGraph


def rounded_tray():
    root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
    defs = ET.SubElement(root, f"{{{SVG}}}defs")
    ET.SubElement(defs, f"{{{INKSCAPE}}}path-effect", {
        "effect": "fillet_chamfer", "id": "corner", "is_visible": "true", "lpeversion": "1",
        "nodesatellites_param": "F,0,0,1,0,0,0,1 @ F,0,0,1,0,5,0,1 @ "
                                "F,0,0,1,0,0,0,1 @ F,0,0,1,0,0,0,1",
        "radius": "0", "unit": "px", "method": "auto", "mode": "F", "chamfer_steps": "1",
        "flexible": "false", "use_knot_distance": "true", "apply_no_radius": "true",
        "apply_with_radius": "true", "only_selected": "false", "hide_knots": "false",
    })
    shapes = [
        ("rect", {"id": "face", "x": "50", "y": "50", "width": "40", "height": "30"}),
        ("rect", {"id": "north", "x": "50", "y": "40", "width": "40", "height": "10"}),
        ("path", {"id": "east", "d": "M90 50 H95 A5 5 0 0 1 100 55 V80 H90 Z",
                  f"{{{INKSCAPE}}}path-effect": "#corner",
                  f"{{{INKSCAPE}}}original-d": "M90 50 H100 V80 H90 Z"}),
        ("rect", {"id": "south", "x": "50", "y": "80", "width": "40", "height": "10"}),
        ("rect", {"id": "west", "x": "40", "y": "50", "width": "10", "height": "30"}),
    ]
    for tag, attributes in shapes:
        ET.SubElement(root, f"{{{SVG}}}{tag}", attributes)
    return ET.tostring(root)


class CornerEffectTests(unittest.TestCase):
    def test_original_path_drives_topology(self):
        graph = PanelGraph("rounded.svg", _source_bytes=rounded_tray())
        east = next(panel for panel in graph.panels if panel.id == "east")
        self.assertTrue(all(isinstance(segment, Line) for segment in east.path))
        self.assertEqual(len(graph.seams), 4)
        self.assertAlmostEqual(east.corner_treatments[1].radius, 5)
        self.assertEqual(graph.report()["editable_corners"], [
            {"shape": "east", "vertex": 1, "radius": 5.0},
        ])

    def test_resize_preserves_live_effect_and_physical_radius(self):
        resized = PanelGraph("rounded.svg", _source_bytes=rounded_tray()).resized(
            width=60, length=50, height=20)
        root = ET.fromstring(resized.source_bytes)
        east = root.find(f".//{{{SVG}}}path[@id='east']")
        self.assertIsNotNone(east.get(f"{{{INKSCAPE}}}path-effect"))
        self.assertIsNotNone(east.get(f"{{{INKSCAPE}}}original-d"))
        rendered = parse_path(east.get("d"))
        arc = next(segment for segment in rendered if isinstance(segment, Arc))
        self.assertAlmostEqual(arc.radius.real, 5)
        restored = PanelGraph("resized.svg", _source_bytes=resized.source_bytes)
        self.assertAlmostEqual(restored._resize_structure()["dimensions"]["height"], 20)
        restored_east = next(panel for panel in restored.panels if panel.id == "east")
        self.assertAlmostEqual(restored_east.corner_treatments[1].radius, 5)

    def test_compiled_contour_has_editable_effect_and_round_trips(self):
        graph = PanelGraph("rounded.svg", _source_bytes=rounded_tray())
        output = io.BytesIO()
        report = graph.compile(output, thickness=0.5)
        root = ET.fromstring(output.getvalue())
        cut = root.find(f".//{{{SVG}}}path[@id='cut-0']")
        self.assertIsNotNone(cut.get(f"{{{INKSCAPE}}}path-effect"))
        self.assertIsNotNone(cut.get(f"{{{INKSCAPE}}}original-d"))
        self.assertTrue(any(isinstance(segment, Arc) for segment in parse_path(cut.get("d"))))
        effects = root.findall(f".//{{{INKSCAPE}}}path-effect")
        self.assertEqual(len(effects), 1)
        self.assertEqual(report["metadata"]["editable_corners"], [
            {"shape": "east", "vertex": 1, "radius": 5.0},
        ])
        model = root.findtext(f".//{{{BOXXER}}}model")
        self.assertIn('"editable_corners"', model)
        recompiled = io.BytesIO()
        PanelGraph("compiled.svg", _source_bytes=output.getvalue()).compile(recompiled)
        self.assertEqual(output.getvalue(), recompiled.getvalue())

    def test_live_control_distance_is_recomputed_after_source_scaling(self):
        root = ET.fromstring(rounded_tray())
        group = ET.Element(f"{{{SVG}}}g", transform="scale(2)")
        for child in list(root):
            if child.tag != f"{{{SVG}}}defs":
                root.remove(child)
                group.append(child)
        root.append(group)
        graph = PanelGraph("scaled.svg", _source_bytes=ET.tostring(root))
        east = next(panel for panel in graph.panels if panel.id == "east")
        self.assertAlmostEqual(east.corner_treatments[1].radius, 10)
        output = io.BytesIO()
        graph.compile(output)
        effect = ET.fromstring(output.getvalue()).find(f".//{{{INKSCAPE}}}path-effect")
        active = [entry for entry in effect.get("nodesatellites_param").split(" @ ")
                  if abs(float(entry.split(",")[5])) > 1e-8]
        self.assertEqual(len(active), 1)
        self.assertAlmostEqual(float(active[0].split(",")[5]), 10)

    def test_internal_cut_keeps_visible_and_editable_radius(self):
        root = ET.fromstring(rounded_tray())
        effect = ET.SubElement(root.find(f"{{{SVG}}}defs"), f"{{{INKSCAPE}}}path-effect", {
            "effect": "fillet_chamfer", "id": "internal-corner", "is_visible": "true",
            "lpeversion": "1", "nodesatellites_param":
                "F,0,0,1,0,0,0,1 @ F,0,0,1,0,2,0,1 @ "
                "F,0,0,1,0,0,0,1 @ F,0,0,1,0,0,0,1",
            "radius": "0", "unit": "px", "method": "auto", "mode": "F", "chamfer_steps": "1",
            "flexible": "false", "use_knot_distance": "true", "apply_no_radius": "true",
            "apply_with_radius": "true", "only_selected": "false", "hide_knots": "false",
        })
        self.assertIsNotNone(effect)
        ET.SubElement(root, f"{{{SVG}}}path", {"id": "opening",
            "d": "M60 60 H68 A2 2 0 0 1 70 62 V70 H60 Z",
            f"{{{INKSCAPE}}}path-effect": "#internal-corner",
            f"{{{INKSCAPE}}}original-d": "M60 60 H70 V70 H60 Z"})
        graph = PanelGraph("internal.svg", _source_bytes=ET.tostring(root))
        output = io.BytesIO()
        graph.compile(output)
        compiled = ET.fromstring(output.getvalue())
        opening = compiled.find(f".//{{{SVG}}}path[@id='internal-opening']")
        self.assertTrue(any(isinstance(segment, Arc) for segment in parse_path(opening.get("d"))))
        self.assertIsNotNone(opening.get(f"{{{INKSCAPE}}}path-effect"))
        self.assertIsNotNone(opening.get(f"{{{INKSCAPE}}}original-d"))

    def test_radius_must_still_fit_after_resize(self):
        graph = PanelGraph("rounded.svg", _source_bytes=rounded_tray())
        with self.assertRaisesRegex(ValueError, "radius no longer fits"):
            graph.resized(height=4)

    def test_unsupported_live_effect_is_rejected(self):
        root = ET.fromstring(rounded_tray())
        root.find(f".//{{{INKSCAPE}}}path-effect").set("effect", "bend")
        with self.assertRaisesRegex(ValueError, "unsupported Inkscape path effect"):
            PanelGraph("unsupported.svg", _source_bytes=ET.tostring(root))

    def test_inconsistent_or_degenerate_effect_is_rejected(self):
        root = ET.fromstring(rounded_tray())
        effect = root.find(f".//{{{INKSCAPE}}}path-effect")
        effect.set("nodesatellites_param", "F,0,0,1,0,0,0,1 @ F,0,0,1,0,0,0,1 @ "
                                            "F,0,0,1,0,5,0,1 @ F,0,0,1,0,0,0,1")
        with self.assertRaisesRegex(ValueError, "does not meet its original-d edges"):
            PanelGraph("inconsistent.svg", _source_bytes=ET.tostring(root))

        root = ET.fromstring(rounded_tray())
        east = root.find(f".//{{{SVG}}}path[@id='east']")
        east.set("d", "M90 50 H94 A5 5 0 0 1 100 57 V80 H90 Z")
        with self.assertRaisesRegex(ValueError, "not the circular fillet"):
            PanelGraph("stale-render.svg", _source_bytes=ET.tostring(root))

        root = ET.fromstring(rounded_tray())
        east = root.find(f".//{{{SVG}}}path[@id='east']")
        east.set(f"{{{INKSCAPE}}}original-d", "M90 50 H95 H100 V80 H90 Z")
        effect = root.find(f".//{{{INKSCAPE}}}path-effect")
        effect.set("nodesatellites_param", "F,0,0,1,0,0,0,1 @ F,0,0,1,0,5,0,1 @ "
                                            "F,0,0,1,0,0,0,1 @ F,0,0,1,0,0,0,1 @ "
                                            "F,0,0,1,0,0,0,1")
        with self.assertRaisesRegex(ValueError, "non-collinear"):
            PanelGraph("degenerate.svg", _source_bytes=ET.tostring(root))


if __name__ == "__main__":
    unittest.main()
