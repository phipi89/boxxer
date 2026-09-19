"""Compile editable SVG panel collections into plotter cut and score paths."""
import argparse
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from panelgraph import DEFAULT_NEAR, PanelGraph


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
    for warning in report["warnings"]:
        print("Warning:", warning)
    if "output" in report:
        print(f"Wrote {report['output']}: {report['cut_contours']} cut contour(s), "
              f"{report['internal_cuts']} internal cut(s), {len(report['folds'])} score line(s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "compile"):
        command = subparsers.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("--near-distance", type=float, default=DEFAULT_NEAR,
                             help=f"Report candidate seams separated by at most this many mm (default: {DEFAULT_NEAR:g})")
        command.add_argument("--snap-near", action="store_true",
                             help="Explicitly repair near-coincident straight seams using their midpoint")
        command.add_argument("--json", action="store_true")
        if name == "compile":
            command.add_argument("--output", "-o", type=Path, required=True)
            command.add_argument("--thickness", type=float,
                                 help="Material thickness metadata in mm; no compensation is applied yet")
            command.add_argument("--fold-offset", type=float, default=0,
                                 help="Score-axis offset from material midplane metadata in mm (default: 0)")
    args = parser.parse_args()
    try:
        if args.command == "compile" and args.output.resolve() == args.input.resolve():
            raise ValueError("Input and output paths must differ")
        if not math.isfinite(args.near_distance) or args.near_distance <= 0:
            raise ValueError("Near-edge distance must be finite and positive")
        graph = PanelGraph(args.input, args.near_distance, args.snap_near)
        report = graph.report() if args.command == "inspect" else graph.compile(
            args.output, thickness=args.thickness, fold_offset=args.fold_offset)
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
