"""Folder discovery and interactive selection using generated sketches."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from boxxer.cli import choose_interactive_source, load_presets
from boxxer.panelgraph import BOXXER
from tests.test_fingercuts import tray


class PresetDirectoryTests(unittest.TestCase):
    def test_interactive_auto_repairs_without_cutout_size_prompts(self):
        from contextlib import redirect_stdout
        from boxxer.cli import interactive_session
        from tests.test_symmetry import cyclic_corner, rotation_marker
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = ET.fromstring(cyclic_corner())
            root.find(".//{http://www.w3.org/2000/svg}path[@id='rotation']").set(
                "d", rotation_marker(center=20.0002+20.0003j))
            source = directory/"sketch.svg"
            source.write_bytes(ET.tostring(root))
            output = directory/"out.svg"
            answers = iter(["", "50", "50", "50", ".4", "", "opposing", str(output), "", "y"])
            prompts, messages = [], []

            def answer(prompt):
                prompts.append(prompt)
                return next(answers)

            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=answer, print_fn=messages.append)
            self.assertTrue(any("Automatically repairing" in message for message in messages))
            self.assertFalse(any("Repair these" in prompt or "diameter" in prompt or "depth" in prompt
                                 for prompt in prompts))
            self.assertEqual(report["finger_cutout"], {"mode": "opposing", "diameter": 30, "depth": 10})
            self.assertEqual(len(report["finger_cutouts"]), 2)

    def test_discovery_names_order_and_nonrecursive_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("z_tray.svg", "A-box.SVG", "notes.txt", "presets.json"):
                (directory/name).write_bytes(tray() if name.lower().endswith(".svg") else b"ignored")
            (directory/"nested.svg").mkdir()
            (directory/"nested.svg"/"hidden.svg").write_bytes(tray())
            presets = load_presets(directory)
            self.assertEqual([p["name"] for p in presets], ["A box", "Z tray"])
            self.assertEqual([p["source"] for p in presets],
                             [(directory/"A-box.SVG").resolve(), (directory/"z_tray.svg").resolve()])

    def test_empty_directory_still_accepts_a_path(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as sketches:
            source = Path(sketches)/"tray.svg"
            source.write_bytes(tray())
            answers = iter(["1", str(source)])
            selected, _ = choose_interactive_source(None, empty, input_fn=lambda _: next(answers),
                                                    print_fn=lambda *args: None)
            self.assertEqual(selected, source)
            self.assertEqual(load_presets(empty), [])
            # Direct paths do not depend on the preset folder existing.
            self.assertEqual(choose_interactive_source(source, Path(empty)/"missing")[0], source)
            with self.assertRaisesRegex(ValueError, "directory"):
                load_presets(source)

    def test_interactive_cli_folder_and_direct_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory/"sample-tray.svg"
            source.write_bytes(tray())
            for selection in ("1", str(source)):
                output = directory/"result.svg"
                if output.exists():
                    output.unlink()
                answers = "\n".join([selection, "", "80", "70", "25", "", "", "none", str(output), "", "y"])+"\n"
                result = subprocess.run([sys.executable, "-m", "boxxer", "interactive", "--presets", str(directory)],
                                        input=answers, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Sample tray", result.stdout)
                model = json.loads(ET.parse(output).findtext(f".//{{{BOXXER}}}model"))
                self.assertEqual(model["thickness"], .5)
                self.assertEqual(model["resize"]["effective_dimensions"], {"width": 80, "length": 70, "height": 25})


if __name__ == "__main__":
    unittest.main()
