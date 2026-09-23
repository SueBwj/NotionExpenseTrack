"""Prepare WeChat Pay and Alipay CSV exports for review before Notion import."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


FIELDS = {
    "time": ("交易时间", "创建时间", "付款时间"),
    "type": ("交易类型", "交易分类", "类型"),
    "party": ("交易对方", "对方", "商家名称"),
    "item": ("商品", "商品名称", "商品说明"),
    "direction": ("收/支", "收支类型", "收支"),
    "amount": ("金额(元)", "金额（元）", "金额", "交易金额"),
    "status": ("当前状态", "交易状态", "状态"),
    "id": ("交易单号", "交易订单号", "交易号", "支付宝交易号", "微信支付交易单号"),
    "method": ("支付方式", "收/付款方式", "支付渠道", "资金渠道"),
    "note": ("备注", "备注说明"),
}
OUTPUT = ("key", "source", "time", "type", "party", "item", "direction", "amount_yuan", "status", "method", "transaction_id", "note", "kind", "reason")


def read_export(path):
    if path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError("读取 xlsx 需要 openpyxl；请使用 README 中的运行环境") from None
        workbook = load_workbook(path, read_only=True, data_only=True)
        candidates = [list(sheet.values) for sheet in workbook]
    else:
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                content = data.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError(f"{path}: 无法识别编码")
        candidates = [list(csv.reader(content.splitlines(), delimiter=d)) for d in (",", "\t")]
    for rows in candidates:
        for index, row in enumerate(rows):
            names = {str(cell).strip().lstrip("\ufeff") for cell in row if cell is not None}
            if any(x in names for x in FIELDS["time"]) and any(x in names for x in FIELDS["amount"]):
                header = [str(cell).strip().lstrip("\ufeff") if cell is not None else "" for cell in row]
                return [dict(zip(header, cells)) for cells in rows[index + 1:]], header
    raise ValueError(f"{path}: 找不到交易表头")


def value(row, field):
    return next((str(row[name]).strip() for name in FIELDS[field] if name in row and row[name] is not None), "")


def normalize(path, source):
    rows, header = read_export(path)
    missing = [name for name in ("time", "direction", "amount", "status", "id") if not any(x in header for x in FIELDS[name])]
    if missing:
        raise ValueError(f"{path}: 缺少必要列 {', '.join(missing)}")
    result = []
    for line, row in enumerate(rows, 2):
        if not any(str(v).strip() for v in row.values() if v is not None):
            continue
        raw_amount = value(row, "amount").replace(",", "").replace("¥", "").replace("￥", "")
        try:
            amount = Decimal(raw_amount)
            if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise ValueError(f"{path}: 交易行 {line} 金额无效: {raw_amount!r}") from None
        if amount == 0:
            continue
        when = value(row, "time")
        try:
            date = datetime.fromisoformat(when.replace("/", "-"))
        except ValueError:
            raise ValueError(f"{path}: 交易行 {line} 时间无效: {when!r}") from None
        direction = value(row, "direction")
        status = value(row, "status")
        kind = "review"
        reason = "需人工确认"
        if re.search("失败|关闭|撤销|未支付|待支付|处理中", status):
            kind, reason = "exclude", "未完成交易"
        elif not re.search("成功|完成|已收款|已收钱|已转账|已存入|已到账|已退款|已全额退款", status):
            kind, reason = "review", "交易状态未识别"
        elif re.search("退款|退回", value(row, "type")) or (direction == "收入" and re.search("退款|退回", status)):
            kind, reason = "refund", "退款"
        elif re.search("充值|提现|还款|余额宝转入|余额宝转出", value(row, "type")) or re.search("信用卡还款", value(row, "item")):
            kind, reason = "transfer", "资金转移"
        elif re.search("转账|红包|亲属卡", value(row, "type") + value(row, "item")):
            kind, reason = "review", "转账或红包需确认用途"
        elif direction == "支出":
            kind, reason = "expense", "已完成支出"
        elif direction == "收入":
            kind, reason = "income", "已完成收入"
        transaction_id = value(row, "id")
        if not transaction_id and kind != "exclude":
            kind, reason = "review", "缺少交易单号"
        key = hashlib.sha256(f"{source}|{transaction_id}|{value(row, 'type')}".encode()).hexdigest()[:20] if transaction_id else ""
        result.append(dict(key=key, source=source, time=date.isoformat(sep=" "), type=value(row, "type"),
                           party=value(row, "party"), item=value(row, "item"), direction=direction,
                           amount_yuan=f"{amount:.2f}", status=status, method=value(row, "method"),
                           transaction_id=transaction_id, note=value(row, "note"), kind=kind, reason=reason))
    return result


def run(files, out, month=None):
    records = []
    seen = {}
    outside_month = 0
    for source, path in files:
        for row in normalize(path, source):
            if month and row["time"][:7] != month:
                outside_month += 1
                continue
            if row["key"] and row["kind"] != "exclude":
                signature = (row["time"], row["direction"], row["amount_yuan"])
                if row["key"] in seen:
                    original = records[seen[row["key"]]]
                    if (original["time"], original["direction"], original["amount_yuan"]) == signature:
                        row["kind"], row["reason"] = "exclude", "重复交易单号"
                    else:
                        original["kind"], original["reason"] = "review", "同单号内容冲突"
                        row["kind"], row["reason"] = "review", "同单号内容冲突"
                else:
                    seen[row["key"]] = len(records)
            records.append(row)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "normalized.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT)
        writer.writeheader()
        writer.writerows(records)
    review = [r for r in records if r["kind"] == "review"]
    with (out / "review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT)
        writer.writeheader()
        writer.writerows(review)
    summary = {"month": month, "outside_month": outside_month,
               "counts": dict(Counter(r["kind"] for r in records)),
               "amount_yuan": {kind: str(sum((Decimal(r["amount_yuan"]) for r in records if r["kind"] == kind), Decimal(0)))
                               for kind in ("expense", "income", "refund", "transfer", "review")}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat", type=Path, action="append", default=[])
    parser.add_argument("--alipay", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, default=Path("output"))
    parser.add_argument("--month", help="自然月，格式 YYYY-MM")
    args = parser.parse_args()
    if not args.wechat and not args.alipay:
        parser.error("至少提供一份账单")
    if args.month:
        try:
            datetime.strptime(args.month, "%Y-%m")
        except ValueError:
            parser.error("--month 格式必须是 YYYY-MM")
    print(json.dumps(run([*(('wechat', p) for p in args.wechat), *(('alipay', p) for p in args.alipay)], args.out, args.month), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
