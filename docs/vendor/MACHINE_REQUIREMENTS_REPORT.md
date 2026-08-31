# Vendor Machine-Requirements Report

Structured SECS/GEM interface-fact extraction from vendor docs in
`tmp/doc_extract/`, for cross-checking middleware coverage. Citations =
source file + line numbers (or section/page). Ambiguities are flagged explicitly.

Sources: `vendor__NexGen MG Series SECS - V1.1.18.txt` (10768 ln),
`vendor__Omega_SECSII_SPTS fxP 200mm SECSII Manual (Cimetrix).txt` (13417 ln),
`xlsx_full_dump.txt` (MueTec DaVinci 200 MC4_HC1 SECS-items workbook, 1941 ln),
`vendor__davinci-200__DaVinci 200 - User Manual V1.8.txt` (1635 ln),
`vendor__davinci-200__Software Operation Manual_EN.txt` (10313 ln).

---

## 1. NexGen MG Series (NWS MG21/MG22/MG22-300, V1.1.18)

GEM-compliant; GEM300 only on FOUP-equipped platforms (L315). Standards:
E4, E5, E37, E30, E39, E40, E87, E90, E94 (L326-344).

### 1.1 Connection parameters
- HSMS transport **implied only** (E37, L335; "other protocols like HSMS", L2752).
  **No port number, device-ID value, active/passive, IP, or T3-T8 anywhere**
  (grep: no hits). Device ID appears only as S9F1 error cause (L3250-3252).
- Timer: only `CommDelay` in comms state model (L407-463). Only 2 ECs (8.4):
  ECID 4 `EstablishCommunicationsTimeout` (U2 s, S1F13 retry interval,
  L6025-6031); ECID 5 `TimeFormat` (0=12B/1=16B, default 1, L6033-6049).
- S1F13/F14 host- or equipment-initiated (7.1 L3871-3958; 6.1 L2405-2427).
  Example: MDLN=`MG22`, SOFTREV=`3.7.0.0` (L9615-9618, L10194-10197).
- Control states (3.2 L487-636): OFF-LINE (EQUIPMENT/ATTEMPT ON-LINE/HOST
  OFF-LINE), ON-LINE (LOCAL/REMOTE); S1F15-18 OFF/ON-LINE (L2460-2485);
  OFF-LINE answers Sx,F0 to non-S1F13/17 primaries (L501-515); default
  ON-LINE or OFF-LINE configurable. Comms failure → NOT COMMUNICATING (L383);
  DISABLED **flushes output message queues** (L369-373).

### 1.2 CEID families (8.1, L4295-4572)
- **Core GEM 0-19**: 0 EquipmentOffline, 1/2 ControlStateLocal/Remote, 3
  OperatorCommandIssued, 4/5/6 ProcessingStarted/Completed/Stopped, 7
  ProcessingStateChange, 8/9 AlarmDetected/Cleared, 10 OperatorECChange, 11
  LimitZoneTransition, 12 ProcessProgramChange, 13 processRecipeSelected,
  14/15 MaterialReceived/Removed, 16/17/18 SpoolingActivated/Deactivated/
  SpoolTransmitFailure, 19 MessageRecognition.
- **Init 100-103, misc 110-111**: initCompleted, processStateSetup,
  setupCompleted, readyForProcess, buzzerStateChanged, signalTowerStateChanged.
- **Load ports 120-153**: 120-123/124-127 port1-4ReadyToLoad/ReadyToUnload;
  130-133/134-137 port1-4CasPlaced/CasRemoved; 140-143 port1-4CasMapped;
  145 cassetteMapped (any port); 150-153 processingStartedPort1-4.
- **PM1 200-231**: 200 pm1StatusChanged; 210/211 Occupied/Unoccupied;
  212/213 WaferStarted/Finished; 214/215/216 ProcessStopping/Aborting/Aborted;
  220/221 StepStarted/Finished; 222/223 MediumStep; 224/225 DiStep;
  226/227 N2DryStep; 228/229 DiwO3Step; 230/231 MediumOffStep.
- **PM2 300-331**: mirror of 200-231.
- **CHC/chemistry 400-479**: 400-402 chc1-3StateChanged; 410-412 chc1-3Ready;
  415-417 med1-3NotReady; 420-425 med1-3RefillStarted/Finished; 450-479 med
  1-3 × comp1-5 concentration High/Low limits.
- **Metrology/special 510-600**: 510/511 Atmsi1/2MeasFinished; 514-517
  Pm1/2HpcStep; 518-521 Pm1/2BemStep; 522-525 Pm1/2LowFlowStep; 531/532
  Roughness1/2MeasFinished (IRM); 533-538 Pm1/2EpdStep + 535/538
  EndpointDetected; 600 WaferAlignmentStatus (PreAligner).
- **GEM300 700-862**: 700-718 ProcessJob transitions (Pj…); 720-733 ControlJob
  transitions (Cj…); 751-760 LP transfer/service states; 772-790 carrier
  ID/slot-map verification + accessing (E87); 801-808 LP access
  mode/reservation/association; 850-862 substrate tracking (E90), 860/861
  SubstLoc occupancy; 862 SubstToNoState.
- **880-883**: chc1-4CanisterFillPaused (V1.1.15).

### 1.3 SVIDs of interest (8.2 SVs, L5493-5935)
- 8 AlarmsEnabled; **9 AlarmsSet = "Not Supported"**; 11 ControlState (1-5);
  12 EventsEnabled; 15 ProcessState (0/1/2/3/4/5/7/8/9/10/12/20 — no 6,11,
  13-19); **16 LastEventID (U4, de-facto liveness counter)**; 17-20 spool SVs
  = "Not supported" (L5500-5526).
- Load ports 3100-3433 (per-port JobPending/MapResult/Status/JobId/LotId/PPID/
  Cid; Status 0=unload req,1=load req,2=unavailable). PM live 3500-3750
  (flows/temps/speeds/DiwO3/HPC/BEM/LowFlow/CO2/Exhaust/State/WaferCount).
  Supply/facility 3900-4004; light tower/buzzer 4303/4304; med etch/bath
  life 4310-4332; CHC 4350-4367; concentration 4400-4456 (4450 diwO3Conc).
- GEM300 4999-5141: 4999 CarrierLocationMatrix; 5000-5039 LP1-4 access/carrier/
  association/reservation/transfer; 5100-5141 subst locations (PM1/PM2,
  ROBOT, LECO/UECO 1-2, PA1/2, ATMSI1/2); 5300/5301 CjQueueAvailableSpace,
  QueuedCJobs.

### 1.4 Report layout facts (3.3 diagrams, L638-810)
- Per-step-type CEID + fixed VID set (PM1 example, 8-step recipe): CEID 223
  pm1MediumStepFinished / 225 pm1DiStepFinished / 227 pm1N2DryStepFinished with
  VIDs 1100-1105 N2 flows, 1130-1132 med temp, 1150-1152 med flow, 1160-1162
  DI flow, 1170-1172 chuck speed (Min/Max/Avr F4), 1180 pm1StepTypePrevStep
  (A). ASCII diagram maps CEID at each step → VID list (L700-807).
- DVVAL validity: each DV valid only on listed CEIDs (e.g. 1100-1102 on
  223,225,227,229) (8.2.1, L4579+).
- Worked example (9.1.1.2-.6, L9670-9800): RPTID1=DVARs 1835/1836 → CEID 4;
  RPTID2=DVARs 1830/1831 → CEID 5; RPTID3=SVAR 4306 mapResultLastMap →
  CEIDs 140/141 (one list SV for whole map). S2F33 define / S2F35 link
  (default disabled) / S2F37 enable; S6F11 plain, S6F13 annotated, S6F15-18
  event-report request, S6F19/20 individual report (6.2 L2570-2660; 6.5
  L3024-3120).

### 1.5 Data-loss facts
- **Spooling NOT implemented** (2.1: "implemented No / GEM compliant No";
  SVs 17-20 "not supported"; CEIDs 16-18 defined but never said to fire).
- **Events during host outage are not retained** — no buffering anywhere;
  DISABLED flushes output queues (L369-373). Implied (not stated): events
  generated while disconnected are lost.
- **Enable-all**: S5F3 zero-length ALID = all alarms (L2958); S5F5 zero-length
  = all alarms regardless of ALED (L2968); S2F37 zero-length CEID list = all
  (L2636); S5F7/F8 list enabled alarms (L2976-2988). Alarms reported via S5F1
  + CEIDs 8/9 (7.3 L4040-4062).

### 1.6 Two-vs-four load ports
- Interface uniformly models **4 load ports** (CEIDs 120-153, DVs port1-4,
  SVs 3100-3433, LoadPort1-4 alarm groups). **No statement that any platform
  has 2 vs 4 physical ports** (grep "two/four load ports" = 0; platforms only
  named, L266-269). Example 9.1 uses ports 1-2 only (MAP Port1/Port2).
  **Ambiguous**: middleware must treat port count as configurable 2-4.

---

## 2. SPTS fxP Omega (Cimetrix SECS module)

### 2.1 Connection parameters
- HSMS over TCP/IP/Ethernet (L321-324); no fixed port/IP — configurable ECs.
- **1st host (Table 8, L2025-2033)**: 1000186 Device_ID (0-32767); 1000187
  Port_Number (1-65535); 1000188-1000192 T3/T8 1000-120000 ms, T5/T6/T7
  1000-240000 ms; 1000193 Link_Test_Idle_Time (0-240000 ms).
- **2nd host (L2060-2069)**: 1000490-1000499 same + 1000498 Connection_Type
  (**0=Active, 1=Passive**) + 1000499 Connection_IP_Address (A).
- **Protocol params (Table 3, L602-629)**: T3 1-120 s (typ 30); T5 1-120 s
  (typ 5); T6 1-120 s (typ 10); T7 1-120 s (typ 5); T8 1-120 s (typ 6);
  Connect Mode Active/Passive (**typ Passive**).
- **GEM ECs (Table 6, L1755-1800)**: 6 EstablishCommunicationTimeout (10 s,
  0-1800); 8 DefaultCommState (0/256); 9 DefaultControlState (1-5); 10
  HeartBeat S1F1 (30 s, 0-1800); 11 DefaultCtrlOfflineState; 18/19/20
  WBitS10/S5/S6; 67 TimeFormat; 4022 EventReportMsg (0/67075=S6F3/
  67083=S6F11/67085=S6F13); 4028 LimitFreq.
- Legacy SECS-I ECs 1000500-1000508 (L2070-2078). MDLN=`'P300'|'fxP'`,
  SOFTREV=`wxy{Pz}` (L906-932).

### 2.2 Table 5 CEID families (7.2, L955-1180)
Columns: CEID | Keyword | Description | **Valid DVs for Reports**.
- **Core 3-24**: 3/4 MaterialReceived/Removed (DV 6102 PortID); 6
  OperatorCommandIssued (6102,16); 7 PPChange (3,4); 8 EquipmentOffline;
  9/10 ControlStateLocal/Remote; 15 MessageRecognition; 16-18 Spool
  TransmitFailure/Activated/Deactivated; 19 HostECChange (48,49); 20
  HostPPChange; 24 ECChange (7,2052,2053).
- **VCE A/B process states 100-206**: 100/101 ProcessStateChange1/2; 120-176
  (VCE A) and 181-206 (VCE B) Pausing/Paused/Resumed + full Idle-Selecting-
  Selected-Starting-Running-Stopping-Restarting-Abandoning-Stopped set.
- **Lot/wafer 330-391**: 330/331 MBCStart1/2; 336/337 MBCComplete1/2;
  342/343 MBStart1/2; 348/349 MBComplete1/2; 354-391 OpSelect/Start/Stop/
  Pause/Resume/Cancel/Abandon 1/2.
- **State/mode 400-620**: 400 TransportStateChange; 401-408 VCEA/PM1-6/VCEB
  StateChange; 410 CoolerStateChange; 422-427 PM1-6RecipeStart; 430
  CoolerRecipeStart; 442-447 RecipeEnd; 450 CoolerRecipeEnd; 462-467
  RecipeStepStart; 470 CoolerRecipeStepStart; 482-487 RecipeStepEnd; 490
  CoolerRecipeStepEnd; 500-515 WaferStatusChange (arms/VCE/PM1-6/aligner/
  incooler/buffer); 520/521 ReadyForProcessA/B; 610-618/620 ModeChange.
- **Doors/SMIF/load 701-802**: 701/702 DoorOpen1/2; 711/712 DoorClosed1/2;
  721-762 SMIFPod Present/Absent/Clamped/Unclamped/Homed 1/2 (LPI2200);
  771/781/791/801 VCEA/B MaterialPresent/MaterialAbsent/LoadComplete/
  UnloadComplete.
- **810 WaferStatisticalDataAvailable** (6102,6503,5101,5102,5100,5117,5114).
- **Cassette/wafer 850-860**: 850 SlotMapRead; 851/852 CassetteStarted/
  Complete; 853/854 PMWaferIn/Out; 855/856 ProcessingStarted/Finished;
  857/858 RecipeStepStart/End; 859/860 WaferStarted/WaferComplete.
- **861-911**: Lamp1-4StatusChanged (861-864), BuzzerStatusChanged (865),
  DatabaseAutoSwapPending/Complete (870/871), PM1-6WaferIn/Out (880-891),
  PM1-6RFOn/Off (900-911).
- **Alarm CEIDs computed** (8.3, L1330-1378): ON CEID =
  (StationNo×10⁷)+(StationType×10⁵)+10000+offset; OFF CEID = +1,000,000,000+
  10000+offset. Stations: 0 Transport, 1 VCE A, 2-7 PM1-6, 8 VCE B,
  10 Cool Station.

### 2.3 SVID ranges and Appendix E VID formula
- **GEM SVs 22-40** (Table 7, L1806-1855): 22 AlarmID, 23 AlarmsEnabled,
  24 AlarmsSet, 25 AlarmState, **26 ASer (alarm serial counter — liveness/
  sequence counter)**, 27 Clock, 28 ControlState, 29 DataID, 30 EventsEnabled,
  31 CommState, 32 MDLN, 34 LastCEID, 36 PreviousControlState, 39 SOFTREV,
  40 Time.
- **Spool SVs 2016-2019** (SpoolCountActual/Total/FullTime/StartTime).
- **Equipment SVs 1100-5310** (12.8, L2161-2420): 1100-1115 wafer status;
  1201-1275 VCE A/B per-slot wafer tracking; 1500-1510 modes; 1550-1560
  states; 1572-1577 module recipes; 1601-1662 VCE process/pause/cassette
  recipe/lotid/cycling/datalogging; 1700-1726 wafer counts; 2039-2055
  recipe/NVS/ResyncNVS; 5103 Energy; 5200-5209 lamp tower; 5300-5310
  cassette maps/EquipmentReady.
- **Appendix E (L10006-10026)**: **VID = (station no × 10000) + (station type
  offset × 100) + (variable offset) + 10000**. Types: 3 Forcefill PM, 4
  Sputter Deposition PM, 7 HSE PM, 9 Heat PM, 20/21/22 Brooks MX
  Transport/Coolstation/Cassette, 24 Etch, 25 Deposition RevB, 26 SoftEtch
  RevB, 27 Heat RevB, 46 C3M, 53 Primaxx Monarch 25, 56 c2L Transport,
  58 Pro CVE PM. Names `StatX Y MV|DSV …`; per-type offset tables follow
  (e.g. Forcefill offset 0 = chamberPirani).

### 2.4 Report / subscription behavior
- Default reports on hard disk linked to CEIDs; host reconfigures via
  S2F33/35/37; **definitions/links/enable states persist in non-volatile
  storage** (11.2, L1678-1694). Operator console cannot configure.
- **No full per-event report layouts** — only the "Valid DVs for Reports"
  column (e.g. 5111,5113,5114,5115,5116,5117,5118 for PM recipe events).
- S2F37 zero-length list = **all CEIDs** (L4147-4157); S6F11/12 with
  S6F5/6 inquire-grant for multi-block; S6F15 demands an event report
  (L955-975).

### 2.5 Spool / retention (section 9, L1382-1650)
- Full GEM spooling: enable per stream/function via **S2F43/F44** (stream 1
  excluded); unload via **S6F23** (RSCD=0 transmit, RSDC=1 purge; one table
  row says "S7,F23" — typo, L1577). ECs 4004 MaxSpoolMessages, 4005
  MaxSpoolTransmit (0=unlimited), 4009 OverwriteSpool (1=overwrite oldest/
  0=discard new), 4010 SpoolEnabled. SVs 2016-2019; CEIDs 16-18.
- Spool survives power-down (non-volatile, L1605-1610). While SPOOL ACTIVE,
  non-spool-enabled primaries (except stream 1) are **discarded**
  (L1450-1454). Multi-block inquire/grant excluded from counts.

### 2.6 RCMD / commands (section 15, L2645-3188)
- **VCE/cassette (Table 10)**: SELECT, PP_SELECT (VCE, LOTID, RECIPE; +
  CASSETTEID if EC 1000352 true), START, STOP, CANCEL, ABORT (=CANCEL),
  ABANDON, PAUSE, RESUME, LOAD, UNLOAD, CLAMP, UNCLAMP, SCANSLOTMAP (VCE,
  CASSETTEID), SPSELECT (VCE, LOTID, up to 25 SLOTxBASERECIPE pairs,
  NEWRECIPE).
- **PM (Table 12)**: IDLE, READY, PROCESS, SERVICE, ABORTPM, HOLD, RESTORE
  (PM 1-6 [+RECIPE]).
- Commands = "request action to be initiated", reply HCACK=4 (L3168-3170);
  host/operator availability EC-configurable. PM state machine:
  DOWNLOADING→RESET→CONFIG→SHUTDOWN→IDLE→READY→PROCESS + HOLD/ABORTED.

### 2.7 Parallel load ports / PMs
- **2 cassette ports (VCE A, VCE B)** each with independent process state
  machine (L2650-2652), Transport (arms A/B), **PM1-PM6**, Cool station,
  Aligner, InCooler, Buffer — all independently addressed in CEIDs/SVIDs.
- PP: unformatted only; EC 1000358 individual-recipe target (0 off, 1-6
  PM1-6, 7 Coolstation, 8 Conditioning, 9 Wafer, 10 Cassette), EC 1000359
  Process/Service; custom S7F65/66 child-recipe list (16.2, L3188-3300).
  For 200 mm tools set to Cassette Recipes.

---

## 3. DaVinci 200 MC4/HC1 — SECS-items workbook

10 sheets in dump. **No "Tool Configuration" sheet in this dump** although the
Version History references per-model config sheets.

### 3.1 Sheet inventory (counts by 7-digit ID)
| Sheet | Data rows | ID range | Notes |
|---|---|---|---|
| Version History | 43 | — | see 3.6 |
| SV | 113 | 1010001-1170018 | |
| DV | 102 | 2010001-2170007 | |
| Events | 282 | 3010001-3230002 | 8 cols |
| EC | 37 | 4010001-4100001 | |
| Data Formats | ~50 | — | ResultList/TestResultList/enums… |
| Alarm Categories | 3 | — | 64=TC Alarm, 65=TC Warning, 66=TC Message |
| Alarms | 1017 | 5010001-5170005 | 17 categories |
| RCMD | **0** | — | **empty (header only)** |
| RCMD Params | **0** | — | **empty (header only)** |

### 3.2 SV families (113)
101 Control (5): ControlState, PreviousControlState, **EventsEnabled**,
**LastEventID**, Clock. 102 Alarm (2): AlarmsEnabled, AlarmsSet. 103 Spool (4):
SpoolCountActual/Total/StartTime/FullTime. 104 PP (1): PPError. 105 Process
(2): ProcessState (E30), PreviousProcessState. 106 PM1 (4): OperationMode/
RecipeActive/RecipeName/ReadyForProcess. 107 TM1 (2). 108/109 LP1/LP2 (17
each): E84 signals, Clamp/Door/IsMapped/CarrierPresent/MaterialMap/
OperationMode/State. 110 CJ queue (2): QueueAvailableSpace, QueuedCJobs.
111 Carrier (5): CarrierLocationMatrix, Reservation/Transfer/Association/
PortStateInfo lists. 112/113 LP1/LP2 detail (11 each). 114 PM1/Station1
substrates (3). 115 TM1 arms (6). 116 AL/Station1 (3). 117 Process values
(18): FFU pressures/fans, main pressure/vacuum, Vacuum8/12PM1/PM2.

### 3.3 DV families (102)
201 Control (2); 202 Alarm (3): AlarmID, AlarmCode (ALCD byte), AlarmText;
203 Limits (3); 204 EC (3): ECID, Current/PreviousValueOfLastChangedEC; 205
RemoteCommandName; 206 PP (3): PPChangeName/State/Owner; 207 TID; 208/209 PM
(14): RecipeName, WaferID, LotID, ResultFile, ResultPath, AbortReason,
PathOfImages, TestResults, SlotID, UnitFoupID, Results, SubstrateID; 210 TM
(2): EndEffector, Action; 211/212 LP RF tag (CarrierTag, PageNumber);
213 PRJob (10); 214 ControlJob (9); 215 Carrier (13); 216 Substrate (30);
217 Additional (7): ErrorCode/Text, ObjID/Type, ModelName, From/ToState.

### 3.4 Events columns (282 events)
Columns: ID | Name | Description | **Cascaded Events** | **Valid Variables**
| Previous State | Current State | **Enabled**.
- **Valid Variables** = DV names valid when the event fires — recommended
  report payload (e.g. PM1/ProcessingFinished → WaferID, LotID, RecipeName,
  ResultFile, ResultPath, PathOfImages, TestResults; OperatorCommandIssued →
  OperatorCommand).
- **Cascaded Events** = other events auto-triggered with it (e.g. 3190017
  Pausing2SettingUp cascades 3190016 Pause2Executing; 3210067 cascades
  3210065+3210059).
- **Enabled**: default flag — **all "Yes"**. Previous/Current State = state
  transition for state-driven events.
- Families: 301 Control (4), 302 Alarm (2), 303 Spool (3), 304 EC (1),
  305 Material (2), 306/307/308/309 PM1/TM1/LP1/LP2 material (2 each), 310
  RemoteCommand (2), 311 Terminal, 312 PP, 313 ProcessState, 314 PM (7:
  OperationModeChanged, ProcessingStarted/Finished/Aborted,
  RecipeSelected/RecipeSelectFailed, ProcessingResultArrived), 315 TM (3),
  316/317 LP1/LP2 (36 each: E84 TP1-6 timeouts, ValidOn, LReq/UReq,
  PIOE84Failure, InvalidCarrierType…), 318 Reader (2: SubstrateIdRead/
  ReadFailed), 319 PRJob (50), 320 ControlJob (21), 321 Carrier mgmt (68),
  322 Substrate mgmt (32), 323 Additional (2).

### 3.5 EC (37)
401 GEM: TimeFormat (1), HeartbeatInterval (0=off), EstablishCommunications-
Timeout (10 s), EnableWBit (1). 402 Spooling: **EnableSpooling (0=off)**,
OverWriteSpool (1=overwrite), MaxSpoolMessages (20), MaxSpoolTransmit (5).
403 TC: AllowOverrideFlow/ModuleRecipes (0), **MachineName ("DaVinci 200")**,
AlignmentAngleForReading150/200mm, WaferIDReadingMode. 404/405 LP1/LP2 (9
each): AutoUnload, UnclampControl, ReleaseControl, E84TP1-TP6. 406
IDReader/ReadMode (obsolete). 407 SetUpName. 408 BypassReadID. 409
SubstrateReaderEnabled. 410 PM1/Installed. (**Only PM1 — no PM2/PM3 ECs.**)

### 3.6 Tool-model metadata (Version History)
- v1.26 (18-09-25): added **DaVinci_200_MC4**. Note: "Bei der DaVinci_200_MC3
  sind in den Alarmen welche vom LP3 dabei. Diesen gibt es aber nicht." — the
  **MC3 alarm list includes LP3 alarms although MC3 has no LP3** (workbook has
  a Loadport 3 section: 5100001-5100013, 7 alarms; alarm sheets shared across
  variants).
- Other configs: Sentinel_300, DaVinci_i2OVL_G5 (1.26); DaVinci_200_MC3
  (1.25); Argos200F (1.24); DaVinci_200IR_MC1 (1.23); DaVinci150 (1.23);
  Deflector_300-F_MC1 (1.22); ARGOS_150_HT (1.21); Spector_5500_MC1 (1.18);
  DaVinci_200_MC2 (1.16).
- Notable evolution: SubstrateIdReadFailed + DV SubstID (1.17);
  ProcessingResultArrived + WaferID/LotID/Results (1.13) and Quality,
  PathOfKlarf, WaferCenterX/Y, FoilCenterX/Y, WaferRotation, WaferDiameter,
  ChipDimensionWidth/Height, FoilRoughness, WaferRoughness, SlotID,
  UnitFoupID (1.12); RecipeSelected/RecipeSelectFailed for Argos/Rembrandt +
  EC "Installed" (1.10); WaferIDReadingMode EC moved Reader→General (1.14);
  partition/batch DVs removed (1.1); ProcessState SV Error=7 (1.22).

---

## 4. DaVinci 200 operator/software manuals — SECS/GEM content

### 4.1 Software Operation Manual_EN (Kontron AIS GmbH)
- **Host interface = MueTec FabLink® suite** (L1485; FabLink 6.5.0, L1386),
  covering E5/E30/E37/E39/E40/E94 (L735-775).
- **9.6 Host Interface (L6305-6520)**: 300 mm + GEM services; in **Online
  Remote, interlocks block local job create/modify and local carrier
  management** (host only); Offline/Online Local → interlocks off (L6313-6322).
- **9.6.1 Parameters (Table 52, L6360-6424)**: Communication Mode
  **[Server, Client]**; Host Name/Address (TCP/IP; not required for Server);
  **TCP/IP Port (1-65535)**; **HSMS T3/T5/T6/T7/T8 [s] (1-120)**; Soft Start
  Timeout (1-36000); Loadports Bypass Read ID; PPM Allow Override Flow/Module
  Recipes. No fixed port documented (configurable).
- **9.6.2 Manual Operation (L6436-6520)**: readouts FabLink connection,
  **HSMS State, Communications State, Control State, Processing State,
  Spooling State, Spool full**; buttons **Offline / Online Local / Online
  Remote** (user right "Control Fab Hostinterface") = the ON-LINE/OFF-LINE
  switch.
- User rights incl. "Control Fab Hostinterface" (L1612-1615); light tower
  "SECS Offline" (L6802).
- Internal (non-SECS): [HostInterface] TLKPort **3000** / TLKAddress
  **127.0.0.1** FabLink↔ToolCommander (L2522-2544); InternalCommunication
  port Control↔Visualization (L2196); Modbus/TCP/IP to PLC in BaseConfig.xml
  (L2921-2922).
- **EAP**: only "Brooks Vision LEAP via TCP/IP" (L7007) — an aligner vision
  component, not SECS/GEM EAP integration. No other EAP notes.

### 4.2 DaVinci 200 User Manual V1.8
- GEM Status display Offline/Online Local/Online Remote with colors
  (L828-870); blue light = automatic mode (SECS/GEM protocol) (L521).
- Loading FOUP/cassette/SMIF + setting CarrierID in GEM Mode Offline and
  Local Online (4.6, L1172-1210); autoload-after-place config controls Job
  Creator behavior.
- **No host IP/port/SECS configuration content** (grep IP/TCP/Ethernet/
  FabLink: none) — operator-focused.

---

## 5. Data-loss-relevant facts across all docs

| Aspect | NexGen MG | SPTS fxP Omega | DaVinci 200 (MueTec) |
|---|---|---|---|
| Spooling | **Not implemented** (2.1; SVs 17-20 unsupported; CEIDs 16-18 defined) | **Full GEM spooling** (S2F43/44, S6F23, ECs 4004/4005/4009/4010, SVs 2016-2019, non-volatile across power cycle) | **Supported by SECS stack, default OFF** (EnableSpooling=0; OverWriteSpool=1, MaxSpoolMessages=20, MaxSpoolTransmit=5; SVs 1030001-4; events 3030001-3; HMI spool state/full LED) |
| Events while host disconnected | Lost (no buffering; DISABLED flushes output queues) | Spooled if enabled for those messages; non-spooled primaries discarded while SPOOL ACTIVE; stream 1 never spooled | Depends on EnableSpooling; no loss-behavior statement when disabled |
| Alarm retention | S5F6/F8 upload full/enabled lists; SVs 8/9 (9 unsupported); S5F3 zero-ALID = all | S5F5/6, S5F7/8; SVs 22-26 (ASer counter); S5F3 zero-ALID = all; alarm CEIDs computed | Alarms sheet (cat. 64/65/66); DVs 2020001-3; SVs 1020001-2; retention not discussed |
| Event enable-all | S2F37 zero CEID list = all (6.2; worked example disables all) | S2F37 zero list = all CEIDs (L4151) | Not stated in dump |
| Process programs | Unformatted only; PPBODY = .gz-wrapped .xml (7.6); S7F3-F20 | Unformatted cassette/wafer; EC 1000358/9; S7F65/66 | ECs AllowOverrideFlow/ModuleRecipes=0; PP DVs; RecipeExportPath |
| Lot / CSV lifecycle | No CSV/lot files; lot data via DVs 1830-1839, CEIDs 4/5 | No CSV; cassette-level processing (VCE A/B); DatabaseAutoSwap events 870/871 | No CSV in manuals (0 hits); Production Log view/delete/export rights; VCTC production-log alarms 5160023-7; results auto-sent to host (User Manual L1008-1010) |

### Cross-cutting notes / ambiguities
- NexGen: no HSMS port/device-ID/active-passive values in the SECS manual —
  they come from equipment configuration, not the doc.
- NexGen alarm table: 832 rows, IDs 12…299011+, groups General/System/
  Handling/PM1/PM2/Robot/…/Atmsi1/2 (L6055-9430); no official count; ALIDs do
  not map to CEIDs (8/9 are generic set/clear).
- SPTS: "Table 5" = CEID table in 7.2; S6F23 appendix row says "S7,F23" once
  (L1577) — typo. EC 4022 lets host choose S6F3/S6F11/S6F13 as event message.
- DaVinci xlsx: RCMD + RCMD Params sheets **empty** — no documented remote
  commands; alarm categories are TC severity codes, not GEM ALCD classes.
- DaVinci xlsx: only LP1/LP2 ECs + LP1/LP2 (and 7 LP3) alarm sections —
  consistent with 2-load-port tool (MC3 lacks LP3; MC4 config added
  2025-09-18); PM1-only EC/alarms suggest one PM modelled (or dump omits PM2).
