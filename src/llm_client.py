import json
import os
from typing import Any

import httpx


class LLMClient:
    def __init__(self, config: dict[str, Any]):
        model = config.get("model", {})
        self.endpoint = _resolve_env(model.get("endpoint"))
        self.api_key = _resolve_env(model.get("api_key"))
        self.model = model.get("name", "model")
        self.timeout = int(model.get("timeout", 120))

    async def complete_json_async(self, messages: list[dict[str, str]], output_schema: dict[str, Any]) -> dict[str, Any]:
        if self._use_mock():
            return self._mock_annotation(output_schema)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url(), headers=self._headers(), json=self._payload(messages))
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"llm request failed: {exc}") from exc
        return _parse_content(body)

    def complete_json(self, messages: list[dict[str, str]], output_schema: dict[str, Any]) -> dict[str, Any]:
        if self._use_mock():
            return self._mock_annotation(output_schema)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self._url(), headers=self._headers(), json=self._payload(messages))
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"llm request failed: {exc}") from exc
        return _parse_content(body)

    def _url(self) -> str:
        return self.endpoint.rstrip("/") + "/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

    def _use_mock(self) -> bool:
        return (
            os.environ.get("MOCK_LLM") == "1"
            or not self.endpoint
            or self.endpoint.startswith("${")
            or self.endpoint.startswith("mock://")
        )

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


def _resolve_env(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value
