"""Compile editable SVG panel collections into plotter cut and score paths."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

from .panelgraph import DEFAULT_NEAR, PanelGraph
from .session import ResizeRequest

# Editable checkouts use repository sketches; wheels bundle the same presets.
PRESET_DIRECTORY = Path(__file__).parent / "presets"
if not PRESET_DIRECTORY.is_dir():
    PRESET_DIRECTORY = Path(__file__).resolve().parents[2] / "presets"


class InteractiveCancelled(Exception):
    pass


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


def validate_resize_paths(input_path, output, source_output=None):
    if paths_alias(output, input_path):
        raise ValueError("Input and output paths must differ")
    if source_output is not None and (paths_alias(source_output, input_path) or
                                      paths_alias(source_output, output)):
        raise ValueError("Input, compiled output, and source output paths must differ")
    if output.is_dir() or (source_output is not None and source_output.is_dir()):
        raise ValueError("Output paths must not be directories")


def replace_outputs(replacements):
    backups = {}
    installed = []
    try:
        for _, target in replacements:
            if target.exists():
                backup = temporary_output(target)
                backup.unlink()
                os.replace(target, backup)
                backups[target] = backup
        for temporary, target in replacements:
            os.replace(temporary, target)
            installed.append(target)
    except Exception:
        for target in installed:
            target.unlink(missing_ok=True)
        for target, backup in backups.items():
            os.replace(backup, target)
        raise
    finally:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def resize_and_compile(graph, input_path, output, source_output, resize_options, *,
                       thickness, fold_offset, lid_clearance, finger_cutout=None,
                       finger_cutout_diameter=None, finger_cutout_depth=None):
    validate_resize_paths(input_path, output, source_output)
    resized = graph.resized(**resize_options)
    compiled_temp = temporary_output(output)
    source_temp = None
    try:
        source_temp = temporary_output(source_output) if source_output is not None else None
        report = resized.compile(compiled_temp, thickness=thickness, fold_offset=fold_offset,
                                 lid_clearance=lid_clearance, finger_cutout=finger_cutout,
                                 finger_cutout_diameter=finger_cutout_diameter,
                                 finger_cutout_depth=finger_cutout_depth,
                                 output_format="dxf" if output.suffix.lower() == ".dxf" else "svg")
        if source_temp is not None:
            source_temp.write_bytes(resized.source_bytes)
        replacements = [(compiled_temp, output)]
        if source_temp is not None:
            replacements.append((source_temp, source_output))
        replace_outputs(replacements)
        report["output"] = str(output)
        return report
    finally:
        compiled_temp.unlink(missing_ok=True)
        if source_temp is not None:
            source_temp.unlink(missing_ok=True)


def load_presets(directory=PRESET_DIRECTORY):
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise ValueError(f"Preset directory does not exist: {directory}")
    sources = sorted((source for source in directory.iterdir()
                      if source.is_file() and source.suffix.lower() == ".svg"),
                     key=lambda source: (source.name.casefold(), source.name))
    return [{"name": source.stem.replace("-", " ").replace("_", " ").capitalize(),
             "source": source.resolve()} for source in sources]


def prompt_text(label, default=None, *, input_fn=input):
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        value = input_fn(f"{label}{suffix}: ").strip()
    except EOFError as exc:
        raise InteractiveCancelled from exc
    if value.lower() in ("q", "quit"):
        raise InteractiveCancelled
    return str(default) if not value and default is not None else value


def prompt_float(label, default, *, minimum=None, input_fn=input, print_fn=print):
    while True:
        raw = prompt_text(label, f"{default:g}", input_fn=input_fn)
        try:
            value = float(raw)
        except ValueError:
            print_fn("Enter a number, or q to cancel.")
            continue
        if not math.isfinite(value) or (minimum is not None and value < minimum):
            requirement = f"a number of at least {minimum:g}" if minimum is not None else "a finite number"
            print_fn(f"Enter {requirement}.")
            continue
        return value


def prompt_yes_no(label, default=True, *, input_fn=input, print_fn=print):
    suffix = "Y/n" if default else "y/N"
    while True:
        value = prompt_text(f"{label} [{suffix}]", input_fn=input_fn).lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print_fn("Enter y or n, or q to cancel.")


def choose_interactive_source(input_path, directory, *, input_fn=input, print_fn=print, ui=None):
    if input_path is not None:
        return Path(input_path), {}
    presets = load_presets(directory)
    if not ui:
        print_fn("Available presets:")
        for index, preset in enumerate(presets, 1):
            print_fn(f"  {index}. {preset['name']}")
        print_fn("Enter a preset number or a path to another SVG.")
    while True:
        selection = prompt_text("Source", input_fn=input_fn)
        if selection.isdigit() and 1 <= int(selection) <= len(presets):
            preset = presets[int(selection)-1]
            return preset["source"], preset
        source = Path(selection).expanduser()
        if source.is_file():
            return source, {}
        print_fn(f"SVG not found: {source}")


def interactive_session(input_path=None, directory=PRESET_DIRECTORY, *, input_fn=input, print_fn=print, ui=None):
    print_fn("Boxxer interactive resize")
    source, preset = choose_interactive_source(input_path, directory, input_fn=input_fn, print_fn=print_fn, ui=ui)
    graph = PanelGraph(source)
    if graph.near_seams and not graph.snap_near:
        print_fn(f"Automatically repairing {len(graph.near_seams)} near-coincident seam pair(s).")
        graph = PanelGraph(source, graph.near, True)
    resize_model = graph._resize_structure()
    if resize_model["status"] != "supported":
        raise ValueError("Cannot resize this panel graph: "+resize_model["reason"])
    dimensions = resize_model["dimensions"]
    print_fn(f"Detected {resize_model['kind']} box net.")
    print_fn("Current dimensions: "+", ".join(
        f"{key.replace('_', ' ')} {value:.1f} mm" for key, value in dimensions.items()))

    dimensions = {key: round(value, 1) for key, value in dimensions.items()}
    resize_options = {
        "width": prompt_float("Width (mm)", dimensions["width"], minimum=1.0000001,
                              input_fn=input_fn, print_fn=print_fn),
        "length": prompt_float("Length (mm)", dimensions["length"], minimum=1.0000001,
                               input_fn=input_fn, print_fn=print_fn),
    }
    if resize_model["kind"] == "single":
        resize_options["height"] = prompt_float(
            "Height (mm)", dimensions["height"], minimum=1.0000001,
            input_fn=input_fn, print_fn=print_fn)
    else:
        resize_options["base_height"] = prompt_float(
            "Base height (mm)", dimensions["base_height"], minimum=1.0000001,
            input_fn=input_fn, print_fn=print_fn)
        resize_options["lid_height"] = prompt_float(
            "Lid height (mm)", dimensions["lid_height"], minimum=1.0000001,
            input_fn=input_fn, print_fn=print_fn)
        if resize_model["kind"] == "connected":
            resize_options["connector_depth"] = prompt_float(
                "Connector depth (mm)", dimensions["connector_depth"], minimum=1.0000001,
                input_fn=input_fn, print_fn=print_fn)

    stored = graph.compiled_settings
    thickness_default = stored.get("thickness")
    if thickness_default is None:
        thickness_default = preset.get("thickness")
    thickness_default = 0.5 if thickness_default is None else thickness_default
    fold_default = stored.get("fold_offset")
    if fold_default is None:
        fold_default = preset.get("fold_offset")
    fold_default = 0.0 if fold_default is None else fold_default
    clearance_default = stored.get("lid_clearance")
    if clearance_default is None:
        clearance_default = preset.get("lid_clearance")
    clearance_default = 0.0 if clearance_default is None else clearance_default
    thickness = prompt_float("Material thickness (mm)", thickness_default, minimum=0,
                             input_fn=input_fn, print_fn=print_fn)
    fold_offset = prompt_float("Fold offset (mm)", fold_default,
                               input_fn=input_fn, print_fn=print_fn)
    lid_clearance = 0.0
    if resize_model["kind"] != "single":
        lid_clearance = prompt_float("Lid clearance (mm)", clearance_default, minimum=0,
                                      input_fn=input_fn, print_fn=print_fn)

    cutout_defaults = graph.finger_cutout_settings
    while True:
        finger_cutout = prompt_text("Finger cutout (none/one/opposing)", cutout_defaults["mode"], input_fn=input_fn)
        if finger_cutout in ("none", "one", "opposing"):
            break
        print_fn("Choose none, one, or opposing.")
    cutout_diameter, cutout_depth = cutout_defaults["diameter"], cutout_defaults["depth"]

    stem = Path(graph.source_name).stem
    output = Path(prompt_text("Compiled output", f"{stem}-compiled.svg", input_fn=input_fn)).expanduser()
    source_value = prompt_text("Editable source output (blank for none)", input_fn=input_fn)
    source_output = Path(source_value).expanduser() if source_value else None
    validate_resize_paths(source, output, source_output)
    existing = [path for path in (output, source_output) if path is not None and path.exists()]
    if existing and not prompt_yes_no(
            "Overwrite "+", ".join(str(path) for path in existing), default=False,
            input_fn=input_fn, print_fn=print_fn):
        raise InteractiveCancelled

    request = ResizeRequest(source, output, source_output, resize_options, thickness, fold_offset,
                            lid_clearance, finger_cutout, cutout_diameter, cutout_depth, graph.near, graph.snap_near)
    if ui:
        ui.summary(request)
    else:
        print_fn("\nResize summary:")
        for key, value in request.summary():
            print_fn(f"  {key}: {value}")
    if not prompt_yes_no("Create these files", input_fn=input_fn, print_fn=print_fn):
        raise InteractiveCancelled
    report = request.execute(graph)
    report["equivalent_command"] = request.command()
    if ui:
        ui.results(report)
    else:
        print_report(report)
    if ui:
        ui.command(report["equivalent_command"])
    else:
        print_fn("\nEquivalent command:")
        print_fn(report["equivalent_command"])
    return report


def terminal_session(input_path, directory):
    from .tui import Navigate, TerminalWizard
    ui = TerminalWizard(load_presets(directory) if input_path is None else [])
    while True:
        try:
            return interactive_session(input_path, directory, input_fn=ui.input, print_fn=ui.print, ui=ui)
        except Navigate as navigation:
            ui.navigate(navigation.target)
        except (ValueError, OSError, ET.ParseError) as error:
            if not ui.retry(error):
                raise InteractiveCancelled from error


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
        print(f"  {fold['id']}: {fold['panels'][0]} ↔ {fold['panels'][1]}, {fold['length']:.3f} mm, "
              f"{fold['angle']:+d}°, {fold['score_side']}-side score, {fold['placement']}{repaired}")
    for edge in report["near_edges"]:
        status = "Repaired near edges" if any(f["repaired"] and f["panels"] == edge["panels"] for f in report["folds"]) else "Warning: near edges"
        print(f"{status} {edge['panels'][0]} / {edge['panels'][1]}: "
              f"{edge['length']:.3f} mm overlap at {edge['distance']:.6f} mm separation")
    for overlap in report["overlaps"]:
        print(f"Error: panels {overlap['panels'][0]} / {overlap['panels'][1]} overlap by {overlap['area']:.6f} mm²")
    if report.get("editable_corners"):
        print(f"Editable Inkscape corners: {len(report['editable_corners'])}")
        for corner in report["editable_corners"]:
            print(f"  {corner['shape']} vertex {corner['vertex']}: {corner['radius']:.3f} mm")
    if report.get("contacts"):
        print(f"Inferred glue-tab contacts: {len(report['contacts'])}")
        for contact in report["contacts"]:
            print(f"  {contact['tab']} → {contact['receiver']} via {contact['fold']} ({contact['placement']})")
    structure = report.get("structure", {})
    if structure.get("status") == "inferred":
        if structure.get("layout") == "separate":
            print(f"Separate enclosure: upper lid {structure['top']}; lower base {structure['bottom']}")
        else:
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
    if report.get("mirror_axes"):
        print(f"Expanded mirror axes: {report['mirror_axes']}")
    for symmetry in report.get("rotational_symmetry", []):
        print(f"Expanded rotational symmetry: {symmetry['order']} copies")
    for cutout in report.get("finger_cutouts", []):
        if cutout["status"] == "applied":
            cropped = "; cropped for 3 mm clearance" if cutout["cropped"] else ""
            print(f"Finger cutout {cutout['panel']}: {cutout['mouth_width']:.3f} mm wide, "
                  f"{cutout['depth']:.3f} mm deep{cropped}")
        else:
            print(f"Finger cutout {cutout['panel']} omitted: {cutout['reason']}")
    for row in report.get("fold_stack", []):
        if row["distance"] > 1e-8:
            print(f"Fold stack {row['fold']}: shift score by "
                  f"({row['shift'][0]:.3f}, {row['shift'][1]:.3f}) mm ({row['score_side']} side)")
    for row in report.get("flap_shortening", []):
        print(f"Shortened flap {row['tab']} on {row['receiver']}: "
              f"{row['original_depth']:.3f} → {row['depth']:.3f} mm to avoid overlap")
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
            command.add_argument("--output", "-o", type=Path, required=True,
                                 help="Compiled SVG or DXF output (.dxf selects coloured millimetre DXF)")
            command.add_argument("--finger-cutout", nargs="?", const="one", choices=("none", "one", "opposing"),
                                 help="Add a finger opening on the top free flap, or on two opposing flaps")
            command.add_argument("--finger-cutout-diameter", type=float,
                                 help="Finger-circle diameter in mm (default 30)")
            command.add_argument("--finger-cutout-depth", type=float,
                                 help="Penetration into the free edge in mm (default 10; leaves 3 mm material)")
        if name == "resize":
            command.add_argument("--width", type=float, help="Nominal base/principal fold-to-fold width in mm")
            command.add_argument("--length", type=float, help="Nominal base/principal fold-to-fold length in mm")
            command.add_argument("--height", type=float, help="Nominal wall height in mm")
            command.add_argument("--base-height", type=float, help="Connected base wall height in mm")
            command.add_argument("--lid-height", type=float, help="Connected lid wall height in mm")
            command.add_argument("--connector-depth", type=float, help="Connected hinge-panel depth in mm")
            command.add_argument("--source-output", type=Path,
                                 help="Also write the editable resized panel-source SVG")
    interactive = subparsers.add_parser("interactive", help="Guided resize using a preset or SVG path")
    interactive.add_argument("input", nargs="?", type=Path,
                             help="SVG to resize; omit to choose a preset or enter a path")
    interactive.add_argument("--presets", type=Path, default=PRESET_DIRECTORY,
                             help="Directory of SVG sketches to list as presets")
    interactive.add_argument("--plain", action="store_true", help="Use plain text prompts instead of keyboard menus")
    args = parser.parse_args()
    try:
        if args.command == "interactive":
            report = (terminal_session(args.input, args.presets)
                      if not args.plain and sys.stdin.isatty() and sys.stdout.isatty()
                      else interactive_session(args.input, args.presets))
            return 1 if report["warnings"] else 0
        if args.command == "compile" and paths_alias(args.output, args.input):
            raise ValueError("Input and output paths must differ")
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
                request = ResizeRequest(
                    args.input, args.output, args.source_output,
                    {"width": args.width, "length": args.length, "height": args.height,
                     "base_height": args.base_height, "lid_height": args.lid_height,
                     "connector_depth": args.connector_depth},
                    thickness=thickness, fold_offset=fold_offset, lid_clearance=lid_clearance,
                    finger_cutout=args.finger_cutout, finger_cutout_diameter=args.finger_cutout_diameter,
                    finger_cutout_depth=args.finger_cutout_depth, near=graph.near, snap_near=graph.snap_near)
                report = request.execute(graph)
            else:
                report = graph.compile(args.output, thickness=thickness, fold_offset=fold_offset,
                                       lid_clearance=lid_clearance, finger_cutout=args.finger_cutout,
                                       finger_cutout_diameter=args.finger_cutout_diameter,
                                       finger_cutout_depth=args.finger_cutout_depth)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_report(report)
        return 1 if report["warnings"] else 0
    except InteractiveCancelled:
        print("Cancelled.")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (ValueError, OSError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
