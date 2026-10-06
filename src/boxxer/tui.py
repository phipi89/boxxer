"""Keyboard-driven terminal prompts with retained values when editing a request."""
import math
import re

import questionary
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Navigate(Exception):
    def __init__(self, target):
        self.target = target


class TerminalWizard:
    def __init__(self, presets):
        self.presets = presets
        self.values = {}
        self.history = []
        self.replay_until = None
        self.console = Console()

    def navigate(self, target):
        self.replay_until = target

    def ask(self, question, label, *, advance=False):
        bindings = KeyBindings()

        @bindings.add("s-tab", eager=True)
        def previous(event):
            index = self.history.index(label) if label in self.history else len(self.history)
            if index:
                event.app.exit(exception=Navigate(self.history[index-1]))

        if advance:
            @bindings.add("tab", eager=True)
            def accept(event):
                event.current_buffer.validate_and_handle()

        question.application.key_bindings = merge_key_bindings(
            [question.application.key_bindings or KeyBindings(), bindings])
        return question.ask()

    def text(self, label, default, *, path=False, validate=None):
        hint = "Enter: accept · Tab: "+("complete" if path else "next")+" · Shift+Tab: back"

        def toolbar():
            width = get_app().output.get_size().columns
            return [("#888888", " "*max(0, width-get_cwidth(hint)-1)+hint)]

        question = questionary.text(
            label, default="", placeholder=[("#888888", default)],
            bottom_toolbar=toolbar, style=questionary.Style([("bottom-toolbar", "noreverse")]),
            validate=validate, completer=PathCompleter(expanduser=True) if path else None)
        value = self.ask(question, label, advance=not path)
        return default if value == "" else value

    def input(self, prompt):
        label = prompt.split(" [", 1)[0].rstrip(": ")
        match = re.search(r"\[([^\]]*)\]", prompt)
        default = self.values.get(label, match.group(1) if match else "")
        if self.replay_until:
            if label == self.replay_until:
                self.replay_until = None
            elif label in self.values:
                return self.values[label]
            else:
                # A different source/layout removed the requested field.
                self.replay_until = None
        if label == "Source" and self.presets:
            choices = [questionary.Choice(preset["name"], value=str(preset["source"])) for preset in self.presets]
            choices.append(questionary.Choice("Open another SVG…", value=""))
            selected_default = default if any(choice.value == default for choice in choices) else None
            value = self.ask(questionary.select("Choose a sketch", choices=choices, default=selected_default), label)
            if value == "":
                value = self.text(label, default if selected_default is None else "", path=True)
        elif label.startswith("Finger cutout"):
            value = self.ask(questionary.select("Finger cutouts", choices=[
                questionary.Choice("None", "none"), questionary.Choice("One", "one"),
                questionary.Choice("Opposing sides", "opposing")], default=default or "none"), label)
        elif label == "Create these files":
            value = self.ask(questionary.select("Next step", choices=[
                questionary.Choice("Generate", "generate"), questionary.Choice("Edit settings", "edit"),
                questionary.Choice("Cancel", "cancel")]), label)
            if value == "edit":
                target = self.ask(questionary.select("Setting to edit", choices=[
                    questionary.Choice(f"{key}: {self.values[key]}", key) for key in self.history]), label)
                if target is None:
                    raise KeyboardInterrupt
                raise Navigate(target)
            return "y" if value == "generate" else "n"
        elif "[Y/n]" in prompt or "[y/N]" in prompt:
            value = self.ask(questionary.confirm(label, default="[Y/n]" in prompt), label)
            if value is not None:
                return "y" if value else "n"
        elif "output" in label.lower() or label == "Source":
            value = self.text(label, default, path=True)
        else:
            def validate(raw):
                try:
                    value = float(raw or default)
                    if not math.isfinite(value):
                        return "Enter a finite number"
                    if label != "Fold offset (mm)" and value < (0 if "thickness" in label.lower() or "clearance" in label.lower() else 1.0000001):
                        return "Value is too small"
                    return True
                except ValueError:
                    return "Enter a number"
            value = self.text(label, default, validate=validate)
        if value is None:
            raise KeyboardInterrupt
        if label == "Source" and self.values.get(label) != value:
            self.values.clear()
            self.history.clear()
        self.values[label] = value
        if label not in self.history:
            self.history.append(label)
        return value

    def print(self, message):
        self.console.print(Text(str(message)))

    def summary(self, request):
        table = Table(show_header=False, box=None, padding=(0, 2))
        for key, value in request.summary():
            table.add_row(key, str(value))
        self.console.print(Panel(table, title="Review your box", border_style="cyan"))

    def command(self, command):
        self.console.print("\nEquivalent command:", style="bold cyan")
        # Leave wrapping to the terminal so copied text remains one shell line.
        self.console.print(Text(command), soft_wrap=True)

    def results(self, report):
        table = Table(show_header=False, box=None)
        table.add_row("Output", report["output"])
        table.add_row("Paths", f"{report['cut_contours']} cut contours · {len(report['folds'])} scores · {report['internal_cuts']} internal cuts")
        for cutout in report.get("finger_cutouts", []):
            description = (f"{cutout['mouth_width']:.2f} × {cutout['depth']:.2f} mm"+
                           (" (cropped)" if cutout["cropped"] else "")) if cutout["status"] == "applied" else cutout["reason"]
            table.add_row("Finger cutout", f"{cutout['panel']}: {description}")
        self.console.print(Panel(table, title="Box generated", border_style="green"))
        for warning in report.get("warnings", []):
            self.print("Warning: "+warning)
        if report.get("page_overflow"):
            self.print("Output geometry extends beyond the page.")

    def retry(self, error):
        self.console.print(Panel(Text(str(error)), title="Could not generate box", border_style="red"))
        return questionary.confirm("Edit settings and try again?", default=True).ask()
