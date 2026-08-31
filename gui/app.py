"""Passive desktop client for the always-running ASTAR EAP service."""

from __future__ import annotations

import argparse
import copy
import logging
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from eap_middleware.config import ConfigError, service_config_from_dict
from eap_middleware.control import (
    StaleConfigError,
    file_revision,
    load_status,
    save_config_atomic,
    submit_command,
)

from eap_middleware import netinfo
from eap_middleware.tkwidgets import ScrollableTab

from . import __version__, model

POLL_MS = 1000
LOG_LINES_KEPT = 5000
LOG_SOURCE_ALL = "Everything (all machines)"
# Enough for LOG_LINES_KEPT lines of a wide DEBUG record. Bounding the read
# matters because this runs on every poll and the file rotates at 20 MB.
LOG_TAIL_BYTES = 2_000_000


def _tail_lines(path: Path, limit: int) -> List[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    # The seek can land mid-character and mid-line; errors="replace" handles
    # the first and dropping the leading partial line handles the second.
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(data) == LOG_TAIL_BYTES and lines:
        lines = lines[1:]
    return lines[-limit:]


def candidate_config_paths() -> List[Path]:
    return model.candidate_config_paths()


class App(tk.Tk):
    """The ASTAR EAP control window."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        super().__init__()
        self.title(f"ASTAR EAP Control {__version__}")
        self.geometry("1280x820")
        self.minsize(980, 640)
        self.config_path: Optional[Path] = None
        self.loaded_revision = ""
        self.pending_revision = ""
        self.pending_commands: Dict[str, str] = {}
        self.raw: Dict[str, Any] = {}
        self.status: Dict[str, Any] = {}
        self.vars: Dict[str, Tuple[Any, tk.Variable]] = {}
        self.machine_vars: Dict[str, Tuple[Any, tk.Variable]] = {}
        self.secret_entries: List[ttk.Entry] = []
        self.selected_row: Optional[int] = None
        # Populated off the UI thread: discovery calls getaddrinfo, which on a
        # Windows host with no reachable DNS blocks for seconds, and this ran
        # while the machine form was being built - delaying the whole window.
        self.address_boxes: List[ttk.Combobox] = []
        self._addresses: Optional[List[str]] = None
        self._hosts: List[Any] = []
        self._detect_thread: Optional[threading.Thread] = None
        self._detect_listeners: List[str] = []
        self._detect_running_listeners: List[str] = []
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_result: List[str] = []
        self._scan_note = ""
        # The runtime this panel can own. The middleware normally runs as a
        # Windows service and this panel is a passive client of it - but on a
        # new install no service exists yet, and a panel whose Start button only
        # writes enabled: true to a file nothing reads simply looks broken.
        self._service: Optional[Any] = None
        self._service_busy = ""
        self._service_error = ""
        self._service_thread: Optional[threading.Thread] = None
        self._close_deadline: Optional[float] = None
        self._probe_thread: Optional[threading.Thread] = None
        self._probe_result: Optional[Any] = None
        self._probe_error = ""

        self._build_ui()
        self._start_address_detection()
        self._load(config_path or self._first_existing_config())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll_status)

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        for text, command in (
            ("Open…", self._on_open),
            ("Save", self._on_save),
            ("Validate", self._on_validate),
            ("Enable all", self._on_start_all),
            ("Disable all", self._on_stop_all),
        ):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 4))
        self.show_secrets = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar,
            text="Show secrets",
            variable=self.show_secrets,
            command=self._apply_secret_masking,
        ).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="no config loaded")
        self.log_source_var = tk.StringVar(value=LOG_SOURCE_ALL)
        ttk.Label(bar, textvariable=self.status_var).pack(side="right")

        self._build_service_bar()

        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_machines_tab(book)
        self._build_upstream_tab(book)
        self._build_service_tab(book)
        self._build_log_tab(book)

    def _build_service_bar(self) -> None:
        """Say what is actually collecting data, and let it be started.

        Without this the panel gives no way to tell a working install from
        a dead one: every runtime column reads "-" whether the service is
        stopped or merely idle.
        """
        bar = ttk.Frame(self, padding=(6, 0, 6, 6))
        bar.pack(fill="x")
        self.service_label = ttk.Label(
            bar, text="", wraplength=820, justify="left"
        )
        self.service_label.pack(side="left", fill="x", expand=True)
        self.stop_service_button = ttk.Button(
            bar, text="Stop service", command=self._on_stop_service
        )
        self.stop_service_button.pack(side="right")
        self.run_service_button = ttk.Button(
            bar, text="Run service here", command=self._on_run_service
        )
        self.run_service_button.pack(side="right", padx=(0, 4))
        self._refresh_service_bar()

    def _refresh_service_bar(self) -> None:
        state, sentence = model.service_state(
            self._data_dir(), owned=self._service is not None,
            busy=self._service_busy,
        )
        if self._service_error:
            sentence = f"{sentence}\n{self._service_error}"
        colours = {
            model.SERVICE_LOCAL: "#1a7f37",
            model.SERVICE_EXTERNAL: "#1a7f37",
            model.SERVICE_BUSY: "#555555",
            model.SERVICE_STOPPED: "#9a6700",
        }
        self.service_label.configure(
            text=sentence,
            foreground="#a51d2d" if self._service_error else colours[state],
        )
        self.run_service_button.configure(
            state="normal" if state == model.SERVICE_STOPPED else "disabled"
        )
        self.stop_service_button.configure(
            state="normal" if state == model.SERVICE_LOCAL else "disabled"
        )

    def _service_busy_now(self, verb: str) -> None:
        self._service_busy = verb
        self._service_error = ""
        self._refresh_service_bar()

    def _on_run_service(self) -> None:
        """Start the middleware inside this window.

        Everything heavy happens on a worker thread: start() takes the
        single-instance lock, opens the outboxes and dials every enabled
        machine, which is seconds of work that would freeze the window.
        """
        if self._service is not None or self._service_busy:
            return
        config = self._validated_config()
        if config is None:
            return
        if not self._save():
            return
        missing = model.simulator_unavailable_machines(config)
        if missing:
            messagebox.showerror(
                "No simulator installed on this machine",
                f"{', '.join(missing)} " 
                + ("is" if len(missing) == 1 else "are")
                + " set to Runtime mode 'simulated', which runs a simulator "
                "INSIDE the middleware, and this install does not have one.\n\n"
                "If you are connecting to a simulator running on another "
                "machine, that is Runtime mode 'real' with its address in "
                "'Equipment host / IP'. 'simulated' ignores the address "
                "entirely.",
            )
            return
        problems = model.writable_path_problems(config)
        if problems:
            messagebox.showerror(
                "Cannot write to the service folders",
                "The service needs to write to these, and cannot:\n\n"
                + "\n".join(problems)
                + "\n\nThe installer creates them as Administrator, so a "
                "panel started normally may not be able to write there. "
                "Either re-run SETUP.bat from this build (it now grants "
                "access), or right-click the ASTAR EAP Control shortcut and "
                "choose 'Run as administrator'.",
            )
            return
        enabled = [m for m in config.machines if m.enabled]
        if not enabled and not messagebox.askyesno(
            "No machines enabled",
            "No machine is enabled, so the service will start and collect "
            "nothing.\n\nSelect a machine and press Enable first.\n\n"
            "Start the service anyway?",
        ):
            return
        self._service_busy_now("starting")
        config_path = str(self.config_path) if self.config_path else None

        def run() -> None:
            service = None
            try:
                from eap_middleware.logging_setup import configure_logging
                from eap_middleware.service import EapMiddlewareService

                configure_logging(config.logging, config.paths.log_dir)
                service = EapMiddlewareService(config, config_path=config_path)
                service.start()
                self._service = service
            except Exception as exc:  # surfaced in the bar, never a crash
                self._service = None
                self._service_error = f"Could not start: {exc}"
                # start() takes the single-instance lock before anything else,
                # so a failure after that point leaves it held with no owner
                # and EVERY later attempt fails on the lock instead of the
                # original cause. Unwind before dropping the reference.
                if service is not None:
                    try:
                        service.stop()
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "Cleanup after a failed start also failed",
                            exc_info=True,
                        )
            finally:
                self._service_busy = ""

        self._service_thread = threading.Thread(
            target=run, name="EapGuiServiceStart", daemon=True
        )
        self._service_thread.start()

    def _on_stop_service(self) -> None:
        service = self._service
        if service is None or self._service_busy:
            return
        self._service_busy_now("stopping")

        def run() -> None:
            # The service owns the budget (STOP_TIMEOUT_SEC): it shares one
            # deadline across every machine and worker instead of spending a
            # fresh 10s on each join. Releasing the button is in `finally`, so
            # the button is disabled for exactly as long as the teardown takes
            # - which is why the teardown has to be bounded rather than the
            # button separately timed out.
            try:
                service.stop()
            except Exception as exc:
                self._service_error = f"Stop reported: {exc}"
                # Keep the handle. stop() may have raised partway through, so
                # the service can still be running - holding the single-instance
                # lockfile and its HSMS/CSV threads. Dropping _service here
                # orphaned it: the bar then read the live ticks as an external
                # Windows service with both "Stop service" and "Run service
                # here" disabled, and the only recovery was closing the window
                # (which killed the daemon threads out from under the service).
                # A retried stop() is idempotent, so leave the handle for it.
            else:
                self._service = None
            finally:
                self._service_busy = ""

        self._service_thread = threading.Thread(
            target=run, name="EapGuiServiceStop", daemon=True
        )
        self._service_thread.start()

    def _build_machines_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book, padding=6)
        book.add(scroller, text="Machines")
        tab = scroller.body
        columns = (
            "endpoint", "name", "profile", "runtime", "address", "enabled",
            "hsms", "gem", "https", "simulator",
        )
        headings = {
            "endpoint": ("Endpoint", 85),
            "name": ("Display name", 145),
            "profile": ("Profile", 135),
            "runtime": ("Mode", 70),
            "address": ("Host:port", 125),
            "enabled": ("Enabled", 60),
            "hsms": ("HSMS", 80),
            "gem": ("GEM", 100),
            "https": ("HTTP/queue/dead", 105),
            "simulator": ("Simulator", 80),
        }
        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=9)
        for key in columns:
            title, width = headings[key]
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_machine)

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(6, 0))
        for text, command in (
            ("Add", self._on_add_machine),
            ("Duplicate", self._on_duplicate_machine),
            ("Remove", self._on_remove_machine),
            # Named for what they do: these edit the configuration. What
            # actually connects to a machine is the service, run from the
            # bar above.
            ("Enable", self._on_start),
            ("Disable", self._on_stop),
            ("Restart", self._on_restart),
            ("Test connection", self._on_test_connection),
            ("Test Linkstuffs", self._on_test_linkstuffs),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 4))

        detail = ttk.LabelFrame(tab, text="Selected machine", padding=6)
        detail.pack(fill="both", expand=True, pady=(8, 0))
        # Sub-tabs, not one flat grid. Ten of these forty-odd settings decide
        # whether a link comes up; the rest have working defaults. Shown all
        # at once, the two are indistinguishable.
        detail_book = ttk.Notebook(detail)
        detail_book.pack(fill="both", expand=True)
        self.machine_vars = {}
        for title, group, columns in model.MACHINE_GROUPS:
            page = ttk.Frame(detail_book, padding=6)
            detail_book.add(page, text=title)
            fields = [
                (
                    path,
                    label,
                    model.profile_ids() if path == "machine_profile" else kind,
                )
                for path, label, kind in group
            ]
            self.machine_vars.update(
                self._build_form(page, fields, columns=columns)
            )

    def _build_upstream_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book, padding=6)
        book.add(scroller, text="Upstream defaults")
        tab = scroller.body
        for title, fields in (
            ("Linkstuffs HTTPS defaults", model.HTTP_FIELDS),
            ("Linkstuffs MQTT gateway", model.MQTT_FIELDS),
            ("Legacy Tool Data API", model.LEGACY_FIELDS),
        ):
            frame = ttk.LabelFrame(tab, text=title, padding=6)
            frame.pack(fill="x", pady=(0, 8))
            self.vars.update(self._build_form(frame, fields, columns=3))

    def _build_service_tab(self, book: ttk.Notebook) -> None:
        scroller = ScrollableTab(book, padding=6)
        book.add(scroller, text="Service settings")
        tab = scroller.body
        for title, fields in (("Paths", model.PATH_FIELDS), ("Runtime", model.RUNTIME_FIELDS)):
            frame = ttk.LabelFrame(tab, text=title, padding=6)
            frame.pack(fill="x", pady=(0, 8))
            self.vars.update(self._build_form(frame, fields, columns=3))

    def _build_log_tab(self, book: ttk.Notebook) -> None:
        tab = ttk.Frame(book, padding=6)
        book.add(tab, text="Logs")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Show:").pack(side="left")
        self.log_source = ttk.Combobox(
            bar, textvariable=self.log_source_var, state="readonly", width=32
        )
        self.log_source.pack(side="left", padx=(4, 12))
        # Everything, unfiltered, is the default: a per-machine middleware.log
        # is written through a filter that can only guess which endpoint an
        # unattributed record belongs to, so the wire trace, the subscription
        # result and the outbox writes are missing from it. This file is the
        # root handler's own output and drops nothing.
        self.log_source["values"] = (LOG_SOURCE_ALL,)
        ttk.Label(
            bar,
            text=(
                "Service settings > Runtime > Log level = DEBUG adds the raw "
                "HSMS bytes and the per-event detail."
            ),
        ).pack(side="left")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        self.log_text = tk.Text(body, wrap="none", state="disabled", height=20)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    def _build_form(
        self, parent: ttk.Widget, fields: Sequence[model.Field], columns: int = 2
    ) -> Dict[str, Tuple[Any, tk.Variable]]:
        created: Dict[str, Tuple[Any, tk.Variable]] = {}
        for index, (path, label, kind) in enumerate(fields):
            row, column = divmod(index, columns)
            cell = ttk.Frame(parent)
            cell.grid(row=row, column=column, sticky="ew", padx=4, pady=2)
            parent.columnconfigure(column, weight=1)
            if kind == "bool":
                variable: tk.Variable = tk.BooleanVar(value=False)
                ttk.Checkbutton(cell, text=label, variable=variable).pack(anchor="w")
            else:
                variable = tk.StringVar()
                ttk.Label(cell, text=label).pack(anchor="w")
                row_frame = ttk.Frame(cell)
                row_frame.pack(fill="x")
                if isinstance(kind, (tuple, list)):
                    ttk.Combobox(
                        row_frame, textvariable=variable, values=list(kind), state="readonly"
                    ).pack(fill="x")
                elif kind == "address":
                    # Editable on purpose: a real tool sits on the equipment
                    # network, not on one of this machine's own addresses.
                    #
                    # Built empty and filled by _poll_status once detection
                    # finishes, so the window opens now rather than after a
                    # DNS timeout.
                    box = ttk.Combobox(row_frame, textvariable=variable)
                    box.pack(side="left", fill="x", expand=True)
                    # Entries carry a note ("this pc", "listening on 5051").
                    # Strip it the moment one is chosen so the variable always
                    # holds an address.
                    box.bind(
                        "<<ComboboxSelected>>",
                        lambda _e, v=variable: v.set(
                            model.address_from_choice(v.get())
                        ),
                    )
                    self.address_boxes.append(box)
                    self.scan_button = ttk.Button(
                        row_frame, text="Find…", width=7,
                        command=self._on_scan_network,
                    )
                    self.scan_button.pack(side="left", padx=(3, 0))
                else:
                    entry = ttk.Entry(row_frame, textvariable=variable)
                    entry.pack(side="left", fill="x", expand=True)
                    if kind == "secret":
                        self.secret_entries.append(entry)
                    if path.endswith(("_dir", "_path")):
                        ttk.Button(
                            row_frame,
                            text="…",
                            width=3,
                            command=lambda p=path, v=variable: self._browse(p, v),
                        ).pack(side="left", padx=(3, 0))
                    elif kind == "mapping":
                        ttk.Button(
                            row_frame,
                            text="Edit…",
                            command=lambda label=label, v=variable: self._edit_mapping(
                                label, v
                            ),
                        ).pack(side="left", padx=(3, 0))
            help_text = model.FIELD_HELP.get(path)
            if help_text:
                ttk.Label(
                    cell, text=help_text, foreground="#555555",
                    wraplength=300, justify="left",
                ).pack(anchor="w")
            created[path] = (kind, variable)
        self._apply_secret_masking()
        return created

    def _edit_mapping(self, label: str, variable: tk.Variable) -> None:
        window = tk.Toplevel(self)
        window.title(label)
        window.geometry("760x480")
        editor = tk.Text(window, wrap="none")
        editor.pack(fill="both", expand=True, padx=8, pady=8)
        editor.insert(
            "1.0",
            yaml.safe_dump(
                model.parse_value("mapping", variable.get()), sort_keys=False
            ),
        )

        def apply() -> None:
            try:
                value = yaml.safe_load(editor.get("1.0", "end")) or {}
                if not isinstance(value, dict):
                    raise ValueError("value must be a YAML mapping")
            except (ValueError, yaml.YAMLError) as exc:
                messagebox.showerror("Invalid mapping", str(exc), parent=window)
                return
            variable.set(model.format_value("mapping", value))
            window.destroy()

        ttk.Button(window, text="Apply", command=apply).pack(pady=(0, 8))

    def _browse(self, path: str, variable: tk.Variable) -> None:
        if path.endswith("event_subscription_path"):
            chosen = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        else:
            chosen = filedialog.askdirectory()
        if chosen:
            variable.set(chosen)

    def _apply_secret_masking(self) -> None:
        mask = "" if self.show_secrets.get() else "•"
        for entry in self.secret_entries:
            entry.configure(show=mask)

    def _first_existing_config(self) -> Optional[Path]:
        found = next(
            (path for path in candidate_config_paths() if path.is_file()), None
        )
        if found is not None:
            return found
        # Nothing configured yet. Seeding from the shipped template is what
        # makes the panel usable on a machine that has only just been
        # installed - the alternative is an empty form with no machines,
        # which is why the installer used to open Notepad instead.
        target = model.seed_target_path()
        if target is None:
            # No install directory anywhere - the installer has not run.
            # Seeding into a fabricated tree would hide that.
            return None
        try:
            return model.seed_config(target)
        except OSError as exc:
            messagebox.showwarning(
                "No configuration yet",
                "Could not create a starter configuration:\n\n"
                f"{exc}\n\nUse Open… to load one.",
            )
            return None

    def _load(self, path: Optional[Path]) -> None:
        if path is None:
            self.raw = {}
            self.loaded_revision = ""
            self.status_var.set("no config found")
        else:
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                messagebox.showerror("Open failed", str(exc))
                return
            if not isinstance(loaded, dict):
                messagebox.showerror("Open failed", "Top-level config must be a mapping")
                return
            self.raw = loaded
            self.config_path = path
            self.loaded_revision = file_revision(path)
            self.status_var.set(str(path))
        self.selected_row = None
        self._refresh_forms()
        self._refresh_machines()

    def _refresh_forms(self) -> None:
        for path, (kind, variable) in self.vars.items():
            variable.set(model.format_value(kind, model.get_path(self.raw, path)))
        self._apply_secret_masking()

    def _collect_forms(self) -> None:
        for path, (kind, variable) in self.vars.items():
            try:
                model.set_path(self.raw, path, model.parse_value(kind, variable.get()))
            except (ValueError, yaml.YAMLError) as exc:
                raise ValueError(f"{path}: {exc}") from exc

    def _on_open(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open middleware config",
            filetypes=[("YAML", "*.yaml *.yml"), ("All", "*.*")],
        )
        if chosen:
            self._load(Path(chosen))

    def _save(self) -> bool:
        config = self._validated_config()
        if config is None:
            return False
        path = self.config_path
        if path is None:
            chosen = filedialog.asksaveasfilename(
                title="Save middleware config", defaultextension=".yaml",
                filetypes=[("YAML", "*.yaml *.yml")],
            )
            if not chosen:
                return False
            path = Path(chosen)
        try:
            self.loaded_revision = save_config_atomic(
                path,
                self.raw,
                expected_revision=self.loaded_revision if self.config_path else None,
            )
        except StaleConfigError as exc:
            messagebox.showerror("Config changed", str(exc))
            return False
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return False
        self.config_path = path
        self.pending_revision = self.loaded_revision
        self.status_var.set(f"saved {path}")
        return True

    def _on_save(self) -> None:
        self._save()

    def _apply_all(self) -> bool:
        try:
            self._collect_forms()
            self._collect_machine_form()
        except (ValueError, yaml.YAMLError) as exc:
            messagebox.showerror("Invalid value", str(exc))
            return False
        return True

    def _validated_config(self) -> Optional[Any]:
        if not self._apply_all():
            return None
        try:
            return service_config_from_dict(self.raw)
        except ConfigError as exc:
            messagebox.showerror("Config error", str(exc))
            return None

    def _on_validate(self) -> None:
        config = self._validated_config()
        if config is not None:
            enabled = sum(machine.enabled for machine in config.machines)
            messagebox.showinfo(
                "Config valid", f"{len(config.machines)} configured, {enabled} enabled."
            )

    def _machines(self) -> List[Dict[str, Any]]:
        return model.machines_of(self.raw)

    def _runtime_status(self, endpoint_id: str) -> Dict[str, Any]:
        machines = self.status.get("machines", {})
        value = machines.get(endpoint_id, {}) if isinstance(machines, dict) else {}
        return value if isinstance(value, dict) else {}

    def _row_values(self, machine: Dict[str, Any]) -> Tuple[Any, ...]:
        endpoint_id = str(machine.get("endpoint_id", ""))
        runtime = self._runtime_status(endpoint_id)
        queue = runtime.get("https_queue", {})
        pending = queue.get("pending", 0) if isinstance(queue, dict) else 0
        dead = queue.get("dead", 0) if isinstance(queue, dict) else 0
        http_status = runtime.get("last_http_status") or "-"
        return (
            endpoint_id,
            machine.get("display_name", ""),
            model.profile_label(str(machine.get("machine_profile", "")), runtime),
            machine.get("runtime_mode", "real"),
            f"{machine.get('host', '')}:{machine.get('port', '')}",
            "yes" if machine.get("enabled", True) else "no",
            runtime.get("hsms_state", "-"),
            runtime.get("gem_state", "-"),
            f"{http_status}/{pending}/{dead}",
            runtime.get("simulator_state", "-"),
        )

    def _refresh_machines(self) -> None:
        selected = self.selected_row
        self.tree.delete(*self.tree.get_children())
        for index, machine in enumerate(self._machines()):
            self.tree.insert("", "end", iid=str(index), values=self._row_values(machine))
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
        self._load_machine_form()

    def _sync_rows(self) -> None:
        for index, machine in enumerate(self._machines()):
            if self.tree.exists(str(index)):
                self.tree.item(str(index), values=self._row_values(machine))

    def _on_select_machine(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if index == self.selected_row:
            return
        if not self._apply_all():
            return
        self.selected_row = index
        self._load_machine_form()
        self._sync_rows()

    def _load_machine_form(self) -> None:
        machines = self._machines()
        if self.selected_row is None and machines:
            self.selected_row = 0
            self.tree.selection_set("0")
        machine = (
            machines[self.selected_row]
            if self.selected_row is not None and self.selected_row < len(machines)
            else {}
        )
        for path, (kind, variable) in self.machine_vars.items():
            variable.set(
                model.format_value(kind, model.machine_value(self.raw, machine, path))
            )
        self._apply_secret_masking()

    def _collect_machine_form(self) -> None:
        machines = self._machines()
        if self.selected_row is None or self.selected_row >= len(machines):
            return
        machine = machines[self.selected_row]
        for path, (kind, variable) in self.machine_vars.items():
            try:
                value = model.parse_value(kind, variable.get())
            except (ValueError, yaml.YAMLError) as exc:
                raise ValueError(f"{machine.get('endpoint_id', '?')}.{path}: {exc}") from exc
            if (
                path == "runtime_mode"
                and value != str(machine.get("runtime_mode", "real"))
                and not messagebox.askyesno(
                    "Switch runtime mode",
                    f"Switch {machine.get('endpoint_id', '?')} to {value}? "
                    "Only this endpoint will restart when the service applies it.",
                )
            ):
                raise ValueError("runtime mode change was cancelled")
            model.set_path(machine, path, value)

    def _on_add_machine(self) -> None:
        if self._apply_all():
            machines = self._machines()
            machines.append(model.new_machine(machines))
            self.selected_row = len(machines) - 1
            self._refresh_machines()

    def _on_duplicate_machine(self) -> None:
        if not self._apply_all() or self.selected_row is None:
            return
        machines = self._machines()
        clone = copy.deepcopy(machines[self.selected_row])
        fresh = model.new_machine(machines)
        clone.update(
            endpoint_id=fresh["endpoint_id"],
            display_name=fresh["display_name"],
            port=fresh["port"],
            enabled=False,
        )
        machines.append(clone)
        self.selected_row = len(machines) - 1
        self._refresh_machines()

    def _on_remove_machine(self) -> None:
        if self.selected_row is None:
            return
        machine = self._machines()[self.selected_row]
        endpoint = str(machine.get("endpoint_id", "?"))
        if messagebox.askyesno(
            "Remove machine",
            f"Remove {endpoint} from configuration? Existing logs, CSVs and outbox remain.",
        ):
            self._machines().pop(self.selected_row)
            self.selected_row = None
            self._refresh_machines()

    def _apply_enabled(self, machine: Dict[str, Any], enabled: bool) -> None:
        """Set the flag on the row AND on the widget that owns it.

        "Enabled" is both a dict key and a checkbox in the machine form, and
        _save re-collects that form on its way to disk - so for the selected
        machine the checkbox is the authority. Writing only the dict was
        silently undone by the save it triggered: the row stayed disabled,
        while Enable still reported success. It also made "Enable all" work
        for every machine except the selected one.
        """
        machine["enabled"] = enabled
        machines = self._machines()
        if (
            self.selected_row is not None
            and self.selected_row < len(machines)
            and machines[self.selected_row] is machine
            and "enabled" in self.machine_vars
        ):
            self.machine_vars["enabled"][1].set(enabled)

    def _set_selected_enabled(self, enabled: bool) -> None:
        if not self._apply_all() or self.selected_row is None:
            return
        machine = self._machines()[self.selected_row]
        self._apply_enabled(machine, enabled)
        if enabled and not self._local_storage_ready(
            {str(machine.get("endpoint_id", ""))}
        ):
            self._apply_enabled(machine, False)
            return
        if self._save():
            self._sync_rows()
            self._note_enable_outcome(enabled, str(machine.get("endpoint_id", "?")))

    def _note_enable_outcome(self, enabled: bool, endpoint: str) -> None:
        """Say what enabling did, which is not the same as what it looks like.

        Enable writes enabled: true and saves. If no service is running,
        nothing acts on it - and the row's runtime columns stay "-", which
        reads exactly like a failure to connect.
        """
        word = "enabled" if enabled else "disabled"
        if self._service is not None or model.service_is_live(self._data_dir()):
            self.status_var.set(f"{endpoint} {word}; the service is applying it")
            return
        self.status_var.set(f"{endpoint} {word} in the configuration")
        if enabled:
            messagebox.showinfo(
                f"{endpoint} enabled",
                f"{endpoint} is now enabled in the configuration.\n\n"
                "No service is running yet, so nothing is collecting from it. "
                "Press 'Run service here' in the bar at the top to start "
                "collecting, or install the Windows service with "
                "scripts\\install_service.ps1.",
            )

    def _on_start(self) -> None:
        self._set_selected_enabled(True)

    def _on_stop(self) -> None:
        self._set_selected_enabled(False)

    def _set_all_enabled(self, enabled: bool) -> None:
        if not self._apply_all():
            return
        affected = [
            str(machine.get("endpoint_id", "?"))
            for machine in self._machines()
            if bool(machine.get("enabled", True)) != enabled
        ]
        if not affected:
            return
        action = "Enable" if enabled else "Disable"
        if not messagebox.askyesno(
            f"{action} all", f"{action} these endpoints?\n\n" + "\n".join(affected)
        ):
            return
        for machine in self._machines():
            if str(machine.get("endpoint_id", "?")) in affected:
                self._apply_enabled(machine, enabled)
        if enabled and not self._local_storage_ready(set(affected)):
            for machine in self._machines():
                if str(machine.get("endpoint_id", "?")) in affected:
                    self._apply_enabled(machine, False)
            return
        if self._save():
            self._sync_rows()
            self.status_var.set(
                f"{len(affected)} machine(s) {'enabled' if enabled else 'disabled'}"
            )

    def _on_start_all(self) -> None:
        self._set_all_enabled(True)

    def _on_stop_all(self) -> None:
        self._set_all_enabled(False)

    def _local_storage_ready(self, endpoint_ids: set[str]) -> bool:
        """Can these machines actually write their CSVs?

        The two failures here are unrelated and used to share one dialog
        titled "Local storage unavailable" - so a configuration complaint
        about Linkstuffs tokens was reported as a disk problem, sending the
        operator to look at a directory that was fine.
        """
        try:
            config = service_config_from_dict(self.raw)
        except ConfigError as exc:
            messagebox.showerror("Configuration is not valid yet", str(exc))
            return False
        for machine in config.machines:
            if machine.endpoint_id not in endpoint_ids:
                continue
            directory = machine.csv_local_dir
            try:
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=directory, delete=True):
                    pass
            except OSError as exc:
                messagebox.showerror(
                    "Local storage unavailable",
                    f"{machine.endpoint_id} cannot write to its CSV folder:\n\n"
                    f"{directory}\n\n{exc}\n\n"
                    "The template's D: drive is often a read-only optical drive. "
                    "Point 'Local CSV dir' somewhere writable, such as "
                    "C:/SECSGEM_EAP/data/csv_in.",
                )
                return False
        return True

    def _submit_selected(self, action: str) -> None:
        if not self._apply_all() or self.selected_row is None:
            return
        endpoint = str(self._machines()[self.selected_row].get("endpoint_id", ""))
        request_id = submit_command(self._data_dir(), action, endpoint)
        self.pending_commands[request_id] = action
        self.status_var.set(f"{action} requested for {endpoint} ({request_id[:8]})")

    def _on_restart(self) -> None:
        self._submit_selected("restart")

    def _on_test_connection(self) -> None:
        """Verify one HSMS link, with or without a service behind us.

        Submitting a command file only works when a service is running to
        consume it. On a fresh install nothing consumes it and the panel
        appears to hang, so probe directly in that case - the same probe
        `test-machine` runs.
        """
        if model.service_is_live(self._data_dir()):
            self._submit_selected("test_connection")
            return
        if not self._apply_all() or self.selected_row is None:
            return
        try:
            target = model.probe_target(self._machines()[self.selected_row])
        except ValueError as exc:
            messagebox.showerror("Check the connection settings", str(exc))
            return
        self._start_probe(target)

    def _start_probe(self, machine: Any) -> None:
        """Run the probe off the UI thread; it blocks for up to the timeout."""
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        self.status_var.set(
            f"testing {machine.endpoint_id} at {machine.host}:{machine.port}…"
        )
        self._probe_result = None
        self._probe_error = ""

        def run() -> None:
            # probe_machine never raises, but importing it can - it pulls in
            # secsgem. An unhandled failure here left _probe_result None and
            # the status stuck on "testing…" with no way to retry.
            try:
                from eap_middleware.probe import probe_machine

                self._probe_result = probe_machine(machine)
            except Exception as exc:
                self._probe_error = str(exc).strip() or type(exc).__name__

        self._probe_thread = threading.Thread(
            target=run, name="EapGuiProbe", daemon=True
        )
        self._probe_thread.start()
        self.after(POLL_MS, self._poll_probe)

    def _poll_probe(self) -> None:
        thread = self._probe_thread
        if thread is not None and thread.is_alive():
            self.after(POLL_MS, self._poll_probe)
            return
        self._probe_thread = None
        result = self._probe_result
        if result is None:
            reason = self._probe_error or "the connection test did not run"
            self._probe_error = ""
            self.status_var.set(f"connection test failed: {reason}")
            messagebox.showerror("Could not test the connection", reason)
            return
        self.status_var.set(result.as_line())
        if result.ok:
            messagebox.showinfo(
                "Connected",
                f"{result.endpoint_id} answered at {result.host}:{result.port}.\n\n"
                f"Identity: {result.identity!r}",
            )
        else:
            messagebox.showerror(
                "Not connected",
                f"{result.endpoint_id} at {result.host}:{result.port}\n\n"
                f"{result.error}\n\n"
                "Check that the peer is started, that the two ends are not "
                "both passive or both active, and that the port is open in "
                "the firewall on the listening machine.",
            )

    def _on_test_linkstuffs(self) -> None:
        self._submit_selected("test_linkstuffs")

    def _start_address_detection(self, listeners: Sequence[str] = ()) -> None:
        """Fill the host pick-list without holding up the window.

        Everything here shells out or resolves names, which on a host
        with no reachable DNS blocks for seconds.
        """
        self._detect_listeners = list(listeners)
        if self._detect_thread is not None and self._detect_thread.is_alive():
            # Two runs racing would both write _addresses, and the slower one
            # wins - discarding a scan result the operator just asked for. The
            # newest request is honoured once the running one finishes (see
            # _adopt_addresses), instead of being dropped outright.
            return
        self._launch_detection(list(self._detect_listeners))

    def _launch_detection(self, listeners: List[str]) -> None:
        frozen = list(listeners)
        self._detect_running_listeners = frozen
        port = self._selected_port()

        def detect() -> None:
            hosts = netinfo.discover_hosts(port=port, listeners=frozen)
            self._hosts = hosts
            self._addresses = [host.label for host in hosts]

        self._detect_thread = threading.Thread(
            target=detect, name="EapGuiAddressScan", daemon=True
        )
        self._detect_thread.start()

    def _selected_port(self) -> int:
        try:
            return int(str(self.machine_vars["port"][1].get()).strip())
        except (KeyError, ValueError):
            return 0

    def _adopt_addresses(self) -> None:
        thread = self._detect_thread
        if thread is None or thread.is_alive():
            return
        self._detect_thread = None
        found = self._addresses or []
        self._addresses = found
        for box in self.address_boxes:
            box.configure(values=found)
        if self._scan_note:
            self.status_var.set(self._scan_note)
            self._scan_note = ""
        # A network scan finished while this run was in flight: its listener
        # set was parked. Run once more with the newest set, otherwise the
        # status bar claims "found N hosts" over a pick-list that never
        # adopted them.
        if self._detect_listeners != self._detect_running_listeners:
            self._launch_detection(list(self._detect_listeners))

    def _on_scan_network(self) -> None:
        """Ask which host on our own networks is listening on this port.

        Narrow by design: one port, only networks this machine is directly
        attached to, capped by netinfo.MAX_SCAN_HOSTS. That is enough to
        name the peer on a local network and is not a general network sweep.
        """
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        port = self._selected_port()
        if not 1 <= port <= 65535:
            messagebox.showerror(
                "Set the port first",
                "Enter the machine's HSMS port before searching. The search "
                "looks for a host that is listening on it.",
            )
            return
        # local_interfaces() shells out to PowerShell, which takes seconds on
        # Windows. Doing it here froze the window before the button even
        # showed as disabled, so the click looked ignored.
        self.scan_button.configure(state="disabled")
        self.status_var.set(f"looking for networks, then hosts listening on {port}…")

        # Scan only. Reading a Tk variable off the main thread raises
        # "main thread is not in main loop", so detection - which reads the
        # port - is started from _poll_scan instead.
        def run() -> None:
            try:
                networks = [
                    i.network for i in netinfo.local_interfaces()
                    if i.network is not None
                ]
                if not networks:
                    self._scan_result = []
                    self._scan_note = (
                        "no network found: this machine is not on any "
                        "network that can be searched"
                    )
                    return
                names = ", ".join(str(n) for n in networks)
                found = netinfo.scan_for_listeners(networks, port)
                self._scan_result = found
                self._scan_note = (
                    f"found {len(found)} host(s) listening on {port}: "
                    + ", ".join(found)
                    if found
                    else f"no host on {names} is listening on {port}"
                )
            except Exception as exc:
                # A dead worker left the previous result in place and said
                # nothing, so the button came back with no explanation.
                self._scan_result = []
                self._scan_note = (
                    f"search failed: {str(exc).strip() or type(exc).__name__}"
                )

        self._scan_thread = threading.Thread(
            target=run, name="EapGuiPeerScan", daemon=True
        )
        self._scan_thread.start()
        self.after(POLL_MS, self._poll_scan)

    def _poll_scan(self) -> None:
        thread = self._scan_thread
        if thread is not None and thread.is_alive():
            self.after(POLL_MS, self._poll_scan)
            return
        self._scan_thread = None
        self.scan_button.configure(state="normal")
        # Back on the UI thread, so reading the port here is safe.
        self._start_address_detection(listeners=getattr(self, "_scan_result", []))

    def _data_dir(self) -> Path:
        configured = model.get_path(self.raw, "paths.control_dir", "")
        if configured:
            return Path(str(configured))
        data_dir = Path(
            str(model.get_path(self.raw, "paths.data_dir", "C:/SECSGEM_EAP/data"))
        )
        return data_dir.parent / "control"

    def _poll_status(self) -> None:
        self._adopt_addresses()
        self._refresh_service_bar()
        self.status = load_status(self._data_dir())
        if self.pending_revision:
            if self.status.get("configuration_revision") == self.pending_revision:
                self.status_var.set("configuration applied by service")
                self.pending_revision = ""
            elif model.service_is_live(self._data_dir()) or self._service is not None:
                self.status_var.set("waiting for service to apply configuration…")
            else:
                # No service is running to apply it - the normal state of a
                # fresh install. The file is already saved and the next service
                # start reads it; keep claiming we are "waiting for service"
                # and we contradict the "Service not running" line one row up.
                self.pending_revision = ""
        results = self.status.get("command_results", {})
        if isinstance(results, dict):
            for request_id in list(self.pending_commands):
                result = results.get(request_id)
                if isinstance(result, dict):
                    self.status_var.set(model.format_command_result(result))
                    self.pending_commands.pop(request_id, None)
        self._sync_rows()
        self._refresh_log()
        self.after(POLL_MS, self._poll_status)

    def _log_sources(self) -> Dict[str, Path]:
        """Label -> file, newest-first in the order an operator scans them."""
        global_dir = Path(str(model.get_path(self.raw, "paths.log_dir", "logs")))
        sources: Dict[str, Path] = {
            LOG_SOURCE_ALL: global_dir / "eap_middleware.log"
        }
        for machine in self._machines():
            endpoint_id = str(machine.get("endpoint_id", "")) or "?"
            configured = model.machine_value(self.raw, machine, "storage.log_dir")
            if not configured:
                continue
            sources[f"{endpoint_id} (machine)"] = Path(str(configured)) / "middleware.log"
            simulator_dir = model.machine_value(
                self.raw, machine, "storage.simulator_log_dir"
            )
            if simulator_dir:
                sources[f"{endpoint_id} (simulator)"] = (
                    Path(str(simulator_dir)) / "simulator.log"
                )
        return sources

    def _refresh_log(self) -> None:
        sources = self._log_sources()
        labels = tuple(sources)
        if self.log_source["values"] != labels:
            self.log_source["values"] = labels
        if self.log_source_var.get() not in sources:
            self.log_source_var.set(LOG_SOURCE_ALL)
        path = sources[self.log_source_var.get()]
        lines = _tail_lines(path, LOG_LINES_KEPT)
        content = "\n".join(lines)
        if self.log_text.get("1.0", "end-1c") == content:
            return
        # Only follow the tail when the operator is already at it, so reading
        # back through a trace is not interrupted every poll.
        at_end = self.log_text.yview()[1] >= 0.999
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content)
        self.log_text.configure(state="disabled")
        if at_end:
            self.log_text.see("end")

    def _on_close(self) -> None:
        """Leave an externally-run service alone; shut down one we own.

        A service started here holds the single-instance lock and has CSVs
        to flush, so it gets a clean stop - but on a worker thread. Joining
        it here would freeze the window for as long as the teardown takes.
        """
        if self._service is None and not self._service_busy:
            self.destroy()
            return
        if self._service is None:
            # A start is still in flight. Destroying now would leave it
            # holding the single-instance lock with no owner, so the next
            # launch refuses to start. Wait for it, then stop it - but on the
            # same 20s bound as the stop path, so a wedged start cannot make
            # the window unclosable.
            if self._close_deadline is None:
                self._close_deadline = time.monotonic() + 20.0
            if time.monotonic() >= self._close_deadline:
                self.destroy()
                return
            self.status_var.set("waiting for the service start to finish…")
            self.after(POLL_MS, self._on_close)
            return
        if not messagebox.askyesno(
            "Service still running",
            "The middleware service is running in this window. Closing stops "
            "it and collection ends.\n\nStop it and close?",
        ):
            return
        self._on_stop_service()
        self._await_service_stop(deadline=time.monotonic() + 20.0)

    def _await_service_stop(self, deadline: float) -> None:
        """Poll the stop off the event loop so the window keeps repainting."""
        if not self._service_busy or time.monotonic() >= deadline:
            self.destroy()
            return
        self.after(POLL_MS, lambda: self._await_service_stop(deadline))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="AstarEapGui")
    parser.add_argument("--config", help="path to production.yaml")
    args = parser.parse_args(argv)
    App(Path(args.config) if args.config else None).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
