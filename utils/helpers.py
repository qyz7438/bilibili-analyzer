"""
通用工具函数
"""
import re
import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs


def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """配置日志"""
    handlers = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_path), encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("bilibili-analyzer")


def extract_bvid(url: str) -> Optional[str]:
    """从 URL 中提取 BV 号"""
    patterns = [
        r"BV[0-9A-Za-z]{9,12}",
        r"bv[0-9A-Za-z]{9,12}",
        r"video/(BV[0-9A-Za-z]{9,12})",
        r"video/(bv[0-9A-Za-z]{9,12})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            bvid = match.group(1) if match.groups() else match.group(0)
            return bvid
    return None


def extract_aid(url: str) -> Optional[int]:
    """从 URL 中提取 AV 号"""
    match = re.search(r"[aA][vV](\d+)", url)
    if match:
        return int(match.group(1))
    return None


def ensure_dir(path: str) -> Path:
    """确保目录存在"""
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """加载配置文件"""
    import yaml

    path = Path(config_path)
    if not path.is_absolute():
        # 先尝试当前目录，再尝试脚本所在目录
        candidates = [
            Path.cwd() / config_path,
            Path(__file__).parent.parent / config_path,
        ]
        for c in candidates:
            if c.exists():
                path = c
                break

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_json(data: Any, path: str):
    """保存 JSON 文件"""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: str) -> Any:
    """加载 JSON 文件"""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """清理文件名"""
    # 移除非法字符
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    # 移除控制字符
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    # 限制长度
    if len(name) > max_length:
        name = name[:max_length]
    # 去除首尾空格和点
    name = name.strip(" .")
    return name or "untitled"


def format_duration(seconds: int) -> str:
    """格式化时长"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_number(num: int) -> str:
    """格式化数字 (万/亿)"""
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    elif num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def get_wsl_host_ip() -> Optional[str]:
    """获取 WSL 宿主机 IP"""
    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError:
        pass
    return None


def _is_wsl() -> bool:
    """判断当前是否运行在 WSL 中"""
    try:
        with open("/proc/sys/kernel/osrelease", "r", encoding="utf-8") as f:
            release = f.read().lower()
        return "microsoft" in release or "wsl" in release
    except Exception:
        pass
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        pass
    return False


def resolve_llm_url(base_url: str) -> str:
    """解析 LLM URL。

    默认保留配置的 URL。WSL 下可直接配置服务可访问的地址。
    如明确需要使用 /etc/resolv.conf 的 nameserver 地址，可设置：
        BILIBILI_ANALYZER_LLM_RESOLVE_WSL=1
    nameserver 不一定是 Windows 宿主地址，使用前需自行确认。
    """
    parsed = urlparse(base_url)
    host = parsed.hostname
    if host in ("localhost", "127.0.0.1") and _is_wsl():
        if os.environ.get("BILIBILI_ANALYZER_LLM_RESOLVE_WSL", "0") == "1":
            wsl_host = get_wsl_host_ip()
            if not wsl_host:
                raise RuntimeError("WSL nameserver unavailable; configure llm.base_url explicitly or disable BILIBILI_ANALYZER_LLM_RESOLVE_WSL")
            if wsl_host and wsl_host != host:
                netloc = parsed.netloc.replace(host, wsl_host, 1)
                return parsed._replace(netloc=netloc).geturl()
    return base_url
