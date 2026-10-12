# astrbot_plugin_ima_kb

让机器人查**腾讯 ima 知识库**（含订阅库）里的专业资料。

命中资料后交给模型**用自己的口吻转述**，不是把原文复制一遍——所以群聊里看起来像它自己知道，而不是"查了一下资料显示"。

## 功能

| 能力 | 说明 |
|---|---|
| LLM 工具 `ima_ask` | 模型自己判断"这问题需要查资料"时调用，闲聊不触发 |
| `/ima <问题>` | 手动查一次，直接把答案发出来（不走模型改写） |
| `/ima_status` | 看插件状态（仅管理员） |
| 多知识库 | 配置里挂多个库，按问题里的关键词自动选库 |
| 提示词可控 | 可自定义发给 ima 的提问模板与答案字数上限，直接控制回答的篇幅 |
| 结果缓存 | 同一个问题 N 分钟内直接复用，不再打 ima |
| 超时降级 | 超过配置秒数就放弃查询，让模型正常回答，群里不会干等 |

## 原理：为什么不走 ima 官方 OpenAPI

ima 有两条门：

| 门 | 地址 | 认证 | 订阅库 |
|---|---|---|---|
| 开放平台 OpenAPI | `ima.qq.com/openapi/wiki/v1/*` | Client ID + API Key | ❌ 按知识库归属鉴权，返回 `220030 没有权限` |
| **内部接口（本插件走这条）** | `ima.qq.com/cgi-bin/*` | 浏览器登录态 cookie | ✅ 权限跟随账号，订阅库可读 |

所以本插件用 `x-ima-cookie` + `x-ima-bkn` 调内部接口：

```
POST /cgi-bin/session_logic/init_session   # 建会话并绑定知识库
POST /cgi-bin/assistant/qa                 # 提问，返回 SSE 流
POST /cgi-bin/auth_login/refresh           # token 到期自动续期
```

> ⚠️ 两条路不能混为一谈。「订阅库搜不到」那个结论只对 OpenAPI 成立。

## 安装

1. 把 `astrbot_plugin_ima_kb` 整个目录放进 `AstrBot/data/plugins/`
   （或在 WebUI 插件页上传本目录打成的 zip）
2. 重启 AstrBot 或在该插件卡片上点「重载插件」

插件依赖 `httpx`（见 `requirements.txt`），AstrBot 通常已自带。

## 配置

在 WebUI 的插件配置页填写：

### 1. 登录态（必填）

| 字段 | 怎么拿 |
|---|---|
| `x-ima-cookie` | 浏览器登录 https://ima.qq.com → `F12` → **Network** → 在 ima 里随便发一条消息 → 点 `/cgi-bin/assistant/qa` 请求 → **Request Headers** → 复制 `x-ima-cookie` |
| `x-ima-bkn` | 同一个请求头里的 `x-ima-bkn`（一般是一串数字） |

**必须确认 `x-ima-cookie` 里含 `IMA-REFRESH-TOKEN`**，否则 token 到期后无法自动续期。

> 🔒 这串 cookie 等于你 ima 账号的访问凭证，不要外传、不要提交到任何公开仓库。

### 2. 知识库列表

一行一个，格式 `名字=知识库ID`：

```
游戏设定资料=1234567890123456
推理题合集=6543210987654321
```

库 ID 的拿法：在 ima 里选中该知识库，`F12` → 找 `/cgi-bin/session_logic/init_session` 请求 → Payload 里的 `knowledge_base_id`。

名字随便取，插件会用它匹配用户问题里的关键词来选库。

### 3. 运行参数

| 配置 | 默认 | 说明 |
|---|---|---|
| `default_knowledge_base` | 空 | 匹配不到库名时用哪个；留空用列表第一个 |
| `enable_llm_tool` | 开 | 关掉后模型不会自动查，只能用 `/ima` |
| `max_seconds` | 45 | 单次查询超时。实测一次 15~25 秒，别设太小 |
| `max_chars` | 900 | 喂给模型的资料上限。太长会挤压上下文、也更容易被大段照抄 |
| `cache_minutes` | 10 | 同问题缓存时长，0 = 关闭 |
| `ask_prompt` | 空 | 发给 ima 的提问模板，`{question}` 代表用户那句话；留空 = 原样提问 |
| `answer_limit` | 0 | 答案字数上限；大于 0 时自动在提问末尾追加「请用不超过 N 字回答」 |

### 4. 怎么控制答案长短

ima 的问答接口**没有长度参数**，只能靠提示词约束。两种写法选一个：

- **只想改长度** ⇒ `answer_limit` 填 `300`（最省事，插件自动拼句子）
- **想连风格一起管** ⇒ `ask_prompt` 写完整模板，例如
  `请用简洁的中文回答，只讲结论和要点，不要罗列原文。{question}`

> `{question}` 是占位符，插件会替换成用户真正问的那句。不写占位符也行——模板会被当成前置指令，问题自动附在下一行。
>
> ⚠️ 别"提示词里手写「不超过 300 字」+ `answer_limit` 填 300"同时用，会重复要求。

## Known limits（实测）

- **慢**：一次问答 15~25 秒。所以走的是"模型自己判断要不要查"，而不是每条消息都查
- **串行**：插件内部并发锁为 1，ima 侧对突发并发敏感（会返回 `Code=3`）
- **图片类内容靠 OCR**：知识库里的图片条目返回的是识别文本，可能掺进界面文字
- **cookie 会过期**：过期后日志会报登录态失效，重新抓一次即可
- **用的是内部接口**：ima 改版可能失效，届时需要重新对齐

## 致谢与灵感来源

本插件的设计参考了以下项目，**代码为重新实现**，未直接拷贝：

- [highkay/tencent-ima-copilot-mcp](https://github.com/highkay/tencent-ima-copilot-mcp)（MIT）——
  ima 内部接口的调用方式（`init_session` / `qa` / `refresh` 的请求结构与 SSE 流解析思路）参考了它的实现。
- [NickCharlie/astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning)（AGPL-3.0）——
  顶层 `__init__.py` 里"显式登记父级命名空间包，避免装依赖后相对导入失效"的做法参考了它的注释与思路。

如果原项目作者认为本仓库的借鉴方式不妥，请联系调整。

## 许可

本插件源码以 MIT 协议分发，可自由使用与修改。
