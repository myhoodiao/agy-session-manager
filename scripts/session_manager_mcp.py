"""
Agy 会话检索与命名管家（Agy-Session-Manager）MCP Server。

提供 4 个工具：
- scan_agy_sessions：只读扫描全部本地 Agy 会话（含真实对话首尾摘要、步数、产物、进程锁状态及孤立残留文件）。
- search_agy_sessions：按关键词、项目路径或模糊描述检索历史会话，返回会话名称+ID、定位依据及 agy --conversation <ID> 命令。
- preview_session_changes：只读校验重命名与清理方案，生成《清理排查清单》和《改名前后对照表》，并签发一次性 confirmation_token。
- execute_session_changes：在用户明确回复确认后，凭一次性 confirmation_token 和用户确认原话执行完整备份、批量改删与回读核验。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_project_root() -> Path:
    raw = os.environ.get("AGY_SESSION_MANAGER_ROOT", "").strip()
    root = Path(raw) if raw else DEFAULT_PROJECT_ROOT
    script_path = root / "scripts" / "session_manager.py"
    if not script_path.is_file():
        # 兼容直接将 session_manager.py 与 session_manager_mcp.py 放在同级目录的部署方式
        alt_script = Path(__file__).resolve().parent / "session_manager.py"
        if alt_script.is_file():
            return alt_script.parent.parent
        raise FileNotFoundError(f"Agy-Session-Manager 核心后端缺失：{script_path}")
    return root


def load_backend_module(project_root: Path | None = None) -> Any:
    root = project_root or resolve_project_root()
    script_path = root / "scripts" / "session_manager.py"
    spec = importlib.util.spec_from_file_location("agy_session_manager_backend", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载核心后端：{script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def handle_scan_agy_sessions(arguments: dict[str, Any]) -> dict[str, Any]:
    sm = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = sm.scan_sessions()
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def handle_search_agy_sessions(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query", "") or "")
    workspace_filter = arguments.get("workspace_filter")
    sm = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = sm.search_sessions(query=query, workspace_filter=workspace_filter)
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def handle_preview_session_changes(arguments: dict[str, Any]) -> dict[str, Any]:
    renames = arguments.get("renames") or []
    deletes = arguments.get("deletes") or []
    uncertain = arguments.get("uncertain") or []
    keeps = arguments.get("keeps") or []
    clean_orphans = bool(arguments.get("clean_orphans", True))

    if not isinstance(renames, list) or not isinstance(deletes, list):
        raise ValueError("参数 'renames' 和 'deletes' 必须为列表。")

    sm = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = sm.preview_changes(
            renames=renames,
            deletes=deletes,
            uncertain=uncertain,
            keeps=keeps,
            clean_orphans=clean_orphans,
        )
    return {
        "content": [{"type": "text", "text": result["preview_markdown"]}],
        "isError": False,
    }


def handle_execute_session_changes(arguments: dict[str, Any]) -> dict[str, Any]:
    token = arguments.get("confirmation_token")
    quote = arguments.get("user_confirmation_quote")

    sm = load_backend_module()
    with contextlib.redirect_stdout(sys.stderr):
        result = sm.execute_changes(
            confirmation_token=token,
            user_confirmation_quote=quote,
        )

    ver = result["verification"]
    status_icon = "✅ 已全部执行并通过回读核验" if ver["passed"] else "⚠️ 已执行但存在核验提示"
    lines = [
        f"### {status_icon}",
        "",
        f"- **备份目录**：`{result['backup_dir']}`",
        f"- **一键还原命令**：`{result['restore_command']}`",
        f"- **已重命名会话**：{result['renamed_count']} 条",
        f"- **已清理无效会话**：{result['deleted_session_count']} 条（另清理无主残留 `.lock` {result['deleted_orphan_count']} 个）",
        f"- **释放逻辑空间**：{result['freed_logical_bytes']:,} bytes",
        f"- **当前剩余有效会话**：{ver['remaining_session_count']} 条",
        f"- **四处一致性回读核验**：{'通过（SQLite 摘要表、Protobuf 字段、FTS5 全文索引、pbtxt 注释、jetbox 汇总文件全部一致，已删项零残留）' if ver['passed'] else '; '.join(ver['issues'])}",
    ]
    if result.get("delayed_sync_sessions"):
        lines.append(
            f"- **活跃终端延迟同步**：已对当前开启中的会话 `{result['delayed_sync_sessions']}` 启动后台 3.5s/7.0s 延迟补刷，防止本轮回复结束时内存旧标题回写覆盖。"
        )

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }


TOOLS_LIST = [
    {
        "name": "scan_agy_sessions",
        "description": (
            "只读扫描本机全部 Agy CLI 本地会话，返回每条会话的 ID、当前标题、步数、最后修改时间、"
            "工作区路径、终端文件锁状态（is_locked）、首尾对话内容摘要、brain 产出物列表及孤立残留文件。"
            "整理会话或检索会话前应优先调用本工具获取真实全貌。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_agy_sessions",
        "description": (
            "按关键词、工作区路径或模糊描述检索本机 Agy 历史会话。"
            "返回匹配会话的名称、ID、定位依据（匹配到的标题、路径、对话原文或产出文件）及便捷进入命令 agy --conversation <ID>。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（多个词用空格分隔，匹配标题、对话全文、工作区路径及产出文件名）",
                },
                "workspace_filter": {
                    "type": "string",
                    "description": "可选，按项目或工作区路径过滤",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "preview_session_changes",
        "description": (
            "只读校验会话重命名与清理方案（检查简体中文、15~25字以内、无省略号、非模糊名、终端锁状态），"
            "生成《清理排查清单》和《改名前后对照表》核对卡，并签发一次性 confirmation_token。"
            "任何修改或删除会话前必须先调用本工具，向用户完整展示预览表并停下来等待用户确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "renames": {
                    "type": "array",
                    "description": "拟重命名的会话列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "conversation_id": {"type": "string"},
                            "new_title": {
                                "type": "string",
                                "description": "规范简体中文新标题（15~25字以内，项目类用‘项目名称-简短说明’，工具类用‘分类前缀-具体事项’）",
                            },
                            "category": {
                                "type": "string",
                                "description": "分类标签，如‘项目类’或‘工具类’",
                            },
                            "reason": {"type": "string", "description": "重命名说明"},
                        },
                        "required": ["conversation_id", "new_title"],
                        "additionalProperties": False,
                    },
                },
                "deletes": {
                    "type": "array",
                    "description": "拟清理删除的无效会话列表（严禁仅因轮次少就删除，必须确认无实质成果或属于纯测试/重复中断废会话）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "conversation_id": {"type": "string"},
                            "reason": {"type": "string", "description": "判定为无效会话的具体依据"},
                        },
                        "required": ["conversation_id", "reason"],
                        "additionalProperties": False,
                    },
                },
                "uncertain": {
                    "type": "array",
                    "description": "可选，拿不准、需单独向用户请示是否保留的会话列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "conversation_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["conversation_id", "reason"],
                        "additionalProperties": False,
                    },
                },
                "keeps": {
                    "type": "array",
                    "description": "可选，各保留会话的具体保留理由说明（未传的保留会话将自动补齐到清单中）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "conversation_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["conversation_id", "reason"],
                        "additionalProperties": False,
                    },
                },
                "clean_orphans": {
                    "type": "boolean",
                    "description": "是否顺带清理数据库中不存在的无主孤立 .lock 残留文件（默认 true）",
                },
            },
            "required": ["renames", "deletes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "execute_session_changes",
        "description": (
            "在用户看过 preview_session_changes 生成的预览表并在下一条消息中明确回复确认后，"
            "凭一次性 confirmation_token 和用户确认原话执行：完整备份至 D:\\Agy Backups -> "
            "联动修改 SQLite/FTS5/Protobuf/pbtxt 并清理无效会话 -> 自动回读核验。"
            "严禁在同一轮对话中连续调用 preview_session_changes 与 execute_session_changes。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirmation_token": {
                    "type": "string",
                    "description": "由 preview_session_changes 返回的一次性确认令牌",
                },
                "user_confirmation_quote": {
                    "type": "string",
                    "description": "用户在查看预览表后的下一条消息中明确回复的确认原话（如‘同意，执行吧’）",
                },
            },
            "required": ["confirmation_token", "user_confirmation_quote"],
            "additionalProperties": False,
        },
    },
]


def handle_jsonrpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        try:
            sm = load_backend_module()
            with contextlib.redirect_stdout(sys.stderr):
                sm.reconcile_disk_truth()
        except Exception:
            pass
        client_ver = params.get("protocolVersion")
        negotiated = (
            client_ver
            if isinstance(client_ver, str) and client_ver in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "agy-session-manager-mcp",
                    "version": "1.0.0",
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_LIST}}

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if tool_name == "scan_agy_sessions":
                result = handle_scan_agy_sessions(arguments)
            elif tool_name == "search_agy_sessions":
                result = handle_search_agy_sessions(arguments)
            elif tool_name == "preview_session_changes":
                result = handle_preview_session_changes(arguments)
            elif tool_name == "execute_session_changes":
                result = handle_execute_session_changes(arguments)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"未知工具: {tool_name}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"执行失败: {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_jsonrpc_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
