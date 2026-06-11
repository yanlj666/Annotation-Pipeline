import json
import os
import urllib.error
import urllib.request
from typing import Any


class LLMClient:
    def __init__(self, config: dict[str, Any]):
        model = config.get("model", {})
        self.endpoint = _resolve_env(model.get("endpoint"))
        self.api_key = _resolve_env(model.get("api_key"))
        self.model = model.get("name", "model")
        self.timeout = int(model.get("timeout", 60))

    def complete_json(self, messages: list[dict[str, str]], output_schema: dict[str, Any]) -> dict[str, Any]:
        if self._use_mock():
            return self._mock_annotation(output_schema)
        url = self.endpoint.rstrip("/") + "/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"llm request failed: {exc}") from exc
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)

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


def _resolve_env(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value
