# OpenClaw Collaboration Guide

This project is the caller. OpenClaw is expected to provide an OpenAI-compatible chat completion endpoint.

## Expected Endpoint

```text
POST {OPENCLAW_ENDPOINT}/v1/chat/completions
Authorization: Bearer {OPENCLAW_API_KEY}
Content-Type: application/json
```

The request body includes:

```json
{
  "model": "openclaw-model",
  "messages": [],
  "temperature": 0,
  "response_format": {"type": "json_object"}
}
```

The response should include JSON content at:

```text
choices[0].message.content
```

That content must itself be a JSON object matching the selected task `output_schema`.

## Collaboration Expectations

OpenClaw-side collaborators should help confirm:

- endpoint and `/v1/chat/completions` route are reachable from the VM
- API key is valid
- selected model supports JSON-only output
- service timeout is compatible with the configured client timeout
- concurrency and rate limits are appropriate for batch labeling
- 429, 5xx, and timeout behavior is visible in OpenClaw logs

Pipeline-side collaborators should provide:

- command used
- model and engine configuration summary
- latest `logs/label_*.jsonl`
- failure summary from `/api/failures/summary`
- representative failed task IDs and raw error text
- prompt and output schema under `config/tasks/`

## High-Frequency Problems

### Timeout

Likely causes:

- model is slow
- endpoint is overloaded
- prompt is too long
- concurrency is too high

Suggested first actions:

- lower `engine.concurrency`
- lower `engine.rate_limit_per_min`
- increase `model.timeout`
- check OpenClaw service load

### Rate Limited

Likely causes:

- 429 response
- OpenClaw queue protection
- caller burst too high

Suggested first actions:

- lower `engine.rate_limit_per_min`
- lower `engine.burst`
- increase `engine.interval_ms`

### Invalid JSON

Likely causes:

- model returned explanation text
- model ignored JSON-only instruction
- endpoint did not honor `response_format`

Suggested first actions:

- strengthen prompt
- add a compact valid JSON example in task prompt
- confirm OpenClaw model behavior with the same request body

### Schema Error

Likely causes:

- missing required field
- wrong type
- extra field
- ambiguous task schema

Suggested first actions:

- inspect `config/tasks/*.yaml`
- inspect representative response
- simplify or clarify field definitions

## Standard Issue Template

```text
Run command:
Config summary:
OpenClaw endpoint route:
Total tasks:
Failed tasks:
Failure type distribution:
Latest label log:
Representative task IDs:
Representative raw errors:
Prompt/schema file:
Expected OpenClaw-side help:
```
