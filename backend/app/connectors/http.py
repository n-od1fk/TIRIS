import ipaddress
import json
import socket
import time
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.config import get_settings
from app.connectors.contracts import RetryableSourceError, SourceError


def canonicalize_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            host += f":{parsed.port}"
        query = urlencode(
            [
                (key, val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
            ]
        )
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))
    except (ValueError, UnicodeError) as exc:
        raise SourceError("仅支持无账号密码、使用 80/443 端口的 HTTP/HTTPS URL") from exc


def resolve_doh(hostname: str) -> list[str]:
    """Explicit opt-in public DNS for Fake-IP environments; no proxy or TLS bypass."""
    request = httpx.Request(
        "GET",
        "https://1.1.1.1/dns-query",
        params={"name": hostname, "type": "A"},
        headers={"Host": "cloudflare-dns.com", "Accept": "application/dns-json"},
        extensions={"sni_hostname": "cloudflare-dns.com"},
    )
    try:
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            with closing(client.send(request, stream=True)) as response:
                if response.status_code != 200:
                    raise RetryableSourceError("加密 DNS 暂不可用")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 16_384:
                        raise SourceError("加密 DNS 响应格式异常")
                data = json.loads(payload)
                if data.get("Status") != 0:
                    raise RetryableSourceError("加密 DNS 未解析到来源域名")
                return list(
                    dict.fromkeys(
                        answer["data"]
                        for answer in data.get("Answer", [])
                        if answer.get("type") == 1
                    )
                )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RetryableSourceError("加密 DNS 请求失败，未回退到不安全地址") from exc


def resolve_public_host(url: str, mode: str = "system") -> list[str]:
    parsed = urlsplit(canonicalize_url(url))
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    elif mode == "doh":
        addresses = resolve_doh(parsed.hostname)
    else:
        addresses = resolve_system_host(parsed)
    try:
        public_only = addresses and all(ipaddress.ip_address(ip).is_global for ip in addresses)
    except ValueError:
        public_only = False
    if not public_only:
        raise SourceError("拒绝访问本机、内网、保留或云元数据地址；Fake-IP 环境请查看 DNS 配置说明")
    return addresses


def resolve_system_host(parsed) -> list[str]:
    try:
        addresses = list(
            dict.fromkeys(
                record[4][0]
                for record in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            )
        )
    except OSError as exc:
        raise RetryableSourceError("来源域名无法解析，请稍后重试") from exc
    return addresses


class PublicTransport(httpx.BaseTransport):
    def __init__(self, dns_mode: str = "system"):
        self.transport = httpx.HTTPTransport(retries=0)
        self.dns_mode = dns_mode

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        addresses = resolve_public_host(str(request.url), self.dns_mode)
        hostname = request.url.host
        # Pin the validated IP while preserving Host and TLS SNI to avoid DNS rebinding.
        request.headers["Host"] = request.url.netloc.decode("ascii")
        # Pool origins are now IPs. HTTP/1.1 close prevents TLS reuse across different
        # original hostnames that happen to share that IP (e.g. a CDN).
        request.headers["Connection"] = "close"
        request.extensions["sni_hostname"] = hostname
        pinned_ip = next((ip for ip in addresses if ":" not in ip), addresses[0])
        request.url = request.url.copy_with(host=pinned_ip)
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


class SafeHTTP:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.settings = get_settings()
        self.client = httpx.Client(
            transport=transport or PublicTransport(self.settings.fetch_dns_mode),
            trust_env=False,
            timeout=self.settings.fetch_timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "TIRIS/0.1 (personal technical reading tool)"},
        )
        self.last_request: dict[str, float] = {}

    def close(self):
        self.client.close()

    def get(
        self, url: str, headers: dict | None = None, *, max_bytes: int | None = None
    ) -> httpx.Response:
        byte_limit = min(
            max_bytes or self.settings.fetch_max_bytes, self.settings.material_max_bytes
        )
        target = canonicalize_url(url)
        request_headers = dict(headers or {})
        deadline = time.monotonic() + self.settings.fetch_timeout_seconds * 2
        for _ in range(6):
            host = urlsplit(target).hostname
            delay = self.settings.fetch_domain_interval_seconds - (
                time.monotonic() - self.last_request.get(host, 0)
            )
            if delay > 0:
                time.sleep(delay)
            self.last_request[host] = time.monotonic()
            if time.monotonic() > deadline:
                raise RetryableSourceError("来源下载超过总时间预算")
            try:
                with self.client.stream("GET", target, headers=request_headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceError("来源返回无目标地址的重定向")
                        redirected = canonicalize_url(str(httpx.URL(target).join(location)))
                        if urlsplit(redirected).netloc != urlsplit(target).netloc or (
                            urlsplit(redirected).scheme != urlsplit(target).scheme
                        ):
                            request_headers = {
                                key: value
                                for key, value in request_headers.items()
                                if key.lower() != "authorization"
                            }
                        target = redirected
                        continue
                    if response.status_code == 304:
                        return httpx.Response(
                            304, headers=response.headers, request=response.request
                        )
                    if (
                        response.status_code == 429
                        or response.status_code >= 500
                        or (
                            response.status_code == 403
                            and (
                                response.headers.get("x-ratelimit-remaining") == "0"
                                or "retry-after" in response.headers
                            )
                        )
                    ):
                        raise RetryableSourceError(
                            f"来源暂不可用或限流（HTTP {response.status_code}）"
                        )
                    if response.status_code >= 400:
                        raise SourceError(f"来源拒绝访问或不存在（HTTP {response.status_code}）")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise RetryableSourceError("来源下载超过总时间预算")
                        chunks.extend(chunk)
                        if len(chunks) > byte_limit:
                            raise SourceError("来源响应超过允许大小，已停止下载")
                    decoded_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower() not in {"content-encoding", "content-length"}
                    }
                    return httpx.Response(
                        response.status_code,
                        headers=decoded_headers,
                        content=bytes(chunks),
                        request=httpx.Request("GET", target),
                    )
            except httpx.HTTPError as exc:
                raise RetryableSourceError("来源请求超时、TLS 验证失败或连接中断") from exc
        raise SourceError("来源重定向次数超过上限")
