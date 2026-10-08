"""Authenticated encryption, bound to identity and row. Never returned by an API."""

import json
import os
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.connectors.contracts import SourceError

_key_lock = Lock()


def cipher(*, create=False):
    path = get_settings().source_credential_key_file
    try:
        with _key_lock:
            if create and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(Fernet.generate_key())
                        output.flush()
                        os.fsync(output.fileno())
            key = path.read_bytes()
        return Fernet(key)
    except (OSError, ValueError):
        raise SourceError(
            "凭证密钥不可用；请检查密钥文件或恢复 source_secrets 卷，不要删除数据库"
        ) from None


def encrypt_values(values, user_id, row_id):
    if not values:
        return None
    payload = {"user": str(user_id), "row": str(row_id), "values": values}
    return cipher(create=True).encrypt(json.dumps(payload).encode()).decode("ascii")


def decrypt_values(ciphertext, user_id, row_id):
    if not ciphertext:
        return {}
    try:
        payload = json.loads(cipher().decrypt(ciphertext.encode("ascii")))
        if payload["user"] != str(user_id) or payload["row"] != str(row_id):
            raise ValueError
        values = payload["values"]
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise ValueError
        return values
    except (InvalidToken, ValueError, KeyError, TypeError, UnicodeError):
        raise SourceError(
            "无法解密该来源的凭证，请恢复正确密钥或在来源页面重新填写；未回退到其他身份凭证"
        ) from None


def source_values(source):
    if source.credential_mode == "stored":
        if not source.credentials_ciphertext:
            raise SourceError("来源凭证记录损坏，请重新填写；未回退到匿名或环境凭证")
        return decrypt_values(source.credentials_ciphertext, source.user_id, source.id)
    if source.credential_mode == "legacy_env" and source.kind == "github":
        token = get_settings().github_token
        return {"token": token} if token else {}
    return {}
