# Charging reports

Made by `tools/reports-table.py` from `data/reports.json`; don't edit this file by hand. Each row is one [charging report](https://github.com/frame-commons/charge-watch/issues?q=label%3Areport) someone chose to share. Make your own with `./charge_watch.py report` (see the README).

## Patterns so far

| Pattern | Reports | Builds | What it means |
|---|---|---|---|
| **The headset doesn't see the charger while it's on, but it charges while asleep** | 0 | – | While the headset is awake, nothing on its USB-C side reacts to the charger: no USB power, no USB-C partner, no power agreement. Asleep, the battery went up. So the charger and cable work, and the headset's USB-C power detection fails only while the system runs. Charge it asleep or off for now. charge-watch's auto-shutdown ("shutdown_at" in its settings) turns it off before it runs flat, and then it charges. |
| **The headset doesn't see the charger at all** | 0 | – | With the charger plugged in, nothing on the headset's USB-C side reacts: no USB power, no USB-C partner, no power agreement. Try another cable and another charger. If none is ever seen, it's the headset, not the charger. Run the sleep test too: some units charge only while asleep. |
| **The charger chip is stuck (the QPH2 bug, SteamOS issue #2879)** | 0 | – | The headset agreed power with the charger, but its charger chip went into its QPH2 mode and stopped taking input. Reseat the cable or switch chargers. #2879 has a workaround (a root change: read the thread first). |
| **The headset sees the charger but refuses its power** | 0 | – | The charger chip reports no usable input, without the QPH2 state. #2879 reports this with some power banks. Try a wall charger and another cable. |
| **The charger offers more than 5 V, but the headset stays at 5 V** | 0 | – | The charger advertises higher voltages, but the headset never agreed to one. It charges slowly at best, and drains while in use. Try another cable: a cable without USB-C PD support causes exactly this. |
| **Only 5 V: a slow charger or cable** | 0 | – | The headset gets 5 V and no USB-C power agreement. That charges slowly when idle and can't keep up in use. Use a USB-C PD charger (9 V or more) and a PD cable. |
| **Power comes in, but the battery still drains** | 0 | – | Current is flowing in, but the battery gauge shows the battery losing charge. The charger is too weak for the load. Use a stronger PD charger. |
| **Charging normally** | 0 | – | With this charger and cable, the battery is charging. If it fails at other times, run the report again then, while it's happening. |
| **No known pattern matched** | 0 | – | Nothing here matches a pattern charge-watch knows. Please share the report: it's exactly what's needed to learn a new one. |

## Every report

None yet.
