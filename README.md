# Mac 任务看板 · mac-task-workbench

把一台 Mac 上的**定时任务、系统状态、运行日志**，收进一个只读的本地网页。

打开 `http://127.0.0.1:8765` 就能看到：现在机器忙不忙、哪些任务配置了会跑、上次跑成功还是失败、失败卡在哪一步。

> **只读、只在本机、不联网。** 不启停任何任务，不改任何业务脚本，不读任何凭证。
> 数据全部来自你自己的电脑，仓库里不带任何数据。

**2026-11-12 更新**：详情抽屉新增「本机相关日志」。`logs_allow.json` 现在可以授权**整个日志目录**，
看板会按任务名与可执行文件路径自动匹配相关文件（例：程序 `dvc-core-exe` → `core_0.log`），
同目录其余日志收进折叠区。匹配规则完全通用，不内置任何厂商名；一个都匹配不上时如实显示「未找到」。
日志正文新增本机用户名打码，环形日志文件的 NUL 填充也会被清掉。详见下方「配置」一节。

**2026-11-09 更新**：看板新增亮色 / 暗色 / 跟随系统三档主题（右上角切换，选择记在本机浏览器）；
详情抽屉新增「存活变动」时间线，用来观察 `KeepAlive` 常驻守护进程中途有没有掉过 —— 这类进程
从不退出，`launchd` 的 `runs` 和 `last exit code` 永远没有信息量，所以改为在每轮采集时对比 pid，
只在变化时记一条事件（基线 / 启动 / 停止 / 重启），保留 30 天、最多 2000 条。

---

## 它解决什么问题

Mac 上的定时任务散落在三处：`launchd`（系统 + 各种软件）、`cron`、以及各类自动化平台。想知道「昨晚跑没跑」「为什么失败」通常要翻几个地方，还要会看 `launchctl` 输出。

这个项目做三件事：

1. **汇总** —— 把 `launchd` 的任务、WorkBuddy 的自动化任务（可选）扫到一张表里
2. **判定** —— 区分「配置为启用」「进程在跑」「上次执行成功/失败/漏跑/卡死」，不混为一谈
3. **可查** —— 点任务看历史记录和详细日志，日志走白名单 + 脱敏 + 截断

---

## 数据从哪来（全部只读）

| 数据 | 来源 | 是否必需 |
|---|---|---|
| 本机任务清单 | `~/Library/LaunchAgents`、`/Library/LaunchAgents`、`/Library/LaunchDaemons` 下的 plist + `launchctl print` | 必需 |
| 系统状态 | `mach host_statistics`（CPU）、`vm_stat`（内存）、`statvfs`（磁盘）、`route` + TCP 探测（网络） | 必需 |
| 脚本运行日志 | 本项目 `data/runs/*.jsonl`，由可选包装器 `run_task.py` 写入 | 可选 |
| 本机已有日志文件 | `logs_allow.json` 显式授权的文件 / 目录，按任务关键词匹配 | **可选**，不配就读不到 |
| 平台任务与记录 | `~/.workbuddy/workbuddy.db`（只读、只取 4 个非敏感字段） | **可选**，没装 WorkBuddy 也能用 |

没装 WorkBuddy 的机器上，「WorkBuddy 任务」一栏显示为空，其余功能正常——不会报错，也不会编数据。

---

## 跑起来（30 秒）

前置：macOS，自带 `python3`（3.9+ 即可，不需要 `pip install`）。

```bash
git clone https://github.com/MSsxj/mac-task-workbench.git
cd mac-task-workbench
python3 serve.py
```

浏览器会自动打开 `http://127.0.0.1:8765`。首次启动会自动扫一遍本机任务。

> **刚打开是空的、或者任务很少，这是正常的。** 它不预置任何示例数据，页面上每一条都来自你当前这台机器。
> 想让它有更多内容：跑一遍 `python3 scan_tasks.py` 重新扫描，或者在 `采集器` 安装后等 5 分钟自动采一轮。

只想扫一次、不做常驻服务：

```bash
python3 scan_tasks.py     # 生成 data/scan-data.json 与 .js
open dashboard.html       # 直接用 file:// 打开也能看（无刷新按钮）
```

---

## 可选：开机自启 + 每 5 分钟自动采集

双击运行，各自独立：

| 文件 | 作用 |
|---|---|
| `安装自启.command` | 开机自动起服务（launchd KeepAlive，进程挂了会自己回来） |
| `安装采集器.command` | 每 5 分钟扫一轮，写心跳；网页据此显示「数据是否新鲜」 |
| `卸载自启.command` / `卸载采集器.command` | 对应卸载 |

> 如果在 Finder 里双击提示「无法打开，因为来自身份不明的开发者」：右键 → 打开 → 再点一次「打开」。

装完看板顶部会显示采集时间。**不装也能用**，只是数据不会自动更新。

---

## 目录结构

```
mac-task-workbench/
├── serve.py              # 本地 HTTP 服务（只绑 127.0.0.1），静态白名单 + 4 个 API
├── dashboard.html        # 单页看板（原生 HTML/CSS/JS，无构建步骤）
├── scan_tasks.py         # 扫 plist + launchctl，产出任务清单
├── system_stats.py       # CPU / 内存 / 磁盘 / 网络，标准库 + 系统命令
├── build_runs.py         # 汇总运行记录，判 MISSED / STALE
├── collect.py            # 常驻采集器：扫一轮 + 写心跳（launchd 每 5 分钟调）
├── state_history.py      # 常驻任务的存活变动观测（只在 pid 变化时记事件）
├── logview.py            # 日志白名单读取 + 脱敏 + 截断
├── run_task.py           # 可选：包装器，给你的脚本套一层自动记日志
├── runlog.py             # 运行记录写入（追加式 jsonl）
├── atomic.py             # 原子写（临时文件 + os.replace，防半截 JSON）
├── wb_automations.py     # 可选：只读 WorkBuddy 本地库的任务列表
├── wb_runlog.py          # 可选：只读 WorkBuddy 的运行记录
├── 验收检查.py            # 10 项只读自检，输出通过/不通过
└── *.command / *.sh      # 双击即用的安装、启动、卸载入口
```

运行时生成（已 gitignore，每台机器各自的）：

```
data/
├── scan-data.json/.js    # 本机任务清单
├── runs.js               # 运行记录汇总
├── runs/*.jsonl          # 包装器写入的日志
├── state.json/.js        # 采集器心跳
├── state-snapshot.json   # 上一轮常驻任务的 pid / state 快照
├── state-history.jsonl   # 存活变动事件（追加写）
└── automations.js        # WorkBuddy 任务快照（降级用）
```

---

## 配置（都可选）

| 文件 | 作用 | 不配的后果 |
|---|---|---|
| `logs_allow.example.json` | 复制为 `logs_allow.json`，列出你允许网页读取的日志路径 | 只能读本项目自己的日志 |
| `task_profiles.example.json` | 复制为 `task_profiles.json`，定义怎么从脚本输出里识别步骤和行数 | 只按 `[1/3]` 这类通用标记识别步骤 |

### 把本机已有的日志接进来

`logs_allow.json` 支持两种写法，`files` 放单个文件，`dirs` 放整个目录：

```json
{
  "files": ["~/Library/Logs/MyApp/run.log"],
  "dirs":  ["~/Library/Logs/MyApp", "~/Library/Application Support/SomeTool/log"]
}
```

- `dirs` 递归扫描最多 3 层，**只放行 `.log` / `.txt` / `.jsonl`**，且必须在授权目录内（`realpath` 校验，符号链接跳不出去）。
- 看板不会把目录里所有文件都塞给每个任务。它从任务的 `label`、可执行文件路径、归属、`StandardOutPath` 提取关键词，路径里命中关键词的文件才进「本机相关日志」；同目录其余文件放在「同目录其他日志」折叠区，可点开但会标注「未必属于本任务」。
- 程序自己不写日志文件的任务（比如只配了 `KeepAlive` 的常驻守护进程），会显示「未找到与该任务相关的日志文件」。**看板只读取，不会替任务生成日志**；想让它有日志，需要任务自己配置输出路径，那属于改动任务，不在本项目的只读边界内。
- 前端永远只传 `task_id + source_id`，真实路径由服务端解析；伪造或跨任务的 `source_id` 会被拒绝。
- 删掉某个目录即可一键撤回授权。

---

## 给你的脚本接上运行日志（可选）

如果你的定时任务是「跑一个 Python / shell 脚本」，可以用包装器包一层，就能在页面上看到每次运行的步骤、行数、耗时、错误摘要——**不用改你的脚本一行**。

```bash
python3 run_task.py \
  --task-id my-job-001 \
  --name "我的日报脚本" \
  --workdir ~/myproject \
  --profile example \
  -- python3 daily_report.py
```

参数说明：

- `--task-id`：自己起的唯一 id，页面用它关联记录
- `--profile`：对应 `task_profiles.json` 里的规则名，用于识别步骤/行数
- `--dry-run`：声明这次不写外部系统（只影响记录，不改变子进程行为）
- `--` 之后是真实命令，退出码原样透传

然后在你的调度器里（`crontab`、launchd、或自动化平台的 prompt）把原来的命令换成上面这一整条即可。

---

## 安全边界（这些是写死在代码里的）

1. **只绑 `127.0.0.1`** —— 局域网内其他设备也访问不到
2. **静态文件白名单** —— 只放行看板页和 4 个数据 JS；源码、`data/runs/*.jsonl` 原始日志、`.git`、配置一律 404
3. **前端不能传路径** —— 日志接口只接受 `task_id + source_id`，真实路径由服务端白名单解析
4. **不读敏感字段** —— WorkBuddy 库只取 `id/name/status/rrule`，绝不读 `prompt`、会话正文、`runs_json`
5. **日志脱敏** —— 密码 / token / Bearer / 连接串 / IP / 邮箱 / 长数字串 / 本机登录名一并打码；单文件最多读尾部 96 KB、返回 300 行，单行超 400 字截断
6. **不启停任务** —— 没有增删改任何任务的接口
7. **不外发** —— 没有出网请求，不上传任何数据

---

## 自检

服务起来后：

```bash
python3 验收检查.py
```

会跑 10 项检查（数值与系统工具一致性、任务数量、日志来源、越权访问、敏感信息、原有功能等），逐项给通过/不通过。全部只读，不触发任何业务任务。

想看「刷新会不会误写业务目录」这一项，先设环境变量：

```bash
WORKBENCH_WATCH_DIR=~/myproject python3 验收检查.py
```

---

## 常见问题

**页面顶部提示「采集器 N 分钟没有更新」**
没装采集器，或采集器没跑起来。双击 `安装采集器.command`；或手动跑一次 `./采集器.sh` 看报错。

**「WorkBuddy 任务」一栏是空的**
这台机器没装 WorkBuddy，或本地库路径不同。该部分是可选的，不影响其他功能。

**端口 8765 被占用**
`python3 serve.py --port 8766`，地址相应改成 `http://127.0.0.1:8766`。

**点开任务，日志来源只有两条「不可用」**
先确认 `logs_allow.json` 有没有授权对应的日志目录；没配就只会读本项目自己的日志。配好仍显示「未找到与该任务相关的日志文件」，说明该程序在本机确实没有留下文件日志（常驻守护进程很常见）——本项目只读，不会替它生成。这类任务可以看「存活变动」时间线，那是每轮采集对比 pid 得到的真实启停记录。

**任务列表里有一堆认不出的 label**
正常，那是系统和其他软件的任务。把你能认出的加进 `scan_tasks.py` 的 `KNOWN_NOTES`，页面会多一行说明。

**CPU 数字跟「活动监视器」对不上**
本项目用内核 tick 增量，`活动监视器` 用自己的加权口径，两者有采样差异属正常。磁盘同理：本项目按 `statvfs`（Finder 口径），和 `df /` 不同是 APFS 的正常现象。

---

## 给 AI 的复刻指令

在另一台 Mac 上，把下面这段连同仓库地址发给 AI 即可：

> 帮我在这台 Mac 上部署这个项目：`<仓库地址>`。
> 步骤：clone 到 `~/mac-task-workbench` → 确认 `python3 --version` ≥ 3.9 → 运行 `python3 serve.py` 验证 `http://127.0.0.1:8765` 返回 200 → 运行 `python3 验收检查.py` 把 10 项结果贴给我 → 问我要不要装开机自启（`安装自启.command`）和采集器（`安装采集器.command`）。
> 注意：这是只读工具，不要让它启停任何任务、不要改我的业务脚本、不要读任何密码或 token。装完告诉我三个数字：本机任务数、运行记录条数、采集器是否在更新。

---

## License

MIT
