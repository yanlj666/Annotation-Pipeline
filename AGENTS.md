# Agent Notes

This repository implements a small, deterministic annotation pipeline. Keep the core contract stable:

- `task_id`, `turns`, and `status` are fixed fields.
- All business-specific imported fields must stay in `payload`; do not hard-code business column names in Python code.
- Label output must validate against the selected task `output_schema` before writing `status=labeled`.
- The pipeline must be idempotent by `task_id`; repeated imports should not create duplicate work.
- Model endpoint, API key, and model name are configuration values. Never hard-code private endpoints or secrets.
- Exports must pass through masking. Raw full conversations should not be exported.

The code intentionally uses mostly Python standard library plus PyYAML. Prefer small deterministic helpers over new framework dependencies.
