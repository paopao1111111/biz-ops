from pathlib import Path
import re
import unittest

ROOT = Path("/private/tmp/x-browse-v2-staging/controller")
INDEX = (ROOT / "templates/index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "templates/login.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/app.css").read_text(encoding="utf-8")
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class UIStaticContractTest(unittest.TestCase):
    def test_true_four_view_information_architecture(self):
        for view in ("overview", "accounts", "runs", "write", "workflow", "postflow", "system"):
            self.assertIn(f'id="view-{view}"', INDEX)
            self.assertIn(f'data-view="{view}"', INDEX)
            self.assertIn(f'href="#{view}"', INDEX)
        self.assertNotIn('id="live"', INDEX)
        self.assertNotIn('id="history"', INDEX)
        self.assertNotIn('id="safety"', INDEX)

    def test_workflow_view_wiring(self):
        self.assertIn('id="view-workflow"', INDEX)
        self.assertIn('"overview","accounts","runs","write","workflow","postflow","system"', JS)
        self.assertIn('loadWorkflow', JS)
        self.assertIn('data-wf-action="approve"', JS)
        self.assertIn('data-wf-action="draft"', JS)
        self.assertIn('/api/x/workflow/candidates', JS)

    def test_postflow_view_wiring(self):
        self.assertIn('id="view-postflow"', INDEX)
        self.assertIn('loadPostflow', JS)
        self.assertIn('data-pf-action="generate"', JS)
        self.assertIn('data-pf-action="copy"', JS)
        self.assertIn('data-pf-action="manual_sent"', JS)
        self.assertIn('/api/x/postflow/topics', JS)
        self.assertIn('/api/x/postflow/summarize', JS)

    def test_required_status_and_drawer_ids(self):
        required = (
            "globalScheduleButton", "scheduleState", "workerState", "workloadState",
            "lastSync", "attentionList", "accountsBody",
            "runsBody", "systemGrid", "accountDrawer", "runDrawer",
            "settingsForm", "confirmDialog",
        )
        combined = INDEX + JS
        for value in required:
            self.assertIn(value, combined)

    def test_old_generated_aesthetic_is_removed(self):
        banned = (
            "CONTROL CENTER", "TODAY AT A GLANCE", "PROFILE OPERATIONS",
            "LIVE EXECUTION", "RECENT ACTIVITY", "READ-ONLY GUARANTEE",
            "IWEAVER OPERATIONS", "iWeaver-style operations", "brand-mark",
            "eyebrow", "radial-gradient", "linear-gradient", "backdrop-filter",
            "glass", "glow", "Safety card", "3 slots", "Slot 1/3",
        )
        combined = INDEX + LOGIN + CSS + JS
        for value in banned:
            self.assertNotIn(value, combined)
        self.assertNotRegex(CSS, r"\.progress-(?:[0-9]|100)\b")

    def test_security_and_forbidden_actions(self):
        forbidden_ui = (
            "force-kill", "force_kill", "auto-login", "auto_login",
            "bulk browser kill", "bulk-browser-kill", "resume-global-schedule",
            'data-action="like"', 'data-action="reply"', 'data-action="follow"',
            'data-action="repost"', 'data-action="post"',
        )
        for value in forbidden_ui:
            self.assertNotIn(value, INDEX + JS)
        self.assertIn('hostname==="x.com"', JS)
        self.assertIn('X-CSRF-Token', JS)
        self.assertIn('response.status===401', JS)
        self.assertIn('const esc=', JS)

    def test_exact_v13_endpoints_and_server_capabilities(self):
        self.assertIn('/configuration`', JS)
        self.assertIn('settings:{', JS)
        self.assertIn('keywords:{keywords,selected_count:selected}', JS)
        self.assertIn('/api/x/jobs/${encodeURIComponent(job)}/${action}', JS)
        self.assertIn('/api/x/runs/${encodeURIComponent(id)}', JS)
        self.assertIn('/api/x/system-info', JS)
        self.assertIn('a.account_capabilities||a.capabilities', JS)
        self.assertNotIn('/settings`', JS)
        self.assertNotIn('/keywords`', JS)

    def test_workload_is_authoritative_with_fallback_only(self):
        self.assertIn('if(data.workload&&typeof data.workload==="object")return data.workload', JS)
        self.assertIn('wl=workload(d)', JS)
        self.assertNotIn("3 slots available", JS)
        self.assertNotIn("3 个并发槽位", JS)
        self.assertNotRegex(JS, r"Slot\s*\$\{")

    def test_no_inline_script_or_style(self):
        self.assertNotRegex(INDEX, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(LOGIN, r"<script")
        self.assertNotRegex(INDEX + LOGIN, r"\sstyle\s*=")
        self.assertNotRegex(INDEX + LOGIN, r"<style\b")

    def test_native_progress_is_used(self):
        self.assertIn("<progress", JS)
        self.assertNotIn("data-progress", JS)

    def test_precise_status_codes_have_chinese_labels(self):
        codes = (
            "manual_profile_in_use", "webdriver_frozen", "no_forward_progress",
            "hard_runtime_exceeded", "browser_start_failed", "adspower_unavailable",
            "controller_unavailable", "browser_crashed", "lease_expired",
            "cleanup_uncertain", "handle_mismatch", "authentication", "source",
            "dom", "network", "proxy",
        )
        for code in codes:
            self.assertRegex(JS, rf'{code}:"[^"_]+"')

    def test_attention_deduplicates_and_uses_operator_copy(self):
        self.assertIn('function issueKey(i)', JS)
        self.assertIn('return`account:${i.account_id}:${code}`', JS)
        self.assertIn('seen=new Map()', JS)
        self.assertIn('function issueMessage(i)', JS)
        self.assertIn('账号尚未完成手动登录或验证，自动浏览保持阻塞。', JS)
        self.assertIn('${esc(issueMessage(i))}', JS)
        self.assertNotIn('const key=[i.code,i.account_id,i.job_id,i.profile_id,i.message]', JS)

    def test_reconciled_profile_seeds_are_preserved(self):
        self.assertNotIn('"k1euoamd"', APP)
        self.assertNotIn('@lkerrvjb', APP)
        self.assertIn('"k1f2qx1l","Earl Leedy","@EarlLeedy3"', APP)

    def test_write_view_structure_and_isolation_copy(self):
        self.assertIn('id="view-write"', INDEX)
        self.assertIn('data-view="write"', INDEX)
        self.assertIn('href="#write"', INDEX)
        self.assertIn('浏览严格只读', INDEX)
        self.assertIn('浏览 Worker 明确禁止的交互', INDEX)
        self.assertIn('官方 X API 写入服务', INDEX)
        self.assertIn('"write","workflow","postflow","system"', JS)

    def test_write_actions_use_isolated_namespace(self):
        self.assertIn('data-write-action="create-draft"', JS)
        self.assertIn('e.target.closest("[data-write-action]")', JS)
        for verb in ('data-action="like"', 'data-action="repost"', 'data-action="post"'):
            self.assertNotIn(verb, INDEX + JS)

    def test_write_approval_requires_frozen_hash_and_version(self):
        self.assertIn('{content_hash:d.content_hash,request_version:d.version}', JS)
        self.assertIn('{content_hash:r.content_hash,request_version:r.version}', JS)
        self.assertIn('批准此精确请求', JS)
        self.assertIn('/api/x-write/requests/${writeState.draft.id}/submit', JS)
        self.assertIn('/api/x-write/operations/${id}/approve-next-step', JS)
        self.assertIn('不会重新发送，只记录对账结论', JS)

    def test_write_pause_is_separate_from_browse_schedule(self):
        self.assertIn('/api/x-write/global/${resume?"resume":"pause"}', JS)
        self.assertIn('与浏览排程互不影响', JS)
        self.assertIn('浏览功能不受影响', JS)
        self.assertIn('/api/x/schedule/resume', JS)

    def test_write_account_onboarding_uses_oauth_without_server_file_fields(self):
        self.assertIn('/api/x-write/credential-refs', JS)
        self.assertIn('/api/x-write/oauth/start', JS)
        self.assertIn('/api/x-write/oauth/app', JS)
        self.assertIn('/api/x-write/credentials/oauth1', JS)
        self.assertIn('data-oauth-connect-field="source_profile_id"', JS)
        self.assertIn('id="oauthAppDialog"', INDEX)
        self.assertIn('id="oauth1Dialog"', INDEX)
        self.assertNotIn('data-write-add-field="account_key"', JS)
        self.assertNotIn('/etc/x-write-service.secrets.json', INDEX + JS)

    def test_write_bff_never_reaches_worker_or_readonly_gates(self):
        self.assertIn('def xwrite(self,method,suffix,body):', APP)
        self.assertIn('X_CONSOLE_XWRITE_SECRET', APP)
        self.assertIn('if p.startswith("/api/x-write/"):return self.xwrite("GET",p[12:],None)', APP)
        self.assertIn('if p.startswith("/api/x-write/"):return self.xwrite("POST",p[12:],b)', APP)
        xwrite_body = APP.split('def xwrite(self,method,suffix,body):', 1)[1].split('def do_GET', 1)[0]
        self.assertNotIn('/api/worker', xwrite_body)
        self.assertIn('def readonly_assert(value,path="snapshot"):', APP)
        self.assertIn('"click","like","follow","reply","post","retweet","repost"', APP)
        worker = Path("/private/tmp/x-browse-v2-staging/worker/worker.py").read_text(encoding="utf-8")
        self.assertIn("contains_forbidden_action", worker)
        self.assertIn('snapshot.get("read_only") is not True', worker)

    def test_disabled_browse_menu_is_inert_and_explained(self):
        self.assertIn('details class="duration-menu ${browse.allowed&&!busy?"":"disabled"}', JS)
        self.assertIn('data-disabled=\\"true\\"', JS)
        self.assertIn('aria-disabled="${browse.allowed&&!busy?"false":"true"}', JS)
        self.assertIn('details.duration-menu[data-disabled=', JS)
        self.assertIn('浏览不可用：', JS)
        self.assertIn('summary[aria-disabled="true"]', CSS)

    def test_runtime_does_not_assign_inline_styles(self):
        self.assertNotRegex(JS, r"\.style(?:\.|\[|\s*=)")
        self.assertNotRegex(JS, r"setAttribute\(\s*['\"]style")
        self.assertIn('document.body.classList.add("drawer-open")', JS)
        self.assertIn('body.drawer-open{overflow:hidden}', CSS)

    def test_heartbeat_focus_and_cleanup_copy(self):
        self.assertIn('function relativeTime(', JS)
        self.assertIn('workerDetail").title=', JS)
        self.assertIn('function containDrawerFocus(', JS)
        self.assertIn('event.shiftKey', JS)
        self.assertIn('<dt>清理确认</dt>', JS)
        self.assertNotIn('<dt>预算确认</dt>', JS)

    def test_primary_lists_use_concise_failure_labels(self):
        self.assertIn('function accountIssue(a){return a.current_issue?textStatus(a.current_issue.code):"—";}', JS)
        self.assertIn('function runSummary(r){const code=', JS)
        self.assertNotIn('function runSummary(r){return r.failure_detail||r.error', JS)
        self.assertIn('<section class="drawer-section technical">', JS)
        self.assertIn('r.failure_detail||r.error||r.quarantine_reason', JS)


if __name__ == "__main__":
    unittest.main()
