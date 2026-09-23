"""Classify normalized transactions with TypeSafe Jev through OpenRouter."""

import argparse
import csv
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CATEGORIES = {
    "Food": "餐馆、外卖、饮品和日常食品",
    "Housing": "房租、宿舍、水电燃气和居住相关费用",
    "Transportation": "租车、打车、公共交通、停车、过路费和充电",
    "Academics": "学费、教材、课程和学习相关费用",
    "Shopping": "衣物、日用品、电子产品和其他商品购物",
    "Entertainment": "游戏、电影、旅游娱乐和休闲活动",
    "Health": "医疗、药品和健康服务",
    "Subscribe": "软件、影音、会员等周期性订阅服务",
    "Other": "无法归入上述类别的支出",
}
KINDS = {
    "expense": "已付款的商品或服务消费，包括已付款但尚未确认收货的订单",
    "income": "实际收到的非退款收入",
    "refund": "商家退款或退回的款项",
    "transfer": "自有账户间转移、充值、提现、信用卡还款等，不计收支",
    "exclude": "失败、关闭、未付款、重复或不属于实际收支的记录",
}
PAYMENT_ACCOUNTS = {
    "source_wallet": "使用交易来源对应的钱包账户（微信来源选微信钱包，支付宝来源选支付宝钱包）",
    "bank_account": "使用账单付款方式中识别出的银行卡账户",
    "huabei": "使用支付宝花呗或花呗分期账户",
    "unknown": "付款账户无法从付款方式中可靠判断",
}


def api_key(path):
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"\'')
        if line.startswith("sk-"):
            return line
    raise ValueError(".env 中未找到 OPENROUTER_API_KEY")


def is_family_card(method):
    return "亲情卡" in (method or "")


def is_huabei(method):
    return "花呗" in (method or "")


def classify(party, item, token, transaction_type="", direction="", status="", source="", method=""):
    if is_family_card(method):
        return "exclude", 1.0, "Other", 0.0, "unknown", 1.0

    # Jev needs transaction context, but not contact details, amounts, or account identifiers.
    def redact(text):
        text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]", text)
        text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[手机号]", text)
        return re.sub(r"[A-Za-z]*\d{8,}[A-Za-z\d-]*", "[编号]", text)

    party, item, transaction_type, status, method = map(redact, (party, item, transaction_type, status, method))
    body = {
        "model": "typesafe/jev-1.13",
        "state": {"merchant": party, "item": item, "transaction_type": transaction_type,
                  "direction": direction, "status": status, "source": source, "payment_method": method},
        "questions": {
            "kind": {"type": "choice", "instructions": "判断交易处理方式。结合类型、收支方向和状态；退款优先于收入，账户间资金移动属于 transfer。",
                     "criteria": KINDS},
            "category": {"type": "choice", "instructions": "假设这是一笔支出，按交易对方和商品说明选择最贴切类别；无法判断时选 Other。",
                         "criteria": CATEGORIES},
            "payment_account": {"type": "choice", "instructions": "从付款方式判断实际扣款账户。若同时出现银行卡和立减金等优惠，按银行卡判断；来源只用于选择相应平台钱包。不要根据商户猜测账户。",
                                "criteria": PAYMENT_ACCOUNTS},
        },
    }
    request = Request("https://openrouter.ai/api/alpha/decisions",
                      data=json.dumps(body, ensure_ascii=False).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            answers = json.load(response)["answers"]
    except HTTPError as error:
        raise RuntimeError(f"OpenRouter API 返回 HTTP {error.code}") from None
    kind, category, payment_account = answers["kind"], answers["category"], answers["payment_account"]
    if kind["choice"] not in KINDS or category["choice"] not in CATEGORIES or payment_account["choice"] not in PAYMENT_ACCOUNTS:
        raise ValueError("Jev 返回未知交易方式、类别或付款账户")
    confidence = lambda answer: float(answer.get("probabilities", {}).get(answer["choice"], answer.get("confidence", 0)))
    return (kind["choice"], confidence(kind), category["choice"], confidence(category),
            payment_account["choice"], confidence(payment_account))


def run(source, out, token, on_progress=None):
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("分类输入文件没有交易记录")
    cache = {}
    for index, row in enumerate(rows, 1):
        identity = (row["party"], row["item"], row["type"], row["direction"], row["status"], row["source"], row["method"])
        if identity not in cache:
            party, item, transaction_type, direction, status, source, method = identity
            cache[identity] = classify(party, item, token, transaction_type, direction, status, source, method)
        kind, kind_probability, category, category_probability, payment_account, account_probability = cache[identity]
        if is_huabei(row.get("method", "")):
            payment_account, account_probability = "huabei", 1.0
        row["kind"] = row["kind_suggestion"] = kind
        row["kind_probability"] = f"{kind_probability:.3f}"
        row["reason"] = "Jev 自动判断"
        row["category_suggestion"] = category if kind == "expense" else ""
        row["category_review_reason"] = ""
        row["category"] = category if kind == "expense" else ""
        row["category_probability"] = f"{category_probability:.3f}" if kind == "expense" else ""
        row["account_suggestion"] = payment_account
        row["account_probability"] = f"{account_probability:.3f}"
        if on_progress:
            on_progress(index, len(rows), row)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*rows[0].keys()])
        writer.writeheader()
        writer.writerows(rows)
    return {"expenses": sum(r["kind"] == "expense" for r in rows),
            "kind_counts": {kind: sum(r["kind"] == kind for r in rows) for kind in KINDS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.out, api_key(args.env)), ensure_ascii=False))


if __name__ == "__main__":
    main()
