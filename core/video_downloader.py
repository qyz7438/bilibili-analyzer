"""
视频下载模块
使用 yt-dlp 下载视频/音频/字幕；B站 412 时回退到官方 API DASH 音频下载
"""
import os
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests

from utils.helpers import ensure_dir, sanitize_filename, extract_bvid

logger = logging.getLogger("bilibili-analyzer")


class VideoDownloader:
    """视频下载器"""

    def __init__(
        self,
        download_dir: str = "~/bilibili-downloads",
        format_spec: str = "bestaudio/best",
        download_video: bool = False,
        extract_subtitles: bool = True,
        download_audio: bool = True,
    ):
        self.download_dir = ensure_dir(download_dir)
        self.format_spec = format_spec
        self.download_video = download_video
        self.extract_subtitles = extract_subtitles
        # 新增：是否下载音频，收藏夹/cron/queue 模式默认启用
        self.download_audio = download_audio

    def _run_yt_dlp(self, url: str, options: List[str]) -> Dict[str, Any]:
        """运行 yt-dlp 命令"""
        cmd = ["yt-dlp", "--no-warnings", "--dump-json"] + options + [url]
        logger.info(f"执行: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"yt-dlp 错误: {result.stderr}")
                return {"success": False, "error": result.stderr}

            # 解析 JSON 输出
            lines = result.stdout.strip().split("\n")
            info = {}
            for line in lines:
                if line.strip():
                    try:
                        info = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            return {"success": True, "info": info}
        except subprocess.TimeoutExpired:
            logger.error("yt-dlp 超时")
            return {"success": False, "error": "timeout"}
        except Exception as e:
            logger.error(f"yt-dlp 异常: {e}")
            return {"success": False, "error": str(e)}

    def download(self, url: str, title: Optional[str] = None) -> Dict[str, Any]:
        """下载视频/音频"""
        output_template = str(self.download_dir / "%(id)s_%(title).80s.%(ext)s")

        options = [
            "--format", self.format_spec,
            "--output", output_template,
            "--write-info-json",
            "--no-playlist",
        ]

        # 若未要求视频且启用音频，则提取音频为 mp3
        if not self.download_video and self.download_audio:
            options.extend(["--extract-audio", "--audio-format", "mp3"])

        if self.extract_subtitles:
            options.extend([
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", "zh-CN,zh-Hans,zh,en",
                "--convert-subs", "srt",
            ])

        # 先获取信息
        info_result = self._run_yt_dlp(url, ["--skip-download"])

        if info_result["success"]:
            info = info_result.get("info", {})
            video_id = info.get("id", "unknown")
            safe_title = sanitize_filename(title or info.get("title", video_id))
        else:
            # yt-dlp 信息获取失败（如 B站 412），尝试 API 回退下载音频
            if self.download_audio and not self.download_video:
                logger.warning("yt-dlp 信息获取失败，尝试使用 B站 API 回退下载音频...")
                bvid = extract_bvid(url) or "unknown"
                safe_title = sanitize_filename(title or bvid)
                api_result = self._download_audio_via_api(url, bvid, safe_title)
                if api_result["success"]:
                    api_result["video_id"] = bvid
                    api_result["title"] = safe_title
                    api_result["download_dir"] = str(self.download_dir)
                    return api_result
            return info_result

        # 自定义输出文件名
        output_template = str(self.download_dir / f"{video_id}_{safe_title}.%(ext)s")
        options = [opt for opt in options if not opt.startswith(str(self.download_dir))]
        options.extend(["--output", output_template])

        # 执行下载
        result = self._run_yt_dlp(url, options)

        # B站 412 等错误时回退到 API DASH 音频下载
        if not result["success"] and self.download_audio and not self.download_video:
            logger.warning("yt-dlp 失败，尝试使用 B站 API 回退下载音频...")
            api_result = self._download_audio_via_api(url, video_id, safe_title)
            if api_result["success"]:
                result = api_result

        result["video_id"] = video_id
        result["title"] = safe_title
        result["download_dir"] = str(self.download_dir)

        # 查找下载的文件
        if result["success"]:
            downloaded_files = list(self.download_dir.glob(f"{video_id}_*"))
            result["files"] = [str(f) for f in downloaded_files]

        return result

    def _download_audio_via_api(
        self, url: str, video_id: str, safe_title: str
    ) -> Dict[str, Any]:
        """通过 B站 API 获取 DASH 音频地址并下载（yt-dlp 412 回退）"""
        bvid = extract_bvid(url)
        if not bvid:
            return {"success": False, "error": "无法从 URL 提取 BV 号"}

        try:
            from core.bilibili_api import BilibiliAPI

            api = BilibiliAPI()
            info = api.get_video_info(bvid=bvid)
            cid = info.get("cid", 0)
            if not cid:
                return {"success": False, "error": "无法获取视频 cid"}

            playurl = api.get_playurl(bvid, cid)
            audio_urls = playurl.get("audio_urls", [])
            if not audio_urls:
                return {"success": False, "error": "API 未返回音频地址"}

            # 使用第一个音频地址下载
            audio_url = audio_urls[0]
            ext = "m4a"  # B站 DASH 音频为 AAC in MP4， faster-whisper 可直接读取
            out_path = self.download_dir / f"{video_id}_{safe_title}.{ext}"

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
                "Range": "bytes=0-",
            }
            logger.info(f"API 回退下载音频: {out_path.name}")
            session = requests.Session()
            # 重试策略，应对 IncompleteRead / 短连接
            from requests.adapters import HTTPAdapter
            session.mount("http://", HTTPAdapter(max_retries=2))
            session.mount("https://", HTTPAdapter(max_retries=2))
            with session.get(audio_url, headers=headers, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

            return {
                "success": True,
                "info": {"id": video_id, "title": safe_title},
                "files": [str(out_path)],
                "downloaded_via": "bilibili_api",
            }
        except Exception as e:
            logger.error(f"API 回退下载失败: {e}")
            return {"success": False, "error": f"API 回退下载失败: {e}"}

    def extract_audio(self, url: str, title: Optional[str] = None) -> Dict[str, Any]:
        """仅提取音频"""
        old_download_video = self.download_video
        old_download_audio = self.download_audio
        self.download_video = False
        self.download_audio = True
        try:
            return self.download(url, title)
        finally:
            self.download_video = old_download_video
            self.download_audio = old_download_audio

    def get_subtitles(self, url: str) -> List[Dict[str, Any]]:
        """获取字幕列表"""
        result = self._run_yt_dlp(url, ["--list-subs", "--skip-download"])
        if not result["success"]:
            return []

        info = result.get("info", {})
        subtitles = info.get("subtitles", {})
        auto_subs = info.get("automatic_captions", {})

        all_subs = []
        for lang, subs in {**subtitles, **auto_subs}.items():
            for sub in subs:
                all_subs.append({
                    "lang": lang,
                    "url": sub.get("url", ""),
                    "name": sub.get("name", ""),
                    "auto": lang in auto_subs,
                })
        return all_subs


def download_video(
    url: str,
    download_dir: str = "~/bilibili-downloads",
    audio_only: bool = True,
) -> Dict[str, Any]:
    """便捷函数: 下载视频"""
    downloader = VideoDownloader(
        download_dir=download_dir,
        download_video=not audio_only,
        download_audio=audio_only,
    )
    return downloader.download(url)
