"""离线测试工具：模拟 DCS 网关，无需现场设备即可开发与测试。

示例::

    from ragany_dcs_gateway_sdk import DCSGatewayClient
    from ragany_dcs_gateway_sdk.testing import FakeGateway

    gw = FakeGateway()
    client = DCSGatewayClient(transport=gw.transport())
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from .utils import join_path

# 一个发酵罐的示例点位树：驱动 s / 通道 CP201 / 设备 D122（F-101 发酵罐）
DEMO_TREE: dict[str, Any] = {
    "name": "VarDevice",
    "tags": [],
    "drivers": [
        {
            "name": "s",
            "d": "Modbus 驱动",
            "channels": [
                {
                    "name": "CP201",
                    "d": "通道 CP201",
                    "devices": [
                        {
                            "name": "D122",
                            "d": "F-101 发酵罐",
                            "tags": [
                                {"n": "ph", "d": "发酵液 pH", "t": 6, "u": 95, "v": "6.85", "w": 0},
                                {"n": "temp", "d": "罐温", "t": 6, "u": 1, "v": "37.2", "w": 0},
                                {"n": "level", "d": "液位", "t": 6, "u": 2, "v": "68.5", "w": 0},
                                {"n": "do", "d": "溶氧", "t": 6, "u": 2, "v": "42.0", "w": 0},
                                {"n": "pressure", "d": "罐压", "t": 6, "u": 3, "v": "0.05", "w": 0},
                                {"n": "qq1", "d": "搅拌转速", "t": 3, "u": 95, "v": "9", "w": 1},
                                {"n": "qq2", "d": "补料泵启停", "t": 1, "u": 95, "v": "0", "w": 1},
                            ],
                            "groups": [
                                {
                                    "name": "Alarm",
                                    "tags": [
                                        {"n": "high_temp", "d": "高温报警", "t": 1, "u": 95, "v": "0", "w": 0},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}

Failure = str | int  # "connect" | "timeout" | HTTP 状态码


class FakeGateway:
    """基于 :class:`httpx.MockTransport` 的网关模拟器，同时支持同步与异步客户端。

    - ``values``：点位当前值，默认取自点位树中的 ``v`` 字段，可用 :meth:`set_value` 修改
    - ``fail_next``：注入故障（连接失败 / 超时 / 指定 HTTP 状态码），用于验证重试逻辑
    - ``calls``：记录收到的所有请求 ``(函数名, 请求体, headers)``
    """

    def __init__(
        self,
        tree: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        prefix: str = "/soap",
    ) -> None:
        self.tree = copy.deepcopy(tree if tree is not None else DEMO_TREE)
        self.token = token
        self.prefix = prefix.rstrip("/")
        self.values: dict[str, Any] = {}
        self._collect(self.tree, "")
        self.calls: list[tuple[str, Any, httpx.Headers]] = []
        self._failures: list[Failure] = []
        self.overrides: dict[str, httpx.Response] = {}

    # ---- 配置 ----
    def _collect(self, node: dict[str, Any], prefix: str) -> None:
        for tag in node.get("tags") or []:
            self.values[join_path(prefix, tag["n"])] = tag.get("v")
        for key in ("drivers", "channels", "devices", "groups"):
            for child in node.get(key) or []:
                self._collect(child, join_path(prefix, child["name"]))

    def set_value(self, name: str, value: Any) -> None:
        self.values[name] = value

    def fail_next(self, *failures: Failure) -> None:
        """依次注入故障，例如 ``fail_next("connect", "timeout", 503)``。"""
        self._failures.extend(failures)

    def respond_with(self, function: str, response: httpx.Response) -> None:
        """固定某个函数的响应（用于构造畸形报文）。"""
        self.overrides[function] = response

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls_to(self, function: str) -> list[Any]:
        return [body for fn, body, _ in self.calls if fn == function]

    # ---- 请求处理 ----
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        function = path[len(self.prefix) + 1 :] if path.startswith(self.prefix + "/") else path
        body = json.loads(request.content or b"null")
        self.calls.append((function, body, request.headers))

        if self._failures:
            failure = self._failures.pop(0)
            if failure == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            return httpx.Response(int(failure), text="injected failure")

        if self.token is not None and request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="unauthorized")

        if function in self.overrides:
            return self.overrides[function]
        if request.method != "POST":
            return httpx.Response(405)
        if function == "BrowseTag":
            return self._browse(body)
        if function == "ReadTag":
            return self._read(body)
        return httpx.Response(404, text=f"unknown function {function}")

    def _browse(self, body: Any) -> httpx.Response:
        node_path = (body or {}).get("node", "")
        node: dict[str, Any] | None = self.tree
        if node_path:
            for seg in node_path.split("."):
                children = [c for k in ("drivers", "channels", "devices", "groups") for c in (node or {}).get(k) or []]
                node = next((c for c in children if c["name"] == seg), None)
                if node is None:
                    return httpx.Response(404, text=f"node not found: {node_path}")
        result: dict[str, Any] = copy.deepcopy(node or {})
        self._refresh_values(result, node_path)
        return httpx.Response(200, json=result)

    def _refresh_values(self, node: dict[str, Any], prefix: str) -> None:
        for tag in node.get("tags") or []:
            tag["v"] = self.values.get(join_path(prefix, tag["n"]), tag.get("v"))
        for key in ("drivers", "channels", "devices", "groups"):
            for child in node.get(key) or []:
                self._refresh_values(child, join_path(prefix, child["name"]))

    def _read(self, body: Any) -> httpx.Response:
        data = [
            {"name": item["name"], "value": self.values[item["name"]]}
            for item in (body or {}).get("data", [])
            if item.get("name") in self.values  # 不存在的点位不返回
        ]
        return httpx.Response(200, json={"data": data})


__all__ = ["DEMO_TREE", "FakeGateway"]
