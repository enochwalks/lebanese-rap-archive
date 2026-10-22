# PS Lab – 3D machine maintenance map

A 3D map of the PS Lab floor in Bouar. Every machine is on the map. Click a machine, or any part of it, to see what is wrong with it, when the oil or filters are due, the condition of each part, and its history.

## Start it

Copy the whole `ps-lab` folder to the PC, keeping the `vendor` folder next to `index.html`. Then double-click `index.html` to open it in Chrome or Edge. It needs no installation and no internet. Internet is only used by the AI assistant.

## What is already in it

The PS Lab floor has a CNC hall (concrete columns) and a workshop room (white steel beams). It holds the 11 Gildemeister CNC machines, modelled from the photos:

| Machines | Code on the map |
|---|---|
| 4 × CTX 310 graziano | CTX310-1 … CTX310-4 |
| 2 × CTX 420 linear | CTX420-1, CTX420-2 |
| CTX beta 850, CTX beta 1250 | BETA-850, BETA-1250 |
| 2 × GMX 250 linear | GMX-1, GMX-2 |
| LNIX | LNIX |

Each machine is built from its real parts, and you can click every one of them in 3D. These include the main spindle and chuck, turret, tailstock or counter-spindle, axes, sliding door and safety window, CNC panel, hydraulic unit, central lubrication, air unit, coolant tank, chip conveyor and bin, electrical cabinet and filter mats, and the signal tower. The linear machines also have a cooling unit, and the GMX has the B-axis milling spindle and the mist extractor. Each machine also has the usual CNC-lathe service list: way oil, coolant, chuck grease, cabinet filters, hydraulic oil and filter, and so on.

## First-time setup

1. **Edit layout**: drag each machine to its exact place. The first layout is a guess from the photos.
2. Open each machine and press **▶ Start counting all from today** in **Oil & service** if you don't know the old service dates. Otherwise enter the real dates with **Edit**.
3. Check the intervals and oil types against each machine's manual and lubrication plate. The defaults are typical values, not the manufacturer's numbers.
4. Optional: **📷 Add photo** on each machine, and fill in the serial number and year from the name plate.
5. To track the conventional lathes and milling machines in the workshop too, use **+ Add machine** with the "Conventional lathe" or "Milling machine" type.

## Daily use

- The left panel shows what to do now: problems first, then overdue oil and filter changes, then parts to replace.
- **⚠ Report a problem** on a machine, or **Problem here** on a single part.
- **✔ Done today** on a service task resets its countdown.
- **Update hours** with the hour-meter reading so hour-based oil changes are counted.
- **Parts health view** colours every part in 3D: green = good, yellow = worn, orange = replace soon, red = broken.
- **Report** prints a list of everything that is due.

## Assistant

**Ask the assistant** answers questions such as "Which machines need an oil change?", "What is wrong with IM-01?" or "What needs to be done today?".

- **Without a key** it works offline and answers from the records.
- **With an Anthropic API key** (set it in **AI settings**) it understands any question in English, Arabic or French. It can also add general repair advice, which it labels as general. The key is saved only in that browser.

## Data and backups

All data is stored inside the browser on that PC. Use **Backup → Download backup** every week and keep the file safe. To see the same data on another PC, use **Load backup file** there.
