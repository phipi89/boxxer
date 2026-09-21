# Boxxer panel compiler

Boxxer converts an editable SVG made from separate material panels into plotter
paths. It does not recognize a hardcoded box net. Its model is a graph:

- Each top-level closed shape is a material panel.
- A shared boundary interval between two panels is a fold.
- Every panel boundary not shared by another panel is a cut.
- A closed shape strictly contained inside one panel is internal cut geometry.
- A dashed panel outline is an explicit hint that the panel is placed outside
  its receiving surface. Solid panel outlines default to inside placement.

Rectangles are only one possible panel shape. Rotations, oblique straight edges,
rounded rectangles, circular arcs, quadratic curves, and cubic curves are read
as native SVG geometry. Native curves remain curves in the compiled output.

Thickness compensation is derived from this topology. Boxxer folds each connected
component virtually, infers unambiguous leaf-tab contacts, and moves the tab,
crease, and attached internal geometry together. Connected top/bottom shells can
also be fitted by expanding the outer lid. Generated detached lids and the
interactive workflow remain outside the current compiler.

## Setup

```sh
uv sync
```

## Inspect a panel source

```sh
uv run python boxxer.py inspect rotational.svg
uv run python boxxer.py inspect second.svg --json
```

Inspection reports:

- Panel IDs and areas.
- Internal geometry and its owning panel.
- Exact folds.
- Connected components.
- Panel pairs whose edges are nearly, but not exactly, coincident.
- Invalid material-area overlaps.
- Inferred leaf-tab contacts and achieved compensated separation.
- Inferred connected top/bottom shells and flap-aware lid fit.
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
uv run python boxxer.py compile rotational.svg --snap-near \
  --thickness 0.5 --output rotational-compiled.svg

uv run python boxxer.py compile second.svg --snap-near \
  --thickness 0.5 --fold-offset 0 --output second-compiled.svg

uv run python boxxer.py compile drafts/connected.svg --snap-near \
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
uv run python boxxer.py compile compiled/connected-compiled.svg \
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
uv run python boxxer.py inspect design.svg --near-distance 0.002
```

Compilation without `--snap-near` refuses to proceed while candidate seams remain.
Panel interior overlaps always block compilation.

## Resize a box net

Resizing operates on uncompensated panel-source geometry before lid fitting,
thickness compensation, and relief generation:

```sh
uv run python boxxer.py resize drafts/connected.svg --snap-near \
  --width 80 --length 70 --height 25 \
  --thickness 0.5 --lid-clearance 0.1 \
  --source-output resized-source.svg \
  --output resized-compiled.svg
```

`--width` and `--length` are nominal principal-face fold dimensions. `--height`
sets a single shell's wall height. In a connected enclosure it sets the base and
also sets the lid when their source heights match. `--base-height` and
`--lid-height` override those values independently. Connector depth follows the
base-height change unless `--connector-depth` is supplied.

For connected designs, width and length target the base. The same deltas are
applied to the lid, preserving any source lid/base difference; physical lid fit is
then added from material thickness, flap layers, and lid clearance.

Structural spans stretch, but manufacturing features keep their physical size.
Tab roots grow with wall height while retaining fixed end reliefs; tab overlap,
slots, holes, curved tab profiles, and notches remain unchanged. A resize is
rejected if fixed geometry no longer fits, panel interiors overlap, containment
changes, or fold topology changes.

The main output is a schema-4 manufacturing SVG embedding the resized,
uncompensated source. `--source-output` additionally writes that editable source.
Resizing either an original source or a compiled schema-4 file is therefore
absolute and does not accumulate earlier compensation.

Resize output always uses a portrait A4 SVG page (`210 x 297 mm`). Geometry is
translated to start at `10,10` but is intentionally allowed to extend beyond the
page. Actual output bounds and `page_overflow` are recorded in metadata.

Automatic resizing currently supports one connected box-net component,
axis-aligned rectangular principal faces, straight structural folds, uniform wall
height within each shell, and at most one shared connector. Native curves away
from structural folds are retained.

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
allowance; the total may not be negative. Solid tabs default to inside placement
and move toward their supporting panel. Dashed tabs explicitly select outside
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

## Connected lids

For a connected panel tree, Boxxer can infer two dominant principal faces joined
by a simple, non-branching connector. By convention, the principal face higher on
the SVG page is the top outer lid and the lower face is the bottom inner shell.
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
unchanged bottom edge. `--lid-clearance` adds nonnegative per-side manufacturing
clearance and defaults to zero.

Automatic lid fitting currently requires straight principal, shell, and connector
folds, an axis-aligned rectangular principal face, and one shared connector panel.
Unsupported, cyclic, or ambiguous structures are reported rather than reshaped
heuristically. Glue-contact residuals are verified on local source geometry before
lid fitting and are labeled accordingly in connected-layout metadata; emitted fold
lengths in metadata reflect the fitted paths.

## Current example results

With explicit near-edge repair:

| Source | Panels | Folds | Internal cuts | Components |
|---|---:|---:|---:|---:|
| `rotational.svg` | 9 | 8 | 0 | 1 |
| `second.svg` | 9 | 8 | 4 | 1 |
| `drafts/connected.svg` | 15 | 14 | 0 | 1 |

`second.svg`'s four dashed curved tabs are panels with outside-placement hints.
Its four rounded slot shapes are retained unchanged as internal cuts owned by the
two side-wall panels. Boxxer does not yet simulate tabs entering slots.

One connected component is one material piece. Multiple disconnected components
are still reported without assigning box-specific meaning; two components do not
automatically mean top and bottom.

## Tests

```sh
uv run python -m unittest -v
```

The test suite covers the supplied examples, transforms, partial seams, oblique
panels, native rounded and curved output, strict internal-geometry ownership,
near-edge diagnostics and explicit repair, metadata, overlap rejection, shared
points, separate components, inside and outside compensation, signed fold offset,
relief cuts, folded-separation verification, attached internal geometry, and CLI
safety. It also covers connected shell inference, local contact selection,
flap-aware lid expansion, tapered connectors, and configurable lid clearance.
Resize coverage includes single and connected shells, fixed-size internal and
curved features, editable source output, schema-4 round trips, and A4 overflow.
