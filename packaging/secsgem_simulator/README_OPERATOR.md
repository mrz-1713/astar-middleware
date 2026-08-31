# ASTAR SECS/GEM Simulator — Windows 11 x64

A standalone SECS/GEM simulator. It needs no Python, no middleware installation, and it never controls real equipment.

It can stand in for **either end** of a SECS/GEM link:

- as **EQUIPMENT** — it pretends to be the tool, so you can prove the middleware (or any other host) works;
- as **HOST** — it pretends to be the EAP, so you can prove a real tool works before the middleware exists.

## The two settings that decide the wiring

These are the settings to get right, and they are **independent of each other**. All four combinations are valid.

| Setting | Values | Question it answers |
|---|---|---|
| `connection.role` | `equipment` / `host` | What does this simulator *pretend to be*? |
| `connection.mode` | `passive` / `active` | Which end *opens the TCP connection*? |

`passive` means this process listens and the peer dials in. `active` means this process dials out to `connection.address`.

Knowing only that a simulator is "passive" does **not** tell you whether it is the tool or the EAP. That is why the role is a separate setting, is printed on every start-up, and is shown live in the control panel.

| This simulator | Then the peer must be |
|---|---|
| EQUIPMENT + passive (listen) | HOST, HSMS active, dialling this machine |
| EQUIPMENT + active (dial out) | HOST, HSMS passive, listening |
| HOST + passive (listen) | EQUIPMENT, HSMS active, dialling this machine |
| HOST + active (dial out) | EQUIPMENT, HSMS passive, listening |

When the peer is the ASTAR middleware, its `hsms_mode` in `production.yaml` is always the **opposite** of this simulator's `connection.mode`.

## Recommended installation

1. Run `SecsGemSimulator-Setup-1.0.0-win-x64.exe`.
2. Accept the default installation location. Administrator access is not required.
3. Open **Simulator Control Panel** from the Start Menu or the desktop.

Python 3.11, `secsgem`, and every other runtime dependency are already embedded. Do not install Python, run `pip`, or download wheel files on the simulator computer.

The default per-user installation location is:

```text
%LOCALAPPDATA%\Programs\SecsGemSimulator
```

The portable ZIP contains the same application for IT-managed or diagnostic use. Extract the complete folder before launching; do not move `SecsGemSimulator.exe` or `AstarSimulatorGui.exe` out of it.

Uninstall removes the application and its shortcuts. Your edited YAML files and generated logs stay in the installation folder.

## Using the control panel (recommended)

`AstarSimulatorGui.exe` edits `simulator.yaml` and runs it in place.

1. **Link** tab — pick the role, pick the HSMS mode, fill in the endpoint.
2. Read the **Resulting wiring** box. It states in plain words what this simulator is, what the peer must be, and what to set in the middleware's `production.yaml`.
3. **Equipment** or **Host** tab — whichever applies. The other one is greyed out and labelled *(not used)*, because it genuinely has no effect in the selected role.
4. **Save**, then **Start**. The **Run log** tab shows the live log, and the Link tab shows the connection state and, in host role, how many events have arrived.

Anything saved by the panel runs identically headless:

```powershell
.\SecsGemSimulator.exe run --config .\simulator.yaml
```

## Using the shortcuts (no panel)

Each shortcut names the role first and the direction second:

| Shortcut | File | Wiring |
|---|---|---|
| Run as EQUIPMENT (listen, HSMS passive) | `davinci-passive.yaml` | middleware dials in with `hsms_mode: active` |
| Run as EQUIPMENT (dial out, HSMS active) | `davinci-active.yaml` | middleware listens with `hsms_mode: passive` |
| Run as HOST (dial out, HSMS active) | `host-example.yaml` | the tool listens; the middleware is not involved |

Edit the matching YAML before launching. For an active configuration, `connection.address` must be the **peer's** IP; for a passive one it is the local interface to bind (`0.0.0.0` = all).

## What the host role actually does

With `connection.role: host` the simulator performs the same opening sequence the production middleware performs, then logs everything that arrives instead of forwarding it upstream:

1. S1F17 request ON-LINE (`host.request_online`) — a tool left OFF-LINE ignores subscriptions and reports nothing;
2. S2F33 / S2F35 / S2F37 event subscription, taken from the selected profile;
3. S6F23 spool drain (`host.drain_spool`, off by default);
4. S5F3 enable all alarms (`host.enable_alarms`);
5. S1F3 read-back of the profile's identity SVs (`host.read_identity`).

Received events appear in the log as `event #N CEID=… (name)`, with the CEID resolved through the profile. If the name shows as `unmapped`, the tool is sending CEIDs the selected profile does not document.

## Expected status sequence

The console and `secsgem-simulator.log` under the configured `logging.directory` show:

1. the role and endpoint, plus what the peer must be set to;
2. TCP connected;
3. HSMS selected and GEM communication established;
4. S6F11 lifecycle events (sent as equipment, received as host);
5. S5F1 alarm set and clear.

A relative log directory resolves beside the YAML file. Press `Ctrl+C` to stop cleanly, or **Stop** in the panel.

## Validate without connecting

```powershell
.\SecsGemSimulator.exe check-config --config .\simulator.yaml
.\SecsGemSimulator.exe version
```

`check-config` prints `gem_role`, `hsms_mode` and both wiring sentences, so it is the fastest way to confirm a file before a site visit.

## Important safety rules

- Use a dedicated test port such as 5050 or 5051. Do not take port 5000 from a real tool or an existing host session.
- Exactly one side must be Active and the other Passive.
- Exactly one side must be the equipment and the other the host. Two equipment simulators, or two hosts, will connect at TCP level and then never establish GEM communication.
- Port and device ID must match on both sides.
- The passive templates bind to loopback by default. Do not change that to `0.0.0.0` on an untrusted network.
- Only one peer may connect to one simulator process.

## Troubleshooting

| Symptom | Check |
|---|---|
| Active mode keeps waiting | The peer is listening, its firewall permits the port, and the configured IP is reachable. |
| Passive mode says port unavailable | Another process owns the address/port. Choose a dedicated port or stop the conflicting program. |
| TCP connects but GEM never communicates | Both ends may hold the same SECS role. Check `gem_role` in `check-config` on both sides, then device ID and HSMS pairing. |
| Host role connects but receives nothing | The tool may be OFF-LINE (`host.request_online: true`), or it refused the subscription — the log names the refused bands. |
| Host role logs `unmapped` events | The selected `simulation.profile` does not document the CEIDs this tool sends. |
| Events do not appear in ASTAR | Confirm ASTAR accepted S2F33/S2F35/S2F37 and that the profile matches. |
| Simulator reconnects after a cable/process interruption | Expected. A partial lot is discarded and the next connection begins a fresh lot. |

Exit codes: `0` clean stop, `2` configuration error, `3` listener/startup error, `4` restart limit exhausted, `1` unexpected fatal error.
