"""Safe multi-provider narrative layer for computed market analysis.

The deterministic structure engine owns every price, level and risk calculation.
Models may explain that payload, but can never execute orders or alter SL/TP.
"""
import json
from typing import Dict, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import (
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GEMINI_API_KEY, GEMINI_MODEL,
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MODEL_PROVIDER,
    OLLAMA_BASE_URL, OLLAMA_ENABLED, OLLAMA_MODEL,
    OPENAI_API_KEY, OPENAI_MODEL,
)


SYSTEM_PROMPT = """You are an Indian-market paper-trading research assistant.
Explain only the supplied deterministic calculations. Never invent a price,
promise profit, describe simulated data as live, or override SL/TP/risk controls.
State uncertainty and keep the answer concise and professional."""


def _context(analysis: Dict) -> Dict:
    keys = ("symbol", "price", "bias", "confidence", "timeframes", "market_structure", "liquidity", "support_resistance", "order_block", "fvg", "poi", "trade_plan")
    return {key: analysis[key] for key in keys}


def _post(url: str, payload: Dict, headers: Dict) -> Dict:
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    with urlopen(request, timeout=18) as response:
        return json.loads(response.read())


class BaseProvider:
    name = "local"
    model = "market-structure-ensemble"
    free_tier = True
    available = True

    def enhance(self, analysis: Dict) -> str:
        return analysis["narrative"]

    def info(self) -> Dict:
        return {"provider": self.name, "model": self.model, "available": self.available, "free_tier": self.free_tier}


class OpenAICompatibleProvider(BaseProvider):
    name = "openai-compatible"
    free_tier = False

    def __init__(self, base_url: str, model: str, api_key: str = "", name: str = "openai-compatible", free_tier: bool = False):
        self.base_url, self.model, self.api_key, self.name, self.free_tier = base_url.rstrip("/"), model, api_key, name, free_tier
        self.available = bool(self.base_url and self.model)

    def enhance(self, analysis: Dict) -> str:
        data = _post(f"{self.base_url}/chat/completions", {"model": self.model, "temperature": .2, "max_tokens": 450, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Explain this computed paper-trading analysis:\n" + json.dumps(_context(analysis))}]}, {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
        return data["choices"][0]["message"]["content"].strip()


class GeminiProvider(BaseProvider):
    name = "gemini"
    free_tier = True

    def __init__(self):
        self.model, self.available = GEMINI_MODEL, bool(GEMINI_API_KEY)

    def enhance(self, analysis: Dict) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model)}:generateContent?key={quote(GEMINI_API_KEY)}"
        prompt = SYSTEM_PROMPT + "\n\nExplain this computed paper-trading analysis:\n" + json.dumps(_context(analysis))
        data = _post(url, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": .2, "maxOutputTokens": 450}}, {})
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    free_tier = False

    def __init__(self):
        self.model, self.available = ANTHROPIC_MODEL, bool(ANTHROPIC_API_KEY)

    def enhance(self, analysis: Dict) -> str:
        data = _post("https://api.anthropic.com/v1/messages", {"model": self.model, "max_tokens": 450, "temperature": .2, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": "Explain this computed paper-trading analysis:\n" + json.dumps(_context(analysis))}]}, {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
        return data["content"][0]["text"].strip()


def get_provider() -> BaseProvider:
    provider = MODEL_PROVIDER
    if provider == "auto":
        if GEMINI_API_KEY:
            return GeminiProvider()
        if OLLAMA_ENABLED:
            return OpenAICompatibleProvider(OLLAMA_BASE_URL, OLLAMA_MODEL, name="ollama", free_tier=True)
        if LLM_BASE_URL and LLM_MODEL:
            return OpenAICompatibleProvider(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)
        return BaseProvider()
    if provider == "gemini":
        return GeminiProvider()
    if provider == "anthropic":
        return AnthropicProvider()
    if provider == "openai":
        selected = OpenAICompatibleProvider("https://api.openai.com/v1", OPENAI_MODEL, OPENAI_API_KEY, name="openai")
        selected.available = bool(OPENAI_API_KEY)
        return selected
    if provider == "ollama":
        return OpenAICompatibleProvider(OLLAMA_BASE_URL, OLLAMA_MODEL, name="ollama", free_tier=True)
    if provider == "compatible":
        return OpenAICompatibleProvider(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)
    return BaseProvider()


class NarrativeModel:
    """Compatibility wrapper used by the analysis service."""
    def __init__(self):
        self.provider = get_provider()

    @property
    def available(self) -> bool:
        return self.provider.available

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def model_name(self) -> str:
        return self.provider.model

    def enhance(self, analysis: Dict) -> str:
        if not self.available:
            return analysis["narrative"]
        try:
            return self.provider.enhance(analysis)
        except Exception:
            return analysis["narrative"] + " The configured narrative provider was unavailable, so this explanation was generated locally."

    def info(self) -> Dict:
        return self.provider.info()


def provider_catalog() -> Dict:
    active = get_provider().info()
    return {"active": active, "providers": [
        {"id": "local", "model": "market-structure-ensemble", "free_tier": True, "configured": True},
        {"id": "gemini", "model": GEMINI_MODEL, "free_tier": True, "configured": bool(GEMINI_API_KEY)},
        {"id": "ollama", "model": OLLAMA_MODEL, "free_tier": True, "configured": OLLAMA_ENABLED},
        {"id": "anthropic", "model": ANTHROPIC_MODEL, "free_tier": False, "configured": bool(ANTHROPIC_API_KEY)},
        {"id": "openai", "model": OPENAI_MODEL, "free_tier": False, "configured": bool(OPENAI_API_KEY)},
    ]}
