# mcp-agentos

## CLIProxyAPI（124 /opt/CLIProxyAPI）
- 本体是上游开源 Go 二进制（CLIProxyAPI），**源码不在此仓**；此处仅保留：
  - `CLIProxyAPI/static/management.html` —— 我们定制过的管理台（用量审计页、筛选、分页等）
- 部署 = 官方二进制 + `config.yaml`（**含渠道 API key，只存在于服务器，永不入仓**）+ systemd。
- 升级：下载上游新二进制替换 → 保留 config.yaml → 替换本目录的 management.html → 重启。

## new-api（124 Docker，端口 8318）
- 容器化部署，配置在容器卷与 env；模型/渠道在 Web 面板管理。
- X 工作流 LLM 走它：`WORKFLOW_LLM_BASE_URL=http://127.0.0.1:8318/v1`，key 存 console env。

## agentos_mcp_orchestrator_transfer（124 /srv/cloudcli-workspaces/default/）
- MCP orchestrator 与技能/适配器代码（含 case 分析、飞书适配器等）。
- `.env`（飞书 app id/secret 等）只在服务器。

## companion（124 systemd companion.service）
- Companion 服务，unit 在 `infra/systemd/`。
