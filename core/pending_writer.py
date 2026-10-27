"""
候选笔记与待审批摘要管理模块

改造后的工作流：
1. 后台拉取 + LLM 分析后，把结果写到 pending_vault（候选笔记目录），
   不入库到正式 obsidian_vault（llm-wiki）。
2. 同时生成一份待审批摘要清单 pending_summary.md，供前台快速浏览并勾选。
3. 提供 approve_note() / reject_note() 工具，把通过的笔记迁移到正式 vault，
   并在摘要清单中更新状态。
"""
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils.helpers import ensure_dir, sanitize_filename, format_duration, format_number
from core.obsidian_writer import ObsidianWriter

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

    # 优先输出带时间戳的分段，便于定位
    if segments:
        for seg in segments:
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            text = seg.get("text", "").strip()
            if text:
                lines.append(f"[{start:.1f}s - {end:.1f}s] {text}")
    else:
        lines.append(full_text.strip())

    # 控制总长度，避免单个 Markdown 文件过大
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


class PendingManager:
    """候选笔记管理器"""

    def __init__(
        self,
        pending_vault: str = "./output/pending",
        approved_vault: str = "./output/obsidian",
    ):
        self.pending_vault = ensure_dir(pending_vault)
        self.approved_vault = ensure_dir(approved_vault)
        self.notes_path = ensure_dir(self.pending_vault / "Notes")
        self.study_path = ensure_dir(self.pending_vault / "StudyGuides")
        self.summary_path = self.pending_vault / "pending_summary.md"

    def write_pending_note(
        self,
        video_data: Dict[str, Any],
        analysis: Dict[str, Any],
        study_guide: Optional[str] = None,
        source: str = "manual",
        transcript: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """写入一份候选笔记，并更新待审批摘要清单

        Returns:
            {"note_path": ..., "study_path": ..., "bvid": ..., "status": "pending"}
        """
        info = video_data.get("info", {})
        bvid = info.get("bvid", "")
        title = info.get("title", "未命名视频")
        safe_title = sanitize_filename(title, max_length=80)
        timestamp = datetime.now().strftime("%Y%m%d")

        # 候选笔记文件名
        note_filename = f"{timestamp}_{safe_title}_{bvid}.md"
        note_path = self.notes_path / note_filename

        # 写入候选 Markdown（结构与正式笔记一致，但 frontmatter 加 pending 标记）
        content = self._generate_pending_note(
            video_data, analysis, source=source, transcript=transcript
        )
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"候选笔记已写入: {note_path}")

        # 写入学习指南到 pending
        study_path = None
        if study_guide:
            study_filename = f"{timestamp}_{safe_title}_StudyGuide.md"
            study_path = self.study_path / study_filename
            with open(study_path, "w", encoding="utf-8") as f:
                f.write(study_guide)
            logger.info(f"候选学习指南已写入: {study_path}")

        # 更新摘要清单
        self._update_summary(
            {
                "bvid": bvid,
                "title": title,
                "up": info.get("owner", {}).get("name", ""),
                "url": video_data.get("url", ""),
                "note_path": str(note_path),
                "study_path": str(study_path) if study_path else "",
                "status": "pending",
                "created": datetime.now().isoformat(),
                "source": source,
                "learning_value": str(analysis.get("learning_value", "")),
                "summary": analysis.get("summary", ""),
            }
        )

        return {
            "note_path": str(note_path),
            "study_path": str(study_path) if study_path else "",
            "bvid": bvid,
            "status": "pending",
        }

    def _generate_pending_note(
        self,
        video_data: Dict[str, Any],
        analysis: Dict[str, Any],
        source: str = "manual",
        transcript: Optional[Dict[str, Any]] = None,
    ) -> str:
        """生成候选笔记内容

        与 ObsidianWriter 的正式笔记结构保持一致，仅在 frontmatter 中增加
        status: pending 与 pending_source，便于审批时识别。
        """
        info = video_data.get("info", {})
        url = video_data.get("url", "")
        title = info.get("title", "")
        description = info.get("description", "")
        owner = info.get("owner", {})
        stat = info.get("stat", {})
        tags = info.get("tags", [])
        duration = info.get("duration", 0)
        pubdate = info.get("pubdate", 0)
        bvid = info.get("bvid", "")

        summary = analysis.get("summary", "")
        key_points = analysis.get("key_points", [])
        analysis_tags = analysis.get("tags", [])
        sentiment = analysis.get("sentiment", "unknown")
        audience_level = analysis.get("audience_level", "unknown")
        learning_value = analysis.get("learning_value", 0)
        related_topics = analysis.get("related_topics", [])
        action_items = analysis.get("action_items", [])
        qa_pairs = analysis.get("qa_pairs", [])

        all_tags = list(set(tags + analysis_tags))

        pub_date_str = ""
        if pubdate:
            pub_date_str = datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d")

        lines = [
            "---",
            f"created: {datetime.now().isoformat()}",
            f"source: {url}",
            f"bvid: {bvid}",
            f"up: {owner.get('name', '')}",
            f"tags: {', '.join(all_tags)}",
            f"sentiment: {sentiment}",
            f"level: {audience_level}",
            f"learning_value: {learning_value}/10",
            "status: pending",
            f"pending_source: {source}",
            "---",
            "",
            f"# {title}",
            "",
            f"> **UP主**: [{owner.get('name', '')}](https://space.bilibili.com/{owner.get('mid', 0)})  |  **发布日期**: {pub_date_str}  |  **时长**: {format_duration(duration)}",
            f">",
            f"> **播放**: {format_number(stat.get('view', 0))}  |  **弹幕**: {format_number(stat.get('danmaku', 0))}  |  **点赞**: {format_number(stat.get('like', 0))}  |  **收藏**: {format_number(stat.get('favorite', 0))}",
            "",
            "## 内容摘要",
            "",
            f"{summary}",
            "",
            "## 核心知识点",
            "",
        ]

        for point in key_points:
            lines.append(f"- {point}")

        lines.extend(["", "## 标签", ""])
        for tag in all_tags:
            lines.append(f"- #{tag.replace(' ', '_').replace('/', '_')}")

        lines.extend(["", "## 相关主题", ""])
        for topic in related_topics:
            lines.append(f"- [[{topic}]]")

        lines.extend(["", "## 建议行动", ""])
        for action in action_items:
            lines.append(f"- [ ] {action}")

        lines.extend(["", "## 问答", ""])
        for qa in qa_pairs:
            lines.extend([
                f"**Q**: {qa.get('question', '')}",
                "",
                f"**A**: {qa.get('answer', '')}",
                "",
            ])

        lines.extend(_format_transcript_section(transcript))
        lines.extend(["", "## 视频描述", "", "```", f"{description}", "```", "", "## 链接", ""])
        lines.append(f"- [B站原视频]({url})")
        lines.append(f"- [UP主主页](https://space.bilibili.com/{owner.get('mid', 0)})")
        lines.extend(["", "---", "*由 Bilibili Analyzer 生成（候选状态，等待审批）*"])

        return "\n".join(lines)

    def _update_summary(self, entry: Dict[str, str]):
        """追加/更新待审批摘要清单中的条目"""
        entries = self._load_summary_entries()

        # 按 bvid 去重更新：保留旧条目的 study_path 等未渲染字段
        existing = {e["bvid"]: i for i, e in enumerate(entries)}
        if entry["bvid"] in existing:
            old = entries[existing[entry["bvid"]]]
            merged = dict(old)
            merged.update(entry)
            entries[existing[entry["bvid"]]] = merged
        else:
            entries.append(entry)

        self._write_summary(entries)

    def _load_summary_entries(self) -> List[Dict[str, str]]:
        if not self.summary_path.exists():
            return []
        entries = []
        current = {}
        in_entry = False
        with open(self.summary_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("### "):
                    if current:
                        entries.append(current)
                    current = {"title": line[4:].strip()}
                    in_entry = True
                elif in_entry and line.startswith("- "):
                    kv = line[2:].split(":", 1)
                    if len(kv) == 2:
                        key = kv[0].strip().lower().replace(" ", "_")
                        value = kv[1].strip()
                        # 去掉 Markdown 代码标记
                        if value.startswith("`") and value.endswith("`"):
                            value = value[1:-1]
                        mapped = {
                            "bv号": "bvid",
                            "up主": "up",
                            "来源": "source",
                            "状态": "status",
                            "学习价值": "learning_value",
                            "候选笔记": "note_path",
                            "视频链接": "url",
                            "摘要": "summary",
                            "学习指南": "study_path",
                        }
                        if key in mapped:
                            # 状态行可能带图标，如 "✅ approved"，只取后面单词
                            if key == "状态":
                                value = value.split()[-1]
                            current[mapped[key]] = value
                elif line.strip() == "":
                    if current and in_entry:
                        # 条目间空行表示当前条目结束
                        entries.append(current)
                        current = {}
                        in_entry = False
                    continue
        if current and in_entry:
            entries.append(current)
        return entries

    def _save_summary_entries(self, entries: List[Dict[str, str]]):
        """持久化摘要条目（保留所有字段，包括不在渲染中的 study_path）"""
        self._write_summary(entries)

    def _write_summary(self, entries: List[Dict[str, str]]):
        lines = [
            "# 待审批 B站视频候选清单",
            "",
            "> 本文件由后台自动生成。前台审阅者请勾选想要入库的条目，",
            "> 然后运行 `python3 main.py --approve <bvid>` 将其移入正式知识库。",
            "",
            "## 待审批列表",
            "",
        ]

        pending = [e for e in entries if e.get("status") == "pending"]
        approved = [e for e in entries if e.get("status") == "approved"]
        rejected = [e for e in entries if e.get("status") == "rejected"]

        for e in pending:
            lines.extend(self._render_summary_item(e))

        if approved:
            lines.extend(["", "## 已通过", ""])
            for e in approved:
                lines.extend(self._render_summary_item(e))
            # 在已通过条目后追加审批命令提示
            lines.extend([
                "",
                "> 已通过条目已移入正式知识库，对应源文件已清理。",
                "",
            ])

        if rejected:
            lines.extend(["", "## 已拒绝", ""])
            for e in rejected:
                lines.extend(self._render_summary_item(e))
            lines.extend([
                "",
                "> 已拒绝条目原文件已移动到 pending/Rejected/ 目录。",
                "",
            ])

        lines.extend([
            "",
            "---",
            f"*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        ])

        with open(self.summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"待审批摘要清单已更新: {self.summary_path}")

    def _render_summary_item(self, entry: Dict[str, str]) -> List[str]:
        title = entry.get("title", "未知")
        status = entry.get("status", "pending")
        icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(status, "⏳")
        note_path = entry.get("note_path", "")
        rel_note = Path(note_path).name if note_path else ""
        # 学习价值在写入时已存为纯数字字符串；展示时补 /10
        learning_value = entry.get("learning_value", "")
        if learning_value.endswith("/10"):
            learning_value = learning_value[:-3]
        try:
            learning_value = str(int(float(learning_value)))
        except Exception:
            pass
        return [
            f"### {title}",
            f"- BV号: {entry.get('bvid', '')}",
            f"- UP主: {entry.get('up', '')}",
            f"- 来源: {entry.get('source', '')}",
            f"- 学习价值: {learning_value}/10",
            f"- 状态: {icon} {status}",
            f"- 候选笔记: `{rel_note}`",
            f"- 视频链接: {entry.get('url', '')}",
            f"- 摘要: {entry.get('summary', '')}",
            f"- 学习指南: `{Path(entry.get('study_path', '')).name or 'N/A'}`",
            "",
        ]

    def list_pending(self) -> List[Dict[str, str]]:
        """列出所有 pending 条目"""
        return [e for e in self._load_summary_entries() if e.get("status") == "pending"]

    def _find_entry(self, bvid: str) -> Optional[Dict[str, str]]:
        for e in self._load_summary_entries():
            if e.get("bvid") == bvid:
                return e
        return None

    def approve_note(self, bvid: str) -> Dict[str, Any]:
        """把候选笔记迁移到正式 vault 并更新摘要状态"""
        entry = self._find_entry(bvid)
        if not entry:
            return {"success": False, "error": f"未找到候选: {bvid}"}

        note_path = Path(entry.get("note_path", ""))
        if not note_path.is_absolute():
            note_path = self.pending_vault / "Notes" / note_path.name
        if not note_path.exists():
            return {"success": False, "error": f"候选笔记文件不存在: {note_path}"}

        # 迁移到正式 vault
        writer = ObsidianWriter(str(self.approved_vault))
        target_notes = ensure_dir(self.approved_vault / "Notes")
        target_study = ensure_dir(self.approved_vault / "StudyGuides")

        target_note = target_notes / note_path.name
        shutil.copy2(note_path, target_note)

        # 修改 frontmatter 中的 status
        self._mark_approved_in_file(target_note)

        # 迁移学习指南
        study_path = entry.get("study_path", "")
        target_study_path = None
        if study_path:
            study_path = Path(study_path)
            if not study_path.is_absolute():
                study_path = self.pending_vault / "StudyGuides" / study_path.name
            if study_path.exists():
                target_study = ensure_dir(self.approved_vault / "StudyGuides")
                target_study_path = target_study / study_path.name
                shutil.copy2(study_path, target_study_path)

        # 更新索引
        writer = ObsidianWriter(str(self.approved_vault))
        writer._update_index()

        # 更新摘要状态
        entry["status"] = "approved"
        entry["approved_at"] = datetime.now().isoformat()
        entry["approved_note_path"] = str(target_note)
        self._update_summary(entry)

        # 可选：审批后从 pending 删除源文件
        try:
            note_path.unlink()
            if study_path:
                study_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"清理 pending 源文件失败: {e}")

        return {
            "success": True,
            "bvid": bvid,
            "approved_note_path": str(target_note),
            "approved_study_path": str(target_study_path) if target_study_path else "",
        }

    def reject_note(self, bvid: str) -> Dict[str, Any]:
        """拒绝候选笔记"""
        entry = self._find_entry(bvid)
        if not entry:
            return {"success": False, "error": f"未找到候选: {bvid}"}

        entry["status"] = "rejected"
        entry["rejected_at"] = datetime.now().isoformat()
        self._update_summary(entry)

        # 可选：移动/保留原文件，这里保留到 rejected 子目录
        note_path = Path(entry.get("note_path", ""))
        if not note_path.is_absolute():
            note_path = self.pending_vault / "Notes" / note_path.name
        if note_path.exists():
            rejected_dir = ensure_dir(self.pending_vault / "Rejected")
            try:
                shutil.move(str(note_path), str(rejected_dir / note_path.name))
            except Exception as e:
                logger.warning(f"移动拒绝文件失败: {e}")
        # 同时移动学习指南
        study_path = entry.get("study_path", "")
        if study_path:
            study_path = Path(study_path)
            if not study_path.is_absolute():
                study_path = self.pending_vault / "StudyGuides" / study_path.name
            if study_path.exists():
                try:
                    shutil.move(str(study_path), str(rejected_dir / study_path.name))
                except Exception as e:
                    logger.warning(f"移动拒绝学习指南失败: {e}")

        return {"success": True, "bvid": bvid, "status": "rejected"}

    def _mark_approved_in_file(self, path: Path):
        """把笔记 frontmatter 中的 status: pending 改为 status: approved"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            with open(path, "w", encoding="utf-8") as f:
                in_frontmatter = False
                for line in lines:
                    if line.strip() == "---":
                        in_frontmatter = not in_frontmatter
                    if in_frontmatter and line.startswith("status: pending"):
                        line = "status: approved\n"
                    f.write(line)
        except Exception as e:
            logger.warning(f"修改 frontmatter 状态失败: {e}")


def write_pending_note(
    video_data: Dict[str, Any],
    analysis: Dict[str, Any],
    pending_vault: str = "./output/pending",
    approved_vault: str = "./output/obsidian",
    study_guide: Optional[str] = None,
    source: str = "manual",
    transcript: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """便捷函数: 写入候选笔记"""
    manager = PendingManager(pending_vault, approved_vault)
    return manager.write_pending_note(video_data, analysis, study_guide, source, transcript=transcript)


def approve_pending_note(
    bvid: str,
    pending_vault: str = "./output/pending",
    approved_vault: str = "./output/obsidian",
) -> Dict[str, Any]:
    """便捷函数: 审批通过候选笔记"""
    manager = PendingManager(pending_vault, approved_vault)
    return manager.approve_note(bvid)


def reject_pending_note(
    bvid: str,
    pending_vault: str = "./output/pending",
    approved_vault: str = "./output/obsidian",
) -> Dict[str, Any]:
    """便捷函数: 拒绝候选笔记"""
    manager = PendingManager(pending_vault, approved_vault)
    return manager.reject_note(bvid)
