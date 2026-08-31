# NexGen MG Series HSMS/SECS-GEM commissioning research

**Research date:** 2026-08-25  
**Target:** connect a NexGen Wafer Systems machine to this ASTAR middleware  
**Evidence rule:** vendor-specific claims below come from NexGen material; protocol-layer claims come from SEMI or the middleware's pinned `secsgem` implementation; anything not supported by those sources is marked **FIELD VALIDATION REQUIRED**.

## Executive answer

The repository has the correct NexGen application-interface document and a purpose-built `nexgen_mg_series` profile, but the NexGen manual does **not** contain enough network configuration information to commission a real tool without looking at the tool HMI or asking NexGen support. In particular, the manual does not state the machine's HSMS TCP active/passive role, IP-address fields, TCP port, SECS device/session ID, T3/T5/T6/T7/T8 values, link-test interval, or a menu path for changing them. The repository's NexGen values `5000`, device ID `0`, middleware `active`, and timers `45/10/5/10/5` are explicitly placeholders, not NexGen defaults. [NexGen manual, pp. 9, 44-46](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf); [profile source](../../eap_middleware/profiles.py); [production template](../../config/production.yaml)

What **is** vendor-verified is sufficient to complete the commissioning once those missing values are obtained: MG21, MG22, and MG22-300 implement HSMS/SECS-II/GEM; their user can enable or disable SECS communications; they support both host- and equipment-initiated GEM `S1F13/S1F14` establishment; and they implement the dynamic report, event, alarm, status, equipment-constant, and control-state messages used by this middleware. [NexGen manual, pp. 8-12, 44-46, 56-58, 88-92](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

NexGen also publicly describes its equipment line as designed for SECS/GEM and GEM300 integration, and identifies HSMS/E37 as the TCP/IP delivery layer, SECS-II/E5 as the message layer, and GEM/E30 as the equipment-behavior layer. That corporate statement establishes vendor intent but does not replace the serial-number/build-specific MG interface manual. [NexGen, "Introduction to SECS/GEM"](https://www.nexgen-wafer-systems.com/introduction-secs-gem/)

The shortest safe path is therefore:

1. On the actual NexGen HMI, or through NexGen support, complete the field worksheet below.
2. Configure the middleware in the **opposite** HSMS TCP role.
3. Put SECS communications in `ENABLED` and GEM control in `ON-LINE/LOCAL` for observation-only commissioning.
4. Validate the YAML, prove TCP/HSMS/GEM identity with `test-machine`, then start the service and verify report-subscription acknowledgements and an `S6F11` event.

Do not connect this middleware to a production tool port already assigned to a factory host until the tool owner confirms that this is the intended host connection and approves the event/alarm subscription changes.

## 1. Scope and confidence

### Verified MG coverage

NexGen's `NWS MG Series SECS/GEM Documentation V1.1.18`, dated 1 April 2025, explicitly applies to **MG21, MG22, and MG22-300**. It declares GEM compliance and cites E5-0707, E37-0303, and E30-0307, with GEM300 support only on equipment fitted with FOUP. [NexGen manual, pp. 2-3 and 8-9](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

The local source used for this research is [`docs/vendor/NexGen MG Series SECS - V1.1.18.pdf`](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf), SHA-256 `ec404ad3eee181f0a1ded8bb703273fdf9c65de0a0175da612c8969d281d7da8`. Pages 8, 10-11, 14-15, 88, and 131 were rendered and visually checked in addition to full-text extraction.

The manual warns that its SECS/GEM package can change without notice and specifically says data types, CEIDs, VIDs, and processing-state numbers may change. Treat the actual tool software revision and its generated SEDD/interface manual as controlling if it differs from V1.1.18. [NexGen manual, p. 8](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

### NexGen machines outside that coverage

NexGen's current public product material lists MG21, MG22, and SERENO; the official brochure describes MG21 as single-chamber, MG22 as dual-chamber, and SERENO as multi-chamber. [NexGen product page](https://www.nexgen-wafer-systems.com/our-solutions/); [official NexGen brochure, pp. 19-21](https://www.nexgen-wafer-systems.com/wp-content/uploads/2024/07/NexGen-Wafer-Systems-Brochure.pdf)

**FIELD VALIDATION REQUIRED:** this repository has no vendor interface manual for SERENO or any non-MG NexGen platform. Do not assign `machine_profile: "nexgen_mg_series"` to SERENO merely because it is made by NexGen. Obtain the exact model's SECS/GEM interface document and compare its MDLN/SOFTREV, CEIDs, VIDs, message directions, and GEM300 options first.

## 2. What the NexGen manual verifies

| Item | Vendor-verified behavior | Source |
|---|---|---|
| Transport/application stack | The MG interface cites HSMS E37, SECS-II E5, and GEM E30. | NexGen manual p. 9 |
| Communications enable | The communications state can be `DISABLED` or `ENABLED`; a user action transitions between them. `DISABLED` terminates open transactions and flushes output queues. | NexGen manual pp. 10-12 |
| GEM establishment | In `ENABLED/NOT COMMUNICATING`, the equipment processes `S1F13/S1F14`; accepted establishment is `COMMACK=0`. Either side may initiate the GEM establishment exchange. | NexGen manual pp. 10-12, 49, 57, 88 |
| Establish retry | ECID 4, `EstablishCommunicationsTimeout` (`U2`, seconds), controls the interval between equipment attempts to send `S1F13`. The manual gives no range or default. | NexGen manual p. 131 |
| Identity | `S1F1/S1F2` is implemented; `S1F2` contains `MDLN` and `SOFTREV`. | NexGen manual pp. 44, 56 |
| Online state | While `OFF-LINE`, most automation messages receive `SxF0`; `S1F13` and `S1F17` are handled. `S1F17` is accepted only from `HOST OFF-LINE`; an operator must move `EQUIPMENT OFF-LINE` toward online. | NexGen manual pp. 14-16, 44, 58 |
| Local versus remote | All SECS-II functions can operate while `ON-LINE`; `ON-LINE/LOCAL` retains console operation. Remote process commands require `ON-LINE/REMOTE`. Entry into online follows the front-panel LOCAL/REMOTE switch. | NexGen manual pp. 14-16, 127-130 |
| Event setup | Host defines reports (`S2F33/34`), links them to CEIDs (`S2F35/36`), and enables events (`S2F37/38`); equipment then sends `S6F11`, acknowledged by `S6F12`. | NexGen manual pp. 44-45, 70-73, 90 |
| Status verification | `S1F3/S1F4` reads status values; SVID 11 is `ControlState` and SVID 12 is `EventsEnabled`. | NexGen manual pp. 56, 119 |
| Alarms | `S5F3/S5F4` enables/disables alarm reporting and `S5F1/S5F2` carries alarm reports. | NexGen manual pp. 45, 68-70 |
| Device-ID failure | The equipment implements `S9F1 Unrecognized Device ID`. | NexGen manual p. 46 |
| Spooling | The compliance table marks spooling as not implemented. | NexGen manual pp. 8-9 |

### A critical terminology distinction

The NexGen communication diagram's **host-initiated** and **equipment-initiated** states refer to which application initiates `S1F13 Establish Communications`; they do **not** document which endpoint opens the TCP socket. The document cites E37 but never says HSMS TCP `active` or `passive`. [NexGen manual pp. 9-12 and 88](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

SEMI describes HSMS as the TCP/IP-oriented communications layer and HSMS-SS as its selected-session subordinate standard; GEM is the standard equipment behavior implemented using SECS-II. [SEMI E37 official abstract](https://store-us.semi.org/products/e03700-semi-e37-high-speed-secs-message-services-hsms-generic-services); [SEMI E30 official abstract](https://store-us.semi.org/products/e03000-semi-e30-specification-for-the-generic-model-for-communications-and-control-of-manufacturing-equipment-gem); [SEMI E5 official listing](https://store-us.semi.org/products/e00500-semi-e5-specification-for-semi-equipment-communications-standard-2-message-content-secs-ii)

## 3. The missing machine-side values

One machine-side navigation detail is publicly visible: an [official NexGen MG22 product photograph](https://www.nexgen-wafer-systems.com/wp-content/uploads/2022/10/NexGen_MG22-200_KK_DSC8747-scaled.jpg) shows the title `NexGen Wafer Systems GmbH - MG22 Equipment Controller 3.7.2.30` and a top-level `SECS` tab in its navigation. The photograph does not show the tab open, so it verifies only that one photographed MG22 software build has a `SECS` entry; it does not reveal the sub-menu, permissions, enable control, or any connection value.

No exact path below that `SECS` tab was found in the vendor manual or NexGen's public product/support pages. The manual proves there is a user enable/disable action and a front-panel online/local/remote control, but it does not name the HMI page or required access level. [NexGen manual pp. 11, 15-16](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

Complete this worksheet from the live tool before editing `production.yaml`:

| Required field observation | Why it is required | Evidence status |
|---|---|---|
| Exact platform (`MG21`, `MG22`, `MG22-300`, or other), serial number, equipment software build, SECS package version | Confirms whether V1.1.18 and this profile apply. | **FIELD VALIDATION REQUIRED**; manual p. 8 warns revisions can change constants. |
| HMI path and access level for SECS/GEM/Host Communication settings | Needed to enable and safely change the interface. | **FIELD VALIDATION REQUIRED**; no menu path is published. |
| SECS/GEM communication `ENABLED`/`DISABLED` state | The MG must be enabled to attempt or maintain host communication. | State verified, current setting field-required; manual pp. 10-12. |
| HSMS TCP role: machine `ACTIVE/client/connect` or `PASSIVE/server/listen` | The middleware must use the opposite role. | **FIELD VALIDATION REQUIRED**; role is absent from the manual. |
| If machine is passive: machine IP and listen port | Middleware active mode dials this endpoint. | **FIELD VALIDATION REQUIRED**. |
| If machine is active: middleware/EAP destination IP and port configured on the machine | Middleware passive mode listens for this connection. | **FIELD VALIDATION REQUIRED**. |
| SECS device ID / HSMS session ID | Both peers must put the same value in SECS data-message headers; the MG can report `S9F1` for a mismatch. | Existence verified; actual value absent; manual p. 46. |
| T3, T5, T6, T7, and T8 | Commission the middleware with the tool's approved timer values. | **FIELD VALIDATION REQUIRED**; all values are absent from the manual. |
| Linktest behavior/interval | Record it to diagnose keepalive expectations. The pinned middleware stack sends Linktest every 30 seconds and automatically answers equipment Linktest requests; the two peers' initiation intervals do not need to be identical. | **FIELD VALIDATION REQUIRED** for the machine; the value is absent from the manual. Middleware behavior was verified in pinned `secsgem==0.3.0`. |
| Startup control state and front-panel LOCAL/REMOTE switch | Determines whether the tool is `EQUIPMENT OFF-LINE`, `HOST OFF-LINE`, `ON-LINE/LOCAL`, or `ON-LINE/REMOTE`. | State model verified; live/default setting field-required; manual pp. 14-16. |
| Whether applying communication settings requires an interface-service restart, HMI restart, or tool restart | Determines the approved change procedure. | **FIELD VALIDATION REQUIRED**; not documented. |

When the HMI does not expose these values, send the worksheet to [NexGen's official Singapore headquarters/support contact](https://www.nexgen-wafer-systems.com/contact/) and request the **machine serial-number-specific Host/SECS-GEM communication setup procedure** and current interface manual. NexGen's public contact page lists `singapore@nexgenws.com` for its headquarters, and its public support-engineer description says NexGen personnel perform system installation, tool start-ups/upgrades, troubleshooting, and installation/service reporting. [NexGen contact page](https://www.nexgen-wafer-systems.com/contact/); [NexGen customer-support role](https://www.nexgen-wafer-systems.com/customer-support-engineer/)

Suggested request:

> Please provide the SECS/GEM commissioning procedure for machine serial ___, model ___, equipment software ___, and SECS package ___. We need the exact HMI menu/access level, communication-enable setting, HSMS TCP active/passive role, local/remote IP fields, TCP port, device/session ID, T3/T5/T6/T7/T8 and link-test values, default communications/control state, whether settings require a service restart, and the current SEDD/interface manual.

## 4. Map the machine settings into this middleware

The middleware uses `secsgem==0.3.0`. Its `hsms_mode` names the **middleware's** role: `active` dials the equipment; `passive` listens on `hsms_bind_address:port`. The configured `secs_device_id` is passed as the HSMS `session_id`. [`requirements.txt`](../../requirements.txt); [`gateway/host.py`](../../gateway/host.py); [secsgem 0.3.0 HSMS settings](https://secsgem.readthedocs.io/en/v0.3.0/reference/hsms/settings.html)

| Actual NexGen setting | Middleware setting |
|---|---|
| NexGen is HSMS **passive/server/listen** at `EQUIPMENT_IP:EQUIPMENT_PORT` | `hsms_mode: "active"`, `host: "EQUIPMENT_IP"`, `port: EQUIPMENT_PORT` |
| NexGen is HSMS **active/client/connect** and is configured to dial `EAP_IP:EAP_PORT` | `hsms_mode: "passive"`, `hsms_bind_address: "EAP_IP"` (or `0.0.0.0` on a controlled interface), `port: EAP_PORT`; the YAML `host` is informational in passive mode |
| NexGen device/session ID is `N` | `secs_device_id: N` |
| NexGen shows T3/T5/T6/T7/T8 | Copy the same seconds into `hsms_timers` |

The two endpoints cannot both wait as TCP servers or both continually act as the intended single connection initiator; select one listener and one dialer. This middleware mapping is implemented in [`create_host_settings`](../../gateway/host.py), and the pinned library defines active/passive roles plus remote-active/local-passive address semantics. [secsgem HSMS settings](https://secsgem.readthedocs.io/en/v0.3.0/reference/hsms/settings.html)

### Do not copy the repository placeholders as machine facts

For `nexgen_mg_series`, the tracked profile currently ships:

```text
port/session/middleware role: 5000 / 0 / active
T3/T5/T6/T7/T8:              45 / 10 / 5 / 10 / 5 seconds
```

The profile source and production template both label the connection values as guesses. The timer comment says the MG manual supplies no timers and that these numbers were borrowed from the DaVinci profile. [`eap_middleware/profiles.py`](../../eap_middleware/profiles.py); [`config/production.yaml`](../../config/production.yaml)

The pinned library itself uses T3/T5/T6/T7/T8 defaults of 45/10/5/8/5 and describes them respectively as reply, connect-separation, control-transaction, not-selected, and network-intercharacter timeouts; those library defaults are not NexGen defaults either. [secsgem timeout reference](https://secsgem.readthedocs.io/en/v0.3.0/reference/common.html#secsgem.common.Timeouts)

### Commissioning YAML

Replace every `<...>` with a value observed on or approved for the real tool:

```yaml
machines:
  - endpoint_id: "TOOL_NEXGEN_01"
    display_name: "NEXGEN_MG_01"
    machine_profile: "nexgen_mg_series"

    # Active-middleware case: point to the passive/listening NexGen tool.
    host: "<EQUIPMENT_IP>"
    port: <ACTUAL_HSMS_PORT>
    secs_device_id: <ACTUAL_DEVICE_OR_SESSION_ID>
    hsms_mode: "active"

    # Passive-middleware case instead:
    # host: "<EQUIPMENT_IP>"       # informational in passive mode
    # port: <EAP_LISTEN_PORT>
    # hsms_mode: "passive"
    # hsms_bind_address: "<EAP_FAB_INTERFACE_IP>"

    hsms_timers:
      t3: <ACTUAL_T3_SECONDS>
      t5: <ACTUAL_T5_SECONDS>
      t6: <ACTUAL_T6_SECONDS>
      t7: <ACTUAL_T7_SECONDS>
      t8: <ACTUAL_T8_SECONDS>

    # Full-service commissioning stage. Keep this false for the one-shot
    # test-machine probe, then switch it to true before run-service.
    enabled: true
    runtime_mode: "real"
    offline_test_mode: true       # allow startup without an upstream credential/route

    request_online: true
    enable_alarms: true
    drain_spool_on_connect: false # MG spooling is not implemented
    reset_subscription_on_connect: false
```

The loader accepts only `active`/`passive`, validates port `1..65535`, device ID `0..32767`, and timer seconds `1..120`; these are middleware validation rules, not discovered NexGen ranges. [`eap_middleware/config.py`](../../eap_middleware/config.py)

Use two explicit stages. For the first `test-machine` identity probe, keep the entry `enabled: false`; the command can still target it by `endpoint_id`. For full service commissioning, set `enabled: true`. `offline_test_mode: true` permits that enabled machine to start without a valid upstream Linkstuffs credential/route, but it does **not** itself guarantee that no upstream publication occurs if Linkstuffs remains configured. Disable the upstream routes explicitly when the commissioning environment must be completely isolated. [`eap_middleware/config.py`](../../eap_middleware/config.py); [`eap_middleware/cli.py`](../../eap_middleware/cli.py)

`request_online: true` makes the middleware send `S1F17` after GEM communication is established. On the MG, this succeeds from `HOST OFF-LINE` or reports already online, but cannot override `EQUIPMENT OFF-LINE`; an operator must clear that state. Entry into online follows the machine's LOCAL/REMOTE switch, so use the front-panel **LOCAL** selection for an observation-only commission unless the tool owner explicitly wants remote operation. [NexGen manual pp. 14-16 and 52, 58](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf); [`_provision_after_connect`](../../eap_middleware/secs_runtime.py); [`request_online`](../../gateway/host.py)

The middleware does not send NexGen's `REMOTE`, `PPSELECT`, `MAP`, or `START` remote commands in its normal collection flow. Its post-connect sequence requests online, defines/links/enables reports, optionally drains a spool, and enables alarms. [`eap_middleware/secs_runtime.py`](../../eap_middleware/secs_runtime.py); NexGen manual pp. 127-130.

Use `test-machine` before `run-service` for the least invasive first proof: it establishes communication and reads identity but does not provision report definitions. Starting the service is a machine-state change: it can send `S1F17`, writes `S2F33/S2F35/S2F37` report definitions/links/enables, and sends `S5F3` alarm enable. Obtain the tool owner's approval before doing this on a commissioned tool or one already assigned to another host. [`eap_middleware/probe.py`](../../eap_middleware/probe.py); [`eap_middleware/secs_runtime.py`](../../eap_middleware/secs_runtime.py)

Leave `reset_subscription_on_connect: false` during initial commissioning so the service does not additionally issue the global disable-all/unlink-all/delete-all sequence. If the tool owner confirms stale prior-host definitions and approves clearing them, setting it to `true` performs that reset before rebuilding this middleware's reports. [`config/production.yaml`](../../config/production.yaml); [`gateway/event_subscription.py`](../../gateway/event_subscription.py)

## 5. Machine-side sequence

The exact labels and menu path are **FIELD VALIDATION REQUIRED**. The sequence below uses only state transitions and parameters the vendor interface requires, without inventing a UI:

1. Record the machine model, serial, equipment software build, and SECS package/interface version. Confirm it is covered by V1.1.18.
2. During an approved maintenance/commissioning window, open the machine's Host/SECS/GEM communication settings using the OEM-authorized service level.
3. Photograph or export every page before changing anything.
4. Record the current communication enable, TCP role, IP fields, port, session/device ID, T3-T8, link-test interval, startup communication state, and startup control state.
5. Choose the topology with the tool owner:
   - machine passive/listening and middleware active/dialing; or
   - machine active/dialing and middleware passive/listening.
6. Set the network values so the active endpoint targets the passive endpoint's fab-interface IP and port. Do not put the HSMS path through NAT unless the fab network owner explicitly designs it that way.
7. Set the same device/session ID and the tool-approved timers on both endpoints.
8. Set SECS communications to `ENABLED`. This should enter `NOT COMMUNICATING` until `S1F13/S1F14` succeeds. [NexGen manual pp. 10-12](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)
9. For data-only commissioning, select LOCAL on the front panel and move the tool to the operator-approved online state. If it remains `HOST OFF-LINE`, the middleware can request online; if it is `EQUIPMENT OFF-LINE`, the operator must act. [NexGen manual pp. 14-16](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)
10. Apply or restart only the exact interface service/component instructed by the OEM procedure; the public MG manual does not document whether a restart is required.

## 6. Network and middleware verification

### 6.1 Prove the TCP path

When the machine is passive/listening, run from the Windows middleware host:

```powershell
Test-NetConnection -ComputerName <EQUIPMENT_IP> -Port <ACTUAL_HSMS_PORT> -InformationLevel Detailed
```

`TcpTestSucceeded : True` proves only that a TCP connection can be made; it does not prove HSMS Select, session ID, or GEM communication. Microsoft documents `Test-NetConnection -Port` as a TCP connectivity diagnostic. [Microsoft `Test-NetConnection`](https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection)

When the middleware is passive/listening, start the middleware listener and allow inbound TCP only from the approved equipment IP. Windows firewall rules can scope direction, protocol, local port, and remote address. [Microsoft `New-NetFirewallRule`](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)

```powershell
New-NetFirewallRule `
  -DisplayName "Allow NexGen HSMS <PORT>" `
  -Direction Inbound -Action Allow -Protocol TCP `
  -LocalPort <PORT> -RemoteAddress <EQUIPMENT_IP>
```

### 6.2 Validate and probe the middleware

From the repository/app directory:

```powershell
# Ensure the installed service does not already own this tool connection.
Stop-Service AstarSecsGemEapMiddleware -ErrorAction SilentlyContinue

python -m eap_middleware validate-config `
  --config config\production.yaml

python -m eap_middleware test-machine `
  --config config\production.yaml `
  --endpoint-id TOOL_NEXGEN_01
```

Run only one middleware connection to the tool at a time. Do not run `test-machine` while the Windows service or another host session owns the same HSMS endpoint; stop the installed service first (using its actual service name if it differs from the example above).

The repository defines `test-machine` as a one-shot HSMS/GEM communication and `S1F1/S1F2` identity probe. Success prints this form: [`README.md`](../../README.md); [`eap_middleware/probe.py`](../../eap_middleware/probe.py)

```text
secs-ok: TOOL_NEXGEN_01 <address>:<port> device_id=<N> identity=[<MDLN>, <SOFTREV>]
```

Record the returned `MDLN` and `SOFTREV` and compare them with the machine documentation. The MG manual defines those as the `S1F2` online identity payload. [NexGen manual p. 56](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

For passive-middleware mode the one-shot probe listens only while it is running, so the machine must initiate/re-initiate its connection during that window. The probe timeout is five seconds. [`eap_middleware/probe.py`](../../eap_middleware/probe.py)

### 6.3 Start service and verify the complete flow

```powershell
python -m eap_middleware run-service `
  --config config\production.yaml
```

The expected wire/application progression is:

1. TCP connection.
2. HSMS `Select.req/Select.rsp` selected session. The pinned library documents Select as HSMS communication establishment, automatically performs periodic Linktest every 30 seconds, and answers incoming Linktest requests. This middleware interval is not exposed in the YAML and does not have to equal the equipment's own Linktest initiation interval. [secsgem HSMS messages](https://secsgem.readthedocs.io/en/v0.3.0/hsms/messages.html); [secsgem HSMS protocol](https://secsgem.readthedocs.io/en/v0.3.0/hsms/protocol.html); [`requirements.txt`](../../requirements.txt)
3. GEM `S1F13/S1F14`, with `COMMACK=0`. [NexGen manual pp. 49, 57, 88](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)
4. If configured, `S1F17/S1F18`, with `ONLACK=0` accepted or `2` already online. `ONLACK=1` means not allowed. [NexGen manual pp. 52, 58](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)
5. Per subscription band: `S2F33/34` define, `S2F35/36` link, and `S2F37/38` enable. Accepted define/link acknowledgements are zero. [NexGen manual pp. 50-52 and 90](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf); [`gateway/event_subscription.py`](../../gateway/event_subscription.py)
6. `S5F3/S5F4` alarm reporting enable. [NexGen manual pp. 45, 68-70](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)
7. On a safe operator-generated event, equipment sends `S6F11`; middleware returns `S6F12`. [NexGen manual pp. 45, 70-73, 90](../vendor/NexGen%20MG%20Series%20SECS%20-%20V1.1.18.pdf)

Do not declare commissioning complete merely because TCP or HSMS Select is green. Completion evidence should include the identity, accepted subscription bands, `ControlState`/`EventsEnabled` readback, and at least one decoded event written to the machine's configured log/CSV path.

## 7. Fault isolation

| Symptom | Most likely layer | Evidence-backed checks |
|---|---|---|
| TCP test fails in middleware-active topology | Network or no listener | Re-check equipment IP/listen port, subnet/route, physical NIC, tool communication service, and outbound/inbound firewall. The NexGen manual publishes no default port. |
| Middleware passive but machine never connects | Network, destination, or TCP role | Confirm the machine is actually active/client and targets the EAP fab-interface IP/port; confirm the middleware listener is bound and inbound firewall permits the equipment IP. |
| Repeated connect failures or no socket | Both endpoints configured in the wrong/complementary roles | Re-read the live HMI role. `hsms_mode` is the middleware role, not the machine role. [`gateway/host.py`](../../gateway/host.py) |
| TCP connects but never becomes selected/communicating | HSMS Select, T7/T6, or role collision | Check HSMS control logs for `Select.req/rsp`, match T6/T7 to the tool, and verify only the intended endpoint initiates. The pinned stack uses T6 for control transactions and T7 for not-selected timeout. [secsgem timeout reference](https://secsgem.readthedocs.io/en/v0.3.0/reference/common.html#secsgem.common.Timeouts) |
| `S9F1 Unrecognized Device ID`, session mismatch log, or selected link with ignored SECS data | Device/session ID mismatch | Copy the machine's exact device/session ID into `secs_device_id`; the middleware rejects data messages addressed to a different session ID. NexGen manual p. 46; [`GatewayHost._on_message_received`](../../gateway/host.py). |
| GEM establishment repeats with `COMMACK=1` | Equipment denied application establishment | Confirm SECS communications are enabled, inspect tool alarm/host logs, and use the OEM-approved ECID 4 retry value. NexGen defines `COMMACK=1` as denied/try again. Manual pp. 49, 57, 88, 131. |
| `test-machine` reaches TCP/HSMS but identity fails or receives function zero | Tool is not application-communicating or is offline | Check communication state and control state. `S1F1` treats function zero as inoperative; while OFF-LINE, MG returns `SxF0` to most host primaries. NexGen manual pp. 14, 56. |
| `S1F17` returns `ONLACK=1` | Online transition not allowed | If the machine is `EQUIPMENT OFF-LINE`, an operator must request online; `S1F17` is accepted from `HOST OFF-LINE`. NexGen manual pp. 14-16, 52. |
| Connection and identity work, but all subscription messages fail or no events arrive | Offline state, communication disable, or report refusal | Verify `ControlState` SVID 11 is 4 (`OnlineLocal`) or 5 (`OnlineRemote`), inspect per-band DRACK/LRACK/ERACK, and read `EventsEnabled` SVID 12. NexGen manual pp. 119 and 171-176. |
| `DRACK=3` or `LRACK=3` | Existing report ID or CEID link | Coordinate with the tool owner. If prior-host definitions are stale and clearing is approved, use `reset_subscription_on_connect: true` once, then return it to false. NexGen ack definitions pp. 50-52; [`gateway/event_subscription.py`](../../gateway/event_subscription.py). |
| Some subscription bands pass and others fail | Model/option-specific CEIDs or manual/profile revision mismatch | Keep the successful-band evidence; compare rejected CEIDs with the actual tool's current SEDD and enabled options. V1.1.18 warns constants may change. Manual p. 8; [`docs/NEXGEN_MG_PROFILE_NOTES.md`](../NEXGEN_MG_PROFILE_NOTES.md). |
| Link drops around replies/control messages/partial packets | Timer mismatch or network quality | Copy the approved tool timers exactly and inspect T3/T6/T7/T8 errors at both ends. Do not infer MG values from the repository placeholders. [secsgem timeout reference](https://secsgem.readthedocs.io/en/v0.3.0/reference/common.html#secsgem.common.Timeouts) |
| Events during middleware outage are missing after reconnect | Expected MG limitation | NexGen marks spooling unsupported; keep `drain_spool_on_connect: false` and plan around unbuffered downtime. NexGen manual pp. 8-9. |

## 8. Commissioning acceptance record

Capture this as the handover evidence:

```text
Machine model / serial:
Equipment software:
SECS package / document revision:
HMI communication menu path and access level:
Machine HSMS role:
Machine fab IP:
EAP fab IP:
TCP listen/destination port:
Device/session ID:
T3 / T5 / T6 / T7 / T8:
Link-test interval:
SECS communications state:
GEM control state:
S1F2 MDLN / SOFTREV:
S1F14 COMMACK:
S1F18 ONLACK (if sent):
Subscription band results:
ControlState SVID 11:
EventsEnabled SVID 12:
First verified S6F11 CEID and timestamp:
Middleware log path:
Middleware CSV path:
Approved by tool owner / NexGen engineer:
```

## 9. Bottom line

For an MG21, MG22, or MG22-300, this middleware already speaks the documented NexGen GEM interface. The remaining commissioning dependency is not more code; it is obtaining and validating the real tool's unpublished connection values and HMI procedure. For SERENO or any other NexGen platform, obtain a model-specific interface manual before using the MG profile.
