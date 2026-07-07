# cu

Python 异步脚本，仅供编程学习与技术交流。示例涵盖 `httpx` 并发请求、Cookie 会话管理、多账号调度，以及常见加解密与签名流程的实现方式。

> 请勿用于商业用途或任何违反服务条款的场景；使用风险由使用者自行承担。

## 项目结构

```text
cu.py
├── 工具函数 ........................ 响应解析、异常格式化、Cookie 处理
├── Unicom .......................... 核心业务类（请求 / 登录 / 各任务模块）
└── main ............................ 多账号并发入口
```

## 任务模块

登录后按顺序执行（单任务失败不影响其余任务）：

| 标签 | 模块 | 备注 |
|------|------|------|
| 权益超市 | `market_task` | |
| 签到区 | `sign_task` | |
| 新疆联通 | `xj_task` | 按归属地跳过 |
| 天天领现金 | `ttlxj_task` | |
| 联通祝福 | `ltzf_task` | |
| 安全管家 | `sec_task` | |
| 通通乡村 | `farm_task` | |
| 云盘乘风活动 | `cloudpan_task` | 需云盘 token |
| 上传大比拼 | `cloud_battle_task` | 需云盘 token |
| 商都福利 | `shangdu_task` | 按归属地跳过 |

## 快速开始

### 依赖

```bash
pip install httpx pycryptodome gmssl
```

`gmssl` 仅部分接口解密需要，可按需安装。

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

### 可选环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `UNICOM_TTXC_GARBAGE_WAIT` | `28` | 农场垃圾任务等待秒数 |
| `UNICOM_TTXC_GROW_MAX_CHARGE_PER_LAND` | `20` | 单地块最大充能次数 |
| `UNICOM_TTXC_HARVEST_WAIT` | `3` | 收获等待秒数 |
| `UNICOM_YPHD_MGTV_IMG_FID` | 空 | 模板图片 fid |
| `UNICOM_CLOUD_BATTLE_FILE` | `文本.txt` | 上传文件名 |
| `UNICOM_CLOUD_BATTLE_CONTENT` | `1` | 上传文件内容 |
| `SHANGDU_LOTTERY_MAX` | 剩余次数 | 单次最多抽奖次数 |

## 说明

- 默认关闭 HTTP/2，使用 HTTP/1.1
- 日志中对手机号等信息做脱敏处理
- 请勿将 token、密钥等敏感信息提交到公开仓库

## 免责声明

本项目仅供学习交流，不得用于违法用途。开发者不对使用后果承担责任。