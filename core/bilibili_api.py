"""
B站 API 封装模块
提供视频信息、弹幕、评论的获取功能
"""
import os
import re
import zlib
import hashlib
import time
import logging
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

import requests

from utils.helpers import extract_bvid, extract_aid, load_config

logger = logging.getLogger("bilibili-analyzer")

# API 基础地址
BASE_API = "https://api.bilibili.com"

# 请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class BilibiliAPI:
    """B站 API 客户端"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # 显式读取代理环境变量；requests 在部分 WSL 环境中无法自动继承
        # 仅使用用户显式配置的代理，避免依赖特定机器的本地代理。
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        if https_proxy:
            self.session.proxies["https"] = https_proxy
        if http_proxy:
            self.session.proxies["http"] = http_proxy
        # 读取配置文件中的 Cookie（如 SESSDATA），用于访问登录态接口
        try:
            cfg = load_config("config.yaml")
            cookie = cfg.get("bilibili", {}).get("cookie", "")
            if cookie:
                self.session.headers["Cookie"] = cookie
        except Exception:
            pass
        self._wbi_key = None  # lazy init
        self._wbi_ts = 0

    # B站 WBI 签名固定置换表
    _MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]

    def _fetch_wbi_key(self):
        """获取 WBI 签名密钥（缓存1小时）"""
        now = int(time.time())
        if self._wbi_key and (now - self._wbi_ts) < 3600:
            return
        try:
            resp = self.session.get(
                f"{BASE_API}/x/web-interface/nav", timeout=self.timeout
            )
            data = resp.json().get("data", {})
            wbi = data.get("wbi_img", {})
            img_url = wbi.get("img_url", "")
            sub_url = wbi.get("sub_url", "")
            if img_url and sub_url:
                img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                raw = img_key + sub_key  # 64 chars
                self._wbi_key = "".join(raw[i] for i in self._MIXIN_KEY_ENC_TAB)[:32]
                self._wbi_ts = now
                logger.debug(f"WBI key refreshed: {self._wbi_key[:8]}...")
        except Exception as e:
            logger.warning(f"WBI key fetch failed: {e}")

    def _sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """为参数添加 WBI 签名"""
        self._fetch_wbi_key()
        if not self._wbi_key:
            return params
        params = dict(params)
        params["wts"] = int(time.time())
        # Filter characters not allowed by Bilibili signing spec
        filtered = {}
        for k, v in params.items():
            val = str(v)
            val = "".join(c for c in val if c not in "!'()*")
            filtered[k] = val
        # Sort by key and build query string
        sorted_keys = sorted(filtered.keys())
        query = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)
        sign_str = query + self._wbi_key
        params["w_rid"] = hashlib.md5(sign_str.encode()).hexdigest()
        return params

    def _get(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """发送 GET 请求（自动 WBI 签名）"""
        if params:
            params = self._sign_params(params)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"API 错误: {data.get('message', '未知错误')}")
                return {}
            return data.get("data", {})
        except requests.exceptions.ReadTimeout as e:
            logger.error(f"请求超时: {url} - {e}")
            return {}
        except requests.RequestException as e:
            logger.error(f"请求失败: {e}")
            return {}

    def get_video_info(self, bvid: Optional[str] = None, aid: Optional[int] = None) -> Dict[str, Any]:
        """获取视频基本信息"""
        url = f"{BASE_API}/x/web-interface/view"
        params = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            logger.error("必须提供 bvid 或 aid")
            return {}

        data = self._get(url, params)
        if not data:
            return {}

        # 提取关键信息
        owner = data.get("owner", {})
        stat = data.get("stat", {})

        # 保留原始完整描述，并提取描述中的 URL
        raw_desc = data.get("desc", "")
        desc_urls = re.findall(r"https?://[^\s\u4e00-\u9fff]+", raw_desc or "")

        return {
            "bvid": data.get("bvid", ""),
            "aid": data.get("aid", 0),
            "cid": data.get("cid", 0),
            "title": data.get("title", ""),
            # 清洗后的描述（去除多余空白），保留 raw_desc 供链接发现
            "description": (raw_desc or "").strip(),
            "raw_desc": raw_desc or "",
            "description_urls": desc_urls,
            "pic": data.get("pic", ""),
            "duration": data.get("duration", 0),
            "pubdate": data.get("pubdate", 0),
            "owner": {
                "mid": owner.get("mid", 0),
                "name": owner.get("name", ""),
                "face": owner.get("face", ""),
            },
            "stat": {
                "view": stat.get("view", 0),
                "danmaku": stat.get("danmaku", 0),
                "reply": stat.get("reply", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "favorite": stat.get("favorite", 0),
                "share": stat.get("share", 0),
            },
            "tname": data.get("tname", ""),  # 分区
            "videos": data.get("videos", 1),  # 分P数
            "pages": [
                {
                    "cid": p.get("cid", 0),
                    "page": p.get("page", 0),
                    "part": p.get("part", ""),
                    "duration": p.get("duration", 0),
                }
                for p in data.get("pages", [])
            ],
            "tags": [],  # 稍后通过 tag API 获取
        }

    def get_video_tags(self, bvid: Optional[str] = None, aid: Optional[int] = None) -> List[str]:
        """获取视频标签"""
        url = f"{BASE_API}/x/tag/archive/tags"
        params = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            return []

        data = self._get(url, params)
        if isinstance(data, list):
            return [tag.get("tag_name", "") for tag in data if tag.get("tag_name")]
        return []

    def get_danmaku(self, cid: int) -> List[Dict[str, Any]]:
        """获取弹幕列表 (XML 格式)"""
        url = f"{BASE_API}/x/v1/dm/list.so"
        params = {"oid": cid}

        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()

            # 处理压缩
            content = resp.content
            if content[:2] == b"\x1f\x8b":  # gzip
                content = zlib.decompress(content, 16 + zlib.MAX_WBITS)

            # 解析 XML
            root = ET.fromstring(content)
            danmaku_list = []
            for d in root.findall("d"):
                attr = d.get("p", "").split(",")
                if len(attr) >= 8:
                    danmaku_list.append({
                        "time": float(attr[0]),      # 出现时间
                        "type": int(attr[1]),         # 弹幕类型
                        "size": int(attr[2]),         # 字体大小
                        "color": int(attr[3]),        # 颜色
                        "timestamp": int(attr[4]),    # 发送时间戳
                        "pool": int(attr[5]),         # 弹幕池
                        "mid_hash": attr[6],          # 发送者 mid hash
                        "id": int(attr[7]),           # 弹幕 ID
                        "text": d.text or "",
                    })
            return danmaku_list
        except Exception as e:
            logger.error(f"获取弹幕失败: {e}")
            return []

    def get_comments(self, oid: int, page: int = 1, ps: int = 20) -> List[Dict[str, Any]]:
        """获取评论列表"""
        url = f"{BASE_API}/x/v2/reply"
        params = {
            "oid": oid,
            "type": 1,
            "pn": page,
            "ps": ps,
            "sort": 2,  # 按热度排序
        }

        data = self._get(url, params) or {}
        replies = data.get("replies", []) or []
        comments = []
        for r in replies:
            member = r.get("member", {})
            content = r.get("content", {})
            # 保留原始消息和点赞数，新增签名、楼层等元信息
            comments.append({
                "rpid": r.get("rpid", 0),
                "mid": member.get("mid", 0),
                "uname": member.get("uname", ""),
                "message": content.get("message", ""),
                "raw_message": content.get("message", ""),
                "like": r.get("like", 0),
                "ctime": r.get("ctime", 0),
                "replies_count": len(r.get("replies") or []),
                "sign": member.get("sign", ""),
                "level": member.get("level_info", {}).get("current_level", 0),
            })
        return comments

    def get_all_comments(self, oid: int, max_count: int = 100) -> List[Dict[str, Any]]:
        """获取所有评论 (分页)"""
        all_comments = []
        page = 1
        while len(all_comments) < max_count:
            comments = self.get_comments(oid, page=page, ps=20)
            if not comments:
                break
            all_comments.extend(comments)
            page += 1
            if len(comments) < 20:
                break
        return all_comments[:max_count]

    def get_up_videos(self, mid: int, page: int = 1, ps: int = 30) -> List[Dict[str, Any]]:
        """获取 UP 主视频列表"""
        url = f"{BASE_API}/x/space/wbi/arc/search"
        params = {
            "mid": mid,
            "pn": page,
            "ps": ps,
            "order": "pubdate",
        }

        data = self._get(url, params)
        vlist = data.get("list", {}).get("vlist", [])
        return [
            {
                "bvid": v.get("bvid", ""),
                "aid": v.get("aid", 0),
                "title": v.get("title", ""),
                "author": v.get("author", ""),
                "mid": v.get("mid", 0),
                "description": v.get("description", ""),
                "pic": v.get("pic", ""),
                "length": v.get("length", ""),
                "duration": self._parse_length(v.get("length", "")),
                "play": v.get("play", 0),
                "comment": v.get("comment", 0),
                "created": v.get("created", 0),
            }
            for v in vlist
        ]

    @staticmethod
    def _parse_length(length_str: str) -> int:
        """将 'mm:ss' 或 'hh:mm:ss' 格式转为秒数"""
        if not length_str:
            return 0
        parts = str(length_str).split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, TypeError):
            pass
        return 0

    def get_favorite_list(self, uid: int) -> List[Dict[str, Any]]:
        """获取用户收藏夹列表"""
        url = f"{BASE_API}/x/v3/fav/folder/created/list-all"
        params = {"up_mid": uid}
        data = self._get(url, params)
        folders = data.get("list", [])
        return [
            {"id": f["id"], "title": f["title"], "media_count": f["media_count"]}
            for f in folders
        ]

    def get_favorite_videos(
        self, media_id: int, page: int = 1, ps: int = 20
    ) -> List[Dict[str, Any]]:
        """获取收藏夹视频列表"""
        url = f"{BASE_API}/x/v3/fav/resource/list"
        params = {"media_id": media_id, "pn": page, "ps": ps, "platform": "web"}
        data = self._get(url, params)
        medias = data.get("medias", []) or []
        return [
            {
                "bvid": m.get("bvid", ""),
                "aid": m.get("id", 0),
                "title": m.get("title", ""),
                "upper_name": m.get("upper", {}).get("name", ""),
                "upper_mid": m.get("upper", {}).get("mid", 0),
                "cover": m.get("cover", ""),
                "duration": m.get("duration", 0),
                "pubdate": m.get("pubdate", 0),
                "favid": m.get("id", 0),
            }
            for m in medias
        ]

    def get_playurl(self, bvid: str, cid: int) -> Dict[str, Any]:
        """获取视频播放地址（DASH），用于 yt-dlp 失败时回退下载"""
        url = f"{BASE_API}/x/player/playurl"
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": 80,
            "fnval": 16,  # DASH
            "fourk": 1,
        }
        data = self._get(url, params) or {}
        if not data:
            return {}
        dash = data.get("dash", {})
        audio_urls = []
        for audio in dash.get("audio", []):
            base_url = audio.get("baseUrl") or audio.get("base_url")
            if base_url:
                audio_urls.append(base_url)
            for backup in audio.get("backupUrl", []) or audio.get("backup_url", []) or []:
                if backup and backup not in audio_urls:
                    audio_urls.append(backup)
        return {
            "audio_urls": audio_urls,
            "video_urls": [v.get("baseUrl") or v.get("base_url") for v in dash.get("video", [])],
            "duration": dash.get("duration", 0),
        }

    def resolve_url(self, url: str) -> Dict[str, Any]:
        """解析 URL 并获取完整视频信息"""
        bvid = extract_bvid(url)
        aid = extract_aid(url)

        if not bvid and not aid:
            logger.error(f"无法从 URL 解析视频 ID: {url}")
            return {}

        info = self.get_video_info(bvid=bvid, aid=aid)
        if info:
            info["tags"] = self.get_video_tags(bvid=info.get("bvid"), aid=info.get("aid"))
        return info


# 便捷函数
def fetch_video_data(url: str, max_danmaku: int = 500, max_comments: int = 100) -> Dict[str, Any]:
    """获取视频完整数据"""
    api = BilibiliAPI()
    info = api.resolve_url(url)
    if not info:
        return {}

    cid = info.get("cid", 0)
    aid = info.get("aid", 0)

    # 获取弹幕
    danmaku = api.get_danmaku(cid)[:max_danmaku]

    # 获取评论
    comments = api.get_all_comments(aid, max_count=max_comments)

    return {
        "info": info,
        "danmaku": danmaku,
        "comments": comments,
        "url": url,
    }
