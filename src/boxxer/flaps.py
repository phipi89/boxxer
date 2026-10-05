"""Shorten contacting flaps using their folded footprints on a receiver."""
from itertools import combinations

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from svgpathtools import Line, Path


EPS = 1e-8


def _dot(point, axis):
    return (point*axis.conjugate()).real


def _clip_polygon(polygon, axis, limit):
    points = [complex(x, y) for x, y in polygon.exterior.coords]
    normal = [_dot(point, axis) for point in points]
    if max(normal) <= limit:
        return polygon
    if min(normal) >= limit:
        return Polygon()
    tangent = axis*1j
    across = [_dot(point, tangent) for point in points]
    margin = max(max(normal)-min(normal), max(across)-min(across))
    corners = [axis*n+tangent*t for n, t in (
        (min(normal)-margin, min(across)-margin), (limit, min(across)-margin),
        (limit, max(across)+margin), (min(normal)-margin, max(across)+margin))]
    return polygon.intersection(Polygon([(point.real, point.imag) for point in corners]))


def _clip_path(path, axis, limit):
    """Clip native segments to a half-plane, cropping curves without flattening."""
    x0, x1, y0, y1 = path.bbox()
    tangent = axis*1j
    across = [_dot(complex(x, y), tangent) for x in (x0, x1) for y in (y0, y1)]
    extent = max(x1-x0, y1-y0)
    cutter = Line(axis*limit+tangent*(min(across)-extent),
                  axis*limit+tangent*(max(across)+extent))
    pieces = []
    for segment in path:
        if isinstance(segment, Line):
            denominator = _dot(segment.end-segment.start, axis)
            crossings = [] if abs(denominator) <= EPS else [
                (limit-_dot(segment.start, axis))/denominator]
        else:
            crossings = [first for first, _ in segment.intersect(cutter)]
        parameters = sorted({0.0, 1.0, *(value for value in crossings if EPS < value < 1-EPS)})
        for start, end in zip(parameters, parameters[1:]):
            if _dot(segment.point((start+end)/2), axis) <= limit+EPS:
                pieces.append(segment if start == 0 and end == 1 else segment.cropped(start, end))
    if not pieces:
        raise ValueError("Flap shortening would eliminate a flap")
    segments = []
    for index, segment in enumerate(pieces):
        segments.append(segment)
        following = pieces[(index+1) % len(pieces)]
        if abs(segment.end-following.start) > EPS:
            segments.append(Line(segment.end, following.start))
    return Path(*segments)


def shortening_plan(panels, seams, contacts, transforms, polygon_for):
    """Equal free-end trimming for each overlapping same-side contact pair.

    Pair constraints are combined by maximum required trim, making the result
    independent of panel IDs and traversal order. Removing material cannot create
    another overlap. Opposite-side flaps are separate contact domains.
    """
    by_id = {panel.id: panel for panel in panels}
    seam_by_id = {seam.id: seam for seam in seams}
    states = []
    for contact in contacts:
        panel = by_id[contact.tab]
        seam = seam_by_id[contact.seam]
        tangent = (seam.end-seam.start)/abs(seam.end-seam.start)
        axis = tangent*1j
        center = complex(*panel.polygon.representative_point().coords[0])
        if _dot(center-seam.start, axis) < 0:
            axis = -axis
        root = _dot(seam.start, axis)
        depth = max(_dot(complex(x, y), axis)-root for x, y in panel.polygon.exterior.coords)
        relative = np.linalg.inv(transforms[contact.receiver])@transforms[contact.tab]
        planar = relative[np.ix_([0, 1], [0, 1])]
        offset = relative[:2, 3]
        footprint = affine_transform(panel.polygon, [*planar[0], *planar[1], *offset])
        folded_axis = planar@np.array([axis.real, axis.imag])
        folded_axis = complex(*folded_axis)
        folded_root = root+_dot(complex(*offset), folded_axis)
        states.append({"contact": contact, "axis": axis, "root": root, "depth": depth,
                       "footprint": footprint, "folded_axis": folded_axis,
                       "folded_root": folded_root, "trim": 0.0})

    for first, second in combinations(states, 2):
        a, b = first["contact"], second["contact"]
        if (a.receiver, a.placement) != (b.receiver, b.placement):
            continue
        if first["footprint"].intersection(second["footprint"]).area <= EPS:
            continue

        def overlap(trim):
            clipped = [_clip_polygon(state["footprint"], state["folded_axis"],
                                     state["folded_root"]+state["depth"]-trim)
                       for state in (first, second)]
            return clipped[0].intersection(clipped[1]).area

        low, high = 0.0, min(first["depth"], second["depth"])-EPS
        if high <= 0 or overlap(high) > EPS:
            raise ValueError(f"Cannot separate flaps {a.tab} and {b.tab} without eliminating a flap")
        # Bisection uses an absolute geometric tolerance, not a flap-size ratio.
        while high-low > EPS:
            middle = (low+high)/2
            if overlap(middle) > EPS:
                low = middle
            else:
                high = middle
        for state in (first, second):
            state["trim"] = max(state["trim"], high)

    paths, adjustments = {}, []
    for state in states:
        if state["trim"] <= EPS:
            continue
        contact = state["contact"]
        panel = by_id[contact.tab]
        depth = state["depth"]-state["trim"]
        path = _clip_path(panel.path, state["axis"], state["root"]+depth)
        polygon = polygon_for(path)
        expected = _clip_polygon(panel.polygon, state["axis"], state["root"]+depth)
        # A concave flap may become disconnected under a free-end cut.
        if not isinstance(expected, Polygon) or expected.is_empty:
            raise ValueError(f"Shortening flap {panel.id} would disconnect its material")
        if any(not polygon.contains(shape.polygon) or
               polygon.boundary.distance(shape.polygon) <= EPS for shape in panel.internal):
            raise ValueError(f"Shortening flap {panel.id} would cut through its internal geometry")
        paths[panel.id] = path
        adjustments.append({"tab": panel.id, "receiver": contact.receiver,
                            "placement": contact.placement, "original_depth": state["depth"],
                            "depth": depth, "shortened_by": state["trim"]})
    return paths, adjustments
