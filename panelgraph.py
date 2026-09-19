"""Generic SVG panel graph and manufacturing-path compiler.

Input shapes describe individual pieces of material. Coincident boundary
intervals become folds; every remaining boundary becomes a cut. Strictly
contained shapes are retained as internal cuts owned by the smallest panel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
import json
import math
from pathlib import Path as FilePath
import re
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from svgpathtools import Arc, CubicBezier, Line, Path, QuadraticBezier, parse_path
from svgpathtools.path import transform as transform_path

SVG = "http://www.w3.org/2000/svg"
BOXXER = "https://boxxer.local/ns/1"
ET.register_namespace("", SVG)
ET.register_namespace("boxxer", BOXXER)
NUMERIC_EPS = 1e-8
DEFAULT_NEAR = 0.01


def numbers(text: str) -> list[float]:
    return [float(s) for s in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", text)]


def length_mm(text: str) -> float:
    match = re.fullmatch(r"\s*([\d.eE+-]+)(mm|cm|in|px|pt)?\s*", text)
    if not match:
        raise ValueError(f"Unsupported SVG length: {text}")
    return float(match[1])*{None: 25.4/96, "px": 25.4/96, "mm": 1,
                            "cm": 10, "in": 25.4, "pt": 25.4/72}[match[2]]


def matrix(text: str) -> np.ndarray:
    result = np.eye(3)
    for name, raw in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        values = numbers(raw)
        operation = np.eye(3)
        if name == "matrix" and len(values) == 6:
            a, b, c, d, e, f = values
            operation = np.array([[a, c, e], [b, d, f], [0, 0, 1]], dtype=float)
        elif name == "translate" and 1 <= len(values) <= 2:
            operation[0, 2] = values[0]
            operation[1, 2] = values[1] if len(values) == 2 else 0
        elif name == "scale" and 1 <= len(values) <= 2:
            operation[0, 0] = values[0]
            operation[1, 1] = values[1] if len(values) == 2 else values[0]
        elif name == "rotate" and len(values) in (1, 3):
            angle = math.radians(values[0])
            rotation = np.array([[math.cos(angle), -math.sin(angle), 0],
                                 [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
            if len(values) == 3:
                cx, cy = values[1:]
                operation = matrix(f"translate({cx},{cy})")@rotation@matrix(f"translate({-cx},{-cy})")
            else:
                operation = rotation
        elif name == "skewX" and len(values) == 1:
            operation[0, 1] = math.tan(math.radians(values[0]))
        elif name == "skewY" and len(values) == 1:
            operation[1, 0] = math.tan(math.radians(values[0]))
        else:
            raise ValueError(f"Unsupported SVG transform: {name}({raw})")
        # SVG transform functions are applied in written order.
        result = result@operation
    return result


def style_map(element, inherited: dict[str, str]) -> dict[str, str]:
    result = dict(inherited)
    for key in ("display", "visibility", "stroke", "stroke-dasharray", "fill"):
        if key in element.attrib:
            result[key] = element.attrib[key]
    result.update(dict(part.strip().split(":", 1) for part in element.get("style", "").split(";") if ":" in part))
    return result


def shape_path(element) -> Path:
    tag = element.tag.split("}")[-1]
    if tag == "path":
        return parse_path(element.get("d", ""))
    if tag == "line":
        return Path(Line(complex(float(element.get("x1", 0)), float(element.get("y1", 0))),
                              complex(float(element.get("x2", 0)), float(element.get("y2", 0)))))
    if tag in ("polygon", "polyline"):
        values = numbers(element.get("points", ""))
        points = [complex(x, y) for x, y in zip(values[::2], values[1::2])]
        if tag == "polygon" and points and points[-1] != points[0]:
            points.append(points[0])
        return Path(*(Line(a, b) for a, b in zip(points, points[1:])))
    if tag == "rect":
        x, y = float(element.get("x", 0)), float(element.get("y", 0))
        w, h = float(element.get("width", 0)), float(element.get("height", 0))
        rx_value = element.get("rx")
        ry_value = element.get("ry")
        rx = float(rx_value) if rx_value is not None else float(ry_value or 0)
        ry = float(ry_value) if ry_value is not None else float(rx_value or 0)
        rx, ry = min(abs(rx), w/2), min(abs(ry), h/2)
        if rx <= 0 or ry <= 0:
            points = [complex(x, y), complex(x+w, y), complex(x+w, y+h), complex(x, y+h), complex(x, y)]
            return Path(*(Line(a, b) for a, b in zip(points, points[1:])))
        return parse_path(f"M {x+rx},{y} H {x+w-rx} A {rx},{ry} 0 0 1 {x+w},{y+ry} "
                          f"V {y+h-ry} A {rx},{ry} 0 0 1 {x+w-rx},{y+h} H {x+rx} "
                          f"A {rx},{ry} 0 0 1 {x},{y+h-ry} V {y+ry} A {rx},{ry} 0 0 1 {x+rx},{y} Z")
    if tag in ("circle", "ellipse"):
        cx, cy = float(element.get("cx", 0)), float(element.get("cy", 0))
        rx = float(element.get("r", element.get("rx", 0)))
        ry = float(element.get("r", element.get("ry", 0)))
        return parse_path(f"M {cx-rx},{cy} A {rx},{ry} 0 1 0 {cx+rx},{cy} A {rx},{ry} 0 1 0 {cx-rx},{cy} Z")
    raise ValueError(f"Unsupported SVG shape: {tag}")


def sample_segment(segment, resolution=0.03) -> list[tuple[float, float]]:
    count = 1 if isinstance(segment, Line) else max(8, math.ceil(segment.length(error=1e-5)/resolution))
    return [(segment.point(i/count).real, segment.point(i/count).imag) for i in range(count+1)]


def polygon_for(path: Path) -> Polygon:
    points = []
    for segment in path:
        sampled = sample_segment(segment)
        points.extend(sampled[:-1])
    if path:
        points.append((path[-1].end.real, path[-1].end.imag))
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.area <= NUMERIC_EPS:
        raise ValueError("Shape is not a valid closed material boundary")
    return polygon


def dashed(style: dict[str, str]) -> bool:
    value = style.get("stroke-dasharray", "").strip().lower()
    return value not in ("", "none", "0")


@dataclass
class SourceShape:
    id: str
    path: Path
    polygon: Polygon
    outside_hint: bool
    order: int


@dataclass
class Panel:
    id: str
    path: Path
    polygon: Polygon
    outside_hint: bool = False
    internal: list[SourceShape] = field(default_factory=list)


@dataclass
class Seam:
    id: str
    panel_a: str
    panel_b: str
    curve: object
    segment_a: int
    segment_b: int
    interval_a: tuple[float, float]
    interval_b: tuple[float, float]
    discrepancy: float
    repaired: bool

    @property
    def length(self):
        return self.curve.length(error=1e-6)

    @property
    def start(self):
        return self.curve.start

    @property
    def end(self):
        return self.curve.end


def replace_line(path: Path, index: int, start: complex, end: complex):
    segments = list(path)
    segments[index] = Line(start, end)
    return Path(*segments)


def move_segment_endpoint(segment, *, start=None, end=None):
    """Move only a join point while retaining the native segment type."""
    start = segment.start if start is None else start
    end = segment.end if end is None else end
    ds, de = start-segment.start, end-segment.end
    if isinstance(segment, Line):
        return Line(start, end)
    if isinstance(segment, CubicBezier):
        return CubicBezier(start, segment.control1+ds, segment.control2+de, end)
    if isinstance(segment, QuadraticBezier):
        return QuadraticBezier(start, segment.control+(ds+de)/2, end)
    if isinstance(segment, Arc):
        return Arc(start, segment.radius, segment.rotation, segment.large_arc,
                   segment.sweep, end, autoscale_radius=True)
    raise ValueError(f"Unsupported path segment: {type(segment).__name__}")


class PanelGraph:
    def __init__(self, filename, near=DEFAULT_NEAR, snap_near=False):
        if not math.isfinite(near) or near <= 0:
            raise ValueError("Near-edge distance must be finite and positive")
        self.filename = FilePath(filename)
        self.near = near
        self.snap_near = snap_near
        root = ET.parse(filename).getroot()
        self.page = (length_mm(root.get("width", "210mm")), length_mm(root.get("height", "297mm")))
        viewbox = numbers(root.get("viewBox", f"0 0 {self.page[0]} {self.page[1]}"))
        if len(viewbox) != 4 or min(viewbox[2:]) <= 0:
            raise ValueError("Invalid SVG viewBox")
        sx, sy = self.page[0]/viewbox[2], self.page[1]/viewbox[3]
        page_matrix = np.array([[sx, 0, -viewbox[0]*sx], [0, sy, -viewbox[1]*sy], [0, 0, 1]])
        shapes = []

        def visit(element, parent_matrix, inherited):
            tag = element.tag.split("}")[-1]
            if tag in ("defs", "metadata", "namedview"):
                return
            current_style = style_map(element, inherited)
            if current_style.get("display") == "none" or current_style.get("visibility") == "hidden":
                return
            current_matrix = parent_matrix@matrix(element.get("transform", ""))
            if tag in ("path", "rect", "line", "polygon", "polyline", "circle", "ellipse"):
                path = shape_path(element)
                if not path:
                    return
                path = transform_path(path, current_matrix)
                path = Path(*(segment for segment in path if abs(segment.end-segment.start) > NUMERIC_EPS))
                if not path or abs(path[0].start-path[-1].end) > NUMERIC_EPS:
                    raise ValueError(f"{element.get('id', tag)} is open; panel-source geometry must be closed")
                path[-1] = move_segment_endpoint(path[-1], end=path[0].start)
                shape = SourceShape(element.get("id", f"shape-{len(shapes)}"), path, polygon_for(path),
                                    dashed(current_style), len(shapes))
                shapes.append(shape)
            for child in element:
                visit(child, current_matrix, current_style)

        visit(root, page_matrix, {})
        if not shapes:
            raise ValueError("No closed SVG shapes found")
        owners = {}
        for child in shapes:
            containers = [candidate for candidate in shapes if candidate is not child
                          and candidate.polygon.contains(child.polygon)
                          and candidate.polygon.boundary.distance(child.polygon) > NUMERIC_EPS]
            if containers:
                owners[child.id] = min(containers, key=lambda s: s.polygon.area)
        self.panels = [Panel(s.id, s.path, s.polygon, s.outside_hint) for s in shapes if s.id not in owners]
        panel_by_id = {p.id: p for p in self.panels}
        for shape in shapes:
            if shape.id in owners:
                owner = owners[shape.id]
                while owner.id in owners:
                    owner = owners[owner.id]
                panel_by_id[owner.id].internal.append(shape)
        self.near_seams = []
        self.seams = []
        self._detect_seams()
        self.overlaps = self._overlaps()
        self.components = self._components()

    def _detect_seams(self):
        seam_index = 0
        for first, second in combinations(self.panels, 2):
            for ia, a in enumerate(first.path):
                for ib, b in enumerate(second.path):
                    match = self._line_match(a, b) if isinstance(a, Line) and isinstance(b, Line) else self._curve_match(a, b)
                    if match is None:
                        continue
                    (ta0, ta1), (tb0, tb1), curve, distance = match
                    start, end = curve.start, curve.end
                    tangent = curve.derivative(0.5)
                    direction = tangent/abs(tangent)
                    normal = complex(-direction.imag, direction.real)
                    midpoint = curve.point(0.5)
                    probe = min(self.near*2, curve.length(error=1e-6)/10)
                    def occupied(panel, sign):
                        point = midpoint+normal*probe*sign
                        return panel.polygon.buffer(self.near/10).covers(Point(point.real, point.imag))
                    sides_a = {sign for sign in (-1, 1) if occupied(first, sign)}
                    sides_b = {sign for sign in (-1, 1) if occupied(second, sign)}
                    if not sides_a or not sides_b or any(sign in sides_b for sign in sides_a):
                        # Coincident boundaries with material on the same side
                        # are overlap evidence, never a fold.
                        continue
                    repaired = distance > NUMERIC_EPS
                    row = {"panels": [first.id, second.id], "segments": [ia, ib],
                           "length": abs(end-start), "distance": distance}
                    if repaired:
                        self.near_seams.append(row)
                        if not self.snap_near:
                            continue
                    self.seams.append(Seam(f"fold-{seam_index}", first.id, second.id, curve,
                                           ia, ib, (ta0, ta1), (tb0, tb1), distance, repaired))
                    seam_index += 1

    def _curve_match(self, a, b):
        """Recognize a complete exact curved seam, in either direction.

        Partial and near-coincident curved seams need curve-aware splitting and
        are intentionally not repaired by the straight-edge snap operation.
        """
        if isinstance(a, Line) or isinstance(b, Line):
            return None
        samples = np.linspace(0, 1, 33)
        forward = max(abs(a.point(t)-b.point(t)) for t in samples)
        reverse = max(abs(a.point(t)-b.point(1-t)) for t in samples)
        if min(forward, reverse) > NUMERIC_EPS:
            return None
        curve = a
        interval_b = (0.0, 1.0) if forward <= reverse else (1.0, 0.0)
        return (0.0, 1.0), interval_b, curve, min(forward, reverse)

    def _overlaps(self):
        rows = []
        for a, b in combinations(self.panels, 2):
            area = a.polygon.intersection(b.polygon).area
            explained = sum(s.length*s.discrepancy for s in self.seams
                            if {s.panel_a, s.panel_b} == {a.id, b.id} and s.repaired)
            explained += sum(edge["length"]*edge["distance"] for edge in self.near_seams
                             if set(edge["panels"]) == {a.id, b.id})
            if area > max(self.near*self.near, explained*2+NUMERIC_EPS):
                rows.append({"panels": [a.id, b.id], "area": area})
        return rows

    def _line_match(self, a: Line, b: Line):
        av, bv = a.end-a.start, b.end-b.start
        la, lb = abs(av), abs(bv)
        if min(la, lb) <= NUMERIC_EPS:
            return None
        ua, ub = av/la, bv/lb
        if abs(abs((ua.conjugate()*ub).real)-1) > 1e-8:
            return None
        normal = complex(-ua.imag, ua.real)
        distances = [abs(((point-a.start)*normal.conjugate()).real) for point in (b.start, b.end)]
        distance = max(distances)
        if distance > self.near:
            return None
        projections = sorted(((point-a.start)*ua.conjugate()).real for point in (b.start, b.end))
        lo, hi = max(0.0, projections[0]), min(la, projections[1])
        # Near-collinear corners can project to microscopic false overlaps. A
        # fold must have useful extent, not merely share an almost-common point.
        if hi-lo <= self.near:
            return None
        pa0, pa1 = a.start+ua*lo, a.start+ua*hi
        # Average the corresponding point on the other supporting line. This is
        # the canonical repaired seam, not an implicit manufacturing tolerance.
        def project(point, line):
            direction = line.end-line.start
            return line.start+direction*(((point-line.start)*direction.conjugate()).real/(abs(direction)**2))
        start, end = (pa0+project(pa0, b))/2, (pa1+project(pa1, b))/2
        def parameter(point, line):
            direction = line.end-line.start
            return ((point-line.start)*direction.conjugate()).real/(abs(direction)**2)
        ta = sorted((parameter(start, a), parameter(end, a)))
        tb = sorted((parameter(start, b), parameter(end, b)))
        def clamp_interval(values, line_length):
            tolerance = self.near/line_length
            lo = 0.0 if abs(values[0]) <= tolerance else max(0, values[0])
            hi = 1.0 if abs(1-values[1]) <= tolerance else min(1, values[1])
            return lo, hi
        return clamp_interval(ta, la), clamp_interval(tb, lb), Line(start, end), distance

    def _components(self):
        adjacency = {p.id: set() for p in self.panels}
        for seam in self.seams:
            adjacency[seam.panel_a].add(seam.panel_b)
            adjacency[seam.panel_b].add(seam.panel_a)
        result, unseen = [], set(adjacency)
        while unseen:
            stack, component = [next(iter(unseen))], []
            while stack:
                node = stack.pop()
                if node not in unseen:
                    continue
                unseen.remove(node)
                component.append(node)
                stack.extend(adjacency[node])
            result.append(sorted(component))
        return sorted(result, key=lambda c: (-len(c), c))

    def report(self):
        internals = [{"id": shape.id, "panel": panel.id, "area": shape.polygon.area,
                      "outside_hint": shape.outside_hint}
                     for panel in self.panels for shape in panel.internal]
        return {"format": "panel-source", "source": self.filename.name,
                "page_mm": list(self.page), "panels": [
                    {"id": p.id, "area": p.polygon.area, "outside_hint": p.outside_hint,
                     "internal_geometry": [s.id for s in p.internal]} for p in self.panels],
                "internal_geometry": internals,
                "folds": [{"id": s.id, "panels": [s.panel_a, s.panel_b], "length": s.length,
                           "repaired": bool(s.repaired), "original_discrepancy": float(s.discrepancy),
                           "placement": "outside" if (next(p for p in self.panels if p.id == s.panel_a).outside_hint or
                                                        next(p for p in self.panels if p.id == s.panel_b).outside_hint) else "inside"}
                          for s in self.seams],
                "near_edges": self.near_seams, "overlaps": self.overlaps, "components": self.components,
                "warnings": (([f"{len(self.near_seams)} near-coincident edge pair(s) are not folds; compile with --snap-near to repair them."]
                               if self.near_seams and not self.snap_near else [])+
                              ([f"{len(self.overlaps)} panel overlap(s) must be resolved before compiling."] if self.overlaps else []))}

    def _fragments(self):
        intervals = {(p.id, i): [] for p in self.panels for i, _ in enumerate(p.path)}
        for seam in self.seams:
            intervals[seam.panel_a, seam.segment_a].append(seam.interval_a)
            intervals[seam.panel_b, seam.segment_b].append(tuple(sorted(seam.interval_b)))
        fragments = []
        for panel in self.panels:
            for index, segment in enumerate(panel.path):
                cuts = sorted(intervals[panel.id, index])
                cursor = 0.0
                for lo, hi in cuts:
                    if lo > cursor+NUMERIC_EPS:
                        fragments.append(segment.cropped(cursor, lo))
                    cursor = max(cursor, hi)
                if cursor < 1-NUMERIC_EPS:
                    fragments.append(segment.cropped(cursor, 1))
        return fragments

    def _trace(self, fragments):
        """Trace exposed source segments into closed contours, preserving curves."""
        unused = list(fragments)
        paths = []
        while unused:
            chain = [unused.pop(0)]
            while abs(chain[-1].end-chain[0].start) > self.near:
                endpoint = chain[-1].end
                candidates = [(i, False, s) for i, s in enumerate(unused) if abs(s.start-endpoint) <= self.near]
                candidates += [(i, True, s) for i, s in enumerate(unused) if abs(s.end-endpoint) <= self.near]
                if len(candidates) != 1:
                    raise ValueError(f"Exposed cut boundary cannot be traced at {endpoint.real:.6f},{endpoint.imag:.6f}; "
                                     "repair near edges first or inspect overlapping panels")
                index, reverse, segment = candidates[0]
                unused.pop(index)
                segment = segment.reversed() if reverse else segment
                chain.append(move_segment_endpoint(segment, start=endpoint))
            chain[-1] = move_segment_endpoint(chain[-1], end=chain[0].start)
            paths.append(Path(*chain))
        return paths

    def compile(self, output, *, thickness=None, fold_offset=0.0):
        if self.near_seams and not self.snap_near:
            raise ValueError("Near-coincident panel edges remain; use --snap-near after reviewing the diagnostics")
        if self.overlaps:
            pairs = ", ".join("/".join(row["panels"]) for row in self.overlaps)
            raise ValueError(f"Panel interiors overlap: {pairs}")
        if thickness is not None and (not math.isfinite(thickness) or thickness <= 0):
            raise ValueError("Thickness must be finite and positive")
        if not math.isfinite(fold_offset):
            raise ValueError("Fold offset must be finite")
        contours = self._trace(self._fragments())
        root = ET.Element(f"{{{SVG}}}svg", width=f"{self.page[0]:g}mm", height=f"{self.page[1]:g}mm",
                          viewBox=f"0 0 {self.page[0]:g} {self.page[1]:g}")
        metadata = {"schema": 1, "source": self.filename.name, "units": "mm", "thickness": thickness,
                    "fold_offset_from_midplane": fold_offset,
                    "panels": [{"id": p.id, "outside_hint": p.outside_hint,
                                "internal_geometry": [s.id for s in p.internal]} for p in self.panels],
                    "folds": self.report()["folds"], "components": self.components,
                    "repairs": self.near_seams if self.snap_near else []}
        node = ET.SubElement(ET.SubElement(root, f"{{{SVG}}}metadata"), f"{{{BOXXER}}}model")
        node.text = json.dumps(metadata, separators=(",", ":"))
        cuts = ET.SubElement(root, f"{{{SVG}}}g", id="boxxer-cuts", stroke="#000000", fill="none",
                             **{"stroke-width": "0.1"})
        for i, contour in enumerate(contours):
            ET.SubElement(cuts, f"{{{SVG}}}path", id=f"cut-{i}", d=contour.d())
        for panel in self.panels:
            for shape in panel.internal:
                ET.SubElement(cuts, f"{{{SVG}}}path", id=f"internal-{shape.id}", d=shape.path.d(),
                              **{f"{{{BOXXER}}}panel": panel.id, f"{{{BOXXER}}}source-id": shape.id})
        scores = ET.SubElement(root, f"{{{SVG}}}g", id="boxxer-scores", stroke="#ff0000", fill="none",
                               **{"stroke-width": "0.1"})
        for seam in self.seams:
            ET.SubElement(scores, f"{{{SVG}}}path", id=seam.id,
                          d=Path(seam.curve).d(),
                          **{f"{{{BOXXER}}}panels": f"{seam.panel_a} {seam.panel_b}"})
        ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
        return {**self.report(), "output": str(output), "cut_contours": len(contours),
                "internal_cuts": sum(len(p.internal) for p in self.panels), "metadata": metadata}
