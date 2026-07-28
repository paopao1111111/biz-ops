# biz-ops

业务自动化系统的统一代码仓库。**本仓库是唯一事实源**——所有改动先提交到这里，再从仓库部署到服务器。

> **新人先看**：[`HANDOVER.md`](HANDOVER.md) ——「服务器/电脑全丢，如何用本仓库 + 飞书【敏感】清单从零重建全部服务」的环境矩阵与步骤。秘密（密码/token/密钥）在飞书【敏感】账号密码清单，本仓库不含任何秘密。
> **运营同学先看**：[`docs/x-console-manual/`](docs/x-console-manual/) ——X 浏览控制台操作手册（含全部界面截图与红线）。

## 目录

| 目录 | 内容 | 部署位置 |
|---|---|---|
| `x-automation/console/` | X 浏览控制台（stdlib HTTP 服务、工作流 LLM、飞书记录、HTTPS 网关） | 124 `/opt/x-browse-console`（systemd `x-browse-console` 8790 + `x-https-gateway` 8443） |
| `x-automation/worker/` | Windows 只读浏览 worker（AdsPower + geckodriver，3 并发槽） | Windows `C:\x-browse-console-worker`（计划任务 `X Browse Console Worker`） |
| `x-automation/write-service/` | X 官方 API 写入服务（草稿→冻结审批→执行→审计状态机、媒体上传、定时发布） | 124 `/opt/x-write-service`（systemd `x-write-service`，loopback 8791） |
| `x-automation/deploy/` | 暂停态部署脚本（备份→停服→scp→起服→验证不变量） | 从 Mac 运行 |
| `x-automation/tests/` | console 侧全部测试（UI 静态合约、工作流、OAuth BFF、生命周期） | 本地跑 |
| `edm/edm-system/` | EDM 邮件自动回复系统（客服邮件自动应答、反馈通知、Gmail/Superset/飞书集成） | 124 `/opt/edm-system`（systemd `edm-auto-reply` / `feedback-autoreply` / `mihomo-edm`） |
| `analytics/iweaver-monitoring/` | iWeaver 监控（窗口告警、每日报告） | 124 `/opt/iweaver-monitoring`（systemd `iweaver-monitor-*`） |
| `analytics/multisite-dashboard/` | iWeaver/Palmly/Learning Coach 多站点周报面板 | 124 `/opt/multisite-dashboard`（systemd `dashboard-*` / `multisite-weekly-*`，端口 8780） |
| `mcp-agentos/agentos_mcp_orchestrator_transfer/` | MCP orchestrator + 各适配器（case 分析日报、dashboard 指标、wp_tes、competitor_seo、iweaver_admin） | 124 `/srv/cloudcli-workspaces/default/`（systemd `iweaver-case-analysis-daily` 等） |
| `mcp-agentos/README.md` | CLIProxy 特别说明：**CLIProxy（127.0.0.1:8317）为原负责人私用模型中转站，非交接资产**；模型端点需改到 new-api(8318) 或自建网关 | — |
| `infra/runbooks/` | 排障手册（代理链、HTTPS 网关、X credits 等） | — |
| `infra/proxy/` | 代理链配置示例（已脱敏，凭据位 REPLACE） | Mac + Windows |
| `infra/systemd/` | 全部 systemd unit 模板 | 124 |
| `docs/x-console-manual/` | 运营操作手册（Markdown + Word docx + 截图 + 构建脚本） | — |

## 铁律

1. **秘密永不进仓库**：密码 / token / 密钥 / OAuth 凭据只存在于服务器 env 文件（mode 0600）和飞书【敏感】清单。仓库里只允许 `*.example` 占位文件。
2. **数据不进仓库**：`*.db` / `data/` / 日志一律 gitignore；数据靠 SQLite 定时备份。
3. **提交前扫描**：任何新增文件先过 secret 扫描（见下）。
4. 浏览 worker 严格只读；全局写入暂停不会被自动恢复。

## 快速验证

```bash
# console 测试（从仓库根目录）
cd x-automation/tests && python3 -m unittest test_ui_static_contract test_workflow test_oauth_bff

# write-service 测试
cd x-automation/write-service/tests && python3 -m unittest discover -p "test_*.py"

# secret 扫描（提交前）
grep -rInE "(password|secret|token|api[_-]?key)" . --include="*.py" --include="*.json" | grep -vE "example|test_|getenv|environ|placeholder|REPLACE"
```

## 部署原则

- 改动在仓库提交 → 用 `x-automation/deploy/` 的暂停态脚本部署 → 验证安全不变量（global_write_paused、无活跃任务、无 secret 回显）→ 失败回滚备份。
- 生产机不做 `git pull` 式部署；用 scp 定点推文件，保持可回滚。

## 交接状态（2026-07-28）

- **仓库已公开**：https://github.com/paopao1111111/biz-ops —— 接收人直接打开链接即可访问，无需登录或邀请。
- **离职人个人服务器（124 / 8）离职后清空**：本仓库是唯一存活的事实源。所有「部署于 124 /opt/...」的描述，对接收方而言 = 代码在仓内，按 `HANDOVER.md` 自行部署到贵方服务器。
- **秘密不在仓库**：凭据见飞书【敏感】账号密码清单。控制台管理员密码此前在聊天中泄露，**交接前必须轮换**。
- **待办**：
  1. 代理链修复（住宅代理续费 + 闪电机场新订阅到手后，按 `infra/runbooks/proxy-chain.md` 更新 Mac/Win xray config）。
  2. X Developer Console credits 充值后，重新授权 9 个 OAuth2 账号（media.write）再启用自动发布。
  3. 评论配额护栏（5 条/号/天、≥15 分钟）待实现。
  4. 建议离职前把仓库 owner 转给公司/接收人，避免个人账号失效后无人维护。
