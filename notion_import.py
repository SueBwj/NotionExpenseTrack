"""Preview and import classified bills into a configurable Notion workspace."""

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import classify_jev


SOURCE_ENV = {
    "expense": "NOTION_EXPENSE_DATA_SOURCE_ID",
    "income": "NOTION_INCOME_DATA_SOURCE_ID",
    "categories": "NOTION_CATEGORIES_DATA_SOURCE_ID",
    "accounts": "NOTION_ACCOUNTS_DATA_SOURCE_ID",
    "subscriptions": "NOTION_SUBSCRIPTIONS_DATA_SOURCE_ID",
    "transfer": "NOTION_TRANSFERS_DATA_SOURCE_ID",
}
ACCOUNT_ENV = {
    "wechat": "NOTION_WECHAT_ACCOUNT_ID",
    "alipay": "NOTION_ALIPAY_ACCOUNT_ID",
    "bank_account": "NOTION_BANK_ACCOUNT_ID",
}
# Non-sensitive placeholders keep the helpers easy to exercise without a workspace.
SOURCES = {key: env_name for key, env_name in SOURCE_ENV.items()}
ACCOUNTS = {key: env_name for key, env_name in ACCOUNT_ENV.items()}
TZ = timezone(timedelta(hours=8))


def read_env(path):
    values = {}
    if path and path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
            elif line.startswith("sk-"):
                values["OPENROUTER_API_KEY"] = line
            elif line.startswith("ntn_"):
                values["NOTION_API_KEY"] = line
    values.update(os.environ)
    return values


def settings_from_env(path):
    values = read_env(path)
    settings = {
        "sources": {key: values.get(env_name, "") for key, env_name in SOURCE_ENV.items()},
        "accounts": {key: values.get(env_name, "") for key, env_name in ACCOUNT_ENV.items()},
    }
    missing = [name for name in (*SOURCE_ENV.values(), *ACCOUNT_ENV.values()) if not values.get(name)]
    if missing:
        raise ValueError("Notion 配置缺失，请在 .env 中设置: " + ", ".join(missing))
    return settings


def token_from_env(path):
    values = read_env(path)
    token = values.get("NOTION_API_KEY", "")
    if not token:
        raise ValueError(".env 中未找到 NOTION_API_KEY")
    return token


class Notion:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = Request("https://api.notion.com/v1/" + path, data=body, method=method,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Notion-Version": "2026-03-11", "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(int(error.headers.get("Retry-After", "1")), 10))
                    continue
                detail = error.read().decode(errors="replace")[:300]
                raise RuntimeError(f"Notion HTTP {error.code}: {detail}") from None

    def rows(self, source_id, on_page=None):
        cursor = None
        count = 0
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self.call("POST", f"data_sources/{source_id}/query", payload)
            count += len(result["results"])
            if on_page:
                on_page(count, result["has_more"])
            yield from result["results"]
            if not result["has_more"]:
                return
            cursor = result["next_cursor"]


def plain(property_value):
    return "".join(part.get("plain_text", "") for part in property_value.get(property_value["type"], []))


def text(value):
    return [{"type": "text", "text": {"content": value[:2000]}}]


def bank_name(row):
    method = row["method"]
    match = re.search(r"([^()（）&＋+]*银行[^()（）&＋+]*卡)[(（](\d{4})[)）]", method)
    if match:
        return f"{match[1]}（{match[2]}）"
    if "银行卡" in method or ("银行" in method and "卡" in method):
        raise ValueError(f"无法识别银行卡付款方式: {method}")
    return None


def account(row, bank_accounts, account_ids=None):
    account_ids = account_ids or ACCOUNTS
    name = bank_name(row)
    if name:
        if name in bank_accounts:
            return bank_accounts[name]
        raise ValueError(f"Notion 中缺少银行卡账户: {name}")
    suggestion = "huabei" if classify_jev.is_huabei(row.get("method", "")) else row.get("account_suggestion", "")
    if suggestion == "source_wallet":
        if row["source"] in account_ids:
            return account_ids[row["source"]]
        raise ValueError(f"未知支付来源: {row['source']}")
    if suggestion == "bank_account":
        return account_ids["bank_account"]
    if suggestion == "huabei":
        if "花呗" in bank_accounts:
            return bank_accounts["花呗"]
        raise ValueError("Notion 中缺少花呗账户")
    if suggestion == "unknown":
        raise ValueError(f"Jev 无法识别付款账户: {row['method']}")
    if row["source"] in account_ids:
        return account_ids[row["source"]]
    raise ValueError(f"未知支付来源或账户: {row['source']} / {row.get('method', '')}")


def is_huabei_repayment(row):
    description = " ".join(row.get(field, "") for field in ("type", "party", "item", "note", "status"))
    return "花呗" in description and "还款" in description


def finance_properties(row, category_id=None, bank_accounts=None, account_ids=None):
    date = datetime.fromisoformat(row["time"]).replace(tzinfo=TZ).isoformat()
    misc = "; ".join(f"{field}={row[field]}" for field in
                     ("source", "key", "transaction_id", "type", "item", "status", "method", "note"))
    properties = {
        "Name": {"title": text(row["party"] or row["item"] or row["type"])},
        "Date": {"date": {"start": date}},
        "Amount": {"number": float(Decimal(row["amount_yuan"]))},
        "Accounts": {"relation": [{"id": account(row, bank_accounts or {}, account_ids)}]},
        "Misc": {"rich_text": text(misc)},
    }
    if category_id:
        properties["Categories"] = {"relation": [{"id": category_id}]}
    return properties


def subscription_properties(row, category_id, bank_accounts, account_ids=None):
    return {
        "Name": {"title": text(row["party"] or row["item"] or row["type"])},
        "Amount": {"number": float(Decimal(row["amount_yuan"]))},
        "Account": {"relation": [{"id": account(row, bank_accounts, account_ids)}]},
        "Category": {"relation": [{"id": category_id}]},
        # Jev does not determine billing cadence; default the tracker entry to Monthly.
        "Type": {"select": {"name": "Monthly"}},
    }


def subscription_account_key(row, bank_accounts, account_ids=None):
    if classify_jev.is_huabei(row.get("method", "")) and "花呗" not in bank_accounts:
        return "new-account:huabei"
    try:
        return account(row, bank_accounts, account_ids)
    except ValueError:
        name = bank_name(row)
        if name:
            return "new-bank:" + name
        raise


def transfer_properties(row, from_account_id, huabei_account_id):
    date = datetime.fromisoformat(row["time"]).replace(tzinfo=TZ).isoformat()
    return {
        "Name": {"title": text("花呗还款")},
        "Date": {"date": {"start": date}},
        "Amount": {"number": float(Decimal(row["amount_yuan"]))},
        "From Account": {"relation": [{"id": from_account_id}]},
        "To Account": {"relation": [{"id": huabei_account_id}]},
    }


def run(source, notion, apply=False, on_progress=None, settings=None):
    settings = settings or {"sources": SOURCES, "accounts": ACCOUNTS}
    sources, account_ids = settings["sources"], settings["accounts"]
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "category" not in rows[0]:
        raise ValueError("请先运行 classify_jev.py 生成含 category 的 CSV")
    keys = [r["key"] for r in rows if (r["kind"] in ("expense", "income", "refund") or is_huabei_repayment(r))
            and not classify_jev.is_family_card(r.get("method", ""))]
    if len(keys) != len(set(keys)):
        raise ValueError("输入文件存在重复来源键；先处理账单冲突")
    unknown_account = next((r for r in rows if (r["kind"] in ("expense", "income", "refund") or is_huabei_repayment(r))
                            and not classify_jev.is_family_card(r.get("method", ""))
                            and r.get("account_suggestion") == "unknown" and not bank_name(r)
                            and not classify_jev.is_huabei(r.get("method", ""))), None)
    if unknown_account:
        raise ValueError(f"Jev 无法识别付款账户: {unknown_account['method']}")
    existing = {}
    def report(phase, phase_index, detail, done=0, total=0, actions=None, latest=""):
        if on_progress:
            on_progress({"phase": phase, "phase_index": phase_index, "phase_count": 5,
                         "detail": detail, "done": done, "total": total,
                         "actions": dict(actions or {}), "latest": latest})

    def tracked_rows(source_id, name, phase_index):
        report(name, phase_index, "正在连接 Notion…")
        return notion.rows(source_id, lambda count, more: report(
            name, phase_index, f"已读取 {count} 条" + ("，继续读取…" if more else "，读取完成"), count, 0))

    for kind in ("expense", "income"):
        for page in tracked_rows(sources[kind], "读取已有交易", 1):
            match = re.search(r"(?:^|; )key=([0-9a-f]{20})(?:;|$)", plain(page["properties"]["Misc"]))
            if match:
                if match.group(1) in existing:
                    raise ValueError(f"Notion 中存在重复来源键 {match.group(1)}")
                existing[match.group(1)] = (kind, page)
    categories = {}
    for page in tracked_rows(sources["categories"], "读取类别", 2):
        props = page["properties"]
        name = plain(props["Name"])
        archived = props["Archived"]["checkbox"]
        if name not in categories or (categories[name][1] and not archived):
            categories[name] = (page["id"], archived)
    wanted = {r["category"] for r in rows if r["kind"] == "expense" and r["category"] not in ("", "Review")}
    missing = sorted(wanted - categories.keys())
    bank_accounts = {plain(p["properties"]["Name"]): p["id"] for p in tracked_rows(sources["accounts"], "读取账户", 3)
                     if not p["properties"]["Archived"]["checkbox"]}
    subscriptions = {}
    for page in tracked_rows(sources["subscriptions"], "读取订阅", 3):
        props = page["properties"]
        fingerprint = (plain(props["Name"]).casefold(), tuple(
            relation["id"] for relation in props.get("Account", {}).get("relation", [])))
        subscriptions[fingerprint] = (page["id"], props.get("Archived", {}).get("checkbox", False))
    transfers = {}
    for page in tracked_rows(sources["transfer"], "读取还款转账", 3):
        props = page["properties"]
        date = props.get("Date", {}).get("date", {}).get("start", "")
        fingerprint = (plain(props["Name"]).casefold(), date[:10], Decimal(str(props["Amount"]["number"])),
                       tuple(x["id"] for x in props.get("From Account", {}).get("relation", [])),
                       tuple(x["id"] for x in props.get("To Account", {}).get("relation", [])))
        transfers[fingerprint] = page["id"]
    missing_accounts = {name for r in rows if r["kind"] in ("expense", "income", "refund") or is_huabei_repayment(r)
                        if (name := bank_name(r)) and name not in bank_accounts}
    if any(classify_jev.is_huabei(r.get("method", "")) or is_huabei_repayment(r) for r in rows) and "花呗" not in bank_accounts:
        missing_accounts.add("花呗")
    missing_accounts = sorted(missing_accounts)
    actions = {"create_categories": missing, "create_accounts": missing_accounts,
               "create_expense": 0, "create_income": 0, "create_refund": 0, "create_subscription": 0,
               "create_huabei_repayment": 0, "update_category": 0, "update_account": 0,
               "already_correct": 0, "needs_review": 0}
    if apply:
        for name in missing_accounts:
            page = notion.call("POST", "pages", {"parent": {"type": "data_source_id", "data_source_id": sources["accounts"]},
                                                  "properties": {"Name": {"title": text(name)},
                                                                 "Account Type": {"select": {"name": "Credit Card" if "信用卡" in name or name == "花呗" else "Checking Account"}},
                                                                 "Archived": {"checkbox": False}}})
            bank_accounts[name] = page["id"]
        for name in missing:
            page = notion.call("POST", "pages", {"parent": {"type": "data_source_id", "data_source_id": sources["categories"]},
                                                  "properties": {"Name": {"title": text(name)}, "Archived": {"checkbox": False}}})
            categories[name] = (page["id"], False)
    report("核对账单", 4, "开始逐笔比对", 0, len(rows), actions)
    seen_transfers = set()
    for index, row in enumerate(rows, 1):
        if classify_jev.is_family_card(row.get("method", "")):
            report("核对账单", 4, "跳过非本人付款亲情卡", index, len(rows), actions, row["party"] or row["item"])
            continue
        source_kind = row["kind"]
        kind = "income" if source_kind == "refund" else source_kind
        if is_huabei_repayment(row):
            if not row["key"]:
                raise ValueError("花呗还款记录缺少来源键")
            from_account_id = account(row, bank_accounts, account_ids)
            huabei_id = bank_accounts.get("花呗")
            name = "花呗还款"
            fingerprint = (name.casefold(), row["time"][:10], Decimal(row["amount_yuan"]),
                           (from_account_id,), (huabei_id or "new-account:huabei",))
            if fingerprint in transfers or fingerprint in seen_transfers:
                actions["already_correct"] += 1
            else:
                actions["create_huabei_repayment"] += 1
                if apply:
                    notion.call("POST", "pages", {
                        "parent": {"type": "data_source_id", "data_source_id": sources["transfer"]},
                        "properties": transfer_properties(row, from_account_id, huabei_id),
                    })
                seen_transfers.add(fingerprint)
            report("核对账单", 4, "记录花呗还款转账", index, len(rows), actions, name)
            continue
        if kind not in ("expense", "income"):
            report("核对账单", 4, "跳过非收支记录", index, len(rows), actions, row["party"] or row["item"])
            continue
        if not row["key"]:
            raise ValueError("导入候选缺少来源键")
        category = row["category"] if kind == "expense" else ""
        if kind == "expense" and category == "Subscribe":
            name = row["party"] or row["item"] or row["type"]
            fingerprint = (name.casefold(), (subscription_account_key(row, bank_accounts, account_ids),))
            if fingerprint not in subscriptions:
                actions["create_subscription"] += 1
                if apply:
                    page = notion.call("POST", "pages", {
                        "parent": {"type": "data_source_id", "data_source_id": sources["subscriptions"]},
                        "properties": subscription_properties(row, categories["Subscribe"][0], bank_accounts, account_ids),
                    })
                    subscriptions[fingerprint] = (page["id"], False)
                else:
                    subscriptions[fingerprint] = ("preview", False)
            else:
                page_id, archived = subscriptions[fingerprint]
                if archived:
                    if apply:
                        notion.call("PATCH", f"pages/{page_id}", {"properties": {"Archived": {"checkbox": False}}})
                    subscriptions[fingerprint] = (page_id, False)
                actions["already_correct"] += 1
            report("核对账单", 4, "核对订阅", index, len(rows), actions, name)
            continue
        if category == "Review":
            actions["needs_review"] += 1
        page_info = existing.get(row["key"])
        if page_info:
            old_kind, page = page_info
            if old_kind != kind or Decimal(str(page["properties"]["Amount"]["number"])) != Decimal(row["amount_yuan"]):
                raise ValueError(f"已有交易与账单冲突: {row['key']}")
            current = [x["id"] for x in page["properties"].get("Categories", {}).get("relation", [])]
            target = categories.get(category, (None, False))[0]
            desired_account = account(row, bank_accounts, account_ids) if not classify_jev.is_huabei(row.get("method", "")) or "花呗" in bank_accounts else None
            current_account = [x["id"] for x in page["properties"].get("Accounts", {}).get("relation", [])]
            account_change = desired_account is None or current_account != [desired_account]
            changed = False
            if category and category != "Review" and (target is None or current != [target]):
                actions["update_category"] += 1
                changed = True
                if apply:
                    notion.call("PATCH", f"pages/{page['id']}", {"properties": {"Categories": {"relation": [{"id": target}]}}})
            if account_change:
                actions["update_account"] += 1
                changed = True
                if apply and desired_account:
                    notion.call("PATCH", f"pages/{page['id']}", {"properties": {"Accounts": {"relation": [{"id": desired_account}]}}})
            if not changed:
                actions["already_correct"] += 1
            report("核对账单", 4, "检查已有记录", index, len(rows), actions, row["party"] or row["item"])
            continue
        if kind == "expense" and category == "Review":
            report("核对账单", 4, "支出类别待复核", index, len(rows), actions, row["party"] or row["item"])
            continue
        actions["create_refund" if source_kind == "refund" else "create_" + kind] += 1
        if apply:
            notion.call("POST", "pages", {"parent": {"type": "data_source_id", "data_source_id": sources[kind]},
                                          "properties": finance_properties(row, categories.get(category, (None, False))[0], bank_accounts, account_ids)})
        report("核对账单", 4, "生成预览条目", index, len(rows), actions, row["party"] or row["item"])
    report("预览完成", 5, "Notion 数据仅查询，尚未写入", len(rows), len(rows), actions)
    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--apply", action="store_true", help="实际写入 Notion；默认只预览")
    args = parser.parse_args()
    print(json.dumps(run(args.source, Notion(token_from_env(args.env)), args.apply,
                        settings=settings_from_env(args.env)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
