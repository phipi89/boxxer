"""Panel-graph folding and idealized finite-thickness assemblies (millimeters).

Red scores are inward 90-degree folds. Models are static closed assemblies,
not folding simulations. Hinges use trimmed planar miters, not bend radii.
"""
from collections import deque
from dataclasses import dataclass
from itertools import combinations
import io
import math
from pathlib import Path
import statistics

import manifold3d as manifold
import numpy as np
from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union
import trimesh

EPS = 1e-6
AREA_EPS = 1e-6
VOLUME_EPS = 1e-7


def xyz(points, matrix):
    points = np.asarray(points, dtype=float)
    if points.shape[1] == 2:
        points = np.column_stack((points, np.zeros(len(points))))
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def rotation_about(a, b, angle):
    a, b = np.r_[a, 0.0], np.r_[b, 0.0]
    axis = b-a
    axis /= np.linalg.norm(axis)
    x, y, z = axis
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    r = np.eye(3)*math.cos(angle)+(1-math.cos(angle))*np.outer(axis, axis)+math.sin(angle)*cross
    result = np.eye(4)
    result[:3, :3] = r
    result[:3, 3] = a-r@a
    return result


def line_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [line for part in geometry.geoms for line in line_parts(part)]
    return []


def as_solid(mesh):
    result = manifold.Manifold(manifold.Mesh64(
        vert_properties=np.asarray(mesh.vertices, dtype=np.float64),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint64)))
    if result.status() != manifold.Error.NoError:
        raise ValueError(f"Invalid panel mesh: {result.status()}")
    return result


def as_mesh(solid):
    data = solid.to_mesh64()
    return trimesh.Trimesh(vertices=np.asarray(data.vert_properties)[:, :3],
                           faces=np.asarray(data.tri_verts), process=False)


def extrude(polygon, thickness):
    triangles = constrained_delaunay_triangles(polygon)
    vertices, faces, indices = [], [], {}
    for triangle in triangles.geoms:
        face = []
        for pt in list(triangle.exterior.coords)[:3]:
            if pt not in indices:
                indices[pt] = len(vertices)
                vertices.append(pt)
            face.append(indices[pt])
        faces.append(face)
    if not faces:
        raise ValueError("Cannot triangulate an empty panel")
    mesh = trimesh.creation.extrude_triangulation(np.array(vertices), np.array(faces), thickness)
    mesh.apply_translation([0, 0, -thickness/2])
    return mesh


@dataclass
class Face:
    id: str
    polygon: Polygon
    outline: str
    kind: str = ""
    role: str = ""
    transform: np.ndarray | None = None
    solid: object = None
    raw_solid: object = None
    name: str = ""
    direction: str = ""

    @property
    def normal(self):
        return self.transform[:3, 2]

    @property
    def center(self):
        return xyz([self.polygon.centroid.coords[0]], self.transform)[0]

    @property
    def surface(self):
        return xyz(list(self.polygon.exterior.coords)[:-1], self.transform)


@dataclass
class Fold:
    a: int
    b: int
    start: tuple
    end: tuple
    score: str


class Assembly:
    def __init__(self, design, thickness=None, lid_lift=0.0):
        if not math.isfinite(lid_lift) or lid_lift < 0:
            raise ValueError("Lid lift must be finite and nonnegative")
        if design.connected and lid_lift:
            raise ValueError("A connected lid is positioned by its hinges; --lid-lift applies only to separate lids")
        self.design = design
        self.lid_lift = lid_lift
        self.faces = []
        self.folds = []
        self.normalization = []
        self._extract()
        self._fold()
        self.candidate_contacts = self._contact_candidates()
        self.thickness, self.thickness_source = self._thickness(thickness)
        self._place_lid()
        self._name_panels()
        self._solids()
        self._report = None

    def _extract(self):
        # Old exports contain sub-micron differences between cut vertices and
        # their rectangular score anchors. Resolve these explicitly and report
        # every adjustment. Larger endpoint/score errors require boxxer fix.
        anchors = [[p.bounds[axis] for p in self.design.panels] +
                   [p.bounds[axis+2] for p in self.design.panels] for axis in (0, 1)]
        scores = [s for s in self.design.shapes if s.color == "red"]
        segments = [(s.id, LineString([a, b])) for s in scores
                    for a, b in zip(s.points, s.points[1:]) if math.dist(a, b) > EPS]
        for outline in self.design.outlines:
            points = []
            for index, old in enumerate(outline.points):
                new = list(old)
                for axis in (0, 1):
                    target = min(anchors[axis], key=lambda v: abs(v-old[axis]))
                    if EPS < abs(target-old[axis]) <= 0.001:
                        new[axis] = target
                if math.dist(old, new) > EPS:
                    self.normalization.append({"outline": outline.id, "vertex": index,
                                               "before": list(old), "after": new})
                points.append(new)
            material = Polygon(points)
            if not material.is_valid:
                raise ValueError("Cut/score anchor normalization would invalidate the outline")
            lines = [material.boundary]
            local_segments = []
            for name, line in segments:
                # A separate tray's scores belong to just one cut outline.
                if material.distance(line) > EPS:
                    continue
                if line.difference(material.buffer(EPS)).length > EPS:
                    raise ValueError(f"Score {name} extends beyond material; run boxxer fix first")
                clipped = line.intersection(material)
                lines.append(clipped)
                local_segments.append((name, clipped))
            polygons = [p for p in polygonize(unary_union(lines)) if material.covers(p.representative_point())]
            if abs(sum(p.area for p in polygons)-material.area) > AREA_EPS:
                raise ValueError("Score network does not partition the cut material")
            start = len(self.faces)
            for polygon in sorted(polygons, key=lambda p: (p.centroid.y, p.centroid.x)):
                if polygon.area <= AREA_EPS:
                    raise ValueError("Score network creates a sliver; repair the source geometry")
                self.faces.append(Face(f"panel-{len(self.faces)}", polygon, outline.id))
            for i, j in combinations(range(start, len(self.faces)), 2):
                shared = self.faces[i].polygon.boundary.intersection(self.faces[j].polygon.boundary)
                if shared.length <= EPS:
                    continue
                matched = False
                for name, score in local_segments:
                    hinge = shared.intersection(score)
                    parts = line_parts(hinge)
                    if sum(p.length for p in parts) <= EPS:
                        continue
                    coords = [pt for p in parts for pt in p.coords]
                    a, b = max(combinations(coords, 2), key=lambda pair: math.dist(*pair))
                    if abs(math.dist(a, b)-sum(p.length for p in parts)) > EPS:
                        raise ValueError("Disconnected hinge segments need an explicit panel model")
                    self.folds.append(Fold(i, j, a, b, name))
                    matched = True
                if not matched:
                    raise ValueError("Panel boundary has no matching score")
        self.roots = {}
        for panel in self.design.panels:
            reference = box(*panel.bounds)
            matches = [i for i, f in enumerate(self.faces)
                       if f.polygon.symmetric_difference(reference).area <= AREA_EPS]
            if len(matches) != 1:
                raise ValueError(f"Cannot extract {panel.role} main panel; run boxxer fix first")
            self.roots[panel.role] = matches[0]
            self.faces[matches[0]].kind = "main"
            self.faces[matches[0]].role = panel.role
        self.adjacency = {i: [] for i in range(len(self.faces))}
        for fold in self.folds:
            self.adjacency[fold.a].append((fold.b, fold))
            self.adjacency[fold.b].append((fold.a, fold))
        for i, face in enumerate(self.faces):
            if face.kind:
                continue
            neighbors = [role for role, root in self.roots.items()
                         if any(j == root for j, _ in self.adjacency[i])]
            if neighbors:
                face.kind = "connector" if len(neighbors) == 2 else "wall"
                face.role = "base" if len(neighbors) == 2 else neighbors[0]
        for i, face in enumerate(self.faces):
            if face.kind:
                continue
            walls = [self.faces[j] for j, _ in self.adjacency[i] if self.faces[j].kind == "wall"]
            if len(walls) != 1 or len(self.adjacency[i]) != 1:
                raise ValueError("Unsupported panel attachment: expected a tab on a wall")
            face.kind, face.role = "tab", walls[0].role
        expected = len(self.faces)-(1 if self.design.connected else 2)
        if len(self.folds) != expected:
            raise ValueError("Fold graph is incomplete or cyclic; repair dangling scores first")
        expected_tabs = sum(1 for s in scores if not s.panel)
        if sum(f.kind == "tab" for f in self.faces) != expected_tabs:
            raise ValueError("A tab score does not divide the material; run boxxer fix --align-reliefs")

    def _fold(self):
        starts = [self.roots["base"]] if self.design.connected else list(self.roots.values())
        for root in starts:
            face = self.faces[root]
            matrix = np.eye(4)
            matrix[:2, 3] = -np.array(face.polygon.centroid.coords[0])
            face.transform = matrix
            queue = deque([root])
            visited = {root}
            while queue:
                i = queue.popleft()
                for j, fold in self.adjacency[i]:
                    if j in visited:
                        continue
                    a, b = np.array(fold.start), np.array(fold.end)
                    interior = np.array(self.faces[j].polygon.representative_point().coords[0])-a
                    side = np.cross(np.r_[b-a, 0], np.r_[interior, 0])[2]
                    if abs(side) < EPS:
                        raise ValueError("Cannot determine fold direction")
                    self.faces[j].transform = self.faces[i].transform @ rotation_about(a, b, math.copysign(math.pi/2, side))
                    visited.add(j)
                    queue.append(j)
        if any(f.transform is None for f in self.faces):
            raise ValueError("Fold graph has an unattached component")

    def _contact_candidates(self):
        contacts = []
        for tab in [f for f in self.faces if f.kind == "tab"]:
            candidates = []
            for wall in self.faces:
                if wall.kind not in ("wall", "connector") or wall.role != tab.role:
                    continue
                if abs(np.dot(tab.normal, wall.normal)) < 1-1e-8:
                    continue
                axis = int(np.argmax(np.abs(wall.normal)))
                axes = [k for k in range(3) if k != axis]
                overlap = Polygon(tab.surface[:, axes]).intersection(Polygon(wall.surface[:, axes])).area
                distance = float(np.dot(tab.center-wall.center, wall.normal))
                if overlap > AREA_EPS and 0 < distance < min(self.design.base.height, self.design.lid.height)/4:
                    candidates.append({"tab": tab.id, "wall": wall.id,
                                       "separation": distance, "overlap_area": overlap})
            if len(candidates) != 1:
                raise ValueError(f"Cannot identify a unique inward receiving wall for {tab.id}")
            contacts.extend(candidates)
        return contacts

    def _thickness(self, value):
        if value is not None:
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Thickness must be finite and positive")
            return value, "specified"
        if len(self.candidate_contacts) < 3:
            raise ValueError("Too few folded contacts to infer thickness; specify --thickness")
        value = round(statistics.median(c["separation"] for c in self.candidate_contacts), 3)
        support = sum(abs(c["separation"]-value) <= 0.001 for c in self.candidate_contacts)
        if value <= 0 or support <= len(self.candidate_contacts)/2:
            raise ValueError("Folded contacts do not agree on thickness; specify --thickness")
        return value, "inferred from folded tab/wall contacts (0.001 mm precision)"

    def _place_lid(self):
        if self.design.connected:
            return
        # Both trays were folded interior-up about their own centered main panel.
        # Turn the lid over, center in XY, and place its inner ceiling at the
        # highest actual base rim. Clearance above that position is optional.
        rim = max(float(f.surface[:, 2].max()) for f in self.faces if f.role == "base")
        pose = np.diag([1.0, -1.0, -1.0, 1.0])
        pose[2, 3] = rim+self.thickness/2+self.lid_lift
        for f in self.faces:
            if f.role == "lid":
                f.transform = pose @ f.transform

    def _solids(self):
        for face in self.faces:
            mesh = extrude(face.polygon, self.thickness)
            mesh.apply_transform(face.transform)
            # 90-degree rotations leave ~1e-14 artifacts on nominally identical
            # contact planes. Canonicalize below the input SVG's precision before
            # boolean operations, or STL vertex welding can expose false seams.
            mesh.vertices = np.round(mesh.vertices, 8)
            face.solid = as_solid(mesh)
            face.raw_solid = face.solid
        # Partition overlapping slab ends along hinge bisectors. Only known fold
        # junctions are mitered; all other material collisions remain detectable.
        for fold in self.folds:
            parent, child = self.faces[fold.a], self.faces[fold.b]
            hinge = xyz([fold.start, fold.end], parent.transform)
            other = xyz([fold.start, fold.end], child.transform)
            if np.max(np.abs(hinge-other)) > EPS:
                raise ValueError("Fold propagation left disconnected hinge endpoints")
            origin = hinge.mean(axis=0)
            axis = hinge[1]-hinge[0]
            axis /= np.linalg.norm(axis)
            inward = []
            for face in (parent, child):
                direction = face.center-origin
                direction -= axis*np.dot(direction, axis)
                direction /= np.linalg.norm(direction)
                inward.append(direction)
            normal = inward[0]-inward[1]
            normal /= np.linalg.norm(normal)
            offset = float(np.dot(normal, origin))
            # Partition only the actual overlapping slab ends. Trimming an
            # infinite supporting plane would also remove material beyond a
            # partial hinge's endpoints, changing the cut's corner geometry.
            junction = parent.raw_solid ^ child.raw_solid
            parent.solid = parent.solid-junction.trim_by_plane(tuple(-normal), -offset)
            child.solid = child.solid-junction.trim_by_plane(tuple(normal), offset)
        for face in self.faces:
            if face.solid.is_empty() or face.solid.status() != manifold.Error.NoError:
                raise ValueError(f"Thickness eliminates or invalidates {face.id}")

    def _name_panels(self):
        """Names use assembled orientation: front is +Y, right is +X.

        The connected back hinge lies at -Y. The same convention is used for
        separate trays, including the turned-over lid.
        """
        for face in self.faces:
            role = face.role.title()
            if face.kind == "main":
                face.name = "Base bottom" if face.role == "base" else "Lid top"
            elif face.kind == "connector":
                face.name, face.direction = "Shared back / hinge panel", "back"
            elif face.kind == "wall":
                center = self.faces[self.roots[face.role]].center
                axis = int(np.argmax(np.abs(face.normal)))
                positive = face.center[axis] > center[axis]
                face.direction = ("right" if positive else "left") if axis == 0 else ("front" if positive else "back")
                face.name = f"{role} {face.direction} wall"
        for i, face in enumerate(self.faces):
            if face.kind != "tab":
                continue
            wall = self.faces[self.adjacency[i][0][0]]
            center = self.faces[self.roots[face.role]].center
            if wall.direction in ("left", "right"):
                side = wall.direction
                end = "front" if face.center[1] > center[1] else "back"
            else:
                side = "right" if face.center[0] > center[0] else "left"
                end = wall.direction
            face.direction = f"{side}-{end}"
            face.name = f"{face.role.title()} {face.direction} glue tab"

    def _crease_rows(self):
        rows = []
        for i, fold in enumerate(self.folds):
            a, b = self.faces[fold.a], self.faces[fold.b]
            tab = a if a.kind == "tab" else b if b.kind == "tab" else None
            name = f"{tab.name} crease" if tab else f"{a.name} ↔ {b.name} crease"
            angle = math.degrees(math.acos(np.clip(np.dot(a.normal, b.normal), -1, 1)))
            rows.append({"id": f"crease-{i}", "name": name, "panels": [a.id, b.id],
                         "panel_names": [a.name, b.name], "score": fold.score,
                         "start": list(fold.start), "end": list(fold.end), "angle": angle})
        return rows

    def _parallel_gaps(self):
        # Use actual broad faces after mitering, rather than untrimmed panel
        # polygons, to avoid inventing contact in the modeled hinge junctions.
        surfaces = {}
        for face in self.faces:
            mesh = as_mesh(face.solid)
            axis = int(np.argmax(np.abs(face.normal)))
            axes = [k for k in range(3) if k != axis]
            for side in (-1, 1):
                selected = mesh.face_normals[:, axis]*side > 1-1e-8
                plane = face.center[axis]+side*self.thickness/2
                selected &= np.max(np.abs(mesh.triangles[:, :, axis]-plane), axis=1) <= EPS
                triangles = mesh.triangles[selected]
                if not len(triangles):
                    continue
                projection = unary_union([Polygon(t[:, axes]) for t in triangles])
                surfaces[face.id, side] = (float(triangles[:, :, axis].mean()), projection)
        self._broad_surfaces = surfaces
        gaps = []
        for a, b in combinations(self.faces, 2):
            if abs(np.dot(a.normal, b.normal)) < 1-1e-8:
                continue
            axis = int(np.argmax(np.abs(a.normal)))
            if abs(a.center[axis]-b.center[axis]) <= EPS:
                continue
            left, right = (a, b) if a.center[axis] < b.center[axis] else (b, a)
            if (left.id, 1) not in surfaces or (right.id, -1) not in surfaces:
                continue
            lo, lp = surfaces[left.id, 1]
            hi, rp = surfaces[right.id, -1]
            overlap = lp.intersection(rp).area
            if overlap <= AREA_EPS:
                continue
            gaps.append({"panels": [left.id, right.id], "axis": "xyz"[axis],
                         "gap": hi-lo, "overlap_area": overlap,
                         "cross_part": left.role != right.role})
        return gaps

    def _mating_gaps(self):
        gaps = []
        base_center = self.faces[self.roots["base"]].center
        lid_center = self.faces[self.roots["lid"]].center
        for base in [f for f in self.faces if f.role == "base" and f.kind not in ("main", "connector")]:
            for lid in [f for f in self.faces if f.role == "lid" and f.kind != "main"]:
                if abs(np.dot(base.normal, lid.normal)) < 1-1e-8:
                    continue
                axis = int(np.argmax(np.abs(base.normal)))
                if axis == 2:
                    continue
                side = 1 if lid.center[axis] > lid_center[axis] else -1
                if side*(base.center[axis]-base_center[axis]) <= EPS:
                    continue
                outer = self._broad_surfaces.get((base.id, side))
                inner = self._broad_surfaces.get((lid.id, -side))
                if outer is None or inner is None:
                    continue
                area = outer[1].intersection(inner[1]).area
                if area <= AREA_EPS:
                    continue
                # This is an oriented nesting constraint, not an unsigned plane
                # distance. A lid wall that has passed to the inside of the base
                # must fail even if the two thin slabs no longer intersect.
                gaps.append({"panels": [base.id, lid.id], "axis": "xyz"[axis],
                             "side": "positive" if side == 1 else "negative",
                             "gap": side*(inner[0]-outer[0]), "overlap_area": area})
        return gaps

    def report(self):
        if self._report is not None:
            return self._report
        collisions = []
        meshes = {f.id: as_mesh(f.solid) for f in self.faces}
        for a, b in combinations(self.faces, 2):
            amin, amax = meshes[a.id].bounds
            bmin, bmax = meshes[b.id].bounds
            if np.any(np.minimum(amax, bmax)-np.maximum(amin, bmin) <= EPS):
                continue
            intersection = a.solid ^ b.solid
            volume = float(intersection.volume())
            if volume > VOLUME_EPS:
                vertices = as_mesh(intersection).vertices
                extents = np.ptp(vertices, axis=0)
                if a.kind == "main" or b.kind == "main":
                    main = a if a.kind == "main" else b
                    axis = int(np.argmax(np.abs(main.normal)))
                elif abs(np.dot(a.normal, b.normal)) > 1-1e-8:
                    axis = int(np.argmax(np.abs(a.normal)))
                else:
                    axes = {int(np.argmax(np.abs(a.normal))), int(np.argmax(np.abs(b.normal)))}
                    axis = min(axes, key=lambda k: extents[k])
                collisions.append({"panels": [a.id, b.id], "panel_names": [a.name, b.name], "volume": volume,
                                    "overlap_depth": float(extents[axis]), "axis": "xyz"[axis],
                                    "distance_measure": "extent of the actual intersection along the named axis; not a minimum separating translation",
                                    "cross_part": a.role != b.role})
        gaps = self._parallel_gaps()
        contacts = []
        warnings = []
        names = {f.id: f.name for f in self.faces}
        angle_rows = self._crease_rows()
        for c in self.candidate_contacts:
            gap = c["separation"]-self.thickness
            crease = next(f for f in angle_rows if c["tab"] in f["panels"])
            contacts.append({**c, "tab_name": names[c["tab"]], "wall_name": names[c["wall"]],
                             "crease": crease["id"], "crease_name": crease["name"],
                             "score": crease["score"], "gap": gap,
                             "status": "contact" if abs(gap) <= EPS else "gap" if gap > 0 else "interference"})
            if abs(gap) > EPS:
                warnings.append(f"{names[c['tab']]} / {names[c['wall']]}: signed contact gap {gap:.6f} mm")
        for collision in collisions:
            direction = {"x": "left/right", "y": "front/back", "z": "vertical"}[collision["axis"]]
            warnings.append(f"{' / '.join(collision['panel_names'])}: {collision['overlap_depth']:.6f} mm {direction} overlap")
        wall_stacks = []
        for wall in [f for f in self.faces if f.kind in ("wall", "connector")]:
            wall_stacks.append({"wall": wall.id, "single_layer_thickness": self.thickness,
                                "tab_regions": [{"tab": c["tab"], "layers": 2,
                                                 "material_thickness": 2*self.thickness,
                                                 "envelope_thickness": self.thickness+c["separation"],
                                                 "overlap_area": c["overlap_area"], "gap": c["gap"]}
                                                for c in contacts if c["wall"] == wall.id]})
        mating = self._mating_gaps()
        clearance = {axis: min((g["gap"] for g in mating if g["axis"] == axis), default=None)
                     for axis in ("x", "y")}
        base_rim = max(float(meshes[f.id].bounds[1, 2]) for f in self.faces
                       if f.role == "base" and f.kind not in ("main", "connector"))
        lid_main = self.faces[self.roots["lid"]]
        ceiling = float(lid_main.center[2]-self.thickness/2)
        rim_gap = ceiling-base_rim
        if rim_gap < -EPS:
            warnings.append(f"Base rim is {-rim_gap:.6f} mm above the lid's inner ceiling")
        if any(v is None for v in clearance.values()):
            warnings.append("Lid/base walls do not overlap along both footprint axes; nested fit is undetermined")
        for axis, gap in clearance.items():
            if gap is not None and gap < -EPS:
                warnings.append(f"Lid nesting constraint fails on {axis.upper()}: signed clearance {gap:.6f} mm")
        if any(v is not None and abs(v) <= EPS for v in clearance.values()):
            warnings.append("Lid has zero nominal sliding clearance at a limiting surface")
        panel_rows = []
        for face in self.faces:
            panel_rows.append({"id": face.id, "name": face.name, "direction": face.direction,
                               "role": face.role, "kind": face.kind,
                               "source_outline": face.outline, "flat_area": face.polygon.area,
                               "flat_boundary": [list(pt) for pt in face.polygon.exterior.coords],
                               "normal": face.normal.tolist(), "transform": face.transform.tolist(),
                               "bounds": meshes[face.id].bounds.tolist(), "volume": float(face.solid.volume())})
        envelopes = {}
        for role in ("base", "lid"):
            members = [f for f in self.faces if f.role == role]
            bounds = np.array([meshes[f.id].bounds for f in members])
            lo, hi = bounds[:, 0, :].min(axis=0), bounds[:, 1, :].max(axis=0)
            center = self.faces[self.roots[role]].center
            inner = {}
            for axis in (0, 1):
                walls = [f for f in members if abs(f.normal[axis]) > 1-1e-8]
                near = [f.center[axis]+self.thickness/2 for f in walls if f.center[axis] < center[axis]-EPS]
                far = [f.center[axis]-self.thickness/2 for f in walls if f.center[axis] > center[axis]+EPS]
                inner["xy"[axis]] = float(min(far)-max(near)) if near and far else None
            envelopes[role] = {"bounds": [lo.tolist(), hi.tolist()], "outer_extent": (hi-lo).tolist(),
                               "limiting_inner_plane_span": inner}
        self._report = {
            "units": {"length": "mm", "area": "mm²", "volume": "mm³"},
            "layout": "connected" if self.design.connected else "separate",
            "thickness": self.thickness, "thickness_source": self.thickness_source,
            "dimensions": self.design.report()["panels"],
            "connector_depth": self.design.connector_depth, "part_envelopes": envelopes,
            "model": {"folds": "inward 90 degrees", "hinges": "slabs trimmed at hinge bisectors; no bend radius",
                      "placement": "hinge-constrained" if self.design.connected else "centered XY; inner lid ceiling at highest base rim plus lid lift",
                      "lid_lift": self.lid_lift,
                      "numerical_epsilon_mm": EPS, "intersection_volume_epsilon_mm3": VOLUME_EPS,
                      "mesh_coordinate_precision_mm": 1e-8,
                      "scope": "static assembled state; does not prove a collision-free closing path or physical bend behavior"},
            "topology_adjustments": self.normalization, "panels": panel_rows,
            "folds": angle_rows, "tab_contacts": contacts, "wall_stacks": wall_stacks,
            "parallel_surfaces": gaps,
            "interferences": collisions,
            "lid_fit": {"minimum_clearance_per_side": clearance,
                        "local_clearances": mating, "rim_to_ceiling": rim_gap,
                        "material_interference": any(c["cross_part"] for c in collisions),
                        "static_fit": (not any(c["cross_part"] for c in collisions) and rim_gap >= -EPS
                                       and all(v is not None and v >= -EPS for v in clearance.values())),
                        "all_material_interference_free": not collisions},
            "warnings": warnings,
        }
        return self._report

    def mesh(self, role=None):
        # The miter partitions each hinge overlap without changing its union.
        # Union the original prisms directly: exporting two separately capped
        # coincident miter faces can leave sub-floating-point seams in STL.
        solids = [f.raw_solid for f in self.faces if role is None or f.role == role]
        if not solids:
            raise ValueError(f"No panels for {role}")
        combined = manifold.Manifold.batch_boolean(solids, manifold.OpType.Add)
        mesh = as_mesh(combined)
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
            raise ValueError("Export did not produce a closed, consistently oriented mesh")
        return mesh

    def write_stl(self, filename, role=None):
        # Union removes internal coincident faces for a usable STL. The report
        # records pre-union interference, so export never hides fit errors.
        mesh = self.mesh(role)
        data = mesh.export(file_type="stl")
        # Binary STL is float32 and has no shared vertex indices. Check the
        # actual serialized result, not just the indexed in-memory mesh.
        loaded = trimesh.load_mesh(io.BytesIO(data), file_type="stl")
        if not loaded.is_watertight or not loaded.is_winding_consistent or loaded.volume <= 0:
            raise ValueError("STL precision would create a non-closed mesh")
        Path(filename).write_bytes(data)
