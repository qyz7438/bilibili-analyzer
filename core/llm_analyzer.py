"""
LLM 分析模块
调用本地/远程 LLM API 分析视频内容
"""
import json
import logging
import time
import re
from typing import Dict, Any, List, Optional

import requests

from utils.helpers import resolve_llm_url

logger = logging.getLogger("bilibili-analyzer")


def _extract_links_from_video_data(video_data: Dict[str, Any]) -> List[str]:
    """
    从视频简介、评论区、弹幕中抽取所有 GitHub/GitLab 相关链接。
    返回去重后的链接列表。
    """
    info = video_data.get("info", {})
    comments = video_data.get("comments", [])
    danmaku = video_data.get("danmaku", [])

    text_parts = [
        info.get("description", ""),
        info.get("raw_desc", ""),
    ]
    text_parts.extend(info.get("description_urls", []))
    for c in comments:
        text_parts.append(c.get("message", ""))
    for d in danmaku:
        text_parts.append(d.get("text", ""))

    all_text = "\n".join(text_parts)
    return _extract_github_links(all_text)


def _extract_all_urls_from_video_data(video_data: Dict[str, Any]) -> List[str]:
    """从视频数据中提取所有 URL，供 LLM 进一步判断相关项目"""
    info = video_data.get("info", {})
    comments = video_data.get("comments", [])
    danmaku = video_data.get("danmaku", [])

    text_parts = [
        info.get("description", ""),
        info.get("raw_desc", ""),
    ]
    text_parts.extend(info.get("description_urls", []))
    for c in comments:
        text_parts.append(c.get("message", ""))
    for d in danmaku:
        text_parts.append(d.get("text", ""))

    return _extract_urls("\n".join(text_parts))


# 内联简单的 URL/GitHub 链接提取规则，避免依赖不存在的 core.transcriber 模块
_URL_RE = re.compile(r"https?://[^\s<>\"\']+", re.IGNORECASE)
# 捕获完整 URL（而不仅是域名），确保返回可点击的项目链接
_GH_RE = re.compile(r"(https?://(?:www\.)?(?:github\.com|gitlab\.com)/[^\s<>\"\']+)", re.IGNORECASE)


def _extract_urls(text: str) -> List[str]:
    return list(dict.fromkeys(_URL_RE.findall(text)))


def _extract_github_links(text: str) -> List[str]:
    return list(dict.fromkeys(_GH_RE.findall(text)))


def _summarize_transcript(transcript_data: Dict[str, Any], max_chars: int = 3000) -> str:
    """
    对转录文本做摘要。若 full_text 较短则直接返回；
    否则按时间段拼接前 N 段作为代表性摘要，避免 prompt 过长。
    """
    full_text = transcript_data.get("full_text", "")
    if not full_text:
        return ""
    if len(full_text) <= max_chars:
        return full_text
    # 取前 max_chars 个字符，截断到最近的换行
    truncated = full_text[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars * 0.8:
        truncated = truncated[:last_nl]
    return truncated + "\n...（转录文本已截断）"


class LLMAnalyzer:
    """LLM 分析器"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4000/v1",
        model: str = "kimi-code",
        api_key: str = "",
        timeout: int = 120,
        max_retries: int = 3,
    ):
        self.base_url = resolve_llm_url(base_url)
        self.model = model
        self.api_key = api_key or "sk-dummy"
        self.timeout = timeout
        self.max_retries = max_retries

    def _call_api(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        fallback_models: Optional[List[str]] = None,
    ) -> str:
        """调用 LLM API，支持 fallback 模型和参数"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        fallback_models = fallback_models or ["kimi-k2.7-code", "deepseek-v4-pro", "gpt-5.5"]
        models_to_try = [self.model] + [m for m in fallback_models if m != self.model]

        last_error = None
        for model_idx, model in enumerate(models_to_try):
            for attempt in range(self.max_retries):
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 4096,
                }
                try:
                    logger.info(f"调用 LLM API model={model} (尝试 {attempt + 1}/{self.max_retries})")
                    resp = requests.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.timeout,
                    )
                    if resp.status_code >= 400:
                        # 记录详细错误响应，便于诊断
                        err_text = resp.text[:2000]
                        logger.error(f"HTTP {resp.status_code}: {err_text}")
                        # 400 时尝试不带 max_tokens 的 fallback
                        if resp.status_code == 400 and attempt == 0:
                            payload.pop("max_tokens", None)
                            logger.info("尝试移除 max_tokens 重试")
                            resp2 = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                            if resp2.status_code < 400:
                                data = resp2.json()
                                return data["choices"][0]["message"]["content"]
                            else:
                                logger.error(f"移除 max_tokens 后仍失败: {resp2.status_code} {resp2.text[:500]}")
                        resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
                except requests.exceptions.ConnectionError as e:
                    logger.warning(f"连接失败: {e}")
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        last_error = e
                except requests.exceptions.Timeout:
                    logger.warning("请求超时")
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
                    else:
                        last_error = Exception("timeout")
                except Exception as e:
                    logger.error(f"API 调用失败: {e}")
                    last_error = e
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
        if last_error:
            raise last_error
        return ""

    def analyze_video(
        self,
        video_data: Dict[str, Any],
        transcript: Optional[Dict[str, Any]] = None,
        research_heavy: bool = False,
    ) -> Dict[str, Any]:
        """
        分析视频内容，生成结构化笔记。

        Args:
            video_data: 包含 info / danmaku / comments 的视频数据字典。
            transcript: 可选，AudioTranscriber.transcribe() 返回的转录结果。
            research_heavy: 若为 True，则按科研深度分析，生成不少于 1000 字的长摘要。
        """
        info = video_data.get("info", {})
        danmaku = video_data.get("danmaku", [])
        comments = video_data.get("comments", [])

        # 构建分析提示
        title = info.get("title", "")
        description = info.get("description", "")
        tags = info.get("tags", [])
        owner = info.get("owner", {})
        stat = info.get("stat", {})

        # 弹幕文本
        danmaku_texts = [d["text"] for d in danmaku[:100] if d.get("text")]
        danmaku_sample = "\n".join(danmaku_texts[:50])

        # 评论文本（保留原始消息和点赞数）
        comment_texts = []
        for c in comments[:50]:
            msg = c.get("message", "").strip()
            like = c.get("like", 0)
            if msg:
                comment_texts.append(f"[赞 {like}] {msg}")
        comment_sample = "\n".join(comment_texts[:30])

        # 转录文本摘要
        transcript_summary = ""
        transcript_section = ""
        if transcript and transcript.get("success"):
            transcript_summary = _summarize_transcript(transcript)
            transcript_section = f"""## 视频语音转录摘要
{transcript_summary}
"""

        # 从视频数据中提取项目链接
        github_links = _extract_links_from_video_data(video_data)
        all_urls = _extract_all_urls_from_video_data(video_data)
        url_section = ""
        if all_urls:
            url_section = "## 视频中发现的外部链接\n" + "\n".join(f"- {u}" for u in all_urls[:30]) + "\n"

        if research_heavy:
            summary_instruction = "视频内容深度摘要（不少于 1000 字，需涵盖研究背景、方法、实验结果、结论与启示，请基于描述、弹幕、评论和语音转录真实内容生成）"
            extra_fields = '"research_details": "详细研究内容拆解（方法、实验、结论）",\n  "innovation_points": ["创新点1", "创新点2"],\n  "limitations": ["局限性1", "局限性2"],\n  "future_directions": ["未来方向1", "未来方向2"],'
        else:
            summary_instruction = "视频内容摘要 (200字以内，请基于描述、弹幕、评论和语音转录真实内容生成)"
            extra_fields = ""

        prompt = f"""请分析以下 B站视频内容，生成结构化学习笔记。

## 视频信息
- 标题: {title}
- UP主: {owner.get('name', '')}
- 标签: {', '.join(tags)}
- 描述: {description[:800]}
- 播放量: {stat.get('view', 0)} | 弹幕: {stat.get('danmaku', 0)} | 点赞: {stat.get('like', 0)}

## 弹幕样本
{danmaku_sample}

## 评论样本
{comment_sample}

{transcript_section}

{url_section}

请生成以下结构化内容 (JSON 格式):
{{
  "summary": "{summary_instruction}",
  "key_points": ["核心知识点1", "核心知识点2", "核心知识点3"],
  "tags": ["标签1", "标签2", "标签3"],
  "sentiment": "整体情感倾向 (positive/neutral/negative)",
  "audience_level": "受众水平 (beginner/intermediate/advanced)",
  "learning_value": "学习价值评估 (1-10)",
  "related_topics": ["相关主题1", "相关主题2"],
  "action_items": ["建议行动1", "建议行动2"],
  "qa_pairs": [
    {{"question": "常见问题1", "answer": "答案1"}},
    {{"question": "常见问题2", "answer": "答案2"}}
  ],
  "github_links": ["从描述、评论、弹幕或转录中提取的 GitHub/GitLab 项目链接"],
  "transcript_summary": "用 2-3 句话概括转录内容（如果没有请写空字符串）",
  "related_projects": ["根据链接和上下文推断的相关开源项目名称或链接"]{',' + extra_fields if extra_fields else ''}
}}

只返回 JSON，不要其他文字。"""

        messages = [
            {"role": "system", "content": "你是一个专业的视频内容分析助手，擅长从视频信息、弹幕、评论和语音转录中提取知识要点，生成结构化的学习笔记。请优先基于真实视频内容生成摘要。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_api(messages, temperature=0.5)
            # 提取 JSON
            json_str = self._extract_json(response)
            analysis = json.loads(json_str)
            analysis["raw_response"] = response

            # 若 LLM 未返回 github_links，使用规则提取兜底
            if "github_links" not in analysis or not analysis["github_links"]:
                analysis["github_links"] = github_links
            else:
                # 合并规则提取结果与 LLM 返回结果，去重
                combined_links = list(dict.fromkeys(github_links + analysis["github_links"]))
                analysis["github_links"] = combined_links

            # 确保 transcript_summary 字段存在
            if "transcript_summary" not in analysis:
                analysis["transcript_summary"] = transcript_summary[:500] if transcript_summary else ""

            # 确保 related_projects 字段存在
            if "related_projects" not in analysis:
                analysis["related_projects"] = analysis.get("github_links", [])

            return analysis
        except Exception as e:
            logger.error(f"分析失败: {e}")
            # 保留最小可用的元信息，避免整篇笔记报废
            return {
                "summary": f"[LLM分析失败] {e}",
                "key_points": [
                    f"原始标题: {title}",
                    f"UP主: {owner.get('name', '')}",
                    f"标签: {', '.join(tags)[:200]}",
                ],
                "tags": tags or ["待分析"],
                "sentiment": "unknown",
                "audience_level": "unknown",
                "learning_value": 0,
                "related_topics": [],
                "action_items": ["请手动补充或重新触发分析"],
                "qa_pairs": [],
                "github_links": github_links,
                "transcript_summary": transcript_summary[:500] if transcript_summary else "",
                "related_projects": github_links,
                "error": str(e),
            }

    def generate_study_guide(self, video_data: Dict[str, Any], analysis: Dict[str, Any]) -> str:
        """生成学习指南文档"""
        info = video_data.get("info", {})
        title = info.get("title", "")
        tags = analysis.get("tags", [])
        key_points = analysis.get("key_points", [])
        qa_pairs = analysis.get("qa_pairs", [])
        github_links = analysis.get("github_links", [])
        transcript_summary = analysis.get("transcript_summary", "")

        prompt = f"""基于以下视频分析结果，生成一份详细的学习指南。

视频标题: {title}
标签: {', '.join(tags)}
核心知识点:
""" + "\n".join(f"- {p}" for p in key_points) + """

问答对:
""" + "\n".join(f"Q: {q['question']}\nA: {q['answer']}" for q in qa_pairs[:5]) + """

语音转录摘要:
{transcript_summary}

相关项目链接:
""" + "\n".join(f"- {u}" for u in github_links[:10]) + """

请生成 Markdown 格式的学习指南，包含:
1. 学习目标
2. 前置知识
3. 核心概念详解
4. 实践练习
5. 延伸阅读建议
6. 自测题目

使用中文，格式清晰。"""

        messages = [
            {"role": "system", "content": "你是一个专业的教育内容创作者，擅长将视频内容转化为系统化的学习材料。"},
            {"role": "user", "content": prompt},
        ]

        try:
            return self._call_api(messages, temperature=0.7)
        except Exception as e:
            logger.error(f"生成学习指南失败: {e}")
            return f"# 学习指南生成失败\n\n错误: {e}"

    def _extract_json(self, text: str) -> str:
        """从文本中提取 JSON"""
        # 尝试直接解析
        text = text.strip()
        if text.startswith("{") and text.endswith("}"):
            return text

        # 查找代码块
        import re
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            return match.group(1)

        # 查找第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]

        raise ValueError("无法从响应中提取 JSON")


def analyze_video_content(
    video_data: Dict[str, Any],
    config: Optional[Dict] = None,
    transcript: Optional[Dict[str, Any]] = None,
    research_heavy: bool = False,
) -> Dict[str, Any]:
    """
    便捷函数: 分析视频内容。

    Args:
        video_data: 视频数据字典。
        config: 配置字典。
        transcript: 可选，AudioTranscriber.transcribe() 的返回结果。
        research_heavy: 若为 True，则按科研深度分析，生成不少于 1000 字的长摘要。
    """
    cfg = config or {}
    analyzer = LLMAnalyzer(
        base_url=cfg.get("llm", {}).get("base_url", "http://127.0.0.1:4000/v1"),
        model=cfg.get("llm", {}).get("model", "kimi-code"),
        api_key=cfg.get("llm", {}).get("api_key", ""),
        timeout=cfg.get("llm", {}).get("timeout", 120),
        max_retries=cfg.get("llm", {}).get("max_retries", 3),
    )
    return analyzer.analyze_video(video_data, transcript=transcript, research_heavy=research_heavy)
