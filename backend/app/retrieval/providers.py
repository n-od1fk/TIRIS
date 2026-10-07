import hashlib
import importlib.util
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

import httpx

from app.config import Settings

LOCAL_MODEL = "Qwen/Qwen3-Embedding-0.6B"
LOCAL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QUERY_INSTRUCTION = "Given a technical learning query, retrieve relevant technical passages"
PIPELINE = "title200-char600-overlap100-max8-normalize-v1"
DIMENSIONS = 1024
model_lock = Lock()


class EmbeddingError(Exception):
    """Controlled public messages only: never include keys, URLs or response bodies."""


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    reported_tokens: int = 0


def cloud_url(config: Settings) -> str:
    parsed = urlsplit(config.bailian_base_url.strip().rstrip("/"))
    host = parsed.hostname or ""
    allowed = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or (
        host.endswith(".maas.aliyuncs.com") and host != "maas.aliyuncs.com"
    )
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or parsed.path != "/compatible-mode/v1"
    ):
        raise EmbeddingError("请配置百炼控制台提供的 HTTPS compatible-mode/v1 地址")
    return config.bailian_base_url.strip().rstrip("/")


def descriptor(provider: str, config: Settings) -> dict:
    if provider not in {"local", "bailian"}:
        raise EmbeddingError("不支持的向量后端")
    cloud = provider == "bailian"
    return {
        "provider": provider,
        "model": "text-embedding-v4" if cloud else LOCAL_MODEL,
        "revision": config.bailian_embedding_revision if cloud else LOCAL_REVISION,
        "endpoint": cloud_url(config) if cloud else "local",
        "dimensions": DIMENSIONS,
        "pipeline": PIPELINE,
        "query_instruction": "provider-default" if cloud else QUERY_INSTRUCTION,
        "normalization": "l2",
        "local_max_tokens": 4096 if not cloud else None,
    }


def space_key(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def model_dir(config: Settings) -> Path:
    return config.embedding_cache_dir / "qwen3-embedding-0.6b" / LOCAL_REVISION


def readiness(provider: str, config: Settings) -> str | None:
    if provider == "bailian":
        try:
            cloud_url(config)
        except (EmbeddingError, ValueError):
            return "尚未配置有效的百炼 Base URL"
        if not config.bailian_api_key.strip():
            return "尚未配置 BAILIAN_API_KEY"
    elif provider == "local":
        if importlib.util.find_spec("sentence_transformers") is None:
            return "未安装 local-embedding 可选依赖"
        folder = model_dir(config)
        if not (folder / "model.safetensors").is_file() or not (folder / ".ready").is_file():
            return "本地模型尚未准备，请运行 prepare-model 命令"
    else:
        return "不支持的向量后端"
    return None


def validate_vectors(raw, count: int) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != count:
        raise EmbeddingError("向量数量与输入不一致")
    result = []
    for vector in raw:
        if not isinstance(vector, list) or len(vector) != DIMENSIONS:
            raise EmbeddingError("向量维度不正确，未写入索引")
        if any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in vector
        ):
            raise EmbeddingError("向量包含非有限数值，未写入索引")
        norm = math.hypot(*(float(x) for x in vector))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise EmbeddingError("向量无效，未写入索引")
        result.append([float(x) / norm for x in vector])
    return result


@lru_cache(maxsize=1)
def local_model(folder: str):
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(4)
    model = SentenceTransformer(
        folder, device="cpu", local_files_only=True, trust_remote_code=False
    )
    model.max_seq_length = 4096
    return model


class LocalEmbedding:
    def __init__(self, config: Settings):
        self.config = config

    def encode(self, texts: list[str], *, query: bool = False) -> EmbeddingResult:
        try:
            with model_lock:
                model = local_model(str(model_dir(self.config)))
                prompt = f"Instruct: {QUERY_INSTRUCTION}\nQuery: " if query else ""
                inputs = [prompt + value for value in texts]
                # Never silently truncate material to the model's token window.
                if any(len(ids) > 4096 for ids in model.tokenizer(inputs)["input_ids"]):
                    raise EmbeddingError("输入超出本地模型窗口，请缩短或重新切分")
                raw = model.encode(
                    inputs, batch_size=2, normalize_embeddings=True, show_progress_bar=False
                ).tolist()
            return EmbeddingResult(validate_vectors(raw, len(texts)))
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError("本地向量计算失败，请检查模型文件与可选依赖") from None


class BailianEmbedding:
    def __init__(self, config: Settings, transport=None):
        self.config, self.transport = config, transport

    def encode(self, texts: list[str], *, query: bool = False) -> EmbeddingResult:
        if not self.config.bailian_api_key.strip():
            raise EmbeddingError("尚未配置 BAILIAN_API_KEY")
        if len(texts) > 10:
            raise EmbeddingError("百炼单次批量最多 10 条")
        try:
            with httpx.Client(
                timeout=self.config.embedding_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                # No HTTP retries: a lost response may already be billable.
                with client.stream(
                    "POST",
                    cloud_url(self.config) + "/embeddings",
                    headers={"Authorization": f"Bearer {self.config.bailian_api_key}"},
                    json={
                        "model": "text-embedding-v4",
                        "input": texts,
                        "dimensions": DIMENSIONS,
                        "encoding_format": "float",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise EmbeddingError(
                            f"百炼向量请求失败（HTTP {response.status_code}），未自动重试"
                        )
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > 2_000_000:
                            raise EmbeddingError("百炼响应超过大小限制")
            data = json.loads(body)
            rows = data["data"]
            if (
                not isinstance(rows, list)
                or len(rows) != len(texts)
                or any(type(row["index"]) is not int for row in rows)
                or sorted(row["index"] for row in rows) != list(range(len(texts)))
            ):
                raise EmbeddingError("百炼返回的向量序号无效")
            raw = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise EmbeddingError("百炼用量字段无效")
            if data.get("model") not in (None, "text-embedding-v4"):
                raise EmbeddingError("百炼返回的模型与配置不一致")
            return EmbeddingResult(validate_vectors(raw, len(texts)), tokens)
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError("百炼连接或响应解析失败，未自动重试；请检查网络与配置") from None


def get_embedder(provider: str, config: Settings):
    reason = readiness(provider, config)
    if reason:
        raise EmbeddingError(reason)
    return LocalEmbedding(config) if provider == "local" else BailianEmbedding(config)


def prepare_model(config: Settings):
    from huggingface_hub import snapshot_download

    folder = model_dir(config)
    snapshot_download(
        LOCAL_MODEL,
        revision=LOCAL_REVISION,
        local_dir=folder,
        cache_dir=config.embedding_cache_dir / "downloads",
        allow_patterns=["*.json", "*.txt", "*.safetensors", "1_Pooling/*", "README.md"],
    )
    # Download completion marker, not model inference or quality validation.
    (folder / ".ready").touch()
