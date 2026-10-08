"""框架自带路由（导航页 ``/``、健康检查 ``/api/health``）。

站点自己的路由与 ``app.state`` 访问器在 ``<site>/routes/`` 里（例如
``pixiv/routes/__init__.py`` 的 ``get_api`` / ``get_oauth`` / ``get_image_proxy``）。
"""
