"""LLM-based feedback analyzer with FAQ matching"""
import json
import logging
from lib.llm_client import call_llm

logger = logging.getLogger(__name__)

FAQ_KNOWLEDGE = """
1. 【关于取消订阅】操作路径：点击页面左下角的账户图标 → 进入 Billing → 选择 取消订阅
2. 【取消订阅已处理】已取消订阅，不会再扣费
3. 【关于帐户点数的解释】点数用于大文档处理，每月5000点，每页约10-50点
4. 【要求退款】退款7-15个工作日到账，会员权益保留
5. 【关于Bug反馈】团队正在审核，将尽快修复
6. 【Bug修复后的跟进】问题已解决，清除浏览器缓存或换Chrome重试
7. 【验证码不显示的问题】清除浏览器缓存，换Chrome浏览器
8. 【新需求反馈】已转发给产品团队审核考量
9. 【需求上线后跟进】功能已上线，可以试用
10. 【询问折扣码】20%学生折扣（学校邮箱），10%合作伙伴折扣
11. 【删除账户】Settings → Account → Delete Account
12. 【问题不清晰需截图】请提供截图帮助确认问题
13. 【Bug修复并赠送会员】问题已解决，赠送15天Pro会员
14. 【多账号登录问题】Google登录和邮箱登录是两个账号
15. 【开发票问题】请提供：公司名、VAT号、地址、邮箱
16. 【收到开票信息后】5-7个工作日处理发票
"""

PROMPT_TEMPLATE = """你是 iWeaver 的客户服务助手。分析用户反馈，识别问题原因，匹配FAQ，生成邮件回复。

## FAQ知识库
{faq}

## 用户反馈
{input}

## 处理流程
1. 翻译（非中文→中文）
2. 原因识别（结合点赞/点踩和对话内容）
3. 问题总结（15字以内）
4. FAQ匹配（语义匹配，置信度：高/中/低/无）
5. 退费检测（涉及退款/取消订阅标记is_refund=true）
6. 生成邮件（匹配成功且点踩→基于FAQ回复；点赞→感谢邮件）

## 输出格式（纯JSON，不要markdown代码块）
{{
  "translation_zh": "中文翻译",
  "reason_category": "原因分类",
  "problem_summary": "15字以内问题总结",
  "matched_faq": "匹配到的FAQ" 或 null,
  "match_confidence": "高/中/低/无",
  "is_refund": true或false,
  "email_subject": "邮件标题",
  "email_body": "完整邮件正文",
  "feishu_summary": "一句话中文总结"
}}
"""


def analyze_feedback(feedback_data):
    """Analyze user feedback using LLM with FAQ matching"""
    try:
        input_text = json.dumps(feedback_data, ensure_ascii=False, indent=2)
        prompt = PROMPT_TEMPLATE.format(faq=FAQ_KNOWLEDGE, input=input_text)

        result = call_llm(prompt, max_tokens=2000, temperature=0.3)
        if not result.get('success'):
            logger.warning(f'LLM failed, using manual-review fallback: {result.get("error")}')
            return {
                "translation_zh": "",
                "reason_category": "LLM分析失败",
                "problem_summary": "需人工查看",
                "matched_faq": None,
                "match_confidence": "无",
                "is_refund": False,
                "email_subject": "",
                "email_body": "",
                "feishu_summary": "LLM分析失败，请人工查看原始反馈"
            }

        content = result['output']

        # Extract JSON from possible markdown code blocks
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content:
            content = content.split('```')[1].split('```')[0].strip()

        parsed = json.loads(content)
        logger.info(f'Analysis done: {parsed.get("problem_summary", "N/A")}')
        return parsed

    except Exception as e:
        logger.error(f'Analysis error: {e}')
        return None
