# Boxxer

Turn SVG box sketches into cut and score paths for a plotter. Draw separate
panels in Inkscape, or start with a preset; Boxxer finds the folds, resizes the
box, and accounts for material thickness.

## Get started

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run boxxer interactive
```

The wizard lets you choose a sketch, set dimensions and paper thickness, and
optionally add finger cutouts. It writes a compiled SVG ready to open in your
plotter software.

For the command line:

```sh
uv run boxxer compile presets/gluefree.svg --snap-near \
  --thickness 0.4 --finger-cutout --output box.svg
```

Draw adjoining panels with shared edges for folds. Boxxer also supports signed
folds, mirror and rotational symmetry, and editable rounded corners. Output has
black cuts, red front-side scores, and blue dashed backside scores.

See **[the design guide](docs/design.md)** for drawing conventions, all options,
geometry details, and current limits. Run `uv run boxxer --help` for commands.

## Development

Code lives in `src/boxxer/`, tests in `tests/`, and editable presets in `presets/`.

```sh
uv run python -m unittest -v
```
