"""Can a player actually walk it? Geometry checks for stairs and doors, no game needed.

A stair flight has to line up across two levels at once: something to stand on in front of the bottom step,
head room above every step (the ceiling of the floor above must be open), and a floor to step out onto at the
top. Doors need floor and head room on both sides. Block collision is approximated from block names: small
things (torches, signs, carpets, flowers, rails…) count as open; doors and open trapdoors / gates count as
passable; everything else is solid. Positions that are void or outside the structure count as unknown and
are given the benefit of the doubt (the world around the structure is not known).
"""
from __future__ import annotations

from dataclasses import dataclass

from .structure import Structure, parse_state

FACING = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}

_OPEN_EXACT = {
    "air", "cave_air", "void_air", "structure_void", "light", "water", "lava", "fire", "soul_fire",
    "torch", "soul_torch", "redstone_torch", "lever", "ladder", "vine", "redstone_wire", "tripwire",
    "tripwire_hook", "cobweb", "grass", "short_grass", "tall_grass", "fern", "large_fern", "dead_bush",
    "poppy", "dandelion", "blue_orchid", "allium", "azure_bluet", "oxeye_daisy", "cornflower",
    "lily_of_the_valley", "wither_rose", "sunflower", "lilac", "rose_bush", "peony", "torchflower",
    "sugar_cane", "glow_lichen", "sculk_vein", "hanging_roots", "spore_blossom", "nether_sprouts",
    "crimson_roots", "warped_roots", "seagrass", "tall_seagrass", "kelp", "kelp_plant", "snow",
    "moss_carpet", "candle", "nether_portal", "end_portal", "string",
}
_OPEN_SUFFIX = (
    "_torch", "_wall_torch", "_sign", "_wall_sign", "_hanging_sign", "_banner", "_wall_banner", "_button",
    "_pressure_plate", "rail", "_carpet", "_sapling", "_tulip", "_mushroom", "_candle", "_coral_fan",
    "_coral_wall_fan", "_coral", "_vines", "_vines_plant", "_head", "_skull",
)


@dataclass
class Problem:
    kind: str
    pos: tuple[int, int, int]
    text: str

    def __str__(self) -> str:
        x, y, z = self.pos
        return f"  [{self.kind}] ({x}, {y}, {z}) {self.text}"


def _short(state: str) -> str:
    return state.replace("minecraft:", "")


def is_open(state: str | None) -> bool:
    """No collision worth stopping a player (void counts as open: unknown, assume air)."""
    if state is None:
        return True
    name, props = parse_state(state)
    base = name.split(":", 1)[1]
    if base in _OPEN_EXACT or base.endswith(_OPEN_SUFFIX):
        return True
    if base.endswith("_door"):
        return True                               # you open it as you walk through
    if base.endswith(("_trapdoor", "fence_gate")):
        return props.get("open") == "true"
    return False


def is_floor(state: str | None) -> bool:
    """Something you can stand on (void counts: the ground under a structure is usually the world's)."""
    return state is None or not is_open(state) and not parse_state(state)[0].endswith("_door")


class Checker:
    def __init__(self, s: Structure):
        self.s = s

    def at(self, x: int, y: int, z: int) -> str | None:
        return self.s.blocks.get((x, y, z))

    def known(self, x: int, y: int, z: int) -> bool:
        return self.s.inside(x, y, z) and (x, y, z) in self.s.blocks

    def why_not_standing(self, x: int, y: int, z: int) -> str | None:
        """Why a player cannot stand with their feet at (x, y, z), or None if they can."""
        for dy, part in ((0, "feet"), (1, "head")):
            st = self.at(x, y + dy, z)
            if not is_open(st):
                return f"{part} blocked by {_short(st)} at ({x}, {y + dy}, {z})"
        if self.known(x, y - 1, z) and not is_floor(self.at(x, y - 1, z)):
            return f"nothing to stand on at ({x}, {y - 1}, {z}) — a drop"
        return None

    def covered(self, x: int, y: int, z: int) -> bool:
        return any(not is_open(self.at(x, yy, z)) for yy in range(y + 1, self.s.size[1]))

    # ------------------------------------------------------------------ stairs
    def stair(self, pos, props) -> list[Problem]:
        x, y, z = pos
        dx, dz = FACING[props.get("facing", "north")]
        out: list[Problem] = []
        # head room over the step: feet end up at y+1, head at y+2 — the ceiling above must be open
        for dy, what in ((1, "blocked right above the step"), (2, "no head room over the step (the ceiling must be open here)")):
            st = self.at(x, y + dy, z)
            if not is_open(st):
                out.append(Problem("stairs", pos, f"{what}: {_short(st)} at ({x}, {y + dy}, {z})"))
        # the bottom: somewhere to stand in front of it (on the floor, or on the step below)
        lx, lz = x - dx, z - dz
        if self.s.inside(lx, y, lz):
            why = self.why_not_standing(lx, y, lz)
            if why:
                out.append(Problem("stairs", pos, f"can't get onto the bottom step from ({lx}, {y}, {lz}): {why}"))
        # the top: the next step of the flight, or a floor to step out onto
        hx, hz = x + dx, z + dz
        if self.s.inside(hx, y + 1, hz):
            nxt = self.at(hx, y + 1, hz)
            if nxt is not None and parse_state(nxt)[0].endswith("_stairs"):
                n_props = parse_state(nxt)[1]
                if n_props.get("half", "bottom") == "bottom" and n_props.get("facing") != props.get("facing"):
                    out.append(Problem("stairs", pos, f"the next step at ({hx}, {y + 1}, {hz}) faces "
                                                      f"{n_props.get('facing')}, this one {props.get('facing')}"))
                elif n_props.get("half") == "top":
                    out.append(Problem("stairs", pos, f"the step above is upside-down at ({hx}, {y + 1}, {hz})"))
            else:
                why = self.why_not_standing(hx, y + 1, hz)
                if why:
                    out.append(Problem("stairs", pos, f"can't step off the top at ({hx}, {y + 1}, {hz}): {why}"))
        return out

    # ------------------------------------------------------------------ doors
    def door(self, pos, name, props) -> list[Problem]:
        x, y, z = pos
        out: list[Problem] = []
        top = self.at(x, y + 1, z)
        if top is None or parse_state(top)[0] != name:
            out.append(Problem("door", pos, "has no upper half above it"))
        dx, dz = FACING[props.get("facing", "north")]
        for sign, side in ((1, "front"), (-1, "back")):
            px, pz = x + dx * sign, z + dz * sign
            if not self.s.inside(px, y, pz):
                continue
            why = self.why_not_standing(px, y, pz)
            if why:
                out.append(Problem("door", pos, f"{side} side ({px}, {y}, {pz}): {why}"))
        if name == "minecraft:iron_door":
            out.append(Problem("door", pos, "iron door: it only opens with redstone (a button or lever next to it)"))
        return out


def check(s: Structure, box=None, include_open_sky: bool = False) -> tuple[list[Problem], dict[str, int]]:
    """All problems found, plus counts of what was checked / skipped."""
    c = Checker(s)
    problems: list[Problem] = []
    n = {"stairs": 0, "doors": 0, "open_sky": 0, "upside_down": 0}
    for pos, st in sorted(s.blocks.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        if box and not all(box[0][i] <= pos[i] <= box[1][i] for i in range(3)):
            continue
        name, props = parse_state(st)
        if name.endswith("_stairs"):
            if props.get("half") == "top":
                n["upside_down"] += 1                  # decoration (eaves, window sills), not a walkway
                continue
            if not include_open_sky and not c.covered(*pos):
                n["open_sky"] += 1                     # roof slopes, outdoor steps
                continue
            n["stairs"] += 1
            problems += c.stair(pos, props)
        elif name.endswith("_door") and not name.endswith("_trapdoor") and props.get("half") == "lower":
            n["doors"] += 1
            problems += c.door(pos, name, props)
    return problems, n
