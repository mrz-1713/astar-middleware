"""Tk widgets shared by the two control panels.

This lives in eap_middleware because it is the only package both install
roles get: a middleware host has gui/ and no simulator_gui/, a test machine
has the reverse. Nothing in the headless service imports it - tkinter is
imported at module scope here, and eap_middleware/__init__ deliberately does
not pull this module in, so a Windows service host never loads Tk.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollableTab(ttk.Frame):
    """A notebook tab whose content may be taller than the window.

    A plain ttk.Frame in a Notebook simply clips: no scrollbar, no hint that
    anything was cut off. The Link tab outgrew an 800px-high window the
    moment it gained a pairing section, so the last section - the one that
    tells you what to type on the other machine - was invisible with no way
    to reach it.

    Use `.body` as the parent for content.
    """

    def __init__(self, parent: tk.Misc, padding: int = 8) -> None:
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(
            self, orient="vertical", command=self._canvas.yview
        )
        self.body = ttk.Frame(self._canvas, padding=padding)
        self._window = self._canvas.create_window(
            (0, 0), window=self.body, anchor="nw"
        )
        self._canvas.configure(yscrollcommand=scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        # Binding the wheel on enter/leave rather than globally keeps two
        # scrollable tabs from both reacting to one wheel event. Enter/Leave
        # are bound on the whole tab (self), not the canvas: the canvas is the
        # parent of `.body`, so the pointer crossing from canvas onto body
        # fired Leave (NotifyInferior) and unbound the wheel exactly where the
        # operator scrolls.
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _on_body_resize(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event: tk.Event) -> None:
        # Match the inner frame to the canvas width so content wraps rather
        # than scrolling sideways.
        self._canvas.itemconfigure(self._window, width=event.width)

    # Keys as well as the wheel: a settings form is filled from the keyboard,
    # and Tab moving focus to a widget scrolled out of sight - with no way to
    # scroll without reaching for the mouse - is its own small trap.
    _WHEEL_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")
    _KEY_SEQUENCES = ("<Prior>", "<Next>", "<Up>", "<Down>", "<Home>", "<End>")

    def _bind_wheel(self, _event: tk.Event) -> None:
        for sequence in self._WHEEL_SEQUENCES:
            self._canvas.bind_all(sequence, self._on_wheel)
        for sequence in self._KEY_SEQUENCES:
            self._canvas.bind_all(sequence, self._on_key)

    def _unbind_wheel(self, _event: tk.Event) -> None:
        for sequence in self._WHEEL_SEQUENCES + self._KEY_SEQUENCES:
            self._canvas.unbind_all(sequence)

    def _on_key(self, event: tk.Event) -> None:
        """Scroll with the keys, unless a text field wants them.

        Up/Down inside an Entry or a Combobox belong to that widget - moving
        the page instead would make editing a value feel broken.
        """
        # Widgets that own their own arrow/page navigation. The machine list
        # is a Treeview living inside a scrollable tab, so without this Up and
        # Down would scroll the page instead of changing the selected row.
        widget = getattr(event, "widget", None)
        owns_keys = isinstance(
            widget,
            (
                tk.Entry, ttk.Entry, ttk.Combobox, tk.Text,
                ttk.Treeview, tk.Listbox, tk.Spinbox, ttk.Spinbox, tk.Scale,
                # Focus-navigating controls: Up/Down/Home/End move the selected
                # tab, radio, checkbox or button - not the page. bind_all made
                # those keys scroll application-wide, so a radio group or the
                # notebook could not be driven from the keyboard at all.
                tk.Button, ttk.Button, tk.Checkbutton, ttk.Checkbutton,
                tk.Radiobutton, ttk.Radiobutton, ttk.Notebook,
                tk.Scrollbar, ttk.Scrollbar, tk.Menubutton, ttk.Menubutton,
            ),
        )
        keysym = getattr(event, "keysym", "")
        if owns_keys and keysym in ("Prior", "Next"):
            return
        if keysym in ("Prior", "Next"):
            self._canvas.yview_scroll(-1 if keysym == "Prior" else 1, "pages")
        elif keysym == "Home" and not owns_keys:
            self._canvas.yview_moveto(0.0)
        elif keysym == "End" and not owns_keys:
            self._canvas.yview_moveto(1.0)
        elif keysym in ("Up", "Down") and not owns_keys:
            self._canvas.yview_scroll(-1 if keysym == "Up" else 1, "units")

    def scroll_into_view(self, widget: tk.Misc) -> None:
        """Bring a widget into the visible area of this tab."""
        try:
            top = widget.winfo_rooty() - self.body.winfo_rooty()
            total = max(self.body.winfo_height(), 1)
        except tk.TclError:
            return
        self._canvas.yview_moveto(max(0.0, min(1.0, top / total)))

    def _on_wheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            # Windows reports multiples of 120; macOS reports small values.
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")
