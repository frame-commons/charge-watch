# SPDX-License-Identifier: MIT
"""Log excerpts posted in public on SteamOS issue #2879, copied as posted except the hostname (dropped).
Unlike fixtures.py these are real headsets. Each says where it came from and what state the unit was in."""

# KangaBearSkippy, 2026-11-18: a replacement headset working fine, charged, on Valve's 45 W charger.
# https://github.com/ValveSoftware/SteamOS/issues/2879#issuecomment-6993898843
# `journalctl -b -t deckard_charge_daemon | grep -E "QPH2|AICL"` (time, message):
HEALTHY_45W_JOURNAL = [
    ("08:36:32", "Triggering AICL"),
    ("08:36:32", "QPH(1) - Sel: AICL; Load: 0xE"),
    ("08:36:32", "QPH(1) - Sel: AICL; Load: 0xE"),
    ("08:36:32", "QPH(1) - Sel: AICL; Load: 0xE"),
    ("08:36:51", "QPH(1) - Sel: AICL; Load: 0xA"),
    ("08:36:51", "QPH(1) - Sel: AICL; Load: 0x0"),
    ("08:36:51", "QPH(1) - Sel: AICL; Load: 0x0"),
    ("08:37:07", "QPH(1) - Sel: AICL; Load: 0x0"),
    ("08:37:07", "QPH2 AICL Timeout, Trying anyway"),
    ("08:37:07", "QPH2 Entered"),
    ("08:37:07", "Exiting QPH2 - Low Current and No Load"),
]
# `cat /sys/class/power_supply/pm8550b-charger/status_detailed`, same unit, same time:
HEALTHY_45W_STATUS_DETAILED = """JEITA: 0
CV: 1
CC: 0
ICL: 0
Boost: 0
Buck: 1
USB: 1
Input OK: 1
Buck OK: 1
Temp OK: 1
BATT OK: 1
CHG Ctrl OK: 1
USB UV: 0
USB OV: 0
USB Suspend: 0
IMP Trigger: 0
Buck SS Done: 1
Buck OC: 0
VPH UV: 0
Die Temp Bad: 0
JEITA Cold: 0
JEITA Hot: 0
QPH VBAT1 OK: 1
QPH VIN OK: 1
"""

# arealjim, 2026-11-16: the last status block from a unit that later died on the charger. At this moment it was
# in QPH2 but drawing ~1 A at 15 V with the battery full: NOT the 0 A stuck state.
# https://github.com/ValveSoftware/SteamOS/issues/2879#issuecomment-6931139380
QPH2_WITH_CURRENT_FULL = [
    "QPH(2) - Sel: 2; Load: 0xE",
    "CHG(1) - V:  8.8, Flt:  8.8;  Cur: -0.03, Lim:  1.3;  Stat: Charging",
    "FG INT - V: 8.784;            Cur: -0.00;             SOC: 100 %",
    "USB(1) - V: 14.9, Neg: 15.0;  Cur:  0.99, Neg:  3.0;  Type: Unknown",
]


def secs(hms):
    h, m, s = map(int, hms.split(":"))
    return float(h * 3600 + m * 60 + s)
