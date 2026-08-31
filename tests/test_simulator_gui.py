"""The simulator panel must make the equipment/host choice unmissable.

Two failures this file guards against. First, a setting lands in
simulator/config.py and never gets a widget, so operators hand-edit YAML
the panel will later overwrite. Second - the reason the panel exists -
the SECS role and the HSMS mode get conflated again, leaving an operator
to infer from "passive" alone whether this process is the tool or the EAP.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Sequence, Set

import pytest
import yaml

from simulator import config as sim_config
from simulator_gui import model


def _paths(fields: Sequence[model.Field], prefix: str) -> Set[str]:
    return {
        path[len(prefix):] for path, _label, _kind in fields
        if path.startswith(prefix)
    }


def _field_names(dataclass_type: Any) -> Set[str]:
    return {field.name for field in dataclasses.fields(dataclass_type)}


# ----- every configurable setting reaches a widget -----

def test_host_opening_sequence_is_fully_exposed():
    assert _field_names(sim_config.HostConfig) == _paths(
        model.HOST_FIELDS, "host."
    )


def test_recovery_and_logging_are_fully_exposed():
    assert _field_names(sim_config.RecoveryConfig) == _paths(
        model.RECOVERY_FIELDS, "recovery."
    )
    assert _field_names(sim_config.SimulatorLoggingConfig) == _paths(
        model.LOGGING_FIELDS, "logging."
    )


def test_connection_settings_all_have_a_control():
    # role, mode and address get bespoke controls on the Link tab (radio
    # groups and a mode-dependent label), so they are not in the generic
    # field table. allow_external_bind is bespoke too: app.py derives it
    # from the address the operator picks (model.requires_external_bind)
    # instead of asking for it a second time in an unrelated checkbox.
    bespoke = {"role", "mode", "address", "allow_external_bind"}
    # hsms_timers is a mapping, so its controls are one field per timer at
    # connection.hsms_timers.<name>. Compare on the leading segment so a
    # nested group counts as covered - the guard is still "no setting in
    # simulator/config.py is unreachable from the panel".
    exposed = {
        path.split(".")[0]
        for path in _paths(model.CONNECTION_FIELDS, "connection.")
    } | bespoke
    assert _field_names(sim_config.ConnectionConfig) == exposed


def test_every_hsms_timer_has_its_own_control_and_a_default():
    """All five timers, individually editable, never blank.

    Individually because they are five independent SEMI E37 values and a
    tool states them one by one. Never blank because the panel parses this
    field as an int on save - an empty box would fail validation rather
    than mean "use the default" - so default_config() carries the shipped
    values and the loaded form always shows a number.
    """
    from gateway.host import DEFAULT_HSMS_TIMERS

    exposed = _paths(model.CONNECTION_FIELDS, "connection.hsms_timers.")
    assert exposed == set(DEFAULT_HSMS_TIMERS)
    defaults = model.default_config()["connection"]["hsms_timers"]
    assert defaults == DEFAULT_HSMS_TIMERS
    for path, _label, kind in model.CONNECTION_FIELDS:
        if path.startswith("connection.hsms_timers."):
            assert kind == "int"
            assert model.get_path(model.default_config(), path) is not None


def test_equipment_lot_settings_all_have_a_control():
    # Driven from the profile or from the middleware's generated files
    # rather than typed in this panel.
    not_typed_here = {
        "profile",            # its own combobox on the Equipment tab
        "subscription_path",  # comes from the profile
        "ceid_overrides", "svid_values", "svid_types",
        "dvid_values", "dvid_types",
    }
    exposed = _paths(model.EQUIPMENT_FIELDS, "simulation.") | not_typed_here
    assert _field_names(sim_config.SimulationConfig) == exposed


# ----- the blank form must produce a file the packaged exe accepts -----

def test_default_configuration_is_valid():
    config = model.validate(model.default_config())
    assert config.connection.role == "equipment"
    assert config.connection.mode == "passive"


def test_every_role_and_mode_combination_validates():
    for role in sim_config.GEM_ROLES:
        for mode in sim_config.HSMS_MODES:
            data = model.default_config()
            data["connection"]["role"] = role
            data["connection"]["mode"] = mode
            data["connection"]["address"] = (
                "0.0.0.0" if mode == "passive" else "192.168.1.20"
            )
            data["connection"]["allow_external_bind"] = mode == "passive"
            config = model.validate(data)
            assert config.connection.role == role
            assert config.connection.mode == mode


# ----- role and mode stay independent and legible -----

@pytest.mark.parametrize(
    ("role", "mode", "expected_self", "expected_peer"),
    [
        ("equipment", "passive", "EQUIPMENT", "HOST in HSMS ACTIVE"),
        ("equipment", "active", "EQUIPMENT", "HOST in HSMS PASSIVE"),
        ("host", "passive", "HOST", "EQUIPMENT in HSMS ACTIVE"),
        ("host", "active", "HOST", "EQUIPMENT in HSMS PASSIVE"),
    ],
)
def test_wiring_sentences_name_both_ends(
    role, mode, expected_self, expected_peer
):
    self_line, peer_line = model.wiring_lines(role, mode, "10.0.0.5", 5051, 0)
    assert expected_self in self_line
    assert expected_peer in peer_line
    # The listening end must never be described as dialling.
    assert ("listens" in self_line) == (mode == "passive")


def test_address_label_changes_meaning_with_the_mode():
    """Same entry box, opposite meanings - the label has to say which."""
    assert "Bind" in model.address_label("passive")
    assert "Peer" in model.address_label("active")
    assert model.address_label("passive") != model.address_label("active")


def test_middleware_hint_inverts_the_mode_for_the_operator():
    assert "hsms_mode: active" in model.peer_middleware_hint(
        "equipment", "passive"
    )
    assert "hsms_mode: passive" in model.peer_middleware_hint(
        "equipment", "active"
    )
    # As a host the peer is a tool, so production.yaml is not involved.
    assert "not involved" in model.peer_middleware_hint("host", "active")


# ----- the external-bind consent tracks the address, not a second control -----

@pytest.mark.parametrize(
    ("mode", "address", "expected"),
    [
        ("passive", "127.0.0.1", False),
        ("passive", "localhost", False),
        ("passive", "0.0.0.0", True),
        ("passive", "", True),  # blank means "every adapter", same as 0.0.0.0
        ("passive", "10.0.0.5", True),
        ("active", "0.0.0.0", False),  # irrelevant outside passive mode
    ],
)
def test_requires_external_bind_follows_the_chosen_address(
    mode, address, expected
):
    assert model.requires_external_bind(mode, address) is expected


def test_listening_summary_warns_when_it_reaches_other_machines():
    everywhere = model.listening_summary("passive", "0.0.0.0", [])
    assert "accept connections from other machines" in everywhere

    pinned_lan = model.listening_summary("passive", "10.0.0.5", [])
    assert "accept connections from other machines" in pinned_lan

    loopback_only = model.listening_summary("passive", "127.0.0.1", [])
    assert "accept connections from other machines" not in loopback_only


# ----- saved files stay unambiguous -----

def test_saving_an_equipment_drops_the_host_only_section(tmp_path):
    data = model.default_config()
    data["host"] = {"request_online": False}
    path = tmp_path / "simulator.yaml"

    model.save_yaml(path, data)

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "host" not in written
    # ...and what was written still loads.
    assert sim_config.load_simulator_config(path).connection.role == "equipment"


def test_saving_a_host_keeps_its_opening_sequence(tmp_path):
    data = model.default_config()
    data["connection"]["role"] = "host"
    data["connection"]["mode"] = "active"
    data["connection"]["address"] = "192.168.1.30"
    data["host"]["request_online"] = False
    path = tmp_path / "simulator.yaml"

    model.save_yaml(path, data)

    config = sim_config.load_simulator_config(path)
    assert config.connection.role == "host"
    assert config.host.request_online is False
    assert config.host.enable_alarms is True


def test_save_is_atomic_enough_to_survive_a_reload(tmp_path):
    path = tmp_path / "simulator.yaml"
    model.save_yaml(path, model.default_config())
    reloaded = model.load_yaml(path)
    assert model.validate(reloaded).connection.role == "equipment"
    assert not list(tmp_path.glob("*.tmp"))


# ----- the tkinter layer builds (skipped where there is no display) -----

def _app():
    pytest.importorskip("tkinter")
    import tkinter

    from simulator_gui import app as gui_app

    try:
        return gui_app.App()
    except tkinter.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no Tk display: {exc}")


def test_panel_builds_and_starts_on_the_equipment_role():
    app = _app()
    try:
        assert app.role_var.get() == "equipment"
        assert "EQUIPMENT" in app.self_label.cget("text")
        assert "HOST in HSMS ACTIVE" in app.peer_label.cget("text")
    finally:
        app.destroy()


def test_switching_to_host_marks_the_equipment_settings_unused():
    app = _app()
    try:
        tabs = lambda: [
            app.book.tab(index, "text")
            for index in range(app.book.index("end"))
        ]
        assert "Host (not used)" in tabs()

        app.role_var.set("host")
        app.update()

        assert "Equipment (not used)" in tabs()
        assert "Host" in tabs()
        assert "HOST" in app.self_label.cget("text")
        assert "ignored" in app.equipment_note.cget("text")
    finally:
        app.destroy()


def test_switching_mode_relabels_the_address_field():
    app = _app()
    try:
        app.mode_var.set("passive")
        app.update()
        passive_label = app.address_label.cget("text")

        app.mode_var.set("active")
        app.update()

        assert app.address_label.cget("text") != passive_label
        assert "dials" in app.self_label.cget("text")
    finally:
        app.destroy()


# ----- the panel must fit, stay responsive, and not ask the wrong question -----

def test_every_settings_tab_can_scroll():
    """A ttk.Frame in a Notebook clips with no scrollbar and no hint that
    anything was cut off. The Link tab outgrew an 800px window the moment it
    gained a pairing section, so the section telling you what to type on the
    other machine was unreachable."""
    # _app() first: it owns the importorskip guard, so importing the panel
    # module before it would hard-fail wherever tkinter is absent.
    app = _app()
    from simulator_gui.app import ScrollableTab

    try:
        for index, name in enumerate(("Link", "Equipment", "Host", "Advanced")):
            widget = app.book.nametowidget(app.book.tabs()[index])
            assert isinstance(widget, ScrollableTab), name
    finally:
        app.destroy()


def test_a_passive_listener_is_not_asked_for_a_bind_address():
    """The value goes straight to socket.bind, so the default already
    accepts on every adapter: it restricts, it is not a destination.
    Showing it as step 3 is what led an operator to pin one NIC on a
    multi-homed machine."""
    app = _app()
    try:
        app.mode_var.set("passive")
        app.restrict_var.set(False)
        app._on_restrict_toggled()
        app.update()

        assert not app.address_frame.winfo_manager(), "bind box must be hidden"
        assert app.listen_frame.winfo_manager(), "listening summary must show"
    finally:
        app.destroy()


def test_an_active_dialler_is_asked_for_the_peer_address():
    """The mirror image: where the address is a real decision, ask for it."""
    app = _app()
    try:
        app.mode_var.set("active")
        app.update()

        assert app.address_frame.winfo_manager(), "peer box must be visible"
        assert not app.listen_frame.winfo_manager()
    finally:
        app.destroy()


def test_unticking_the_restriction_clears_it_rather_than_hiding_it():
    """A hidden box still holding a pinned adapter is how a config ends up
    restricted by a setting nobody can see."""
    app = _app()
    try:
        app.mode_var.set("passive")
        app._addresses = ["10.0.0.5", "10.0.1.5"]
        app._primary = "10.0.0.5"

        app.restrict_var.set(False)
        app._on_restrict_toggled()
        app.restrict_var.set(True)
        app._on_restrict_toggled()
        app.update()
        assert app.address_var.get() == "10.0.0.5"

        app.restrict_var.set(False)
        app._on_restrict_toggled()
        app.update()
        assert app.address_var.get() == "0.0.0.0"
        assert not app.address_frame.winfo_manager()
    finally:
        app.destroy()


def test_accepting_on_every_adapter_validates_without_a_separate_flag():
    """Reproduces the panel's own "Configuration error": accepting on every
    adapter (or a pinned LAN address) used to leave allow_external_bind at
    its default False, since it lived in a checkbox nowhere near the
    address controls. It is now derived from the address itself, so the
    "Resulting wiring" summary and Start can never disagree."""
    app = _app()
    try:
        app.mode_var.set("passive")

        app.address_var.set("0.0.0.0")
        app._collect_forms()
        assert app.raw["connection"]["allow_external_bind"] is True
        model.validate(app.raw)  # must not raise

        app.address_var.set("172.16.89.132")
        app._collect_forms()
        assert app.raw["connection"]["allow_external_bind"] is True
        model.validate(app.raw)  # must not raise

        app.address_var.set("127.0.0.1")
        app._collect_forms()
        assert app.raw["connection"]["allow_external_bind"] is False
        model.validate(app.raw)  # must not raise
    finally:
        app.destroy()


def test_a_pinned_config_opens_with_the_advanced_box_already_ticked():
    """Loading a restricted file must not leave the panel claiming it
    accepts everywhere while the config says one adapter."""
    app = _app()
    try:
        app.raw = {
            "connection": {
                "role": "equipment", "mode": "passive",
                "address": "10.0.0.5", "port": 5051, "device_id": 0,
            }
        }
        app._refresh_forms()
        app.update()

        assert app.restrict_var.get()
        assert app.address_frame.winfo_manager()
    finally:
        app.destroy()


def test_address_detection_does_not_block_the_window():
    """getaddrinfo on a Windows host with no reachable DNS blocks for
    seconds, and the pick-list was rebuilt from a widget trace that fires
    on every keystroke - so typing an IP re-ran the scan per character."""
    app = _app()
    try:
        # Detection owns its own thread and the panel says so meanwhile.
        assert app._detect_thread is not None or app._addresses is not None

        # Let the real scan finish before staging a fake one. It writes
        # _addresses when it completes, so on a cold DNS lookup it would
        # land after the assignment below and undo it.
        if app._detect_thread is not None:
            app._detect_thread.join(timeout=30)
            assert not app._detect_thread.is_alive(), "detection never finished"
        app._detect_thread = None
        app._addresses = None
        app.mode_var.set("passive")
        app.restrict_var.set(False)
        app._on_restrict_toggled()
        app._refresh_wiring()
        app.update()
        assert "Detecting" in app.listen_label.cget("text")

        app._addresses = ["10.0.0.5"]
        app._primary = "10.0.0.5"
        app._refresh_wiring()
        app.update()
        assert "every adapter" in app.listen_label.cget("text")
        assert "10.0.0.5:5051" in app.pairing_label.cget("text")
    finally:
        app.destroy()
