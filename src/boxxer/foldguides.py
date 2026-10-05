"""Sketch folds and finite symmetry groups, independent of SVG colour or IDs."""
from dataclasses import dataclass, replace
import math
import re

import numpy as np
from svgpathtools import Arc, Line, Path
from svgpathtools.path import transform as transform_path


@dataclass
class Guide:
    id: str
    line: Line
    angle: int | None  # None denotes a mirror axis.


@dataclass
class RotationGuide:
    id: str
    center: complex
    order: int


def dash_pattern(style):
    raw = style.get("stroke-dasharray", "none")
    values = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", raw)]
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Invalid dash pattern")
    if len(values) % 2:
        values *= 2
    dash_dot = (len(values) >= 4 and len(values) % 4 == 0 and
                all(values[i] > values[i+2]+1e-8 and values[i+1] > 0 and values[i+3] > 0
                    for i in range(0, len(values), 4)))
    return values, dash_dot


def rotation_guide_for(path, style, identifier):
    if not path or not dash_pattern(style)[1] or all(isinstance(s, Line) for s in path):
        return None
    if not path.iscontinuous() or any(not isinstance(s, Arc) for s in path):
        raise ValueError(f"Rotation marker {identifier} must be an open circular arc")
    first = path[0]
    radius = abs(first.radius.real)
    tolerance = max(1e-6, radius*1e-6)
    if any(abs(s.radius.real-s.radius.imag) > tolerance or
           abs(s.radius.real-radius) > tolerance or abs(s.center-first.center) > tolerance or
           s.sweep != first.sweep for s in path):
        raise ValueError(f"Rotation marker {identifier} must lie on one circle")
    span = sum(abs(s.delta) for s in path)
    order = round(360/span) if span > 1e-8 else 0
    if order < 2 or order > 360 or abs(span*order-360) > 1e-4:
        raise ValueError(f"Rotation marker {identifier} angle must divide 360° (2–360 copies)")
    return RotationGuide(identifier, first.center, order)


def guide_for(path, style, identifier, tolerance=1e-8):
    """Recognise straight, zero-area strokes before polygon validation.

    Subdividing a stroke does not change its multiplicity. A closing Z is a
    real return traversal, just like an explicit L back to the starting point.
    """
    if not path or any(not isinstance(segment, Line) for segment in path):
        return None
    if not path.iscontinuous():
        raise ValueError(f"Fold guide {identifier} must be a continuous stroke")
    origin = path[0].start
    vector = path[0].end-origin
    unit = vector/abs(vector)
    points = [point for segment in path for point in (segment.start, segment.end)]
    if any(abs(((point-origin)*unit.conjugate()).imag) > tolerance for point in points):
        return None
    intervals = [sorted((((segment.start-origin)*unit.conjugate()).real,
                         ((segment.end-origin)*unit.conjugate()).real)) for segment in path]
    stops = sorted({value for interval in intervals for value in interval})
    counts = {sum(lo-tolerance < (a+b)/2 < hi+tolerance for lo, hi in intervals)
              for a, b in zip(stops, stops[1:]) if b-a > tolerance}
    if len(counts) != 1 or not counts <= {1, 2}:
        raise ValueError(f"Fold guide {identifier} must traverse its whole length once or twice")
    count = counts.pop()
    if count == 2 and abs(path.start-path.end) > tolerance:
        raise ValueError(f"Double fold guide {identifier} must return to its starting point")
    # SVG repeats odd-length patterns. Dash-dot is a long mark, gap, short
    # mark, gap (or a repeated copy); absolute dash lengths do not matter.
    values, mirror = dash_pattern(style)
    if mirror and count != 1:
        raise ValueError(f"Mirror axis {identifier} must be a single traversal")
    angle = None if mirror else count*90*(-1 if any(values) else 1)
    return Guide(identifier, Line(origin+unit*stops[0], origin+unit*stops[-1]), angle)


def reflection(line):
    unit = (line.end-line.start)/abs(line.end-line.start)
    direction = np.array([unit.real, unit.imag])
    linear = 2*np.outer(direction, direction)-np.eye(2)
    result = np.eye(3)
    result[:2, :2] = linear
    origin = np.array([line.start.real, line.start.imag])
    result[:2, 2] = origin-linear@origin
    return result


def expand_symmetry(shapes, guides, polygon_for, tolerance, rotations=()):
    axes = [guide for guide in guides if guide.angle is None]
    folds = [guide for guide in guides if guide.angle is not None]
    unique_axes = []
    for axis in axes:
        vector = axis.line.end-axis.line.start
        unit = vector/abs(vector)
        if any(abs(((old.line.end-old.line.start)/abs(old.line.end-old.line.start)*unit.conjugate()).imag) < 1e-8
               and abs(((old.line.start-axis.line.start)*unit.conjugate()).imag) <= tolerance
               for old in unique_axes):
            continue
        unique_axes.append(axis)
    if len(unique_axes) > 2:
        raise ValueError("Sketch mirroring supports at most two perpendicular axes")
    if len(unique_axes) == 2:
        a, b = [axis.line.end-axis.line.start for axis in unique_axes]
        if abs((a.conjugate()*b).real)/(abs(a)*abs(b)) > 1e-8:
            raise ValueError("Two mirror axes must be perpendicular")
    rotation = rotations[0] if rotations else None
    if rotation:
        if any(abs(other.center-rotation.center) > tolerance or other.order != rotation.order
               for other in rotations):
            raise ValueError("Rotation markers must specify the same centre and repeat angle")
        for axis in unique_axes:
            unit = (axis.line.end-axis.line.start)/abs(axis.line.end-axis.line.start)
            if abs(((rotation.center-axis.line.start)*unit.conjugate()).imag) > tolerance:
                raise ValueError("Mirror axes must pass through the rotation centre")
        if len(unique_axes) == 2 and rotation.order % 2:
            raise ValueError("Two perpendicular mirrors require an even rotation order")
    shapes, folds = list(shapes), list(folds)
    identifiers = {shape.id for shape in shapes} | {guide.id for guide in guides}

    def identifier(base, suffix):
        candidate = f"{base}-{suffix}"
        while candidate in identifiers:
            candidate += "-copy"
        identifiers.add(candidate)
        return candidate

    def apply(transform, suffix, source_shapes, source_folds):
        for shape in source_shapes:
            path = transform_path(shape.path, transform)
            polygon = polygon_for(path)
            matches = [other for other in shapes
                       if polygon.hausdorff_distance(other.polygon) <= tolerance and
                       polygon.symmetric_difference(other.polygon).area <= tolerance*polygon.length]
            if matches:
                if any(other.outside_hint != shape.outside_hint or other.feature != shape.feature or
                       len(other.corner_treatments) != len(shape.corner_treatments) or
                       any(not any(abs(path[i].start-other.path[j].start) <= tolerance and
                                   abs(treatment.radius-candidate.radius) <= 1e-7
                                   for j, candidate in other.corner_treatments.items())
                           for i, treatment in shape.corner_treatments.items())
                       for other in matches):
                    raise ValueError(f"Conflicting symmetry copies of material at {shape.id}")
                continue
            shapes.append(replace(shape, id=identifier(shape.id, suffix), path=path,
                                  polygon=polygon, order=len(shapes)))
        for guide in source_folds:
            line = transform_path(Path(guide.line), transform)[0]
            matches = [other for other in folds if min(
                max(abs(line.start-other.line.start), abs(line.end-other.line.end)),
                max(abs(line.start-other.line.end), abs(line.end-other.line.start))) <= tolerance]
            if matches:
                if any(other.angle != guide.angle for other in matches):
                    raise ValueError(f"Conflicting symmetry copies of fold at {guide.id}")
                continue
            folds.append(Guide(identifier(guide.id, suffix), line, guide.angle))
    if rotation:
        source_shapes, source_folds = list(shapes), list(folds)
        center = np.array([rotation.center.real, rotation.center.imag])
        for index in range(1, rotation.order):
            angle = index*2*math.pi/rotation.order
            transform = np.eye(3)
            transform[:2, :2] = [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]]
            transform[:2, 2] = center-transform[:2, :2]@center
            apply(transform, f"rotate-{index}", source_shapes, source_folds)
    for index, axis in enumerate(unique_axes):
        apply(reflection(axis.line), f"mirror-{index}", list(shapes), list(folds))
    return shapes, folds, unique_axes


def attach_guides(seams, guides, match, tolerance):
    assigned = {}
    for guide in guides:
        intervals = []
        for seam in seams:
            if not isinstance(seam.curve, Line):
                continue
            hit = match(guide.line, seam.curve)
            if hit is None:
                continue
            guide_interval, seam_interval, _, _ = hit
            if seam_interval[0]*seam.length > tolerance or (1-seam_interval[1])*seam.length > tolerance:
                raise ValueError(f"Fold guide {guide.id} covers only part of {seam.id}")
            if seam.id in assigned and assigned[seam.id] != guide.angle:
                raise ValueError(f"Conflicting fold guides on {seam.id}")
            assigned[seam.id] = guide.angle
            seam.angle = guide.angle
            seam.annotated = True
            intervals.append(guide_interval)
        cursor = 0.0
        for lo, hi in sorted(intervals):
            if (lo-cursor)*guide.line.length() > tolerance:
                break
            cursor = max(cursor, hi)
        if (1-cursor)*guide.line.length() > tolerance:
            raise ValueError(f"Fold guide {guide.id} does not match a complete shared panel boundary")
