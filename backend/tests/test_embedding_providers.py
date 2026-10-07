import math

import httpx
import pytest

from app.config import get_settings
from app.retrieval.chunks import passages
from app.retrieval.providers import (
    DIMENSIONS,
    BailianEmbedding,
    EmbeddingError,
    cloud_url,
    descriptor,
    space_key,
    validate_vectors,
)


def vector(position=0):
    return [1.0 if i == position else 0.0 for i in range(DIMENSIONS)]


def config(**changes):
    return get_settings().model_copy(
        update={
            "bailian_base_url": "https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "bailian_api_key": "synthetic-test-key",
            **changes,
        }
    )


def test_chunk_bounds_overlap_and_material_scope():
    body = "x" * 4500
    chunks = passages("T" * 201, body)
    assert len(chunks) == 8
    assert chunks[0]["evidence"]["start_char"] == 0
    assert chunks[1]["evidence"]["start_char"] == 500
    assert chunks[-1]["evidence"]["end_char"] == 4100
    assert chunks[-1]["evidence"]["body_chars"] == 4500
    assert chunks[0]["evidence"]["title_truncated"] is True
    assert chunks[0]["evidence"]["body_truncated"] is True
    assert len(passages("Only metadata", "")) == 1
    assert len(passages("Title", "x" * 600)) == 1


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [1.0] * 10,
        [0.0] * DIMENSIONS,
        [float("nan")] * DIMENSIONS,
        [float("inf")] * DIMENSIONS,
        [True] * DIMENSIONS,
    ],
)
def test_invalid_vectors_are_rejected(bad):
    with pytest.raises(EmbeddingError):
        validate_vectors([bad], 1)


def test_vector_validation_count_and_normalization():
    with pytest.raises(EmbeddingError):
        validate_vectors([vector()], 2)
    normalized = validate_vectors([[1e200] * DIMENSIONS], 1)[0]
    assert math.isclose(sum(x * x for x in normalized), 1.0)


@pytest.mark.parametrize(
    "url",
    [
        "http://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://evil.example/compatible-mode/v1",
        "https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1",
        "https://u:p@dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1?token=secret",
        "https://127.0.0.1/compatible-mode/v1",
    ],
)
def test_cloud_endpoint_rejects_private_arbitrary_or_secret_url(url):
    with pytest.raises(EmbeddingError):
        cloud_url(config(bailian_base_url=url))


def test_space_identity_changes_for_model_endpoint_revision_or_pipeline():
    local = descriptor("local", config())
    cloud = descriptor("bailian", config())
    assert local["dimensions"] == cloud["dimensions"] == 1024
    assert space_key(local) != space_key(cloud)
    for field in ["revision", "endpoint", "pipeline", "query_instruction"]:
        assert space_key(cloud) != space_key({**cloud, field: "changed"})
    assert "synthetic-test-key" not in str(cloud)


def test_cloud_api_payload_order_usage_and_no_secret_echo():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-v4",
                "data": [
                    {"index": 1, "embedding": vector(1)},
                    {"index": 0, "embedding": vector(0)},
                ],
                "usage": {"total_tokens": 12},
            },
        )

    result = BailianEmbedding(config(), httpx.MockTransport(handler)).encode(["one", "two"])
    assert result.vectors == [vector(0), vector(1)]
    assert result.reported_tokens == 12
    assert len(requests) == 1
    assert b'"dimensions":1024' in requests[0].content
    assert b"long_term_profile" not in requests[0].content


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_cloud_failures_do_not_retry_redirect_or_echo_body(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="synthetic-test-key private query",
            headers={"Location": "https://evil.example/"},
        )

    with pytest.raises(EmbeddingError) as error:
        BailianEmbedding(config(), httpx.MockTransport(handler)).encode(["private query"])
    assert len(requests) == 1
    assert str(status) in str(error.value)
    assert "synthetic-test-key" not in str(error.value)
    assert "private query" not in str(error.value)


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": True, "embedding": vector()}],
        [{"index": 3, "embedding": vector()}],
        [{"index": 0, "embedding": [1, 2]}],
    ],
)
def test_cloud_malformed_response_rejected(rows):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": rows}))
    with pytest.raises(EmbeddingError):
        BailianEmbedding(config(), transport).encode(["text"])
