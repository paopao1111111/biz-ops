# mcp-agentos

AgentOS / MCP 相关组件（124 服务器）。

## 包含

- `agentos_mcp_orchestrator_transfer/` —— MCP orchestrator 与技能/适配器代码（case 分析、飞书适配器、报告服务等），源位置 `124:/srv/cloudcli-workspaces/default/agentos_mcp_orchestrator_transfer`。`config.example.yaml` 为脱敏配置示例，真实 `.env`/token 只在服务器与飞书【敏感】清单。

## new-api（124 Docker，端口 8318）

- 容器化模型网关，X 内容工作流的 LLM 走它：`WORKFLOW_LLM_BASE_URL=http://127.0.0.1:8318/v1`，key 存 console env。
- 模型/渠道在其 Web 面板管理。

## ⚠️ 特别说明：模型中转依赖（CLIProxy，非交接资产）

- 服务器上另有一个 **CLIProxy**（127.0.0.1:8317），是**原负责人私用的模型中转站**，**不属于交接范围**，本仓库也不收录其任何内容。
- 但**历史上多个项目的模型调用走它**（orchestrator 部分适配器、EDM `lib/llm_client.py` 与 `lib/config.py` 中的 cli 端点等）。代码里看到的 `127.0.0.1:8317` / cli-proxy 字样即指向它。
- **交接接收方注意**：该中转随原负责人离开即不可用。相关服务需把模型端点改到 **new-api（8318）** 或自建的任意 OpenAI 兼容网关，或改用直连模型 API key。改动点：各服务 env 中的 `*_BASE_URL` / `*_API_KEY`（代码默认常量仅作兜底）。
- 迁移先例：X 工作流已走 new-api（8318），orchestrator 的 `direct_llm.py` 是直连实现，可参照。
