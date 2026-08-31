# Linkstuffs / Linkstuffs Setup Guide

How to configure Linkstuffs (or the Linkstuffs platform) to receive data
from the ASTAR SECS/GEM EAP Middleware. Pair this with
[OPERATIONS.md](OPERATIONS.md) (middleware install) and
[../deploy/README_DEPLOY.txt](../deploy/README_DEPLOY.txt) (Windows server install).

> **Setting up a specific tool?** This page is the platform-level walkthrough
> (gateway, transports, what the middleware publishes). The per-machine work —
> device records, tokens, SvidLists, alarm rules and dashboards for each of the
> four tools — is in
> [THINGSBOARD_CE_PER_MACHINE.md](THINGSBOARD_CE_PER_MACHINE.md).

## Prerequisites

- Linkstuffs 3.5+ already installed and reachable from the middleware
  server. Contact your Linkstuffs administrator if the server is not yet set up.
- A Linkstuffs tenant administrator account.

The middleware recommends the Linkstuffs HTTPS device API for production and
also supports the **MQTT Gateway protocol** as an explicit fallback. This guide
covers MQTT gateway setup: one gateway token represents the ASTAR server, and
downstream devices are auto-created when the middleware publishes for them.

> ### ⚠ HTTPS transport: devices are NOT auto-created
>
> Auto-creation is an **MQTT gateway** behaviour. On the HTTPS device API each
> tool must already exist in Linkstuffs and have its token listed under
> `linkstuffs_http.device_tokens` in `production.yaml`, keyed by the machine's
> `display_name`. Configuration validation now rejects an enabled HTTPS route
> with no token before any equipment session starts.
>
> Create the device and issue its token *before* install, and confirm with
> `python -m eap_middleware validate-config --config config/production.yaml`.
>
> The `display_name` is the key into that map, which is also why profiles that
> cover several tool variants use one variant-neutral name (for example
> `NEXGEN_MG_01` covers MG21, MG22 and MG22-300): a variant-specific name would
> force a token reissue if the variant guess turned out wrong.

An enabled, non-offline machine needs at least one usable route: its HTTPS
route with a device token, or the enabled global MQTT gateway with its gateway
token and TLS policy. Both may be enabled for intentional dual delivery.

---

## Step 1 — Create the Gateway device

In Linkstuffs, devices come in two flavors: **device** and **gateway**.
The middleware is a gateway because it speaks for many downstream tools.

1. Log in as tenant admin.
2. Sidebar → **Entities → Devices → +Add new device**.
3. Fill in:
   - **Name**: `ASTAR_EAP_GATEWAY` (or any clear identifier)
   - **Device profile**: leave as `default` for now (we'll customize later)
   - **Is gateway**: ✓ check this box (critical — without it, the v1/gateway/* topics won't be accepted)
4. Click **Add**.

## Step 2 — Copy the access token

1. Open the newly-created `ASTAR_EAP_GATEWAY` device.
2. Click **Manage credentials** → **Copy** the **Access token**.
3. Keep the configuration reference secret-free:

   ```yaml
   linkstuffs:
     access_token: "${LINKSTUFFS_GATEWAY_ACCESS_TOKEN}"
   ```

4. Have the Windows/service administrator inject
   `LINKSTUFFS_GATEWAY_ACCESS_TOKEN` into the middleware service account using
   the site's approved service-manager secret facility or protected credential
   store. The service reads it only through its process environment when the
   configuration is loaded. Do not use `setx`, place the token on a command
   line, echo it, or include it in logs or screenshots.

That single token authenticates the middleware for **all** downstream
machines. You do NOT need a token per machine.

---

## Step 3 — Confirm the upstream connection

On the Windows server:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware test-linkstuffs --config config\production.yaml
```

Expected: connects to Linkstuffs's MQTT broker, returns success. If it fails:

- `Connection refused` → host/port wrong in `linkstuffs:` section of
  `production.yaml`, or the Linkstuffs MQTT transport is off.
- `Connection timed out` → firewall between server and Linkstuffs.
- `Not authorized` → token typo. Re-copy from Linkstuffs credentials page.

---

## Step 4 — Run the middleware and watch devices appear

```powershell
python -m eap_middleware run-service --config config\production.yaml
```

Within ~30 seconds, Linkstuffs's **Entities → Devices** list should show
new devices appearing for each enabled machine:

- `SPTS_fxP_OMEGA_01`
- `DAVINCI200_MC4_HC1_01`
- `PTIQ_01`
- ... and so on for all 22

These are created automatically by the gateway protocol — you don't add
them manually. Each device's name equals the middleware's `display_name`
for that machine (the `display_name:` field in `production.yaml`).

If a device doesn't appear, check `C:\SECSGEM_EAP\logs\service_stderr.log`
for `v1/gateway/connect` publish errors.

---

## Step 5 — What the middleware publishes

For each downstream device, Linkstuffs receives three things:

### 5a. Attributes (the device profile)

Topic: `v1/gateway/attributes`

```json
{
  "DAVINCI200_MC4_HC1_01": {
    "endpoint_id": "TOOL_02",
    "display_name": "DAVINCI200_MC4_HC1_01",
    "machine_profile": "davinci_200_mc4_hc1",
    "vendor": "MueTec",
    "model": "DaVinci 200 MC4 HC1",
    "secs_host": "10.10.20.32",
    "secs_port": 5000,
    "secs_device_id": 0,
    "csv_local_path": "D:/MachineData/EAP_DAVINCI200_MC4_HC1_01/csv_in",
    "csv_network_path": "\\\\TD-DATASVR-F2C4\\TD_DAVINCI200_MC4_HC1_01.csv_in"
  }
}
```

These land under the device's **Attributes** tab in Linkstuffs.

### 5b. Telemetry (events as they happen)

Topic: `v1/gateway/telemetry`

```json
{
  "DAVINCI200_MC4_HC1_01": [
    {
      "ts": 1764298020000,
      "values": {
        "event_type": "lot_start",
        "ceid": 3200017,
        "raw_event_name": "ControlJob:Selected-Executing",
        "secs_raw_event": "LotStarted",
        "load_port": "1",
        "lot_id": "LOT_M42",
        "wafer_id": "",
        "recipe": "Recipe_Overlay_v3",
        "chamber": "NA",
        "endpoint_id": "TOOL_02",
        "display_name": "DAVINCI200_MC4_HC1_01",
        "machine_profile": "davinci_200_mc4_hc1",
        "vendor": "MueTec",
        "model": "DaVinci 200 MC4 HC1",
        "raw_CtrlJobID": "CJ_0001"
      }
    }
  ]
}
```

Every CEID becomes one telemetry message. `event_type` is the canonical
classification:

| `event_type` | When it fires |
|---|---|
| `lot_start` | Control job starts executing |
| `lot_end` | Control job completed |
| `wafer_start` / `wafer_end` | E90 substrate enters / exits processing |
| `process_start` / `process_end` | PM chamber recipe start / finish |
| `loaded` / `unloaded` | Carrier arrives / departs LP |
| `clamped` / `unclamped` | Carrier physical clamping |
| `mounted` / `unmounted` | Material received / removed |
| `alarm` | S5F1 alarm set or cleared |
| `control_state` | Equipment Online/Offline transitions |
| `svid_sample` | Periodic SVID poll (from SvidList.json) |
| `connection_state` | Middleware-side HSMS up/down |
| `unknown` | CEID not in the vendor profile (still captured) |

Plus `raw_<DV_NAME>` keys for every DV the equipment sent in the report
payload (CtrlJobID, PRJobID, ResultFile, TestResults, etc.).

### 5c. Connection events

Topic: `v1/gateway/connect`

Published once per machine when the service prepares it (startup and every
config reload) — not per HSMS connect.

`v1/gateway/disconnect` is defined in the code but **never published**, so the
gateway-managed online indicator never flips back when a tool drops. Link
health reaches Linkstuffs as ordinary telemetry instead: `event_type:
connection_state` with the state in `raw_event_name` (`connected`,
`disconnected`, `reconnect_failing`, `no_event_reports`, …). Build the
"tool is down" alarm on that key, plus the device inactivity timeout as the
backstop for the middleware itself dying. See
[THINGSBOARD_CE_PER_MACHINE.md](THINGSBOARD_CE_PER_MACHINE.md) §9.

---

## Step 6 — Configure device profiles (optional but recommended)

To get nicer dashboards and rule-chain support, create one device profile
per machine type so Linkstuffs knows what telemetry keys to expect.

1. Sidebar → **Entities → Device profiles → +Add new profile**.
2. Name: `SPTS_fxP_OMEGA` (or `DaVinci_MC4_HC1`, `PTIQ`).
3. Transport type: **MQTT**.
4. Save.
5. After creation, edit the profile → **Alarm rules** tab to add alarms
   driven by telemetry (see Step 8).

To bulk-assign existing devices to a profile:

1. Devices list → filter by name (`DAVINCI200_*`) → select multiple.
2. **Assign profile** → pick the DaVinci profile.

This is purely organizational; it doesn't change what the middleware sends.

---

## Step 7 — Build a basic dashboard

Quick dashboard so operators see live state:

1. Sidebar → **Dashboards → +Add new dashboard**.
2. Name: `ASTAR Fab Overview`.
3. Add aliases: **Entity Alias → +Add alias**.
   - Name: `all_machines`
   - Filter type: **Device Type**
   - Profile: `default` (or your specific profile from Step 6)
4. Add a widget → **Cards → Entities Table**:
   - Data source: alias `all_machines`
   - Columns: `display_name`, `event_type` (latest telemetry value),
     `lot_id` (latest), `recipe` (latest)
5. Add a widget → **Charts → Timeseries Line Chart**:
   - Data source: alias `all_machines`
   - Telemetry key: pick numeric SVID keys. Periodic SVID samples publish as
     `raw_svid_<Name>` — `raw_svid_FFUGaugePressurePM`,
     `raw_svid_MainPressure`, `raw_svid_PM1RunningWaferCount`. (`raw_<Name>`
     without `svid_` is a data variable that came in an event report.)
6. Save dashboard.

Open the dashboard. As the middleware ingests events, rows update live.

---

## Step 8 — Alarm rules (optional)

To turn middleware alarms into Linkstuffs alarms:

1. Sidebar → **Device profiles → DaVinci_MC4_HC1 (or whichever)**.
2. **Alarm rules** tab → **+Add alarm**.
3. Alarm type: `Equipment Alarm`.
4. Create two rules that both require telemetry key `event_type` to equal
   `alarm`:
   - **Warning:** `raw_alid >= 5000000` and `raw_alid < 6000000`.
   - **Critical:** `raw_alid >= 6000000`.
5. Assign the first rule Warning severity and the second Critical severity;
   do not rely on the profile's default severity.
6. Save both rules.

Now any S5F1 from a tool surfaces in Linkstuffs's Alarms view + can fire
emails/webhooks via the rule chain.

---

## Step 9 — Rule-chain forwarding (optional)

If you want to forward telemetry into another system (Slack, email,
external database):

1. Sidebar → **Rule chains → Root rule chain → Edit**.
2. Drag a node → **External → REST API call** (for HTTP) or **Slack** etc.
3. Connect: `Originator telemetry → REST API call → success`.
4. Add a telemetry/message filter ahead of it and read the incoming payload,
   for example `msg.event_type === 'alarm'`. `event_type` is telemetry data,
   not an originator attribute.

---

## Step 10 — Verify end-to-end

After Steps 1-5 are done, run one real event through the system to confirm
the pipeline works. Easiest test:

1. On the Windows server, run the simulator pointing at one of the tool
   slots (or use the DaVinci simulator):

   ```powershell
   python -m simulator.secsgem_equipment --port 5050 --wafers 1 --interval 1 --loop
   ```

2. In `production.yaml`, point a machine entry at that simulator
   (`host: "127.0.0.1"`, `port: 5050`, `hsms_mode: "active"`).

3. Watch the Linkstuffs device's **Latest telemetry** tab — events arrive
   within ~5 seconds.

4. Verify the per-lot CSV file appears under
   `D:\MachineData\EAP_<display_name>\csv_in\` on the server.

If both work, you're done. Restore the real machine IP/port in
`production.yaml` and restart the middleware service.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Devices not auto-creating | `is gateway` flag not set on the gateway device | Edit `ASTAR_EAP_GATEWAY` → Details → check "Is gateway" |
| Telemetry shows up but device stays "Inactive" | Inactivity timeout too short | Device profile → Default rule chain → adjust inactivity_timeout |
| Latest telemetry missing some keys | Middleware didn't include them (e.g. lot_id empty pre-lot) | Expected — wait for the lifecycle event that has them |
| `Not authorized` in middleware log | Wrong access token | Re-provision the service-account secret through the approved secret facility, then restart the service; never print the token |
| MQTT works locally but not over internet | TLS not enabled | `linkstuffs.tls: true` + `port: 8883` in production.yaml; install CA cert if Linkstuffs uses private CA |
| Alarm rules don't fire | Wrong condition key | Telemetry key is `event_type` not `eventType` (case sensitive) |
| All 22 devices show as one | Existing device is not marked as a gateway | Edit the existing device and enable **Is gateway** first to preserve references and history; reset/recreate only as a documented last resort |

---

## What the middleware does NOT do (so Linkstuffs handles it)

- **Long-term storage analytics** — Linkstuffs's PostgreSQL/Cassandra
  backend handles time-series queries. Middleware just publishes.
- **Per-device thresholds** — configure these in Linkstuffs alarm rules.
- **Email / Slack notifications** — Linkstuffs rule chains.
- **User access control** — Linkstuffs customer/user model.
- **Multi-tenant separation** — one Linkstuffs tenant per fab; ASTAR
  Gateway lives in that tenant.

The middleware is the ingestion layer; Linkstuffs is the analytics layer.

---

## Reference: ports + protocols

| What | Port | Direction | Notes |
|---|---|---|---|
| Middleware ↔ Linkstuffs MQTT | 1883 (plaintext) / 8883 (TLS) | outbound from middleware | Use TLS when this hop crosses untrusted networks |
| Middleware ↔ Equipment HSMS | 5000 (typical, configurable per tool) | outbound (active) or inbound (passive) per machine | Set in `production.yaml` `port:` field |
| Linkstuffs UI | 8080 (default) | inbound to Linkstuffs server | For administrators only |

---

## Reference: example payload screenshots in your tests

The full structure of every payload type the middleware sends is captured
in test outputs you can show your manager / operators:

```powershell
python -m pytest tests\test_davinci_pipeline_proof.py -s
```

Prints exact wire JSON for one alarm + wafer start/end +
measurement event from a DaVinci tool. The same shape applies for all
three vendor profiles.
