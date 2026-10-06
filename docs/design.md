# Boxxer design and reference

This is the technical reference. For a quick start, see the [README](../README.md).
Commands below run from the repository root.

## Project layout

```text
src/boxxer/     Python package and command-line interface
tests/         Regression tests and generated geometry
presets/       Editable SVG sketches discovered by the interactive menu
docs/design.md Drawing conventions, algorithms, limits, and development notes
examples/      Curated compiled SVG examples
drafts/        Experimental source sketches
compiled/      Local generated output (ignored for new files)
```

`uv sync` installs the package in editable mode. Run `uv run boxxer` or
`uv run python -m boxxer`; imports use the `boxxer` package. Built wheels include
the presets, so the interactive menu also works outside a source checkout.

### Code responsibilities

- `cli.py`: arguments, interactive prompts, reporting, and atomic output handling.
- `session.py`: shared resize requests and shell-quoted replay commands.
- `tui.py`: Questionary keyboard prompts and Rich review/results panels.
- `dxf.py`: millimetre DXF manufacturing paths with coloured operation layers.
- `panelgraph.py`: SVG import, panel/seam topology, shell inference, resizing,
  fitting, compensation, and SVG compilation.
- `corners.py`: editable Inkscape fillet effects and physical radii.
- `features.py`: anchored slots and rigid boundary/internal features.
- `flaps.py`: folded-footprint overlap and flap shortening.
- `foldguides.py`: signed fold overlays and finite symmetry expansion.
- `foldstack.py`: signed-fold thickness placement and closure checks.
- `fingercuts.py`: procedural finger openings and material clearance.

## Model

Boxxer converts an editable SVG made from separate material panels into plotter
paths. It does not recognize a hardcoded box net. Its model is a graph:

- Each top-level closed shape is a material panel.
- A shared boundary interval between two panels is a fold.
- Every panel boundary not shared by another panel is a cut.
- A closed shape strictly contained inside one panel is internal cut geometry.
- An explicit solid fill marks a tab as landing on the outside of its receiving
  surface. Unfilled panels default to inside placement. Panel dash style does
  not specify placement.
- Objects with effective overall opacity below 100% are ignored, including
  panels, holes, fold guides, symmetry markers, and entire translucent groups.

Rectangles are only one possible panel shape. Rotations, oblique straight edges,
rounded rectangles, circular arcs, quadratic curves, and cubic curves are read
as native SVG geometry. Native curves remain curves in the compiled output.

Thickness compensation is derived from this topology. Boxxer folds each connected
component virtually, infers unambiguous leaf-tab contacts, and moves the tab,
crease, and attached internal geometry together. Connected and detached top/bottom
shells can be fitted by expanding the outer lid.

## Setup

```sh
uv sync
```

## Fold overlays and symmetry markers

### Panel fill and sketch opacity

Apply any solid fill to a tab to select outside placement. Fill colour does not
matter; `fill="none"` means inside. Explicit fill inherited from an enclosing
group counts, but SVG's implicit black default does not. Gradients and pattern
fills are not solid placement markers. Inline style overrides presentation
attributes. Resized source files preserve outside markers using a light solid
fill, and compiled metadata retains the resulting placement.

Use overall object `opacity` below 1 (or below `100%`) for sketch-only geometry.
It is skipped before transforms or geometry validation, so construction lines
do not need to be closed or valid material boundaries. A translucent group
excludes all its descendants even if a child sets opacity to 1. `fill-opacity`
and `stroke-opacity` are paint styling, not object opacity: they do not exclude
objects or change the fill placement marker. Keep manufacturing geometry at
100% overall opacity. Older sketches using translucent material outlines or
dashed outside tabs need their styles updated to these conventions.

Draw straight line overlays snapped to shared panel boundaries. Colour is only
a visual aid. Traversal count and dash style specify the fold:

| Overlay | Rotation from flat | Score side |
| --- | --- | --- |
| None, or single solid stroke | +90° valley | Front |
| Single dashed stroke | −90° mountain | Back |
| Solid out-and-back stroke | +180° valley | Front |
| Dashed out-and-back stroke | −180° mountain | Back |

A double line is **one path traversing the same edge in both directions**, not
two separated parallel lines. For example, `M10 6 V10 Z` travels down the seam
and closes back along it. Inkscape's path-direction indicators show both
directions. Subdividing a stroke does not change its angle. Partial returns,
conflicting annotations, and guides not covering complete shared seams are
rejected. Path drawing direction and stroke colour do not affect fold signs.
The front is the face represented by the sketch; valley folds bring front
faces together. Only zero-area line overlays specify mountain folds; dashed
panel outlines have no placement meaning.

A **single dash-dotted line** (`long dash, gap, short dash, gap`) is a mirror
axis. Boxxer reflects panel geometry, internal cuts, and fold overlays across
it before building the graph. Shapes already present at the reflected location
are retained once. One axis or two perpendicular axes are supported, including
rotated axes. Axis extent does not crop the reflection. Mirroring within the
sheet preserves mountain/valley signs. Resized editable sources contain the
expanded geometry and fold overlays, so they do not expand a second time.

A **dash-dotted open circular arc** indicates rotational symmetry. Its circle
centre is the rotation centre and its angular span is the repeat angle:
180° gives two copies, 120° three, 90° four, and 60° six, including the original.
The radius, stroke colour, and direction of drawing are immaterial. In Inkscape,
use the ellipse tool's open-arc mode and set the start/end angles numerically.
The span must divide 360° (2–360 copies); an ellipse or a full circle is not a
rotation marker. Transformed and multi-segment circular arcs are supported.

Rotation copies material, internal cuts, editable corners, and fold annotations.
Existing copies are deduplicated geometrically. Multiple rotational markers
must agree on the centre and order. Mirror axes combined with rotation must
pass through its centre; two perpendicular mirror axes require an even rotation
order. Conflicting fold or material copies are rejected. These are general
geometry rules, independent of source filenames. Expansion precedes resizing,
allowing a fourfold square net to become a rectangular net while retaining
the corner-gusset angles.

Compiled output has front scores in `boxxer-scores` and backside scores in
`boxxer-scores-back`, with signed angle and score-side attributes. A 180° guide
produces one score operation, not two passes. Both groups use the same front-view
coordinates; registration/mirroring for physically turning the sheet is a
separate plotter setup step. Backside scores are displayed blue and dashed.

### Gusset thickness and crease placement

Signed-fold components use a **scored-surface sharp-hinge model**: panels are
midplanes, and the hinge lies half a thickness above the midplane for a valley
or below it for a mountain. Every closed loop is checked with the signed angles.
Thickness placement solves all crease shifts together, keeps the recognized
principal panel's creases fixed (falling back to the largest panel for an
unrecognized component), and checks that parallel overlapping faces occupy
separate material layers. Inconsistent loops are rejected even at zero thickness.

For the top-left corner of `presets/gluefree.svg`, at `--thickness 0.4` and
`--fold-offset 0`, the solution is:

- Vertical backside return crease: **0.4 mm left**.
- Horizontal gusset crease: **0.8 mm upward**.
- Diagonal and base creases: unchanged.

The two returning gusset midplanes sit 0.4 mm and 0.8 mm behind the wall's
midplane. Half-thickness surface offsets are already included in the hinge
equations; subtracting another 0.2 mm from the horizontal shift would break
closure. For these signed-fold components, `fold_offset` adjusts the effective
layer pitch to `thickness + fold_offset`; it defaults to zero. A negative value
that makes physical layers overlap is rejected. Existing unannotated glue-tab
designs retain their original compensation rule below.

Scores are shifted within the existing cut outline, and free endpoints are
clipped to the material. Internal crease endpoints can separate at a thick
multi-fold junction. This idealised construction does not model bend radius,
paper compression, folding sequence, or the deformation of that small junction;
it does not add automatic relief holes. The layer check covers parallel stacked
faces, not a full collision simulation during folding.

The glue-free preset expands to 13 panels, 16 folds, and four slots. Resizing
scales corner gussets uniformly with wall height, preserving their diagonal
angles and native curved boundaries; slots retain their feature rules.

```sh
uv run boxxer compile presets/gluefree.svg --snap-near \
  --thickness 0.4 --fold-offset 0 --output compiled/gluefree-compiled.svg
```

## DXF output for Silhouette Studio

An output filename ending in `.dxf` (case-insensitive) selects direct DXF export
for `compile`, `resize`, and interactive mode. All manufacturing
geometry uses the same resizing, compensation, corner rendering, and cutout
pipeline as SVG output. No intermediate Inkscape conversion is required.

```sh
uv run boxxer compile presets/gluefree_rot.svg --snap-near \
  --thickness 0.4 --finger-cutout opposing --output box.dxf
```

The exporter writes an AutoCAD R2000 ASCII DXF with `$INSUNITS=4` (millimetres)
and metric measurement settings. Coordinates remain in physical millimetres;
SVG page-down Y is converted to CAD page-up Y using the document height.

| Operation | Layer | Explicit indexed colour |
| --- | --- | --- |
| Exterior cuts, holes, and relief cuts | `CUT` | 7: black/white (black on a white background) |
| Front-side scores | `SCORE_FRONT` | 1: red |
| Backside scores | `SCORE_BACK` | 5: blue |

Colour is assigned on each entity as well as its layer. Entities use continuous
linetypes: a dashed SVG score remains one continuous score, not a collection of
short segments. Closed contours and holes are closed lightweight polylines;
open scores and relief cuts remain open. All curves, including circular arcs,
rounded corners, finger cutouts, elliptical arcs, and Bezier curves, are flattened
with a maximum chord error of 0.01 mm. DXF contains straight polyline segments
only, with no arc bulges: Studio was observed to misplace an opposing cutout
when importing otherwise valid curved polylines. SVG output retains its native
curves. This compatibility export avoids relying on Studio's arc interpretation.

Use Studio's original-size / “As Is” DXF import setting rather than “Fit to Page”.
The colours can be assigned separate settings in **Send → Line**, if preserved
by your Studio version. Selecting an operation colour does not automatically
flip the sheet or configure tool pressure: backside scoring is a separate pass.

DXF is manufacturing output only. It does not retain editable Inkscape effects
or Boxxer's embedded-source metadata, and cannot be used as Boxxer input. Keep
the original SVG or request `--source-output editable.svg` when resizing. For
programmatic file-like output, use a text stream and `output_format="dxf"`;
SVG continues to use binary streams by default.

## Generated finger cutouts

Finger openings are a generation option; no sketch annotations are needed:

```sh
uv run boxxer compile source.svg --snap-near --thickness 0.4 \
  --finger-cutout --output compiled.svg

uv run boxxer resize source.svg --snap-near \
  --width 100 --length 80 --height 20 \
  --finger-cutout opposing --finger-cutout-diameter 30 --finger-cutout-depth 10 \
  --output resized.svg
```

- `--finger-cutout` (or `--finger-cutout one`) selects one opening.
- `--finger-cutout opposing` selects two opposite free walls.
- `--finger-cutout none` disables openings, including a setting stored in compiled input.
- Diameter defaults to **30 mm**, and penetration into the edge to **10 mm**.
  This is a circular segment with an approximately **28.28 mm mouth**, not a
  semicircle. Diameter and depth must be positive; depth may not exceed the radius.

On a hinged lid, the selected flap is opposite the hinge, independent of page
rotation. On a detached lid or single-shell tray, the default is the page-top
wall. A lid-and-base enclosure receives openings in the lid only. The opposing
option requires two free opposite walls; it never cuts the lid's hinge panel.
An ambiguous top wall is reported instead of selecting one arbitrarily.

Each opening is centred along its principal fold and projected onto the free
edge. It is clipped to leave **at least 3 mm of material** to other cut boundaries,
internal slots/holes, and final score lines. The circle radius is retained;
cropping can introduce straight sides or a flat bottom. The crop keeps the
widest clear mouth, then limits depth. A flap with no usable safe opening, an
already occupied edge centre, or no straight free edge is left without a cutout,
with the reason included in the report.

Openings are calculated after resizing, fitting, and thickness compensation.
The generated cut follows native SVG arcs and lines, retaining editable corner
effects elsewhere on the contour. Settings and actual openings are recorded in
`finger_cutout` and `finger_cutouts` metadata. Compiled SVG input reuses its
settings unless overridden, and regenerates openings from the embedded source
on recompilation or resize. Editable source output remains the pre-manufacturing
panel sketch; the procedural openings are in the compiled cut contour.
The interactive wizard asks only for the cutout mode. Diameter and depth default
to 30 mm and 10 mm, or reuse settings stored in compiled input; customize them
with command-line flags.

## Inspect a panel source

```sh
uv run boxxer inspect rotational.svg
uv run boxxer inspect second.svg --json
```

Inspection reports:

- Panel IDs and areas.
- Internal geometry and its owning panel.
- Exact folds.
- Connected components.
- Panel pairs whose edges are nearly, but not exactly, coincident.
- Invalid material-area overlaps.
- Inferred leaf-tab contacts and achieved compensated separation.
- Inferred connected or detached top/bottom shells and flap-aware lid fit.
- Whether the graph belongs to the resizable box-net family and its nominal
  width, length, wall heights, and connector depth.

Only coincident boundaries are folds by default. A shared point is not a fold.
Material must lie on opposite sides of a seam; two coincident boundaries with
material on the same side are overlap evidence, not a fold.

Both supplied drawings contain drafting discrepancies measured in millionths of
a millimeter. For example, one edge in `rotational.svg` differs by 0.000002 mm.
These are reported rather than silently accepted.

## Compile cut and score paths

```sh
uv run boxxer compile rotational.svg --snap-near \
  --thickness 0.5 --output rotational-compiled.svg

uv run boxxer compile second.svg --snap-near \
  --thickness 0.5 --fold-offset 0 --output second-compiled.svg

uv run boxxer compile drafts/connected.svg --snap-near \
  --thickness 0.5 --lid-clearance 0 --output compiled/connected-compiled.svg
```

Compilation produces:

- Black paths for exposed cut contours.
- Red paths for shared fold seams.
- Black paths for contained internal geometry, such as the four slots in
  `second.svg`.
- Separate black open paths for inward-fold relief cuts.
- Embedded `boxxer:model` JSON metadata containing source panel IDs, folds,
  components, inside/outside hints, repairs, contact inference, compensation,
  achieved folded separation, thickness, and fold offset.

The complete cut and score design is translated together so the exterior cut
boundary starts at `10,10` mm in the output SVG. The applied translation is stored
in metadata; ordinary compilation never modifies source geometry.

Schema-4 compiled SVGs are self-contained. They embed the exact uncompensated
source SVG with a checksum, along with thickness, fold offset, lid clearance, and
seam-repair settings. A compiled file can therefore be recompiled directly:

```sh
uv run boxxer compile compiled/connected-compiled.svg \
  --thickness 0.8 --lid-clearance 0.1 \
  --output compiled/connected-08.svg
```

Omitted properties reuse their stored values; supplied options override them.
Boxxer always starts from the embedded source, so compensation is never applied
cumulatively. Editing a metadata property manually takes effect on the next
compilation, not immediately on the existing manufacturing paths. Older compiled
SVGs without embedded source geometry must be regenerated from their original
source before they can be recompiled.

`--snap-near` is explicit permission to repair near-coincident straight seams.
The canonical seam lies halfway between the two source edges. Fold intervals are
removed from both panels, and neighboring exposed segments are joined at the
repaired seam endpoints. The original file is never changed. Every discrepancy
is recorded in the metadata.

The default near-edge search distance is 0.01 mm. Override it with
`--near-distance`, but this is a candidate-search and repair limit, not a hidden
manufacturing tolerance:

```sh
uv run boxxer inspect design.svg --near-distance 0.002
```

Compilation without `--snap-near` refuses to proceed while candidate seams remain.
Panel interior overlaps always block compilation.

## Resize a box net

Resizing operates on uncompensated panel-source geometry before lid fitting,
thickness compensation, and relief generation:

```sh
uv run boxxer resize drafts/connected.svg --snap-near \
  --width 80 --length 70 --height 25 \
  --thickness 0.5 --lid-clearance 0.1 \
  --source-output resized-source.svg \
  --output resized-compiled.svg
```

`--width` and `--length` are nominal principal-face fold dimensions. `--height`
sets a single shell's wall height. In a two-shell enclosure it sets the base and
also sets the lid when their source heights match. `--base-height` and
`--lid-height` override those values independently. Connector depth follows the
base-height change unless `--connector-depth` is supplied.

For connected and separate designs, width and length target the base. The same deltas are
applied to the lid, preserving any source lid/base difference; physical lid fit is
then added from material thickness, flap layers, and lid clearance.
Detached pieces retain their original material-to-material page gap.

Structural spans stretch according to their role:

- Receiving slots retain their width and end-cap profiles. Each end keeps its
  original inset from the nearest transverse edge of the owning panel. Their
  length therefore follows changes to that panel's span, while their lateral
  position follows their fractional attachment position on those edges.
- Finger cutouts and inward edge notches retain their complete profiles and their
  relative position along the supporting straight edge. A centred cutout stays
  centred. Holes and other rigid internal cuts retain their size.
- Tab roots grow with wall height while retaining fixed end reliefs. Flap depth
  remains unchanged unless overlapping folded flaps require shortening.

For example, a vertical slot on a 20 mm wall with 3 mm insets at each end is
14 mm long. Increasing the wall height to 30 mm makes it 24 mm long, with the same
width and end insets. These feature rules also apply during lid fitting.

Non-square rectangular and circular-ended rounded internal cuts are automatically
treated as slots. Other internal profiles stay rigid. For an elongated decorative
opening that should remain rigid, specify `boxxer:feature="fixed"`. Explicit
`boxxer:feature="slot"` requires a supported slot profile and transverse edge
anchors. Declare the namespace on the SVG root:

```xml
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:boxxer="https://boxxer.local/ns/1"
     width="210mm" height="297mm" viewBox="0 0 210 297">
  <!-- Other panel-source geometry ... -->
  <rect id="decorative-opening" x="60" y="42" width="1" height="6"
        boxxer:feature="fixed"/>
</svg>
```

Feature policies survive editable-source output and compiled round trips. A resize
is rejected if slot end profiles and insets no longer fit, panel interiors overlap,
containment changes, or fold topology changes.

### Editable corner radii

Boxxer preserves Inkscape's `fillet_chamfer` (Corners/Chamfer) Live Path Effect on
exposed corners between straight source edges. The LPE's `inkscape:original-d`
path is used as the sharp structural geometry for fold detection, resizing,
fitting, and compensation. The physical corner radius remains fixed while its
adjacent structural spans change.

Editable resized source files contain regenerated live panel-level effects. The
merged manufacturing cut contour also receives a live `fillet_chamfer` effect,
with generated contour vertices left at zero radius and tracked source corners
restored at their original radii. Its visible `d` geometry is already rounded, so
ordinary SVG renderers and plotters see the intended result without opening the
file in Inkscape. Inkscape can subsequently adjust the same corners directly.

Only circular fillets represented by a `fillet_chamfer` effect are currently
supported. Chamfers, arbitrary path effects, nonuniformly scaled radii, effects on
curved `original-d` paths, and radii that no longer fit after a resize are rejected
explicitly rather than flattened or silently discarded. Compiled metadata lists
the originating shape, vertex, and physical radius under `editable_corners`.

The main output is a schema-4 manufacturing SVG embedding the resized,
uncompensated source. `--source-output` additionally writes that editable source.
Resizing either an original source or a compiled schema-4 file is therefore
absolute and does not accumulate earlier compensation.

Resize output always uses a portrait A4 SVG page (`210 x 297 mm`). Geometry is
translated to start at `10,10` but is intentionally allowed to extend beyond the
page. Actual output bounds and `page_overflow` are recorded in metadata.

Automatic resizing supports one shell, two detached shells, or two shells joined
by one connector. It currently requires axis-aligned rectangular principal faces,
straight structural folds, and uniform wall height within each shell. Native
curves away from structural folds are retained.

## Interactive resize

Run the guided terminal workflow without an input path to choose one of the
supplied presets:

```sh
uv run boxxer interactive
```

The wizard lists SVG files in the preset folder, automatically repairs reported
near-coincident seams, shows the inferred dimensions, and prompts
for target dimensions, material settings, and output paths. In a terminal,
Questionary provides arrow-key menus and path completion; Rich displays compact
review and result panels. Numeric values are validated as they are entered.
Keyboard hints appear faintly in a right-aligned bottom toolbar, separate from
the field label and input.
Defaults are faint placeholders in empty text fields: Enter accepts the default,
while typing replaces it instead of appending to it. Detected dimension defaults
are rounded to 0.1 mm; explicit entered values retain their precision. This only
rounds the wizard's suggested targets, not imported sketch geometry.

Shift+Tab returns to the previous setting. Numeric fields also accept Tab to
advance; path fields keep Tab for path completion. Accepted values are preserved
and become placeholders when revisiting a field. At review, choose **Generate**,
**Edit settings** (then select a field), or **Cancel**. Changing the source sketch
clears prior settings so its geometry supplies fresh defaults. The equivalent
command reflects the final edited values.

Press Ctrl-C to cancel. If generation fails, choose
to edit and try again; previously entered field values are retained.

Use `uv run boxxer interactive --plain` for the simple text interface (Enter
retains defaults; `q` cancels). Non-TTY input or output automatically selects
plain prompts, preserving scripted workflows.

Instead of selecting a preset, enter any SVG path at the source prompt. A path can
also be supplied directly:

```sh
uv run boxxer interactive path/to/design.svg
```

The interactive menu discovers SVG files directly in `presets/`, sorted by
filename, with readable names derived from their filenames. There is no manifest.
Use another folder with `--presets path/to/sketches`. Adding or removing an SVG
updates the menu automatically; nested folders and non-SVG files are ignored.
Manufacturing defaults are thickness 0.5 mm, fold offset 0, and lid clearance 0;
compiled SVGs reuse their stored settings. Interactive output uses the same validation,
hard-link protection, and atomic writes as the regular `resize` command.

After successful generation, both interfaces print an **equivalent command**.
It includes the source, target dimensions, near-edge tolerance and repair mode,
material settings, effective finger-cutout mode/diameter/depth, and both output
paths when requested. Paths are shell-quoted for POSIX shells, including spaces
and apostrophes. The command uses the same `ResizeRequest` execution path as the
wizard and regular CLI resize command. Copy it to reproduce the same output
from the same input; replaying it replaces the specified outputs just like a
normal CLI resize.

## Thickness compensation

Boxxer considers a degree-one panel a glue tab only when virtual 90-degree folding
finds exactly one non-adjacent receiving panel with significant coplanar overlap.
Ambiguous contacts are reported rather than guessed. If compensation is requested
while contact inference is incomplete, compilation stops instead of silently
producing a partially compensated result.

The requested tab-to-receiver midplane separation is:

```text
thickness + fold_offset
```

`--fold-offset` is signed, so it can increase or reduce the material-thickness
allowance; the total may not be negative. Unfilled tabs default to inside placement
and move toward their supporting panel. Solid-filled tabs explicitly select outside
placement and move away. Source-coordinate discrepancies from an approved
`--snap-near` repair are included when solving the displacement, and metadata
records both the geometric shift and the residual after virtual folding.

The supporting wall uses a square-overlap corner convention. Its terminal edge
moves by half the material thickness to the receiver's exterior face. Thus, for a
0.5 mm inside tab, the tab crease moves 0.5 mm inward while the wall edge moves
0.25 mm in the opposite, outward direction. For an outside tab, the crease and
wall edge both move outward, by 0.5 mm and 0.25 mm respectively. The exposed
remainder beside the tab moves as a whole. `fold_offset` changes only the tab
crease; the wall-face offset remains based on physical thickness.

For inside tabs, Boxxer emits two open relief cuts between the outside-flush wall
edge and the compensated crease while retaining a simple exterior contour.
Outside-tab root connectors remain part of the exterior contour. Internal cuts
owned by a moved tab translate rigidly with that tab.

### Overlapping flaps

Before manufacturing paths are generated, inferred flaps are compared in their
folded receiver coordinate system. Flaps on the same receiving surface and side
must not overlap. When they do, their free ends are shortened equally until their
footprints no longer overlap; hinge attachments remain unchanged. Two opposing
20 mm flaps on a 30 mm receiving wall become 15 mm each. Multiple pair constraints
are combined independently of SVG element order.

Shortening clips the free-end profile, retaining surviving native curve segments.
It is rejected if it cuts through internal geometry, disconnects material, or
changes fold topology or inferred contacts. Disjoint flaps and flaps on opposite
sides of a receiving wall are not shortened. An inside lid flap and an outside
base flap still contribute separate layers to the nesting gap.

Both direct compilation and resizing apply this rule. Resize source output
contains the shortened flaps. Direct compilation embeds the original source so a
subsequent enlargement can recover the intended flap depths. Adjustments are
reported in CLI output and `flap_shortening` metadata.

## Lid fitting

Boxxer recognizes either two dominant principal faces joined by a simple,
non-branching connector or exactly two disconnected components that each form one
valid box shell. By convention, the principal face higher on the SVG page is the
top outer lid and the lower face is the bottom inner shell.
Tabs are matched to receivers within their own shell, preventing coincident walls
in the closed assembly from creating false contact ambiguity. Shared connector
panels belong to both contact domains while retaining their separate deformation
role, so tabs from either shell receive the same crease, wall-end, and relief-cut
compensation as any other inferred tab.

The outer-lid wall offset is calculated independently for each corresponding wall
pair:

```text
thickness + gap-facing flap layers * thickness + lid_clearance
```

An inside flap on the top shell occupies the nesting gap, as does an outside flap
on the bottom shell. In `connected.svg`, the two top side walls therefore move by
1.0 mm for 0.5 mm stock, while the flap-free front wall moves by 0.5 mm. The shared
connector keeps both hinge axes and tapers from the expanded top edge to the
unchanged bottom edge. A detached lid has four independently fitted wall pairs;
its base remains unchanged. `--lid-clearance` adds nonnegative per-side
manufacturing clearance and defaults to zero.

Automatic lid fitting currently requires straight principal and shell folds and
axis-aligned rectangular principal faces. A connected layout requires one shared
connector panel; a detached layout requires exactly two recognizable shells.
Unsupported, cyclic, or ambiguous structures are reported rather than reshaped
heuristically. Glue-contact residuals are verified on local source geometry before
lid fitting and are labeled accordingly in connected-layout metadata; emitted fold
lengths in metadata reflect the fitted paths.

## Current example results

With explicit near-edge repair:

| Source | Panels | Folds | Internal cuts | Components |
|---|---:|---:|---:|---:|
| `rotational.svg` | 18 | 16 | 0 | 2 |
| `second.svg` | 9 | 8 | 4 | 1 |
| `drafts/connected.svg` | 15 | 14 | 0 | 1 |

The historical `second.svg` example used dashed curved tabs for outside-placement
hints; updated sketches must use solid fills instead.
Its four rounded slot shapes are retained unchanged as internal cuts owned by the
two side-wall panels. Boxxer does not yet simulate tabs entering slots.

One connected component is one material piece. Exactly two components are assigned
upper-lid/lower-base roles only when each independently satisfies the shell
topology. Other multi-component drawings remain neutral.

## Tests

```sh
uv run python -m unittest -v
```

The test suite covers the supplied examples, transforms, partial seams, oblique
panels, native rounded and curved output, strict internal-geometry ownership,
near-edge diagnostics and explicit repair, metadata, overlap rejection, shared
points, separate components, inside and outside compensation, signed fold offset,
relief cuts, folded-separation verification, attached internal geometry, and CLI
safety. It also covers connected and detached shell inference, local contact
selection, flap-aware lid expansion, tapered connectors, and configurable lid
clearance. Resize coverage includes single, connected, and detached shells,
fixed-size internal and curved features, preserved detached-piece gaps, editable
source output, schema-4 round trips, and A4 overflow.
Additional generated-design tests cover anchored slot ends, fixed slot width and
caps, rigid holes and edge cutouts, feature overrides, folded flap overlap,
shortening under rotation and element reordering, internal-feature protection,
shortening round trips, editable Inkscape corner effects, resized radii, merged
cut-contour effects, and compiled-file corner round trips.

New geometry tests should use small generated sketches to exercise general
rules, including symmetry, fold directions, and cutout clearances. Project SVGs
are useful manual integration examples; avoid adding filename-specific behaviour
or turning each new drawing into a dedicated algorithm regression.

Run a specific module with `uv run python -m unittest tests.test_symmetry -v`.
Build an installable wheel with `uv build --wheel`.

## Further cleanup

The next useful refactor is to split `panelgraph.py` along its pipeline stages:
SVG/source handling, topology and shell inference, geometric planning, and
manufacturing output. Keep `PanelGraph` as the coordinating API and preserve the
existing regression suite while moving one stage at a time.

Other worthwhile follow-ups:

- Extract shared generated-sketch builders into `tests/fixtures.py`, so test
  modules no longer import helper functions from each other.
- Gradually replace large plan/report dictionaries with named records where
  they cross module boundaries; retain the existing JSON metadata format.
- Review root-level sketches with their author, then curate reusable examples
  into `presets/` and experiments into `drafts/`. Keep local manufacturing output
  in `compiled/`; retain only deliberate reference outputs in `examples/`.
- Review the already tracked `test.svg` and `compiled/connected-compiled.svg`
  before removing generated artifacts from version control. Ignore rules do not
  remove tracked files.
- Add a small CI job for the regression suite and wheel/CLI smoke checks once a
  hosted workflow is wanted.
