# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Unit tests (no root, no network): python3 -m unittest discover -s tests -p 'test_*.py'"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ["VPN_TRAY_CONFIG_DIR"] = tempfile.mkdtemp()
os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp()

from vpntray import core  # noqa: E402
from vpntray.backends.happ import read_happ_conf  # noqa: E402
from vpntray.backends.nm import parse_vpn_data  # noqa: E402
from vpntray import agent as agent_mod  # noqa: E402
from vpntray.core import (BACKENDS, Backend, Manager, OpLock, Profile, ProfileStore, State,  # noqa: E402
                          StateFile, Status, human_bytes, split_hostport)


class FakeBackend(Backend):
    key = "fake"

    def __init__(self):
        self.up = set()
        self.calls = []

    def discover(self):
        return [Profile(id="fake:a", backend="fake", name="A"),
                Profile(id="fake:b", backend="fake", name="B"),
                Profile(id="fake:split", backend="fake", name="Split", full_tunnel=False)]

    def connect(self, p):
        self.calls.append(("up", p.id))
        self.up.add(p.id)

    def disconnect(self, p):
        self.calls.append(("down", p.id))
        self.up.discard(p.id)

    def status(self, p):
        on = p.id in self.up
        return Status(state=State.CONNECTED if on else State.DISCONNECTED,
                      rx_bytes=1000 if on else 0, tx_bytes=10 if on else 0)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(BACKENDS)
        BACKENDS.clear()
        self.fake = BACKENDS["fake"] = FakeBackend()
        self.path = Path(tempfile.mkdtemp()) / "profiles.json"
        self.m = Manager(ProfileStore(self.path))
        self.m.discover()

    def tearDown(self):
        BACKENDS.clear()
        BACKENDS.update(self.saved)

    def test_discover_lists_profiles(self):
        self.assertEqual({p.id for p in self.m.profiles()}, {"fake:a", "fake:b", "fake:split"})

    def test_full_tunnel_is_exclusive_and_new_goes_up_first(self):
        self.m.connect("fake:a", timeout=2)
        self.m.connect("fake:split", timeout=2)
        self.m.connect("fake:b", timeout=2)
        self.assertEqual(self.fake.up, {"fake:b", "fake:split"})
        # break before make: the old full-tunnel VPN goes down before the new one comes up
        self.assertLess(self.fake.calls.index(("down", "fake:a")), self.fake.calls.index(("up", "fake:b")))

    def test_failed_switch_restores_previous_vpn(self):
        self.m.connect("fake:a", timeout=2)
        real = self.fake.connect
        self.fake.connect = lambda p: real(p) if p.id != "fake:b" else None   # b never comes up
        with self.assertRaises(RuntimeError):
            self.m.connect("fake:b", timeout=1)
        self.assertIn("fake:a", self.fake.up)
        self.assertEqual(self.m.state.read()["desired"], "fake:a")

    def test_rename_persists_and_survives_rediscovery(self):
        self.m.rename("fake:a", "  Office VPN ")
        self.m.discover()
        self.assertEqual(ProfileStore(self.path).profiles["fake:a"].name, "Office VPN")
        with self.assertRaises(ValueError):
            self.m.rename("fake:a", "   ")

    def test_deleted_vpn_disappears_but_failed_discovery_keeps_profiles(self):
        orig = self.fake.discover
        self.fake.discover = lambda: [p for p in orig() if p.id != "fake:b"]
        self.m.discover()
        self.assertNotIn("fake:b", {p.id for p in self.m.profiles()})
        self.fake.discover = lambda: (_ for _ in ()).throw(RuntimeError("nmcli down"))
        self.m.discover()
        self.assertIn("fake:a", {p.id for p in self.m.profiles()})

    def test_rates(self):
        self.m.connect("fake:a", timeout=2)
        self.m.status("fake:a")
        self.fake.status = lambda p: Status(state=State.CONNECTED, rx_bytes=1000 + 5000, tx_bytes=10)
        st = self.m.status("fake:a")
        self.assertGreater(st.rx_rate, 0)

    def test_connect_timeout(self):
        self.fake.connect = lambda p: None
        with self.assertRaises(RuntimeError):
            self.m.connect("fake:a", timeout=1)
        self.assertNotIn("fake:a", self.m._pending)


class DesiredAndLockTest(ManagerTest.__base__):
    def setUp(self):
        ManagerTest.setUp(self)

    tearDown = ManagerTest.tearDown

    def test_desired_follows_user_actions(self):
        self.m.connect("fake:a", timeout=2)
        self.assertEqual(self.m.state.read()["desired"], "fake:a")
        self.m.connect("fake:b", timeout=2)       # a goes down via exclusivity
        self.assertEqual(self.m.state.read()["desired"], "fake:b")
        self.m.disconnect("fake:b", timeout=2)
        self.assertIsNone(self.m.state.read()["desired"])

    def test_oplock_reentrant_and_exclusive_between_instances(self):
        a, b = OpLock(), OpLock()
        with a:
            with a:
                self.assertFalse(b.acquire(timeout=0.3))
        self.assertTrue(b.acquire(timeout=1))
        b.release()


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(BACKENDS)
        BACKENDS.clear()
        self.fake = BACKENDS["fake"] = FakeBackend()
        d = Path(tempfile.mkdtemp())
        self.m = Manager(ProfileStore(d / "profiles.json"), StateFile(d / "state.json"))
        self.m.discover()
        self.ks = {"on": False, "applied": 0}
        self.m.killswitch_status = lambda: (self.ks["on"], [])
        def apply(on=None):
            self.ks["on"] = True if on is None else on
            self.ks["applied"] += 1
            return "ok"
        self.m.apply_killswitch = apply
        self.a = agent_mod.Agent(self.m)
        self.a.next_discover = float("inf")
        agent_mod.probe = lambda timeout=6: True

    def tearDown(self):
        BACKENDS.clear()
        BACKENDS.update(self.saved)

    def test_reconnects_dropped_desired_vpn(self):
        self.m.connect("fake:a", timeout=2)
        self.fake.up.clear()                      # tunnel died behind our back
        self.a.tick()
        self.assertIn("fake:a", self.fake.up)

    def test_no_reconnect_after_user_disconnect(self):
        self.m.connect("fake:a", timeout=2)
        self.m.disconnect("fake:a", timeout=2)
        self.a.tick()
        self.assertEqual(self.fake.up, set())

    def test_backoff_on_failure(self):
        self.m.state.update(desired="fake:a")
        self.fake.connect = lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
        self.a.tick()
        self.assertGreater(self.a.next_try, 0)
        self.assertEqual(self.a.backoff, agent_mod.BACKOFF_MIN * 2)

    def test_killswitch_restored_when_missing(self):
        self.a.tick()
        self.assertTrue(self.ks["on"])
        self.ks["on"] = False                     # somebody flushed nftables
        self.a.next_ks = 0
        self.a.tick()
        self.assertTrue(self.ks["on"])
        self.assertEqual(self.ks["applied"], 2)

    def test_probe_failures_trigger_reconnect(self):
        self.m.connect("fake:a", timeout=2)
        agent_mod.probe = lambda timeout=6: False
        for _ in range(agent_mod.PROBE_FAILS_TO_RECONNECT):
            self.a.next_probe = 0
            self.a.tick()
        self.assertIn(("down", "fake:a"), self.fake.calls)
        self.assertIn("fake:a", self.fake.up)


class HappStatesTest(unittest.TestCase):
    """Several Happ VPNs = snapshots of Happ's state; selection keys rewritten in Happ.conf."""

    def setUp(self):
        from vpntray.backends import happ
        self.h = happ
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (happ.HAPP_CONF, happ.STATES_DIR)
        happ.HAPP_CONF = self.tmp / "Happ.conf"
        happ.STATES_DIR = self.tmp / "states"
        happ.HAPP_CONF.write_text("[General]\nbld=1\n[Preferences]\nAdvancedSettings\\startMinimized=true\n"
                                  "lastServer=1\nlastServerGuid=g-alpha\nlastServerName=Alpha\n"
                                  "windowX=5\n[Vacancy]\nx=1\n")
        for slug, guid, name in (("default", "g-alpha", "Alpha"), ("beta", "g-beta", "Beta")):
            d = happ.STATES_DIR / slug
            d.mkdir(parents=True)
            (d / "subs.db").write_bytes(b"db-" + slug.encode())
            (d / "selection.ini").write_text(f"lastServer=2\nlastServerGuid={guid}\nlastServerName={name}\n"
                                             f"lastServerHost={slug}.example\nlastServerPort=8390\n")

    def tearDown(self):
        self.h.HAPP_CONF, self.h.STATES_DIR = self._saved

    def test_selection_rewrite_keeps_other_settings(self):
        self.h._write_conf_selection((self.h.STATES_DIR / "beta" / "selection.ini").read_text().splitlines())
        text = self.h.HAPP_CONF.read_text()
        conf = read_happ_conf(self.h.HAPP_CONF)
        self.assertEqual(conf["lastServerGuid"], "g-beta")
        self.assertEqual(conf["lastServerName"], "Beta")
        self.assertEqual(text.count("lastServerGuid="), 1)
        for keep in ("bld=1", "startMinimized=true", "windowX=5", "[Vacancy]"):
            self.assertIn(keep, text)
        # selection keys stay in [Preferences]
        self.assertLess(text.index("[Preferences]"), text.index("lastServerGuid="))
        self.assertLess(text.index("lastServerGuid="), text.index("[Vacancy]"))

    def test_active_state_follows_happ_conf(self):
        self.assertEqual(self.h.list_states(), ["beta", "default"])
        self.assertEqual(self.h.active_state(), "default")
        self.h._write_conf_selection(["lastServerGuid=g-beta"])
        self.assertEqual(self.h.active_state(), "beta")

    def test_discover_one_profile_per_snapshot(self):
        ps = {p.id: p for p in self.h.HappBackend().discover()}
        self.assertEqual(set(ps), {"happ:default", "happ:beta"})
        self.assertEqual(ps["happ:beta"].params["state"], "beta")
        self.assertIn("Beta", ps["happ:beta"].name)

    def test_inactive_snapshot_is_disconnected(self):
        b = self.h.HappBackend()
        p = [x for x in b.discover() if x.id == "happ:beta"][0]
        self.assertFalse(b._is_active(p))
        st = b.status(p)
        self.assertEqual(st.state, State.DISCONNECTED)
        self.assertEqual(st.server_host, "beta.example")   # from its own snapshot, not Happ.conf

    def test_import_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.h.import_link("Bad Name", "happ://x")
        with self.assertRaises(ValueError):
            self.h.import_link("ok", "vless://not-a-subscription")


class HelpersTest(unittest.TestCase):
    def test_split_hostport(self):
        self.assertEqual(split_hostport("203.0.113.7:51820"), ("203.0.113.7", 51820))
        self.assertEqual(split_hostport("[2001:db8::1]:443"), ("2001:db8::1", 443))
        self.assertEqual(split_hostport("vpn.example.com"), ("vpn.example.com", None))
        self.assertEqual(split_hostport(""), ("", None))

    def test_parse_vpn_data(self):
        d = parse_vpn_data("remote = vpn.example.com:1194, dev = tun, ta-dir = 1")
        self.assertEqual(d["remote"], "vpn.example.com:1194")
        self.assertEqual(d["dev"], "tun")

    def test_read_happ_conf(self):
        f = Path(tempfile.mkdtemp()) / "Happ.conf"
        f.write_text('[General]\nbld="x"\n[Preferences]\nlastServerHost=vpn.example.org\n'
                     'lastServerName=Example\nlastServerPort=8390\n')
        c = read_happ_conf(f)
        self.assertEqual((c["lastServerHost"], c["lastServerPort"]), ("vpn.example.org", "8390"))
        self.assertEqual(read_happ_conf(Path("/nonexistent")), {})

    def test_human(self):
        self.assertEqual(human_bytes(512), "512 Б")
        self.assertEqual(human_bytes(1536), "1.5 КБ")

    def test_connected_subnets_skips_vpn_ifaces(self):
        nets = core.connected_subnets()
        self.assertTrue(nets)
        self.assertTrue(all("/" in n for n in nets))

    def test_resolve_ip_passthrough(self):
        self.assertEqual(core.resolve("1.2.3.4"), "1.2.3.4")


if __name__ == "__main__":
    unittest.main()
