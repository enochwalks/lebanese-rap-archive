# PS Lab – 3D machine maintenance map

A 3D map of the PS Lab floor in Bouar. Every machine is on the map. Click a machine, or any part of it, to see what is wrong with it, when the oil or filters are due, the condition of each part, and its history.

## Start it

Copy the whole `ps-lab` folder to the PC, keeping the `vendor` folder next to `index.html`. Then double-click `index.html` to open it in Chrome or Edge. It needs no installation and no internet. Internet is only used by the AI assistant.

## First-time setup (about 30 minutes)

1. **Building & floor plan**: load the picture of the floor (from the `work` folder on the Desktop) and type the real width of the hall in metres.
2. **Backup → Delete all example machines**.
3. **+ Add machine** for each real machine. Pick the closest type (injection moulding, press, CNC, compressor, pump, conveyor, mixer, oven, generator, other). The type sets the 3D model and a starting list of parts and service tasks.
4. **Edit layout**: drag each machine onto its real place on the floor picture. Use ⟳ Rotate in the machine's panel to turn it.
5. Open each machine and correct the **Oil & service** intervals from the manufacturer's manual. The default intervals are only suggestions. Also write the oil type and quantity in the task notes.

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
