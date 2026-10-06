# Boxxer

Turn SVG box sketches into cut and score paths for a plotter. Draw separate
panels in Inkscape, or start with a preset; Boxxer finds the folds, resizes the
box, and accounts for material thickness.

![A one-corner rotational sketch becomes a complete tray with cut paths, front and back scores, slots, and finger cutouts.](docs/assets/sketch-to-compiled.png)

**Pinwheel tray:** [editable sketch](presets/gluefree_rot.svg) →
[compiled SVG](examples/pinwheel-tray.svg). One corner and a 90° repeat marker
produce the complete net, with thickness allowance and two finger cutouts.

## Get started

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run boxxer interactive
```

The keyboard-driven wizard lets you choose a sketch, set dimensions and paper thickness, and
optionally add finger cutouts. It writes a compiled SVG or DXF ready to open in your
plotter software and shows the equivalent CLI command so you can repeat it later.
Use `--plain` for simple text prompts; piped input selects them automatically.
Enter accepts faint defaults; typing replaces them. Shift+Tab goes back, and the
final review lets you edit a setting before generating.

For the command line:

```sh
uv run boxxer compile presets/gluefree_rot.svg --snap-near \
  --thickness 0.4 --finger-cutout opposing --output box.svg
```

Draw adjoining panels with shared edges for folds. Boxxer also supports signed
folds, mirror and rotational symmetry, and editable rounded corners. Output has
black cuts, red front-side scores, and blue dashed backside scores.

For Silhouette Studio Basic, use an output filename ending in **`.dxf`**. DXF
uses millimetres and separate black/red/blue cut and score layers. In Studio,
import at original size (not “Fit to Page”) and use **Send → Line** to assign
operations by colour. DXF score geometry is continuous, including backside scores.

See **[the design guide](docs/design.md)** for drawing conventions, all options,
geometry details, and current limits. Run `uv run boxxer --help` for commands.

## Development

Code lives in `src/boxxer/`, tests in `tests/`, and editable presets in `presets/`.

```sh
uv run python -m unittest -v
```
