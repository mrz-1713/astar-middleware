# Machine Protocol Facts — Vendor Document Supplement

**Date:** 2026-08-17
**Source:** independent extraction from `tmp/doc_extract/` (pdftotext -layout of the NexGen MG Series SECS manual, the SPTS fxP Omega SECSII manual, the DaVinci SECS-Items workbook dump, and the DaVinci Software Operation Manual §9.6). Companion to `docs/vendor/MACHINE_REQUIREMENTS_REPORT.md`; this supplement carries the details the main report compressed: exact message tables, the SPTS alarm-CEID formula, spooling ECs, W-bit semantics, and the NexGen lot-start sequence.

## Protocol stack
- SPTS fxP: E37 HSMS over TCP/IP, E5 SECS-II, E30 GEM (E4 SECS-I allowed below HSMS).
- NexGen: E4/E5/E30/E37/E39/E40/E87/E90/E94; GEM300 only on FOUP platforms. Formatted PP Mgmt = No, Terminal Services = No, **Spooling = No**; all other GEM capabilities Yes.
- DaVinci 200: FabLink suite v6.5.0, E5-0707/E30-0307/E37-0303/E39/E40/E87/E90/E94/E95.

## Connection parameters
- SPTS Table 3: T3=1-120s (typ 30), T5=1-120s (typ 5), T6=1-120s (typ 10), T7=1-120s (typ 5), T8=1-120s (typ 6); Connect Mode Active/Passive, **default Passive**.
- DaVinci (Table 52): Communication Mode Server/Client, TCP/IP port 1-65535, T3-T8 1-120s, Soft Start Timeout 1-36000s, Enable yes/no, Bypass Read ID.
- Establish-communications timeout EC: SPTS ECID 6 (U2, 0-1800s, default 10); NexGen ECID 4. TimeFormat EC: SPTS 67, NexGen 5 (0=12-byte, 1=16-byte; NexGen default 1).
- **No document states an HSMS port number or device ID.**

## Communications state model (GEM E30, SPTS + NexGen)
- DISABLED → ENABLED → NOT COMMUNICATING → COMMUNICATING (S1F13/14, COMMACK=0).
- NOT COMMUNICATING: only S1F13/14 (and S9Fx) processed; equipment initiates connect per the EC; if the EC = 0 it waits for the host.
- Heartbeat S1F1: period = HeartBeat EC (SPTS ECID 10, 0-1800s, default 30; 0 = none).
- SPTS CommState SVID 31: 0=Disabled, 260=Communicating, 273=Enabled/Not Communicating.

## Control state model
- OFF-LINE (EQUIPMENT OFF-LINE / ATTEMPT ON-LINE / HOST OFF-LINE) vs ON-LINE (LOCAL/REMOTE). While OFF-LINE, non-allowed host primaries get SxF0; SPTS rejects S1F17 with non-zero ONLACK unless HOST OFF-LINE. S2F41 commands only in ON-LINE/REMOTE.
- S1F17/18 ONLACK: 0 accepted, 1 not allowed, 2 already on-line. NexGen honors S1F17 only in HOST OFF-LINE.
- ECs: DefaultCommState (0/256), DefaultControlState (1-5), DefaultCtrlOfflineState (1 Eq Offline/3 Host Offline). SVs: ControlState (1 Offline … 5 Remote), PreviousControlState.

## SPTS alarm CEID formula (used to recognise alarm collection events)

NaNAlarmID = StationNo × 10,000,000 + StationType × 100,000 + offset`
ON  (set)    CEID = AlarmID + 10,000 + offset
OFF (clear)  CEID = AlarmID + 1,000,010,000 + offset

Station numbers: 0 Transport, 1 VCE A, 2-7 PM1-PM6, 8 VCE B, 10 Cool Station. Station types: 3 Forcefill, 4 Sputter Dep, 7 HSE, 9 Heat, 20-23 Brooks MX, 24 Etch, 25 Deposition RevB, 26 SoftEtch RevB, 27 Heat RevB, 34 Delta APM, 58 Pro CVE. Alarm text ≤ 40 chars.

Because the numbers depend on the tool layout, the middleware cannot pre-alias them; the overlay mechanism accepts them by name: add the layout's CEIDs to the machine's subscription file named `AlarmNDetected` / `AlarmNCleared` (now in GENERIC_EVENT_ALIASES) and they route through the alarm pipeline. Otherwise they arrive as readable `unknown` events (kept in CSV and telemetry, warned once per machine).

## Spooling (SPTS only; NexGen documents it as not implemented; DaVinci supports it but defaults OFF)
- S2F43/44 toggle (empty list = disable all; Stream 1 never spooled); states SPOOL INACTIVE ↔ ACTIVE (LOAD NOT FULL/FULL; UNLOAD NO OUTPUT/TRANSMIT/PURGE).
- Recovery is host-initiated: S6F23 after reconnect (RSDC 0=transmit, 1=purge; RSDA 0=OK, 1=busy, 2=no spooled data). One open transaction at a time; MaxSpoolTransmit caps per S6F23.
- ECs: MaxSpoolMessages 4004, MaxSpoolTransmit 4005, OverwriteSpool 4009, SpoolEnabled 4010. SVs: SpoolCountActual 2016, SpoolCountTotal 2017, SpoolFullTime 2018, SpoolStartTime 2019, SpoolState.
- Events: SpoolingActivated CEID 17, SpoolingDeactivated 18, SpoolTransmitFailure 16.

## W-bit semantics (SPTS)
- WBitS5 ECID 19, WBitS6 20, WBitS10 18 — whether the equipment expects a host reply to S5F1/S6F1,3,5,11/S10F1.

## Stream 9 error messages (both tools)
- S9F1 Unrecognized Device ID, S9F3 Unrecognized Stream, S9F5 Unrecognized Function, S9F7 Illegal Data, S9F9 Transaction Timer Timeout, S9F11 Data Too Long, S9F13 Conversation Timeout.

## NexGen lot-start sequence (manual §9.1, host-driven)
- connect (S1F13/14) → delete reports/links (S2F33/35 empty) → disable CEIDs (S2F37) → define reports → link → enable → verify enabled (S2F37 readback) → REMOTE (S2F41) → MAP port (S2F41) → PPSELECT (S2F41) → START (S2F41) → S6F11 CEID 4 ProcessingStarted. GEM300 adds Carrier Bind (S3), PRJob create (S16F11), CJ create (S16F27). The middleware observes only; it never drives this sequence.

## Remote commands (SPTS, S2F41; HCACK 0-6)
- Cassette (VCE A/B): SELECT/PP_SELECT, START, STOP, CANCEL, ABORT, ABANDON, PAUSE, RESUME, LOAD, UNLOAD, CLAMP, UNCLAMP, SCANSLOTMAP, SPSELECT.
- Process modules PM1-6: IDLE, READY, PROCESS, SERVICE, ABORTPM, HOLD, RESTORE, MODE.
- Control: LOCAL, REMOTE, ACKFAULTS, ACKOPERATOR, ACKALL, LAMPTOWER.
- NexGen: REMOTE, PPSELECT, MAP(Port1/2), START.

## Data formats / ack codes
- Types: A, B, Bo, Bi, F4, F8, I1/I2/I4, U1/U2/U4, L. TIME 12-byte YYMMDDhhmmss or 16-byte YYYYMMDDhhmmsscc.
- DRACK: 0 accept, 1 no space, 2 invalid format, 3 RPTID already defined, 4 VID unknown. LRACK: 0, 1 space, 2 format, 3 CEID already linked, 4 CEID unknown, 5 RPTID unknown. ERACK 0/1. HCACK: 0 ack, 1 unknown command, 2 cannot now, 3 invalid param, 4 will perform (later completion), 5 already in condition, 6 no such object.

## ID-scheme summary (cross-checked against the audit)
- SPTS: 158 SVIDs (19 GEM + 139 general), 224 Table 5 CEIDs + 811, Appendix E VID formula, alarm formula above.
- DaVinci: 282 events, 113 SVs, 102 DVs, 37 ECs, 1017 alarms (RCMD sheets empty).
- NexGen: 243 CEIDs, 255 SVIDs (9, 17-20 not supported), 162 report-referenced DVs.
