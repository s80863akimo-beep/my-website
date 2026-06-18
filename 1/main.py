# main.py  —  摸摸頭 momohair × Google Calendar 同步後端
# 依賴：fastapi uvicorn google-api-python-client python-dotenv
# 啟動：uvicorn main:app --reload --port 8000
# 前提：Google Calendar 行事曆必須設為「公開」才能用 API Key 讀取

import re
import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("momohair.sync")

# ─────────────────────────────────────────────
# 官方精準價目表（key 長度由長到短，避免短字截斷長字）
# ─────────────────────────────────────────────
PRICE_TABLE: dict[str, int] = {
    # 紓壓調理
    "頭皮初淨調理":       2000,
    "頭皮健康髮浴":         600,
    # 精緻剪髮
    "頭皮初淨調理+剪髮":  2200,
    "健康髮浴+剪":         1100,
    "孩童洗+剪":            550,
    "洗+剪":                800,
    "孩童剪髮":             400,
    "剪髮":                 600,
    # 單色染髮
    "特長染髮":            3100,
    "長髮染髮":            2800,
    "中長染髮":            2500,
    "短髮染髮":            2200,
    "男生短髮":            1400,
    # 特殊色
    "特殊色":              5000,
    # 髮絲保養
    "結構式護髮":          1800,
    "髮絲水潤修護":        1200,
}

SORTED_KEYS = sorted(PRICE_TABLE.keys(), key=len, reverse=True)

PAYMENT_ALIASES = {
    "現金": "現金",
    "cash": "現金",
    "儲值扣款": "儲值扣款",
    "儲值付款": "儲值扣款",
    "儲值金扣款": "儲值扣款",
    "prepaid": "儲值扣款",
    "儲值進帳": "儲值進帳",
    "儲值入帳": "儲值進帳",
    "儲值": "儲值進帳",
    "topup": "儲值進帳",
    "top up": "儲值進帳",
}

TW = ZoneInfo("Asia/Taipei")

# ─────────────────────────────────────────────
# 服務大分類對應表
# Service: 欄位值 → 統計用大分類
# ─────────────────────────────────────────────
CATEGORY_MAP = {
    "剪髮": "剪髮",
    "洗髮": "洗護其他",
    "燙髮": "燙髮",
    "染髮": "染髮",
    "護髮": "洗護其他",
    "頭皮": "洗護其他",
    "產品": "洗護其他",
}

def classify_service(service_text: str) -> str:
    """
    根據 Service 欄位的大分類名稱（如「燙髮」、「剪髮」、「染髮」）
    對應到統計用的四大類別：剪髮 / 燙髮 / 染髮 / 洗護其他
    """
    for key, cat in CATEGORY_MAP.items():
        if key in service_text:
            return cat
    return "洗護其他"

app = FastAPI(
    title="摸摸頭 momohair 同步 API",
    version="2.0.0",
    docs_url="/docs" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────

def parse_order_id(description: str) -> str | None:
    """從行事曆 description 抓出 Order ID。"""
    m = re.search(r"Order\s*ID\s*[：:]\s*([A-Z0-9_-]+)", description, re.IGNORECASE)
    return m.group(1) if m else None


def parse_service_text(description: str) -> str:
    """擷取 Service: 後方、下一個換行前的完整文字。"""
    m = re.search(r"(?:Service|服務項目|服務)\s*[：:]\s*([^\r\n]+)", description, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_labeled_value(description: str, labels: list[str]) -> str | None:
    """讀取 description 中指定標籤後方、同一行的值。"""
    pattern = rf"(?:{'|'.join(re.escape(label) for label in labels)})\s*[：:]\s*([^\r\n]+)"
    match = re.search(pattern, description, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_payment_method(description: str, service_text: str) -> str:
    """解析付款方式；未標示時才使用現金，避免儲值金流被誤分類。"""
    raw = parse_labeled_value(
        description,
        ["Payment Method", "Payment", "付款方式", "付款", "金流"],
    )
    candidate = (raw or "").strip().lower()
    for alias, normalized in PAYMENT_ALIASES.items():
        if alias.lower() in candidate:
            return normalized

    combined = f"{description}\n{service_text}".lower()
    if "儲值扣款" in combined or "儲值付款" in combined:
        return "儲值扣款"
    if "儲值進帳" in combined or "儲值入帳" in combined or "儲值專用" in combined:
        return "儲值進帳"
    return "現金"


def parse_explicit_amount(description: str) -> int | None:
    """解析行事曆明確填寫的金額，支援千分位與 NT$。"""
    raw = parse_labeled_value(description, ["Amount", "Price", "金額", "價格", "收款"])
    if not raw:
        return None
    match = re.search(r"(?:NT\$|TWD|\$)?\s*([0-9][0-9,]*)", raw, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_customer_name(summary: str) -> str:
    """清理事件標題中的 MOMO、括號備註與前後分隔符。"""
    if not summary:
        return "預約顧客"
    name = re.sub(r"momo", " ", summary, flags=re.IGNORECASE)
    name = re.sub(r"[\(（].*?[\)）]", " ", name)
    name = re.sub(r"^[\s\-—_|:：]+|[\s\-—_|:：]+$", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name or "預約顧客"


def calc_amount(service_text: str) -> int:
    """對 service_text 用 SORTED_KEYS 做貪婪比對，累加並回傳總金額。"""
    # 移除中括號及圓括號內容，避免大分類標籤與備註干擾比對
    cleaned = service_text
    cleaned = re.sub(r"\[.*?\]", " ", cleaned)
    cleaned = re.sub(r"（.*?）", " ", cleaned)
    cleaned = re.sub(r"\(.*?\)", " ", cleaned)
    
    remaining = cleaned
    total = 0
    for key in SORTED_KEYS:
        if key in remaining:
            total += PRICE_TABLE[key]
            remaining = remaining.replace(key, "", 1)
    return total


def get_event_date(event: dict) -> str | None:
    """
    取得事件的台灣本地日期字串（YYYY-MM-DD）。
    支援全天事件（date）與時間事件（dateTime）。
    """
    start = event.get("start", {})
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        dt_tw = dt.astimezone(TW)
        return dt_tw.strftime("%Y-%m-%d")
    elif "date" in start:
        return start["date"]
    return None


def get_calendar_service():
    """建立 Google Calendar API 服務（API Key，行事曆需設為公開）。"""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 GOOGLE_API_KEY 環境變數")
    return build(
        "calendar",
        "v3",
        developerKey=api_key,
        cache_discovery=False,
    )


def get_calendar_id() -> str:
    calendar_id = os.getenv("CALENDAR_ID")
    if not calendar_id:
        raise RuntimeError("缺少 CALENDAR_ID 環境變數")
    return calendar_id


def fetch_calendar_events(service, calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    """逐頁抓取指定時間範圍的所有 Calendar 事件。"""
    events: list[dict] = []
    page_token: str | None = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return events


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.get("/api/sync")
@app.post("/api/sync")
def sync_calendar(
    year: int | None = Query(default=None, ge=2020, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
):
    """
    拉取指定年月（預設本月）的 Google Calendar 事件，
    解析每筆 Order，回傳：

    1. 月度總業績與訂單列表
    2. 本月每日業績明細（按日期分類）
    3. 今日業績與各服務大分類金額

    回傳格式：
    {
        "year": 2026,
        "month": 6,
        "monthly_revenue": 36800,
        "order_count": 12,
        "orders": [...],

        // 本月每日業績（日期分類）
        "daily_revenue": [
            {
                "date": "2026-06-01",
                "date_label": "6/1 (一)",
                "total": 4500,
                "order_count": 2,
                "categories": {
                    "剪髮": 600,
                    "燙髮": 3900,
                    "染髮": 0,
                    "洗護其他": 0
                }
            },
            ...
        ],

        // 今日業績
        "today": {
            "date": "2026-06-06",
            "total": 7800,
            "order_count": 3,
            "categories": {
                "剪髮": 800,
                "燙髮": 4500,
                "染髮": 2500,
                "洗護其他": 0
            }
        }
    }
    """
    now = datetime.now(tz=TW)
    y = year  or now.year
    m = month or now.month
    today_str = now.strftime("%Y-%m-%d")

    # 計算同步範圍：包含指定月份與前一個月（共兩個月，利於跨月對帳）
    if m == 1:
        prev_y = y - 1
        prev_m = 12
    else:
        prev_y = y
        prev_m = m - 1

    time_min = datetime(prev_y, prev_m, 1, tzinfo=TW).astimezone(timezone.utc).isoformat()
    if m == 12:
        time_max = datetime(y + 1, 1, 1, tzinfo=TW).astimezone(timezone.utc).isoformat()
    else:
        time_max = datetime(y, m + 1, 1, tzinfo=TW).astimezone(timezone.utc).isoformat()

    try:
        service = get_calendar_service()
        events = fetch_calendar_events(service, get_calendar_id(), time_min, time_max)
    except RuntimeError as exc:
        logger.error("Calendar configuration error: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HttpError as exc:
        logger.exception("Google Calendar API error")
        raise HTTPException(status_code=502, detail="Google Calendar API 回應失敗，請檢查金鑰與行事曆權限") from exc
    except Exception as exc:
        logger.exception("Unexpected Google Calendar sync error")
        raise HTTPException(status_code=502, detail="Google Calendar 連線失敗，請稍後再試") from exc

    seen_orders: set[str] = set()
    orders: list[dict] = []
    selected_month_revenue = 0
    selected_month_cash_in = 0
    range_revenue = 0
    range_cash_in = 0
    diagnostics = {
        "events_received": len(events),
        "orders_parsed": 0,
        "missing_description": 0,
        "missing_order_id": 0,
        "duplicates": 0,
        "cancelled": 0,
        "missing_date": 0,
        "zero_amount": 0,
    }

    # 每日統計用的資料結構
    # daily_data[date_str] = { total, order_count, categories: {剪髮, 燙髮, 染髮, 洗護其他} }
    daily_data: dict[str, dict] = defaultdict(lambda: {
        "total": 0,
        "order_count": 0,
        "categories": {"剪髮": 0, "燙髮": 0, "染髮": 0, "洗護其他": 0}
    })

    WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

    payment_summary = {"現金": 0, "儲值扣款": 0, "儲值進帳": 0}
    selected_month_prefix = f"{y:04d}-{m:02d}"

    for event in events:
        if event.get("status") == "cancelled":
            diagnostics["cancelled"] += 1
            continue
        desc = event.get("description", "") or ""
        if not desc:
            diagnostics["missing_description"] += 1
            continue

        order_id = parse_order_id(desc)
        if not order_id:
            diagnostics["missing_order_id"] += 1
            continue
        if order_id in seen_orders:
            diagnostics["duplicates"] += 1
            continue
        seen_orders.add(order_id)

        service_text = parse_service_text(desc)
        explicit_amount = parse_explicit_amount(desc)
        amount = explicit_amount if explicit_amount is not None else calc_amount(service_text)
        payment_method = parse_payment_method(desc, service_text)
        category = classify_service(service_text)
        event_date = get_event_date(event)
        if not event_date:
            diagnostics["missing_date"] += 1
            continue
        if amount <= 0:
            diagnostics["zero_amount"] += 1

        # 從事件標題（summary）提取姓名並清理
        summary = event.get("summary", "") or ""
        customer_name = parse_customer_name(summary)

        if payment_method != "儲值進帳":
            range_revenue += amount
        if payment_method != "儲值扣款":
            range_cash_in += amount
        is_selected_month = event_date.startswith(selected_month_prefix)
        if is_selected_month:
            payment_summary[payment_method] += amount
            if payment_method != "儲值進帳":
                selected_month_revenue += amount
            if payment_method != "儲值扣款":
                selected_month_cash_in += amount

        orders.append({
            "order_id":      order_id,
            "customer_name": customer_name,
            "customerName":  customer_name,  # 前後端相容
            "service":       service_text,
            "serviceName":   service_text,
            "category":      category,
            "amount":        amount,
            "date":          event_date,
            "paymentMethod": payment_method,
        })
        diagnostics["orders_parsed"] += 1

        if event_date and payment_method != "儲值進帳":
            daily_data[event_date]["total"] += amount
            daily_data[event_date]["order_count"] += 1
            daily_data[event_date]["categories"][category] += amount

    # 整理同步範圍每日業績，再切出指定月份資料。
    range_daily_revenue = []
    for date_str in sorted(daily_data.keys()):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = WEEKDAY_ZH[dt.weekday()]
            date_label = f"{dt.month}/{dt.day} ({weekday})"
        except Exception:
            date_label = date_str

        day = daily_data[date_str]
        range_daily_revenue.append({
            "date":        date_str,
            "date_label":  date_label,
            "total":       day["total"],
            "order_count": day["order_count"],
            "categories":  day["categories"],
        })

    daily_revenue = [
        day for day in range_daily_revenue
        if day["date"].startswith(selected_month_prefix)
    ]

    # 整理今日業績
    today_day = daily_data.get(today_str, {
        "total": 0,
        "order_count": 0,
        "categories": {"剪髮": 0, "燙髮": 0, "染髮": 0, "洗護其他": 0}
    })
    today_result = {
        "date":        today_str,
        "total":       today_day["total"],
        "order_count": today_day["order_count"],
        "categories":  today_day["categories"],
    }

    return {
        "year":            y,
        "month":           m,
        "range": {
            "from": f"{prev_y:04d}-{prev_m:02d}-01",
            "to_exclusive": f"{y + 1:04d}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01",
        },
        "monthly_revenue": selected_month_revenue,
        "monthly_cash_in": selected_month_cash_in,
        "range_revenue":   range_revenue,
        "range_cash_in":   range_cash_in,
        "order_count":     sum(
            1 for order in orders
            if order["date"].startswith(selected_month_prefix)
            and order["paymentMethod"] != "儲值進帳"
        ),
        "range_order_count": len(orders),
        "orders":          orders,
        "daily_revenue":   daily_revenue,
        "range_daily_revenue": range_daily_revenue,
        "payment_summary": payment_summary,
        "today":           today_result,
        "diagnostics":     diagnostics,
    }


@app.get("/health")
def health_check():
    """Render 與人工檢查使用，不呼叫 Google API。"""
    return {
        "status": "ok",
        "service": "momohair-calendar-sync",
        "timezone": str(TW),
        "configured": bool(os.getenv("GOOGLE_API_KEY") and os.getenv("CALENDAR_ID")),
        "time": datetime.now(tz=TW).isoformat(),
    }
