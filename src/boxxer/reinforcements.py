"""Own-wall return contacts and final material-layer validation."""
import numpy as np
from shapely.geometry import box
from svgpathtools import Line

from .corners import rounded_path
from .interactions import folded_transforms, planar, polygon_for_path, polygon_transform
from .features import _frame
from .fingercuts import CURVE_ERROR
from .tolerances import AREA_EPS


def find_returns(graph, transforms, principals):
    by_id = {panel.id: panel for panel in graph.panels}
    adjacency = graph._adjacency()
    result = {}
    for panel in graph.panels:
        if len(adjacency[panel.id]) != 1 or panel.id not in transforms:
            continue
        parent, seam = adjacency[panel.id][0]
        if abs(seam.angle) != 180 or parent not in transforms:
            continue
        relative = np.linalg.inv(transforms[parent])@transforms[panel.id]
        polygon = polygon_for_path(rounded_path(panel.path, panel.corner_treatments))
        footprint = polygon_transform(polygon, planar(relative))
        wall = polygon_for_path(rounded_path(by_id[parent].path, by_id[parent].corner_treatments))
        for cut in by_id[parent].internal:
            wall = wall.difference(polygon_for_path(rounded_path(cut.path, cut.corner_treatments)))
        if footprint.intersection(wall).area <= 1e-8:
            continue
        wall_center = np.array([by_id[parent].polygon.centroid.x, by_id[parent].polygon.centroid.y, 0, 1])
        wall_world = (transforms[parent]@wall_center)[:3]
        roots = [identifier for identifier, _ in adjacency[parent] if identifier in principals]
        front_inside = True
        if roots:
            root = by_id[roots[0]].polygon.centroid
            center = (transforms[roots[0]]@np.array([root.x, root.y, 0, 1]))[:3]
            front_inside = float((center-wall_world)@transforms[parent][:3, 2]) >= 0
        inside = (seam.angle > 0) == front_inside
        result[panel.id] = {"panel": panel.id, "receiver": parent, "fold": seam.id,
                            "placement": "inside" if inside else "outside", "angle": seam.angle}
    return result


def resize_returns(graph, paths, mappers, target):
    """Preserve return-end gaps to wall boundaries and nearby folded glue tabs."""
    from .panelgraph import affine_between_segments, transform_point
    by_id = {panel.id: panel for panel in graph.panels}
    seam_by_id = {seam.id: seam for seam in graph.seams}
    records = []
    for identifier, binding in graph.reinforcements.items():
        parent = binding["receiver"]
        seam = seam_by_id[binding["fold"]]
        axis = (seam.end-seam.start)/abs(seam.end-seam.start)
        start, end = mappers[parent](seam.start), mappers[parent](seam.end)
        new_axis = (end-start)/abs(end-start)
        old_frame, new_frame = np.linalg.inv(_frame(axis)), np.linalg.inv(_frame(new_axis))
        old_relative = planar(np.linalg.inv(graph._folded_transforms[parent])@graph._folded_transforms[identifier])
        new_relative = planar(np.linalg.inv(target._folded_transforms[parent])@target._folded_transforms[identifier])
        old_foot = polygon_transform(polygon_for_path(rounded_path(by_id[identifier].path,
                                             by_id[identifier].corner_treatments)), old_frame@old_relative)
        new_foot = polygon_transform(polygon_for_path(rounded_path(paths[identifier],
                                             by_id[identifier].corner_treatments)), new_frame@new_relative)
        x0, y0, x1, y1 = old_foot.bounds
        nx0, ny0, nx1, ny1 = new_foot.bounds
        references = []
        # The attachment edge provides longitudinal limits even when the wall
        # ends in a chamfer or fillet instead of a perpendicular straight edge.
        # Rounded source/target paths retain corresponding segment indices.
        source_boundary = rounded_path(by_id[parent].path, by_id[parent].corner_treatments)
        target_boundary = rounded_path(paths[parent], by_id[parent].corner_treatments)
        attachment_intervals = []
        for index, edge in enumerate(source_boundary):
            if not isinstance(edge, Line):
                continue
            a, b = edge.start/axis, edge.end/axis
            offset = ((seam.start-edge.start)/axis).imag
            if abs(a.imag-b.imag) > graph.near or abs(offset) > graph.near:
                continue
            mapped_edge = target_boundary[index]
            endpoints = sorted([(a.real, (mapped_edge.start/new_axis).real),
                                (b.real, (mapped_edge.end/new_axis).real)])
            attachment_intervals.append(endpoints)
        merged = []
        for first, last in sorted(attachment_intervals):
            if merged and first[0] <= merged[-1][1][0]+1e-8:
                if last[0] > merged[-1][1][0]:
                    merged[-1][1] = last
            else:
                merged.append([first, last])
        for first, last in merged:
            if min(last[0], x1)-max(first[0], x0) > graph.near:
                references.extend([(first[0], first[1], None), (last[0], last[1], None)])
        for edge in by_id[parent].path:
            a, b = edge.start/axis, edge.end/axis
            if abs(a.real-b.real) <= graph.near and abs(a.imag-b.imag) > graph.near:
                references.append((a.real, (mappers[parent](edge.point(.5))/new_axis).real, None))
        for contact in graph.contacts:
            if contact.receiver != parent or contact.placement != binding["placement"]:
                continue
            old_pose = planar(np.linalg.inv(graph._folded_transforms[parent])@graph._folded_transforms[contact.tab])
            new_pose = planar(np.linalg.inv(target._folded_transforms[parent])@target._folded_transforms[contact.tab])
            old_tab = polygon_transform(polygon_for_path(rounded_path(by_id[contact.tab].path,
                                                   by_id[contact.tab].corner_treatments)), old_frame@old_pose)
            new_tab = polygon_transform(polygon_for_path(rounded_path(paths[contact.tab],
                                                   by_id[contact.tab].corner_treatments)), new_frame@new_pose)
            margin = max(old_tab.length, old_foot.length, new_tab.length, new_foot.length)
            old_band = old_tab.intersection(box(x0-margin, y0, x1+margin, y1))
            new_band = new_tab.intersection(box(nx0-margin, ny0, nx1+margin, ny1))
            if old_band.is_empty:
                continue
            if new_band.is_empty:
                new_band = new_tab
            if old_band.bounds[0] >= x1-graph.near:
                references.append((old_band.bounds[0], new_band.bounds[0], contact.tab))
            elif old_band.bounds[2] <= x0+graph.near:
                references.append((old_band.bounds[2], new_band.bounds[2], contact.tab))
        middle = (x0+x1)/2
        lower = [reference for reference in references if reference[0] <= middle]
        upper = [reference for reference in references if reference[0] >= middle]
        if not lower or not upper:
            raise ValueError(f"Return {identifier} has no stable end-clearance references")
        first = min(lower, key=lambda reference: abs(reference[0]-x0))
        last = min(upper, key=lambda reference: abs(reference[0]-x1))
        new_min, new_max = first[1]+x0-first[0], last[1]+x1-last[0]
        if new_max-new_min <= graph.near:
            raise ValueError(f"Return {identifier} has no room between its folded clearance boundaries")
        # Keep depth scaling, but derive longitudinal span from its contacts.
        scale = (new_max-new_min)/(x1-x0)
        source_start, source_end = (seam.start/axis).real, (seam.end/axis).real
        lateral = (start/new_axis).imag
        new_start = new_axis*complex(new_min+(source_start-x0)*scale, lateral)
        new_end = new_axis*complex(new_min+(source_end-x0)*scale, lateral)
        old_depth = np.linalg.norm(mappers[identifier](seam.start+axis*1j)-mappers[identifier](seam.start))
        transform = affine_between_segments(seam.start, seam.end, new_start, new_end, old_depth)
        mapper = lambda point, transform=transform: transform_point(point, transform)
        mappers[identifier] = mapper
        paths[identifier] = graph._mapped_path(by_id[identifier].path, mapper)
        for endpoint, reference, value in (("start", first, x0), ("end", last, x1)):
            if reference[2] is not None:
                records.append({"panel": identifier, "receiver": parent, "neighbor": reference[2],
                                "end": endpoint, "clearance": abs(value-reference[0])})
    return records


def validate_layers(graph, paths, scores, thickness, adjustment, internals=()):
    if not thickness or not any(seam.angle != 90 for seam in graph.seams):
        return
    transforms = folded_transforms(graph, paths, scores, thickness+adjustment)
    by_id = {panel.id: panel for panel in graph.panels}
    materials = {}
    for identifier, path in paths.items():
        materials[identifier] = polygon_for_path(rounded_path(path, by_id[identifier].corner_treatments))
    for owner, shape, path in internals:
        materials[owner] = materials[owner].difference(polygon_for_path(rounded_path(path, shape.corner_treatments)))
    surfaces = []
    for identifier, polygon in materials.items():
        if not polygon.is_empty:
            point = polygon.representative_point()
            transform = transforms[identifier]
            world = (transform@np.array([point.x, point.y, 0, 1]))[:3]
            surfaces.append((identifier, world, transform[:3, 2]))
    tolerance = max(1e-7, CURVE_ERROR*2, sum(seam.discrepancy for seam in graph.seams)*2)
    for index, (identifier, world, normal) in enumerate(surfaces):
        for other_id, other_world, other_normal in surfaces[index+1:]:
            if abs(abs(float(normal@other_normal))-1) > 1e-7:
                continue
            separation = abs(float((world-other_world)@normal))
            if separation >= thickness-tolerance:
                continue
            relative = np.linalg.inv(transforms[identifier])@transforms[other_id]
            footprint = polygon_transform(materials[other_id], planar(relative))
            overlap = materials[identifier].intersection(footprint)
            if overlap.area > AREA_EPS and not overlap.buffer(-tolerance).is_empty:
                raise ValueError(f"Folded material layers overlap: {identifier}/{other_id}; "
                                 "the final compensated assembly needs distinct layers or clearance")


def gap_layer_counts(graph):
    """Measure occupied wall-face layers, including cyclic folded gussets.

    Nominal footprints determine contacts. Unit-stock poses determine ply rank,
    so the estimate does not depend on actual stock being zero or very thin.
    """
    from .foldstack import stack_plan
    from .panelgraph import translation_2d
    from svgpathtools import Path
    from svgpathtools.path import transform as transform_path
    if graph.structure.get("status") != "inferred":
        return {}
    by_id = {panel.id: panel for panel in graph.panels}
    roots = {graph.structure["top"], graph.structure["bottom"]}
    adjacency = graph._adjacency()
    walls = {neighbor for root in roots for neighbor, _ in adjacency[root]}
    scores = {seam.id: seam.curve for seam in graph.seams}
    paths = {panel.id: panel.path for panel in graph.panels}
    for row in stack_plan(graph, 1, 0, check_collisions=False):
        scores[row["fold"]] = transform_path(Path(scores[row["fold"]]), translation_2d(complex(*row["shift"])))[0]
    for row in graph.compensation_plan(1, 0):
        shift = translation_2d(complex(*row["shift"]))
        scores[row["fold"]] = transform_path(Path(scores[row["fold"]]), shift)[0]
        paths[row["tab"]] = transform_path(paths[row["tab"]], shift)
    poses = folded_transforms(graph, paths, scores, 1)
    result = {}
    connectors = set(graph.structure.get("shared_connector", []))
    for role, principal in (("top", graph.structure["top"]), ("bottom", graph.structure["bottom"])):
        members = set(graph.structure[role+"_shell"])
        for wall, _ in adjacency[principal]:
            if wall in connectors:
                continue
            center = by_id[principal].polygon.centroid
            wall_center = by_id[wall].polygon.centroid
            direction = (graph._folded_transforms[principal]@np.array([center.x, center.y, 0, 1])-
                         graph._folded_transforms[wall]@np.array([wall_center.x, wall_center.y, 0, 1]))[:3]
            inside_sign = 1 if direction@graph._folded_transforms[wall][:3, 2] >= 0 else -1
            desired = inside_sign if role == "top" else -inside_sign
            layers = 0
            for identifier in members-roots-walls:
                relative = np.linalg.inv(graph._folded_transforms[wall])@graph._folded_transforms[identifier]
                if abs(abs(relative[2, 2])-1) > 1e-7 or abs(relative[2, 3]) > graph.near:
                    continue
                footprint = polygon_transform(by_id[identifier].polygon, planar(relative))
                if footprint.intersection(by_id[wall].polygon).area <= AREA_EPS:
                    continue
                final = np.linalg.inv(poses[wall])@poses[identifier]
                layers = max(layers, int(round(float(final[2, 3])*desired)))
            result[wall] = max(0, layers)
    return result
