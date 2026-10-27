#!/usr/bin/env python3
"""
B站视频分析自动化系统 - 主入口

用法:
    python3 main.py --url "https://www.bilibili.com/video/BVxxxxxxxxxx"
    python3 main.py --up 123456
    python3 main.py --queue
    python3 main.py --cron
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils.helpers import load_config, setup_logging, save_json, load_json, ensure_dir
from core.bilibili_api import BilibiliAPI, fetch_video_data
from core.llm_analyzer import LLMAnalyzer, analyze_video_content
from core.obsidian_writer import ObsidianWriter, write_to_obsidian
from core.video_downloader import VideoDownloader
from core.transcriber import AudioTranscriber
from core.pending_writer import PendingManager, write_pending_note, approve_pending_note, reject_pending_note

logger = setup_logging()


def _should_download_audio(config: Dict[str, Any], source: str, manual_download: bool) -> bool:
    """
    判断当前来源是否应下载音频。
    手动 --download 优先级最高；favorites/queue/cron 默认读取 video_download.download_audio。
    """
    if manual_download:
        return True
    if source in ("favorites", "queue", "cron"):
        return config.get("video_download", {}).get("download_audio", True)
    return False


def _transcribe_audio(
    bvid: str,
    config: Dict[str, Any],
    download_dir: str,
) -> Optional[Dict[str, Any]]:
    """
    根据配置对下载的音频执行 ASR 转录。
    如果 transcription.enabled 为 false 则返回 None。
    """
    tc_config = config.get("transcription", {})
    if not tc_config.get("enabled", True):
        logger.info("转录功能已禁用，跳过 ASR")
        return None

    transcriber = AudioTranscriber(
        model=tc_config.get("model", "distil-large-v3"),
        device=tc_config.get("device", "auto"),
        compute_type=tc_config.get("compute_type", "int8"),
        download_root=config.get("paths", {}).get("cache_dir", "./cache"),
    )
    result = transcriber.transcribe_by_bvid(
        bvid,
        download_dir=download_dir,
        segment_length=tc_config.get("segment_length", 300),
        max_duration_seconds=tc_config.get("max_duration_seconds", 1800),
    )
    return result


def process_single_video(
    url: str,
    config: Dict[str, Any],
    download: bool = False,
    mode: str = "approved",
    source: str = "manual",
    research_heavy: bool = False,
) -> Dict[str, Any]:
    """处理单个视频

    Args:
        download: 是否同时下载音频（手动模式由命令行控制）。
        mode: "approved" 直接写入正式 vault；"pending" 写入候选目录等待审批。
        source: 来源标识（favorites / up / queue / manual / cron 等）
        research_heavy: 是否为科研深度分析（生成不少于 1000 字的长摘要）。
    """
    result = {
        "url": url,
        "status": "pending",
        "mode": mode,
        "info": {},
        "analysis": {},
        "transcript": None,
        "note_path": None,
        "error": None,
    }

    try:
        # 1. 获取视频数据
        logger.info(f"正在获取视频数据: {url}")
        max_danmaku = config.get("analysis", {}).get("max_danmaku", 500)
        max_comments = config.get("analysis", {}).get("max_comments", 100)
        video_data = fetch_video_data(url, max_danmaku=max_danmaku, max_comments=max_comments)

        if not video_data or not video_data.get("info"):
            result["status"] = "failed"
            result["error"] = "无法获取视频信息"
            return result

        result["info"] = video_data["info"]
        bvid = video_data["info"].get("bvid", "")
        logger.info(f"视频: {video_data['info'].get('title', '')}")

        # 2. 下载视频/音频
        need_download = _should_download_audio(config, source, manual_download=download)
        if need_download:
            logger.info("正在下载音频...")
            vd_config = config.get("video_download", {})
            paths_config = config.get("paths", {})
            downloader = VideoDownloader(
                download_dir=vd_config.get("download_dir", paths_config.get("download_dir", "./downloads")),
                format_spec=vd_config.get("format", "bestaudio/best"),
                download_video=vd_config.get("download_video", False),
                extract_subtitles=vd_config.get("extract_subtitles", True),
                download_audio=True,
            )
            dl_result = downloader.extract_audio(url, title=video_data["info"].get("title"))
            if dl_result["success"]:
                logger.info(f"下载完成: {dl_result.get('files', [])}")
            else:
                logger.warning(f"下载失败: {dl_result.get('error', '')}")

            # 3. 音频转录
            download_dir = vd_config.get("download_dir", paths_config.get("download_dir", "./downloads"))
            transcript = _transcribe_audio(bvid, config, download_dir)
            if transcript and transcript.get("success"):
                logger.info(f"转录完成，共 {len(transcript.get('segments', []))} 段")
                result["transcript"] = transcript
            elif transcript:
                logger.warning(f"转录失败或跳过: {transcript.get('error', '')}")

        # 4. LLM 分析（传入转录结果）
        logger.info("正在进行 LLM 分析...")
        analysis = analyze_video_content(
            video_data,
            config=config,
            transcript=result.get("transcript"),
            research_heavy=research_heavy,
        )
        result["analysis"] = analysis

        # 5. 生成学习指南
        logger.info("正在生成学习指南...")
        llm_config = config.get("llm", {})
        analyzer = LLMAnalyzer(
            base_url=llm_config.get("base_url", "http://127.0.0.1:4000/v1"),
            model=llm_config.get("model", "kimi-code"),
            api_key=llm_config.get("api_key", ""),
            timeout=llm_config.get("timeout", 120),
            max_retries=llm_config.get("max_retries", 3),
        )
        study_guide = analyzer.generate_study_guide(video_data, analysis)

        # 6. 写入目标目录
        paths = config.get("paths", {})
        if mode == "pending":
            logger.info("正在写入候选笔记（等待审批）...")
            pending_result = write_pending_note(
                video_data,
                analysis,
                pending_vault=paths.get("pending_vault", "./pending"),
                approved_vault=paths.get("obsidian_vault", "./output/obsidian"),
                study_guide=study_guide,
                source=source,
                transcript=result.get("transcript"),
            )
            result["note_path"] = pending_result["note_path"]
            result["status"] = "pending"
        else:
            logger.info("正在写入 Obsidian...")
            vault_path = paths.get("obsidian_vault", "./output/obsidian")
            note_path = write_to_obsidian(video_data, analysis, vault_path, study_guide, transcript=result.get("transcript"))
            result["note_path"] = note_path
            result["status"] = "success"

        logger.info(f"处理完成: {result['note_path']}")

    except Exception as e:
        logger.exception("处理视频时出错")
        result["status"] = "failed"
        result["error"] = str(e)

    return result


def process_queue(config: Dict[str, Any], mode: str = "pending") -> List[Dict[str, Any]]:
    """处理队列中的视频

    Args:
        mode: "pending" 生成候选笔记；"approved" 直接写入正式 vault。
    """
    queue_file = Path(config.get("cronjob", {}).get("queue_file", "./output/queue.json")).expanduser()
    queue = load_json(str(queue_file))

    if not queue or "urls" not in queue:
        logger.info("队列为空")
        return []

    urls = queue.get("urls", [])
    max_per_run = config.get("cronjob", {}).get("max_per_run", 5)

    # 取前 N 个未处理的
    pending = [u for u in urls if u.get("status") in ("pending", None, "failed")][:max_per_run]

    if not pending:
        logger.info("没有待处理的视频")
        return []

    results = []
    for item in pending:
        url = item.get("url", "")
        logger.info(f"处理队列项: {url}")
        # queue/cron 模式默认下载音频由配置控制
        result = process_single_video(
            url, config, download=False, mode=mode, source="queue"
        )
        results.append(result)

        # 更新队列状态
        item["status"] = result["status"]
        item["note_path"] = result.get("note_path")
        item["error"] = result.get("error")

    # 保存队列
    save_json(queue, str(queue_file))

    # 写入每日日志（根据模式选择位置）
    if mode == "approved":
        vault_path = Path(config.get("paths", {}).get("obsidian_vault", "./output/obsidian")).expanduser()
    else:
        vault_path = Path(config.get("paths", {}).get("pending_vault", "./pending")).expanduser()
    writer = ObsidianWriter(str(vault_path))
    writer.write_daily_log(results)

    return results


def add_to_queue(url: str, config: Dict[str, Any], download: bool = False):
    """添加视频到队列"""
    queue_file = Path(config.get("cronjob", {}).get("queue_file", "./output/queue.json")).expanduser()
    queue = load_json(str(queue_file))

    if "urls" not in queue:
        queue["urls"] = []

    # 检查是否已存在
    for item in queue["urls"]:
        if item.get("url") == url:
            logger.info(f"URL 已存在于队列中: {url}")
            return

    queue["urls"].append({
        "url": url,
        "status": "pending",
        "download": download,
        "added_at": __import__("datetime").datetime.now().isoformat(),
    })

    save_json(queue, str(queue_file))
    logger.info(f"已添加到队列: {url}")


def process_up_videos(mid: int, config: Dict[str, Any], limit: int = 5):
    """处理 UP 主视频"""
    api = BilibiliAPI()
    videos = api.get_up_videos(mid, ps=limit)

    logger.info(f"获取到 {len(videos)} 个视频")
    for v in videos:
        bvid = v.get("bvid", "")
        url = f"https://www.bilibili.com/video/{bvid}"
        add_to_queue(url, config, download=False)

    return videos


def main():
    parser = argparse.ArgumentParser(description="B站视频分析自动化系统")
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    parser.add_argument("--url", "-u", help="B站视频 URL")
    parser.add_argument("--up", type=int, help="UP 主 ID")
    parser.add_argument("--favorites", type=int, help="收藏夹用户 UID（从收藏夹拉取最新视频，默认生成候选）")
    parser.add_argument("--fav-folder", type=int, default=0, help="收藏夹ID（0=默认收藏夹）")
    parser.add_argument("--queue", "-q", action="store_true", help="处理队列（默认生成候选）")
    parser.add_argument("--add-queue", "-a", help="添加 URL 到队列")
    parser.add_argument("--download", "-d", action="store_true", help="同时下载音频（手动模式）")
    parser.add_argument("--no-download", action="store_true", help="覆盖配置，强制不下载音频")
    parser.add_argument("--cron", action="store_true", help="Cronjob 模式 (处理队列，默认生成候选)")
    parser.add_argument("--limit", "-l", type=int, default=5, help="UP 主/收藏夹视频数量限制")
    parser.add_argument("--approve", help="审批通过指定 BV 号，移入正式知识库")
    parser.add_argument("--reject", help="拒绝指定 BV 号候选")
    parser.add_argument("--auto-approve", action="store_true", help="本次运行直接入库（覆盖 workflow.auto_approve=false）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    paths = config.get("paths", {})
    workflow = config.get("workflow", {})

    # 决定默认模式：全局配置可被 --auto-approve 覆盖
    default_mode = "approved" if workflow.get("auto_approve", False) else "pending"
    if args.auto_approve:
        default_mode = "approved"

    # 强制不下载时覆盖配置
    if args.no_download:
        config.setdefault("video_download", {})["download_audio"] = False

    # 设置日志级别
    if args.verbose:
        logging.getLogger("bilibili-analyzer").setLevel(logging.DEBUG)

    # 确保目录存在
    ensure_dir(paths.get("obsidian_vault", "./output/obsidian"))
    ensure_dir(paths.get("pending_vault", "./pending"))
    ensure_dir("./output/logs")

    if args.url:
        # 直接处理单个视频：默认直接入库，保持旧行为
        result = process_single_video(args.url, config, download=args.download, mode="approved", source="manual")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["status"] in ("success", "pending") else 1)

    elif args.add_queue:
        # 添加到队列
        add_to_queue(args.add_queue, config, download=args.download)
        print(f"已添加到队列: {args.add_queue}")

    elif args.up:
        # 获取 UP 主视频并加入队列
        videos = process_up_videos(args.up, config, limit=args.limit)
        print(f"已添加 {len(videos)} 个视频到队列")

    elif args.approve:
        result = approve_pending_note(
            args.approve,
            pending_vault=paths.get("pending_vault", "./pending"),
            approved_vault=paths.get("obsidian_vault", "./output/obsidian"),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 1)

    elif args.reject:
        result = reject_pending_note(
            args.reject,
            pending_vault=paths.get("pending_vault", "./pending"),
            approved_vault=paths.get("obsidian_vault", "./output/obsidian"),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 1)

    elif args.favorites is not None:
        # 收藏夹模式：拉取最新收藏 → 加入队列 → 按 mode 处理
        api = BilibiliAPI()
        uid = args.favorites

        # 获取默认收藏夹 ID
        fav_id = args.fav_folder
        if fav_id == 0:
            folders = api.get_favorite_list(uid)
            default = next((f for f in folders if f.get("title") == "默认收藏夹"), folders[0] if folders else None)
            if not default:
                print("未找到收藏夹")
                sys.exit(1)
            fav_id = default["id"]
            print(f"收藏夹: {default['title']} ({default['media_count']} 个视频)")

        # 拉取最新视频（翻页取前 N 个）
        max_fetch = max(args.limit * 3, 30)  # 多拉一些做去重
        all_videos = []
        for pn in range(1, (max_fetch // 20) + 2):
            vids = api.get_favorite_videos(fav_id, page=pn, ps=20)
            if not vids:
                break
            all_videos.extend(vids)
            if len(all_videos) >= max_fetch:
                break

        print(f"拉取了 {len(all_videos)} 个收藏视频")

        # 检查已处理过的（通过 queue.json 去重）
        queue_file = Path(config.get("cronjob", {}).get("queue_file", "./output/queue.json")).expanduser()
        queue = load_json(str(queue_file))
        processed_bvids = {item.get("url", "").split("/")[-1] for item in queue.get("urls", []) if item.get("status") == "success"}

        new_count = 0
        for v in all_videos:
            bvid = v.get("bvid", "")
            if bvid and bvid not in processed_bvids:
                url = f"https://www.bilibili.com/video/{bvid}"
                add_to_queue(url, config, download=False)
                processed_bvids.add(bvid)
                new_count += 1
            if new_count >= args.limit:
                break

        print(f"新增 {new_count} 个视频到队列")

        # 按 mode 处理队列
        if new_count > 0:
            results = process_queue(config, mode=default_mode)
            success = sum(1 for r in results if r["status"] in ("success", "pending"))
            failed = sum(1 for r in results if r["status"] == "failed")
            print(f"处理完成: 成功/候选 {success}, 失败 {failed}")
            for r in results:
                status_icon = {"success": "✓", "pending": "⏳", "failed": "✗"}.get(r["status"], "?")
                title = r.get("info", {}).get("title", r.get("url", ""))
                print(f"  {status_icon} {title[:70]}")

    elif args.queue or args.cron:
        # 处理队列
        results = process_queue(config, mode=default_mode)
        success = sum(1 for r in results if r["status"] in ("success", "pending"))
        failed = sum(1 for r in results if r["status"] == "failed")
        print(f"处理完成: 成功/候选 {success}, 失败 {failed}")
        for r in results:
            status_icon = {"success": "✓", "pending": "⏳", "failed": "✗"}.get(r["status"], "?")
            print(f"  {status_icon} {r.get('info', {}).get('title', r['url'])}")
        sys.exit(0 if failed == 0 else 1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
