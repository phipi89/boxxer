"""Procedural circular finger openings, clipped to retain material clearance."""
import math

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from svgpathtools import Arc, CubicBezier, Line, Path
from svgpathtools.path import transform as transform_path

MARGIN = 3.0
CURVE_ERROR = 0.00005


def clearance_points(segment):
    """Flatten native curves with a bounded chord error for clearance checks."""
    if isinstance(segment, Line):
        points = [segment.start, segment.end]
    elif isinstance(segment, Arc):
        count = max(1, math.ceil(abs(math.radians(segment.delta))*
                                math.sqrt(max(abs(segment.radius.real), abs(segment.radius.imag))/(8*CURVE_ERROR))))
        points = [segment.start]+[segment.point(i/count) for i in range(1, count)]+[segment.end]
    else:
        controls = ([segment.control1, segment.control2] if isinstance(segment, CubicBezier)
                    else [segment.control])
        vector = segment.end-segment.start

        def distance(point):
            fraction = max(0, min(1, ((point-segment.start)*vector.conjugate()).real/abs(vector)**2)) if abs(vector) > 1e-12 else 0
            return abs(point-segment.start-vector*fraction)

        if max(map(distance, controls)) <= CURVE_ERROR:
            points = [segment.start, segment.end]
        else:
            return clearance_points(segment.cropped(0, .5))[:-1]+clearance_points(segment.cropped(.5, 1))
    return [(point.real, point.imag) for point in points]


def settings(stored=None, *, mode=None, diameter=None, depth=None):
    if stored is not None and not isinstance(stored, dict):
        raise ValueError("Finger-cutout settings must be an object")
    result = {"mode": "none", "diameter": 30.0, "depth": 10.0, **(stored or {})}
    for name, value in (("mode", mode), ("diameter", diameter), ("depth", depth)):
        if value is not None:
            result[name] = value
    if result["mode"] not in ("none", "one", "opposing"):
        raise ValueError("Finger cutout must be none, one, or opposing")
    for name in ("diameter", "depth"):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Finger-cutout {name} must be finite and positive")
    if result["depth"] > result["diameter"]/2:
        raise ValueError("Finger-cutout depth cannot exceed half its diameter")
    return {name: result[name] for name in ("mode", "diameter", "depth")}


def cap_path(radius, depth, half_width, clip_depth):
    """Exact circular cap clipped by a symmetric rectangle; left to right."""
    center_y = depth-radius
    width = min(half_width, math.sqrt(max(0, radius**2-center_y**2)))
    shoulder = center_y+math.sqrt(max(0, radius**2-width**2))
    parts = []

    def line(start, end):
        if abs(end-start) > 1e-9:
            parts.append(Line(start, end))

    def arc(start, end):
        if abs(end-start) > 1e-9:
            parts.append(Arc(start, complex(radius, radius), 0, False, False, end))

    if clip_depth <= shoulder:
        line(complex(-width, 0), complex(-width, clip_depth))
        line(complex(-width, clip_depth), complex(width, clip_depth))
        line(complex(width, clip_depth), complex(width, 0))
    else:
        left, right = complex(-width, shoulder), complex(width, shoulder)
        line(complex(-width, 0), left)
        if clip_depth < depth-1e-9:
            top_x = math.sqrt(max(0, radius**2-(clip_depth-center_y)**2))
            arc(left, complex(-top_x, clip_depth))
            line(complex(-top_x, clip_depth), complex(top_x, clip_depth))
            arc(complex(top_x, clip_depth), right)
        else:
            arc(left, right)
        line(right, complex(width, 0))
    return Path(*parts)


def selected_walls(graph, mode):
    panels = {panel.id: panel for panel in graph.panels}
    adjacency = graph._adjacency()
    structure = graph.structure
    if structure.get("status") == "inferred":
        principal = structure["top"]
        connector = set(structure.get("shared_connector", []))
    else:
        model = graph._resize_structure()
        if model.get("kind") != "single":
            raise ValueError("Cannot identify a principal face for finger-cutout placement")
        principal, connector = model["principal"], set()
    origin = complex(*panels[principal].polygon.centroid.coords[0])
    candidates = [(identifier, seam) for identifier, seam in adjacency[principal]
                  if identifier not in connector and isinstance(seam.curve, Line)]
    if not candidates:
        raise ValueError("No free wall opposite the lid hinge for a finger cutout")

    def direction(candidate):
        seam = candidate[1]
        vector = (seam.start+seam.end)/2-origin
        return vector/abs(vector)

    if connector:
        hinge = next((seam for identifier, seam in adjacency[principal] if identifier in connector), None)
        if hinge is None:
            raise ValueError("Cannot identify the lid hinge for a finger cutout")
        target = origin-(hinge.start+hinge.end)/2
        target /= abs(target)
    else:
        target = -1j  # Page-top free wall for an unhinged box.
    ranked = sorted(candidates, key=lambda candidate: -(direction(candidate)*target.conjugate()).real)
    if len(ranked) > 1 and abs(((direction(ranked[0])-direction(ranked[1]))*target.conjugate()).real) < 1e-7:
        raise ValueError("Top finger-cutout wall is ambiguous")
    first = ranked[0]
    result = [first]
    if mode == "opposing":
        opposite = [candidate for candidate in candidates
                    if (direction(candidate)*direction(first).conjugate()).real < -1+1e-7]
        if len(opposite) != 1:
            raise ValueError("Opposing finger cutouts require two free opposite walls (not a hinge panel)")
        result.append(opposite[0])
    return result


def apply_cutouts(graph, contour_data, fragments, seams, internals, config,
                  *, polygon_for, rounded_path):
    if config["mode"] == "none":
        return contour_data, []
    result, reports = list(contour_data), []
    for owner, hinge in selected_walls(graph, config["mode"]):
        panel = next(panel for panel in graph.panels if panel.id == owner)
        hinge_curve = seams[hinge.id]
        midpoint = (hinge_curve.start+hinge_curve.end)/2
        tangent = (hinge_curve.end-hinge_curve.start)/abs(hinge_curve.end-hinge_curve.start)
        normal = tangent*1j
        centroid = complex(*panel.polygon.centroid.coords[0])
        outward = normal if ((centroid-midpoint)*normal.conjugate()).real > 0 else -normal
        candidates = [fragment.segment for fragment in fragments if fragment.owner == owner and
                      isinstance(fragment.segment, Line) and
                      abs(((fragment.segment.end-fragment.segment.start)*tangent.conjugate()).imag) < 1e-7]
        candidates = [edge for edge in candidates if ((edge.point(.5)-midpoint)*outward.conjugate()).real > 1e-7]
        if not candidates:
            reports.append({"panel": owner, "status": "omitted", "reason": "no straight free edge"})
            continue
        distance = max(((edge.point(.5)-midpoint)*outward.conjugate()).real for edge in candidates)
        center = midpoint+outward*distance
        candidates = [edge for edge in candidates if abs(((edge.point(.5)-center)*outward.conjugate()).real) < graph.near and
                      0 < ((center-edge.start)/(edge.end-edge.start)).real < 1]
        if len(candidates) != 1:
            reports.append({"panel": owner, "status": "omitted", "reason": "free-edge centre is not available"})
            continue
        edge = candidates[0]
        locations = [(ci, si, segment) for ci, (path, _) in enumerate(result) for si, segment in enumerate(path)
                     if isinstance(segment, Line) and min(
                         max(abs(segment.start-edge.start), abs(segment.end-edge.end)),
                         max(abs(segment.start-edge.end), abs(segment.end-edge.start))) <= graph.near]
        if len(locations) != 1:
            raise ValueError("Cannot locate the free flap edge on the compiled cut contour")
        contour_index, segment_index, edge = locations[0]
        axis = (edge.end-edge.start)/abs(edge.end-edge.start)
        # Tracing can repair a near-coincident endpoint. Anchor the mouth to
        # the actual emitted edge, not its pre-repair supporting line.
        center = edge.start+axis*((center-edge.start)*axis.conjugate()).real
        inward = axis*1j
        if (inward*outward.conjugate()).real > 0:
            inward = -inward
        frame = np.array([[axis.real, inward.real, center.real],
                          [axis.imag, inward.imag, center.imag], [0, 0, 1]])
        radius, depth = config["diameter"]/2, config["depth"]
        desired_width = math.sqrt(radius**2-(radius-depth)**2)
        half_width = min(desired_width, abs(center-edge.start)-MARGIN, abs(center-edge.end)-MARGIN)
        if half_width <= 1e-5:
            reports.append({"panel": owner, "status": "omitted", "reason": "less than 3 mm side clearance"})
            continue
        rendered = [rounded_path(path, treatments) for path, treatments in result]
        material = unary_union([polygon_for(path) for path in rendered])
        barriers = []
        for path in rendered:
            for segment in path:
                # The mouth is the only boundary from which clearance is waived.
                if (isinstance(segment, Line) and
                        all(abs(((point-center)*inward.conjugate()).real) < 1e-7 and
                            -graph.near <= ((point-edge.start)/axis).real <= abs(edge.end-edge.start)+graph.near
                            for point in (segment.start, segment.end))):
                    continue
                barriers.append(LineString(clearance_points(segment)))
        barriers.extend(LineString(clearance_points(curve)) for curve in seams.values())
        for _, _, path in internals:
            polygon = polygon_for(path)
            material = material.difference(polygon)
            barriers.extend(LineString(clearance_points(segment)) for segment in path)
        protected = unary_union(barriers)

        def opening(width, penetration):
            path = transform_path(cap_path(radius, depth, width, penetration), frame)
            polygon = Polygon([point for segment in path for point in clearance_points(segment)[:-1]]+
                              [(path.end.real, path.end.imag)])
            if not polygon.is_valid or polygon.area <= 0:
                raise ValueError("Finger cutout is too small to construct accurately")
            return path, polygon

        def fits(width, penetration):
            _, polygon = opening(width, penetration)
            # Reserve more than the chord error of the sampled native curves.
            return (material.buffer(1e-8).covers(polygon) and polygon.distance(protected) >= MARGIN+2*CURVE_ERROR)

        tiny = 1e-4
        if not fits(tiny, tiny):
            reports.append({"panel": owner, "status": "omitted", "reason": "no opening with 3 mm clearance"})
            continue

        def largest(limit, predicate):
            if predicate(limit):
                return limit
            lo, hi = tiny, limit
            for _ in range(40):
                middle = (lo+hi)/2
                if predicate(middle):
                    lo = middle
                else:
                    hi = middle
            return lo

        half_width = largest(half_width, lambda width: fits(width, tiny))
        penetration = largest(depth, lambda value: fits(half_width, value))
        if min(half_width, penetration) <= tiny*1.01:
            reports.append({"panel": owner, "status": "omitted", "reason": "no usable opening with 3 mm clearance"})
            continue
        path, removed = opening(half_width, penetration)
        contour, treatments = result[contour_index]
        replacement = [Line(edge.start, path.start), *path, Line(path.end, edge.end)]
        segments = list(contour[:segment_index])+replacement+list(contour[segment_index+1:])
        extra = len(replacement)-1
        mapped_treatments = {index+(extra if index > segment_index else 0): treatment
                             for index, treatment in treatments.items()}
        updated = Path(*segments)
        polygon_for(rounded_path(updated, mapped_treatments))
        result[contour_index] = updated, mapped_treatments
        reports.append({"panel": owner, "status": "applied", "diameter": config["diameter"],
                        "requested_depth": depth, "depth": penetration, "mouth_width": half_width*2,
                        "minimum_material": MARGIN, "clearance": removed.distance(protected),
                        "cropped": penetration < depth-1e-6 or half_width < desired_width-1e-6})
    return result, reports
