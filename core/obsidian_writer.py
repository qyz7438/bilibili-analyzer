"""
Obsidian 知识库写入模块
生成 Markdown 笔记并写入 Obsidian Vault
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils.helpers import ensure_dir, sanitize_filename, format_duration, format_number

logger = logging.getLogger("bilibili-analyzer")


def _format_transcript_section(transcript: Optional[Dict[str, Any]], max_chars: int = 20000) -> List[str]:
    """把 ASR 转录结果格式化为 Markdown 章节。"""
    lines = ["", "## 语音转录文本", ""]
    if not transcript:
        lines.append("*未启用转录或转录结果不可用。*")
        return lines
    if not transcript.get("success"):
        error = transcript.get("error", "未知错误")
        lines.append(f"*转录失败：{error}*")
        return lines

    full_text = transcript.get("full_text", "")
    segments = transcript.get("segments", [])
    if not full_text and not segments:
        lines.append("*转录结果为空。*")
        return lines

    if segments:
        for seg in segments:
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            text = seg.get("text", "").strip()
            if text:
                lines.append(f"[{start:.1f}s - {end:.1f}s] {text}")
    else:
        lines.append(full_text.strip())

    text_so_far = "\n".join(lines)
    if len(text_so_far) > max_chars:
        kept = []
        current_len = 0
        for line in lines[3:]:
            if current_len + len(line) + 1 > max_chars:
                kept.append("...（转录文本已截断）")
                break
            kept.append(line)
            current_len += len(line) + 1
        lines = ["", "## 语音转录文本", ""] + kept

    return lines


class ObsidianWriter:
    """Obsidian 笔记写入器"""

    def __init__(self, vault_path: str = "./output/obsidian"):
        self.vault_path = ensure_dir(vault_path)
        self.inbox_path = ensure_dir(self.vault_path / "Inbox")
        self.notes_path = ensure_dir(self.vault_path / "Notes")
        self.study_path = ensure_dir(self.vault_path / "StudyGuides")
        self.daily_path = ensure_dir(self.vault_path / "Daily")

    def write_video_note(
        self,
        video_data: Dict[str, Any],
        analysis: Dict[str, Any],
        study_guide: Optional[str] = None,
        transcript: Optional[Dict[str, Any]] = None,
    ) -> str:
        """写入视频分析笔记"""
        info = video_data.get("info", {})
        bvid = info.get("bvid", "")
        title = info.get("title", "未命名视频")
        safe_title = sanitize_filename(title, max_length=80)

        # 文件名
        timestamp = datetime.now().strftime("%Y%m%d")
        filename = f"{timestamp}_{safe_title}_{bvid}.md"
        filepath = self.notes_path / filename

        # 生成 Markdown 内容
        content = self._generate_note(video_data, analysis, transcript=transcript)

        # 写入文件
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"笔记已写入: {filepath}")

        # 写入学习指南
        if study_guide:
            study_filename = f"{timestamp}_{safe_title}_StudyGuide.md"
            study_filepath = self.study_path / study_filename
            with open(study_filepath, "w", encoding="utf-8") as f:
                f.write(study_guide)
            logger.info(f"学习指南已写入: {study_filepath}")

        # 更新索引
        self._update_index()

        return str(filepath)

    def _generate_note(
        self,
        video_data: Dict[str, Any],
        analysis: Dict[str, Any],
        transcript: Optional[Dict[str, Any]] = None,
    ) -> str:
        """生成 Markdown 笔记内容"""
        info = video_data.get("info", {})
        danmaku = video_data.get("danmaku", [])
        comments = video_data.get("comments", [])
        url = video_data.get("url", "")

        title = info.get("title", "")
        description = info.get("description", "")
        owner = info.get("owner", {})
        stat = info.get("stat", {})
        tags = info.get("tags", [])
        duration = info.get("duration", 0)
        pubdate = info.get("pubdate", 0)
        bvid = info.get("bvid", "")

        # 分析结果
        summary = analysis.get("summary", "")
        key_points = analysis.get("key_points", [])
        analysis_tags = analysis.get("tags", [])
        sentiment = analysis.get("sentiment", "unknown")
        audience_level = analysis.get("audience_level", "unknown")
        learning_value = analysis.get("learning_value", 0)
        related_topics = analysis.get("related_topics", [])
        action_items = analysis.get("action_items", [])
        qa_pairs = analysis.get("qa_pairs", [])

        # 合并标签
        all_tags = list(set(tags + analysis_tags))

        # 格式化日期
        pub_date_str = ""
        if pubdate:
            pub_date_str = datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d")

        # 弹幕统计
        danmaku_highlights = []
        if danmaku:
            # 按出现时间排序，取前 20 条
            sorted_dm = sorted(danmaku, key=lambda x: x.get("time", 0))[:20]
            danmaku_highlights = [d["text"] for d in sorted_dm if d.get("text")]

        # 热门评论
        top_comments = []
        if comments:
            sorted_comments = sorted(comments, key=lambda x: x.get("like", 0), reverse=True)[:10]
            top_comments = [
                f"- **{c['uname']}** (👍 {c['like']}): {c['message'][:100]}"
                for c in sorted_comments
            ]

        # 构建 Markdown
        lines = [
            f"---",
            f"created: {datetime.now().isoformat()}",
            f"source: {url}",
            f"bvid: {bvid}",
            f"up: {owner.get('name', '')}",
            f"tags: {', '.join(all_tags)}",
            f"sentiment: {sentiment}",
            f"level: {audience_level}",
            f"learning_value: {learning_value}/10",
            f"---",
            f"",
            f"# {title}",
            f"",
            f"> **UP主**: [{owner.get('name', '')}](https://space.bilibili.com/{owner.get('mid', 0)})  |  **发布日期**: {pub_date_str}  |  **时长**: {format_duration(duration)}",
            f">",
            f"> **播放**: {format_number(stat.get('view', 0))}  |  **弹幕**: {format_number(stat.get('danmaku', 0))}  |  **点赞**: {format_number(stat.get('like', 0))}  |  **收藏**: {format_number(stat.get('favorite', 0))}",
            f"",
            f"## 内容摘要",
            f"",
            f"{summary}",
            f"",
            f"## 核心知识点",
            f"",
        ]

        for point in key_points:
            lines.append(f"- {point}")

        lines.extend([
            f"",
            f"## 标签",
            f"",
        ])
        for tag in all_tags:
            lines.append(f"- #{tag.replace(' ', '_').replace('/', '_')}")

        lines.extend([
            f"",
            f"## 相关主题",
            f"",
        ])
        for topic in related_topics:
            lines.append(f"- [[{topic}]]")

        lines.extend([
            f"",
            f"## 建议行动",
            f"",
        ])
        for action in action_items:
            lines.append(f"- [ ] {action}")

        lines.extend([
            f"",
            f"## 问答",
            f"",
        ])
        for qa in qa_pairs:
            lines.extend([
                f"**Q**: {qa.get('question', '')}",
                f"",
                f"**A**: {qa.get('answer', '')}",
                f"",
            ])

        lines.extend([
            f"## 弹幕亮点",
            f"",
        ])
        for dm in danmaku_highlights[:20]:
            lines.append(f"> {dm}")

        lines.extend([
            f"",
            f"## 热门评论",
            f"",
        ])
        for comment in top_comments:
            lines.append(comment)

        lines.extend(_format_transcript_section(transcript))
        lines.extend([
            f"",
            f"## 视频描述",
            f"",
            f"```",
            f"{description}",
            f"```",
            f"",
            f"## 链接",
            f"",
            f"- [B站原视频]({url})",
            f"- [UP主主页](https://space.bilibili.com/{owner.get('mid', 0)})",
            f"",
            f"---",
            f"*由 Bilibili Analyzer 自动生成*",
        ])

        return "\n".join(lines)

    def _update_index(self):
        """更新索引文件"""
        index_path = self.vault_path / "Index.md"

        # 收集所有笔记
        notes = sorted(self.notes_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

        lines = [
            f"# Bilibili 视频笔记索引",
            f"",
            f"*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
            f"",
            f"## 最近笔记",
            f"",
        ]

        for note in notes[:50]:
            # 提取标题
            title = note.stem
            try:
                with open(note, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
            except Exception:
                pass
            rel_path = note.relative_to(self.vault_path)
            lines.append(f"- [[{rel_path.with_suffix('')}|{title}]]")

        lines.extend([
            f"",
            f"## 统计",
            f"",
            f"- 总笔记数: {len(notes)}",
            f"- 学习指南: {len(list(self.study_path.glob('*.md')))}",
        ])

        with open(index_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def write_daily_log(self, processed: List[Dict[str, Any]]):
        """写入每日处理日志"""
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = self.daily_path / f"{today}.md"

        lines = [
            f"# {today} 处理日志",
            f"",
            f"共处理 {len(processed)} 个视频",
            f"",
        ]

        for item in processed:
            info = item.get("info", {})
            status = item.get("status", "unknown")
            lines.extend([
                f"## {info.get('title', '未知')}",
                f"- 状态: {status}",
                f"- BV号: {info.get('bvid', '')}",
                f"- 笔记: {item.get('note_path', 'N/A')}",
                f"",
            ])

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


def write_to_obsidian(
    video_data: Dict[str, Any],
    analysis: Dict[str, Any],
    vault_path: str = "./output/obsidian",
    study_guide: Optional[str] = None,
    transcript: Optional[Dict[str, Any]] = None,
) -> str:
    """便捷函数: 写入 Obsidian"""
    writer = ObsidianWriter(vault_path)
    return writer.write_video_note(video_data, analysis, study_guide, transcript=transcript)
