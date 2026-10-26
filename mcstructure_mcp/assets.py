"""Block models and textures, read straight from the user's own Minecraft client jar (nothing is redistributed).

Resolves a block state the way the game does: blockstates/<id>.json (variants or multipart) -> model chain
(parents, texture variables) -> elements with per-face textures, plus the variant's x/y rotation.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .structure import parse_state

#: Default tints (the game takes them from the biome; these are plains colours).
TINTS = {
    "grass": (124, 189, 107), "foliage": (72, 181, 24), "water": (63, 118, 228), "redstone": (180, 20, 20),
    "spruce": (97, 153, 97), "birch": (128, 167, 85),
}
TINTED = {
    "grass_block": "grass", "grass": "grass", "short_grass": "grass", "tall_grass": "grass", "fern": "grass",
    "large_fern": "grass", "sugar_cane": "grass", "vine": "foliage", "oak_leaves": "foliage", "jungle_leaves": "foliage",
    "acacia_leaves": "foliage", "dark_oak_leaves": "foliage", "mangrove_leaves": "foliage", "spruce_leaves": "spruce",
    "birch_leaves": "birch", "lily_pad": "foliage", "water": "water", "redstone_wire": "redstone",
}


@dataclass
class Face:
    texture: Image.Image          # RGBA, already cropped to the face's uv and rotated
    tint: tuple[int, int, int] | None
    cull: bool                    # the face sits on the block boundary (hidden by a full neighbour)


@dataclass
class Element:
    frm: tuple[float, float, float]
    to: tuple[float, float, float]
    faces: dict[str, Face]
    rotation: dict | None         # element rotation {origin, axis, angle}


@dataclass
class Model:
    elements: list[Element]
    x: int = 0                    # variant rotation
    y: int = 0
    full_opaque: bool = False     # a plain full cube with no transparent pixels (culls neighbours' faces)


def find_client_jar() -> Path | None:
    """MC_CLIENT_JAR, else a Fabric Loom cache or the launcher's versions folder (1.20.1 preferred)."""
    env = os.environ.get("MC_CLIENT_JAR")
    if env and Path(env).is_file():
        return Path(env)
    home = Path.home()
    candidates = [home / ".gradle/caches/fabric-loom/1.20.1/minecraft-client.jar"]
    appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    for base in (appdata / ".minecraft/versions", home / ".minecraft/versions",
                 home / "Library/Application Support/minecraft/versions"):
        if base.is_dir():
            for v in sorted(base.iterdir(), reverse=True):
                candidates.append(v / f"{v.name}.jar")
    for c in candidates:
        if c.is_file():
            return c
    return None


class Assets:
    def __init__(self, jar: Path | None = None):
        jar = jar or find_client_jar()
        if jar is None:
            raise RuntimeError("No Minecraft client jar found. Set MC_CLIENT_JAR to e.g. "
                               "%APPDATA%/.minecraft/versions/1.20.1/1.20.1.jar")
        self.jar_path = jar
        self.zip = zipfile.ZipFile(jar)
        self.names = set(self.zip.namelist())

    # ------------------------------------------------------------------ raw files
    def _json(self, path: str):
        if path not in self.names:
            return None
        return json.loads(self.zip.read(path))

    @staticmethod
    def _ref(ref: str, kind: str) -> str:
        ns, _, p = ref.partition(":") if ":" in ref else ("minecraft", "", ref)
        return f"assets/{ns}/{kind}/{p}"

    @lru_cache(maxsize=4096)
    def texture(self, ref: str) -> Image.Image | None:
        path = self._ref(ref, "textures") + ".png"
        if path not in self.names:
            return None
        im = Image.open(io.BytesIO(self.zip.read(path))).convert("RGBA")
        if im.height > im.width:          # animated strip: first frame
            im = im.crop((0, 0, im.width, im.width))
        return im

    @lru_cache(maxsize=4096)
    def _model_json(self, ref: str) -> dict:
        """The model with its parent chain merged (textures overlaid, first elements found win)."""
        data = self._json(self._ref(ref, "models") + ".json") or {}
        parent = data.get("parent")
        merged = {"textures": {}, "elements": None}
        if parent and not parent.startswith("builtin/"):
            p = self._model_json(parent)
            merged["textures"].update(p["textures"])
            merged["elements"] = p["elements"]
        merged["textures"].update(data.get("textures", {}))
        if "elements" in data:
            merged["elements"] = data["elements"]
        return merged

    @staticmethod
    def _resolve_tex(textures: dict, key: str, depth: int = 0) -> str | None:
        if depth > 8 or key is None:
            return None
        if key.startswith("#"):
            return Assets._resolve_tex(textures, textures.get(key[1:]), depth + 1)
        return key

    # ------------------------------------------------------------------ block states
    def _matches(self, cond: dict, props: dict) -> bool:
        if "OR" in cond:
            return any(self._matches(c, props) for c in cond["OR"])
        if "AND" in cond:
            return all(self._matches(c, props) for c in cond["AND"])
        for k, v in cond.items():
            allowed = str(v).lower().split("|")
            if props.get(k, None) is None:
                if "false" not in allowed and "none" not in allowed:
                    return False
            elif props[k].lower() not in allowed:
                return False
        return True

    def _variants(self, name: str, props: dict) -> list[dict]:
        bid = name.split(":", 1)[1]
        bs = self._json(f"assets/minecraft/blockstates/{bid}.json") if name.startswith("minecraft:") \
            else self._json(self._ref(name, "blockstates") + ".json")
        if not bs:
            return []
        if "variants" in bs:
            best = None
            for key, val in bs["variants"].items():
                want = dict(p.split("=", 1) for p in key.split(",") if "=" in p)
                if all(props.get(k, v) == v for k, v in want.items()):
                    best = val
                    break
            if best is None:
                best = next(iter(bs["variants"].values()))
            return [best[0] if isinstance(best, list) else best]
        out = []
        for part in bs.get("multipart", []):
            if "when" not in part or self._matches(part["when"], props):
                a = part["apply"]
                out.append(a[0] if isinstance(a, list) else a)
        return out

    @lru_cache(maxsize=8192)
    def models(self, state: str) -> tuple[Model, ...]:
        name, props = parse_state(state)
        bid = name.split(":", 1)[1]
        tint = TINTS.get(TINTED.get(bid, ""), None)
        out = []
        for v in self._variants(name, props):
            mj = self._model_json(v["model"])
            elements = []
            for e in (mj["elements"] or []):
                faces = {}
                for d, f in e.get("faces", {}).items():
                    ref = self._resolve_tex(mj["textures"], f.get("texture"))
                    img = self.texture(ref) if ref else None
                    if img is None:
                        continue
                    faces[d] = Face(self._crop(img, f, e, d), tint if "tintindex" in f else None, "cullface" in f)
                if faces:
                    elements.append(Element(tuple(e["from"]), tuple(e["to"]), faces, e.get("rotation")))
            full = (len(elements) == 1 and elements[0].frm == (0, 0, 0) and elements[0].to == (16, 16, 16)
                    and len(elements[0].faces) == 6
                    and all(f.texture.getextrema()[3][0] == 255 for f in elements[0].faces.values()))
            out.append(Model(elements, v.get("x", 0), v.get("y", 0), full))
        return tuple(out)

    @staticmethod
    def _crop(img: Image.Image, f: dict, e: dict, d: str) -> Image.Image:
        uv = f.get("uv")
        if uv is None:                     # default uv: the element's extent projected on the face
            (x0, y0, z0), (x1, y1, z1) = e["from"], e["to"]
            uv = {"down": [x0, 16 - z1, x1, 16 - z0], "up": [x0, z0, x1, z1],
                  "north": [16 - x1, 16 - y1, 16 - x0, 16 - y0], "south": [x0, 16 - y1, x1, 16 - y0],
                  "west": [z0, 16 - y1, z1, 16 - y0], "east": [16 - z1, 16 - y1, 16 - z0, 16 - y0]}[d]
        s = img.width / 16.0
        u0, v0, u1, v1 = uv
        flip_x, flip_y = u1 < u0, v1 < v0
        box = (int(round(min(u0, u1) * s)), int(round(min(v0, v1) * s)),
               max(int(round(max(u0, u1) * s)), int(round(min(u0, u1) * s)) + 1),
               max(int(round(max(v0, v1) * s)), int(round(min(v0, v1) * s)) + 1))
        t = img.crop(box)
        if flip_x:
            t = t.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if flip_y:
            t = t.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        rot = f.get("rotation", 0)
        if rot:
            t = t.rotate(-rot, expand=True)
        return t
