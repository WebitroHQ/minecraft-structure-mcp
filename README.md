# minecraft-structure-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude and others) **design Minecraft
structures**: place and fill blocks, read layers back as text, look at the result as isometric renders drawn with
the game's own textures, and save a structure-block `.nbt` file you can load in game.

No game needs to be running, and no Minecraft assets are shipped: models and textures are read from **your own**
Minecraft client jar.

## What it can do

| Tool | |
|---|---|
| `new_structure`, `open_structure`, `save_structure`, `list_structures`, `structure_info`, `resize_structure` | files |
| `set_blocks` (batch), `fill` (replace / hollow / outline / walls / keep), `replace_blocks`, `copy_region`, `set_block_nbt` (chest contents…), `undo` | editing |
| `layer_text` | one layer as a character map — exact and cheap |
| `render` | isometric view from any corner, with `cut_y` to see inside rooms |
| `render_layer` | top-down plan of one layer, with a grid and coordinates |

Renders resolve blockstates and models the way the game does (variants, multipart, parents, element and variant
rotations, face culling, grass/leaf/water tints). Blocks drawn by entity renderers (chests, skulls, signs, beds)
appear as simple placeholder boxes.

## Install

```bash
pip install -e .
```

Then add it to your MCP client. Claude Code:

```bash
claude mcp add minecraft-structure --scope user -- python -m mcstructure_mcp.server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "minecraft-structure": { "command": "python", "args": ["-m", "mcstructure_mcp.server"] } } }
```

### Settings (environment variables)

- `MC_CLIENT_JAR` — path to a Minecraft client jar, e.g. `%APPDATA%/.minecraft/versions/1.20.1/1.20.1.jar`.
  If unset, the newest version in your launcher's `versions` folder (or a Fabric Loom cache) is used.
- `MCSTRUCT_EXTRA_ASSETS` — mod jars or resource folders (containing `assets/`) whose blocks you want to
  render too, separated by `;` on Windows and `:` elsewhere.
- `MCSTRUCT_WORKSPACE` — where `.nbt` files and renders go (default `~/minecraft-structures`).

## Versions

Structures are saved with a `DataVersion` (default **1.20.1**). Save with the **oldest** version you target: newer
games upgrade old structures automatically, older games cannot load newer ones. Stick to blocks that exist in that
version.

## Using the result

- In game: place a structure block in **Load** mode, enter the name (copy the file to
  `<world>/generated/minecraft/structures/<name>.nbt`), and load it.
- From a mod or datapack: put it in `data/<namespace>/structure/` (`structures/` before 1.21) and place it with
  `StructureTemplateManager` / `/place template`.

## License

MIT. Minecraft is a trademark of Mojang; this project is not affiliated with Mojang or Microsoft.
