"""Shared inspection policy and explainable fit repairs, independent of output."""
import math
import re

EPS = 1e-6


def mm(value):
    """Human precision without negative zero or hiding small real errors."""
    if abs(value) <= EPS:
        return "0"
    if abs(value) < 0.001:
        return f"{value:.6f}".rstrip("0")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def score_names(design):
    names = {}
    try:
        contacts = design.tab_contacts()
    except ValueError:
        return names
    for contact in contacts:
        panel, shape = contact["panel"], contact["shape"]
        side = "left" if sum(x for x, _ in shape.points)/2 < panel.bounds[0] else "right"
        front = contact["edge"] == ("top" if panel.role == "lid" else "bottom")
        end = "front" if front else "back"
        names[shape.id] = f"{panel.role.title()} {side}-{end} glue tab crease"
    return names


def explain_source(message, names):
    for key in sorted(names, key=len, reverse=True):
        message = re.sub(r"(?<![\w-])"+re.escape(key)+r"(?![\w-])", names[key], message)
    return message


def assembly_issues(model):
    """Group related intersections into problems, retaining per-pair evidence."""
    panels = {p["id"]: p for p in model["panels"]}
    issues, covered = [], set()
    collisions = model["interferences"]

    def issue(kind, title, message, members, *, creases=(), details=(), suggestion=None, severity="error", **extra):
        row = {"kind": kind, "severity": severity, "title": title, "message": message,
               "panels": sorted(set(members)), "creases": list(creases), "details": list(details), **extra}
        if suggestion:
            row["suggestion"] = suggestion
        issues.append(row)

    for contact in model["tab_contacts"]:
        if contact["status"] == "contact":
            continue
        crease = next(f for f in model["folds"] if f["id"] == contact["crease"])
        pair = frozenset((contact["tab"], contact["wall"]))
        covered.add(pair)
        gap = contact["gap"]
        relation = "overlaps" if gap < 0 else "is separated from"
        issue("tab_contact", contact["crease_name"],
              f"{contact['tab_name']} {relation} {contact['wall_name']} by {mm(abs(gap))} mm.",
              [*crease["panels"], contact["wall"]], creases=[crease["id"]],
              details=[f"Crease offset: {mm(contact['separation'])} mm; required for contact: {mm(model['thickness'])} mm."],
              suggestion="Run fix --align-reliefs to align this crease and its adjoining cut shoulder.",
              distance_mm=abs(gap))

    lid_top = next(p["id"] for p in panels.values() if p["role"] == "lid" and p["kind"] == "main")
    rim_gap = model["lid_fit"]["rim_to_ceiling"]
    if rim_gap < -EPS:
        affected = [c for c in collisions if lid_top in c["panels"] and c["cross_part"]]
        for c in affected:
            covered.add(frozenset(c["panels"]))
        members = [lid_top]+[p for c in affected for p in c["panels"]]
        suggestion = (f"Increase the shared back depth from {mm(model['connector_depth'])} to at least "
                      f"{mm(model['connector_depth']-rim_gap)} mm; fix --fit can do this."
                      if model["layout"] == "connected" else "Raise the seated lid or revise its closure geometry.")
        issue("lid_ceiling", "Lid top is too low", f"The base rim extends {mm(-rim_gap)} mm into the lid ceiling.",
              members, details=[collision_text(c) for c in affected], suggestion=suggestion,
              distance_mm=-rim_gap, direction="vertical")

    for axis in ("x", "y"):
        affected = [g for g in model["lid_fit"]["local_clearances"] if g["axis"] == axis and g["gap"] < -EPS]
        if not affected:
            continue
        worst = min(affected, key=lambda g: g["gap"])
        sides = {g["side"] for g in affected}
        if len(sides) == 2:
            location = "Left/right" if axis == "x" else "Front/back"
        else:
            location = ({"positive": "Right", "negative": "Left"} if axis == "x" else
                        {"positive": "Front", "negative": "Back"})[worst["side"]]
        for g in affected:
            covered.add(frozenset(g["panels"]))
        base, lid = (panels[i]["name"] for i in worst["panels"])
        physical = next((c for c in collisions if set(c["panels"]) == set(worst["panels"])), None)
        message = (collision_text(physical) if physical else
                   f"{lid} extends {mm(-worst['gap'])} mm inside the required boundary of {base}.")
        issue("lid_side", f"{location}-side fit", message,
              [p for g in affected for p in g["panels"]],
              details=[collision_text(c) for c in collisions if any(set(c["panels"]) == set(g["panels"]) for g in affected)],
              suggestion="Adjust the lid footprint or registration in the SVG; automatic lateral fit repair is not supported.",
              distance_mm=-worst["gap"], direction="left/right" if axis == "x" else "front/back")

    for c in collisions:
        if frozenset(c["panels"]) not in covered:
            issue("intersection", "Panel overlap", collision_text(c), c["panels"],
                  distance_mm=c["overlap_depth"], direction=c["axis"],
                  suggestion="Revise the named panels or their crease positions; this collision has no automatic repair rule.")
    missing = [axis.upper() for axis, gap in model["lid_fit"]["minimum_clearance_per_side"].items() if gap is None]
    if missing:
        issue("unverified_fit", "Nested fit cannot be verified", "No overlapping lid/base walls along " + "/".join(missing) + ".", [])
    touching = [g for g in model["lid_fit"]["local_clearances"] if abs(g["gap"]) <= EPS]
    if touching:
        issue("zero_clearance", "No sliding clearance", "Some lid/base surfaces touch exactly; there is no positive sliding allowance.",
              [p for g in touching for p in g["panels"]], severity="warning")
    return issues


def collision_text(collision):
    a, b = collision["panel_names"]
    direction = {"x": "left/right", "y": "front/back", "z": "vertically"}[collision["axis"]]
    return f"{a} overlaps {b} by {mm(collision['overlap_depth'])} mm {direction}."


def inspect_design(design, thickness=None, *, flat=False, scores=False, lid_lift=0):
    report = design.report()
    names = score_names(design)
    issues = [{"kind": "layout", "severity": "error", "title": "Cut/score layout",
               "message": explain_source(w, names), "panels": [], "creases": [], "details": []}
              for w in report["warnings"]]
    report["checks"] = {"layout": "issues" if issues else "passes", "assembly": "not_checked"}
    if scores:
        try:
            report["scores"] = design.score_analysis(thickness)
        except ValueError as exc:
            report["score_error"] = str(exc)
    if flat:
        report["assembly_reason"] = "Flat-only inspection requested (--flat); assembled fit has not been checked."
        if scores:
            messages = report.get("scores", {}).get("issues", [])
            if "score_error" in report:
                messages = messages+[report["score_error"]]
            issues.extend({"kind": "score", "severity": "error", "title": "Crease check",
                           "message": explain_source(m, names), "panels": [], "creases": [], "details": []}
                          for m in messages)
    else:
        from folded import Assembly
        try:
            assembly = Assembly(design, thickness, lid_lift)
            report["assembly"] = assembly.report()
        except ValueError as exc:
            report["assembly_reason"] = explain_source(str(exc), names)
            issues.append({"kind": "assembly_blocked", "severity": "error", "title": "Assembly not checked",
                           "message": report["assembly_reason"], "panels": [], "creases": [], "details": [],
                           "suggestion": "Repair the cut/score layout with fix --align-reliefs, then inspect again. Supply --thickness if inference is unavailable."})
        else:
            report["checks"]["assembly"] = "checked"
            issues.extend(assembly_issues(report["assembly"]))
            report["fit"] = "Idealized inward-90° assembled-state checks"
    for index, issue in enumerate(issues, 1):
        issue["id"] = index
    report["issues"] = issues
    report["warnings"] = [i["message"] for i in issues]
    report["status"] = ("not_checked" if report["checks"]["assembly"] == "not_checked" else
                        "fails" if any(i["severity"] == "error" for i in issues) else
                        "warning" if issues else "passes")
    return report


def repair_fit(design, thickness, top_clearance=0):
    """One bounded rule: increase a connected back to clear the actual rim.

    Never move a separate lid in 3D and claim the source SVG has been repaired.
    Side clearance/registration and unsupported contacts remain explicit issues.
    """
    if not math.isfinite(top_clearance) or top_clearance < 0:
        raise ValueError("Top clearance must be finite and nonnegative")
    before = inspect_design(design, thickness)
    result = {"changes": [], "target_top_clearance": top_clearance, "complete": False}
    if "assembly" not in before:
        result["unresolved"] = [before["assembly_reason"]]
        return result
    if top_clearance and not design.connected:
        raise ValueError("--top-clearance requires a connected box; separate lid placement is not an SVG fit repair")
    gap = before["assembly"]["lid_fit"]["rim_to_ceiling"]
    if design.connected and gap < top_clearance-EPS:
        old = design.connector_depth
        target = round(old+top_clearance-gap, 8)
        design.resize(connector_depth=target)
        result["changes"].append({"parameter": "connector_depth", "name": "Shared back / hinge panel depth",
                                  "before": old, "after": target,
                                  "reason": "Place the lid ceiling above the highest base rim"})
    after = inspect_design(design, thickness)
    result["unresolved"] = [i["message"] for i in after["issues"]]
    result["complete"] = after["status"] == "passes"
    result["after_status"] = after["status"]
    return result
