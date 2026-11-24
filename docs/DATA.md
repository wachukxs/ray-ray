# Dataset notes

Source: `data/raw/device-telemetry-dataset.ndjson` (downloaded from the link in the brief). 750 lines,
one point-in-time device snapshot per line.

| | |
|---|---|
| Companies | `acme-001` (10 devices), `globex-002` (8), `initech-003` (7) |
| Employees | 14 distinct IDs (5 / 4 / 5). Numbering overlaps across tenants (`emp-acme-1002` vs `emp-globex-1002`) |
| Window | 2026-05-14 → 2026-06-12, exactly 30 snapshots per device (~daily) |
| OS | macOS 13.6 / 14.5 / 15.3 / 15.4; Windows 10 22H2 / 11 22H2 / 11 23H2 |
| Models | MacBook Pro, MacBook Air, Mac mini, ThinkPad X1 Carbon, Dell XPS 15 |
| Compliance checks | `disk_encryption` (high), `screen_lock` (medium), `os_up_to_date` (medium) |

## Record shape

`device_id, company_id, employee_id, collected_at, agent_version, os{…, product_version, hostname},
device_identity{model_name, processor, total_memory, …}, memory{total_memory_bytes, used_memory_bytes, …},
disk_volumes[{mount_point, size_bytes, available_bytes, encrypted}], battery{condition, cycle_count,
full_charge_capacity, …} | null, network[…], installed_software[{name, version, publisher}],
compliance_results[{check_id, status, severity}]`

## Quirks and how they are handled

| Observation | Handling |
|---|---|
| `battery` is `null` on 100 snapshots: 90 are Mac minis (no battery), 10 are laptop telemetry gaps (MT7PJB7N5LRE ×5, others ×1–2) | A device is a laptop if it *ever* reported a battery. Gaps are reported as missing data, not as removal. |
| `network` missing on 16 snapshots | Not used. |
| No battery *design* capacity | Battery health is based on condition, cycle count (1000 rated) and the full-charge-capacity trend. The tool output states this limitation. |
| One disk volume per device, mounted at `/` or `C:\` | The system volume is chosen by mount point. |
| `disk_encryption` (the only high-severity check) passes on every snapshot | "Laptops failing high-severity checks" correctly returns **none** (a test of not inventing an answer). |
| `os_up_to_date` fails on every snapshot for 19 devices | Reported as a persistent failure, not as drift. |
| Windows versions like `10 22H2` | Numeric tuple compare `(10, 22) < (11,)`. |
| Apple-silicon Macs (processor `Apple M*`) | RAM and storage are soldered. The policy refuses RAM/storage upgrade orders and points to replacement instead. |

## Signals present in the data (what a good copilot should find)

| Signal | Devices |
|---|---|
| Low disk now (<10% free) | Acme MT7PJB7N5LRE, 8NM23J95R5I6 · Globex JRZSGXVMKE6M, E4V0SB5I0MYU · Initech DARFCP0BQM2G, RLD7WV7J82SH |
| Battery end of life ("Service Recommended", ≥1000 cycles, capacity −4.2%) | Acme 7VP16KHV88LM, 8NM23J95R5I6, M4XVHUV1MEPZ |
| Sustained RAM pressure (>90% used in ≥50% of snapshots) | Acme M4XVHUV1MEPZ (Dell, 16 GB) · Initech I3JLKILKQTAC (Mac mini, 8 GB, soldered). Globex JRZSGXVMKE6M is intermittent (27%): watchlist only |
| Compliance drift: `screen_lock` pass → fail mid-window | 5 Acme, 2 Globex, 3 Initech devices |
| Unapproved software (uTorrent, TeamViewer, CleanMyMac X) | 2 Acme, 1 Globex, 3 Initech devices |
