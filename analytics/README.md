# analytics（124 /opt/iweaver-monitoring + /opt/multisite-dashboard）

- `iweaver-monitoring/`：iWeaver 监控（窗口告警、日报、case 分析每日任务；units `iweaver-monitor-*`、`iweaver-case-analysis-daily`、`dashboard-metrics-*`）
- `multisite-dashboard/`：多站点周报面板（iWeaver/Palmly/Learning Coach；units `multisite-weekly-*`、`dashboard-web`）
- DB（指标存储）在服务器数据目录，定时备份，不入仓。
