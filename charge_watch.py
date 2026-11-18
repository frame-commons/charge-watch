#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""charge-watch: warn when a Steam Frame is plugged in but not actually charging.

  charge-watch run [--alert ...]        watch and warn out loud (the systemd user unit runs this)
  charge-watch status [--json]          one reading: what the charger and battery say, and the verdict
  charge-watch replay FILE [--json]     run the rules over a saved journal (journalctl -o json) and list alerts
  charge-watch test-alert [--push]      play the warning once (and send a test push), to check it reaches you
  charge-watch test-shutdown            play the low-battery shutdown warning; shows the power-off command, runs nothing

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
  stuck         charger in QPH2 with no input (Input OK 0 + QPH2 1, or QPH(2) with < 0.10 A in): the #2879 bug
  no-input      plugged in, but under 0.05 A comes in, or Input OK 0 without QPH2 (the power-bank variant)
  cant-keep-up  the gauge shows the battery losing charge while real current comes in: charger too weak
  discharge     the gauge shows the battery losing charge while plugged in, cause unknown
  draining      backstop: the battery % fell by >= --drop points within --window while plugged in
  slow          5 V only (said once per plug-in; it may still charge, slowly)

Alerts are loud on purpose: an alarm played --beeps times, with the volume raised to at least --volume-floor (and
unmuted) for the alarm, then put back. Severe warnings repeat every --repeat s, every --repeat-low s at 15 % or
below. Optional phone push (ntfy, Pushover, Discord, any webhook) is set in ~/.config/charge-watch/config.json.

Optional auto-shutdown (--shutdown-at N, off by default): at N % or below and not charging, plugged in or not, it
warns, counts down --countdown s, and powers off, unless charging starts first. A dead-flat Frame may never charge
again (#2879); shutting down early keeps it above that. It cannot help while the headset is asleep.

Blind spots: it runs only while the headset is awake. Asleep (charging by firmware) and powered off it cannot
watch or warn, and #2879 has units that died in exactly those states.
Nothing leaves the device unless you set up push; then only the warning text and battery % go to the service you
name. Events go to ~/.local/state/charge-watch/events.jsonl.
"""
import argparse, glob, json, math, os, re, select, shlex, shutil, signal, struct, subprocess, sys, tempfile, threading, time, \
    urllib.parse, urllib.request, wave

DAEMON = "deckard_charge_daemon"
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "charge-watch")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "charge-watch", "config.json")

# ---------- parsing the daemon's status block ----------

NUM = r"(-?\d+(?:\.\d+)?)"
PATTERNS = {
    "qph": re.compile(r"QPH\((\d+)\)"),
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


def read_sysfs(root="/sys/class/power_supply", run_dir="/run/deckardcharger"):
    """The battery's fuel gauge (preferred over the charger chip), whether a USB supply is online, the offered
    voltage, and the charger chip's status_detailed if present."""
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
            "shutdown_at": None, "shutdown_hold": 60, "countdown": 60}

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
    "shutdown-warn": "Battery low and not charging. Shutting down in {countdown} seconds to protect the battery. "
                     "Plug into a charger that works to cancel.",
    "shutdown-cancel": "Charging. Shutdown cancelled.",
    "poweroff-failed": "Could not shut down by itself. The battery is nearly empty: shut the headset down now, "
                       "or plug it into a charger that works.",
    "poweroff": "Shutting down now to protect the battery. Charge it on a different charger or cable, "
                "and check the battery is going up.",
}
TITLES = {"stuck": "charger stuck, not charging", "no-input": "plugged in, no power coming in",
          "cant-keep-up": "charger can't keep up", "discharge": "discharging while plugged in",
          "draining": "battery dropping while plugged in", "slow": "slow 5 V charger", "ok": "charging again",
          "shutdown-warn": "low battery, shutting down soon", "shutdown-cancel": "shutdown cancelled",
          "poweroff": "shut down to protect the battery", "poweroff-failed": "could not shut down, do it by hand",
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
        stuck = (detail.get("Input OK") == 0 and detail.get("QPH2") == 1) or \
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


def find_speaker():
    for p in ("spd-say", "espeak-ng", "espeak"):
        if shutil.which(p):
            return p
    return None


def _run_quiet(cmd, timeout):
    try:
        subprocess.run(cmd, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def message(kind, opts=None):
    return MESSAGES[kind].format(countdown=(opts or {}).get("countdown", DEFAULTS["countdown"]))


def alert(kind, mode="auto", say_cmd=None, volume_floor=DEFAULTS["volume_floor"], beeps=DEFAULTS["beeps"], text=None):
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
                sp = say_cmd or ([find_speaker()] if find_speaker() else None)
                if sp == ["spd-say"]:
                    sp = ["spd-say", "-w"]
                if sp:
                    _run_quiet(sp + [msg], 30)
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
    keys = (HEADER, "QPH(", "USB(", "FG INT", "CHG(")
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


# ---------- commands ----------

SETTINGS = {"repeat": int, "repeat_low": int, "volume_floor": float, "beeps": int, "push_repeat": int,
            "shutdown_at": int, "countdown": int, "shutdown_hold": int, "alert": str, "say_cmd": str}
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
            f"push {', '.join(sv) if sv else 'off'}")


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
         "settings": {**{k: o[k] for k in SETTINGS}, "push": push_services(cfg)}}
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
          text="This is a charge-watch test. " + MESSAGES["no-input"])
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
    alert("shutdown-warn", o["alert"], _say(o), o["volume_floor"], o["beeps"], text=message("shutdown-warn", o))
    alert("poweroff", o["alert"], _say(o), o["volume_floor"], 1)
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
        alert(k, o["alert"], _say(o), o["volume_floor"], 1 if k == "poweroff" else o["beeps"], text=text)
        return th

    def handle(s):
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
        sp.add_argument("--say-cmd", help="speech command to use instead of spd-say/espeak-ng, e.g. 'piper-say'")
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
    a = p.parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "replay": cmd_replay, "test-alert": cmd_test_alert,
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
