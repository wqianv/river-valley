"""Daily simulation. No UI, wall clock, network, or third-party dependencies."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import random


DAYS_PER_YEAR = 360
VILLAGES = ("upstream", "downstream")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Action:
    actor: str
    kind: str
    target: str | None = None
    amount: int = 0


class ActionError(ValueError):
    pass


def get_path(state, path):
    for key in path:
        state = state[key]
    return state


def set_path(state, path, value):
    parent = get_path(state, path[:-1])
    parent[path[-1]] = deepcopy(value)


class World:
    """Food belongs to households, with edible and seed grain kept separately.

    Crop biomass is in thousandths of a grain unit. A person consumes one grain
    unit/day. Water refreshes daily; its unused portion exits via river/canals.
    Actors get one successful action/day. Failed actions never mutate state.
    """

    def __init__(self, seed=42, scenario="normal", event_sink=None):
        if type(seed) is not int or scenario not in ("normal", "drought"):
            raise ValueError("integer seed and normal/drought scenario required")
        rng = random.Random(seed)
        households, people = {}, {}
        initial_seed = 0
        for index in range(40):
            hid = f"h{index:02d}"
            village = VILLAGES[index // 20]
            households[hid] = dict(village=village, grain=297, seed_grain=3, coins=200,
                                   land=3, soil=rng.randint(900, 1100),
                                   crop=0, planted=False, labor=0, labor_source=-1, hunger=0,
                                   harvested=0, consumed=0, donated=0)
            initial_seed += 3
            for slot, role in enumerate(("farmer", "trader", "carer")):
                pid = f"p{index * 3 + slot:03d}"
                people[pid] = dict(household=hid, location=village, role=role,
                                   last_action=-1, need=1)
        people["player"] = dict(household="h00", location="upstream",
                                 role="player", last_action=-1, need=1)
        # Starting food is 100 days/person, including the additional player.
        households["h00"]["grain"] += 100
        self.state = dict(version=2, seed=seed, scenario=scenario, day=0,
                          season="spring", households=households, people=people,
                          river=dict(rainfall=0, inflow=0, diversion=0,
                                     upstream=0, downstream=0, outlet=0,
                                     canal_loss=0, drinking=0, irrigation=0,
                                     unmet_drinking=0),
                          climate=dict(rainfall_override=None),
                          sources=dict(rainfall=None, diversion=None),
                          ledger=dict(initial_grain=12100,
                                      initial_edible_grain=12100 - initial_seed,
                                      initial_seed_grain=initial_seed,
                                      initial_coins=8000,
                                      harvest=0, harvest_food=0, harvest_seed=0,
                                      consumed=0, seed_used=0, legacy_seed_used=0,
                                      seed_transferred=0,
                                      water_in=0, water_drunk=0, water_irrigated=0,
                                      water_out=0, water_canal=0),
                          totals=dict(hunger_days=0, thirst_days=0))
        self.events = deque(maxlen=500)
        self.event_sink = event_sink
        self.sequence = 0
        self.chain = ""
        self._write(dict(id=0, day=0, kind="genesis", actor=None, cause=None,
                         details={}, state=deepcopy(self.state)))

    def _write(self, event):
        event["previous_hash"] = self.chain
        event["hash"] = digest(event)
        self.chain = event["hash"]
        self.events.append(event)
        if self.event_sink is not None:
            self.event_sink.write(canonical(event) + "\n")
        self.sequence = event["id"] + 1
        return event["id"]

    def _emit(self, kind, updates, actor=None, cause=None, details=None):
        changes = []
        seen = set()
        for path, after in updates:
            path = tuple(path)
            if path in seen:
                raise ValueError("duplicate update path")
            seen.add(path)
            before = get_path(self.state, path)
            if before != after:
                changes.append(dict(path=list(path), before=deepcopy(before),
                                    after=deepcopy(after)))
        event = dict(id=self.sequence, day=self.state["day"], kind=kind,
                     actor=actor, cause=cause, details=details or {}, changes=changes)
        for change in changes:
            set_path(self.state, change["path"], change["after"])
        return self._write(event)

    def observe(self, actor):
        """Private own stock + public local offers/requests, no remote stocks."""
        if actor not in self.state["people"]:
            raise ActionError("unknown actor")
        person = self.state["people"][actor]
        hid = person["household"]
        offers, requests = [], []
        for other, home in self.state["households"].items():
            if other == hid or home["village"] != person["location"]:
                continue
            if home["grain"] > 180:
                offers.append(dict(household=other, amount=home["grain"] - 180, price=1))
            if home["grain"] < 30:
                requests.append(dict(household=other, amount=30 - home["grain"]))
        return dict(day=self.state["day"], season=self.state["season"],
                    person=deepcopy(person), home=deepcopy(self.state["households"][hid]),
                    offers=offers, requests=requests)

    def act(self, action, reason="player input"):
        """Validate completely, then atomically settle the shared action API."""
        if not isinstance(action, Action):
            raise ActionError("Action required")
        if type(action.amount) is not int or action.actor not in self.state["people"]:
            raise ActionError("unknown actor or noninteger amount")
        person = self.state["people"][action.actor]
        day = self.state["day"]
        if person["last_action"] == day:
            raise ActionError("one action per actor per day")
        hid = person["household"]
        home = self.state["households"][hid]
        updates = []
        if action.kind == "move":
            if action.target not in VILLAGES or action.target == person["location"]:
                raise ActionError("choose the other village")
            updates.append((("people", action.actor, "location"), action.target))
        else:
            if person["location"] != home["village"]:
                raise ActionError("household inventory and land must be locally accessible")
            if action.kind == "produce":
                if day % 180 >= 90 or home["labor"] >= 1:
                    raise ActionError("outside growing season or land already tended")
                if not home["planted"]:
                    if day % 180 >= 30 or home["seed_grain"] < 3:
                        raise ActionError("planting needs 3 reserved seed grain and an open planting window")
                    updates += [(("households", hid, "seed_grain"), home["seed_grain"] - 3),
                                (("households", hid, "planted"), True),
                                (("ledger", "seed_used"), self.state["ledger"]["seed_used"] + 3)]
                updates.append((("households", hid, "labor"), 1))
                updates.append((("households", hid, "labor_source"), self.sequence))
            elif action.kind in ("transfer", "trade"):
                target = self.state["households"].get(action.target)
                if target is None or action.target == hid or target["village"] != person["location"]:
                    raise ActionError("a distinct local household is required")
                if action.amount <= 0:
                    raise ActionError("positive quantity required")
                if action.kind == "transfer":
                    source, dest = home, target
                    source_id, dest_id = hid, action.target
                    if source["grain"] < action.amount:
                        raise ActionError("insufficient grain")
                    updates.append((("households", hid, "donated"), home["donated"] + action.amount))
                else:
                    # Buyer initiates; only publicly offered surplus can be bought.
                    source, dest = target, home
                    source_id, dest_id = action.target, hid
                    if action.amount > max(0, target["grain"] - 180) or home["coins"] < action.amount:
                        raise ActionError("offer unavailable or insufficient coins")
                    updates += [(("households", hid, "coins"), home["coins"] - action.amount),
                                (("households", action.target, "coins"), target["coins"] + action.amount)]
                updates += [(("households", source_id, "grain"), source["grain"] - action.amount),
                            (("households", dest_id, "grain"), dest["grain"] + action.amount)]
            elif action.kind == "divert":
                if person["location"] != "upstream" or not 0 <= action.amount <= 100:
                    raise ActionError("operate the upstream sluice locally, 0..100 percent")
                updates.append((("river", "diversion"), action.amount))
                updates.append((("sources", "diversion"), self.sequence))
            else:
                raise ActionError("unknown action")
        updates.append((("people", action.actor, "last_action"), day))
        return self._emit("action", updates, actor=action.actor,
                          details=dict(kind=action.kind, target=action.target,
                                       amount=action.amount, reason=reason))

    def set_rainfall(self, amount):
        """An exogenous experiment input, never an NPC/player supernatural action."""
        if amount is not None and (type(amount) is not int or not 0 <= amount <= 1000):
            raise ValueError("rainfall must be None or an integer in 0..1000")
        return self._emit("climate_input", [(('climate', 'rainfall_override'), amount),
                                           (('sources', 'rainfall'), self.sequence)],
                          details=dict(source="experimenter", rainfall=amount))

    def _npc_actions(self):
        for pid, person in self.state["people"].items():
            if pid == "player" or person["last_action"] == self.state["day"]:
                continue
            view = self.observe(pid)
            home = view["home"]
            action = None
            reason = ""
            if person["location"] != home["village"]:
                action, reason = Action(pid, "move", home["village"]), "return to household supplies"
            elif person["role"] == "farmer" and self.state["day"] % 180 < 90:
                if home["labor"] == 0 and (home["planted"] or
                                          (self.state["day"] % 180 < 30 and home["seed_grain"] >= 3)):
                    action, reason = Action(pid, "produce"), "local land needs seasonal labor"
            elif person["role"] == "trader" and home["grain"] < 90 and home["coins"] > 0:
                if view["offers"]:
                    offer = max(view["offers"], key=lambda x: x["amount"])
                    amount = min(30, offer["amount"], home["coins"])
                    action, reason = Action(pid, "trade", offer["household"], amount), "low own stock, local offer"
                elif self.state["day"] % 30 == 0:
                    action, reason = Action(pid, "move", VILLAGES[1 - VILLAGES.index(person["location"])]), "visit neighboring village; no local offer"
            elif person["role"] == "carer" and home["grain"] > 240 and view["requests"]:
                request = view["requests"][0]
                action, reason = Action(pid, "transfer", request["household"],
                                        min(6, request["amount"])), "public local food request and own surplus"
            if action is not None:
                self.act(action, reason)

    def _weather(self):
        day = self.state["day"]
        seasonal = (100, 80, 110, 90)[day % 360 // 90]
        rainfall = seasonal + random.Random(f"{self.state['seed']}:{day}").randint(-20, 20)
        if self.state["scenario"] == "drought" and 2 <= day // 360 < 5:
            rainfall //= 4
        if self.state["climate"]["rainfall_override"] is not None:
            rainfall = self.state["climate"]["rainfall_override"]
        return rainfall

    def step(self):
        self._npc_actions()
        rainfall = self._weather()
        inflow = rainfall * 10
        upstream = inflow * (50 + self.state["river"]["diversion"] * 40 // 100) // 100
        downstream = inflow - upstream
        members = {hid: 0 for hid in self.state["households"]}
        residents = dict.fromkeys(VILLAGES, 0)
        for person in self.state["people"].values():
            members[person["household"]] += person["need"]
            residents[person["location"]] += person["need"]
        irrigation, drank, missing = {}, 0, 0
        for village, supply in zip(VILLAGES, (upstream, downstream)):
            drink = min(supply, residents[village])
            drank += drink
            missing += residents[village] - drink
            demand = sum(h["land"] * 7 for h in self.state["households"].values()
                         if h["village"] == village and h["planted"])
            irrigation[village] = min(supply - drink, demand)
        used_irrigation = sum(irrigation.values())
        # Upstream allocation is withdrawn into canals; surplus drains below
        # downstream farmland. It still leaves the basin, never disappears.
        up_drink = min(upstream, residents["upstream"])
        canal_loss = upstream - up_drink - irrigation["upstream"]
        outlet = inflow - drank - used_irrigation - canal_loss
        river = self.state["river"]
        values = dict(rainfall=rainfall, inflow=inflow, upstream=upstream,
                      downstream=downstream, drinking=drank, irrigation=used_irrigation,
                      outlet=outlet, canal_loss=canal_loss, unmet_drinking=missing)
        updates = [(("river", key), value) for key, value in values.items()]
        ledger = self.state["ledger"]
        for key, value in dict(water_in=inflow, water_drunk=drank,
                               water_irrigated=used_irrigation, water_out=outlet,
                               water_canal=canal_loss).items():
            updates.append((("ledger", key), ledger[key] + value))
        updates.append((("totals", "thirst_days"), self.state["totals"]["thirst_days"] + missing))
        water_event = self._emit("hydrology", updates,
                                 details=dict(source="rainfall and sluice", rainfall=rainfall,
                                              diversion=river["diversion"], allocations=values,
                                              input_events=deepcopy(self.state["sources"])))
        updates, harvested, harvest_food, harvest_seed = [], 0, 0, 0
        consumed, hungry, seed_transferred = 0, 0, 0
        growth_sources = {}
        harvest_day = self.state["day"] % 180 == 89
        for hid, home in self.state["households"].items():
            village = home["village"]
            demand = sum(h["land"] * 7 for h in self.state["households"].values()
                         if h["village"] == village and h["planted"])
            growth = (8000 * home["land"] // 3 * home["soil"] // 1000 * irrigation[village] // demand
                      if demand and home["planted"] and home["labor"] else 0)
            crop = home["crop"] + growth
            if home["planted"]:
                growth_sources[hid] = dict(growth=growth, labor_event=home["labor_source"],
                                           water_event=water_event)
            grain = home["grain"]
            seed_grain = home["seed_grain"]
            if harvest_day and home["planted"]:
                amount = crop // 1000
                harvested += amount
                seed_amount = min(amount, max(0, 3 - seed_grain))
                seed_grain += seed_amount
                harvest_seed += seed_amount
                harvest_food += amount - seed_amount
                grain += amount - seed_amount
                crop = 0
                updates += [(("households", hid, "planted"), False),
                            (("households", hid, "harvested"), home["harvested"] + amount)]
            # Preserve up to one 3-unit sowing ration before food consumption.
            # Harvest fills the reserve first; edible stock tops it up after a
            # poor harvest, so the daily food debit cannot consume it.
            transfer = min(max(0, 3 - seed_grain), grain)
            seed_grain += transfer
            grain -= transfer
            seed_transferred += transfer
            eaten = min(grain, members[hid])
            shortage = members[hid] - eaten
            consumed += eaten
            hungry += shortage
            updates += [(("households", hid, "grain"), grain - eaten),
                        (("households", hid, "seed_grain"), seed_grain),
                        (("households", hid, "crop"), crop),
                        (("households", hid, "labor"), 0),
                        (("households", hid, "consumed"), home["consumed"] + eaten),
                        (("households", hid, "hunger"), home["hunger"] + shortage)]
        updates += [(("ledger", "harvest"), ledger["harvest"] + harvested),
                    (("ledger", "harvest_food"), ledger["harvest_food"] + harvest_food),
                    (("ledger", "harvest_seed"), ledger["harvest_seed"] + harvest_seed),
                    (("ledger", "consumed"), ledger["consumed"] + consumed),
                    (("ledger", "seed_transferred"), ledger["seed_transferred"] + seed_transferred),
                    (("totals", "hunger_days"), self.state["totals"]["hunger_days"] + hungry)]
        self._emit("agriculture_and_consumption", updates, cause=water_event,
                   details=dict(growth_source="planted land + labor + soil + irrigation",
                                harvest=harvested, harvest_food=harvest_food,
                                harvest_seed=harvest_seed, seed_transferred=seed_transferred,
                                consumed=consumed, unmet_food=hungry,
                                harvest_day=harvest_day, growth_sources=growth_sources))
        day = self.state["day"] + 1
        self._emit("clock", [(('day',), day),
                             (('season',), ("spring", "summer", "autumn", "winter")[day % 360 // 90])])
        self.assert_invariants()

    def advance(self, days):
        if type(days) is not int or days < 0:
            raise ValueError("nonnegative integer days required")
        for _ in range(days):
            self.step()

    def assert_invariants(self):
        ledger = self.state["ledger"]
        homes = self.state["households"].values()
        homes = list(homes)
        for home in homes:
            for key in ("grain", "seed_grain", "coins", "crop", "hunger", "consumed", "harvested"):
                if type(home[key]) is not int or home[key] < 0:
                    raise AssertionError(f"invalid household {key}")
        if sum(h["grain"] for h in homes) != (
                ledger["initial_edible_grain"] + ledger["harvest_food"]
                - ledger["consumed"] - ledger["seed_transferred"]
                - ledger["legacy_seed_used"]):
            raise AssertionError("edible grain conservation violated")
        if sum(h["seed_grain"] for h in homes) != (
                ledger["initial_seed_grain"] + ledger["harvest_seed"]
                + ledger["seed_transferred"] - ledger["seed_used"]):
            raise AssertionError("seed grain conservation violated")
        if sum(h["grain"] + h["seed_grain"] for h in homes) != (
                ledger["initial_grain"] + ledger["harvest"] - ledger["consumed"]
                - ledger["legacy_seed_used"] - ledger["seed_used"]):
            raise AssertionError("total grain conservation violated")
        if sum(h["coins"] for h in homes) != ledger["initial_coins"]:
            raise AssertionError("coin conservation violated")
        if ledger["water_in"] != sum(ledger[k] for k in
                                      ("water_drunk", "water_irrigated", "water_out", "water_canal")):
            raise AssertionError("water conservation violated")
        river = self.state["river"]
        if any(river[k] < 0 for k in river) or river["inflow"] != sum(
                river[k] for k in ("drinking", "irrigation", "outlet", "canal_loss")):
            raise AssertionError("daily water balance violated")

    def snapshot(self):
        return deepcopy(self.state)

    def summary(self):
        return dict(day=self.state["day"], years=self.state["day"] / 360,
                    seed=self.state["seed"], scenario=self.state["scenario"],
                    population=len(self.state["people"]), households=40,
                    grain=sum(h["grain"] for h in self.state["households"].values()),
                    seed_grain=sum(h["seed_grain"] for h in self.state["households"].values()),
                    harvest=self.state["ledger"]["harvest"],
                    consumed=self.state["ledger"]["consumed"],
                    hunger_days=self.state["totals"]["hunger_days"],
                    thirst_days=self.state["totals"]["thirst_days"],
                    villages={v: dict(grain=sum(h["grain"] for h in self.state["households"].values()
                                                if h["village"] == v),
                                      harvest=sum(h["harvested"] for h in self.state["households"].values()
                                                  if h["village"] == v),
                                      hunger_days=sum(h["hunger"] for h in self.state["households"].values()
                                                      if h["village"] == v)) for v in VILLAGES},
                    state_hash=digest(self.state), events=self.sequence)

    def save(self, path):
        with open(path, "w", encoding="utf-8") as file:
            json.dump(dict(state=self.state, sequence=self.sequence, chain=self.chain), file,
                      ensure_ascii=False, sort_keys=True)

    @classmethod
    def load(cls, path, event_sink=None):
        with open(path, encoding="utf-8") as file:
            saved = json.load(file)
        if saved["state"]["version"] not in (1, 2):
            raise ValueError("unsupported save version")
        world = cls.__new__(cls)
        world.state = saved["state"]
        legacy_state = deepcopy(world.state) if world.state.get("version") == 1 else None
        _upgrade_seed_ledger(world.state)
        world.sequence, world.chain = saved["sequence"], saved["chain"]
        world.events = deque(maxlen=500)
        world.event_sink = event_sink
        world.assert_invariants()
        if legacy_state is not None and event_sink is not None:
            world._write(dict(id=world.sequence, day=world.state["day"],
                              kind="schema_upgrade", actor=None, cause=None,
                              details=dict(from_version=1, to_version=2,
                                           previous_state_hash=digest(legacy_state)),
                              state=deepcopy(world.state)))
        return world


def replay(lines, until_day=None):
    """Replay exact recorded deltas, checking chain, sequence and old values.

    Does not invoke NPCs or simulation rules. A full log starts with genesis.
    """
    if until_day is not None and (type(until_day) is not int or until_day < 0):
        raise ValueError("replay day must be a nonnegative integer")
    state, previous, count = None, "", 0
    for line in lines:
        event = json.loads(line)
        claimed = event.pop("hash")
        if event["id"] != count or event["previous_hash"] != previous or digest(event) != claimed:
            raise ValueError("invalid event sequence or hash")
        if count == 0:
            if event["kind"] != "genesis":
                raise ValueError("full replay requires genesis")
            state = event["state"]
        elif event["kind"] == "schema_upgrade":
            if event["day"] != state["day"] or event["details"].get(
                    "previous_state_hash") != digest(state):
                raise ValueError("invalid state migration event")
            state = event["state"]
        else:
            if event["day"] != state["day"]:
                raise ValueError("event day mismatch")
            for change in event["changes"]:
                if get_path(state, change["path"]) != change["before"]:
                    raise ValueError("event prior value mismatch")
                set_path(state, change["path"], change["after"])
        previous = claimed
        count += 1
        if until_day is not None and state["day"] == until_day:
            break
    if state is None:
        raise ValueError("empty log")
    if until_day is not None and state["day"] != until_day:
        raise ValueError("requested day is beyond the log")
    _upgrade_seed_ledger(state)
    world = World.__new__(World)
    world.state, world.sequence, world.chain = state, count, previous
    world.events, world.event_sink = deque(maxlen=500), None
    world.assert_invariants()
    return world


def _upgrade_seed_ledger(state):
    """Migrate v1 snapshots/replays while preserving every grain unit."""
    if state.get("version") == 2:
        return
    if state.get("version") != 1:
        raise ValueError("unsupported state version")
    ledger = state["ledger"]
    initial_seed = 0
    for home in state["households"].values():
        reserve = min(3, home["grain"])
        home["grain"] -= reserve
        home["seed_grain"] = reserve
        initial_seed += reserve
    legacy_seed_used = ledger["seed_used"]
    ledger.update(initial_edible_grain=ledger["initial_grain"] - initial_seed,
                  initial_seed_grain=initial_seed,
                  harvest_food=ledger["harvest"], harvest_seed=0,
                  legacy_seed_used=legacy_seed_used, seed_transferred=0,
                  seed_used=0)
    state["version"] = 2
