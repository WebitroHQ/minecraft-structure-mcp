"""The Studio's agent loop: send the conversation to the chosen model, run the tools it asks for, show the
renders back to it, repeat until it is done (or the step limit, or Stop)."""
from __future__ import annotations

import threading
import time
import traceback

from .. import server
from . import tools
from .llm import Call, make_chat

SYSTEM = """You are a master Minecraft builder working in a structure editor. The user describes something
(a ship, a starship, a cathedral, a house) and you build it as a HIGHLY DETAILED Minecraft structure — the kind
of build that wins showcase competitions. Never low-poly, never a box with a texture: every surface has
shape, depth and material variation. You have plenty of steps; use them.

Scale (decide first, and go big)
- Pick the scale from the smallest feature that must read clearly: a porthole, a window, a railing, a panel
  seam should each be at least one block, signature parts (funnels, cockpit, turrets) many blocks.
  Titanic: 1:1 (about 270 long, ~60 tall to the funnel tops). Millennium Falcon: about 90-110 blocks across.
  Buildings: real-world metres, 1 block = 1 m. Bigger is better as long as it fits (sides up to 512).
- Before building, write: the scale, the bounding box, and a parts list with real proportions.

Method (passes — do them all, in order)
1. Massing: the overall silhouette with correct proportions, built from computed shapes (build_script with
   math: tapering hull cross-sections, discs, cylinders, arches). Render it from two opposite views and
   compare with what the real thing looks like. Fix proportions now, it is cheap.
2. Shape refinement: smooth every curve and slope with stairs and slabs (half=top/bottom, the right facing)
   so edges are not stair-stepped cubes; add the secondary forms (decks, wings, mandibles, buttresses,
   towers, bridges, superstructure tiers). Nothing important may be a plain box.
3. Surface and materials: 3-6 blocks per material zone, mixed with weights (block mixes) and gradients
   (darker/weathered low, lighter high); panel lines, plating seams, trim bands, waterlines, stripes.
   Use texture blocks deliberately (e.g. light_gray_concrete + light_gray_concrete_powder + andesite +
   polished_andesite + smooth_stone for spaceship hull plating; black/red concrete and terracotta for a
   liner's hull; white concrete + quartz + calcite + smooth quartz for white superstructure).
4. Depth and detail ("greebling"): recess windows and portholes by one block with frames; add ledges,
   pillars, ribs, vents, pipes, antennas, lights (sea_lantern, glowstone hidden behind trapdoors, end_rods),
   railings (fences, iron_bars, walls, chains), small parts from trapdoors, buttons, levers, flower pots,
   lightning rods, grindstones, heads. A flat wall more than ~4 blocks without detail is a failure.
5. Signature features: model every feature people recognise in full (Titanic: four buff funnels with black
   tops and the dummy 4th, both masts with rigging lines, cranes, lifeboats with davits along the boat deck,
   promenade decks with window rows, the cruiser stern, rudder and three propellers, anchors, the bridge.
   Millennium Falcon: the offset cockpit tube with its glazed nose, mandibles with the docking gap, the
   radar dish, upper and lower quad laser turrets, the long engine glow strip at the back, hull panelling,
   escape-pod hatches, the sensor rectenna and the circular top hatch).
6. Interior only if asked (or for buildings): floors, rooms, stairs, furniture made of stairs/slabs/trapdoors.
7. Critique and fix — at least two rounds before you finish: render all four views and close-ups of each
   part with render_region (mark_region every part), list concretely what is still blocky, flat, wrongly
   proportioned or missing compared with the real thing, then fix those things. Repeat.
Only then: check_walkable if there are interiors, save_structure, export_schem, and a short summary
(scale, size, block count, parts; load the .schem with Litematica/WorldEdit, the .nbt with a structure
block only up to 48x48x48).

Tools
- build_script is your main tool: one script per part, computed shapes, loops for repetition, float maths
  with rounding; use mix() for material variation. Use set_blocks for hand-placed details, fill for big
  solid areas, transform_region + copy_region for symmetry, shape for spheres/cylinders.
- render renders the whole build (scale it so the image is readable); render_region gives close-ups — use
  them, details are invisible in a whole-ship render. layer_text is exact when you need coordinates.

Minecraft facts
- Axes: x = east, y = up, z = south. Java 1.20.1 block ids (minecraft: prefix optional).
- Stairs: facing = the direction of the HIGH (full) side; half=top for upside-down; shape=straight|inner_*|
  outer_* for corners. Slabs: type=bottom|top|double. Logs/pillars: axis=x|y|z. Panes, bars, fences and walls
  connect via north/east/south/west=true (walls use none/low/tall). Trapdoors: facing, half, open.
- "void" leaves a position untouched when placed in a world; "air" carves space.
Be decisive and keep going until it is genuinely detailed; do not ask the user questions mid-build.
Reply in the user's language."""


class Agent:
    def __init__(self):
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.chat = None
        self.cfg: dict = {}
        self.running = False
        self.stop_flag = False
        self.version = 0            # bumps whenever blocks change (the preview re-renders)
        self.current: str | None = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.thread: threading.Thread | None = None
        self.last_png: bytes | None = None

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, text: str = "", **extra) -> None:
        with self.lock:
            self.events.append({"id": len(self.events), "t": time.time(), "kind": kind, "text": text, **extra})

    def since(self, n: int) -> list[dict]:
        with self.lock:
            return self.events[n:]

    # ------------------------------------------------------------------ control
    def reset(self) -> None:
        if self.running:
            raise RuntimeError("Stop the current run first.")
        self.chat = None
        self.events = []
        self.tokens_in = self.tokens_out = 0

    def start(self, message: str, cfg: dict) -> None:
        if self.running:
            raise RuntimeError("Already running — wait or press Stop.")
        # a new connection starts a new conversation; other settings (effort, steps) apply to the next turn
        if self.chat is None or any(cfg.get(k) != self.cfg.get(k) for k in ("provider", "model", "api_key", "base_url")):
            self.chat = make_chat(cfg, SYSTEM)
        if hasattr(self.chat, "effort"):
            self.chat.effort = cfg.get("effort") or "xhigh"
        self.cfg = dict(cfg)
        self.chat.add_user(message)
        self.emit("user", message)
        self.running, self.stop_flag = True, False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag = True

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        specs = tools.tool_specs()
        steps = int(self.cfg.get("max_steps") or 300)
        try:
            for step in range(steps):
                if self.stop_flag:
                    self.emit("status", "Stopped.")
                    return
                self.emit("status", f"Thinking… (step {step + 1})")
                reply = self._turn(specs)
                self.tokens_in += reply.input_tokens
                self.tokens_out += reply.output_tokens
                if reply.text.strip():
                    self.emit("assistant", reply.text.strip())
                if reply.stop == "refusal":
                    self.emit("error", "The model declined this request.")
                    return
                if not reply.calls:
                    self.emit("status", "Done.")
                    return
                results = []
                for call in reply.calls:
                    results.append(self._exec(call))
                    if self.stop_flag:
                        break
                # every call needs an answer, even the ones skipped by Stop
                done = {r[0].id for r in results}
                results += [(c, "Skipped: the user pressed Stop.", None, True) for c in reply.calls if c.id not in done]
                self.chat.add_results(results)
            self.emit("status", f"Stopped after {steps} steps (the step limit). Send 'continue' to go on.")
        except Exception as e:  # noqa: BLE001 — surface any provider error in the UI
            self.emit("error", f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-2000:])
        finally:
            self.running = False

    def _turn(self, specs):
        for attempt in range(3):
            try:
                return self.chat.turn(specs)
            except ValueError as e:      # a streamed tool input that could not be parsed: ask again
                self.emit("status", f"Retrying a garbled reply ({e})")
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if attempt < 2 and any(k in name for k in ("RateLimit", "Overloaded", "InternalServer", "Timeout",
                                                           "APIConnection")):
                    self.emit("status", f"{name}: retrying in {10 * (attempt + 1)} s")
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
        return self.chat.turn(specs)

    def _exec(self, call: Call):
        short = ", ".join(f"{k}={str(v)[:40]}" for k, v in call.args.items() if k not in ("code", "blocks"))
        if "code" in call.args:
            short += f" ({len(str(call.args['code']).splitlines())} lines of script)"
        if "blocks" in call.args:
            short += f" ({len(call.args['blocks'])} blocks)"
        self.emit("tool", short, tool=call.name, code=call.args.get("code"))
        try:
            text, png = tools.call(call.name, call.args)
            is_error = False
        except Exception as e:  # noqa: BLE001 — the model sees the error and fixes its call
            text, png, is_error = f"{type(e).__name__}: {e}", None, True
        if call.name in tools.MUTATING and not is_error:
            self.version += 1
        if call.args.get("name") and call.args.get("name") in server.STRUCTURES:
            self.current = call.args["name"]
        self.emit("result", text[:1500], tool=call.name, error=is_error, image=png is not None)
        if png:
            self.last_png = png
        return call, text[:20000], png, is_error
