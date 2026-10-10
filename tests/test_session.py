"""Replay the wizard's command and verify retained configuration on retry."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from rich.console import Console

from boxxer.cli import interactive_session, terminal_session
from boxxer.panelgraph import PanelGraph
from boxxer.tui import Navigate, TerminalWizard
from tests.test_fingercuts import tray


class SessionTests(unittest.TestCase):
    def test_equivalent_command_replays_exact_outputs_with_quoted_paths(self):
        with tempfile.TemporaryDirectory(prefix="boxxer sketches ") as directory:
            source = Path(directory)/"a 'quoted' sketch.svg"
            output = Path(directory)/"compiled box.svg"
            editable = Path(directory)/"editable box.svg"
            source.write_bytes(tray())
            answers = iter(["", "50", "50", "50", ".4", ".1", "opposing", str(output), str(editable), "y"])
            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            expected_output, expected_source = output.read_bytes(), editable.read_bytes()
            args = shlex.split(report["equivalent_command"])
            self.assertEqual(args[:4], ["uv", "run", "boxxer", "resize"])
            self.assertEqual(args[4], str(source))
            result = subprocess.run([sys.executable, "-m", "boxxer", *args[3:], "--json"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), expected_output)
            self.assertEqual(editable.read_bytes(), expected_source)
            self.assertEqual(json.loads(result.stdout)["finger_cutout"], report["finger_cutout"])

    def test_stored_cutout_settings_are_explicit_in_replay_command(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"compiled.svg"
            PanelGraph("generated.svg", _source_bytes=tray()).compile(
                source, thickness=.4, finger_cutout="one", finger_cutout_diameter=20, finger_cutout_depth=6)
            output = Path(directory)/"out.svg"
            answers = iter(["", "", "", "", "", "", "", str(output), "", "y"])
            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=lambda _: next(answers), print_fn=lambda *args: None)
            args = shlex.split(report["equivalent_command"])
            self.assertEqual(args[args.index("--finger-cutout-diameter")+1], "20")
            self.assertEqual(args[args.index("--finger-cutout-depth")+1], "6")

    def test_terminal_retains_entered_values_and_cancel(self):
        wizard = TerminalWizard([])
        with patch("boxxer.tui.questionary.text") as text:
            text.return_value.ask.return_value = "50"
            self.assertEqual(wizard.input("Width (mm) [100]: "), "50")
            wizard.input("Width (mm) [100]: ")
            self.assertEqual(text.call_args.kwargs["default"], "")
            self.assertEqual(text.call_args.kwargs["placeholder"][0][1], "50")
            validator = text.call_args.kwargs["validate"]
            self.assertIs(validator("50"), True)
            self.assertIsNot(validator("nan"), True)
            text.return_value.ask.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                wizard.input("Width (mm) [100]: ")

    def test_generation_failure_retries_with_same_terminal_instance(self):
        with patch("boxxer.tui.TerminalWizard") as terminal, patch("boxxer.cli.interactive_session") as session:
            session.side_effect = [ValueError("Bad dimensions"), {"warnings": []}]
            terminal.return_value.retry.return_value = True
            self.assertEqual(terminal_session(Path("source.svg"), Path("unused")), {"warnings": []})
            terminal.assert_called_once()
            self.assertEqual(session.call_count, 2)
            terminal.return_value.retry.assert_called_once()

    def test_keyboard_wizard_generates_and_renders_summary_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"tray.svg"
            output = Path(directory)/"out.svg"
            source.write_bytes(tray())
            wizard = TerminalWizard([{"name": "Tray", "source": source}])
            display = io.StringIO()
            wizard.console = Console(file=display, width=120)

            def select(label, **kwargs):
                values = {"Choose a sketch": str(source), "Finger cutouts": "opposing", "Next step": "generate"}
                return Mock(ask=Mock(return_value=values[label]))

            def text(label, **kwargs):
                return Mock(ask=Mock(return_value=str(output) if label == "Compiled output" else ""))

            with patch("boxxer.tui.questionary.select", side_effect=select), \
                    patch("boxxer.tui.questionary.text", side_effect=text), \
                    patch("boxxer.tui.questionary.confirm") as confirm:
                confirm.side_effect = lambda label, **kwargs: Mock(ask=Mock(return_value=label != "Create matching lid"))
                report = interactive_session(directory=directory, input_fn=wizard.input,
                                             print_fn=wizard.print, ui=wizard)
            self.assertTrue(output.exists())
            self.assertEqual(report["finger_cutout"]["mode"], "opposing")
            rendered = display.getvalue()
            self.assertIn("Review your box", rendered)
            self.assertIn("Box generated", rendered)
            self.assertIn("Equivalent command", rendered)
            self.assertIn("uv run boxxer resize", rendered)

    def test_real_text_prompts_accept_defaults_replace_and_go_back(self):
        import questionary
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        original = questionary.text
        wizard = TerminalWizard([])
        with create_pipe_input() as pipe:
            def text(label, **kwargs):
                return original(label, input=pipe, output=DummyOutput(), **kwargs)

            with patch("boxxer.tui.questionary.text", side_effect=text):
                pipe.send_text("\r")
                self.assertEqual(wizard.input("Width (mm) [100]: "), "100")
                pipe.send_text("80\r")
                self.assertEqual(wizard.input("Width (mm) [100]: "), "80")
                pipe.send_text("\x1b[Z")  # Shift+Tab from the next setting.
                with self.assertRaises(Navigate) as back:
                    wizard.input("Length (mm) [100]: ")
                self.assertEqual(back.exception.target, "Width (mm)")
                wizard.navigate(back.exception.target)
                pipe.send_text("70\t")
                self.assertEqual(wizard.input("Width (mm) [100]: "), "70")
                self.assertEqual(wizard.values["Width (mm)"], "70")

    def test_navigation_replays_earlier_fields_and_source_resets_defaults(self):
        wizard = TerminalWizard([])
        wizard.values = {"Source": "old.svg", "Width (mm)": "80", "Length (mm)": "90"}
        wizard.history = list(wizard.values)
        wizard.navigate("Length (mm)")
        with patch("boxxer.tui.questionary.text"), patch.object(wizard, "ask", return_value="50") as ask:
            self.assertEqual(wizard.input("Source: "), "old.svg")
            self.assertEqual(wizard.input("Width (mm) [100]: "), "80")
            ask.assert_not_called()
            self.assertEqual(wizard.input("Length (mm) [100]: "), "50")
        with patch("boxxer.tui.questionary.text"), patch.object(wizard, "ask", return_value="new.svg"):
            wizard.input("Source: ")
        self.assertEqual(wizard.values, {"Source": "new.svg"})

    def test_detected_dimensions_are_rounded_and_edited_values_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"sketch.svg"
            source.write_bytes(tray(height=20.0001))
            output = Path(directory)/"out.svg"
            answers = iter(["", "", "", "", "", "", "none", str(output), "", "y"])
            prompts = []

            def answer(prompt):
                prompts.append(prompt)
                return next(answers)

            with redirect_stdout(io.StringIO()):
                report = interactive_session(source, input_fn=answer, print_fn=lambda *args: None)
            self.assertIn("Height (mm) [20]: ", prompts)
            self.assertEqual(report["metadata"]["resize"]["effective_dimensions"]["height"], 20)
            self.assertIn("--height 20.0", report["equivalent_command"])

    def test_review_edit_updates_generated_box_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory)/"sketch.svg", Path(directory)/"out.svg"
            source.write_bytes(tray())
            wizard = TerminalWizard([])
            wizard.console = Console(file=io.StringIO())
            reviews = iter(["edit", "generate"])
            widths = iter(["80", "60"])

            def select(label, **kwargs):
                values = {"Finger cutouts": "none", "Setting to edit": "Width (mm)"}
                value = next(reviews) if label == "Next step" else values[label]
                return Mock(ask=Mock(return_value=value))

            def text(label, **kwargs):
                value = next(widths) if label == "Width (mm)" else str(output) if label == "Compiled output" else ""
                return Mock(ask=Mock(return_value=value))

            with patch("boxxer.tui.TerminalWizard", return_value=wizard), \
                    patch("boxxer.tui.questionary.select", side_effect=select), \
                    patch("boxxer.tui.questionary.confirm", return_value=Mock(ask=Mock(return_value=False))), \
                    patch("boxxer.tui.questionary.text", side_effect=text):
                report = terminal_session(source, Path(directory))
            self.assertEqual(report["metadata"]["resize"]["effective_dimensions"]["width"], 60)
            self.assertIn("--width 60.0", report["equivalent_command"])


if __name__ == "__main__":
    unittest.main()
