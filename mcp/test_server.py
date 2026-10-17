from pathlib import Path
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
import server
import capture_actor_sleeve_skeleton as capture


class ServerTests(unittest.TestCase):
    def test_tools_include_export_and_scoped_actor_control(self):
        tools = server.mcp._tool_manager.list_tools()
        self.assertEqual({tool.name for tool in tools}, {
            "vfx_animation_status", "inspect_actor_sleeve_skeleton", "inspect_glove_bone_bindings",
            "search_pap_animations",
            "inspect_pap_animation_file", "export_pap_animation_gltf",
            "inspect_local_sklb_skeleton", "export_local_sklb_skeleton",
            "write_local_sklb_bone_translations", "inspect_local_phyb",
            "play_actor_animation", "freeze_actor_animation",
            "get_actor_animation_state", "resume_actor_animation",
            "reset_actor_animation"})

    def test_local_skeleton_tools_map_without_game_bridge(self):
        source = str(capture.WORKSPACE_ROOT / "a.sklb")
        gltf = str(capture.WORKSPACE_ROOT / "a.glb")
        output = str(capture.WORKSPACE_ROOT / "b.sklb")
        with patch.object(server, "inspect_sklb", return_value={"ok": True}) as inspect:
            self.assertTrue(server.inspect_local_sklb_skeleton(source)["ok"])
            inspect.assert_called_once_with(source)
        with patch.object(server, "export_sklb", return_value={"ok": True}) as export:
            self.assertTrue(server.export_local_sklb_skeleton(
                source, gltf)["ok"])
            export.assert_called_once_with(source, gltf, False)
        with patch.object(server, "inspect_phyb", return_value={"ok": True}) as inspect:
            phyb = str(capture.WORKSPACE_ROOT / "a.phyb")
            self.assertTrue(server.inspect_local_phyb(phyb)["ok"])
            inspect.assert_called_once_with(phyb)
        with patch.object(server, "write_sklb_local_translations", return_value={"ok": True}) as write:
            self.assertTrue(server.write_local_sklb_bone_translations(
                source, output, "a" * 64,
                [{"bone": "sl_a", "translation": [1, 2, 3]}])["ok"])
            write.assert_called_once_with(
                source, output, "a" * 64,
                [{"bone": "sl_a", "translation": [1, 2, 3]}], False)
        with self.assertRaises(ToolError):
            server.export_local_sklb_skeleton("a.sklb", "a.glb", overwrite="yes")
        with self.assertRaises(ToolError):
            server.write_local_sklb_bone_translations(
                "a.sklb", "b.sklb", "a" * 64, [], overwrite="yes")

    def test_search_validation_and_mapping(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            server.search_pap_animations("坐下", "c0801", False)
            bridge.assert_called_once_with("search_pap", query="坐下", raceCode="c0801", includeCommon=False)
        for race in ("0801", "c801", "m0801"):
            with self.assertRaises(ToolError):
                server.search_pap_animations("sit", race)

    def test_live_sleeve_skeleton_maps_to_read_only_command(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            self.assertTrue(server.inspect_actor_sleeve_skeleton()["ok"])
            bridge.assert_called_once_with("inspect_actor_sleeve_skeleton")
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            self.assertTrue(server.inspect_glove_bone_bindings()["ok"])
            bridge.assert_called_once_with("inspect_glove_bone_bindings")
        valid = str(capture.WORKSPACE_ROOT / "new-report.json")
        outside = str(capture.WORKSPACE_ROOT.parent / "outside-report.json")
        invalid_suffix = str(capture.WORKSPACE_ROOT / "report.txt")
        self.assertEqual(capture.safe_output_path(valid).suffix, ".json")
        for path in (outside, invalid_suffix):
            with self.subTest(path=path), self.assertRaises(ValueError):
                capture.safe_output_path(path)

    def test_glove_binding_endpoint_is_guarded_before_native_access(self):
        source = (Path(__file__).parents[1] / "VFXEditor" / "ActorSkeletonInspector.cs").read_text(encoding="utf-8")
        body = source.split("public static object InspectGloveBoneBindings()", 1)[1].split("public static object InspectSleeveBones()", 1)[0]
        self.assertIn("enabled = false", body)
        self.assertIn("disabled before actor/native access", body)
        for forbidden in ("CurrentGposeCharacterBase", "AttachBones", "AttachBoneMasks", "BoneList", "BoneName"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_sleeve_whitelist_includes_fixed_shoulder_arm_and_hand_chain(self):
        source = (Path(__file__).parents[1] / "VFXEditor" / "ActorSkeletonInspector.cs").read_text(encoding="utf-8")
        whitelist = source.split("private static readonly HashSet<string> TargetNames", 1)[1].split("};", 1)[0]
        for name in ("j_sako_l", "j_sako_r", "j_ude_a_l", "j_ude_a_r", "j_ude_b_l", "j_ude_b_r", "j_te_l", "j_te_r"):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', whitelist)

    def test_pap_path_rejects_local_and_traversal(self):
        for path in ("C:/game/a.pap", "../a.pap", "/a.pap", "a.txt", ""):
            with self.subTest(path=path), self.assertRaises(ToolError):
                server.inspect_pap_animation_file(path)

    def test_export_defaults_to_complete_skeleton(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            server.export_pap_animation_gltf("chara/human/c0801/a.pap", "groundsit.gltf", animation_name="cbem_jmn")
            bridge.assert_called_once_with("export_animation", papPath="chara/human/c0801/a.pap",
                outputName="groundsit.gltf", animationName="cbem_jmn", animationIndex=None, skipUnanimated=False)

    def test_export_name_and_selector_validation(self):
        for name in ("../x.gltf", "x.glb", "路径.gltf"):
            with self.assertRaises(ToolError):
                server.export_pap_animation_gltf("a.pap", name, animation_index=0)
        with self.assertRaises(ToolError):
            server.export_pap_animation_gltf("a.pap", "x.gltf")

    def test_offline_is_reported(self):
        with patch.object(server.socket, "create_connection", side_effect=OSError):
            with self.assertRaises(ToolError):
                server.vfx_animation_status()

    def test_play_validation_and_mapping(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            server.play_actor_animation(123, "chara/action/test.tmb", "forced_replay")
            bridge.assert_called_once_with("play_actor_animation", timelineId=123,
                                           tmbPath="chara/action/test.tmb", forceReplay=True)
        for kwargs in ({}, {"action_timeline_id": 0}, {"tmb_path": "a.pap"}, {"tmb_path": "C:/a.tmb"},
                       {"action_timeline_id": 1, "loop_mode": "loop"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                server.play_actor_animation(**kwargs)

    def test_freeze_validation_and_mapping(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            server.freeze_actor_animation(42, 60)
            bridge.assert_called_once_with("freeze_actor_animation", frame=42, fps=60.0)
        for frame, fps in ((-1, 30), (1.5, 30), (1, 0), (1, 241), (True, 30)):
            with self.subTest(frame=frame, fps=fps), self.assertRaises(ToolError):
                server.freeze_actor_animation(frame, fps)

    def test_control_commands(self):
        with patch.object(server, "call_bridge", return_value={"ok": True}) as bridge:
            server.get_actor_animation_state()
            server.resume_actor_animation()
            server.reset_actor_animation()
            self.assertEqual([call.args[0] for call in bridge.call_args_list], [
                "get_actor_animation_state", "resume_actor_animation", "reset_actor_animation"])


if __name__ == "__main__":
    unittest.main()
