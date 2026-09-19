"""Inspect, repair, resize, and assemble two-panel box nets. Units are mm."""
import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import statistics
import sys
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box
from svgpathtools import Line, parse_path

SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
TOL = 0.01  # Numeric comparison only; never applied as output snapping.
EPS = 1e-6  # Floating-point/serialization noise, not a manufacturing allowance.


def numbers(text):
    return [float(s) for s in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", text)]


def transform(text):
    result = np.eye(3)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        v = numbers(args)
        m = np.eye(3)
        if name == "scale":
            m[0, 0], m[1, 1] = v[0], v[-1]
        elif name == "translate":
            m[0, 2], m[1, 2] = v[0], v[1] if len(v) > 1 else 0
        elif name == "matrix" and len(v) == 6:
            a, b, c, d, e, f = v
            m = np.array([[a, c, e], [b, d, f], [0, 0, 1]])
        elif name == "rotate":
            a = math.radians(v[0])
            m[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
            if len(v) == 3:
                p = np.array(v[1:])
                m[:2, 2] = p - m[:2, :2] @ p
        else:
            raise ValueError(f"Unsupported SVG transform: {name}")
        result = result @ m
    return result


def mm(text):
    match = re.fullmatch(r"([\d.eE+-]+)(mm|cm|in|px|pt)?", text)
    if not match:
        raise ValueError(f"Unsupported SVG length: {text}")
    return float(match[1]) * {None: 25.4 / 96, "px": 25.4 / 96,
                             "mm": 1, "cm": 10, "in": 25.4, "pt": 25.4 / 72}[match[2]]


def path_data(points):
    return "M " + " L ".join(f"{x:.8f},{y:.8f}" for x, y in points)


@dataclass
class Shape:
    id: str
    color: str
    points: list
    panel: bool = False
    curved: bool = False
    output_d: str | None = None

    @property
    def geometry(self):
        return Polygon(self.points) if self.color == "black" else LineString(self.points)


@dataclass
class Panel:
    shape: Shape
    outline: Shape
    bounds: tuple
    height: float
    depths: list
    role: str = ""

    @property
    def width(self):
        return self.bounds[2] - self.bounds[0]

    @property
    def length(self):
        return self.bounds[3] - self.bounds[1]


class Design:
    def __init__(self, filename):
        root = ET.parse(filename).getroot()
        self.page = (mm(root.get("width", "210mm")), mm(root.get("height", "297mm")))
        vb = numbers(root.get("viewBox", f"0 0 {self.page[0]} {self.page[1]}"))
        if len(vb) != 4 or min(vb[2:]) <= 0:
            raise ValueError("Invalid viewBox")
        sx, sy = self.page[0] / vb[2], self.page[1] / vb[3]
        if abs(sx - sy) > 1e-6:
            raise ValueError("Only matching page/viewBox aspect ratios are supported")
        initial = np.array([[sx, 0, -vb[0] * sx], [0, sy, -vb[1] * sy], [0, 0, 1]])
        self.shapes = []

        def visit(element, matrix, inherited):
            tag = element.tag.split("}")[-1]
            if tag in {"defs", "metadata", "namedview"}:
                return
            if tag in {"style", "use", "clipPath", "mask", "image"}:
                raise ValueError(f"Unsupported SVG element: {tag}; flatten it first")
            style = dict(inherited)
            style.update({k: element.attrib[k] for k in ("stroke", "display", "visibility") if k in element.attrib})
            style.update(dict(s.strip().split(":", 1) for s in element.get("style", "").split(";") if ":" in s))
            if style.get("display") == "none" or style.get("visibility") == "hidden":
                return
            matrix = matrix @ transform(element.get("transform", ""))
            color = {"#000000": "black", "#000": "black", "black": "black",
                     "#ff0000": "red", "#f00": "red", "red": "red"}.get(style.get("stroke", "").strip().lower())
            if tag in {"path", "rect", "line", "polygon", "polyline", "circle", "ellipse"}:
                if not color:
                    raise ValueError(f"Unclassified stroke on {element.get('id', tag)}")
                candidate = False
                if tag == "rect":
                    if float(element.get("rx", 0)) or float(element.get("ry", 0)):
                        raise ValueError("Rounded rectangles must be converted to paths")
                    x, y = float(element.get("x", 0)), float(element.get("y", 0))
                    w, h = float(element.get("width")), float(element.get("height"))
                    d = path_data([(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)])
                    candidate = color == "red"
                elif tag == "path":
                    d = element.get("d", "")
                elif tag == "line":
                    d = path_data([(float(element.get(f"x{i}", 0)), float(element.get(f"y{i}", 0))) for i in (1, 2)])
                elif tag in {"polygon", "polyline"}:
                    values = numbers(element.get("points", ""))
                    pts = list(zip(values[::2], values[1::2]))
                    d = path_data(pts + ([pts[0]] if tag == "polygon" else []))
                else:
                    raise ValueError(f"Convert {tag} to a path first")
                parsed = parse_path(d)
                if not parsed or len(parsed.continuous_subpaths()) != 1:
                    raise ValueError("Each shape must be one continuous path")
                curved = any(not isinstance(s, Line) for s in parsed)
                pts = []
                for segment in parsed:
                    count = 1 if isinstance(segment, Line) else max(32, math.ceil(segment.length() / 0.1))
                    for i in range(count):
                        z = segment.point(i / count)
                        p = matrix @ [z.real, z.imag, 1]
                        pts.append(tuple(p[:2]))
                p = matrix @ [parsed.end.real, parsed.end.imag, 1]
                pts.append(tuple(p[:2]))
                if color == "black" and math.dist(pts[0], pts[-1]) > 1e-6:
                    raise ValueError("This version requires closed black cut outlines")
                if color == "red" and len(pts) == 5 and math.dist(pts[0], pts[-1]) < 1e-6:
                    candidate = all(abs(a[0]-b[0]) < TOL or abs(a[1]-b[1]) < TOL for a, b in zip(pts, pts[1:]))
                self.shapes.append(Shape(element.get("id", f"shape{len(self.shapes)}"), color, pts, candidate, curved))
            for child in element:
                visit(child, matrix, style)

        visit(root, initial, {})
        self.outlines = [s for s in self.shapes if s.color == "black"]
        if len(self.outlines) not in (1, 2) or any(not s.geometry.is_valid for s in self.outlines):
            raise ValueError("Expected one or two valid closed box outlines")
        self.panels = []
        for shape in [s for s in self.shapes if s.panel]:
            bounds = Polygon(shape.points).bounds
            if abs(Polygon(shape.points).area - box(*bounds).area) > TOL:
                raise ValueError("Main panels must be axis-aligned rectangles")
            owners = [s for s in self.outlines if s.geometry.buffer(TOL).covers(box(*bounds))]
            if len(owners) != 1:
                raise ValueError(f"Cannot assign main panel {shape.id} to an outline")
            outline = owners[0]
            x0, y0, x1, y1 = bounds
            depths = []
            for start, end in [((x0, (y0+y1)/2), (x0-1000, (y0+y1)/2)),
                               ((x1, (y0+y1)/2), (x1+1000, (y0+y1)/2))]:
                hit = LineString([start, end]).intersection(outline.geometry.boundary)
                if hit.geom_type != "Point":
                    raise ValueError("Cannot infer side-wall depth")
                depths.append(math.dist(start, hit.coords[0]))
            height = statistics.mean(depths)
            if abs(depths[0]-depths[1]) > TOL:
                raise ValueError("Unequal side-wall depths require an explicit template (not yet supported)")
            self.panels.append(Panel(shape, outline, bounds, height, depths))
        if len(self.panels) != 2:
            raise ValueError("Expected exactly two rectangular main-panel score loops")
        self.panels.sort(key=lambda p: p.bounds[1])
        self.connected = len(self.outlines) == 1
        large, small = sorted(self.panels, key=lambda p: p.width*p.length, reverse=True)
        if large.width < small.width-TOL or large.length < small.length-TOL or abs(large.width*large.length-small.width*small.length) < TOL:
            raise ValueError("Ambiguous lid/base assignment")
        large.role, small.role = "lid", "base"
        self.lid, self.base = large, small
        if self.connected and (self.panels[0] is not self.lid or self.base.bounds[1] <= self.lid.bounds[3]):
            raise ValueError("Connected layout must have lid above base")
        self.connector_depth = self.base.bounds[1]-self.lid.bounds[3] if self.connected else None

    def tab_contacts(self):
        """Candidate inward-90-degree tab contacts for the supported tray family.

        A side-wall tab's folded middle plane is at its score's Y coordinate.
        The receiving end wall's middle plane is at the main rectangle edge.
        Their separation is consequently the implied sheet thickness.
        """
        contacts = []
        for s in self.shapes:
            if s.color != "red" or s.panel:
                continue
            if s.curved or len(s.points) != 2 or abs(s.points[0][1]-s.points[1][1]) > EPS:
                raise ValueError(f"Cannot interpret tab score {s.id}: expected a horizontal segment")
            cx = statistics.mean(x for x, _ in s.points)
            y = statistics.mean(y for _, y in s.points)
            candidates = []
            for p in self.panels:
                x0, y0, x1, y1 = p.bounds
                if not (x0-p.height-TOL <= cx < x0 or x1 < cx <= x1+p.height+TOL):
                    continue
                for edge, anchor, sign in (("top", y0, 1), ("bottom", y1, -1)):
                    offset = sign*(y-anchor)
                    if 0 < offset < p.height/4:
                        candidates.append((abs(offset), p, edge, anchor, sign))
            if len(candidates) != 1:
                raise ValueError(f"Ambiguous tab/wall contact for {s.id}")
            offset, p, edge, anchor, sign = candidates[0]
            contacts.append({"shape": s, "panel": p, "edge": edge,
                             "anchor": anchor, "sign": sign, "offset": offset})
        return contacts

    def score_analysis(self, thickness=None):
        contacts = self.tab_contacts()
        if thickness is not None and (not math.isfinite(thickness) or thickness <= 0):
            raise ValueError("Thickness must be finite and positive")
        inferred = thickness is None
        if inferred:
            # Precision is explicit: infer nominal stock thickness to 0.001 mm.
            # Residuals are still reported, not forgiven by that rounding.
            if len(contacts) < 3:
                raise ValueError("At least three tab contacts are needed to infer thickness; supply --thickness")
            thickness = round(statistics.median(c["offset"] for c in contacts), 3)
            if thickness <= 0:
                raise ValueError("Cannot infer a positive sheet thickness")
            support = sum(abs(c["offset"]-thickness) <= 0.001 for c in contacts)
            if support <= len(contacts)/2:
                raise ValueError("No majority thickness agreement between tab contacts; supply --thickness")
        rows = []
        issues = []
        for c in contacts:
            s, p = c["shape"], c["panel"]
            target = c["anchor"]+c["sign"]*thickness
            residual = c["offset"]-thickness
            rows.append({"score": s.id, "panel": p.role, "edge": c["edge"],
                         "implied_thickness": c["offset"], "contact_gap": residual,
                         "target_y": target})
            if abs(residual) > EPS:
                issues.append(f"Score {s.id}: tab/wall {'gap' if residual > 0 else 'interference'} {abs(residual):.6f} mm")
            for index, pt in enumerate(s.points):
                distance = p.outline.geometry.boundary.distance(Point(pt))
                if distance > EPS:
                    issues.append(f"Score {s.id} endpoint {index}: {distance:.6f} mm from cut boundary")
        return {"thickness": thickness, "thickness_source": "inferred (0.001 mm precision)" if inferred else "specified",
                "assumption": "Inward 90-degree side tabs against end walls; not a full 3D assembly check",
                "contacts": rows, "issues": issues}

    def fix_scores(self, thickness=None, max_move=0.25, align_reliefs=False):
        """Repair scores, optionally aligning their adjoining cut shoulders."""
        if not math.isfinite(max_move) or max_move <= 0:
            raise ValueError("Maximum repair movement must be finite and positive")
        if any(s.curved for s in self.shapes):
            raise ValueError("Score repair currently requires straight-segment input to preserve the cut geometry")
        before = self.score_analysis(thickness)
        thickness = before["thickness"]
        changes, unresolved = [], []
        anchor_changes = []
        if align_reliefs:
            # Repair drafting roundoff at main-score anchors as actual SVG edits,
            # so downstream face extraction needs no geometric gap allowance.
            anchors = [[p.bounds[axis] for p in self.panels] +
                       [p.bounds[axis+2] for p in self.panels] for axis in (0, 1)]
            for outline in self.outlines:
                points, edits = [], []
                for index, old in enumerate(outline.points):
                    new = list(old)
                    for axis in (0, 1):
                        value = min(anchors[axis], key=lambda v: abs(v-old[axis]))
                        if EPS < abs(value-old[axis]) <= min(0.001, max_move/2):
                            new[axis] = value
                    if math.dist(old, new) > EPS:
                        edits.append({"outline": outline.id, "vertex": index,
                                      "before": list(old), "after": new})
                    points.append(tuple(new))
                if not Polygon(points).is_valid:
                    raise ValueError("Main-score anchor repair would invalidate the cut outline")
                outline.points = points
                anchor_changes.extend(edits)
        for c in self.tab_contacts():
            s, p = c["shape"], c["panel"]
            y = c["anchor"]+c["sign"]*thickness
            if max(abs(y-pt[1]) for pt in s.points) > max_move:
                unresolved.append(f"Score {s.id}: contact repair exceeds --max-move {max_move:g} mm")
                continue
            outline_before = p.outline.points
            cut_changes = []
            if align_reliefs:
                # Only the short horizontal relief adjoining the tab's inner
                # endpoint is eligible. No whole-outline grid snapping.
                x0, _, x1, _ = p.bounds
                inner = min(s.points, key=lambda pt: min(abs(pt[0]-x0), abs(pt[0]-x1)))
                pts = list(outline_before[:-1])
                shoulders = []
                for i, pt in enumerate(pts):
                    if abs(pt[0]-inner[0]) > 0.001 or abs(pt[1]-y) > max_move:
                        continue
                    for j in ((i-1) % len(pts), (i+1) % len(pts)):
                        other = pts[j]
                        if abs(pt[1]-other[1]) <= EPS and EPS < abs(pt[0]-other[0]) < p.height/2:
                            # The other end must point toward the main panel.
                            if min(abs(other[0]-x0), abs(other[0]-x1)) < min(abs(pt[0]-x0), abs(pt[0]-x1)):
                                shoulders.append((i, j))
                if len(shoulders) != 1:
                    unresolved.append(f"Score {s.id}: no unique adjoining cut shoulder within --max-move")
                    continue
                for index in shoulders[0]:
                    old = pts[index]
                    new = (old[0], y)
                    if math.dist(old, new) > EPS:
                        cut_changes.append({"outline": p.outline.id, "vertex": index,
                                            "before": old, "after": new})
                        pts[index] = new
                        # A drafted relief may contain intermediate collinear
                        # vertices. Collapse those swept by the moving shoulder
                        # so its vertical edge cannot double back on itself.
                        for k, step in enumerate(pts):
                            if abs(step[0]-old[0]) <= EPS and min(old[1], y) < step[1] < max(old[1], y):
                                cut_changes.append({"outline": p.outline.id, "vertex": k,
                                                    "before": step, "after": (step[0], y)})
                                pts[k] = (step[0], y)
                proposed = Polygon(pts)
                if not proposed.is_valid or any(not proposed.buffer(TOL).covers(box(*q.bounds)) for q in self.panels if q.outline is p.outline):
                    unresolved.append(f"Score {s.id}: relief alignment would damage the outline or main panel")
                    continue
                p.outline.points = pts+[pts[0]]
            # Intersect the target fold's supporting line with the real cut.
            # Endpoints may move along this line, never off it to a nearby vertex.
            bounds = p.outline.geometry.bounds
            hits = p.outline.geometry.boundary.intersection(LineString([(bounds[0]-1, y), (bounds[2]+1, y)]))
            def endpoints(geometry):
                if geometry.is_empty:
                    return []
                if geometry.geom_type == "Point":
                    return [tuple(geometry.coords[0])]
                if geometry.geom_type == "LineString":
                    return [tuple(geometry.coords[0]), tuple(geometry.coords[-1])]
                return [pt for part in geometry.geoms for pt in endpoints(part)]

            hits = [Point(pt) for pt in set(endpoints(hits))]
            new_points = []
            reasons = []
            for index, pt in enumerate(s.points):
                candidates = [h for h in hits if math.dist(pt, h.coords[0]) <= max_move]
                if len(candidates) != 1:
                    reasons.append(f"endpoint {index} needs a cut-outline change or has no unique boundary target within {max_move:g} mm")
                else:
                    new_points.append(tuple(candidates[0].coords[0]))
            if reasons:
                p.outline.points = outline_before
                unresolved.append(f"Score {s.id}: " + "; ".join(reasons))
                continue
            line = LineString(new_points)
            x0, _, x1, _ = p.bounds
            on_side = max(x for x, _ in new_points) <= x0+EPS or min(x for x, _ in new_points) >= x1-EPS
            if line.length <= EPS or not on_side or not p.outline.geometry.buffer(EPS).covers(line) or line.intersection(p.outline.geometry.boundary).length > EPS:
                p.outline.points = outline_before
                unresolved.append(f"Score {s.id}: proposed fold leaves its side wall or crosses a cut")
                continue
            movement = max(math.dist(a, b) for a, b in zip(s.points, new_points))
            if movement > EPS or cut_changes:
                changes.append({"score": s.id, "before": s.points, "after": new_points,
                                "max_movement": movement,
                                "cut_changes": cut_changes,
                                "reason": "Align inward tab contact and terminate on cut boundary"})
                s.points = new_points
        after = self.score_analysis(thickness)
        return {"thickness": thickness, "thickness_source": before["thickness_source"],
                "changes": changes, "anchor_changes": anchor_changes,
                "unresolved": unresolved, "remaining_issues": after["issues"],
                "complete": not unresolved and not after["issues"]}

    def report(self, thickness=None, scores=False):
        warnings = []
        for shape in self.shapes:
            if shape.color == "red":
                if not any(s.geometry.buffer(TOL).covers(shape.geometry) for s in self.outlines):
                    warnings.append(f"Score {shape.id} extends beyond material (tolerance {TOL} mm)")
        for s in self.outlines:
            if not box(0, 0, *self.page).covers(s.geometry):
                warnings.append(f"Outline {s.id} extends beyond the page")
        if len(self.outlines) == 2 and self.outlines[0].geometry.intersection(self.outlines[1].geometry).area > TOL:
            warnings.append("Separate pieces overlap")
        for p in self.panels:
            outer_y = p.outline.geometry.bounds[1 if p is self.panels[0] else 3]
            fold_y = p.bounds[1 if p is self.panels[0] else 3]
            if abs(abs(outer_y-fold_y)-p.height) > TOL:
                warnings.append(f"{p.role} outer flap depth differs from nominal side-wall height")
        result = {"layout": "connected" if self.connected else "separate",
                "panels": {p.role: {"id": p.shape.id, "width": p.width, "length": p.length,
                                    "height": p.height, "bounds": list(p.bounds)} for p in self.panels},
                "heights_linked": abs(self.lid.height-self.base.height) <= TOL,
                "connector_depth": self.connector_depth,
                "lid_minus_base": [self.lid.width-self.base.width, self.lid.length-self.base.length],
                "fit": "Nominal fold-to-fold dimensions only; physical fit not verified",
                "warnings": warnings}
        if scores or thickness is not None:
            result["scores"] = self.score_analysis(thickness)
            result["warnings"].extend(result["scores"]["issues"])
        return result

    def resize(self, height=None, base_height=None, lid_height=None, width=None, length=None, connector_depth=None):
        if any(s.curved for s in self.shapes):
            raise ValueError("Resizing existing curves is not supported yet; resize the source and add the notch afterward")
        linked = abs(self.lid.height-self.base.height) <= TOL
        targets = {"base": self.base.height, "lid": self.lid.height}
        if height is not None:
            targets["base"] = height
            if linked:
                targets["lid"] = height
        if base_height is not None:
            targets["base"] = base_height
        if lid_height is not None:
            targets["lid"] = lid_height
        dw = 0 if width is None else width-self.base.width
        dl = 0 if length is None else length-self.base.length
        if any(not math.isfinite(v) or v <= 1 for v in targets.values()):
            raise ValueError("Heights must be finite and greater than 1 mm")
        if any(min(p.width+dw, p.length+dl) <= 0 for p in self.panels):
            raise ValueError("Footprint dimensions must remain positive")
        if connector_depth is not None and (not self.connected or not math.isfinite(connector_depth) or connector_depth <= 0):
            raise ValueError("--connector-depth requires a connected design and a finite positive depth")
        new_connector = None
        if self.connected:
            new_connector = (self.connector_depth+targets["base"]-self.base.height
                             if connector_depth is None else connector_depth)
            if new_connector <= 0:
                raise ValueError("Requested height eliminates the connector")
        shifts = {id(p): 0.0 for p in self.panels}
        upper, lower = self.panels
        if self.connected:
            shifts[id(lower)] = dl + new_connector-self.connector_depth
        else:
            # Preserve the original vertical gap between the separate pieces.
            shifts[id(lower)] = dl + targets[upper.role]-upper.height + targets[lower.role]-lower.height

        side_regions = {}
        if self.connected:
            # The tabs of the base can extend well into the connector's Y range.
            # Assign them by material connectivity, not by the connector midpoint.
            for side, clip in (
                ("left", box(-10000, -10000, min(p.bounds[0] for p in self.panels)-1.01, 10000)),
                ("right", box(max(p.bounds[2] for p in self.panels)+1.01, -10000, 10000, 10000)),
            ):
                pieces = upper.outline.geometry.intersection(clip)
                regions = list(pieces.geoms) if pieces.geom_type == "MultiPolygon" else [pieces]
                if len(regions) != 2 or any(r.geom_type != "Polygon" for r in regions):
                    raise ValueError("Cannot separate connected side walls and tabs")
                regions.sort(key=lambda r: r.bounds[1])
                side_regions[side] = list(zip(self.panels, regions))

        reliefs = {}
        for p in self.panels:
            x0, y0, x1, y1 = p.bounds
            points = p.outline.points
            # Infer the inner ends of the side tabs from their distance to the
            # main-panel edge. These relief widths stay fixed as walls grow.
            left = [x0-x for x, y in points if y0-TOL <= y <= y1+TOL and TOL < x0-x < p.height-TOL]
            right = [x-x1 for x, y in points if y0-TOL <= y <= y1+TOL and TOL < x-x1 < p.height-TOL]
            # Preserve the small end offsets around folds. Larger protrusions
            # are overlap tabs, whose lengths are fixed by the side-tab rule.
            ends = [min(abs(y-y0), abs(y-y1)) for x, y in points
                    if x0-p.height-TOL <= x <= x1+p.height+TOL
                    and TOL < min(abs(y-y0), abs(y-y1)) < p.height/4]
            reliefs[id(p)] = (min(left, default=0), min(right, default=0), max(ends, default=0))

        def map_panel(p, x, y):
            x0, y0, x1, y1 = p.bounds
            dh = targets[p.role]-p.height
            left_relief, right_relief, end_relief = reliefs[id(p)]
            if targets[p.role] <= max(left_relief, right_relief, end_relief):
                raise ValueError("Requested height is smaller than the detected reliefs")
            if x < x0:
                distance = x0-x
                xx = x-dw/2 - dh*max(0, distance-left_relief)/(p.height-left_relief)
            elif x > x1:
                distance = x-x1
                xx = x+dw/2 + dh*max(0, distance-right_relief)/(p.height-right_relief)
            else:
                xx = x + dw*((x-x0)/p.width-0.5)
            if y < y0:
                # Side tabs keep their overlap length; front walls grow.
                yy = y-dh*max(0, y0-y-end_relief)/(p.height-end_relief) if x0-TOL <= x <= x1+TOL else y
            elif y > y1:
                yy = y+dl+(dh*max(0, y-y1-end_relief)/(p.height-end_relief) if x0-TOL <= x <= x1+TOL else 0)
            else:
                # Preserve end reliefs and translate attached tabs with their fold.
                t = min(1, max(0, (y-y0-end_relief)/(p.length-2*end_relief))) if p.length > 2*end_relief else (y-y0)/p.length
                yy = y+dl*t
            return xx, yy+shifts[id(p)]

        def mapping(s, x, y):
            if not self.connected:
                if s.color == "black":
                    p = next(p for p in self.panels if p.outline is s)
                else:
                    p = min(self.panels, key=lambda p: p.outline.geometry.distance(s.geometry))
                return map_panel(p, x, y)
            a, b = upper.bounds[3], lower.bounds[1]
            if y <= a:
                return map_panel(upper, x, y)
            if y >= b:
                return map_panel(lower, x, y)
            # Connector and adjacent tabs travel with their nearest panel; only
            # the tapered bridge between the two main-panel edges stretches.
            t = (y-a)/(b-a)
            left = upper.bounds[0]*(1-t)+lower.bounds[0]*t
            right = upper.bounds[2]*(1-t)+lower.bounds[2]*t
            if left-TOL <= x <= right+TOL:
                xx = x+dw*((x-left)/(right-left)-0.5)
                return xx, a+dl+(y-a)*(new_connector/self.connector_depth)
            regions = side_regions["left" if x < left else "right"]
            owner = min(regions, key=lambda pr: pr[1].distance(Point(x, y)))[0]
            return map_panel(owner, x, y)

        # Calculate all shapes before changing geometry used for ownership tests.
        updates = [(s, [mapping(s, x, y) for x, y in s.points]) for s in self.shapes]
        for s, points in updates:
            if s.color == "black" and not Polygon(points).is_valid:
                raise ValueError("Resize creates an invalid cut outline; requested dimensions are unsupported")
        for s, points in updates:
            s.points = points
        for p in self.panels:
            p.bounds = Polygon(p.shape.points).bounds
            p.height = targets[p.role]
        self.connector_depth = new_connector

    def notch(self, width=25, depth=15):
        if not all(math.isfinite(v) and v > 0 for v in (width, depth)):
            raise ValueError("Notch dimensions must be finite and positive")
        panel = self.panels[0]
        shape = panel.outline
        x0, fold, x1, _ = panel.bounds
        cx = (x0+x1)/2
        candidates = [(i, a, b) for i, (a, b) in enumerate(zip(shape.points, shape.points[1:]))
                      if abs(a[1]-b[1]) < TOL and a[1] < fold
                      and min(a[0], b[0]) < cx < max(a[0], b[0])]
        if not candidates:
            raise ValueError("No straight uppermost flap edge available for a notch")
        i, a, b = min(candidates, key=lambda c: c[1][1])
        edge = a[1]
        actual = min(depth, fold-edge-2)
        r = width/2
        if actual < r:
            raise ValueError(f"A {width:g} mm semicircular notch needs at least {r:g} mm depth; only {actual:g} mm is available with 2 mm fold clearance")
        if cx-r <= min(a[0], b[0])+TOL or cx+r >= max(a[0], b[0])-TOL:
            raise ValueError("Notch is too wide for the flap")
        cy = edge+actual-r
        # Polygonal boolean for topology validation; export the exact circle arc.
        arc = [(cx+r*math.cos(t), cy+r*math.sin(t)) for t in np.linspace(0, math.pi, 257)]
        cutter = Polygon([(cx-r, edge-1), (cx+r, edge-1)] + arc + [(cx-r, edge-1)])
        difference = shape.geometry.difference(cutter)
        if difference.geom_type != "Polygon" or not difference.is_valid or difference.interiors:
            raise ValueError("Notch would split material or create a hole")
        direction = 1 if b[0] > a[0] else -1
        first, last = cx-direction*r, cx+direction*r
        d = path_data(shape.points[:i+1])
        d += f" L {first:.8f},{edge:.8f} L {first:.8f},{cy:.8f}"
        d += f" A {r:.8f},{r:.8f} 0 0 {0 if direction == 1 else 1} {last:.8f},{cy:.8f}"
        d += f" L {last:.8f},{edge:.8f}"
        d += "".join(f" L {x:.8f},{y:.8f}" for x, y in shape.points[i+1:]) + " Z"
        shape.output_d = d
        shape.points = list(difference.exterior.coords)
        return {"width": width, "requested_depth": depth, "actual_depth": actual,
                "fold_clearance": fold-edge-actual}

    def write(self, filename, preview=False):
        root = ET.Element(f"{{{SVG}}}svg", width=f"{self.page[0]}mm", height=f"{self.page[1]}mm",
                          viewBox=f"0 0 {self.page[0]} {self.page[1]}")
        for s in self.shapes:
            ET.SubElement(root, f"{{{SVG}}}path", id=s.id,
                          d=s.output_d or path_data(s.points)+( " Z" if s.color == "black" else ""),
                          fill="none", stroke=s.color, **{"stroke-width": "0.1"})
        if preview:
            for p in self.panels:
                x0, y0, x1, y1 = p.bounds
                text = ET.SubElement(root, f"{{{SVG}}}text", x=str((x0+x1)/2), y=str((y0+y1)/2),
                                     fill="blue", **{"font-size": "3", "text-anchor": "middle"})
                text.text = f"{p.role}: {p.width:.2f} × {p.length:.2f} × {p.height:.2f} mm"
        ET.ElementTree(root).write(filename, encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "check", "resize", "fix", "assemble"):
        p = sub.add_parser(name)
        p.add_argument("input", type=Path)
        p.add_argument("--json", action="store_true")
        if name != "resize":
            p.add_argument("--details", action="store_true", help="Show individual intersections, IDs, and repair details")
            p.add_argument("--ascii", action="store_true", help="Use plain ASCII for the terminal diagram")
        if name in ("inspect", "check", "fix", "assemble"):
            p.add_argument("--thickness", type=lambda v: None if v == "auto" else float(v), help="Sheet thickness in mm, or auto (default)")
        if name in ("inspect", "check"):
            p.add_argument("--scores", action="store_true", help="Check inward tab contacts and exact score endpoints")
            mode = p.add_mutually_exclusive_group()
            mode.add_argument("--flat", action="store_true", help="Check only the drawing; explicitly skip assembled fit")
            mode.add_argument("--folded", dest="flat", action="store_false", help="Compatibility alias: full 3D inspection is now the default")
            p.set_defaults(flat=False)
        if name in ("inspect", "check", "assemble"):
            p.add_argument("--lid-lift", type=float, default=0, help="Raise a separate lid above its seated position, in mm")
        if name == "inspect":
            p.add_argument("--preview", type=Path)
        if name in ("resize", "fix", "assemble"):
            p.add_argument("--output", type=Path, required=True)
        if name == "assemble":
            p.add_argument("--report", type=Path, help="JSON report path (default: output path with .json suffix)")
            p.add_argument("--base-output", type=Path, help="Also export base-role panels as STL")
            p.add_argument("--lid-output", type=Path, help="Also export lid-role panels as STL")
        if name == "fix":
            p.add_argument("--max-move", type=float, default=0.25, help="Maximum score endpoint or relief vertex movement in mm (default: 0.25)")
            p.add_argument("--align-reliefs", action="store_true", help="Also align adjoining cut shoulders with the repaired scores")
            p.add_argument("--report", type=Path, help="Write repair details as JSON")
            p.add_argument("--fit", action="store_true", help="Also apply supported dimension-changing fit repairs and verify the result")
            p.add_argument("--top-clearance", type=float, help="With --fit: desired rim-to-lid clearance for a connected box (default: 0 mm)")
        if name == "resize":
            for flag in ("height", "base-height", "lid-height", "width", "length", "connector-depth"):
                p.add_argument(f"--{flag}", type=float)
            p.add_argument("--finger-notch", action="store_true")
            p.add_argument("--notch-width", type=float, default=25)
            p.add_argument("--notch-depth", type=float, default=15)
    args = parser.parse_args()
    try:
        thickness = getattr(args, "thickness", None)
        if thickness is not None and (not math.isfinite(thickness) or thickness <= 0):
            raise ValueError("Thickness must be finite and positive")
        lift = getattr(args, "lid_lift", 0)
        if not math.isfinite(lift) or lift < 0:
            raise ValueError("Lid lift must be finite and nonnegative")
        if args.command == "assemble" and args.report is None:
            args.report = args.output.with_suffix(".json")
        if getattr(args, "lid_lift", 0) and getattr(args, "flat", False):
            raise ValueError("--lid-lift cannot be used with --flat")
        if args.command == "fix" and args.top_clearance is not None and not args.fit:
            raise ValueError("--top-clearance requires fix --fit")
        destinations = []
        for name in ("output", "preview", "report", "base_output", "lid_output"):
            destination = getattr(args, name, None)
            if destination and destination.resolve() == args.input.resolve():
                raise ValueError("Use a different output path to preserve the source design")
            if destination:
                if destination.resolve() in destinations:
                    raise ValueError("Output and report paths must be different")
                destinations.append(destination.resolve())
        design = Design(args.input)
        notch = None
        repair = None
        fit_repair = None
        if args.command == "fix":
            repair = design.fix_scores(args.thickness, args.max_move, args.align_reliefs)
            if args.fit:
                from inspection import repair_fit
                fit_repair = repair_fit(design, repair["thickness"], args.top_clearance or 0)
            design.write(args.output)
        if args.command == "resize":
            values = {k: getattr(args, k) for k in ("height", "base_height", "lid_height", "width", "length", "connector_depth")}
            if any(v is not None and not math.isfinite(v) for v in values.values()):
                raise ValueError("Dimensions must be finite")
            design.resize(**values)
            if args.finger_notch:
                notch = design.notch(args.notch_width, args.notch_depth)
            design.write(args.output)
        if getattr(args, "preview", None):
            design.write(args.preview, preview=True)
        if args.command == "resize":
            report = design.report()
        else:
            from inspection import inspect_design
            thickness = repair["thickness"] if repair else args.thickness
            report = inspect_design(design, thickness, flat=getattr(args, "flat", False),
                                    scores=getattr(args, "scores", False), lid_lift=getattr(args, "lid_lift", 0))
        if args.command == "assemble":
            from folded import Assembly
            if "assembly" not in report:
                raise ValueError(report["assembly_reason"])
            assembly = Assembly(design, args.thickness, args.lid_lift)
            assembly.write_stl(args.output)
            if args.base_output:
                assembly.write_stl(args.base_output, "base")
            if args.lid_output:
                assembly.write_stl(args.lid_output, "lid")
            args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        if repair is not None:
            report["repair"] = repair
            report["repair"]["scope"] = "score/relief geometry only; see the assembly check for overall fit"
            if fit_repair is not None:
                report["fit_repair"] = fit_repair
            if args.report:
                args.report.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        if notch:
            report["notch"] = notch
        if args.json:
            print(json.dumps(report, indent=2))
        elif args.command != "resize":
            from terminal import render_report
            print(render_report(report, details=args.details, ascii_only=args.ascii))
            if args.command in ("fix", "assemble"):
                print(f"\nWrote {args.output}")
        else:
            print(f"Layout: {report['layout']}")
            for role, p in report["panels"].items():
                print(f"{role.title()}: {p['width']:.3f} × {p['length']:.3f} mm; height {p['height']:.3f} mm")
            print("Heights:", "linked" if report["heights_linked"] else "independent")
            print("Lid − base: " + " × ".join(f"{v:.3f}" for v in report["lid_minus_base"]) + " mm")
            print(report["fit"])
            if notch:
                print(f"Notch: {notch['width']:g} mm wide, {notch['actual_depth']:.3f} mm deep; fold clearance {notch['fold_clearance']:.3f} mm")
            for warning in report["warnings"]:
                print("Warning:", warning)
        return 1 if (args.command == "check" and report["warnings"]) or (repair is not None and (not repair["complete"] or report["warnings"])) else 0
    except (ValueError, OSError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
