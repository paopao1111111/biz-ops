"""Web server for viewing and downloading SEO reports."""

import os
from datetime import datetime
from pathlib import Path

import markdown
from flask import Flask, abort, render_template_string, send_file

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/srv/cloudcli-workspaces/default/agentos_mcp_orchestrator_transfer/storage/reports"))
PORT = int(os.getenv("REPORT_SERVER_PORT", "8320"))

app = Flask(__name__)

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>竞品 SEO 调研报告</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b1020;color:#e5e7eb;line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:24px}
header{padding:24px 0;border-bottom:1px solid #1f2a44;margin-bottom:24px;display:flex;justify-content:space-between;align-items:center}
h1{font-size:24px;font-weight:700}
.badge{background:#2563eb;color:#fff;padding:4px 12px;border-radius:20px;font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:16px}
th,td{padding:12px 16px;text-align:left;border-bottom:1px solid #1f2a44}
th{background:#111827;color:#94a3b8;font-weight:600;font-size:13px;text-transform:uppercase}
tr:hover{background:#111827}
a{color:#93c5fd;text-decoration:none}
a:hover{text-decoration:underline}
.btn{display:inline-block;padding:8px 16px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;border:none}
.btn-primary{background:#2563eb;color:#fff}
.btn-secondary{background:#1f2a44;color:#e5e7eb;border:1px solid #334155}
.empty{text-align:center;padding:48px;color:#64748b}
.size{color:#64748b;font-size:13px}
.report-body{background:#0f172a;border:1px solid #1f2a44;border-radius:12px;padding:32px;margin-top:24px}
.report-body h1,.report-body h2,.report-body h3{color:#e5e7eb;margin-top:24px;margin-bottom:12px}
.report-body h1{font-size:28px;border-bottom:1px solid #1f2a44;padding-bottom:12px}
.report-body h2{font-size:22px}
.report-body h3{font-size:18px}
.report-body p{margin-bottom:12px}
.report-body ul,.report-body ol{margin:12px 0 12px 24px}
.report-body li{margin-bottom:6px}
.report-body table{margin:16px 0}
.report-body code{background:#1e293b;padding:2px 6px;border-radius:4px;font-size:13px}
.report-body pre{background:#1e293b;padding:16px;border-radius:8px;overflow-x:auto;margin:16px 0}
.report-body blockquote{border-left:4px solid #2563eb;padding-left:16px;margin:16px 0;color:#94a3b8}
.back{margin-bottom:16px}
</style>
</head>
<body>
<div class="container">
<header>
<h1>竞品 SEO 调研报告</h1>
<span class="badge">{{ count }} 份报告</span>
</header>
{% if reports %}
<table>
<thead><tr><th>报告名称</th><th>生成时间</th><th>大小</th><th>操作</th></tr></thead>
<tbody>
{% for r in reports %}
<tr>
<td><a href="/report/{{ r.name }}">{{ r.name }}</a></td>
<td>{{ r.mtime }}</td>
<td class="size">{{ r.size }}</td>
<td><a class="btn btn-secondary" href="/download/{{ r.name }}">下载 .md</a></td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<div class="empty">暂无报告。运行 competitor_seo_analyze 后报告会自动出现在这里。</div>
{% endif %}
</div>
</body>
</html>"""

REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ name }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b1020;color:#e5e7eb;line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:24px}
header{padding:24px 0;border-bottom:1px solid #1f2a44;margin-bottom:24px;display:flex;justify-content:space-between;align-items:center}
h1{font-size:24px;font-weight:700}
a{color:#93c5fd;text-decoration:none}
.btn{display:inline-block;padding:8px 16px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;border:none}
.btn-primary{background:#2563eb;color:#fff}
.btn-secondary{background:#1f2a44;color:#e5e7eb;border:1px solid #334155}
.report-body{background:#0f172a;border:1px solid #1f2a44;border-radius:12px;padding:32px;margin-top:24px}
.report-body h1,.report-body h2,.report-body h3{color:#e5e7eb;margin-top:24px;margin-bottom:12px}
.report-body h1{font-size:28px;border-bottom:1px solid #1f2a44;padding-bottom:12px}
.report-body h2{font-size:22px}
.report-body h3{font-size:18px}
.report-body p{margin-bottom:12px}
.report-body ul,.report-body ol{margin:12px 0 12px 24px}
.report-body li{margin-bottom:6px}
.report-body table{width:100%;border-collapse:collapse;margin:16px 0}
.report-body th,.report-body td{padding:10px 14px;text-align:left;border-bottom:1px solid #1f2a44}
.report-body th{background:#111827;color:#94a3b8;font-weight:600}
.report-body code{background:#1e293b;padding:2px 6px;border-radius:4px;font-size:13px}
.report-body pre{background:#1e293b;padding:16px;border-radius:8px;overflow-x:auto;margin:16px 0}
.report-body blockquote{border-left:4px solid #2563eb;padding-left:16px;margin:16px 0;color:#94a3b8}
.actions{display:flex;gap:12px;align-items:center}
</style>
</head>
<body>
<div class="container">
<header>
<div class="actions">
<a href="/">&larr; 返回列表</a>
<h1>{{ name }}</h1>
</div>
<a class="btn btn-primary" href="/download/{{ name }}">下载 .md</a>
</header>
<div class="report-body">{{ content|safe }}</div>
</div>
</body>
</html>"""


def _list_reports():
    if not REPORTS_DIR.exists():
        return []
    reports = []
    for f in sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        reports.append({
            "name": f.name,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size": f"{stat.st_size // 1024}KB" if stat.st_size > 1024 else f"{stat.st_size}B",
        })
    return reports


@app.route("/")
def index():
    reports = _list_reports()
    return render_template_string(TEMPLATE, reports=reports, count=len(reports))


@app.route("/report/<name>")
def view_report(name):
    path = REPORTS_DIR / name
    if not path.exists() or not path.suffix == ".md":
        abort(404)
    content = path.read_text(encoding="utf-8")
    html = markdown.markdown(content, extensions=["tables", "fenced_code"])
    return render_template_string(REPORT_TEMPLATE, name=name, content=html)


@app.route("/download/<name>")
def download_report(name):
    path = REPORTS_DIR / name
    if not path.exists() or not path.suffix == ".md":
        abort(404)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
