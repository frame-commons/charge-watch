#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""charge-watch: warn when a Steam Frame is plugged in but not actually charging.

  charge-watch run [--alert ...]        watch and warn out loud (the systemd user unit runs this)
  charge-watch status [--json]          one reading: what the charger and battery say, and the verdict
  charge-watch replay FILE [--json]     run the rules over a saved journal (journalctl -o json) and list alerts
  charge-watch test-alert [--push]      play the warning once (and send a test push), to check it reaches you
  charge-watch test-shutdown            play the low-battery shutdown warning; shows the power-off command, runs nothing
  charge-watch report [--sleep-test]    guided unplug/plug check (and sleep test) -> one text file to read and share

Sources, best first (field names from SteamOS issue #2879):
  1. The charge daemon's status block, logged every ~20 s by deckard_charge_daemon. The lines come in any order;
     these values are made up:
       ---- Current State ----
       USB(1) - V: 15.0, Neg: 15.0;  Cur: -0.02, Neg:  3.0;  Type: C PD [PD_PPS]
       CHG(1) - V:  7.6, Flt:  8.8;  Cur:  1.10, Lim:  1.0;  Stat: Not charging
       FG INT - V: 7.600;            Cur: -1.20;             SOC:  42 %
       QPH(2) - Sel: 2; Load: 0xE
  2. sysfs: the battery fuel gauge (status, current_now, temp), the charger chip's status_detailed
     (Input OK / IMP Trigger / QPH2), whether a USB supply is online, and /run/deckardcharger/typec_contract.
     The fuel gauge is what counts: the charger chip's own status is not reliable while it is stuck.

Warnings. Each must last its whole hold time, and none fires at 95 % or above or while the battery is hot
(it pauses charging on purpose when hot):
  stuck         charger in QPH2 with no input (Input OK 0 + IMP Trigger 1 or QPH2 1, or QPH(2) / "QPH2 Entered"
                with < 0.10 A in): the #2879 bug
  no-input      plugged in, but under 0.05 A comes in, or Input OK 0 without QPH2 (the power-bank variant)
  cant-keep-up  the gauge shows the battery losing charge while real current comes in: charger too weak
  discharge     the gauge shows the battery losing charge while plugged in, cause unknown
  draining      backstop: the battery % fell by >= --drop points within --window while plugged in
  slow          5 V only (said once per plug-in; it may still charge, slowly)

Alerts are loud on purpose: an alarm played --beeps times, with the volume raised to at least --volume-floor (and
unmuted) for the alarm, then put back. Severe warnings repeat every --repeat s, every --repeat-low s at 15 % or
below. Optional phone push (ntfy, Pushover, Discord, any webhook) is set in ~/.config/charge-watch/config.json.

Optional auto-shutdown (--shutdown-at N, off by default): at N % or below and not charging, plugged in or not, it
warns, counts down --countdown s, and powers off, unless charging starts first. It is a last resort, not protection:
#2879 has units that drained while powered off on a stuck charger and never came back. After it shuts down, unplug
it and charge it on a charger that works. It cannot help while the headset is asleep.

Blind spots: it runs only while the headset is awake. Asleep (charging by firmware) and powered off it cannot
watch or warn, and #2879 has units that died in exactly those states.
Nothing leaves the device unless you set up push; then only the warning text and battery % go to the service you
name. Events go to ~/.local/state/charge-watch/events.jsonl.
"""
import argparse, getpass, glob, json, math, os, re, select, shlex, shutil, signal, socket, struct, subprocess, sys, tempfile, \
    textwrap, threading, time, urllib.parse, urllib.request, wave

DAEMON = "deckard_charge_daemon"
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "charge-watch")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "charge-watch", "config.json")

# ---------- parsing the daemon's status block ----------

NUM = r"(-?\d+(?:\.\d+)?)"
PATTERNS = {
    "qph": re.compile(r"QPH\((\d+)\)"),
    "entered": re.compile(r"\bQPH2 Entered\b"),        # logged on entry to QPH2, after its QPH(2) line (#2879)
    "usb": re.compile(r"USB\((\d+)\) - V:\s*" + NUM + r",\s*Neg:\s*" + NUM + r";\s*Cur:\s*" + NUM + r",\s*Neg:\s*" + NUM),
    "fg": re.compile(r"FG INT - V:\s*" + NUM + r";\s*Cur:\s*" + NUM + r";\s*SOC:\s*(\d+)\s*%"),
    "chg": re.compile(r"CHG\((\d+)\) - V:\s*" + NUM + r".*?Cur:\s*" + NUM + r".*?Stat:\s*([A-Za-z][A-Za-z ]*?)\s*$"),
}
HEADER = "---- Current State ----"
BLOCK_GAP = 3.0   # s: lines of one block are logged together; a longer gap starts a new block


def message_text(msg):
    """journald's MESSAGE is a string, or a byte array for non-UTF-8 text."""
    if isinstance(msg, list):
        return bytes(msg).decode(errors="replace")
    return msg or ""


class BlockParser:
    """Groups daemon lines into status blocks, whatever order the lines come in. A block ends at the next header,
    when one of its fields repeats, or after BLOCK_GAP s of quiet (`stale`). feed() returns a finished block or
    None; each block is a dict with only the fields it carried."""

    def __init__(self):
        self.cur, self.seen, self.last_t = None, set(), None

    def _start(self, t):
        self.cur, self.seen = {"t": t, "src": "daemon"}, set()

    def feed(self, t, line):
        out = None
        if self.cur is not None and self.last_t is not None and t - self.last_t > BLOCK_GAP:
            out = self.flush()
        self.last_t = t
        if HEADER in line:
            done = self.flush()
            self._start(t)
            return done or out
        got = {}
        for key, pat in PATTERNS.items():
            m = pat.search(line)
            if m:
                got[key] = m
        if not got:
            return out
        if self.cur is not None and set(got) & self.seen:
            out = self.flush() or out
        if self.cur is None:
            self._start(t)
        self.seen |= set(got)
        c = self.cur
        if "qph" in got:
            c["qph"] = int(got["qph"].group(1))
        if "entered" in got:
            c["qph"] = 2
        if "usb" in got:
            m = got["usb"]
            c.update(usb_online=int(m.group(1)), usb_v=float(m.group(2)), usb_neg_v=float(m.group(3)),
                     usb_cur=float(m.group(4)), usb_neg_a=float(m.group(5)))
        if "fg" in got:
            c["fg_cur"], c["soc"] = float(got["fg"].group(2)), int(got["fg"].group(3))
        if "chg" in got:
            c["chg_cur"], c["chg_stat"] = float(got["chg"].group(3)), got["chg"].group(4)
        return out

    def flush(self):
        """End the current block and return it (None if it carried nothing)."""
        c = self.cur
        self.cur, self.seen = None, set()
        return c if c and len(c) > 2 else None

    def stale(self, now):
        """For live use: end a block that has been quiet for BLOCK_GAP s."""
        if self.cur is not None and self.last_t is not None and now - self.last_t > BLOCK_GAP:
            return self.flush()
        return None


def samples_from_lines(pairs):
    bp = BlockParser()
    for t, line in pairs:
        s = bp.feed(t, line)
        if s:
            yield s
    s = bp.flush()
    if s:
        yield s


# ---------- sysfs ----------

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(v, scale=1.0):
    try:
        return int(v) / scale
    except (TypeError, ValueError):
        return None


DETAIL = re.compile(r"([A-Z][A-Za-z0-9 _]*?):\s*(-?\d+)")


def parse_detail(text):
    """The charger chip's `status_detailed`: 'Name: value' pairs, one or several per line -> {name: int}."""
    return {k.strip(): int(v) for k, v in DETAIL.findall(text or "")}


def qph2_stuck(detail):
    """The #2879 bug in the charger chip's status_detailed: no input accepted (Input OK 0) together with the
    impedance trigger QPH2 entry sets (IMP Trigger 1), or a QPH2 field of 1 where the driver shows one. Input OK 0
    with both at 0 is the separate power-bank case (no-input)."""
    return bool(detail) and detail.get("Input OK") == 0 and (detail.get("IMP Trigger") == 1 or detail.get("QPH2") == 1)


def read_sysfs(root="/sys/class/power_supply", run_dir="/run/deckardcharger", typec=None):
    """The battery's fuel gauge (preferred over the charger chip), whether a USB supply is online, the offered
    voltage, and the charger chip's status_detailed if present.
    Plugged in also counts when a USB-C partner is attached and a power contract of 4.5 V or more stands, even with
    every supply offline: the power-bank case in #2879 (15 V agreed, Input OK 0, the charger chip offline)."""
    s = {"t": time.time(), "src": "sysfs"}
    online, bats = False, []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        name, typ = os.path.basename(d), _read(os.path.join(d, "type"))
        if os.path.exists(os.path.join(d, "status_detailed")):
            s["detail"] = parse_detail(_read(os.path.join(d, "status_detailed")))
        if typ == "Battery" and "charger" not in name:
            bats.append(d)
        elif (typ in ("USB", "USB_C", "USB_PD", "Mains") or name.startswith("tcpm-source")) \
                and _read(os.path.join(d, "online")) == "1":
            online = True
            v = _num(_read(os.path.join(d, "voltage_now")) or _read(os.path.join(d, "voltage_max")), 1e6)
            if v:
                s["usb_neg_v"] = max(s.get("usb_neg_v", 0), v)
    bats.sort(key=lambda d: (not os.path.basename(d).startswith("max1720x"), d))  # the Frame's fuel gauge first
    if bats:
        d = bats[0]
        cap = _num(_read(os.path.join(d, "capacity")))
        s["soc"] = int(cap) if cap is not None else None
        s["bat_status"] = _read(os.path.join(d, "status"))
        s["bat_cur"] = _num(_read(os.path.join(d, "current_now")), 1e6)
        s["bat_temp"] = _num(_read(os.path.join(d, "temp")), 10)
    if _read(os.path.join(run_dir, "ac_status")) == "Connected":
        online = True
    m = re.search(r"Voltage=(\d+)mV", _read(os.path.join(run_dir, "typec_contract")) or "")
    if m:
        s["usb_neg_v"] = int(m.group(1)) / 1000
        typec = typec or os.path.join(os.path.dirname(os.path.abspath(root)), "typec")
        if s["usb_neg_v"] >= 4.5 and glob.glob(os.path.join(typec, "port*-partner")):
            online = True     # the partner exists only while something is attached, so a stale contract can't count
    s["usb_online"] = 1 if online else 0
    return s


def merge_sysfs(s, sysfs):
    """Add the fuel gauge, temperature and charger-chip detail to a daemon block."""
    for k in ("bat_status", "bat_cur", "bat_temp", "detail"):
        if sysfs.get(k) is not None:
            s.setdefault(k, sysfs[k])
    if "usb_online" not in s and "usb_online" in sysfs:
        s["usb_online_sysfs"] = sysfs["usb_online"]
    return s


# ---------- rules ----------

DEFAULTS = {"drop": 2, "window": 600, "hold": 60, "drain_hold": 90, "slow_hold": 120, "repeat": 60,
            "repeat_low": 30, "low": 15, "hot": 42.0, "full": 95,
            "volume_floor": 0.8, "beeps": 3, "push_repeat": 600,
            "shutdown_at": None, "shutdown_hold": 60, "countdown": 60, "voice": "auto"}

MESSAGES = {
    "stuck": "Power warning. Plugged in, but the charger is stuck and taking no power. "
             "Reseat the cable, or try another charger or cable.",
    "no-input": "Power warning. Plugged in, but no power is coming in. Reseat the cable, or try another charger or cable.",
    "cant-keep-up": "Power warning. The charger can't keep up and the battery is going down. "
                    "Use a stronger charger or a better cable.",
    "discharge": "Power warning. Plugged in, but the battery is discharging. Reseat the cable, or try another charger or cable.",
    "draining": "Power warning. The headset is plugged in but the battery is going down. "
                "Reseat the cable, or try another charger or cable.",
    "slow": "Slow charger. Only five volts. The headset may not keep up while in use.",
    "ok": "Charging again.",
    "shutdown-warn": "Battery low and not charging. Shutting down in {countdown} seconds. "
                     "Plug into a charger that works to cancel.",
    "shutdown-cancel": "Charging. Shutdown cancelled.",
    "poweroff-failed": "Could not shut down by itself. The battery is nearly empty: shut the headset down now, "
                       "or plug it into a charger that works.",
    "poweroff": "Shutting down now. Unplug this charger: the battery can still drain while off. "
                "Charge it on a different charger, and check the battery goes up.",
}
TITLES = {"stuck": "charger stuck, not charging", "no-input": "plugged in, no power coming in",
          "cant-keep-up": "charger can't keep up", "discharge": "discharging while plugged in",
          "draining": "battery dropping while plugged in", "slow": "slow 5 V charger", "ok": "charging again",
          "shutdown-warn": "low battery, shutting down soon", "shutdown-cancel": "shutdown cancelled",
          "poweroff": "shut down, battery low: unplug it", "poweroff-failed": "could not shut down, do it by hand",
          "test": "test alert"}
SEVERE = ("stuck", "no-input", "cant-keep-up", "discharge", "draining")  # most specific first
URGENT = SEVERE + ("shutdown-warn", "poweroff", "poweroff-failed")


def plugged(s):
    """True / False, or None when this sample does not say (a partial block)."""
    if "usb_online" in s:
        if s.get("src") == "daemon":
            return s["usb_online"] == 1 and (s.get("usb_v") or 0) >= 4.5
        return s["usb_online"] == 1
    if "usb_online_sysfs" in s:
        return s["usb_online_sysfs"] == 1
    return None


def battery_current(s):
    return s.get("bat_cur") if s.get("bat_cur") is not None else s.get("fg_cur")


def not_charging_for_sure(s):
    """For powering off, every source must agree it is not charging: no current into the battery by the daemon's
    gauge line or by sysfs (whose sign is not confirmed on every driver), and no Charging/Full status."""
    for cur in (s.get("fg_cur"), s.get("bat_cur")):
        if cur is not None and cur > 0.05:
            return False
    if s.get("bat_status") in ("Charging", "Full"):
        return False
    return s.get("fg_cur") is not None or s.get("bat_cur") is not None or s.get("bat_status") is not None


def charging_now(s):
    cur = battery_current(s)
    if cur is not None:
        return cur > 0.05
    return (s.get("bat_status") or s.get("chg_stat")) in ("Charging", "Full")


class Rules:
    """Feed samples in time order; `step` returns the warnings that are active (held long enough) now."""

    def __init__(self, **kw):
        self.o = {**DEFAULTS, **{k: v for k, v in kw.items() if v is not None}}
        self.hist, self.since, self.active = [], {}, []

    def _held(self, name, cond, t, hold):
        if not cond:
            self.since.pop(name, None)
            return False
        self.since.setdefault(name, t)
        return t - self.since[name] >= hold

    def step(self, s):
        p = plugged(s)
        if p is None:           # a partial block says nothing about the charger: keep the state as it is
            return self.active
        t, soc = s["t"], s.get("soc")
        if not p:
            self.hist, self.since, self.active = [], {}, []  # an unplug restarts everything
            return []
        if soc is not None:
            self.hist.append((t, soc))
        self.hist = [h for h in self.hist if h[0] >= t - self.o["window"]]
        guard = (soc is None or soc < self.o["full"]) and (s.get("bat_temp") or 0) < self.o["hot"]
        detail, usb_cur, active = s.get("detail") or {}, s.get("usb_cur"), []
        stuck = qph2_stuck(detail) or \
                (s.get("qph") == 2 and usb_cur is not None and usb_cur < 0.10)
        if self._held("stuck", guard and stuck, t, self.o["hold"]):
            active.append("stuck")
        noin = not stuck and (detail.get("Input OK") == 0 or (usb_cur is not None and usb_cur < 0.05))
        if self._held("no-input", guard and noin, t, self.o["hold"]):
            active.append("no-input")
        cur = battery_current(s)
        losing = cur <= -0.10 if cur is not None else s.get("bat_status") == "Discharging"
        if self._held("losing", guard and losing, t, self.o["drain_hold"]) and not active:
            active.append("cant-keep-up" if usb_cur is not None and usb_cur >= 0.30 else "discharge")
        dropped = bool(self.hist) and soc is not None and max(h[1] for h in self.hist) - soc >= self.o["drop"]
        if self._held("draining", guard and dropped, t, self.o["drain_hold"]) and not active:
            active.append("draining")
        v = s.get("usb_neg_v")
        if self._held("slow", guard and v is not None and v <= 5.5, t, self.o["slow_hold"]):
            active.append("slow")
        self.active = active
        return active


class Announcer:
    """Turns active warnings into alerts: severe ones repeat every `repeat` s, `slow` is said once per plug-in,
    and 'Charging again' is said only when a severe warning clears AND the battery is actually charging."""

    def __init__(self, repeat=DEFAULTS["repeat"], repeat_low=DEFAULTS["repeat_low"], low=DEFAULTS["low"]):
        self.repeat, self.repeat_low, self.low = repeat, repeat_low, low
        self.last, self.said_slow, self.severe_on = {}, False, False

    def step(self, t, active, plugged_now, charging=False, soc=None):
        out = []
        if plugged_now is False:
            self.said_slow, self.severe_on = False, False
            return out
        severe = [a for a in SEVERE if a in active]
        every = self.repeat_low if soc is not None and soc <= self.low else self.repeat
        for a in severe[:1]:  # one message at a time, the most specific
            if t - self.last.get(a, -1e18) >= every:
                out.append(a); self.last[a] = t
        if "slow" in active and not severe and not self.said_slow:
            out.append("slow"); self.said_slow = True
        if self.severe_on and not severe:
            if charging:
                out.append("ok")
            self.last.clear()
        self.severe_on = bool(severe)
        return out


class ShutdownGuard:
    """Off unless `at` is set. When the battery is at or below `at` % and not charging (plugged in or not) for
    `hold` s, step() returns 'shutdown-warn'; `countdown` s later 'poweroff', unless charging lasts `ok_hold` s
    or the battery climbs above `at` first ('shutdown-cancel').

    Every time here is `now`, the moment a sample is *processed* (time.monotonic() when live), never the sample's
    own timestamp: journal replays, sleep and clock steps must not shorten the hold or the countdown. A gap of more
    than `gap` s between samples (the headset slept, or the daemon went quiet) starts it all over."""

    def __init__(self, at=None, hold=DEFAULTS["shutdown_hold"], countdown=DEFAULTS["countdown"], ok_hold=30, gap=90):
        self.at, self.hold, self.countdown, self.ok_hold, self.gap = at, hold, countdown, ok_hold, gap
        self.since = self.deadline = self.ok_since = self.last = None
        self.done = False

    def _reset(self):
        out = ["shutdown-cancel"] if self.deadline is not None else []
        self.since = self.deadline = self.ok_since = None
        return out

    def step(self, s, now=None):
        if not self.at or self.done:
            return []
        now = s["t"] if now is None else now
        soc = s.get("soc")
        if soc is None:          # a partial block says nothing about the battery
            return []
        if self.last is not None and now - self.last > self.gap:
            self.since = self.deadline = self.ok_since = None     # stale: start over, silently
        self.last = now
        if soc > self.at:
            return self._reset()
        if not_charging_for_sure(s):
            self.ok_since = None
        else:                    # any sign of charging: wait until it has lasted ok_hold s before standing down
            if self.ok_since is None:
                self.ok_since = now
            if now - self.ok_since >= self.ok_hold:
                return self._reset()
            return []
        if self.deadline is not None:
            if now >= self.deadline:
                self.done = True
                return ["poweroff"]
            return []
        if self.since is None:
            self.since = now
        if now - self.since >= self.hold:
            self.deadline = now + self.countdown
            return ["shutdown-warn"]
        return []

    def failed(self, now):
        """The power-off did not happen: warn again and retry after another countdown."""
        self.done, self.deadline = False, now + self.countdown


class PushGate:
    """Phone pushes are rarer than sounds: an urgent kind is pushed when it starts and then at most every
    `every` s while it lasts; everything else (slow, ok, shutdown-cancel) once each time it is said."""

    def __init__(self, every=DEFAULTS["push_repeat"]):
        self.every, self.last = every, {}

    def allow(self, t, kind):
        if kind in SEVERE:
            if t - self.last.get(kind, -1e18) < self.every:
                return False
            self.last[kind] = t
        elif kind == "ok":
            self.last.clear()
        return True


# ---------- alerts ----------

def chime_wav(path, kind="warn", beeps=3, rate=22050):
    """Written with the stdlib. 'warn': a loud high-low alarm, `beeps` times over; 'ok': a short rising chime."""
    if kind == "ok":
        notes, amp = [(660, 0.15, 0), (880, 0.22, 0)], 0.5
    else:
        notes, amp = [(1046, 0.16, 0.04), (784, 0.16, 0.04), (1046, 0.16, 0.04), (784, 0.30, 0.35)] * max(1, beeps), 0.9
    frames = bytearray()
    for f, d, gap in notes:
        n = int(rate * d)
        for i in range(n):
            env = min(1.0, i / (0.005 * rate), (n - i) / (0.02 * rate))
            frames += struct.pack("<h", int(amp * 32767 * env * math.sin(2 * math.pi * f * i / rate)))
        frames += b"\0\0" * int(rate * gap)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))


SINK = "@DEFAULT_AUDIO_SINK@"


def sink_volume():
    """(volume, muted) of the default output from `wpctl get-volume`, or None."""
    if not shutil.which("wpctl"):
        return None
    try:
        out = subprocess.run(["wpctl", "get-volume", SINK], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Volume:\s*([\d.]+)", out)
    return (float(m.group(1)), "MUTED" in out) if m else None


class Loud:
    """For the length of an alarm: unmute and raise the default output to at least `floor` (0..1), then put
    the volume and mute back as they were. floor 0 = leave the volume alone."""

    def __init__(self, floor):
        self.floor, self.prev = floor or 0, None

    def __enter__(self):
        self.prev = sink_volume() if self.floor > 0 else None
        if self.prev:
            vol, muted = self.prev
            if muted:
                _run_quiet(["wpctl", "set-mute", SINK, "0"], 5)
            if vol < self.floor:
                _run_quiet(["wpctl", "set-volume", SINK, f"{self.floor:.2f}"], 5)
        return self

    def __exit__(self, *exc):
        if self.prev:
            vol, muted = self.prev
            if vol < self.floor:
                _run_quiet(["wpctl", "set-volume", SINK, f"{vol:.2f}"], 5)
            if muted:
                _run_quiet(["wpctl", "set-mute", SINK, "1"], 5)
        return False


def find_player():
    for p in ("pw-play", "paplay", "aplay"):
        if shutil.which(p):
            return p
    return None


# ---------- speech ----------
# Spoken lines, one per alert kind. The bundled clips in voice/ are these lines, made by tools/make-voice.py with
# Kokoro-82M on a PC (synthetic speech, no recording of anyone). Drop your own <kind>.wav in
# ~/.config/charge-watch/voice/ to replace any of them, your own voice for example.
SPOKEN = {
    "stuck": MESSAGES["stuck"], "no-input": MESSAGES["no-input"], "cant-keep-up": MESSAGES["cant-keep-up"],
    "discharge": MESSAGES["discharge"], "draining": MESSAGES["draining"], "slow": MESSAGES["slow"], "ok": MESSAGES["ok"],
    "shutdown-warn": "Battery low and not charging. Shutting down soon. "
                     "Plug into a charger that works to cancel.",
    "shutdown-cancel": MESSAGES["shutdown-cancel"], "poweroff": MESSAGES["poweroff"],
    "poweroff-failed": MESSAGES["poweroff-failed"], "test": "This is a charge-watch test.",
}
VOICE_MODES = ("auto", "tts", "off")


def voice_dirs():
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return [os.path.join(cfg, "charge-watch", "voice"),            # your own recordings win
            os.path.join(data, "charge-watch", "voice"),           # what install.sh copied
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "voice")]   # running from the checkout


def clip_for(kind):
    for d in voice_dirs():
        p = os.path.join(d, kind + ".wav")
        if os.path.isfile(p):
            return p
    return None


_TTS = {}


def working_tts():
    """A text-to-speech engine that really speaks, or None. Being installed is not enough: SteamOS on the Frame
    ships spd-say, but its only working module is 'dummy', which plays a recording saying speech isn't set up
    (issue #1). So espeak-ng counts only if it writes real audio, and spd-say only with a module besides dummy.
    Returns ("file", exe) to synthesize a WAV and play it ourselves, or ("spd-say",)."""
    if "v" in _TTS:
        return _TTS["v"]
    v = None
    for exe in ("espeak-ng", "espeak"):
        if shutil.which(exe):
            try:
                r = subprocess.run([exe, "--stdout", "test"], capture_output=True, timeout=10)
                if r.returncode == 0 and r.stdout[:4] == b"RIFF" and len(r.stdout) > 2000:
                    v = ("file", exe); break
            except (OSError, subprocess.SubprocessError):
                pass
    if v is None and shutil.which("spd-say"):
        try:
            out = subprocess.run(["spd-say", "-O"], capture_output=True, text=True, timeout=10).stdout
            mods = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "OUTPUT MODULES"]
            if any(m != "dummy" for m in mods):
                v = ("spd-say",)
        except (OSError, subprocess.SubprocessError):
            pass
    _TTS["v"] = v
    return v


def speech_path(voice="auto", say_cmd=None):
    """What `speak` would use, for status: 'your --say-cmd', 'bundled clips', 'espeak-ng', 'spd-say' or 'none'."""
    if voice == "off":
        return "off"
    if say_cmd:
        return "your say_cmd"
    if voice == "auto" and clip_for("stuck") and find_player():
        return "voice clips (" + os.path.dirname(clip_for("stuck")) + ")"
    t = working_tts()
    return (t[1] if t[0] == "file" else "spd-say") if t else "none (no clips found, no working text-to-speech)"


def speak(kinds, text, say_cmd=None, voice="auto"):
    """Say it: your say_cmd, else the clips for `kinds` (voice auto), else a text-to-speech engine that works.
    Returns what it used, or None."""
    if voice == "off":
        return None
    if say_cmd:
        _run_quiet(say_cmd + [text], 30)
        return "say_cmd"
    player = find_player()
    clips = [clip_for(k) for k in kinds]
    if voice == "auto" and player and clips and all(clips):
        for c in clips:
            _run_quiet([player, c], 30)
        return "clips"
    t = working_tts()
    if t and t[0] == "file" and player:
        fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        try:
            _run_quiet([t[1], "-w", wav, text], 30)
            _run_quiet([player, wav], 30)
        finally:
            os.unlink(wav)
        return t[1]
    if t and t[0] == "spd-say":
        _run_quiet(["spd-say", "-w", text], 30)
        return "spd-say"
    return None


def _run_quiet(cmd, timeout):
    try:
        subprocess.run(cmd, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def message(kind, opts=None):
    return MESSAGES[kind].format(countdown=(opts or {}).get("countdown", DEFAULTS["countdown"]))


def alert(kind, mode="auto", say_cmd=None, volume_floor=DEFAULTS["volume_floor"], beeps=DEFAULTS["beeps"], text=None,
          voice="auto", spoken=None):
    """The alarm (loud: see Loud), a desktop notification where one is shown, and speech if a speech command
    exists. mode: auto | chime | speech | print. Never raises."""
    msg = text or message(kind)
    print(f"{time.strftime('%H:%M:%S')} {kind}: {msg}", flush=True)
    if mode == "print":
        return
    calm = kind in ("ok", "slow", "shutdown-cancel")
    try:
        with Loud(0 if calm else volume_floor):
            if mode in ("auto", "chime"):
                player = find_player()
                if player:
                    fd, wav = tempfile.mkstemp(suffix=".wav"); os.close(fd)
                    try:
                        chime_wav(wav, "ok" if kind in ("ok", "shutdown-cancel") else "warn", beeps=1 if calm else beeps)
                        _run_quiet([player, wav], 20)
                    finally:
                        os.unlink(wav)
                if shutil.which("notify-send"):
                    _run_quiet(["notify-send", "-a", "charge-watch", "-u", "normal" if calm else "critical",
                                "Steam Frame: " + TITLES.get(kind, kind), msg], 10)
            if mode in ("auto", "speech"):
                speak(spoken or [kind], msg, say_cmd, voice)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"charge-watch: alert failed: {e}", file=sys.stderr)


# ---------- push to a phone (opt-in: nothing is sent unless config.json names a service) ----------

def _post(url, data, headers, timeout=10):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "charge-watch", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def push(kind, text, cfg, soc=None):
    """Send one push to every service configured in cfg['push']. Returns {service: 'ok' | error}. Never raises.
      "ntfy": "https://ntfy.sh/<your-topic>"         (free, no account; pick a long random topic)
      "pushover_user": "...", "pushover_token": "..."
      "discord": "<webhook url>"
      "webhook": "<url>"                             (JSON POST: title, message, kind, soc, t)"""
    p, res = (cfg or {}).get("push") or {}, {}
    urgent = kind in URGENT
    title = "Steam Frame: " + TITLES.get(kind, kind)
    body = text + (f" (battery {soc} %)" if soc is not None else "")
    jobs = []
    if p.get("ntfy"):
        jobs.append(("ntfy", lambda: _post(p["ntfy"], body.encode(), {
            "Title": title, "Priority": "urgent" if urgent else "default",
            "Tags": "rotating_light,battery" if urgent else "battery"})))
    if p.get("pushover_user") and p.get("pushover_token"):
        jobs.append(("pushover", lambda: _post("https://api.pushover.net/1/messages.json", urllib.parse.urlencode({
            "token": p["pushover_token"], "user": p["pushover_user"], "title": title, "message": body,
            "priority": 1 if urgent else 0}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})))
    if p.get("discord"):
        jobs.append(("discord", lambda: _post(p["discord"], json.dumps({"content": f"**{title}**\n{body}"}).encode(),
                                              {"Content-Type": "application/json"})))
    if p.get("webhook"):
        jobs.append(("webhook", lambda: _post(p["webhook"], json.dumps({
            "title": title, "message": body, "kind": kind, "soc": soc, "t": time.time()}).encode(),
            {"Content-Type": "application/json"})))
    for name, job in jobs:
        try:
            st = job()
            res[name] = "ok" if 200 <= st < 300 else f"HTTP {st}"
        except Exception as e:  # noqa: BLE001 -- a push must never stop the watcher
            res[name] = f"{type(e).__name__}: {e}"
        if res[name] != "ok":
            print(f"charge-watch: push to {name} failed: {res[name]}", file=sys.stderr, flush=True)
    return res


def push_services(cfg):
    p = (cfg or {}).get("push") or {}
    return [n for n, ok in (("ntfy", p.get("ntfy")), ("pushover", p.get("pushover_user") and p.get("pushover_token")),
                            ("discord", p.get("discord")), ("webhook", p.get("webhook"))) if ok]


# ---------- powering off ----------

POWEROFF = (["pkexec", "/usr/bin/steamos-polkit-helpers/steamos-poweroff-now"],  # SteamOS: allowed for the user
            ["systemctl", "poweroff"])                                            # elsewhere: the active session


def _poweroff_candidates():
    own = os.environ.get("CHARGE_WATCH_POWEROFF")          # your own command, e.g. for a non-SteamOS device
    if own:
        return [shlex.split(own)]
    return [c for c in POWEROFF if shutil.which(c[0]) and (c[0] != "pkexec" or os.path.exists(c[1]))]


def poweroff_command():
    c = _poweroff_candidates()
    return c[0] if c else None


def power_off():
    """Try each power-off command; returns the one that worked, or None."""
    for c in _poweroff_candidates():
        try:
            if subprocess.run(c, timeout=60, capture_output=True).returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def load_config(path=None):
    path = path or CONFIG
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"charge-watch: ignoring {path}: {e}", file=sys.stderr)
        return {}


def log_event(ev):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "events.jsonl"), "a") as f:
            f.write(json.dumps(ev) + "\n")
    except OSError:
        pass


# ---------- journal ----------

def _journal_entry(raw):
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    return int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6, message_text(e.get("MESSAGE"))


def journal_readable():
    try:
        r = subprocess.run(["journalctl", "-t", DAEMON, "-n", "20", "--no-pager", "-q"],
                           capture_output=True, text=True, timeout=10)
        return any(k in r.stdout for k in ("Current State", "QPH(", "USB(", "FG INT"))
    except (OSError, subprocess.SubprocessError):
        return False


def load_saved(path):
    """(t, line) pairs from a saved journal: `journalctl -o json` lines, {"t":…,"line":…} lines, or plain text."""
    out = []
    with open(path, errors="replace") as f:
        for i, raw in enumerate(f):
            raw = raw.rstrip("\n")
            try:
                e = json.loads(raw)
            except ValueError:
                out.append((i * 0.1, raw)); continue   # plain text: no times, so no gap splitting
            if "MESSAGE" in e:
                out.append((int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6, message_text(e["MESSAGE"])))
            elif "line" in e:
                out.append((float(e.get("t", i)), e["line"]))
    keys = (HEADER, "QPH(", "QPH2 Entered", "USB(", "FG INT", "CHG(")
    return [x for x in out if any(k in x[1] for k in keys)]


class JournalFollower:
    """`journalctl -f` for the daemon, read without blocking: lines() returns what arrived within `timeout` s."""

    def __init__(self):
        self.p = subprocess.Popen(["journalctl", "-o", "json", "-t", DAEMON, "--no-pager", "-f", "-n", "12"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""

    def lines(self, timeout):
        out = []
        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        if r:
            chunk = os.read(self.p.stdout.fileno(), 65536)
            if not chunk:
                raise EOFError("journalctl ended")
            self.buf += chunk
            *done, self.buf = self.buf.split(b"\n")
            for raw in done:
                e = _journal_entry(raw.decode(errors="replace"))
                if e:
                    out.append(e)
        return out

    def close(self):
        try:
            self.p.kill()
        except OSError:
            pass


# ---------- report: a file to read, then hand to a helper or to Valve support (charge-watch report) ----------
# Deterministic: no network, no model. It reads charging-related files and log lines, scrubs anything that
# could identify the person or their network, asks a few questions, and writes one plain-text file.

REPORT_CLASSES = ("sys/class/power_supply", "sys/class/typec", "sys/class/usb_power_delivery")
REPORT_RUN = "run/deckardcharger"
REPORT_IIO = "sys/bus/iio/devices"
IIO_NAMES = re.compile(r"pm8550|charg|smb|usb|vbus|adc|bat|fg|max1720", re.I)
SKIP_NAME = re.compile(r"serial|uevent|^modalias$|^(power|subsystem|device|driver|of_node|firmware_node|hwmon\d*|wakeup\d+)$", re.I)
KERNEL_LINES = re.compile(r"tcpm|typec|ucsi|pdphy|usb.?pd|\bpd\b|vbus|charger|pm8550|pmic|battery|power.?supply|qph", re.I)


def _read_capped(path, cap=4096):
    try:
        with open(path, errors="replace") as f:
            return f.read(cap).strip()
    except OSError:
        return None


def scrubber():
    """A function that removes what could identify the person or their network from a line of text."""
    host = socket.gethostname() if hasattr(socket, "gethostname") else ""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    home = os.path.expanduser("~")
    pats = [(re.compile(r"\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b", re.I), "<mac>"),
            (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
            (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
            (re.compile(r"(serial(?:[ _-]?(?:number|no))?\s*[:=]\s*)\S+", re.I), r"\1<serial>")]

    def scrub(s):
        s = s or ""
        if home and home != "/":
            s = s.replace(home, "~")
        for word, tag in ((host, "<host>"), (user, "<user>")):
            if word and len(word) > 2:
                s = re.sub(r"\b%s\b" % re.escape(word), tag, s)
        for p, r in pats:
            s = p.sub(r, s)
        return s
    return scrub


def collect(root="/", scrub=None):
    """Every readable charging-related value: {relative path: text}. Serial numbers, uevent files and the
    usual sysfs back-links are skipped; values go through `scrub`."""
    scrub = scrub or scrubber()
    out = {}

    def walk(top, label, depth):
        try:
            names = sorted(os.listdir(top))
        except OSError:
            return
        for n in names:
            if SKIP_NAME.search(n):
                continue
            p = os.path.join(top, n)
            if os.path.islink(p) and depth > 0:
                continue                      # sysfs back-links (device, subsystem …) loop
            if os.path.isdir(p):
                if depth == 0 and label.endswith("power_supply") and \
                        (_read_capped(os.path.join(p, "scope")) or "").lower() == "device":
                    continue                  # a controller's or a Bluetooth device's battery: not the headset's
                if depth < 3:
                    walk(os.path.realpath(p) if depth == 0 else p, f"{label}/{n}", depth + 1)
            elif os.path.isfile(p) and os.access(p, os.R_OK):
                v = _read_capped(p)
                if v is not None:
                    out[scrub(f"{label}/{n}")] = scrub(v)

    for c in REPORT_CLASSES:
        walk(os.path.join(root, c), c, 0)
    walk(os.path.join(root, REPORT_RUN), REPORT_RUN, 1)
    iio = os.path.join(root, REPORT_IIO)
    for dev in sorted(os.listdir(iio)) if os.path.isdir(iio) else []:
        d = os.path.join(iio, dev)
        name = _read_capped(os.path.join(d, "name")) or ""
        if not IIO_NAMES.search(name):
            continue
        for f in sorted(os.listdir(d)):
            if f.startswith("in_") and f.endswith(("_input", "_raw", "_scale")) or f == "name":
                v = _read_capped(os.path.join(d, f))
                if v is not None:
                    out[scrub(f"{REPORT_IIO}/{dev}/{f}")] = scrub(v)
    return out


def facts(snap):
    """What a snapshot says, in the few terms the diagnosis needs."""
    f = {"soc": None, "bat_status": None, "bat_cur": None, "charge_mah": None, "usb_online": False,
         "partner": False, "contract_v": None, "pdos": [], "detail": {}, "vbus_v": None}
    gauges = sorted(k.rsplit("/", 1)[0] for k in snap if k.startswith("sys/class/power_supply/") and
                    k.endswith("/type") and snap[k] == "Battery" and "charger" not in k)
    gauges.sort(key=lambda g: ("max1720x" not in g, g))
    if gauges:
        g = gauges[0]
        f["soc"] = _num(snap.get(g + "/capacity"))
        f["soc"] = int(f["soc"]) if f["soc"] is not None else None
        f["bat_status"] = snap.get(g + "/status")
        f["bat_cur"] = _num(snap.get(g + "/current_now"), 1e6)
        f["charge_mah"] = _num(snap.get(g + "/charge_now"), 1e3)
    for k, v in snap.items():
        if not k.startswith("sys/class/power_supply/"):
            continue
        sup = k.rsplit("/", 1)[0]
        typ = snap.get(sup + "/type")
        if k.endswith("/online") and v == "1" and (typ in ("USB", "USB_C", "USB_PD", "Mains") or "tcpm-source" in sup):
            f["usb_online"] = True
        if k.endswith("/status_detailed"):
            f["detail"] = parse_detail(v)
    f["partner"] = any(re.match(r"sys/class/typec/port\d+-partner/", k) for k in snap) or \
        any(re.match(r"sys/class/typec/port\d+/port\d+-partner/", k) for k in snap)
    m = re.search(r"Voltage=(\d+)mV", snap.get(REPORT_RUN + "/typec_contract", ""))
    if m:
        f["contract_v"] = int(m.group(1)) / 1000
    if snap.get(REPORT_RUN + "/ac_status") == "Connected":
        f["usb_online"] = True
    caps = {}
    for k, v in snap.items():
        m = re.match(r"sys/class/usb_power_delivery/(pd\d+)/source-capabilities/([^/]+)/(voltage|maximum_voltage|"
                     r"maximum_current)$", k)
        if m:
            caps.setdefault((m.group(1), m.group(2)), {})[m.group(3)] = _num(re.sub(r"[^\d]", "", v))
    for (_, pdo), c in sorted(caps.items(), key=lambda x: x[0][1]):
        volt = c.get("voltage") or c.get("maximum_voltage")
        if volt:
            f["pdos"].append((round(volt / 1000, 1), round((c.get("maximum_current") or 0) / 1000, 2)))
    f["pdos"] = sorted(set(f["pdos"]))
    for k, v in snap.items():
        if k.startswith(REPORT_IIO) and re.search(r"(usb.?in|vbus).*(volt|_v)|in_voltage.*(usb|vbus)", k, re.I) \
                and k.endswith("_input"):
            n = _num(v)
            if n is not None:
                f["vbus_v"] = round(n / 1e6 if n > 1e5 else n / 1e3 if n > 100 else n, 2)
    return f


def seen(f):
    """Does the headset see a power source at all?"""
    return bool(f["usb_online"] or f["partner"] or f["contract_v"] or (f["vbus_v"] or 0) > 4.0)


def short(f):
    pd = (f"{f['contract_v']:.0f} V agreed" if f["contract_v"] else "no PD contract")
    cur = f"{f['bat_cur']:+.2f} A" if f["bat_cur"] is not None else "? A"
    return (f"{'sees power' if seen(f) else 'sees NO power'} (USB online {'yes' if f['usb_online'] else 'no'}, "
            f"USB-C partner {'yes' if f['partner'] else 'no'}, {pd}"
            + (f", VBUS {f['vbus_v']} V" if f["vbus_v"] is not None else "") + f"), battery {cur}"
            + (f", {f['soc']} %" if f["soc"] is not None else ""))


def boot_sleep_total():
    """Seconds this boot has spent suspended (CLOCK_BOOTTIME keeps counting in suspend, MONOTONIC stops)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return 0.0


FINDINGS = {
    "not-detected-but-charges-asleep": (
        "The headset doesn't see the charger while it's on, but it charges while asleep",
        "While the headset is awake, nothing on its USB-C side reacts to the charger: no USB power, no USB-C "
        "partner, no power agreement. Asleep, the battery went up. So the charger and cable work, and the "
        "headset's USB-C power detection fails only while the system runs. Charge it asleep or off for now. "
        "charge-watch's auto-shutdown (\"shutdown_at\" in its settings) turns it off before it runs flat. "
        "Check the battery % goes up after: #2879 has units that drained while powered off on a charger."),
    "not-detected": (
        "The headset doesn't see the charger at all",
        "With the charger plugged in, nothing on the headset's USB-C side reacts: no USB power, no USB-C "
        "partner, no power agreement. Try another cable and another charger. If none is ever seen, "
        "it's the headset, not the charger. Run the sleep test too: some units charge only while asleep."),
    "qph2-stuck": (
        "The charger chip is stuck (the QPH2 bug, SteamOS issue #2879)",
        "The headset agreed power with the charger, but its charger chip went into its QPH2 mode and stopped "
        "taking input. Reseat the cable or switch chargers. #2879 has a workaround (a root change: read the "
        "thread first). Don't let it reach 0 %, and don't leave it powered off on that charger: units in "
        "#2879 drained that way and never came back. Unplug it."),
    "input-refused": (
        "The headset sees the charger but refuses its power",
        "The charger chip reports no usable input, without the QPH2 state. #2879 reports this with some power "
        "banks. Try a wall charger and another cable."),
    "pd-not-negotiated": (
        "The charger offers more than 5 V, but the headset stays at 5 V",
        "The charger advertises higher voltages, but the headset never agreed to one. It charges slowly at "
        "best, and drains while in use. Try another cable: a cable without USB-C PD support causes exactly "
        "this."),
    "5v-only": (
        "Only 5 V: a slow charger or cable",
        "The headset gets 5 V and no USB-C power agreement. That charges slowly when idle and can't keep up in "
        "use. Use a USB-C PD charger (9 V or more) and a PD cable."),
    "cant-keep-up": (
        "Power comes in, but the battery still drains",
        "Current is flowing in, but the battery gauge shows the battery losing charge. The charger is too weak "
        "for the load. Use a stronger PD charger."),
    "charging": (
        "Charging normally",
        "With this charger and cable, the battery is charging. If it fails at other times, run the report again "
        "then, while it's happening."),
    "unplugged": (
        "No charger plugged in",
        "Run `charge-watch report` with the charger you want to test plugged in, and follow the steps."),
    "unclear": (
        "No known pattern matched",
        "Nothing here matches a pattern charge-watch knows. Please share the report: it's exactly what's "
        "needed to learn a new one."),
}


def diagnose(steps, user_plugged=None, sleep=None):
    """steps: [(label, facts)], the last one being 'now, plugged in' for a guided run. Returns a pattern id."""
    allf = [f for _, f in steps]
    last = allf[-1]
    plugged_now = user_plugged if user_plugged is not None else seen(last)
    if not plugged_now:
        return "unplugged"
    if any(qph2_stuck(f["detail"]) for f in allf):
        return "qph2-stuck"
    replug = [f for label, f in steps if label.startswith("plugged")] or [last]
    if user_plugged and not any(seen(f) for f in replug):
        if sleep and sleep.get("charged"):
            return "not-detected-but-charges-asleep"
        return "not-detected"
    if any(f["detail"].get("Input OK") == 0 for f in replug):
        return "input-refused"
    full = last["bat_status"] == "Full" or (last["soc"] or 0) >= 95 and last["bat_status"] == "Not charging"
    if (last["bat_cur"] or 0) > 0.05 or last["bat_status"] == "Charging" or full:
        if (last["contract_v"] or 0) <= 5.5 and any(v > 5.5 for v, _ in last["pdos"]):
            return "pd-not-negotiated"
        return "charging"
    if last["contract_v"] is not None and last["contract_v"] <= 5.5:
        return "pd-not-negotiated" if any(v > 5.5 for v, _ in last["pdos"]) else "5v-only"
    if (last["bat_cur"] or 0) < -0.1 and seen(last):
        return "cant-keep-up"
    return "unclear"


def os_info(root="/"):
    rel = {}
    for line in (_read_capped(os.path.join(root, "etc/os-release"), 8192) or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            rel[k] = v.strip().strip('"')
    return {"name": rel.get("PRETTY_NAME") or rel.get("NAME"), "version": rel.get("VERSION_ID"),
            "build": rel.get("BUILD_ID"), "kernel": os.uname().release if hasattr(os, "uname") else None}


def journal_lines(args, pattern=None, n=150, scrub=None):
    """`journalctl -o json` lines as 'HH:MM:SS message' (no hostname field), filtered, scrubbed, last n."""
    scrub = scrub or scrubber()
    try:
        r = subprocess.run(["journalctl", "--no-pager", "-q", "-o", "json"] + args, capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ["(journal not readable)"]
    out = []
    for raw in r.stdout.splitlines():
        e = _journal_entry(raw)
        if not e:
            continue
        t, msg = e
        if pattern and not pattern.search(msg):
            continue
        out.append(time.strftime("%m-%d %H:%M:%S", time.gmtime(t)) + "Z " + scrub(msg))
    return out[-n:] or ["(nothing)"]


def valve_text(pattern, info, f0, steps, sleep, charger):
    pd = ", ".join(f"{v:g} V {a:g} A" for v, a in f0["pdos"]) if f0["pdos"] else None
    lines = [f"Steam Frame, {info.get('name') or 'SteamOS'} build {info.get('build') or '?'}."]
    lines.append(f"Charger and cable: {charger or '<fill in: charger model and wattage, cable>'}"
                 + (f" (the charger advertises {pd})." if pd else "."))
    obs = {
        "not-detected-but-charges-asleep": "While the headset is on, it does not detect the charger at all: no "
                                           "USB power, no USB-C partner, no PD contract. It does charge while "
                                           "asleep or off.",
        "not-detected": "With the charger plugged in, the headset does not detect it at all: no USB power, no "
                        "USB-C partner, no PD contract.",
        "qph2-stuck": "The charger chip enters QPH2 and stops taking input (status_detailed: Input OK 0 with IMP Trigger 1), "
                      "so the battery drains while plugged in. This matches SteamOS issue #2879.",
        "input-refused": "The headset sees the charger, but the charger chip reports no usable input "
                         "(status_detailed: Input OK 0).",
        "pd-not-negotiated": "The charger offers more than 5 V, but the headset never negotiates above 5 V.",
        "5v-only": "The headset only ever gets 5 V and no PD contract.",
        "cant-keep-up": "Current flows in, but the battery still discharges while plugged in.",
    }.get(pattern)
    if obs:
        lines.append(obs)
    if sleep and sleep.get("minutes"):
        lines.append(f"Sleep test: {sleep['summary']}.")
    lines.append("Recorded with charge-watch's guided test (github.com/frame-commons/charge-watch); the full report "
                 "is attached.")
    return " ".join(lines)


def _wait_enter(ask, prompt):
    try:
        ask(prompt)
        return True
    except EOFError:
        return False


def run_report(root="/", ask=input, say=print, guided=True, sleep_test=None, charger=None, out_dir=None,
               watch_s=30, sleep_wait_s=3600, awake_s=300, poll=2.0, clock=time.monotonic, slept=boot_sleep_total):
    """The guided report. `ask`/`say` are the console (tests pass their own). Returns the report's path."""
    scrub = scrubber()
    say("charge-watch report: " + ("a few questions, then " if guided else "") +
        "one text file you can read before you share it.")
    say("It reads charging-related files and log lines on this headset. It sends nothing anywhere.\n")
    t_start = time.time()
    steps, timeline, user_plugged, sleep = [], [], None, None
    snap0 = collect(root, scrub)
    steps.append(("as found", facts(snap0)))
    snaps = {"as found": snap0}
    if guided:
        a = ""
        try:
            a = ask("Is a charger plugged into the headset right now? [y/n] ").strip().lower()
        except EOFError:
            guided = False
        if guided:
            if not charger:
                try:
                    charger = ask("Which charger and cable? (e.g. 'Valve 45 W, its own cable'; Enter to skip) ").strip() or None
                except EOFError:
                    pass
            had = not a.startswith("n")
            if had:
                ok = _wait_enter(ask, "Unplug the charger from the headset now, then press Enter. ")
                if ok:
                    time.sleep(min(5, poll * 2))
                    s = collect(root, scrub); snaps["unplugged"] = s; steps.append(("unplugged", facts(s)))
                    say("  unplugged: " + short(steps[-1][1]))
            else:
                ok = True
            if ok:
                if _wait_enter(ask, ("Plug it back in now" if had else "Plug the charger into the headset now") +
                               ", then press Enter. "):
                    user_plugged = True
                    say(f"  watching for {watch_s} s …")
                    t0 = clock()
                    while True:
                        s = collect(root, scrub); f = facts(s)
                        timeline.append((round(clock() - t0), f))
                        if clock() - t0 >= watch_s:
                            break
                        time.sleep(poll)
                    snaps["plugged in"] = s; steps.append(("plugged in", f))
                    say("  plugged in: " + short(f))
            elif a.startswith("y"):
                user_plugged = True
            if sleep_test is None:
                try:
                    sleep_test = ask("Optional sleep test, 20 minutes or more: does it charge while asleep? "
                                     "[y/N] ").strip().lower().startswith("y")
                except EOFError:
                    sleep_test = False
            if sleep_test:
                sleep = run_sleep_test(root, ask, say, scrub, sleep_wait_s, awake_s, poll, slept)
    pattern = diagnose(steps, user_plugged, sleep)
    title, explain = FINDINGS[pattern]
    info = os_info(root)
    f0 = steps[-1][1]
    text = [f"charge-watch report, {time.strftime('%Y-%m-%d %H:%M', time.gmtime(t_start))} UTC",
            "",
            "READ THIS BEFORE YOU SHARE IT. It holds charging values from this headset's system files, the charge",
            "daemon's and the kernel's USB-C/charger log lines from this boot, and your answers. Serial numbers,",
            "the hostname, user name, home folder, MAC and IP addresses are removed. Nothing was sent anywhere.",
            "", "== Result ==", title, "", textwrap.fill(explain, 110), "",
            "== For Valve support (copy and paste; one line on purpose) ==",
            valve_text(pattern, info, f0, steps, sleep, charger), "",
            "== What it saw =="]
    text += [f"{label}: {short(f)}" for label, f in steps]
    if timeline:
        text += ["", "After plugging back in (seconds: what it saw):"]
        text += [f"  +{t:>3} s  {short(f)}" for t, f in timeline]
    if sleep:
        text += ["", "== Sleep test ==", sleep["summary"]]
    text += ["", "== System ==", f"{info.get('name')}, version {info.get('version')}, build {info.get('build')}, "
                                  f"kernel {info.get('kernel')}", f"user answer, charger plugged in: "
                                                                 f"{'yes' if user_plugged else 'no/unknown'}",
             f"charger and cable (user): {charger or '-'}", ""]
    text += ["== Charge daemon log, this boot (last 150 lines) =="]
    text += journal_lines(["-b", "-t", DAEMON], n=150, scrub=scrub)
    text += ["", "== Kernel log, USB-C and charger lines, this boot (last 150) =="]
    text += journal_lines(["-k", "-b"], KERNEL_LINES, n=150, scrub=scrub)
    ev = []
    try:
        with open(os.path.join(STATE_DIR, "events.jsonl")) as fh:
            ev = fh.read().splitlines()[-50:]
    except OSError:
        pass
    text += ["", "== charge-watch's own events (last 50) =="] + ([scrub(e) for e in ev] or ["(none)"])
    text += ["", "== Values: as found =="] + [f"{k} = {v}" for k, v in sorted(snap0.items())]
    prev = snap0
    for label in ("unplugged", "plugged in"):
        if label in snaps:
            diff = {k: v for k, v in snaps[label].items() if prev.get(k) != v}
            gone = [k for k in prev if k not in snaps[label]]
            text += ["", f"== Values: changed when {label} =="] + [f"{k} = {v}" for k, v in sorted(diff.items())] \
                + [f"{k} (gone)" for k in sorted(gone)] + ([] if diff or gone else ["(nothing changed)"])
            prev = snaps[label]
    summary = {"v": 1, "pattern": pattern, "build": info.get("build"), "version": info.get("version"),
               "charger": charger, "pdos": f0["pdos"], "guided": bool(guided and user_plugged is not None),
               "sleep": {k: sleep[k] for k in ("minutes", "soc_delta", "mah_delta", "charged") if k in sleep}
               if sleep else None}
    text += ["", "== Summary (one line, for the reports table) ==", "charge-watch-summary " + json.dumps(summary)]
    out_dir = out_dir or os.path.expanduser("~")
    path = os.path.join(out_dir, time.strftime("charge-watch-report-%Y%m%dT%H%MZ.txt", time.gmtime(t_start)))
    with open(path, "w") as fh:
        fh.write("\n".join(text) + "\n")
    say(f"\nResult: {title}")
    say(f"\nWrote {path}")
    say("Read it first. To share it: open https://github.com/frame-commons/charge-watch/issues/new?template="
        "charging-report.yml and drag the file in, or attach it to your Valve support ticket.")
    return path


def run_sleep_test(root, ask, say, scrub, wait_s, awake_s, poll, slept):
    """Charge gained across one suspend, then over `awake_s` awake, both on the charger."""
    a = facts(collect(root, scrub)); s0 = slept()
    if not _wait_enter(ask, "Keep the charger plugged in. Press Enter, then put the headset to sleep (power button). "
                            "Wake it after 20 minutes or more; the test carries on by itself. "):
        return None
    t0 = time.monotonic()
    while slept() - s0 < 60 and time.monotonic() - t0 < wait_s:
        time.sleep(poll)
    asleep = slept() - s0
    if asleep < 60:
        return {"summary": "No sleep was detected, so there is no sleep result."}
    time.sleep(min(10, poll * 5))
    b = facts(collect(root, scrub))
    say(f"  awake again after {asleep / 60:.0f} min asleep; now {awake_s // 60} min awake on the charger …")
    time.sleep(awake_s)
    c = facts(collect(root, scrub))
    res = {"minutes": round(asleep / 60), "soc_delta": None, "mah_delta": None}
    if a["soc"] is not None and b["soc"] is not None:
        res["soc_delta"] = b["soc"] - a["soc"]
    if a["charge_mah"] is not None and b["charge_mah"] is not None:
        res["mah_delta"] = round(b["charge_mah"] - a["charge_mah"])
    res["charged"] = (res["mah_delta"] or 0) > 20 or (res["soc_delta"] or 0) >= 1
    awake = ""
    if b["soc"] is not None and c["soc"] is not None:
        awake = f"; then {c['soc'] - b['soc']:+d} % in {awake_s // 60} min awake"
    if b["charge_mah"] is not None and c["charge_mah"] is not None:
        awake += f" ({c['charge_mah'] - b['charge_mah']:+.0f} mAh)"
    gain = f"{res['soc_delta']:+d} %" if res["soc_delta"] is not None else "?"
    if res["mah_delta"] is not None:
        gain += f" ({res['mah_delta']:+d} mAh)"
    res["summary"] = f"{gain} in {res['minutes']} min asleep on the charger{awake}"
    return res


def summary_line(path):
    """The reports-table entry for a report file (for maintainers: data/reports.json)."""
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("charge-watch-summary "):
                return json.loads(line.split(" ", 1)[1])
    raise ValueError("no charge-watch-summary line in " + path)


class SleepNote:
    """While `run` watches: notices that the headset was asleep, and what the battery did meanwhile."""
    def __init__(self, slept=boot_sleep_total):
        self.slept, self.last, self.prev = slept, slept(), None

    def step(self, s):
        now, ev = self.slept(), None
        if now - self.last >= 60 and self.prev is not None:
            ev = {"t": time.time(), "event": "slept", "minutes": round((now - self.last) / 60),
                  "soc_before": self.prev.get("soc"), "soc_after": s.get("soc"),
                  "plugged_before": bool(plugged(self.prev)), "plugged_after": bool(plugged(s))}
        self.last, self.prev = now, s
        return ev


# ---------- commands ----------

SETTINGS = {"repeat": int, "repeat_low": int, "volume_floor": float, "beeps": int, "push_repeat": int,
            "shutdown_at": int, "countdown": int, "shutdown_hold": int, "alert": str, "say_cmd": str, "voice": str}
ALERT_MODES = ("auto", "chime", "speech", "print")


def _warn(msg):
    print("charge-watch: " + msg, file=sys.stderr, flush=True)


def settings(a, cfg):
    """Each setting from the command line, else config.json, else the default. A bad value in config.json is
    reported and replaced by the default: a typo must never stop the watcher at the moment it is needed."""
    o = {}
    for k, typ in SETTINGS.items():
        v = getattr(a, k, None)
        if v is None:
            v = cfg.get(k)
        if v is None or v == "":
            o[k] = DEFAULTS.get(k)
            continue
        try:
            if typ is int and isinstance(v, float) and v != int(v):
                raise ValueError
            v = typ(v) if not isinstance(v, bool) else None
            if v is None or (typ in (int, float) and v < 0):
                raise ValueError
            o[k] = v
        except (TypeError, ValueError):
            _warn(f"ignoring {k}={v!r} in the settings (want a {typ.__name__}); using {DEFAULTS.get(k)!r}")
            o[k] = DEFAULTS.get(k)
    if o["alert"] not in ALERT_MODES:
        if o["alert"] is not None:
            _warn(f"ignoring alert={o['alert']!r}; using auto")
        o["alert"] = "auto"
    if o["voice"] not in VOICE_MODES:
        _warn(f"ignoring voice={o['voice']!r} (want auto, tts or off); using auto")
        o["voice"] = "auto"
    o["volume_floor"] = min(float(o["volume_floor"]), 1.0)
    o["beeps"] = max(1, min(o["beeps"], 10))
    o["repeat"], o["repeat_low"] = max(10, o["repeat"]), max(10, o["repeat_low"])
    if o["shutdown_at"] == 0:
        o["shutdown_at"] = None          # 0 = off
    if o["shutdown_at"] is not None and not 1 <= o["shutdown_at"] <= 30:
        _warn(f"shutdown_at={o['shutdown_at']} is outside 1-30 %; auto-shutdown is OFF")
        o["shutdown_at"] = None
    return o


def describe_settings(o, cfg):
    sv = push_services(cfg)
    return (f"repeat {o['repeat']} s ({o['repeat_low']} s at {DEFAULTS['low']} % or below), "
            f"volume floor {o['volume_floor']}, beeps {o['beeps']}, "
            f"auto-shutdown {'at ' + str(o['shutdown_at']) + ' %' if o['shutdown_at'] else 'off'}, "
            f"push {', '.join(sv) if sv else 'off'}, speech: {speech_path(o['voice'], _say(o))}")


def watchers(a, o):
    return (Rules(drop=a.drop, window=a.window, hold=a.hold), Announcer(o["repeat"], o["repeat_low"]),
            ShutdownGuard(o["shutdown_at"], hold=o["shutdown_hold"], countdown=o["countdown"]))


def cmd_replay(a):
    o = settings(a, {})
    rules, ann, guard = watchers(a, o)
    alerts, n, last = [], 0, None
    for s in samples_from_lines(load_saved(a.file)):
        n += 1; last = s
        act = rules.step(s)
        for k in ann.step(s["t"], act, plugged(s), charging_now(s), s.get("soc")) + guard.step(s):
            alerts.append({"t": s["t"], "alert": k, "sample": s})
        if guard.done:
            break                # the headset would have powered off here
    if a.json:
        print(json.dumps({"samples": n, "alerts": alerts, "last": last}))
    else:
        print(f"{n} status blocks read" + (f", last: {describe(last)}" if last else ""))
        for e in alerts:
            print(f"  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['t']))}  {e['alert']:<12}  {describe(e['sample'])}")
        if not alerts:
            print("  no alerts")
    return 0


def describe(s):
    parts = []
    if "qph" in s:
        parts.append(f"QPH{s['qph']}")
    if "usb_v" in s:
        parts.append(f"USB {s['usb_v']:.1f} V {s.get('usb_cur', 0):.2f} A (offered {s.get('usb_neg_v', 0):.0f} V {s.get('usb_neg_a', 0):.1f} A)")
    elif "usb_online" in s:
        parts.append("plugged in" if s["usb_online"] else "unplugged")
        if s.get("usb_neg_v"):
            parts.append(f"{s['usb_neg_v']:.1f} V offered")
    if s.get("soc") is not None:
        parts.append(f"{s['soc']} %")
    cur = battery_current(s)
    if cur is not None:
        parts.append(f"battery {cur:+.2f} A")
    if s.get("bat_status") or s.get("chg_stat"):
        parts.append((s.get("bat_status") or s.get("chg_stat")).lower())
    return ", ".join(parts)


def latest_daemon_sample():
    if not journal_readable():
        return None
    try:
        r = subprocess.run(["journalctl", "-o", "json", "-t", DAEMON, "-n", "20", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    pairs = [e for e in (_journal_entry(x) for x in r.stdout.splitlines()) if e]
    got = [s for s in samples_from_lines(pairs) if "usb_online" in s]
    return got[-1] if got else None


STALE = 120  # s: a daemon block older than this is not "now"


def cmd_status(a):
    cfg = load_config(a.config)
    o = settings(a, cfg)
    sysfs = read_sysfs(a.sysfs)
    s, note = latest_daemon_sample(), None
    if s and time.time() - s["t"] > STALE:
        note = f"the charge daemon last logged {int(time.time() - s['t'])} s ago; using sysfs"
        s = None
    s = merge_sysfs(s, sysfs) if s else sysfs
    p = plugged(s)
    act = Rules(hold=0, drain_hold=0, slow_hold=0).step(s) if p else []
    r = {"source": s.get("src"), "sample": s, "plugged": bool(p), "verdict": act or ["ok"],
         "messages": [MESSAGES[k] for k in act], "note": note,
         "settings": {**{k: o[k] for k in SETTINGS}, "push": push_services(cfg), "speech": speech_path(o["voice"], _say(o))}}
    if a.json:
        print(json.dumps(r))
    else:
        print(f"{describe(s)}  [{s.get('src')}]" + (f"  ({note})" if note else ""))
        print(("verdict: " + ", ".join(r["verdict"])) if p else "verdict: not plugged in")
        for m in r["messages"]:
            print("  " + m)
        print("settings: " + describe_settings(o, cfg))
    return 1 if set(act) - {"slow"} else 0


def _say(o):
    return o["say_cmd"].split() if o["say_cmd"] else None


def cmd_test_alert(a):
    cfg = load_config(a.config)
    o = settings(a, cfg)
    alert("no-input", o["alert"], _say(o), o["volume_floor"], o["beeps"],
          text="This is a charge-watch test. " + MESSAGES["no-input"], voice=o["voice"], spoken=["test", "no-input"])
    if a.push:
        if not push_services(cfg):
            print(f"charge-watch: no push service set up in {a.config or CONFIG}", file=sys.stderr)
            return 1
        res = push("test", "Test from charge-watch: if you can read this, push works.", cfg)
        print("push: " + ", ".join(f"{k} {v}" for k, v in res.items()))
        return 0 if all(v == "ok" for v in res.values()) else 1
    return 0


def cmd_test_shutdown(a):
    cfg = load_config(a.config)
    o = settings(a, cfg)
    alert("shutdown-warn", o["alert"], _say(o), o["volume_floor"], o["beeps"], text=message("shutdown-warn", o),
          voice=o["voice"])
    alert("poweroff", o["alert"], _say(o), o["volume_floor"], 1, voice=o["voice"])
    c = poweroff_command()
    print("test only, nothing powered off. It would run: " + (" ".join(c) if c else "(no power-off command found)"))
    print("auto-shutdown is " + (f"ON at {o['shutdown_at']} %" if o["shutdown_at"] else
                                 'OFF (turn it on with "shutdown_at": 5 in ' + (a.config or CONFIG) + ")"))
    return 0 if c else 1


DAEMON_SILENT = 90  # s without a finished daemon block -> poll sysfs until it comes back


def cmd_run(a):
    cfg = load_config(a.config)
    o = settings(a, cfg)
    rules, ann, guard = watchers(a, o)
    gate = PushGate(o["push_repeat"])
    use_daemon = not a.sysfs_only and journal_readable()
    print(f"charge-watch: watching via {'the charge daemon journal' if use_daemon else 'sysfs every %ds' % a.every}"
          f"; alerts: {o['alert']}; {describe_settings(o, cfg)}", flush=True)
    log_event({"t": time.time(), "event": "start", "source": "daemon" if use_daemon else "sysfs",
               "shutdown_at": o["shutdown_at"], "push": push_services(cfg)})

    def fire(k, s):
        text = message(k, o)
        log_event({"t": time.time(), "event": "alert", "alert": k, "sample": s})
        th = None
        if push_services(cfg) and gate.allow(s["t"], k):
            th = threading.Thread(target=push, args=(k, text, cfg, s.get("soc")), daemon=True)
            th.start()
        alert(k, o["alert"], _say(o), o["volume_floor"], 1 if k == "poweroff" else o["beeps"], text=text, voice=o["voice"])
        return th

    naps = SleepNote()

    def handle(s):
        nap = naps.step(s)
        if nap:
            log_event(nap)
            print(f"charge-watch: was asleep {nap['minutes']} min; battery {nap['soc_before']} -> {nap['soc_after']} %"
                  f"{' (on the charger)' if nap['plugged_before'] else ''}", flush=True)
        act = rules.step(s)
        for k in ann.step(s["t"], act, plugged(s), charging_now(s), s.get("soc")) + guard.step(s, time.monotonic()):
            th = fire(k, s)
            if k == "poweroff":
                if th:
                    th.join(15)          # let the phone hear about it before the network goes
                c = power_off()
                log_event({"t": time.time(), "event": "poweroff", "command": c and " ".join(c)})
                if not c:
                    _warn("could not power off (no command worked); will warn and retry")
                    guard.failed(time.monotonic())
                    fire("poweroff-failed", s)

    if not use_daemon:
        while True:
            handle(read_sysfs(a.sysfs))
            time.sleep(a.every)
    jf, bp = JournalFollower(), BlockParser()
    last_block, last_poll, silent = time.time(), 0.0, False
    try:
        while True:
            done = []
            for t, line in jf.lines(timeout=5):
                s = bp.feed(t, line)
                if s:
                    done.append(s)
            s = bp.stale(time.time())
            if s:
                done.append(s)
            for s in done:
                if "usb_online" in s:
                    last_block = time.time()
                    if silent:
                        print("charge-watch: the charge daemon is logging again", flush=True); silent = False
                handle(merge_sysfs(s, read_sysfs(a.sysfs)))
            if time.time() - last_block > DAEMON_SILENT and time.time() - last_poll >= a.every:
                if not silent:
                    print("charge-watch: the charge daemon went quiet; polling sysfs", flush=True); silent = True
                last_poll = time.time()
                handle(read_sysfs(a.sysfs))
    finally:
        jf.close()


def cmd_report(a):
    run_report(root=a.root, guided=not a.quick, sleep_test=True if a.sleep_test else (False if a.quick else None),
               charger=a.charger, out_dir=a.out)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="charge-watch", description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 2)[2])
    sub = p.add_subparsers(dest="cmd", required=True)

    def rule_args(sp):
        sp.add_argument("--drop", type=int, help=f"battery points lost while plugged in (default {DEFAULTS['drop']})")
        sp.add_argument("--window", type=int, help=f"seconds the drop is measured over (default {DEFAULTS['window']})")
        sp.add_argument("--hold", type=int, help=f"seconds a stuck/no-input state must last (default {DEFAULTS['hold']})")
        sp.add_argument("--repeat", type=int, help=f"seconds between repeated warnings (default {DEFAULTS['repeat']})")
        sp.add_argument("--repeat-low", type=int, help=f"the same at {DEFAULTS['low']} %% or below "
                                                       f"(default {DEFAULTS['repeat_low']})")
        sp.add_argument("--shutdown-at", type=int, metavar="PCT",
                        help="power off at this battery %% or below when not charging (default off; 5 is a reasonable start)")
        sp.add_argument("--countdown", type=int, help=f"seconds of warning before that power-off "
                                                      f"(default {DEFAULTS['countdown']})")
        sp.add_argument("--shutdown-hold", type=int, help=f"seconds low and not charging before the warning "
                                                          f"(default {DEFAULTS['shutdown_hold']})")

    def alert_args(sp):
        sp.add_argument("--alert", choices=["auto", "chime", "speech", "print"])
        sp.add_argument("--say-cmd", help="your own speech command, given the text as its last argument (e.g. a Piper wrapper)")
        sp.add_argument("--voice", choices=VOICE_MODES, help="auto: the voice clips, else a text-to-speech engine that "
                                                              "works (default); tts: skip the clips; off: no speech")
        sp.add_argument("--volume-floor", type=float, metavar="0-1",
                        help=f"raise the volume to at least this for an alarm (default {DEFAULTS['volume_floor']}; 0 = never)")
        sp.add_argument("--beeps", type=int, help=f"alarm repeats per warning (default {DEFAULTS['beeps']})")
        sp.add_argument("--config", help=f"settings file (default {CONFIG})")

    r = sub.add_parser("run"); rule_args(r); alert_args(r)
    r.add_argument("--sysfs-only", action="store_true"); r.add_argument("--every", type=int, default=20)
    r.add_argument("--sysfs", default="/sys/class/power_supply")
    r.add_argument("--push-repeat", type=int, help=f"seconds between repeated pushes (default {DEFAULTS['push_repeat']})")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.add_argument("--sysfs", default="/sys/class/power_supply")
    s.add_argument("--config", help=f"settings file (default {CONFIG})")
    rp = sub.add_parser("replay"); rp.add_argument("file"); rp.add_argument("--json", action="store_true"); rule_args(rp)
    ta = sub.add_parser("test-alert"); alert_args(ta)
    ta.add_argument("--push", action="store_true", help="also send a test push to the services in the config")
    ts = sub.add_parser("test-shutdown"); alert_args(ts)
    ts.add_argument("--shutdown-at", type=int, help=argparse.SUPPRESS); ts.add_argument("--countdown", type=int, help=argparse.SUPPRESS)
    rep = sub.add_parser("report", help="a guided check that writes one text file to read, then share if you want",
                         description="Writes a plain-text charging report: what the headset sees as you unplug and plug "
                                     "the charger, optionally whether it charges while asleep, a result in plain "
                                     "words, and a paragraph for Valve support. Nothing is sent anywhere.")
    rep.add_argument("--quick", action="store_true", help="no questions: just record what it sees now")
    rep.add_argument("--sleep-test", action="store_true", help="include the sleep test without asking")
    rep.add_argument("--charger", help="the charger and cable, e.g. 'Anker 65 W, Valve cable'")
    rep.add_argument("--out", help="folder for the report (default: your home folder)")
    rep.add_argument("--root", default="/", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    return {"run": cmd_run, "report": cmd_report, "status": cmd_status, "replay": cmd_replay, "test-alert": cmd_test_alert,
            "test-shutdown": cmd_test_shutdown}[a.cmd](a)


def _term(signum, frame):
    raise SystemExit(128 + signum)   # unwinds through Loud.__exit__, so the volume is put back


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
