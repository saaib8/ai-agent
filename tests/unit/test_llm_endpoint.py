"""Every model client goes to the one configured endpoint.

OpenAI by default; an OpenAI-compatible endpoint - Azure OpenAI's v1 API - when
`llm.base_url` is set. Chat, embeddings and images are built from the same
settings, so none of them can quietly keep calling the old provider.
"""

from __future__ import annotations

import pytest
from app.core.config import LLMSettings, VisualizationSettings
from app.integrations.embeddings import OpenAIQueryEmbedder
from app.integrations.image_generation import OpenAIImageGenerator
from app.integrations.llm import OpenAIStructuredClient
from pydantic import SecretStr

AZURE_ROOT = "https://zory-test.services.ai.azure.com/openai/v1/"
OPENAI_ROOT = "https://api.openai.com/v1/"


def _llm(base_url: str | None) -> LLMSettings:
    return LLMSettings(
        api_key=SecretStr("key-not-real"),
        model="deployment",
        embedding_model="text-embedding-3-large",
        base_url=base_url,
    )


@pytest.mark.parametrize(("base_url", "expected"), [(None, OPENAI_ROOT), (AZURE_ROOT, AZURE_ROOT)])
def test_the_chat_client_calls_the_configured_endpoint(base_url: str | None, expected: str) -> None:
    client = OpenAIStructuredClient(_llm(base_url))

    assert str(client._client.base_url) == expected


@pytest.mark.parametrize(("base_url", "expected"), [(None, OPENAI_ROOT), (AZURE_ROOT, AZURE_ROOT)])
def test_the_embedder_calls_the_configured_endpoint(base_url: str | None, expected: str) -> None:
    embedder = OpenAIQueryEmbedder(_llm(base_url))

    assert str(embedder._client.base_url) == expected


@pytest.mark.parametrize(("base_url", "expected"), [(None, OPENAI_ROOT), (AZURE_ROOT, AZURE_ROOT)])
def test_the_image_generator_calls_the_configured_endpoint(
    base_url: str | None, expected: str
) -> None:
    generator = OpenAIImageGenerator(
        VisualizationSettings(openai_model="image-deployment"),
        api_key="key-not-real",
        base_url=base_url,
    )

    assert str(generator._client.base_url) == expected
