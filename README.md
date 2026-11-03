# cu

Python 异步脚本，仅供编程学习与技术交流。示例涵盖 `httpx` 并发请求、Cookie 会话管理、多账号调度，以及常见加解密与签名流程的实现方式。

> 请勿用于商业用途或任何违反服务条款的场景；使用风险由使用者自行承担。

## 项目结构

```text
cu.py                       # 主入口（青龙：task cu.py）
shangdu_claim.py            # 商都节点券专用入口（仅领签到节点券）
cu_tasks/
├── config.py               # Settings：启动时一次性读取并校验全部环境变量
├── session.py              # Account：在线登录、连接池、账号日志
├── base.py                 # Task 基类：隔离客户端、换票（openPlat / edop）、任务日志
├── net.py                  # 共享连接池 + 按任务复制 Cookie、统一响应结构
├── jftask.py               # 积分中心 m.jf.10010.com 签名客户端（安全管家 / 沃阅读共用）
├── common.py               # 无状态工具：整数校验、响应摘要、日志脱敏
├── registry.py             # 唯一任务列表、顺序与选择规则
├── runner.py               # 调度：多账号并发、账号内任务并发、通知汇总
└── tasks/                  # 每个活动一个 Task 子类
    ├── market.py  sign.py  welfare.py  xinjiang.py  security.py
    └── farm.py  shangdu.py  uphone.py  woread.py
tests/                      # 离线协议与调度回归测试
```

## 任务模块

登录后执行以下任务。默认同一账号内最多 3 个任务并发，单个任务失败不影响其他任务。

| 名称 | 标签 | 类 | 备注 |
|------|------|----|------|
| `market` | 权益超市 | `MarketTask` | 10 点开抢，排在最前 |
| `sign` | 签到区 | `SignTask` | |
| `welfare` | 福利商城九宫格 | `WelfareTask` | 抓包活动，默认每轮最多 3 次 |
| `xj` | 新疆联通 | `XinjiangTask` | 仅新疆归属地 |
| `sec` | 安全管家 | `SecurityTask` | |
| `farm` | 通通乡村 | `FarmTask` | |
| `shangdu` | 商都福利 | `ShangduTask` | 仅河南归属地 |
| `uphone` | 云手机积分 | `UphoneTask` | 见下方说明 |
| `woread` | 沃阅读积分 | `WoreadTask` | 每账号自动换票 |

云盘活动（校园季、上传大比拼）已下线，相关代码已删除。

### 福利商城九宫格（`welfare`）

依据 2026-10-31 抓包更新为积分活动，默认活动 `AC20260920094443`，渠道 `ZXGS97000020362`；本次抓包奖品为积分，中奖示例为 8 积分。

1. `findActivity` 校验活动状态与参与资格，`findLotteryCount` 查询剩余次数。
2. 以表单 `params` 承载 JSON 调用 `luckyDraw`，每次生成独立 `reqSeq`。
3. 等待 3 秒，以同一 `reqSeq` 调用 `findWinInfo`；返回 `isWin=false` 时每隔 1 秒再查，最多共 4 次，中奖立即停止查询，再刷新剩余次数。
4. `findWinInfo` 的 `1123` 按未中奖处理；`luckyDraw` 的 `1129` 会查询结果及次数，确认次数已减少才允许继续下一次，否则停止。连续提交之间额外等待 30 秒，每次提交都计入本轮上限。
5. 次数为零、次数未减少、未知响应时停止。超时或未知业务失败后仅进行上述有上限的结果查询并停止，不重发原抽奖请求。

新抓包返回 `luckType=0`、`consumptionIntegral=""`，初始剩余 3 次；空配置不能据此认定次数免费，以活动页面实际积分规则为准。默认每轮最多提交 3 次，`UNICOM_WELFARE_MAX_DRAWS` 可设为 0–10（0 跳过）。此上限是单次运行上限，不是跨进程的每日限制。活动更换时可覆盖活动 ID 与渠道，不需要修改业务代码。状态不在已验证范围内时跳过。

复用在线登录后的当前账号 Cookie，不读取 HAR，也不固定抓包中的账号、设备 ID 或请求编号。新活动业务分支通过脱敏响应及模拟 HTTP 回归测试；2026-10-31 已更新服务器凭证，4 个不同账号均登录成功，并完成新活动状态与次数的只读验证。凭证失效时需重新登录并更新青龙的 `chinaUnicomCookie`，多个 `token_online` 用 `@` 分隔；登录失败日志会显示接口的 `dsc` 原因。

### 商都福利（`shangdu`）

依据 2026-10-31 小程序抓包，使用当前账号的 `ecs_token`，经 `getTicketByNative`（`edop_unicom_4b80047a`）换票，再调用 `loginAppAppletFree` 获取当前账号授权；请求使用 `X-Client-Type: APPLET`。

签到节点券每天 **09:00、11:00、15:00、19:00** 放出。每个账号每月 **2 元券、5 元券各限领取 1 张**，对应累计签到 7 天、14 天的节点；以服务端当月节点状态判断是否已领取，仅对 `CLAIMABLE` 且当前场次 `disabled=false` 的节点提交一次。领取后读回 `CLAIMED` 才记录成功；`SESSION_EXHAUSTED` 表示本场已领完，不能把业务码 200 当成领取成功。签到后先刷新节点，避免使用签到前的旧资格。

青龙商都领取专用任务使用 `task shangdu_claim.py`，cron 为 `0 9,11,15,19 * * *`（北京时间）。此入口只领取节点券；日常签到、抽奖仍由主任务负责。旧的凌晨领取任务已替换。正常日每轮最多抽奖 1 次，节气回馈日最多 2 次，用户配置只能进一步降低上限；状态查询失败、次数不下降或提交结果不确定时停止。

### 云手机（`uphone`）

在手厅 `ecs_token` 基础上换票进入云手机活动体系，顺序为：

1. **SSO**：`getTicketByNative` → `getTokenByTicket` 得到 `cpToken`
2. **打卡挑战赛**（CLD13 / `h5forphone`）：复用 `cpToken` 作 `accesstoken`；`querySignInList` → `signIn` → 有订单则 `raffleSignIn`；补领 `signInRightList` 中 `state=1` 奖品（需 QQ/微信的 type=2 跳过）
3. **活动登录** + **积分签到**（`Points_Sign_2507`）
4. **赚积分任务**（`Points_Obtain_2507`）：可 API 完成的任务自动上报并领取；讨论区、看广告等需真机行为的任务跳过
5. **积分十连**：余额 ≥ 阈值且当日仍有次数时执行（默认满 100 分）
6. **夏日刮一刮**（`HD2026062200218`）：仅领 `2508-01` 换 1 次次数，有次数则抽完

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

部署时需同时保留 `cu.py` 和 `cu_tasks/`，不能只复制入口文件。

仅执行九宫格（PowerShell）：

```powershell
$env:UNICOM_ONLY_TASK = "welfare"
python cu.py
```

`UNICOM_ONLY_TASK` 可用逗号指定多个任务，按指定顺序执行并去重；留空执行全部任务。`UNICOM_SKIP_TASK` 的排除规则优先。所有环境变量在登录前统一校验，名称拼错或数值非法时直接报错退出，不会执行意外任务。

### 可选环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `UNICOM_ONLY_TASK` | 全部任务 | `market,sign,welfare,xj,sec,farm,shangdu,uphone,woread` 中的任务 |
| `UNICOM_SKIP_TASK` | 空 | 逗号分隔的排除任务 |
| `UNICOM_TASK_CONCURRENCY` | `3` | 同一账号内并发任务数，1–9；设为 1 则按上表顺序串行 |
| `UNICOM_WELFARE_ACTIVITY_ID` | `AC20260920094443` | 九宫格活动 ID |
| `UNICOM_WELFARE_FROM` | `ZXGS97000020362` | 九宫格渠道 |
| `UNICOM_WELFARE_MAX_DRAWS` | `3` | 每轮最多抽奖次数，0–10 |
| `UNICOM_MARKET_WAIT_MAX` | `300` | 权益超市开抢前最多等待秒数，超过则跳过 |
| `UNICOM_MARKET_API_RETRIES` | `3` | 权益超市查询接口网络/5xx 重试次数 |
| `UNICOM_TTXC_GARBAGE_WAIT` | `28` | 农场垃圾任务等待秒数 |
| `UNICOM_TTXC_GROW_MAX_CHARGE_PER_LAND` | `20` | 单地块最大充能次数 |
| `UNICOM_TTXC_HARVEST_WAIT` | `3` | 收获等待秒数 |
| `SHANGDU_LOTTERY_MAX` | 普通日 1 / 节气日 2 | 每轮抽奖上限，只能进一步降低 |
| `UNICOM_UPHONE_LOTTERY_COST` | `100` | 云手机积分十连触发余额阈值 |

## 说明

- 默认关闭 HTTP/2，使用 HTTP/1.1；连接超时 15 秒，读取超时 50 秒
- 日志中对手机号、token、ticket 等敏感信息做脱敏处理
- 请勿将 token、密钥等敏感信息提交到公开仓库

## 免责声明

本项目仅供学习交流，不得用于违法用途。开发者不对使用后果承担责任。

## 架构与开发约定

- **账号与任务分离**：`Account` 只负责在线登录和登录态；每个活动是独立的 `Task` 子类，状态（token、用户 ID 等）保存在任务实例内，不再共享一个大类。
- **隔离与复用**：每个任务有自己的 `httpx` 客户端。创建时复制登录 Cookie，之后各自修改互不影响；所有客户端共用同一账号的连接池（keep-alive）。
- **并发**：多账号并发；账号内由 `UNICOM_TASK_CONCURRENCY` 限制并发数。日志实时打印，通知按账号与注册顺序汇总，不受并发交错影响。
- **配置**：全部环境变量由 `Settings.from_env()` 在登录前读取并校验，任务通过 `self.settings` 读取，模块导入时不读取环境变量。
- **错误边界**：调度层负责异常隔离、日志标签与连接关闭；取消信号继续向上传递，且会先收拢同账号的其他任务。
- **业务判定**：各活动使用自己的业务码与字段校验；抽奖等有副作用的循环必须设置明确上限，结果不确定时停止写操作。

新增活动：在 `cu_tasks/tasks/` 新建模块，继承 `Task`，声明 `name` / `label`（按归属地限制时再声明 `provinces`），实现 `async def run(self)`，然后加入 `registry.py` 的 `TASKS`。常用能力：`self.req`、`self.get_ticket`（openPlat 跳转）、`self.edop_ticket`（getTicketByNative）、`self.tlog`。

```bash
python -m pip install ruff
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
```

测试覆盖九宫格与商都协议、次数边界、活动资格，以及配置校验、任务选择、并发上限、Cookie 隔离、异常隔离、归属地过滤，和取消时的连接释放。
