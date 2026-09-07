#!/usr/bin/env python3
"""
TDnet の日次開示一覧から「月次」開示(月次売上高・既存店売上高等)を見つけ、
PDF中の表を解析して前年同月比(または増減率)が閾値を超える項目を Discord に通知する。

データソース: https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html
  (東京証券取引所 適時開示情報閲覧サービス。個別開示PDFへの直リンクを含む公開ページ)

前年同月比の解釈:
  - 「前年同月比」「対前年」「比」を含む項目 → 100 を基準とした比率(例: 130.5% → +30.5pt)
  - 「増減率」「伸び率」「成長率」を含む項目 → 0 を基準とした増減率そのもの
  - 判別できない場合は比率(100基準)として扱う(月次開示で最も一般的な形式のため)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

JST = ZoneInfo("Asia/Tokyo")
TDNET_BASE = "https://www.release.tdnet.info/inbs/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
THRESHOLD = 30.0  # 前年同月比 |value-100| >= 30 (または増減率 |value| >= 30) で通知

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "notified.json"

ROW_RE = re.compile(
    r'kjCode"\s*noWrap>(?P<code>\d+)</td>\s*'
    r'<td class="[^"]*kjName"\s*noWrap>(?P<name>[^<]*)</td>\s*'
    r'<td class="[^"]*kjTitle"[^>]*><a href="(?P<href>[^"]+)"[^>]*>(?P<title>[^<]*)</a>'
)
MONTH_HEADER_RE = re.compile(r"^(\d{1,2})月$")
PERCENT_CELL_RE = re.compile(r"^(?P<sign>[△▲+\-]?)(?P<num>\d{1,3}(?:\.\d+)?)%$")

RATIO_KEYWORDS = ("前年同月比", "対前年", "前年比", "比")
RATE_KEYWORDS = ("増減率", "伸び率", "成長率", "増加率", "減少率")


@dataclass
class Hit:
    code: str
    name: str
    title: str
    item: str
    raw_value: str
    delta: float
    pdf_url: str


@dataclass
class ProcessResult:
    code: str
    name: str
    title: str
    pdf_url: str
    doc_id: str
    hits: list = field(default_factory=list)
    error: str | None = None


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def fetch_tdnet_list(date: datetime.date) -> str:
    url = f"{TDNET_BASE}I_list_001_{date:%Y%m%d}.html"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


TITLE_KEYWORDS = ("月次", "速報")


def parse_monthly_disclosures(html: str):
    for m in ROW_RE.finditer(html):
        title = nfkc(m.group("title"))
        if not any(k in title for k in TITLE_KEYWORDS):
            continue
        yield {
            "code": nfkc(m.group("code")),
            "name": nfkc(m.group("name")),
            "title": title,
            "href": m.group("href").strip(),
            "pdf_url": TDNET_BASE + m.group("href").strip(),
            "doc_id": m.group("href").strip(),
        }


def classify_label(label: str) -> str:
    if any(k in label for k in RATE_KEYWORDS):
        return "rate"
    if any(k in label for k in RATIO_KEYWORDS):
        return "ratio"
    return "ratio"  # default: 月次開示で最も一般的な「前年同月比(100基準)」


def signed_delta(sign: str, num: float, label_context: str) -> float:
    """符号(+/-/△/▲)が明示されていれば増減率(0基準)、符号なしならラベルの
    キーワードで比率(100基準)/増減率を判定する(比率表記は常に符号なしの
    正数で書かれるため、符号の有無は強いシグナルになる)。"""
    if sign in ("-", "△", "▲"):
        return -num
    if sign == "+":
        return num
    style = classify_label(label_context)
    return (num - 100.0) if style == "ratio" else num


def parse_percent_cell(raw: str):
    raw_n = nfkc(raw)
    m = PERCENT_CELL_RE.match(raw_n)
    if not m:
        return None
    return m.group("sign"), float(m.group("num"))


def extract_hits_from_tables(pdf_path: Path):
    hits = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                hits.extend(_extract_hits_from_one_table(table))
    return hits


def _extract_hits_from_one_table(table):
    hits = []
    header_row_idx = None
    month_cols = []  # list of (col_idx, month_int) contiguous in header row
    for r_idx, row in enumerate(table):
        cols = []
        for c_idx, cell in enumerate(row):
            if cell is None:
                continue
            text = nfkc(str(cell))
            m = MONTH_HEADER_RE.match(text)
            if m:
                cols.append((c_idx, int(m.group(1))))
        if len(cols) >= 2:
            header_row_idx = r_idx
            month_cols = cols
            break
    if header_row_idx is None or not month_cols:
        return hits

    target_col = month_cols[-1][0]  # 直近月(表内で一番右の月列)
    first_month_col = month_cols[0][0]

    # ラベル列(月列より左)を上から下へフォワードフィルして項目名を作る
    label_col_count = first_month_col
    carried = [""] * label_col_count
    for row in table[header_row_idx + 1 :]:
        for c in range(min(label_col_count, len(row))):
            cell = row[c]
            if cell:
                carried[c] = nfkc(str(cell)).replace("\n", " ")
        label = " ".join(x for x in carried if x)
        if not label:
            continue
        if target_col >= len(row) or row[target_col] is None:
            continue
        parsed = parse_percent_cell(str(row[target_col]))
        if parsed is None:
            continue
        sign, num = parsed
        delta = signed_delta(sign, num, label)
        if abs(delta) >= THRESHOLD:
            hits.append(
                {
                    "item": label,
                    "raw_value": nfkc(str(row[target_col])),
                    "delta": delta,
                }
            )
    return hits


NUM_TOKEN = r"[\d,]+(?:\.\d+)?"
PCT_TOKEN = r"[△▲+\-]?\d{1,3}(?:\.\d+)?"

# 罫線なしPDF向け: 月ヘッダー行(「1月 2月 3月...」)と、その直後に現れる
# 「対前年同月比 110.1% 120.7% ...」のような値行をペアリングする。
MONTH_LINE_RE = re.compile(r"^(\d{1,2}月(?:\s+\d{1,2}月)+)$")
RATIO_VALUE_LINE_RE = re.compile(
    r"^(?P<label>[^\d\n]{1,20}?)\s+(?P<values>(?:" + PCT_TOKEN + r"%\s*)+)$"
)

# 罫線なしPDF向け: 「実績 前年 前年比」形式の単月比較行
# 例: 「売上高 278 266 104.8% 2,970 2,971 99.9%」
RATIO_ROW_INLINE_RE = re.compile(
    r"^(?P<label>[^\d\n%]{1,20}?)\s+"
    + NUM_TOKEN
    + r"\s+"
    + NUM_TOKEN
    + r"\s+(?P<pct>"
    + PCT_TOKEN
    + r")%"
)
RATIO_ROW_NUMBERS_ONLY_RE = re.compile(
    r"^" + NUM_TOKEN + r"\s+" + NUM_TOKEN + r"\s+(?P<pct>" + PCT_TOKEN + r")%"
)
HEADER_HINT_RE = re.compile(r"実績|前年|当期|当月")


def _pct_to_delta(num_str: str, label_context: str) -> float:
    s = num_str
    sign = ""
    if s and s[0] in "△▲+-":
        sign, s = s[0], s[1:]
    return signed_delta(sign, float(s), label_context)


def extract_hits_from_month_line_pairs(pdf_path: Path):
    """罫線なし・月ヘッダー横並び形式(例: G-ユニネク型)"""
    hits = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            raw_lines = [nfkc(l) for l in text.splitlines()]
            lines = [l for l in raw_lines if l.strip()]
            month_positions = []
            for i, line in enumerate(lines):
                if MONTH_LINE_RE.match(line):
                    month_positions.append((i, line.split()))
            for i, months in month_positions:
                # ヘッダー行の後、次のヘッダー行までの間で値行を探す
                for j in range(i + 1, min(i + 6, len(lines))):
                    m = RATIO_VALUE_LINE_RE.match(lines[j])
                    if not m:
                        continue
                    label = m.group("label").strip()
                    if not label or "月" in label:
                        continue
                    values = m.group("values").split()
                    if len(values) > len(months):
                        continue
                    # 値は月ヘッダーの左詰めで対応(その月まで実績が出ている想定)
                    # 最新(=一番右)の値が直近月
                    idx = len(values) - 1
                    raw = values[idx]
                    month_label = months[idx]
                    delta = _pct_to_delta(raw.rstrip("%"), label)
                    if abs(delta) >= THRESHOLD:
                        hits.append(
                            {
                                "item": f"{label}({month_label})",
                                "raw_value": raw,
                                "delta": delta,
                            }
                        )
                    break
    return hits


def extract_hits_from_ratio_rows(pdf_path: Path):
    """罫線なし・「実績 前年 前年比」形式(例: 東邦レマック型)"""
    hits = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            raw_lines = [nfkc(l) for l in text.splitlines()]
            lines = [l for l in raw_lines if l.strip()]
            last_label_line = None
            for i, line in enumerate(lines):
                m = RATIO_ROW_INLINE_RE.match(line)
                if m:
                    label = m.group("label").strip()
                    delta = _pct_to_delta(m.group("pct"), "前年比")
                    if abs(delta) >= THRESHOLD:
                        hits.append(
                            {"item": label, "raw_value": m.group("pct") + "%", "delta": delta}
                        )
                    last_label_line = None
                    continue
                m2 = RATIO_ROW_NUMBERS_ONLY_RE.match(line)
                if m2 and last_label_line and not HEADER_HINT_RE.search(last_label_line):
                    delta = _pct_to_delta(m2.group("pct"), "前年比")
                    if abs(delta) >= THRESHOLD:
                        hits.append(
                            {
                                "item": last_label_line,
                                "raw_value": m2.group("pct") + "%",
                                "delta": delta,
                            }
                        )
                    continue
                if line and not HEADER_HINT_RE.search(line):
                    last_label_line = line
    return hits


STRICT_FALLBACK_RE = re.compile(
    r"(?:前年同月比|対前年同月比)\s*[:：]?\s*(?P<sign>[△▲+\-]?)(?P<num>\d{1,3}(?:\.\d+)?)\s*%"
    r"(?P<suffix>増|減)?"
)


def extract_hits_from_text_fallback(pdf_path: Path):
    """最終手段: 「前年同月比 XX.X%」に直接隣接する数値のみを拾う狭いフォールバック。
    「XX%増/減」という書き方は増減率(0基準)、それ以外は比率(100基準)として扱う。
    """
    hits = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line_n = nfkc(line)
                for m in STRICT_FALLBACK_RE.finditer(line_n):
                    num = float(m.group("num"))
                    sign = m.group("sign")
                    suffix = m.group("suffix")
                    if suffix == "増":
                        delta = num
                    elif suffix == "減":
                        delta = -num
                    else:
                        delta = _pct_to_delta((sign or "") + m.group("num"), "前年同月比")
                    if abs(delta) >= THRESHOLD:
                        hits.append(
                            {
                                "item": line_n.strip()[:40],
                                "raw_value": m.group(0),
                                "delta": delta,
                            }
                        )
    return hits


def process_disclosure(item: dict, tmpdir: Path) -> ProcessResult:
    result = ProcessResult(
        code=item["code"],
        name=item["name"],
        title=item["title"],
        pdf_url=item["pdf_url"],
        doc_id=item["doc_id"],
    )
    try:
        resp = requests.get(
            item["pdf_url"], headers={"User-Agent": USER_AGENT}, timeout=60
        )
        resp.raise_for_status()
        pdf_path = tmpdir / item["doc_id"]
        pdf_path.write_bytes(resp.content)

        raw_hits = extract_hits_from_tables(pdf_path)
        if not raw_hits:
            raw_hits = extract_hits_from_month_line_pairs(pdf_path)
        if not raw_hits:
            raw_hits = extract_hits_from_ratio_rows(pdf_path)
        if not raw_hits:
            raw_hits = extract_hits_from_text_fallback(pdf_path)

        # 同一項目・同一値の重複を除去
        seen = set()
        for h in raw_hits:
            key = (h["item"], h["raw_value"])
            if key in seen:
                continue
            seen.add(key)
            result.hits.append(
                Hit(
                    code=item["code"],
                    name=item["name"],
                    title=item["title"],
                    item=h["item"],
                    raw_value=h["raw_value"],
                    delta=h["delta"],
                    pdf_url=item["pdf_url"],
                )
            )
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"
    return result


def load_state() -> set:
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_state(doc_ids: set):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(sorted(doc_ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_discord(webhook_url: str, results: list[ProcessResult]):
    all_hits = [h for r in results for h in r.hits]
    if not all_hits:
        return
    lines = [f"**\U0001f4c8 月次データ前年同月比 {THRESHOLD:.0f}%超 検知 ({len(all_hits)}件)**"]
    by_company: dict[str, list[Hit]] = {}
    for h in all_hits:
        by_company.setdefault(f"{h.name}({h.code})", []).append(h)
    for company, hits in by_company.items():
        lines.append(f"\n**{company}**  {hits[0].title}")
        for h in hits:
            sign = "+" if h.delta >= 0 else ""
            lines.append(f"・{h.item}: {h.raw_value} ({sign}{h.delta:.1f}pt)")
        lines.append(f"<{hits[0].pdf_url}>")

    content = "\n".join(lines)
    # Discord の1メッセージ2000文字制限に合わせて分割
    chunks = []
    cur = ""
    for line in content.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        if resp.status_code >= 300:
            print(f"[WARN] Discord webhook failed: {resp.status_code} {resp.text}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        help="対象日 YYYY-MM-DD (省略時は JST の今日)",
        default=None,
    )
    parser.add_argument("--dry-run", action="store_true", help="Discord送信をスキップ")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.date.fromisoformat(args.date)
    else:
        target_date = datetime.datetime.now(JST).date()

    if pdfplumber is None:
        print("pdfplumber がインストールされていません", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Checking TDnet disclosures for {target_date}")
    html = fetch_tdnet_list(target_date)
    disclosures = list(parse_monthly_disclosures(html))
    print(f"[INFO] Found {len(disclosures)} monthly disclosure(s)")

    state = load_state()
    new_disclosures = [d for d in disclosures if d["doc_id"] not in state]
    print(f"[INFO] {len(new_disclosures)} not yet processed")

    tmpdir = Path("tmp_pdfs")
    tmpdir.mkdir(exist_ok=True)

    results = []
    for item in new_disclosures:
        print(f"[INFO] Processing {item['name']} - {item['title']}")
        res = process_disclosure(item, tmpdir)
        if res.error:
            print(f"[WARN] {item['name']}: {res.error}", file=sys.stderr)
        results.append(res)
        state.add(item["doc_id"])

    total_hits = sum(len(r.hits) for r in results)
    print(f"[INFO] Total hits: {total_hits}")

    if not args.dry_run:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if webhook_url and total_hits:
            send_discord(webhook_url, results)
        elif not webhook_url and total_hits:
            print("[WARN] DISCORD_WEBHOOK_URL not set, skipping notification", file=sys.stderr)

    save_state(state)


if __name__ == "__main__":
    main()
