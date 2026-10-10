# Boxxer

Boxxer generates cut and score lines for plotting paper trays and boxes.

![Sketch and compiled output.](docs/assets/sketch-to-compiled.png)

Input sketches define the basic shape / layout. Boxxer then understands internal (overlapping) edges as folds, virtually folds the box and moves all folds and outlines to account for paper thickness and lid clearance.

Supports resizing, mirror and rotational symmetry, lids, finger cutouts, internal and external flaps, ±90° and ±180° folds, and scaling of interlocking elements.


## Get started

The above example is produced with

```sh
boxxer resize presets/box_clamshell_glued-flaps.svg --width 50 --length 50 --base-height 25 --lid-height 25 --connector-depth 25 --snap-near --thickness 0.4 --lid-clearance 0 --finger-cutout one --output out.svg
```

The easiest way to get started is with interactive mode:

```sh
uv run boxxer interactive
```

or have a look at `presets/*.svg`.


## More

AI tools, please read the [design guide](docs/design.md).

For Silhouette Studio, output as `*.dxf` and in Silhouette Studio chose settings > import > dxf > original position, to not rescale dxfs on import.
