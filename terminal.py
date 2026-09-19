"""Readable reports and a complete schematic of panels and their creases."""
import shutil
import textwrap

from inspection import mm


def markers(issues, field, value):
    ids = [str(i["id"]) for i in issues if value in i.get(field, [])]
    return " ".join("["+i+"]" for i in ids)


def layout_diagram(model, issues, *, width=88, ascii_only=False):
    panels = {p["id"]: p for p in model["panels"]}
    folds = {frozenset(f["panels"]): f for f in model["folds"]}
    main = {p["role"]: p for p in panels.values() if p["kind"] == "main"}
    ordered = sorted(main, key=lambda role: min(y for _, y in main[role]["flat_boundary"]))
    roots = {ordered[0]: 1, ordered[1]: 3 if model["layout"] == "connected" else 5}
    grid = {}
    for p in panels.values():
        points = p["flat_boundary"]
        cx = (min(x for x, _ in points)+max(x for x, _ in points))/2
        cy = (min(y for _, y in points)+max(y for _, y in points))/2
        ref = main[p["role"]]["flat_boundary"]
        x0, x1 = min(x for x, _ in ref), max(x for x, _ in ref)
        y0, y1 = min(y for _, y in ref), max(y for _, y in ref)
        row, col = roots[p["role"]], 1
        if p["kind"] == "connector":
            row = 2
        elif p["kind"] == "tab":
            row += -1 if cy < (y0+y1)/2 else 1
            col = 0 if cx < (x0+x1)/2 else 2
        elif p["kind"] == "wall":
            if cx < x0:
                col = 0
            elif cx > x1:
                col = 2
            else:
                row += -1 if cy < (y0+y1)/2 else 1
        if (row, col) in grid:
            return connection_diagram(model, issues, width=width, ascii_only=ascii_only)
        grid[row, col] = p
    if width < 66:
        return connection_diagram(model, issues, width=width, ascii_only=ascii_only)
    # Only use the grid when it represents every graph edge as a neighboring
    # border. Otherwise retain every panel and crease in the connection view.
    locations = {p["id"]: pos for pos, p in grid.items()}
    if any(sum(abs(a-b) for a, b in zip(locations[f["panels"][0]], locations[f["panels"][1]])) != 1
           for f in model["folds"]):
        return connection_diagram(model, issues, width=width, ascii_only=ascii_only)

    inside = min(26, (width-6)//3-2)
    cell = inside+2
    cut, score, vertical, vscore = ("=", "-", "|", ":") if ascii_only else ("─", "┄", "│", "┆")
    tl, tr, bl, br = ("+",)*4 if ascii_only else ("┌", "┐", "└", "┘")

    def crease(pos, offset):
        a, b = grid.get(pos), grid.get((pos[0]+offset[0], pos[1]+offset[1]))
        return folds.get(frozenset((a["id"], b["id"]))) if a and b else None

    def border(pos, top):
        f = crease(pos, (-1 if top else 1, 0))
        stroke = score if f else cut
        label = markers(issues, "creases", f["id"]) if f else ""
        return (tl if top else bl)+label.center(inside, stroke)+(tr if top else br)

    lines = ["Layout (SVG arrangement; not to scale)"]
    for row in range(max(r for r, _ in grid)+1):
        present = [(row, col) in grid for col in range(3)]
        if not any(present):
            lines.append("")
            continue
        texts = {}
        for col in range(3):
            if not present[col]:
                continue
            p = grid[row, col]
            text = p["name"]
            label = markers(issues, "panels", p["id"])
            if label:
                text += " "+label
            texts[col] = textwrap.wrap(text, inside, break_long_words=False, break_on_hyphens=False)
        height = max(len(t) for t in texts.values())
        lines.append("   ".join(border((row, col), True) if present[col] else " "*cell for col in range(3)).rstrip())
        for k in range(height):
            parts = []
            for col in range(3):
                if present[col]:
                    left = vscore if crease((row, col), (0, -1)) else vertical
                    right = vscore if crease((row, col), (0, 1)) else vertical
                    text = texts[col][k] if k < len(texts[col]) else ""
                    parts.append(left+text.center(inside)+right)
                else:
                    parts.append(" "*cell)
                if col < 2:
                    f = crease((row, col), (0, 1))
                    if f and k == height//2:
                        label = markers(issues, "creases", f["id"])
                        parts.append(label.center(3) if len(label) <= 3 and label else score*3)
                    else:
                        parts.append(" "*3)
            lines.append("".join(parts).rstrip())
        lines.append("   ".join(border((row, col), False) if present[col] else " "*cell for col in range(3)).rstrip())
    lines.append(f"{cut*3} cut boundary   {score*3}/{vscore} crease   [n] problem reference")
    lines.append("Front = opposite the back hinge; left/right viewed from the front.")
    return "\n".join(lines)


def connection_diagram(model, issues, *, width=60, ascii_only=False):
    """Narrow-terminal fallback: all panels and all creases, no truncated tabs."""
    names = {p["id"]: p["name"] for p in model["panels"]}
    lines = ["Layout connections (narrow view; every panel and crease)"]
    cut, score = ("=", "-") if ascii_only else ("─", "┄")
    corners = ("+", "+", "+", "+", "|") if ascii_only else ("┌", "┐", "└", "┘", "│")
    inside = max(12, width-4)
    for p in model["panels"]:
        title = p["name"]+" "+markers(issues, "panels", p["id"])
        texts = textwrap.wrap(title, inside, break_on_hyphens=False)
        lines.append(corners[0]+cut*inside+corners[1])
        lines.extend(corners[4]+t.ljust(inside)+corners[4] for t in texts)
        lines.append(corners[2]+cut*inside+corners[3])
        for f in model["folds"]:
            if p["id"] not in f["panels"]:
                continue
            other = next(i for i in f["panels"] if i != p["id"])
            text = f"{score} crease {markers(issues, 'creases', f['id'])} to {names[other]}"
            lines.extend(textwrap.wrap(text, max(16, width), subsequent_indent="  ", break_on_hyphens=False))
    lines.append("Boxes identify panels; dashed links are creases (listed at each end).")
    return "\n".join(lines)


def render_report(report, *, details=False, ascii_only=False, width=None):
    width = width or shutil.get_terminal_size((88, 24)).columns
    statuses = {"passes": "PASSES", "warning": "FITS WITH WARNINGS", "fails": "HAS PROBLEMS", "not_checked": "ASSEMBLY NOT CHECKED"}
    lines = [f"{report['layout'].upper()} BOX — {statuses[report['status']]}", ""]
    for role, p in report["panels"].items():
        height = f"{p['height']:.3f}".rstrip("0").rstrip(".")
        lines.append(f"  {role.title()}: {mm(p['width'])} × {mm(p['length'])} mm between folds; walls {height} mm")
    if "assembly" in report:
        model = report["assembly"]
        source = "inferred from folded tab contacts" if model["thickness_source"].startswith("inferred") else "specified"
        lines.append(f"  Material: {mm(model['thickness'])} mm ({source})")
    lines.append("")
    if report["issues"]:
        lines.append("Problems / warnings")
        for issue in report["issues"]:
            lines.append(f"  [{issue['id']}] {issue['title']}")
            lines.append("      "+issue["message"])
            if "suggestion" in issue:
                lines.append("      "+issue["suggestion"])
            # Contact/crease offsets are actionable in the concise view too.
            if details or issue["kind"] == "tab_contact":
                lines.extend("      "+s for s in issue.get("details", []))
        lines.append("")
    if "assembly" in report:
        model = report["assembly"]
        contacts = model["tab_contacts"]
        matching = sum(c["status"] == "contact" for c in contacts)
        lines.append(f"Checks: layout {report['checks']['layout']}; tab contacts {matching}/{len(contacts)} match; 3D assembly checked.")
        fit = model["lid_fit"]
        for label, value in (("Left/right", fit["minimum_clearance_per_side"]["x"]),
                             ("Front/back", fit["minimum_clearance_per_side"]["y"]),
                             ("Above base rim", fit["rim_to_ceiling"])):
            text = "not verified" if value is None else f"{mm(abs(value))} mm {'overlap' if value < -1e-6 else 'clearance'}"
            lines.append(f"  {label}: {text}")
        lines.extend(["", layout_diagram(model, report["issues"], width=width, ascii_only=ascii_only)])
        if details:
            lines.append("\nGeometry details")
            lines.append(f"  {len(model['panels'])} panels; {len(model['folds'])} inward 90° folds; {len(model['topology_adjustments'])} recorded anchor adjustments.")
            for c in model["interferences"]:
                lines.append(f"  {' / '.join(c['panel_names'])}: {mm(c['overlap_depth'])} mm along {c['axis']}; volume {c['volume']:.6f} mm³")
            lines.append("  Overlap distance is the intersection extent along the named direction, not a minimum repair movement.")
            lines.extend(f"  {p['id']}: {p['name']}" for p in model["panels"])
            lines.extend(f"  {c['id']}: {c['name']} (SVG score {c['score']})" for c in model["folds"])
        lines.append("\nModel: inward 90° folds; idealized hinges, no physical bend/closing-motion simulation.")
    else:
        lines.append("Assembly not checked: "+report.get("assembly_reason", "unavailable"))
        lines.append("A clean flat drawing does not establish that the box fits when folded.")
    if "repair" in report:
        r = report["repair"]
        lines.append("\nScore repair: "+("complete" if r["complete"] else "partial")+
                     f" ({len(r['changes'])} creases; {len(r['anchor_changes'])} cut-anchor corrections).")
        lines.extend("  Unresolved: "+s for s in r["unresolved"])
        if details:
            for c in r["changes"]:
                lines.append(f"  {c['score']}: endpoint movement {mm(c['max_movement'])} mm; {len(c['cut_changes'])} adjoining cut edits")
        lines.append("  Score repair completion refers to the drawing; assembled fit is reported above.")
    if "fit_repair" in report:
        r = report["fit_repair"]
        lines.append("\nFit repair: "+("complete" if r["complete"] else "partial / unresolved"))
        for c in r["changes"]:
            lines.append(f"  {c['name']}: {mm(c['before'])} → {mm(c['after'])} mm")
        lines.extend("  Unresolved: "+s for s in r["unresolved"])
    text = "\n".join(lines)
    if ascii_only:
        for old, new in (("—", "-"), ("↔", "<->"), ("→", "->"), ("×", "x"), ("°", " deg"), ("³", "^3")):
            text = text.replace(old, new)
    return text
