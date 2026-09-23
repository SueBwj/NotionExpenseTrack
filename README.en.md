# NotionExpenseTrack

[English] | [简体中文](README.md)

Turn WeChat Pay and Alipay statements into reviewable transactions, then import them into your own Notion finance workspace when ready. You can start with the [Finance Tracker template by Vince Lin](https://www.notion.com/templates/finance-credit-budget-tracker) and configure the data sources in your own copy. The project includes a command-line workflow and a local web console. The console listens only on your local machine, and bills and run history stay on disk.

## Features

- Read CSV and XLSX exports from WeChat Pay and Alipay, normalize transaction fields, and deduplicate by source key.
- Ask Jev through OpenRouter to suggest transaction types, expense categories, and payment accounts; keep uncertain records for review.
- Preview Notion import actions before writing. Repeated runs check existing source keys to avoid duplicate entries.
- Use the local web console to upload bills, review transactions, follow classification progress, and browse run history.

## Quick start

Python 3.10 or newer is required. Install `openpyxl` to read XLSX files; CSV processing uses the Python standard library.

```sh
git clone https://github.com/SueBwj/NotionExpenseTrack.git
cd NotionExpenseTrack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` as described below. You can skip API credentials if you only want to normalize bills without using AI classification or Notion.

## Usage

### Command line

Normalize bills and create a review list:

```sh
python bills.py --wechat path/to/wechat.csv --month 2026-09 --out output/2026-09
# Add --alipay path/to/alipay.xlsx, or repeat either platform option for multiple files.
```

Run Jev classification to create `classified.csv`:

```sh
python classify_jev.py output/2026-09/normalized.csv output/2026-09/classified.csv
```

Preview the Notion import first. Check transaction counts, amount totals, payment accounts, and any records that still need review before using `--apply`:

```sh
python notion_import.py output/2026-09/classified.csv
python notion_import.py output/2026-09/classified.csv --apply
```

### Web console

```sh
python flow_console.py
```

Open <http://127.0.0.1:8765>. Upload bills to see each processing and review step. Writing to Notion still requires a separate confirmation. Press `Ctrl+C` to stop the server.

## Configure Notion and OpenRouter

Copy `.env.example` to `.env` and set:

- `OPENROUTER_API_KEY`: used for Jev transaction classification.
- `NOTION_API_KEY`: your Notion internal integration secret.
- The six `NOTION_*_DATA_SOURCE_ID` values: the IDs for your expenses, income/refunds, categories, accounts, subscriptions, and transfers data sources.
- `NOTION_WECHAT_ACCOUNT_ID`, `NOTION_ALIPAY_ACCOUNT_ID`, and `NOTION_BANK_ACCOUNT_ID`: the page IDs for the corresponding payment accounts. Set the bank account ID to match your own workspace.

Share the required data sources with your Notion integration and grant it read and write access. Database IDs differ between copies of the Finance Tracker template, so use the IDs from your own copy. The importer expects properties for names, dates, amounts, account relations, category relations, and source keys. If you customized the template fields, check that the property names in `notion_import.py` match your workspace.

## Privacy and data safety

`.env`, `data/`, `output/`, and `assets/` are ignored by Git. Do not commit API keys, original statements, import results, or screenshots. Jev receives merchant names, item descriptions, transaction types, directions, statuses, platforms, and payment methods. The program removes long identifiers, email addresses, and phone numbers; it does not send amounts, transaction IDs, or notes. OpenRouter usage may incur fees.

## Input and output

Use original CSV/XLSX exports from WeChat Pay and Alipay; do not edit the source exports manually. Processing creates `normalized.csv`, `review.csv`, and `summary.json`. Jev creates `classified.csv`. Amounts are stored as positive values in Chinese yuan, with transaction direction recorded separately.

## License

This project is licensed under the MIT License. The Notion template is provided by its creator under the Notion Marketplace terms; this repository does not include the template itself.
