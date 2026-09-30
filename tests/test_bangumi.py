"""Bangumi 条目解析模块测试（纯逻辑层，网络层 mock）。"""
from unittest.mock import AsyncMock, patch

import pytest
from plugin_pkg.suwayomi import bangumi
from plugin_pkg.suwayomi.bangumi import (
    Resolution,
    best_alias_score,
    confident_aliases,
    parse_search,
    parse_subject,
    resolve_aliases,
)
from plugin_pkg.suwayomi.ranking import STRONG_MATCH_THRESHOLD


def test_parse_search_keeps_order_and_dedupes():
    raw = {"list": [{"id": 3}, {"id": 1}, {"id": 3}, {"id": "bad"}, {"id": 2}]}
    assert parse_search(raw) == [3, 1, 2]
    assert parse_search({}) == []
    assert parse_search(None) == []


def test_parse_subject_names_and_aliases():
    raw = {
        "name": "転生王女と天才令嬢の魔法革命",
        "name_cn": "转生王女与天才千金的魔法革命",
        "infobox": [
            {"key": "中文名", "value": "x"},
            {"key": "别名", "value": [{"v": "転天"}, {"v": "MagiRevo"}, "転天革命"]},
        ],
    }
    names = parse_subject(raw)
    assert names[0] == "转生王女与天才千金的魔法革命"
    assert "転天" in names and "転天革命" in names and "MagiRevo" in names


def _resolution():
    return Resolution(
        by_subject={
            311834: [
                "转生王女与天才千金的魔法革命",
                "転生王女と天才令嬢の魔法革命",
                "転天",
                "MagiRevo",
            ],
            490753: [
                "无力圣女与无能王女～魔力值零却被召唤的圣女异世界救国记～",
                "无用圣女与无能王女～被召唤至异世界的零魔力圣女救国纪～",
            ],
        }
    )


def test_confidence_via_jp_alias_equality():
    r = _resolution()
    r.best_alias_score = best_alias_score("转天", r)
    assert r.best_alias_score == 1000.0  # 転天 归一化后与 转天 相等
    r.confident = True
    assert "転天" in confident_aliases("转天", r)  # 全名因子序列也过线，但 転天 必在列


@pytest.mark.asyncio
async def test_resolve_aliases_network_failure_returns_none():
    with patch.object(bangumi, "_get_json", AsyncMock(side_effect=Exception("boom"))):
        assert await resolve_aliases("转天") is None


@pytest.mark.asyncio
async def test_resolve_aliases_jp_retry_used_when_first_round_weak():
    """第一轮弱命中 → 用日文字形变体再搜一次。"""
    search_responses = {
        "https://api.bgm.tv/search/subject/%E8%BD%AC%E5%A4%A9?type=1&max_results=8": {"list": []},
        "https://api.bgm.tv/search/subject/%E8%BB%A2%E5%A4%A9?type=1&max_results=8": {"list": [{"id": 311834}]},
    }
    subject_response = {
        "name": "転生王女と天才令嬢の魔法革命",
        "name_cn": "转生王女与天才千金的魔法革命",
        "infobox": [{"key": "别名", "value": [{"v": "転天"}]}],
    }

    async def fake_get_json(session, url):
        if url in search_responses:
            return search_responses[url]
        return subject_response

    with patch.object(bangumi, "_get_json", side_effect=fake_get_json):
        resolution = await resolve_aliases("转天")
    assert resolution is not None
    assert 311834 in resolution.by_subject
    assert resolution.confident
    assert resolution.best_alias_score == 1000.0


@pytest.mark.asyncio
async def test_resolve_aliases_skips_jp_retry_when_confident():
    calls = []

    async def fake_get_json(session, url):
        calls.append(url)
        if "/v0/subjects/" in url:
            return {
                "name": "我的首推是恶役大小姐",
                "name_cn": "我的首推是恶役大小姐",
                "infobox": [],
            }
        return {"list": [{"id": 1}]}

    with patch.object(bangumi, "_get_json", side_effect=fake_get_json):
        resolution = await resolve_aliases("我的首推是恶役大小姐")
    assert resolution.confident
    search_calls = [u for u in calls if "search/subject" in u]
    assert len(search_calls) == 1  # 置信则不做日文重试
