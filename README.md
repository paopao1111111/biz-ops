# biz-ops

业务自动化系统的统一代码仓库（私有）。**本仓库是唯一事实源**——所有改动先提交到这里，再从仓库部署到服务器。

## 目录

| 目录 | 内容 | 部署位置 |
|---|---|---|
| `x-automation/console/` | X 浏览控制台（Flask 风格 stdlib HTTP 服务、工作流 LLM、飞书记录） | 124 `/opt/x-browse-console`（systemd `x-browse-console`，端口 8790） |
| `x-automation/worker/` | Windows 只读浏览 worker（AdsPower + geckodriver，3 并发槽） | Windows `C:\x-browse-console-worker`（计划任务 `X Browse Console Worker`） |
| `x-automation/write-service/` | X 官方 API 写入服务（草稿→冻结审批→执行→审计状态机、媒体上传、定时发布） | 124 `/opt/x-write-service`（systemd `x-write-service`，loopback 8791） |
| `x-automation/deploy/` | 暂停态部署脚本（备份→停服→scp→起服→验证不变量） | 从 Mac 运行 |
| `x-automation/tests/` | console 侧全部测试（UI 静态合约、工作流、OAuth BFF、生命周期） | 本地跑 |
| `infra/runbooks/` | 排障手册（代理链、HTTPS 网关、X credits 等） | — |

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
