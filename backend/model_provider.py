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
    NVIDIA_API_KEY, NVIDIA_BASE_URL, NVIDIA_MODEL,
)


SYSTEM_PROMPT = """You are an Indian-market paper-trading research assistant.
Explain only the supplied deterministic calculations. Never invent a price,
promise profit, describe simulated data as live, or override SL/TP/risk controls.
State uncertainty and keep the answer concise and professional."""

BOT_SYSTEM_PROMPT = SYSTEM_PROMPT + """
You are responding inside a persistent research chat. Use the supplied guarded
result as the source of truth. Do not add prices, option premiums, Greeks or
claims that are absent. Never instruct the system to place a live order.
Answer only from supplied context. If the answer is not explicitly supported by
the guarded result or retrieved context, say you do not have verified data for
that question and list the exact data needed."""


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

    def chat(self, message: str, guarded_result: str, context: Dict) -> str:
        return guarded_result

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

    def chat(self, message: str, guarded_result: str, context: Dict) -> str:
        payload = {"message": message, "guarded_result": guarded_result, "context": context}
        data = _post(f"{self.base_url}/chat/completions", {"model": self.model, "temperature": .15, "max_tokens": 500, "messages": [{"role":"system","content":BOT_SYSTEM_PROMPT},{"role":"user","content":json.dumps(payload)}]}, {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
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

    def chat(self, message: str, guarded_result: str, context: Dict) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model)}:generateContent?key={quote(GEMINI_API_KEY)}"
        prompt = BOT_SYSTEM_PROMPT + "\n\n" + json.dumps({"message":message,"guarded_result":guarded_result,"context":context})
        data = _post(url, {"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":.15,"maxOutputTokens":500}}, {})
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    free_tier = False

    def __init__(self):
        self.model, self.available = ANTHROPIC_MODEL, bool(ANTHROPIC_API_KEY)

    def enhance(self, analysis: Dict) -> str:
        data = _post("https://api.anthropic.com/v1/messages", {"model": self.model, "max_tokens": 450, "temperature": .2, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": "Explain this computed paper-trading analysis:\n" + json.dumps(_context(analysis))}]}, {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"})
        return data["content"][0]["text"].strip()

    def chat(self, message: str, guarded_result: str, context: Dict) -> str:
        payload = json.dumps({"message":message,"guarded_result":guarded_result,"context":context})
        data = _post("https://api.anthropic.com/v1/messages", {"model":self.model,"max_tokens":500,"temperature":.15,"system":BOT_SYSTEM_PROMPT,"messages":[{"role":"user","content":payload}]}, {"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"})
        return data["content"][0]["text"].strip()


def get_provider() -> BaseProvider:
    provider = MODEL_PROVIDER
    if provider == "auto":
        if NVIDIA_API_KEY:
            selected=OpenAICompatibleProvider(NVIDIA_BASE_URL,NVIDIA_MODEL,NVIDIA_API_KEY,name="nvidia-nim")
            selected.available=True
            return selected
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
    if provider in {"nvidia","nvidia-nim"}:
        selected=OpenAICompatibleProvider(NVIDIA_BASE_URL,NVIDIA_MODEL,NVIDIA_API_KEY,name="nvidia-nim")
        selected.available=bool(NVIDIA_API_KEY and NVIDIA_BASE_URL and NVIDIA_MODEL)
        return selected
    if provider == "ollama":
        return OpenAICompatibleProvider(OLLAMA_BASE_URL, OLLAMA_MODEL, name="ollama", free_tier=True)
    if provider == "compatible":
        return OpenAICompatibleProvider(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)
    return BaseProvider()


def configured_providers() -> list[BaseProvider]:
    """Return all configured narrative/chat providers in a safe priority order.

    The local provider is intentionally excluded here; callers add it as a
    deterministic fallback after external model attempts are complete.
    """
    providers: list[BaseProvider] = []
    if NVIDIA_API_KEY:
        selected=OpenAICompatibleProvider(NVIDIA_BASE_URL,NVIDIA_MODEL,NVIDIA_API_KEY,name="nvidia-nim")
        selected.available=bool(NVIDIA_BASE_URL and NVIDIA_MODEL)
        providers.append(selected)
    if GEMINI_API_KEY:
        providers.append(GeminiProvider())
    if OLLAMA_ENABLED:
        providers.append(OpenAICompatibleProvider(OLLAMA_BASE_URL, OLLAMA_MODEL, name="ollama", free_tier=True))
    if ANTHROPIC_API_KEY:
        providers.append(AnthropicProvider())
    if OPENAI_API_KEY:
        selected=OpenAICompatibleProvider("https://api.openai.com/v1", OPENAI_MODEL, OPENAI_API_KEY, name="openai")
        selected.available=True
        providers.append(selected)
    if LLM_BASE_URL and LLM_MODEL:
        providers.append(OpenAICompatibleProvider(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, name="compatible"))
    seen=set()
    unique=[]
    for provider in providers:
        key=(provider.name,provider.model)
        if provider.available and key not in seen:
            seen.add(key)
            unique.append(provider)
    return unique


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
        {"id": "nvidia-nim", "model": NVIDIA_MODEL, "free_tier": False, "quota_dependent": True, "configured": bool(NVIDIA_API_KEY)},
    ]}
