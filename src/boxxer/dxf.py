"""Silhouette-friendly millimetre DXF paths with explicit operation colours."""
import math
from pathlib import Path as FilePath

import ezdxf
from svgpathtools import Arc, CubicBezier, Line, QuadraticBezier

LAYERS = {"CUT": 7, "SCORE_FRONT": 1, "SCORE_BACK": 5}
CURVE_TOLERANCE = 0.01  # Maximum chord error in mm for every curve.


def flatten(segment, tolerance=CURVE_TOLERANCE, depth=0):
    """Bound curve deviation from chords; do not use a fixed point count."""
    if isinstance(segment, Line):
        return [segment.start, segment.end]
    if isinstance(segment, Arc):
        radius = max(abs(segment.radius.real), abs(segment.radius.imag))
        step = 2*math.acos(max(-1, 1-tolerance/radius))
        count = max(1, math.ceil(abs(math.radians(segment.delta))/step))
        return [segment.start]+[segment.point(index/count) for index in range(1, count)]+[segment.end]
    if not isinstance(segment, (CubicBezier, QuadraticBezier)):
        raise ValueError(f"Unsupported DXF curve: {type(segment).__name__}")
    controls = [segment.control1, segment.control2] if isinstance(segment, CubicBezier) else [segment.control]
    vector = segment.end-segment.start

    def distance(point):
        fraction = max(0, min(1, ((point-segment.start)*vector.conjugate()).real/abs(vector)**2)) if abs(vector) > 1e-12 else 0
        return abs(point-segment.start-vector*fraction)

    if max(map(distance, controls)) <= tolerance:
        return [segment.start, segment.end]
    if depth >= 30:
        raise ValueError("DXF curve cannot be flattened within the requested tolerance")
    return flatten(segment.cropped(0, .5), tolerance, depth+1)[:-1]+flatten(segment.cropped(.5, 1), tolerance, depth+1)


def vertices(path, page_height):
    if not path or not path.iscontinuous():
        raise ValueError("DXF operations must be nonempty continuous paths")
    result = []
    for segment in path:
        if isinstance(segment, Line) and abs(segment.end-segment.start) < 1e-10:
            continue
        # Studio can misplace circular segments in mixed curved polylines.
        # Use straight vertices for all curves, with no native bulges.
        result.extend((point.real, page_height-point.imag, 0) for point in flatten(segment)[:-1])
    if not path.isclosed():
        result.append((path.end.real, page_height-path.end.imag, 0))
    if len(result) < 2:
        raise ValueError("DXF operation has no useful extent")
    return result


def write_dxf(output, operations, page):
    """Write final manufacturing paths; SVG dash patterns never fragment scores."""
    document = ezdxf.new("R2000")
    document.units = ezdxf.units.MM
    document.header["$MEASUREMENT"] = 1
    document.header["$LUNITS"] = 2
    document.header["$LUPREC"] = 4
    for layer, color in LAYERS.items():
        document.layers.new(layer, dxfattribs={"color": color, "linetype": "CONTINUOUS"})
    space = document.modelspace()
    for layer, path in operations:
        space.add_lwpolyline(vertices(path, page[1]), format="xyb", close=path.isclosed(),
                            dxfattribs={"layer": layer, "color": LAYERS[layer], "linetype": "CONTINUOUS"})
    if isinstance(output, (str, FilePath)):
        document.saveas(output)
    else:
        document.write(output)
