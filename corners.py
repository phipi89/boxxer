"""Inkscape fillet/chamfer corner effects with a sharp canonical path."""
from dataclasses import dataclass
import math

from svgpathtools import Arc, Line, Path, parse_path
from svgpathtools.path import transform as transform_path


INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
DEFAULT_SATELLITE = "F,0,0,1,0,0,0,1"
EPS = 1e-8


@dataclass(frozen=True)
class CornerTreatment:
    radius: float
    satellite: str
    effect: tuple[tuple[str, str], ...]


def _corner_tangent(path, index, radius):
    previous = path[(index-1) % len(path)]
    following = path[index]
    if not isinstance(previous, Line) or not isinstance(following, Line):
        raise ValueError("Editable corner no longer joins two straight contour edges")
    corner = following.start
    incoming_length, outgoing_length = abs(corner-previous.start), abs(following.end-corner)
    if min(incoming_length, outgoing_length) <= EPS:
        raise ValueError("Editable corner has a zero-length adjacent edge")
    toward_previous = (previous.start-corner)/incoming_length
    toward_next = (following.end-corner)/outgoing_length
    cosine = max(-1.0, min(1.0, (toward_previous.conjugate()*toward_next).real))
    angle = math.acos(cosine)
    if angle <= 1e-7 or math.pi-angle <= 1e-7:
        raise ValueError("Editable corner requires two non-collinear edges")
    return previous, following, corner, toward_previous, toward_next, radius/math.tan(angle/2)


def _point_on_line(point, line, tolerance=1e-5):
    vector = line.end-line.start
    fraction = ((point-line.start)*vector.conjugate()).real/(abs(vector)**2)
    projected = line.start+vector*fraction
    return -tolerance <= fraction <= 1+tolerance and abs(projected-point) <= tolerance


def parse_corner_lpe(element, effects, transform):
    """Return the sharp path and per-vertex fillets for one Inkscape path."""
    reference = element.get(f"{{{INKSCAPE}}}path-effect")
    original = element.get(f"{{{INKSCAPE}}}original-d")
    if not reference and not original:
        return None
    if not reference or not original or not reference.startswith("#"):
        raise ValueError(f"{element.get('id', 'path')} has incomplete Inkscape path-effect data")
    effect = effects.get(reference[1:])
    if effect is None or effect.get("effect") != "fillet_chamfer":
        raise ValueError(f"{element.get('id', 'path')} uses an unsupported Inkscape path effect")
    if effect.get("mode", "F") != "F" or effect.get("chamfer_steps", "1") != "1":
        raise ValueError("Only rounded fillet_chamfer corners are supported")
    sharp = transform_path(parse_path(original), transform)
    visible = transform_path(parse_path(element.get("d", "")), transform)
    if not sharp or any(not isinstance(segment, Line) for segment in sharp):
        raise ValueError("Editable corner effects require a straight-segment original-d path")
    entries = [entry.strip() for entry in effect.get("nodesatellites_param", "").split("@")]
    if len(entries) != len(sharp):
        raise ValueError("Inkscape corner parameters do not match original-d vertices")
    arcs = [segment for segment in visible if isinstance(segment, Arc)]
    attributes = tuple(sorted((key, value) for key, value in effect.attrib.items()
                              if key not in ("id", "nodesatellites_param")))
    treatments = {}
    used_arcs = set()
    for index, entry in enumerate(entries):
        fields = [field.strip() for field in entry.split(",")]
        try:
            active = len(fields) > 5 and abs(float(fields[5])) > EPS
        except ValueError as exc:
            raise ValueError("Invalid Inkscape corner parameter") from exc
        if not active:
            continue
        previous, following, corner, _, _, _ = _corner_tangent(sharp, index, 1)
        candidates = [arc for arc in arcs if id(arc) not in used_arcs and
                      _point_on_line(arc.start, previous) and _point_on_line(arc.end, following)]
        if not candidates:
            raise ValueError("Active Inkscape corner arc does not meet its original-d edges")
        arc = min(candidates, key=lambda candidate: abs(candidate.start-corner)+abs(candidate.end-corner))
        if abs(abs(arc.radius.real)-abs(arc.radius.imag)) > 1e-6:
            raise ValueError("Editable corners cannot use a nonuniformly scaled radius")
        radius = (abs(arc.radius.real)+abs(arc.radius.imag))/2
        if radius <= EPS:
            raise ValueError("Inkscape corner radius must be positive")
        _, _, _, toward_previous, toward_next, tangent = _corner_tangent(sharp, index, radius)
        expected_start = corner+toward_previous*tangent
        expected_end = corner+toward_next*tangent
        incoming = (corner-previous.start)/abs(corner-previous.start)
        outgoing = (following.end-corner)/abs(following.end-corner)
        expected_sweep = (incoming.conjugate()*outgoing).imag > 0
        tolerance = max(1e-5, radius*1e-6)
        if (abs(arc.start-expected_start) > tolerance or abs(arc.end-expected_end) > tolerance or
                bool(arc.sweep) != expected_sweep or bool(arc.large_arc)):
            raise ValueError("Rendered Inkscape corner is not the circular fillet described by original-d")
        used_arcs.add(id(arc))
        treatments[index] = CornerTreatment(radius, entry, attributes)
    return sharp, treatments


def rounded_path(path, treatments):
    """Render circular fillets while retaining the sharp path for the live LPE."""
    if not treatments:
        return Path(*path)
    segments = list(path)
    starts, ends, arcs = {}, {}, {}
    for index, treatment in treatments.items():
        previous, following, corner, toward_previous, toward_next, tangent = _corner_tangent(
            path, index, treatment.radius)
        incoming_length, outgoing_length = abs(corner-previous.start), abs(following.end-corner)
        if tangent >= min(incoming_length, outgoing_length)-EPS:
            raise ValueError("Corner radius no longer fits its adjacent contour edges")
        before = corner+toward_previous*tangent
        after = corner+toward_next*tangent
        incoming = (corner-previous.start)/incoming_length
        outgoing = (following.end-corner)/outgoing_length
        sweep = (incoming.conjugate()*outgoing).imag > 0
        ends[(index-1) % len(segments)] = before
        starts[index] = after
        arcs[(index-1) % len(segments)] = Arc(
            before, complex(treatment.radius, treatment.radius), 0, False, sweep, after)
    rendered = []
    for index, segment in enumerate(segments):
        rendered.append(Line(starts.get(index, segment.start), ends.get(index, segment.end))
                        if index in starts or index in ends else segment)
        if index in arcs:
            rendered.append(arcs[index])
    return Path(*rendered)


def lpe_attributes(treatments, path, effect_id):
    """Build one live fillet effect for a source panel or merged cut contour."""
    entries = [DEFAULT_SATELLITE]*len(path)
    first = None
    for index, treatment in treatments.items():
        if first is not None and treatment.effect != first.effect:
            raise ValueError("One contour cannot combine incompatible Inkscape corner effects")
        fields = treatment.satellite.split(",")
        if len(fields) <= 5:
            raise ValueError("Invalid Inkscape corner parameter")
        fields[5] = f"{_corner_tangent(path, index, treatment.radius)[-1]:.12g}"
        entries[index] = ",".join(fields)
        first = first or treatment
    if first is None:
        return None, None
    effect = dict(first.effect)
    effect["id"] = effect_id
    effect["nodesatellites_param"] = " @ ".join(entries)
    path_attributes = {f"{{{INKSCAPE}}}path-effect": f"#{effect_id}"}
    return effect, path_attributes
