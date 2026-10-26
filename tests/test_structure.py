"""Tests that need no Minecraft jar: editing, the .nbt round trip, block-state parsing."""
from mcstructure_mcp.structure import DATA_VERSIONS, Structure, normalize_state, parse_state


def test_state_parsing():
    assert normalize_state("oak_stairs[half=bottom,facing=east]") == "minecraft:oak_stairs[facing=east,half=bottom]"
    assert parse_state("araz:dolap[facing=west]") == ("araz:dolap", {"facing": "west"})


def test_roundtrip(tmp_path):
    s = Structure((4, 3, 2))
    s.set(0, 0, 0, "stone")
    s.set(3, 2, 1, "chest[facing=north,type=single,waterlogged=false]")
    path = s.save(tmp_path / "t.nbt")
    t = Structure.load(path)
    assert t.size == (4, 3, 2)
    assert t.get(0, 0, 0) == "minecraft:stone"
    assert t.get(3, 2, 1).startswith("minecraft:chest[")
    assert t.data_version == DATA_VERSIONS["1.20.1"]


def test_outside_rejected():
    s = Structure((2, 2, 2))
    try:
        s.set(2, 0, 0, "stone")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_undo():
    s = Structure((2, 2, 2))
    s.snapshot()
    s.set(0, 0, 0, "stone")
    assert s.undo() and s.get(0, 0, 0) is None


def _stairwell(ceiling_open: bool):
    """Two floors joined by a 3-step flight going east; the upper floor has a hole over the steps or not."""
    from mcstructure_mcp.walkable import check
    s = Structure((8, 7, 3))
    for x in range(8):
        for z in range(3):
            s.set(x, 0, z, "stone")
            if not (ceiling_open and 1 <= x <= 3):
                s.set(x, 3, z, "oak_planks")
            s.set(x, 6, z, "stone")           # roof, so the flight is "covered"
    for i in range(3):
        s.set(1 + i, 1 + i, 1, "oak_stairs[facing=east,half=bottom]")
    return check(s)


def test_stairs_walkable():
    problems, n = _stairwell(ceiling_open=True)
    assert n["stairs"] == 3 and not problems, problems


def test_stairs_hit_ceiling():
    problems, _ = _stairwell(ceiling_open=False)
    assert any("head room" in p.text or "blocked right above" in p.text for p in problems)


def test_door_needs_upper_half():
    from mcstructure_mcp.walkable import check
    s = Structure((3, 3, 3))
    for x in range(3):
        for z in range(3):
            s.set(x, 0, z, "stone")
    s.set(1, 1, 1, "oak_door[facing=north,half=lower,hinge=left,open=false,powered=false]")
    problems, n = check(s)
    assert n["doors"] == 1 and any("upper half" in p.text for p in problems)


def test_regions_saved_and_cropped(tmp_path):
    s = Structure((5, 5, 5))
    s.set(1, 4, 1, "oak_stairs[facing=east,half=bottom]")
    s.set(0, 0, 0, "stone")
    s.regions["roof"] = ((0, 4, 0), (4, 4, 4))
    t = Structure.load(s.save(tmp_path / "r.nbt"))
    assert t.regions["roof"] == ((0, 4, 0), (4, 4, 4))
    roof = t.crop(t.regions["roof"])
    assert roof.size == (5, 1, 5) and list(roof.blocks) == [(1, 0, 1)]


def test_server_tools_run():
    """The server module loads and its text tools answer (catches a broken tool before it ships)."""
    from mcstructure_mcp import server
    server.STRUCTURES["t"] = Structure((4, 4, 4))
    server.fill("t", 0, 0, 0, 3, 0, 3, "stone")
    assert "region 'top'" in server.mark_region("t", "top", 0, 3, 0, 3, 3, 3).lower()
    assert "All walkable" in server.check_walkable("t", region="top")
    assert "Cleared" in server.clear_region("t", "top")


def test_turning_states():
    from mcstructure_mcp.shapes import _CW, _MIRROR, turn_state
    assert turn_state("minecraft:oak_stairs[facing=north,half=bottom]", _CW) == "minecraft:oak_stairs[facing=east,half=bottom]"
    assert turn_state("minecraft:rail[shape=north_east]", _CW) == "minecraft:rail[shape=south_east]"
    assert turn_state("minecraft:oak_log[axis=x]", _CW) == "minecraft:oak_log[axis=z]"
    door = turn_state("minecraft:oak_door[facing=east,half=lower,hinge=left]", _MIRROR["x"], "x")
    assert door == "minecraft:oak_door[facing=west,half=lower,hinge=right]"


def test_rotate_box_four_times_is_identity():
    from mcstructure_mcp.shapes import transform_box
    s = Structure((5, 2, 5))
    s.set(0, 0, 0, "oak_stairs[facing=north,half=bottom]")
    s.set(2, 1, 0, "stone")
    before = dict(s.blocks)
    box = ((0, 0, 0), (2, 1, 1))
    for _ in range(4):
        box = transform_box(s, box, "cw90")
    assert s.blocks == before


def test_mix_and_exports(tmp_path):
    import nbtlib
    from mcstructure_mcp.export import save_schem, to_mcfunction
    from mcstructure_mcp.shapes import Pattern
    p = Pattern("stone*3,oak_stairs[facing=east,half=bottom]", seed=1)
    assert {p.pick() for _ in range(50)} == {"stone", "oak_stairs[facing=east,half=bottom]"}
    s = Structure((3, 2, 3))
    s.set(1, 0, 1, "chest[facing=north,type=single,waterlogged=false]")
    s.block_nbt[(1, 0, 1)] = nbtlib.parse_nbt('{Items:[{Slot:0b,id:"minecraft:stone",Count:1b}]}')
    f = nbtlib.load(save_schem(s, tmp_path / "s.schem"))
    assert f.root_name == "Schematic" and int(f["Width"]) == 3 and len(f["BlockData"]) == 18
    assert "setblock ~1 ~0 ~1 minecraft:chest[" in to_mcfunction(s)


def test_gable_roof_symmetric():
    from mcstructure_mcp.shapes import gable_roof
    s = Structure((5, 4, 5))
    gable_roof(s, 0, 0, 4, 4, 0, "x", "oak_stairs", "oak_slab", "oak_planks")
    assert s.get(2, 0, 0) == "minecraft:oak_stairs[facing=south,half=bottom]"
    assert s.get(2, 0, 4) == "minecraft:oak_stairs[facing=north,half=bottom]"
    assert s.get(2, 2, 2) == "minecraft:oak_planks"   # the ridge


def test_studio_tools_and_sandbox():
    """The Studio's tool list has schemas, build_script builds, and the sandbox refuses escapes."""
    import pytest
    from mcstructure_mcp import server
    from mcstructure_mcp.studio import tools
    specs = {t["name"]: t for t in tools.tool_specs()}
    assert {"build_script", "render", "fill"} <= specs.keys()
    assert specs["fill"]["parameters"]["properties"]["mode"]["enum"][0] == "replace"
    server.STRUCTURES["sb"] = Structure((10, 10, 10))
    text, png = tools.call("build_script", {"name": "sb", "code": "for i in range(10):\n    block(i, 0, 0, 'stone')"})
    assert "placed 10 blocks" in text and png is None
    for bad in ("import os", "open('x')", "().__class__", "__builtins__"):
        with pytest.raises(tools.ScriptError):
            tools.call("build_script", {"name": "sb", "code": bad})


def test_studio_agent_loop_with_a_scripted_model():
    """One full agent run against a fake model: tool calls run, results go back, the run ends."""
    from mcstructure_mcp.studio import agent as agent_mod
    from mcstructure_mcp.studio.llm import Call, Reply

    class Fake:
        def __init__(self):
            self.n, self.results = 0, []

        def add_user(self, text):
            pass

        def add_results(self, results):
            self.results += results

        def turn(self, tools):
            self.n += 1
            if self.n == 1:
                return Reply("plan", [Call("1", "new_structure", {"name": "ag", "size_x": 5, "size_y": 5, "size_z": 5}),
                                      Call("2", "fill", {"name": "ag", "x1": 0, "y1": 0, "z1": 0, "x2": 4, "y2": 0, "z2": 4,
                                                         "state": "stone"})], 10, 5, "tool_use")
            return Reply("done", [], 10, 5, "end_turn")

    a = agent_mod.Agent()
    fake = Fake()
    a.chat, a.cfg = fake, {"provider": "anthropic", "model": "m", "api_key": "k", "base_url": None}
    a.start("build", dict(a.cfg))
    a.thread.join(10)
    kinds = [e["kind"] for e in a.events]
    assert kinds[0] == "user" and "error" not in kinds and a.events[-1]["text"] == "Done."
    assert a.current == "ag" and a.version == 2 and len(fake.results) == 2 and a.tokens_out == 10
