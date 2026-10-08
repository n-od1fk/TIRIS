from app.connectors.contracts import SourceError
from app.platforms.github import GitHubPlatform
from app.platforms.providers import ArxivPlatform, BlackHatPlatform, RSSPlatform, WebPlatform

PLATFORMS = {
    p.kind: p
    for p in (
        GitHubPlatform(),
        RSSPlatform(),
        BlackHatPlatform(),
        ArxivPlatform(),
        WebPlatform(),
    )
}


def register_platform(provider):
    from app.connectors.registry import CONNECTORS, SourceConnector, platform_only_reader

    if provider.kind in PLATFORMS:
        raise ValueError("Platform already registered")
    if provider.kind in CONNECTORS:
        raise ValueError("Source kind already registered")
    if not provider.kind or len(provider.kind) > 20:
        raise ValueError("Platform kind must contain 1-20 characters")
    CONNECTORS[provider.kind] = SourceConnector(
        provider.kind,
        provider.label,
        getattr(provider, "hint", "平台连接，使用平台工具读取内容"),
        getattr(provider, "default_url", "https://example.com/"),
        platform_only_reader,
        getattr(provider, "validate_url", None),
        default_url=getattr(provider, "default_url", None),
    )
    PLATFORMS[provider.kind] = provider


def get_platform(kind):
    try:
        return PLATFORMS[kind]
    except KeyError:
        raise SourceError("此来源尚无平台级工具") from None


def bound_platform(source):
    from app.services.credentials import source_values

    provider = get_platform(source.kind)
    binder = getattr(provider, "with_credentials", None)
    return binder(source_values(source)) if binder else provider


def connection_info(url):
    for provider in PLATFORMS.values():
        detector = getattr(provider, "connection_info", None)
        if detector:
            result = detector(url)
            if result:
                return result
    return None


def catalog():
    from app.config import get_settings

    return [
        {
            "kind": p.kind,
            "label": p.label,
            "operations": p.operations,
            "detail": True,
            "original_files": True,
            "credential_env": "GITHUB_TOKEN" if p.kind == "github" else None,
            "credential_configured": bool(get_settings().github_token)
            if p.kind == "github"
            else None,
            "credential_file": ".env" if p.kind == "github" else None,
        }
        for p in PLATFORMS.values()
    ]
