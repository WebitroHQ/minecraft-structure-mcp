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
