"""Generic SVG panel graph and manufacturing-path compiler.

Input shapes describe individual pieces of material. Coincident boundary
intervals become folds; every remaining boundary becomes a cut. Strictly
contained shapes are retained as internal cuts owned by the smallest panel.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path as FilePath
import re
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import nearest_points
from svgpathtools import Arc, CubicBezier, Line, Path, QuadraticBezier, parse_path
from svgpathtools.path import transform as transform_path

SVG = "http://www.w3.org/2000/svg"
BOXXER = "https://boxxer.local/ns/1"
ET.register_namespace("", SVG)
ET.register_namespace("boxxer", BOXXER)
NUMERIC_EPS = 1e-8
DEFAULT_NEAR = 0.01
MAX_EMBEDDED_SOURCE_BYTES = 10_000_000


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
    order: int = 0


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


@dataclass
class Contact:
    tab: str
    receiver: str
    seam: str
    placement: str
    overlap_area: float
    initial_separation: float


def translation_2d(offset: complex):
    return np.array([[1, 0, offset.real], [0, 1, offset.imag], [0, 0, 1]], dtype=float)


def transform_point(point: complex, transform):
    value = transform@np.array([point.real, point.imag, 1.0])
    return complex(value[0], value[1])


def affine_between_segments(start, end, new_start, new_end):
    vector = end-start
    mapped = new_end-new_start
    normal = complex(-vector.imag, vector.real)
    source = np.array([[vector.real, normal.real], [vector.imag, normal.imag]])
    target = np.array([[mapped.real, normal.real], [mapped.imag, normal.imag]])
    linear = target@np.linalg.inv(source)
    result = np.eye(3)
    result[:2, :2] = linear
    result[:2, 2] = np.array([new_start.real, new_start.imag])-linear@np.array([start.real, start.imag])
    return result


def transform_points(points, transform):
    values = np.column_stack((np.real(points), np.imag(points), np.zeros(len(points)), np.ones(len(points))))
    return values@transform.T


def rotation_3d(start: complex, end: complex, angle: float):
    a = np.array([start.real, start.imag, 0.0])
    b = np.array([end.real, end.imag, 0.0])
    axis = (b-a)/np.linalg.norm(b-a)
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    rotation = np.eye(3)*math.cos(angle)+(1-math.cos(angle))*np.outer(axis, axis)+math.sin(angle)*cross
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = a-rotation@a
    return result


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
    def __init__(self, filename, near=None, snap_near=None, *, _source_bytes=None, _source_name=None):
        self.filename = FilePath(filename)
        input_bytes = self.filename.read_bytes() if _source_bytes is None else _source_bytes
        outer_root = ET.fromstring(input_bytes)
        boxxer_metadata = [node for node in outer_root.findall(f"./{{{SVG}}}metadata")
                            if node.find(f"{{{BOXXER}}}model") is not None]
        if len(boxxer_metadata) > 1:
            raise ValueError("SVG contains multiple Boxxer metadata records")
        metadata_node = boxxer_metadata[0] if boxxer_metadata else None
        model_node = metadata_node.find(f"{{{BOXXER}}}model") if metadata_node is not None else None
        source_node = metadata_node.find(f"{{{BOXXER}}}source") if metadata_node is not None else None
        self.compiled_settings = {}
        self.input_is_compiled = model_node is not None
        if model_node is not None:
            try:
                self.compiled_settings = json.loads(model_node.text or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Compiled SVG contains invalid Boxxer metadata") from exc
            if not isinstance(self.compiled_settings, dict):
                raise ValueError("Compiled SVG Boxxer metadata must be a JSON object")
            schema = self.compiled_settings.get("schema")
            if schema != 4:
                raise ValueError(f"Compiled SVG schema {schema!r} is not supported; regenerate it from the original source SVG")
            if source_node is None:
                raise ValueError("Compiled SVG does not embed source geometry; compile from the original source SVG")
            if source_node.get("encoding") != "base64":
                raise ValueError("Compiled SVG uses an unsupported embedded-source encoding")
            try:
                encoded_source = "".join((source_node.text or "").split())
                if len(encoded_source) > math.ceil(MAX_EMBEDDED_SOURCE_BYTES*4/3)+4:
                    raise ValueError("embedded source exceeds the size limit")
                source_bytes = base64.b64decode(encoded_source, validate=True)
            except ValueError as exc:
                raise ValueError("Compiled SVG contains invalid embedded source geometry") from exc
            digest = source_node.get("sha256")
            if digest is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("Compiled SVG embedded source requires a valid SHA-256 checksum")
            if hashlib.sha256(source_bytes).hexdigest() != digest:
                raise ValueError("Compiled SVG embedded source checksum does not match")
            root = ET.fromstring(source_bytes)
            if root.find(f".//{{{BOXXER}}}model") is not None:
                raise ValueError("Nested compiled SVG sources are not supported")
            self.source_bytes = source_bytes
            self.source_name = self.compiled_settings.get("source", self.filename.name)
            if not isinstance(self.source_name, str):
                raise ValueError("Compiled SVG source name must be a string")
        else:
            root = outer_root
            self.source_bytes = input_bytes
            self.source_name = _source_name or self.filename.name
        self.resize_metadata = self.compiled_settings.get("resize")
        stored_options = self.compiled_settings.get("compiler_options", {})
        if not isinstance(stored_options, dict):
            raise ValueError("Compiled SVG compiler_options must be an object")
        for key in ("thickness", "fold_offset", "lid_clearance"):
            value = self.compiled_settings.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value)):
                raise ValueError(f"Compiled SVG {key} must be a finite number or null")
        if self.compiled_settings.get("thickness") is not None and self.compiled_settings["thickness"] < 0:
            raise ValueError("Compiled SVG thickness must be nonnegative")
        if self.compiled_settings.get("lid_clearance", 0) < 0:
            raise ValueError("Compiled SVG lid_clearance must be nonnegative")
        stored_near = stored_options.get("near_distance", DEFAULT_NEAR)
        stored_snap = stored_options.get("snap_near", False)
        if (isinstance(stored_near, bool) or not isinstance(stored_near, (int, float)) or
                not math.isfinite(stored_near) or stored_near <= 0):
            raise ValueError("Compiled SVG near_distance must be finite and positive")
        if not isinstance(stored_snap, bool):
            raise ValueError("Compiled SVG snap_near must be boolean")
        near = stored_near if near is None else near
        snap_near = stored_snap if snap_near is None else snap_near
        if not math.isfinite(near) or near <= 0:
            raise ValueError("Near-edge distance must be finite and positive")
        self.near = near
        self.snap_near = bool(snap_near)
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
        self.panels = [Panel(s.id, s.path, s.polygon, s.outside_hint, order=s.order)
                       for s in shapes if s.id not in owners]
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
        self.structure = self._infer_structure()
        self.contacts, self.contact_warnings, self.contact_diagnostics = self._infer_contacts()

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

    def _adjacency(self):
        adjacency = {p.id: [] for p in self.panels}
        for seam in self.seams:
            adjacency[seam.panel_a].append((seam.panel_b, seam))
            adjacency[seam.panel_b].append((seam.panel_a, seam))
        return adjacency

    def _infer_structure(self):
        """Recognize a top and bottom shell joined by a simple hinge spine."""
        if len(self.components) != 1:
            return {"status": "none"}
        by_id = {p.id: p for p in self.panels}
        adjacency = self._adjacency()
        largest = max(p.polygon.area for p in self.panels)
        tolerance = max(self.near*self.near, largest*1e-6)
        principals = [p.id for p in self.panels
                      if len(adjacency[p.id]) >= 3 and abs(p.polygon.area-largest) <= tolerance]
        if len(principals) != 2:
            return {"status": "none"}
        first, second = principals
        parent = {first: None}
        queue = [first]
        while queue and second not in parent:
            current = queue.pop(0)
            for neighbor, _ in adjacency[current]:
                if neighbor not in parent:
                    parent[neighbor] = current
                    queue.append(neighbor)
        if second not in parent:
            return {"status": "none"}
        path = [second]
        while path[-1] != first:
            path.append(parent[path[-1]])
        path.reverse()
        connector = path[1:-1]
        if not connector or any(len(adjacency[node]) != 2 for node in connector):
            return {"status": "none"}
        path_set = set(path)
        if any(sum(neighbor not in path_set for neighbor, _ in adjacency[node]) < 2
               for node in (first, second)):
            return {"status": "none"}
        first_y = by_id[first].polygon.centroid.y
        second_y = by_id[second].polygon.centroid.y
        if abs(first_y-second_y) <= self.near:
            return {"status": "ambiguous", "reason": "principal faces have the same page Y position"}
        top, bottom = (first, second) if first_y < second_y else (second, first)
        blocked = set(connector)

        def shell(root):
            result, stack = set(), [root]
            while stack:
                current = stack.pop()
                if current in result or current in blocked:
                    continue
                result.add(current)
                stack.extend(neighbor for neighbor, _ in adjacency[current])
            return sorted(result)

        top_shell, bottom_shell = shell(top), shell(bottom)
        roles = {panel_id: "top_shell" for panel_id in top_shell}
        roles.update({panel_id: "bottom_shell" for panel_id in bottom_shell})
        roles.update({panel_id: "shared_connector" for panel_id in connector})
        roles[top], roles[bottom] = "principal_top", "principal_bottom"
        contact_domains = {panel_id: ["top"] for panel_id in top_shell}
        contact_domains.update({panel_id: ["bottom"] for panel_id in bottom_shell})
        contact_domains.update({panel_id: ["top", "bottom"] for panel_id in connector})
        return {"status": "inferred", "method": "largest-faces-clean-spine-page-y-v1",
                "top": top, "bottom": bottom, "path": path, "shared_connector": connector,
                "top_shell": top_shell, "bottom_shell": bottom_shell, "panel_roles": roles,
                "contact_domains": contact_domains,
                "ordering": "svg-page-y-down", "top_outside": True}

    def _infer_contacts(self):
        """Find leaf panels that fold onto one unique non-adjacent panel.

        This is geometric contact inference, not a leaf=tab naming rule. A leaf
        with no unique coincident folded surface remains an ordinary panel.
        """
        by_id = {p.id: p for p in self.panels}
        adjacency = self._adjacency()
        transforms = {}
        warnings = []
        diagnostics = []
        for component in self.components:
            root = max(component, key=lambda panel_id: by_id[panel_id].polygon.area)
            edge_count = sum(1 for seam in self.seams
                             if seam.panel_a in component and seam.panel_b in component)
            if edge_count != len(component)-1:
                warnings.append(f"Component containing {root} is cyclic; rigid contact inference skipped.")
                continue
            transforms[root] = np.eye(4)
            queue = [root]
            visited = {root}
            while queue:
                parent_id = queue.pop(0)
                for child_id, seam in adjacency[parent_id]:
                    if child_id in visited:
                        continue
                    if not isinstance(seam.curve, Line):
                        warnings.append(f"Component containing {root} has curved fold {seam.id}; rigid contact inference skipped.")
                        transforms.pop(root, None)
                        queue = []
                        break
                    child = by_id[child_id]
                    interior = complex(*child.polygon.representative_point().coords[0])-seam.start
                    edge = seam.end-seam.start
                    side = np.cross(np.array([edge.real, edge.imag, 0]),
                                    np.array([interior.real, interior.imag, 0]))[2]
                    if abs(side) <= NUMERIC_EPS:
                        warnings.append(f"Cannot determine fold side for {seam.id}.")
                        queue = []
                        break
                    transforms[child_id] = transforms[parent_id]@rotation_3d(
                        seam.start, seam.end, math.copysign(math.pi/2, side))
                    visited.add(child_id)
                    queue.append(child_id)
            if len(visited) != len(component):
                warnings.append(f"Component containing {root} is cyclic or could not be folded as a rigid tree; contact inference skipped.")
                for panel_id in component:
                    transforms.pop(panel_id, None)

        surfaces = {}
        for panel in self.panels:
            if panel.id not in transforms:
                continue
            points = [complex(x, y) for x, y in list(panel.polygon.exterior.coords)[:-1]]
            world = transform_points(points, transforms[panel.id])[:, :3]
            normal = transforms[panel.id][:3, 2]
            center = world.mean(axis=0)
            surfaces[panel.id] = (world, normal, center)

        contacts = []
        for panel in self.panels:
            if len(adjacency[panel.id]) != 1 or panel.id not in surfaces:
                continue
            parent_id, seam = adjacency[panel.id][0]
            if (self.structure.get("status") == "inferred" and
                    parent_id in (self.structure["top"], self.structure["bottom"])):
                # A principal's direct leaves are structural walls, not glue tabs.
                continue
            points, normal, center = surfaces[panel.id]
            candidates = []
            for receiver in self.panels:
                if receiver.id in (panel.id, parent_id) or receiver.id not in surfaces:
                    continue
                # Near-equal panels are peer walls, not a tab and receiving face.
                if receiver.polygon.area <= panel.polygon.area*1.05+NUMERIC_EPS:
                    continue
                other, other_normal, other_center = surfaces[receiver.id]
                if abs(abs(float(np.dot(normal, other_normal)))-1) > 1e-7:
                    continue
                separation = abs(float(np.dot(center-other_center, other_normal)))
                if separation > self.near:
                    continue
                # Project both panels into the receiver's own folded XY basis.
                basis_x = transforms[receiver.id][:3, 0]
                basis_y = transforms[receiver.id][:3, 1]
                projected_panel = Polygon([(np.dot(p, basis_x), np.dot(p, basis_y)) for p in points])
                projected_receiver = Polygon([(np.dot(p, basis_x), np.dot(p, basis_y)) for p in other])
                overlap = projected_panel.intersection(projected_receiver).area
                if overlap > self.near*self.near:
                    candidates.append((receiver.id, overlap, separation))
            compatible = candidates
            if self.structure["status"] == "inferred":
                domains = self.structure["contact_domains"]
                parent_domains = set(domains.get(parent_id, []))
                compatible = [candidate for candidate in candidates
                              if parent_domains.intersection(domains.get(candidate[0], []))]
            if candidates:
                diagnostics.append({"tab": panel.id, "parent": parent_id,
                                    "geometric_candidates": [candidate[0] for candidate in candidates],
                                    "compatible_candidates": [candidate[0] for candidate in compatible]})
            if len(compatible) == 1:
                receiver, overlap, separation = compatible[0]
                contacts.append(Contact(panel.id, receiver, seam.id,
                                        "outside" if panel.outside_hint else "inside",
                                        overlap, separation))
                if diagnostics and diagnostics[-1]["tab"] == panel.id:
                    diagnostics[-1]["selected"] = receiver
            elif len(compatible) > 1:
                warnings.append(f"Leaf panel {panel.id} has multiple possible receiving panels; no compensation will be applied.")
            elif candidates:
                warnings.append(f"Leaf panel {panel.id} has only cross-shell receiving panels; no compensation will be applied.")
        return contacts, warnings, diagnostics

    def compensation_plan(self, thickness=None, fold_offset=0.0):
        thickness = 0.0 if thickness is None else thickness
        if not math.isfinite(thickness) or thickness < 0:
            raise ValueError("Thickness must be finite and nonnegative")
        if not math.isfinite(fold_offset):
            raise ValueError("Fold offset must be finite")
        distance = thickness+fold_offset
        if distance < 0:
            raise ValueError("Thickness plus fold offset cannot be negative")
        panels = {p.id: p for p in self.panels}
        seams = {s.id: s for s in self.seams}
        rows = []
        for contact in self.contacts:
            seam = seams[contact.seam]
            if not isinstance(seam.curve, Line):
                raise ValueError(f"Thickness compensation for curved tab fold {seam.id} is not supported")
            tab = panels[contact.tab]
            midpoint = (seam.start+seam.end)/2
            tangent = (seam.end-seam.start)/abs(seam.end-seam.start)
            normal = complex(-tangent.imag, tangent.real)
            centroid = complex(*tab.polygon.centroid.coords[0])
            side = 1 if ((centroid-midpoint)*normal.conjugate()).real > 0 else -1
            selected = side if contact.placement == "outside" else -side
            direction = normal*selected
            shift = direction*distance
            wall_direction = direction if contact.placement == "outside" else -direction
            wall_shift = wall_direction*thickness/2
            parent = seam.panel_b if seam.panel_a == contact.tab else seam.panel_a
            rows.append({"tab": contact.tab, "parent": parent, "receiver": contact.receiver,
                         "fold": contact.seam, "placement": contact.placement,
                         "thickness": thickness, "fold_offset": fold_offset,
                         "distance": distance, "direction": [direction.real, direction.imag],
                         "shift": [shift.real, shift.imag],
                         "wall_edge_shift": [wall_shift.real, wall_shift.imag]})
        if distance > NUMERIC_EPS and rows:
            measured = self.compensated_contacts(rows)
            probe = min(0.001, distance/10)
            for index, row in enumerate(rows):
                achieved = measured[index]["achieved_midplane_separation"]
                unit = complex(*row["direction"])
                trial = [{**item, "shift": list(item["shift"])} for item in rows]
                candidate = complex(*trial[index]["shift"])+unit*probe
                trial[index]["shift"] = [candidate.real, candidate.imag]
                shifted = self.compensated_contacts(trial)[index]["achieved_midplane_separation"]
                derivative = (shifted-achieved)/probe
                if abs(derivative) < 0.5:
                    raise ValueError(f"Cannot solve thickness compensation for {row['fold']}")
                corrected = max(0.0, distance+(distance-achieved)/derivative)
                vector = unit*corrected
                row["shift"] = [vector.real, vector.imag]
                row["geometric_shift_distance"] = corrected
        return rows

    def compensated_contacts(self, plans):
        """Fold the compensated panel graph and measure achieved plane spacing."""
        if not plans:
            return []
        by_id = {p.id: p for p in self.panels}
        shifts = {row["tab"]: complex(*row["shift"]) for row in plans}
        seam_curves = {s.id: s.curve for s in self.seams}
        for row in plans:
            shift = complex(*row["shift"])
            seam_curves[row["fold"]] = transform_path(Path(seam_curves[row["fold"]]), translation_2d(shift))[0]
        adjacency = {p.id: [] for p in self.panels}
        for seam in self.seams:
            adjacency[seam.panel_a].append((seam.panel_b, seam))
            adjacency[seam.panel_b].append((seam.panel_a, seam))
        transforms = {}
        for component in self.components:
            root = max(component, key=lambda panel_id: by_id[panel_id].polygon.area)
            transforms[root] = np.eye(4)
            queue, visited = [root], {root}
            while queue:
                parent_id = queue.pop(0)
                for child_id, seam in adjacency[parent_id]:
                    if child_id in visited:
                        continue
                    curve = seam_curves[seam.id]
                    if not isinstance(curve, Line):
                        raise ValueError(f"Cannot verify compensation across curved fold {seam.id}")
                    shift = shifts.get(child_id, 0j)
                    point = complex(*by_id[child_id].polygon.representative_point().coords[0])+shift
                    interior = point-curve.start
                    edge = curve.end-curve.start
                    side = np.cross(np.array([edge.real, edge.imag, 0]),
                                    np.array([interior.real, interior.imag, 0]))[2]
                    transforms[child_id] = transforms[parent_id]@rotation_3d(
                        curve.start, curve.end, math.copysign(math.pi/2, side))
                    visited.add(child_id)
                    queue.append(child_id)
        results = []
        for row in plans:
            tab, receiver = by_id[row["tab"]], by_id[row["receiver"]]
            tab_shift = shifts.get(tab.id, 0j)
            tab_center = complex(*tab.polygon.centroid.coords[0])+tab_shift
            receiver_center = complex(*receiver.polygon.centroid.coords[0])+shifts.get(receiver.id, 0j)
            tab_world = transform_points([tab_center], transforms[tab.id])[0, :3]
            receiver_world = transform_points([receiver_center], transforms[receiver.id])[0, :3]
            receiver_normal = transforms[receiver.id][:3, 2]
            achieved = abs(float(np.dot(tab_world-receiver_world, receiver_normal)))
            results.append({**row, "achieved_midplane_separation": achieved,
                            "residual": achieved-row["distance"]})
        return results

    def _resize_structure(self):
        """Describe the axis-aligned box-net subset that can be resized."""
        if len(self.components) != 1:
            return {"status": "unsupported", "reason": "resizing requires one connected panel graph"}
        by_id = {panel.id: panel for panel in self.panels}
        adjacency = self._adjacency()

        def rectangle(panel):
            if len(panel.path) != 4 or any(not isinstance(segment, Line) for segment in panel.path):
                return None
            x0, y0, x1, y1 = panel.polygon.bounds
            if (x1-x0 <= NUMERIC_EPS or y1-y0 <= NUMERIC_EPS or
                    abs(panel.polygon.area-(x1-x0)*(y1-y0)) > max(self.near**2, panel.polygon.area*1e-6)):
                return None
            if any(abs(segment.end.real-segment.start.real) > self.near and
                   abs(segment.end.imag-segment.start.imag) > self.near for segment in panel.path):
                return None
            return x0, y0, x1, y1

        def wall_height(principal_id, wall_id):
            seam = next((seam for neighbor, seam in adjacency[principal_id] if neighbor == wall_id), None)
            if seam is None or not isinstance(seam.curve, Line):
                raise ValueError("resizing requires straight principal folds")
            vector = seam.end-seam.start
            if abs(vector) <= NUMERIC_EPS:
                raise ValueError("resizing found a zero-length principal fold")
            distances = [abs(((complex(x, y)-seam.start)*vector.conjugate()).imag)/abs(vector)
                         for x, y in by_id[wall_id].polygon.exterior.coords]
            return max(distances)

        if self.structure.get("status") == "inferred":
            top, bottom = self.structure["top"], self.structure["bottom"]
            top_bounds, bottom_bounds = rectangle(by_id[top]), rectangle(by_id[bottom])
            if top_bounds is None or bottom_bounds is None:
                return {"status": "unsupported", "reason": "principal faces must be axis-aligned rectangles"}
            connector_ids = self.structure["shared_connector"]
            if len(connector_ids) != 1:
                return {"status": "unsupported", "reason": "resizing requires one shared connector panel"}
            connector = connector_ids[0]
            top_walls = [panel_id for panel_id, _ in adjacency[top] if panel_id != connector]
            bottom_walls = [panel_id for panel_id, _ in adjacency[bottom] if panel_id != connector]
            try:
                top_heights = [wall_height(top, wall) for wall in top_walls]
                bottom_heights = [wall_height(bottom, wall) for wall in bottom_walls]
            except ValueError as exc:
                return {"status": "unsupported", "reason": str(exc)}
            if not top_heights or not bottom_heights:
                return {"status": "unsupported", "reason": "both shells need structural walls"}
            top_height, bottom_height = sum(top_heights)/len(top_heights), sum(bottom_heights)/len(bottom_heights)
            if (max(top_heights)-min(top_heights) > self.near or
                    max(bottom_heights)-min(bottom_heights) > self.near):
                return {"status": "unsupported", "reason": "each shell must have one uniform wall height"}
            top_seam = next(seam for neighbor, seam in adjacency[top] if neighbor == connector)
            bottom_seam = next(seam for neighbor, seam in adjacency[bottom] if neighbor == connector)
            connector_depth = abs(complex(*by_id[top].polygon.centroid.coords[0])-complex(
                *by_id[bottom].polygon.centroid.coords[0]))-(top_bounds[3]-top_bounds[1])/2-(bottom_bounds[3]-bottom_bounds[1])/2
            connector_depth = abs(connector_depth)
            if not isinstance(top_seam.curve, Line) or not isinstance(bottom_seam.curve, Line):
                return {"status": "unsupported", "reason": "connector folds must be straight"}
            return {"status": "supported", "kind": "connected", "top": top, "base": bottom,
                    "connector": connector, "top_shell": self.structure["top_shell"],
                    "base_shell": self.structure["bottom_shell"],
                    "top_bounds": list(top_bounds), "base_bounds": list(bottom_bounds),
                    "dimensions": {"width": bottom_bounds[2]-bottom_bounds[0],
                                   "length": bottom_bounds[3]-bottom_bounds[1],
                                   "base_height": bottom_height, "lid_height": top_height,
                                   "lid_width": top_bounds[2]-top_bounds[0],
                                   "lid_length": top_bounds[3]-top_bounds[1],
                                   "connector_depth": connector_depth}}

        largest = max(panel.polygon.area for panel in self.panels)
        candidates = [panel for panel in self.panels if len(adjacency[panel.id]) >= 3 and
                      abs(panel.polygon.area-largest) <= max(self.near**2, largest*1e-6)]
        if len(candidates) != 1:
            return {"status": "unsupported", "reason": "cannot identify one rectangular principal face"}
        principal = candidates[0]
        bounds = rectangle(principal)
        if bounds is None:
            return {"status": "unsupported", "reason": "principal face must be an axis-aligned rectangle"}
        walls = [panel_id for panel_id, _ in adjacency[principal.id]]
        try:
            heights = [wall_height(principal.id, wall) for wall in walls]
        except ValueError as exc:
            return {"status": "unsupported", "reason": str(exc)}
        if not heights or max(heights)-min(heights) > self.near:
            return {"status": "unsupported", "reason": "the shell must have one uniform wall height"}
        return {"status": "supported", "kind": "single", "principal": principal.id,
                "shell": self.components[0], "bounds": list(bounds),
                "dimensions": {"width": bounds[2]-bounds[0], "length": bounds[3]-bounds[1],
                               "height": sum(heights)/len(heights)}}

    @staticmethod
    def _mapped_path(path, mapper, *, preserve_curves=True):
        """Map structural vertices while translating curved features rigidly."""
        curve_offsets = {}
        remaining = {index for index, segment in enumerate(path) if not isinstance(segment, Line)}
        while remaining:
            group, stack = set(), [next(iter(remaining))]
            while stack:
                index = stack.pop()
                if index not in remaining:
                    continue
                remaining.remove(index)
                group.add(index)
                for neighbor in ((index-1) % len(path), (index+1) % len(path)):
                    if neighbor in remaining:
                        stack.append(neighbor)
            boundary_anchors = []
            for index in group:
                if (index-1) % len(path) not in group:
                    boundary_anchors.append(path[index].start)
                if (index+1) % len(path) not in group:
                    boundary_anchors.append(path[index].end)
            anchors = boundary_anchors or [path[index].point(0.5) for index in group]
            anchor = sum(anchors, 0j)/len(anchors)
            offset = mapper(anchor)-anchor
            curve_offsets.update({index: offset for index in group})
        segments = []
        for index, segment in enumerate(path):
            if preserve_curves and not isinstance(segment, Line):
                segments.append(transform_path(Path(segment), translation_2d(curve_offsets[index]))[0])
            else:
                segments.append(Line(mapper(segment.start), mapper(segment.end)))
        for index in range(len(segments)):
            following = (index+1) % len(segments)
            source_join = path[index].end
            if preserve_curves and not isinstance(path[index], Line):
                join = segments[index].end
            elif preserve_curves and not isinstance(path[following], Line):
                join = segments[following].start
            else:
                join = mapper(source_join)
            segments[index] = move_segment_endpoint(segments[index], end=join)
            segments[following] = move_segment_endpoint(segments[following], start=join)
        return Path(*segments)

    def resized(self, *, width=None, length=None, height=None, base_height=None,
                lid_height=None, connector_depth=None):
        """Return a fresh graph built from resized uncompensated source geometry."""
        requested = {key: value for key, value in {
            "width": width, "length": length, "height": height,
            "base_height": base_height, "lid_height": lid_height,
            "connector_depth": connector_depth}.items() if value is not None}
        if not requested:
            raise ValueError("Resize requires at least one target dimension")
        if any(isinstance(value, bool) or not math.isfinite(value) or value <= 1
               for value in requested.values()):
            raise ValueError("Resize dimensions must be finite and greater than 1 mm")
        model = self._resize_structure()
        if model["status"] != "supported":
            raise ValueError("Cannot resize this panel graph: "+model["reason"])
        if model["kind"] == "single" and any(value is not None for value in (base_height, lid_height, connector_depth)):
            raise ValueError("Base, lid, and connector dimensions require a connected enclosure")

        by_id = {panel.id: panel for panel in self.panels}

        def reliefs(panel_ids, bounds, wall_height):
            x0, y0, x1, y1 = bounds
            points = [point for panel_id in panel_ids for segment in by_id[panel_id].path
                      for point in (segment.start, segment.end)]
            left = [x0-point.real for point in points if y0-self.near <= point.imag <= y1+self.near
                    and self.near < x0-point.real < wall_height-self.near]
            right = [point.real-x1 for point in points if y0-self.near <= point.imag <= y1+self.near
                     and self.near < point.real-x1 < wall_height-self.near]
            ends = [min(abs(point.imag-y0), abs(point.imag-y1)) for point in points
                    if x0-wall_height-self.near <= point.real <= x1+wall_height+self.near
                    and self.near < min(abs(point.imag-y0), abs(point.imag-y1)) < wall_height/4]
            return min(left, default=0), min(right, default=0), max(ends, default=0)

        def shell_mapper(bounds, old_height, new_height, new_width, new_length, panel_ids, shift=0.0):
            x0, y0, x1, y1 = bounds
            dw, dl, dh = new_width-(x1-x0), new_length-(y1-y0), new_height-old_height
            left_relief, right_relief, end_relief = reliefs(panel_ids, bounds, old_height)
            if new_height <= max(left_relief, right_relief, end_relief)+self.near:
                raise ValueError("Requested height is smaller than fixed tab reliefs")

            def mapper(point):
                x, y = point.real, point.imag
                if x < x0:
                    distance = x0-x
                    xx = x-dw/2-dh*max(0, distance-left_relief)/(old_height-left_relief)
                elif x > x1:
                    distance = x-x1
                    xx = x+dw/2+dh*max(0, distance-right_relief)/(old_height-right_relief)
                else:
                    xx = x+dw*((x-x0)/(x1-x0)-0.5)
                if y < y0:
                    yy = y-dh*max(0, y0-y-end_relief)/(old_height-end_relief) if x0-self.near <= x <= x1+self.near else y
                elif y > y1:
                    yy = y+dl+(dh*max(0, y-y1-end_relief)/(old_height-end_relief)
                               if x0-self.near <= x <= x1+self.near else 0)
                else:
                    denominator = y1-y0-2*end_relief
                    t = min(1, max(0, (y-y0-end_relief)/denominator)) if denominator > self.near else (y-y0)/(y1-y0)
                    yy = y+dl*t
                return complex(xx, yy+shift)
            return mapper

        mappers = {}
        effective = {}
        if model["kind"] == "single":
            old = model["dimensions"]
            target_width, target_length = width or old["width"], length or old["length"]
            target_height = height or old["height"]
            mapper = shell_mapper(model["bounds"], old["height"], target_height,
                                  target_width, target_length, model["shell"])
            mappers.update({panel_id: mapper for panel_id in model["shell"]})
            effective = {"width": target_width, "length": target_length, "height": target_height}
        else:
            old = model["dimensions"]
            target_width, target_length = width or old["width"], length or old["length"]
            target_base_height = height or old["base_height"]
            target_lid_height = (height if height is not None and
                                 abs(old["base_height"]-old["lid_height"]) <= self.near else old["lid_height"])
            target_base_height = base_height or target_base_height
            target_lid_height = lid_height or target_lid_height
            target_lid_width = old["lid_width"]+target_width-old["width"]
            target_lid_length = old["lid_length"]+target_length-old["length"]
            target_connector = connector_depth or old["connector_depth"]+target_base_height-old["base_height"]
            if min(target_lid_width, target_lid_length, target_connector) <= 1:
                raise ValueError("Requested dimensions eliminate the lid or connector")
            top_mapper = shell_mapper(model["top_bounds"], old["lid_height"], target_lid_height,
                                      target_lid_width, target_lid_length, model["top_shell"])
            base_shift = target_length-old["length"]+target_connector-old["connector_depth"]
            base_mapper = shell_mapper(model["base_bounds"], old["base_height"], target_base_height,
                                       target_width, target_length, model["base_shell"], base_shift)
            mappers.update({panel_id: top_mapper for panel_id in model["top_shell"]})
            mappers.update({panel_id: base_mapper for panel_id in model["base_shell"]})
            top_bounds, base_bounds = model["top_bounds"], model["base_bounds"]
            source_top_y, source_base_y = top_bounds[3], base_bounds[1]
            mapped_top_y = top_mapper(complex((top_bounds[0]+top_bounds[2])/2, source_top_y)).imag
            mapped_base_y = base_mapper(complex((base_bounds[0]+base_bounds[2])/2, source_base_y)).imag

            def connector_mapper(point):
                depth = (point.imag-source_top_y)/(source_base_y-source_top_y)
                source_left = top_bounds[0]+(base_bounds[0]-top_bounds[0])*depth
                source_right = top_bounds[2]+(base_bounds[2]-top_bounds[2])*depth
                along = (point.real-source_left)/(source_right-source_left)
                top_left = top_mapper(complex(top_bounds[0], source_top_y)).real
                top_right = top_mapper(complex(top_bounds[2], source_top_y)).real
                base_left = base_mapper(complex(base_bounds[0], source_base_y)).real
                base_right = base_mapper(complex(base_bounds[2], source_base_y)).real
                target_left = top_left+(base_left-top_left)*depth
                target_right = top_right+(base_right-top_right)*depth
                return complex(target_left+(target_right-target_left)*along,
                               mapped_top_y+(mapped_base_y-mapped_top_y)*depth)

            mappers[model["connector"]] = connector_mapper
            effective = {"width": target_width, "length": target_length,
                         "base_height": target_base_height, "lid_height": target_lid_height,
                         "lid_width": target_lid_width, "lid_length": target_lid_length,
                         "connector_depth": target_connector}

        seam_by_id = {seam.id: seam for seam in self.seams}
        for contact in self.contacts:
            seam = seam_by_id[contact.seam]
            parent = seam.panel_b if seam.panel_a == contact.tab else seam.panel_a
            parent_mapper = mappers[parent]
            transform = affine_between_segments(
                seam.start, seam.end, parent_mapper(seam.start), parent_mapper(seam.end))
            mappers[contact.tab] = lambda point, transform=transform: transform_point(point, transform)

        records = []
        owners = {}
        for panel in self.panels:
            mapper = mappers.get(panel.id)
            if mapper is None:
                raise ValueError(f"Cannot assign resize role to panel {panel.id}")
            records.append((panel.order, panel.id, self._mapped_path(panel.path, mapper), panel.outside_hint))
            for shape in panel.internal:
                centroid = complex(*shape.polygon.centroid.coords[0])
                path = transform_path(shape.path, translation_2d(mapper(centroid)-centroid))
                records.append((shape.order, shape.id, path, shape.outside_hint))
                owners[shape.id] = panel.id
        root = ET.Element(f"{{{SVG}}}svg", width="210mm", height="297mm", viewBox="0 0 210 297")
        for _, shape_id, path, outside_hint in sorted(records):
            attributes = {"id": shape_id, "d": path.d(), "fill": "none", "stroke": "#000000"}
            if outside_hint:
                attributes["stroke-dasharray"] = "1,1"
            ET.SubElement(root, f"{{{SVG}}}path", **attributes)
        source_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        resized = PanelGraph(self.source_name, self.near, self.snap_near,
                             _source_bytes=source_bytes, _source_name=self.source_name)
        if resized.overlaps:
            raise ValueError("Resize creates overlapping panel interiors")
        if {panel.id for panel in resized.panels} != {panel.id for panel in self.panels}:
            raise ValueError("Resize changes panel containment or topology")
        resized_owners = {shape.id: panel.id for panel in resized.panels for shape in panel.internal}
        if resized_owners != owners:
            raise ValueError("Resize moves fixed internal geometry outside its owning panel")
        source_pairs = {frozenset((seam.panel_a, seam.panel_b)) for seam in self.seams}
        target_pairs = {frozenset((seam.panel_a, seam.panel_b)) for seam in resized.seams}
        if source_pairs != target_pairs:
            raise ValueError("Resize changes the panel fold topology")
        source_contacts = {(contact.tab, contact.receiver, contact.placement) for contact in self.contacts}
        target_contacts = {(contact.tab, contact.receiver, contact.placement) for contact in resized.contacts}
        if source_contacts != target_contacts or resized.contact_warnings:
            raise ValueError("Resize changes inferred tab contacts; fixed features no longer fit")
        resized.resize_metadata = {"version": 1, "requested": requested,
                                   "source_dimensions": model["dimensions"],
                                   "effective_dimensions": effective,
                                   "reference_shell": "base" if model["kind"] == "connected" else "single",
                                   "feature_policy": "fixed-physical-size"}
        return resized

    def report(self, thickness=None, fold_offset=None, lid_clearance=None):
        if self.input_is_compiled:
            thickness = self.compiled_settings.get("thickness") if thickness is None else thickness
        fold_offset = self.compiled_settings.get("fold_offset", 0) if fold_offset is None else fold_offset
        lid_clearance = self.compiled_settings.get("lid_clearance", 0) if lid_clearance is None else lid_clearance
        internals = [{"id": shape.id, "panel": panel.id, "area": shape.polygon.area,
                      "outside_hint": shape.outside_hint}
                     for panel in self.panels for shape in panel.internal]
        return {"format": "panel-source", "source": self.source_name,
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
                "structure": self.structure, "resizing": self._resize_structure(),
                "lid_fit": self.lid_fit_plan(thickness, lid_clearance),
                "contacts": [{"tab": c.tab, "receiver": c.receiver, "fold": c.seam,
                              "placement": c.placement, "overlap_area": c.overlap_area,
                              "initial_midplane_separation": c.initial_separation} for c in self.contacts],
                "contact_warnings": self.contact_warnings,
                "contact_diagnostics": self.contact_diagnostics,
                "compensation": self.compensated_contacts(self.compensation_plan(thickness, fold_offset)),
                "warnings": (([f"{len(self.near_seams)} near-coincident edge pair(s) are not folds; compile with --snap-near to repair them."]
                               if self.near_seams and not self.snap_near else [])+
                              ([f"{len(self.overlaps)} panel overlap(s) must be resolved before compiling."] if self.overlaps else [])+
                               self.contact_warnings)}

    def _side_label(self, principal_id, wall_id):
        by_id = {p.id: p for p in self.panels}
        principal = by_id[principal_id].polygon.centroid
        wall = by_id[wall_id].polygon.centroid
        dx, dy = wall.x-principal.x, wall.y-principal.y
        if abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "far"

    def lid_fit_plan(self, thickness=None, lid_clearance=0.0):
        thickness = 0.0 if thickness is None else thickness
        if not math.isfinite(lid_clearance) or lid_clearance < 0:
            raise ValueError("Lid clearance must be finite and nonnegative")
        if self.structure["status"] != "inferred":
            if lid_clearance > NUMERIC_EPS:
                raise ValueError("Lid clearance requires an inferred connected top/bottom enclosure")
            return {"status": "none", "clearance": lid_clearance, "walls": []}
        if len(self.structure["shared_connector"]) != 1:
            raise ValueError("Lid fitting currently requires one shared connector panel")
        adjacency = self._adjacency()
        top, bottom = self.structure["top"], self.structure["bottom"]
        connector = self.structure["shared_connector"][0]
        top_walls = [panel_id for panel_id, _ in adjacency[top] if panel_id != connector]
        bottom_walls = [panel_id for panel_id, _ in adjacency[bottom] if panel_id != connector]
        top_by_side = {self._side_label(top, panel_id): panel_id for panel_id in top_walls}
        bottom_by_side = {self._side_label(bottom, panel_id): panel_id for panel_id in bottom_walls}
        if len(top_by_side) != len(top_walls) or set(top_by_side) != set(bottom_by_side):
            raise ValueError("Cannot pair top and bottom walls for lid fitting")
        rows = []
        for side in sorted(top_by_side):
            top_wall, bottom_wall = top_by_side[side], bottom_by_side[side]
            top_inside = any(c.receiver == top_wall and c.placement == "inside" and
                             c.tab in self.structure["top_shell"] for c in self.contacts)
            bottom_outside = any(c.receiver == bottom_wall and c.placement == "outside" and
                                 c.tab in self.structure["bottom_shell"] for c in self.contacts)
            layers = int(top_inside)+int(bottom_outside)
            rows.append({"side": side, "top_wall": top_wall, "bottom_wall": bottom_wall,
                         "gap_layers": layers, "midplane_offset": thickness*(1+layers)+lid_clearance})
        return {"status": "applied" if thickness > NUMERIC_EPS or lid_clearance > NUMERIC_EPS else "inferred",
                "top": top, "bottom": bottom, "connector": connector,
                "thickness": thickness, "clearance": lid_clearance, "walls": rows}

    def _fitted_geometry(self, thickness, lid_clearance):
        plan = self.lid_fit_plan(thickness, lid_clearance)
        fragments = self._fragments()
        seams = {seam.id: seam.curve for seam in self.seams}
        internals = [(panel.id, shape, shape.path) for panel in self.panels for shape in panel.internal]
        if plan["status"] in ("none", "inferred"):
            return fragments, seams, internals, plan
        by_id = {p.id: p for p in self.panels}
        adjacency = self._adjacency()
        top, connector = plan["top"], plan["connector"]
        top_panel = by_id[top]
        min_x, min_y, max_x, max_y = top_panel.polygon.bounds
        new_min_x, new_y, new_max_x, new_max_y = min_x, min_y, max_x, max_y
        for row in plan["walls"]:
            offset = row["midplane_offset"]
            if row["side"] == "left":
                new_min_x -= offset
            elif row["side"] == "right":
                new_max_x += offset
            elif row["side"] == "far":
                wall_center = by_id[row["top_wall"]].polygon.centroid
                if wall_center.y < top_panel.polygon.centroid.y:
                    new_y -= offset
                else:
                    new_max_y += offset
        if any(not isinstance(segment, Line) for segment in top_panel.path):
            raise ValueError("Lid fitting currently requires a straight-sided principal face")
        if len(top_panel.path) != 4 or any(
                abs(segment.end.real-segment.start.real) > self.near and
                abs(segment.end.imag-segment.start.imag) > self.near
                for segment in top_panel.path):
            raise ValueError("Lid fitting currently requires an axis-aligned rectangular principal face")
        principal_transform = np.array([
            [(new_max_x-new_min_x)/(max_x-min_x), 0,
             new_min_x-min_x*(new_max_x-new_min_x)/(max_x-min_x)],
            [0, (new_max_y-new_y)/(max_y-min_y),
             new_y-min_y*(new_max_y-new_y)/(max_y-min_y)],
            [0, 0, 1]], dtype=float)
        transforms = {top: principal_transform}
        top_shell = set(self.structure["top_shell"])
        queue = [top]
        while queue:
            parent_id = queue.pop(0)
            for child_id, seam in adjacency[parent_id]:
                if child_id not in top_shell or child_id in transforms:
                    continue
                if not isinstance(seam.curve, Line):
                    raise ValueError("Lid fitting currently requires straight shell folds")
                parent_transform = transforms[parent_id]
                mapped_start = transform_point(seam.start, parent_transform)
                mapped_end = transform_point(seam.end, parent_transform)
                transforms[child_id] = affine_between_segments(
                    seam.start, seam.end, mapped_start, mapped_end)
                queue.append(child_id)

        top_connector = next(seam for neighbor, seam in adjacency[top] if neighbor == connector)
        bottom = plan["bottom"]
        bottom_connector = next(seam for neighbor, seam in adjacency[bottom] if neighbor == connector)
        if not isinstance(top_connector.curve, Line) or not isinstance(bottom_connector.curve, Line):
            raise ValueError("Lid fitting currently requires straight connector folds")
        top_direction = (top_connector.end-top_connector.start)/abs(top_connector.end-top_connector.start)
        bottom_direction = (bottom_connector.end-bottom_connector.start)/abs(bottom_connector.end-bottom_connector.start)
        if abs((top_direction*bottom_direction.conjugate()).imag) > 1e-7:
            raise ValueError("Lid fitting currently requires parallel connector folds")
        top_start, top_end = top_connector.start, top_connector.end
        mapped_top_start = transform_point(top_start, principal_transform)
        mapped_top_end = transform_point(top_end, principal_transform)
        top_vector = top_end-top_start
        top_mid = (top_start+top_end)/2
        bottom_mid = (bottom_connector.start+bottom_connector.end)/2
        connector_axis = bottom_mid-top_mid

        def map_connector_point(point):
            along = ((point-top_start)*top_vector.conjugate()).real/(abs(top_vector)**2)
            depth = ((point-top_mid)*connector_axis.conjugate()).real/(abs(connector_axis)**2)
            source_top = top_start+top_vector*along
            mapped_top = mapped_top_start+(mapped_top_end-mapped_top_start)*along
            return point+(mapped_top-source_top)*(1-depth)

        def map_connector_segment(segment):
            if not isinstance(segment, Line):
                raise ValueError("Lid fitting currently requires a straight-sided connector")
            return Line(map_connector_point(segment.start), map_connector_point(segment.end))

        fitted_fragments = []
        for owner, segment in fragments:
            if owner in transforms:
                segment = transform_path(Path(segment), transforms[owner])[0]
            elif owner == connector:
                segment = map_connector_segment(segment)
            fitted_fragments.append((owner, segment))
        fitted_seams = {}
        for seam in self.seams:
            if seam.panel_a in transforms:
                fitted_seams[seam.id] = transform_path(Path(seam.curve), transforms[seam.panel_a])[0]
            elif seam.panel_b in transforms:
                fitted_seams[seam.id] = transform_path(Path(seam.curve), transforms[seam.panel_b])[0]
            else:
                fitted_seams[seam.id] = seam.curve
        fitted_internals = []
        for panel_id, shape, path in internals:
            if panel_id in transforms:
                path = transform_path(path, transforms[panel_id])
            elif panel_id == connector:
                path = Path(*(map_connector_segment(segment) for segment in path))
            fitted_internals.append((panel_id, shape, path))
        return fitted_fragments, fitted_seams, fitted_internals, plan

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
                        fragments.append((panel.id, segment.cropped(cursor, lo)))
                    cursor = max(cursor, hi)
                if cursor < 1-NUMERIC_EPS:
                    fragments.append((panel.id, segment.cropped(cursor, 1)))
        return fragments

    def _compensated_geometry(self, thickness, fold_offset, lid_clearance):
        plans = self.compensation_plan(thickness, fold_offset)
        fragments, seams, internals, lid_fit = self._fitted_geometry(thickness, lid_clearance)
        shifts = {row["tab"]: complex(*row["shift"]) for row in plans}
        moved = []
        reliefs = []
        corner_reliefs = []
        for owner, segment in fragments:
            shift = shifts.get(owner, 0j)
            if shift:
                segment = transform_path(Path(segment), translation_2d(shift))[0]
            moved.append((owner, segment))
        seam_by_id = {seam.id: seam for seam in self.seams}
        for row in plans:
            seam = seam_by_id[row["fold"]]
            seam_curve = seams[seam.id]
            shift = complex(*row["shift"])
            wall_shift = complex(*row["wall_edge_shift"])
            if abs(shift) <= NUMERIC_EPS and abs(wall_shift) <= NUMERIC_EPS:
                continue
            shifted = transform_path(Path(seam_curve), translation_2d(shift))[0]
            seams[seam.id] = shifted
            roots = [(seam_curve.start, shifted.start), (seam_curve.end, shifted.end)]
            seam_tangent = (seam_curve.end-seam_curve.start)/abs(seam_curve.end-seam_curve.start)
            for original, displaced in roots:
                landing = original+wall_shift

                # Move the supporting wall's remaining terminal edge as a whole.
                parent_candidates = []
                for index, (owner, segment) in enumerate(moved):
                    if owner != row["parent"]:
                        continue
                    if abs(segment.start-original) <= self.near:
                        parent_candidates.append((index, True))
                    if abs(segment.end-original) <= self.near:
                        parent_candidates.append((index, False))
                if len(parent_candidates) != 1:
                    raise ValueError(f"Cannot construct square-overlap wall end at "
                                     f"{original.real:.6f},{original.imag:.6f}")
                parent_index, parent_at_start = parent_candidates[0]
                owner, parent_segment = moved[parent_index]
                endpoint_tangent = parent_segment.derivative(0 if parent_at_start else 1)
                endpoint_tangent /= abs(endpoint_tangent)
                collinear = abs((endpoint_tangent*seam_tangent.conjugate()).imag) <= 1e-7
                if collinear:
                    if not isinstance(parent_segment, Line):
                        raise ValueError(f"Curved supporting wall end at {original.real:.6f},"
                                         f"{original.imag:.6f} cannot be offset")
                    translated = transform_path(Path(parent_segment), translation_2d(wall_shift))[0]
                    moved[parent_index] = (owner, translated)
                    old_far = parent_segment.end if parent_at_start else parent_segment.start
                    new_far = translated.end if parent_at_start else translated.start
                    neighbors = []
                    for index, (neighbor_owner, neighbor) in enumerate(moved):
                        if index == parent_index:
                            continue
                        if abs(neighbor.start-old_far) <= self.near:
                            neighbors.append((index, True, neighbor_owner, neighbor))
                        if abs(neighbor.end-old_far) <= self.near:
                            neighbors.append((index, False, neighbor_owner, neighbor))
                    trimmed = False
                    outer_junction = None
                    if len(neighbors) == 1:
                        index, at_start, neighbor_owner, neighbor = neighbors[0]
                        other = neighbor.end if at_start else neighbor.start
                        outward = other-old_far
                        if (abs(outward) > NUMERIC_EPS and
                                abs((outward/abs(outward)*wall_shift.conjugate()).imag) <= self.near and
                                (outward*wall_shift.conjugate()).real > 0 and
                                abs(wall_shift) < abs(outward)-NUMERIC_EPS):
                            moved[index] = (neighbor_owner, move_segment_endpoint(
                                neighbor, start=new_far) if at_start else
                                move_segment_endpoint(neighbor, end=new_far))
                            trimmed = True
                            outer_junction = new_far
                        elif isinstance(parent_segment, Line) and isinstance(neighbor, Line):
                            crossing = LineString([
                                (translated.start.real, translated.start.imag),
                                (translated.end.real, translated.end.imag),
                            ]).intersection(LineString([
                                (neighbor.start.real, neighbor.start.imag),
                                (neighbor.end.real, neighbor.end.imag),
                            ]))
                            if isinstance(crossing, Point):
                                junction = complex(crossing.x, crossing.y)
                                moved[parent_index] = (owner, move_segment_endpoint(
                                    translated, end=junction) if parent_at_start else
                                    move_segment_endpoint(translated, start=junction))
                                moved[index] = (neighbor_owner, move_segment_endpoint(
                                    neighbor, start=junction) if at_start else
                                    move_segment_endpoint(neighbor, end=junction))
                                trimmed = True
                                outer_junction = junction
                    if not trimmed:
                        moved.append((owner, Line(old_far, new_far)))
                    elif outer_junction is not None:
                        score_ends = []
                        for score_id, curve in seams.items():
                            if abs(curve.start-old_far) <= self.near:
                                score_ends.append((score_id, True, curve.start))
                            if abs(curve.end-old_far) <= self.near:
                                score_ends.append((score_id, False, curve.end))
                        if not score_ends:
                            raise ValueError(f"Shifted wall end at {old_far.real:.6f},{old_far.imag:.6f} "
                                             "does not meet a score corner")
                        stable_ends = [item for item in score_ends
                                       if row["parent"] not in (seam_by_id[item[0]].panel_a,
                                                                 seam_by_id[item[0]].panel_b)]
                        score_corner = stable_ends[0][2] if len(stable_ends) == 1 else min(
                            (point for _, _, point in score_ends), key=lambda point: abs(point-old_far))
                        for score_id, at_start, _ in score_ends:
                            curve = seams[score_id]
                            seams[score_id] = (move_segment_endpoint(curve, start=score_corner)
                                               if at_start else
                                               move_segment_endpoint(curve, end=score_corner))
                        if (abs(outer_junction-score_corner) > NUMERIC_EPS and
                                not any(abs(path.end-score_corner) <= self.near for path in corner_reliefs)):
                            corner_reliefs.append(Path(Line(outer_junction, score_corner)))
                else:
                    moved[parent_index] = (owner, move_segment_endpoint(
                        parent_segment, start=landing) if parent_at_start else
                        move_segment_endpoint(parent_segment, end=landing))

                tab_candidates = []
                for index, (owner, segment) in enumerate(moved):
                    if owner != row["tab"]:
                        continue
                    if abs(segment.start-displaced) <= self.near:
                        tab_candidates.append((index, True))
                    if abs(segment.end-displaced) <= self.near:
                        tab_candidates.append((index, False))
                if len(tab_candidates) != 1:
                    raise ValueError(f"Cannot connect compensated tab at "
                                     f"{original.real:.6f},{original.imag:.6f}")
                tab_index, tab_at_start = tab_candidates[0]
                owner, tab_segment = moved[tab_index]
                moved[tab_index] = (owner, move_segment_endpoint(
                    tab_segment, start=landing) if tab_at_start else
                    move_segment_endpoint(tab_segment, end=landing))
                if row["placement"] == "inside" and abs(displaced-landing) > NUMERIC_EPS:
                    reliefs.append(Path(Line(landing, displaced)))
        moved_internals = []
        for panel_id, shape, path in internals:
            shift = shifts.get(panel_id, 0j)
            path = transform_path(path, translation_2d(shift)) if shift else path
            moved_internals.append((panel_id, shape, path))
        return moved, seams, moved_internals, reliefs, corner_reliefs, plans, lid_fit

    def _trace(self, fragments):
        """Trace exposed source segments into closed contours, preserving curves."""
        unused = [segment for _, segment in fragments]
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

    def _untangle_contour(self, path):
        """Remove small loops introduced by large compensation offsets."""
        path = Path(*path)

        def sampled_line(segment):
            return LineString(sample_segment(segment))

        def segment_parameter(segment, point):
            count = 1 if isinstance(segment, Line) else max(
                8, math.ceil(segment.length(error=1e-5)/0.03))
            best = (math.inf, 0.0)
            for index in range(count):
                start, end = segment.point(index/count), segment.point((index+1)/count)
                vector = end-start
                fraction = 0.0 if abs(vector) <= NUMERIC_EPS else max(0.0, min(
                    1.0, ((point-start)*vector.conjugate()).real/(abs(vector)**2)))
                projected = start+vector*fraction
                candidate = (abs(point-projected), (index+fraction)/count)
                if candidate < best:
                    best = candidate
            return best[1]

        for _ in range(len(path)):
            crossing = None
            for first in range(len(path)):
                for second in range(first+2, len(path)):
                    if second == len(path)-1 and first == 0:
                        continue
                    hit = sampled_line(path[first]).intersection(sampled_line(path[second]))
                    points = [hit] if isinstance(hit, Point) else (
                        list(hit.geoms) if isinstance(hit, MultiPoint) else [])
                    if not points:
                        continue
                    a, b = path[first], path[second]
                    candidates = []
                    for hit_point in points:
                        point = complex(hit_point.x, hit_point.y)
                        ta, tb = segment_parameter(a, point), segment_parameter(b, point)
                        if NUMERIC_EPS < ta < 1-NUMERIC_EPS and NUMERIC_EPS < tb < 1-NUMERIC_EPS:
                            candidates.append((first, second, ta, tb))
                    if candidates:
                        crossing = min(candidates, key=lambda candidate: candidate[2])
                        break
                if crossing:
                    break
            if crossing is None:
                return path
            first, second, ta, tb = crossing
            junction = (path[first].point(ta)+path[second].point(tb))/2
            first_before = move_segment_endpoint(path[first].cropped(0, ta), end=junction)
            first_after = move_segment_endpoint(path[first].cropped(ta, 1), start=junction)
            second_before = move_segment_endpoint(path[second].cropped(0, tb), end=junction)
            second_after = move_segment_endpoint(path[second].cropped(tb, 1), start=junction)
            outside = Path(*(list(path[:first])+[first_before, second_after]+list(path[second+1:])))
            loop = Path(first_after, *path[first+1:second], second_before)

            def sampled_area(candidate):
                points = [point for segment in candidate for point in sample_segment(segment)[:-1]]
                return Polygon(points).area

            path = outside if sampled_area(outside) >= sampled_area(loop) else loop
        raise ValueError("Compensated exterior cut contour contains too many self-intersections")

    def compile(self, output, *, thickness=None, fold_offset=None, lid_clearance=None):
        if self.input_is_compiled:
            thickness = self.compiled_settings.get("thickness") if thickness is None else thickness
        fold_offset = self.compiled_settings.get("fold_offset", 0) if fold_offset is None else fold_offset
        lid_clearance = self.compiled_settings.get("lid_clearance", 0) if lid_clearance is None else lid_clearance
        if self.near_seams and not self.snap_near:
            raise ValueError("Near-coincident panel edges remain; use --snap-near after reviewing the diagnostics")
        if self.overlaps:
            pairs = ", ".join("/".join(row["panels"]) for row in self.overlaps)
            raise ValueError(f"Panel interiors overlap: {pairs}")
        if thickness is not None and (not math.isfinite(thickness) or thickness < 0):
            raise ValueError("Thickness must be finite and nonnegative")
        if not math.isfinite(fold_offset):
            raise ValueError("Fold offset must be finite")
        if ((thickness or 0)+fold_offset) > NUMERIC_EPS and self.contact_warnings:
            raise ValueError("Thickness compensation is incomplete: " + " ".join(self.contact_warnings))
        fragments, seam_curves, internals, reliefs, corner_reliefs, compensation, lid_fit = self._compensated_geometry(
            thickness, fold_offset, lid_clearance)
        contours = [self._untangle_contour(contour) for contour in self._trace(fragments)]
        for contour in contours:
            try:
                polygon_for(contour)
            except ValueError as exc:
                raise ValueError(f"Compensated exterior cut contour is invalid: {exc}") from exc
        contour_lines = [LineString([point for segment in contour for point in sample_segment(segment)])
                         for contour in contours]
        for index, path in enumerate(corner_reliefs):
            score_corner = path.end
            score_point = Point(score_corner.real, score_corner.imag)
            nearest = min((nearest_points(score_point, line)[1] for line in contour_lines),
                          key=lambda point: point.distance(score_point))
            corner_reliefs[index] = Path(Line(complex(nearest.x, nearest.y), score_corner))
        for path in corner_reliefs:
            junction, score_corner = path.start, path.end
            if min(line.distance(Point(junction.real, junction.imag)) for line in contour_lines) > NUMERIC_EPS:
                raise ValueError("Corner-relief cut does not touch the exterior contour")
            if not any(abs(score_corner-curve.start) <= NUMERIC_EPS or
                       abs(score_corner-curve.end) <= NUMERIC_EPS for curve in seam_curves.values()):
                raise ValueError("Corner-relief cut does not touch a score endpoint")
        min_x = min(path.bbox()[0] for path in contours)
        min_y = min(path.bbox()[2] for path in contours)
        output_offset = complex(10-min_x, 10-min_y)
        emitted_paths = (contours+[Path(curve) for curve in seam_curves.values()]+
                         [path for _, _, path in internals]+reliefs+corner_reliefs)
        stroke_margin = 0.05
        output_bounds = (min(path.bbox()[0] for path in emitted_paths)+output_offset.real,
                         max(path.bbox()[1] for path in emitted_paths)+output_offset.real,
                         min(path.bbox()[2] for path in emitted_paths)+output_offset.imag,
                         max(path.bbox()[3] for path in emitted_paths)+output_offset.imag)
        page_overflow = (output_bounds[0] < stroke_margin-NUMERIC_EPS or
                         output_bounds[1] > self.page[0]-stroke_margin+NUMERIC_EPS or
                         output_bounds[2] < stroke_margin-NUMERIC_EPS or
                         output_bounds[3] > self.page[1]-stroke_margin+NUMERIC_EPS)
        output_transform = translation_2d(output_offset)
        contours = [transform_path(path, output_transform) for path in contours]
        seam_curves = {key: transform_path(Path(curve), output_transform)[0]
                       for key, curve in seam_curves.items()}
        internals = [(panel_id, shape, transform_path(path, output_transform))
                     for panel_id, shape, path in internals]
        reliefs = [transform_path(path, output_transform) for path in reliefs]
        corner_reliefs = [transform_path(path, output_transform) for path in corner_reliefs]
        base_report = self.report(thickness, fold_offset, lid_clearance)
        output_folds = []
        for source_row in base_report["folds"]:
            row = dict(source_row)
            row["source_length"] = row["length"]
            row["length"] = seam_curves[row["id"]].length(error=1e-6)
            row["fitted"] = bool(abs(row["length"]-row["source_length"]) > self.near)
            output_folds.append(row)
        compensation_metadata = self.compensated_contacts(compensation)
        if lid_fit["status"] == "applied":
            for row in compensation_metadata:
                row["verification"] = "local-contact-before-lid-fit"
                row.pop("achieved_midplane_separation", None)
                row.pop("residual", None)
        root = ET.Element(f"{{{SVG}}}svg", width=f"{self.page[0]:g}mm", height=f"{self.page[1]:g}mm",
                          viewBox=f"0 0 {self.page[0]:g} {self.page[1]:g}")
        metadata = {"schema": 4, "source": self.source_name, "units": "mm", "thickness": thickness,
                    "fold_offset": fold_offset, "lid_clearance": lid_clearance,
                     "compiler_options": {"near_distance": self.near, "snap_near": self.snap_near},
                     "output_translation": [output_offset.real, output_offset.imag],
                     "output_bounds": list(output_bounds), "page_overflow": page_overflow,
                    "compensation_rule": "tab crease displacement = thickness + fold_offset; supporting wall end targets the receiver exterior face at thickness / 2",
                    "panels": [{"id": p.id, "outside_hint": p.outside_hint,
                                "internal_geometry": [s.id for s in p.internal]} for p in self.panels],
                    "folds": output_folds, "components": self.components,
                    "structure": self.structure, "lid_fit": lid_fit,
                    "contacts": base_report["contacts"],
                    "contact_diagnostics": self.contact_diagnostics,
                    "compensation": compensation_metadata,
                    "cut_operations": {"tab_reliefs": len(reliefs),
                                       "corner_reliefs": len(corner_reliefs)},
                     "repairs": self.near_seams if self.snap_near else []}
        if self.resize_metadata is not None:
            metadata["resize"] = self.resize_metadata
        metadata_node = ET.SubElement(root, f"{{{SVG}}}metadata")
        node = ET.SubElement(metadata_node, f"{{{BOXXER}}}model")
        node.text = json.dumps(metadata, separators=(",", ":"))
        source_node = ET.SubElement(metadata_node, f"{{{BOXXER}}}source", encoding="base64",
                                    sha256=hashlib.sha256(self.source_bytes).hexdigest())
        source_node.text = base64.b64encode(self.source_bytes).decode("ascii")
        cuts = ET.SubElement(root, f"{{{SVG}}}g", id="boxxer-cuts", stroke="#000000", fill="none",
                             **{"stroke-width": "0.1"})
        for i, contour in enumerate(contours):
            ET.SubElement(cuts, f"{{{SVG}}}path", id=f"cut-{i}", d=contour.d())
        for panel_id, shape, path in internals:
            ET.SubElement(cuts, f"{{{SVG}}}path", id=f"internal-{shape.id}", d=path.d(),
                           **{f"{{{BOXXER}}}panel": panel_id, f"{{{BOXXER}}}source-id": shape.id})
        for i, path in enumerate(reliefs):
            ET.SubElement(cuts, f"{{{SVG}}}path", id=f"relief-{i}", d=path.d(),
                          **{f"{{{BOXXER}}}operation": "relief"})
        for i, path in enumerate(corner_reliefs):
            ET.SubElement(cuts, f"{{{SVG}}}path", id=f"corner-relief-{i}", d=path.d(),
                          **{f"{{{BOXXER}}}operation": "corner-relief"})
        scores = ET.SubElement(root, f"{{{SVG}}}g", id="boxxer-scores", stroke="#ff0000", fill="none",
                               **{"stroke-width": "0.1"})
        for seam in self.seams:
            ET.SubElement(scores, f"{{{SVG}}}path", id=seam.id,
                          d=Path(seam_curves[seam.id]).d(),
                          **{f"{{{BOXXER}}}panels": f"{seam.panel_a} {seam.panel_b}"})
        ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
        return {**base_report, "folds": output_folds, "compensation": compensation_metadata,
                "output": str(output), "cut_contours": len(contours),
                "internal_cuts": sum(len(p.internal) for p in self.panels),
                "tab_relief_cuts": len(reliefs), "corner_relief_cuts": len(corner_reliefs),
                 "relief_cuts": len(reliefs)+len(corner_reliefs),
                 "output_translation": [output_offset.real, output_offset.imag],
                 "output_bounds": list(output_bounds), "page_overflow": page_overflow,
                 "metadata": metadata}
