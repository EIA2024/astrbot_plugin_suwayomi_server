from __future__ import annotations

import asyncio
import ipaddress
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

if TYPE_CHECKING:
    from ..suwayomi.client import SuwayomiClient

from ..suwayomi import PLUGIN_NAME

_PLUGIN_NAME = PLUGIN_NAME

# 文件打包路径（下载/AI 发送/自动推送 file 模式）的整章页数硬上限：
# 正常章节远低于此值，仅用于挡住恶意源宣告的超大页列表
FILE_DELIVERY_MAX_PAGES = 300

# 单张图片响应的字节上限（防恶意源用超大响应打满内存/磁盘）
_MAX_IMAGE_BYTES = 64 * 1024 * 1024


async def download_one(
    session: aiohttp.ClientSession, url: str, dest: Path, retries: int = 3
) -> bool:
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        size += len(chunk)
                        if size > _MAX_IMAGE_BYTES:
                            logger.warning(
                                f"[{_PLUGIN_NAME}] 图片响应超过 "
                                f"{_MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限，放弃: {url}"
                            )
                            return False
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    ext = ".jpg"
                    ct = resp.headers.get("Content-Type", "")
                    if "png" in ct:
                        ext = ".png"
                    elif "webp" in ct:
                        ext = ".webp"
                    dest = dest.with_suffix(ext)
                    dest.write_bytes(data)
                    return True
                elif resp.status < 500:
                    return False
                logger.warning(
                    f"[{_PLUGIN_NAME}] 图片下载 HTTP {resp.status}，"
                    f"重试 {attempt + 1}/{retries}: {url}"
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(
                f"[{_PLUGIN_NAME}] 图片下载超时/网络错误，"
                f"重试 {attempt + 1}/{retries}: {e}"
            )
        except Exception as e:
            logger.warning(f"[{_PLUGIN_NAME}] 图片下载异常: {e}")
            return False
        if attempt < retries - 1:
            await asyncio.sleep(0.5 * (2**attempt))
    return False


async def download_images(
    urls: list[str],
    concurrency: int = 6,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[list[str], Path]:
    if custom_tmp:
        Path(custom_tmp).mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="suwayomi_", dir=custom_tmp or None))
    try:
        connector = aiohttp.TCPConnector(limit=concurrency)
        session_kwargs: dict[str, Any] = {"connector": connector}
        if headers:
            session_kwargs["headers"] = headers
        async with aiohttp.ClientSession(**session_kwargs) as session:
            tasks = [
                download_one(session, url, tmp_dir / f"{i:04d}.jpg", retries)
                for i, url in enumerate(urls)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        paths: list[str] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    f"[{_PLUGIN_NAME}] 图片 {i + 1} 下载异常: {result}"
                )
                paths.append("")
            elif result:
                matches = sorted(tmp_dir.glob(f"{i:04d}.*"))
                paths.append(str(matches[-1]) if matches else "")
            else:
                paths.append("")
        return paths, tmp_dir
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _is_private_host(hostname: str | None) -> bool:
    """绝对 URL 的主机是否指向私网/环回/链路本地等不可达外网的目标。

    只识别字面 IP 与 localhost；域名解析到内网（DNS rebinding 类）不在此
    防护范围内，属已知限制。
    """
    host = (hostname or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def resolve_image_url(
    client: SuwayomiClient,
    thumbnail_url: str | None,
    auth_headers: dict[str, str] | None,
) -> tuple[str | None, dict[str, str] | None]:
    """Resolve a thumbnail URL to a fetchable URL plus the auth headers to use.

    Suwayomi's GraphQL ``thumbnailUrl`` is normally a server-relative path
    like ``/api/v1/manga/{id}/thumbnail``. Relative paths always carry the
    given ``auth_headers``; absolute URLs only when they point at the same
    server (scheme + host + port), to avoid leaking Suwayomi credentials to
    third-party hosts. 源扩展可控的第三方绝对地址若指向私网/环回目标
    则拒绝请求（防 SSRF），调用方按「无封面」降级。Returns
    ``(None, None)`` for empty or rejected input.
    """
    if not thumbnail_url:
        return None, None

    if thumbnail_url.startswith(("http://", "https://")):
        url = thumbnail_url
        use_headers = None
        same_server = False
        if client.server_url:
            server = urlparse(client.server_url)
            target = urlparse(thumbnail_url)
            server_port = server.port or (443 if server.scheme == "https" else 80 if server.scheme == "http" else None)
            target_port = target.port or (443 if target.scheme == "https" else 80 if target.scheme == "http" else None)
            same_server = (
                server.scheme == target.scheme
                and server.hostname == target.hostname
                and server_port == target_port
            )
            if same_server:
                use_headers = auth_headers
        if not same_server and _is_private_host(urlparse(thumbnail_url).hostname):
            logger.warning(
                f"[{_PLUGIN_NAME}] 拒绝下载指向私网/环回地址的第三方封面: "
                f"{urlparse(thumbnail_url).hostname}"
            )
            return None, None
    else:
        url = client.build_image_url(thumbnail_url)
        use_headers = auth_headers

    return url, use_headers


async def download_cover(
    client: SuwayomiClient,
    thumbnail_url: str | None,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, Path | None]:
    """Download a single manga cover to a temporary directory.

    Returns ``(local_path, tmp_dir)`` on success, or ``(None, None)`` when the
    cover is unavailable or cannot be downloaded. On success the caller is
    responsible for cleaning ``tmp_dir`` (e.g. via ``schedule_cleanup``).

    URL/auth resolution is delegated to ``resolve_image_url`` (single source
    of truth shared with the card renderer).
    """
    url, use_headers = resolve_image_url(client, thumbnail_url, headers)
    if url is None:
        return None, None

    try:
        paths, tmp_dir = await download_images(
            [url], concurrency=1, custom_tmp=custom_tmp, retries=retries, headers=use_headers
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        logger.warning(f"[{_PLUGIN_NAME}] 封面下载失败: {e}", exc_info=True)
        return None, None
    except Exception:
        logger.exception(f"[{_PLUGIN_NAME}] 封面下载出现未知异常")
        raise

    if not paths or not paths[0]:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None

    return paths[0], tmp_dir


async def fetch_pages_local(
    client: SuwayomiClient,
    chapter_id: int,
    max_pages: int = 0,
    concurrency: int = 6,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[str], list[str], Path | None]:
    pages = await client.fetch_chapter_pages(chapter_id)
    if not pages:
        return 0, [], [], None
    total_pages = len(pages)
    if max_pages > 0:
        pages = pages[:max_pages]
    page_urls = [client.build_image_url(p) for p in pages]
    local_paths, tmp_dir = await download_images(
        page_urls, concurrency, custom_tmp, retries, headers
    )
    return total_pages, page_urls, local_paths, tmp_dir
