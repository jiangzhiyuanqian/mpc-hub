# 多平台连接中心（mpc-hub）

> 独立的社交通道客服后台：自带 agent 运行时，一个进程里读消息、判断、生成回复、走治理闸门，再发回平台。
> **不依赖、不修改、不安装到 DSH 里** —— 它就是一个自己跑、自己存数据的客服服务程序。

- 桌面设置窗口（`mpc gui`）：**API Key 由你自己填**，程序只认你写进配置文件的那一份，不扫描系统里其它软件留下的凭据。
- 终端对话（`mpc chat`）：不填 Key 也能跑通全链路（离线 Mock），填了 Key 就走真实模型。
- HTTP 后台（`mpc serve`）：只监听本机，提供入站、审批、审计、指标接口。
- 平台接入：微信 iLink（扫码即用）、QQ OneBot v11、通用 Webhook、以及**任意第三方连接器**（插件式扩展）。

---

## 1. 安装

需要 Python 3.11 以上（开发环境用 3.14 验证过）。

```bash
git clone https://github.com/jiangzhiyuanqian/mpc-hub.git
cd mpc-hub

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -e .
```

也可以直接装（不克隆）：

```bash
pip install "mpc-hub @ git+https://github.com/jiangzhiyuanqian/mpc-hub.git"
```

装完就有 `mpc` 命令。

---

## 2. 三种用法

### 2.1 `mpc gui` —— 桌面设置窗口（第一次用先跑这个）

```
mpc gui
```

窗口有五个页签：

| 页签 | 作用 |
| --- | --- |
| 模型接口 | 选供应商（DeepSeek / MiniMax / OpenAI / Ollama / 自定义 OpenAI 兼容端点）、填 Base URL、**API Key**、模型名；可一键拉取该接口的模型列表、测试连通性 |
| 平台接入 | 微信 iLink 开关与**扫码登录**（窗口里直接出二维码）、QQ OneBot 地址与 token、通用 Webhook 出站地址、自定义连接器 |
| 治理规则 | 白名单、主人 ID、黑名单、服务时段、每日读写配额、单条回复字数上限、四个开关（承诺需确认 / 群聊需确认 / 群聊仅@时回复 / 丢弃纯招呼） |
| 对话测试 | 不用接平台，先跟客服 agent 聊两句，看它怎么判、怎么回 |
| 运行状态 | 能力矩阵、实时指标、审计流水、连接 / 断开微信通道 |

配置保存到 `~/.mpc-hub/config.json`（可用环境变量 `MPC_CONFIG` 改路径）。
**Key 只写进这一个文件**，文件权限会收紧到仅本人可读；界面与 HTTP 接口返回的都是掩码值，不会回显明文。

### 2.2 `mpc chat` —— 终端里直接对话

```
mpc chat
```

```
你 > 客户问的价格怎么算？
客服 > 您好，价格方案与具体规格相关，方便说明一下您的使用场景吗？
你 > /history      # 看历史
你 > /clear        # 清空当前会话
你 > /exit         # 退出
```

没填 Key 时用的是离线 Mock 回复（不联网），用来验证链路；填了 Key 就走真实模型。

### 2.3 `mpc serve` —— HTTP 后台

```
mpc serve --host 127.0.0.1 --port 8760
```

只监听本机。接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探测 |
| POST | `/api/inbound` | 送一条入站消息进来（跑全链路） |
| POST | `/api/webhook/{platform}` | Webhook 通道的入站入口 |
| GET | `/api/approvals` | 待人工确认的回复 |
| POST | `/api/approvals/{id}` | 批准 / 驳回（可改文案） |
| GET | `/api/capabilities` | 能力矩阵 |
| GET | `/api/metrics` | 实时指标 + 每日账本 |
| GET | `/api/audit` | 审计流水 |
| GET | `/api/sessions/{platform}/{conversation_id}` | 会话历史、水位线、未闭合承诺 |
| GET | `/api/settings` | 读配置（脱敏） |
| POST | `/api/settings` | 写配置（`api_key` 传空串表示保持原样） |

### 2.4 `mpc selftest` —— 离线自检

```
mpc selftest
```

不连任何真实平台，跑一遍：普通提问→发送 / 纯招呼→不回 / 承诺话术→挂起等 8 项。

---

## 3. 接入平台

### 3.1 微信 iLink（最简单，扫码即用）

在「平台接入」页点「扫码登录」→ 窗口里出二维码 → 用微信扫 → 填好自动落盘 → 在「运行状态」页点「连接微信通道」。

限制（协议本身的限制，不是本程序的）：
- **仅私聊**，不支持群聊；
- **一个账号一个 poller**，建议用专用小号；
- 走的是腾讯官方的 iLink Bot 通道，与微信个人号自动化是两回事 —— 不碰你的主号，不注入、不 Hook。

### 3.2 QQ（OneBot v11）

填 OneBot 的地址（如 `http://127.0.0.1:3000`）与 access_token。上游可以用 LLOneBot、Lagrange.Milky 等任意 OneBot v11 实现。

### 3.3 通用 Webhook

给出站地址与可选 token（`X-MPC-Token` 头），任何能把消息 POST 过来的系统都能接。

### 3.4 任意第三方连接器（扩展点）

两种方式，都不用改本程序：

**方式一：配置里写模块路径**（窗口「平台接入 → 自定义连接器」）

```json
{
  "platforms": {
    "custom": [
      {
        "enabled": true,
        "platform": "myplatform",
        "module": "my_pkg.my_connector",
        "attr": "create",
        "options": { "token": "xxx" }
      }
    ]
  }
}
```

模块里导出一个 `create(**options)` 工厂，返回一个满足连接器接口的对象即可。

**方式二：装成 pip 包**（发布方用这个）

```
[project.entry-points."mpc.connectors"]
myplatform = "my_pkg.my_connector:create"
```

装上就自动被发现，程序启动时自动注册。

连接器接口很小，就三件事：

```python
class MyConnector:
    platform = "myplatform"

    def capability(self) -> dict:      # 能不能读、写多少字、支持群聊……
        return {"can_read": True, "can_send": True, "supports_group": False,
                "max_text_length": 1000, "notes": "我的通道"}

    def to_envelope(self, payload) -> Envelope: ...   # 平台消息 → 统一信封
    def send(self, action) -> None: ...              # 统一动作 → 平台发送
    def start(self) -> None: ...                     # 可选：开始收消息
    def stop(self) -> None: ...                      # 可选：停止
```

---

## 4. 统一契约

所有平台的消息进来都变成同一种信封，出去都变成同一种动作：

```python
Envelope(
    message_id="...", platform="wechat", conversation_id="...",
    conversation_kind="private", sender_id="...", sender_name="...",
    text="...", is_mention=False, attachments=[], timestamp=0.0, raw={},
)

Action(
    action_id="...", action_type="send_text", platform="wechat",
    conversation_id="...", text="...", idempotency_key="...",
)
```

所以加平台 = 写一个信封/动作转换器，治理、记忆、审计、指标全部复用。

---

## 5. 治理模型：能回 ≠ 该回

入站依次过这些闸：`allow_platforms` → 黑名单 → 白名单 → 群聊是否 @ → 是否纯招呼 → 服务时段 → 每日读配额。

出站依次过：`黑名单` → `超长/空` → `凭据泄漏（API Key、密码）` → `写配额` → `承诺性话术（金额/下单/保证/法律）` → `身份越界` → `群聊是否需人工确认`。

命中"需确认"就**挂起**，进 `/api/approvals` 等人放行；命中硬拦截就丢弃并留审计。人工放行时不会因为原文里带承诺词而卡死。

---

## 6. 数据落点

- 配置：`~/.mpc-hub/config.json`（Key 只在这里，权限收紧）
- 数据库：`./data/mpc.db`（SQLite：会话、消息、水位线、每日账本、审计、人物档案、承诺台账、KV）
- 微信扫码二维码：`~/.mpc-hub/wechat-login-qr.png`

消息按 `(platform, message_id)` 入站唯一索引，重复投递自动识别为 duplicate，不会重复回话。

---

## 7. 目录结构

```
src/mpc/
  contracts.py        统一契约（Envelope / Action / Capability / Verdict）
  settings.py         配置中心 + 供应商预置（Key 只落本机）
  runtime.py          按配置装配运行时（store / governance / llm / registry / hub）
  store.py            SQLite 存储与审计
  governance.py       入站出站双闸门
  hub.py              枢纽：幂等 → agent → 闸门 → 投递 / 挂起
  desktop.py          tkinter 桌面设置窗口
  cli.py              命令行入口
  server/app.py       FastAPI 后台
  agent/
    graph.py          LangGraph 状态图：ingest → triage → draft → gate → route
    prompts.py        快判 / 起草 / 对话 三套提示词
    llm.py            OpenAI 兼容客户端（重试、限流兜底、SSE）+ 离线 Mock
    chat.py           控制台对话会话
  connectors/
    base.py           连接器接口
    registry.py       连接器注册表
    plugins.py        扩展点加载（配置模块路径 + entry points）
    loopback.py / onebot.py / webhook.py / ilink.py
```

---

## 8. 已知限制

- 微信 iLink：**只支持私聊**，一个账号一个长轮询进程，建议专用小号。
- 微信个人号（4.x）自动化不在本程序范围内 —— 那种做法要 Hook 或注入，风险与封号代价都不小；本程序只走官方通道。
- 没填 Key 时是离线 Mock，不是真模型。
- 内置 agent 运行时基于 LangGraph；LangGraph 缺失时会自动降级成等价的线性执行器。

---

## 9. 开发

```
pip install -e ".[dev]"
pytest -q
mpc selftest
```

---

## 10. 许可证

MIT，见 [LICENSE](LICENSE)。
