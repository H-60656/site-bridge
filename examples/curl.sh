#!/usr/bin/env bash
set -euo pipefail

BASE="${SITE_BRIDGE_URL:-http://127.0.0.1:8765}"

echo "== 健康检查 =="
curl -s "$BASE/api/health"
echo

echo "== 认证状态 =="
curl -s "$BASE/api/auth/status"
echo

echo "== 插画详情（替换 ILLUST_ID） =="
curl -s "$BASE/api/illust/${ILLUST_ID:-12345678}"
echo

echo "== 日榜排行 =="
curl -s "$BASE/api/ranking?mode=day"
echo

echo "== 搜索插画（中文词自动 URL 编码） =="
curl -s -G "$BASE/api/search/illust" \
  --data-urlencode "word=初音ミク" \
  --data-urlencode "sort=popular_desc"
echo

echo "== 用户作品 =="
curl -s "$BASE/api/user/${USER_ID:-11}/illusts?type=illust"
echo

echo "== 翻页：把上一响应里的 next_url 传给 /api/next =="
curl -s -G "$BASE/api/next" \
  --data-urlencode "url=https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30&filter=for_android"
echo

echo "== 图片代理（绕过 i.pximg.net 防盗链） =="
curl -s -o sample.jpg -w "http_code=%{http_code} size=%{size_download}\n" \
  "$BASE/img?url=https://i.pximg.net/c/250x250_80_a2/img-master/img/2023/01/01/00/00/00/11122333_p0_master1200.jpg"
