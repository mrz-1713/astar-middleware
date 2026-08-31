# DaVinci 200 HSMS Connection Guide - Design

Date: 2026-07-19

## Goal

Create a zero-assumption setup guide for connecting a DaVinci 200 to the ASTAR middleware.

## Outputs

1. Replace the existing Word guide with a complete rewrite.
2. Create a browser walkthrough with the same steps.
3. Use the DaVinci screenshots already extracted from the supplied PDFs.
4. Use the user's current Host Interface screenshot for the parameter step.

## Instruction Pattern

Every step uses only these fields:

- Location
- Action
- Expected result
- If not, when a failure branch is required

Sentences stay short. The guide contains no protocol lesson, glossary, background narrative, manual comparison, or filler.

## Sequence

1. Power on the DaVinci only when it is off.
2. Wait for Windows and ToolCommander.
3. Log in with the required rights.
4. Open Components.
5. Open Host Interface (Global).
6. Confirm the component is not Disabled.
7. Open Parameters.
8. Check Enable.
9. Select Server.
10. Set TCP/IP Port to 5000.
11. Save and reopen Parameters.
12. Confirm the saved values.
13. Confirm FabLink is green.
14. Confirm local TCP port 5000 is LISTENING.
15. Find the DaVinci computer's IPv4 address.
16. Enter that IPv4 address and port 5000 in ASTAR.
17. Set device ID 0, HSMS mode active, and enabled true.
18. Run the TCP test.
19. Start or restart the middleware.
20. Confirm HSMS is Connected/selected.
21. Confirm GEM Communications is Communicating.
22. Press Online Local.
23. Confirm the top GEM state is yellow Online Local.
24. Run the final connection checklist.

## Visual Design

- Document type: operator setup guide.
- Preset: `compact_reference_guide`.
- First-page pattern: compact adaptation of `editorial_cover`.
- One main screenshot per screen-based step.
- Numbered callouts identify the exact control.
- Screenshots remain inline, not floating.
- Each screenshot has useful alternative text.
- Green marks a passed result.
- Amber marks an operator action or warning.
- Red appears only for a failed result.
- Commands use a monospaced style.

## Failure Branches

- Host Interface disabled: change its operation mode before editing parameters.
- Enable does not stay checked: confirm login rights, then save again.
- FabLink is not green: stop before changing middleware settings.
- Port 5000 is not listening: check Enable, Server, port 5000, Save, and the component mode.
- TCP test fails: correct the DaVinci IPv4 address, network path, or firewall.
- TCP passes but HSMS does not select: confirm ASTAR uses active mode and DaVinci uses Server mode.
- HSMS selects but GEM is not Communicating: restart the middleware once and recheck.
- Connection works but GEM is Offline: press Online Local.

## Correctness Rules

- Online Local does not start the listener.
- Host Address is ignored when DaVinci is in Server mode.
- Do not use `localhost` in ASTAR unless ASTAR runs on the DaVinci computer.
- Do not press Online Remote during initial setup.
- Do not initialize or power-cycle a machine that is already running only to establish HSMS.
- The light tower is not proof of an HSMS connection.
- ASTAR's TCP test proves reachability only. It does not prove HSMS or GEM communication.

## Verification

1. Cross-check every DaVinci action against the supplied manuals and screenshots.
2. Cross-check ASTAR values against the workspace configuration and runtime behavior.
3. Render the DOCX to PNG.
4. Inspect every page at 100 percent zoom.
5. Fix clipping, overlap, poor image placement, or unclear callouts.
6. Run the DOCX accessibility audit.
7. Test the browser walkthrough at desktop and narrow widths.

## Alternatives Considered

- Two-page quick checklist: rejected because images would be too small for a beginner.
- Long training manual: rejected because it adds explanation the user does not need.
- Guided setup plus a short fault path: selected because it preserves exact actions and fast recovery.
