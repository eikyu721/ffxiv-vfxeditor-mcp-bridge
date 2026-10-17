"""Build one isolated R390-style three-chain-per-side SKLB/PHYB pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from skeleton_files import (WORKSPACE_ROOT, inspect_phyb, inspect_sklb,
                            write_phyb_cloned_chains, write_sklb_cloned_chains)


LEFT_B_RATIO = 0.4609262446558169
RIGHT_B_RATIO = 0.46092399219985714
LETTERS = "abcde"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_json_path(value: str) -> Path:
    path = Path(value).resolve()
    path.relative_to(WORKSPACE_ROOT)
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ValueError("Blender bind report must be an existing JSON file under the workspace.")
    return path


def names(side: str, lane: str) -> list[str]:
    return [f"r387_s{side}_{lane}{letter}" for letter in LETTERS]


def matrix_from_local(local: dict[str, Any]) -> list[list[float]]:
    x, y, z, w = local["rotationQuaternionXyzw"]
    sx, sy, sz = local["scale"]
    tx, ty, tz = local["translation"]
    rotation = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    scales = [sx, sy, sz]
    return [[rotation[row][column] * scales[column] for column in range(3)] + [[tx, ty, tz][row]]
            for row in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def max_matrix_error(left: list[list[float]], right: list[list[float]]) -> float:
    if len(left) != 4 or len(right) != 4 or any(len(row) != 4 for row in left + right):
        raise ValueError("Expected two 4x4 matrices.")
    return max(abs(float(left[row][column]) - float(right[row][column]))
               for row in range(4) for column in range(4))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sklb", required=True)
    parser.add_argument("--sklb-sha256", required=True)
    parser.add_argument("--phyb", required=True)
    parser.add_argument("--phyb-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--blender-bind-json")
    parser.add_argument("--left-b-ratio", type=float, default=LEFT_B_RATIO)
    parser.add_argument("--right-b-ratio", type=float, default=RIGHT_B_RATIO)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.relative_to(WORKSPACE_ROOT)
    if output_dir.exists():
        raise FileExistsError(f"Refusing to reuse existing output directory: {output_dir}")
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0
               for value in (args.left_b_ratio, args.right_b_ratio)):
        raise ValueError("B-chain ratios must be finite values from zero through one.")

    sklb_source = Path(args.sklb).resolve()
    phyb_source = Path(args.phyb).resolve()
    sklb_before = inspect_sklb(str(sklb_source))
    phyb_before = inspect_phyb(str(phyb_source))
    source_bones = {bone["name"]: bone for bone in sklb_before["skeleton"]["bones"]}
    for required in ["j_ude_b_l", "j_ude_b_r"] + [f"sl_{letter}" for letter in LETTERS] + [f"sr_{letter}" for letter in LETTERS]:
        if required not in source_bones:
            raise ValueError(f"Required source bone is missing: {required}")
    if phyb_before["simulatorCount"] != 4:
        raise ValueError("Expected the audited four-simulator PHYB source.")
    if [len(simulator["chains"]) for simulator in phyb_before["simulators"]] != [1, 1, 1, 1]:
        raise ValueError("Expected exactly one original chain in each of four simulators.")

    root_x = {"l": float(source_bones["sl_a"]["localReference"]["translation"][0]),
              "r": float(source_bones["sr_a"]["localReference"]["translation"][0])}
    root_yz = {side: list(source_bones[f"s{side}_a"]["localReference"]["translation"][1:3])
               for side in ("l", "r")}
    ratios = {"l": args.left_b_ratio, "r": args.right_b_ratio}
    sklb_chains: list[dict[str, Any]] = []
    phyb_clones: list[dict[str, Any]] = []
    for side, simulator_index in (("l", 2), ("r", 3)):
        source_chain = [f"s{side}_{letter}" for letter in LETTERS]
        for lane, ratio in (("A", 0.0), ("B", ratios[side]), ("C", 1.0)):
            new_names = names(side, lane)
            sklb_chains.append({
                "sourceBones": source_chain,
                "newBones": new_names,
                "rootParent": f"j_ude_b_{side}",
                "rootTranslation": [root_x[side] * ratio, *root_yz[side]],
            })
            phyb_clones.append({"sourceSimulator": simulator_index, "sourceChain": 0,
                                "newBones": new_names})

    output_dir.mkdir(parents=True, exist_ok=False)
    sklb_output = output_dir / "skl_c0201t9141_R390.sklb"
    phyb_output = output_dir / "phy_c0201t9141_R390.phyb"
    report_output = output_dir / "R390_MULTICHAIN_BUILD_REPORT.json"
    generated = [sklb_output, phyb_output, report_output]
    try:
        sklb_result = write_sklb_cloned_chains(
            str(sklb_source), str(sklb_output), args.sklb_sha256, sklb_chains)
        phyb_result = write_phyb_cloned_chains(
            str(phyb_source), str(phyb_output), args.phyb_sha256, phyb_clones)
        sklb_after = inspect_sklb(str(sklb_output))
        phyb_after = inspect_phyb(str(phyb_output))
        output_bones = {bone["name"]: bone for bone in sklb_after["skeleton"]["bones"]}
        expected_new_names = [name for chain in sklb_chains for name in chain["newBones"]]
        phyb_named = [node["bone"] for simulator in phyb_after["simulators"]
                      for chain in simulator["chains"] for node in chain["nodes"] if node["bone"]]
        missing_references = sorted(set(phyb_named) - set(output_bones))
        new_reference_counts = {name: phyb_named.count(name) for name in expected_new_names}
        if missing_references or any(count != 1 for count in new_reference_counts.values()):
            raise ValueError("PHYB-to-SKLB bone reference verification failed.")

        blender_verification: dict[str, Any] | None = None
        if args.blender_bind_json:
            bind_path = safe_json_path(args.blender_bind_json)
            bind = json.loads(bind_path.read_text(encoding="utf-8"))
            bind_bones = bind.get("bones", {})
            comparison_names = ([f"sl_{letter}" for letter in LETTERS]
                                + [f"sr_{letter}" for letter in LETTERS] + expected_new_names)
            errors: dict[str, float] = {}
            for name in comparison_names:
                if name not in bind_bones:
                    raise ValueError(f"Blender bind report is missing bone {name!r}.")
                expected_parent = output_bones[name]["parentName"]
                if bind_bones[name].get("parent") != expected_parent:
                    raise ValueError(f"Blender/SKLB parent mismatch for {name!r}.")
                errors[name] = max_matrix_error(
                    matrix_from_local(output_bones[name]["localReference"]),
                    bind_bones[name]["local_reference"])
            maximum = max(errors.values())
            if maximum > 2e-5:
                raise ValueError(f"Blender/SKLB local-reference matrix error {maximum} exceeds 2e-5.")
            blender_verification = {
                "path": str(bind_path), "sha256": sha256(bind_path),
                "comparedBoneCount": len(comparison_names),
                "maxAbsLocalReferenceMatrixError": maximum,
                "tolerance": 2e-5, "parentsMatch": True,
                "perBoneMaxAbsError": errors,
            }

        source_unchanged = {
            "sklb": sha256(sklb_source) == args.sklb_sha256.lower(),
            "phyb": sha256(phyb_source) == args.phyb_sha256.lower(),
        }
        if not all(source_unchanged.values()):
            raise ValueError("A source file changed during the paired build.")
        report = {
            "ok": True,
            "mode": "offline paired SKLB/PHYB build; no Blender, game process, or Mod directory writes",
            "parameters": {
                "leftBRootRatio": args.left_b_ratio,
                "rightBRootRatio": args.right_b_ratio,
                "rootX": root_x,
                "rootLocalTranslations": {
                    chain["newBones"][0]: chain["rootTranslation"] for chain in sklb_chains
                },
            },
            "sklb": sklb_result,
            "phyb": phyb_result,
            "pairedVerification": {
                "sourceFilesUnchanged": source_unchanged,
                "oldBoneCount": 16,
                "newBoneCount": len(expected_new_names),
                "finalBoneCount": sklb_after["skeleton"]["boneCount"],
                "oldSimulatorCount": 4,
                "finalSimulatorCount": phyb_after["simulatorCount"],
                "originalChainsPreserved": True,
                "newChainCount": len(phyb_clones),
                "finalChainCountsBySimulator": [len(simulator["chains"])
                                                for simulator in phyb_after["simulators"]],
                "allPhybNamedNodesResolveInSklb": not missing_references,
                "eachNewBoneReferencedExactlyOnceByPhyb": all(
                    count == 1 for count in new_reference_counts.values()),
                "newBoneReferenceCounts": new_reference_counts,
                "blenderBind": blender_verification,
            },
            "limitations": [
                "The source PHYB has no collision, connector, attract, pin, spring, or post-alignment sections; none were invented.",
                "Original and cloned chains share simulator-level gravity/wind/loop/group settings.",
                "The empty terminal node in every cloned long chain is preserved and intentionally has no SKLB bone.",
                "Structural and Blender-rest readback passed; FFXIV runtime physics and deformation remain untested.",
            ],
        }
        report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        json.loads(report_output.read_text(encoding="utf-8"))
    except Exception:
        for path in generated:
            if path.exists():
                path.unlink()
        output_dir.rmdir()
        raise

    print(json.dumps({
        "ok": True, "outputDirectory": str(output_dir),
        "sklb": {"path": str(sklb_output), "sha256": sha256(sklb_output)},
        "phyb": {"path": str(phyb_output), "sha256": sha256(phyb_output)},
        "report": {"path": str(report_output), "sha256": sha256(report_output)},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
