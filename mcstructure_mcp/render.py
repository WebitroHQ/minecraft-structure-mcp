"""Software renderer: isometric views (from any of four corners, with an optional cut-away height) and
top-down layer plans, drawn with the game's own block models and textures."""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .assets import Assets, Model
from .structure import Structure

DIRS = {"up": (0, 1, 0), "down": (0, -1, 0), "north": (0, 0, -1), "south": (0, 0, 1), "west": (-1, 0, 0), "east": (1, 0, 0)}


def _face_points(d: str, a, b):
    """TL, TR, BL corners of an element face, in texture orientation."""
    (x0, y0, z0), (x1, y1, z1) = a, b
    return {
        "up": ((x0, y1, z0), (x1, y1, z0), (x0, y1, z1)),
        "down": ((x0, y0, z1), (x1, y0, z1), (x0, y0, z0)),
        "north": ((x1, y1, z0), (x0, y1, z0), (x1, y0, z0)),
        "south": ((x0, y1, z1), (x1, y1, z1), (x0, y0, z1)),
        "west": ((x0, y1, z0), (x0, y1, z1), (x0, y0, z0)),
        "east": ((x1, y1, z1), (x1, y1, z0), (x1, y0, z1)),
    }[d]


def _rot_matrix(axis: str, deg: float) -> np.ndarray:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _variant_matrix(m: Model) -> np.ndarray:
    # Minecraft: x first, then y; both clockwise (y seen from above: north -> east).
    return _rot_matrix("y", -m.y) @ _rot_matrix("x", -m.x)


SHADE = {(0, 1, 0): 1.0, (0, -1, 0): 0.5, (0, 0, 1): 0.8, (0, 0, -1): 0.8, (1, 0, 0): 0.62, (-1, 0, 0): 0.62}


class Renderer:
    def __init__(self, assets: Assets):
        self.assets = assets

    def _models(self, state: str):
        try:
            return self.assets.models(state)
        except Exception:
            return ()

    def _quads(self, st: Structure, cut_y: int | None, yaw: int):
        """Every visible face as (depth, TL, TR, BL in world units*16, texture, shade, tint)."""
        sx, sy, sz = st.size
        centre = np.array([sx * 8.0, 0.0, sz * 8.0])
        view_rot = _rot_matrix("y", -yaw)
        full = {}
        blocks = {p: s for p, s in st.blocks.items() if cut_y is None or p[1] <= cut_y}
        for p, s in blocks.items():
            ms = self._models(s)
            full[p] = bool(ms) and all(m.full_opaque for m in ms)
        out = []
        view = np.array([1.0, 1.0, 1.0])
        for p, s in blocks.items():
            if s.endswith(":air") or s.startswith("minecraft:air"):
                continue
            ms = self._models(s)
            if not any(m.elements for m in ms):
                out.extend(self._fallback(p, s, centre, view_rot))
                continue
            base = np.array(p, float) * 16.0
            for m in ms:
                vm = _variant_matrix(m)
                for e in m.elements:
                    er = None
                    if e.rotation:
                        er = (np.array(e.rotation.get("origin", [8, 8, 8]), float),
                              _rot_matrix(e.rotation.get("axis", "y"), e.rotation.get("angle", 0)))
                    for d, f in e.faces.items():
                        pts = [np.array(q, float) for q in _face_points(d, e.frm, e.to)]
                        n = np.array(DIRS[d], float)
                        if er is not None:
                            pts = [er[1] @ (q - er[0]) + er[0] for q in pts]
                            n = er[1] @ n
                        pts = [vm @ (q - 8.0) + 8.0 for q in pts]
                        n = vm @ n
                        wn = tuple(int(round(v)) for v in n)
                        if f.cull and wn in SHADE:
                            nb = (p[0] + wn[0], p[1] + wn[1], p[2] + wn[2])
                            if full.get(nb):
                                continue
                        pts = [view_rot @ (q + base - centre) + centre for q in pts]
                        vn = view_rot @ n
                        if vn @ view <= 1e-6:
                            continue
                        c = (pts[1] + pts[2]) / 2.0
                        shade = SHADE.get(wn, 0.8)
                        out.append((c.sum(), pts[0], pts[1], pts[2], f.texture, shade, f.tint))
        out.sort(key=lambda q: q[0])
        return out

    def _fallback(self, p, s, centre, view_rot):
        """Blocks drawn by entity renderers (chests, skulls, signs, beds…): a small grey box."""
        col = (150, 120, 70) if "chest" in s else (170, 170, 175)
        tex = Image.new("RGBA", (4, 4), col + (255,))
        base = np.array(p, float) * 16.0
        a, b = (2, 0, 2), (14, 12 if "chest" in s else 8, 14)
        res = []
        for d in ("up", "south", "east", "north", "west"):
            pts = [view_rot @ (np.array(q, float) + base - centre) + centre for q in _face_points(d, a, b)]
            vn = view_rot @ np.array(DIRS[d], float)
            if vn @ np.array([1.0, 1.0, 1.0]) <= 1e-6:
                continue
            c = (pts[1] + pts[2]) / 2.0
            res.append((c.sum(), pts[0], pts[1], pts[2], tex, SHADE[DIRS[d]], None))
        return res

    def iso(self, st: Structure, scale: int = 32, cut_y: int | None = None, yaw: int = 0,
            background=(28, 30, 38, 255)) -> Image.Image:
        quads = self._quads(st, cut_y, yaw)
        k = scale / 16.0
        cx, sy_ = math.cos(math.radians(30)) * k, 0.5 * k

        def proj(q):
            return ((q[0] - q[2]) * cx, (q[0] + q[2]) * sy_ - q[1] * k)

        sx, sy, sz = st.size
        corners = [proj(np.array([x * 16.0, y * 16.0, z * 16.0])) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
        minx = min(c[0] for c in corners) - scale
        miny = min(c[1] for c in corners) - scale
        w = int(max(c[0] for c in corners) - minx + scale)
        h = int(max(c[1] for c in corners) - miny + scale)
        canvas = Image.new("RGBA", (max(w, 16), max(h, 16)), background)
        for _, tl, tr, bl, tex, shade, tint in quads:
            p0, p1, p2 = (np.array(proj(q)) - (minx, miny) for q in (tl, tr, bl))
            self._paste(canvas, tex, p0, p1, p2, shade, tint)
        return canvas

    @staticmethod
    def _paste(canvas, tex, p0, p1, p2, shade, tint):
        p3 = p1 + p2 - p0
        xs = [p0[0], p1[0], p2[0], p3[0]]
        ys = [p0[1], p1[1], p2[1], p3[1]]
        bx0, by0 = int(math.floor(min(xs))), int(math.floor(min(ys)))
        bw, bh = int(math.ceil(max(xs))) - bx0 + 1, int(math.ceil(max(ys))) - by0 + 1
        tw, th = tex.size
        m = np.array([[(p1 - p0)[0] / tw, (p2 - p0)[0] / th], [(p1 - p0)[1] / tw, (p2 - p0)[1] / th]])
        if abs(np.linalg.det(m)) < 1e-6 or bw > 4096 or bh > 4096:
            return
        inv = np.linalg.inv(m)
        ox, oy = bx0 - p0[0], by0 - p0[1]
        data = (inv[0, 0], inv[0, 1], inv[0, 0] * ox + inv[0, 1] * oy,
                inv[1, 0], inv[1, 1], inv[1, 0] * ox + inv[1, 1] * oy)
        if tint or shade != 1.0:
            arr = np.asarray(tex, dtype=np.float32).copy()
            mul = np.array(tint, np.float32) / 255.0 if tint else np.ones(3, np.float32)
            arr[..., :3] *= mul * shade
            tex = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGBA")
        # sample texel centres; outside the texture is transparent
        big = Image.new("RGBA", (tw + 2, th + 2), (0, 0, 0, 0))
        big.paste(tex, (1, 1))
        data = (data[0], data[1], data[2] + 1, data[3], data[4], data[5] + 1)
        tile = big.transform((bw, bh), Image.Transform.AFFINE, data, resample=Image.Resampling.NEAREST)
        canvas.alpha_composite(tile, (bx0, by0))

    # ------------------------------------------------------------------ plan view
    def layer(self, st: Structure, y: int, cell: int = 24) -> Image.Image:
        sx, _, sz = st.size
        pad = 26
        img = Image.new("RGBA", (sx * cell + pad * 2, sz * cell + pad * 2), (24, 26, 32, 255))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        for (x, by, z), s in st.blocks.items():
            if by not in (y, y - 1) or s.startswith("minecraft:air"):
                continue
            tex = self._top_texture(s)
            if tex is None:
                continue
            t = tex.resize((cell, cell), Image.Resampling.NEAREST)
            if by == y - 1:                       # the floor below, faded
                if (x, y, z) in st.blocks and not st.blocks[(x, y, z)].startswith("minecraft:air"):
                    continue
                t = Image.blend(Image.new("RGBA", t.size, (24, 26, 32, 255)), t, 0.35)
            img.alpha_composite(t, (pad + x * cell, pad + z * cell))
        for x in range(sx + 1):
            d.line([(pad + x * cell, pad), (pad + x * cell, pad + sz * cell)], fill=(70, 74, 86, 255))
        for z in range(sz + 1):
            d.line([(pad, pad + z * cell), (pad + sx * cell, pad + z * cell)], fill=(70, 74, 86, 255))
        for x in range(0, sx, 5):
            d.text((pad + x * cell + 2, 6), str(x), fill=(220, 220, 230, 255), font=font)
        for z in range(0, sz, 5):
            d.text((4, pad + z * cell + 2), str(z), fill=(220, 220, 230, 255), font=font)
        d.text((pad, img.height - 18), f"y = {y}   (x across, z down; faded = floor below)", fill=(160, 165, 180, 255), font=font)
        return img

    def _top_texture(self, s: str):
        ms = self._models(s)
        for m in ms:
            for e in m.elements:
                for key in ("up", "north", "south", "east", "west", "down"):
                    if key in e.faces:
                        f = e.faces[key]
                        t = f.texture
                        if f.tint:
                            arr = np.asarray(t, dtype=np.float32).copy()
                            arr[..., :3] *= np.array(f.tint, np.float32) / 255.0
                            t = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGBA")
                        return t
        return Image.new("RGBA", (4, 4), (150, 120, 70, 255) if "chest" in s else (170, 170, 175, 255))
