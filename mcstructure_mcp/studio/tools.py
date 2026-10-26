"""The building tools the Studio's model can call: the MCP server's own tools (same code, same behaviour) plus
build_script, a small sandboxed Python for loops and curves (hulls, domes, rows of windows)."""
from __future__ import annotations

import ast
import inspect
import math
import random
import sys
import time
import types
import typing
from typing import Any, Literal

from .. import server
from ..shapes import Pattern

#: Tools exposed to the model, in the order it sees them.
TOOL_NAMES = [
    "new_structure", "open_structure", "save_structure", "structure_info", "resize_structure",
    "build_script", "set_blocks", "fill", "replace_blocks", "copy_region", "set_block_nbt", "undo",
    "transform_region", "roof_gable", "shape",
    "mark_region", "list_regions", "clear_region", "delete_region",
    "layer_text", "check_walkable", "render", "render_region", "render_layer",
    "export_schem", "export_mcfunction",
]
#: Tools that change blocks (the preview refreshes after them).
MUTATING = {"new_structure", "open_structure", "resize_structure", "build_script", "set_blocks", "fill",
            "replace_blocks", "copy_region", "set_block_nbt", "undo", "transform_region", "roof_gable", "shape",
            "clear_region"}


# ------------------------------------------------------------------ build_script
_BANNED_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "exit", "quit", "help", "memoryview", "type", "object", "super",
    "classmethod", "staticmethod", "property", "dir", "id", "hash",
}
_SAFE_BUILTINS = {n: getattr(__builtins__, n) if not isinstance(__builtins__, dict) else __builtins__[n]
                  for n in ("range", "len", "min", "max", "abs", "int", "float", "round", "sum", "enumerate", "zip",
                            "list", "dict", "tuple", "set", "sorted", "reversed", "any", "all", "str", "bool",
                            "divmod", "pow", "map", "filter", "isinstance", "ValueError", "True", "False", "None")
                  if (n in __builtins__ if isinstance(__builtins__, dict) else hasattr(__builtins__, n))}
SCRIPT_SECONDS = 60
SCRIPT_MAX_BLOCKS = 3_000_000


class ScriptError(Exception):
    pass


def _check_script(code: str) -> ast.Module:
    tree = ast.parse(code, "<build_script>")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise ScriptError(f"line {getattr(node, 'lineno', '?')}: imports, global and async are not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ScriptError(f"line {node.lineno}: attributes starting with _ are not allowed")
        if isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or node.id.startswith("__")):
            raise ScriptError(f"line {node.lineno}: '{node.id}' is not available")
    return tree


def run_build_script(name: str, code: str) -> str:
    """Run a build script against structure `name`. See the tool description for the API."""
    s = server.get(name)
    tree = _check_script(code)
    s.snapshot()
    placed = [0]
    out: list[str] = []
    sx, sy, sz = s.size

    def block(x, y, z, state):
        x, y, z = int(round(x)), int(round(y)), int(round(z))
        if 0 <= x < sx and 0 <= y < sy and 0 <= z < sz:
            st = state.pick() if isinstance(state, Pattern) else str(state)
            s.set(x, y, z, None if st == "void" else st)
            placed[0] += 1
            if placed[0] > SCRIPT_MAX_BLOCKS:
                raise ScriptError("too many blocks placed (limit 3,000,000)")

    def fill(x1, y1, z1, x2, y2, z2, state, hollow=False):
        ax, bx = sorted((int(x1), int(x2)))
        ay, by = sorted((int(y1), int(y2)))
        az, bz = sorted((int(z1), int(z2)))
        for x in range(max(ax, 0), min(bx, sx - 1) + 1):
            for y in range(max(ay, 0), min(by, sy - 1) + 1):
                for z in range(max(az, 0), min(bz, sz - 1) + 1):
                    edge = x in (ax, bx) or y in (ay, by) or z in (az, bz)
                    block(x, y, z, state if edge or not hollow else "air")

    def get(x, y, z):
        return s.blocks.get((int(x), int(y), int(z)))

    def mix(text, seed=None):
        return Pattern(text, seed)

    def say(*parts):
        out.append(" ".join(str(p) for p in parts))

    env: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS, print=say),
        "block": block, "fill": fill, "get": get, "mix": mix, "print": say,
        "SX": sx, "SY": sy, "SZ": sz, "math": _MathView(), "rng": random.Random(1234),
    }

    deadline = time.monotonic() + SCRIPT_SECONDS
    counter = [0]

    def lines(frame, event, arg):
        counter[0] += 1
        if counter[0] % 20000 == 0 and time.monotonic() > deadline:
            raise ScriptError(f"script ran longer than {SCRIPT_SECONDS}s")
        return lines

    def tracer(frame, event, arg):   # only the script's own lines are counted (block() etc. run untraced)
        return lines if frame.f_code.co_filename == "<build_script>" else None

    before = len(s.blocks)
    old = sys.gettrace()
    sys.settrace(tracer)
    try:
        exec(compile(tree, "<build_script>", "exec"), env)
    except ScriptError:
        raise
    except Exception as e:  # report the script's own error with its line number
        tb = e.__traceback__
        line = None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == "<build_script>":
                line = tb.tb_lineno
            tb = tb.tb_next
        raise ScriptError(f"{type(e).__name__} at line {line}: {e}") from None
    finally:
        sys.settrace(old)
    msg = f"Script placed {placed[0]} blocks ('{name}' now has {len(s.blocks)} blocks, was {before})."
    if out:
        msg += "\nprint output:\n" + "\n".join(out[:200])
    return msg


class _MathView:
    """math without the module object itself (no attribute walking to anything else)."""

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(math, item)


def build_script(name: str, code: str) -> str:
    """Run a short Python script to build with loops and maths — the way to make hulls, curved walls, domes,
    rows of windows, railings, repeated decks. Available: block(x,y,z,state); fill(x1,y1,z1,x2,y2,z2,state,
    hollow=False); get(x,y,z) -> state or None; mix("stone*3,cobblestone") -> a weighted mix you can pass as
    state; SX, SY, SZ (structure size); math (sin, cos, sqrt, pi...); rng (random.Random); print(...).
    Out-of-bounds positions are skipped. No imports, no files. Limit 60 s. Everything the script does is one
    undo step. Prefer one script per part (hull, deck, funnel) over one giant script."""
    return run_build_script(name, code)


# ------------------------------------------------------------------ schemas
def _schema(tp) -> dict:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union or isinstance(tp, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        return _schema(inner[0]) if len(inner) == 1 else {}
    if origin is Literal:
        return {"type": "string" if isinstance(args[0], str) else "number", "enum": list(args)}
    if origin in (list, typing.List):
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin in (dict, typing.Dict) or tp is dict:
        return {"type": "object"}
    return {int: {"type": "integer"}, float: {"type": "number"}, str: {"type": "string"},
            bool: {"type": "boolean"}}.get(tp, {})


def _function(name: str):
    return build_script if name == "build_script" else getattr(server, name)


def tool_specs() -> list[dict]:
    """[{name, description, parameters (JSON schema)}] for every tool."""
    specs = []
    for name in TOOL_NAMES:
        fn = _function(name)
        hints = typing.get_type_hints(fn)
        props, required = {}, []
        for p in inspect.signature(fn).parameters.values():
            sch = _schema(hints.get(p.name, str))
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
            else:
                sch = dict(sch, default=p.default) if p.default is not None else sch
            props[p.name] = sch
        specs.append({
            "name": name,
            "description": inspect.getdoc(fn) or name,
            "parameters": {"type": "object", "properties": props, "required": required},
        })
    return specs


def call(name: str, args: dict) -> tuple[str, bytes | None]:
    """Run a tool: (text result, PNG bytes if it rendered something)."""
    if name not in TOOL_NAMES:
        raise ValueError(f"Unknown tool '{name}'")
    fn = _function(name)
    allowed = set(inspect.signature(fn).parameters)
    args = {k: v for k, v in (args or {}).items() if k in allowed}
    result = fn(**args)
    if hasattr(result, "data") and isinstance(getattr(result, "data", None), bytes):
        return f"Rendered ({name}).", result.data
    return str(result), None
