# ThingsBoard Community Edition — complete per-machine setup

Everything you create in ThingsBoard CE for each tool the middleware speaks
for, plus the `production.yaml` half that binds a tool to its CE device.

- Middleware install: [OPERATIONS.md](OPERATIONS.md)
- Transport/protocol background and the MQTT-gateway walkthrough:
  [LINKSTUFFS_SETUP.md](LINKSTUFFS_SETUP.md)
- This document is the per-machine reference: four tools, four device
  records, four token bindings, four alarm/dashboard sets.

Applies to ThingsBoard **Community Edition 3.6+ / 4.x** ("Linkstuffs" is the
hosted CE instance at `astar-monitoring.linkstuffs.com`). Menu labels below
are CE 3.6/4.x; if the site runs an older build the objects are the same but
the sidebar grouping differs.

## What CE gives you, and what it doesn't

| Needed for this middleware | CE | Note |
|---|---|---|
| Devices + access tokens | yes | one per tool |
| Gateway devices (`v1/gateway/*`) | yes | MQTT transport only |
| Device profiles | yes | alarm rules live here |
| Alarm rules on telemetry keys | yes | per profile, not per device |
| Dashboards, entity aliases | yes | |
| Rule chains (email/webhook out) | yes | SMTP configured by the sysadmin |
| Entity groups, per-group RBAC | **no** (PE only) | replace with naming discipline + one dashboard filter |
| White-labelling, scheduler, reports | **no** (PE only) | |

Because CE has no entity groups, the **device name is the only grouping key
you get**. The middleware already forces the right convention: a device's
name is exactly the machine's `display_name` from `production.yaml`, and the
`machine_profile` client attribute is what dashboard filters key on.

---

## 1. Pick the transport before you create anything

The middleware has two independent upstreams. They can run together, but the
per-machine work differs, so decide first.

| | `linkstuffs_http` (HTTPS device API) | `linkstuffs` (MQTT gateway) |
|---|---|---|
| CE objects | one **device per tool**, created by hand | one **gateway device** total |
| Credentials | one access token **per tool** | one gateway token |
| Device auto-creation | **no** — a missing device is a hard stop | yes, on first publish |
| Device profile assignment | manual, at creation | from the `type` field of `v1/gateway/connect` (= `machine_profile`) |
| Ports out of the fab | 8080 / 443 | 1883 / 8883 |
| Per-machine queue file | yes, one SQLite outbox per `endpoint_id` | one shared outbox |
| Recommended | **yes, for production** | fallback when only 443 leaves the site |

Everything below assumes the HTTPS path and calls out the MQTT differences
where they matter.

> **On the MQTT path, `v1/gateway/disconnect` is never published.** The
> middleware sends `v1/gateway/connect` when it prepares a machine and then
> nothing on the disconnect topic, so CE's gateway-managed online indicator
> never flips back. A tool going down is visible only through
> `event_type: connection_state` telemetry (§9) and CE's own inactivity
> timeout (§2.3). Build the "tool is down" alarm on those, on either transport.

> **The failure that looks like a ThingsBoard fault.** On HTTPS a tool with no
> token in `device_tokens` keeps running: HSMS connects, per-lot CSVs are
> written, and only the dashboard stays empty. The telemetry is not lost — it
> stays queued in that machine's outbox — but nothing tells the operator. On
> an unknown/wrong token CE answers `401`, the publisher treats 4xx as a
> permanent configuration fault, and after 5 attempts the row is dead-lettered.
> Create the device and issue its token **before** install.

---

## 2. One-time server-side objects

### 2.1 Device profiles — names are not free-form

Create four device profiles. On the MQTT path CE auto-creates devices and
assigns them to a profile **named after the `type` field the middleware
sends, which is the `machine_profile` string**. Use those exact names on the
HTTPS path too, so the two transports produce the same object graph:

| Device profile name | Machines | Transport type |
|---|---|---|
| `spts_fxp_omega` | SPTS_fxP_OMEGA_01 | Default |
| `davinci_200_mc4_hc1` | DAVINCI200_MC4_HC1_01 | Default |
| `ptiq_secsgem` | PTIQ_01 | Default |
| `nexgen_mg_series` | NEXGEN_MG_01 | Default |

**Profiles → Device profiles → + Add device profile.** Name it, leave
transport **Default** (that accepts both HTTP and MQTT device APIs), save.
Alarm rules get added per profile in the machine sections below.

Transport type **MQTT** on the profile is only needed if you want to override
the MQTT topic filters — the gateway topics are fixed and work under Default.

### 2.2 Gateway device — MQTT path only

**Entities → Devices → + Add new device**, name `ASTAR_EAP_GATEWAY`, and tick
**Is gateway**. Without that flag the broker accepts the publishes and
silently drops them, so the smoke test passes and no device ever appears.
Skip this section entirely on the HTTPS path.

### 2.3 Inactivity timeout — set it per machine, not globally

CE marks a device *Inactive* after `state.defaultInactivityTimeoutInSec`
(600 s out of the box, `DEFAULT_INACTIVITY_TIMEOUT` in `thingsboard.yml`).
That default is far too slow for a fab tool, and it is server-wide.

Override it per device with a **server-side attribute** `inactivityTimeout`,
in **milliseconds**:

**Device → Attributes → Server attributes → +**, key `inactivityTimeout`,
type Integer. Per-machine values are recommended in each section below; they
are derived from that machine's SVID poll interval, because the SVID sample
is the only telemetry a healthy-but-idle tool produces.

---

## 3. The per-machine recipe

Applied identically to all four tools; sections 4–7 fill in the values.

1. **Create the device.** Entities → Devices → + Add new device.
   Name = the machine's `display_name`, **character for character**. It is the
   key the publisher looks its token up under, and CE device names are
   case-sensitive.
2. **Assign the device profile** from the table in 2.1.
3. **Label** (optional, shown on dashboards): the tool's fab-floor name.
4. **Copy the access token.** Device → Manage credentials → Access token.
5. **Set `inactivityTimeout`** (server attribute, ms).
6. **Bind the token in `production.yaml`** under
   `linkstuffs_http.device_tokens.<display_name>`.
7. **Add the machine's alarm rules** to its device profile (2.1).
8. **Verify**: `test-linkstuffs`, then Latest telemetry on the device.

> **The `machines\<display_name>\config` folder creates itself.** On first
> start the service writes `DataCollectSwitch.json`, `RecipeList.json` and
> `SvidList.json` there with defaults (the SvidList seeded from the profile's
> identity SVIDs). Edit them afterwards — all three are hot-reloaded, no
> restart. The per-machine JSON shown in sections 4–7 replaces the seeded
> `SvidList.json`.

### The token binding, secret-free

`device_tokens` values are env-expanded, and a referenced variable that is not
set is a hard config error rather than a silent empty token:

```yaml
linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.linkstuffs.com"   # origin ONLY
  device_tokens:
    SPTS_fxP_OMEGA_01:     "${TB_TOKEN_SPTS_FXP_OMEGA_01}"
    DAVINCI200_MC4_HC1_01: "${TB_TOKEN_DAVINCI200_MC4_HC1_01}"
    PTIQ_01:               "${TB_TOKEN_PTIQ_01}"
    NEXGEN_MG_01:          "${TB_TOKEN_NEXGEN_MG_01}"
  timeout_sec: 10
  retry_count: 3
  retry_delay_sec: 1
  verify_tls: true
```

`base_url` is an **origin**. The publisher appends
`/api/v1/<token>/telemetry` itself; a full endpoint URL pasted here produces a
doubled path and every publish 404s.

Inject the four variables into the service account through the site's approved
secret facility. Do not use `setx`, do not put a token on a command line, and
do not paste one into a screenshot — the token is a write credential for that
device, and the publisher deliberately redacts it from its own logs.

### Per-machine transport overrides

Any machine can override the whole HTTPS route — useful when one tool sits
behind a different proxy or needs a longer timeout:

```yaml
  - endpoint_id: "TOOL_04"
    display_name: "NEXGEN_MG_01"
    linkstuffs_http:
      enabled: true
      base_url: "https://astar-monitoring.linkstuffs.com"
      device_token: "${TB_TOKEN_NEXGEN_MG_01}"
      verify_tls: true
      timeout_sec: 15
      retry_count: 5
      retry_delay_sec: 2
```

Each machine with an enabled route gets **its own publisher thread and its own
SQLite outbox file**, so one tool's backlog or dead-lettered rows never block
another's:

| endpoint_id | outbox file under `C:\SECSGEM_EAP\data\` |
|---|---|
| TOOL_01 | `linkstuffs_http_outbox.TOOL_01.34e10cc697.sqlite3` |
| TOOL_02 | `linkstuffs_http_outbox.TOOL_02.73c4ea4344.sqlite3` |
| TOOL_03 | `linkstuffs_http_outbox.TOOL_03.b648053b24.sqlite3` |
| TOOL_04 | `linkstuffs_http_outbox.TOOL_04.645537a06d.sqlite3` |

(The digest is `sha1(endpoint_id)[:10]`; it exists so two endpoint ids that
sanitise to the same filename cannot share a queue.)

### Optional: provision the four devices over REST

The UI is authoritative; this is only to save clicks. Run it from an admin
workstation, never with the password on the command line.

```powershell
$tb   = "https://astar-monitoring.linkstuffs.com"
$user = "tenant@yourfab.example"
$pass = Read-Host "ThingsBoard password" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pass))
$jwt = (Invoke-RestMethod -Method Post -Uri "$tb/api/auth/login" `
  -ContentType "application/json" `
  -Body (@{username=$user; password=$plain} | ConvertTo-Json)).token
$plain = $null
$h = @{ "X-Authorization" = "Bearer $jwt" }

$machines = @(
  @{ name = "SPTS_fxP_OMEGA_01";     profile = "spts_fxp_omega";      inactivity = 120000 },
  @{ name = "DAVINCI200_MC4_HC1_01"; profile = "davinci_200_mc4_hc1"; inactivity = 120000 },
  @{ name = "PTIQ_01";               profile = "ptiq_secsgem";        inactivity = 120000 },
  @{ name = "NEXGEN_MG_01";          profile = "nexgen_mg_series";    inactivity =  90000 }
)

$profiles = (Invoke-RestMethod -Headers $h `
  -Uri "$tb/api/deviceProfiles?pageSize=200&page=0").data

foreach ($m in $machines) {
  $p = $profiles | Where-Object { $_.name -eq $m.profile }
  if (-not $p) { Write-Warning "device profile $($m.profile) missing - create it first"; continue }
  $body = @{
    name = $m.name
    deviceProfileId = @{ entityType = "DEVICE_PROFILE"; id = $p.id.id }
  } | ConvertTo-Json -Depth 4
  $dev = Invoke-RestMethod -Method Post -Headers $h -Uri "$tb/api/device" `
    -ContentType "application/json" -Body $body
  $cred = Invoke-RestMethod -Headers $h -Uri "$tb/api/device/$($dev.id.id)/credentials"
  Invoke-RestMethod -Method Post -Headers $h -ContentType "application/json" `
    -Uri "$tb/api/plugins/telemetry/DEVICE/$($dev.id.id)/attributes/SERVER_SCOPE" `
    -Body (@{ inactivityTimeout = $m.inactivity } | ConvertTo-Json) | Out-Null
  "{0}  ->  {1}" -f $m.name, $cred.credentialsId
}
```

The last line prints each device's access token. Move them straight into the
secret store as `TB_TOKEN_<DISPLAY_NAME>`; do not leave the console scrollback
open. Check the payload shapes against your instance's own
`/swagger-ui.html` if it is not on 3.6/4.x.

---

## 4. `SPTS_fxP_OMEGA_01` — SPTS fxP Omega 200mm

### Identity

| | |
|---|---|
| `endpoint_id` | `TOOL_01` |
| `display_name` / CE device name | `SPTS_fxP_OMEGA_01` |
| `machine_profile` / CE device profile | `spts_fxp_omega` |
| vendor / model attributes | `SPTS` / `fxP Omega 200mm` |
| Subscribed events | 96 (`output/spts_fxp_omega/EventSubscription.json`) |
| Profile SVIDs / DVs | 158 / 16 |
| Load ports seen in telemetry | `1` = VCE A, `2` = VCE B |
| Chamber key | always `NA` — the Omega encodes the PM in the alarm id, not the event |
| HSMS timers (tool's own, manual §4.4 Table 3) | T3 30, T5 5, T6 10, T7 5, T8 6 |
| `enable_alarms` default | `false` — S5F3 is not sent unless you opt in |

### CE objects

- Device `SPTS_fxP_OMEGA_01`, profile `spts_fxp_omega`.
- Server attribute `inactivityTimeout` = `120000` (2 min).
- `TB_TOKEN_SPTS_FXP_OMEGA_01` in the secret store.

### SVID collection — `C:\SECSGEM_EAP\machines\SPTS_fxP_OMEGA_01\config\SvidList.json`

Shipped default is the identity set only. Telemetry key = `raw_svid_<Name>`.

```json
{
  "RecipeSvidList": [],
  "SvidList": [
    "MDLN", "SOFTREV", "ControlState", "LastCEID", "Clock",
    "AlarmsSet", "EquipmentReady", "TransportState",
    "MchRunningWaferCount", "MchTotalWaferCount", "EnergyConsumption",
    "PM1State", "PM1Mode", "PM1RunningWaferCount", "PM1ModuleRecipe",
    "PM2State", "PM2RunningWaferCount",
    "VCEAProcessState", "VCEALotid", "VCEBProcessState", "VCEBLotid",
    "SpoolCountActual"
  ]
}
```

| SVID | id | CE telemetry key | Widget |
|---|---|---|---|
| ControlState | 28 | `raw_svid_ControlState` | value card |
| AlarmsSet | 24 | `raw_svid_AlarmsSet` | alarm rule input |
| MchRunningWaferCount | 1720 | `raw_svid_MchRunningWaferCount` | line chart |
| MchTotalWaferCount | 1700 | `raw_svid_MchTotalWaferCount` | counter |
| EnergyConsumption | 5103 | `raw_svid_EnergyConsumption` | line chart |
| PM1RunningWaferCount | 1721 | `raw_svid_PM1RunningWaferCount` | line chart |
| PM1State / PM2State | 1552 / 1553 | `raw_svid_PM1State` … | state cards |
| VCEALotid / VCEBLotid | 1641 / 1642 | `raw_svid_VCEALotid` … | value cards |
| SpoolCountActual | 2016 | `raw_svid_SpoolCountActual` | alarm rule input |

Sampling cadence comes from `DataCollectSwitch.json` in the same folder
(`DataIntervalInSec`, default **1** — see §11 before leaving it there).

### Alarm rules on device profile `spts_fxp_omega`

The Omega is the one tool that tells you *where* the alarm came from: its
ALID is arithmetic over station and station type (manual §8.3), and the
middleware decodes it into extra telemetry keys. Use those instead of numeric
ALID ranges.

| Alarm type | Condition (all keys are Timeseries) |
|---|---|
| `Equipment Alarm` (Critical) | `event_type` = `alarm` **AND** `raw_event_name` = `AlarmSet` **AND** `raw_alid` > `0` |
| Clear rule for the above | `event_type` = `alarm` **AND** `raw_event_name` = `AlarmCleared` |
| `Alarm Storm` (Major) | `event_type` = `alarm` **AND** `raw_alid` = `-1` |
| `Spooled Data Pending` (Warning) | `raw_svid_SpoolCountActual` > `0` |

`raw_alid > 0` is not cosmetic: the middleware emits `raw_alid = 0` for
"alarm state unknown after reconnect" and `raw_alid = -1` for the storm
summary. A rule that only tests `event_type = alarm` fires on both.

Alarm telemetry this machine produces:

`raw_alid`, `raw_alcd`, `raw_altx`, `raw_alarm_source`, `raw_alarm_station`,
`raw_alarm_station_type`, `raw_alarm_offset`

`raw_alarm_source` reads like `Process Module 1 (Etch PM) #5` — put it in the
alarm details template so the operator knows which module to walk to:

```
${raw_altx} — ${raw_alarm_source} (ALID ${raw_alid})
```

### Dashboard widgets

- **Entities table** filtered to this device: `lot_id`, `recipe`, `load_port`,
  `event_type`, `raw_svid_ControlState`.
- **Timeseries line chart**: `raw_svid_MchRunningWaferCount`,
  `raw_svid_PM1RunningWaferCount`, `raw_svid_EnergyConsumption`.
- **Two state cards** for VCE A / VCE B: `raw_svid_VCEAProcessState`,
  `raw_svid_VCEBProcessState`.

### Machine-specific notes

- Alarm CEIDs are computed per tool layout, so the shipped profile carries
  none: alarms reach CE via **S5F1**, which needs the tool's own alarm
  reporting on. Set `enable_alarms: true` for this machine if the tool is not
  already configured to send them.
- The tool documents Connect Mode as **Passive**, which is why the middleware
  dials out (`hsms_mode: "active"`).
- Events name their load port through the CEID (`*1` = VCE A, `*2` = VCE B),
  so `load_port` is populated even when the report carries no PortID.

---

## 5. `DAVINCI200_MC4_HC1_01` — MueTec DaVinci 200 MC4 HC1

### Identity

| | |
|---|---|
| `endpoint_id` | `TOOL_02` |
| `display_name` / CE device name | `DAVINCI200_MC4_HC1_01` |
| `machine_profile` / CE device profile | `davinci_200_mc4_hc1` |
| vendor / model attributes | `MueTec` / `DaVinci 200 MC4 HC1` |
| Subscribed events | 54 (`output/davinci200_mc4_hc1/EventSubscription.json`) |
| Profile SVIDs / DVs | 114 / 18 |
| Load ports | `1`, `2` (LP1/LP2, from the CEID name) |
| HSMS timers (manual §4.3.1.2) | T3 45, T5 10, T6 5, T7 10, T8 5 |
| `enable_alarms` default | `false` |

### CE objects

- Device `DAVINCI200_MC4_HC1_01`, profile `davinci_200_mc4_hc1`.
- Server attribute `inactivityTimeout` = `120000`.
- `TB_TOKEN_DAVINCI200_MC4_HC1_01` in the secret store.

### SVID collection — `…\machines\DAVINCI200_MC4_HC1_01\config\SvidList.json`

```json
{
  "RecipeSvidList": [],
  "SvidList": [
    "ControlState", "ProcessState", "PM1/RecipeName",
    "AlarmsSet", "PPError",
    "MainPressure", "MainVacuumPM", "MainVacuumEFEM",
    "FFUGaugePressurePM", "FFUGaugePressureEFEM1", "FFUGaugePressureEFEM2",
    "Vacuum8PM1", "Vacuum12PM1",
    "QueuedCJobs", "QueueAvailableSpace",
    "LP1/CarrierID", "LP1/PortTransferState",
    "LP2/CarrierID", "LP2/PortTransferState",
    "SpoolCountActual"
  ]
}
```

| SVID | id | CE telemetry key |
|---|---|---|
| ControlState | 1010001 | `raw_svid_ControlState` |
| ProcessState | 1050001 | `raw_svid_ProcessState` |
| PM1/RecipeName | 1060007 | `raw_svid_PM1/RecipeName` |
| MainPressure | 1170004 | `raw_svid_MainPressure` |
| FFUGaugePressurePM | 1170001 | `raw_svid_FFUGaugePressurePM` |
| MainVacuumPM / MainVacuumEFEM | 1170006 / 1170005 | `raw_svid_MainVacuumPM` … |
| Vacuum8PM1 / Vacuum12PM1 | 1170007 / 1170008 | `raw_svid_Vacuum8PM1` … |
| QueuedCJobs | 1100002 | `raw_svid_QueuedCJobs` |
| LP1/CarrierID | 1120001 | `raw_svid_LP1/CarrierID` |
| SpoolCountActual | 1030001 | `raw_svid_SpoolCountActual` |

> **Slashes in key names.** This vendor's SV names contain `/`
> (`PM1/RecipeName`, `LP1/CarrierID`), and the middleware keeps them, so the CE
> telemetry key really is `raw_svid_PM1/RecipeName`. Widgets accept it as
> typed; the **REST API does not** — `?keys=raw_svid_PM1/RecipeName` must be
> URL-encoded as `raw_svid_PM1%2FRecipeName`. If a downstream consumer cannot
> cope, drop the slashed SVs from `SvidList.json` and read the same values
> from the event reports instead.

### Alarm rules on device profile `davinci_200_mc4_hc1`

This tool reports alarms **as collection events**, not only as S5F1:
CEID `3020001` = AlarmNDetected, CEID `3020002` = AlarmNCleared, each carrying
DVs `AlarmID`, `AlarmCode`, `AlarmText`. Those arrive as `raw_AlarmID`,
`raw_AlarmCode`, `raw_AlarmText` — different keys from the S5F1 path's
`raw_alid`/`raw_alcd`/`raw_altx`. Cover both.

| Alarm type | Condition |
|---|---|
| `Equipment Alarm` (Critical) | `ceid` = `3020001` |
| Clear rule | `ceid` = `3020002` |
| `Equipment Alarm (S5F1)` (Critical) | `event_type` = `alarm` AND `raw_event_name` = `AlarmSet` AND `raw_alid` > `0` |
| `Process Program Error` (Major) | `raw_svid_PPError` ≠ `0` |
| `Spooled Data Pending` (Warning) | `raw_svid_SpoolCountActual` > `0` |

Details template for the CEID rule:

```
${raw_AlarmText} (ALID ${raw_AlarmID}, code ${raw_AlarmCode})
```

### Dashboard widgets

- **Timeseries line chart** — facility pressures: `raw_svid_MainPressure`,
  `raw_svid_FFUGaugePressurePM`, `raw_svid_FFUGaugePressureEFEM1`,
  `raw_svid_FFUGaugePressureEFEM2`.
- **Timeseries line chart** — vacuum: `raw_svid_MainVacuumPM`,
  `raw_svid_Vacuum8PM1`, `raw_svid_Vacuum12PM1`.
- **Entities table**: `lot_id`, `wafer_id`, `recipe`, `load_port`,
  `raw_CtrlJobID`, `raw_PRJobID`.
- **Latest values card**: `raw_svid_QueuedCJobs`, `raw_svid_QueueAvailableSpace`.

### Machine-specific notes

- Measurement reports (`raw_TestResults`, `raw_ResultFile`) arrive as nested
  SECS lists and are stored as **JSON strings**. CE will not chart them; show
  them in a table/markdown widget, or unpack them in a rule chain first.
- The complete raw report set is always attached as `raw_secs_reports`
  (JSON string) — that is the audit copy when a report has no profile layout.
- If events stop but the link stays up, the middleware polls `LastEventID`
  (1010004) and publishes `event_type: connection_state` /
  `raw_event_name: no_event_reports`. See §9 — it usually means the
  HostInterface is in E40 event style (S16 instead of S6F11) or is spooling.

---

## 6. `PTIQ_01` — PTIQ SECS/GEM equipment

### Identity

| | |
|---|---|
| `endpoint_id` | `TOOL_03` |
| `display_name` / CE device name | `PTIQ_01` |
| `machine_profile` / CE device profile | `ptiq_secsgem` |
| vendor / model attributes | `PTIQ` / `SECS/GEM Equipment` |
| Subscribed events | 8, from the **generic** `config/EventSubscription.json` |
| Profile SVIDs / DVs | 26 / 12 |
| CEID aliases in the profile | **0** — numbers come from the per-tool EIB export |
| HSMS timers | T3 45, T5 10, T6 5, T7 10, T8 5 (shipped default; no vendor figures) |
| `enable_alarms` default | `false` |

### CE objects

- Device `PTIQ_01`, profile `ptiq_secsgem`.
- Server attribute `inactivityTimeout` = `120000`.
- `TB_TOKEN_PTIQ_01` in the secret store.

### The one thing that is different about this machine

The PTIQ host-interface spec is generic: it names events
(`ProcessingStarted`, `SCHn.LotStarted`, `MaterialReceived`, …) but the
**CEID and SVID numbers are per-equipment**, published in that tool's EIB
model export. Until you load the tool's real numbers:

- Every event still reaches CE — nothing is dropped — but it lands with
  `event_type: unknown` and whatever `raw_event_name` the tool sent.
- Alarm/dashboard rules keyed on `event_type` will not match.
- The first unrecognised CEID per tool is logged once as a warning
  (`Unknown CEID … will be captured as 'unknown'`).

So the per-machine setup for PTIQ has an extra step **before** the CE work is
worth doing:

1. Get the EIB model export from the tool.
2. Write its CEIDs into a per-machine subscription file and point the machine
   at it: `event_subscription_path: "config/EventSubscription.PTIQ_01.json"`.
3. Write its SVID numbers into
   `…\machines\PTIQ_01\config\SvidList.json` as `{SVID, Name}` pairs.

```json
{
  "RecipeSvidList": [],
  "SvidList": [
    "MDLN", "SOFTREV", "ControlState", "ProcessState", "Clock",
    "AlarmsSet", "AlarmsCount", "PPExecName", "PPError", "SpoolCountActual",
    { "SVID": 5001, "Name": "ChamberTemp" },
    { "SVID": 5002, "Name": "ChamberPressure" }
  ]
}
```

> **Key naming for numeric SVIDs.** The telemetry key comes from the
> **profile's** name table, not from the `Name` you write in `SvidList.json`.
> An SVID the `ptiq_secsgem` profile does not know publishes as
> `raw_svid_SVID_5001`, not `raw_svid_ChamberTemp`. Either build your CE
> widgets against `raw_svid_SVID_<n>`, or get the numbers added to the profile
> so both sides agree.

Named SVIDs the profile already resolves: MDLN 32, SOFTREV 39, ControlState 28,
ProcessState 1029, Clock 27, AlarmsSet 24, AlarmsCount 26, AlarmState 25,
AlarmID 22, PPExecName 1040, PPError 1042, LastCEID 34, CommState 31,
SpoolCountActual 2016, EventsEnabled 30.

### Alarm rules on device profile `ptiq_secsgem`

No alarm CEIDs are known for this profile, so alarms arrive on **S5F1** only.
Set `enable_alarms: true` on this machine if the tool does not already have
alarm reporting enabled — otherwise this rule never fires.

| Alarm type | Condition |
|---|---|
| `Equipment Alarm` (Critical) | `event_type` = `alarm` AND `raw_event_name` = `AlarmSet` AND `raw_alid` > `0` |
| Clear rule | `event_type` = `alarm` AND `raw_event_name` = `AlarmCleared` |
| `Unmapped events` (Warning, Duration ≥ 15 min) | `event_type` = `unknown` — tells you the EIB numbers are still missing |
| `Process Program Error` (Major) | `raw_svid_PPError` ≠ `0` |

### Dashboard widgets

Keep it minimal until the EIB export lands:

- **Entities table**: `event_type`, `raw_event_name`, `ceid`, `lot_id`.
- **Latest values**: `raw_svid_ControlState`, `raw_svid_ProcessState`,
  `raw_svid_AlarmsCount`.

### Machine-specific notes

- `ceid_load_port` and `ceid_chamber` are both empty for this profile, so
  `load_port` is only populated when the report itself carries a PortID, and
  `chamber` is always `NA`. Do not build a per-port dashboard for this tool
  until the EIB layout is loaded.
- This is the only machine whose shipped `event_subscription_path` points at
  the shared `config/EventSubscription.json`. Give it a per-machine file as
  soon as you have real numbers, or a second PTIQ tool will inherit the first
  one's CEIDs.

---

## 7. `NEXGEN_MG_01` — NexGen Wafersystems MG Series (MG21 / MG22 / MG22-300)

The largest and the most constrained of the four. Read the notes before
building the dashboard.

### Identity

| | |
|---|---|
| `endpoint_id` | `TOOL_04` |
| `display_name` / CE device name | `NEXGEN_MG_01` |
| `machine_profile` / CE device profile | `nexgen_mg_series` |
| vendor / model attributes | `NexGen Wafersystems` / `MG Series (MG21/MG22/MG22-300)` |
| Subscribed events | 243 (`output/nexgen_mg_series/EventSubscription.json`) |
| Profile SVIDs / DVs | 251 / 457 |
| Load ports | `1`–`4` |
| Chambers | `PM1`, `PM2` |
| HSMS timers | T3 45, T5 10, T6 5, T7 10, T8 5 — **another vendor's defaults**; the MG manual states none |
| `enable_alarms` default | **`true`** (only profile where it is) |
| `request_online` default | **`true`** — S1F17, or an OFF-LINE MG discards every host primary |
| `alarm_rate_limit` default | **50** per window |

One device name covers all three MG variants on purpose: the manual publishes
one CEID table for MG21/MG22/MG22-300, and a variant-specific device name
would force a token reissue if the variant guess turned out wrong.

### CE objects

- Device `NEXGEN_MG_01`, profile `nexgen_mg_series`.
- Server attribute `inactivityTimeout` = `90000` (tighter than the others —
  see the spooling note below).
- `TB_TOKEN_NEXGEN_MG_01` in the secret store.

### SVID collection — `…\machines\NEXGEN_MG_01\config\SvidList.json`

251 SVIDs are available. Collecting them all at 1 s would put ~265 datapoints
per second into CE from this tool alone. Start here:

```json
{
  "RecipeSvidList": [],
  "SvidList": [
    "ControlState", "ProcessState", "LastEventID", "Clock",
    "pm1State", "pm2State", "pm1WaferCount", "pm2WaferCount",
    "pm1ChuckSpeed", "pm2ChuckSpeed",
    "pm1Med1Temp", "pm1Med1Flow", "pm1DiFlow", "pm1N2DryFlow", "pm1Exhaust",
    "facSupplyCdaPressure", "facSupplyDiwPressure",
    "facSupplyN2PressureLeft", "facSupplyN2PressureRight",
    "facSupplyVacuumPressure",
    "med1BathLifeTimeRem", "med1EtchRate", "diwO3Concentration",
    "port1Status", "port1LotId", "port2Status", "port2LotId",
    "lightTowerStatus", "QueuedCJobs"
  ]
}
```

| SVID | id | CE telemetry key | Use |
|---|---|---|---|
| ControlState | 11 | `raw_svid_ControlState` | online/offline card |
| ProcessState | 15 | `raw_svid_ProcessState` | state card |
| pm1State / pm2State | 3550 / 3750 | `raw_svid_pm1State` … | per-chamber state |
| pm1WaferCount / pm2WaferCount | 3531 / 3731 | `raw_svid_pm1WaferCount` … | throughput chart |
| pm1ChuckSpeed | 3530 | `raw_svid_pm1ChuckSpeed` | process chart |
| pm1Med1Temp / pm1Med1Flow | 3503 / 3506 | `raw_svid_pm1Med1Temp` … | process chart |
| facSupplyCdaPressure | 4000 | `raw_svid_facSupplyCdaPressure` | facility alarm |
| facSupplyN2PressureLeft / Right | 4001 / 4002 | `raw_svid_facSupplyN2PressureLeft` … | facility alarm |
| facSupplyDiwPressure | 4004 | `raw_svid_facSupplyDiwPressure` | facility alarm |
| facSupplyVacuumPressure | 4003 | `raw_svid_facSupplyVacuumPressure` | facility alarm |
| med1BathLifeTimeRem | 4320 | `raw_svid_med1BathLifeTimeRem` | consumable gauge |
| med1EtchRate | 4310 | `raw_svid_med1EtchRate` | process chart |
| diwO3Concentration | 4450 | `raw_svid_diwO3Concentration` | process chart |
| port1Status / port1LotId | 3120 / 3131 | `raw_svid_port1Status` … | port table |
| lightTowerStatus | 4303 | `raw_svid_lightTowerStatus` | status card |

### Alarm rules on device profile `nexgen_mg_series`

CEIDs `8` (AlarmDetected) and `9` (AlarmCleared) exist but **link no report**,
so they arrive with no alarm id attached. The identifying data comes from
**S5F1** — which is why `enable_alarms` defaults to true on this profile.

| Alarm type | Condition |
|---|---|
| `Equipment Alarm` (Critical) | `event_type` = `alarm` AND `raw_event_name` = `AlarmSet` AND `raw_alid` > `0` |
| Clear rule | `event_type` = `alarm` AND `raw_event_name` = `AlarmCleared` |
| `Alarm Storm` (Major) | `event_type` = `alarm` AND `raw_alid` = `-1` — the rate limiter shed alarms; the ALIDs are listed in `raw_altx` |
| `Alarm state unknown` (Warning) | `event_type` = `alarm` AND `raw_alid` = `0` — raised on reconnect; this tool cannot report its active alarm set |
| `Facility: N2 pressure` (Major) | `raw_svid_facSupplyN2PressureLeft` < site limit |
| `Facility: CDA pressure` (Major) | `raw_svid_facSupplyCdaPressure` < site limit |
| `Bath life low` (Warning) | `raw_svid_med1BathLifeTimeRem` < site limit |

### Dashboard widgets

- **Two chamber columns.** Filter the same device's telemetry by the
  `chamber` key (`PM1` / `PM2`) — every chamber event carries it, including
  the 129 step events that link no report.
- **Timeseries line chart per chamber**: `raw_svid_pm1ChuckSpeed`,
  `raw_svid_pm1Med1Flow`, `raw_svid_pm1Med1Temp` (and the `pm2*` twins).
- **Facility strip**: the five `raw_svid_facSupply*` keys on one chart.
- **Port table**: `raw_svid_port1Status` … `raw_svid_port4Status` with
  `port<N>LotId` beside them.
- **Consumables**: `raw_svid_med1BathLifeTimeRem`, `raw_svid_med1EtchRate`,
  `raw_svid_diwO3Concentration`.

### Machine-specific notes — read these before trusting the dashboard

- **No spooling.** The MG documents spooling as unsupported and all four spool
  SVs as not supported. There is no equipment-side buffer, so middleware
  downtime is **unrecoverable data loss**, not a backlog that drains later.
  That is why this device gets the tightest `inactivityTimeout` and why the
  connection-state alarm in §9 matters more here than anywhere else.
- **129 of the 243 subscribed events carry no report at all** (the step family
  222–231 / 322–331 and most of the chamber band). They still reach CE, and
  the middleware attributes them to a chamber from the binding the
  wafer-level reports leave behind. A named chamber with no binding yields
  nothing rather than borrowing a machine-wide guess — so an occasional empty
  `load_port` on a step event is correct behaviour, not a gap.
- **328 of the event aliases classify as `control_state`.** Do not put
  `event_type` on a pie chart for this tool; it will be one slice. Use
  `raw_event_name` or `ceid` for breakdowns.
- **The profile is documentation-derived and not hardware-verified.** The
  manual disclaims its own constants ("may change without prior notice") and
  states no port, device id or HSMS role — `port`, `secs_device_id` and
  `hsms_mode` in `production.yaml` are guesses matching the other three tools.
  Correct them on site from the tool's SECS/GEM screen; all three are
  configuration, so a wrong guess never needs a rebuild.
- **Alarm rate limit 50/window** is on by default here alone. Shed alarms are
  still on disk in the ingress journal with the reason; CE sees one
  `AlarmStormSummary` event instead of the flood.

---

## 8. What every device receives — key reference

### Client attributes (all four devices, same 10 keys)

Published when the service prepares a machine — at startup and on every
config reload — not per HSMS connect. The outbox dedups on a payload hash, so
an unchanged attribute set is not re-sent:

`endpoint_id`, `display_name`, `machine_profile`, `vendor`, `model`,
`secs_host`, `secs_port`, `secs_device_id`, `csv_local_path`,
`csv_network_path`

These are **client-side** attributes on both transports. Dashboard entity
aliases filter on them; `machine_profile` is the one to group by.

`secs_host`, `secs_port` and `secs_device_id` go to CE as attributes. If
equipment-network addresses must not leave the fab network, that is a reason
to keep this CE instance inside it.

### Telemetry keys present on every event

`endpoint_id`, `display_name`, `machine_profile`, `vendor`, `model`,
`event_type`, `raw_event_name`, `ceid`, `load_port`, `chamber`, `lot_id`,
`wafer_id`, `recipe`, `secs_raw_event` — 14 keys, plus `raw_*` for every
data variable the report carried, plus `raw_secs_reports` (JSON string) when
a report had no profile layout.

For alarm events, `ceid` **is the ALID**.

### `event_type` domain

| `event_type` | Fires when |
|---|---|
| `lot_start` / `lot_end` | control job / cassette starts / completes |
| `wafer_start` / `wafer_end` | E90 substrate enters / leaves processing |
| `process_start` / `process_end` | PM recipe start / finish |
| `ready_to_load` / `ready_to_unload` | port ready |
| `loaded` / `unloaded` | carrier arrives / departs a load port |
| `clamped` / `unclamped` | carrier clamping |
| `mounted` / `unmounted` | material received / removed |
| `mapped` | slot map result (MG) |
| `recipe_selected` / `recipe_step` | PP selection / step change |
| `control_state` | Online/Offline, EC/PP change, per-cassette state |
| `alarm` | S5F1 set/clear, or a vendor alarm CEID |
| `svid_sample` | periodic SVID poll (`SvidList.json`) |
| `connection_state` | middleware-side link health — see §9 |
| `unknown` | CEID not in the profile; still captured |

---

## 9. Link health — the alarm rules that apply to all four devices

`event_type: connection_state` is generated by the middleware, not the tool,
so it arrives even when the tool is unreachable — as long as the middleware
itself is alive. `raw_event_name` carries the state:

| `raw_event_name` | Meaning | Suggested severity |
|---|---|---|
| `connected` | HSMS up | clear |
| `disconnected` | HSMS down | Critical |
| `reconnect_attempted` | watchdog retrying | — |
| `reconnect_failed` | one retry failed | Warning |
| `reconnect_failing` | retries failing persistently | Critical |
| `runtime_error` | session raised; details in `raw_details` | Critical |
| `no_event_reports` | link up, subscription acked, **zero reports arriving** | Critical |
| `event_reports_ok` | recovered from the above | clear |
| `no_status_response` | tool not answering S1F3 | Major |
| `spooled_messages_pending` | tool has buffered messages the middleware did not drain | Warning |

Add to **each** of the four device profiles:

| Alarm type | Create when | Clear when |
|---|---|---|
| `Tool link down` (Critical) | `event_type`=`connection_state` AND `raw_event_name`=`disconnected` | `raw_event_name`=`connected` |
| `Tool reporting nothing` (Critical) | `raw_event_name`=`no_event_reports` | `raw_event_name`=`event_reports_ok` |
| `Reconnect failing` (Critical) | `raw_event_name`=`reconnect_failing` | `raw_event_name`=`connected` |

Also keep CE's own inactivity flag (§2.3) as the backstop: it is the only
signal that survives the **middleware** dying, which
`connection_state` cannot report.

`raw_details` carries the free-text reason; put `${raw_details}` in the alarm
details template.

---

## 10. Dashboard assembly

One dashboard, four device-scoped tabs, plus an overview:

1. **Dashboards → + Add new dashboard**, name `ASTAR Fab Overview`.
2. **Entity aliases**:
   - `all_tools` — filter type **Device type**, pick the four device profiles.
   - one **Single entity** alias per machine (`spts_01`, `davinci_01`,
     `ptiq_01`, `mg_01`) for the per-tool tabs.
3. **Overview tab** — Entities table on `all_tools` with columns
   `display_name`, `event_type`, `lot_id`, `recipe`, `load_port`, plus an
   **Alarms** widget scoped to `all_tools`.
4. **Per-tool tabs** — the widget lists from §4–§7.

Numeric-looking values arrive with the type the tool sent them as. A value the
equipment reports as ASCII stays a string in CE and will not chart; if a key
you expect to plot renders as text, the SECS item type is the cause, not the
widget.

---

## 11. Data-rate budget — this is a CE instance, not a cluster

CE stores every telemetry key as its own row (PostgreSQL by default). The
dominant cost is the SVID poll, because it fires whether or not the tool is
doing anything.

Per machine, per sample: **14 base keys + one key per collected SVID**.
At the shipped `DataIntervalInSec: 1`:

| SvidList size | keys/sec/machine | 4 machines |
|---|---|---|
| 4–5 (shipped default) | ~19 | ~76/s |
| ~25 (recommended sets above) | ~39 | ~156/s |
| all 251 (MG, everything) | ~265 | ~420/s |

Practical guidance:

- Set `DataIntervalInSec` to **5–10** in each machine's
  `DataCollectSwitch.json` unless a specific SV genuinely needs 1 s
  resolution. Event reports are unaffected — they arrive when the tool fires
  them.
- Keep per-machine SvidLists to what a dashboard or alarm rule actually
  reads. An SV nobody looks at costs storage forever.
- `DataCollectSwitch: "OFF"` stops the poll entirely for one machine without
  touching event reporting.
- Both files are hot-reloaded — no service restart.

---

## 12. Per-machine verification

Run after each machine is wired, on the Windows server:

```powershell
cd C:\SECSGEM_EAP\app
python -m eap_middleware validate-config --config config\production.yaml
```

Fails loudly on: a missing token for an enabled machine, a `base_url` with a
path in it, an env var referenced but not set, and an enabled machine with no
upstream route at all.

```powershell
python -m eap_middleware test-linkstuffs --config config\production.yaml
```

Prints `https-ok: <display_name>` per enabled machine. It performs a real
`GET /api/v1/<token>/attributes?clientKeys=endpoint_id` against CE, so it
proves that machine's token end to end — not just that the host is reachable.
`https-fail: … no device token` means step 6 of §3 was skipped for that tool.

Then, per machine:

| Check | Where | Expect |
|---|---|---|
| Device exists with the exact name | CE → Entities → Devices | one row per `display_name` |
| Attributes landed | Device → Attributes → Client attributes | the 10 keys from §8 |
| Telemetry landing | Device → Latest telemetry | `event_type`, `raw_svid_*` updating |
| Device active | Devices list | green **Active** |
| Nothing stuck locally | `dir C:\SECSGEM_EAP\data\linkstuffs_http_outbox.*` | file small and not growing |
| Per-lot CSVs written | `local_csv_path` for that machine | one file per lot |

If a machine's outbox has dead-lettered rows because its token was wrong, fix
the token and re-queue just that machine:

```powershell
python -m eap_middleware outbox-requeue --config config\production.yaml --endpoint-id TOOL_04
```

Without `--endpoint-id` every outbox the configuration owns is re-queued.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| One tool's device is empty, others fine | no/wrong token for that `display_name` | add it to `device_tokens`, then `outbox-requeue --endpoint-id` |
| Device name mismatch (`SPTS_FXP_OMEGA_01` vs `SPTS_fxP_OMEGA_01`) | CE names are case-sensitive; the map key is the `display_name` | rename the CE device, do not rename the machine |
| Every publish 404s | `base_url` has a path | origin only — the publisher appends `/api/v1/<token>/…` |
| 401 then nothing | token revoked/rotated | reissue, update the secret, re-queue |
| Telemetry arrives, device shows Inactive | `inactivityTimeout` shorter than the SVID interval | raise the attribute or lower `DataIntervalInSec` |
| Alarm rule never fires | wrong key: `event_type` not `eventType`; DaVinci alarms use `raw_AlarmID` not `raw_alid` | see §5 |
| Alarm fires on every reconnect | rule matches `raw_alid` 0 (state-unknown) or -1 (storm summary) | add `raw_alid > 0` |
| MG dashboard mostly one `event_type` | 328 aliases are `control_state` | break down by `raw_event_name` or `ceid` |
| PTIQ events all `unknown` | EIB model export not loaded | §6 |
| Key with `/` rejected by REST | DaVinci SV names contain `/` | URL-encode as `%2F` |
| Devices not auto-creating (MQTT path) | `Is gateway` not ticked | edit `ASTAR_EAP_GATEWAY`, tick it |
| All tools appear as one device (MQTT path) | existing device is not a gateway | enable **Is gateway** on it — preserves history; recreating is a last resort |
| Postgres growing fast | SVID poll too wide/too fast | §11 |

---

## 14. Machine-to-CE summary

| endpoint_id | CE device name | CE device profile | Token variable | Alarms via | Chambers | Ports |
|---|---|---|---|---|---|---|
| TOOL_01 | `SPTS_fxP_OMEGA_01` | `spts_fxp_omega` | `TB_TOKEN_SPTS_FXP_OMEGA_01` | S5F1 + decoded source | — | 1, 2 |
| TOOL_02 | `DAVINCI200_MC4_HC1_01` | `davinci_200_mc4_hc1` | `TB_TOKEN_DAVINCI200_MC4_HC1_01` | CEID 3020001/3020002 (+S5F1) | — | 1, 2 |
| TOOL_03 | `PTIQ_01` | `ptiq_secsgem` | `TB_TOKEN_PTIQ_01` | S5F1 only | — | from payload |
| TOOL_04 | `NEXGEN_MG_01` | `nexgen_mg_series` | `TB_TOKEN_NEXGEN_MG_01` | S5F1 (CEID 8/9 carry no data) | PM1, PM2 | 1–4 |
