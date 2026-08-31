"""The control panel must expose every setting and stand in for every tool.

Two things are easy to get silently wrong here: a new config knob lands in
models.py and never gets a widget (operators then have to hand-edit YAML that
the GUI will overwrite), and the built-in simulator takes the same HSMS role as
the middleware so nothing ever connects.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict, Sequence, Set, Tuple

import pytest
import yaml

from eap_middleware import models
from eap_middleware.config import service_config_from_dict
from gui import model

ROOT = Path(__file__).resolve().parents[1]


def _paths(fields: Sequence[model.Field], prefix: str = "") -> Set[str]:
    return {
        path[len(prefix):] if prefix and path.startswith(prefix) else path
        for path, _label, _kind in fields
    }


# ----- every configurable setting reaches a widget -----

COVERAGE: Tuple[Tuple[Any, str, Sequence[model.Field], Set[str]], ...] = (
    (models.LinkstuffsConfig, "linkstuffs.", model.MQTT_FIELDS, set()),
    # device_tokens is edited on the machine row it belongs to, not as a map.
    (models.LinkstuffsHttpConfig, "linkstuffs_http.", model.HTTP_FIELDS, {"device_tokens"}),
    (models.LegacyApiConfig, "legacy_api.", model.LEGACY_FIELDS, set()),
    (models.MiddlewarePaths, "paths.", model.PATH_FIELDS, set()),
    (models.LoggingConfig, "logging.", model.RUNTIME_FIELDS, set()),
    (models.StorageSafetyConfig, "storage_safety.", model.RUNTIME_FIELDS, set()),
)


@pytest.mark.parametrize("cls,prefix,fields,excused", COVERAGE)
def test_every_config_field_has_a_widget(cls, prefix, fields, excused):
    declared = {field.name for field in dataclasses.fields(cls)} - excused
    missing = declared - _paths(fields, prefix)
    assert not missing, f"{cls.__name__} settings with no GUI field: {sorted(missing)}"


def test_service_level_settings_have_widgets():
    nested = {
        "machines", "linkstuffs", "linkstuffs_http", "legacy_api", "paths",
        "logging", "storage_safety",
    }
    declared = {field.name for field in dataclasses.fields(models.ServiceConfig)} - nested
    missing = declared - _paths(model.RUNTIME_FIELDS)
    assert not missing, f"service settings with no GUI field: {sorted(missing)}"


def test_every_machine_field_has_a_widget():
    direct = {
        field.name for field in dataclasses.fields(models.MachineConfig)
    } - {
        # Legacy flat storage is accepted on load and migrated to storage.*.
        "local_csv_path", "network_csv_path", "admin_config_path",
        "storage", "linkstuffs_http", "simulator",
    }
    assert not direct - _paths(model.CONNECTION_FIELDS)
    assert {
        field.name for field in dataclasses.fields(models.MachineStorageConfig)
    } == _paths(model.STORAGE_FIELDS, "storage.")
    assert {
        field.name
        for field in dataclasses.fields(models.MachineLinkstuffsHttpConfig)
    } == _paths(model.MACHINE_HTTP_FIELDS, "linkstuffs_http.")
    assert {
        field.name for field in dataclasses.fields(models.MachineSimulatorConfig)
    } == _paths(model.SIM_FIELDS, "simulator.")


def test_profile_choices_come_from_the_registry():
    ids = model.profile_ids()
    assert "nexgen_mg_series" in ids
    assert len(ids) >= 4


# ----- form values survive the round trip the GUI performs -----

def test_production_template_round_trips_through_the_forms():
    raw: Dict[str, Any] = yaml.safe_load(
        (ROOT / "config" / "production.yaml").read_text(encoding="utf-8")
    )
    fields = (
        model.HTTP_FIELDS + model.MQTT_FIELDS + model.LEGACY_FIELDS
        + model.PATH_FIELDS + model.RUNTIME_FIELDS
    )
    for path, _label, kind in fields:
        shown = model.format_value(kind, model.get_path(raw, path))
        model.set_path(raw, path, model.parse_value(kind, shown))
    for machine in model.machines_of(raw):
        for path, _label, kind in model.MACHINE_FIELDS:
            shown = model.format_value(kind, model.machine_value(raw, machine, path))
            parsed = model.parse_value(kind, shown)
            model.set_path(machine, path, parsed)
    # Unset numbers must come back as an absent key, not "", or this raises.
    loaded = service_config_from_dict(raw)
    assert [m.endpoint_id for m in loaded.machines] == ["TOOL_01", "TOOL_02", "TOOL_03", "TOOL_04"]


def test_blank_number_means_default_not_empty_string():
    assert model.parse_value("int", "") is None
    assert model.parse_value("float", "   ") is None
    assert model.parse_value("str", "") == ""
    assert model.parse_value("str?", "") is None
    with pytest.raises(ValueError):
        model.parse_value("int", "not a port")


def test_token_follows_a_renamed_machine():
    raw: Dict[str, Any] = {}
    model.set_device_token(raw, "TOOL_A", "secret")
    model.set_device_token(raw, "TOOL_B", "secret", previous_name="TOOL_A")
    assert model.get_path(raw, "linkstuffs_http.device_tokens") == {"TOOL_B": "secret"}


def test_new_machine_avoids_used_ids_and_ports():
    existing = [
        {"endpoint_id": "TOOL_01", "port": 5000},
        {"endpoint_id": "TOOL_02", "port": 5001},
    ]
    fresh = model.new_machine(existing)
    assert fresh["endpoint_id"] == "TOOL_03"
    assert fresh["port"] == 5002
    assert fresh["enabled"] is False


def test_installed_config_environment_path_is_discovered_first(monkeypatch, tmp_path):
    installed = tmp_path / "production.yaml"
    monkeypatch.setenv("ASTAR_EAP_CONFIG", str(installed))

    assert model.candidate_config_paths()[0] == installed


def test_command_result_message_includes_verified_identity():
    message = model.format_command_result(
        {
            "action": "test_connection",
            "endpoint_id": "TOOL_01",
            "status": "ok",
            "connected": True,
            "identity": ["MG Series", "NWS MG 1.1.18"],
        }
    )

    assert "TOOL_01" in message
    assert "MG Series" in message


def test_profile_label_marks_documentation_derived_profiles():
    assert model.profile_label(
        "nexgen_mg_series", {"profile_provenance": "DOCUMENTATION-DERIVED"}
    ).endswith(" ⚠")


def test_close_leaves_an_externally_run_service_alone():
    """The panel is a passive client of a Windows service. Closing the
    window must never stop a service it did not start."""
    pytest.importorskip("tkinter")
    from gui.app import App

    class Stub:
        closed = False
        _service = None          # nothing owned by this window
        _service_busy = ""       # and no start in flight

        def destroy(self) -> None:
            self.closed = True

    app = Stub()
    App._on_close(app)
    assert app.closed is True


def test_close_waits_for_a_service_that_is_still_starting():
    """Destroying mid-start would leave the started service holding the
    single-instance lock with no owner, so the next launch refuses to run."""
    pytest.importorskip("tkinter")
    from gui.app import App

    waited = []

    class Stub:
        closed = False
        _service = None
        _service_busy = "starting"
        _close_deadline = None   # set on the first pass, bounds the wait
        # Referenced by the reschedule: self.after(POLL_MS, self._on_close).
        _on_close = None

        class _Var:
            @staticmethod
            def set(value):
                waited.append(value)

        status_var = _Var()

        def after(self, _ms, callback) -> None:
            waited.append("rescheduled")

        def destroy(self) -> None:
            self.closed = True

    app = Stub()
    App._on_close(app)

    assert app.closed is False, "must not destroy while a start is in flight"
    assert "rescheduled" in waited


def test_a_wedged_start_cannot_make_the_window_unclosable():
    """Waiting for an in-flight start is right; waiting forever is not."""
    import time as _time

    pytest.importorskip("tkinter")
    from gui.app import App

    class Stub:
        closed = False
        _service = None
        _service_busy = "starting"
        _close_deadline = _time.monotonic() - 1.0   # already expired
        _on_close = None

        class _Var:
            @staticmethod
            def set(value):
                pass

        status_var = _Var()

        def after(self, _ms, callback) -> None:
            raise AssertionError("must not keep rescheduling past the deadline")

        def destroy(self) -> None:
            self.closed = True

    app = Stub()
    App._on_close(app)

    assert app.closed is True


def test_close_stops_a_service_this_window_started():
    """A service started here holds the single-instance lock and has CSVs
    to flush, so it needs a real stop - but the wait must go through the
    event loop, never a join that freezes the window."""
    pytest.importorskip("tkinter")
    from gui.app import App

    calls = []

    class Stub:
        closed = False
        _service = object()      # this window owns a running service
        _service_busy = "stopping"

        def _on_stop_service(self) -> None:
            calls.append("stop")

        def _await_service_stop(self, deadline) -> None:
            calls.append("await")

        def destroy(self) -> None:
            self.closed = True

    app = Stub()
    from gui import app as gui_app

    original = gui_app.messagebox
    gui_app.messagebox = type(
        "Box", (), {"askyesno": staticmethod(lambda *a, **k: True)}
    )
    try:
        App._on_close(app)
    finally:
        gui_app.messagebox = original

    assert calls == ["stop", "await"], calls
    # destroy() belongs to the polled wait, not to _on_close itself.
    assert app.closed is False


# ----- the tkinter layer builds (skipped where there is no display) -----

def test_window_builds_and_lists_the_template_machines():
    pytest.importorskip("tkinter")
    import tkinter

    from gui.app import App

    try:
        app = App(ROOT / "config" / "production.yaml")
    except tkinter.TclError as exc:  # no display on this machine
        pytest.skip(f"no Tk display: {exc}")
    try:
        app.update()
        assert len(app.tree.get_children()) == 4
        assert app.tree.item("0", "values")[0] == "TOOL_01"
        # Every declared field really produced a widget.
        assert set(app.machine_vars) == _paths(model.MACHINE_FIELDS)
        assert app._apply_all() is True
        service = service_config_from_dict(app.raw)
        assert len(service.machines) == 4
    finally:
        app._on_close()


def test_packaging_spec_covers_the_lazy_imports():
    spec = (ROOT / "packaging" / "gui" / "AstarEapGui.spec").read_text(encoding="utf-8")
    assert "console=False" in spec
    # The bundled template is what a fresh install opens on.
    assert "production.yaml" in spec


# ----- Enable must survive the save it triggers -----

def _panel_with_config(tmp_path):
    pytest.importorskip("tkinter")
    import tkinter

    import yaml

    from gui.app import App

    raw = yaml.safe_load((ROOT / "config" / "production.yaml").read_text())
    for machine in raw["machines"]:
        machine["offline_test_mode"] = True
        machine["storage"] = {
            "local_csv_path": str(tmp_path / "csv"),
            "network_csv_path": "",
            "admin_config_path": str(tmp_path / "admin"),
            "log_dir": str(tmp_path / "log"),
        }
    raw["paths"] = {
        **raw["paths"],
        "data_dir": str(tmp_path / "data"),
        "log_dir": str(tmp_path / "log"),
        "install_dir": str(tmp_path),
    }
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    try:
        return App(path), path
    except tkinter.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no Tk display: {exc}")


def test_enable_persists_for_the_selected_machine(tmp_path, monkeypatch):
    """"Enabled" is both a dict key and a checkbox, and _save re-collects the
    machine form on its way to disk. Writing only the dict was undone by the
    save it triggered: the row stayed disabled while Enable reported success.
    """
    import yaml
    pytest.importorskip("_tkinter")

    from gui import app as gui_app

    monkeypatch.setattr(
        gui_app, "messagebox",
        type("B", (), {
            "showinfo": staticmethod(lambda *a, **k: None),
            "showerror": staticmethod(lambda *a, **k: None),
            "showwarning": staticmethod(lambda *a, **k: None),
            "askyesno": staticmethod(lambda *a, **k: True),
        }),
    )
    app, path = _panel_with_config(tmp_path)
    try:
        app.selected_row = 3
        app.tree.selection_set("3")
        app._load_machine_form()
        app.update()
        assert app.machine_vars["enabled"][1].get() is False

        app._on_start()
        app.update()

        assert app._machines()[3]["enabled"] is True
        assert app.machine_vars["enabled"][1].get() is True
        assert yaml.safe_load(path.read_text())["machines"][3]["enabled"] is True
    finally:
        app.destroy()


def test_enable_all_does_not_skip_the_selected_machine(tmp_path, monkeypatch):
    """The selected row is the one whose form gets re-collected, so it was
    the one machine "Enable all" silently left disabled."""
    import yaml
    pytest.importorskip("_tkinter")

    from gui import app as gui_app

    monkeypatch.setattr(
        gui_app, "messagebox",
        type("B", (), {
            "showinfo": staticmethod(lambda *a, **k: None),
            "showerror": staticmethod(lambda *a, **k: None),
            "showwarning": staticmethod(lambda *a, **k: None),
            "askyesno": staticmethod(lambda *a, **k: True),
        }),
    )
    app, path = _panel_with_config(tmp_path)
    try:
        app.selected_row = 3
        app.tree.selection_set("3")
        app._load_machine_form()
        app.update()

        app._on_start_all()
        app.update()

        saved = yaml.safe_load(path.read_text())["machines"]
        assert [m["enabled"] for m in saved] == [True] * 4, saved
    finally:
        app.destroy()


def test_disable_persists_too(tmp_path, monkeypatch):
    import yaml
    pytest.importorskip("_tkinter")

    from gui import app as gui_app

    monkeypatch.setattr(
        gui_app, "messagebox",
        type("B", (), {
            "showinfo": staticmethod(lambda *a, **k: None),
            "showerror": staticmethod(lambda *a, **k: None),
            "showwarning": staticmethod(lambda *a, **k: None),
            "askyesno": staticmethod(lambda *a, **k: True),
        }),
    )
    app, path = _panel_with_config(tmp_path)
    try:
        app.selected_row = 0
        app.tree.selection_set("0")
        app._load_machine_form()
        app._on_start()
        app.update()
        assert yaml.safe_load(path.read_text())["machines"][0]["enabled"] is True

        app._on_stop()
        app.update()
        assert yaml.safe_load(path.read_text())["machines"][0]["enabled"] is False
    finally:
        app.destroy()


# ----- the form must not present forty settings as forty equals -----

def test_the_essential_tab_holds_what_decides_a_connection():
    """About ten of the forty-odd machine settings decide whether a link
    comes up at all. In one flat grid they were indistinguishable from the
    thirty with working defaults, which is what made the panel unusable on
    first contact."""
    essential = {path for path, _label, _kind in model.ESSENTIAL_FIELDS}

    assert essential == {
        "endpoint_id", "display_name", "machine_profile", "runtime_mode",
        "host", "port", "secs_device_id", "hsms_mode",
        "enabled", "offline_test_mode",
    }


def test_grouping_loses_no_setting():
    """Every configurable value still reaches a widget; a field dropped from
    the groups would become uneditable without the panel saying so."""
    grouped = set()
    for _title, fields, _columns in model.MACHINE_GROUPS:
        grouped |= {path for path, _label, _kind in fields}

    assert grouped == {path for path, _label, _kind in model.MACHINE_FIELDS}


def test_no_setting_appears_on_two_tabs():
    """Two widgets bound to one path would fight over its value."""
    seen: list = []
    for _title, fields, _columns in model.MACHINE_GROUPS:
        seen.extend(path for path, _label, _kind in fields)

    duplicates = {path for path in seen if seen.count(path) > 1}
    assert not duplicates, duplicates


def test_the_essential_tab_comes_first():
    assert model.MACHINE_GROUPS[0][0] == "Essential"


def test_every_machine_setting_still_has_a_live_widget(tmp_path, monkeypatch):
    """The grouping is presentational: machine_vars must still cover
    everything, or saving would silently drop settings."""
    pytest.importorskip("_tkinter")
    from gui import app as gui_app

    monkeypatch.setattr(
        gui_app, "messagebox",
        type("B", (), {
            "showinfo": staticmethod(lambda *a, **k: None),
            "showerror": staticmethod(lambda *a, **k: None),
            "showwarning": staticmethod(lambda *a, **k: None),
            "askyesno": staticmethod(lambda *a, **k: True),
        }),
    )
    app, _path = _panel_with_config(tmp_path)
    try:
        assert set(app.machine_vars) == {
            path for path, _label, _kind in model.MACHINE_FIELDS
        }
    finally:
        app.destroy()


def test_arrow_keys_in_the_machine_list_do_not_scroll_the_tab():
    """The machine list is a Treeview inside a scrollable tab. Up/Down there
    must move the selected row, not the page."""
    pytest.importorskip("tkinter")
    import tkinter as tk
    from tkinter import ttk

    from eap_middleware.tkwidgets import ScrollableTab

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no Tk display: {exc}")
    try:
        tab = ScrollableTab(root)
        moved = []
        tab._canvas.yview_scroll = lambda *a, **k: moved.append(a)

        for owner in (ttk.Treeview(root), tk.Listbox(root), ttk.Entry(root)):
            for keysym in ("Up", "Down", "Prior", "Next"):
                tab._on_key(type("E", (), {"widget": owner, "keysym": keysym})())
        assert moved == [], moved

        # A plain frame does not own the keys, so the tab still scrolls.
        tab._on_key(type("E", (), {"widget": ttk.Frame(root), "keysym": "Next"})())
        assert moved, "scrolling must still work outside input widgets"
    finally:
        root.destroy()
