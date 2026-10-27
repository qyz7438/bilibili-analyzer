"""
音频转录模块 (ASR)
使用 faster-whisper 对 downloads/ 中的音频文件进行本地转录，
输出带时间戳的文本，支持基本清洗与按时间段分段。
"""
import re
import os
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("bilibili-analyzer")

# torch 仅在实际加载 faster-whisper 模型时按需导入，避免导入本模块就失败
try:
    import torch  # type: ignore
except Exception:
    torch = None  # type: ignore


# 常见中文语气词/重复填充词，用于基本清洗
FILLER_WORDS = [
    "啊", "呃", "嗯", "哦", "哎", "唉", "哈", "呵", "嘿", "呜", "嘛", "呢", "吧",
    "就是说", "那个", "这个", "然后", "然后啊", "就是", "是吧", "对吧", "嗯嗯",
]

# 任意 URL 匹配，用于把简介/评论里的链接也纳入项目发现
URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\uff0c\u3002\uff1b\uff1f\uff01\"'<>]+", re.IGNORECASE)


class AudioTranscriber:
    """基于 faster-whisper 的音频转录器"""

    def __init__(
        self,
        model: str = "distil-large-v3",
        device: str = "auto",
        compute_type: str = "int8",
        download_root: Optional[str] = None,
    ):
        """
        初始化转录器

        Args:
            model: faster-whisper 模型名，默认 distil-large-v3。
            device: 计算设备，可选 cpu / cuda / auto。
            compute_type: 计算精度，例如 int8 / float16 / float32。
            download_root: 模型下载根目录，None 则使用默认缓存目录。
        """
        self.model_name = model
        self.compute_type = compute_type
        self.device = self._resolve_device(device)
        self.download_root = Path(download_root).expanduser() if download_root else None
        self._model = None

    def _resolve_device(self, device: str) -> str:
        """解析设备参数：auto 时优先使用 CUDA"""
        if device == "auto":
            if torch is None:
                return "cpu"
            return "cuda" if torch.cuda.is_available() else "cpu"
        return device

    def _ensure_package(self):
        """检查 faster-whisper 是否已安装；缺失时不自动安装，避免污染环境。"""
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "faster-whisper 未安装。请手动安装后再启用转录功能：\n"
                "  pip install faster-whisper\n"
                "或在 config.yaml 中设置 transcription.enabled: false 跳过转录。"
            ) from exc

    def _load_model(self):
        """延迟加载 faster-whisper 模型"""
        if self._model is not None:
            return
        self._ensure_package()
        from faster_whisper import WhisperModel

        logger.info(
            f"正在加载 faster-whisper 模型: {self.model_name} "
            f"(device={self.device}, compute_type={self.compute_type})"
        )
        start = time.time()
        kwargs = {}
        if self.download_root is not None:
            kwargs["download_root"] = str(self.download_root)
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            **kwargs,
        )
        logger.info(f"模型加载完成，耗时 {time.time() - start:.2f}s")

    def _clean_text(self, text: str) -> str:
        """
        基本清洗：去除首尾空白、重复标点、连续重复字符、常见语气词。
        仅做保守处理，避免误删有效信息。
        """
        text = text.strip()
        # 合并多个连续空格
        text = re.sub(r"\s+", " ", text)
        # 去除明显重复的单个字符（如 "好好好好" -> "好"）
        text = re.sub(r"(.)\1{3,}", r"\1", text)
        # 去除句首独立语气词（如 "嗯，今天..."）
        for fw in FILLER_WORDS:
            # 仅匹配句首或标点后独立出现的语气词
            text = re.sub(rf"(^|[，。！？；、\s]){re.escape(fw)}(?=[，。！？；、\s]|$)", r"\1", text)
        # 清理空括号
        text = re.sub(r"[（(][）)]", "", text)
        return text.strip()

    def _segment_transcript(
        self,
        segments: List[Dict[str, Any]],
        segment_length: int = 300,
    ) -> List[Dict[str, Any]]:
        """
        将原始 fine-grained 片段按时间段合并为更大的段，便于 LLM 处理长视频。

        Args:
            segments: 原始转录片段列表，每个元素至少包含 start/end/text。
            segment_length: 每段最大秒数。
        Returns:
            合并后的段列表，每段包含 start/end/text。
        """
        if not segments:
            return []

        merged: List[Dict[str, Any]] = []
        current_texts: List[str] = []
        current_start: Optional[float] = None
        current_end: float = 0.0

        for seg in segments:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
            text = seg.get("text", "").strip()
            if not text:
                continue
            if current_start is None:
                current_start = start

            # 如果当前段已接近上限，且下一段会让它明显超出，则先结束当前段
            if current_texts and (end - current_start) > segment_length:
                merged.append({
                    "start": round(current_start, 2),
                    "end": round(current_end, 2),
                    "text": " ".join(current_texts),
                })
                current_start = start
                current_texts = []

            current_texts.append(text)
            current_end = end

        if current_texts:
            merged.append({
                "start": round(current_start or 0, 2),
                "end": round(current_end, 2),
                "text": " ".join(current_texts),
            })

        return merged

    def transcribe(
        self,
        audio_path: str,
        segment_length: int = 300,
        max_duration_seconds: Optional[int] = None,
        language: str = "zh",
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> Dict[str, Any]:
        """
        转录音频文件。

        Args:
            audio_path: 音频文件路径（mp3 / m4a / wav 等）。
            segment_length: 合并后的每段最大秒数。
            max_duration_seconds: 超过该时长则跳过或分段；None 表示不限制。
            language: 音频语言，默认中文。
            beam_size: 解码 beam size。
            vad_filter: 是否启用 VAD 过滤静音。
        Returns:
            {
                "success": bool,
                "audio_path": str,
                "duration": float,
                "full_text": str,
                "segments": [{"start", "end", "text"}, ...],
                "language": str,
                "error": str | None,
            }
        """
        path = Path(audio_path).expanduser()
        if not path.exists():
            return {"success": False, "error": f"音频文件不存在: {path}", "audio_path": str(path)}

        self._load_model()

        try:
            # 可选：使用 pydub/ffprobe 获取时长；faster-whisper 本身也会返回时间戳
            duration = self._estimate_duration(str(path))
            if max_duration_seconds and duration and duration > max_duration_seconds:
                logger.warning(
                    f"音频时长 {duration:.0f}s 超过最大限制 {max_duration_seconds}s，本次跳过转录"
                )
                return {
                    "success": False,
                    "error": f"音频过长 ({duration:.0f}s > {max_duration_seconds}s)",
                    "audio_path": str(path),
                    "duration": duration,
                }

            logger.info(f"开始转录: {path.name} (预估时长 {duration:.0f}s)")
            start = time.time()
            # 使用 word_timestamps 获得更细粒度时间戳，便于后续分段
            raw_segments, info = self._model.transcribe(
                str(path),
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                word_timestamps=True,
            )

            # 整理片段并清洗
            cleaned_segments: List[Dict[str, Any]] = []
            for seg in raw_segments:
                text = self._clean_text(seg.text)
                if not text:
                    continue
                cleaned_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": text,
                })

            # 按时间段合并
            merged_segments = self._segment_transcript(cleaned_segments, segment_length=segment_length)
            full_text = "\n".join(
                f"[{self._format_time(s['start'])}] {s['text']}" for s in merged_segments
            )

            elapsed = time.time() - start
            logger.info(
                f"转录完成: {path.name}, 原始片段 {len(cleaned_segments)} 个, "
                f"合并后 {len(merged_segments)} 段, 耗时 {elapsed:.2f}s"
            )

            return {
                "success": True,
                "audio_path": str(path),
                "duration": round(duration or (merged_segments[-1]["end"] if merged_segments else 0), 2),
                "full_text": full_text,
                "segments": merged_segments,
                "language": info.language or language,
                "error": None,
            }
        except Exception as e:
            logger.exception("音频转录失败")
            return {"success": False, "error": str(e), "audio_path": str(path)}

    def _estimate_duration(self, audio_path: str) -> float:
        """估算音频时长（秒），失败返回 0"""
        try:
            from pydub.utils import mediainfo_json
            info = mediainfo_json(audio_path)
            return float(info.get("format", {}).get("duration", 0) or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _format_time(seconds: float) -> str:
        """将秒数格式化为 HH:MM:SS 或 MM:SS"""
        seconds = max(0, seconds)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def find_audio_files(self, download_dir: str, bvid: Optional[str] = None) -> List[Path]:
        """
        在下载目录中查找音频文件。

        Args:
            download_dir: 下载目录路径。
            bvid: 可选，指定 BV 号前缀，优先返回最匹配的音频。
        Returns:
            按修改时间降序排列的音频文件列表。
        """
        root = Path(download_dir).expanduser()
        if not root.exists():
            return []

        exts = {"*.mp3", "*.m4a", "*.wav", "*.flac", "*.aac", "*.ogg", "*.opus", "*.wma"}
        files: List[Path] = []
        for ext in exts:
            files.extend(root.glob(ext))

        # 若指定 bvid，则优先匹配文件名包含该 bvid 的文件
        if bvid:
            matched = [f for f in files if bvid.lower() in f.stem.lower()]
            if matched:
                files = matched

        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files

    def transcribe_by_bvid(
        self,
        bvid: str,
        download_dir: str,
        segment_length: int = 300,
        max_duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        根据 BV 号在下载目录中查找并转录音频。

        Args:
            bvid: BV 号。
            download_dir: 音频下载目录。
            segment_length: 合并后的每段最大秒数。
            max_duration_seconds: 最大允许时长。
        Returns:
            转录结果字典；未找到音频时返回失败信息。
        """
        files = self.find_audio_files(download_dir, bvid=bvid)
        if not files:
            return {
                "success": False,
                "error": f"在 {download_dir} 中未找到 {bvid} 的音频文件",
                "bvid": bvid,
            }
        return self.transcribe(
            str(files[0]),
            segment_length=segment_length,
            max_duration_seconds=max_duration_seconds,
        )


def extract_github_links(text: str) -> List[str]:
    """
    从文本中提取 GitHub / GitLab 项目链接。
    返回去重后的链接列表。
    """
    urls = URL_RE.findall(text or "")
    links: List[str] = []
    for url in urls:
        url = url.strip(".,;:!?\"'()[]<>")
        lower = url.lower()
        if "github.com" in lower or "gitlab.com" in lower or "github.io" in lower:
            # 简单过滤掉 releases/blob 等单页，保留项目首页-ish
            links.append(url)
    # 去重同时保持顺序
    seen = set()
    result: List[str] = []
    for link in links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def extract_urls(text: str) -> List[str]:
    """从文本中提取所有 URL（用于进一步 LLM 发现相关项目）"""
    return [u.strip(".,;:!?\"'()[]<>") for u in URL_RE.findall(text or "")]
