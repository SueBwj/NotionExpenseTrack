# Repository Guide

## Project layout

- `bills.py`: normalize WeChat Pay and Alipay exports.
- `classify_jev.py`: request transaction suggestions from OpenRouter.
- `notion_import.py`: preview and optionally apply imports to Notion.
- `flow_console.py` and `flow_console/`: local web interface and server.
- `test/`: standard-library `unittest` suite.
- `data/`, `output/`, `.env`, and `assets/`: private local files; do not commit them.

## Development

Use Python 3.10+. Install `requirements.txt` when XLSX support is needed. Keep the CSV pipeline compatible with the standard library. Use four-space indentation and `snake_case` names. Preserve existing CSV columns and transaction kinds when changing the workflow.

## Data safety

Never commit API keys, raw financial exports, generated transaction files, or personal workspace identifiers. Keep service and Notion IDs in the local `.env` file. Notion writes must remain opt-in and follow a completed preview.
