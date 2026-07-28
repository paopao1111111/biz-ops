# 代理链排障 Runbook（AdsPower 链式代理）

> 2026-07-28 实战总结：症状"指纹浏览器代理总是出错 / 延迟全 -1 / nssFailure2"，根因是机场换域名导致节点清单过期 + 住宅代理凭据失效。

## 链路结构

```
AdsPower Profile ──> 127.0.0.1:1081x（xray mixed inbound，一账号一端口）
                   ──> ads-chain-out-N（SOCKS，绑定固定住宅代理 IP:62000，一账号一固定出口）
                   ──> x.com
主出口 10808（mixed inbound）──> trojan 机场节点（日常浏览用）
```

- 每台机器（Mac / Windows）各跑一个 xray，配置：Mac `~/Library/Application Support/acc-rpa/mac-xray-chains/config.json`；Windows `C:\acc-rpa\v2ray-config\config.json`（计划任务 `ACC RPA Xray`）。
- 端口↔账号↔出口 IP 映射见 `infra/proxy/chain-mapping.example.json`。

## 分层定位法（按顺序，每层都有判据）

| 层 | 测试 | 判据 |
|---|---|---|
| 1. xray 进程 | `tasklist`/`lsof` 看 xray 在跑、端口在 LISTEN | 不在 → 起计划任务/进程 |
| 2. 本地直连 | `curl https://example.com`（不走代理） | 不通 → 本机网络问题 |
| 3. 上游 TCP | `Test-NetConnection <住宅IP> -Port 62000` | False → 节点死或本机出口被封 |
| 4. 链式转发 | `curl -x socks5://127.0.0.1:10810 https://ipinfo.io` | 见下"症状对照" |
| 5. 凭据直连 | 绕过 xray：`curl -x socks5://USER:PASS@住宅IP:62000 https://x.com` | `User was rejected` → **凭据死**（到期/轮换） |
| 6. 订阅新鲜度 | 拉机场订阅 URL | NXDOMAIN/非 200 → 订阅域名过期，去防失联页找新域名 |

## 症状对照（都是实测过的坑）

- **`SOCKS5 request granted` 但 TLS 失败**： granted 是**本地 xray 自回 ACK**，不代表上游认证过了。必须绕过 xray 直连测（第 5 层）。
- **控制台 `proxy_status=ok`**：可能只是**陈旧缓存**（看 `last_proxy_check_at` 时间戳），不等于现在通。
- **nssFailure2（Firefox）/ schannel failed to receive handshake**：链接受了但转发被断，优先怀疑凭据死。
- **全部节点同时死**：别先怪节点，查**订阅域名**（机场换域名时旧清单全废）。闪电机场防失联页 `sd.369.cyou`。
- **住宅代理和机场同时死**：多半同一家买的，一起到期/轮换。

## 修复流程

1. 机场：开防失联页 → 新官网 → 登录 → 复制新订阅链接 → v2rayN 更新订阅 → 验证节点活
2. 住宅代理：问卖家账号状态（是否到期/被停）→ 拿新凭据（可能连 IP 一起换）
3. 把活的上游写回两处 xray config 的 outbounds（结构不动，只换 address/port/user/pass）
4. 重启 xray（Windows：`schtasks /end /tn "ACC RPA Xray"` 后 `/run`；Mac：kill 后重起）
5. 验收：每个端口 `curl -x socks5://127.0.0.1:1081x https://ipinfo.io` 返回**该账号绑定的**住宅 IP
6. 恢复控制台全局排程（修链期间建议暂停，避免刷失败记录）

## 注意

- 凭据、订阅 token 永远不进仓库、不进聊天；走文件传递或飞书敏感清单。
- 改配置前先 `cp config.json config.json.bak_$(date +%s)`。
