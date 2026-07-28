# edm-system（124 /opt/edm-system，systemd `edm-auto-reply` / `feedback-autoreply` / `mihomo-edm`）

邮件自动回复系统（客服 FAQ 自动应答 + 反馈通知过滤）。units 在 `infra/systemd/`。
- 知识库/FAQ 数据与邮箱凭据在服务器 `.env` 与数据目录（不入仓）。
- 出口代理走 mihomo-edm（配置只在服务器）。
- 排障：见各次修复记录（HTML 解析、多语言、退款意图、通知过滤）。
