# SPDX-License-Identifier: MIT
"""Synthetic charge-daemon journals and sysfs trees. Every number here is made up to exercise a rule;
none of it is a recording from a real headset."""
import json, os

D = "deckard_charge_daemon[1000]: "


REAL_ORDER = ("hdr", "usb", "chg", "fg", "qph", "bcl")        # the order SteamOS logs them in (#2879 dumps)
OLD_ORDER = ("hdr", "bcl", "chg", "fg", "qph", "usb")


def block(qph=1, usb_v=15.0, neg_v=15.0, cur=1.5, neg_a=3.0, soc=60, stat="Charging", online=1, fg_cur=0.5,
          order=REAL_ORDER, qph_sel=None):
    lines = {
        "hdr": D + "---- Current State ----",
        "bcl": D + "BCL(1): l0=3600 l1=3600 [00000000 00000000 00000000]",
        "chg": D + f"CHG(1) - V:  8.4, Flt:  8.8;  Cur: {0.8 if stat == 'Charging' else -0.6:5.2f}, Lim:  1.3;  Stat: {stat}",
        "fg": D + f"FG INT - V: 8.100;            Cur: {fg_cur:5.2f};             SOC: {soc:3d} %",
        "qph": D + f"QPH({qph}) - Sel: {qph_sel or qph}; Load: 0xE",
        "usb": D + f"USB({online}) - V: {usb_v:4.1f}, Neg: {neg_v:4.1f};  Cur: {cur:5.2f}, Neg: {neg_a:4.1f};  Type: C PD [PD_PPS]",
    }
    return [lines[k] for k in order]


def journal(blocks, t0=1_800_000_000.0, every=20.0, fmt="journalctl"):
    """blocks: list of kwargs for block(); returns JSON lines like `journalctl -o json` (or {"t","line"})."""
    out = []
    for i, kw in enumerate(blocks):
        t = t0 + i * every
        for j, line in enumerate(block(**kw)):
            if fmt == "journalctl":
                out.append(json.dumps({"__REALTIME_TIMESTAMP": str(int((t + j * 0.001) * 1e6)), "SYSLOG_IDENTIFIER": "deckard_charge_daemon",
                                       "MESSAGE": line.split(": ", 1)[1]}))
            else:
                out.append(json.dumps({"t": t, "line": line}))
    return "\n".join(out) + "\n"


def sysfs(root, soc=50, status="Charging", online=1, volts=15_000_000, bat_ua=500_000, temp_dc=300, detail=None):
    """A fake /sys/class/power_supply: a fuel gauge, a charger chip that always claims 'Charging', a USB source."""
    for d in ("max1720x_bat_0-00", "pm0000-charger", "tcpm-source-psy-0"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    def w(p, v):
        with open(os.path.join(root, p), "w") as f:
            f.write(f"{v}\n")
    w("max1720x_bat_0-00/type", "Battery"); w("max1720x_bat_0-00/capacity", soc); w("max1720x_bat_0-00/status", status)
    w("max1720x_bat_0-00/current_now", bat_ua); w("max1720x_bat_0-00/temp", temp_dc)
    w("pm0000-charger/type", "Battery"); w("pm0000-charger/status", "Charging"); w("pm0000-charger/capacity", 99)
    det = {"Input OK": 1, "IMP Trigger": 0, "QPH2": 0, "Buck OK": 1, **(detail or {})}
    w("pm0000-charger/status_detailed", "\n".join(f"{k}: {v}" for k, v in det.items()))
    w("tcpm-source-psy-0/type", "USB"); w("tcpm-source-psy-0/online", online); w("tcpm-source-psy-0/voltage_now", volts)
    return root
