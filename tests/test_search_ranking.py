"""Command-level tests for search result ranking (no network)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from plugin_pkg.main import SuwayomiPlugin
from plugin_pkg.suwayomi.cards import CardCache
from plugin_pkg.suwayomi.models import Manga, SearchResult, Source

QUERY = "我的首推是恶役大小姐"


def _plugin(**config_overrides):
    plugin = SuwayomiPlugin.__new__(SuwayomiPlugin)
    plugin.client = MagicMock()
    plugin.client.auth_headers = {}
    plugin.sub_mgr = MagicMock()
    plugin.config = {
        "result_cards_enabled": False,
        **config_overrides,
    }
    plugin.get_kv_data = AsyncMock(return_value={})
    plugin.put_kv_data = AsyncMock()
    plugin._search_cache = {}
    plugin._card_cache = CardCache(ttl=600)
    plugin._card_cooldown_until = 0.0
    plugin.html_render = AsyncMock(return_value="/tmp/card.jpg")
    return plugin


def _event(message="/漫画 搜索 " + QUERY):
    event = MagicMock()
    event.unified_msg_origin = "aiocqhttp:group:g1"
    event.message_str = message
    event.plain_result = MagicMock(side_effect=lambda text: text)
    event.chain_result = MagicMock(side_effect=lambda chain: chain)
    event.send = AsyncMock()
    return event


def _manga(title, mid):
    return Manga(id=mid, source_id=2, url="", title=title, status="ONGOING",
                 thumbnail_url=None, description="")


def _sources():
    return [
        Source(id="11", name="dm5", lang="zh", display_name="动漫屋"),
        Source(id="22", name="mh", lang="zh", display_name="漫画社"),
    ]


def _set_search(plugin, per_source):
    plugin.client.get_sources = AsyncMock(return_value=_sources())

    async def _fake(src_id, query, page=1):
        for sid, mangas in per_source.items():
            if str(src_id) == sid:
                return SearchResult(mangas=list(mangas), has_next_page=False)
        return SearchResult(mangas=[], has_next_page=False)

    plugin.client.search_manga = AsyncMock(side_effect=_fake)


@pytest.mark.asyncio
async def test_ranking_orders_cross_source_results():
    """跨源混排：正篇/番外压过其它源的模糊匹配，行内标注来源。"""
    plugin = _plugin()
    _set_search(plugin, {
        "11": [_manga("恶役大小姐的执事大人", 101), _manga("异世界美食之旅", 102)],
        "22": [_manga("我的首推是恶役大小姐（番外）", 201), _manga("我的首推是恶役大小姐", 202)],
    })
    results = [msg async for msg in plugin.search_manga(_event(), QUERY)]
    text = results[0]
    assert text.index("我的首推是恶役大小姐 - ") < text.index("（番外）")
    assert text.index("（番外）") < text.index("执事大人")
    assert "（漫画社）" in text and "（动漫屋）" in text
    assert "按相关度排序" in text
    # 编号映射与显示一致：订阅 1 = 正篇
    assert plugin._get_cached_manga("aiocqhttp:group:g1", "1").title == "我的首推是恶役大小姐"


@pytest.mark.asyncio
async def test_display_limit_caps_output_and_cache():
    mangas = [_manga(f"恶役大小姐衍生物语第{i}季", 100 + i) for i in range(15)]
    plugin = _plugin(search_display_limit=5)
    _set_search(plugin, {"11": mangas})
    results = [msg async for msg in plugin.search_manga(_event(), QUERY)]
    text = results[0]
    assert "[5]" in text and "[6]" not in text
    assert "已按相关度显示前 5 条（共 15 条）" in text
    assert plugin._get_cached_manga("aiocqhttp:group:g1", "5") is not None
    assert plugin._get_cached_manga("aiocqhttp:group:g1", "6") is None


@pytest.mark.asyncio
async def test_ranking_disabled_keeps_legacy_grouped_format():
    plugin = _plugin(search_result_ranking=False)
    _set_search(plugin, {
        "11": [_manga("恶役大小姐的执事大人", 101)],
        "22": [_manga("我的首推是恶役大小姐", 202)],
    })
    results = [msg async for msg in plugin.search_manga(_event(), QUERY)]
    text = results[0]
    assert "搜索结果（源: 动漫屋）" in text
    assert "搜索结果（源: 漫画社）" in text
    assert "按相关度" not in text
    assert text.index("源: 动漫屋") < text.index("源: 漫画社")


@pytest.mark.asyncio
async def test_zero_results():
    plugin = _plugin()
    _set_search(plugin, {"11": [], "22": []})
    results = [msg async for msg in plugin.search_manga(_event(), QUERY)]
    assert "未找到相关漫画" in results[0]
