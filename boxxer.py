"""Compile editable SVG panel collections into plotter cut and score paths."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

from panelgraph import DEFAULT_NEAR, PanelGraph


def paths_alias(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def temporary_output(target):
    handle = tempfile.NamedTemporaryFile(prefix=f".{target.name}.", dir=target.parent, delete=False)
    handle.close()
    return Path(handle.name)


def print_report(report):
    print(f"Panel source: {report['source']}")
    print(f"Panels: {len(report['panels'])}; internal cuts: {len(report['internal_geometry'])}; components: {len(report['components'])}")
    print(f"Folds: {len(report['folds'])}; near-coincident edge pairs: {len(report['near_edges'])}")
    for panel in report["panels"]:
        hint = "; outside-placement hint" if panel["outside_hint"] else ""
        internals = f"; internal: {', '.join(panel['internal_geometry'])}" if panel["internal_geometry"] else ""
        print(f"  {panel['id']}: {panel['area']:.3f} mm²{hint}{internals}")
    for fold in report["folds"]:
        repaired = f"; repaired {fold['original_discrepancy']:.6f} mm discrepancy" if fold["repaired"] else ""
        print(f"  {fold['id']}: {fold['panels'][0]} ↔ {fold['panels'][1]}, {fold['length']:.3f} mm, {fold['placement']}{repaired}")
    for edge in report["near_edges"]:
        status = "Repaired near edges" if any(f["repaired"] and f["panels"] == edge["panels"] for f in report["folds"]) else "Warning: near edges"
        print(f"{status} {edge['panels'][0]} / {edge['panels'][1]}: "
              f"{edge['length']:.3f} mm overlap at {edge['distance']:.6f} mm separation")
    for overlap in report["overlaps"]:
        print(f"Error: panels {overlap['panels'][0]} / {overlap['panels'][1]} overlap by {overlap['area']:.6f} mm²")
    if report.get("contacts"):
        print(f"Inferred glue-tab contacts: {len(report['contacts'])}")
        for contact in report["contacts"]:
            print(f"  {contact['tab']} → {contact['receiver']} via {contact['fold']} ({contact['placement']})")
    structure = report.get("structure", {})
    if structure.get("status") == "inferred":
        print(f"Connected enclosure: top {structure['top']} outside bottom {structure['bottom']}; "
              f"connector {', '.join(structure['shared_connector'])}")
    resizing = report.get("resizing", {})
    if resizing.get("status") == "supported":
        dimensions = ", ".join(f"{key.replace('_', ' ')} {value:.3f} mm"
                               for key, value in resizing["dimensions"].items())
        print(f"Resizable {resizing['kind']} box net: {dimensions}")
    lid_fit = report.get("lid_fit", {})
    if lid_fit.get("status") == "applied":
        print("Applied outer-lid fit:")
        for row in lid_fit["walls"]:
            print(f"  {row['top_wall']} over {row['bottom_wall']}: {row['midplane_offset']:.3f} mm "
                  f"({row['gap_layers']} flap layer(s) in gap)")
    if any(row["distance"] or any(row["wall_edge_shift"])
           for row in report.get("compensation", [])):
        print("Applied compensation:")
        for row in report["compensation"]:
            print(f"  {row['fold']}: move {row['tab']} by ({row['shift'][0]:.3f}, {row['shift'][1]:.3f}) mm; "
                  f"wall end by ({row['wall_edge_shift'][0]:.3f}, {row['wall_edge_shift'][1]:.3f}) mm; "
                  f"distance {row['distance']:.3f} mm")
    for warning in report["warnings"]:
        print("Warning:", warning)
    if "output" in report:
        print(f"Wrote {report['output']}: {report['cut_contours']} cut contour(s), "
              f"{report['internal_cuts']} internal cut(s), {report['relief_cuts']} relief cut(s), "
              f"{len(report['folds'])} score line(s)")
        if report.get("page_overflow"):
            print("Output geometry extends beyond the A4 page.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "compile", "resize"):
        command = subparsers.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--near-distance", type=float,
                             help=f"Candidate seam distance in mm (source default: {DEFAULT_NEAR:g}; compiled input reuses stored value)")
        command.add_argument("--snap-near", action=argparse.BooleanOptionalAction, default=None,
                             help="Repair near-coincident seams; compiled input reuses its stored setting when omitted")
        command.add_argument("--json", action="store_true")
        command.add_argument("--thickness", type=float,
                             help="Material thickness in mm; enables inferred glue-tab compensation")
        command.add_argument("--fold-offset", type=float,
                             help="Additional signed glue-tab crease displacement in mm")
        command.add_argument("--lid-clearance", type=float,
                             help="Additional per-side clearance for an inferred outer lid in mm")
        if name in ("compile", "resize"):
            command.add_argument("--output", "-o", type=Path, required=True)
        if name == "resize":
            command.add_argument("--width", type=float, help="Nominal base/principal fold-to-fold width in mm")
            command.add_argument("--length", type=float, help="Nominal base/principal fold-to-fold length in mm")
            command.add_argument("--height", type=float, help="Nominal wall height in mm")
            command.add_argument("--base-height", type=float, help="Connected base wall height in mm")
            command.add_argument("--lid-height", type=float, help="Connected lid wall height in mm")
            command.add_argument("--connector-depth", type=float, help="Connected hinge-panel depth in mm")
            command.add_argument("--source-output", type=Path,
                                 help="Also write the editable resized panel-source SVG")
    args = parser.parse_args()
    try:
        if args.command in ("compile", "resize") and paths_alias(args.output, args.input):
            raise ValueError("Input and output paths must differ")
        if args.command == "resize" and args.source_output is not None:
            if (paths_alias(args.source_output, args.input) or
                    paths_alias(args.source_output, args.output)):
                raise ValueError("Input, compiled output, and source output paths must differ")
        if args.near_distance is not None and (not math.isfinite(args.near_distance) or args.near_distance <= 0):
            raise ValueError("Near-edge distance must be finite and positive")
        graph = PanelGraph(args.input, args.near_distance, args.snap_near)
        stored = graph.compiled_settings
        thickness = args.thickness if args.thickness is not None else stored.get("thickness")
        fold_offset = args.fold_offset if args.fold_offset is not None else stored.get("fold_offset", 0)
        lid_clearance = args.lid_clearance if args.lid_clearance is not None else stored.get("lid_clearance", 0)
        if args.command == "inspect":
            report = graph.report(thickness, fold_offset, lid_clearance)
        else:
            if args.command == "resize":
                graph = graph.resized(width=args.width, length=args.length, height=args.height,
                                      base_height=args.base_height, lid_height=args.lid_height,
                                      connector_depth=args.connector_depth)
                compiled_temp = temporary_output(args.output)
                source_temp = temporary_output(args.source_output) if args.source_output is not None else None
                try:
                    report = graph.compile(compiled_temp, thickness=thickness, fold_offset=fold_offset,
                                           lid_clearance=lid_clearance)
                    if source_temp is not None:
                        source_temp.write_bytes(graph.source_bytes)
                    os.replace(compiled_temp, args.output)
                    if source_temp is not None:
                        os.replace(source_temp, args.source_output)
                    report["output"] = str(args.output)
                finally:
                    compiled_temp.unlink(missing_ok=True)
                    if source_temp is not None:
                        source_temp.unlink(missing_ok=True)
            else:
                report = graph.compile(args.output, thickness=thickness, fold_offset=fold_offset,
                                       lid_clearance=lid_clearance)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_report(report)
        return 1 if report["warnings"] else 0
    except (ValueError, OSError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
