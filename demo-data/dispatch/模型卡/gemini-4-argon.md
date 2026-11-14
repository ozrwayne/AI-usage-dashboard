来源: 联网调研 本机历史对照实测；本机 `~/.omp/stats.db` 与 本机历史对照实测；Artificial Analysis 2026-11-14 快照
日期: 2026-11-14

# gemini-4-argon

- 选择器：`google-antigravity-3/gemini-4-argon`。单价、上下文、其他渠道、近 7 日用量见《模型总表》。
- 档位：ultra / high (50) · medium (48)
- 厂商定位：Gemini 4 的高效推理档，2026-10-13 发布。
- 公开强项：编码、方案、英文法律与金融知识工作、读图。
- 公开短板：256k 以上长上下文退化（GraphWalks 84.2%）。2026-12-01 起标价翻倍。每任务 4.97 美元。
- 本机实测：经 Cursor 渠道子代理 81 轮错 25、平均每轮 625 秒，不可靠。经 Antigravity 渠道 69 轮错 3。
- 适合派：走 Ultra 账号 `google-antigravity-3/gemini-4-argon`（角色 `gemini`），用于代码审查的替代。2026-11-14 实测派出成功，1 题答对。
- 不适合派：经 Cursor 渠道当子代理。
- 标签：代码审查、Ultra号、× Cursor
