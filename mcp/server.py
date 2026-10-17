import json
import re
import socket
import struct
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from havok_tagfile import HavokTagfileError
from skeleton_files import (SkeletonFileError, export_sklb, inspect_phyb, inspect_sklb,
                            write_sklb_local_translations)

HOST = "127.0.0.1"
PORT = 42072
PROTOCOL_VERSION = 2
mcp = MCPServer("FFXIV VFXEditor Animation")


def call_bridge(command: str, **parameters: Any) -> dict[str, Any]:
    payload = json.dumps({"command": command, **parameters}, allow_nan=False) + "\n"
    try:
        with socket.create_connection((HOST, PORT), timeout=3) as connection:
            connection.settimeout(30)
            connection.sendall(payload.encode("utf-8"))
            with connection.makefile("rb") as stream:
                line = stream.readline(1_048_577)
        if len(line) > 1_048_576:
            raise ValueError("Response too large")
        result = json.loads(line)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ToolError("无法连接 VFXEditor MCP Bridge；请先在 Dalamud 加载独立插件。") from exc
    if not result.get("ok"):
        raise ToolError(result.get("error", "VFXEditor animation request failed."))
    return result


def _pap_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or len(value) > 255 or not value.lower().endswith(".pap") or ".." in value or value.startswith("/") or ":" in value:
        raise ToolError("pap_path 必须是游戏内部 .pap 路径，不能是本地磁盘路径。")
    return value


@mcp.tool()
def vfx_animation_status() -> dict[str, Any]:
    """查询动画桥接插件版本和能力。桥接器不会替换资源或写入游戏文件。"""
    result = call_bridge("health")
    if result.get("protocolVersion") != PROTOCOL_VERSION:
        raise ToolError("桥接协议版本不匹配，请重载新版插件。")
    return result


@mcp.tool()
def inspect_actor_sleeve_skeleton() -> dict[str, Any]:
    """只读采样当前 GPose 目标的袖子相关骨架。

    仅返回固定白名单中的 j_sako_l/r、j_ude_a_l/r、j_ude_b_l/r、j_te_l/r、sl_a..e、sr_a..e，以及 R390 实验的
    r387_sl_Aa..Ce / r387_sr_Aa..Ce；不接受任意骨名或无限制骨架遍历。读取所有 partial 中匹配骨骼的
    父索引、reference local/model TRS 与当前 model/local pose TRS。读取直接数组且不会调用 Havok
    同步/accessor，不播放、冻结或切换动作，不写角色内存、资源、Penumbra 或游戏文件。
    若当前数组未被 Havok 标为 synchronized，则明确返回 unavailable。
    """
    return call_bridge("inspect_actor_sleeve_skeleton")


@mcp.tool()
def inspect_glove_bone_bindings() -> dict[str, Any]:
    """返回手套骨骼绑定实验诊断已被安全禁用的状态。

    该兼容接口会在任何 actor/native 内存访问前返回 enabled=false。原实现已因国服客户端上的
    AccessViolation 被移除；它不会读取 Model.BoneList、AttachBones 或 AttachBoneMasks。
    """
    return call_bridge("inspect_glove_bone_bindings")


@mcp.tool()
def search_pap_animations(query: str, race_code: str = "c0801", include_common: bool = True) -> dict[str, Any]:
    """按动作中文名、文本命令或内部key搜索 Emote/Common PAP 候选路径。

    race_code 是可玩种族骨架代码，例如猫魅女 c0801。只查询游戏数据，不加载或播放动作；最多返回100项。
    """
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise ToolError("query 必须包含1–100个字符。")
    if not isinstance(race_code, str) or not re.fullmatch(r"c\d{4}", race_code):
        raise ToolError("race_code 格式应为 c 加四位数字，例如 c0801。")
    if type(include_common) is not bool:
        raise ToolError("include_common 必须是布尔值。")
    return call_bridge("search_pap", query=query.strip(), raceCode=race_code, includeCommon=include_common)


@mcp.tool()
def inspect_pap_animation_file(pap_path: str) -> dict[str, Any]:
    """读取一个游戏内 PAP，自动选择其头信息匹配的基础骨架，返回动画、时长、loop、轨道和完整骨骼列表。

    不创建编辑文档、不写游戏文件、不受当前角色或 Penumbra 集合影响。
    """
    return call_bridge("inspect_pap", papPath=_pap_path(pap_path))


@mcp.tool()
def export_pap_animation_gltf(pap_path: str, output_name: str,
                              animation_name: str | None = None,
                              animation_index: int | None = None,
                              skip_unanimated: bool = False) -> dict[str, Any]:
    """把指定 PAP 动画连同匹配骨架导出为完整 glTF+.bin，默认保留未动画骨骼。

    output_name 只能是简单ASCII文件名，输出固定在插件自己的 McpExports 目录。
    animation_name 与 animation_index 至少给一个；同时给出时索引优先。返回路径、SHA256、骨架及轨道覆盖摘要。
    只导出本地文件，不修改游戏资源、Penumbra、角色动作或 redraw。
    """
    pap_path = _pap_path(pap_path)
    if not isinstance(output_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.gltf", output_name):
        raise ToolError("output_name 必须是最多120字符、不含目录的ASCII .gltf文件名。")
    if animation_name is not None and (not isinstance(animation_name, str) or not animation_name.strip() or len(animation_name) > 100):
        raise ToolError("animation_name 必须是非空且不超过100字符的名称。")
    if animation_index is not None and (type(animation_index) is not int or animation_index < 0):
        raise ToolError("animation_index 必须是非负整数。")
    if animation_name is None and animation_index is None:
        raise ToolError("animation_name 与 animation_index 至少提供一个。")
    if type(skip_unanimated) is not bool:
        raise ToolError("skip_unanimated 必须是布尔值。")
    return call_bridge("export_animation", papPath=pap_path, outputName=output_name,
                       animationName=animation_name.strip() if animation_name else None,
                       animationIndex=animation_index, skipUnanimated=skip_unanimated)


@mcp.tool()
def inspect_local_sklb_skeleton(sklb_path: str) -> dict[str, Any]:
    """离线只读检查已配置工作区内的本地 SKLB。

    返回源 SHA256、SKLB/Havok 版本、层和原始父骨架标识、骨名/索引/父子关系及局部参考 TRS；
    mapper 缺失与尚不支持的语义会明确区分。该工具不连接游戏、不调用原生 Havok/NotAssetCc、不修改文件。
    """
    try:
        return inspect_sklb(sklb_path)
    except (SkeletonFileError, HavokTagfileError, OSError, struct.error) as exc:
        raise ToolError(f"SKLB 离线检查失败：{exc}") from exc


@mcp.tool()
def export_local_sklb_skeleton(sklb_path: str, output_path: str,
                               overwrite: bool = False) -> dict[str, Any]:
    """把本地 SKLB 参考骨架导出成 Blender 可导入的 .gltf 或 .glb，并生成机器可读 .skeleton.json。

    输入和输出必须位于已配置工作区；默认拒绝覆盖。导出只创建独立文件，保留原 SKLB，
    不打开或修改现有 Blender 场景。返回每个产物的 SHA256 和容器/骨名顺序读回验证。
    """
    if type(overwrite) is not bool:
        raise ToolError("overwrite 必须是布尔值。")
    try:
        return export_sklb(sklb_path, output_path, overwrite)
    except (SkeletonFileError, HavokTagfileError, OSError, struct.error, json.JSONDecodeError) as exc:
        raise ToolError(f"SKLB 骨架导出失败：{exc}") from exc


@mcp.tool()
def write_local_sklb_bone_translations(sklb_path: str, output_path: str,
                                       expected_source_sha256: str,
                                       edits: list[dict[str, Any]],
                                       overwrite: bool = False) -> dict[str, Any]:
    """离线写出只修改指定骨骼 localReference.translation 的独立 SKLB。

    输入/输出必须位于已配置工作区，输出不得等于源文件且默认拒绝覆盖；必须提供源 SHA256。
    edits 是 [{"bone":"sl_a","translation":[x,y,z]}]，重复或不存在的骨名、非有限数值及超过
    ±100 模型单位的分量都会拒绝。工具完整解析并重序列化 Havok tagfile，写盘前后重新读取整个已支持
    对象图，确认骨名/顺序、父索引、非目标 TRS、header/layer/mapper 均保持；不连接或修改游戏。
    """
    if type(overwrite) is not bool:
        raise ToolError("overwrite 必须是布尔值。")
    try:
        return write_sklb_local_translations(
            sklb_path, output_path, expected_source_sha256, edits, overwrite)
    except (SkeletonFileError, HavokTagfileError, OSError, struct.error, ValueError) as exc:
        raise ToolError(f"SKLB 本地平移写出失败：{exc}") from exc


@mcp.tool()
def inspect_local_phyb(phyb_path: str) -> dict[str, Any]:
    """离线只读检查已配置工作区内的本地 PHYB 链、节点和参数。

    返回重力、风、阻尼、链节点、碰撞计数/引用与节点碰撞 flags。若存在未实现的扩展或非链段，
    会拒绝或明确标记 partial；不会把字节解析冒充游戏物理解算与穿模验证。
    """
    try:
        return inspect_phyb(phyb_path)
    except (SkeletonFileError, OSError, struct.error, UnicodeDecodeError) as exc:
        raise ToolError(f"PHYB 离线检查失败：{exc}") from exc


def _tmb_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or len(value) > 255 or not value.lower().endswith(".tmb") or ".." in value or value.startswith("/") or ":" in value:
        raise ToolError("tmb_path 必须是游戏内部 .tmb 路径，不能是 PAP 或本地磁盘路径。")
    return value


@mcp.tool()
def play_actor_animation(action_timeline_id: int | None = None,
                         tmb_path: str | None = None,
                         loop_mode: str = "native") -> dict[str, Any]:
    """仅在 GPose 中对当前 GPose 目标播放真实 ActionTimeline 动作。

    必须给 action_timeline_id 或可解析到该 ID 的游戏内部 TMB 路径；PAP 不是可直接播放的 TMB。
    loop_mode='native' 不干涉动作本身的循环属性；'forced_replay' 会在主身体动画抵达末尾时重新触发。
    非 GPose 请求会在任何角色写入前拒绝。此工具只控制动作，不修改模型、蒙皮、Penumbra 文件或游戏文件。
    """
    if action_timeline_id is not None and (type(action_timeline_id) is not int or not 1 <= action_timeline_id <= 65535):
        raise ToolError("action_timeline_id 必须是 1–65535 的整数。")
    if tmb_path is not None:
        if not isinstance(tmb_path, str):
            raise ToolError("tmb_path 必须是字符串。")
        tmb_path = _tmb_path(tmb_path)
    if action_timeline_id is None and tmb_path is None:
        raise ToolError("action_timeline_id 与 tmb_path 至少提供一个。")
    if loop_mode not in ("native", "forced_replay"):
        raise ToolError("loop_mode 只能是 native 或 forced_replay。")
    return call_bridge("play_actor_animation", timelineId=action_timeline_id,
                       tmbPath=tmb_path, forceReplay=loop_mode == "forced_replay")


@mcp.tool()
def freeze_actor_animation(frame: int, fps: float = 30.0) -> dict[str, Any]:
    """冻结桥接器当前拥有的动作到零基帧，并读回实际主身体时间验证。

    fps 仅用于 frame/fps 换算，不改变游戏播放速率。等待 slot、Havok 控制器身份和30fps时间轴时钟一致后才暂停；
    回读必须仍是请求的整数帧。只处理 partial 0 / control 0；不会盲目同步脸、武器等复合控制器。
    仅在 GPose 且 Brio API v2 确实管理当前目标时可用。普通模式和无 Brio 后端会在写入前拒绝；
    若 Brio 已预先冻结或设置非 1 速度，会报告所有权冲突而不是与它抢写。
    """
    if type(frame) is not int or frame < 0:
        raise ToolError("frame 必须是非负整数（零基）。")
    if type(fps) not in (int, float) or isinstance(fps, bool) or not 1 <= fps <= 240:
        raise ToolError("fps 必须在 1–240 之间。")
    return call_bridge("freeze_actor_animation", frame=frame, fps=float(fps))


@mcp.tool()
def get_actor_animation_state() -> dict[str, Any]:
    """查询受控角色、ActionTimeline、主身体实际时间/时长、速度、循环方式和冻结读回状态。"""
    return call_bridge("get_actor_animation_state")


@mcp.tool()
def resume_actor_animation() -> dict[str, Any]:
    """解除桥接器拥有的冻结，并等待数个游戏帧确认主身体动画时间确实继续前进。"""
    return call_bridge("resume_actor_animation")


@mcp.tool()
def reset_actor_animation() -> dict[str, Any]:
    """先解除暂停，再恢复桥接器接管前的角色模式、基础覆盖和实际 slot-0 ActionTimeline；不会遗留速度 0。"""
    return call_bridge("reset_actor_animation")


if __name__ == "__main__":
    mcp.run()
