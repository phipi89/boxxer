"""Bind receiving openings to uniquely overlapping folded flaps or gussets."""
import math

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union
from svgpathtools import Line, Path
from svgpathtools.path import transform as transform_path

from .features import _frame, _slot_frame, stretch_slot
from .fingercuts import clearance_points
from .corners import rounded_path


def planar(relative):
    result = np.eye(3)
    result[:2, :2] = relative[:2, :2]
    result[:2, 2] = relative[:2, 3]
    return result


def polygon_transform(polygon, matrix):
    return affine_transform(polygon, [matrix[0, 0], matrix[0, 1], matrix[1, 0], matrix[1, 1], matrix[0, 2], matrix[1, 2]])


def find_interactions(graph):
    by_id = {panel.id: panel for panel in graph.panels}
    model = graph._resize_structure()
    principals = ({model["principal"]} if model.get("kind") == "single" else
                  {graph.structure["top"], graph.structure["bottom"]} if graph.structure.get("status") == "inferred" else set())
    if not principals:
        principals = {graph._fold_root(component) for component in graph.components
                      if len(graph._adjacency()[graph._fold_root(component)]) >= 3}
    walls = {neighbor for principal in principals for neighbor, _ in graph._adjacency()[principal]}
    eligible = {contact.tab for contact in graph.contacts}
    eligible.update(identifier for seam in graph.seams if abs(seam.angle) == 180
                    for identifier in (seam.panel_a, seam.panel_b) if identifier not in principals | walls)
    adjacency = {identifier: set() for identifier in eligible}
    for seam in graph.seams:
        if abs(seam.angle) == 180 and seam.panel_a in eligible and seam.panel_b in eligible:
            adjacency[seam.panel_a].add(seam.panel_b)
            adjacency[seam.panel_b].add(seam.panel_a)
    groups, remaining = [], set(eligible)
    while remaining:
        stack, group = [min(remaining)], set()
        while stack:
            identifier = stack.pop()
            if identifier not in remaining:
                continue
            remaining.remove(identifier)
            group.add(identifier)
            stack.extend(adjacency[identifier])
        groups.append(tuple(sorted(group)))
    bindings, warnings = {}, []
    for receiver in graph.panels:
        if receiver.id not in graph._folded_transforms:
            continue
        for opening in receiver.internal:
            if opening.feature == "fixed":
                continue
            matches = []
            for group in groups:
                if receiver.id in group:
                    continue
                hits = []
                for identifier in group:
                    if identifier not in graph._folded_transforms:
                        continue
                    relative = np.linalg.inv(graph._folded_transforms[receiver.id])@graph._folded_transforms[identifier]
                    if abs(abs(relative[2, 2])-1) > 1e-7 or abs(relative[2, 3]) > graph.near:
                        continue
                    physical = polygon_for_path(rounded_path(by_id[identifier].path, by_id[identifier].corner_treatments))
                    for cut in by_id[identifier].internal:
                        physical = physical.difference(polygon_for_path(rounded_path(cut.path, cut.corner_treatments)))
                    footprint = polygon_transform(physical, planar(relative))
                    cut = polygon_for_path(rounded_path(opening.path, opening.corner_treatments))
                    overlap = footprint.intersection(cut).area
                    if overlap > 1e-8:
                        hits.append((overlap, identifier))
                if hits:
                    matches.append((group, max(hits)[1]))
            if len(matches) > 1:
                warnings.append(f"Opening {opening.id} overlaps multiple folded structures; its engagement is ambiguous.")
            elif matches:
                group, representative = matches[0]
                bindings[opening.id] = {"opening": opening.id, "receiver": receiver.id,
                                        "structure": list(group), "representative": representative,
                                        "kind": "gusset" if len(group) > 1 else "flap"}
    for panel in graph.panels:
        for opening in panel.internal:
            if opening.feature == "slot" and opening.id not in bindings:
                warnings.append(f"Explicit slot {opening.id} has no unique folded engagement.")
    return bindings, warnings


def map_opening(graph, binding, opening, target_paths, target_transforms, mappers, target_scores, boundary_references=None):
    receiver, engager = binding["receiver"], binding["representative"]
    by_id = {panel.id: panel for panel in graph.panels}
    old_relative = planar(np.linalg.inv(graph._folded_transforms[receiver])@graph._folded_transforms[engager])
    new_relative = planar(np.linalg.inv(target_transforms[receiver])@target_transforms[engager])
    inverse = np.linalg.inv(old_relative)
    old_engager_path = rounded_path(by_id[engager].path, by_id[engager].corner_treatments)
    new_engager_path = rounded_path(target_paths[engager], by_id[engager].corner_treatments)

    def mapped(point):
        source = inverse@np.array([point.real, point.imag, 1])
        target = mappers[engager](complex(*source[:2]))
        result = new_relative@np.array([target.real, target.imag, 1])
        return complex(*result[:2])

    def mapped_boundary(point):
        source = inverse@np.array([point.real, point.imag, 1])
        source_point = complex(*source[:2])
        candidates = []
        for index, segment in enumerate(old_engager_path):
            if isinstance(segment, Line):
                vector = segment.end-segment.start
                parameter = min(1, max(0, ((source_point-segment.start)*vector.conjugate()).real/abs(vector)**2))
            else:
                nearest = min(range(65), key=lambda i: abs(segment.point(i/64)-source_point))
                lo, hi = max(0, (nearest-1)/64), min(1, (nearest+1)/64)
                for _ in range(40):
                    a, b = (2*lo+hi)/3, (lo+2*hi)/3
                    if abs(segment.point(a)-source_point) < abs(segment.point(b)-source_point):
                        hi = b
                    else:
                        lo = a
                parameter = (lo+hi)/2
            candidates.append((abs(segment.point(parameter)-source_point), index, parameter))
        _, index, parameter = min(candidates)
        old = old_engager_path[index].point(parameter)
        target = new_engager_path[index].point(parameter)+(source_point-old)
        result = new_relative@np.array([target.real, target.imag, 1])
        return complex(*result[:2])

    slot = _slot_frame(opening.path, graph.near)
    if slot is None:
        center = complex(*opening.polygon.centroid.coords[0])
        return transform_path(opening.path, _frame(1, mapped(center)-center))
    axis, local, (x0, x1, y0, y1) = slot
    center = axis*complex((x0+x1)/2, (y0+y1)/2)
    new_axis = mapped(center+axis)-mapped(center)
    new_axis /= abs(new_axis)
    old_frame, new_frame = np.linalg.inv(_frame(axis)), np.linalg.inv(_frame(new_axis))
    old_physical = polygon_for_path(old_engager_path)
    old_footprint = polygon_transform(old_physical, old_frame@old_relative)
    target_polygon = polygon_for_path(new_engager_path)
    new_footprint = polygon_transform(target_polygon, new_frame@new_relative)
    references = [(old_footprint.bounds[0], new_footprint.bounds[0]),
                  (old_footprint.bounds[2], new_footprint.bounds[2])]
    for index, segment in enumerate(by_id[receiver].path):
        if not isinstance(segment, Line):
            continue
        start, end = segment.start/axis, segment.end/axis
        if abs(start.real-end.real) > graph.near or abs(start.imag-end.imag) <= graph.near:
            continue
        fraction = min(1, max(0, ((y0+y1)/2-start.imag)/(end.imag-start.imag)))
        target = target_paths[receiver][index].point(fraction)
        source_point = segment.point(fraction)
        for old, new in (boundary_references or {}).get((receiver, index), []):
            vector = old.end-old.start
            parameter = ((source_point-old.start)*vector.conjugate()).real/abs(vector)**2
            if -1e-8 <= parameter <= 1+1e-8:
                target = new.point(min(1, max(0, parameter)))
                break
        for seam in graph.seams:
            owner_index = seam.segment_a if seam.panel_a == receiver else seam.segment_b if seam.panel_b == receiver else None
            if owner_index == index and seam.id in target_scores:
                interval = sorted(seam.interval_a if seam.panel_a == receiver else seam.interval_b)
                if not interval[0]-1e-8 <= fraction <= interval[1]+1e-8:
                    continue
                curve = target_scores[seam.id]
                # Scores can be oppositely directed to this panel boundary.
                reverse = ((seam.end-seam.start).conjugate()*(segment.end-segment.start)).real < 0
                along = min(1, max(0, (fraction-interval[0])/(interval[1]-interval[0])))
                target = curve.point(1-along if reverse else along)
                break
        references.append((start.real, (target/new_axis).real))
    middle = (x0+x1)/2
    lower = [reference for reference in references if reference[0] <= middle]
    upper = [reference for reference in references if reference[0] >= middle]
    before = min(lower or references, key=lambda item: abs(item[0]-x0))
    after = min(upper or references, key=lambda item: abs(item[0]-x1))
    new_start, new_end = before[1]+x0-before[0], after[1]+x1-after[0]
    # Attach lateral placement to the closest free engaging boundary, retaining
    # its physical clearance instead of scaling a percentage across the wall.
    boundary = polygon_transform(old_physical, old_relative).boundary
    fold_edges = [LineString(clearance_points(transform_path(Path(seam.curve), old_relative)[0]))
                  for seam in graph.seams if engager in (seam.panel_a, seam.panel_b)]
    if fold_edges:
        boundary = boundary.difference(unary_union(fold_edges).buffer(graph.near))
    if boundary.is_empty:
        boundary = polygon_transform(old_physical, old_relative).boundary
    normal = axis*1j
    extent = max(by_id[engager].polygon.length, opening.polygon.length)*2
    a, b = center-normal*extent, center+normal*extent
    crossing = boundary.intersection(LineString([(a.real, a.imag), (b.real, b.imag)]))
    anchor = nearest_points(crossing if not crossing.is_empty else boundary, Point(center.real, center.imag))[0]
    old_anchor = complex(anchor.x, anchor.y)
    lateral = (mapped_boundary(old_anchor)/new_axis).imag+((center-old_anchor)/axis).imag
    return stretch_slot(slot, new_axis, new_start, new_end, lateral, graph.near)


def exposed_references(graph, final_fragments):
    """Pair exposed source intervals with their fitted/compensated boundaries."""
    result = {}
    by_id = {panel.id: panel for panel in graph.panels}
    for source, target in zip(graph._fragments(), final_fragments):
        if source.owner != target.owner or not isinstance(source.segment, Line):
            continue
        for index, segment in enumerate(by_id[source.owner].path):
            if not isinstance(segment, Line):
                continue
            vector = segment.end-segment.start
            parameters = [(point-segment.start)/vector for point in (source.segment.start, source.segment.end)]
            if all(abs(value.imag) < 1e-8 and -1e-8 <= value.real <= 1+1e-8 for value in parameters):
                result.setdefault((source.owner, index), []).append((source.segment, target.segment))
                break
    return result


def polygon_for_path(path):
    from shapely.geometry import Polygon
    return Polygon([point for segment in path for point in clearance_points(segment)[:-1]])


def projected_engagement(graph, binding, cut_path, target_paths, transforms):
    """Check projected alignment; finite-thickness layers need not be coplanar."""
    by_id = {panel.id: panel for panel in graph.panels}
    cut = polygon_for_path(cut_path)
    for identifier in binding["structure"]:
        relative = planar(np.linalg.inv(transforms[binding["receiver"]])@transforms[identifier])
        path = rounded_path(target_paths[identifier], by_id[identifier].corner_treatments)
        footprint = polygon_transform(polygon_for_path(path), relative)
        if footprint.intersection(cut).area > 1e-8:
            return True
    return False


def folded_transforms(graph, paths, scores, pitch=0):
    """Compose final hinges for projected opening placement, including surface axes."""
    from .panelgraph import rotation_3d
    result = {}
    adjacency = graph._adjacency()
    for component in graph.components:
        root = graph._fold_root(component)
        result[root] = np.eye(4)
        signed = any(seam.annotated and seam.angle != 90 and seam.panel_a in component for seam in graph.seams)
        queue = [root]
        while queue:
            parent = queue.pop(0)
            for child, seam in adjacency[parent]:
                if child in result:
                    continue
                curve = scores[seam.id]
                if not isinstance(curve, Line):
                    raise ValueError("Opening engagement requires straight fold hinges")
                point = polygon_for_path(paths[child]).representative_point()
                interior = complex(point.x, point.y)-curve.start
                side = ((curve.end-curve.start).conjugate()*interior).imag
                surface = np.eye(4)
                surface[2, 3] = math.copysign(pitch/2, seam.angle) if signed else 0
                result[child] = result[parent]@surface@rotation_3d(
                    curve.start, curve.end, math.copysign(1, side)*math.radians(seam.angle))@np.linalg.inv(surface)
                queue.append(child)
    return result
