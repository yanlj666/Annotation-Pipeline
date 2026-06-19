import json
import os
from typing import Any

import httpx


class LLMClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        model = config.get("model", {})
        self.endpoint = _resolve_env(model.get("endpoint"))
        self.api_key = _resolve_env(model.get("api_key"))
        self.model = model.get("name", "model")
        self.timeout = int(model.get("timeout", 120))
        self.last_usage: dict[str, Any] | None = None

    async def complete_json_async(
        self,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._use_mock():
            return self._mock_annotation(output_schema)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url(), headers=self._headers(), content=self._payload_body(messages, sampling))
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise exc
        self.last_usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        return _parse_content(body)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any],
        sampling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._use_mock():
            return self._mock_annotation(output_schema)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self._url(), headers=self._headers(), content=self._payload_body(messages, sampling))
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise exc
        self.last_usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        return _parse_content(body)

    def _url(self) -> str:
        return self.endpoint.rstrip("/") + "/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list[dict[str, str]], sampling: dict[str, Any] | None = None) -> dict[str, Any]:
        sampling = {"temperature": 0, "top_p": 1, "seed": None, **(sampling or {})}
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if thinking_enabled(sampling.get("thinking")):
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["temperature"] = sampling["temperature"]
            payload["top_p"] = sampling["top_p"]
            seed = sampling.get("seed")
            if isinstance(seed, int) and not isinstance(seed, bool):
                payload["seed"] = seed
        return payload

    def _payload_body(self, messages: list[dict[str, str]], sampling: dict[str, Any] | None = None) -> bytes:
        return json.dumps(self._payload(messages, sampling), ensure_ascii=False).encode("utf-8")

    def _use_mock(self) -> bool:
        return (
            os.environ.get("MOCK_LLM") == "1"
            or not self.endpoint
            or self.endpoint.startswith("${")
            or self.endpoint.startswith("mock://")
        )

    def mock_reason(self) -> str | None:
        if os.environ.get("MOCK_LLM") == "1":
            return "MOCK_LLM=1"
        if not self.endpoint:
            return "model endpoint is not configured"
        if self.endpoint.startswith("${"):
            return f"environment variable was not resolved: {self.endpoint}"
        if self.endpoint.startswith("mock://"):
            return "model endpoint uses mock://"
        return None

    @staticmethod
    def _mock_annotation(output_schema: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, spec in output_schema.items():
            typ = spec.get("type", "string")
            if typ == "boolean":
                result[key] = False
            elif typ == "integer":
                result[key] = 0
            elif typ == "number":
                result[key] = 0.0
            elif typ == "array":
                result[key] = []
            elif typ == "object":
                result[key] = {}
            else:
                result[key] = "mock"
        return result


def _parse_content(body: dict[str, Any]) -> dict[str, Any]:
    content = body["choices"][0]["message"]["content"]
    return json.loads(content)


def thinking_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return False


def _resolve_env(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value
