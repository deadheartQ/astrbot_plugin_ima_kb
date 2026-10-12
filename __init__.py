"""astrbot_plugin_ima_kb 包初始化。

AstrBot 通过 `__import__("data.plugins.<插件名>.main")` 加载插件，靠的是
**隐式命名空间包**（Python 3.3+ 无 __init__.py 也能导入）。但这些命名空间包
在 `pip_installer` 补依赖期间动过 sys.path / sys.modules 之后可能失效，
于是**延迟执行的相对导入**（`from .core.ima_client import ImaClient`）会报
`No module named 'data.plugins.astrbot_plugin_ima_kb'`。

本插件有 `requirements.txt`（httpx），会触发依赖安装流程，所以这里显式把父级
命名空间包登记进 sys.modules，把这个失效路径堵掉。写法参考
NickCharlie/astrbot_plugin_self_learning 的同名文件（AGPL-3.0，此处仅借鉴思路）。
"""

__version__ = "1.0.0"

import sys as _sys
import types as _types

_pkg_name = __name__  # 例如 "data.plugins.astrbot_plugin_ima_kb"
_parts = _pkg_name.split(".")
for _i in range(1, len(_parts)):
    _ns = ".".join(_parts[:_i])
    if _ns not in _sys.modules:
        _mod = _types.ModuleType(_ns)
        _mod.__path__ = []
        _mod.__package__ = _ns
        _sys.modules[_ns] = _mod
