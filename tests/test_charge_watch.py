# SPDX-License-Identifier: MIT
"""Tests on synthetic journals and sysfs trees (tests/fixtures.py). Run: python3 -m unittest discover tests"""
import contextlib, io, json, os, sys, tempfile, unittest, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import charge_watch as cw  # noqa: E402
import fixtures as fx  # noqa: E402

OK = dict(qph=1, cur=1.5, soc=60)


def replay(blocks, fmt="journalctl", **kw):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(fx.journal(blocks, fmt=fmt))
    args = ["replay", f.name, "--json"] + sum(([f"--{k}", str(v)] for k, v in kw.items()), [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cw.main(args)
    os.unlink(f.name)
    return json.loads(out.getvalue())


def kinds(r):
    return [a["alert"] for a in r["alerts"]]


class Parse(unittest.TestCase):
    def test_block_fields(self):
        got = list(cw.samples_from_lines([(1.0, l) for l in fx.block(qph=2, usb_v=14.6, neg_v=15.0, cur=0.75, neg_a=3.0,
                                                                         soc=100)]))
        self.assertEqual(len(got), 1); got = got[0]
        self.assertEqual(got["qph"], 2); self.assertEqual(got["soc"], 100)
        self.assertAlmostEqual(got["usb_v"], 14.6); self.assertAlmostEqual(got["usb_cur"], 0.75)
        self.assertAlmostEqual(got["usb_neg_v"], 15.0); self.assertEqual(got["chg_stat"], "Charging")

    def test_any_line_order_gives_one_block_each(self):
        for order in (fx.REAL_ORDER, fx.OLD_ORDER, ("usb", "fg", "qph"), ("qph", "chg", "fg", "usb")):
            r = replay([dict(OK, order=order)] * 7)
            self.assertEqual(r["samples"], 7, order)

    def test_not_charging_status_and_negative_input(self):
        got = list(cw.samples_from_lines([(1.0, l) for l in fx.block(stat="Not charging", cur=-0.02)]))[0]
        self.assertEqual(got["chg_stat"], "Not charging"); self.assertAlmostEqual(got["usb_cur"], -0.02)

    def test_time_gap_splits_headerless_blocks(self):
        lines = [(t0 + j * 0.001, l) for t0 in (10.0, 30.0) for j, l in enumerate(fx.block(order=("usb", "fg", "qph")))]
        self.assertEqual(len(list(cw.samples_from_lines(lines))), 2)

    def test_stale_flushes_a_quiet_block(self):
        bp = cw.BlockParser()
        for l in fx.block():
            bp.feed(100.0, l)
        self.assertIsNone(bp.stale(101.0)); self.assertIsNotNone(bp.stale(104.0))

    def test_byte_array_message(self):
        self.assertEqual(cw.message_text(list(b"QPH(2) - Sel: 2")), "QPH(2) - Sel: 2")
        t, m = cw._journal_entry(json.dumps({"__REALTIME_TIMESTAMP": "5000000", "MESSAGE": list(b"USB(1) x")}))
        self.assertEqual((t, m), (5.0, "USB(1) x"))

    def test_both_saved_formats(self):
        for fmt in ("journalctl", "tline"):
            self.assertEqual(replay([OK] * 5, fmt=fmt)["samples"], 5)

    def test_partial_block_without_header(self):
        got = list(cw.samples_from_lines([(1, "USB(1) - V:  5.0, Neg:  5.0;  Cur:  0.40, Neg:  1.5;  Type: Unknown")]))
        self.assertEqual(got[0]["usb_v"], 5.0)

    def test_parse_detail_several_fields_per_line(self):
        d = cw.parse_detail("Input OK: 0\nIMP Trigger: 1\nQPH2: 1\nUSB UV: 0   USB OV: 0   USB Suspend: 0\nJEITA Hot: 0")
        self.assertEqual(d, {"Input OK": 0, "IMP Trigger": 1, "QPH2": 1, "USB UV": 0, "USB OV": 0, "USB Suspend": 0,
                             "JEITA Hot": 0})


class Rules(unittest.TestCase):
    def test_healthy_is_silent(self):
        self.assertEqual(kinds(replay([OK] * 30)), [])

    def test_unplugged_is_silent(self):
        self.assertEqual(kinds(replay([dict(online=0, usb_v=0.0, neg_v=0.0, cur=0.0, soc=40)] * 30)), [])

    def test_stuck_qph2_fires_after_hold_then_repeats_then_clears(self):
        bad = dict(qph=2, cur=0.0, soc=60)
        r = replay([OK] * 3 + [bad] * 12 + [OK] * 3, hold=60, repeat=120)
        k = kinds(r)
        self.assertEqual(k[0], "stuck")
        self.assertEqual(k.count("stuck"), 2)          # 220 s of stuck at 20 s blocks: once at 60 s, again at 180 s
        self.assertEqual(k[-1], "ok")
        first = r["alerts"][0]["t"] - 1_800_000_000.0
        self.assertGreaterEqual(first, 3 * 20 + 60)     # not before the hold time

    def test_qph2_with_current_is_fine(self):
        self.assertEqual(kinds(replay([dict(qph=2, cur=1.10, soc=100)] * 20)), [])

    def test_short_glitch_does_not_fire(self):
        self.assertEqual(kinds(replay([OK] * 3 + [dict(qph=2, cur=0.0, soc=60)] * 2 + [OK] * 5, hold=60)), [])

    def test_draining_while_plugged(self):
        blocks = [dict(qph=1, cur=0.4, soc=s) for s in (60, 60, 59, 59) + (58,) * 8]
        self.assertIn("draining", kinds(replay(blocks, drop=2)))

    def test_real_order_stuck_case_from_2879_fires(self):
        """The reviewer's case: 40 blocks in the real order, QPH(2), -0.02 A in, the gauge at -1.03 A, 74 -> 70 %."""
        blocks = [dict(qph=2, cur=-0.02, fg_cur=-1.03, soc=74 - i // 10, stat="Not charging") for i in range(40)]
        self.assertEqual(kinds(replay(blocks))[0], "stuck")

    def test_stuck_shown_as_qph1_aicl_is_no_input(self):
        blocks = [dict(qph=1, qph_sel="AICL", cur=-0.02, fg_cur=-1.23, soc=16, stat="Not charging")] * 8
        self.assertEqual(kinds(replay(blocks))[0], "no-input")

    def test_no_charging_again_while_still_discharging(self):
        bad = dict(qph=2, cur=0.0, fg_cur=-0.9, soc=90)
        hot = dict(qph=1, cur=1.2, fg_cur=-0.3, soc=90)       # recovered input, but the gauge still says losing
        k = kinds(replay([bad] * 6 + [dict(hot, soc=96)] * 3))   # crossing 95 % clears the warning, not charging
        self.assertIn("stuck", k); self.assertNotIn("ok", k)

    def test_unplug_resets_drain_window(self):
        blocks = [dict(qph=1, cur=1.0, soc=60), dict(online=0, usb_v=0.0, neg_v=0.0, cur=0.0, soc=59),
                  dict(qph=1, cur=1.0, soc=58), dict(qph=1, cur=1.0, soc=58)]
        self.assertNotIn("draining", kinds(replay(blocks, drop=2)))

    def test_slow_5v_said_once(self):
        k = kinds(replay([dict(qph=1, usb_v=5.0, neg_v=5.0, cur=1.4, neg_a=3.0, soc=50)] * 20))
        self.assertEqual(k, ["slow"])

    def test_slow_not_said_when_nearly_full(self):
        self.assertEqual(kinds(replay([dict(qph=1, usb_v=5.0, neg_v=5.0, cur=0.4, soc=97)] * 20)), [])

    def test_no_input_without_qph_state(self):
        s = [{"t": 1000 + 20 * i, "src": "daemon", "usb_online": 1, "usb_v": 5.1, "usb_neg_v": 9.0, "usb_cur": 0.0, "soc": 40}
             for i in range(6)]
        rules, act = cw.Rules(hold=60), []
        for x in s:
            act = rules.step(x)
        self.assertIn("no-input", act)

    def _sys(self, n, **kw):
        base = {"src": "sysfs", "usb_online": 1, "usb_neg_v": 15.0, "soc": 50, "bat_status": "Charging", "bat_cur": 0.5,
                "bat_temp": 30.0, "detail": {"Input OK": 1, "QPH2": 0}}
        rules, act = cw.Rules(), []
        for i in range(n):
            act = rules.step({**base, **kw, "t": 1000 + 20 * i})
        return act

    def test_gauge_discharging_while_plugged(self):
        self.assertEqual(self._sys(6, bat_status="Discharging", bat_cur=-0.8), ["discharge"])
        self.assertEqual(self._sys(4, bat_status="Discharging", bat_cur=-0.8), [])        # 60 s < the 90 s hold

    def test_charger_chip_claims_charging_but_gauge_wins(self):
        self.assertIn("discharge", self._sys(6, bat_cur=-0.6))                            # status says Charging

    def test_status_detailed_stuck(self):
        self.assertIn("stuck", self._sys(4, detail={"Input OK": 0, "IMP Trigger": 1, "QPH2": 1}, bat_cur=-0.9))

    def test_power_bank_variant(self):
        act = self._sys(4, detail={"Input OK": 0, "IMP Trigger": 0, "QPH2": 0}, bat_cur=-0.9)
        self.assertIn("no-input", act); self.assertNotIn("stuck", act)

    def test_hot_battery_is_not_a_fault(self):
        self.assertEqual(self._sys(10, bat_status="Discharging", bat_cur=-0.4, bat_temp=44.0), [])

    def test_full_or_at_limit_is_quiet(self):
        self.assertEqual(self._sys(10, soc=100, bat_cur=-0.0), [])
        self.assertEqual(self._sys(10, soc=80, bat_cur=0.0), [])
        self.assertEqual(self._sys(10, soc=80, bat_cur=-0.01, bat_status="Discharging"), [])   # a charge limit
        self.assertEqual(self._sys(10, soc=80, bat_cur=None, bat_status="Discharging"), ["discharge"])  # no current: status counts

    def test_cant_keep_up_when_current_flows(self):
        rules, act = cw.Rules(), []
        for i in range(6):
            act = rules.step({"t": 1000 + 20 * i, "src": "daemon", "usb_online": 1, "usb_v": 14.7, "usb_neg_v": 15.0,
                              "usb_cur": 1.2, "qph": 1, "soc": 60, "fg_cur": -0.7})
        self.assertEqual(act, ["cant-keep-up"])


class Sysfs(unittest.TestCase):
    def test_read_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            fx.sysfs(d, soc=42, status="Discharging", online=1, volts=5_000_000, bat_ua=-700_000)
            s = cw.read_sysfs(d, run_dir=os.path.join(d, "none"))
            self.assertEqual((s["soc"], s["bat_status"], s["usb_online"]), (42, "Discharging", 1))   # the gauge, not the chip
            self.assertAlmostEqual(s["usb_neg_v"], 5.0); self.assertAlmostEqual(s["bat_cur"], -0.7)
            self.assertEqual(s["detail"]["Input OK"], 1); self.assertAlmostEqual(s["bat_temp"], 30.0)
            cw.latest_daemon_sample = lambda: None  # no journal in the test
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cw.main(["status", "--json", "--sysfs", d])
            r = json.loads(out.getvalue())
            self.assertEqual(rc, 1)
            self.assertIn("discharge", r["verdict"]); self.assertIn("slow", r["verdict"])


class RunDir(unittest.TestCase):
    def test_typec_contract_and_ac_status(self):
        with tempfile.TemporaryDirectory() as d:
            fx.sysfs(d, online=0)
            run = os.path.join(d, "run"); os.makedirs(run)
            for n, v in (("ac_status", "Connected"), ("typec_contract", "Valid=1, Voltage=9000mV, Current=3000mA")):
                with open(os.path.join(run, n), "w") as f:
                    f.write(v + "\n")
            s = cw.read_sysfs(d, run_dir=run)
            self.assertEqual(s["usb_online"], 1); self.assertAlmostEqual(s["usb_neg_v"], 9.0)


class Alert(unittest.TestCase):
    def test_chime_is_a_valid_wav(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.wav"); cw.chime_wav(p)
            with wave.open(p) as w:
                self.assertGreater(w.getnframes(), 5000); self.assertEqual(w.getnchannels(), 1)

    def test_print_mode_never_touches_audio(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cw.alert("stuck", mode="print")
        self.assertIn("stuck", out.getvalue())


class RunLoop(unittest.TestCase):
    """`charge-watch run` against a stub journalctl on PATH that logs a stuck charger, in the real line order."""

    def test_run_warns_on_a_live_stuck_journal(self):
        import subprocess, time as _t
        with tempfile.TemporaryDirectory() as d:
            now = _t.time()
            data = os.path.join(d, "journal.json")
            with open(data, "w") as f:
                f.write(fx.journal([dict(qph=2, cur=-0.02, fg_cur=-1.0, soc=70, stat="Not charging")] * 8, t0=now))
            stub = os.path.join(d, "journalctl")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\n"
                        f'case "$*" in *-f*) cat "{data}"; sleep 9 ;; *) echo "---- Current State ----" ;; esac\n')
            os.chmod(stub, 0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""), XDG_STATE_HOME=d, XDG_CONFIG_HOME=d)
            script = os.path.join(os.path.dirname(HERE), "charge_watch.py")
            try:
                out = subprocess.run([sys.executable, script, "run", "--alert", "print", "--sysfs", os.path.join(d, "none")],
                                     env=env, capture_output=True, text=True, timeout=8).stdout
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            self.assertIn("charge daemon journal", out)
            self.assertIn("stuck:", out)
            self.assertTrue(os.path.exists(os.path.join(d, "charge-watch", "events.jsonl")))


LOW_UNPLUGGED = dict(online=0, usb_v=0.0, neg_v=0.0, cur=0.0, fg_cur=-1.1, stat="Not charging")


class Shutdown(unittest.TestCase):
    def test_off_by_default(self):
        k = kinds(replay([dict(LOW_UNPLUGGED, soc=3)] * 20))
        self.assertNotIn("poweroff", k); self.assertNotIn("shutdown-warn", k)

    def test_low_and_not_charging_warns_then_powers_off(self):
        r = replay([dict(LOW_UNPLUGGED, soc=4)] * 20, **{"shutdown-at": 5, "countdown": 60})
        k = kinds(r)
        self.assertEqual(k, ["shutdown-warn", "poweroff"])
        t = [a["t"] - 1_800_000_000.0 for a in r["alerts"]]
        self.assertGreaterEqual(t[0], 60)               # held 60 s first
        self.assertGreaterEqual(t[1] - t[0], 60)        # then the whole countdown
        self.assertLess(r["samples"], 20)               # replay stops where the headset would be off

    def test_plugged_in_but_stuck_also_counts(self):
        k = kinds(replay([dict(qph=2, cur=0.0, fg_cur=-1.0, soc=5, stat="Not charging")] * 20, **{"shutdown-at": 5}))
        self.assertIn("stuck", k); self.assertEqual(k[-1], "poweroff")

    def test_charging_cancels_the_countdown(self):
        low = [dict(LOW_UNPLUGGED, soc=4)] * 5                       # warn at 80 s, deadline 140 s
        good = [dict(qph=1, cur=1.5, fg_cur=0.8, soc=4)] * 5
        k = kinds(replay(low + good, **{"shutdown-at": 5, "countdown": 60}))
        self.assertIn("shutdown-warn", k); self.assertIn("shutdown-cancel", k); self.assertNotIn("poweroff", k)

    def test_charging_slowly_at_low_battery_is_left_alone(self):
        k = kinds(replay([dict(qph=1, cur=0.6, fg_cur=0.2, soc=3)] * 20, **{"shutdown-at": 5}))
        self.assertNotIn("shutdown-warn", k)

    def test_above_threshold_is_left_alone(self):
        self.assertEqual(kinds(replay([dict(LOW_UNPLUGGED, soc=6)] * 20, **{"shutdown-at": 5})), [])

    def test_settings_validate_shutdown_at(self):
        a = cw.argparse.Namespace(shutdown_at=None)
        self.assertIsNone(cw.settings(a, {"shutdown_at": 0})["shutdown_at"])
        self.assertEqual(cw.settings(a, {"shutdown_at": "5"})["shutdown_at"], 5)
        self.assertIsNone(cw.settings(a, {"shutdown_at": 60})["shutdown_at"])     # out of range: off, not a crash
        self.assertEqual(cw.settings(cw.argparse.Namespace(shutdown_at=3), {"shutdown_at": 5})["shutdown_at"], 3)


class Escalation(unittest.TestCase):
    def test_repeats_faster_when_low(self):
        bad = dict(qph=2, cur=0.0, fg_cur=-1.0, stat="Not charging")
        high = kinds(replay([dict(bad, soc=60)] * 30, repeat=60, **{"repeat-low": 30}))
        low = kinds(replay([dict(bad, soc=10)] * 30, repeat=60, **{"repeat-low": 30}))
        self.assertGreater(low.count("stuck"), high.count("stuck"))

    def test_push_gate(self):
        g = cw.PushGate(600)
        self.assertTrue(g.allow(0, "stuck")); self.assertFalse(g.allow(60, "stuck")); self.assertTrue(g.allow(600, "stuck"))
        self.assertTrue(g.allow(601, "ok")); self.assertTrue(g.allow(602, "stuck"))   # a new episode pushes at once
        self.assertTrue(g.allow(603, "poweroff"))


def _stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, 0o755)
    return p


class Loudness(unittest.TestCase):
    def _with_wpctl(self, answer, floor):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "calls")
            _stub(d, "wpctl", f'echo "$*" >> "{log}"; case "$1" in get-volume) echo "{answer}";; esac')
            old = os.environ["PATH"]; os.environ["PATH"] = d + os.pathsep + old
            try:
                with cw.Loud(floor):
                    pass
            finally:
                os.environ["PATH"] = old
            with open(log) as f:
                return [l.split()[0] + " " + l.split()[-1] for l in f.read().split("\n") if l]

    def test_raises_and_unmutes_then_restores(self):
        calls = self._with_wpctl("Volume: 0.30 [MUTED]", 0.8)
        self.assertEqual(calls, ["get-volume @DEFAULT_AUDIO_SINK@", "set-mute 0", "set-volume 0.80",
                                 "set-volume 0.30", "set-mute 1"])

    def test_leaves_loud_volume_alone(self):
        self.assertEqual(self._with_wpctl("Volume: 0.95", 0.8), ["get-volume @DEFAULT_AUDIO_SINK@"])

    def test_floor_zero_never_touches_volume(self):
        with tempfile.TemporaryDirectory() as d:
            _stub(d, "wpctl", f'touch "{d}/touched"')
            old = os.environ["PATH"]; os.environ["PATH"] = d + os.pathsep + old
            try:
                with cw.Loud(0):
                    pass
            finally:
                os.environ["PATH"] = old
            self.assertFalse(os.path.exists(os.path.join(d, "touched")))

    def test_alarm_is_longer_and_louder_than_ok(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.wav"), os.path.join(d, "b.wav")
            cw.chime_wav(a, "warn", beeps=3); cw.chime_wav(b, "ok")
            with wave.open(a) as w, wave.open(b) as v:
                self.assertGreater(w.getnframes(), 3 * v.getnframes())


class Push(unittest.TestCase):
    def setUp(self):
        import http.server, threading
        self.got = []
        got = self.got

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                got.append((self.path, dict(self.headers), self.rfile.read(n).decode()))
                self.send_response(200); self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()

    def test_ntfy_discord_webhook(self):
        cfg = {"push": {"ntfy": self.url + "/my-topic", "discord": self.url + "/dc", "webhook": self.url + "/wh"}}
        res = cw.push("stuck", cw.MESSAGES["stuck"], cfg, soc=40)
        self.assertEqual(res, {"ntfy": "ok", "discord": "ok", "webhook": "ok"})
        by = {p: (h, b) for p, h, b in self.got}
        self.assertEqual(by["/my-topic"][0]["Priority"], "urgent")
        self.assertIn("40 %", by["/my-topic"][1])
        self.assertIn("charger stuck", json.loads(by["/dc"][1])["content"])
        self.assertEqual(json.loads(by["/wh"][1])["kind"], "stuck")

    def test_calm_kinds_are_not_urgent(self):
        cw.push("ok", cw.MESSAGES["ok"], {"push": {"ntfy": self.url + "/t"}})
        self.assertEqual(self.got[0][1]["Priority"], "default")

    def test_pushover_form(self):
        sent = []
        orig = cw._post
        cw._post = lambda url, data, headers, timeout=10: sent.append((url, data)) or 200
        try:
            cw.push("poweroff", "x", {"push": {"pushover_user": "u", "pushover_token": "k"}})
        finally:
            cw._post = orig
        self.assertIn("api.pushover.net", sent[0][0]); self.assertIn(b"priority=1", sent[0][1])

    def test_a_dead_service_never_raises(self):
        res = cw.push("stuck", "x", {"push": {"ntfy": "http://127.0.0.1:9/nothing"}})
        self.assertNotEqual(res["ntfy"], "ok")

    def test_nothing_configured_sends_nothing(self):
        self.assertEqual(cw.push("stuck", "x", {}), {}); self.assertEqual(self.got, [])


class PowerOffLoop(unittest.TestCase):
    """`run --shutdown-at 5` on a stub journal of a battery at 4 % and not charging: it must run the power-off
    command (a stub here; CHARGE_WATCH_POWEROFF and PATH both point at it, so nothing real can power off)."""

    def _env(self, d):
        marker = os.path.join(d, "powered-off")
        off = _stub(d, "fake-poweroff", f'touch "{marker}"')
        _stub(d, "systemctl", f'touch "{marker}.systemctl"'); _stub(d, "pkexec", f'touch "{marker}.pkexec"')
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""), XDG_STATE_HOME=d,
                   XDG_CONFIG_HOME=d, CHARGE_WATCH_POWEROFF=off)
        return env, marker

    def _live_journal(self, d, blocks, every):
        """A stub journalctl that, with -f, prints one block every `every` s of real time, stamped with real time."""
        lines = [l.split(": ", 1)[1] for kw in blocks for l in fx.block(**kw)]
        stub = os.path.join(d, "journalctl")
        with open(stub, "w") as f:
            f.write(f"""#!{sys.executable}
import json, sys, time
if "-f" not in sys.argv:
    print("---- Current State ----"); sys.exit(0)
lines = {lines!r}
for i in range(0, len(lines), 6):
    for l in lines[i:i + 6]:
        print(json.dumps({{"__REALTIME_TIMESTAMP": str(int(time.time() * 1e6)), "MESSAGE": l}}), flush=True)
    time.sleep({every})
time.sleep(30)
""")
        os.chmod(stub, 0o755)

    def _run(self, env, d, extra, timeout):
        import subprocess
        script = os.path.join(os.path.dirname(HERE), "charge_watch.py")
        try:
            return subprocess.run([sys.executable, script, "run", "--alert", "print", "--sysfs", os.path.join(d, "none")]
                                  + extra, env=env, capture_output=True, text=True, timeout=timeout).stdout
        except subprocess.TimeoutExpired as e:
            return (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")

    def test_run_powers_off_after_real_hold_and_countdown(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            env, marker = self._env(d)
            self._live_journal(d, [dict(LOW_UNPLUGGED, soc=4)] * 30, every=0.5)
            t0 = _t.monotonic()
            self._run(env, d, ["--shutdown-at", "5", "--shutdown-hold", "2", "--countdown", "2"], timeout=10)
            self.assertTrue(os.path.exists(marker))
            self.assertFalse(os.path.exists(marker + ".systemctl"))
            with open(os.path.join(d, "charge-watch", "events.jsonl")) as f:
                ev = [json.loads(l) for l in f]
            off = [e for e in ev if e.get("event") == "poweroff" and e.get("command")]
            self.assertEqual(len(off), 1)
            warn = [e["t"] for e in ev if e.get("alert") == "shutdown-warn"]
            self.assertGreaterEqual(off[0]["t"] - warn[0], 2)        # the countdown really elapsed

    def test_old_journal_lines_cannot_power_off(self):
        """The v2 review's case: a journal whose timestamps span minutes but arrives all at once (a replay at
        start-up) must not count as minutes of low battery."""
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            env, marker = self._env(d)
            data = os.path.join(d, "journal.json")
            with open(data, "w") as f:
                f.write(fx.journal([dict(LOW_UNPLUGGED, soc=4)] * 30, t0=_t.time() - 600))
            _stub(d, "journalctl", f'case "$*" in *-f*) cat "{data}"; sleep 9 ;; *) echo "---- Current State ----" ;; esac')
            self._run(env, d, ["--shutdown-at", "5", "--countdown", "1"], timeout=6)
            self.assertFalse(os.path.exists(marker))

    def test_test_shutdown_never_powers_off(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            env, marker = self._env(d)
            script = os.path.join(os.path.dirname(HERE), "charge_watch.py")
            out = subprocess.run([sys.executable, script, "test-shutdown", "--alert", "print"],
                                 env=env, capture_output=True, text=True, timeout=10).stdout
            self.assertIn("nothing powered off", out)
            self.assertFalse(any(os.path.exists(marker + x) for x in ("", ".systemctl", ".pkexec")))


class GuardTiming(unittest.TestCase):
    """ShutdownGuard against explicit processing times (`now`), as the live loop feeds it."""

    def s(self, **kw):
        return {"t": 0, "src": "daemon", "soc": kw.pop("soc", 4), "fg_cur": kw.pop("fg_cur", -1.0), **kw}

    def feed(self, g, times, **kw):
        out = []
        for n in times:
            out += [(n, k) for k in g.step(self.s(**kw), now=n)]
        return out

    def test_sleep_gap_restarts_instead_of_powering_off(self):
        g = cw.ShutdownGuard(5, hold=60, countdown=60)
        got = self.feed(g, range(0, 81, 20))                      # warns at 60 s
        self.assertEqual([k for _, k in got], ["shutdown-warn"])
        got = self.feed(g, [80 + 600])                             # wakes 10 min later
        self.assertEqual(got, [])                                  # no instant power-off
        got = self.feed(g, range(700, 900, 20))
        self.assertEqual([k for _, k in got], ["shutdown-warn", "poweroff"])   # a fresh hold + countdown

    def test_stale_then_fresh_sample_skips_nothing(self):
        g = cw.ShutdownGuard(5, hold=60)
        self.assertEqual(self.feed(g, [0, 7200]), [])

    def test_flapping_current_does_not_cancel_or_block(self):
        g = cw.ShutdownGuard(5, hold=60, countdown=60)
        got = []
        for i, n in enumerate(range(0, 400, 20)):
            got += [k for k in g.step(self.s(fg_cur=0.2 if i % 3 == 2 else -1.0), now=n)]
        self.assertIn("poweroff", got); self.assertNotIn("shutdown-cancel", got)

    def test_lasting_charge_cancels(self):
        g = cw.ShutdownGuard(5, hold=60, countdown=120)
        got = self.feed(g, range(0, 81, 20)) + self.feed(g, range(100, 200, 20), fg_cur=0.5)
        self.assertEqual([k for _, k in got], ["shutdown-warn", "shutdown-cancel"])

    def test_status_charging_with_negative_current_is_left_alone(self):
        g = cw.ShutdownGuard(5, hold=0, countdown=0)
        self.assertEqual(self.feed(g, range(0, 300, 20), bat_status="Charging"), [])

    def test_positive_sysfs_current_is_left_alone(self):
        g = cw.ShutdownGuard(5, hold=0, countdown=0)
        self.assertEqual(self.feed(g, range(0, 300, 20), bat_cur=0.4), [])

    def test_failed_poweroff_retries(self):
        g = cw.ShutdownGuard(5, hold=0, countdown=20)
        got = [k for _, k in self.feed(g, [0, 20])]
        self.assertEqual(got, ["shutdown-warn", "poweroff"])
        g.failed(20)
        self.assertEqual([k for _, k in self.feed(g, [30, 40])], ["poweroff"])


class Settings(unittest.TestCase):
    def test_bad_config_values_fall_back(self):
        a = cw.argparse.Namespace()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            o = cw.settings(a, {"repeat": "abc", "countdown": None, "volume_floor": "loud", "beeps": 2.5,
                                "push_repeat": "300", "alert": "siren", "shutdown_at": True})
        self.assertEqual(o["repeat"], cw.DEFAULTS["repeat"]); self.assertEqual(o["countdown"], cw.DEFAULTS["countdown"])
        self.assertEqual(o["volume_floor"], cw.DEFAULTS["volume_floor"]); self.assertEqual(o["beeps"], cw.DEFAULTS["beeps"])
        self.assertEqual(o["push_repeat"], 300); self.assertEqual(o["alert"], "auto"); self.assertIsNone(o["shutdown_at"])
        self.assertIn("ignoring", err.getvalue())

    def test_cli_beats_config(self):
        o = cw.settings(cw.argparse.Namespace(repeat=45), {"repeat": 90, "beeps": 5})
        self.assertEqual((o["repeat"], o["beeps"]), (45, 5))


class Sigterm(unittest.TestCase):
    def test_killed_mid_alarm_puts_volume_back(self):
        import signal, subprocess, time as _t
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "calls")
            _stub(d, "wpctl", f'echo "$*" >> "{log}"; case "$1" in get-volume) echo "Volume: 0.30 [MUTED]";; esac')
            _stub(d, "pw-play", f'touch "{d}/playing"; sleep 20')
            env = dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""), XDG_CONFIG_HOME=d, XDG_STATE_HOME=d)
            script = os.path.join(os.path.dirname(HERE), "charge_watch.py")
            p = subprocess.Popen([sys.executable, script, "test-alert", "--alert", "chime"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if os.path.exists(os.path.join(d, "playing")):
                    break
                _t.sleep(0.1)
            p.send_signal(signal.SIGTERM); p.wait(10)
            with open(log) as f:
                calls = [l.split()[0] + " " + l.split()[-1] for l in f.read().split("\n") if l]
            self.assertEqual(calls[-2:], ["set-volume 0.30", "set-mute 1"])


class Speech(unittest.TestCase):
    """Issue #1: SteamOS on the Frame ships spd-say whose only working module is 'dummy', which plays a recording
    saying speech isn't set up. Being installed must not count as working; the bundled clips speak instead."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = os.path.join(self.d, "calls")
        self.old = {k: os.environ.get(k) for k in ("PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME")}
        os.environ.update(PATH=self.d, XDG_CONFIG_HOME=os.path.join(self.d, "cfg"), XDG_DATA_HOME=os.path.join(self.d, "data"))
        cw._TTS.clear()

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cw._TTS.clear()

    def stub(self, name, body):
        return _stub(self.d, name, f'echo "{name} $*" >> "{self.log}"\n' + body)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_every_spoken_line_has_a_clip(self):
        repo_voice = os.path.join(os.path.dirname(HERE), "voice")
        if not os.path.isdir(repo_voice):
            self.skipTest("no voice/ in this checkout (made by tools/make-voice.py)")
        for kind in cw.SPOKEN:
            p = os.path.join(repo_voice, kind + ".wav")
            self.assertTrue(os.path.isfile(p), kind)
            with wave.open(p) as w:
                self.assertEqual((w.getframerate(), w.getnchannels()), (16000, 1), kind)
                self.assertGreater(w.getnframes(), 8000, kind)
        for kind in cw.MESSAGES:
            self.assertIn(kind, cw.SPOKEN)

    def test_dummy_only_spd_say_is_not_speech(self):
        self.stub("spd-say", 'case "$1" in -O) printf "OUTPUT MODULES\\ndummy\\n";; esac')
        self.assertIsNone(cw.working_tts())
        self.stub("pw-play", "")
        self.assertIsNone(cw.speak(["stuck"], "x", voice="tts"))       # no clips allowed, no engine: silent
        self.assertFalse([c for c in self.calls() if c.startswith("spd-say -w")])

    def test_issue_1_case_speaks_the_clips(self):
        self.stub("spd-say", 'case "$1" in -O) printf "OUTPUT MODULES\\ndummy\\n";; esac')
        self.stub("pw-play", "")
        mine = os.path.join(self.d, "cfg", "charge-watch", "voice")
        os.makedirs(mine)
        for k in ("test", "no-input"):
            cw.chime_wav(os.path.join(mine, k + ".wav"), "ok")
        self.assertEqual(cw.speak(["test", "no-input"], "x"), "clips")
        played = [c for c in self.calls() if c.startswith("pw-play")]
        self.assertEqual([os.path.basename(c.split()[-1]) for c in played], ["test.wav", "no-input.wav"])
        self.assertFalse([c for c in self.calls() if c.startswith("spd-say -w")])

    def test_spd_say_with_a_real_module_counts(self):
        self.stub("spd-say", 'case "$1" in -O) printf "OUTPUT MODULES\\nespeak-ng\\ndummy\\n";; esac')
        self.assertEqual(cw.working_tts(), ("spd-say",))

    def test_espeak_ng_counts_only_if_it_writes_audio(self):
        self.stub("espeak-ng", 'exit 1')
        self.assertIsNone(cw.working_tts())
        cw._TTS.clear()
        self.stub("espeak-ng", 'printf "RIFF"; /usr/bin/head -c 4000 /dev/zero')
        self.assertEqual(cw.working_tts(), ("file", "espeak-ng"))

    def test_your_own_recording_wins(self):
        mine = os.path.join(self.d, "cfg", "charge-watch", "voice")
        os.makedirs(mine)
        with open(os.path.join(mine, "stuck.wav"), "wb") as f:
            f.write(b"RIFF")
        self.assertEqual(cw.clip_for("stuck"), os.path.join(mine, "stuck.wav"))
        self.assertIsNone(cw.clip_for("no-such-line"))

    def test_voice_off(self):
        self.stub("pw-play", "")
        self.assertIsNone(cw.speak(["stuck"], "x", voice="off"))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
