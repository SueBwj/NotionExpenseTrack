import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import classify_jev


class ClassifyJevTest(unittest.TestCase):
    def test_family_card_is_excluded_without_calling_jev(self):
        with patch.object(classify_jev, "urlopen") as urlopen:
            result = classify_jev.classify("店铺", "商品", "token", method="亲情卡(少忠(王少忠))")
        self.assertEqual(result[0], "exclude")
        self.assertEqual(result[4], "unknown")
        urlopen.assert_not_called()

    def test_jev_account_suggestion_is_overridden_for_huabei(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "input.csv", Path(folder) / "output.csv"
            source.write_text(
                "key,kind,party,item,type,direction,status,source,method\n"
                "1,expense,Store,Order,消费,支出,支付成功,alipay,花呗分期(3期)\n", encoding="utf-8-sig")
            with patch.object(classify_jev, "classify", return_value=("expense", 0.9, "Shopping", 0.9, "source_wallet", 0.9)):
                classify_jev.run(source, output, "test-token")
            with output.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["account_suggestion"], "huabei")

    def test_redacts_personal_identifiers_before_sending(self):
        with patch.object(classify_jev, "urlopen") as urlopen, patch.object(
                classify_jev.json, "load", return_value={
                    "answers": {"kind": {"choice": "expense", "probabilities": {"expense": 0.9}},
                                "category": {"choice": "Food", "probabilities": {"Food": 0.9}},
                                "payment_account": {"choice": "bank_account", "probabilities": {"bank_account": 0.95}}}}):
            classify_jev.classify("Cafe 13800138000", "Lunch sue@example.com 123456789012", "token",
                                  "消费", "支出", "交易成功", "alipay", "示例银行储蓄卡(1234)&中国银行立减金")
            body = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(body["state"], {"merchant": "Cafe [手机号]", "item": "Lunch [邮箱] [编号]",
                                              "transaction_type": "消费", "direction": "支出", "status": "交易成功",
                                              "source": "alipay", "payment_method": "示例银行储蓄卡(1234)&中国银行立减金"})
            self.assertIn("kind", body["questions"])
            self.assertIn("payment_account", body["questions"])
            self.assertIn("Subscribe", body["questions"]["category"]["criteria"])

    def test_jev_assigns_transaction_kind_without_manual_review(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "input.csv", Path(folder) / "output.csv"
            source.write_text(
                "key,kind,party,item,type,direction,status,source,method\n"
                "1,review,Shop,Refund,退款,收入,退款成功,alipay,支付宝余额\n"
                "2,expense,Cafe,Coffee,消费,支出,支付成功,alipay,示例银行储蓄卡(1234)\n"
                "3,review,Shop,Unknown,其他,支出,交易成功,wechat,零钱\n",
                encoding="utf-8-sig")
            answers = [("refund", 0.99, "Other", 0.99, "source_wallet", 0.99),
                       ("expense", 0.72, "Food", 0.72, "bank_account", 0.98),
                       ("exclude", 0.99, "Other", 0.99, "source_wallet", 0.99)]
            progress = []
            with patch.object(classify_jev, "classify", side_effect=answers):
                summary = classify_jev.run(source, output, "test-token",
                                           lambda done, total, row: progress.append((done, total, row["kind"])))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["kind"] for r in rows], ["refund", "expense", "exclude"])
            self.assertEqual(rows[1]["category"], "Food")
            self.assertEqual(rows[1]["account_suggestion"], "bank_account")
            self.assertEqual(rows[0]["category"], "")
            self.assertEqual(summary["kind_counts"], {"expense": 1, "income": 0, "refund": 1, "transfer": 0, "exclude": 1})
            self.assertEqual(progress, [(1, 3, "refund"), (2, 3, "expense"), (3, 3, "exclude")])


if __name__ == "__main__":
    unittest.main()
