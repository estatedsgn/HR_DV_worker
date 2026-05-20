# Tests

This directory contains the pytest suite for the project. It is intentionally committed to the repository so a local checkout should include it.

Current coverage focuses on the CRMchat integration scaffold:

- `test_crmchat_connector.py` verifies Telegram Raw API request shaping, method allowlisting, FLOOD_WAIT parsing, and dialog response normalization.
- `test_crmchat_webhooks.py` verifies CRMchat webhook signature validation and envelope parsing.

Run the suite from the repository root after installing development dependencies:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

If this directory is missing in a local checkout, pull the latest repository changes and reinstall the editable package:

```powershell
git pull
python -m pip install -e ".[dev]"
```


Additional inbound-queue coverage:

- `test_inbound_event_repository.py` verifies duplicate lookup behavior for `external_event_id`, `external_message_id`, and empty-key calls.
- `test_inbound_pipeline.py` verifies enqueue behavior for new events, pre-check duplicates, and DB unique-conflict fallback after `IntegrityError`.
- `test_inbound_queue_worker.py` verifies queue status transitions (`queued|retry -> processing -> processed|failed|retry|dead_letter`), retry scheduling/backoff, and attempt/error bookkeeping.
- `test_crmchat_webhook_endpoint.py` includes a queue-level duplicate scenario (`duplicate_scope=inbound_queue`).

Run inbound-focused tests:

```powershell
PYTHONPATH=. pytest -q tests/test_inbound_event_repository.py tests/test_inbound_pipeline.py tests/test_inbound_queue_worker.py tests/test_crmchat_webhook_endpoint.py
```


Run extended inbound checks:

```powershell
PYTHONPATH=. pytest -q tests/test_inbound_event_model.py tests/test_inbound_event_repository.py tests/test_inbound_pipeline.py tests/test_inbound_queue_worker.py tests/test_crmchat_webhook_endpoint.py
```
