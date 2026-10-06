"""One resize request for execution, summaries, and reproducible CLI commands."""
from dataclasses import dataclass
from pathlib import Path
import shlex


@dataclass
class ResizeRequest:
    source: Path
    output: Path
    source_output: Path | None
    dimensions: dict
    thickness: float | None
    fold_offset: float
    lid_clearance: float
    finger_cutout: str | None
    finger_cutout_diameter: float | None
    finger_cutout_depth: float | None
    near: float
    snap_near: bool

    def execute(self, graph):
        from .cli import resize_and_compile
        return resize_and_compile(
            graph, self.source, self.output, self.source_output, self.dimensions,
            thickness=self.thickness, fold_offset=self.fold_offset, lid_clearance=self.lid_clearance,
            finger_cutout=self.finger_cutout, finger_cutout_diameter=self.finger_cutout_diameter,
            finger_cutout_depth=self.finger_cutout_depth)

    def command(self):
        args = ["uv", "run", "boxxer", "resize", str(self.source)]
        for key, value in self.dimensions.items():
            if value is not None:
                args.extend(["--"+key.replace("_", "-"), repr(value)])
        args.extend(["--near-distance", repr(self.near), "--snap-near" if self.snap_near else "--no-snap-near"])
        for key in ("thickness", "fold_offset", "lid_clearance", "finger_cutout",
                    "finger_cutout_diameter", "finger_cutout_depth"):
            value = getattr(self, key)
            if value is not None:
                args.extend(["--"+key.replace("_", "-"), str(value)])
        args.extend(["--output", str(self.output)])
        if self.source_output is not None:
            args.extend(["--source-output", str(self.source_output)])
        return shlex.join(args)

    def summary(self):
        rows = [(key.replace("_", " ").title(), f"{value:g} mm") for key, value in self.dimensions.items()
                if value is not None]
        rows += [("Material thickness", f"{self.thickness:g} mm"), ("Fold offset", f"{self.fold_offset:g} mm"),
                 ("Lid clearance", f"{self.lid_clearance:g} mm"), ("Finger cutout", self.finger_cutout),
                 ("Compiled output", str(self.output))]
        if self.source_output:
            rows.append(("Editable source", str(self.source_output)))
        return rows
