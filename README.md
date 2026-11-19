# charge-watch

**Warns you, loudly and repeatedly, when your Steam Frame is plugged in but not actually charging, and can shut
it down before the battery goes flat.**

Some Frames get stuck in a charger state where they take little or no power from the wall
([SteamOS #2879](https://github.com/ValveSoftware/SteamOS/issues/2879), "QPH2"). The headset then drains while
plugged in, and several have died on their chargers. Steam shows a small yellow "!" on the battery icon, which is
easy to miss inside a game. charge-watch sounds an alarm instead, says what's wrong, and keeps at it until it's fixed.
It can also push the warning to your phone, and power the headset off at a low battery level you choose, because a
Frame that runs completely flat may never charge again (#2879).

> **Status: early, and looking for testers.** Tested on synthetic logs in the formats and line order quoted in #2879.
> It has **not yet run on a headset**: ours died of this bug and is away for repair. Whether the notification shows
> inside VR is untested; the alarm is the part to rely on. See [Testing it](#testing-it-please) for what to report.

## Install (on the headset, in desktop mode or over ssh)

```sh
git clone https://github.com/frame-commons/charge-watch
cd charge-watch
./install.sh                         # copies one script to ~/.local/bin and starts a systemd user service
~/.local/bin/charge-watch status     # what the charger and battery say right now, and the verdict
~/.local/bin/charge-watch test-alert # hear the warning once (it is loud on purpose)
```

Everything lives in your home directory: no root, nothing changed in SteamOS, and it survives OS updates.
Remove it with `./install.sh --remove`. To update: `git pull && ./install.sh`.

## Find out what your headset is doing: `charge-watch report`

Not every charging problem is the #2879 bug. Some Frames don't see the charger at all while they're on; some only
get 5 V; some charge only while asleep. `report` walks you through a short test and writes one plain-text file
that says which case yours is:

```sh
./charge_watch.py report                # about 2 minutes: unplug, plug back in, it watches what changes
./charge_watch.py report --sleep-test   # adds a sleep test (20 minutes or more): does it charge while asleep?
```

The file has:
- **A result in plain words**, and what to try next. All the patterns, and how many reports showed each one, are
  in [REPORTS.md](REPORTS.md).
- **A paragraph for Valve support**, ready to paste into a ticket.
- **What it saw** at each step: whether the headset sees a charger, what voltage it agreed with the charger, the
  battery current, and every charging value the system exposes before and after.
- **The charge daemon's and the kernel's USB-C/charger log lines** from this boot.

It runs no AI and sends nothing anywhere. Serial numbers, the hostname, your user name, home folder, and MAC and IP
addresses are removed. **Read the file before you share it.** To share it, open a
[charging report](https://github.com/frame-commons/charge-watch/issues/new?template=charging-report.yml) and drag
the file in, or attach it to your Valve ticket. Each shared report adds a row to [REPORTS.md](REPORTS.md):
its pattern, SteamOS build, charger, and sleep result, nothing else.

While the service runs, it also notes each time the headset was asleep and what the battery did meanwhile (in
`events.jsonl`, which the report includes).

## Settings

The installer writes `~/.config/charge-watch/config.json`. Edit it, then `systemctl --user restart charge-watch`.

```json
{
  "shutdown_at": 5,
  "volume_floor": 0.8,
  "beeps": 3,
  "repeat": 60,
  "push": { "ntfy": "https://ntfy.sh/pick-a-long-random-topic-name" }
}
```

**Auto-shutdown** (`shutdown_at`, off by default; `0` = off, otherwise 1-30). When the battery is at or below that %
and not charging (plugged in or not) for a minute of real time, it warns, counts down 60 s (`countdown`), and powers
off. Every source has to agree it isn't charging (the gauge current both ways, and the battery status), so any doubt
means no shutdown. Charging that lasts 30 s during the countdown cancels it; a flicker doesn't. If the headset sleeps
in between, it starts over when it wakes, and never powers off straight from waking. If the power-off fails, it says
so out loud and tries again. It uses SteamOS's own power-off helper
(`steamos-poweroff-now`, then `systemctl poweroff`), so it needs no root. Both work as the normal user on a Frame
from a terminal; that they also work from the background service is what tester step 4 checks. **Why:** #2879 has several units that ran to 0 % and never charged
again, even on a good charger. 5 % is a guess at a safe margin, not a measured one. **What it can't do:** after it
shuts down, the battery is still low. Charge it on a *different* charger or cable, and check the % goes up. #2879
also has units that drained while powered off on a charger that was stuck.

**Loudness.** For each alarm it unmutes and raises the volume to at least `volume_floor` (0-1; `0` = leave the volume
alone), plays a high-low alarm `beeps` times, then puts your volume back. Severe warnings repeat every `repeat`
seconds (default 60), and every 30 s once the battery is at 15 % or below.

**Phone push** (optional; leave all empty for none). Any or all of these:

| Service | Setting | Notes |
|---|---|---|
| [ntfy](https://ntfy.sh) | `"ntfy": "https://ntfy.sh/<topic>"` | Free, no account: install the ntfy app and subscribe to the same topic. Anyone who guesses a topic can read it, so make it long and random. Warnings go out at urgent priority. |
| [Pushover](https://pushover.net) | `"pushover_user"`, `"pushover_token"` | Paid app; high priority for warnings. |
| Discord | `"discord": "<webhook url>"` | A channel webhook. |
| Anything else | `"webhook": "<url>"` | JSON POST: `title`, `message`, `kind`, `soc`, `t`. |

A warning is pushed when it starts, then at most every 10 minutes while it lasts (`push_repeat`). The 5 V notice,
"charging again", and the shutdown warning, cancel and power-off are pushed once each. Check it with `charge-watch test-alert --push`.

## What it warns about

| Warning | What it means | What to do |
|---|---|---|
| **stuck** | The charger chip is in QPH2 with no input: `Input OK: 0` + `QPH2: 1` in `status_detailed`, or `QPH(2)` with under 0.10 A in the daemon log. This is the #2879 bug. | Reseat the cable or switch chargers. #2879 also has a workaround (a drop-in that disables QPH2); read the thread first. |
| **no-input** | Plugged in, but under 0.05 A comes in, or the charger chip refuses the input (`Input OK: 0` without QPH2). #2879 reports this with some power banks, and on stuck units that show `QPH(1) - Sel: AICL`. | Try a wall charger, or another cable. |
| **cant-keep-up** | Current is coming in, but the battery gauge shows it losing charge anyway. | Use a stronger charger (PD, 9 V or more) or a better cable. |
| **discharge** | The battery gauge shows it losing charge while plugged in, cause unknown. | Reseat, then switch chargers. |
| **draining** | Backstop: the battery % itself fell 2 points within 10 minutes while plugged in. | Same. |
| **slow** | The charger only offers 5 V. Said once per plug-in; the headset may still charge, just slowly. | Fine when idle; use a PD charger when playing. |

Severe warnings repeat every minute (every 30 s at 15 % or below) until fixed. You hear "Charging again" when a
warning clears and the battery gauge shows it charging.

**No warning fires** at 95 % or above, or while the battery is hot (it pauses charging above about 42 °C on
purpose). A battery holding steady, at a charge limit for example, is not losing charge, so it stays quiet too.
Every warning has to last 60–120 s first, so replugging doesn't set it off.

**It is blind while the headset sleeps or is off**, and so is the auto-shutdown. Charging is then handled by firmware, and nothing on the
headset can watch it. #2879 has units that died in exactly those states. Don't leave a headset asleep on a charger
you don't trust.

## How it reads the charger

Best source first:

1. **The charge daemon's status block**, which `deckard_charge_daemon` writes to the system journal every ~20 s.
   The lines can come in any order; these values are made up:
   ```
   ---- Current State ----
   USB(1) - V: 15.0, Neg: 15.0;  Cur: -0.02, Neg:  3.0;  Type: C PD [PD_PPS]
   CHG(1) - V:  7.6, Flt:  8.8;  Cur:  1.10, Lim:  1.0;  Stat: Not charging
   FG INT - V: 7.600;            Cur: -1.20;             SOC:  42 %
   QPH(2) - Sel: 2; Load: 0xE
   ```
   If the daemon goes quiet for 90 s, charge-watch falls back to reading sysfs every 20 s until it comes back.
2. **sysfs and the daemon's state files.** These cover:
   - the battery **fuel gauge** (`status`, `current_now`, `temp`);
   - the charger chip's `status_detailed`;
   - whether a USB-C source is online;
   - `/run/deckardcharger/typec_contract` for the offered voltage.

   The charger chip's own status is not reliable while it is stuck, so charge-watch trusts the fuel gauge.

## Commands

```
charge-watch run [--alert auto|chime|speech|print] [--shutdown-at PCT]   what the service runs
charge-watch status [--json]                     one reading, the verdict (exit 1 = a warning) and your settings
charge-watch replay FILE [--json] [--shutdown-at PCT]   run the rules over a saved journal
charge-watch test-alert [--push]                 play the warning once (and send a test push)
charge-watch test-shutdown                       play the shutdown warning; shows the power-off command, runs nothing
charge-watch report [--sleep-test] [--quick]     the guided check: one text file with a result and a Valve paragraph
```

Command-line options override `config.json`; `charge-watch run --help` lists them all (the rule timings `--hold`,
`--drop` and `--window` are command-line only). A bad value in `config.json` is reported in the service log and
replaced by its default, so a typo can't stop the watcher.

`replay` takes `journalctl -t deckard_charge_daemon -o json > charge.json` output, so you can check what
happened before a headset died.

**Sound:**
- **The alarm:** generated by the script (no sound files needed), played through `pw-play`, `paplay` or `aplay`,
  plus a desktop notification.
- **Speech:** after the alarm it says the warning out loud.
  - **The voice clips in `voice/`** come first, and `install.sh` installs them. They are synthetic speech, not a
    recording of anyone: made with the open text-to-speech model
    [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0), voice `am_puck`, by `tools/make-voice.py`.
    `voice/SOURCES` lists each clip's sha256.
  - **Rather not use them?** `"voice": "tts"` skips the clips, and `"voice": "off"` turns speech off; the alarm and
    the notification stay. Or record your own: any line as `~/.config/charge-watch/voice/<name>.wav` takes the place
    of the shipped one. The names and words are in `SPOKEN` in `charge_watch.py`: `stuck`, `no-input`,
    `cant-keep-up`, `discharge`, `draining`, `slow`, `ok`, `shutdown-warn`, `shutdown-cancel`, `poweroff`,
    `poweroff-failed`, `test`. To remake the set in another voice on a PC: `pip install kokoro`, then
    `python3 tools/make-voice.py --voice <name>`.
  - Without clips it uses a text-to-speech engine, but only one that really speaks. SteamOS ships `spd-say`, but
    its only working module plays a "speech isn't set up" message instead (issue #1), so it's skipped.
  - `--say-cmd` uses your own speech command.
  - `charge-watch status` shows which one it will use.

## Privacy

Nothing leaves the headset unless you set up phone push. Then each push carries the message text and battery % (a
generic webhook also gets the warning's name and the time), and pushes happen only when charge-watch says something. No telemetry. It writes one local log of its warnings,
`~/.local/state/charge-watch/events.jsonl`.
`charge-watch report` writes one file in your home folder and sends nothing; you decide whether to share it.

## Testing it, please

Ours is away for repair, so this needs people with a headset. Any of these help; post what you find as an
[issue](https://github.com/frame-commons/charge-watch/issues):

1. **On a charger that works:** `charge-watch status`. Paste the output, along with your `grep BUILD_ID /etc/os-release`
   and charger model. It should say `verdict: ok`, and while the % is rising, `battery +…` should show a **plus** sign.
   Anything else is a false alarm, and that's worth knowing.
2. **The alarm:** `charge-watch test-alert`, once in desktop mode and once inside a game. Did you hear it? Did the
   notification show in VR?
3. **Push:** set one up and run `charge-watch test-alert --push`. Did it arrive?
4. **The power-off path:** `charge-watch test-shutdown`. Which command does it show? For a real test of the service:
   at 25 % or less, set `"shutdown_at": 30` in `config.json`, run `systemctl --user restart charge-watch`, unplug, and
   check it warns within about 2 minutes and powers off 60 s after that. Plugging in during the countdown should
   cancel it. Set it back to 5 afterwards.
5. **If your unit has the bug:** did it warn, and was the warning right? Attach
   `journalctl --user -u charge-watch` and `~/.local/state/charge-watch/events.jsonl`.
6. **If your unit won't charge, whatever the cause:** run `charge-watch report --sleep-test` and share the file as a
   [charging report](https://github.com/frame-commons/charge-watch/issues/new?template=charging-report.yml). Each
   one makes the patterns table better for the next person.

## Other tools, and how this one differs

Two tools already warn when the headset stops charging. Use whichever fits:
- **[cuff](https://github.com/Brobert-in-aus/cuff)** (MIT): a wrist panel with a charging alert (buzz, chime or
  glow), plus time, batteries and quick actions.
- **[frame-power-notifier](https://github.com/Emisune/frame-power-notifier)**: a VR toast when external power is
  lost, aimed at battery-bank users.

charge-watch is narrower:
- **It tells you why** it isn't charging: a stuck QPH2 charger, no input, a charger too weak for the load, or
  5 V only. The fix is different in each case.
- **`replay`** reads a saved journal after the fact.
- **It's small enough to build on.** `charge-watch status --json` gives a verdict any overlay can show.

**Credits:** the field names and states come from the reports in SteamOS #2879, especially IvoryKir's. The
layout (stdlib Python, a user service) follows cympfh's
[frame-vitals-osc](https://github.com/cympfh/frame-vitals-osc).

MIT licence ([LICENSE](LICENSE)). Part of [frame-commons](https://github.com/frame-commons).

Made with help from Claude.
