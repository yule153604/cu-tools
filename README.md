# cu

Python 异步脚本，仅供编程学习与技术交流。示例涵盖 `httpx` 并发请求、Cookie 会话管理、多账号调度，以及常见加解密与签名流程的实现方式。

> 请勿用于商业用途或任何违反服务条款的场景；使用风险由使用者自行承担。

## 项目结构

```text
cu.py                      # 账号会话、登录、调度、多账号入口
shangdu_claim.py            # 商都场次领取专用入口（仅领签到节点券）
cu_tasks/
├── common.py              # 共享配置、响应解析、日志脱敏、Cookie 工具
├── registry.py            # 唯一任务列表、顺序、选择规则与云盘依赖
├── welfare.py             # 福利商城九宫格
├── sign.py / market.py    # 签到 / 权益超市
├── xj.py / security.py    # 新疆活动 / 安全管家
├── farm.py / shangdu.py   # 通通农场 / 商都福利
├── uphone.py / woread.py  # 云手机 / 沃阅读
├── cloud.py               # 云盘登录、签名与上传共用能力
└── campus.py / battle.py  # 校园季 / 上传大比拼
tests/                     # 离线协议与调度回归测试
pyproject.toml             # Ruff 格式与静态检查规则
requirements.txt           # 运行依赖
```

## 任务模块

登录后按顺序执行（单任务失败不影响其余任务）：

| 标签 | 模块 | 备注 |
|------|------|------|
| 权益超市 | `market_task` | |
| 签到区 | `sign_task` | |
| 福利商城九宫格 | `welfare_task` | 抓包活动，默认每轮最多 3 次 |
| 新疆联通 | `xj_task` | 按归属地跳过 |
| 安全管家 | `sec_task` | |
| 通通乡村 | `farm_task` | |
| 商都福利 | `shangdu_task` | 按归属地跳过 |
| 云手机积分 | `uphone_task` | 见下方说明 |
| 沃阅读积分 | `woread_task` | 每账号自动换票 |
| 校园季（已暂停） | `campus_task` | 暂不运行，保留实现 |
| 上传大比拼（已暂停） | `cloud_battle_task` | 暂不运行，保留实现 |

2026-10-31 起暂停全部云盘活动（校园季、上传大比拼），默认运行不再进行云盘登录、上传、转存、AI 对话或云盘抽奖。显式指定 `campus` / `battle` 也会在登录前提示已暂停；恢复时将 `registry.py` 中对应条目从 `PAUSED_TASKS` 移回 `TASKS`。云手机积分和沃阅读继续运行。

### 福利商城九宫格（`welfare_task`）

依据 2026-10-31 抓包更新为积分活动，默认活动 `AC20260920094443`，渠道 `ZXGS97000020362`；本次抓包奖品为积分，中奖示例为 8 积分。

1. `findActivity` 校验活动状态与参与资格，`findLotteryCount` 查询剩余次数。
2. 以表单 `params` 承载 JSON 调用 `luckyDraw`，每次生成独立 `reqSeq`。
3. 等待 3 秒，以同一 `reqSeq` 调用 `findWinInfo`；返回 `isWin=false` 时每隔 1 秒再查，最多共 4 次，中奖立即停止查询，再刷新剩余次数。
4. `findWinInfo` 的 `1123` 按未中奖处理；`luckyDraw` 的 `1129` 会查询结果及次数，确认次数已减少才允许继续下一次，否则停止。连续提交之间额外等待 30 秒，每次提交都计入本轮上限。
5. 次数为零、次数未减少、未知响应时停止。超时或未知业务失败后仅进行上述有上限的结果查询并停止，不重发原抽奖请求。

新抓包返回 `luckType=0`、`consumptionIntegral=""`，初始剩余 3 次；空配置不能据此认定次数免费，以活动页面实际积分规则为准。默认每轮最多提交 3 次，`UNICOM_WELFARE_MAX_DRAWS` 可设为 0–10（0 跳过）。此上限是单次运行上限，不是跨进程的每日限制。活动更换时可覆盖活动 ID 与渠道，不需要修改业务代码。状态不在已验证范围内时跳过。

复用在线登录后的当前账号 Cookie，不读取 HAR，也不固定抓包中的账号、设备 ID 或请求编号。新活动业务分支通过脱敏响应及模拟 HTTP 回归测试；2026-10-31 已更新服务器凭证，4 个不同账号均登录成功，并完成新活动状态与次数的只读验证。凭证失效时需重新登录并更新青龙的 `chinaUnicomCookie`，多个 `token_online` 用 `@` 分隔；登录失败日志会显示接口的 `dsc` 原因。

### 商都福利（`shangdu_task`）

依据 2026-10-31 小程序抓包，使用当前账号的 `ecs_token`，经 `getTicketByNative`（`edop_unicom_4b80047a`）换票，再调用 `loginAppAppletFree` 获取当前账号授权；请求使用 `X-Client-Type: APPLET`。

签到节点券每天 **09:00、11:00、15:00、19:00** 放出。每个账号每月 **2 元券、5 元券各限领取 1 张**，对应累计签到 7 天、14 天的节点；以服务端当月节点状态判断是否已领取，仅对 `CLAIMABLE` 且当前场次 `disabled=false` 的节点提交一次。领取后读回 `CLAIMED` 才记录成功；`SESSION_EXHAUSTED` 表示本场已领完，不能把业务码 200 当成领取成功。签到后先刷新节点，避免使用签到前的旧资格。

青龙商都领取专用任务使用 `task shangdu_claim.py`，cron 为 `0 9,11,15,19 * * *`（北京时间）。此入口只领取节点券；日常签到、抽奖仍由主任务负责。旧的凌晨领取任务已替换。正常日每轮最多抽奖 1 次，节气回馈日最多 2 次，用户配置只能进一步降低上限；状态查询失败、次数不下降或提交结果不确定时停止。

### 云手机（`uphone_task`）

在手厅 `ecs_token` 基础上换票进入云手机活动体系，顺序为：

1. **SSO**：`getTicketByNative` → `getTokenByTicket` 得到 `cpToken`
2. **打卡挑战赛**（CLD13 / `h5forphone`）：复用 `cpToken` 作 `accesstoken`；`querySignInList` → `signIn` → 有订单则 `raffleSignIn`；补领 `signInRightList` 中 `state=1` 奖品（需 QQ/微信的 type=2 跳过）
3. **活动登录** + **积分签到**（`Points_Sign_2507`）
4. **赚积分任务**（`Points_Obtain_2507`）：可 API 完成的任务自动上报并领取；讨论区、看广告等需真机行为的任务跳过
5. **积分十连**：余额 ≥ 阈值且当日仍有次数时执行（默认满 100 分）
6. **夏日刮一刮**（`HD2026062200218`）：仅领 `2508-01` 换 1 次次数，有次数则抽完

### 校园季（`campus_task`）

当前已暂停，以下流程保留供恢复时参考。

1. **激活**后先查任务状态：已满的任务自动跳过，避免重复副作用
2. **上传**占位文件 + **AI 学习助手对话**（SSE 流式，独立新会话）
3. **转存教育/娱乐内容**：依次尝试校园 tab 候选 → 芒果TV 内容源
   （需芒果会员，无权益秒退）→ mbh 影视/教育频道（不限会员），
   内容池耗尽自动切换，已转存过的内容自动跳过
4. **自动抽奖**（次数本地硬上限保护）

任务进度查询失败时停止本轮校园季，避免将未知状态当作未完成。上传遇到 HTTP 500 或网络异常时，等待后读回进度；进度增加则停止重复上传，未确认也不盲目重传。教育频道按课程集数扫描，包含后续集，默认最多检查 20 个候选，并分别记录已转存、查询失败和转存失败的数量。

### 上传大比拼（`cloud_battle_task`）

当前已暂停，以下流程保留供恢复时参考。

与校园季同源的 panservice 签名 / upload2C 上传协议，但页面、活动 ID、上传域名独立：

1. 进入活动页经 `openPlatLineNew.htm` 跳转拿带 ticket 的 Referer（lottery-times 必需）
2. 校验 `activity-status` 与冲榜开启状态；未开启则用归属省自动开启
3. 无抽奖次数时上传 1 个占位文件，轮询次数到账
4. **自动抽奖**（每日 1 次，首次必中）；榜单查询失败不阻断抽奖

`activityId` 每期更换（30 → 38），下期换活动只需改 `UNICOM_BATTLE_ACTIVITY_ID`。

## 快速开始

### 依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10 或更新版本。`gmssl` 用于商都接口解密，当前模块加载需要安装该依赖。

### 配置

通过环境变量 `chinaUnicomCookie` 传入 `token_online`（多账号用 `@` 分隔）：

```bash
export chinaUnicomCookie="token1@token2"
```

可用仓库内的 `login.py` 获取 token，或从抓包中提取 `onLine.htm` 请求体内的 `token_online` 字段。

### 运行

```bash
python cu.py
```

部署时需同时保留 `cu.py` 和 `cu_tasks/`，不能再只复制入口文件。

仅执行九宫格（PowerShell）：

```powershell
$env:UNICOM_ONLY_TASK = "welfare"
python cu.py
```

`UNICOM_ONLY_TASK` 可用逗号指定多个任务，按指定顺序执行并去重；留空执行全部启用的任务。`UNICOM_SKIP_TASK` 的排除规则优先。两者出现未知名称均在登录前报错，避免配置拼错后执行意外任务。云盘活动 `campus` / `battle` 已暂停，不能通过 `UNICOM_ONLY_TASK` 启用。

### 可选环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `UNICOM_ONLY_TASK` | 全部启用任务 | `market,sign,welfare,xj,sec,farm,shangdu,uphone,woread` 中的任务 |
| `UNICOM_SKIP_TASK` | 空 | 逗号分隔的排除任务 |
| `UNICOM_WELFARE_ACTIVITY_ID` | `AC20260920094443` | 九宫格活动 ID |
| `UNICOM_WELFARE_FROM` | `ZXGS97000020362` | 九宫格渠道 |
| `UNICOM_WELFARE_MAX_DRAWS` | `3` | 每轮最多抽奖次数，0–10 |
| `UNICOM_TTXC_GARBAGE_WAIT` | `28` | 农场垃圾任务等待秒数 |
| `UNICOM_TTXC_GROW_MAX_CHARGE_PER_LAND` | `20` | 单地块最大充能次数 |
| `UNICOM_TTXC_HARVEST_WAIT` | `3` | 收获等待秒数 |
| `UNICOM_CAMPUS_EDU_ID` | 见源码 | 校园季教育转存兑底内容 ID |
| `UNICOM_CAMPUS_ENT_ID` | 见源码 | 校园季娱乐转存兑底内容 ID |
| `UNICOM_BATTLE_ACTIVITY_ID` | `Mzg=` | 上传大比拼活动 ID（每期更换） |
| `UNICOM_BATTLE_UPLOAD_URL` | 见源码 | 上传大比拼上传域名（逗号分隔多候选） |
| `UNICOM_CLOUD_BATTLE_FILE` | `文本.txt` | 上传大比拼占位文件名 |
| `UNICOM_CLOUD_BATTLE_CONTENT` | `1` | 上传大比拼占位文件内容 |
| `SHANGDU_LOTTERY_MAX` | 普通日 1 / 节气日 2 | 每轮抽奖上限，只能进一步降低 |
| `UNICOM_UPHONE_LOTTERY_COST` | `100` | 云手机积分十连触发余额阈值 |

## 说明

- 默认关闭 HTTP/2，使用 HTTP/1.1
- 日志中对手机号、token、ticket 等敏感信息做脱敏处理
- 请勿将 token、密钥等敏感信息提交到公开仓库

## 免责声明

本项目仅供学习交流，不得用于违法用途。开发者不对使用后果承担责任。

## 开发边界与检查

- 入口持有账号状态和 HTTP 客户端；活动模块复用宿主 `req`、`get_ticket`、`tlog` 等接口，不导入 `cu.py`，不创建全局账号会话。
- 活动接口使用各自的业务码与字段校验，不把不同服务的成功规则混为一谈。新活动使用独立方法前缀，注册到 `registry.py`；共用云盘能力放在 `cloud.py`。
- 调度层负责任务异常隔离、日志标签恢复和连接关闭；取消信号继续向上传递。云盘活动当前全部暂停，默认任务不触发云盘登录。
- 九宫格配置校验非空活动参数与次数范围；抽奖结果不确定时停止写操作。增加活动时应为有副作用的循环设置明确上限。

```bash
python -m pip install ruff
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
```

测试覆盖九宫格请求格式、成功/火爆/超时响应、次数边界、活动资格，以及任务选择、异常隔离、云盘活动暂停和取消时释放连接。
