"""A Minecraft structure (the format of structure-block .nbt files) held in memory."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import nbtlib
from nbtlib import Compound, Int, List, String

#: Data versions of common releases. Save with the OLDEST version you target: newer games upgrade old
#: structures automatically, older games cannot read newer ones.
DATA_VERSIONS = {
    "1.20.1": 3465, "1.20.4": 3700, "1.20.6": 3839, "1.21": 3953, "1.21.1": 3955,
    "1.21.4": 4189, "1.21.5": 4325, "1.21.8": 4440, "1.21.10": 4556,
}
AIR = "minecraft:air"


def normalize_state(state: str) -> str:
    """'oak_stairs[facing=east]' -> 'minecraft:oak_stairs[facing=east]' (properties sorted)."""
    state = state.strip()
    name, props = parse_state(state)
    return format_state(name, props)


def parse_state(state: str) -> tuple[str, dict[str, str]]:
    state = state.strip()
    props: dict[str, str] = {}
    if "[" in state:
        name, rest = state.split("[", 1)
        rest = rest.rstrip("]")
        for part in filter(None, (p.strip() for p in rest.split(","))):
            k, v = part.split("=", 1)
            props[k.strip()] = v.strip()
    else:
        name = state
    if ":" not in name:
        name = "minecraft:" + name
    return name, props


def format_state(name: str, props: dict[str, str]) -> str:
    if not props:
        return name
    return name + "[" + ",".join(f"{k}={props[k]}" for k in sorted(props)) + "]"


@dataclass
class Structure:
    size: tuple[int, int, int]
    data_version: int = DATA_VERSIONS["1.20.1"]
    #: (x, y, z) -> block state string. Missing positions are structure voids (left untouched when placed).
    blocks: dict[tuple[int, int, int], str] = field(default_factory=dict)
    #: (x, y, z) -> block entity NBT (e.g. chest contents), as an nbtlib Compound.
    block_nbt: dict[tuple[int, int, int], Compound] = field(default_factory=dict)
    entities: list = field(default_factory=list)
    #: Named boxes ('roof' -> ((x1, y1, z1), (x2, y2, z2)), inclusive) so a part can be edited on its own.
    #: Saved in the .nbt under a key the game ignores.
    regions: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = field(default_factory=dict)
    history: list = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ editing
    def inside(self, x: int, y: int, z: int) -> bool:
        sx, sy, sz = self.size
        return 0 <= x < sx and 0 <= y < sy and 0 <= z < sz

    def snapshot(self) -> None:
        self.history.append((dict(self.blocks), copy.deepcopy(self.block_nbt)))
        del self.history[:-30]

    def undo(self) -> bool:
        if not self.history:
            return False
        self.blocks, self.block_nbt = self.history.pop()
        return True

    def set(self, x: int, y: int, z: int, state: str | None) -> None:
        if not self.inside(x, y, z):
            raise ValueError(f"({x}, {y}, {z}) is outside the structure (size {self.size})")
        pos = (x, y, z)
        if state is None:
            self.blocks.pop(pos, None)
        else:
            self.blocks[pos] = normalize_state(state)
        self.block_nbt.pop(pos, None)

    def get(self, x: int, y: int, z: int) -> str | None:
        return self.blocks.get((x, y, z))

    def resize(self, size: tuple[int, int, int]) -> None:
        self.size = size
        self.blocks = {p: s for p, s in self.blocks.items() if self.inside(*p)}
        self.block_nbt = {p: n for p, n in self.block_nbt.items() if self.inside(*p)}
        self.regions = {n: (a, tuple(min(v, s - 1) for v, s in zip(b, size)))
                        for n, (a, b) in self.regions.items() if self.inside(*a)}

    def crop(self, box) -> "Structure":
        """A copy of just this box (moved to the origin), e.g. to render one region on its own."""
        (ax, ay, az), (bx, by, bz) = box
        out = Structure((bx - ax + 1, by - ay + 1, bz - az + 1), self.data_version)
        for (x, y, z), st in self.blocks.items():
            if ax <= x <= bx and ay <= y <= by and az <= z <= bz:
                out.blocks[(x - ax, y - ay, z - az)] = st
        return out

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.blocks.values():
            out[s] = out.get(s, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    # ------------------------------------------------------------------ nbt
    def to_nbt(self) -> nbtlib.File:
        palette: list[str] = []
        index: dict[str, int] = {}
        blocks = List[Compound]()
        for pos in sorted(self.blocks, key=lambda p: (p[1], p[2], p[0])):
            state = self.blocks[pos]
            if state not in index:
                index[state] = len(palette)
                palette.append(state)
            entry = Compound({"pos": List[Int]([Int(c) for c in pos]), "state": Int(index[state])})
            if pos in self.block_nbt:
                entry["nbt"] = self.block_nbt[pos]
            blocks.append(entry)
        pal = List[Compound]()
        for state in palette:
            name, props = parse_state(state)
            c = Compound({"Name": String(name)})
            if props:
                c["Properties"] = Compound({k: String(v) for k, v in props.items()})
            pal.append(c)
        root = Compound({
            "DataVersion": Int(self.data_version),
            "size": List[Int]([Int(v) for v in self.size]),
            "palette": pal,
            "blocks": blocks,
            "entities": List[Compound](self.entities),
        })
        if self.regions:
            root["mcstructure_regions"] = Compound({n: List[Int]([Int(v) for v in (*a, *b)])
                                                     for n, (a, b) in self.regions.items()})
        return nbtlib.File(root, gzipped=True)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.to_nbt().save(path, gzipped=True)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Structure":
        f = nbtlib.load(path)
        root = f if "size" in f else f[""]
        size = tuple(int(v) for v in root["size"])
        palette_tag = root["palette"] if "palette" in root else root["palettes"][0]
        palette = []
        for p in palette_tag:
            props = {str(k): str(v) for k, v in p.get("Properties", {}).items()}
            palette.append(format_state(str(p["Name"]), props))
        s = cls(size=size, data_version=int(root.get("DataVersion", DATA_VERSIONS["1.20.1"])))
        for b in root["blocks"]:
            pos = tuple(int(v) for v in b["pos"])
            s.blocks[pos] = palette[int(b["state"])]
            if "nbt" in b:
                s.block_nbt[pos] = b["nbt"]
        s.entities = list(root.get("entities", []))
        for n, v in root.get("mcstructure_regions", {}).items():
            v = [int(i) for i in v]
            s.regions[str(n)] = (tuple(v[:3]), tuple(v[3:]))
        return s
