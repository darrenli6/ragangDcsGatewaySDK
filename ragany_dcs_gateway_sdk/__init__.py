"""ragany_dcs_gateway_sdk —— 工业 DCS 网关 HTTP 采集 SDK（发酵工艺）。

版权所有 © 2026 Ragany DCS Gateway SDK 权利人。保留一切权利。
本项目为专有软件，未经书面授权不得复制、修改、反向工程、分发或商用。
完整条款见项目根目录 LICENSE 与 NOTICE.md。

快速开始::

    from ragany_dcs_gateway_sdk import DCSGatewayClient, PhPoint

    with DCSGatewayClient(base_url="http://192.168.1.88") as client:
        ph = client.read_point("s.CP201.D122.ph", PhPoint)
        print(ph.display())
"""

__version__ = "0.1.0"

from .client import AsyncDCSGatewayClient, DCSGatewayClient  # noqa: E402
from .config import GatewayConfig  # noqa: E402
from .exceptions import (  # noqa: E402
    AuthenticationError,
    ConfigError,
    DCSGatewayError,
    GatewayHTTPError,
    GatewayOfflineError,
    GatewayTimeoutError,
    PointValidationError,
    ResponseValidationError,
    TagNotFoundError,
)
from .models import (  # noqa: E402
    AccessMode,
    AnalogPoint,
    BatchData,
    BrowseNode,
    DataType,
    DissolvedOxygenPoint,
    LevelPoint,
    PhPoint,
    PressurePoint,
    ProcessPoint,
    TagInfo,
    TagValue,
    TemperaturePoint,
    TempPoint,
)
from .utils import PointSpec, TagMap, build_tag_path  # noqa: E402

__all__ = [
    "__version__",
    # clients
    "DCSGatewayClient",
    "AsyncDCSGatewayClient",
    "GatewayConfig",
    # models
    "AccessMode",
    "AnalogPoint",
    "BatchData",
    "BrowseNode",
    "DataType",
    "DissolvedOxygenPoint",
    "LevelPoint",
    "PhPoint",
    "PressurePoint",
    "ProcessPoint",
    "TagInfo",
    "TagValue",
    "TemperaturePoint",
    "TempPoint",
    # mapping
    "PointSpec",
    "TagMap",
    "build_tag_path",
    # exceptions
    "AuthenticationError",
    "ConfigError",
    "DCSGatewayError",
    "GatewayHTTPError",
    "GatewayOfflineError",
    "GatewayTimeoutError",
    "PointValidationError",
    "ResponseValidationError",
    "TagNotFoundError",
]
