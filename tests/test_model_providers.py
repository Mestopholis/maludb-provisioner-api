"""The model providers the memory worker calls (ADR-079, memory slice 5b).

No network: every call is answered by an `httpx.MockTransport`, which records what
the worker actually sent. What these hold:

- the request goes to the provider's fixed host and path, with the key in a header
  and the customer's model name only in the body;
- a provider's error never carries the key back out, even when the provider echoes it;
- retryable answers are retried and honour `Retry-After`; the rest fail at once, and
  the failures that would repeat for every item are marked fatal;
- a model's answer is checked edge by edge, and what was unusable is reported.
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.control_plane import model_providers
from services.control_plane.model_providers import Models, ProviderError

KEY = "sk-test-" + "Q1w2E3r4" * 5 + "zzzz"  # noqa: S105 - test fixture, not a real key


def _models(handler, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return Models(transport=httpx.MockTransport(handler), sleep=sleeps.append)


def _edges(*pairs):
    return {"candidate_edges": [{"subject_text": s, "verb_text": v, "source_span": f"{s} {v} it",
                                 "confidence": 0.9} for s, v in pairs]}


def test_anthropic_extraction_goes_to_the_fixed_host_with_a_structured_output_schema():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(_edges(("carol", "owns")))}]})

    extraction = _models(handler).extract("anthropic", "claude-opus-5", KEY, "carol owns the parser")
    assert [e["subject_text"] for e in extraction.edges] == ["carol"]
    request = seen[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY and request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "carol owns the parser" in body["messages"][0]["content"]
    assert KEY not in request.content.decode()


def test_openai_extraction_and_both_embedding_providers_use_their_fixed_endpoints():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(_edges(("dave", "prefers")))}}]})
        # Deliberately out of order: the answer is placed by `index`, not position.
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(len(body["input"])))]})

    models = _models(handler)
    assert models.extract("openai", "gpt-4o", KEY, "dave prefers tea").edges[0]["verb_text"] == "prefers"
    assert models.embed("openai", "text-embedding-3-small", KEY, ["a", "b"]) == [[0.0, 1.0], [1.0, 1.0]]
    assert models.embed("voyage", "voyage-3.5", KEY, ["a"]) == [[0.0, 1.0]]

    assert [str(r.url) for r in seen] == [
        "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v1/embeddings",
        "https://api.voyageai.com/v1/embeddings",
    ]
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in seen)
    assert json.loads(seen[0].content)["response_format"] == {"type": "json_object"}
    assert json.loads(seen[2].content)["input_type"] == "document"


@pytest.mark.parametrize("model", ["http://169.254.169.254/latest", "../v1/files", "gpt 4", "", "x" * 101])
def test_a_model_name_cannot_carry_a_url_or_a_path(model):
    def handler(_request):
        raise AssertionError("a refused model name must not reach a provider")

    with pytest.raises(ValueError):
        _models(handler).extract("anthropic", model, KEY, "text")


def test_the_environment_cannot_redirect_or_proxy_the_calls():
    assert Models()._client.trust_env is False


def test_a_refused_key_is_fatal_and_the_key_never_comes_back_in_the_message():
    def handler(_request):
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})

    with pytest.raises(ProviderError) as refused:
        _models(handler).extract("openai", "gpt-4o", KEY, "text")
    assert refused.value.kind == "auth" and refused.value.fatal
    assert KEY not in str(refused.value) and "[key]" in str(refused.value)


def test_a_rate_limit_is_retried_after_the_providers_delay_and_then_answered():
    answers = [httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}),
               httpx.Response(529, json={"error": {"message": "overloaded"}}),
               httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}]})]
    sleeps: list[float] = []
    assert _models(lambda _r: answers.pop(0), sleeps).embed("voyage", "voyage-3.5", KEY, ["a"]) == [[0.5]]
    assert sleeps[0] == 7.0 and len(sleeps) == 2


def test_a_limit_that_outlasts_the_retries_stops_the_ingest():
    sleeps: list[float] = []

    def handler(_request):
        return httpx.Response(429, headers={"retry-after": "3600"}, json={"error": {"message": "quota"}})

    with pytest.raises(ProviderError) as limited:
        _models(handler, sleeps).embed("openai", "text-embedding-3-small", KEY, ["a"])
    assert limited.value.kind == "rate_limited" and limited.value.fatal
    assert all(s <= model_providers.MAX_RETRY_AFTER for s in sleeps), "a provider cannot park the worker for an hour"


def test_a_refusal_belongs_to_one_item_and_does_not_stop_the_ingest():
    def handler(_request):
        return httpx.Response(200, json={"stop_reason": "refusal", "content": []})

    with pytest.raises(ProviderError) as refused:
        _models(handler).extract("anthropic", "claude-opus-5", KEY, "text")
    assert refused.value.kind == "refused" and not refused.value.fatal


def test_an_unusable_answer_is_reported_edge_by_edge():
    candidates = [{"subject_text": "ok", "verb_text": "owns", "source_span": "ok owns"},
                  {"subject_text": "", "verb_text": "owns", "source_span": "x"},
                  "not an object"]
    candidates += [{"subject_text": f"s{i}", "verb_text": "v", "source_span": "span"} for i in range(60)]
    extraction = model_providers.parse_edges(json.dumps({"candidate_edges": candidates}))
    assert len(extraction.edges) == model_providers.MAX_EDGES - 2
    assert [r["edge"] for r in extraction.rejected] == [1, 2]
    assert extraction.truncated == len(candidates) - model_providers.MAX_EDGES
    with pytest.raises(ProviderError):
        model_providers.parse_edges("I could not find anything")


def test_embeddings_of_the_wrong_count_or_mixed_dimensions_are_refused():
    with pytest.raises(ProviderError):
        model_providers.parse_embeddings({"data": [{"index": 0, "embedding": [1.0]}]}, 2, "openai")
    with pytest.raises(ProviderError):
        model_providers.parse_embeddings({"data": [{"index": 0, "embedding": [1.0]},
                                                   {"index": 1, "embedding": [1.0, 2.0]}]}, 2, "openai")
    with pytest.raises(ProviderError):
        model_providers.parse_embeddings({"data": [{"index": 0, "embedding": [float("nan")]}]}, 1, "openai")
