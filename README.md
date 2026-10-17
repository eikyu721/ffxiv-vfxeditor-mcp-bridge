# VFXEditor MCP Bridge

A safety-scoped MCP bridge derived from [0ceal0t/Dalamud-VFXEditor](https://github.com/0ceal0t/Dalamud-VFXEditor). It adds headless PAP inspection/export, local SKLB/PHYB inspection, and narrowly owned GPose actor-animation control without replacing game resources.

This is an independent fork with its own assembly, Dalamud identity, command, and loopback endpoint. It can coexist with the upstream VFXEditor plugin.

## What it provides

- Search and inspect PAP animations and export them as glTF.
- Inspect local SKLB skeletons and export them as glTF/GLB.
- Write a separately named SKLB with explicit, hash-guarded local bone-translation edits.
- Inspect supported PHYB chain, node, gravity, wind, damping, and collision fields.
- Play a real ActionTimeline on the current GPose target.
- Freeze a zero-based frame only when Brio confirms ownership of the target actor.
- Query, resume, and reset bridge-owned animation state.
- Read a deliberately restricted set of sleeve bones from the current GPose target.

The bridge does **not** replace game resources, change Penumbra mappings or collections, redraw actors, or expose unrestricted native skeleton traversal. The experimental glove-binding reader is disabled because an earlier implementation caused a confirmed access violation on the CN client.

See [MCP_README.md](MCP_README.md) for the full tool list, build instructions, safety model, ownership rules, and known limitations.

## Build

Requirements: Dalamud API 15 development assemblies, .NET 10 SDK, Python 3.11, and [uv](https://docs.astral.sh/uv/).

```powershell
git clone --recurse-submodules <repository-url>
cd ffxiv-vfxeditor-mcp-bridge
dotnet build VFXEditor/VFXEditor.csproj -c Debug -p:DalamudLibPath="C:\path\to\Dalamud\dev"
cd mcp
uv sync
uv run python -m unittest discover -v
```

Load `VFXEditor/bin/Debug/VFXEditorMcpBridge.dll` as a Dalamud development plugin. `/vfxmcpbridge` reports the local endpoint and export directory.

## Upstream and license

This project retains the upstream commit history and MIT license. Original VFXEditor copyright and attribution remain in [LICENSE](LICENSE). The MCP-specific changes are maintained as a fork and are not affiliated with Square Enix, Dalamud, Brio, or the upstream VFXEditor author.

FFXIV and related marks belong to their respective owners. Use development plugins and game-memory tooling at your own risk.
