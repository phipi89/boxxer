# Boxxer — first working prototype

Inspect, repair, resize, and virtually assemble cardboard box layouts, using millimeters.
Black is cut; red is score. The larger rectangular score loop is interpreted as
the lid and the smaller as the base. Dimensions are fold-to-fold, not guaranteed
internal dimensions. `connected.svg` has been corrected: its lower front flap
is now 15 mm deep, rather than 15.5 mm.

## Setup

```sh
uv sync
```

## Inspect

```sh
uv run python boxxer.py inspect examples/connected-fixed.svg
uv run python boxxer.py inspect examples/connected-fixed.svg --details
uv run python boxxer.py inspect examples/connected-fixed.svg --json
uv run python boxxer.py inspect connected.svg --flat --scores
uv run python boxxer.py inspect connected.svg --preview inspection.svg
```

**Full 3D inspection is now the default for `inspect` and `check`.** It checks
the flat drawing, builds the same folded model used by STL export, and checks
contacts, material overlaps, and lid fit. `--folded` remains a compatibility alias
with identical behavior. `--flat` explicitly skips the assembly and prints
**ASSEMBLY NOT CHECKED**. A clean flat drawing is not a successful box-fit check.

If bad scores prevent building the assembly, the report shows the layout problems
and says why the assembly could not be checked. It does not silently fall back to
a passing flat-only result. Unrepaired original examples need `fix` first.

### Names, distances, and the terminal layout

Reports use names such as **Lid top**, **Base front wall**, and **Base left-front
glue tab**, with named creases between them. The large folding flaps are called
*walls*; small overlapping flaps are called *glue tabs*. Front is +Y in the
assembled box, opposite the connected back hinge; right is +X. Separate trays
use the same assembled orientation, including the turned-over lid.

Related intersections are grouped into numbered problems. For example, the
connected design's seven rim/ceiling intersections become **Lid top is too low**,
with a 0.2503 mm vertical overlap. Its small side-position error is reported as
**Lid right wall overlaps Base right wall by 0.00009 mm**. `--details` lists the
individual panel pairs, source IDs, all crease names, volumes, and anchor edits.

The default box-drawing schematic includes **every wall, glue tab, and crease**.
Solid borders represent cut boundaries and dashed borders/links represent
creases. `[1]`, `[2]`, etc. locate the corresponding problems on affected panels;
crease-related problems are marked on the crease itself too. The arrangement
comes from the SVG and fold graph. It is a schematic, not a scale drawing of
reliefs or curved cuts. On narrow terminals it switches to a complete panel and
crease connection view, rather than dropping tabs. `--ascii` uses plain ASCII.
When the graph cannot be extracted, the report explains the blocker instead of
drawing guessed panels.

Overlap distances are measured along a named direction from the **actual 3D
intersection**, not by dividing its volume by an arbitrary area. They are not
general minimum separating translations. Lid nesting constraints retain their
sign even if a misplaced wall has passed entirely through its counterpart.
Very small real errors stay visible; floating-point negative zero displays as 0.

The preview includes blue measurement labels and is for inspection, not plotting.
Reports show the inferred roles, dimensions, height linkage, lid/base differences,
and geometric warnings. The source files contain some score overruns of about
0.05 mm; these are reported, not silently corrected. The original layout detector
uses a 0.01 mm recognition tolerance. The additional score/contact checks below
use only a 0.000001 mm numerical epsilon, not a manufacturing gap allowance.

## Diagnose and fix scores

```sh
uv run python boxxer.py inspect connected.svg --flat --scores
uv run python boxxer.py check connected.svg --flat --scores --thickness 0.5
uv run python boxxer.py fix connected.svg --output fixed.svg --report fixes.json
```

For the supported tray construction, inward 90° folds put each tab's middle
surface parallel to its receiving end wall. Their separation is the tab-score
offset from the main panel's end score. At perfect contact, that separation equals
one sheet thickness. Positive `contact_gap` means a gap; negative means material
interference under this idealized contact model.

Thickness is estimated from the median of at least three such contacts, rounded
to 0.001 mm. A strict majority must support that estimate within 0.001 mm;
otherwise specify `--thickness`. This agreement test only chooses a thickness:
**all individual residuals above the numerical epsilon are still reported and
repaired**, including small drafting errors. The lid/base dimension difference
is not used to infer thickness. Both source examples infer **0.500 mm**.

`fix` moves tab scores to their exact thickness-based positions and snaps their
endpoints to the actual cut boundary along that fold line. It records before/after
coordinates and moves at most 0.25 mm by default. `--max-move` limits how much a
repair may change; it does not permit that much error in the repaired result.

Some inconsistencies also need a cut adjustment. `--align-reliefs` explicitly
allows moving the short adjoining cut shoulder to meet the score, including
collapsing intermediate collinear vertices swept by that movement. These cut
vertex changes are recorded individually. This option also aligns cut vertices
within 0.001 mm of main-score anchors, recording those edits in `anchor_changes`.
The exposed tab ends are not translated with a shoulder, so the overlap length can
change by the shoulder's displacement. The main-panel score rectangles stay fixed.

The connected source has about **0.5 mm relief misalignment**, including two
scores ending inside the material. These commands produce fully repaired examples
for the implemented score/contact checks:

```sh
uv run python boxxer.py fix connected.svg --align-reliefs --max-move 0.6 \
  --output examples/connected-fixed.svg --report examples/connected-fixes.json
uv run python boxxer.py fix drafts1.svg --align-reliefs \
  --output examples/separate-fixed.svg --report examples/separate-fixes.json
uv run python boxxer.py check examples/connected-fixed.svg --flat --scores
```

Repairs are performed per score. Unambiguous repairs are saved even if others
cannot be resolved; unresolved cases are listed, and the command exits **1**.
`fix` always reruns the full inspection after repairing scores. Exit **0** means
the repaired drawing and assembly have no reported issues; **1** means a partial
repair, remaining fit problem, blocked assembly check, or warning (including zero
sliding clearance). **2** means an input/operation error. `repair.complete` in JSON
refers only to score/relief repair; the top-level `status` describes overall
inspection. Thus a score repair can complete while the box still fails to fit.
Repeating a successful score repair makes
no further changes, including after SVG export/reimport. Input and output paths
must differ. `--json` prints the full result, and `--report` saves it as JSON.

`--flat --scores` is a quick analytical check of the known side-tab contacts.
Ordinary `inspect` uses the actual folded panel graph. Score repair assumes the standard inward-fold tray
arrangement and does not optimize overall lid fit.
Repair currently requires straight-segment input; fix before adding a notch.

### Explicit fit repair

Dimension-changing repairs require **`fix --fit`**:

```sh
uv run python boxxer.py fix examples/connected-fixed.svg \
  --align-reliefs --fit --top-clearance 0.2 \
  --output fitted.svg --report fitted.json
uv run python boxxer.py inspect fitted.svg
```

The first supported rule increases a connected box's back-panel depth so the lid
ceiling clears the highest base rim. `--top-clearance` sets the minimum desired
gap in millimeters; the default is 0 (contact without penetration). It does not
shrink an already deeper back panel. The change is logged in `fit_repair.changes`,
then the full assembly is rebuilt and checked. Repeating it makes no further
dimension changes once the target is reached.

For the supplied connected example, 0.2 mm top clearance produces a back depth
of approximately **15.4503 mm**. The tiny horizontal misregistration remains an
explicit problem, so this is reported as a partial fit repair with exit status 1.
Automatic footprint enlargement, lateral recentering, and arbitrary collision
repairs are not yet supported. A separate lid's 3D placement is not misrepresented
as an SVG repair; `--top-clearance` requires a connected box. `--max-move` limits
score/relief edits, not the explicitly requested back-depth repair.

## Resize

```sh
uv run python boxxer.py resize connected.svg --height 25 --output resized.svg
uv run python boxxer.py resize drafts1.svg --base-height 25 --lid-height 10 --output shallow-lid.svg
uv run python boxxer.py resize connected.svg --width 80 --length 50 --height 25 --output larger.svg
```

- `--height` changes the base and also the lid when their original nominal
  heights match within 0.01 mm. Otherwise the lid stays at its current height.
- `--base-height` and `--lid-height` override those choices independently.
- Width and length refer to the base. The measured lid/base dimensional
  differences are retained; there is no hardcoded 1 × 2 mm clearance.
- For connected layouts, connector depth changes by the same amount as base
  height, preserving an existing connector allowance. The measured taper is
  retained. `--connector-depth 15.5` sets the back panel depth independently;
  this lets the lid ceiling clear the base rim without increasing side-wall height.
- Separate pieces retain their original vertical layout gap.
- Side-wall depth is inferred from the cut outline; relief offsets are measured
  from vertices. Side tabs retain their overlap length as walls grow.
- The output is a clean manufacturing SVG, preserving red/black operations.
  Original editor metadata and styling are not retained. Sources cannot be
  overwritten through the CLI.

## Rounded finger notch

```sh
uv run python boxxer.py resize connected.svg --finger-notch --output notched.svg
uv run python boxxer.py resize connected.svg --height 25 --finger-notch --output tall-notched.svg
```

Defaults: **25 mm width, 15 mm requested depth**, with straight sides and a
semicircular bottom. `--notch-width` and `--notch-depth` override those dimensions.
The notch is centered on the uppermost main flap's exposed horizontal edge, after
resizing. Subtraction is checked with a polygon boolean and the exported boundary
uses an exact SVG circular arc.

Depth is capped to leave **at least 2 mm before the fold**. A 15 mm flap therefore
gets a 13 mm-deep notch; a 25 mm flap gets the requested 15 mm. Actual dimensions
are reported. A 25 mm-wide semicircular bottom needs at least 12.5 mm depth: if
that cannot fit with the fold clearance, the operation fails rather than narrowing
the notch or changing its shape.

## Checks and tests

Ready-to-open trial files are in `examples/`:

- `connected-15-notch.svg`: original height, notch capped to 13 mm.
- `connected-25-notch.svg`: both heights 25 mm, notch depth 15 mm.
- `separate-25-notch.svg`: separate lid/base at 25 mm with a lid notch.
- `connected-fixed.svg`, `separate-fixed.svg`: repaired source layouts, with
  matching `*-fixes.json` change reports. These are the clean starting points
  for further resizing; the earlier notched trial files retain the original scores.

```sh
uv run python boxxer.py check resized.svg
uv run python -m unittest -v
```

`check` exits 0 for no reported issues, 1 for problems/warnings or a blocked
assembly check, and 2 for an invalid input/operation. It checks outline validity, score containment, page
bounds, separate-piece overlap, and outer-flap/nominal-height disagreement.
The 3D checks below run by default. `--flat --scores` restricts checking to the
flat drawing and quick tab-contact/endpoint checks. A zero sliding clearance is reported as a warning,
even when the mathematical solids do not intersect.

## Folded 3D models and STL

```sh
# Inspect or check an existing repaired design without writing meshes.
uv run python boxxer.py inspect examples/3d/separate-source.svg
uv run python boxxer.py check examples/3d/connected-source.svg

# Export the complete model, individual part groups, and a JSON report.
uv run python boxxer.py assemble examples/3d/separate-source.svg \
  --output assembly.stl --base-output base.stl --lid-output lid.stl

# Explicit material thickness overrides inference; "auto" is also accepted.
uv run python boxxer.py inspect examples/3d/separate-source.svg --thickness 0.5
```

`assemble` automatically writes a report beside the STL (`assembly.json` here).
`--report path.json` chooses another location. Successful export exits 0 even if
the report identifies interference: the mesh is useful for inspecting the error.
Use `check` for a pass/warning exit status. The original SVG is preserved.

### Model and checks

1. Polygonize the cut material and score network into **actual panels** and a
   fold-adjacency graph. The separate example produces 18 panels and 16 folds;
   the connected example produces 15 panels and 14 folds. Dangling scores,
   ambiguous attachments, and cyclic fold graphs are rejected.
2. Propagate rigid transforms through the graph, using **inward 90° folds**.
   Panel middle surfaces pass through the score axes. Shared hinge endpoints
   must remain coincident after folding.
3. Discover tab/wall contacts from parallel folded planes, overlapping projected
   areas, inward placement, and panel roles. Infer thickness from their plane
   separation independently of the lid/base dimension difference. If contacts
   are insufficient or inconsistent, specify `--thickness`.
4. Give every panel material thickness **±t/2 about its middle surface**. At
   hinges, overlapping slab ends are partitioned at the bisector, representing
   an idealized miter. This avoids false hinge collisions while retaining the
   material union. Other intersections remain errors.
5. Measure signed contact gaps, local single-/double-layer wall envelopes,
   actual volumetric intersections, lid/base side clearance where surfaces
   overlap, and base-rim versus lid-ceiling clearance. The connecting back
   panel's hinge edge is not treated as a free rim.

The JSON report includes numbered structured `issues`, explicit `checks` and
`status`, semantic panel/crease names alongside their IDs, roles, flat polygons, 3D transforms, fold
angles, per-panel bounds/volumes, local wall stacks, tab contacts, parallel
surface gaps, directional overlap distances, collision volumes, and lid-fit results. `part_envelopes` includes
outside extents and limiting opposing inner-plane spans; those spans can vary
locally because tabs cover only parts of a wall. `minimum_clearance_per_side`
is the tightest **local gap**, not the sum of both sides. Negative means overlap.

For separate trays, the base middle plane is Z=0, the parts are centered in XY,
and the lid's inner ceiling is placed at the highest base rim. `--lid-lift 0.3`
raises a separate lid by 0.3 mm. It is available on `assemble`, `inspect`, and
`check`, but not with `--flat`.
For connected designs, the hinge graph fixes the lid pose; independent lid lift
is rejected. A lid lifted entirely out of wall overlap is not declared a verified
nested fit. Lateral placement is not optimized automatically.

### What the examples reveal

- **Separate trays:** inferred thickness 0.500 mm, eight matching tab contacts,
  no material intersections, and approximately **zero minimum X/Y clearance**.
  This is an ideal touching fit, not a positive sliding allowance.
- **Connected design:** inferred thickness 0.500 mm and six matching tab contacts,
  but the lid ceiling is approximately **0.25 mm below the base rim**. The 3D
  checker detects the resulting collisions. Front clearance is approximately
  1 mm; the drawing also has a 0.00009 mm horizontal main-panel center offset,
  producing a tiny side interference at otherwise zero clearance.

An independent back-panel adjustment is now possible:

```sh
uv run python boxxer.py resize examples/3d/connected-source.svg \
  --connector-depth 15.5 --output higher-back.svg
uv run python boxxer.py inspect higher-back.svg
```

That gives about 0.25 mm rim clearance in this example; it does not automatically
change the footprint clearance or recenter the main panels.

### Export and approximation details

- STL coordinates are **millimeters**, with the base floor's middle plane at
  Z=0. STL itself has no unit metadata; the JSON records the units.
- The complete STL is a material union, removing internal coincident faces.
  The report measures collisions **before union**, so a successful mesh export
  does not conceal an invalid assembly. Base/lid exports allow separate viewing;
  for a connected box these are logical panel groups, not separable cut pieces.
- Exports are checked for closedness and consistent winding **after STL
  serialization and reimport**, as well as in memory. Binary STL uses float32.
- Numeric contact epsilon is 0.000001 mm and intersection-volume epsilon is
  0.0000001 mm³. Mesh coordinates are canonicalized to 0.00000001 mm to remove
  floating-point rotation artifacts; these are numerical controls, not allowed
  physical gaps.
- Old source cut vertices within 0.001 mm of main-score anchors are explicitly
  normalized for graph extraction and listed in `topology_adjustments`. Rerun
  `fix --align-reliefs` to persist those anchor corrections in the SVG; the new
  `examples/3d/*-source.svg` files already contain them.
- Curves such as the generated finger notch are tessellated for the mesh. They
  can be assembled, though resizing an already curved source is still unsupported.
- This is a **static closed-state** check with prescribed 90° folds. It does not
  prove a collision-free closing sequence or model bend radius, compression,
  glue thickness, springback, or the force needed to close a zero-clearance lid.

Ready-to-open models and their reports are in `examples/3d/`: `separate.stl`,
`connected.stl`, and corresponding `*-base.stl` / `*-lid.stl` files. Their clean
source SVGs and repair logs are included. `connected.stl` intentionally represents
the detected invalid closure rather than silently adjusting the design.
`separate-25-notch.stl` and `separate-25-notch-lid.stl` demonstrate the resized
25 mm walls and the rounded finger notch in 3D.

## Supported scope and next extensions

This is a family-specific prototype, not yet the general template framework:

- Two axis-aligned rectangular main-panel score loops, either on separate cut
  outlines or in the supplied lid-above-base connected arrangement.
- Straight-segment input for resizing. Existing curves can be inspected and
  assembled, but cannot yet be resized. Regenerate notched designs from the
  unnotched source.
- Closed, simple cut outlines without internal slots or holes; side tabs arranged
  as in the examples. Connected side-wall material must separate into two regions
  on each side of the connector.
- SVG transforms and inherited basic stroke styles are resolved. CSS classes,
  clones, clipping, and other unsupported constructs require flattening first.
  Main-panel rotations other than axis-aligned orientations are unsupported.
- Near-fold offsets under one quarter of nominal wall height are treated as
  fixed reliefs. This is a recognition heuristic for these layouts, not a general
  feature-attachment model. Substantially different flap constructions need
  explicit rules before resizing.

Each run reads the current geometry again, so edits to panel margins are measured
fresh. There is currently no sidecar or manual role override. Natural next steps
are explicit role/feature hints, additional real design fixtures, fixed-size
existing curve features, and physical bend/closing-path models.
