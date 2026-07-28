import os
from urllib.parse import urlparse

_PROXY_URL = os.getenv('MCP_FOREIGN_PROXY_URL', 'socks5h://127.0.0.1:10808').strip()
_DIRECT_SUFFIXES = tuple(
    item.strip().lower()
    for item in os.getenv(
        'MCP_DIRECT_DOMAIN_SUFFIXES',
        '.cn,.com.cn,.net.cn,.org.cn,.gov.cn,.edu.cn,.alicdn.com,.aliyuncs.com,.iweaver.ai,.xiaoduoai.com'
    ).split(',')
    if item.strip()
)
_DIRECT_HOSTS = {
    'localhost',
    '127.0.0.1',
    '::1',
    'galaxy.iweaver.ai',
    'agent.xiaoduoai.com',
}

_PATCHED = False


def _is_private_ipv4(host):
    parts = host.split('.')
    if len(parts) != 4:
        return False
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return False
    if nums[0] == 10:
        return True
    if nums[0] == 172 and 16 <= nums[1] <= 31:
        return True
    if nums[0] == 192 and nums[1] == 168:
        return True
    if nums[0] == 169 and nums[1] == 254:
        return True
    return False


def should_proxy(url):
    if not _PROXY_URL:
        return False
    parsed = urlparse(str(url))
    host = (parsed.hostname or '').lower().strip('.')
    if not host:
        return False
    if host in _DIRECT_HOSTS or _is_private_ipv4(host):
        return False
    if any(host.endswith(suffix) for suffix in _DIRECT_SUFFIXES):
        return False
    return True


def proxies_for(url):
    if should_proxy(url):
        return {'http': _PROXY_URL, 'https': _PROXY_URL}
    return None


def patch_requests():
    global _PATCHED
    if _PATCHED:
        return
    import requests

    original_request = requests.sessions.Session.request

    def routed_request(self, method, url, **kwargs):
        if kwargs.get('proxies') is None:
            proxies = proxies_for(url)
            if proxies:
                kwargs['proxies'] = proxies
        return original_request(self, method, url, **kwargs)

    requests.sessions.Session.request = routed_request
    _PATCHED = True
