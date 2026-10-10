"""Repair editable seed geometry while satisfying its expanded symmetry graph."""
import copy
from dataclasses import replace
from importlib.metadata import version
import io
import math
import xml.etree.ElementTree as ET

import numpy as np
from svgpathtools import Line, Path
from svgpathtools.path import transform as transform_path

from .corners import INKSCAPE, lpe_attributes, rounded_path
from .panelgraph import DEFAULT_NEAR, SVG, PanelGraph, move_segment_endpoint
from .presetmeta import embed


def repair_paths(graph):
    shapes = {shape.id: shape for shape in graph.seed_shapes}
    indices, size = {}, 0
    for shape in shapes.values():
        for index in range(len(shape.path)):
            indices[shape.id, index] = size
            size += 2
    equations, targets = [], []

    def point(seed, segment, parameter, transform):
        path = shapes[seed].path
        row = np.zeros((2, size))
        for index, weight in ((segment, 1-parameter), ((segment+1) % len(path), parameter)):
            column = indices[seed, index]
            row[:, column:column+2] += transform[:2, :2]*weight
        value = path[segment].point(parameter)
        constant = (transform@np.array([value.real, value.imag, 1]))[:2]
        return row, constant

    def equal(first, second):
        equations.extend(first[0]-second[0])
        targets.extend(second[1]-first[1])

    by_id = {panel.id: panel for panel in graph.panels}
    for seam in graph.seams:
        second = by_id[seam.panel_b].path[seam.segment_b]
        seed_a, matrix_a = graph.symmetry_provenance[seam.panel_a]
        seed_b, matrix_b = graph.symmetry_provenance[seam.panel_b]
        tb = sorted(seam.interval_b)
        if abs(second.point(tb[0])-seam.start) > abs(second.point(tb[1])-seam.start):
            tb.reverse()
        for ta, t_b in zip(seam.interval_a, tb):
            equal(point(seed_a, seam.segment_a, ta, matrix_a), point(seed_b, seam.segment_b, t_b, matrix_b))

    # Deduplicated copies still constrain seed geometry. For example, rotating
    # a central rectangle onto itself forces its centre onto the rotation marker.
    for seed_a, matrix_a, seed_b, matrix_b in graph.symmetry_duplicates:
        for index, segment in enumerate(shapes[seed_a].path):
            first = point(seed_a, index, 0, matrix_a)
            candidates = [(np.linalg.norm(first[1]-point(seed_b, other, 0, matrix_b)[1]), other)
                          for other in range(len(shapes[seed_b].path))]
            distance, other = min(candidates)
            if distance <= graph.near:
                equal(first, point(seed_b, other, 0, matrix_b))

    internal = {shape.id for panel in graph.panels for shape in panel.internal}
    for shape in shapes.values():
        if shape.id in internal:
            # Preparing a receiving slot or hole must not change its size.
            for index in range(1, len(shape.path)):
                for axis in range(2):
                    row = np.zeros(size)
                    row[indices[shape.id, index]+axis] = 1
                    row[indices[shape.id, 0]+axis] = -1
                    equations.append(row)
                    targets.append(0)
        for index, segment in enumerate(shape.path):
            if not isinstance(segment, Line):
                continue
            vector = segment.end-segment.start
            # Preserve straight-edge directions. Near-cardinal/45° edges are
            # canonicalized within the repair tolerance, so a rounded decimal
            # gusset diagonal cannot acquire a cycle-breaking angular error.
            angle = math.atan2(vector.imag, vector.real)
            canonical = round(angle/(math.pi/4))*math.pi/4
            normal = np.array([-math.sin(canonical), math.cos(canonical)])
            if shape.id in internal or abs(normal@np.array([vector.real, vector.imag])) > graph.near:
                normal = np.array([-vector.imag, vector.real])/abs(vector)
            row = np.zeros(size)
            end = indices[shape.id, (index+1) % len(shape.path)]
            start = indices[shape.id, index]
            row[end:end+2] += normal
            row[start:start+2] -= normal
            equations.append(row)
            targets.append(-normal@np.array([vector.real, vector.imag]))

    if not equations:
        delta = np.zeros(size)
    else:
        system, target = np.array(equations), np.array(targets)
        delta = np.linalg.lstsq(system, target, rcond=1e-12)[0]
        if np.max(np.abs(system@delta-target)) > 1e-8:
            raise ValueError("Seam repair constraints conflict with the sketch's symmetry or structural edges")
    paths, maximum = {}, 0.0
    for shape in shapes.values():
        offsets = [complex(*delta[indices[shape.id, index]:indices[shape.id, index]+2]) for index in range(len(shape.path))]
        maximum = max(maximum, *(abs(offset) for offset in offsets))
        if any(abs(offset) > graph.near+1e-8 for offset in offsets):
            raise ValueError(f"Repair of {shape.id} exceeds the {graph.near:g} mm near-edge tolerance")
        paths[shape.id] = Path(*(move_segment_endpoint(segment, start=segment.start+offsets[index],
                            end=segment.end+offsets[(index+1) % len(offsets)])
                            for index, segment in enumerate(shape.path)))
    return paths, maximum


def write_path(element, world_path, transform, treatments, root, index):
    local = transform_path(world_path, np.linalg.inv(transform))
    element.tag = f"{{{SVG}}}path"
    for key in list(element.attrib):
        if key in ("x", "y", "width", "height", "rx", "ry", "r", "cx", "cy", "x1", "y1", "x2", "y2", "points"):
            del element.attrib[key]
    if treatments:
        scale = np.linalg.norm(transform[:2, 0])
        local_treatments = {vertex: replace(treatment, radius=treatment.radius/scale)
                            for vertex, treatment in treatments.items()}
        effect_id = f"boxxer-prepared-corners-{index}"
        ids = {node.get("id") for node in root.iter()}
        while effect_id in ids:
            effect_id += "-copy"
        effect, attributes = lpe_attributes(local_treatments, local, effect_id)
        defs = root.find(f"{{{SVG}}}defs")
        if defs is None:
            defs = ET.SubElement(root, f"{{{SVG}}}defs")
        ET.SubElement(defs, f"{{{INKSCAPE}}}path-effect", effect)
        element.attrib.update(attributes)
        element.set(f"{{{INKSCAPE}}}original-d", local.d())
        element.set("d", rounded_path(local, local_treatments).d())
    else:
        element.set("d", local.d())


def prepare(filename, *, near=DEFAULT_NEAR, thickness=None, fold_offset=None, lid_clearance=None):
    defaults = {key: value for key, value in {"thickness": thickness, "fold_offset": fold_offset,
                                             "lid_clearance": lid_clearance}.items() if value is not None}
    if any(not math.isfinite(value) for value in defaults.values()):
        raise ValueError("Preset material defaults must be finite")
    if any(defaults.get(key, 0) < 0 for key in ("thickness", "lid_clearance")):
        raise ValueError("Preset thickness and lid clearance must be nonnegative")
    graph = PanelGraph(filename, near=near, snap_near=True)
    defaults = {**graph.preset_defaults, **defaults}
    if graph.input_is_compiled:
        raise ValueError("Prepare requires an editable source sketch, not compiled manufacturing output")
    blocking_warnings = [warning for warning in graph.contact_warnings if "Inconsistent fold cycle" not in warning]
    if graph.overlaps or blocking_warnings:
        raise ValueError("Source topology is not ready: "+str(graph.overlaps or graph.contact_warnings))
    paths, maximum = repair_paths(graph)
    root = copy.deepcopy(graph.source_root)
    copies = {id(original): duplicate for original, duplicate in zip(graph.source_root.iter(), root.iter())}
    for index, shape in enumerate(graph.seed_shapes):
        original, transform = graph.source_elements[shape.id]
        element = copies[id(original)]
        element.set("id", shape.id)
        if max(abs(a.start-b.start) for a, b in zip(shape.path, paths[shape.id])) > 1e-10:
            write_path(element, paths[shape.id], transform, shape.corner_treatments, root, index)

    provisional = PanelGraph(filename, near=near, snap_near=True, _source_bytes=ET.tostring(root))
    for guide in graph.seed_guides:
        if guide.angle is None:
            continue
        matches = [seam for seam in provisional.seams if isinstance(seam.curve, Line) and
                   provisional._line_match(guide.line, seam.curve) is not None]
        if not matches:
            raise ValueError(f"Cannot relocate fold guide {guide.id} after repair")
        unit = (guide.line.end-guide.line.start)/abs(guide.line.end-guide.line.start)
        points = [point for seam in matches for point in (seam.start, seam.end)]
        points.sort(key=lambda point: ((point-guide.line.start)*unit.conjugate()).real)
        start, end = points[0], points[-1]
        if abs(start-guide.line.start) < 1e-10 and abs(end-guide.line.end) < 1e-10:
            continue
        path = Path(Line(start, end))
        if abs(guide.angle) == 180:
            path.append(Line(end, start))
        original, transform = graph.source_elements[guide.id]
        write_path(copies[id(original)], path, transform, {}, root, 0)

    ready = PanelGraph(filename, near=near, snap_near=False, _source_bytes=ET.tostring(root))
    if ready.near_seams:
        raise ValueError("Prepared geometry still requires near-edge repair")
    if ready.overlaps or ready.contact_warnings:
        raise ValueError("Repair changed material topology or fold closure: "+str(ready.overlaps or ready.contact_warnings))
    def topology(candidate):
        return sorted((tuple(sorted((seam.panel_a, seam.panel_b))), seam.segment_a, seam.segment_b, seam.angle)
                      for seam in candidate.seams)

    before, after = topology(graph), topology(ready)
    if before != after or {p.id for p in graph.panels} != {p.id for p in ready.panels}:
        raise ValueError("Repair changed panel containment or fold topology")
    if ({(shape.id, panel.id) for panel in graph.panels for shape in panel.internal} !=
            {(shape.id, panel.id) for panel in ready.panels for shape in panel.internal} or
            {(c.tab, c.receiver, c.placement) for c in graph.contacts} !=
            {(c.tab, c.receiver, c.placement) for c in ready.contacts}):
        raise ValueError("Repair changed internal-cut ownership or inferred tab contacts")
    model = ready._resize_structure()
    if model["status"] != "supported":
        raise ValueError("Cannot identify preset dimensions: "+model["reason"])
    ready.compile(io.BytesIO(), thickness=defaults.get("thickness"), fold_offset=defaults.get("fold_offset"),
                  lid_clearance=defaults.get("lid_clearance"))
    metadata = embed(root, {"tool_version": version("boxxer"), "units": "mm", "layout": model["kind"], "dimensions": model["dimensions"],
                           "panels": len(ready.panels), "folds": len(ready.seams),
                           "internal_cuts": sum(len(panel.internal) for panel in ready.panels),
                           "receiving_openings": len(ready.opening_interactions),
                           "reinforcement_returns": len(ready.reinforcements),
                           "fold_angles": {str(angle): sum(seam.angle == angle for seam in ready.seams)
                                           for angle in sorted({seam.angle for seam in ready.seams})},
                           "rotation_order": ready.rotational_symmetry[0]["order"] if ready.rotational_symmetry else 1,
                           "mirror_axes": len(ready.mirror_axes), "near_distance": near, "defaults": defaults,
                           "validation": {"compile": True, "material_thickness": defaults.get("thickness")}})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), {
        "status": "ready", "repaired_seams": len(graph.near_seams), "maximum_vertex_shift": maximum,
        "preset": metadata}
