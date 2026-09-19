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

This branch intentionally starts again at panel topology. Thickness compensation,
resizing, folded 3D analysis, generated lids, and the interactive workflow will be
rebuilt on this graph after the source compiler is established.

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
```

Compilation produces:

- Black paths for exposed cut contours.
- Red paths for shared fold seams.
- Black paths for contained internal geometry, such as the four slots in
  `second.svg`.
- Embedded `boxxer:model` JSON metadata containing source panel IDs, folds,
  components, inside/outside hints, repairs, thickness, and fold offset.

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

## Current example results

With explicit near-edge repair:

| Source | Panels | Folds | Internal cuts | Components |
|---|---:|---:|---:|---:|
| `rotational.svg` | 9 | 8 | 0 | 1 |
| `second.svg` | 9 | 8 | 4 | 1 |

`second.svg`'s four dashed curved tabs are panels with outside-placement hints.
Its four rounded slot shapes are retained unchanged as internal cuts owned by the
two side-wall panels. Boxxer does not yet simulate tabs entering slots.

## Metadata semantics

`thickness` and `fold_offset_from_midplane` are currently stored, validated, and
round-tripped as model intent. They do **not** yet move seams. The next milestone
will apply compensation by changing the shared seam and all connected panel
geometry coherently; moving a red output line alone would make the panel graph
inconsistent.

One connected component is one material piece. Multiple components are reported
without assigning box-specific meaning. A later interpretation layer may propose
lid/base roles or generate a matching lid, but the geometry compiler does not
assume that two components necessarily mean top and bottom.

## Tests

```sh
uv run python -m unittest -v
```

The test suite covers the supplied examples, transforms, partial seams, oblique
panels, native rounded and curved output, strict internal-geometry ownership,
near-edge diagnostics and explicit repair, metadata, overlap rejection, shared
points, separate components, and CLI safety.
