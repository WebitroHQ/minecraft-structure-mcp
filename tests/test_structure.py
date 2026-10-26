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
