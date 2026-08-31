# SECS/GEM EAP Middleware Operations

## Client Setup Contract

The client configures machine connectivity and selects a known profile:

```yaml
endpoint_id: TOOL_01
display_name: SPTS_fxP_OMEGA_01
machine_profile: spts_fxp_omega
host: 192.0.2.31  # TEST-NET placeholder; replace with the equipment IP
port: 5000
secs_device_id: 0
enabled: true
```

ThingsBoard HTTPS is the recommended production upstream. The tracked template
keeps all machines and transports disabled and contains no live secrets. Every
enabled machine using HTTPS must resolve a device token keyed by its exact
`display_name`. MQTT gateway mode is an explicit, disabled-by-default fallback.

## Commands

```powershell
python -m eap_middleware list-profiles --json
python -m eap_middleware validate-config --config config\production.yaml
python -m eap_middleware init-admin-config --config config\production.yaml
python -m eap_middleware test-linkstuffs --config config\production.yaml
python -m eap_middleware test-machine --config config\production.yaml --endpoint-id ALL
python -m eap_middleware run-service --config config\production.yaml
```

The service watches `production.yaml` for atomic replacements. A valid change
is reconciled by `endpoint_id`; unchanged sessions keep their connection,
connection/profile changes restart only that endpoint, and storage or HTTPS
changes are applied without reconnecting HSMS. Invalid YAML is reported in
`<paths.data_dir>\runtime_status.json` and never replaces the last valid
runtime configuration.

The desktop control panel is a passive client. It writes unique restart and
connection-test requests under `<paths.data_dir>\commands` and reads the status
snapshot above. Closing the panel does not stop the Windows service.

## Admin SVID Files

Each machine has an admin folder, for example:

```text
C:\SECSGEM_EAP\machines\SPTS_fxP_OMEGA_01\config
```

The middleware reads and hot-reloads:

```text
DataCollectSwitch.json
RecipeList.json
SvidList.json
```

`DataCollectSwitch.json`:

```json
{
  "DataCollectSwitch": "ON",
  "DataIntervalInSec": 1
}
```

`RecipeList.json`:

```json
{
  "Recipe_List": ["RCP1", "RCP2", "RCP3", "RCP4"]
}
```

`SvidList.json`:

```json
{
  "RecipeSvidList": ["RCP1", "RCP2", "RCP3", "RCP4"],
  "SvidList": [
    "Stat3_Etch_MV_Heater1Temp",
    "Stat3_Etch_MV_Heater2Temp",
    "Stat3_Etch_MV_Pressure"
  ]
}
```

Engineering may also use direct IDs:

```json
{"SVID": 1001, "Name": "Stat3_Etch_MV_Heater1Temp"}
```

## CSV Output

The lot CSV header is fixed:

```csv
Datetime,ToolEvent,EAP_ToolName,LoadPort,Chamber,LotID,WaferID,Recipe,SECSGEM_Raw_Event
```

SPTS Omega required output paths:

```text
D:\MachineData\EAP_SPTS_fxP_OMEGA_01\csv_in
\\FILESERVER\EAP_SPTS_fxP_OMEGA_01.csv_in  # replace FILESERVER with the site host
```

The writer uses a temporary file and atomic rename so downstream readers do not
consume partial files. The network mirror can fail without losing the local CSV.

## Linkstuffs / ThingsBoard HTTPS

Create one ThingsBoard device per enabled machine. In a repository checkout,
copy the public template to ignored `config/production.local.yaml`. On the
installed Windows system, keep the operational configuration outside version
control. Set each token value under the exact machine `display_name` using an
environment reference, for example `${LINKSTUFFS_HTTP_DAVINCI_TOKEN}`. The
configuration loader resolves the environment variable at startup. Startup
fails if an enabled machine has no usable HTTPS route while MQTT is disabled;
this prevents silent telemetry loss.

Telemetry is posted to `/api/v1/{token}/telemetry` and attributes to
`/api/v1/{token}/attributes`. `test-linkstuffs` performs a non-mutating HTTPS
attributes query. HTTP 408, 425, 429, and 5xx responses are retried; invalid
credentials and payload errors are dead-lettered.

HTTP routes require an `https://` origin with certificate verification.
Plaintext HTTP or `verify_tls: false` fails configuration validation unless
`allow_insecure: true` is also set. That override is only for an isolated,
approved test/lab network; it emits a prominent warning and must remain false
in production.

MQTT is optional. Secure MQTT defaults to TLS on port 8883. Plaintext MQTT is
rejected unless `allow_insecure: true` is explicitly set for an approved test
network. When `linkstuffs.enabled: false`, it starts no worker and creates no
outbox rows.

## Optional Encrypted Legacy API

If the site still wants the n8n-style encrypted HTTP API path, enable
`legacy_api` in `config/production.yaml`. New peers use authenticated
AES-256-GCM v2. Generate a random 32-byte key and store only its base64 form in
the machine environment:

The legacy endpoint must also use HTTPS. Plain HTTP requires the same explicit
`allow_insecure: true` lab-only override as the primary HTTP publisher and is
not an acceptable production setting.

```powershell
$key = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($key)
$keyB64 = [Convert]::ToBase64String($key)
[Environment]::SetEnvironmentVariable("LEGACY_API_AES256_KEY_B64", $keyB64, "Machine")
```

Configure the publisher and peer for the versioned `v2.` envelope:

```yaml
legacy_api:
  enabled: true
  url: "https://flow.linkbot.sg/webhook/EncryptedMachineData/api/API_MachineStatus/00_Machine_Status_Event"
  encrypted: true
  encryption_mode: "aes_256_gcm_v2"
  encryption_key_b64: "${LEGACY_API_AES256_KEY_B64}"
  send_tool_events: ["Lot_Start", "Lot_End"]
```

Version 2 uses a fresh 12-byte nonce per request and authenticates the nonce,
ciphertext, tag, and protocol context. The request remains JSON:

```json
{"data": "v2.<base64(nonce + ciphertext + authentication-tag)>"}
```

There is no automatic downgrade. A wrong key or modified payload is rejected
with a generic authentication error.

### Existing n8n/PHP compatibility

The supplied older workflow expects this historic contract:

- cipher: `aes-256-ctr`;
- IV length: 16 bytes;
- HMAC: `sha3-512` over ciphertext only; and
- final data: `base64(IV + HMAC + ciphertext)`.

That format does not authenticate its IV. Use it only for a peer that cannot
yet read v2, select `legacy_ctr_v1` explicitly, and schedule coordinated peer
migration. Do not label a new integration as v1.

Use environment variables for the keys. If the client gives raw PHP/OpenSSL
passphrases, configure `first_key` and `second_key`:

```powershell
[Environment]::SetEnvironmentVariable("LEGACY_API_FIRST_KEY", "<first-key>", "Machine")
[Environment]::SetEnvironmentVariable("LEGACY_API_SECOND_KEY", "<second-key>", "Machine")
```

Then configure:

```yaml
legacy_api:
  enabled: true
  url: "https://flow.linkbot.sg/webhook/EncryptedMachineData/api/API_MachineStatus/00_Machine_Status_Event"
  encrypted: true
  encryption_mode: "legacy_ctr_v1"
  first_key: "${LEGACY_API_FIRST_KEY}"
  second_key: "${LEGACY_API_SECOND_KEY}"
  send_tool_events: ["Lot_Start", "Lot_End"]
```

If the client exports the n8n workflow keys exactly as base64 strings, configure
`first_key_b64` and `second_key_b64` instead:

```powershell
[Environment]::SetEnvironmentVariable("LEGACY_API_FIRST_KEY_B64", "<first-key-b64>", "Machine")
[Environment]::SetEnvironmentVariable("LEGACY_API_SECOND_KEY_B64", "<second-key-b64>", "Machine")
```

```yaml
legacy_api:
  enabled: true
  url: "https://flow.linkbot.sg/webhook/EncryptedMachineData/api/API_MachineStatus/00_Machine_Status_Event"
  encrypted: true
  encryption_mode: "legacy_ctr_v1"
  first_key_b64: "${LEGACY_API_FIRST_KEY_B64}"
  second_key_b64: "${LEGACY_API_SECOND_KEY_B64}"
  send_tool_events: ["Lot_Start", "Lot_End"]
```

Do not mix the two formats. `first_key_b64` must decode to exactly 32 bytes.

This legacy API path is optional. The recommended production path remains
Linkstuffs HTTPS; MQTT gateway mode is an explicit fallback for sites that have
approved MQTT connectivity and gateway device mapping.

## HTTPS Upstream (Cloudflare-friendly)

When MQTT port 1883/8883 is blocked (e.g. Linkstuffs behind Cloudflare), use the
HTTPS publisher instead. It POSTs to `POST /api/v1/{token}/telemetry`, one token
per device, keyed by `display_name` under `linkstuffs_http.device_tokens`.

Never commit a live token. Use environment references in the ignored local or
installed operational configuration, keyed by each tool's `display_name`:

```yaml
linkstuffs_http:
  enabled: true
  base_url: "https://astar-monitoring.linkstuffs.com"
  device_tokens:
    DAVINCI200_MC4_HC1_01: "${LINKSTUFFS_HTTP_DAVINCI_TOKEN}"
  verify_tls: true
```

Restart the service after editing. Telemetry that fails to send is retained in a
durable SQLite outbox and retried with exponential backoff, so short Linkstuffs
outages do not lose data.

## Troubleshooting

- **No collection events arrive from the tool.** Check three things in order:
  1. **E30-style reports are recommended** for full-detail S6F11 collection.
     E40 may still be ingested, but it normally exposes coarser lifecycle data.
     If detailed events are required, confirm the Kontron FabLink report style
     and perform the documented Host Interface restart after changing it.
  2. The per-machine subscription file must be present. The DaVinci profile uses
     `output/davinci200_mc4_hc1/EventSubscription.json`; the service logs
     `Event subscription file not found ...` and aborts subscription if missing.
  3. Logs should show `Reports defined successfully`, `Events linked
     successfully`, and `Events enabled successfully`. A non-zero
     DRACK/LRACK/ERACK means the tool rejected the report config.
- **Only lifecycle events show up, no low-level transitions.** That is by
  design: the active subscription is curated to events the mapper categorizes.
  To capture everything, point the profile at `EventSubscription.full.json` or
  add the CEIDs to `profiles.py:DAVINCI_CEID_ALIASES` and re-curate.
- **No alarms arrive.** Most tools enable alarms by default. If yours does not,
  set `enable_alarms: true` for that machine (sends S5F3 Enable All Alarms on
  connect) after confirming the tool accepts it.
- **Health `spooled_messages_pending`, or events are missing for exactly the
  window the host was down.** The tool spooled them and still holds them. On a
  spooling tool the *only* thing that empties the spool is an S6F23 from the
  host, so a backlog stays on the equipment until one is sent - and a tool that
  refuses to send while a backlog exists then spools everything that follows,
  so one event spooled before the host connected can silence a healthy link
  indefinitely.

  Set `drain_spool_on_connect: true` for that machine. The middleware then
  sends S6F23 (RSDC=Transmit) on every connect, *after* the subscription is
  rebuilt, so the retransmitted S6F11/S5F1 land on live report definitions and
  flow through the normal path (journal -> CSV + telemetry). It is safe to
  leave on: a tool that is not spooling answers RSDA=2 and the call is a no-op.

  Which tools this applies to, from their own manuals:

  | Profile | Spooling | Source |
  |---|---|---|
  | `spts_fxp_omega` | **Yes** | Omega GEM compliance table p9; §9 Spooling; ECID 4010 `SpoolEnabled` |
  | `davinci_200_mc4_hc1` | **Yes** | Software Operation Manual §9.6.2 and Maintenance Manual §3.1.2 both show *Spooling State* / *Spool Full* on the Host Interface panel |
  | `nexgen_mg_series` | **No** | MG manual §2.1 compliance "Spooling: No"; SVIDs 17-20 `SpoolCountActual/Total/StartTime/FullTime` all "Not supported" |

  It is **on** for `SPTS_fxP_OMEGA_01` and `DAVINCI200_MC4_HC1_01` in the
  shipped `production.yaml`, because both tools spool and a stranded backlog is
  a data-loss bug rather than a tuning preference. For the NexGen MG there is
  nothing to drain, which is why its config sets it `false` explicitly.

  The *code* default remains `false` (`MachineConfig.drain_spool_on_connect`),
  so it is a per-machine decision rather than a blanket one — an MG that
  inherited a `true` default would send an S6F23 to a tool that documents
  spooling as unsupported. If you add a machine, set it explicitly from the
  table above.

  Turning it on adds one message to a sequence a commissioned tool has already
  accepted. If a tool rejects the S6F23, the log records `Spool drain denied:
  RSDA=<n>` and nothing else changes — the drain is a recovery step, not a
  precondition for the subscription.

  The middleware only raises this health event for profiles whose manual
  documents a spool counter (`health_spool_count_svid`), so the NexGen never
  reports it.
- **Connection looks stuck after a tool reboot.** The reconnect watchdog polls
  `is_connected`; a dropped HSMS link is detected within ~30 s (linktest) and
  the session is restarted automatically.
- **`Reconnect watchdog: <endpoint> is disconnected, restarting session.`
  repeats and never recovers.** After three consecutive failures the watchdog
  logs one `ERROR` naming the state, and publishes health
  `reconnect_failing`. Read the `gem_state=` field in it - the two causes need
  opposite fixes:
  - `gem_state=NOT_COMMUNICATING`, `tcp_connected=False` - the transport never
    came up. Check the address and port, the firewall, and that nothing else
    already holds the tool's HSMS connection: **equipment serves exactly one
    peer**, so a second middleware instance, a leftover vendor tool, or an
    orphaned connection from an earlier run all present as "the port is there
    but we cannot connect".
  - `gem_state=WAIT_CRA`, `tcp_connected=True` - the tool is reachable and
    answered Select, but never replied to S1F13 with S1F14. It is refusing to
    establish communications: check that it is ON-LINE and that
    `secs_device_id` matches the tool's session ID.

  If events keep arriving in the log while the watchdog insists the machine is
  disconnected, that combination means a *different* connection is delivering
  them. Confirm with `netstat -ano | findstr :<port>` on the equipment side:
  more than one ESTABLISHED peer, or an ESTABLISHED peer with no LISTENING
  socket, is the signature. Stop every middleware instance, confirm the port
  is idle, then start one.

## Windows Service

Use the PowerShell script in `scripts/install_service.ps1` to register a
Windows service via NSSM or WinSW. The script intentionally leaves the service
manager path as an operator-provided parameter so production can use the
standard service wrapper approved by the site.

NSSM is configured and verified with automatic start, restart on unexpected
child-process exit, and a five-second restart delay. Release acceptance kills
the child process and proves that NSSM creates a replacement.

## Storage reserve and backpressure

`storage_safety` samples every filesystem used for journals, outboxes, CSV,
logs, archives, configuration, and machine state. Warning keeps ingress open;
critical quiesces SECS sessions and rejects any race-window ingress before its
journal write. Recovery requires the higher byte and percentage hysteresis
thresholds plus SQLite integrity checks. Transitions are written to normal
status/log output and independently to Windows Application Event Log.

Never lower `critical_free_bytes` below the measured safe-stop/repair reserve.
Use [STORAGE_CAPACITY_AND_RECOVERY.md](STORAGE_CAPACITY_AND_RECOVERY.md) for the
sizing worksheet, backup scope, restore verification, and drill schedule.

## Reconnect retransmission window

`cross_generation_retransmit_window_sec` bounds deduplication across HSMS
connection generations. A byte-identical transaction in the same generation
remains a retry. Across a reconnect it is a retry only inside this window;
outside it becomes a new event identity. Record the commissioned value for each
equipment family and choose the service-wide value conservatively when a host
serves mixed tools.

## Versioned Windows upgrades

`deploy/upgrade.ps1` stages a complete release under `releases/`, runs manifest,
dependency, import, and configuration probes, stops an existing service, and
switches the `app`/`current` junctions. Runtime data and the external `config/`
directory remain outside releases. A failed post-switch health check restores
the prior pointer and prior running state; previous releases remain until an
explicit retention cleanup.
