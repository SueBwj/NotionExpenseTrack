"""Local web console for the bill processing workflow."""

import csv
from email import policy
from email.parser import BytesParser
import json
import mimetypes
import re
import secrets
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import bills
import classify_jev
import notion_import


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
WEB = ROOT / "flow_console"
LOCK = threading.Lock()  # ponytail: one local run mutation at a time; per-run locks if concurrent use matters
KINDS = {"expense", "income", "refund", "transfer", "exclude"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_name(name):
    name = Path(name).name
    return re.sub(r"[^\w.() -]", "_", name)[:120] or "bill.csv"


def run_dir(run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("无效的运行编号")
    matches = list(OUTPUT.glob(f"*/.console/{run_id}"))
    if len(matches) != 1:
        raise FileNotFoundError("找不到该运行记录")
    return matches[0]


def read_run(path):
    return json.loads((path / "run.json").read_text(encoding="utf-8"))


def save_run(path, data):
    data["updated_at"] = now()
    temp = path / "run.json.tmp"
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path / "run.json")


def save_rows(path, filename, rows):
    with (path / filename).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else list(bills.OUTPUT))
        writer.writeheader()
        writer.writerows(rows)


def load_rows(path, filename):
    with (path / filename).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def records():
    found = []
    for file in OUTPUT.glob("*/.console/*/run.json"):
        try:
            found.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda r: r.get("created_at", ""), reverse=True)


def public_run(data):
    return {k: v for k, v in data.items() if k not in ("files", "csrf")}


class Handler(BaseHTTPRequestHandler):
    server_version = "NotionExpenseTrack/1.0"

    def log_message(self, *_):
        pass

    def _json(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, code=400):
        self._json(code, {"error": str(message)})

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 100 * 1024 * 1024:
            raise ValueError("单次上传不能超过 100 MB")
        return json.loads(self.rfile.read(size) or b"{}")

    def _multipart(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 100 * 1024 * 1024:
            raise ValueError("单次上传不能超过 100 MB")
        raw = self.rfile.read(size)
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
        fields = {}
        uploads = []
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                fields.setdefault(name, []).append((filename, payload))
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8")
        return fields

    def _check_origin(self):
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise PermissionError("控制台只接受本机访问")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
            raise PermissionError("请求来源不匹配")

    def _mutating(self):
        self._check_origin()
        cookie = self.headers.get("Cookie", "")
        token = next((p.split("=", 1)[1] for p in cookie.split("; ") if p.startswith("flow_csrf=")), "")
        if not token or not secrets.compare_digest(token, self.headers.get("X-Flow-CSRF", "")):
            raise PermissionError("页面已过期，请刷新后重试")

    def do_GET(self):
        try:
            self._check_origin()
            parsed = urlparse(self.path)
            if parsed.path == "/api/session":
                return self._json(200, {"csrf": secrets.token_urlsafe(32)})
            if parsed.path == "/api/runs":
                return self._json(200, [public_run(r) for r in records()])
            if parsed.path.startswith("/api/runs/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "rows":
                    folder = run_dir(parts[2])
                    requested = parse_qs(parsed.query).get("file", [""])[0]
                    filename = requested if requested in ("normalized.csv", "classified.csv") else "normalized.csv"
                    if filename == "classified.csv" and not (folder / filename).exists():
                        raise FileNotFoundError("分类结果尚未生成")
                    return self._json(200, {"filename": filename, "rows": load_rows(folder, filename)})
                data = read_run(run_dir(parts[2] if len(parts) == 3 else ""))
                return self._json(200, public_run(data))
            name = "index.html" if parsed.path == "/" else unquote(parsed.path.lstrip("/"))
            file = (WEB / name).resolve()
            if WEB.resolve() not in file.parents or not file.is_file():
                return self._error("未找到", 404)
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._error(e, 403 if isinstance(e, PermissionError) else 400)

    def do_POST(self):
        try:
            self._mutating()
            with LOCK:
                self._route_post(urlparse(self.path).path)
        except Exception as e:
            self._error(e, 403 if isinstance(e, PermissionError) else 400)

    def _route_post(self, path):
        if path == "/api/runs":
            return self._new_run()
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "api" or parts[1] != "runs":
            return self._error("未找到", 404)
        folder = run_dir(parts[2])
        data = read_run(folder)
        action = parts[3]
        if action == "review" and len(parts) == 4:
            rows = load_rows(folder, "normalized.csv")
            edits = self._body().get("edits", {})
            for key, edit in edits.items():
                try:
                    row = rows[int(key.removeprefix("row-"))]
                except (ValueError, IndexError):
                    row = None
                if row is None or row["kind"] != "review":
                    raise ValueError("审核记录已变化，请刷新页面")
                kind = edit.get("kind")
                if kind not in KINDS:
                    raise ValueError("无效的交易处理方式")
                category = edit.get("category", "")
                if kind == "expense" and category not in classify_jev.CATEGORIES:
                    raise ValueError("请为支出选择有效类别")
                if kind != "expense" and category:
                    raise ValueError("只有支出可以设置类别")
                data["edits"].append({"key": row["key"], "field": "kind", "from": row["kind"], "to": kind, "at": now()})
                row["kind"], row["reason"] = kind, "网页人工确认"
                if kind == "expense":
                    row["category"] = category
            unresolved = [r for i, r in enumerate(rows) if r["kind"] == "review"]
            save_rows(folder, "normalized.csv", rows)
            save_rows(folder, "review.csv", unresolved or [dict.fromkeys(rows[0], "")])
            data["stages"]["review"] = {"status": "success", "count": 0}
            data["stage"] = "classification"
            save_run(folder, data)
            return self._json(200, public_run(data))
        if action == "classify":
            try:
                progress = {"done": 0, "total": 0, "kind_counts": {}, "latest": []}
                data["stages"]["classification"] = {"status": "running", "progress": progress}
                data["stage"] = "classification"
                save_run(folder, data)

                def update_progress(done, total, row):
                    progress["done"], progress["total"] = done, total
                    kind = row["kind"]
                    progress["kind_counts"][kind] = progress["kind_counts"].get(kind, 0) + 1
                    progress["latest"] = [*progress["latest"], {
                        "party": row["party"], "item": row["item"], "kind": kind,
                        "category": row["category"], "kind_probability": row["kind_probability"],
                        "category_probability": row["category_probability"],
                        "account": row["account_suggestion"], "account_probability": row["account_probability"]}][-15:]
                    save_run(folder, data)

                summary = classify_jev.run(folder / "normalized.csv", folder / "classified.csv",
                                          classify_jev.api_key(ROOT / ".env"), update_progress)
                rows = load_rows(folder, "classified.csv")
                data["stages"]["classification"] = {"status": "success", "count": len(rows), "kind_counts": summary["kind_counts"]}
                data["stages"]["review"] = {"status": "success", "count": 0}
                data["stages"]["category_review"] = {"status": "success", "count": 0}
                data["stage"] = "preview"
                save_run(folder, data)
                return self._json(200, public_run(data))
            except Exception as e:
                data["stages"]["classification"] = {"status": "error", "error": str(e), "progress": progress}
                save_run(folder, data)
                raise
        if action == "normalize":
            try:
                summary = bills.run([(f["source"], ROOT / f["path"]) for f in data["files"]], folder, data["month"])
                rows = load_rows(folder, "normalized.csv")
                data["stages"]["normalization"] = {"status": "success", "count": len(rows), "summary": summary}
                data["stages"]["review"] = {"status": "success", "count": 0}
                data["stage"] = "classification"
                save_run(folder, data)
                return self._json(200, public_run(data))
            except Exception as e:
                data["stages"]["normalization"] = {"status": "error", "error": str(e)}
                save_run(folder, data)
                raise
        if action == "categories":
            rows = load_rows(folder, "classified.csv")
            edits = self._body().get("edits", {})
            valid = {*classify_jev.CATEGORIES, "Review"}
            for key, edit in edits.items():
                try:
                    row = rows[int(key.removeprefix("row-"))]
                except (ValueError, IndexError):
                    row = None
                category = edit.get("category")
                if row is None or row["category"] != "Review" or category not in valid - {"Review"}:
                    raise ValueError("类别无效或记录已变化")
                data["edits"].append({"key": row["key"], "field": "category", "from": row["category"], "to": category, "at": now()})
                row["category"] = category
            pending = sum(r["category"] == "Review" for r in rows)
            save_rows(folder, "classified.csv", rows)
            data["stages"]["category_review"] = {"status": "blocked" if pending else "success", "count": pending}
            data["stage"] = "category_review" if pending else "preview"
            save_run(folder, data)
            return self._json(200, public_run(data))
        if action == "preview":
            rows = load_rows(folder, "classified.csv")
            if any(r["kind"] == "review" or (r["kind"] == "expense" and r["category"] == "Review") for r in rows):
                raise ValueError("仍有待审核记录")
            try:
                progress = {"phase": "准备连接 Notion", "phase_index": 0, "phase_count": 5,
                            "detail": "准备只读查询…", "done": 0, "total": 0, "actions": {}, "latest": ""}
                data["stages"]["preview"] = {"status": "running", "progress": progress}
                save_run(folder, data)

                def update_preview(progress_update):
                    progress.update(progress_update)
                    save_run(folder, data)

                result = notion_import.run(
                    folder / "classified.csv",
                    notion_import.Notion(notion_import.token_from_env(ROOT / ".env")),
                    False, update_preview,
                    settings=notion_import.settings_from_env(ROOT / ".env"),
                )
                data["stages"]["preview"] = {"status": "success", "result": result, "progress": progress}
                data["stage"] = "import"
                save_run(folder, data)
                return self._json(200, public_run(data))
            except Exception as e:
                data["stages"]["preview"] = {"status": "error", "error": str(e), "progress": progress}
                save_run(folder, data)
                raise
        if action == "import":
            if data.get("stage") != "import" or data["stages"].get("preview", {}).get("status") != "success":
                raise ValueError("请先完成导入预览")
            try:
                result = notion_import.run(folder / "classified.csv", notion_import.Notion(notion_import.token_from_env(ROOT / ".env")), True,
                                  settings=notion_import.settings_from_env(ROOT / ".env"))
                data["stages"]["import"] = {"status": "success", "result": result}
                data["stage"] = "complete"
            except Exception as e:
                data["stages"]["import"] = {"status": "error", "error": str(e)}
                save_run(folder, data)
                raise
            save_run(folder, data)
            return self._json(200, public_run(data))
        if action == "rows":
            filename = "classified.csv" if (folder / "classified.csv").exists() else "normalized.csv"
            rows = load_rows(folder, filename)
            return self._json(200, {"filename": filename, "rows": rows})
        return self._error("未找到", 404)

    def _new_run(self):
        form = self._multipart()
        month = form.get("month", "")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("月份格式须为 YYYY-MM")
        uploads = []
        for source in ("wechat", "alipay"):
            for upload_name, payload in form.get(source, []):
                if upload_name and payload:
                    filename = safe_name(upload_name)
                    if Path(filename).suffix.lower() not in (".csv", ".xlsx"):
                        raise ValueError("仅支持 CSV 或 XLSX 账单")
                    uploads.append((source, filename, payload))
        if not uploads:
            raise ValueError("请至少上传一份微信或支付宝账单")
        rid = uuid.uuid4().hex
        folder = OUTPUT / month / ".console" / rid
        (folder / "uploads").mkdir(parents=True)
        file_entries = []
        for source, filename, payload in uploads:
            path = folder / "uploads" / f"{source}-{len(file_entries) + 1}-{filename}"
            path.write_bytes(payload)
            file_entries.append({"source": source, "path": str(path.relative_to(ROOT)), "name": filename})
        data = {"id": rid, "month": month, "platforms": sorted({f["source"] for f in file_entries}),
                "created_at": now(), "updated_at": now(), "stage": "normalization", "files": file_entries,
                "edits": [], "stages": {"upload": {"status": "success", "count": len(file_entries)},
                "normalization": {"status": "pending"}, "review": {"status": "pending"},
                "classification": {"status": "pending"}, "category_review": {"status": "pending"},
                "preview": {"status": "pending"}, "import": {"status": "pending"}}}
        save_run(folder, data)
        try:
            result = bills.run([(f["source"], ROOT / f["path"]) for f in file_entries], folder, month)
            rows = load_rows(folder, "normalized.csv")
            data["stages"]["normalization"] = {"status": "success", "count": len(rows), "summary": result}
            data["stages"]["review"] = {"status": "success", "count": 0}
            data["stage"] = "classification"
            save_run(folder, data)
            return self._json(200, public_run(data))
        except Exception as e:
            data["stages"]["normalization"] = {"status": "error", "error": str(e)}
            data["stage"] = "normalization"
            save_run(folder, data)
            raise


def main():
    OUTPUT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("账单流程控制台: http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
