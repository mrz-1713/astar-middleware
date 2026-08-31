# DaVinci 200 MC4 HC1 Client Files

Generated from:
- /Users/nrzngr/Downloads/SECS-Items_MueTec DaVinci 200 MC4_HC1.xlsx
- /Users/nrzngr/Downloads/Host Interface Manual DaVinci 200 MC4 HC1.pdf

What the client sent:
- Host Interface Manual: MueTec DaVinci200 host interface, version 4.3.0, 258 pages.
- SECS items workbook: machine-specific GEM IDs for SV, DV, Events, EC, Alarms.

Extracted counts:
- Status variables: 113
- Data variables: 102
- Events: 282 total, 208 with valid variables
- Equipment constants: 37
- Alarms: 1017

Manual facts that matter:
- HSMS-SS / SECS-II interface.
- Equipment default connect mode: PASSIVE.
- Default TCP port: 5000.
- Default HSMS device ID: 0.
- Dynamic event report configuration is supported.
- Trace data supports up to 10 traces with up to 200 status variables each.
- FabLink creates standard reports for each collection event at startup.
- Alarm set CEID equals ALID; alarm clear CEID is ALID + 1.
- Remote command tabs are empty in this workbook.

Ponytail next step:
1. Ask client for real equipment IP address and confirm port 5000/device ID 0.
2. Use S1F13/S1F14 to confirm communication.
3. Poll a tiny SVID set first: ControlState, Clock, ProcessState.
4. Subscribe to only the few production events your CSV needs, not all 282 events.
5. Add dynamic S6F11 report parsing only after one real S6F11 sample is captured.
