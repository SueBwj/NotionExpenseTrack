import csv
import tempfile
import unittest
from pathlib import Path

from notion_import import ACCOUNTS, SOURCES, account, run


class FakeNotion:
    def __init__(self):
        self.created = []

    def rows(self, source_id, on_page=None):
        pages = []
        if source_id == SOURCES["categories"]:
            pages.append({"id": "archived-food", "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Food"}]},
                "Archived": {"checkbox": True},
            }})
        if source_id == SOURCES["accounts"]:
            pages.append({"id": "boc-account", "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "示例银行储蓄卡（1234）"}]},
                "Archived": {"checkbox": False},
            }})
        if on_page:
            on_page(len(pages), False)
        yield from pages

    def call(self, method, path, payload):
        self.created.append((method, path, payload))
        return {"id": "new-page"}


class FakeExistingHuabei(FakeNotion):
    def rows(self, source_id, on_page=None):
        pages = []
        if source_id == SOURCES["expense"]:
            pages.append({"id": "old-expense", "properties": {
                "Misc": {"type": "rich_text", "rich_text": [{"plain_text": "key=72345678901234567890"}]},
                "Amount": {"number": 1200.0},
                "Categories": {"relation": []},
                "Accounts": {"relation": [{"id": "fixture-alipay-account"}]},
            }})
        if source_id == SOURCES["categories"]:
            pages.append({"id": "food-category", "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Food"}]},
                "Archived": {"checkbox": False},
            }})
        yield from pages


class NotionImportTest(unittest.TestCase):
    def test_jev_account_choice_handles_bank_card_with_promotion_suffix(self):
        row = {"source": "alipay", "method": "示例银行储蓄卡(1234)&中国银行立减金",
               "account_suggestion": "bank_account"}
        self.assertEqual(account(row, {}, ACCOUNTS), ACCOUNTS["bank_account"])

    def test_reuses_archived_category_instead_of_creating_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category",
                ])
                writer.writeheader()
                writer.writerow({
                    "key": "12345678901234567890", "source": "wechat", "time": "2026-08-01 10:00:00",
                    "type": "商户消费", "party": "店铺", "item": "午餐", "amount_yuan": "12.30",
                    "status": "支付成功", "method": "零钱", "transaction_id": "tx1", "note": "",
                    "kind": "expense", "category": "Food",
                })
                writer.writerow({
                    "key": "22345678901234567890", "source": "wechat", "time": "2026-08-02 10:00:00",
                    "type": "工资", "party": "雇主", "item": "工资", "amount_yuan": "100.00",
                    "status": "到账", "method": "示例银行储蓄卡（1234）", "transaction_id": "tx2", "note": "",
                    "kind": "income", "category": "",
                })
                writer.writerow({
                    "key": "32345678901234567890", "source": "wechat", "time": "2026-08-03 10:00:00",
                    "type": "退款", "party": "店铺", "item": "午餐退款", "amount_yuan": "2.30",
                    "status": "退款成功", "method": "零钱", "transaction_id": "tx3", "note": "",
                    "kind": "refund", "category": "",
                })

            notion = FakeNotion()
            progress = []
            actions = run(source, notion, apply=True, on_progress=progress.append)

        self.assertEqual(actions["create_categories"], [])
        self.assertEqual(actions["create_expense"], 1)
        self.assertEqual(actions["create_income"], 1)
        self.assertEqual(actions["create_refund"], 1)
        self.assertEqual(len(notion.created), 3)
        self.assertTrue(any(p["phase"] == "核对账单" for p in progress))
        self.assertEqual(progress[-1]["phase"], "预览完成")
        properties = notion.created[0][2]["properties"]
        self.assertEqual(properties["Categories"]["relation"], [{"id": "archived-food"}])
        self.assertEqual([item[2]["parent"]["data_source_id"] for item in notion.created], [
            SOURCES["expense"], SOURCES["income"], SOURCES["income"],
        ])
        self.assertNotIn("Categories", notion.created[1][2]["properties"])
        self.assertNotIn("Categories", notion.created[2][2]["properties"])

    def test_creates_one_account_for_new_bank_card(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category",
                ])
                writer.writeheader()
                for index in (1, 2):
                    writer.writerow({
                        "key": str(index).zfill(20), "source": "wechat", "time": "2026-09-01 10:00:00",
                        "type": "商户消费", "party": "店铺", "item": "午餐", "amount_yuan": "12.30",
                        "status": "支付成功", "method": "建设银行储蓄卡(5278)",
                        "transaction_id": f"tx{index}", "note": "", "kind": "expense", "category": "Food",
                    })
            notion = FakeNotion()
            actions = run(source, notion, apply=True)

        self.assertEqual(actions["create_accounts"], ["建设银行储蓄卡（5278）"])
        self.assertEqual(len(notion.created), 3)
        self.assertEqual(notion.created[0][2]["properties"]["Account Type"]["select"]["name"], "Checking Account")
        for _, _, payload in notion.created[1:]:
            self.assertEqual(payload["properties"]["Accounts"]["relation"], [{"id": "new-page"}])

    def test_routes_subscriptions_to_subscriptions_database_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category", "account_suggestion",
                ])
                writer.writeheader()
                for index in (1, 2):
                    writer.writerow({
                        "key": str(index).zfill(20), "source": "alipay", "time": "2026-09-01 10:00:00",
                        "type": "订阅扣费", "party": "Music Plus", "item": "Music Plus 月费",
                        "amount_yuan": "18.00", "status": "支付成功", "method": "支付宝余额",
                        "transaction_id": f"tx{index}", "note": "", "kind": "expense",
                        "category": "Subscribe", "account_suggestion": "source_wallet",
                    })
            notion = FakeNotion()
            actions = run(source, notion, apply=True)

        subscription = next((call for call in notion.created if call[1] == "pages"
                             and call[2]["parent"]["data_source_id"] == SOURCES["subscriptions"]), None)
        self.assertEqual(actions["create_subscription"], 1)
        self.assertEqual(actions["create_expense"], 0)
        self.assertIsNotNone(subscription)
        properties = subscription[2]["properties"]
        self.assertEqual(properties["Type"]["select"]["name"], "Monthly")
        self.assertEqual(properties["Account"]["relation"], [{"id": ACCOUNTS["alipay"]}])

    def test_never_imports_family_card_even_if_old_classification_says_expense(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category", "account_suggestion",
                ])
                writer.writeheader()
                writer.writerow({
                    "key": "42345678901234567890", "source": "alipay", "time": "2026-09-01 10:00:00",
                    "type": "商户消费", "party": "店铺", "item": "订单", "amount_yuan": "12.00",
                    "status": "支付成功", "method": "亲情卡(家人)", "transaction_id": "tx-family",
                    "note": "", "kind": "expense", "category": "Food", "account_suggestion": "unknown",
                })
            notion = FakeNotion()
            actions = run(source, notion, apply=True)

        self.assertEqual(actions["create_expense"], 0)
        self.assertEqual(notion.created, [])

    def test_maps_huabei_purchases_and_repayments_without_expense_repayment_double_count(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category", "account_suggestion",
                ])
                writer.writeheader()
                writer.writerow({
                    "key": "52345678901234567890", "source": "alipay", "time": "2026-09-01 10:00:00",
                    "type": "数码电器", "party": "店铺", "item": "耳机", "amount_yuan": "1200.00",
                    "status": "支付成功", "method": "花呗分期(3期)", "transaction_id": "purchase",
                    "note": "", "kind": "expense", "category": "Shopping", "account_suggestion": "huabei",
                })
                writer.writerow({
                    "key": "62345678901234567890", "source": "alipay", "time": "2026-09-20 10:00:00",
                    "type": "信用借还", "party": "支付宝花呗", "item": "花呗还款", "amount_yuan": "400.00",
                    "status": "还款成功", "method": "示例银行储蓄卡(1234)", "transaction_id": "repayment",
                    "note": "", "kind": "transfer", "category": "", "account_suggestion": "bank_account",
                })
            notion = FakeNotion()
            actions = run(source, notion, apply=True)

        self.assertEqual(actions["create_accounts"], ["花呗"])
        self.assertEqual(actions["create_expense"], 1)
        self.assertEqual(actions["create_huabei_repayment"], 1)
        self.assertEqual(len([c for c in notion.created if c[1] == "pages"]), 4)
        expense = next(c for c in notion.created if c[2].get("parent", {}).get("data_source_id") == SOURCES["expense"])
        repayment = next(c for c in notion.created if c[2].get("parent", {}).get("data_source_id") == SOURCES["transfer"])
        self.assertEqual(expense[2]["properties"]["Accounts"]["relation"], [{"id": "new-page"}])
        self.assertEqual(repayment[2]["properties"]["From Account"]["relation"], [{"id": ACCOUNTS["bank_account"]}])
        self.assertEqual(repayment[2]["properties"]["To Account"]["relation"], [{"id": "new-page"}])

    def test_updates_already_imported_huabei_purchase_to_huabei_account(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "classified.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "key", "source", "time", "type", "party", "item", "amount_yuan",
                    "status", "method", "transaction_id", "note", "kind", "category", "account_suggestion",
                ])
                writer.writeheader()
                writer.writerow({
                    "key": "72345678901234567890", "source": "alipay", "time": "2026-09-01 10:00:00",
                    "type": "数码电器", "party": "店铺", "item": "耳机", "amount_yuan": "1200.00",
                    "status": "支付成功", "method": "花呗", "transaction_id": "purchase",
                    "note": "", "kind": "expense", "category": "Food", "account_suggestion": "huabei",
                })
            notion = FakeExistingHuabei()
            actions = run(source, notion, apply=True)

        self.assertEqual(actions["update_account"], 1)
        patched = next(call for call in notion.created if call[0] == "PATCH" and call[1] == "pages/old-expense"
                       and "Accounts" in call[2]["properties"])
        self.assertEqual(patched[2]["properties"]["Accounts"]["relation"], [{"id": "new-page"}])


if __name__ == "__main__":
    unittest.main()
