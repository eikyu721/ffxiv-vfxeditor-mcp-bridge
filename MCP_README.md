# VFXEditor MCP Bridge

This is a headless PAP inspection/glTF export and scoped actor-animation-control fork of
[Dalamud-VFXEditor](https://github.com/0ceal0t/Dalamud-VFXEditor), retained under the upstream MIT license in `LICENSE`.

Identity is isolated from upstream: assembly `VFXEditorMcpBridge`, Dalamud `InternalName`/name `VFXEditorMcpBridge` / `VFXEditor MCP Bridge`, command `/vfxmcpbridge`, TCP endpoint `127.0.0.1:42072`, and no configuration or UI state.

The game-data tools remain read-only and their PAP exports are bounded to `.gltf` + `.bin` under the loaded plugin's `McpExports` directory. Four additional local-file tools inspect SKLB/PHYB, export a reference skeleton, and create a separately named SKLB with explicit local-translation edits without connecting to the game. The actor tools can apply a real ActionTimeline to the current GPose target, freeze a zero-based frame, query readback state, resume, and reset. They never replace a game resource, edit Penumbra mappings, redraw, or initialize upstream VFX resource hooks.

Actor playback accepts an ActionTimeline ID or an internal `.tmb` path that resolves to one. An arbitrary `.pap` path is not directly playable. Scrubbing intentionally touches only the main body `partial 0 / control 0`; face, weapon, and other composite controls are not blindly synchronized. `loop_mode=native` leaves the authored behavior alone, while `forced_replay` explicitly restarts the timeline near its main-control end.

Frame freeze is available only in GPose when Brio API 2 or 3 confirms it manages the target actor. API 2 uses the legacy `Brio.Actor.*` endpoints; API 3 uses its generated versioned endpoints (`Brio.ActorExists.V3`, `Brio.GetActorSpeed.V3`, and `Brio.SetActorSpeed.V3`). Normal-mode and unmanaged-actor freeze requests are rejected before any Havok or speed write. A pre-existing non-1 Brio speed is treated as an ownership conflict. Brio exposes no IPC to clear its nullable speed override, so after a bridge-owned Brio freeze, reset sets it to `1` and reports `brioSpeedOverrideMayRemainAtOne=true`; it never intentionally leaves speed `0`.

After requesting speed 0, the bridge waits until both the actor `OverallSpeed` and main Havok control `PlaybackSpeed` read back as zero before seeking. This avoids treating Brio's accepted request as proof that the lower-level control has already stopped. Readiness requires the requested slot-0 timeline, two observations of the same control/binding/animation identity, and agreement between the Havok local time and the timeline scheduler's 30fps clock. A cross-timeline play must also replace the pre-play control identity. If a finished/non-looping control is retained while its scheduler has restarted, the bridge reissues the requested timeline once and waits again, with a bounded 120-tick limit. The scrub then follows Brio's local-time-only path; at most three one-tick `LocalTime` writes are attempted, and the readback must quantize to the requested frame before success. Failure automatically releases the pause.

Reset records the actual slot-0 timeline before takeover and restores that slot together with the original mode and base override. Timeline 3 is used only when the pre-takeover slot was empty; reset verification no longer assumes that every GPose actor started on timeline 3.

Playback enters `AnimLock` through the game's `Character.SetMode`. Reset exits through the same function, then waits eight framework ticks and verifies mode, mode parameter, base override, slot 0, and positive speed before releasing bridge ownership.

Build against the CN API 15 development assemblies:

```powershell
dotnet build VFXEditor/VFXEditor.csproj -c Debug -p:DalamudLibPath='C:\path\to\Dalamud\dev\'
```

Load `VFXEditor/bin/Debug/VFXEditorMcpBridge.dll` as a dev plugin. This may coexist with the original VFXEditor because its plugin and command identities differ. Use `/vfxmcpbridge` only to show endpoint/export-root status.

Python MCP setup:

```powershell
cd mcp
uv sync
uv run python server.py
```

Codex MCP registration example (adjust paths to the clone):

```toml
[mcp_servers.vfxeditor]
type = "stdio"
command = 'C:\path\to\repo\mcp\.venv\Scripts\python.exe'
args = ['C:\path\to\repo\mcp\server.py']
```

Local skeleton/physics tools (they do not require the Dalamud DLL to be loaded):

- `inspect_local_sklb_skeleton(sklb_path)` parses a local SKLB under the configured workspace root, including its header, raw parent-skeleton pairs, layers, Havok 2014 binary tagfile, bones, hierarchy, and local reference TRS. It reports mapper absence separately from unsupported mapper semantics.
- `export_local_sklb_skeleton(sklb_path, output_path, overwrite=false)` creates `.glb` or `.gltf` + `.bin` and a `.skeleton.json` sidecar. Input and output are restricted to the configured workspace root; every target refuses overwrite by default. It reads the result back and verifies bone order before returning hashes.
- `write_local_sklb_bone_translations(sklb_path, output_path, expected_source_sha256, edits, overwrite=false)` fully parses the Havok tagfile and writes a distinct `.sklb` after changing only named bones' `localReference.translation`. Each edit is `{"bone":"sl_a","translation":[x,y,z]}`. Source SHA mismatch, duplicate/missing bone names, non-finite or ±100-out-of-range values, in-place output, unsupported parsed fields, and accidental overwrite are rejected. Before placement it reopens the candidate and compares the complete supported typed object graph; the original container header and layer bytes must be byte-identical. The Havok string-pool encoding can be canonicalized, so a semantic no-op is not promised to be byte-identical. This is structural validation only, not proof that FFXIV physics will behave as intended.
- `inspect_local_phyb(phyb_path)` parses known PHYB simulator/chain/node data, gravity, wind, damping, and collision references. Unknown non-chain or extended sections are explicitly partial/refused rather than silently discarded. This is byte parsing, not a physics simulation.

Example sleeve-anchor candidate:

```json
{
  "sklb_path": "C:/path/inside/workspace/source.sklb",
  "output_path": "C:/path/inside/workspace/source-edited.sklb",
  "expected_source_sha256": "E25D1A03DA541FC7D5D837B16FFC5068A5A9FC18375F63CC87CD93B9B91864BA",
  "edits": [
    {"bone": "sl_a", "translation": [0.125738, 0.013000000268220901, 0]},
    {"bone": "sr_a", "translation": [0.125738, 0.013000000268220901, 0]}
  ]
}
```

For Blender, import the GLB/glTF normally. Values are copied from Havok local QsTransforms into right-handed Y-up glTF; Blender performs its standard Y-up to Z-up conversion. When later round-tripping through VFXEditor, keep “Guess Original Bind Pose” disabled as recommended by its skeleton workflow.

`inspect_actor_sleeve_skeleton()` is a deliberately narrow live readback for the current GPose target. It scans every partial but emits only `j_ude_b_l/r`, `sl_a..e`, `sr_a..e`, and the explicitly enumerated R390 experiment names `r387_sl_Aa..Ce` / `r387_sr_Aa..Ce`. It does not accept arbitrary bone-name input or expose an unrestricted skeleton walk. It reports parent indices, reference local/model TRS, direct current local/model pose arrays, pose-slot availability, and synchronization flags. It follows Brio's reviewed choice of pose slot 0 but does not call Havok `Access*`/`Sync*`; an unsynchronized array is reported unavailable instead of being mutated as a side effect. Current model transforms are partial-skeleton model space, not actor/world space, and partial-to-base attachment is reported separately rather than folded in.

`inspect_glove_bone_bindings()` is retained only as a compatibility endpoint and returns `enabled=false` before any actor/native access. The experimental `Model.BoneList` and `Skeleton.AttachBones`/mask reads were removed after a confirmed CN-client `AccessViolationException`. Use offline MDL auditing and the previously validated `inspect_actor_sleeve_skeleton()` pose reader instead.

The workspace root defaults to the parent directory containing the repository. Set `FFXIV_MCP_WORKSPACE_ROOT` for another bounded root. If a Codex task has not refreshed MCP tool discovery yet, after manually reloading the game plugin it can still capture the same report with `uv run python capture_actor_sleeve_skeleton.py C:/path/inside/workspace/NEW_REPORT.json`. The helper requires a new `.json` path under the workspace and refuses overwrite.

All tools: `vfx_animation_status`, `inspect_actor_sleeve_skeleton`, `inspect_glove_bone_bindings`, `search_pap_animations`, `inspect_pap_animation_file`, `export_pap_animation_gltf`, `inspect_local_sklb_skeleton`, `export_local_sklb_skeleton`, `write_local_sklb_bone_translations`, `inspect_local_phyb`, `play_actor_animation`, `freeze_actor_animation`, `get_actor_animation_state`, `resume_actor_animation`, and `reset_actor_animation`.
