"""Other formats: Sponge .schem (v2, read by WorldEdit, FAWE and Litematica) and a .mcfunction of setblock
commands (runs from any datapack or command block, relative to where it is run)."""
from __future__ import annotations

from pathlib import Path

import nbtlib
from nbtlib import ByteArray, Compound, Int, IntArray, List, Short, String

from .structure import AIR, Structure


def _varint(v: int) -> list[int]:
    out = []
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return out


def to_schem(s: Structure, void_as: str = AIR) -> nbtlib.File:
    """Sponge schematic v2. A .schem has no 'leave this alone' block: void positions become void_as."""
    w, h, l = s.size
    palette: dict[str, int] = {}
    data: list[int] = []
    for y in range(h):
        for z in range(l):
            for x in range(w):
                st = s.blocks.get((x, y, z), void_as)
                if st not in palette:
                    palette[st] = len(palette)
                data += _varint(palette[st])
    block_entities = List[Compound]()
    for (x, y, z), nbt in s.block_nbt.items():
        be = Compound(nbt)
        be["Pos"] = IntArray([x, y, z])
        be["Id"] = String(s.blocks[(x, y, z)].split("[")[0])
        block_entities.append(be)
    root = Compound({
        "Version": Int(2),
        "DataVersion": Int(s.data_version),
        "Width": Short(w), "Height": Short(h), "Length": Short(l),
        "Offset": IntArray([0, 0, 0]),
        "PaletteMax": Int(len(palette)),
        "Palette": Compound({k: Int(v) for k, v in palette.items()}),
        "BlockData": ByteArray([b - 256 if b > 127 else b for b in data]),
        "BlockEntities": block_entities,
    })
    return nbtlib.File(root, gzipped=True, root_name="Schematic")   # v2: the fields sit in a root named Schematic


def to_mcfunction(s: Structure, skip_air: bool = False) -> str:
    """setblock commands relative to the runner (~x ~y ~z), bottom layer first so things like torches have
    something to hang on. Block entity NBT (chest contents) is kept."""
    lines = [f"# {s.size[0]}x{s.size[1]}x{s.size[2]} structure, {len(s.blocks)} blocks. Run it standing at the "
             f"lowest north-west corner."]
    for (x, y, z), st in sorted(s.blocks.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        if skip_air and st == AIR:
            continue
        nbt = s.block_nbt.get((x, y, z))
        lines.append(f"setblock ~{x} ~{y} ~{z} {st}{nbt.snbt() if nbt is not None else ''}")
    return "\n".join(lines) + "\n"


def save_schem(s: Structure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    to_schem(s).save(path, gzipped=True)
    return path
