"""SVG drawing semantics, independent of project sketches."""
import io
import unittest
import xml.etree.ElementTree as ET

from boxxer.panelgraph import PanelGraph, SVG
from tests.test_features import graph_from_shapes, tray_shapes
from tests.test_flaps import flap_shapes


def source(extra="", group="", panel=""):
    return (f'<svg xmlns="{SVG}" width="100mm" height="100mm" viewBox="0 0 100 100">'
            f'<g {group}><rect id="material" x="10" y="10" width="20" height="20" {panel}/></g>'
            f'{extra}</svg>').encode()


def graph(data):
    return PanelGraph("generated.svg", _source_bytes=data)


class SketchStyleTests(unittest.TestCase):
    def test_only_explicit_solid_fill_marks_outside(self):
        cases = [("", "", False), ("", 'stroke-dasharray="1,1"', False),
                 ("", 'fill="none"', False), ("", 'fill="#eaeaea"', True),
                 ('fill="red"', "", True), ('fill="red"', 'fill="none"', False),
                 ('fill="red"', 'fill="inherit"', True),
                 ("", 'fill="red" style="fill:none"', False),
                 ("", 'fill="none" style="fill:blue"', True),
                 ("", 'fill="red" style="fill-opacity:.2;stroke-opacity:.1"', True),
                 ("", 'fill="url(#gradient)"', False)]
        for group, panel, expected in cases:
            with self.subTest(group=group, panel=panel):
                self.assertEqual(graph(source(group=group, panel=panel)).panels[0].outside_hint, expected)

    def test_translucent_shapes_and_guides_are_ignored_before_validation(self):
        for opacity in ("0", ".5", ".999999", "99%"):
            extra = (f'<g opacity="{opacity}"><path d="M0 0 H10"/>'
                     '<path d="invalid" transform="invalid(1)"/>'
                     '<rect x="10" y="10" width="20" height="20"/></g>')
            g = graph(source(extra))
            self.assertEqual([p.id for p in g.panels], ["material"])
            self.assertEqual(g.fold_guides, [])
            self.assertEqual(g.overlaps, [])
        # Opacity inherited through a group cannot be cancelled by a child.
        extra = '<g style="opacity:50%"><g opacity="1"><path d="invalid"/></g></g>'
        self.assertEqual(len(graph(source(extra)).panels), 1)
        extra = ('<path d="invalid" opacity=".9"/>'
                 '<rect x="10" y="10" width="20" height="20" style="opacity:.9"/>'
                 '<path d="M0 0 H50" style="opacity:.9;stroke-dasharray:4,2,1,2"/>'
                 '<path d="M40 20 A20 20 0 0 1 20 40" style="opacity:.9;stroke-dasharray:4,2,1,2"/>')
        g = graph(source(extra))
        self.assertEqual(len(g.panels), 1)
        self.assertEqual(g.mirror_axes, [])
        self.assertEqual(g.rotational_symmetry, [])

    def test_css_opacity_precedence_and_full_opacity(self):
        for attributes in ('opacity="1"', 'opacity="100%"', 'style="opacity:100%"',
                           'opacity=".3" style="opacity:1"'):
            self.assertEqual(len(graph(source(panel=attributes)).panels), 1)
        with self.assertRaisesRegex(ValueError, "No closed"):
            graph(source(panel='opacity="1" style="opacity:.99"'))
        with self.assertRaisesRegex(ValueError, "opacity"):
            graph(source(panel='opacity="nan"'))

    def test_filled_outside_tab_survives_resize_and_recompile(self):
        shapes = flap_shapes(14, 14)
        shapes[-1][1]["fill"] = "#eaeaea"
        g = graph_from_shapes(shapes)
        outside = {panel.id for panel in g.panels if panel.outside_hint}
        self.assertTrue(outside)
        self.assertEqual(next(c.placement for c in g.contacts if c.tab in outside), "outside")
        resized = g.resized(width=120, length=90, height=25)
        self.assertEqual({p.id for p in resized.panels if p.outside_hint}, outside)
        root = ET.fromstring(resized.source_bytes)
        for identifier in outside:
            node = root.find(f"{{{SVG}}}path[@id='{identifier}']")
            self.assertEqual(node.get("fill"), "#eaeaea")
            self.assertIsNone(node.get("stroke-dasharray"))
        output = io.BytesIO()
        resized.compile(output, thickness=.4)
        reimported = graph(output.getvalue())
        self.assertEqual({p.id for p in reimported.panels if p.outside_hint}, outside)


if __name__ == "__main__":
    unittest.main()
