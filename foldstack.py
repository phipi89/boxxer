"""Finite-thickness score placement for explicitly annotated folded components.

Panels are rigid midplanes. Each hinge rotates at the scored surface, +t/2
for valley and -t/2 for mountain. This is a sharp-hinge construction, not a
bend-radius or material-compression model. Crease shifts are solved together
around cycles, keeping the largest panel's creases fixed.
"""
import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from svgpathtools import Line


def stack_plan(graph, thickness, adjustment):
    thickness = thickness or 0.0
    if not math.isfinite(thickness) or thickness < 0 or not math.isfinite(adjustment):
        raise ValueError("Fold stack thickness and adjustment must be finite; thickness must be nonnegative")
    pitch = thickness+adjustment
    if pitch < 0:
        raise ValueError("Thickness plus fold offset cannot be negative")
    panels = {panel.id: panel for panel in graph.panels}
    rows = []
    for component in graph.components:
        seams = [seam for seam in graph.seams if seam.panel_a in component]
        if not any(seam.annotated and seam.angle != 90 for seam in seams):
            continue
        if graph.contact_warnings:
            raise ValueError("Cannot place signed-fold scores: " + " ".join(graph.contact_warnings))
        if any(not isinstance(seam.curve, Line) for seam in seams):
            raise ValueError("Signed-fold thickness placement requires straight hinges")
        root = max(component, key=lambda identifier: (panels[identifier].polygon.area, identifier))
        moving = [identifier for identifier in component if identifier != root]
        # Incidence for world-space panel translations, and the effect of each
        # planar perpendicular crease displacement on its hinge equation.
        incidence = np.zeros((3*len(seams), 3*len(moving)))
        shifts = np.zeros((3*len(seams), len(seams)))
        target = np.zeros(3*len(seams))
        normals = []
        for index, seam in enumerate(seams):
            first, second = seam.panel_a, seam.panel_b
            r1 = graph._folded_transforms[first][:3, :3]
            r2 = graph._folded_transforms[second][:3, :3]
            difference = r1-r2
            unit = (seam.end-seam.start)/abs(seam.end-seam.start)
            normal = complex(-unit.imag, unit.real)
            normals.append(normal)
            block = slice(3*index, 3*index+3)
            for identifier, sign in ((second, 1), (first, -1)):
                if identifier != root:
                    column = moving.index(identifier)*3
                    incidence[block, column:column+3] = sign*np.eye(3)
            shifts[block, index] = difference@np.array([normal.real, normal.imag, 0])
            target[block] = difference@np.array([0, 0, math.copysign(pitch/2, seam.angle)])
        # Eliminate free panel translations. What remains are precisely the
        # loop-closure equations. Minimise score motion, not panel translation.
        projection = np.eye(len(target))-incidence@np.linalg.pinv(incidence)
        system, rhs = [projection@shifts], [-projection@target]
        for index, seam in enumerate(seams):
            if root in (seam.panel_a, seam.panel_b):
                anchor = np.zeros((1, len(seams)))
                anchor[0, index] = 1
                system.append(anchor)
                rhs.append(np.zeros(1))
        system, rhs = np.vstack(system), np.concatenate(rhs)
        distances = np.linalg.lstsq(system, rhs, rcond=1e-10)[0]
        delta = np.linalg.lstsq(incidence, shifts@distances+target, rcond=None)[0]
        residual = float(np.max(np.abs(incidence@delta-shifts@distances-target), initial=0))
        if residual > 1e-7 or np.max(np.abs(system@distances-rhs), initial=0) > 1e-7:
            raise ValueError("Signed fold stack cannot close with fixed principal creases")
        translations = {root: np.zeros(3)}
        translations.update({identifier: delta[3*i:3*i+3] for i, identifier in enumerate(moving)})
        # Parallel overlapping faces must not occupy the same physical layer.
        surfaces = []
        for identifier in component:
            panel = panels[identifier]
            transform = graph._folded_transforms[identifier]
            points = np.array([[x, y, 0, 1] for x, y in panel.polygon.exterior.coords])
            world = (points@transform.T)[:, :3]+translations[identifier]
            surfaces.append((identifier, world, transform[:3, :3]))
        for index, (identifier, world, rotation) in enumerate(surfaces):
            normal = rotation[:, 2]
            polygon = Polygon(world@rotation[:, :2])
            for other_id, other, other_rotation in surfaces[index+1:]:
                if abs(abs(float(normal@other_rotation[:, 2]))-1) > 1e-7:
                    continue
                separation = abs(float((world[0]-other[0])@normal))
                overlap = polygon.intersection(Polygon(other@rotation[:, :2])).area
                repair_tolerance = max(1e-7, sum(seam.discrepancy for seam in seams)*2)
                if overlap > graph.near**2 and separation < thickness-repair_tolerance:
                    raise ValueError(f"Folded material layers overlap: {identifier}/{other_id}; "
                                     "fold offset cannot compress physical thickness")
        for index, seam in enumerate(seams):
            shift = normals[index]*distances[index]
            rows.append({"fold": seam.id, "angle": seam.angle,
                         "score_side": "front" if seam.angle > 0 else "back",
                         "shift": [float(shift.real), float(shift.imag)],
                         "distance": float(abs(shift)), "thickness": thickness,
                         "fold_offset": adjustment, "layer_pitch": pitch,
                         "closure_residual": residual,
                         "model": "scored-surface-sharp-hinge"})
    return rows


def shifted_scores(seams, curves, plans, material, tolerance):
    """Translate score lines; terminate free ends at the original cut boundary.

    At internal multi-crease junctions, retain translated endpoints: they are
    finite-thickness crease terminations, not a fictitious common rigid vertex.
    """
    result = dict(curves)
    by_id = {seam.id: seam for seam in seams}
    for row in plans:
        offset = complex(*row["shift"])
        if abs(offset) < 1e-9:
            continue
        seam = by_id[row["fold"]]
        curve = curves[seam.id]
        unit = (curve.end-curve.start)/abs(curve.end-curve.start)
        extension = abs(offset)*4+tolerance*4
        start, end = curve.start+offset, curve.end+offset
        free_start = material.boundary.distance(Point(curve.start.real, curve.start.imag)) <= tolerance
        free_end = material.boundary.distance(Point(curve.end.real, curve.end.imag)) <= tolerance
        a = start-unit*extension if free_start else start
        b = end+unit*extension if free_end else end
        clipped = material.intersection(LineString([(a.real, a.imag), (b.real, b.imag)]))
        if clipped.geom_type != "LineString" or clipped.length <= tolerance:
            raise ValueError(f"Shifted score {seam.id} leaves or crosses a cut in the material")
        coords = list(clipped.coords)
        points = [complex(*coords[0]), complex(*coords[-1])]
        points.sort(key=lambda point: ((point-start)*unit.conjugate()).real)
        result[seam.id] = Line(*points)
    return result
