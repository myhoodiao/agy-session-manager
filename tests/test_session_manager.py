"""
Agy-Session-Manager 离线单元测试套件。
所有测试均在 tempfile 沙盒目录中执行，绝不触碰真实 Agy 会话目录。
"""

from __future__ import annotations

import json
import msvcrt
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import session_manager as sm  # noqa: E402
import session_manager_mcp as mcp  # noqa: E402


def _build_sample_raw_summary(cid: str, title: str, step_count: int = 12) -> bytes:
    title_b = title.encode("utf-8")
    ann_b = sm.encode_proto_fields([(1, 2, title_b)])
    fields: list[tuple[int, int, int | bytes]] = [
        (1, 2, title_b),
        (2, 0, step_count),
        (3, 2, sm.encode_proto_fields([(1, 0, 1794657164), (2, 0, 100)])),
        (4, 2, b"76e4cd44-fafc-4587-a134-4015af0f8124"),
        (5, 0, 1),
        (9, 2, sm.encode_proto_fields([(1, 2, b"file:///C:/Users/Outlaws")])),
        (15, 2, ann_b),
        (16, 0, 25),
        (17, 2, sm.encode_proto_fields([(6, 2, cid.encode("utf-8"))])),
        (22, 0, 4),
    ]
    return sm.encode_proto_fields(fields)


class SessionManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "antigravity-cli"
        self.backup_dir = self.root / "backups"
        self.state_dir = self.root / "state"

        for sub in ("annotations", "brain", "conversations", "presence"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

        os.environ["AGY_SESSION_DATA_DIR"] = str(self.data_dir)
        os.environ["AGY_SESSION_BACKUP_DIR"] = str(self.backup_dir)
        os.environ["AGY_SESSION_STATE_DIR"] = str(self.state_dir)

        self.cid_keep = "11111111-1111-4111-8111-111111111111"
        self.cid_delete = "22222222-2222-4222-8222-222222222222"
        self.cid_orphan = "99999999-9999-4999-8999-999999999999"

        self._init_sandbox_db()

    def tearDown(self) -> None:
        for k in ("AGY_SESSION_DATA_DIR", "AGY_SESSION_BACKUP_DIR", "AGY_SESSION_STATE_DIR"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _init_sandbox_db(self) -> None:
        db_path = self.data_dir / "conversation_summaries.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id text PRIMARY KEY,
                title text NOT NULL DEFAULT "",
                preview text NOT NULL DEFAULT "",
                step_count integer NOT NULL DEFAULT 0,
                last_modified_time datetime NOT NULL,
                workspace_uris text NOT NULL,
                parent_conversation_id text NOT NULL DEFAULT "",
                raw_summary blob
            )
            """
        )
        cur.execute(
            """
            CREATE VIRTUAL TABLE conversations_fts USING fts5(
                conversation_id UNINDEXED,
                title,
                workspace,
                user_body,
                agent_body
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE conversation_parents (
                conversation_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )

        raw_keep = _build_sample_raw_summary(self.cid_keep, "English Old Title", 20)
        raw_del = _build_sample_raw_summary(self.cid_delete, "只回复：测试完成", 2)

        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.cid_keep,
                "English Old Title",
                "English Old Title",
                20,
                "2026-11-14 15:00:00+00:00",
                '["file:///D:/00_Project/DemoProj"]',
                "",
                raw_keep,
            ),
        )
        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.cid_delete,
                "",
                "只回复：测试完成",
                2,
                "2026-11-14 14:00:00+00:00",
                '["file:///C:/Temp/smoke"]',
                "",
                raw_del,
            ),
        )

        cur.execute(
            "INSERT INTO conversations_fts VALUES (?, ?, ?, ?, ?)",
            (self.cid_keep, "English Old Title", "D:/00_Project/DemoProj", "帮我梳理架构文档", "已完成架构文档整理"),
        )
        cur.execute(
            "INSERT INTO conversations_fts VALUES (?, ?, ?, ?, ?)",
            (self.cid_delete, "", "C:/Temp/smoke", "只回复：测试完成", "测试完成"),
        )
        conn.commit()
        conn.close()

        # jetbox_summaries_proto.pb
        pb_bytes = sm.encode_proto_fields([
            (1, 2, sm.encode_proto_fields([(1, 2, self.cid_keep.encode()), (2, 2, raw_keep)])),
            (1, 2, sm.encode_proto_fields([(1, 2, self.cid_delete.encode()), (2, 2, raw_del)])),
        ])
        (self.data_dir / "jetbox_summaries_proto.pb").write_bytes(pb_bytes)

        # annotations
        (self.data_dir / "annotations" / f"{self.cid_keep}.pbtxt").write_bytes(
            sm.format_annotation_pbtxt("English Old Title")
        )

        # conversations/<id>.db + brain + presence
        for cid, steps_cnt in ((self.cid_keep, 20), (self.cid_delete, 2)):
            c_db = self.data_dir / "conversations" / f"{cid}.db"
            c_conn = sqlite3.connect(str(c_db))
            c_conn.execute("CREATE TABLE steps (idx integer PRIMARY KEY)")
            for idx in range(steps_cnt):
                c_conn.execute("INSERT INTO steps VALUES (?)", (idx,))
            c_conn.commit()
            c_conn.close()

            (self.data_dir / "presence" / f"{cid}.lock").write_bytes(b"")
            log_dir = self.data_dir / "brain" / cid / ".system_generated" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "transcript.jsonl").write_text(
                json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>测试输入</USER_REQUEST>"})
                + "\n",
                encoding="utf-8",
            )

        # 孤立残留 .lock
        (self.data_dir / "presence" / f"{self.cid_orphan}.lock").write_bytes(b"")

    def test_protobuf_roundtrip_and_title_update(self) -> None:
        orig = _build_sample_raw_summary(self.cid_keep, "Old Title", 33)
        self.assertEqual(sm.encode_proto_fields(sm.parse_proto_fields(orig)), orig)

        updated = sm.update_raw_summary_title(orig, "DemoProj-架构梳理与基线对齐")
        f1, f15 = sm.extract_raw_summary_titles(updated)
        self.assertEqual(f1, "DemoProj-架构梳理与基线对齐")
        self.assertEqual(f15, "DemoProj-架构梳理与基线对齐")

        # 其余非标题字段应完整保留
        orig_other = [(f, w, v) for f, w, v in sm.parse_proto_fields(orig) if f not in (1, 15)]
        upd_other = [(f, w, v) for f, w, v in sm.parse_proto_fields(updated) if f not in (1, 15)]
        self.assertEqual(orig_other, upd_other)

    def test_title_validation_rules(self) -> None:
        self.assertEqual(sm.validate_new_title("DemoProj-架构梳理与基线对齐"), [])
        self.assertTrue(sm.validate_new_title("Pure English Title"))
        self.assertTrue(sm.validate_new_title("新会话"))
        self.assertTrue(sm.validate_new_title("带有省略号的标题..."))
        self.assertTrue(sm.validate_new_title("这是一个超过二十五个字上限的超长测试会话标题用来验证拦截规则是否生效"))

    def test_scan_and_search_sessions(self) -> None:
        scan = sm.scan_sessions()
        self.assertEqual(scan["session_count"], 2)
        self.assertEqual(scan["orphan_count"], 1)
        self.assertEqual(scan["orphans"][0]["conversation_id"], self.cid_orphan)

        search_res = sm.search_sessions("架构文档")
        self.assertEqual(search_res["match_count"], 1)
        self.assertEqual(search_res["matches"][0]["conversation_id"], self.cid_keep)
        self.assertIn(f"agy --conversation {self.cid_keep}", search_res["matches"][0]["resume_command"])

    def test_preview_and_confirmation_gate_and_rollback(self) -> None:
        preview = sm.preview_changes(
            renames=[
                {
                    "conversation_id": self.cid_keep,
                    "new_title": "DemoProj-架构文档梳理与基线建立",
                    "category": "项目类",
                }
            ],
            deletes=[
                {
                    "conversation_id": self.cid_delete,
                    "reason": "纯冒烟测试会话（只回复测试完成），无业务成果",
                }
            ],
            clean_orphans=True,
        )
        token = preview["confirmation_token"]
        self.assertIn("DemoProj-架构文档梳理与基线建立", preview["preview_markdown"])

        # 拦截占位确认词
        with self.assertRaises(PermissionError):
            sm.execute_changes(token, "yes")

        # 拦截锁占用的待删会话
        lock_file = self.data_dir / "presence" / f"{self.cid_delete}.lock"
        fd = os.open(str(lock_file), os.O_RDWR)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        try:
            with self.assertRaises(RuntimeError):
                sm.execute_changes(token, "同意，按方案执行")
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

        # 解锁后正常执行
        exec_res = sm.execute_changes(token, "同意，按方案执行")
        self.assertEqual(exec_res["status"], "verified")
        self.assertTrue(exec_res["verification"]["passed"])
        self.assertEqual(exec_res["verification"]["remaining_session_count"], 1)

        # 禁止重复消费同一个 token
        with self.assertRaises(PermissionError):
            sm.execute_changes(token, "同意，按方案执行")

        # 测试一键回滚脚本 restore.py
        backup_dir = Path(exec_res["backup_dir"])
        restore_py = backup_dir / "restore.py"
        self.assertTrue(restore_py.is_file())
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(restore_py)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("已成功还原", proc.stdout)

        # 验证回滚后已删除的会话与旧标题均完整恢复
        after_restore = sm.scan_sessions()
        self.assertEqual(after_restore["session_count"], 2)
        self.assertEqual(after_restore["orphan_count"], 1)
        titles_by_id = {s["conversation_id"]: s["effective_title"] for s in after_restore["sessions"]}
        self.assertEqual(titles_by_id[self.cid_keep], "English Old Title")

    def test_reconcile_disk_truth_heals_overwritten_pb_and_ghosts(self) -> None:
        new_title = "DemoProj-架构文档梳理与基线建立"
        preview = sm.preview_changes(
            renames=[
                {
                    "conversation_id": self.cid_keep,
                    "new_title": new_title,
                    "category": "项目类",
                }
            ],
            deletes=[
                {
                    "conversation_id": self.cid_delete,
                    "reason": "纯冒烟测试会话，无业务成果",
                }
            ],
            clean_orphans=True,
        )
        exec_res = sm.execute_changes(preview["confirmation_token"], "同意，执行整理")
        self.assertTrue(exec_res["verification"]["passed"])
        self.assertFalse((self.data_dir / "conversations" / f"{self.cid_delete}.db").exists())

        # 模拟运行中旧 agy.exe 覆写回滚：把 cid_keep 改回英文旧名，并把已删的 cid_delete 塞回 db 和 pb
        old_raw_keep = _build_sample_raw_summary(self.cid_keep, "English Old Title", 20)
        old_raw_del = _build_sample_raw_summary(self.cid_delete, "只回复：测试完成", 2)

        db_path = self.data_dir / "conversation_summaries.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                UPDATE conversation_summaries
                SET title = ?, preview = ?, raw_summary = ?
                WHERE conversation_id = ?
                """,
                ("English Old Title", "English Old Title", old_raw_keep, self.cid_keep),
            )
            conn.execute(
                "UPDATE conversations_fts SET title = ? WHERE conversation_id = ?",
                ("English Old Title", self.cid_keep),
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.cid_delete,
                    "",
                    "只回复：测试完成",
                    2,
                    "2026-11-14 14:00:00+00:00",
                    '["file:///C:/Temp/smoke"]',
                    "",
                    old_raw_del,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        pb_path = self.data_dir / "jetbox_summaries_proto.pb"
        pb_bytes = sm.encode_proto_fields([
            (1, 2, sm.encode_proto_fields([(1, 2, self.cid_keep.encode()), (2, 2, old_raw_keep)])),
            (1, 2, sm.encode_proto_fields([(1, 2, self.cid_delete.encode()), (2, 2, old_raw_del)])),
        ])
        pb_path.write_bytes(pb_bytes)

        # 调用 scan_sessions 应自动触发底稿自愈：剔除幽灵 cid_delete，并把 cid_keep 全部恢复为新中文名
        healed = sm.scan_sessions()
        self.assertEqual(healed["session_count"], 1)
        self.assertEqual(healed["sessions"][0]["conversation_id"], self.cid_keep)
        self.assertEqual(healed["sessions"][0]["effective_title"], new_title)

        conn_ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn_ro.execute(
                "SELECT title, preview, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
                (self.cid_keep,),
            ).fetchone()
            self.assertEqual(row[0], new_title)
            self.assertEqual(row[1], new_title)
            self.assertEqual(sm.extract_raw_summary_titles(row[2]), (new_title, new_title))

            fts_row = conn_ro.execute(
                "SELECT title FROM conversations_fts WHERE conversation_id = ?",
                (self.cid_keep,),
            ).fetchone()
            self.assertEqual(fts_row[0], new_title)

            ghost_row = conn_ro.execute(
                "SELECT 1 FROM conversation_summaries WHERE conversation_id = ?",
                (self.cid_delete,),
            ).fetchone()
            self.assertIsNone(ghost_row)
        finally:
            conn_ro.close()

        pb_map = sm.parse_jetbox_pb(pb_path.read_bytes())
        self.assertNotIn(self.cid_delete, pb_map)
        self.assertIn(self.cid_keep, pb_map)
        self.assertEqual(sm.extract_raw_summary_titles(pb_map[self.cid_keep]), (new_title, new_title))

    def test_mcp_jsonrpc_handlers(self) -> None:
        os.environ["AGY_SESSION_MANAGER_ROOT"] = str(PROJECT_ROOT)
        init_resp = mcp.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "agy-session-manager-mcp")

        list_resp = mcp.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        self.assertEqual(
            tool_names,
            ["scan_agy_sessions", "search_agy_sessions", "preview_session_changes", "execute_session_changes"],
        )


if __name__ == "__main__":
    unittest.main()
