"""Exercise the installed Timeline manager without a running HA dependency."""
import ast
import asyncio
import copy
import logging
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


class ManualTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        source = Path(__file__).resolve().parents[1] / "custom_components/thermostat_timeline/__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        manager = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AutoApplyManager")
        merge_pauses = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_preserve_advances")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), merge_pauses, manager], type_ignores=[])
        self.globals = {
            "asyncio": asyncio, "math": math, "timedelta": timedelta, "DOMAIN": "thermostat_timeline",
            "_norm_instance_id": lambda value: value or "default", "callback": lambda fn: fn,
            "_LOGGER": logging.getLogger("test"), "_f_to_c": lambda f: (f - 32) * 5 / 9,
            "_c_to_f": lambda c: c * 9 / 5 + 32, "UnitOfTemperature": SimpleNamespace(FAHRENHEIT="°F"),
            "time": SimpleNamespace(time=lambda: self.now.timestamp()),
            "dt_util": SimpleNamespace(now=lambda: self.now, utcnow=lambda: self.now.astimezone(timezone.utc),
                                       as_utc=lambda dt: dt.astimezone(timezone.utc),
                                       utc_from_timestamp=lambda ts: datetime.fromtimestamp(ts, timezone.utc)),
        }
        exec(compile(ast.fix_missing_locations(module), "installed_timeline_manager", "exec"), self.globals)
        self.Manager = self.globals["AutoApplyManager"]
        self.a, self.b = "climate.study", "climate.bedroom"
        self.schedules = {eid: {"defaultTemp": 15, "blocks": [
            {"startMin": 405, "endMin": 480, "temp": 19},
            {"startMin": 1020, "endMin": 1260, "temp": 19},
        ]} for eid in (self.a, self.b)}
        self.settings = {"auto_apply_enabled": True, "storage_temp_unit": "C", "room_pause": {}}
        self.states = {eid: SimpleNamespace(state="heat", attributes={"temperature": 15, "hvac_modes": ["heat", "off"]}) for eid in self.schedules}
        self.calls, self.saved = [], None

        async def call(domain, service, data, blocking=False, context=None):
            self.calls.append((domain, service, copy.deepcopy(data)))
            if domain == "thermostat_timeline":
                self.assertFalse(data["activate"])
                for eid, hold in data["room_pause_updates"].items():
                    if hold is None:
                        self.settings["room_pause"].pop(eid, None)
                    else:
                        self.settings["room_pause"][eid] = copy.deepcopy(hold)
                self.saved = copy.deepcopy(self.settings)
            elif domain == "climate" and service == "set_temperature":
                self.states[data["entity_id"]].attributes["temperature"] = data["temperature"]

        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get),
                                    services=SimpleNamespace(async_call=call),
                                    config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
                                    data={"thermostat_timeline": {"active_instance_id": "default"}})
        self.mgr = self.make_manager()

    def make_manager(self):
        mgr = self.Manager(self.hass)
        mgr._get_data = lambda: (self.schedules, self.settings)
        mgr._effective_blocks_today = lambda row, settings: row["blocks"]
        mgr._desired_for = lambda eid, schedules, settings, minute: 15
        mgr._owd_mgr = lambda: None
        return mgr

    async def manual(self, eid, value):
        self.states[eid].attributes["temperature"] = value
        await self.mgr._remember_manual_target(eid, value)

    async def test_manual_hold_survives_both_startup_apply_paths(self):
        await self.manual(self.a, 20)
        hold = self.settings["room_pause"][self.a]
        self.assertEqual(hold["until_ms"], self.now.replace(hour=17).timestamp() * 1000)
        self.assertEqual(hold["target_c"], 20)
        self.settings = copy.deepcopy(self.saved)  # Load the persisted settings after restart.
        restored = self.make_manager()
        self.calls.clear()
        await restored._maybe_apply_now(force=True)
        await restored._maybe_apply_now(force=True, reconcile=True)
        await restored.async_apply_entity_now(self.a, reconcile=True)  # Late entity recovery.
        self.assertEqual(self.states[self.a].attributes["temperature"], 20)
        self.assertFalse(any(domain == "climate" for domain, _, _ in self.calls))

    async def test_native_room_resume_releases_only_expired_room(self):
        await self.manual(self.a, 20)
        self.now = self.now.replace(hour=17)
        self.mgr._next_room_resume = self.a
        self.mgr._next_room_resume_at = self.now
        async def noop(*args, **kwargs):
            pass
        self.mgr._maybe_control_boiler = noop
        self.mgr._schedule_next = noop
        self.calls.clear()
        await self.mgr._timer_fire()
        climate_calls = [(s, d) for domain, s, d in self.calls if domain == "climate"]
        self.assertEqual(climate_calls, [("set_temperature", {"entity_id": self.a, "temperature": 15.0})])
        self.assertEqual(self.states[self.b].attributes["temperature"], 15)

    async def test_restart_after_missed_boundary_applies_schedule(self):
        await self.manual(self.a, 20)
        self.now = self.now.replace(hour=18)
        restored = self.make_manager()
        await restored._maybe_apply_now(force=True, reconcile=True)
        self.assertEqual(self.states[self.a].attributes["temperature"], 15)

    async def test_schedule_writes_do_not_create_holds_and_cancel_owned_hold(self):
        await self.manual(self.a, 15)
        self.assertEqual(self.settings["room_pause"], {})
        await self.manual(self.a, 20)
        await self.manual(self.a, 15)
        self.assertEqual(self.settings["room_pause"], {})

    async def test_independent_manual_changes_preserve_both_rooms(self):
        self.states[self.a].attributes["temperature"] = 20
        self.states[self.b].attributes["temperature"] = 21
        await asyncio.gather(self.mgr._remember_manual_target(self.a, 20), self.mgr._remember_manual_target(self.b, 21))
        self.assertEqual(set(self.saved["room_pause"]), {self.a, self.b})

    async def test_existing_user_pause_and_disabled_room_are_preserved(self):
        self.settings["room_pause"][self.a] = {"indef": True}
        await self.manual(self.a, 20)
        self.assertEqual(self.settings["room_pause"][self.a], {"indef": True})
        self.settings["apply_enabled"] = {self.b: False}
        await self.manual(self.b, 20)
        self.assertNotIn(self.b, self.settings["room_pause"])

    async def test_superseded_events_do_not_store_stale_targets(self):
        self.states[self.a].attributes["temperature"] = 21
        await self.mgr._remember_manual_target(self.a, 20)
        self.assertEqual(self.settings["room_pause"], {})

    async def test_target_watcher_captures_ha_and_dial_handoff_changes(self):
        listeners, tasks = [], []
        def track(hass, entities, listener):
            listeners.append(listener)
            return lambda: None
        self.globals["async_track_state_change_event"] = track
        self.hass.async_create_task = lambda coro: tasks.append(asyncio.create_task(coro))
        self.mgr._reset_target_recovery_watch()
        self.mgr._reset_target_recovery_watch()  # A pause/settings save keeps the listener attached.
        self.assertEqual(len(listeners), 1)
        old = SimpleNamespace(attributes={"temperature": 15})
        self.states[self.a].attributes["temperature"] = 20
        listeners[0](SimpleNamespace(data={"entity_id": self.a, "old_state": old, "new_state": self.states[self.a]}))
        await asyncio.gather(*tasks)
        self.assertEqual(self.saved["room_pause"][self.a]["target_c"], 20)

    async def test_midnight_boundary_and_fahrenheit(self):
        self.now = self.now.replace(hour=23)
        await self.manual(self.a, 20)
        tomorrow = (self.now + timedelta(days=1)).replace(hour=0)
        self.assertEqual(self.settings["room_pause"][self.a]["until_ms"], tomorrow.timestamp() * 1000)
        self.hass.config.units.temperature_unit = "°F"
        await self.manual(self.a, 68)
        self.assertEqual(self.settings["room_pause"][self.a]["target_c"], 20)


class AdvanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = ManualTargetTests.setUp

    def make_manager(self):
        mgr = self.Manager(self.hass)
        mgr._get_data = lambda: (self.schedules, self.settings)
        return mgr

    async def test_advance_all_with_warmer_block_and_cooler_default(self):
        self.schedules[self.b]["blocks"] = [{"startMin": 0, "endMin": 720, "temp": 21}]
        self.states[self.b].attributes["temperature"] = 21
        result = await self.mgr.async_advance(None, None)
        self.assertEqual(result[self.a]["status"], "advanced")
        self.assertEqual(result[self.a]["until_ms"], self.now.replace(hour=17).timestamp() * 1000)
        self.assertEqual(result[self.b]["status"], "advanced")
        self.assertEqual(result[self.b]["until_ms"], self.now.replace(hour=12).timestamp() * 1000)
        self.assertEqual(self.states[self.a].attributes["temperature"], 19)
        self.assertEqual(self.states[self.b].attributes["temperature"], 15)
        self.calls.clear()
        result = await self.mgr.async_advance(None, None)
        self.assertTrue(all(room["status"] == "already_advanced" for room in result.values()))
        self.assertEqual(self.calls, [])

    async def test_midnight_weekday_and_equal_boundaries(self):
        self.now = self.now.replace(hour=23)
        self.settings["weekdays_enabled"] = True
        self.schedules[self.a]["weekly"] = {"days": {
            "mon": [{"startMin": 1370, "endMin": 1400, "temp": 15}],
            "tue": [{"startMin": 90, "endMin": 480, "temp": 20}],
        }}
        result = (await self.mgr.async_advance(self.a, None))[self.a]
        tuesday = (self.now + timedelta(days=1)).replace(hour=1, minute=30)
        self.assertEqual(result["target_c"], 20)
        self.assertEqual(result["until_ms"], tuesday.timestamp() * 1000)

    async def test_spring_clock_change_uses_first_real_minute(self):
        self.now = datetime(2026, 3, 29, 0, 59, tzinfo=ZoneInfo("Europe/London"))
        self.schedules[self.a]["blocks"] = [{"startMin": 90, "endMin": 180, "temp": 20}]
        result = (await self.mgr.async_advance(self.a, None))[self.a]
        self.assertEqual(result["until_ms"], (self.now + timedelta(minutes=1)).timestamp() * 1000)

    async def test_advance_survives_restart_and_resume_returns_to_current_target(self):
        await self.mgr.async_advance(self.a, None)
        self.settings = copy.deepcopy(self.saved)
        restarted = self.make_manager()
        await restarted._maybe_apply_now(force=True, reconcile=True)
        await restarted.async_apply_entity_now(self.a, reconcile=True)
        self.assertEqual(self.states[self.a].attributes["temperature"], 19)
        self.calls.clear()
        await restarted.async_cancel_advance(self.a, None)
        self.assertEqual(self.states[self.a].attributes["temperature"], 15)
        self.assertNotIn(self.a, self.settings["room_pause"])
        self.assertEqual([service for _, service, _ in self.calls], ["set_temperature", "set_store"])

    async def test_manual_dial_change_replaces_advance(self):
        await self.mgr.async_advance(self.a, None)
        await self.mgr._remember_manual_target(self.a, 19)
        self.assertEqual(self.settings["room_pause"][self.a]["source"], "advance")
        self.states[self.a].attributes["temperature"] = 20
        await self.mgr._remember_manual_target(self.a, 20)
        self.assertEqual(self.settings["room_pause"][self.a]["source"], "manual_target")
        self.assertEqual(self.settings["room_pause"][self.a]["target_c"], 20)

    async def test_respects_pause_disabled_rooms_and_windows(self):
        self.settings["room_pause"][self.a] = {"indef": True}
        self.settings["apply_enabled"] = {self.b: False}
        result = await self.mgr.async_advance(None, None)
        self.assertEqual(result[self.a]["status"], "room_paused")
        self.assertEqual(result[self.b]["status"], "schedule_disabled")
        self.settings["room_pause"].clear()
        self.states[self.a].attributes["window_manager"] = {"window_state": "on"}
        result = await self.mgr.async_advance(self.a, None)
        self.assertEqual(result[self.a]["status"], "window_open")
        self.assertFalse(self.calls)

    async def test_failed_write_restores_previous_manual_hold(self):
        self.states[self.a].attributes["temperature"] = 20
        await self.mgr._remember_manual_target(self.a, 20)
        original_hold = self.settings["room_pause"][self.a]

        async def fail(*args, **kwargs):
            return False

        self.mgr._apply_setpoint = fail
        result = await self.mgr.async_advance(self.a, None)
        self.assertEqual(result[self.a]["status"], "apply_failed")
        self.assertEqual(self.settings["room_pause"][self.a], original_hold)

    async def test_stale_dashboard_save_cannot_remove_or_resurrect_an_advance(self):
        await self.mgr.async_advance(self.a, None)
        merge_pauses = self.globals["_preserve_advances"]
        active = self.settings["room_pause"]
        stale = {self.b: {"indef": True}}
        self.assertEqual(merge_pauses(active, stale), {**active, **stale})
        self.assertEqual(merge_pauses(stale, {**stale, **active}), stale)

    async def test_reaching_the_change_resumes_the_schedule(self):
        await self.mgr.async_advance(self.a, None)
        self.now = self.now.replace(hour=17)
        self.mgr._next_room_resume = self.a
        self.mgr._next_room_resume_at = self.now

        async def noop(*args, **kwargs):
            pass

        self.mgr._maybe_control_boiler = noop
        self.mgr._schedule_next = noop
        await self.mgr._timer_fire()
        self.assertEqual(self.states[self.a].attributes["temperature"], 19)
        self.now = self.now.replace(hour=21)
        await self.mgr._maybe_apply_now(boundary_only=True)
        self.assertEqual(self.states[self.a].attributes["temperature"], 15)


if __name__ == "__main__":
    unittest.main()
