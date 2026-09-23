import tempfile
import unittest
from pathlib import Path

from bills import run


class BillsTest(unittest.TestCase):
    def test_zero_amount_rows_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bill = root / "alipay.csv"
            bill.write_text("交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号\n"
                            "2026-09-17 10:00:00,其他,平台,零金额记录,支出,0.00,余额,交易成功,ZERO\n"
                            "2026-09-17 10:01:00,其他,商户,正常交易,支出,1.00,余额,交易成功,PAID\n",
                            encoding="utf-8")
            result = run([("alipay", bill)], root / "out")
            self.assertEqual(result["counts"], {"expense": 1})

    def test_alipay_export_columns_normalize_to_common_transactions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bill = root / "alipay.csv"
            bill.write_text("支付宝交易记录明细\n交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
                            "2026-09-23 14:02:09,其他,商户,account,商品,支出,72.78,示例银行储蓄卡(1234),交易成功,ORDER1,MERCHANT1,\n",
                            encoding="utf-8-sig")
            result = run([("alipay", bill)], root / "out", "2026-09")
            self.assertEqual(result["counts"], {"expense": 1})
            import csv
            with (root / "out" / "normalized.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["transaction_id"], "ORDER1")
            self.assertEqual(row["method"], "示例银行储蓄卡(1234)")

    def test_classify_and_dedupe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bill = root / "wechat.csv"
            bill.write_text("微信支付账单\n交易时间,交易类型,交易对方,商品,收/支,金额(元),当前状态,交易单号,支付方式\n"
                            "2026-08-01 10:00:00,商户消费,超市,食品,支出,12.30,支付成功,123,零钱\n"
                            "2026-08-02 10:00:00,退款,超市,食品,收入,2.30,退款成功,124,零钱\n"
                            "2026-08-03 10:00:00,转账,朋友,转账,支出,5.00,支付成功,125,零钱\n"
                            "2026-08-04 10:00:00,商户消费,房东,电费充值,支出,20.08,支付成功,126,零钱\n", encoding="utf-8-sig")
            result = run([("wechat", bill), ("wechat", bill)], root / "out")
            self.assertEqual(result["counts"], {"expense": 2, "refund": 1, "review": 1, "exclude": 4})
            self.assertEqual(result["amount_yuan"]["expense"], "32.38")

    def test_unknown_status_and_conflicting_duplicate_need_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bill = root / "alipay.csv"
            bill.write_text("交易时间,交易类型,收/支,金额,交易状态,交易单号\n"
                            "2026-08-01 10:00:00,消费,支出,10,交易成功,A1\n"
                            "2026-08-01 10:00:00,消费,支出,11,交易成功,A1\n"
                            "2026-08-01 11:00:00,消费,支出,12,未知状态,A2\n"
                            "2026-08-01 12:00:00,商户消费,支出,20,已全额退款,A3\n"
                            "2026-08-01 13:00:00,退款,收入,20,已全额退款,A4\n"
                            "2026-08-01 14:00:00,商户消费,支出,30,已退款(¥0.63),A5\n"
                            "2026-08-01 15:00:00,退款,收入,0.63,已退款¥0.63,A6\n"
                            "2026-08-01 16:00:00,商户消费,支出,20,支付成功,A7\n", encoding="utf-8")
            result = run([("alipay", bill)], root / "out")
            self.assertEqual(result["counts"], {"review": 3, "expense": 3, "refund": 2})

    def test_failed_transactions_are_excluded_and_missing_ids_are_reviewed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bill = root / "wechat.csv"
            bill.write_text("交易时间,交易类型,收/支,金额,当前状态,交易单号\n"
                            "2026-08-01 10:00:00,商户消费,支出,10,支付失败,FAIL1\n"
                            "2026-08-01 11:00:00,商户消费,支出,12,支付成功,\n", encoding="utf-8")
            result = run([("wechat", bill)], root / "out")
            self.assertEqual(result["counts"], {"exclude": 1, "review": 1})


if __name__ == "__main__":
    unittest.main()
