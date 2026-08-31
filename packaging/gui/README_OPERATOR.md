# ASTAR EAP Middleware — Control Panel

`AstarEapGui.exe` is the operator front end for the middleware. It edits the
configuration and monitors the always-running Windows service. The panel owns
no HSMS or simulator threads. Closing it does not stop any machine session.

## First run

1. Unzip anywhere, e.g. `C:\SECSGEM_EAP\gui`.
2. Double-click `AstarEapGui.exe`.
3. It opens the first config it finds:
   `.\config\production.yaml` next to the exe, otherwise the template bundled
   inside the exe. **Save** writes to the file shown in the title bar; use
   **Open…** to point at your real config first.

## Toolbar

| Button | What it does |
|---|---|
| Open… / Save | Load and atomically replace the YAML after a stale-editor check. |
| Validate | Runs the same check as `eap-middleware validate-config`. Do this before Start. |
| Start all | Enables the listed endpoints after showing the exact affected set. |
| Stop all | Disables the listed endpoints after showing the exact affected set. |
| Show secrets | Unmasks tokens and API keys while you type them. |

**Save writes what you typed, verbatim.** A token typed into a field is stored
in the YAML in plain text. To keep secrets out of the file, type an environment
reference instead — `${LINKSTUFFS_HTTP_DAVINCI_TOKEN}` — and set the variable on
the server; the middleware expands it at load. Never Save real tokens into a
config file that is under version control.

## Machines tab

The table reads the service status snapshot and shows per-machine HSMS, GEM,
HTTPS queue and simulator state.

- **Add machine** appends a disabled machine on an unused endpoint id and port.
- **Duplicate** copies the selected machine's settings onto a new id/port —
  the fast way to reach 22 tools.
- Editing the form below applies to the selected row as soon as you select
  another row, add a machine, save, or start anything.
- **Start / Stop** persist that machine's `enabled` value.
- **Restart / Test connection / Test Linkstuffs** submit one unique local
  command. They never open a network-facing admin port.
- Each machine has an explicit HTTPS route and token. Create the Linkstuffs
  device first (`docs/LINKSTUFFS_SETUP.md`).

## Built-in simulators

Choose `runtime_mode: simulated`, configure the Simulator fields, and press
Start. The Windows service owns both the middleware peer and the GEM Equipment
simulator. It always uses loopback and the opposite HSMS role:

| Machine `hsms_mode` | Simulator |
|---|---|
| `active` (middleware dials out) | listens on `0.0.0.0:<port>` |
| `passive` (middleware listens) | dials `127.0.0.1:<port>` |

Every profile uses the same universal SECS/GEM simulator, which sends that
tool's own CEIDs — so all four are simulated end-to-end: real CEIDs, mapped
events, per-lot CSV files and upstream telemetry.

`spts_fxp_omega`, `davinci_200_mc4_hc1` and `nexgen_mg_series` use the CEIDs
from their vendor manuals. `ptiq_secsgem` publishes no CEID numbers (they are
set per installation in the tool's EIB model export), so it falls back to the
general GEM events — 1001 pod arrived, 1002 lot start, 1003 wafer start, 1004
wafer end, 1005 lot end, 1006 pod removed. Those match the shipped
`EventSubscription.json`, so it maps correctly out of the box; point the
machine at your tool's own subscription file to simulate its real numbers.

Steps a tool does not have are simply not sent — the MG documents no clamp
event, so no clamp event appears.

Real and simulated machines can run side by side. Give each simulated machine
its own port; validation rejects local endpoint collisions.

## Log tab

The selected machine's rotating log, or the global service log when no
machine-specific log is available.

## When something does not connect

- `Link` stays `disconnected`: wrong `host`/`port`/`secs_device_id`, or the
  tool expects the other `hsms_mode`. `Validate` cannot catch these — they are
  facts about the tool.
- `Simulator` shows `Stopped` or the machine shows `Error`: inspect its status
  and `simulator.log`; the port may already be in use.
- Two passive machines cannot share a port; `Validate` rejects that.
