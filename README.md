# NotionExpenseTrack

[English](README.en.md) | 简体中文

把微信支付和支付宝账单整理成可审核的交易记录，再按需导入自己的 Notion 财务工作区。可从 [Finance Tracker 模板（Vince Lin）](https://www.notion.com/templates/finance-credit-budget-tracker) 开始，并在自己的模板副本中配置数据源。项目提供命令行流程和本机网页控制台；网页只监听本机地址，账单与运行记录保存在本地。

## 功能

- 读取微信支付、支付宝导出的 CSV 和 XLSX 账单，统一字段并按来源键去重。
- 使用 OpenRouter 上的 Jev 对交易类型、支出类别和付款账户提出判断，保留待复核项。
- 先预览 Notion 导入结果，再由你确认写入；重复运行会检查已有来源键。
- 本机网页控制台支持上传账单、复核、查看分类进度和运行记录。

## 快速开始

需要 Python 3.10 或更新版本。处理 XLSX 时需安装 `openpyxl`；CSV 处理使用 Python 标准库。

```sh
git clone https://github.com/SueBwj/NotionExpenseTrack.git
cd NotionExpenseTrack
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

按下方说明填写 `.env`。只处理账单、暂不调用 AI 或 Notion 时，可跳过密钥配置。

## 使用方式

### 命令行

准备规范化账单和待复核列表：

```sh
python bills.py --wechat path/to/wechat.csv --month 2026-09 --out output/2026-09
# 也可添加 --alipay path/to/alipay.xlsx，或重复指定同一平台参数
```

让 Jev 分类并生成 `classified.csv`：

```sh
python classify_jev.py output/2026-09/normalized.csv output/2026-09/classified.csv
```

导入前先预览。检查交易数量、金额小计、付款账户和待复核项后，才使用 `--apply`：

```sh
python notion_import.py output/2026-09/classified.csv
python notion_import.py output/2026-09/classified.csv --apply
```

### 网页控制台

```sh
python flow_console.py
```

打开 <http://127.0.0.1:8765>。上传账单后，页面会展示处理和复核步骤；Notion 写入仍需单独确认。按 `Ctrl+C` 停止服务。

## 配置 Notion 与 OpenRouter

将 `.env.example` 复制为 `.env`，填写：

- `OPENROUTER_API_KEY`：用于 Jev 交易判断。
- `NOTION_API_KEY`：Notion internal integration secret。
- 六个 `NOTION_*_DATA_SOURCE_ID`：支出、收入/退款、类别、账户、订阅和转账数据源 ID。
- `NOTION_WECHAT_ACCOUNT_ID`、`NOTION_ALIPAY_ACCOUNT_ID`、`NOTION_BANK_ACCOUNT_ID`：对应付款账户页面 ID；银行卡账户 ID 可按你的银行账户设置。

在 Notion 中把需要访问的数据源共享给 integration，并授予读取和写入权限。Finance Tracker 模板的不同副本可能有不同的数据库 ID；从模板复制到自己的工作区后，填入自己副本的数据源与账户页面 ID。数据源需提供名称、日期、金额、账户关联、类别关联和来源键等导入器使用的属性。若自定义过模板字段，请核对 `notion_import.py` 中的属性名称是否与工作区一致。

## 隐私与数据安全

`.env`、`data/`、`output/` 和 `assets/` 已加入 Git 忽略规则。不要提交密钥、原始账单、导入结果或截图。调用 Jev 时会发送交易对方、商品说明、交易类型、收支方向、状态、平台和付款方式；程序会清除长编号、邮箱和手机号，不发送金额、交易单号或备注。OpenRouter 请求会产生相应用量费用。

## 输入与输出

微信、支付宝账单应使用平台导出的原始 CSV/XLSX 文件。原始导出不应手工改写。运行输出包括 `normalized.csv`、`review.csv` 和 `summary.json`；Jev 运行后另有 `classified.csv`。所有金额以人民币元保存，金额为正数，收支方向单独记录。

## License

本项目使用 MIT License。Notion 模板由其创作者按 Notion Marketplace 条款提供；本仓库不包含该模板内容。
