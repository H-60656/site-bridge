# 上游 PixEz (lib/er/hoster.dart, Hoster._constMap) 中 app-api.pixiv.net 与
# oauth.secure.pixiv.net 共用同一个源站 IP：210.140.139.155。
# 原先首位使用的 210.140.92.x / 210.209.84.x 段经全网段扫描已确认无任何存活主机，
# 因此把上游当前 IP 放在首位，旧值保留在后面作为回退。
PIXIV_API_IPS = (
    "210.140.139.155",
    "210.140.92.136",
    "210.140.92.138",
    "210.209.84.142",
    "104.18.42.239",
    "172.64.145.17",
)
PIXIV_OAUTH_IPS = (
    "210.140.139.155",
    "210.140.92.144",
    "210.140.92.145",
    "210.209.84.141",
)
PIXIV_IMAGE_IPS = (
    "210.140.139.133",
    "210.140.139.134",
    "210.140.139.130",
    "210.140.139.136",
    "210.140.139.137",
    "210.140.139.138",
    "210.140.139.131",
    "210.140.139.129",
)

HOST_IP_POOLS = {
    "app-api.pixiv.net": PIXIV_API_IPS,
    "oauth.secure.pixiv.net": PIXIV_OAUTH_IPS,
    "i.pximg.net": PIXIV_IMAGE_IPS,
}

# ECH 的目标必须是 Cloudflare 边缘（pixiv 自己的源站 IP 不解 ECH）：
# 104.18.10.118 / 104.18.11.118 来自公开 ECH 配置里的 ipv4hint（PixEz 的 ech 模式用的也是这两个），
# 104.18.42.239 是 app-api.pixiv.net 经 DoH 解析出的 Cloudflare 地址。
# 原来的第四位 172.64.145.17 实测连 TCP 都建不起来（4/4 超时），已换成同段可达的
# 104.16.132.229；ECH 全失败时 ech_client 会把每个 IP 的子错误一并报出来。
PIXIV_ECH_IPS = (
    "104.18.10.118",
    "104.18.11.118",
    "104.18.42.239",
    "104.16.132.229",
)


def resolve(host, extra_ips=""):
    custom = tuple(ip.strip() for ip in extra_ips.split(",") if ip.strip())
    if custom:
        return custom
    return HOST_IP_POOLS.get(host, (host,))


def resolve_ech(host, extra_ips=""):
    """ECH 模式的目标 IP 池：不管哪个域名，都必须连到 Cloudflare 边缘。"""
    custom = tuple(ip.strip() for ip in extra_ips.split(",") if ip.strip())
    if custom:
        return custom
    return PIXIV_ECH_IPS
