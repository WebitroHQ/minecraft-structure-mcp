"""Building helpers: weighted block mixes, rotating / mirroring a box (block states turn with it), gable roofs,
spheres and cylinders."""
from __future__ import annotations

import math
import random

from .structure import Structure, format_state, parse_state

# ------------------------------------------------------------------ block mixes
def split_states(text: str) -> list[str]:
    """Split on commas outside [...]: 'a[x=1,y=2]*3,b' -> ['a[x=1,y=2]*3', 'b']."""
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "["
        depth -= ch == "]"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


class Pattern:
    """'stone_bricks*6,cracked_stone_bricks*3,mossy_stone_bricks' — one state, or a weighted random mix."""

    def __init__(self, text: str, seed: int | None = None):
        self.choices: list[tuple[str, float]] = []
        for part in split_states(text):
            state, weight = part, 1.0
            tail = part.rsplit("]", 1)[-1]
            if "*" in tail:
                state, w = part.rsplit("*", 1)
                weight = float(w)
            self.choices.append((state.strip(), weight))
        if not self.choices:
            raise ValueError("Empty block pattern.")
        self.rng = random.Random(seed)

    def pick(self) -> str:
        if len(self.choices) == 1:
            return self.choices[0][0]
        states, weights = zip(*self.choices)
        return self.rng.choices(states, weights)[0]


# ------------------------------------------------------------------ turning block states
_CW = {"north": "east", "east": "south", "south": "west", "west": "north"}
_MIRROR = {"x": {"east": "west", "west": "east"}, "z": {"north": "south", "south": "north"}}
_RAILS = {"north_south", "east_west", "north_east", "north_west", "south_east", "south_west",
          "ascending_north", "ascending_south", "ascending_east", "ascending_west"}
_CHIRAL = {"left": "right", "right": "left", "inner_left": "inner_right", "inner_right": "inner_left",
           "outer_left": "outer_right", "outer_right": "outer_left"}


def _turn_words(value: str, table: dict[str, str]) -> str:
    return "_".join(table.get(w, w) for w in value.split("_"))


def _rail(value: str) -> str:
    if value in _RAILS:
        return value
    a, _, b = value.partition("_")
    return f"{b}_{a}"                                  # east_north -> north_east


def turn_state(state: str, table: dict[str, str], mirror_axis: str | None = None) -> str:
    """Apply a direction table (one 90° clockwise step, or a mirror) to every direction in a block state."""
    name, props = parse_state(state)
    if not props:
        return state
    out = dict(props)
    for k, v in props.items():
        if k in ("facing", "orientation"):
            out[k] = _turn_words(v, table)
        elif k in table and v in ("true", "false", "none", "low", "tall", "side", "up"):
            out[table[k]] = v                          # fence / wall / redstone connections
        elif k == "axis" and mirror_axis is None:
            out[k] = {"x": "z", "z": "x"}.get(v, v)
        elif k == "rotation":
            r = int(v)
            out[k] = str((r + 4) % 16 if mirror_axis is None else (16 - r) % 16 if mirror_axis == "x" else (8 - r) % 16)
        elif k == "shape" and "_" in v and any(d in v for d in _CW):
            out[k] = _rail(_turn_words(v, table))
    if mirror_axis is not None:
        for k in ("shape", "hinge", "type", "chest_type"):
            if k in out and out[k] in _CHIRAL and not (k == "type" and "chest" not in name):
                out[k] = _CHIRAL[out[k]]
    return format_state(name, out)


def transform_box(s: Structure, box, op: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Rotate (cw90, 180, ccw90) or mirror (mirror_x, mirror_z) the blocks of a box in place, around its own
    lowest corner. A 90° turn swaps the box's x and z sides. Returns the new box."""
    (ax, ay, az), (bx, by, bz) = box
    w, l = bx - ax + 1, bz - az + 1
    steps = {"cw90": 1, "180": 2, "ccw90": 3}.get(op)
    if steps is None and op not in ("mirror_x", "mirror_z"):
        raise ValueError("op: cw90, 180, ccw90, mirror_x or mirror_z")
    items = {(x, y, z): (st, s.block_nbt.get((x, y, z))) for (x, y, z), st in s.blocks.items()
             if ax <= x <= bx and ay <= y <= by and az <= z <= bz}
    nw, nl = (l, w) if steps in (1, 3) else (w, l)
    new_box = ((ax, ay, az), (ax + nw - 1, by, az + nl - 1))
    if not s.inside(*new_box[1]):
        raise ValueError(f"Turned, the box would end at {new_box[1]} — outside the structure (size {s.size}).")
    for p in items:
        s.blocks.pop(p, None)
        s.block_nbt.pop(p, None)
    for (x, y, z), (st, nbt) in items.items():
        lx, lz = x - ax, z - az
        if op == "mirror_x":
            lx, st = w - 1 - lx, turn_state(st, _MIRROR["x"], "x")
        elif op == "mirror_z":
            lz, st = l - 1 - lz, turn_state(st, _MIRROR["z"], "z")
        else:
            cw, cl = w, l
            for _ in range(steps):
                lx, lz = cl - 1 - lz, lx            # one clockwise step: north -> east
                cw, cl = cl, cw
                st = turn_state(st, _CW)
        p = (ax + lx, y, az + lz)
        s.blocks[p] = st
        if nbt is not None:
            s.block_nbt[p] = nbt
    return new_box


# ------------------------------------------------------------------ roofs and solids
def gable_roof(s: Structure, x1: int, z1: int, x2: int, z2: int, y: int, ridge_axis: str, stairs: str, slab: str,
               block: str, pitch: float = 1.0, gable: str | None = None) -> int:
    """A gable roof over the footprint (x1..x2, z1..z2) starting at height y, its ridge running along
    ridge_axis. pitch 1 = stairs (45°), 0.5 = slabs (shallow), 2 = stairs on blocks (steep). gable: a block
    to fill the triangular end walls with (optional). Returns blocks placed."""
    if pitch not in (0.5, 1.0, 2.0):
        raise ValueError("pitch must be 0.5, 1 or 2")
    xa, xb, za, zb = min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2)
    along_x = ridge_axis == "x"
    lo, hi = (za, zb) if along_x else (xa, xb)       # the sloping direction
    run = (xa, xb) if along_x else (za, zb)
    n = 0

    def put(u: int, v: int, yy: int, st: str) -> None:
        nonlocal n
        x, z = (v, u) if along_x else (u, v)
        if s.inside(x, yy, z):
            s.set(x, yy, z, st)
            n += 1

    width = hi - lo + 1
    for i in range((width + 1) // 2):
        sides = [(lo + i, "south" if along_x else "east"), (hi - i, "north" if along_x else "west")]
        if lo + i == hi - i:
            sides = sides[:1]
        for u, facing in sides:
            ridge = lo + i == hi - i
            for v in range(run[0], run[1] + 1):
                if pitch == 1.0:
                    put(u, v, y + i, f"{block}" if ridge else f"{stairs}[facing={facing},half=bottom]")
                elif pitch == 2.0:
                    put(u, v, y + 2 * i, block)
                    put(u, v, y + 2 * i + 1, block if ridge else f"{stairs}[facing={facing},half=bottom]")
                else:
                    put(u, v, y + i // 2, f"{slab}[type={'top' if i % 2 else 'bottom'}]")
            if gable:                                  # fill the end wall under this slope step
                top = y + (i if pitch == 1.0 else 2 * i if pitch == 2.0 else i // 2)
                for v in run:
                    for yy in range(y, top):
                        put(u, v, yy, gable)
    return n


def solid(s: Structure, kind: str, cx: int, cy: int, cz: int, radius: float, height: int, pattern: Pattern,
          hollow: bool, axis: str = "y") -> int:
    """A sphere (radius) or a cylinder (radius, height, along axis). hollow keeps a one-block shell."""
    r = radius + 0.5
    n = 0
    rng = int(math.ceil(radius))
    if kind == "sphere":
        cells = [(dx, dy, dz) for dx in range(-rng, rng + 1) for dy in range(-rng, rng + 1) for dz in range(-rng, rng + 1)]
        inside = lambda dx, dy, dz: dx * dx + dy * dy + dz * dz < r * r
    else:
        cells = [(dx, h, dz) for dx in range(-rng, rng + 1) for dz in range(-rng, rng + 1) for h in range(height)]
        inside = lambda dx, h, dz: dx * dx + dz * dz < r * r and 0 <= h < height
    for dx, dy, dz in cells:
        if not inside(dx, dy, dz):
            continue
        sides = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)) + (((0, 1, 0), (0, -1, 0)) if kind == "sphere" else ())
        if hollow and all(inside(dx + a, dy + b, dz + c) for a, b, c in sides):   # a hollow cylinder is open-ended
            continue
        if kind == "cylinder" and axis != "y":
            p = (cx + dy, cy + dx, cz + dz) if axis == "x" else (cx + dx, cy + dz, cz + dy)
        else:
            p = (cx + dx, cy + dy, cz + dz)
        if s.inside(*p):
            s.set(*p, pattern.pick())
            n += 1
    return n
