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
