"""
Lone Wolf Labs bot v6-F - FINISHER
for Builders - Mech Royale (mechfight/1).

Based on v6, which scored 2.42.

Changes:
- Strongly prefers enemies below 35% HP.
- Stays locked onto a weak target longer.
- Boosts only to finish a weak target.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    """Ground height from the map's terrain.i16."""

    def __init__(self, map_dir: str) -> None:
        self.ok = False
        try:
            base = os.path.join(map_dir, "v2")
            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)["terrain"]
            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])
            data = array.array("h")
            with open(os.path.join(base, str(meta.get("file", "terrain.i16"))), "rb") as f:
                data.fromfile(f, self.nx * self.ny)
            if sys.byteorder == "big":
                data.byteswap()
            self.data = data
            self.ok = True
        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None
        i = max(0, min(self.nx - 1, int(round((x - self.min_x) / self.step))))
        j = max(0, min(self.ny - 1, int(round((y - self.min_y) / self.step))))
        return self.data[j * self.nx + i] / 100.0


class Bot:
    name = "lone-wolf-v6-finisher"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        self.pickup_radius: float = float(loot_cfg.get("pickup_radius", 4.0))
        self.upgrades_cfg: List[Dict[str, Any]] = cfg.get("upgrades", [])
        self.upgrade_priority: Dict[str, int] = {}

        for upgrade in self.upgrades_cfg:
            uid = str(upgrade.get("id", ""))
            effects = upgrade.get("effects", [])
            score = 0

            for effect in effects:
                path = str(effect.get("path", ""))

                if "armor" in uid or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 30
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 22
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 18
                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 12

            self.upgrade_priority[uid] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t: float = -999.0
        self.last_damage_bearing: Optional[float] = None
        self.laser_charging: List[bool] = [False, False]
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))
        self.skip_pickups: Dict[int, float] = {}
        self.loot_mem: Optional[Dict[str, Any]] = None
        self.last_pos: Optional[Tuple[float, float]] = None
        self.stuck_ticks: int = 0

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])

        if not choices:
            return 0

        best_index = 0
        best_score = -10**9

        for index, choice in enumerate(choices):
            uid = str(choice.get("id", ""))
            description = str(choice.get("desc", "")).lower()
            score = self.upgrade_priority.get(uid, 0)

            if "armor" in description or "hp" in description or "regen" in description:
                score += 25
            if "vision" in description or "sensor" in description or "sound" in description:
                score += 15
            if "speed" in description or "energy" in description:
                score += 12

            if score > best_score:
                best_score = score
                best_index = index

        return best_index

    def _choose_zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        current_center = zone.get("center", [0.0, 0.0])
        current_radius = float(zone.get("radius", 500.0))
        next_center = zone.get("next_center", current_center)
        next_radius = float(zone.get("next_radius", current_radius))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                progress = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                progress = 1.0

            center_x = current_center[0] + (next_center[0] - current_center[0]) * progress
            center_y = current_center[1] + (next_center[1] - current_center[1]) * progress
            radius = current_radius + (next_radius - current_radius) * progress
        else:
            center_x, center_y, radius = current_center[0], current_center[1], current_radius

        safe_radius = max(5.0, radius * 0.72)
        delta_x = next_center[0] - center_x
        delta_y = next_center[1] - center_y
        delta_length = math.hypot(delta_x, delta_y)

        if delta_length > 1e-6:
            target_x = center_x + (delta_x / delta_length) * min(delta_length * 0.65, safe_radius * 0.5)
            target_y = center_y + (delta_y / delta_length) * min(delta_length * 0.65, safe_radius * 0.5)
        else:
            target_x, target_y = center_x, center_y

        current_distance = math.hypot(x - center_x, y - center_y)
        next_distance = math.hypot(x - next_center[0], y - next_center[1])

        if current_distance < safe_radius and next_distance > next_radius * 0.8:
            return float(next_center[0]), float(next_center[1])

        return float(target_x), float(target_y)

    def _weapon_fire_decision(
        self,
        slot_index: int,
        weapon: Optional[mf.WeaponObs],
        distance: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if weapon is None or not has_target:
            self.laser_charging[slot_index] = False
            return False

        kind = weapon.get("kind")
        stats = weapon.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(weapon.get("ammo", 0.0))
            reload_left = float(weapon.get("reload", 0.0))

            if reload_left > 0.0 or ammo <= 0.0:
                return False

            return distance <= max_range * 0.95

        if kind == "lightning":
            if energy < 12.0 or distance > min(45.0, max_range):
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if distance < 24.0 or distance > max_range:
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(weapon.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))

            if distance > max_range:
                self.laser_charging[slot_index] = False
                return False

            target_charge = full_charge * (0.85 if target_has_mortar else 0.98)

            if not self.laser_charging[slot_index]:
                if float(weapon.get("cooldown", 0.0)) <= 0.05 and energy > 15.0:
                    self.laser_charging[slot_index] = True
                    return True

                return False

            if charge_time >= target_charge:
                self.laser_charging[slot_index] = False
                return False

            return True

        return False

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_stats = me.get("stats", {})
        muzz