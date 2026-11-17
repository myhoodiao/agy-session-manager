# agy-session-manager

> 面向 Windows 平台 **Google Antigravity CLI (`agy`)** 的本地会话（Thread）检索、规范化中文重命名与安全清理插件（`MCP Server + Skill + Rule`，零第三方 Python 依赖）。

---

## 1. 这是什么？（项目定位与核心能力）

使用 `agy` 一段时间后，本地历史会话（Thread）会持续堆积：有英文自动标题、有中断重开的重复会话、也有纯连通性测试的临时会话。而 `agy` CLI 本身未提供命令行批量重命名或删除指定历史会话的子命令。

本项目将 `agy` 的本地会话整理变成**“平时正常干活不管标题，需要整理或找会话时只说一句话，全程零手动操作”**的自然语言工作流：

1. **找得到（历史会话检索）**：
   - 按关键词、项目路径或模糊描述检索本地历史会话，联合匹配标题、工作区路径、`conversations_fts` 全文索引、`transcript.jsonl` 对话日志与 `brain/<id>` 产出物。
   - 固定输出三要素：**会话名称（及 ID）** + **定位依据** + **可直接复制进入的 `agy --conversation <ID>` 命令**。
2. **看得懂（规范化中文重命名）**：
   - 统一将纯英文或杂乱标题转换为 **15~25 个字以内的简体中文**（禁省略号、禁“新会话”等模糊词）。
   - **项目类会话**：固定采用 **`项目名称-简短说明`**（如 `Agy会话管家-需求对齐与基线建立`、`工作邮件插件-迁移至Agy与门禁验收`）。
   - **工具/运维类会话**：采用统一 4 字中文前缀 + 具体事项（`规则配置-xxx`、`插件与钩子-xxx`、`系统运维-xxx`、`日常工具-xxx`、`知识库-xxx`）。
3. **清得安全（防误杀与四重安全门禁）**：
   - **防误杀硬约束**：严禁仅凭对话轮次少（如 1~2 轮）就判为废会话；必须真实读取对话首尾摘要与 `brain/<id>` 产出物，仅清理 0 轮空白会话、纯冒烟测试会话、以及重复开启后中断的无产物废会话（及无主孤立 `.lock` 残留）。
   - **一次性确认令牌门禁（`Preview -> Token -> Execute`）**：修改或删除前强制先输出**《清理排查清单》**与**《改名前后对照表》**并停下等待用户确认；未经用户下一轮明确回复确认，底层拒绝执行任何写操作。
   - **执行前全量快照备份 + 一键回滚**：每次执行前自动将涉及的数据库、Protobuf 汇总文件、注释文件及待删会话目录完整备份至 `D:\Agy Backups\agy-session-manager-<时间戳>`（无 D 盘时自动回退至 `%USERPROFILE%\Agy Backups`），并生成 SHA-256 校验清单 `manifest.json` 与一键无损还原脚本 `restore.py`。
   - **四处存储同步与回读核验**：自动联动修改 `annotations/<id>.pbtxt`、`conversation_summaries.db`（含 `raw_summary` 二进制 Protobuf 字段与 `conversations_fts` 全文表）、`jetbox_summaries_proto.pb`，并在执行后自动回读核验四处一致性。

---

## 2. 仓库文件清单

```text
agy-session-manager/
├── README.md                                  # 项目说明、安装指南、使用方式与底层机制文档
├── install.ps1                                # Windows PowerShell 一键安装与自检脚本
├── plugin/
│   ├── plugin.json                            # Agy 全局插件声明模板
│   └── skills/
│       └── agy-session-manager/
│           └── SKILL.md                       # 会话检索、命名与清理两阶段工作流说明
├── scripts/
│   ├── session_manager.py                     # 核心会话管理引擎（纯 Python 标准库，零第三方依赖）
│   └── session_manager_mcp.py                 # 基于 stdio JSON-RPC 的 MCP 服务端（暴露 4 个工具）
└── tests/
    └── test_session_manager.py                # 离线沙盒单元测试套件（含 Protobuf 往返、令牌门禁与一键回滚测试）
```

---

## 3. 在另一台 Windows 设备上安装

### 前置条件

- Windows 10 / 11
- 已安装 **Google Antigravity CLI (`agy`)**
- 已安装 **Python 3.10+**（仅使用 Python 标准库 `sqlite3`、`hashlib`、`msvcrt`、`json` 等，无需 `pip install` 任何第三方包）

---

### 方式 A：直接让另一台电脑的 `agy` 自动安装（推荐）

如果目标 Windows 电脑已经装好了 `agy`，直接打开 `agy` 并把下面这段话复制发送给它即可：

```text
请帮我参考 GitHub 仓库 https://github.com/myhoodiao/agy-session-manager，在这台 Windows 电脑上完整安装并配置 agy-session-manager（Agy 会话检索与命名管家）全局插件。

具体要求：
1. 先将该仓库克隆或下载到本地目录（如果你有固定的项目目录可放在项目目录下，否则可克隆到临时目录），阅读 README.md 了解插件结构与安全门禁机制；
2. 直接运行仓库根目录下的 PowerShell 安装脚本：powershell -ExecutionPolicy Bypass -File .\install.ps1；
3. 确认 install.ps1 中的 5 个步骤全部通过（定位本机 Python 3.10+、创建默认备份目录、部署插件到 %USERPROFILE%\.gemini\config\plugins\agy-session-manager、通过全部 5 项离线沙盒单元测试、通过 MCP JSON-RPC 握手与 4 个工具列表自检）；
4. 安装完成后，只读调用一次 session_manager.py --scan，告诉我当前这台电脑上共有多少条 Agy 本地会话，先不要做任何修改或删除。
```

---

### 方式 B：PowerShell 一键脚本安装

在目标 Windows 电脑的 PowerShell 中执行：

```powershell
git clone https://github.com/myhoodiao/agy-session-manager.git
cd agy-session-manager
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` 会自动完成以下工作（支持重复运行，幂等安全）：
1. 自动探测本机可用的 `python.exe`（自动避开 WindowsApps 商店空壳占位符，校验版本 `>= 3.10`）；
2. 自动准备默认备份目录（存在 `D:\` 时默认使用 `D:\Agy Backups`，无 `D:\` 时自动使用 `%USERPROFILE%\Agy Backups`；也可通过 `-BackupDir "路径"` 自定义）；
3. 将插件配置、MCP 脚本、技能 `SKILL.md` 及门禁规则 `rules/AGENTS.md` 完整部署到 `%USERPROFILE%\.gemini\config\plugins\agy-session-manager`（即使克隆在临时目录，安装后删除临时目录也不影响插件独立运行）；
4. 自动生成适配当前电脑真实 Python 路径与备份目录的 `mcp_config.json`（UTF-8 无 BOM）；
5. 自动在临时沙盒目录中运行 `tests/test_session_manager.py` 全部单元测试，并执行 MCP JSON-RPC `initialize` 与 `tools/list` 握手自检。

安装完成后，**新开一个 `agy` 终端窗口**即可直接通过自然语言使用。

---

### 方式 C：手动分步安装

1. **创建全局插件目录**：
   ```powershell
   $PluginRoot = "$env:USERPROFILE\.gemini\config\plugins\agy-session-manager"
   mkdir "$PluginRoot\scripts", "$PluginRoot\rules", "$PluginRoot\skills\agy-session-manager" -Force
   ```
2. **复制核心文件**：
   - 将 `scripts\session_manager.py` 与 `scripts\session_manager_mcp.py` 复制到 `$PluginRoot\scripts\`
   - 将 `plugin\plugin.json` 复制到 `$PluginRoot\plugin.json`
   - 将 `plugin\skills\agy-session-manager\SKILL.md` 复制到 `$PluginRoot\skills\agy-session-manager\SKILL.md`
3. **编写 `$PluginRoot\mcp_config.json`**（将 `<PYTHON_EXE_PATH>` 替换为本机 `python.exe` 绝对路径，注意 JSON 反斜杠转义）：
   ```json
   {
     "mcpServers": {
       "session-manager": {
         "command": "C:\\Users\\你的用户名\\AppData\\Local\\Programs\\Python\\Python314\\python.exe",
         "args": [
           "-X",
           "utf8",
           "C:\\Users\\你的用户名\\.gemini\\config\\plugins\\agy-session-manager\\scripts\\session_manager_mcp.py"
         ],
         "env": {
           "AGY_SESSION_MANAGER_ROOT": "C:\\Users\\你的用户名\\.gemini\\config\\plugins\\agy-session-manager",
           "AGY_SESSION_BACKUP_DIR": "D:\\Agy Backups",
           "PYTHONIOENCODING": "utf-8"
         }
       }
     }
   }
   ```
4. **运行沙盒测试验证**：
   ```powershell
   python -B -X utf8 -m unittest discover -s .\tests -v
   ```

---

## 4. 日常怎么用？（零手动操作）

安装后新开 `agy` 窗口，在任意对话中直接用大白话吩咐即可：

### 场景 1：整理与重命名历史会话（两阶段门禁）

- **你说**：
  > *“按照我们之前的约定，帮我整理一下 thread，先不执行，先给我预览。”*
- **Agy 第一步（只读扫描 + 生成预览表 + 停下等确认）**：
  自动调用 `scan_agy_sessions` 和 `preview_session_changes`，向你展示完整的**《清理排查清单》**和**《改名前后对照表》**，生成 30 分钟有效的一次性 `confirmation_token`，然后**立刻停下等你回复**：
  ```text
  #### 一、《清理排查清单》
  | 处置决定 | 原会话标题 | 步数 | 终端状态 | 判断依据 | 会话 ID |
  | 🗑️ 拟清理 | 只回复：测试完成 | 2 | 已关闭 | 纯冒烟测试会话，无业务成果 | 36523093-... |
  | ✅ 保留 | Pi Agent 邮件插件迁移 Agy | 140 | 已关闭 | 完成 pi-work-mail 迁移至 Agy 插件 | 6a0448d8-... |

  #### 二、《改名前后对照表》
  | 分类 | 原会话名称 | 拟改新名称（简体中文 15~25 字内） | 字数 | 终端状态 | 便捷进入命令 |
  | 项目类 | Pi Agent 邮件插件迁移 Agy | 工作邮件插件-迁移至Agy与门禁验收 | 18 | 已关闭 | agy --conversation 6a0448d8-... |
  | 工具类 | 彻底卸载 Sefer 软件 | 系统运维-彻底卸载Sefer软件及残留清理 | 21 | 已关闭 | agy --conversation 3da0468c-... |
  ```
- **你第二步（确认或微调）**：
  > *“同意，执行吧”*（或者说 *“第 2 条名字改成 xxx，别的同意执行”*）
- **Agy 第三步（自动备份 + 四处改写 + 回读核验）**：
  自动携带 `confirmation_token` 和你的确认原话调用 `execute_session_changes`：先全量备份到 `D:\Agy Backups\agy-session-manager-<时间戳>`，再同步改写数据库与 Protobuf 文件、删除废会话，最后完成四处一致性回读核验并向你汇报备份位置与一键还原命令。

### 场景 2：检索寻找历史会话

- **你说**：
  > *“帮我找一下之前处理无线网络工程文档的那个会话。”*
- **Agy 回复**：
  自动调用 `search_agy_sessions`，给出匹配的**会话名称 + ID**、**命中依据**（目录/对话内容/产出物）以及直接可复制执行的 **`agy --conversation <ID>`** 命令。

### 场景 3：万一误删或想撤回，如何一键还原？

每次执行修改或清理前，都会在备份目录下自动生成一个独立可运行的 `restore.py`。只需在终端执行汇报中给出的还原命令：
```powershell
python "D:\Agy Backups\agy-session-manager-20261115_012245_354\restore.py"
```
即可在 1 秒内校验全量备份文件的 SHA-256 并原样恢复所有数据库、Protobuf 索引、注释文件与已删会话目录。

---

## 5. MCP 工具清单

| 工具名 | 读写属性 | 用途说明 |
|---|---|---|
| `scan_agy_sessions` | 纯只读 | 扫描本机全部 Agy 会话，返回标题、步数、工作区、文件锁状态（`is_locked`）、首尾对话摘要、`brain` 产出物列表与无主孤立 `.lock` 文件 |
| `search_agy_sessions` | 纯只读 | 按关键词或工作区路径检索历史会话，返回三要素（名称+ID、定位依据、`agy --conversation <ID>`） |
| `preview_session_changes` | 只读预检 | 校验新标题规范（简体中文、≤25字、无省略号、非模糊词）与终端锁状态，生成预览核对卡并签发一次性 `confirmation_token` |
| `execute_session_changes` | 备份+写入 | 校验 `confirmation_token` 与用户确认原话（拦截 `yes`/`confirm` 等自动占位词），完成完整备份、多处联动改删与回读核验 |

---

## 6. Windows 底层存储机制与关键避坑说明

本插件针对 `agy` 在 Windows 下的底层存储与对账（Reconciliation）机制做了专门适配，二次修改时请勿破坏以下逻辑：

1. **为什么重命名必须“四处联动”？**
   - `agy` 内部包含 `SummaryStore`（`conversation_summaries.db`）、备用汇总文件（`jetbox_summaries_proto.pb`）以及磁盘注释源（`annotations/<id>.pbtxt`），并在启动与后台运行 `reconcileSummaries`。
   - 如果只改 SQLite 明文列而不改 `annotations/<id>.pbtxt` 或 `raw_summary` 二进制 Protobuf（`Field 1` 顶层标题与 `Field 15` 嵌套 `Annotation.title`），`agy` 界面列表或下次启动对账时会发生标题回弹。
   - 本引擎实现了纯标准库的 Protobuf Wire Format 无损编解码器，在重编码 `Field 1` 与 `Field 15` 字符串长度前缀（Varint）时，其余所有字段（时间戳、步数、Workspace URI、UUID 等）保持 **100% 逐字节原样不变**。
2. **为什么删除会话不能只删 SQLite 行？**
   - 若仅从 `conversation_summaries.db` 删除记录而保留 `conversations/<id>.db`，`agy` 的后台 `reconcile` 扫描磁盘时会把该会话重新扫回列表。
   - 因此清理会话时，引擎在完成完整备份后，会同步移除 `conversation_summaries`、`conversations_fts`、`conversation_parents`、`jetbox_summaries_proto.pb` 中的条目，以及磁盘上的 `conversations/<id>.db*`、`brain/<id>/`、`annotations/<id>.pbtxt`、`presence/<id>.lock`。
3. **运行中终端的文件锁检测与保护（`msvcrt.locking`）**：
   - 正在运行的 `agy.exe` 进程会对当前会话的 `presence/<id>.lock` 持有 Windows 系统级字节锁。
   - 引擎通过 `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` 无损探测每个会话是否仍在某个终端窗口中打开：
     - **待清理会话若仍被锁占用**：预览表会标出 `⚠️ 终端未关闭（执行前需先关）`，执行阶段若发现未关会主动拦截并保持零改动，绝不破坏运行中的进程。
     - **当前正在对话的活跃会话若需重命名**：由于当前 `agy` 进程在本轮回复结束瞬间会将内存中的旧标题往数据库回写一次，引擎除当场更新外，会自动启动一个轻量后台子进程在 `3.5s` 和 `7.0s` 后（即本轮回复落盘结束后）自动补刷一次新标题，彻底解决当前窗口改名被覆写的问题。
