# 交接重建指南（拿着仓库从零恢复全部服务）

本文件回答："如果服务器/电脑全丢，如何用本仓库 + 秘密清单重建一切？"
秘密（密码/token/密钥）在飞书【敏感】账号密码清单；本仓库不含任何秘密。

## 环境矩阵

| 组件 | 机器 | 运行时 | 依赖 | 数据 | 秘密来源 |
|---|---|---|---|---|---|
| X 浏览控制台 | 124 | Python 3.11（**注意 124 系统 Python 是 3.6.8，必须用 3.11**） | 纯 stdlib，无 pip 依赖 | `/opt/x-browse-console/data/console.db`（SQLite WAL） | `/etc/x-browse-console.env`（0600） |
| X 写入服务 | 124 | Python 3.11 | `x-automation/write-service/requirements.txt` | `/var/lib/x-write-service/write.db` | `/etc/x-write-service.json`（640 root:x-write）+ `/etc/x-write-service.secrets.json`（0600，重建时手填） |
| Windows 浏览 worker | Windows 10+（mint 用户） | Python 3.x + PowerShell | AdsPower（Local API 50325）+ geckodriver + xray（v2rayN 内置） | 无（无状态） | `C:\x-browse-console-worker\worker.json`（0600 等效） |
| 代理链 | Mac + Windows | xray | v2rayN 内置 xray 二进制 | 无 | 住宅代理凭据 + 机场订阅（飞书敏感清单 / 机场面板） |
| LLM 网关 | 124 | Docker `new-api`（8318） | 已有容器 | new-api 容器卷 | `WORKFLOW_LLM_KEY`（= New API token） |
| 飞书记录 | — | — | 飞书开放平台自建应用 | 电子表格 KezXs1UF8hXcdYtH8lechX8AnKf（评论 P4Xcs / 发帖 PpGgg） | `FEISHU_APP_ID/SECRET` |

## 从零重建步骤

### 1. X 浏览控制台（124）
```bash
# 仓库 x-automation/console/ → /opt/x-browse-console/
# 1) 建用户与目录：useradd x-browse-console；mkdir -p /opt/x-browse-console/{data,backups}
# 2) scp console/ 全部文件到 /opt/x-browse-console/
# 3) 写 /etc/x-browse-console.env（照 controller/x-browse-console.env.example 填，值从飞书敏感清单取）chmod 600
# 4) 照 controller/x-browse-console.service.template 建 systemd unit，WorkingDirectory=/opt/x-browse-console
# 5) systemctl enable --now x-browse-console；curl 127.0.0.1:8790/login 应 200
# 6) 恢复数据：把最近备份的 console.db 放回 data/（无备份则空库启动，账号需重新种子）
```

### 2. X 写入服务（124）
```bash
# 仓库 x-automation/write-service/ → /opt/x-write-service/
# 1) useradd x-write；mkdir -p /var/lib/x-write-service
# 2) pip install -r requirements.txt（python3.11 -m venv 建议）
# 3) 照 x_write.example.json 写 /etc/x-write-service.json（hmac_secret 随机生成 32+ 字节）chmod 640 root:x-write
# 4) secrets.json 重建：先建空 {}（0600），OAuth 凭据走控制台「写入操作」页逐个重新授权注入，**不手工填**
# 5) systemd 照 systemd/ 模板；默认 global_write_paused=true，逐账号人工启用
```

### 3. Windows worker
```powershell
# 仓库 x-automation/worker/ → C:\x-browse-console-worker\
# 1) 装 AdsPower 并开 Local API（默认 127.0.0.1:50325）
# 2) 照 worker.example.production.json 写 worker.json（worker_secret 从飞书敏感清单）
# 3) 管理员 PowerShell 跑 install-worker.ps1（装 geckodriver + 建计划任务）
# 4) 导入 X-Browse-Console-Worker.scheduled-task.xml 核对计划任务
```

### 4. 代理链（Mac + Windows）
见 `infra/runbooks/proxy-chain.md`。配置示例在 `infra/proxy/`（已脱敏，凭据位 REPLACE）。
关键认知：**住宅代理凭据和机场订阅会过期/轮换，属于消耗品，不在仓库**；链式结构（端口→出口映射）在仓库。

### 5. LLM 网关与飞书
- New API 容器已跑在 124（8318）；`WORKFLOW_LLM_KEY` 用其 token，写进 console env。
- 飞书自建应用 ID/SECRET 从敏感清单取；记录表结构见 `x-automation/console/feishu_records.py` 头部注释。

## 验证清单（交接验收用）

- [ ] `curl 127.0.0.1:8790/login` → 200；控制台能登录，概览有数据
- [ ] `systemctl is-active x-write-service` → active；`curl 127.0.0.1:8791/healthz`（带 HMAC）→ ok
- [ ] write-service `global_write_paused=true`；所有账号 disabled
- [ ] Windows worker 心跳出现在控制台概览；3 槽位可用
- [ ] 代理链：每个端口 `curl -x socks5://127.0.0.1:1081x https://ipinfo.io` 返回对应住宅 IP
- [ ] 仓库 secret 扫描无命中
