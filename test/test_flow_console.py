import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import flow_console


class FlowConsoleTest(unittest.TestCase):
    def test_multipart_and_run_state_survive_review_and_import_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            (root / ".env").write_text("OPENROUTER_API_KEY=test\nNOTION_API_KEY=test\n", encoding="utf-8")
            original_root, original_output = flow_console.ROOT, flow_console.OUTPUT
            flow_console.ROOT, flow_console.OUTPUT = root, output
            try:
                csv_data = ("交易时间,交易类型,交易对方,商品,收/支,金额(元),当前状态,交易单号,支付方式\n"
                            "2026-08-01 10:00:00,商户消费,Cafe,Coffee,支出,12.30,支付成功,ABC1,零钱\n"
                            "2026-08-02 10:00:00,转账,朋友,转账,支出,5.00,支付成功,ABC2,零钱\n")
                boundary = "----money-manage-test"
                multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"month\"\r\n\r\n2026-08\r\n"
                             f"--{boundary}\r\nContent-Disposition: form-data; name=\"wechat\"; filename=\"bill.csv\"\r\n"
                             "Content-Type: text/csv\r\n\r\n" + csv_data + f"\r\n--{boundary}--\r\n").encode()
                parser = SimpleNamespace(headers={"Content-Length": str(len(multipart)),
                                                  "Content-Type": f"multipart/form-data; boundary={boundary}"},
                                         rfile=io.BytesIO(multipart))
                form = flow_console.Handler._multipart(parser)
                self.assertEqual(form["month"], "2026-08")
                self.assertEqual(form["wechat"][0][0], "bill.csv")

                dummy = SimpleNamespace(_json=lambda code, value: setattr(dummy, "response", (code, value)))
                dummy._multipart = lambda: form
                flow_console.Handler._new_run(dummy)
                run = dummy.response[1]
                rid = run["id"]
                self.assertEqual(run["stage"], "classification")

                get_dummy = SimpleNamespace(path=f"/api/runs/{rid}/rows", headers={}, _check_origin=lambda: None,
                                            _json=lambda code, value: setattr(get_dummy, "response", (code, value)),
                                            _error=lambda code, value: setattr(get_dummy, "response", (code, value)))
                with patch.object(flow_console, "run_dir", return_value=flow_console.OUTPUT / "2026-08" / ".console" / rid):
                    flow_console.Handler.do_GET(get_dummy)
                self.assertEqual(len(get_dummy.response[1]["rows"]), 2)

                def route(path, body=None):
                    dummy._body = lambda: body or {}
                    flow_console.Handler._route_post(dummy, path)
                    return dummy.response[1]

                with patch.object(flow_console.classify_jev, "classify", side_effect=[
                        ("expense", 0.9, "Food", 0.6, "source_wallet", 0.99),
                        ("transfer", 0.9, "Other", 0.99, "source_wallet", 0.99)]):
                    run = route(f"/api/runs/{rid}/classify")
                self.assertEqual(run["stage"], "preview")
                self.assertEqual(run["stages"]["classification"]["kind_counts"]["transfer"], 1)
                with patch.object(flow_console.notion_import, "run", return_value={"create_expense": 1}):
                    run = route(f"/api/runs/{rid}/preview")
                self.assertEqual(run["stage"], "import")
                with patch.object(flow_console.notion_import, "run", side_effect=[RuntimeError("offline"), {"create_expense": 1}]):
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        route(f"/api/runs/{rid}/import")
                    persisted = flow_console.read_run(flow_console.run_dir(rid))
                    self.assertEqual(persisted["stages"]["import"]["status"], "error")
                    run = route(f"/api/runs/{rid}/import")
                self.assertEqual(run["stage"], "complete")
                self.assertEqual(len(run["edits"]), 0)
                self.assertEqual(len(flow_console.records()), 1)
            finally:
                flow_console.ROOT, flow_console.OUTPUT = original_root, original_output


if __name__ == "__main__":
    unittest.main()
