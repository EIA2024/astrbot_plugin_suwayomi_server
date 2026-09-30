"""Bangumi (bgm.tv) metadata resolution for search alias expansion.

生态先例：Tachiyomi/Mihon 扩展库的 BangumiScraper、Aidoku、KikoPlay 等
均以 api.bgm.tv 的模糊搜索做标题解析。本模块先落地「解析层」：
关键词 → 搜索 → 取前若干条目（按搜索排名）→ 合并官方中文名/日文名/
别名表。后续提交在此之上接入搜索命令的重搜流程。

要点（实测校准，详见各函数注释）：
- 条目池取搜索排名前 5：正解常在第 4-5 位，且其官方长名对"带错字的
  关键词"打分很低，按分挑选反而会漏
- 首轮无强命中时用日文字形变体重试一次（转天 → 転天——「転天」这类
  俗称缩写常被官方收录在别名表里）
- 任何网络/解析失败返回 None，调用方静默跳过扩展
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field

import aiohttp

from .ranking import (
    STRONG_MATCH_THRESHOLD,
    jp_variant,
    score_title,
)

OFFICIAL_API_BASE = "https://api.bgm.tv"

_SEARCH_PATH = "/search/subject/{kw}?type=1&max_results=8"
_SUBJECT_PATH = "/v0/subjects/{sid}"
_HEADERS = {
    "User-Agent": (
        "astrbot_plugin_suwayomi_server "
        "(https://github.com/FFFold/astrbot_plugin_suwayomi_server)"
    )
}
_REQUEST_TIMEOUT = 8.0

_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class Resolution:
    """Bangumi 解析结果：条目 → 别名集合，及关键词的置信判定。"""

    by_subject: dict[int, list[str]] = field(default_factory=dict)
    confident: bool = False
    best_alias_score: float = 0.0

    def all_aliases(self) -> list[str]:
        seen: list[str] = []
        for names in self.by_subject.values():
            for name in names:
                if name not in seen:
                    seen.append(name)
        return seen


def parse_search(raw: dict) -> list[int]:
    """从搜索响应提取条目 id 列表（保持排名顺序）。"""
    items = (raw or {}).get("list") or []
    ids: list[int] = []
    for item in items:
        try:
            sid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if sid not in ids:
            ids.append(sid)
    return ids


def parse_subject(raw: dict) -> list[str]:
    """从条目详情提取名称集合：官方中文名、日文名、别名表。"""
    names: list[str] = []
    for key in ("name_cn", "name"):
        value = (raw or {}).get(key)
        if value and value not in names:
            names.append(value)
    for item in (raw or {}).get("infobox") or []:
        if item.get("key") != "别名":
            continue
        value = item.get("value")
        if not isinstance(value, list):
            continue
        for entry in value:
            text = entry.get("v") if isinstance(entry, dict) else str(entry)
            if text and text not in names:
                names.append(text)
    return names


def best_alias_score(query: str, resolution: Resolution) -> float:
    best = 0.0
    for names in resolution.by_subject.values():
        for alias in names:
            best = max(best, score_title(query, alias))
    return best


def confident_aliases(query: str, resolution: Resolution) -> list[str]:
    """得分达到强命中阈值的官方别名（提示行展示用）。"""
    out: list[str] = []
    for names in resolution.by_subject.values():
        for alias in names:
            if score_title(query, alias) >= STRONG_MATCH_THRESHOLD and alias not in out:
                out.append(alias)
    return out[:3]


def _quote_keyword(keyword: str) -> str:
    # safe="" 防路径段注入（关键词含 / 时不再拼出多段路径，如
    # 「海贼王/航海王」这类输入曾导致请求落到错误路径而静默失败）；
    # '+' 是本插件多词连接符，bgm 侧按空格分词，先归一。
    return urllib.parse.quote(keyword.replace("+", " "), safe="")


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, headers=_HEADERS) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def resolve_aliases(
    query: str,
    max_subjects: int = 5,
    timeout: float = _REQUEST_TIMEOUT,
) -> Resolution | None:
    """解析关键词的官方条目与别名。失败返回 None（调用方静默跳过扩展）。"""
    query = str(query or "").strip()
    if not query:
        return None
    return await _resolve_via(OFFICIAL_API_BASE, query, max_subjects, timeout)


async def _resolve_via(
    base: str,
    query: str,
    max_subjects: int,
    timeout: float,
) -> Resolution | None:
    resolution = Resolution()
    # trust_env: 若容器配置了 HTTP(S)_PROXY 环境变量（如自建 clash）则自动走代理；
    # 部分网络环境下 api.bgm.tv 的线路被干扰，代理是恢复解析的通路之一。
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg, trust_env=True) as session:

            async def _collect(keyword: str) -> None:
                raw = await _get_json(
                    session, base + _SEARCH_PATH.format(kw=_quote_keyword(keyword))
                )
                sids = parse_search(raw)[:max_subjects]

                async def _fetch(sid: int) -> None:
                    try:
                        subject = await _get_json(
                            session, base + _SUBJECT_PATH.format(sid=sid)
                        )
                    except Exception:
                        return
                    names = parse_subject(subject)
                    if names:
                        resolution.by_subject[sid] = names

                # 条目详情并发拉取，单个失败跳过不阻塞
                await asyncio.gather(*(_fetch(sid) for sid in sids))

            await _collect(query)
            if best_alias_score(query, resolution) < STRONG_MATCH_THRESHOLD:
                jp = jp_variant(query)
                if jp and jp != query:
                    await _collect(jp)
    except Exception:
        return None

    resolution.best_alias_score = best_alias_score(query, resolution)
    resolution.confident = resolution.best_alias_score >= STRONG_MATCH_THRESHOLD
    return resolution
