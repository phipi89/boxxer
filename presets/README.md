# Boxxer presets

`presets.json` lists the SVG templates shown by `boxxer.py interactive`.
`source` paths are resolved relative to the registry file. Each entry may provide
default `thickness`, `fold_offset`, and `lid_clearance` values.

The supplied SVGs are stable copies. Editing `rotational.svg`, `second.svg`, or a
draft therefore does not silently alter the built-in presets.

Add another entry to expose an additional project SVG without copying it into
this directory. The interactive wizard also accepts any SVG path directly.
