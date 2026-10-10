# Boxxer

Boxxer creates plotter cut and score lines for paper trays and boxes.

![Sketch and compiled output.](docs/assets/sketch-to-compiled.png)

Input sketches define the general geometry. Boxxer reads internal overlapping edges as folds, virtually folds the box and moves all folds and outlines to allow for paper thickness. Support for resizing, mirror and rotational symmetry, adding lids, finger cutouts, internal and external flaps, ±90° and ±180° folds and scaling for interlocking elements.


## Get started

The above example is produced with

```
boxxer resize box_clamshell_glued-flaps.svg --width 50 --length 50 --base-height 25 --lid-height 25 --connector-depth 25 --snap-near --thickness 0.4 --lid-clearance 0 --finger-cutout one --output clamshell_50x50x25x0.4.svg
```

But easiest to get started is the interactive mode

```sh
uv run boxxer interactive
```

## More

AI tools, please read the [design guide](docs/design.md).

For Silhouette Studio, use an output filename ending in **`.dxf`** and chose settings > import > dxf > original position, to not rescale dxfs on import.
