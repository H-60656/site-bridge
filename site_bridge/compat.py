"""pydantic 大版本兼容小工具。

Termux / 安卓上装的是 pydantic 1.x（见 requirements-termux.txt：pydantic-core
要 Rust 编译，装不动），而桌面 `.venv` 里是 pydantic 2.x。两代取模型字段的
方法名不一样：

* pydantic 2.x —— ``model.model_dump(**kwargs)``
* pydantic 1.x —— ``model.dict(**kwargs)``

v1 没有 ``model_dump``，直接调用会抛 AttributeError 变成 500。所以路由里一律
走 ``dump_model()``。
"""

from __future__ import annotations


def dump_model(model, **kwargs):
    """按 pydantic 版本导出模型字段：2.x 用 model_dump()，1.x 退回 dict()。"""
    dump = getattr(model, "model_dump", None)
    if dump is None:
        dump = getattr(model, "dict", None)
    if dump is None:
        raise TypeError(
            "取不出字段：%s 既没有 model_dump() 也没有 dict()。" % type(model).__name__
        )
    return dump(**kwargs)
