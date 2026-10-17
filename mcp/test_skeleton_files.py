import json
import os
from pathlib import Path
import tempfile
import unittest

from skeleton_files import (SkeletonFileError, export_sklb, inspect_phyb, inspect_sklb,
                            write_phyb_cloned_chains, write_sklb_cloned_chains,
                            write_sklb_local_translations)


AUDIT = Path(os.environ.get(
    "VFXEDITOR_MCP_TEST_FIXTURE_DIR", Path(__file__).resolve().parent / "fixtures"
))
SKLB = AUDIT / "skl_c0201t9141.sklb"
PHYB = AUDIT / "phy_c0201t9141.phyb"
SKLB_SHA = "e25d1a03da541fc7d5d837b16ffc5068a5a9fc18375f63cc87cd93b9b91864ba"
PHYB_SHA = "efdb70cb2e1a70da158df24e6e9d43829c657ddb0b7e86985ee9ab4456810760"


class SkeletonFileTests(unittest.TestCase):
    def test_sample_sklb_is_fully_parsed_without_native_havok(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        report = inspect_sklb(str(SKLB))
        self.assertEqual(report["source"]["sha256"], SKLB_SHA)
        self.assertEqual(report["havok"]["version"], "hk_2014.2.5-r1")
        self.assertTrue(report["havok"]["fullyConsumed"])
        self.assertEqual(report["skeleton"]["boneCount"], 16)
        self.assertEqual(report["skeleton"]["bones"][6]["name"], "sl_a")
        self.assertEqual(report["skeleton"]["bones"][6]["parentName"], "j_ude_b_l")
        self.assertEqual(report["mappers"]["status"], "absent")
        self.assertEqual([layer["id"] for layer in report["layers"]["layers"]], [4, 5, 25])

    def test_sample_phyb_is_fully_consumed(self):
        if not PHYB.is_file():
            self.skipTest("R356 sample is not present")
        report = inspect_phyb(str(PHYB))
        self.assertEqual(report["source"]["sha256"], "efdb70cb2e1a70da158df24e6e9d43829c657ddb0b7e86985ee9ab4456810760")
        self.assertEqual(report["parseStatus"], "complete-for-known-layout")
        self.assertTrue(report["fullyConsumed"])
        self.assertEqual(report["simulatorCount"], 4)
        self.assertEqual([node["bone"] for node in report["simulators"][2]["chains"][0]["nodes"]],
                         ["sl_a", "sl_b", "sl_c", "sl_d", "sl_e", ""])

    def test_export_gltf_glb_and_default_no_overwrite(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            for suffix in (".glb", ".gltf"):
                with self.subTest(suffix=suffix):
                    output = Path(directory) / f"skeleton_{suffix[1:]}{suffix}"
                    result = export_sklb(str(SKLB), str(output))
                    self.assertTrue(result["readbackVerified"])
                    self.assertTrue(output.is_file())
                    sidecar = output.with_suffix(".skeleton.json")
                    self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8"))["skeleton"]["boneCount"], 16)
                    if suffix == ".gltf":
                        self.assertTrue(output.with_suffix(".bin").is_file())
                    with self.assertRaises(SkeletonFileError):
                        export_sklb(str(SKLB), str(output))

    def test_paths_outside_workspace_are_rejected(self):
        with self.assertRaises(SkeletonFileError):
            inspect_sklb("C:/Windows/not-a-skeleton.sklb")

    def test_sklb_noop_roundtrip_has_full_semantic_readback(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "noop.sklb"
            result = write_sklb_local_translations(str(SKLB), str(output), SKLB_SHA, [])
            self.assertTrue(result["noOp"])
            self.assertTrue(result["verification"]["fullParsedObjectGraphMatchesExpected"])
            self.assertTrue(result["verification"]["containerHeaderAndLayersByteIdentical"])
            self.assertTrue(result["verification"]["nonTargetReferenceTrsPreserved"])
            self.assertTrue(inspect_sklb(str(output))["havok"]["fullyConsumed"])

    def test_sklb_named_translation_edit_preserves_structure(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "sleeve-anchor.sklb"
            result = write_sklb_local_translations(str(SKLB), str(output), SKLB_SHA, [
                {"bone": "sl_a", "translation": [0.125738, 0.013000000268220901, 0]},
                {"bone": "sr_a", "translation": [0.125738, 0.013000000268220901, 0]},
            ])
            self.assertFalse(result["noOp"])
            self.assertEqual([item["bone"] for item in result["editedBones"]], ["sl_a", "sr_a"])
            report = inspect_sklb(str(output))
            bones = {bone["name"]: bone for bone in report["skeleton"]["bones"]}
            self.assertEqual(bones["sl_a"]["parentName"], "j_ude_b_l")
            self.assertEqual(bones["sr_a"]["parentName"], "j_ude_b_r")
            self.assertAlmostEqual(bones["sl_a"]["localReference"]["translation"][0], 0.125738, places=6)
            self.assertAlmostEqual(bones["sr_a"]["localReference"]["translation"][0], 0.125738, places=6)
            self.assertEqual([layer["id"] for layer in report["layers"]["layers"]], [4, 5, 25])
            self.assertEqual(report["mappers"]["status"], "absent")

    def test_sklb_edit_rejects_invalid_requests_and_overwrite(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "guarded.sklb"
            bad_edits = [
                [{"bone": "missing", "translation": [0, 0, 0]}],
                [{"bone": "sl_a", "translation": [0, 0, 0]},
                 {"bone": "sl_a", "translation": [1, 0, 0]}],
                [{"bone": "sl_a", "translation": [101, 0, 0]}],
                [{"bone": "sl_a", "translation": [float("nan"), 0, 0]}],
            ]
            for edits in bad_edits:
                with self.subTest(edits=edits), self.assertRaises(SkeletonFileError):
                    write_sklb_local_translations(str(SKLB), str(output), SKLB_SHA, edits)
            with self.assertRaises(SkeletonFileError):
                write_sklb_local_translations(str(SKLB), str(output), "0" * 64, [])
            write_sklb_local_translations(str(SKLB), str(output), SKLB_SHA, [])
            with self.assertRaises(SkeletonFileError):
                write_sklb_local_translations(str(SKLB), str(output), SKLB_SHA, [])
            with self.assertRaises(SkeletonFileError):
                write_sklb_local_translations(str(SKLB), str(SKLB), SKLB_SHA, [], overwrite=True)

    def test_sklb_chain_append_preserves_old_bones_and_reads_new_chain(self):
        if not SKLB.is_file():
            self.skipTest("R356 sample is not present")
        before = inspect_sklb(str(SKLB))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "cloned.sklb"
            new_names = [f"test_sl_A{letter}" for letter in "abcde"]
            result = write_sklb_cloned_chains(str(SKLB), str(output), SKLB_SHA, [{
                "sourceBones": [f"sl_{letter}" for letter in "abcde"],
                "newBones": new_names,
                "rootParent": "j_ude_b_l",
                "rootTranslation": [0.0, 0.013, 0.0],
            }])
            after = inspect_sklb(str(output))
            without_children = lambda bone: {key: value for key, value in bone.items() if key != "children"}
            self.assertEqual([without_children(bone) for bone in after["skeleton"]["bones"][:16]],
                             [without_children(bone) for bone in before["skeleton"]["bones"]])
            self.assertEqual(after["skeleton"]["boneCount"], 21)
            bones = {bone["name"]: bone for bone in after["skeleton"]["bones"]}
            self.assertEqual(bones[new_names[0]]["parentName"], "j_ude_b_l")
            self.assertEqual(bones[new_names[1]]["parentName"], new_names[0])
            self.assertEqual(bones[new_names[0]]["localReference"]["translation"], [0.0, 0.013000000268220901, 0.0])
            self.assertEqual(bones[new_names[1]]["localReference"], bones["sl_b"]["localReference"])
            self.assertTrue(result["verification"]["allOriginalStoredBoneFieldsAndRestSemanticallyIdentical"])
            self.assertEqual(result["newBoneCount"], 5)
            with self.assertRaises(SkeletonFileError):
                write_sklb_cloned_chains(str(SKLB), str(output), SKLB_SHA, [], overwrite=True)

    def test_phyb_chain_clone_preserves_original_and_empty_terminal(self):
        if not PHYB.is_file():
            self.skipTest("R356 sample is not present")
        before = inspect_phyb(str(PHYB))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            output = Path(directory) / "cloned.phyb"
            new_names = [f"test_sl_A{letter}" for letter in "abcde"]
            result = write_phyb_cloned_chains(str(PHYB), str(output), PHYB_SHA, [{
                "sourceSimulator": 2, "sourceChain": 0, "newBones": new_names,
            }])
            after = inspect_phyb(str(output))
            self.assertTrue(after["fullyConsumed"])
            self.assertEqual(after["simulatorCount"], 4)
            self.assertEqual([len(simulator["chains"]) for simulator in after["simulators"]], [1, 1, 2, 1])
            self.assertEqual(after["simulators"][2]["chains"][0]["nodes"],
                             before["simulators"][2]["chains"][0]["nodes"])
            self.assertEqual([node["bone"] for node in after["simulators"][2]["chains"][1]["nodes"]],
                             new_names + [""])
            self.assertTrue(result["verification"]["allOriginalChainParametersAndNodesPreserved"])
            self.assertTrue(result["verification"]["emptyTerminalNodesPreserved"])


if __name__ == "__main__":
    unittest.main()
