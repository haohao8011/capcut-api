"""pytest 共享配置：必须在 import server 模块前设置环境变量。"""
import os
import warnings

# 关闭登录限流（测试时同一 TestClient 的 IP 触发 5/min 限制）
os.environ.setdefault("CAPCUT_LOGIN_RATE_LIMIT", "0")

# 抑制 starlette 1.x + httpx 内部 deprecation warning（TestClient 用 httpx 转发）
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module="starlette",
)

# 测试用临时数据目录（避免污染真实 data/）
os.environ.setdefault("CAPCUT_TESTING", "1")
