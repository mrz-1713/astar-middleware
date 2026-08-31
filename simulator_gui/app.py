"""Control panel for the standalone ASTAR SECS/GEM simulator.

The panel exists mainly to make one thing impossible to get wrong: which
side of the link this process is. Two settings decide that, and they are
independent, so they get two separate selectors and a live sentence that
spells out the consequence for both ends:

    connection.role -> equipment or host  (who this pretends to be)
    connection.mode -> passive or active  (who opens the TCP connection)

Unlike the middleware panel, this one owns the runtime: Start runs the
simulator in a background thread inside this process, because a simulator
is a test instrument an operator drives, not a service.
"""

from __future__ import annotations

import argparse
import logging
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Sequence, Tuple

from eap_middleware import netinfo
from eap_middleware.tkwidgets import ScrollableTab
from simulator.cli import configure_logging
from simulator.config import SimulatorConfigError
from simulator.runner import SimulatorRunner

from . import __version__, model

POLL_MS = 500
LOG_LINES_KEPT = 20000
# One tick's worth. A DEBUG-level burst can outrun the UI thread;
# draining a bounded slice keeps the panel responsive and the rest
# simply arrives on the next tick.
LOG_DRAIN_MAX = 4000


class _QueueHandler(logging.Handler):
    """Feed the panel's log pane without touching the file handlers.

    A queue rather than a bounded deque, because the pane is now fed the
    full HSMS trace: with "communication" (and, at DEBUG, "bytestream")
    flowing, a single S6F11 burst is dozens of lines. The reader drains what
    arrived since the last tick and appends it, so nothing is re-rendered
    and the widget itself caps how much is kept.
    """

    def __init__(self, sink: "queue.SimpleQueue[str]") -> None:
        super().__init__()
        self._sink = sink
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.put(self.format(record))
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


class App(tk.Tk):
    """The ASTAR SECS/GEM simulator window."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        super().__init__()
        self.title(f"ASTAR SECS/GEM Simulator {__version__}")
        self.geometry("1080x760")
        self.minsize(900, 620)

        self.config_path: Optional[Path] = None
        self.raw: Dict[str, Any] = model.default_config()
        self.vars: Dict[str, Tuple[Any, tk.Variable]] = {}
        self.role_var = tk.StringVar(value="equipment")
        self.mode_var = tk.StringVar(value="passive")
        self.address_var = tk.StringVar(value="127.0.0.1")
        self.profile_var = tk.StringVar(value=model.profile_ids()[0])
        self.status_var = tk.StringVar(value="no configuration loaded")
        self.runtime_var = tk.StringVar(value="stopped")

        self._log_queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._log_handler: Optional[_QueueHandler] = None
        self._peer_target = ""
        # None until detection finishes. Discovery calls getaddrinfo, which
        # on a Windows host with no reachable DNS server blocks for seconds -
        # and these values are read from widget traces that fire on every
        # keystroke. Detect once, off the UI thread, then cache.
        self._addresses: Optional[List[str]] = None
        self._hosts: List[Any] = []
        self._primary = ""
        self._detect_thread: Optional[threading.Thread] = None
        # Latest listener set requested, and the set the running detection was
        # launched with. When they differ once a run finishes, _poll re-launches
        # so a network-scan result is never overwritten by a slower earlier run.
        self._detect_listeners: List[str] = []
        self._detect_running_listeners: List[str] = []
        self._scan_thread: Optional[threading.Thread] = None
        self._firewall_thread: Optional[threading.Thread] = None
        self._firewall_result: Optional[Any] = None
        self._firewall_error = ""
        self._runner: Optional[SimulatorRunner] = None
        self._runner_thread: Optional[threading.Thread] = None
        self._exit_code: Optional[int] = None

        self._build_ui()
        self._start_address_detection()
        self._load(config_path or self._first_existing_config())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll)

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        for text, command in (
            ("Open…", self._on_open),
            ("Save", self._on_save),
            ("Validate", self._on_validate),
        ):
            ttk.Button(bar, text=text, command=command).pack(
                side="left", padx=(0, 4)
            )
        self.start_button = ttk.Button(
            bar, text="Start", command=self._on_start
        )
        self.start_button.pack(side="left", padx=(12, 4))
        self.stop_button = ttk.Button(
            bar, text="Stop", command=self._on_stop, state="disabled"
        )
        self.stop_button.pack(side="left")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right")

        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.book = book
        self._build_link_tab(book)
        self._build_equipment_tab(book)
        self._build_host_tab(book)
        self._build_advanced_tab(book)
        self._build_log_tab(book)

    def _build_link_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book)
        book.add(scroller, text="Link")
        tab = scroller.body

        role_frame = ttk.LabelFrame(
            tab, text="1. What does this simulator act as?", padding=8
        )
        role_frame.pack(fill="x")
        self._build_choice_group(
            role_frame, self.role_var, model.ROLE_CHOICES
        )

        mode_frame = ttk.LabelFrame(
            tab,
            text="2. Which end opens the TCP connection? (HSMS mode)",
            padding=8,
        )
        mode_frame.pack(fill="x", pady=(8, 0))
        self._build_choice_group(
            mode_frame, self.mode_var, model.MODE_CHOICES
        )

        endpoint = ttk.LabelFrame(tab, text="3. Connection", padding=8)
        endpoint.pack(fill="x", pady=(8, 0))

        # Port and device id first, and always: these are the two settings
        # that genuinely have to match on both machines in either mode.
        numbers = ttk.Frame(endpoint)
        numbers.pack(fill="x")
        self.vars.update(self._build_form(numbers, model.CONNECTION_FIELDS, 2))

        # --- passive: there is no address to choose -----------------------
        # In passive mode this value is handed straight to socket.bind, so
        # 0.0.0.0 already accepts on every adapter. It restricts; it is not a
        # destination. Asking for it up front is what leads an operator to
        # pin one NIC on a multi-homed machine and quietly become unreachable.
        self.listen_frame = ttk.Frame(endpoint)
        self.listen_label = ttk.Label(
            self.listen_frame, text="", wraplength=900, justify="left"
        )
        self.listen_label.pack(anchor="w", pady=(8, 0))
        self.restrict_var = tk.BooleanVar(value=False)
        self.restrict_check = ttk.Checkbutton(
            self.listen_frame,
            text="Restrict to a single network adapter (advanced, rarely needed)",
            variable=self.restrict_var,
            command=self._on_restrict_toggled,
        )
        self.restrict_check.pack(anchor="w", pady=(6, 0))
        self.restrict_note = ttk.Label(
            self.listen_frame,
            text=(
                "Only for a machine that must not answer on one of its "
                "networks. Pinning the wrong adapter makes this simulator "
                "unreachable in a way that looks exactly like a wrong IP "
                "on the other machine."
            ),
            foreground="#555555", wraplength=900, justify="left",
        )
        self.restrict_note.pack(anchor="w", padx=(22, 0))

        # --- the address box, shared by both modes -------------------------
        self.address_frame = ttk.Frame(endpoint)
        self.address_label = ttk.Label(self.address_frame, text="")
        self.address_label.pack(anchor="w")
        # Editable, not readonly: an active dialler needs the peer's IP,
        # which is not one of this machine's own addresses. The list is a
        # shortcut, not a closed set.
        picker = ttk.Frame(self.address_frame)
        picker.pack(anchor="w", fill="x")
        self.address_box = ttk.Combobox(
            picker, textvariable=self.address_var, width=32
        )
        self.address_box.pack(side="left")
        # Entries carry a note ("this pc", "listening on 5051"); strip it as
        # soon as one is chosen so the variable always holds an address.
        self.address_box.bind(
            "<<ComboboxSelected>>",
            lambda _e: self.address_var.set(
                model.address_from_choice(self.address_var.get())
            ),
        )
        self.scan_button = ttk.Button(
            picker, text="Find peers…", command=self._on_scan_network
        )
        self.scan_button.pack(side="left", padx=(6, 0))
        self.address_hint = ttk.Label(
            self.address_frame, text="", foreground="#555555",
            wraplength=900, justify="left",
        )
        self.address_hint.pack(anchor="w")

        summary = ttk.LabelFrame(
            tab, text="Resulting wiring: check this before starting", padding=8
        )
        summary.pack(fill="x", pady=(8, 0))
        self.self_label = ttk.Label(
            summary, text="", wraplength=900, justify="left",
            font=("TkDefaultFont", 11, "bold"),
        )
        self.self_label.pack(anchor="w")
        self.peer_label = ttk.Label(
            summary, text="", wraplength=900, justify="left"
        )
        self.peer_label.pack(anchor="w", pady=(4, 0))
        self.hint_label = ttk.Label(
            summary, text="", wraplength=900, justify="left",
            foreground="#1a5fb4",
        )
        self.hint_label.pack(anchor="w", pady=(4, 0))

        pairing = ttk.LabelFrame(
            tab, text="4. On the middleware machine", padding=8
        )
        pairing.pack(fill="x", pady=(8, 0))
        self.pairing_label = ttk.Label(
            pairing, text="", wraplength=620, justify="left",
            font=("TkDefaultFont", 11, "bold"),
        )
        self.pairing_label.grid(row=0, column=0, sticky="w")
        self.copy_button = ttk.Button(
            pairing, text="Copy address", command=self._on_copy_target
        )
        self.copy_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.firewall_button = ttk.Button(
            pairing,
            text="Allow this port through Windows Firewall",
            command=self._on_open_firewall,
        )
        self.firewall_button.grid(row=1, column=1, sticky="e", pady=(6, 0))
        ttk.Label(
            pairing,
            text=(
                "Windows blocks the inbound port by default, which looks "
                "exactly like a wrong IP from the other machine. The button "
                "asks for administrator rights and adds the rule."
            ),
            foreground="#555555", wraplength=620, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        pairing.columnconfigure(0, weight=1)

        runtime = ttk.LabelFrame(tab, text="Runtime", padding=8)
        runtime.pack(fill="x", pady=(8, 0))
        ttk.Label(
            runtime, textvariable=self.runtime_var, wraplength=900,
            justify="left",
        ).pack(anchor="w")

        # The port belongs in this list: it is half of the address the
        # operator copies to the other machine and half of the firewall rule.
        for variable in (
            self.role_var,
            self.mode_var,
            self.address_var,
            self.vars["connection.port"][1],
        ):
            variable.trace_add("write", self._on_wiring_changed)

    def _build_choice_group(
        self,
        parent: ttk.Widget,
        variable: tk.StringVar,
        choices: Sequence[Tuple[str, str, str]],
    ) -> None:
        """One radio per option, each with its explanation underneath.

        A combobox would fit more neatly and is exactly what made this
        setting easy to skip past, so it is deliberately not used here.
        """
        for row, (value, label, explanation) in enumerate(choices):
            ttk.Radiobutton(
                parent, text=label, value=value, variable=variable
            ).grid(row=row * 2, column=0, sticky="w")
            ttk.Label(
                parent, text=explanation, foreground="#555555"
            ).grid(row=row * 2 + 1, column=0, sticky="w", padx=(22, 0))

    def _build_equipment_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book)
        book.add(scroller, text="Equipment")
        # The notebook child, not the body: book.tab() addresses this widget.
        self.equipment_tab = scroller
        tab = scroller.body

        profile = ttk.LabelFrame(tab, text="Simulated machine", padding=8)
        profile.pack(fill="x")
        ttk.Label(profile, text="Machine profile").pack(anchor="w")
        ttk.Combobox(
            profile, textvariable=self.profile_var,
            values=model.profile_ids(), state="readonly",
        ).pack(fill="x")
        ttk.Label(
            profile,
            text=(
                "Decides which vendor's CEIDs, SVIDs and report layout the "
                "simulator uses. Applies to both roles: a host subscribes "
                "to the same profile's events."
            ),
            foreground="#555555", wraplength=980, justify="left",
        ).pack(anchor="w", pady=(2, 0))

        lot = ttk.LabelFrame(tab, text="Lot generation", padding=8)
        lot.pack(fill="x", pady=(8, 0))
        self.equipment_frame = lot
        self.vars.update(self._build_form(lot, model.EQUIPMENT_FIELDS, 3))
        self.equipment_note = ttk.Label(
            tab, text="", foreground="#a51d2d", wraplength=980,
            justify="left",
        )
        self.equipment_note.pack(anchor="w", pady=(6, 0))

    def _build_host_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book)
        book.add(scroller, text="Host")
        self.host_tab = scroller
        tab = scroller.body

        frame = ttk.LabelFrame(
            tab, text="Opening sequence performed once communicating",
            padding=8,
        )
        frame.pack(fill="x")
        self.host_frame = frame
        self.vars.update(self._build_form(frame, model.HOST_FIELDS, 2))
        self.host_note = ttk.Label(
            tab, text="", foreground="#a51d2d", wraplength=980, justify="left"
        )
        self.host_note.pack(anchor="w", pady=(6, 0))

    def _build_advanced_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book)
        book.add(scroller, text="Advanced")
        tab = scroller.body
        for title, fields in (
            ("Logging", model.LOGGING_FIELDS),
            ("Reconnect / restart", model.RECOVERY_FIELDS),
        ):
            frame = ttk.LabelFrame(tab, text=title, padding=8)
            frame.pack(fill="x", pady=(0, 8))
            self.vars.update(self._build_form(frame, fields, 3))

    def _build_log_tab(self, book: ttk.Notebook) -> None:
        tab = ttk.Frame(book, padding=6)
        book.add(tab, text="Run log")
        self.log_text = tk.Text(tab, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(
            tab, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    def _build_form(
        self,
        parent: ttk.Widget,
        fields: Sequence[model.Field],
        columns: int = 2,
        start_row: int = 0,
        start_column: int = 0,
    ) -> Dict[str, Tuple[Any, tk.Variable]]:
        created: Dict[str, Tuple[Any, tk.Variable]] = {}
        for index, (path, label, kind) in enumerate(fields):
            row, column = divmod(index, columns)
            cell = ttk.Frame(parent)
            cell.grid(
                row=start_row + row, column=start_column + column,
                sticky="ew", padx=4, pady=2,
            )
            parent.columnconfigure(start_column + column, weight=1)
            if kind == "bool":
                variable: tk.Variable = tk.BooleanVar(value=False)
                ttk.Checkbutton(
                    cell, text=label, variable=variable
                ).pack(anchor="w")
            else:
                variable = tk.StringVar()
                ttk.Label(cell, text=label).pack(anchor="w")
                if isinstance(kind, (tuple, list)):
                    ttk.Combobox(
                        cell, textvariable=variable, values=list(kind),
                        state="readonly",
                    ).pack(fill="x")
                else:
                    ttk.Entry(cell, textvariable=variable).pack(fill="x")
            created[path] = (kind, variable)
        return created

    # -- role-driven presentation ----------------------------------------

    def _start_address_detection(self, listeners: Sequence[str] = ()) -> None:
        """Find this machine's adapters without blocking the window.

        The panel is usable while this runs: the port, the mode and the role
        do not depend on it. Only the pick-list and the "connect here" line
        wait, and both say so rather than appearing empty or wrong.
        """

        listeners = list(listeners)
        self._detect_listeners = listeners
        if self._detect_thread is not None and self._detect_thread.is_alive():
            # A run is already in flight. The newest listener set wins: _poll
            # re-launches with it once the current run finishes, so a scan
            # result is never silently discarded by a slower earlier run.
            return
        self._launch_detection(listeners)

    def _launch_detection(self, listeners: Sequence[str]) -> None:
        """Start one detection run with the given listeners.

        `listeners` is frozen at launch so the worker never reads a Tk variable
        or a list another thread is mutating.
        """
        frozen = list(listeners)
        self._detect_running_listeners = frozen
        port = _as_int(self.vars["connection.port"][1].get())

        def detect() -> None:
            primary = netinfo.primary_ipv4()
            hosts = netinfo.discover_hosts(port=port, listeners=frozen)
            addresses = netinfo.local_ipv4_addresses()
            # Assigned last, and as a pair: _poll treats a non-None
            # _addresses as "detection finished", so _primary must already
            # be in place by then.
            self._primary = primary
            self._hosts = hosts
            self._addresses = addresses

        self._detect_thread = threading.Thread(
            target=detect, name="SimulatorGuiAddressScan", daemon=True
        )
        self._detect_thread.start()

    @property
    def _detecting(self) -> bool:
        return self._addresses is None

    def _known_addresses(self) -> List[str]:
        return list(self._addresses or ())

    def _on_wiring_changed(self, *_args: object) -> None:
        self._refresh_wiring()

    def _refresh_wiring(self) -> None:
        role = self.role_var.get()
        mode = self.mode_var.get()
        self.address_label.configure(text=model.address_label(mode))
        self.address_hint.configure(text=model.address_hint(mode))
        self._refresh_endpoint(mode)
        self_line, peer_line = model.wiring_lines(
            role, mode, self.address_var.get(),
            self.vars["connection.port"][1].get(),
            self.vars["connection.device_id"][1].get(),
        )
        self.self_label.configure(text=self_line)
        self.peer_label.configure(text=peer_line)
        self.hint_label.configure(text=model.peer_middleware_hint(role, mode))
        self._refresh_pairing(mode)
        self._apply_role_visibility(role)

    def _refresh_endpoint(self, mode: str) -> None:
        """Show only the address control the selected mode actually needs.

        Passive: nothing to choose. The value goes to socket.bind, so the
        default already accepts on every adapter; the box appears only if
        the operator explicitly asks to restrict it.

        Active: the peer's address is a real, required decision.
        """
        if mode == "passive":
            self.address_frame.pack_forget()
            self.listen_frame.pack(fill="x")
            if self._detecting:
                self.listen_label.configure(
                    text="Detecting this machine's network adapters…",
                    foreground="#555555",
                )
            else:
                self.listen_label.configure(
                    text=model.listening_summary(
                        mode, self.address_var.get(), self._known_addresses()
                    ),
                    foreground="#000000",
                )
            if self.restrict_var.get():
                self.address_frame.pack(fill="x", pady=(6, 0), padx=(22, 0))
        else:
            self.listen_frame.pack_forget()
            self.address_frame.pack(fill="x", pady=(8, 0))

        if mode == "passive":
            choices = model.address_choices(mode, self._known_addresses())
        else:
            # Dialling out: offer every host we know about, each labelled so
            # "this pc" cannot be picked by accident.
            choices = model.discovery_choices(mode, self._hosts) or (
                model.address_choices(mode, self._known_addresses())
            )
        self.address_box.configure(values=choices)
        self.scan_button.configure(
            state="disabled"
            if (self._scan_thread is not None and self._scan_thread.is_alive())
            else "normal"
        )

    def _on_restrict_toggled(self) -> None:
        """Turning the restriction off must clear it, not just hide it.

        A hidden box still holding a pinned adapter is how a config ends up
        restricted by a setting nobody can see.
        """
        if not self.restrict_var.get():
            self.address_var.set(netinfo.ALL_INTERFACES)
        elif model.binds_every_adapter(self.address_var.get()):
            addresses = self._known_addresses()
            if addresses:
                self.address_var.set(addresses[0])
        self._refresh_wiring()

    def _refresh_pairing(self, mode: str) -> None:
        """The address:port to enter on the other machine."""
        if self._detecting and mode == "passive":
            self.pairing_label.configure(
                text="Working out this machine's address…",
                foreground="#555555",
            )
            self._peer_target = ""
            self.copy_button.configure(state="disabled")
            self.firewall_button.configure(state="normal")
            return

        target = model.peer_target(
            mode,
            self.address_var.get(),
            self.vars["connection.port"][1].get(),
            self._primary,
        )
        self._peer_target = target
        if target:
            self.pairing_label.configure(
                text=f"Point the middleware at   {target}",
                foreground="#1a5fb4",
            )
        elif mode == "passive":
            self.pairing_label.configure(
                text=(
                    "No network adapter found: this machine has no address "
                    "another machine could reach."
                ),
                foreground="#a51d2d",
            )
        else:
            self.pairing_label.configure(
                text=(
                    "This simulator dials out, so the peer must listen. "
                    "Nothing to enter on the other machine."
                ),
                foreground="#555555",
            )
        self.copy_button.configure(state="normal" if target else "disabled")
        self.firewall_button.configure(
            state="normal" if mode == "passive" else "disabled"
        )

    def _apply_role_visibility(self, role: str) -> None:
        """Grey out whichever half of the settings does not apply.

        Leaving both editable is what lets an operator tune lot settings
        for twenty minutes on a simulator running as a host.
        """
        is_host = role == "host"
        self._set_state(self.equipment_frame, disabled=is_host)
        self._set_state(self.host_frame, disabled=not is_host)
        self.equipment_note.configure(
            text=(
                "Role is HOST: these lot settings are ignored. A host "
                "receives events, it does not produce them."
                if is_host else ""
            )
        )
        self.host_note.configure(
            text=(
                "Role is EQUIPMENT: this opening sequence is ignored. The "
                "peer host performs it against this simulator."
                if not is_host else ""
            )
        )
        self.book.tab(self.equipment_tab, text=(
            "Equipment (not used)" if is_host else "Equipment"
        ))
        self.book.tab(self.host_tab, text=(
            "Host" if is_host else "Host (not used)"
        ))

    def _set_state(self, parent: tk.Misc, disabled: bool) -> None:
        state = "disabled" if disabled else "normal"
        for child in parent.winfo_children():
            for widget in (child, *child.winfo_children()):
                try:
                    widget.configure(**{"state": state})
                except tk.TclError:
                    pass

    # -- config plumbing --------------------------------------------------

    def _first_existing_config(self) -> Optional[Path]:
        return next(
            (path for path in model.candidate_config_paths() if path.is_file()),
            None,
        )

    def _load(self, path: Optional[Path]) -> None:
        if path is not None:
            try:
                self.raw = model.load_yaml(path)
            except (OSError, SimulatorConfigError) as exc:
                messagebox.showerror("Open failed", str(exc))
                return
            self.config_path = path
            self.status_var.set(str(path))
        else:
            self.status_var.set("new configuration (not saved yet)")
        self._refresh_forms()

    def _refresh_forms(self) -> None:
        connection = self.raw.get("connection", {})
        self.role_var.set(str(connection.get("role", "equipment")))
        self.mode_var.set(str(connection.get("mode", "passive")))
        address = str(connection.get("address", "127.0.0.1"))
        self.address_var.set(address)
        # A saved file that pins one adapter must arrive with the advanced
        # box already ticked and open. Hiding it would leave the panel
        # claiming it accepts on every adapter while the config says one.
        self.restrict_var.set(
            self.mode_var.get() == "passive"
            and not model.binds_every_adapter(address)
        )
        self.profile_var.set(
            str(
                model.get_path(self.raw, "simulation.profile")
                or model.profile_ids()[0]
            )
        )
        defaults = model.default_config()
        for path, (kind, variable) in self.vars.items():
            value = model.get_path(self.raw, path)
            if value is None:
                value = model.get_path(defaults, path)
            if value is None:
                value = _empty_for(kind)
            variable.set(model.format_value(kind, value))
        self._refresh_wiring()

    def _collect_forms(self) -> None:
        mode = self.mode_var.get()
        address = self.address_var.get().strip()
        model.set_path(self.raw, "connection.role", self.role_var.get())
        model.set_path(self.raw, "connection.mode", mode)
        model.set_path(self.raw, "connection.address", address)
        # Derived, not typed in: see model.requires_external_bind. Keeping
        # this a side effect of the address the operator already picked is
        # what keeps the "Resulting wiring" summary and Start from ever
        # disagreeing about whether this listener reaches other machines.
        model.set_path(
            self.raw,
            "connection.allow_external_bind",
            model.requires_external_bind(mode, address),
        )
        model.set_path(self.raw, "simulation.profile", self.profile_var.get())
        for path, (kind, variable) in self.vars.items():
            try:
                model.set_path(
                    self.raw, path, model.parse_value(kind, variable.get())
                )
            except ValueError as exc:
                raise SimulatorConfigError(f"{path}: {exc}") from exc
        self.raw = model.strip_inapplicable(self.raw)

    def _validated(self) -> Optional[Any]:
        try:
            self._collect_forms()
            return model.validate(self.raw, self.config_path)
        except SimulatorConfigError as exc:
            messagebox.showerror("Configuration error", str(exc))
            return None

    def _on_validate(self) -> None:
        config = self._validated()
        if config is None:
            return
        messagebox.showinfo(
            "Configuration valid",
            f"{config.connection.describe_self()}\n\n"
            f"{config.connection.describe_peer()}",
        )

    def _on_open(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open simulator configuration",
            filetypes=[("YAML", "*.yaml *.yml"), ("All", "*.*")],
        )
        if chosen:
            self._load(Path(chosen))

    def _on_save(self) -> None:
        if self._validated() is None:
            return
        path = self.config_path
        if path is None:
            chosen = filedialog.asksaveasfilename(
                title="Save simulator configuration",
                defaultextension=".yaml",
                filetypes=[("YAML", "*.yaml *.yml")],
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            model.save_yaml(path, self.raw)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.config_path = path
        self.status_var.set(f"saved {path}")

    # -- pairing helpers ---------------------------------------------------

    def _on_scan_network(self) -> None:
        """Find which host on our networks is listening on the HSMS port."""
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        port = _as_int(self.vars["connection.port"][1].get())
        if not 1 <= port <= 65535:
            messagebox.showerror(
                "Set the port first",
                "Enter the HSMS port before searching. The search looks for "
                "a host that is listening on it.",
            )
            return
        self.scan_button.configure(state="disabled")
        self.status_var.set(f"looking up network interfaces for a listener on {port}…")

        # local_interfaces() shells out (PowerShell on Windows) and can block
        # for seconds, so it runs in the worker - not on the UI thread, where
        # it used to freeze the window. Reading a Tk variable off the main
        # thread also raises, so the worker touches only `port` and `networks`.
        def run() -> None:
            networks = model.scan_networks(netinfo.local_interfaces())
            self._scan_networks = networks
            self._scan_result = []
            if networks:
                self._scan_result = netinfo.scan_for_listeners(networks, port)

        self._scan_networks: List[Any] = []
        self._scan_result: List[str] = []
        self._scan_thread = threading.Thread(
            target=run, name="SimulatorGuiPeerScan", daemon=True
        )
        self._scan_thread.start()
        self.after(POLL_MS, self._poll_scan)

    def _poll_scan(self) -> None:
        thread = self._scan_thread
        if thread is not None and thread.is_alive():
            self.after(POLL_MS, self._poll_scan)
            return
        self._scan_thread = None
        found = getattr(self, "_scan_result", [])
        self.status_var.set(
            f"found {len(found)} listener(s): " + ", ".join(found)
            if found else "no listener found on that port"
        )
        # Back on the UI thread, so reading the port here is safe.
        self._start_address_detection(listeners=found)
        self._refresh_wiring()

    def _on_copy_target(self) -> None:
        """Put address:port on the clipboard so it is never retyped."""
        if not self._peer_target:
            return
        self.clipboard_clear()
        self.clipboard_append(self._peer_target)
        self.status_var.set(f"copied {self._peer_target} to the clipboard")

    def _on_open_firewall(self) -> None:
        port = _as_int(self.vars["connection.port"][1].get())
        if not 1 <= port <= 65535:
            messagebox.showerror(
                "Set the port first",
                "Enter the HSMS port before opening the firewall. The rule "
                "is created for that exact port.",
            )
            return
        if sys.platform != "win32":
            messagebox.showinfo(
                "Windows only",
                "Firewall rules are added on Windows. On this platform, open "
                f"inbound TCP {port} however this host does it.",
            )
            return
        if not messagebox.askyesno(
            "Open the firewall?",
            f"Add an inbound Windows Firewall rule allowing TCP {port}?\n\n"
            "Windows will ask for administrator rights.",
        ):
            return
        if self._firewall_thread is not None and self._firewall_thread.is_alive():
            return
        self.firewall_button.configure(state="disabled")
        self.status_var.set(f"waiting for the administrator prompt for TCP {port}…")
        self._firewall_result = None

        # Off the UI thread: this blocks for as long as the UAC prompt is on
        # screen, which is however long the operator takes to answer it.
        def run() -> None:
            try:
                self._firewall_result = subprocess.run(
                    list(model.firewall_argv(port)),
                    capture_output=True, text=True,
                )
            except OSError as exc:
                self._firewall_error = str(exc) or type(exc).__name__

        self._firewall_error = ""
        self._firewall_thread = threading.Thread(
            target=run, name="SimulatorGuiFirewall", daemon=True
        )
        self._firewall_thread.start()
        self.after(POLL_MS, lambda: self._poll_firewall(port))

    def _poll_firewall(self, port: int) -> None:
        thread = self._firewall_thread
        if thread is not None and thread.is_alive():
            self.after(POLL_MS, lambda: self._poll_firewall(port))
            return
        self._firewall_thread = None
        self.firewall_button.configure(state="normal")
        if self._firewall_error:
            messagebox.showerror("Could not run PowerShell", self._firewall_error)
            self._firewall_error = ""
            return
        completed = self._firewall_result
        if completed is None:
            return
        if completed.returncode == 0:
            self.status_var.set(f"inbound TCP {port} allowed through the firewall")
            messagebox.showinfo(
                "Firewall rule added", f"Inbound TCP {port} is now allowed."
            )
        else:
            # A declined UAC prompt lands here too, which is a choice rather
            # than a fault worth a stack trace.
            self.status_var.set(f"inbound TCP {port} was NOT opened")
            messagebox.showwarning(
                "Firewall rule not added",
                (completed.stderr or completed.stdout or "").strip()
                or "The elevation prompt was declined.",
            )

    # -- runtime -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._runner_thread is not None and self._runner_thread.is_alive()

    def _on_start(self) -> None:
        if self.is_running:
            return
        config = self._validated()
        if config is None:
            return
        self._clear_log()
        self._exit_code = None
        # Same rotating file the CLI writes, so a run started from the panel
        # leaves the same evidence on disk as one started from a shell.
        # configure_logging() uses basicConfig(force=True), which drops every
        # existing root handler - attach the pane's afterwards, not before.
        log_path: Optional[Path] = None
        file_error = ""
        try:
            log_path = configure_logging(config)
        except OSError as exc:
            # A read-only or missing log directory must not stop a run: the
            # pane is still live, and that is what most panel users watch.
            logging.getLogger().setLevel(config.logging.level)
            file_error = str(exc)
        handler = _QueueHandler(self._log_queue)
        logging.getLogger().addHandler(handler)
        self._log_handler = handler
        panel_log = logging.getLogger(__name__)
        if log_path is not None:
            panel_log.info("Log file: %s", log_path)
        else:
            panel_log.warning("No log file (%s); this pane only", file_error)
        panel_log.info(
            "Capturing at %s. The 'communication' logger carries every "
            "SECS message body; DEBUG adds 'bytestream' raw HSMS bytes.",
            config.logging.level,
        )

        runner = SimulatorRunner(config)
        self._runner = runner
        thread = threading.Thread(
            target=self._run, args=(runner,), name="SimulatorGuiRunner",
            daemon=True,
        )
        self._runner_thread = thread
        thread.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(
            f"running as {config.connection.role.upper()} "
            f"(HSMS {config.connection.mode.upper()})"
        )
        self.runtime_var.set(
            "Starting: "
            + (
                "waiting for the middleware to connect…"
                if config.connection.mode == "passive"
                else f"dialling {config.connection.endpoint}…"
            )
        )

    def _run(self, runner: SimulatorRunner) -> None:
        try:
            self._exit_code = runner.run()
        except Exception:
            logging.getLogger(__name__).exception("Simulator failed")
            self._exit_code = 1

    def _on_stop(self) -> None:
        if self._runner is not None:
            self._runner.request_stop()
            # Disable immediately: the teardown takes seconds and a Stop
            # button that stays live invites repeat clicks that do nothing.
            self.stop_button.configure(state="disabled")
            self.status_var.set("stopping: closing the link…")
            self.runtime_var.set("Stopping. Waiting for the HSMS link to close…")

    def _finish_stop(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        self._runner = None
        self._runner_thread = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        code = self._exit_code
        self.status_var.set(
            "stopped" if not code else f"stopped with exit code {code}"
        )

    def _poll(self) -> None:
        thread = self._detect_thread
        if thread is not None and not thread.is_alive():
            # Detection finished: adopt the results once, then stop checking.
            self._detect_thread = None
            if self._addresses is None:
                self._addresses = []
            self._refresh_wiring()
            # A scan finished while this run was in flight: its listener set
            # was parked. Run once more with the newest set so the operator's
            # scan result shows up instead of being overwritten.
            if self._detect_listeners != self._detect_running_listeners:
                self._launch_detection(self._detect_listeners)
        if self._runner_thread is not None and not self._runner_thread.is_alive():
            self._finish_stop()
        self.runtime_var.set(self._runtime_line())
        self._refresh_log()
        self.after(POLL_MS, self._poll)

    def _runtime_line(self) -> str:
        if not self.is_running:
            return "Stopped. Press Start to bring the link up."
        runner = self._runner
        simulator = runner.simulator if runner is not None else None
        if simulator is None:
            return "Starting…"
        try:
            state = simulator.communication_state.current.name
        except Exception:
            state = "unknown"
        line = (
            f"{self.role_var.get().upper()}: "
            f"{model.link_state_sentence(state, self.mode_var.get())}"
        )
        received = getattr(simulator, "events_received", None)
        if received is not None:
            line += (
                f" | events received: {received}"
                f" | alarms: {getattr(simulator, 'alarms_received', 0)}"
            )
            name = getattr(simulator, "last_event_name", "")
            ceid = getattr(simulator, "last_event_ceid", None)
            if ceid is not None:
                line += f" | last: CEID {ceid} ({name or 'unmapped'})"
        return line

    def _clear_log(self) -> None:
        while True:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                break
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _refresh_log(self) -> None:
        lines = []
        while len(lines) < LOG_DRAIN_MAX:
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        at_end = self.log_text.yview()[1] >= 0.999
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        # Tk keeps an implicit final newline, so the last index sits one line
        # past the text that was actually inserted.
        kept = int(self.log_text.index("end-1c").split(".")[0]) - 1
        excess = kept - LOG_LINES_KEPT
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        # Only follow the tail when the operator is already at it: yanking the
        # view back mid-scroll makes the trace impossible to read.
        if at_end:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        """Shut the runner down without freezing the window.

        secsgem's disable() tears down sockets and joins receiver threads,
        which takes seconds. Joining it here blocked the UI thread for up to
        ten of them, so the window stopped repainting and looked hung at
        exactly the moment it was asked to close.
        """
        if not self.is_running:
            self.destroy()
            return
        if not messagebox.askyesno(
            "Simulator still running",
            "The simulator is running. Stop it and close?",
        ):
            return
        self._on_stop()
        self._await_stop(deadline=time.monotonic() + 15.0)

    def _await_stop(self, deadline: float) -> None:
        thread = self._runner_thread
        if thread is None or not thread.is_alive() or time.monotonic() >= deadline:
            self.destroy()
            return
        self.status_var.set("stopping: waiting for the link to close…")
        self.after(POLL_MS, lambda: self._await_stop(deadline))


def _as_int(value: Any, default: int = 0) -> int:
    """Read a port out of a widget without raising on a half-typed value."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _empty_for(kind: Any) -> Any:
    if kind == "bool":
        return False
    if kind in ("int", "float"):
        return 0
    return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="AstarSimulatorGui")
    parser.add_argument("--config", help="path to the simulator YAML")
    args = parser.parse_args(argv)
    App(Path(args.config) if args.config else None).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
