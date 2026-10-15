import io
import json
from pathlib import Path
import tempfile
import unittest

from river_valley import Action, ActionError, World, replay
from river_valley.core import digest


class WorldTests(unittest.TestCase):
    def test_population_and_private_observation(self):
        world = World()
        self.assertEqual(len(world.state["people"]), 121)
        self.assertEqual(len(world.state["households"]), 40)
        observation = world.observe("player")
        self.assertTrue(observation["offers"])
        self.assertTrue(all(int(offer["household"][1:]) < 20 for offer in observation["offers"]))
        observation["home"]["grain"] = 0
        self.assertEqual(world.state["households"]["h00"]["grain"], 397)
        self.assertEqual(world.state["households"]["h00"]["seed_grain"], 3)

    def test_transfer_and_trade_conserve_resources(self):
        for kind in ("transfer", "trade"):
            with self.subTest(kind=kind):
                world = World()
                world.act(Action("player", kind, "h01", 10))
                expected = (387, 307) if kind == "transfer" else (407, 287)
                self.assertEqual(tuple(world.state["households"][h]["grain"] for h in ("h00", "h01")), expected)
                expected_coins = (200, 200) if kind == "transfer" else (190, 210)
                self.assertEqual(tuple(world.state["households"][h]["coins"] for h in ("h00", "h01")), expected_coins)
                world.assert_invariants()

    def test_invalid_actions_are_atomic(self):
        invalid = [Action("player", "transfer", "h01", 401),
                   Action("player", "transfer", "h20", 1),
                   Action("player", "transfer", "h00", 1),
                   Action("player", "trade", "h01", 121),
                   Action("player", "trade", "h01", -1),
                   Action("player", "trade", "h01", True),
                   Action("ghost", "produce"), Action("player", "unknown"),
                   Action("player", "divert", amount=101),
                   Action("player", "move", "upstream")]
        for action in invalid:
            with self.subTest(action=action):
                world = World()
                before, chain, count = world.snapshot(), world.chain, world.sequence
                with self.assertRaises(ActionError):
                    world.act(action)
                self.assertEqual(world.state, before)
                self.assertEqual((world.chain, world.sequence), (chain, count))

    def test_sowing_requires_seed_reserve_and_failure_is_atomic(self):
        world = World()
        # Move the reserve into edible inventory without changing total grain.
        home = world.state["households"]["h00"]
        home["grain"] += 3
        home["seed_grain"] -= 3
        world.state["ledger"]["initial_edible_grain"] += 3
        world.state["ledger"]["initial_seed_grain"] -= 3
        world.assert_invariants()
        before, chain, count = world.snapshot(), world.chain, world.sequence
        with self.assertRaises(ActionError):
            world.act(Action("p000", "produce"))
        self.assertEqual(world.state, before)
        self.assertEqual((world.chain, world.sequence), (chain, count))

    def test_seed_reserve_survives_multiyear_drought_and_restores_sowing(self):
        world = World(42, "drought")
        world.advance(5 * 360)
        homes = world.state["households"]
        self.assertEqual(sum(home["grain"] for home in homes.values()), 0)
        self.assertTrue(all(home["seed_grain"] == 3 for home in homes.values()))
        world.set_rainfall(1000)
        for index in range(40):
            world.act(Action(f"p{index * 3:03d}", "produce"))
        self.assertTrue(all(home["planted"] for home in homes.values()))
        self.assertEqual(sum(home["seed_grain"] for home in homes.values()), 0)
        world.assert_invariants()

    def test_npc_and_player_share_rules_and_daily_budget(self):
        world = World()
        world.act(Action("player", "produce"))
        with self.assertRaises(ActionError):
            world.act(Action("player", "move", "downstream"))
        with self.assertRaises(ActionError):
            world.act(Action("p000", "produce"))  # same already-tended land
        world.step()
        world.act(Action("player", "move", "downstream"))
        world.step()
        with self.assertRaises(ActionError):
            world.act(Action("player", "transfer", "h21", 10))
        with self.assertRaises(ActionError):
            world.act(Action("player", "divert", amount=50))
        world.act(Action("player", "move", "upstream"))
        world.assert_invariants()

    def test_npc_responds_to_local_offers(self):
        world = World()
        world.act(Action("player", "transfer", "h01", 350))
        coins_before = world.state["households"]["h00"]["coins"]
        world.step()
        self.assertLess(world.state["households"]["h00"]["coins"], coins_before)
        trader = [event for event in world.events if event["actor"] == "p001"]
        self.assertEqual(trader[0]["details"]["kind"], "trade")
        world.assert_invariants()

    def test_determinism_and_seed_variation(self):
        first, second, other = World(7), World(7), World(8)
        for world in (first, second, other):
            world.act(Action("player", "divert", amount=40))
            world.advance(100)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.chain, second.chain)
        self.assertNotEqual(digest(first.state), digest(other.state))

    def test_rain_and_diversion_change_real_harvest(self):
        baseline, dry, diverted = World(), World(), World()
        dry.set_rainfall(10)
        diverted.act(Action("player", "divert", amount=100))
        for world in (baseline, dry, diverted):
            world.advance(90)
            world.assert_invariants()
        normal = baseline.summary()
        self.assertGreater(normal["harvest"], dry.summary()["harvest"])
        self.assertGreater(normal["villages"]["downstream"]["harvest"],
                           diverted.summary()["villages"]["downstream"]["harvest"])
        self.assertGreater(diverted.summary()["villages"]["upstream"]["harvest"],
                           normal["villages"]["upstream"]["harvest"])

    def test_transfer_has_subsequent_effect_and_source(self):
        baseline, intervention = World(), World()
        event_id = intervention.act(Action("player", "transfer", "h01", 100))
        intervention.advance(20)
        baseline.advance(20)
        self.assertNotEqual(intervention.state["households"]["h00"]["grain"],
                            baseline.state["households"]["h00"]["grain"])
        # Action event carries both ownership deltas, not a narrative assertion.
        sink = io.StringIO()
        traced = World(event_sink=sink)
        traced.act(Action("player", "transfer", "h01", 100))
        event = json.loads(sink.getvalue().splitlines()[event_id])
        grain = [c for c in event["changes"] if c["path"][-1] == "grain"]
        self.assertEqual(sum(c["after"] - c["before"] for c in grain), 0)

    def test_full_log_replay_and_day_seek(self):
        sink = io.StringIO()
        world = World(9, "drought", sink)
        initial = world.snapshot()
        world.act(Action("player", "divert", amount=100))
        world.set_rainfall(35)
        world.advance(20)
        at_twenty = world.snapshot()
        world.advance(80)
        reconstructed = replay(io.StringIO(sink.getvalue()))
        self.assertEqual(reconstructed.state, world.state)
        self.assertEqual(reconstructed.chain, world.chain)
        self.assertEqual(replay(io.StringIO(sink.getvalue()), 20).state, at_twenty)
        self.assertEqual(replay(io.StringIO(sink.getvalue()), 0).state, initial)
        with self.assertRaises(ValueError):
            replay(io.StringIO(sink.getvalue()), 101)
        events = [json.loads(line) for line in sink.getvalue().splitlines()]
        hydro = next(e for e in events if e["kind"] == "hydrology")
        self.assertEqual(hydro["details"]["input_events"], dict(rainfall=2, diversion=1))
        growth = next(e for e in events if e["kind"] == "agriculture_and_consumption")
        self.assertEqual(growth["cause"], hydro["id"])
        for source in growth["details"]["growth_sources"].values():
            self.assertEqual(events[source["labor_event"]]["details"]["kind"], "produce")

    def test_replay_rejects_tampering_and_missing_events(self):
        sink = io.StringIO()
        world = World(event_sink=sink)
        world.advance(2)
        lines = sink.getvalue().splitlines()
        event = json.loads(lines[1])
        event["details"]["amount"] = 100
        changed = lines[:]
        changed[1] = json.dumps(event)
        with self.assertRaises(ValueError):
            replay(changed)
        with self.assertRaises(ValueError):
            replay(lines[:1] + lines[2:])
        with self.assertRaises(ValueError):
            replay([])

    def test_save_resume_preserves_future_and_log_chain(self):
        original_log = io.StringIO()
        continuous = World(23, "drought", original_log)
        continuous.set_rainfall(70)
        continuous.advance(25)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            continuous.save(path)
            continued_log = io.StringIO()
            restored = World.load(path, continued_log)
        prefix = original_log.getvalue()
        continuous.advance(100)
        restored.advance(100)
        self.assertEqual(restored.state, continuous.state)
        self.assertEqual(restored.chain, continuous.chain)
        self.assertEqual(replay(io.StringIO(prefix + continued_log.getvalue())).state, restored.state)

    def test_legacy_snapshot_upgrade_preserves_total_grain(self):
        state = World().snapshot()
        for home in state["households"].values():
            home["grain"] += home.pop("seed_grain")
        state["version"] = 1
        ledger = state["ledger"]
        for key in ("initial_edible_grain", "initial_seed_grain", "harvest_food",
                    "harvest_seed", "legacy_seed_used", "seed_transferred"):
            ledger.pop(key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            genesis = dict(id=0, day=0, kind="genesis", actor=None, cause=None,
                           details={}, state=state, previous_hash="")
            genesis["hash"] = digest(genesis)
            path.write_text(json.dumps(dict(state=state, sequence=1, chain=genesis["hash"])))
            prefix = json.dumps(genesis) + "\n"
            segment = io.StringIO()
            restored = World.load(path, segment)
            self.assertEqual(sum(h["grain"] + h["seed_grain"]
                                 for h in restored.state["households"].values()), 12100)
            restored.advance(1)
        self.assertEqual(restored.state["version"], 2)
        restored.assert_invariants()
        self.assertEqual(replay(io.StringIO(prefix + segment.getvalue())).state, restored.state)

    def test_seasons_and_drought_window(self):
        normal, dry = World(42), World(42, "drought")
        for day in (0, 719, 720, 1799, 1800):
            normal.state["day"] = dry.state["day"] = day
            expected = normal._weather() // 4 if 720 <= day < 1800 else normal._weather()
            self.assertEqual(dry._weather(), expected)
        world = World()
        world.advance(90)
        self.assertEqual(world.state["season"], "summer")
        with self.assertRaises(ActionError):
            world.act(Action("player", "produce"))

    def test_years_of_autonomy_keep_ledgers_balanced(self):
        for scenario in ("normal", "drought"):
            with self.subTest(scenario=scenario):
                world = World(42, scenario)
                world.advance(6 * 360)
                world.assert_invariants()
                self.assertGreater(world.state["ledger"]["harvest"], 0)
                self.assertGreater(world.state["ledger"]["consumed"], 0)
                self.assertGreater(world.sequence, 6 * 360)
                if scenario == "drought":
                    self.assertGreater(world.summary()["hunger_days"], 0)
                self.assertEqual(sum(h["grain"] + h["seed_grain"]
                                     for h in world.state["households"].values()),
                                 world.state["ledger"]["initial_grain"]
                                 + world.state["ledger"]["harvest"]
                                 - world.state["ledger"]["consumed"]
                                 - world.state["ledger"]["legacy_seed_used"]
                                 - world.state["ledger"]["seed_used"])


if __name__ == "__main__":
    unittest.main()
