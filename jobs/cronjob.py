#!/usr/bin/env python3
"""
Cronjob 包装器
用于 Hermes cronjob 部署

用法:
    python3 jobs/cronjob.py

环境变量:
    BILIBILI_CONFIG: 配置文件路径 (默认: config.yaml)
"""
import os
import sys
import json
import logging
from pathlib import Path

# 添加项目根目录到路径
SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from utils.helpers import load_config, setup_logging, ensure_dir
from main import process_queue

logger = setup_logging()


def run_cronjob():
    """执行 cronjob 任务"""
    config_path = os.environ.get("BILIBILI_CONFIG", str(SCRIPT_DIR / "config.yaml"))
    
    logger.info(f"=== Bilibili Analyzer Cronjob 开始 ===")
    logger.info(f"配置文件: {config_path}")
    
    try:
        config = load_config(config_path)
        
        # 确保目录存在
        ensure_dir(config.get("obsidian_vault", "./output/obsidian"))
        ensure_dir("./output/logs")
        
        # 处理队列
        results = process_queue(config)
        
        success = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        
        logger.info(f"=== Cronjob 完成: 成功 {success}, 失败 {failed} ===")
        
        # 输出结果到 stdout (Hermes 可捕获)
        output = {
            "status": "completed",
            "total": len(results),
            "success": success,
            "failed": failed,
            "results": [
                {
                    "url": r["url"],
                    "status": r["status"],
                    "title": r.get("info", {}).get("title", ""),
                    "note_path": r.get("note_path"),
                    "error": r.get("error"),
                }
                for r in results
            ]
        }
        print(json.dumps(output, ensure_ascii=False))
        
        return 0 if failed == 0 else 1
        
    except Exception as e:
        logger.exception("Cronjob 执行失败")
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(run_cronjob())
