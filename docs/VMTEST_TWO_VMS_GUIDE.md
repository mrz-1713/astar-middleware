# Two-VM Test Rig — the short version

One ZIP, dragged to both VMs, double-clicked on each. No YAML typed by hand, no
IP read off `ipconfig` and retyped, no PowerShell commands, no `.exe` built.

- **VM A — the EAP.** Runs the middleware and the *ASTAR EAP Control* panel.
- **VM B — the test machine.** Runs the simulator pretending to be a tool.

They meet over HSMS/TCP, exactly as the middleware meets real equipment: the
simulator listens, the middleware dials out.

> Rig as built (VMware Fusion, Apple Silicon). Fusion's display name and the
> Windows hostname disagree on both VMs — confirm with `hostname`, never by the
> login-screen name, which shows the user account.
>
> | Fusion name | hostname | FabNet IP | role |
> |---|---|---|---|
> | ASTAR-SERVER | DESKTOP-F1UBSEM | 192.168.102.128 | VM A, middleware |
> | Windows 11 64-bit Arm 2 | DAVINCI-PC | 192.168.102.129 | VM B, simulator |

---

## 1. Build the ZIP (once, on the Mac)

```bash
./scripts/build_deploy_package.sh
```

One ZIP, ~97 MB, in `deploy_out/`. It carries the offline Python installer, all
wheels, the middleware, **and** the simulator — the same file goes to both VMs.

## 2. Copy it to both VMs

Turn on drag-and-drop in each VM's **Isolation** pane, then drag the ZIP onto
each window. Drop it in `Downloads`, not OneDrive.

Fusion on Apple Silicon has no Sharing pane, so VirtIO-FS shared folders are
unavailable. Drag-and-drop or an HTTP server on the Mac's `192.168.102.1`
FabNet interface are the transfer routes.

## 3. Extract, then double-click `SETUP.bat`

Extract the whole folder first. Windows will run a file from inside the ZIP
viewer, but it cannot see the files next to it — that is the one failure this
step has.

A window opens and asks what the computer is for:

- On **VM B**, pick **A test machine — it pretends to be a tool**.
- On **VM A**, pick **The EAP — it collects from machines**.

Press **Install**. Approve the administrator prompt. That is the whole install:
Python, dependencies, firewall rules, desktop shortcuts, and a starter config
are all done for you, and the right panel opens by itself when it finishes.

## 4. VM B — start the simulator

The *ASTAR Simulator* panel is already open on a working config: **EQUIPMENT**,
**PASSIVE**, bound to every adapter on port 5051.

1. On the **Equipment** tab pick a machine profile (e.g. `davinci_200_mc4_hc1`).
2. Press **Start**.

The **On the middleware machine** box now reads:

```text
Point the middleware at   192.168.102.129:5051
```

Press **Copy address** to put it on the clipboard. The inbound firewall rule
was added by the installer; the **Allow this port through Windows Firewall**
button re-adds it if you change the port.

### You do not set a bind address

A passive simulator is *waiting to be dialled*, so there is no address to
choose — only a port. The panel shows **Accepting on every adapter on this
machine** and lists what it found. The only two settings that must match on
both machines are the **HSMS port** and the **SECS device id**.

The bind address exists solely to *restrict* which adapter answers: it is
handed straight to `socket.bind`, and the default already accepts on all of
them. It is behind **Restrict to a single network adapter (advanced)** because
pinning one is almost always a mistake on a VM with both FabNet and WAN
adapters — bind the wrong one and the simulator becomes unreachable in a way
that looks exactly like a wrong IP on the middleware side. If DHCP later moves
the address, a pinned config breaks too.

Leave the panel open.

## 5. VM A — point the middleware at it and test

In *ASTAR EAP Control*, **Machines** tab:

1. Select a row (or **Add** one).
2. Set **Equipment host / IP** to VM B's address. The box lists this machine's
   own adapters and stays editable, so type VM B's address or paste what you
   copied.
3. Set **Port** to `5051` and **Middleware HSMS mode** to `active` — the
   simulator listens, so the middleware dials.
4. Press **Test connection**.

You get the tool's identity back:

```text
secs-ok: TOOL_02 192.168.102.129:5051 device_id=0 identity=['DaVinci200', 'DaVinci200 Version 4.9.3']
```

That is the link proven. **Test connection** probes directly when no Windows
service is running, so this works on a machine that was installed minutes ago —
and it deliberately ignores the Linkstuffs configuration, because whether a
cable works has nothing to do with where the data will eventually be sent.

---

## When it does not connect

The panel's failure message lists these three, in the order they actually bite:

| Symptom | Cause |
|---|---|
| Times out, looks like a wrong IP | Inbound port closed on VM B. Use the panel's firewall button. |
| Times out with the right IP | Both ends passive, or both active. One listens, one dials. |
| Connects then drops | `secs_device_id` mismatch between the two panels. |

A simulator bound to `169.254.x.x` means DHCP failed on that adapter. The
panel never offers those addresses, but a hand-edited config can still carry
one.

## Next step — a full run

Once `secs-ok` appears, the connection is done and the remaining work is
telemetry. Two things block a `run-service` run on this rig, both documented in
`docs/TWO_VM_FABNET_TEST_SETUP.md`:

- **Every enabled machine needs an upstream route.** Validation rejects
  `enabled: true` when both `linkstuffs.enabled` and `linkstuffs_http.enabled`
  are false. For a network-only test, set `linkstuffs_http.enabled: true` with
  a dummy device token — the publisher only enqueues to a local SQLite outbox.
- **`local_csv_path` defaults to `D:/MachineData/...`,** and `D:` is the DVD
  drive on a stock VM. Point it at `C:/SECSGEM_EAP/data/csv_in` and set
  `network_csv_path` to empty, or every event fails with `[WinError 5]` and no
  CSV is written.
