"""
VICTOR 出差申請 — 企業微信插件中介 API
框架：FastAPI
部署：Render / Railway / Fly.io（免費方案即可）

安裝依賴：
    pip install fastapi uvicorn anthropic python-dotenv

啟動本地測試：
    uvicorn wecom_api_server:app --reload --port 8000

部署後把 https://你的域名 填入企業微信「插件URL」
工具路徑填 /parse
"""

import os
import json
from datetime import date
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── 初始化 ────────────────────────────────────────────────────────
app = FastAPI(title="VICTOR 出差申請解析 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── 膳雜費規則（依 VICTOR 管理辦法） ─────────────────────────────
MEAL_RULES = {
    "domestic": {
        "vp":    {"breakfast": 150, "lunch": 250, "dinner": 250, "daily": 650},
        "mgr":   {"breakfast": 100, "lunch": 200, "dinner": 200, "daily": 500},
        "other": {"breakfast": 100, "lunch": 150, "dinner": 150, "daily": 400},
    },
    "china": {
        "vp": "RMB 150/日", "mgr": "RMB 130/日", "other": "RMB 100/日",
    },
    "overseas_tier1": {
        "vp": "USD 70/日", "mgr": "USD 60/日", "other": "USD 50/日",
    },
    "overseas_other": {
        "vp": "USD 60/日", "mgr": "USD 50/日", "other": "USD 40/日",
    },
}

TIER1_KW = ["歐洲", "美國", "加拿大", "日本", "韓國", "印度",
            "europe", "usa", "canada", "japan", "korea", "india"]

RANK_LABEL = {
    "vp": "(副)協理以上",
    "mgr": "(副)經理以上",
    "other": "其他人員",
}


# ── 請求 / 回應模型 ────────────────────────────────────────────────
class ParseRequest(BaseModel):
    description: str                          # 出差描述（自然語言）
    applicant: str                            # 申請人姓名
    rank: Literal["vp", "mgr", "other"]      # 職別


class MealInfo(BaseModel):
    region: str
    rule: str
    total: str
    note: str


class ParseResponse(BaseModel):
    form: dict
    meal_info: MealInfo | None
    summary: str


# ── 工具函數 ──────────────────────────────────────────────────────
def calc_meal(dest: str, rank: str, days: int) -> MealInfo | None:
    if not dest or not rank or not days:
        return None
    dl = dest.lower()

    if any(k in dl for k in ["大陸", "中國", "上海", "北京", "廣州",
                               "深圳", "mainland", "china"]):
        rule = MEAL_RULES["china"][rank]
        return MealInfo(
            region="大陸地區",
            rule=rule,
            total=f"依 {rule} × {days} 天",
            note="不需憑據；不論是否供餐皆按標準核給",
        )

    if any(k in dl for k in ["國內", "台灣", "台中", "台南", "高雄",
                               "花蓮", "taiwan", "domestic"]):
        r = MEAL_RULES["domestic"][rank]
        return MealInfo(
            region="國內",
            rule=f"早{r['breakfast']}+午{r['lunch']}+晚{r['dinner']} = NT${r['daily']}/日",
            total=f"NT$ {r['daily'] * days:,}（{r['daily']} × {days} 天）",
            note="不需憑據；招待客人用餐請交際費時，當日不得再請膳雜費",
        )

    # 國外
    tier = "overseas_tier1" if any(k in dl for k in TIER1_KW) else "overseas_other"
    rule = MEAL_RULES[tier][rank]
    region = "歐洲/美加/日韓/印度" if tier == "overseas_tier1" else "其他國家"
    return MealInfo(
        region=f"國外（{region}）",
        rule=f"{rule}",
        total=f"{rule.split('/')[0]} × {days} 天",
        note="不需憑據；長途飛行機上供餐需扣除（早$5、午$15、晚$15 USD）",
    )


def build_summary(form: dict, meal: MealInfo | None) -> str:
    lines = [
        f"📋 {form.get('applicant', '')}（{form.get('rank', '')}）出差申請單",
        f"📌 主旨：{form.get('title', '—')}",
        f"📍 地點：{form.get('dest', '—')}",
        f"📅 出發：{form.get('depart', '—')} → 回來：{form.get('return_date', '—')}（{form.get('days', '?')}天）",
        f"💰 費用：NT$ {form.get('twd', '?')}（不含膳雜費）",
    ]
    if meal:
        lines.append(f"🍽  膳雜費：{meal.total}（{meal.rule}）")
    if form.get("project"):
        lines.append(f"🗂  專案代碼：{form['project']}")
    lines.append(f"👤 受款人：{form.get('payee', '—')}")
    return "\n".join(lines)


# ── 主要 API 端點 ──────────────────────────────────────────────────
@app.post("/parse", response_model=ParseResponse)
async def parse_trip(req: ParseRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY 未設定")

    today = date.today().isoformat()
    rank_label = RANK_LABEL[req.rank]

    system_prompt = f"""你是勝利體育（VICTOR）公司出差申請單解析助手。
申請人：{req.applicant}，職別：{rank_label}
今天日期：{today}

從使用者描述中提取以下欄位，只回傳 JSON，不加任何說明或 markdown：
{{
  "title":       "差旅主旨",
  "report":      "出差報告書（完整說明）",
  "dest":        "出差地點（明確標示：國內/大陸/國外+城市）",
  "depart":      "YYYY-MM-DD HH:mm",
  "return_date": "YYYY-MM-DD HH:mm",
  "days":        天數整數,
  "zone":        "費用區間（小於兩萬元(含) 或 兩萬元以上）",
  "twd":         新台幣全額數字（不含膳雜費）,
  "advance":     已預支金額（未提及填0）,
  "payable":     實付金額,
  "expenses":    "費用明細條列（不含膳雜費）",
  "exp_desc":    "費用說明",
  "project":     "專案代碼或null",
  "currency":    "幣別或null",
  "rate":        匯率數字或null,
  "orig_amt":    "原幣合計說明或null",
  "twd_total":   台幣合計數字或null,
  "over_limit":  "超限說明或無"
}}
無法提取的填 null。"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": req.description}],
        )
        raw = msg.content[0].text.strip()
        form = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"AI 回傳格式錯誤：{e}")
    except Exception as e:
        raise HTTPException(500, f"API 呼叫失敗：{e}")

    # 注入申請人與職別（由系統決定，不從描述中取）
    form["applicant"] = req.applicant
    form["payee"]     = req.applicant
    form["rank"]      = rank_label

    # 計算膳雜費
    days = int(form.get("days") or 0)
    meal = calc_meal(form.get("dest", ""), req.rank, days)

    return ParseResponse(
        form=form,
        meal_info=meal,
        summary=build_summary(form, meal),
    )


# ── 健康檢查 ────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "VICTOR 出差申請解析 API"}


# ── 本地啟動 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("wecom_api_server:app", host="0.0.0.0", port=8000, reload=True)
