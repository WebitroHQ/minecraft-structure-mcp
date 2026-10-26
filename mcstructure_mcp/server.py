"""MCP server: design Minecraft structures (structure-block .nbt files) with an AI assistant.

Build with blocks and fills, read layers back as text, look at the result as isometric renders or layer plans,
and save a .nbt you can load in game with a structure block (or place from a mod / datapack).
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Literal

import nbtlib
from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

from .assets import Assets
from .render import Renderer
from .structure import AIR, DATA_VERSIONS, Structure, normalize_state
from .export import save_schem, to_mcfunction
from .shapes import Pattern, gable_roof, solid, transform_box
from .walkable import check

mcp = MCPServer("minecraft-structure-designer")
WORKSPACE = Path(os.environ.get("MCSTRUCT_WORKSPACE", Path.home() / "minecraft-structures"))
STRUCTURES: dict[str, Structure] = {}
_renderer: Renderer | None = None


def renderer() -> Renderer:
    global _renderer
    if _renderer is None:
        _renderer = Renderer(Assets())
    return _renderer


def get(name: str) -> Structure:
    if name not in STRUCTURES:
        raise ValueError(f"No structure '{name}'. Open or create one first. Open: {sorted(STRUCTURES)}")
    return STRUCTURES[name]


def _box(x1, y1, z1, x2, y2, z2):
    return (min(x1, x2), min(y1, y2), min(z1, z2)), (max(x1, x2), max(y1, y2), max(z1, z2))


def _image(img: PILImage.Image, name: str, max_size: int) -> Image:
    WORKSPACE.joinpath("renders").mkdir(parents=True, exist_ok=True)
    img.save(WORKSPACE / "renders" / f"{name}.png")
    if max(img.size) > max_size:
        k = max_size / max(img.size)
        img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), PILImage.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


# ------------------------------------------------------------------ files
@mcp.tool()
def new_structure(name: str, size_x: int, size_y: int, size_z: int, minecraft_version: str = "1.20.1") -> str:
    """Create an empty structure (all positions are structure void until you place blocks).
    minecraft_version sets the saved DataVersion: use the OLDEST version you target (newer games upgrade it)."""
    if minecraft_version not in DATA_VERSIONS:
        raise ValueError(f"Known versions: {', '.join(DATA_VERSIONS)}")
    if min(size_x, size_y, size_z) < 1 or max(size_x, size_y, size_z) > 256:
        raise ValueError("Each side must be 1..256 (a vanilla structure block saves at most 48).")
    STRUCTURES[name] = Structure((size_x, size_y, size_z), DATA_VERSIONS[minecraft_version])
    return f"Created '{name}' {size_x}x{size_y}x{size_z} (DataVersion {DATA_VERSIONS[minecraft_version]})."


@mcp.tool()
def open_structure(path: str, name: str | None = None) -> str:
    """Open a structure .nbt file (relative paths are inside the workspace)."""
    p = Path(path)
    p = p if p.is_absolute() else WORKSPACE / p
    s = Structure.load(p)
    name = name or p.stem
    STRUCTURES[name] = s
    return f"Opened '{name}': size {s.size}, {len(s.blocks)} blocks, DataVersion {s.data_version}."


@mcp.tool()
def save_structure(name: str, path: str | None = None) -> str:
    """Save as a gzipped structure .nbt (default: <workspace>/<name>.nbt)."""
    s = get(name)
    p = Path(path) if path else WORKSPACE / f"{name}.nbt"
    p = p if p.is_absolute() else WORKSPACE / p
    return f"Saved {s.save(p)}"


@mcp.tool()
def list_structures() -> str:
    """Structures open in this session, and .nbt files in the workspace."""
    files = sorted(str(f.relative_to(WORKSPACE)) for f in WORKSPACE.rglob("*.nbt")) if WORKSPACE.exists() else []
    opened = [f"{n}: {s.size}, {len(s.blocks)} blocks" for n, s in STRUCTURES.items()]
    return "Open:\n  " + ("\n  ".join(opened) or "-") + f"\nWorkspace {WORKSPACE}:\n  " + ("\n  ".join(files) or "-")


@mcp.tool()
def structure_info(name: str, top: int = 25) -> str:
    """Size, block count and the most used block states."""
    s = get(name)
    lines = [f"'{name}' size {s.size}, {len(s.blocks)} blocks, {len(s.block_nbt)} with NBT, DataVersion {s.data_version}"]
    for state, n in list(s.counts().items())[:top]:
        lines.append(f"  {n:5d}  {state}")
    return "\n".join(lines)


@mcp.tool()
def resize_structure(name: str, size_x: int, size_y: int, size_z: int) -> str:
    """Change the size (blocks outside the new size are dropped)."""
    s = get(name)
    s.snapshot()
    s.resize((size_x, size_y, size_z))
    return f"'{name}' is now {s.size}."


# ------------------------------------------------------------------ editing
@mcp.tool()
def set_blocks(name: str, blocks: list[dict]) -> str:
    """Place many blocks at once: [{"x":1,"y":0,"z":2,"state":"oak_stairs[facing=east,half=bottom]"}, ...].
    state "void" removes the position (left untouched when the structure is placed); "air" places air."""
    s = get(name)
    s.snapshot()
    for b in blocks:
        st = str(b["state"])
        s.set(int(b["x"]), int(b["y"]), int(b["z"]), None if st == "void" else st)
    return f"Set {len(blocks)} blocks in '{name}'."


@mcp.tool()
def fill(name: str, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, state: str,
         mode: Literal["replace", "hollow", "outline", "walls", "keep"] = "replace", seed: int | None = None) -> str:
    """Fill a box (inclusive corners). Modes: replace = every position; hollow = shell of `state`, inside air;
    outline = shell only, inside untouched; walls = the four vertical sides only; keep = only empty positions.
    state "void" clears positions. state can be a weighted mix for natural-looking walls and floors:
    "stone_bricks*6,cracked_stone_bricks*3,mossy_stone_bricks" (seed makes it repeatable)."""
    s = get(name)
    s.snapshot()
    pattern = Pattern(state, seed)
    (ax, ay, az), (bx, by, bz) = _box(x1, y1, z1, x2, y2, z2)
    n = 0
    for x in range(ax, bx + 1):
        for y in range(ay, by + 1):
            for z in range(az, bz + 1):
                edge = x in (ax, bx) or y in (ay, by) or z in (az, bz)
                side = x in (ax, bx) or z in (az, bz)
                if mode == "outline" and not edge or mode == "walls" and not side:
                    continue
                if mode == "keep" and (x, y, z) in s.blocks:
                    continue
                st = pattern.pick() if mode != "hollow" or edge else AIR
                s.set(x, y, z, None if st == "void" else st)
                n += 1
    return f"Filled {n} positions in '{name}'."


@mcp.tool()
def replace_blocks(name: str, old: str, new: str, x1: int | None = None, y1: int | None = None, z1: int | None = None,
                   x2: int | None = None, y2: int | None = None, z2: int | None = None, region: str | None = None) -> str:
    """Replace one block (name or full state) with another, in the whole structure, a box or a named region.
    A bare name (e.g. 'stone_bricks') matches every state of that block. `new` can be a weighted mix
    ("cracked_stone_bricks*1,stone_bricks*3") to weather a wall."""
    s = get(name)
    s.snapshot()
    want = normalize_state(old)
    pattern = Pattern(new)
    bare = "[" not in old
    box = _region(s, region) if region else _box(x1, y1, z1, x2, y2, z2) if x1 is not None else None
    n = 0
    for p, st in list(s.blocks.items()):
        if box and not all(box[0][i] <= p[i] <= box[1][i] for i in range(3)):
            continue
        if st == want or bare and st.split("[")[0] == want:
            pick = pattern.pick()
            s.set(*p, None if pick == "void" else pick)
            n += 1
    return f"Replaced {n} blocks in '{name}'."


@mcp.tool()
def copy_region(name: str, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, dest_x: int, dest_y: int, dest_z: int,
                source: str | None = None) -> str:
    """Copy a box (from `source`, default the same structure) so its lowest corner lands at dest."""
    s = get(name)
    src = get(source) if source else s
    (ax, ay, az), (bx, by, bz) = _box(x1, y1, z1, x2, y2, z2)
    s.snapshot()
    items = [(p, st, src.block_nbt.get(p)) for p, st in src.blocks.items()
             if ax <= p[0] <= bx and ay <= p[1] <= by and az <= p[2] <= bz]
    n = 0
    for (x, y, z), st, nb in items:
        t = (x - ax + dest_x, y - ay + dest_y, z - az + dest_z)
        if s.inside(*t):
            s.set(*t, st)
            if nb is not None:
                s.block_nbt[t] = nb
            n += 1
    return f"Copied {n} blocks."


@mcp.tool()
def set_block_nbt(name: str, x: int, y: int, z: int, snbt: str) -> str:
    """Attach block entity NBT, e.g. chest contents:
    {Items:[{Slot:0b,id:"minecraft:candle",Count:2b}]}  (1.20.1 item format: id + Count)."""
    s = get(name)
    if (x, y, z) not in s.blocks:
        raise ValueError("Place the block first.")
    s.snapshot()
    s.block_nbt[(x, y, z)] = nbtlib.parse_nbt(snbt)
    return "NBT set."


@mcp.tool()
def undo(name: str) -> str:
    """Undo the last edit (up to 30 steps)."""
    return "Undone." if get(name).undo() else "Nothing to undo."


# ------------------------------------------------------------------ shaping
@mcp.tool()
def transform_region(name: str, op: Literal["cw90", "180", "ccw90", "mirror_x", "mirror_z"], region: str | None = None,
                     x1: int | None = None, y1: int | None = None, z1: int | None = None,
                     x2: int | None = None, y2: int | None = None, z2: int | None = None) -> str:
    """Rotate (seen from above, clockwise = north turns east) or mirror a named region or a box, in place.
    Stairs, doors, fences, signs, rails and logs turn with it. Build one wing, copy_region it, then mirror the
    copy for a symmetric house. A 90° turn swaps the box's x and z sides (a named region is updated)."""
    s = get(name)
    box = _region(s, region) if region else _box(x1, y1, z1, x2, y2, z2) if x1 is not None else None
    if box is None:
        raise ValueError("Give a region or a box.")
    s.snapshot()
    new_box = transform_box(s, box, op)
    if region:
        s.regions[region] = new_box
    return f"{op} done: now {new_box[0]}..{new_box[1]}."


@mcp.tool()
def roof_gable(name: str, x1: int, z1: int, x2: int, z2: int, y: int, ridge_axis: Literal["x", "z"],
               stairs: str = "spruce_stairs", slab: str = "spruce_slab", block: str = "spruce_planks",
               pitch: float = 1.0, gable: str | None = None, region: str | None = None) -> str:
    """Build a gable roof over the footprint x1..x2 / z1..z2 starting at height y (usually one above the wall
    tops; widen the footprint by one on each side for eaves). ridge_axis = the direction the ridge runs.
    pitch: 1 = stairs (45 deg), 0.5 = slabs (shallow), 2 = stairs on blocks (steep). gable = block to close
    the triangular end walls. region = also name the roof's box, to fix it later with clear_region."""
    s = get(name)
    s.snapshot()
    n = gable_roof(s, x1, z1, x2, z2, y, ridge_axis, stairs, slab, block, float(pitch), gable)
    if region:
        width = (abs(z2 - z1) if ridge_axis == "x" else abs(x2 - x1)) + 1
        rise = {0.5: (width + 1) // 4, 1.0: (width - 1) // 2, 2.0: width}[float(pitch)]
        a, b = _box(x1, y, z1, x2, min(y + rise, s.size[1] - 1), z2)
        s.regions[region] = (a, b)
    return f"Roof: {n} blocks." + (f" Region '{region}' marked." if region else "")


@mcp.tool()
def shape(name: str, kind: Literal["sphere", "cylinder"], x: int, y: int, z: int, radius: float, state: str,
          height: int = 1, hollow: bool = False, axis: Literal["x", "y", "z"] = "y", seed: int | None = None) -> str:
    """A sphere (centre x,y,z) or cylinder (base centre x,y,z, height along axis): domes, towers, tanks,
    arches. hollow keeps a one-block shell (a hollow cylinder is open-ended). state can be a weighted mix."""
    s = get(name)
    s.snapshot()
    n = solid(s, kind, x, y, z, radius, height, Pattern(state, seed), hollow, axis)
    return f"{kind}: {n} blocks."


# ------------------------------------------------------------------ other formats
@mcp.tool()
def export_schem(name: str, path: str | None = None) -> str:
    """Save as a Sponge .schem (v2) for WorldEdit / FAWE (//schem load) and Litematica. Void positions become
    air: a .schem cannot leave blocks untouched."""
    s = get(name)
    p = Path(path) if path else WORKSPACE / f"{name}.schem"
    p = p if p.is_absolute() else WORKSPACE / p
    return f"Saved {save_schem(s, p)}"


@mcp.tool()
def export_mcfunction(name: str, path: str | None = None, skip_air: bool = False) -> str:
    """Save as a .mcfunction of setblock commands relative to whoever runs it (put it in a datapack, stand at
    the lowest north-west corner, /function). Chest contents are kept. skip_air leaves existing blocks
    where the structure has air."""
    s = get(name)
    p = Path(path) if path else WORKSPACE / f"{name}.mcfunction"
    p = p if p.is_absolute() else WORKSPACE / p
    p.parent.mkdir(parents=True, exist_ok=True)
    text = to_mcfunction(s, skip_air)
    p.write_text(text, encoding="utf-8")
    return f"Saved {p} ({text.count(chr(10)) - 1} commands)."


# ------------------------------------------------------------------ regions
def _region(s: Structure, region: str):
    if region not in s.regions:
        raise ValueError(f"No region '{region}'. Regions: {sorted(s.regions) or '-'} (mark one with mark_region)")
    return s.regions[region]


@mcp.tool()
def mark_region(name: str, region: str, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int) -> str:
    """Name a box ('roof', 'stairwell', 'east_wing') so you can fix that part alone later: render it,
    check it, clear and rebuild it, without reworking the rest. Saved with the structure."""
    s = get(name)
    a, b = _box(x1, y1, z1, x2, y2, z2)
    if not (s.inside(*a) and s.inside(*b)):
        raise ValueError(f"The box must be inside the structure (size {s.size}).")
    s.regions[region] = (a, b)
    return f"Region '{region}' = {a}..{b}."


@mcp.tool()
def list_regions(name: str) -> str:
    """The named regions of a structure, with their boxes and block counts."""
    s = get(name)
    if not s.regions:
        return "No regions. Mark one with mark_region."
    return "\n".join(f"  {n}: {a}..{b}, {len(s.crop((a, b)).blocks)} blocks" for n, (a, b) in s.regions.items())


@mcp.tool()
def delete_region(name: str, region: str) -> str:
    """Forget a region's name (the blocks stay)."""
    s = get(name)
    _region(s, region)
    del s.regions[region]
    return f"Region '{region}' removed."


@mcp.tool()
def clear_region(name: str, region: str, state: str = "void") -> str:
    """Empty a named region (default: void; or "air") so it can be rebuilt — the rest is untouched.
    undo brings it back."""
    s = get(name)
    (ax, ay, az), (bx, by, bz) = _region(s, region)
    s.snapshot()
    for x in range(ax, bx + 1):
        for y in range(ay, by + 1):
            for z in range(az, bz + 1):
                s.set(x, y, z, None if state == "void" else state)
    return f"Cleared region '{region}' ({(bx - ax + 1) * (by - ay + 1) * (bz - az + 1)} positions)."


@mcp.tool()
def render_region(name: str, region: str, view: Literal["se", "sw", "nw", "ne"] = "se", scale: int = 32,
                  max_size: int = 900) -> Image:
    """Render one named region on its own (closer than a full render, so details like a roof pitch show)."""
    s = get(name)
    yaw = {"se": 0, "sw": 90, "nw": 180, "ne": 270}[view]
    img = renderer().iso(s.crop(_region(s, region)), scale=max(4, min(scale, 96)), yaw=yaw)
    return _image(img, f"{name}-{region}-{view}", max_size)


# ------------------------------------------------------------------ looking at it
@mcp.tool()
def layer_text(name: str, y: int) -> str:
    """One horizontal layer as a character map (rows = z, columns = x) with a legend. Cheap and exact —
    use it to check positions; use render/render_layer to see how it looks."""
    s = get(name)
    sx, sy, sz = s.size
    legend: dict[str, str] = {}
    chars = "#=+*%@&$ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    rows = ["     " + "".join(str(x % 10) for x in range(sx))]
    for z in range(sz):
        row = []
        for x in range(sx):
            st = s.blocks.get((x, y, z))
            if st is None:
                row.append(" ")
            elif st == AIR:
                row.append(".")
            else:
                if st not in legend:
                    legend[st] = chars[len(legend) % len(chars)]
                row.append(legend[st])
        rows.append(f"{z:3d}  " + "".join(row))
    leg = "\n".join(f"  {c} = {st}" for st, c in legend.items())
    return f"'{name}' y={y} (' ' void, '.' air)\n" + "\n".join(rows) + "\n" + leg


@mcp.tool()
def check_walkable(name: str, x1: int | None = None, y1: int | None = None, z1: int | None = None,
                   x2: int | None = None, y2: int | None = None, z2: int | None = None,
                   include_open_sky: bool = False, region: str | None = None) -> str:
    """Can a player actually walk it? Checks every stair (something to stand on before the bottom step, head
    room over each step — the ceiling above must be open — and a floor or the next step at the top) and every
    door (upper half, floor and head room on both sides). Optional box (or named region) to check one stairwell. Stairs with
    nothing above them (roof slopes, outdoor steps) are skipped unless include_open_sky."""
    s = get(name)
    box = _region(s, region) if region else _box(x1, y1, z1, x2, y2, z2) if x1 is not None else None
    problems, n = check(s, box, include_open_sky)
    head = (f"'{name}': checked {n['stairs']} stairs and {n['doors']} doors "
            f"(skipped {n['open_sky']} open-sky and {n['upside_down']} upside-down stairs).")
    if not problems:
        return head + " All walkable."
    return head + f" {len(problems)} problem(s):\n" + "\n".join(map(str, problems))


@mcp.tool()
def render(name: str, view: Literal["se", "sw", "nw", "ne"] = "se", cut_y: int | None = None, scale: int = 24,
           max_size: int = 900) -> Image:
    """Isometric render with the game's textures. view = the corner you look from; cut_y hides everything
    above that height (see inside rooms). Also saved under <workspace>/renders/."""
    yaw = {"se": 0, "sw": 90, "nw": 180, "ne": 270}[view]
    img = renderer().iso(get(name), scale=max(4, min(scale, 96)), cut_y=cut_y, yaw=yaw)
    return _image(img, f"{name}-{view}" + (f"-y{cut_y}" if cut_y is not None else ""), max_size)


@mcp.tool()
def render_layer(name: str, y: int, cell: int = 20, max_size: int = 900) -> Image:
    """Top-down plan of one layer (with the floor below faded), grid and coordinates."""
    img = renderer().layer(get(name), y, cell=max(6, min(cell, 48)))
    return _image(img, f"{name}-layer{y}", max_size)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
