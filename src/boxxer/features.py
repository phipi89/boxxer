"""Anchored internal-cut geometry, independent of panel names and page axes."""
import numpy as np
from svgpathtools import Arc, Line, Path
from svgpathtools.path import transform as transform_path


def _frame(axis, origin=0j):
    return np.array([[axis.real, -axis.imag, origin.real],
                     [axis.imag, axis.real, origin.imag], [0, 0, 1]], dtype=float)


def boundary_cutouts(path, *, counterclockwise):
    """Find inward edge excursions between collinear, equally directed leads.

    Straight notch bottoms and rounded corners belong to the same rigid feature.
    Its attachment point is defined by the two mouths on the supporting edge.
    """
    candidates = []
    orientation = 1 if counterclockwise else -1
    for first, before in enumerate(path):
        if not isinstance(before, Line) or abs(before.end-before.start) <= 1e-8:
            continue
        axis = (before.end-before.start)/abs(before.end-before.start)
        for distance in range(2, len(path)):
            last = (first+distance) % len(path)
            after = path[last]
            if not isinstance(after, Line):
                continue
            direction = (after.end-after.start)/axis
            mouth = (after.start-before.end)/axis
            if (direction.real <= 1e-8 or abs(direction.imag) > 1e-8 or
                    mouth.real <= 1e-8 or abs(mouth.imag) > 1e-8):
                continue
            indices = [(first+offset) % len(path) for offset in range(1, distance)]
            local = transform_path(Path(*(path[index] for index in indices)),
                                   np.linalg.inv(_frame(axis, before.end)))
            x0, x1, y0, y1 = local.bbox()
            if x0 < -1e-8 or x1 > mouth.real+1e-8:
                continue
            inward_min, inward_max = ((y0, y1) if orientation > 0 else (-y1, -y0))
            if inward_min >= -1e-8 and inward_max > 1e-8:
                candidates.append(set(indices))
    groups, covered = [], set()
    # Prefer the immediate leads over more distant subdivisions of the same edge.
    for group in sorted(candidates, key=len):
        if not group.intersection(covered):
            groups.append(group)
            covered.update(group)
    return groups


def _slot_frame(path, tolerance):
    """Recognise a non-square rectangle or circular-ended rounded rectangle.

    Unclassified cuts remain rigid. An explicit ``fixed`` feature overrides this
    inference, so an elongated decorative opening need not behave as a slot.
    """
    lines = [segment for segment in path if isinstance(segment, Line)]
    if not lines or any(not isinstance(segment, (Line, Arc)) for segment in path):
        return None
    longest = max(lines, key=lambda segment: abs(segment.end-segment.start))
    if abs(longest.end-longest.start) <= tolerance:
        return None
    axis = (longest.end-longest.start)/abs(longest.end-longest.start)
    local = transform_path(path, np.linalg.inv(_frame(axis)))
    x0, x1, y0, y1 = local.bbox()
    if y1-y0 > x1-x0:
        axis *= 1j
    if axis.real < -1e-8 or (abs(axis.real) <= 1e-8 and axis.imag < 0):
        axis = -axis
    local = transform_path(path, np.linalg.inv(_frame(axis)))
    x0, x1, y0, y1 = local.bbox()
    width = y1-y0
    if width <= tolerance or x1-x0 <= width+tolerance:
        return None
    counterclockwise = sum((segment.start.conjugate()*segment.end).imag for segment in local) > 0
    for segment in local:
        if isinstance(segment, Line):
            if abs(segment.end-segment.start) <= tolerance:
                continue
            # The boundary must run along the sides of its bounding rectangle.
            if abs(segment.end.imag-segment.start.imag) <= tolerance:
                if min(abs(segment.start.imag-y0), abs(segment.start.imag-y1)) > tolerance:
                    return None
            elif abs(segment.end.real-segment.start.real) <= tolerance:
                if min(abs(segment.start.real-x0), abs(segment.start.real-x1)) > tolerance:
                    return None
            else:
                return None
        else:
            if (abs(segment.radius.real-segment.radius.imag) > tolerance or
                    segment.sweep != counterclockwise):
                return None
            a, b, _, _ = segment.bbox()
            if not (b <= x0+width/2+tolerance or a >= x1-width/2-tolerance):
                return None
    return axis, local, (x0, x1, y0, y1)


def map_internal(path, panel_path, mapped_panel_path, mapper, *, policy, tolerance):
    """Keep slot end insets and width, or translate a fixed cut rigidly.

    End anchors belong to the owning panel. Their fractional position along each
    transverse edge fixes the slot's lateral position as that panel changes.
    Both panel paths must retain the same segment correspondence.
    """
    slot = None if policy == "fixed" else _slot_frame(path, tolerance)
    if slot is None:
        if policy == "slot":
            raise ValueError("Explicit slot must be a rectangular or circular-ended elongated cut")
        x0, x1, y0, y1 = path.bbox()
        anchor = complex((x0+x1)/2, (y0+y1)/2)
        return transform_path(path, _frame(1+0j, mapper(anchor)-anchor))

    axis, local, (x0, x1, y0, y1) = slot
    lateral = (y0+y1)/2
    candidates = [[], []]
    for index, segment in enumerate(panel_path):
        if not isinstance(segment, Line):
            continue
        start, end = segment.start/axis, segment.end/axis
        if abs(end.real-start.real) > tolerance or abs(end.imag-start.imag) <= tolerance:
            continue
        fraction = (lateral-start.imag)/(end.imag-start.imag)
        if not 0 <= fraction <= 1:
            continue
        coordinate = (start+(end-start)*fraction).real
        mapped = mapped_panel_path[index].point(fraction)
        if coordinate < x0:
            candidates[0].append((coordinate, mapped))
        if coordinate > x1:
            candidates[1].append((coordinate, mapped))
    if not all(candidates):
        raise ValueError("Slot ends require transverse edges on their owning panel; use boxxer:feature='fixed' for a rigid cut")
    left, mapped_left = max(candidates[0], key=lambda item: item[0])
    right, mapped_right = min(candidates[1], key=lambda item: item[0])
    vector = mapped_right-mapped_left
    span = abs(vector)
    start_inset, end_inset = x0-left, right-x1
    length = span-start_inset-end_inset
    width = y1-y0
    if length <= width+tolerance:
        raise ValueError("Resized slot is too short to retain its width, end profile, and edge insets")
    new_axis = vector/span
    start = mapped_left+new_axis*start_inset

    def mapped(point):
        # End profiles move rigidly; only the straight longitudinal sides grow.
        along = point.real-x0
        if along <= width/2+tolerance:
            x = along
        elif along >= x1-x0-width/2-tolerance:
            x = length-(x1-point.real)
        else:
            x = width/2+(along-width/2)*(length-width)/(x1-x0-width)
        return complex(x, point.imag-lateral)

    segments = []
    for segment in local:
        if isinstance(segment, Line):
            segments.append(Line(mapped(segment.start), mapped(segment.end)))
        else:
            a, b, _, _ = segment.bbox()
            shift = complex(-x0 if b <= x0+width/2+tolerance else length-x1, -lateral)
            segments.append(transform_path(Path(segment), _frame(1+0j, shift))[0])
    return transform_path(Path(*segments), _frame(new_axis, start))
