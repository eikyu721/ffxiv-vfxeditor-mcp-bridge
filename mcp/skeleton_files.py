"""Offline FFXIV SKLB inspection, conservative editing, and skeleton export."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
from typing import Any
import uuid

from havok_tagfile import HavokTagfileError, Reader as HavokReader, serialize_tagfile


WORKSPACE_ROOT = Path(os.environ.get(
    "FFXIV_MCP_WORKSPACE_ROOT", Path(__file__).resolve().parents[2]
)).resolve()
MAX_INPUT_BYTES = 64 * 1024 * 1024


class SkeletonFileError(ValueError):
    pass


def _safe_path(value: str, suffixes: tuple[str, ...], *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SkeletonFileError("Path must be a non-empty string.")
    path = Path(value).resolve()
    try:
        path.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise SkeletonFileError(f"Path must remain under {WORKSPACE_ROOT}.") from exc
    if path.suffix.lower() not in suffixes:
        raise SkeletonFileError(f"Expected one of: {', '.join(suffixes)}.")
    if must_exist:
        if not path.is_file():
            raise SkeletonFileError("Input file does not exist or is not a regular file.")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise SkeletonFileError(f"Input exceeds the {MAX_INPUT_BYTES}-byte safety limit.")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fields(value: dict[str, Any]) -> dict[str, Any]:
    return value.get("$fields", {})


def _classes(value: dict[str, Any]) -> list[str]:
    return value.get("$classes", [])


def _parse_layers(data: bytes, layer_offset: int, havok_offset: int) -> dict[str, Any]:
    if layer_offset == 0:
        return {"status": "absent", "layers": []}
    if not (0 <= layer_offset <= havok_offset - 6):
        raise SkeletonFileError("SKLB layer offset is outside the header.")
    magic, count = struct.unpack_from("<IH", data, layer_offset)
    if magic != 0x616C7068:  # "hpla" bytes / little-endian "alph"
        raise SkeletonFileError("SKLB layer table magic is invalid.")
    cursor = layer_offset + 6
    offsets = list(struct.unpack_from(f"<{count}H", data, cursor)) if count else []
    base = layer_offset
    layers: list[dict[str, Any]] = []
    for index, offset in enumerate(offsets):
        pos = base + offset
        if pos + 6 > havok_offset:
            raise SkeletonFileError("SKLB layer record begins outside the header.")
        layer_id, bone_count = struct.unpack_from("<iH", data, pos)
        end = pos + 6 + bone_count * 2
        if end > havok_offset:
            raise SkeletonFileError("SKLB layer bone list exceeds the header.")
        bone_indices = list(struct.unpack_from(f"<{bone_count}H", data, pos + 6)) if bone_count else []
        layers.append({"index": index, "offset": offset, "id": layer_id, "boneIndices": bone_indices})
    return {"status": "parsed", "rawOffsets": offsets, "layers": layers}


def _collect_objects(root: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            if "$classes" in value:
                found.append(value)
            for child in _fields(value).values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(root)
    return found


def _semantic_snapshot(reader: HavokReader) -> dict[str, Any]:
    """Lossless-for-supported-fields graph snapshot with explicit object references."""
    object_indices = {id(value): index for index, value in reader.objects.items()}

    def convert(value: Any, *, definition: bool = False) -> Any:
        if isinstance(value, dict):
            if not definition and id(value) in object_indices:
                return {"$ref": object_indices[id(value)]}
            return {
                "$classes": list(value.get("$classes", [])),
                "$fields": {key: convert(child) for key, child in value.get("$fields", {}).items()},
            }
        if isinstance(value, list):
            result: dict[str, Any] = {"$values": [convert(child) for child in value]}
            prefix = reader.array_prefixes.get(id(value))
            if prefix is not None:
                result["$arrayPrefix"] = prefix
            column_prefixes = {member: prefix for (array_id, member), prefix
                               in reader.struct_array_prefixes.items() if array_id == id(value)}
            if column_prefixes:
                result["$structColumnPrefixes"] = column_prefixes
            return result
        return value

    return {
        "endian": reader.endian,
        "tagfileVersion": reader.tagfile_version,
        "havokVersion": reader.havok_version,
        "fileLayout": reader.file_layout,
        "types": [{
            "name": info.name,
            "unknown": info.unknown,
            "parent": info.parent,
            "members": [{
                "name": member.name, "kind": member.kind,
                "tupleSize": member.tuple_size, "className": member.class_name,
            } for member in info.members],
        } for info in reader.types],
        "objects": {str(index): convert(value, definition=True)
                    for index, value in sorted(reader.objects.items())},
    }


def _parse_havok_from_sklb(data: bytes, havok_offset: int) -> HavokReader:
    reader = HavokReader(data[havok_offset:])
    reader.parse()
    if reader.pos != len(data) - havok_offset:
        raise SkeletonFileError("SKLB Havok payload was not fully consumed; refusing to rewrite it.")
    return reader


def _single_skeleton(reader: HavokReader) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for value in reader.objects.values():
        objects.extend(_collect_objects(value))
    unique = {id(value): value for value in objects}
    skeletons = [value for value in unique.values() if "hkaSkeleton" in _classes(value)]
    if len(skeletons) != 1:
        raise SkeletonFileError(f"Expected exactly one hkaSkeleton; found {len(skeletons)}.")
    return skeletons[0]


def inspect_sklb(path_value: str) -> dict[str, Any]:
    path = _safe_path(path_value, (".sklb",), must_exist=True)
    data = path.read_bytes()
    if len(data) < 16:
        raise SkeletonFileError("SKLB is shorter than its fixed header.")
    magic, version, data_size, havok_offset = struct.unpack_from("<4I", data)
    if magic != 0x736B6C62:
        raise SkeletonFileError("Invalid SKLB magic.")
    versions = {0x31313030: "1100", 0x31313130: "1110", 0x31323030: "1200",
                0x31333030: "1300", 0x31333031: "1301"}
    version_text = versions.get(version)
    if version_text is None:
        raise SkeletonFileError(f"Unsupported SKLB version 0x{version:08X}.")
    if version_text in ("1100", "1110", "1200"):
        raise SkeletonFileError("Old 11xx/1200 SKLB headers are recognized but not yet supported by this read-only inspector.")
    if havok_offset < 16 or havok_offset >= len(data):
        raise SkeletonFileError("SKLB Havok offset is outside the file.")
    if data_size < 0x18 or data_size % 4:
        raise SkeletonFileError("SKLB 13xx data-size field is invalid.")

    bone_connect_index, _padding, skeleton_id = struct.unpack_from("<hhi", data, 16)
    parent_count = (data_size - 0x18) // 4
    parent_pairs = [list(pair) for pair in struct.iter_unpack("<hh", data[24:24 + parent_count * 4])]
    layer_offset = data_size
    layers = _parse_layers(data, layer_offset, havok_offset)

    reader = HavokReader(data[havok_offset:])
    root = reader.parse()
    objects = _collect_objects(root)
    skeletons = [obj for obj in objects if "hkaSkeleton" in _classes(obj)]
    if len(skeletons) != 1:
        raise SkeletonFileError(f"Expected exactly one hkaSkeleton; found {len(skeletons)}.")
    skeleton = _fields(skeletons[0])
    names = [_fields(item).get("name") for item in skeleton.get("bones", [])]
    parents = skeleton.get("parentIndices", [])
    poses = skeleton.get("referencePose", [])
    if not (len(names) == len(parents) == len(poses)):
        raise SkeletonFileError("Bone, parent-index, and reference-pose counts disagree.")
    if any(not isinstance(name, str) or not name for name in names):
        raise SkeletonFileError("Skeleton contains an empty or invalid bone name.")
    if len(set(names)) != len(names):
        raise SkeletonFileError("Skeleton contains duplicate bone names.")

    bones: list[dict[str, Any]] = []
    for index, (name, parent, pose) in enumerate(zip(names, parents, poses, strict=True)):
        if parent < -1 or parent >= len(names) or parent >= index:
            raise SkeletonFileError(f"Bone {index} has an invalid/non-topological parent index {parent}.")
        if len(pose) != 3 or any(len(vector) != 4 for vector in pose):
            raise SkeletonFileError(f"Bone {index} reference pose is not a QsTransform.")
        flat = [number for vector in pose for number in vector]
        if not all(isinstance(number, (int, float)) and math.isfinite(number) for number in flat):
            raise SkeletonFileError(f"Bone {index} reference pose contains non-finite values.")
        rotation_length = math.sqrt(sum(float(v) ** 2 for v in pose[1]))
        bones.append({
            "index": index,
            "name": name,
            "parentIndex": parent,
            "parentName": names[parent] if parent >= 0 else None,
            "children": [],
            "localReference": {
                "translation": pose[0][:3], "translationW": pose[0][3],
                "rotationQuaternionXyzw": pose[1], "rotationLength": rotation_length,
                "scale": pose[2][:3], "scaleW": pose[2][3],
            },
            "lockTranslation": _fields(skeleton["bones"][index]).get("lockTranslation", 0),
        })
    for bone in bones:
        if bone["parentIndex"] >= 0:
            bones[bone["parentIndex"]]["children"].append(bone["index"])
    for layer in layers["layers"]:
        if any(index >= len(bones) for index in layer["boneIndices"]):
            raise SkeletonFileError("SKLB layer references an out-of-range bone index.")
        layer["boneNames"] = [names[index] for index in layer["boneIndices"]]

    mapper_objects = [obj for obj in objects if "hkaSkeletonMapper" in _classes(obj)]
    mappers: list[dict[str, Any]] = []
    for mapper in mapper_objects:
        fields = _fields(mapper)
        mapping = fields.get("mapping")
        mappers.append({
            "classes": _classes(mapper),
            "status": "present; selected safe scalar/array fields reported",
            "mappingFields": sorted(_fields(mapping).keys()) if isinstance(mapping, dict) else [],
            "rawObjectFields": sorted(fields.keys()),
            "unsupported": ["chainMappings and semantic mapper validation are not implemented"],
        })

    return {
        "ok": True,
        "mode": "offline read-only; no native Havok library and no game process",
        "source": {"path": str(path), "bytes": len(data), "sha256": _sha256(data)},
        "sklb": {
            "version": version_text, "versionRawHex": f"0x{version:08X}",
            "dataSize": data_size, "layerOffset": layer_offset, "havokOffset": havok_offset,
            "boneConnectIndex": bone_connect_index, "skeletonId": skeleton_id,
            "parentSkeletonPairs": parent_pairs,
            "parentSkeletonPairsSemantics": "raw signed-int16 pairs; slot meanings are not confidently decoded",
        },
        "havok": {
            "container": "binary tagfile", "version": reader.havok_version,
            "fileLayoutBytes": getattr(reader, "file_layout", None),
            "objectCount": len(reader.objects), "typeCount": len(reader.types),
            "fullyConsumed": reader.pos == len(data) - havok_offset,
        },
        "coordinateSystem": {
            "source": "FFXIV/Havok local reference QsTransform",
            "translationUnits": "FFXIV model units; numerically treated as metres for glTF export",
            "axes": "source values copied as X/Y/Z; glTF is right-handed Y-up and Blender performs its normal glTF axis conversion",
            "rotation": "quaternion [x,y,z,w]",
            "transformSpace": "local to parent; roots are local to the skeleton space",
        },
        "layers": layers,
        "skeleton": {"name": skeleton.get("name"), "boneCount": len(bones), "bones": bones},
        "mappers": {"status": "parsed" if mapper_objects else "absent", "count": len(mapper_objects), "items": mappers},
        "unsupported": [
            "parent-skeleton pair slot semantics are reported raw",
            "mapper chain semantics are not claimed without a mapper-bearing sample",
            "no runtime animation/physics compatibility is inferred",
        ],
    }


def write_sklb_local_translations(path_value: str, output_value: str,
                                  expected_source_sha256: str,
                                  edits: list[dict[str, Any]],
                                  overwrite: bool = False) -> dict[str, Any]:
    """Write a new SKLB after changing only named-bone local translations."""
    source = _safe_path(path_value, (".sklb",), must_exist=True)
    output = _safe_path(output_value, (".sklb",), must_exist=False)
    if source == output:
        raise SkeletonFileError("Output must be a distinct file; in-place SKLB editing is forbidden.")
    if type(overwrite) is not bool:
        raise SkeletonFileError("overwrite must be a boolean.")
    output_existed = output.exists()
    if output_existed and not overwrite:
        raise SkeletonFileError(f"Refusing to overwrite existing output: {output}")
    if (not isinstance(expected_source_sha256, str)
            or len(expected_source_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in expected_source_sha256)):
        raise SkeletonFileError("expected_source_sha256 must be exactly 64 hexadecimal characters.")
    if not isinstance(edits, list) or len(edits) > 256:
        raise SkeletonFileError("edits must be a list containing at most 256 entries.")

    normalized: list[tuple[str, list[float]]] = []
    requested_names: set[str] = set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {"bone", "translation"}:
            raise SkeletonFileError(f"Edit {index} must contain exactly bone and translation.")
        name = edit["bone"]
        translation = edit["translation"]
        if (not isinstance(name, str) or not name or len(name) > 64
                or any(not (character.isascii() and (character.isalnum() or character == "_")) for character in name)):
            raise SkeletonFileError(f"Edit {index} has an invalid bone name.")
        if name in requested_names:
            raise SkeletonFileError(f"Bone {name!r} appears more than once in edits.")
        requested_names.add(name)
        if not isinstance(translation, list) or len(translation) != 3:
            raise SkeletonFileError(f"Edit {index} translation must contain exactly three numbers.")
        values: list[float] = []
        for component in translation:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise SkeletonFileError(f"Edit {index} translation contains a non-number.")
            number = float(component)
            if not math.isfinite(number):
                raise SkeletonFileError(f"Edit {index} translation contains a non-finite number.")
            if abs(number) > 100.0:
                raise SkeletonFileError(f"Edit {index} translation exceeds the ±100 model-unit safety bound.")
            # The tagfile stores reference transforms as IEEE-754 float32.
            values.append(struct.unpack("<f", struct.pack("<f", number))[0])
        normalized.append((name, values))

    data = source.read_bytes()
    source_sha = _sha256(data)
    if source_sha.lower() != expected_source_sha256.lower():
        raise SkeletonFileError(
            f"Source SHA256 mismatch: expected {expected_source_sha256.lower()}, got {source_sha}.")
    before_report = inspect_sklb(str(source))
    havok_offset = before_report["sklb"]["havokOffset"]
    reader = _parse_havok_from_sklb(data, havok_offset)
    before_semantics = _semantic_snapshot(reader)
    skeleton = _fields(_single_skeleton(reader))
    bones = skeleton.get("bones", [])
    poses = skeleton.get("referencePose", [])
    names = [_fields(bone).get("name") for bone in bones]
    if len(names) != len(poses):
        raise SkeletonFileError("Bone and reference-pose counts disagree.")
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise SkeletonFileError("Skeleton bone names are empty, invalid, or duplicated.")
    missing = [name for name, _ in normalized if name not in names]
    if missing:
        raise SkeletonFileError(f"Requested bone(s) do not exist: {missing}")

    changes: list[dict[str, Any]] = []
    for name, translation in normalized:
        bone_index = names.index(name)
        pose = poses[bone_index]
        if (not isinstance(pose, list) or len(pose) != 3
                or any(not isinstance(vector, list) or len(vector) != 4 for vector in pose)):
            raise SkeletonFileError(f"Bone {name!r} reference pose is not a QsTransform.")
        previous = list(pose[0][:3])
        pose[0][0:3] = translation
        changes.append({"bone": name, "boneIndex": bone_index,
                        "before": previous, "after": list(translation),
                        "changed": previous != translation})

    expected_semantics = _semantic_snapshot(reader)
    payload = serialize_tagfile(reader)
    candidate = data[:havok_offset] + payload
    candidate_reader = _parse_havok_from_sklb(candidate, havok_offset)
    if _semantic_snapshot(candidate_reader) != expected_semantics:
        raise SkeletonFileError("In-memory serialized Havok payload failed full semantic readback.")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.sklb")
    try:
        temporary.write_bytes(candidate)
        after_report = inspect_sklb(str(temporary))
        verified_reader = _parse_havok_from_sklb(temporary.read_bytes(), havok_offset)
        if _semantic_snapshot(verified_reader) != expected_semantics:
            raise SkeletonFileError("On-disk temporary SKLB failed full semantic readback.")

        expected_report = json.loads(json.dumps(before_report))
        expected_report["source"] = after_report["source"]
        expected_report["mode"] = after_report["mode"]
        for change in changes:
            expected_report["skeleton"]["bones"][change["boneIndex"]]["localReference"]["translation"] = change["after"]
        if after_report != expected_report:
            raise SkeletonFileError("SKLB report readback differs outside the requested translation fields.")
        if candidate[:havok_offset] != data[:havok_offset]:
            raise SkeletonFileError("SKLB header/layer bytes changed unexpectedly.")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    final_data = output.read_bytes()
    final_report = inspect_sklb(str(output))
    final_reader = _parse_havok_from_sklb(final_data, havok_offset)
    final_ok = (_semantic_snapshot(final_reader) == expected_semantics
                and final_data[:havok_offset] == data[:havok_offset])
    if not final_ok:
        raise SkeletonFileError("Final SKLB readback verification failed after placement.")
    return {
        "ok": True,
        "mode": "offline semantic tagfile rewrite; no game process or native Havok runtime",
        "source": {"path": str(source), "bytes": len(data), "sha256": source_sha},
        "output": {"path": str(output), "bytes": len(final_data), "sha256": _sha256(final_data)},
        "overwrote": output_existed,
        "noOp": not any(change["changed"] for change in changes),
        "editedBones": changes,
        "verification": {
            "sourceShaMatched": True,
            "inputFullyConsumed": True,
            "outputFullyConsumed": final_report["havok"]["fullyConsumed"],
            "fullParsedObjectGraphMatchesExpected": True,
            "containerHeaderAndLayersByteIdentical": True,
            "boneNamesAndOrderPreserved": True,
            "parentIndicesPreserved": True,
            "nonTargetReferenceTrsPreserved": True,
            "mapperSemanticsPreserved": True,
        },
        "serialization": {
            "kind": "parsed Havok binary tagfile reserialization",
            "byteIdenticalToSource": final_data == data,
            "note": "String-pool encoding may be canonicalized; verification compares the complete supported typed object graph, not raw payload offsets.",
        },
        "evidenceBoundary": "Structural offline readback passed. FFXIV loading, runtime physics, deformation, and collision behavior were not tested.",
        "_debugBeforeSemanticSha256": _sha256(json.dumps(before_semantics, sort_keys=True, separators=(",", ":")).encode()),
        "_debugAfterSemanticSha256": _sha256(json.dumps(expected_semantics, sort_keys=True, separators=(",", ":")).encode()),
    }


def _validated_sha256(value: str, field: str = "expected_source_sha256") -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)):
        raise SkeletonFileError(f"{field} must be exactly 64 hexadecimal characters.")
    return value.lower()


def _validated_bone_name(value: Any, field: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("ascii", errors="ignore")) != len(value)
            or len(value) > 64
            or any(not (character.isalnum() or character == "_") for character in value)):
        raise SkeletonFileError(f"{field} is not a valid ASCII bone name.")
    return value


def _float32_vec3(value: Any, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise SkeletonFileError(f"{field} must contain exactly three numbers.")
    result: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise SkeletonFileError(f"{field} contains a non-number.")
        number = float(component)
        if not math.isfinite(number) or abs(number) > 100.0:
            raise SkeletonFileError(f"{field} contains a non-finite or out-of-range value.")
        result.append(struct.unpack("<f", struct.pack("<f", number))[0])
    return result


def write_sklb_cloned_chains(path_value: str, output_value: str,
                              expected_source_sha256: str,
                              chains: list[dict[str, Any]],
                              overwrite: bool = False) -> dict[str, Any]:
    """Append topological bone chains cloned from existing chains in a new SKLB.

    Each chain request contains ``sourceBones``, ``newBones``, ``rootParent``, and
    ``rootTranslation``. Rotation, scale, translation-W, scale-W, and all child
    local translations are copied from the corresponding source bones. The
    existing skeleton is never edited in place.
    """
    source = _safe_path(path_value, (".sklb",), must_exist=True)
    output = _safe_path(output_value, (".sklb",), must_exist=False)
    if source == output:
        raise SkeletonFileError("Output must be a distinct file; in-place SKLB editing is forbidden.")
    if type(overwrite) is not bool:
        raise SkeletonFileError("overwrite must be a boolean.")
    output_existed = output.exists()
    if output_existed and not overwrite:
        raise SkeletonFileError(f"Refusing to overwrite existing output: {output}")
    expected_sha = _validated_sha256(expected_source_sha256)
    if not isinstance(chains, list) or not 1 <= len(chains) <= 64:
        raise SkeletonFileError("chains must contain between one and 64 chain requests.")

    normalized: list[dict[str, Any]] = []
    requested_names: set[str] = set()
    for chain_index, request in enumerate(chains):
        expected_fields = {"sourceBones", "newBones", "rootParent", "rootTranslation"}
        if not isinstance(request, dict) or set(request) != expected_fields:
            raise SkeletonFileError(
                f"Chain {chain_index} must contain exactly {sorted(expected_fields)}.")
        source_bones = request["sourceBones"]
        new_bones = request["newBones"]
        if (not isinstance(source_bones, list) or not isinstance(new_bones, list)
                or not 1 <= len(source_bones) <= 32 or len(source_bones) != len(new_bones)):
            raise SkeletonFileError(
                f"Chain {chain_index} sourceBones/newBones must have the same length from one to 32.")
        source_names = [_validated_bone_name(name, f"Chain {chain_index} source bone")
                        for name in source_bones]
        new_names = [_validated_bone_name(name, f"Chain {chain_index} new bone")
                     for name in new_bones]
        for name in new_names:
            if name in requested_names:
                raise SkeletonFileError(f"New bone {name!r} is requested more than once.")
            requested_names.add(name)
        normalized.append({
            "sourceBones": source_names,
            "newBones": new_names,
            "rootParent": _validated_bone_name(request["rootParent"], f"Chain {chain_index} root parent"),
            "rootTranslation": _float32_vec3(request["rootTranslation"],
                                               f"Chain {chain_index} root translation"),
        })

    data = source.read_bytes()
    source_sha = _sha256(data)
    if source_sha != expected_sha:
        raise SkeletonFileError(f"Source SHA256 mismatch: expected {expected_sha}, got {source_sha}.")
    before_report = inspect_sklb(str(source))
    if before_report["mappers"]["count"]:
        raise SkeletonFileError("Mapper-bearing SKLB files are not supported for bone-chain append.")
    havok_offset = before_report["sklb"]["havokOffset"]
    reader = _parse_havok_from_sklb(data, havok_offset)
    skeleton = _fields(_single_skeleton(reader))
    bones = skeleton.get("bones")
    parents = skeleton.get("parentIndices")
    poses = skeleton.get("referencePose")
    if not all(isinstance(value, list) for value in (bones, parents, poses)):
        raise SkeletonFileError("Skeleton arrays are unavailable or malformed.")
    original_count = len(bones)
    if not (original_count == len(parents) == len(poses)):
        raise SkeletonFileError("Bone, parent-index, and reference-pose counts disagree.")
    original_names = [_fields(bone).get("name") for bone in bones]
    if any(name in original_names for name in requested_names):
        duplicates = sorted(name for name in requested_names if name in original_names)
        raise SkeletonFileError(f"New bone name(s) already exist: {duplicates}")

    additions: list[dict[str, Any]] = []
    current_names = list(original_names)
    for chain_index, request in enumerate(normalized):
        source_indices: list[int] = []
        for name in request["sourceBones"]:
            if name not in original_names:
                raise SkeletonFileError(f"Chain {chain_index} source bone {name!r} does not exist.")
            source_indices.append(original_names.index(name))
        for child_pos in range(1, len(source_indices)):
            if parents[source_indices[child_pos]] != source_indices[child_pos - 1]:
                raise SkeletonFileError(
                    f"Chain {chain_index} source bones are not a direct parent-child chain at "
                    f"{request['sourceBones'][child_pos]!r}.")
        if request["rootParent"] not in current_names:
            raise SkeletonFileError(
                f"Chain {chain_index} root parent {request['rootParent']!r} does not exist yet.")

        new_indices: list[int] = []
        for bone_pos, (source_index, new_name) in enumerate(zip(
                source_indices, request["newBones"], strict=True)):
            new_bone = copy.deepcopy(bones[source_index])
            _fields(new_bone)["name"] = new_name
            new_pose = copy.deepcopy(poses[source_index])
            if bone_pos == 0:
                new_pose[0][0:3] = request["rootTranslation"]
                parent_index = current_names.index(request["rootParent"])
            else:
                parent_index = new_indices[-1]
            new_index = len(bones)
            bones.append(new_bone)
            parents.append(parent_index)
            poses.append(new_pose)
            current_names.append(new_name)
            new_indices.append(new_index)
            additions.append({
                "chain": chain_index, "sourceBone": original_names[source_index],
                "sourceBoneIndex": source_index, "bone": new_name, "boneIndex": new_index,
                "parent": current_names[parent_index], "parentIndex": parent_index,
                "localReference": {
                    "translation": list(new_pose[0][:3]), "translationW": new_pose[0][3],
                    "rotationQuaternionXyzw": list(new_pose[1]),
                    "scale": list(new_pose[2][:3]), "scaleW": new_pose[2][3],
                },
            })

    expected_semantics = _semantic_snapshot(reader)
    payload = serialize_tagfile(reader)
    candidate = data[:havok_offset] + payload
    candidate_reader = _parse_havok_from_sklb(candidate, havok_offset)
    if _semantic_snapshot(candidate_reader) != expected_semantics:
        raise SkeletonFileError("In-memory appended SKLB failed full semantic readback.")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.sklb")
    try:
        temporary.write_bytes(candidate)
        after_report = inspect_sklb(str(temporary))
        verified_reader = _parse_havok_from_sklb(temporary.read_bytes(), havok_offset)
        if _semantic_snapshot(verified_reader) != expected_semantics:
            raise SkeletonFileError("Temporary appended SKLB failed full semantic readback.")
        if candidate[:havok_offset] != data[:havok_offset]:
            raise SkeletonFileError("SKLB header/layer bytes changed unexpectedly.")
        def original_content(bone: dict[str, Any]) -> dict[str, Any]:
            # ``children`` is derived by the inspector. Existing root bones gain
            # appended children by design; every stored field must stay equal.
            return {key: value for key, value in bone.items() if key != "children"}

        if ([original_content(bone) for bone in after_report["skeleton"]["bones"][:original_count]]
                != [original_content(bone) for bone in before_report["skeleton"]["bones"]]):
            raise SkeletonFileError("An original SKLB stored bone field changed during chain append.")
        if after_report["skeleton"]["boneCount"] != original_count + len(additions):
            raise SkeletonFileError("Appended SKLB bone count is incorrect.")
        for addition in additions:
            bone = after_report["skeleton"]["bones"][addition["boneIndex"]]
            expected_local = addition["localReference"]
            actual_local = bone["localReference"]
            if (bone["name"] != addition["bone"] or bone["parentIndex"] != addition["parentIndex"]
                    or actual_local["translation"] != expected_local["translation"]
                    or actual_local["translationW"] != expected_local["translationW"]
                    or actual_local["rotationQuaternionXyzw"] != expected_local["rotationQuaternionXyzw"]
                    or actual_local["scale"] != expected_local["scale"]
                    or actual_local["scaleW"] != expected_local["scaleW"]):
                raise SkeletonFileError(f"Appended bone {addition['bone']!r} failed readback.")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    final_data = output.read_bytes()
    if _sha256(source.read_bytes()) != source_sha:
        raise SkeletonFileError("Source SKLB changed during append operation.")
    return {
        "ok": True,
        "mode": "offline semantic tagfile chain append; no game process or native Havok runtime",
        "source": {"path": str(source), "bytes": len(data), "sha256": source_sha},
        "output": {"path": str(output), "bytes": len(final_data), "sha256": _sha256(final_data)},
        "overwrote": output_existed,
        "originalBoneCount": original_count,
        "newBoneCount": len(additions),
        "finalBoneCount": original_count + len(additions),
        "addedBones": additions,
        "verification": {
            "sourceShaMatchedAndRemainedUnchanged": True,
            "inputAndOutputFullyConsumed": True,
            "containerHeaderAndLayersByteIdentical": True,
            "allOriginalStoredBoneFieldsAndRestSemanticallyIdentical": True,
            "existingRootChildrenExtendedOnlyByRequestedNewChainRoots": True,
            "newParentsAndLocalReferenceTrsReadBackExactly": True,
            "mapperAbsent": True,
        },
        "evidenceBoundary": "Structural offline readback passed. FFXIV loading and runtime physics were not tested.",
    }


def _align4(data: bytearray) -> None:
    data.extend(b"\0" * ((-len(data)) % 4))


def _gltf_document(report: dict[str, Any], binary_name: str | None) -> tuple[dict[str, Any], bytes]:
    bones = report["skeleton"]["bones"]
    binary = bytearray()

    def add_view(payload: bytes, target: int | None = None) -> int:
        _align4(binary)
        offset = len(binary)
        binary.extend(payload)
        view: dict[str, Any] = {"buffer": 0, "byteOffset": offset, "byteLength": len(payload)}
        if target is not None:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    views: list[dict[str, Any]] = []
    positions = struct.pack("<9f", 0.000001, 0, 0, 0, 0.000001, 0, 0, 0, 0.000001)
    joints = struct.pack("<12H", *([0, 0, 0, 0] * 3))
    weights = struct.pack("<12f", *([1.0, 0, 0, 0] * 3))
    indices = struct.pack("<3H", 0, 1, 2)
    pos_view = add_view(positions, 34962)
    joint_view = add_view(joints, 34962)
    weight_view = add_view(weights, 34962)
    index_view = add_view(indices, 34963)
    accessors = [
        {"bufferView": pos_view, "componentType": 5126, "count": 3, "type": "VEC3",
         "min": [0, 0, 0], "max": [0.000001, 0.000001, 0.000001]},
        {"bufferView": joint_view, "componentType": 5123, "count": 3, "type": "VEC4"},
        {"bufferView": weight_view, "componentType": 5126, "count": 3, "type": "VEC4"},
        {"bufferView": index_view, "componentType": 5123, "count": 3, "type": "SCALAR"},
    ]
    nodes: list[dict[str, Any]] = [{"name": "Armature", "children": []}]
    for bone in bones:
        local = bone["localReference"]
        node = {"name": bone["name"], "translation": local["translation"],
                "rotation": local["rotationQuaternionXyzw"], "scale": local["scale"]}
        if bone["children"]:
            node["children"] = [index + 1 for index in bone["children"]]
        nodes.append(node)
    nodes[0]["children"] = [bone["index"] + 1 for bone in bones if bone["parentIndex"] < 0]
    mesh_node = len(nodes)
    nodes.append({"name": "SKLB_REFERENCE_DUMMY", "mesh": 0, "skin": 0})
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "VFXEditor MCP offline SKLB reader"},
        "scene": 0, "scenes": [{"nodes": [0, mesh_node]}], "nodes": nodes,
        "skins": [{"name": report["skeleton"]["name"] or "SKLB Skeleton",
                   "skeleton": 0, "joints": list(range(1, len(bones) + 1))}],
        "meshes": [{"name": "SKLB_REFERENCE_DUMMY", "primitives": [{
            "attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2}, "indices": 3, "mode": 4}]}],
        "buffers": [{"byteLength": len(binary)}], "bufferViews": views, "accessors": accessors,
        "extras": {"sourceSha256": report["source"]["sha256"], "sourcePath": report["source"]["path"],
                   "coordinateNote": report["coordinateSystem"]},
    }
    if binary_name is not None:
        document["buffers"][0]["uri"] = binary_name
    return document, bytes(binary)


def export_sklb(path_value: str, output_value: str, overwrite: bool = False) -> dict[str, Any]:
    output = _safe_path(output_value, (".gltf", ".glb"), must_exist=False)
    report = inspect_sklb(path_value)
    sidecar = output.with_suffix(".skeleton.json")
    binary_path = output.with_suffix(".bin") if output.suffix.lower() == ".gltf" else None
    targets = [output, sidecar] + ([binary_path] if binary_path else [])
    existing = [str(path) for path in targets if path.exists()]
    if existing and not overwrite:
        raise SkeletonFileError(f"Refusing to overwrite existing output(s): {existing}")
    output.parent.mkdir(parents=True, exist_ok=True)
    document, binary = _gltf_document(report, binary_path.name if binary_path else None)
    if output.suffix.lower() == ".gltf":
        output.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        binary_path.write_bytes(binary)
    else:
        json_bytes = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_bytes += b" " * ((-len(json_bytes)) % 4)
        bin_bytes = binary + b"\0" * ((-len(binary)) % 4)
        total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
        glb = struct.pack("<III", 0x46546C67, 2, total)
        glb += struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
        glb += struct.pack("<II", len(bin_bytes), 0x004E4942) + bin_bytes
        output.write_bytes(glb)
    sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Independent readback: validate container headers/JSON and preserve the parsed bone order.
    if output.suffix.lower() == ".gltf":
        readback = json.loads(output.read_text(encoding="utf-8"))
        binary_ok = binary_path.stat().st_size == len(binary)
    else:
        raw = output.read_bytes()
        magic, version, total = struct.unpack_from("<III", raw)
        json_len, json_type = struct.unpack_from("<II", raw, 12)
        readback = json.loads(raw[20:20 + json_len].decode("utf-8").rstrip(" \0"))
        binary_ok = magic == 0x46546C67 and version == 2 and total == len(raw) and json_type == 0x4E4F534A
    exported_names = [node.get("name") for node in readback["nodes"][1:1 + report["skeleton"]["boneCount"]]]
    source_names = [bone["name"] for bone in report["skeleton"]["bones"]]
    if exported_names != source_names or not binary_ok:
        raise SkeletonFileError("Export readback verification failed.")
    artifacts = []
    for path in targets:
        payload = path.read_bytes()
        artifacts.append({"path": str(path), "bytes": len(payload), "sha256": _sha256(payload)})
    return {"ok": True, "format": output.suffix.lower()[1:], "overwrote": bool(existing),
            "sourceSha256": report["source"]["sha256"], "boneCount": len(source_names),
            "readbackVerified": True, "artifacts": artifacts,
            "blenderImportNote": "Import the glTF/GLB normally; Blender converts glTF Y-up to its Z-up view. Keep Guess Original Bind Pose disabled when round-tripping through VFXEditor."}


def inspect_phyb(path_value: str) -> dict[str, Any]:
    path = _safe_path(path_value, (".phyb",), must_exist=True)
    data = path.read_bytes()
    if len(data) < 16:
        raise SkeletonFileError("PHYB is shorter than its header.")
    version, data_type, collision_offset, sim_offset = struct.unpack_from("<4I", data)
    if version == 0:
        raise SkeletonFileError("Empty/version-0 PHYB is recognized but has no inspectable simulation data.")
    if not (16 <= collision_offset <= sim_offset <= len(data)):
        raise SkeletonFileError("PHYB section offsets are invalid.")
    has_extended = len(data) >= 24 and data[-24:-20] == b"PACK"
    extended_status = "present-but-not-decoded" if has_extended else "absent"
    if has_extended:
        raise SkeletonFileError("EPHB/PACK extended PHYB data is present; this minimal inspector refuses to silently omit it.")
    if sim_offset + 4 > len(data):
        raise SkeletonFileError("PHYB simulator section is truncated.")
    simulator_count = struct.unpack_from("<I", data, sim_offset)[0]
    base = sim_offset + 4
    header_size = 72
    if base + simulator_count * header_size > len(data):
        raise SkeletonFileError("PHYB simulator headers are truncated.")
    parsed_end = base + simulator_count * header_size
    simulators: list[dict[str, Any]] = []
    unsupported: list[str] = []
    if collision_offset != sim_offset:
        unsupported.append(f"{sim_offset - collision_offset} collision-section bytes are present but not decoded")
    section_names = ["collisions", "collisionConnectors", "chains", "connectors", "attracts", "pins", "springs", "postAlignments"]
    for index in range(simulator_count):
        header = base + index * header_size
        counts = list(struct.unpack_from("<8B", data, header))
        gravity = list(struct.unpack_from("<3f", data, header + 8))
        wind = list(struct.unpack_from("<3f", data, header + 20))
        constraint_loop, collision_loop, flags, group, unknown_tail = struct.unpack_from("<hhBBH", data, header + 32)
        offsets = list(struct.unpack_from("<8I", data, header + 40))
        non_chain = {name: counts[i] for i, name in enumerate(section_names) if i != 2 and counts[i]}
        if non_chain:
            unsupported.append(f"simulator {index} non-chain sections not decoded: {non_chain}")
        chains: list[dict[str, Any]] = []
        chain_offset = offsets[2]
        if counts[2] and chain_offset == 0:
            raise SkeletonFileError(f"Simulator {index} has chains but no chain offset.")
        for chain_index in range(counts[2]):
            pos = base + chain_offset + chain_index * 48
            if pos + 48 > len(data):
                raise SkeletonFileError("PHYB chain header is truncated.")
            collision_count, node_count = struct.unpack_from("<2H", data, pos)
            dampening, max_speed, friction, collision_dampening, repulsion = struct.unpack_from("<5f", data, pos + 4)
            last_bone_offset = list(struct.unpack_from("<3f", data, pos + 24))
            chain_type, chain_collision_offset, node_offset = struct.unpack_from("<3I", data, pos + 36)
            if collision_count:
                unsupported.append(f"simulator {index} chain {chain_index} has {collision_count} collision references; raw offset {chain_collision_offset}")
            nodes: list[dict[str, Any]] = []
            for node_index in range(node_count):
                node_pos = base + node_offset + node_index * 84
                if node_pos + 84 > len(data):
                    raise SkeletonFileError("PHYB node record is truncated.")
                raw_name = data[node_pos:node_pos + 32]
                name = raw_name.split(b"\0", 1)[0].decode("ascii")
                values = struct.unpack_from("<11f", data, node_pos + 32)
                if not all(math.isfinite(value) for value in values):
                    raise SkeletonFileError("PHYB node contains non-finite parameters.")
                collision_flag, continuous_flag = struct.unpack_from("<2I", data, node_pos + 76)
                nodes.append({"index": node_index, "bone": name, "radius": values[0],
                    "attractByAnimation": values[1], "windScale": values[2], "gravityScale": values[3],
                    "coneMaxAngleRadians": values[4], "coneAxisOffset": list(values[5:8]),
                    "constraintPlaneNormal": list(values[8:11]), "collisionFlag": collision_flag,
                    "continuousCollisionFlag": continuous_flag})
                parsed_end = max(parsed_end, node_pos + 84)
            chains.append({"index": chain_index, "collisionCount": collision_count,
                "nodeCount": node_count, "dampening": dampening, "maxSpeed": max_speed,
                "friction": friction, "collisionDampening": collision_dampening,
                "repulsionStrength": repulsion, "lastBoneOffset": last_bone_offset,
                "chainType": {0: "sphere", 1: "capsule"}.get(chain_type, f"unknown:{chain_type}"),
                "collisionReferenceOffsetRaw": chain_collision_offset, "nodeOffsetRaw": node_offset,
                "nodes": nodes})
        simulators.append({"index": index, "counts": dict(zip(section_names, counts, strict=True)),
            "gravity": gravity, "wind": wind, "constraintLoop": constraint_loop,
            "collisionLoop": collision_loop, "flags": flags, "group": group,
            "unknownHeaderTailU16": unknown_tail, "rawOffsets": offsets, "chains": chains})
    if unsupported:
        # Return the safely decoded subset but make incompleteness impossible to overlook.
        parse_status = "partial-explicit"
    else:
        parse_status = "complete-for-known-layout"
    if parsed_end != len(data):
        unsupported.append(f"{len(data) - parsed_end} trailing bytes were not structurally decoded")
        parse_status = "partial-explicit"
    return {"ok": True, "mode": "offline read-only; no game physics execution",
        "source": {"path": str(path), "bytes": len(data), "sha256": _sha256(data)},
        "header": {"version": version, "dataType": data_type, "collisionOffset": collision_offset,
                   "simulationOffset": sim_offset, "extended": extended_status},
        "collisionSection": {"bytes": sim_offset - collision_offset,
            "status": "empty" if collision_offset == sim_offset else "present-but-not-decoded"},
        "simulatorCount": simulator_count, "simulators": simulators,
        "parsedEnd": parsed_end, "fullyConsumed": parsed_end == len(data),
        "parseStatus": parse_status, "unsupported": unsupported,
        "evidenceBoundary": "Parameters and references are parsed from bytes; no runtime solver, deformation, collision response, or stability was tested."}


def _phyb_chain_semantics(chain: dict[str, Any]) -> dict[str, Any]:
    """Return chain content without layout offsets or list indices."""
    return {key: copy.deepcopy(value) for key, value in chain.items()
            if key not in {"index", "nodeOffsetRaw", "collisionReferenceOffsetRaw"}}


def write_phyb_cloned_chains(path_value: str, output_value: str,
                              expected_source_sha256: str,
                              clones: list[dict[str, Any]],
                              overwrite: bool = False) -> dict[str, Any]:
    """Append collision-free chain clones to their source simulators.

    Each clone request contains ``sourceSimulator``, ``sourceChain``, and
    ``newBones``. Only non-empty node bone names are replaced; an empty terminal
    node is preserved exactly. This conservative writer refuses extended PHYB,
    collision sections/references, and any non-chain simulator sections rather
    than guessing how cross-chain references should be remapped.
    """
    source = _safe_path(path_value, (".phyb",), must_exist=True)
    output = _safe_path(output_value, (".phyb",), must_exist=False)
    if source == output:
        raise SkeletonFileError("Output must be a distinct file; in-place PHYB editing is forbidden.")
    if type(overwrite) is not bool:
        raise SkeletonFileError("overwrite must be a boolean.")
    output_existed = output.exists()
    if output_existed and not overwrite:
        raise SkeletonFileError(f"Refusing to overwrite existing output: {output}")
    expected_sha = _validated_sha256(expected_source_sha256)
    if not isinstance(clones, list) or not 1 <= len(clones) <= 128:
        raise SkeletonFileError("clones must contain between one and 128 requests.")

    data = source.read_bytes()
    source_sha = _sha256(data)
    if source_sha != expected_sha:
        raise SkeletonFileError(f"Source SHA256 mismatch: expected {expected_sha}, got {source_sha}.")
    before = inspect_phyb(str(source))
    if (not before["fullyConsumed"] or before["parseStatus"] != "complete-for-known-layout"
            or before["unsupported"] or before["header"]["extended"] != "absent"
            or before["collisionSection"]["status"] != "empty"):
        raise SkeletonFileError("PHYB is not a fully consumed collision-free known-layout file.")

    simulators = before["simulators"]
    section_names = ["collisions", "collisionConnectors", "chains", "connectors",
                     "attracts", "pins", "springs", "postAlignments"]
    for simulator in simulators:
        non_chain = {name: count for name, count in simulator["counts"].items()
                     if name != "chains" and count}
        if non_chain:
            raise SkeletonFileError(
                f"Simulator {simulator['index']} has unsupported non-chain sections: {non_chain}")
        for chain in simulator["chains"]:
            if chain["collisionCount"] or chain["collisionReferenceOffsetRaw"]:
                raise SkeletonFileError("Chain collision references require explicit remapping and are refused.")

    normalized: list[dict[str, Any]] = []
    new_names_seen: set[str] = set()
    for clone_index, request in enumerate(clones):
        if not isinstance(request, dict) or set(request) != {"sourceSimulator", "sourceChain", "newBones"}:
            raise SkeletonFileError(
                f"Clone {clone_index} must contain exactly sourceSimulator, sourceChain, and newBones.")
        simulator_index = request["sourceSimulator"]
        chain_index = request["sourceChain"]
        if (type(simulator_index) is not int or type(chain_index) is not int
                or not 0 <= simulator_index < len(simulators)
                or not 0 <= chain_index < len(simulators[simulator_index]["chains"])):
            raise SkeletonFileError(f"Clone {clone_index} references a missing simulator or chain.")
        source_chain = simulators[simulator_index]["chains"][chain_index]
        named_nodes = [node for node in source_chain["nodes"] if node["bone"]]
        new_bones = request["newBones"]
        if not isinstance(new_bones, list) or len(new_bones) != len(named_nodes):
            raise SkeletonFileError(
                f"Clone {clone_index} newBones must contain {len(named_nodes)} names; "
                "the source terminal empty node is preserved automatically.")
        validated: list[str] = []
        for name in new_bones:
            bone_name = _validated_bone_name(name, f"Clone {clone_index} new bone")
            if len(bone_name.encode("ascii")) > 31:
                raise SkeletonFileError("PHYB bone names must fit in 31 ASCII bytes plus a terminator.")
            if bone_name in new_names_seen:
                raise SkeletonFileError(f"New PHYB bone name {bone_name!r} is requested more than once.")
            new_names_seen.add(bone_name)
            validated.append(bone_name)
        normalized.append({"sourceSimulator": simulator_index, "sourceChain": chain_index,
                           "newBones": validated})

    sim_offset = before["header"]["simulationOffset"]
    base = sim_offset + 4
    header_size = 72
    chain_size = 48
    node_size = 84
    original_blocks: list[list[dict[str, Any]]] = []
    covered: list[tuple[int, int]] = []
    header_end = base + len(simulators) * header_size
    for simulator in simulators:
        blocks: list[dict[str, Any]] = []
        for chain in simulator["chains"]:
            chain_pos = base + simulator["rawOffsets"][2] + chain["index"] * chain_size
            node_pos = base + chain["nodeOffsetRaw"]
            chain_end = chain_pos + chain_size
            node_end = node_pos + chain["nodeCount"] * node_size
            if not (header_end <= chain_pos < chain_end <= len(data)
                    and header_end <= node_pos <= node_end <= len(data)):
                raise SkeletonFileError("PHYB chain or node block lies outside the known packed data region.")
            chain_bytes = bytes(data[chain_pos:chain_end])
            node_bytes = [bytes(data[node_pos + index * node_size:node_pos + (index + 1) * node_size])
                          for index in range(chain["nodeCount"])]
            blocks.append({"header": chain_bytes, "nodes": node_bytes,
                           "sourceSimulator": simulator["index"], "sourceChain": chain["index"],
                           "newBones": None})
            covered.extend([(chain_pos, chain_end), (node_pos, node_end)])
        original_blocks.append(blocks)

    packed_ranges = sorted(covered)
    cursor = header_end
    for start, end in packed_ranges:
        if start != cursor:
            raise SkeletonFileError(
                "PHYB chain/node storage contains gaps or an unexpected ordering; refusing to re-layout it.")
        cursor = end
    if cursor != len(data):
        raise SkeletonFileError("PHYB has trailing bytes outside the known chain/node storage.")

    blocks_by_simulator = [list(blocks) for blocks in original_blocks]
    clone_records: list[dict[str, Any]] = []
    for request in normalized:
        source_block = original_blocks[request["sourceSimulator"]][request["sourceChain"]]
        nodes = [bytearray(node) for node in source_block["nodes"]]
        name_index = 0
        for node in nodes:
            old_name = bytes(node[:32]).split(b"\0", 1)[0].decode("ascii")
            if old_name:
                encoded = request["newBones"][name_index].encode("ascii")
                node[:32] = encoded + b"\0" * (32 - len(encoded))
                name_index += 1
        clone_block = {"header": source_block["header"], "nodes": [bytes(node) for node in nodes],
                       "sourceSimulator": request["sourceSimulator"],
                       "sourceChain": request["sourceChain"], "newBones": request["newBones"]}
        blocks_by_simulator[request["sourceSimulator"]].append(clone_block)
        clone_records.append({"targetSimulator": request["sourceSimulator"],
                              "sourceChain": request["sourceChain"],
                              "newBones": request["newBones"]})

    total_chains = sum(len(blocks) for blocks in blocks_by_simulator)
    if any(len(blocks) > 255 for blocks in blocks_by_simulator):
        raise SkeletonFileError("A simulator would exceed the one-byte chain-count limit.")
    sim_headers = [bytearray(data[base + index * header_size:base + (index + 1) * header_size])
                   for index in range(len(simulators))]
    chain_cursor = header_end
    node_cursor = header_end + total_chains * chain_size
    chain_payload = bytearray()
    node_payload = bytearray()
    for simulator_index, blocks in enumerate(blocks_by_simulator):
        sim_headers[simulator_index][2] = len(blocks)
        chain_offset = chain_cursor - base if blocks else 0
        struct.pack_into("<I", sim_headers[simulator_index], 40 + 2 * 4, chain_offset)
        for block in blocks:
            header = bytearray(block["header"])
            struct.pack_into("<I", header, 44, node_cursor - base)
            chain_payload.extend(header)
            chain_cursor += chain_size
            for node in block["nodes"]:
                node_payload.extend(node)
                node_cursor += node_size

    candidate = bytearray(data[:base])
    for header in sim_headers:
        candidate.extend(header)
    candidate.extend(chain_payload)
    candidate.extend(node_payload)
    if len(candidate) != node_cursor:
        raise SkeletonFileError("Internal PHYB layout accounting failed.")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.phyb")
    try:
        temporary.write_bytes(candidate)
        after = inspect_phyb(str(temporary))
        if (not after["fullyConsumed"] or after["parseStatus"] != "complete-for-known-layout"
                or after["unsupported"]):
            raise SkeletonFileError("Temporary cloned PHYB did not fully parse after re-layout.")
        for simulator_index, original_simulator in enumerate(simulators):
            actual_simulator = after["simulators"][simulator_index]
            for field in ("gravity", "wind", "constraintLoop", "collisionLoop", "flags", "group",
                          "unknownHeaderTailU16"):
                if actual_simulator[field] != original_simulator[field]:
                    raise SkeletonFileError(f"Original simulator field {field} changed.")
            for section in section_names:
                expected_count = original_simulator["counts"][section]
                if section == "chains":
                    expected_count = len(blocks_by_simulator[simulator_index])
                if actual_simulator["counts"][section] != expected_count:
                    raise SkeletonFileError(f"Simulator {simulator_index} section count changed unexpectedly.")
            original_chains = original_simulator["chains"]
            for chain_index, original_chain in enumerate(original_chains):
                if (_phyb_chain_semantics(actual_simulator["chains"][chain_index])
                        != _phyb_chain_semantics(original_chain)):
                    raise SkeletonFileError("An original PHYB chain changed during clone append.")
        for record_index, record in enumerate(clone_records):
            simulator_index = record["targetSimulator"]
            original_count = len(simulators[simulator_index]["chains"])
            preceding = sum(1 for earlier in clone_records[:record_index]
                            if earlier["targetSimulator"] == simulator_index)
            cloned = after["simulators"][simulator_index]["chains"][original_count + preceding]
            source_chain = simulators[simulator_index]["chains"][record["sourceChain"]]
            expected_chain = _phyb_chain_semantics(source_chain)
            replacement = iter(record["newBones"])
            for node in expected_chain["nodes"]:
                if node["bone"]:
                    node["bone"] = next(replacement)
            if _phyb_chain_semantics(cloned) != expected_chain:
                raise SkeletonFileError("A cloned PHYB chain failed full parameter/name readback.")
            record["targetChain"] = cloned["index"]
            record["terminalEmptyNodePreserved"] = cloned["nodes"][-1]["bone"] == ""
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    final_data = output.read_bytes()
    if _sha256(source.read_bytes()) != source_sha:
        raise SkeletonFileError("Source PHYB changed during clone operation.")
    return {
        "ok": True,
        "mode": "offline collision-free known-layout PHYB chain clone; no game physics execution",
        "source": {"path": str(source), "bytes": len(data), "sha256": source_sha},
        "output": {"path": str(output), "bytes": len(final_data), "sha256": _sha256(final_data)},
        "overwrote": output_existed,
        "originalSimulatorCount": len(simulators),
        "originalChainCount": sum(len(simulator["chains"]) for simulator in simulators),
        "newChainCount": len(clone_records),
        "finalChainCount": total_chains,
        "clonedChains": clone_records,
        "verification": {
            "sourceShaMatchedAndRemainedUnchanged": True,
            "outputFullyConsumed": True,
            "originalSimulatorParametersPreserved": True,
            "allOriginalChainParametersAndNodesPreserved": True,
            "clonedParametersMatchSourceExceptBoneNames": True,
            "emptyTerminalNodesPreserved": all(record["terminalEmptyNodePreserved"]
                                                for record in clone_records),
            "collisionAndCrossChainSectionsAbsent": True,
        },
        "evidenceBoundary": "Structural offline readback passed. Runtime solver behavior and stability were not tested.",
    }
