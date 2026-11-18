"""
Agy 会话检索与命名管家（Agy-Session-Manager）核心引擎。

职责：
1. 只读盘点与全文/元数据检索本机 Agy CLI 会话（%USERPROFILE%\\.gemini\\antigravity-cli）。
2. 生成《清理排查清单》与《改名前后对照表》预览核对卡，并签发一次性 confirmation_token。
3. 校验确认令牌后，先完整备份至默认备份目录（含 SHA-256 清单与一键还原脚本 restore.py），
   再联动修改 annotations/<id>.pbtxt、conversation_summaries.db（含 FTS5 与 raw_summary Protobuf）、
   jetbox_summaries_proto.pb，并清理已确认删除会话的磁盘文件。
4. 执行后自动回读核验（Read-back Verification），确保四处标题一致、已删会话零残留、保留会话完好可读。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any

DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity-cli"
DEFAULT_BACKUP_ROOT = (
    Path(r"D:\Project\agy Backups")
    if Path(r"D:\Project").exists()
    else (Path(r"D:\Agy Backups") if Path("D:/").exists() else (Path.home() / "Agy Backups"))
)
TOKEN_TTL_SECONDS = 1800
MAX_TITLE_CHARS = 25

PLACEHOLDER_CONFIRMATIONS = {
    "",
    "confirm",
    "confirmed",
    "yes",
    "ok",
    "true",
    "1",
    "placeholder",
    "auto",
    "待确认",
    "自动确认",
}

FORBIDDEN_EXACT_TITLES = {
    "新会话",
    "未命名",
    "未命名会话",
    "新建会话",
    "默认会话",
    "测试",
    "测试会话",
    "new session",
    "untitled",
    "conversation",
}


def resolve_data_dir(override: str | Path | None = None) -> Path:
    raw = str(override or os.environ.get("AGY_SESSION_DATA_DIR", "")).strip()
    path = Path(raw) if raw else DEFAULT_DATA_DIR
    if not path.is_dir():
        raise FileNotFoundError(f"Agy 数据目录不存在：{path}")
    return path


def resolve_backup_root(override: str | Path | None = None) -> Path:
    raw = str(override or os.environ.get("AGY_SESSION_BACKUP_DIR", "")).strip()
    return Path(raw) if raw else DEFAULT_BACKUP_ROOT


def resolve_state_file(data_dir: Path | None = None) -> Path:
    raw = os.environ.get("AGY_SESSION_STATE_DIR", "").strip()
    if raw:
        state_dir = Path(raw)
    else:
        base = data_dir or resolve_data_dir()
        state_dir = base / "session-manager-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "pending_confirmations.json"


# ============================================================================
# 1. 二进制 Protobuf 无损编解码器
# ============================================================================

def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    n = len(data)
    while offset < n:
        b = data[offset]
        offset += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("Protobuf varint 超出 64 位上限")
    raise ValueError("Protobuf varint 数据截断")


def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def parse_proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    offset = 0
    n = len(data)
    fields: list[tuple[int, int, int | bytes]] = []
    while offset < n:
        tag, offset = decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0:
            raise ValueError(f"非法 Protobuf field_num: {field_num}")
        if wire_type == 0:
            val, offset = decode_varint(data, offset)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            if offset + 8 > n:
                raise ValueError("Protobuf 64-bit 字段截断")
            fields.append((field_num, wire_type, data[offset : offset + 8]))
            offset += 8
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            if offset + length > n:
                raise ValueError("Protobuf length-delimited 字段截断")
            fields.append((field_num, wire_type, data[offset : offset + length]))
            offset += length
        elif wire_type == 5:
            if offset + 4 > n:
                raise ValueError("Protobuf 32-bit 字段截断")
            fields.append((field_num, wire_type, data[offset : offset + 4]))
            offset += 4
        else:
            raise ValueError(f"不支持的 Protobuf wire_type: {wire_type}")
    return fields


def encode_proto_fields(fields: list[tuple[int, int, int | bytes]]) -> bytes:
    out = bytearray()
    for field_num, wire_type, val in fields:
        tag = (field_num << 3) | wire_type
        out.extend(encode_varint(tag))
        if wire_type == 0:
            if not isinstance(val, int):
                raise TypeError("wire_type 0 需要 int 值")
            out.extend(encode_varint(val))
        elif wire_type in (1, 5):
            if not isinstance(val, (bytes, bytearray)):
                raise TypeError(f"wire_type {wire_type} 需要 bytes 值")
            out.extend(val)
        elif wire_type == 2:
            if not isinstance(val, (bytes, bytearray)):
                raise TypeError("wire_type 2 需要 bytes 值")
            out.extend(encode_varint(len(val)))
            out.extend(val)
        else:
            raise ValueError(f"不支持的 wire_type: {wire_type}")
    return bytes(out)


def extract_raw_summary_titles(raw_bytes: bytes | None) -> tuple[str, str]:
    """从 raw_summary 二进制中提取 (field_1_title, field_15_annotation_title)。"""
    if not raw_bytes:
        return "", ""
    fields = parse_proto_fields(raw_bytes)
    f1_title = ""
    f15_title = ""
    for fnum, wtype, val in fields:
        if fnum == 1 and wtype == 2 and isinstance(val, bytes):
            f1_title = val.decode("utf-8", errors="replace")
        elif fnum == 15 and wtype == 2 and isinstance(val, bytes):
            try:
                subfields = parse_proto_fields(val)
                for sfnum, swtype, sval in subfields:
                    if sfnum == 1 and swtype == 2 and isinstance(sval, bytes):
                        f15_title = sval.decode("utf-8", errors="replace")
            except Exception:
                pass
    return f1_title, f15_title


def update_raw_summary_title(raw_bytes: bytes, new_title: str) -> bytes:
    """
    更新 raw_summary Protobuf 中的 Field 1 与 Field 15 (Annotation.title)，
    其余字段保持原字节不变。
    """
    title_bytes = new_title.encode("utf-8")
    fields = parse_proto_fields(raw_bytes) if raw_bytes else []

    new_fields: list[tuple[int, int, int | bytes]] = []
    has_f1 = False
    has_f15 = False

    for fnum, wtype, val in fields:
        if fnum == 1 and wtype == 2:
            new_fields.append((1, 2, title_bytes))
            has_f1 = True
        elif fnum == 15 and wtype == 2 and isinstance(val, bytes):
            try:
                subfields = parse_proto_fields(val)
            except Exception:
                subfields = []
            new_sub: list[tuple[int, int, int | bytes]] = []
            sub_has_f1 = False
            for sfnum, swtype, sval in subfields:
                if sfnum == 1 and swtype == 2:
                    new_sub.append((1, 2, title_bytes))
                    sub_has_f1 = True
                else:
                    new_sub.append((sfnum, swtype, sval))
            if not sub_has_f1:
                new_sub.insert(0, (1, 2, title_bytes))
            new_fields.append((15, 2, encode_proto_fields(new_sub)))
            has_f15 = True
        else:
            new_fields.append((fnum, wtype, val))

    if not has_f1:
        new_fields.insert(0, (1, 2, title_bytes))

    if not has_f15:
        ann_blob = encode_proto_fields([(1, 2, title_bytes)])
        inserted = False
        for idx, (fnum, _, _) in enumerate(new_fields):
            if fnum > 15:
                new_fields.insert(idx, (15, 2, ann_blob))
                inserted = True
                break
        if not inserted:
            new_fields.append((15, 2, ann_blob))

    return encode_proto_fields(new_fields)


def parse_jetbox_pb(pb_bytes: bytes) -> dict[str, bytes]:
    """解析 jetbox_summaries_proto.pb 为 {conversation_id: raw_summary_bytes}。"""
    result: dict[str, bytes] = {}
    if not pb_bytes:
        return result
    for fnum, wtype, val in parse_proto_fields(pb_bytes):
        if fnum == 1 and wtype == 2 and isinstance(val, bytes):
            cid = ""
            raw = b""
            for sfnum, swtype, sval in parse_proto_fields(val):
                if sfnum == 1 and swtype == 2 and isinstance(sval, bytes):
                    cid = sval.decode("utf-8", errors="replace")
                elif sfnum == 2 and swtype == 2 and isinstance(sval, bytes):
                    raw = sval
            if cid:
                result[cid] = raw
    return result


def update_jetbox_pb_bytes(
    pb_bytes: bytes,
    updated_raw_map: dict[str, bytes],
    deleted_ids: set[str],
) -> bytes:
    """更新或删除 jetbox_summaries_proto.pb 中的指定条目，其余条目保持原样。"""
    top_fields = parse_proto_fields(pb_bytes) if pb_bytes else []
    new_top: list[tuple[int, int, int | bytes]] = []
    seen_ids: set[str] = set()

    for fnum, wtype, val in top_fields:
        if fnum == 1 and wtype == 2 and isinstance(val, bytes):
            subfields = parse_proto_fields(val)
            cid = ""
            for sfnum, swtype, sval in subfields:
                if sfnum == 1 and swtype == 2 and isinstance(sval, bytes):
                    cid = sval.decode("utf-8", errors="replace")
                    break
            if cid in deleted_ids:
                continue
            if cid in updated_raw_map:
                seen_ids.add(cid)
                new_sub: list[tuple[int, int, int | bytes]] = []
                for sfnum, swtype, sval in subfields:
                    if sfnum == 2 and swtype == 2:
                        new_sub.append((2, 2, updated_raw_map[cid]))
                    else:
                        new_sub.append((sfnum, swtype, sval))
                new_top.append((1, 2, encode_proto_fields(new_sub)))
            else:
                if cid:
                    seen_ids.add(cid)
                new_top.append((fnum, wtype, val))
        else:
            new_top.append((fnum, wtype, val))

    for cid, raw_val in updated_raw_map.items():
        if cid not in seen_ids and cid not in deleted_ids:
            entry_bytes = encode_proto_fields([
                (1, 2, cid.encode("utf-8")),
                (2, 2, raw_val),
            ])
            new_top.append((1, 2, entry_bytes))

    return encode_proto_fields(new_top)


# ============================================================================
# 2. Annotations pbtxt 与文件锁工具
# ============================================================================

def format_annotation_pbtxt(title: str) -> bytes:
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    return f'title:"{escaped}"'.encode("utf-8")


def parse_annotation_pbtxt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    m = re.search(r'title\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not m:
        return ""
    inner = m.group(1)
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def is_session_locked(data_dir: Path, conversation_id: str) -> bool:
    """
    通过 Windows msvcrt.locking 无损探测 presence/<id>.lock 是否被运行中的 agy.exe 锁定。
    """
    lock_path = data_dir / "presence" / f"{conversation_id}.lock"
    if not lock_path.is_file():
        return False
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        except OSError:
            return True
    except OSError:
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ============================================================================
# 3. 标题合规校验
# ============================================================================

def validate_new_title(title: str) -> list[str]:
    """
    按 REQUIREMENTS.md 4.2 校验新标题：
    1. 非空且无换行/控制字符
    2. 长度控制在 25 个字符以内（建议 8~25 字）
    3. 禁止使用省略号（... 或 …）
    4. 禁止使用“新会话”等模糊无意义名称
    5. 必须包含简体中文，禁止纯英文标题
    """
    errors: list[str] = []
    if not isinstance(title, str):
        return ["标题必须为字符串"]
    clean = title.strip()
    if not clean:
        return ["标题不能为空"]
    if any(ord(ch) < 32 for ch in clean):
        errors.append("标题不能包含换行或控制字符")
    char_len = len(clean)
    if char_len > MAX_TITLE_CHARS:
        errors.append(f"标题长度不能超过 {MAX_TITLE_CHARS} 个字（当前 {char_len} 字：{clean}）")
    if char_len < 4:
        errors.append(f"标题过短（当前 {char_len} 字），请提供准确具体的中文标题")
    if "..." in clean or "…" in clean:
        errors.append("标题禁止使用省略号（... 或 …）")
    if clean.lower() in FORBIDDEN_EXACT_TITLES:
        errors.append(f"禁止使用模糊无意义名称：{clean}")
    if not re.search(r"[\u4e00-\u9fff]", clean):
        errors.append("标题必须使用简体中文，禁止纯英文或纯符号标题")
    return errors


# ============================================================================
# 4. 只读盘点与检索
# ============================================================================

def _clean_transcript_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = raw_text
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    else:
        text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
        text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
        text = re.sub(r"<SYSTEM_MESSAGE>.*?</SYSTEM_MESSAGE>", "", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _inspect_brain_dir(brain_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "user_turn_count": 0,
        "planner_turn_count": 0,
        "first_user_prompt": "",
        "last_user_prompt": "",
        "last_planner_response": "",
        "artifacts": [],
        "uploaded_media": [],
    }
    if not brain_dir.is_dir():
        return info

    transcript_path = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if transcript_path.is_file():
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    step_type = obj.get("type", "")
                    content = obj.get("content", "") or ""
                    if step_type == "USER_INPUT":
                        info["user_turn_count"] += 1
                        cleaned = _clean_transcript_text(content)
                        if cleaned:
                            if not info["first_user_prompt"]:
                                info["first_user_prompt"] = cleaned[:240]
                            info["last_user_prompt"] = cleaned[:240]
                    elif step_type == "PLANNER_RESPONSE":
                        info["planner_turn_count"] += 1
                        cleaned = _clean_transcript_text(content)
                        if cleaned:
                            info["last_planner_response"] = cleaned[:240]
        except Exception:
            pass

    for root, dirs, files in os.walk(brain_dir):
        rel_root = Path(root).relative_to(brain_dir)
        parts = rel_root.parts
        if parts and parts[0] == ".system_generated":
            continue
        for f in files:
            rel_path = str((rel_root / f) if parts else Path(f)).replace("\\", "/")
            if parts and parts[0] == ".user_uploaded":
                info["uploaded_media"].append(rel_path)
            else:
                info["artifacts"].append(rel_path)

    info["artifacts"].sort()
    info["uploaded_media"].sort()
    return info


def _compute_session_bytes(data_dir: Path, cid: str) -> int:
    total = 0
    for suffix in (".db", ".db-wal", ".db-shm"):
        p = data_dir / "conversations" / f"{cid}{suffix}"
        if p.is_file():
            total += p.stat().st_size
    ann_p = data_dir / "annotations" / f"{cid}.pbtxt"
    if ann_p.is_file():
        total += ann_p.stat().st_size
    lock_p = data_dir / "presence" / f"{cid}.lock"
    if lock_p.is_file():
        total += lock_p.stat().st_size
    brain_p = data_dir / "brain" / cid
    if brain_p.is_dir():
        for root, _, files in os.walk(brain_p):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    return total


def _get_summaries_table_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
    return {str(row[1]) for row in rows if len(row) > 1}


def _update_metadata_cache(
    data_dir: Path,
    title_updates: dict[str, str],
    deleted_ids: set[str],
) -> None:
    cache_path = data_dir / "cache" / "conversation_metadata.json"
    if not cache_path.is_file() or (not title_updates and not deleted_ids):
        return
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    convs = raw.get("conversations")
    if not isinstance(convs, dict):
        return

    changed = False
    for cid in deleted_ids:
        if cid in convs:
            convs.pop(cid, None)
            changed = True

    for cid, new_title in title_updates.items():
        if cid in deleted_ids:
            continue
        entry = convs.get(cid)
        if isinstance(entry, dict):
            summary = entry.get("summary")
            if isinstance(summary, dict) and summary.get("Title") != new_title:
                summary["Title"] = new_title
                changed = True

    if changed:
        tmp_path = cache_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, cache_path)


def _notify_watcher_deleted(data_dir: Path, deleted_ids: set[str]) -> None:
    """
    针对磁盘原本无 conversations/<id>.db 的待删/幽灵条目，短暂创建并删除 0 字节 .db 文件，
    以触发运行中 agy.exe 的 fsnotify 删除事件（DeleteSummary），主动清理其内存缓存。
    """
    if not deleted_ids:
        return
    conv_dir = data_dir / "conversations"
    if not conv_dir.is_dir():
        return
    for cid in deleted_ids:
        if not cid or "/" in cid or "\\" in cid:
            continue
        db_p = conv_dir / f"{cid}.db"
        if db_p.exists():
            continue
        created = False
        try:
            db_p.write_bytes(b"")
            created = True
        except OSError:
            pass
        finally:
            if created:
                try:
                    db_p.unlink(missing_ok=True)
                except OSError:
                    pass


def reconcile_disk_truth(data_dir: Path | None = None) -> dict[str, Any]:
    """
    以磁盘底稿（conversations/<id>.db 管生死，annotations/<id>.pbtxt 管标题）为真值源，
    幂等清理 CLI 空壳幽灵记录并自愈被旧进程覆盖回退的标题分裂。
    """
    base = resolve_data_dir(data_dir)
    conv_dir = base / "conversations"
    ann_dir = base / "annotations"
    db_path = base / "conversation_summaries.db"
    pb_path = base / "jetbox_summaries_proto.pb"

    valid_conv_ids = {p.stem for p in conv_dir.glob("*.db")} if conv_dir.is_dir() else set()

    pb_bytes = b""
    pb_map: dict[str, bytes] = {}
    if pb_path.is_file():
        try:
            pb_bytes = pb_path.read_bytes()
            pb_map = parse_jetbox_pb(pb_bytes)
        except Exception:
            pb_bytes = b""
            pb_map = {}

    db_info: dict[str, dict[str, Any]] = {}
    fts_titles: dict[str, set[str]] = {}
    non_cli_ids: set[str] = set()

    if db_path.is_file():
        conn_ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            cols = _get_summaries_table_columns(conn_ro)
            has_app_col = "app_data_dir" in cols
            has_depth_col = "nesting_depth" in cols
            select_cols = ["conversation_id", "title", "preview", "raw_summary"]
            if has_app_col:
                select_cols.append("app_data_dir")
            if has_depth_col:
                select_cols.append("nesting_depth")
            sql = f"SELECT {', '.join(select_cols)} FROM conversation_summaries"
            for row in conn_ro.execute(sql):
                cid = row[0]
                if not cid:
                    continue
                idx = 4
                app_dir = ""
                depth = 0
                if has_app_col:
                    app_dir = str(row[idx] or "")
                    idx += 1
                if has_depth_col:
                    depth = int(row[idx] or 0)
                if (has_app_col and app_dir not in ("", "antigravity-cli")) or (
                    has_depth_col and depth != 0
                ):
                    non_cli_ids.add(cid)
                raw_bytes = row[3] or b""
                f1, f15 = extract_raw_summary_titles(raw_bytes)
                db_info[cid] = {
                    "title": row[1] or "",
                    "preview": row[2] or "",
                    "raw_bytes": raw_bytes,
                    "raw_f1": f1,
                    "raw_f15": f15,
                }
            for fcid, ftitle in conn_ro.execute("SELECT conversation_id, title FROM conversations_fts"):
                if fcid:
                    fts_titles.setdefault(fcid, set()).add(ftitle or "")
        finally:
            conn_ro.close()

    # 移除属于非 CLI 会话且无本地 .db 的误植 annotations/*.pbtxt
    if ann_dir.is_dir() and non_cli_ids:
        for ann_p in ann_dir.glob("*.pbtxt"):
            if ann_p.stem in non_cli_ids and ann_p.stem not in valid_conv_ids:
                try:
                    ann_p.unlink(missing_ok=True)
                except OSError:
                    pass

    # 识别并剔除 CLI 空壳幽灵记录
    candidate_ids = (set(db_info.keys()) | set(pb_map.keys())) - non_cli_ids - valid_conv_ids
    ghost_cli_ids: set[str] = {
        cid for cid in candidate_ids if not is_session_locked(base, cid)
    }

    # 识别有效 CLI 会话中的分裂标题
    ann_titles: dict[str, str] = {}
    title_repairs: list[dict[str, str]] = []
    db_title_updates: dict[str, tuple[str, bytes]] = {}
    fts_title_updates: dict[str, str] = {}
    pb_updates: dict[str, bytes] = {}

    if ann_dir.is_dir():
        for cid in sorted(valid_conv_ids):
            if cid in non_cli_ids:
                continue
            ann_p = ann_dir / f"{cid}.pbtxt"
            if not ann_p.is_file():
                continue
            try:
                ann_title = parse_annotation_pbtxt(ann_p.read_bytes()).strip()
            except Exception:
                continue
            if not ann_title:
                continue
            ann_titles[cid] = ann_title

            db_mismatch = False
            if cid in db_info:
                d = db_info[cid]
                if (
                    d["title"] != ann_title
                    or d["preview"] != ann_title
                    or d["raw_f1"] != ann_title
                    or d["raw_f15"] != ann_title
                ):
                    db_mismatch = True

            fts_mismatch = cid in fts_titles and any(t != ann_title for t in fts_titles[cid])

            pb_mismatch = False
            if cid in pb_map:
                pf1, pf15 = extract_raw_summary_titles(pb_map[cid])
                if pf1 != ann_title or pf15 != ann_title:
                    pb_mismatch = True

            if db_mismatch or fts_mismatch or pb_mismatch:
                base_raw = (
                    db_info[cid]["raw_bytes"]
                    if (cid in db_info and db_info[cid]["raw_bytes"])
                    else pb_map.get(cid, b"")
                )
                new_raw = update_raw_summary_title(base_raw, ann_title)
                if db_mismatch:
                    db_title_updates[cid] = (ann_title, new_raw)
                if fts_mismatch:
                    fts_title_updates[cid] = ann_title
                if pb_mismatch or (cid in pb_map and db_mismatch):
                    pb_updates[cid] = new_raw
                title_repairs.append({"conversation_id": cid, "repaired_title": ann_title})

    if db_path.is_file() and (ghost_cli_ids or db_title_updates or fts_title_updates):
        conn = sqlite3.connect(str(db_path), timeout=15.0)
        try:
            cur = conn.cursor()
            for cid, (new_title, new_raw) in db_title_updates.items():
                cur.execute(
                    """
                    UPDATE conversation_summaries
                    SET title = ?, preview = ?, raw_summary = ?
                    WHERE conversation_id = ?
                    """,
                    (new_title, new_title, new_raw, cid),
                )
            for cid, new_title in fts_title_updates.items():
                cur.execute(
                    "UPDATE conversations_fts SET title = ? WHERE conversation_id = ?",
                    (new_title, cid),
                )
            for cid in ghost_cli_ids:
                cur.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
                cur.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,))
                cur.execute(
                    "DELETE FROM conversation_parents WHERE conversation_id = ? OR parent_id = ?",
                    (cid, cid),
                )
            conn.commit()
        finally:
            conn.close()

    pb_deleted_ids = {cid for cid in ghost_cli_ids if cid in pb_map}
    if pb_path.is_file() and (pb_deleted_ids or pb_updates):
        new_pb_bytes = update_jetbox_pb_bytes(pb_bytes, pb_updates, pb_deleted_ids)
        pb_tmp = pb_path.with_suffix(".pb.tmp")
        pb_tmp.write_bytes(new_pb_bytes)
        os.replace(pb_tmp, pb_path)

    if pb_deleted_ids:
        _notify_watcher_deleted(base, pb_deleted_ids)

    _update_metadata_cache(base, ann_titles, ghost_cli_ids)

    return {
        "purged_ghost_ids": sorted(ghost_cli_ids),
        "repaired_titles": title_repairs,
    }


def scan_sessions(data_dir: Path | None = None) -> dict[str, Any]:
    """
    只读盘点本机全部 Agy CLI 会话与孤立残留文件（盘点前先执行幂等底稿自愈对账）。
    """
    base = resolve_data_dir(data_dir)
    reconcile_disk_truth(base)
    db_path = base / "conversation_summaries.db"
    pb_path = base / "jetbox_summaries_proto.pb"

    pb_map: dict[str, bytes] = {}
    if pb_path.is_file():
        try:
            pb_map = parse_jetbox_pb(pb_path.read_bytes())
        except Exception:
            pb_map = {}

    db_rows: dict[str, dict[str, Any]] = {}
    fts_ids: set[str] = set()
    non_cli_ids: set[str] = set()
    if db_path.is_file():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cols = _get_summaries_table_columns(conn)
            where_clauses: list[str] = []
            non_cli_clauses: list[str] = []
            if "app_data_dir" in cols:
                where_clauses.append("app_data_dir IN ('', 'antigravity-cli')")
                non_cli_clauses.append("app_data_dir NOT IN ('', 'antigravity-cli')")
            if "nesting_depth" in cols:
                where_clauses.append("nesting_depth = 0")
                non_cli_clauses.append("nesting_depth != 0")

            if non_cli_clauses:
                for (ncid,) in cur.execute(
                    f"SELECT conversation_id FROM conversation_summaries WHERE {' OR '.join(non_cli_clauses)}"
                ):
                    if ncid:
                        non_cli_ids.add(ncid)

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            for row in cur.execute(
                f"""
                SELECT conversation_id, title, preview, step_count,
                       last_modified_time, workspace_uris, parent_conversation_id, raw_summary
                FROM conversation_summaries
                {where_sql}
                ORDER BY last_modified_time DESC
                """
            ):
                cid, title, preview, step_count, mtime, ws_raw, parent_id, raw_sum = row
                try:
                    ws_list = json.loads(ws_raw) if ws_raw else []
                except Exception:
                    ws_list = [ws_raw] if ws_raw else []
                f1_title, f15_title = extract_raw_summary_titles(raw_sum)
                db_rows[cid] = {
                    "conversation_id": cid,
                    "title": title or "",
                    "preview": preview or "",
                    "raw_f1_title": f1_title,
                    "raw_f15_title": f15_title,
                    "step_count": int(step_count or 0),
                    "last_modified_time": mtime or "",
                    "workspace_uris": ws_list,
                    "parent_conversation_id": parent_id or "",
                }
            for (cid,) in cur.execute("SELECT DISTINCT conversation_id FROM conversations_fts"):
                if cid:
                    fts_ids.add(cid)
        finally:
            conn.close()

    conv_ids = {
        p.stem for p in (base / "conversations").glob("*.db")
    } if (base / "conversations").is_dir() else set()
    brain_ids = {
        p.name for p in (base / "brain").iterdir() if p.is_dir()
    } if (base / "brain").is_dir() else set()
    ann_ids = {
        p.stem for p in (base / "annotations").glob("*.pbtxt")
    } if (base / "annotations").is_dir() else set()
    lock_ids = {
        p.stem for p in (base / "presence").glob("*.lock")
    } if (base / "presence").is_dir() else set()

    sessions: list[dict[str, Any]] = []
    for cid, meta in db_rows.items():
        ann_path = base / "annotations" / f"{cid}.pbtxt"
        ann_title = parse_annotation_pbtxt(ann_path.read_bytes()) if ann_path.is_file() else ""
        brain_info = _inspect_brain_dir(base / "brain" / cid)
        locked = is_session_locked(base, cid)
        locs: list[str] = ["summaries_db"]
        if cid in fts_ids:
            locs.append("fts")
        if cid in pb_map:
            locs.append("jetbox_pb")
        if cid in conv_ids:
            locs.append("conv_db")
        if cid in brain_ids:
            locs.append("brain")
        if cid in ann_ids:
            locs.append("annotation")
        if cid in lock_ids:
            locs.append("lock")

        db_step_count = meta["step_count"]
        conv_db_path = base / "conversations" / f"{cid}.db"
        actual_steps = db_step_count
        if conv_db_path.is_file():
            try:
                c_conn = sqlite3.connect(f"file:{conv_db_path}?mode=ro", uri=True)
                try:
                    row = c_conn.execute("SELECT count(*) FROM steps").fetchone()
                    if row and row[0] > actual_steps:
                        actual_steps = int(row[0])
                finally:
                    c_conn.close()
            except Exception:
                pass

        sessions.append({
            **meta,
            "annotation_title": ann_title,
            "effective_title": meta["title"] or ann_title or meta["preview"] or "（无标题）",
            "actual_step_count": actual_steps,
            "is_locked": locked,
            "storage_locations": locs,
            "total_bytes": _compute_session_bytes(base, cid),
            "resume_command": f"agy --conversation {cid}",
            **brain_info,
        })

    all_disk_ids = fts_ids | set(pb_map.keys()) | conv_ids | brain_ids | ann_ids | lock_ids
    orphan_ids = sorted(all_disk_ids - set(db_rows.keys()) - non_cli_ids)
    orphans: list[dict[str, Any]] = []
    for ocid in orphan_ids:
        locs = []
        if ocid in fts_ids:
            locs.append("fts")
        if ocid in pb_map:
            locs.append("jetbox_pb")
        if ocid in conv_ids:
            locs.append("conv_db")
        if ocid in brain_ids:
            locs.append("brain")
        if ocid in ann_ids:
            locs.append("annotation")
        if ocid in lock_ids:
            locs.append("lock")
        orphans.append({
            "conversation_id": ocid,
            "storage_locations": locs,
            "is_locked": is_session_locked(base, ocid),
            "total_bytes": _compute_session_bytes(base, ocid),
        })

    return {
        "data_dir": str(base),
        "session_count": len(sessions),
        "sessions": sessions,
        "orphan_count": len(orphans),
        "orphans": orphans,
    }


def search_sessions(
    query: str,
    workspace_filter: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """
    按关键词、工作区路径或模糊描述检索历史会话，返回三要素：
    1. 会话名称（及 ID）
    2. 定位依据
    3. 便捷进入方式（agy --conversation <ID>）
    """
    scan_data = scan_sessions(data_dir)
    base = Path(scan_data["data_dir"])
    db_path = base / "conversation_summaries.db"

    q_raw = (query or "").strip()
    ws_raw = (workspace_filter or "").strip().lower().replace("\\", "/")
    tokens = [t.lower() for t in re.split(r"\s+", q_raw) if t.strip()]

    # 读取 FTS 表辅助匹配全文内容
    fts_snippets: dict[str, list[str]] = {}
    if db_path.is_file() and tokens:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            for row in cur.execute(
                "SELECT conversation_id, title, workspace, user_body, agent_body FROM conversations_fts"
            ):
                cid, f_title, f_ws, u_body, a_body = row
                combined = "\n".join(filter(None, [f_title, f_ws, u_body, a_body]))
                combined_lower = combined.lower()
                for tok in tokens:
                    idx = combined_lower.find(tok)
                    if idx != -1:
                        s = max(0, idx - 30)
                        e = min(len(combined), idx + len(tok) + 60)
                        snippet = re.sub(r"\s+", " ", combined[s:e]).strip()
                        fts_snippets.setdefault(cid, [])
                        if len(fts_snippets[cid]) < 3 and snippet not in fts_snippets[cid]:
                            fts_snippets[cid].append(snippet)
        finally:
            conn.close()

    matches: list[dict[str, Any]] = []
    for sess in scan_data["sessions"]:
        cid = sess["conversation_id"]
        reasons: list[str] = []
        score = 0

        ws_joined = " ".join(sess["workspace_uris"]).lower().replace("\\", "/")
        if ws_raw:
            if ws_raw in ws_joined:
                score += 30
                reasons.append(f"工作区路径匹配：{sess['workspace_uris']}")
            else:
                continue

        haystack_fields = [
            ("会话标题", sess["effective_title"]),
            ("会话 ID", cid),
            ("工作区路径", " ".join(sess["workspace_uris"])),
            ("首轮提问", sess["first_user_prompt"]),
            ("末轮提问", sess["last_user_prompt"]),
            ("AI 结论摘要", sess["last_planner_response"]),
            ("产出物文件", ", ".join(sess["artifacts"] + sess["uploaded_media"])),
        ]

        if not tokens and not ws_raw:
            score = 1
            reasons.append("全量列出")
        else:
            for tok in tokens:
                for label, val in haystack_fields:
                    if val and tok in val.lower():
                        score += 20 if label == "会话标题" else 10
                        snippet = val if len(val) <= 100 else val[:100] + "..."
                        reason_str = f"{label}命中「{tok}」：{snippet}"
                        if reason_str not in reasons:
                            reasons.append(reason_str)
                if cid in fts_snippets:
                    for snip in fts_snippets[cid]:
                        if tok in snip.lower():
                            score += 8
                            reason_str = f"对话全文命中「{tok}」：{snip}"
                            if reason_str not in reasons:
                                reasons.append(reason_str)

        if score > 0:
            matches.append({
                "conversation_id": cid,
                "title": sess["effective_title"],
                "step_count": sess["actual_step_count"],
                "last_modified_time": sess["last_modified_time"],
                "is_locked": sess["is_locked"],
                "score": score,
                "evidence": reasons[:5],
                "resume_command": f"agy --conversation {cid}",
            })

    matches.sort(key=lambda x: (-x["score"], x["last_modified_time"]), reverse=True)
    return {
        "query": q_raw,
        "workspace_filter": workspace_filter or "",
        "match_count": len(matches),
        "matches": matches,
    }


# ============================================================================
# 5. 预览门禁与一次性确认令牌（Preview -> Confirmation Token -> Execute）
# ============================================================================

def _load_token_store(state_file: Path) -> dict[str, Any]:
    if not state_file.is_file():
        return {"tokens": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            return data
    except Exception:
        pass
    return {"tokens": {}}


def _save_token_store(state_file: Path, data: dict[str, Any]) -> None:
    tmp_path = state_file.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, state_file)


def _prune_expired_tokens(tokens: dict[str, Any], now: float) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for tok, item in tokens.items():
        expires_at = float(item.get("expires_at", 0))
        consumed_at = float(item.get("consumed_at", 0) or 0)
        if item.get("used"):
            if now - consumed_at <= 3600:
                keep[tok] = item
        elif now <= expires_at:
            keep[tok] = item
    return keep


def _canonical_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preview_changes(
    renames: list[dict[str, Any]],
    deletes: list[dict[str, Any]],
    uncertain: list[dict[str, Any]] | None = None,
    keeps: list[dict[str, Any]] | None = None,
    clean_orphans: bool = True,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """
    只读校验重命名与清理计划，生成《清理排查清单》与《改名前后对照表》，并签发一次性 confirmation_token。
    """
    scan = scan_sessions(data_dir)
    base = Path(scan["data_dir"])
    sess_by_id = {s["conversation_id"]: s for s in scan["sessions"]}
    orphan_by_id = {o["conversation_id"]: o for o in scan["orphans"]}

    uncertain = uncertain or []
    keeps = keeps or []

    # 校验重命名项
    normalized_renames: list[dict[str, Any]] = []
    seen_rename_ids: set[str] = set()
    for item in renames:
        cid = str(item.get("conversation_id", "")).strip()
        new_title = str(item.get("new_title", "")).strip()
        category = str(item.get("category", "项目类")).strip()
        reason = str(item.get("reason", "")).strip()
        if cid not in sess_by_id:
            raise ValueError(f"待重命名会话不存在：{cid}")
        if cid in seen_rename_ids:
            raise ValueError(f"重复的待重命名会话 ID：{cid}")
        seen_rename_ids.add(cid)

        title_errs = validate_new_title(new_title)
        if title_errs:
            raise ValueError(f"会话 {cid} 新标题「{new_title}」不符合规范：{'; '.join(title_errs)}")

        old_title = sess_by_id[cid]["effective_title"]
        normalized_renames.append({
            "conversation_id": cid,
            "old_title": old_title,
            "new_title": new_title,
            "char_length": len(new_title),
            "category": category,
            "reason": reason or "规范化中文标题",
            "is_locked": sess_by_id[cid]["is_locked"],
            "resume_command": f"agy --conversation {cid}",
        })

    # 校验待清理项
    normalized_deletes: list[dict[str, Any]] = []
    seen_delete_ids: set[str] = set()
    for item in deletes:
        cid = str(item.get("conversation_id", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"待清理会话 {cid} 必须提供具体的清理判断依据（reason）")
        if cid in seen_delete_ids:
            raise ValueError(f"重复的待清理会话 ID：{cid}")
        if cid in seen_rename_ids:
            raise ValueError(f"会话 {cid} 不能同时出现在重命名列表和清理列表中")
        if cid not in sess_by_id and cid not in orphan_by_id:
            raise ValueError(f"待清理会话不存在：{cid}")
        seen_delete_ids.add(cid)

        if cid in sess_by_id:
            s = sess_by_id[cid]
            normalized_deletes.append({
                "conversation_id": cid,
                "old_title": s["effective_title"],
                "step_count": s["actual_step_count"],
                "total_bytes": s["total_bytes"],
                "is_locked": s["is_locked"],
                "reason": reason,
            })
        else:
            o = orphan_by_id[cid]
            normalized_deletes.append({
                "conversation_id": cid,
                "old_title": "（孤立残留文件）",
                "step_count": 0,
                "total_bytes": o["total_bytes"],
                "is_locked": o["is_locked"],
                "reason": reason,
            })

    # 待确认项与保留项整理
    normalized_uncertain: list[dict[str, Any]] = []
    seen_uncertain_ids: set[str] = set()
    for item in uncertain:
        cid = str(item.get("conversation_id", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if cid in sess_by_id and cid not in seen_delete_ids:
            seen_uncertain_ids.add(cid)
            s = sess_by_id[cid]
            normalized_uncertain.append({
                "conversation_id": cid,
                "title": s["effective_title"],
                "step_count": s["actual_step_count"],
                "is_locked": s["is_locked"],
                "reason": reason or "需用户确认是否保留",
                "resume_command": f"agy --conversation {cid}",
            })

    keep_reason_map = {
        str(k.get("conversation_id", "")).strip(): str(k.get("reason", "")).strip()
        for k in keeps
    }
    normalized_keeps: list[dict[str, Any]] = []
    for s in scan["sessions"]:
        cid = s["conversation_id"]
        if cid in seen_delete_ids or cid in seen_uncertain_ids:
            continue
        default_reason = "包含有效工作记录或交付成果，予以保留"
        if s["is_locked"]:
            default_reason = "当前活跃/打开的终端会话，包含有效工作内容，予以保留"
        normalized_keeps.append({
            "conversation_id": cid,
            "title": s["effective_title"],
            "step_count": s["actual_step_count"],
            "is_locked": s["is_locked"],
            "reason": keep_reason_map.get(cid) or default_reason,
            "resume_command": f"agy --conversation {cid}",
        })

    orphan_targets = [
        o for o in scan["orphans"]
        if clean_orphans and o["conversation_id"] not in seen_delete_ids and not o["is_locked"]
    ]

    locked_payload = {
        "data_dir": str(base),
        "renames": [
            {"conversation_id": r["conversation_id"], "new_title": r["new_title"]}
            for r in normalized_renames
        ],
        "deletes": [
            {"conversation_id": d["conversation_id"], "reason": d["reason"]}
            for d in normalized_deletes
        ],
        "orphan_ids": [o["conversation_id"] for o in orphan_targets],
    }

    now = time.time()
    token = secrets.token_urlsafe(18)
    state_file = resolve_state_file(base)
    store = _load_token_store(state_file)
    store["tokens"] = _prune_expired_tokens(store.get("tokens", {}), now)
    store["tokens"][token] = {
        "token": token,
        "digest": _canonical_digest(locked_payload),
        "locked_payload": locked_payload,
        "created_at": now,
        "expires_at": now + TOKEN_TTL_SECONDS,
        "used": False,
        "consumed_at": None,
    }
    _save_token_store(state_file, store)

    card_text = format_preview_markdown(
        keeps=normalized_keeps,
        deletes=normalized_deletes,
        uncertain=normalized_uncertain,
        renames=normalized_renames,
        orphans=orphan_targets,
        token=token,
        expires_minutes=TOKEN_TTL_SECONDS // 60,
        backup_root=resolve_backup_root(),
    )

    return {
        "confirmation_token": token,
        "expires_in_seconds": TOKEN_TTL_SECONDS,
        "backup_root": str(resolve_backup_root()),
        "summary": {
            "total_sessions": scan["session_count"],
            "keep_count": len(normalized_keeps),
            "delete_count": len(normalized_deletes),
            "uncertain_count": len(normalized_uncertain),
            "rename_count": len(normalized_renames),
            "orphan_clean_count": len(orphan_targets),
        },
        "keeps": normalized_keeps,
        "deletes": normalized_deletes,
        "uncertain": normalized_uncertain,
        "renames": normalized_renames,
        "orphans": orphan_targets,
        "preview_markdown": card_text,
    }


def format_preview_markdown(
    keeps: list[dict[str, Any]],
    deletes: list[dict[str, Any]],
    uncertain: list[dict[str, Any]],
    renames: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    token: str,
    expires_minutes: int,
    backup_root: Path,
) -> str:
    lines: list[str] = [
        "### 【Agy 会话整理只读预览核对卡】",
        "",
        f"- **当前状态**：只读预览（未修改或删除任何文件）",
        f"- **计划概览**：保留 **{len(keeps)}** 条 | 拟清理 **{len(deletes)}** 条会话"
        + (f"（另清理 **{len(orphans)}** 个无主残留 `.lock` 文件）" if orphans else "")
        + f" | 待确认 **{len(uncertain)}** 条 | 拟重命名 **{len(renames)}** 条",
        f"- **执行前备份目录**：`{backup_root}`",
        f"- **一次性确认令牌**：`{token}`（{expires_minutes} 分钟内有效）",
        "",
        "---",
        "",
        "#### 一、《清理排查清单》",
        "",
        "| 处置决定 | 原会话标题 | 步数 | 终端状态 | 判断依据 | 会话 ID |",
        "|---|---|---:|---|---|---|",
    ]

    for d in deletes:
        lock_tag = "⚠️ 终端未关闭（执行前需先关）" if d["is_locked"] else "已关闭"
        lines.append(
            f"| 🗑️ **拟清理** | {d['old_title']} | {d['step_count']} | {lock_tag} | {d['reason']} | `{d['conversation_id']}` |"
        )
    for o in orphans:
        lines.append(
            f"| 🗑️ **拟清理残留** | （无主孤立 `.lock` 文件） | 0 | 已关闭 | 数据库与会话目录均无记录，仅剩 0 字节 `.lock` 残留 | `{o['conversation_id']}` |"
        )
    for u in uncertain:
        lock_tag = "🟢 终端开启中" if u["is_locked"] else "已关闭"
        lines.append(
            f"| ❓ **待你确认** | {u['title']} | {u['step_count']} | {lock_tag} | {u['reason']} | `{u['conversation_id']}` |"
        )
    for k in keeps:
        lock_tag = "🟢 终端开启中" if k["is_locked"] else "已关闭"
        lines.append(
            f"| ✅ **保留** | {k['title']} | {k['step_count']} | {lock_tag} | {k['reason']} | `{k['conversation_id']}` |"
        )

    lines.extend([
        "",
        "---",
        "",
        "#### 二、《改名前后对照表》",
        "",
        "| 分类 | 原会话名称 | 拟改新名称（简体中文 15~25 字内） | 字数 | 终端状态 | 便捷进入命令 |",
        "|---|---|---|---:|---|---|",
    ])

    if renames:
        for r in renames:
            lock_tag = "🟢 开启中（将启用延迟同步）" if r["is_locked"] else "已关闭"
            lines.append(
                f"| {r['category']} | {r['old_title']} | **{r['new_title']}** | {r['char_length']} | {lock_tag} | `{r['resume_command']}` |"
            )
    else:
        lines.append("| — | （本次无重命名项） | — | — | — | — |")

    lines.extend([
        "",
        "---",
        "",
        "【门禁指令】",
        "1. 请向用户完整展示上述《清理排查清单》与《改名前后对照表》，并立即结束本轮输出。",
        "2. 必须等待用户在下一条消息中明确回复确认（例如“同意，执行吧”）后，方可携带上述 `confirmation_token` 与用户确认原话调用 `execute_session_changes`。",
        "3. 严禁在同一轮对话中连续调用 `preview_session_changes` 与 `execute_session_changes`。",
    ])
    return "\n".join(lines)


# ============================================================================
# 6. 完整备份、一键还原脚本生成、原子执行与回读核验
# ============================================================================

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while chunk := fp.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _copy_and_record(src: Path, dst: Path, base_dir: Path, records: list[dict[str, Any]]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    src_sha = _sha256_file(src)
    dst_sha = _sha256_file(dst)
    if src_sha != dst_sha:
        raise RuntimeError(f"备份文件校验失败（SHA-256 不一致）：{src} -> {dst}")
    records.append({
        "rel_path": str(src.relative_to(base_dir)).replace("\\", "/"),
        "size_bytes": dst.stat().st_size,
        "sha256": dst_sha,
    })


def create_backup_snapshot(
    data_dir: Path,
    delete_ids: list[str],
    backup_root: Path | None = None,
) -> Path:
    """
    在 D:\\Agy Backups 下创建带时间戳的完整备份目录，包含：
    - conversation_summaries.db (及 -wal, -shm)
    - jetbox_summaries_proto.pb
    - annotations/*.pbtxt
    - 所有待删除会话的 conversations/<id>.db*, brain/<id>/**, presence/<id>.lock
    - manifest.json（逐文件字节数与 SHA-256）
    - restore.py（一键无损还原脚本）
    """
    b_root = resolve_backup_root(backup_root)
    b_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
    snap_dir = b_root / f"agy-session-manager-{stamp}"
    files_dir = snap_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []

    # 1. 核心索引文件
    for fname in (
        "conversation_summaries.db",
        "conversation_summaries.db-wal",
        "conversation_summaries.db-shm",
        "jetbox_summaries_proto.pb",
    ):
        src = data_dir / fname
        if src.is_file():
            _copy_and_record(src, files_dir / fname, data_dir, records)

    # 2. 全部 annotations
    ann_dir = data_dir / "annotations"
    if ann_dir.is_dir():
        for p in sorted(ann_dir.glob("*.pbtxt")):
            _copy_and_record(p, files_dir / "annotations" / p.name, data_dir, records)

    # 3. 待删除会话的完整文件与目录
    for cid in delete_ids:
        for suffix in (".db", ".db-wal", ".db-shm"):
            c_path = data_dir / "conversations" / f"{cid}{suffix}"
            if c_path.is_file():
                _copy_and_record(
                    c_path,
                    files_dir / "conversations" / c_path.name,
                    data_dir,
                    records,
                )
        lock_path = data_dir / "presence" / f"{cid}.lock"
        if lock_path.is_file():
            _copy_and_record(
                lock_path,
                files_dir / "presence" / lock_path.name,
                data_dir,
                records,
            )
        brain_path = data_dir / "brain" / cid
        if brain_path.is_dir():
            for root, _, fs in os.walk(brain_path):
                for f in fs:
                    sp = Path(root) / f
                    rel = sp.relative_to(data_dir)
                    _copy_and_record(sp, files_dir / rel, data_dir, records)

    existing_annotations = (
        sorted(p.name for p in ann_dir.glob("*.pbtxt")) if ann_dir.is_dir() else []
    )
    manifest = {
        "created_at": dt.datetime.now().isoformat(),
        "data_dir": str(data_dir),
        "backup_dir": str(snap_dir),
        "deleted_conversation_ids": delete_ids,
        "existing_annotations": existing_annotations,
        "file_count": len(records),
        "total_bytes": sum(r["size_bytes"] for r in records),
        "files": records,
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    restore_script = '''"""一键回滚还原脚本：将本备份目录下的所有会话文件与数据库原样恢复至 Agy 数据目录。"""
import hashlib
import json
from pathlib import Path
import shutil
import sys

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while chunk := fp.read(65536):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    snap_dir = Path(__file__).resolve().parent
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    target_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(manifest["data_dir"])
    files_dir = snap_dir / "files"

    restored = 0
    for item in manifest["files"]:
        rel = item["rel_path"]
        src = files_dir / rel
        dst = target_root / rel
        if sha256_file(src) != item["sha256"]:
            raise RuntimeError(f"备份源完整性校验失败: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1

    if "existing_annotations" in manifest:
        keep_anns = set(manifest["existing_annotations"])
        ann_dir = target_root / "annotations"
        if ann_dir.is_dir():
            for p in ann_dir.glob("*.pbtxt"):
                if p.name not in keep_anns:
                    p.unlink()

    print(f"已成功还原 {restored} 个文件至 {target_root}")

if __name__ == "__main__":
    main()
'''
    (snap_dir / "restore.py").write_text(restore_script, encoding="utf-8")
    return snap_dir


def apply_single_rename_on_disk(data_dir: Path, cid: str, new_title: str) -> None:
    """
    单独同步指定会话的四处标题存储（供常规改名及活跃会话延迟重刷调用）。
    """
    ann_dir = data_dir / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    ann_path = ann_dir / f"{cid}.pbtxt"
    ann_tmp = ann_dir / f"{cid}.pbtxt.tmp"
    ann_tmp.write_bytes(format_annotation_pbtxt(new_title))
    os.replace(ann_tmp, ann_path)

    db_path = data_dir / "conversation_summaries.db"
    updated_raw: bytes | None = None
    if db_path.is_file():
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            if row is not None:
                raw_bytes = row[0] or b""
                updated_raw = update_raw_summary_title(raw_bytes, new_title)
                cur.execute(
                    """
                    UPDATE conversation_summaries
                    SET title = ?, preview = ?, raw_summary = ?
                    WHERE conversation_id = ?
                    """,
                    (new_title, new_title, updated_raw, cid),
                )
            cur.execute(
                "UPDATE conversations_fts SET title = ? WHERE conversation_id = ?",
                (new_title, cid),
            )
            conn.commit()
        finally:
            conn.close()

    pb_path = data_dir / "jetbox_summaries_proto.pb"
    if pb_path.is_file() and updated_raw is not None:
        pb_bytes = pb_path.read_bytes()
        new_pb = update_jetbox_pb_bytes(pb_bytes, {cid: updated_raw}, set())
        pb_tmp = pb_path.with_suffix(".pb.tmp")
        pb_tmp.write_bytes(new_pb)
        os.replace(pb_tmp, pb_path)


def _spawn_delayed_rename_sync(data_dir: Path, locked_renames: list[tuple[str, str]]) -> None:
    """
    当存在运行中的活跃终端会话时，在当前轮次结束后（3.5s 与 7.0s）自动补刷并执行全量底稿对账，
    防止活跃进程在回合结束时用内存中的旧标题或旧条目覆写数据库与缓存。
    """
    payload = json.dumps(
        {"data_dir": str(data_dir), "renames": locked_renames},
        ensure_ascii=False,
    )
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--delayed-sync-json",
        payload,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception:
        pass


def execute_changes(
    confirmation_token: str,
    user_confirmation_quote: str,
    data_dir: Path | None = None,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    """
    校验一次性确认令牌与用户确认原话，执行完整备份 -> 多处联动修改与删除 -> 回读核验。
    """
    token = (confirmation_token or "").strip()
    quote = (user_confirmation_quote or "").strip()

    if not token:
        raise ValueError("缺少 confirmation_token，必须先调用 preview_session_changes 生成预览。")
    if not quote or quote.lower() in PLACEHOLDER_CONFIRMATIONS:
        raise PermissionError(
            "缺少有效的用户确认原话 (user_confirmation_quote)。必须等待用户在预览后明确回复确认，严禁自动占位通过。"
        )

    base = resolve_data_dir(data_dir)
    state_file = resolve_state_file(base)
    now = time.time()
    store = _load_token_store(state_file)
    tokens = store.get("tokens", {})
    record = tokens.get(token)

    if not record:
        raise PermissionError("确认令牌不存在或已过期清理，请重新生成预览。")
    if record.get("used"):
        raise PermissionError("该确认令牌已使用过，禁止重复执行；如需再次整理请重新生成预览。")
    if now > float(record.get("expires_at", 0)):
        raise PermissionError("该确认令牌已超时失效，请重新生成预览并确认。")

    locked_payload = record["locked_payload"]
    if _canonical_digest(locked_payload) != record.get("digest"):
        raise PermissionError("预览快照摘要校验失败，已阻断执行。")

    target_dir = Path(locked_payload["data_dir"])
    renames: list[dict[str, str]] = locked_payload.get("renames", [])
    deletes: list[dict[str, str]] = locked_payload.get("deletes", [])
    orphan_ids: list[str] = locked_payload.get("orphan_ids", [])

    # 1. 执行前再次检查待删会话是否被终端占用锁定
    still_locked_deletes: list[str] = []
    for d in deletes:
        cid = d["conversation_id"]
        if is_session_locked(target_dir, cid):
            still_locked_deletes.append(cid)
    for ocid in orphan_ids:
        if is_session_locked(target_dir, ocid):
            still_locked_deletes.append(ocid)

    if still_locked_deletes:
        raise RuntimeError(
            f"以下待清理会话仍在后台终端窗口中打开占用：{still_locked_deletes}。"
            "请先关闭对应的空闲终端窗口，再回复确认执行（您的确认令牌仍有效，未做任何改动）。"
        )

    # 2. 创建完整备份快照（含 SHA-256 校验与 restore.py）
    all_delete_ids = [d["conversation_id"] for d in deletes] + list(orphan_ids)
    missing_conv_db_before_delete = [
        cid for cid in all_delete_ids if not (target_dir / "conversations" / f"{cid}.db").is_file()
    ]
    freed_bytes_Logical = sum(_compute_session_bytes(target_dir, cid) for cid in all_delete_ids)
    backup_dir = create_backup_snapshot(target_dir, all_delete_ids, backup_root=backup_root)

    # 标记令牌已消费
    record["used"] = True
    record["consumed_at"] = now
    record["user_confirmation_quote"] = quote
    record["backup_dir"] = str(backup_dir)
    _save_token_store(state_file, store)

    # 3. 执行多处联动修改与删除
    db_path = target_dir / "conversation_summaries.db"
    pb_path = target_dir / "jetbox_summaries_proto.pb"
    ann_dir = target_dir / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)

    updated_raw_map: dict[str, bytes] = {}
    locked_renames_for_delayed_sync: list[tuple[str, str]] = []

    conn = sqlite3.connect(str(db_path), timeout=15.0)
    try:
        cur = conn.cursor()
        # 3.1 处理重命名
        for r in renames:
            cid = r["conversation_id"]
            new_title = r["new_title"]
            if is_session_locked(target_dir, cid):
                locked_renames_for_delayed_sync.append((cid, new_title))

            # 写入 annotations/<id>.pbtxt
            ann_path = ann_dir / f"{cid}.pbtxt"
            ann_tmp = ann_dir / f"{cid}.pbtxt.tmp"
            ann_tmp.write_bytes(format_annotation_pbtxt(new_title))
            os.replace(ann_tmp, ann_path)

            row = cur.execute(
                "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            raw_bytes = (row[0] if row and row[0] else b"")
            new_raw = update_raw_summary_title(raw_bytes, new_title)
            updated_raw_map[cid] = new_raw

            cur.execute(
                """
                UPDATE conversation_summaries
                SET title = ?, preview = ?, raw_summary = ?
                WHERE conversation_id = ?
                """,
                (new_title, new_title, new_raw, cid),
            )
            cur.execute(
                "UPDATE conversations_fts SET title = ? WHERE conversation_id = ?",
                (new_title, cid),
            )

        # 3.2 处理数据库中的删除
        for cid in all_delete_ids:
            cur.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
            cur.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,))
            cur.execute(
                "DELETE FROM conversation_parents WHERE conversation_id = ? OR parent_id = ?",
                (cid, cid),
            )

        conn.commit()
    finally:
        conn.close()

    # 3.3 更新 jetbox_summaries_proto.pb
    delete_set = set(all_delete_ids)
    if pb_path.is_file():
        pb_bytes = pb_path.read_bytes()
        new_pb_bytes = update_jetbox_pb_bytes(pb_bytes, updated_raw_map, delete_set)
        pb_tmp = pb_path.with_suffix(".pb.tmp")
        pb_tmp.write_bytes(new_pb_bytes)
        os.replace(pb_tmp, pb_path)

    # 3.4 删除磁盘上的会话文件与目录，并通知运行中 watcher 清理无 .db 幽灵条目
    for cid in all_delete_ids:
        for suffix in (".db", ".db-wal", ".db-shm"):
            p = target_dir / "conversations" / f"{cid}{suffix}"
            if p.is_file():
                p.unlink()
        ann_p = target_dir / "annotations" / f"{cid}.pbtxt"
        if ann_p.is_file():
            ann_p.unlink()
        lock_p = target_dir / "presence" / f"{cid}.lock"
        if lock_p.is_file():
            lock_p.unlink()
        brain_p = target_dir / "brain" / cid
        if brain_p.is_dir():
            shutil.rmtree(brain_p)

    if missing_conv_db_before_delete:
        _notify_watcher_deleted(target_dir, set(missing_conv_db_before_delete))

    _update_metadata_cache(
        target_dir,
        {r["conversation_id"]: r["new_title"] for r in renames},
        delete_set,
    )

    # 3.5 只要存在运行中的活跃终端会话，启动本轮收尾延迟同步（3.5s 与 7.0s）
    active_locked_ids: list[str] = []
    presence_dir = target_dir / "presence"
    if presence_dir.is_dir():
        for lp in sorted(presence_dir.glob("*.lock")):
            lcid = lp.stem
            if lcid not in delete_set and is_session_locked(target_dir, lcid):
                active_locked_ids.append(lcid)

    delayed_sync_sessions = list(
        dict.fromkeys([cid for cid, _ in locked_renames_for_delayed_sync] + active_locked_ids)
    )
    if delayed_sync_sessions and (renames or all_delete_ids):
        _spawn_delayed_rename_sync(target_dir, locked_renames_for_delayed_sync)
    else:
        delayed_sync_sessions = []

    # 4. 执行后回读核验（Read-back Verification）
    verification = verify_execution_state(
        data_dir=target_dir,
        renames=renames,
        deleted_ids=all_delete_ids,
    )

    return {
        "status": "verified" if verification["passed"] else "verification_warning",
        "backup_dir": str(backup_dir),
        "restore_command": f'python "{backup_dir / "restore.py"}"',
        "renamed_count": len(renames),
        "deleted_session_count": len(deletes),
        "deleted_orphan_count": len(orphan_ids),
        "freed_logical_bytes": freed_bytes_Logical,
        "delayed_sync_sessions": delayed_sync_sessions,
        "verification": verification,
    }


def verify_execution_state(
    data_dir: Path,
    renames: list[dict[str, str]],
    deleted_ids: list[str],
) -> dict[str, Any]:
    """
    回读核验：
    1. 所有重命名项在 conversation_summaries、raw_summary (Field 1 & 15)、conversations_fts、
       jetbox_summaries_proto.pb、annotations/<id>.pbtxt 中均已生效一致。
    2. 所有删除项在数据库、全文索引、pb 缓存及磁盘目录中均零残留。
    """
    issues: list[str] = []
    db_path = data_dir / "conversation_summaries.db"
    pb_path = data_dir / "jetbox_summaries_proto.pb"

    pb_map = parse_jetbox_pb(pb_path.read_bytes()) if pb_path.is_file() else {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        for r in renames:
            cid = r["conversation_id"]
            expected = r["new_title"]
            row = cur.execute(
                "SELECT title, preview, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
            if not row:
                issues.append(f"重命名会话 {cid} 在 conversation_summaries 中缺失")
                continue
            db_title, db_preview, raw_sum = row
            f1, f15 = extract_raw_summary_titles(raw_sum)
            if db_title != expected or db_preview != expected:
                issues.append(f"会话 {cid} 数据库明文标题不匹配: title={db_title!r}")
            if f1 != expected or f15 != expected:
                issues.append(f"会话 {cid} raw_summary Protobuf 标题不匹配: f1={f1!r}, f15={f15!r}")

            fts_rows = cur.execute(
                "SELECT DISTINCT title FROM conversations_fts WHERE conversation_id = ?",
                (cid,),
            ).fetchall()
            if fts_rows and any(fr[0] != expected for fr in fts_rows):
                issues.append(f"会话 {cid} FTS5 全文表标题未同步: {fts_rows}")

            ann_path = data_dir / "annotations" / f"{cid}.pbtxt"
            if not ann_path.is_file():
                issues.append(f"会话 {cid} 缺少 annotations/{cid}.pbtxt")
            else:
                ann_t = parse_annotation_pbtxt(ann_path.read_bytes())
                if ann_t != expected:
                    issues.append(f"会话 {cid} pbtxt 标题不匹配: {ann_t!r}")

            if pb_path.is_file():
                if cid not in pb_map:
                    issues.append(f"会话 {cid} 在 jetbox_summaries_proto.pb 中缺失")
                else:
                    pf1, pf15 = extract_raw_summary_titles(pb_map[cid])
                    if pf1 != expected or pf15 != expected:
                        issues.append(f"会话 {cid} 在 jetbox_summaries_proto.pb 中标题不匹配: {pf1!r}")

        for cid in deleted_ids:
            if cur.execute(
                "SELECT 1 FROM conversation_summaries WHERE conversation_id = ?", (cid,)
            ).fetchone():
                issues.append(f"已删会话 {cid} 仍残留在 conversation_summaries")
            if cur.execute(
                "SELECT 1 FROM conversations_fts WHERE conversation_id = ?", (cid,)
            ).fetchone():
                issues.append(f"已删会话 {cid} 仍残留在 conversations_fts")
            if cid in pb_map:
                issues.append(f"已删会话 {cid} 仍残留在 jetbox_summaries_proto.pb")
            for p in (
                data_dir / "conversations" / f"{cid}.db",
                data_dir / "annotations" / f"{cid}.pbtxt",
                data_dir / "presence" / f"{cid}.lock",
                data_dir / "brain" / cid,
            ):
                if p.exists():
                    issues.append(f"已删会话 {cid} 磁盘残留: {p}")

        remaining_count = cur.execute("SELECT count(*) FROM conversation_summaries").fetchone()[0]
    finally:
        conn.close()

    return {
        "passed": len(issues) == 0,
        "remaining_session_count": remaining_count,
        "issues": issues,
    }


def _run_delayed_sync_cli(payload_json: str) -> None:
    data = json.loads(payload_json)
    target_dir = Path(data["data_dir"])
    renames: list[list[str]] = data.get("renames") or []
    for delay in (3.5, 7.0):
        time.sleep(delay)
        for cid, new_title in renames:
            try:
                apply_single_rename_on_disk(target_dir, cid, new_title)
            except Exception:
                pass
        try:
            reconcile_disk_truth(target_dir)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Agy Session Manager CLI")
    parser.add_argument("--delayed-sync-json", type=str, help="内部延迟同步载荷")
    parser.add_argument("--scan", action="store_true", help="只读扫描所有会话")
    parser.add_argument("--search", type=str, help="按关键词搜索会话")
    args = parser.parse_args()

    if args.delayed_sync_json:
        _run_delayed_sync_cli(args.delayed_sync_json)
        return
    if args.scan:
        print(json.dumps(scan_sessions(), ensure_ascii=False, indent=2))
        return
    if args.search is not None:
        print(json.dumps(search_sessions(args.search), ensure_ascii=False, indent=2))
        return
    parser.print_help()


if __name__ == "__main__":
    main()
