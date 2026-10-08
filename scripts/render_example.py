"""Render the README's sketch-to-plotter example from actual Boxxer output.

Run from the repository root: uv run python scripts/render_example.py
"""
import copy
import io
from pathlib import Path
import xml.etree.ElementTree as ET

from boxxer.panelgraph import PanelGraph, SVG, matrix, shape_path
from svgpathtools.path import transform as transform_path

ROOT = Path(__file__).resolve().parents[1]


def element(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{SVG}}}{tag}", {key.replace("_", "-"): str(value) for key, value in attributes.items()})


def text(parent, x, y, value, size=16, colour="#64748b", weight="400"):
    node = element(parent, "text", x=x, y=y, font_size=size, fill=colour, font_weight=weight)
    node.text = value


def bounds(root):
    boxes = []

    def visit(node, parent):
        tag = node.tag.split("}")[-1]
        if tag in ("defs", "metadata", "namedview"):
            return
        transform = parent@matrix(node.get("transform", ""))
        if tag in ("rect", "path", "line", "circle", "ellipse", "polygon", "polyline"):
            path = transform_path(shape_path(node), transform)
            if path:
                boxes.append(path.bbox())
        for child in node:
            visit(child, transform)

    visit(root, matrix(""))
    x0, x1 = min(b[0] for b in boxes), max(b[1] for b in boxes)
    y0, y1 = min(b[2] for b in boxes), max(b[3] for b in boxes)
    pad = 6
    return f"{x0-pad:g} {y0-pad:g} {x1-x0+pad*2:g} {y1-y0+pad*2:g}"


def diagram(parent, drawing, x):
    nested = element(parent, "svg", x=x, y=180, width=500, height=350,
                     viewBox=bounds(drawing), preserveAspectRatio="xMidYMid meet")
    for child in drawing:
        if child.tag.split("}")[-1] not in ("metadata", "namedview"):
            nested.append(copy.deepcopy(child))


def main():
    source = ROOT/"presets"/"tray_gusset_self-locking_pinwheel.svg"
    graph = PanelGraph(source, snap_near=True)
    output = io.BytesIO()
    report = graph.compile(output, thickness=.4, finger_cutout="opposing")
    (ROOT/"examples"/"pinwheel-tray.svg").write_bytes(output.getvalue())
    compiled = ET.fromstring(output.getvalue())
    draft = ET.parse(source).getroot()
    for group in compiled.findall(f"{{{SVG}}}g"):
        group.set("stroke-width", "0.45")
        group.set("stroke-linejoin", "round")
        group.set("stroke-linecap", "round")
    picture = ET.Element(f"{{{SVG}}}svg", width="1200", height="650", viewBox="0 0 1200 650",
                         attrib={"font-family": "Arial, Helvetica, sans-serif"})
    title = element(picture, "title")
    title.text = "Boxxer: a rotational sketch becomes complete cut and score paths"
    description = element(picture, "desc")
    description.text = "Left: one corner with a 90-degree repeat marker. Right: the expanded tray with two finger cutouts, black cuts, red front scores, and blue back scores."
    element(picture, "rect", width=1200, height=650, rx=20, fill="#f1f5f9")
    text(picture, 32, 40, "FROM SKETCH TO PLOTTER", 12, "#64748b", "700")
    text(picture, 32, 77, "Draw a corner. Get the whole box.", 28, "#0f172a", "700")
    for x in (24, 626):
        element(picture, "rect", x=x, y=112, width=550, height=466, rx=16, fill="#ffffff", stroke="#e2e8f0")
    text(picture, 48, 148, "01  Sketch", 21, "#0f172a", "700")
    text(picture, 650, 148, "02  Compiled", 21, "#0f172a", "700")
    diagram(picture, draft, 48)
    diagram(picture, compiled, 650)
    element(picture, "circle", cx=600, cy=342, r=20, fill="#0f172a")
    element(picture, "path", d="M590 342 H610 M604 336 L610 342 L604 348", fill="none", stroke="#ffffff", stroke_width=2)
    text(picture, 48, 556, "One corner + a 90° rotational marker", 15)
    text(picture, 650, 556, "Four corners + material allowance + finger cutouts", 15)
    text(picture, 32, 615, "100 × 100 × 20 mm tray · 0.4 mm stock", 15, "#475569")
    for x, colour, label in ((650, "#0f172a", "Cut"), (775, "#ff0000", "Front score"), (962, "#0000ff", "Back score")):
        attributes = {"stroke_dasharray": "5 4"} if label == "Back score" else {}
        element(picture, "line", x1=x, y1=610, x2=x+28, y2=610, stroke=colour, stroke_width=2, **attributes)
        text(picture, x+38, 615, label, 14, "#475569")
    asset = ROOT/"docs"/"assets"/"sketch-to-compiled.svg"
    asset.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(picture).write(asset, encoding="utf-8", xml_declaration=True)
    print(f"Rendered {asset.relative_to(ROOT)} from {source.relative_to(ROOT)}: "
          f"{len(report['panels'])} panels, {len(report['folds'])} scores, {report['internal_cuts']} slots")


if __name__ == "__main__":
    main()
