"""HTTP 基础设施：账号内共享连接池、按任务隔离 Cookie、统一响应结构。"""

import copy
import json
from urllib.parse import urlparse

import httpx

from cu_tasks.common import format_exception_detail

DEFAULT_TIMEOUT = httpx.Timeout(50.0, connect=15.0)


class SharedTransport(httpx.AsyncBaseTransport):
    """让多个客户端复用同一连接池；连接池由所有者（Account）统一关闭。"""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request):
        return await self._inner.handle_async_request(request)

    async def aclose(self):
        """客户端关闭时不关闭共享连接池。"""


def new_client(transport, *, headers=None, cookies=None) -> httpx.AsyncClient:
    """新建客户端：复用连接池，复制（而非共享）Cookie，任务之间互不覆盖。"""
    client = httpx.AsyncClient(
        transport=SharedTransport(transport),
        follow_redirects=False,
        timeout=DEFAULT_TIMEOUT,
        headers=headers,
    )
    if cookies is not None:
        for cookie in cookies.jar:
            client.cookies.jar.set_cookie(copy.copy(cookie))
    return client


async def request(client: httpx.AsyncClient, method, url, **kw) -> dict:
    """统一响应：{"code", "headers", "data", ["error"]}；网络异常不抛出，code=-1。"""
    allow_redirects = kw.pop("allow_redirects", True)
    try:
        r = await client.request(method, url, follow_redirects=allow_redirects, **kw)
    except httpx.RequestError as e:
        path = urlparse(url).path or url
        return {
            "code": -1,
            "headers": {},
            "data": None,
            "error": format_exception_detail(e, f"{method} {path}"),
        }
    body = r.text
    if body.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            path = urlparse(url).path or url
            return {
                "code": r.status_code,
                "headers": r.headers,
                "data": None,
                "error": format_exception_detail(e, f"{method} {path}"),
            }
    else:
        data = body
    return {"code": r.status_code, "headers": r.headers, "data": data}
