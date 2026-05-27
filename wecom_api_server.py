"""
VICTOR 出差申請 — 企業微信插件中介 API（修正版）
只需一個 text 參數，applicant/rank 從描述中自動解析

安裝：pip install fastapi uvicorn anthropic
啟動：uvicorn wecom_api_server:app --reload --port 8000
"""

import os, json
from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic

app = FastAPI(title="VICTOR 出差申請解析 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── 膳雜費規則 ────────────────────────────────────────────────────
MEAL_RULES = {
    "domestic": {
        "vp":    {"daily": 650, "detail": "早150+午250+晚250"},
        "mgr":   {"daily": 500, "detail": "早100+午200+晚200"},
        "other": {"daily": 400, "detail": "早100+午150+晚150"},
    },
    "china":           {"vp": "RMB 150/日", "mgr": "RMB 130/日", "other": "RMB 100/日"},
    "overseas_tier1":  {"vp": "USD 70/日",  "mgr": "USD 60/日",  "other": "USD 50/日"},
    "overseas_other":  {"vp": "USD 60/日",  "mgr": "USD 50/日",  "other": "USD 40/日"},
}
TIER1 = ["歐洲","美國","加拿大","日本","韓國","印度","europe","usa","canada","japan","korea","india"]
RANK_LABEL = {"vp": "(副)協理以上", "mgr": "(副)經理以上", "other": "其他人員"}

def calc_meal(dest: str, rank: str, days: int) -> dict:
    if not dest or not rank or not days:
        return {}
    d = dest.lower()
    if any(k in d for k in ["大陸","中國","上海","北京","廣州","深圳","china","mainland"]):
        rule = MEAL_RULES["china"][rank]
        return {"region": "大陸", "rule": rule, "total": f"依 {rule} × {days} 天", "note": "不需憑據；不論是否供餐皆按標準核給"}
    if any(k in d for k in ["國內","台灣","台中","台南","高雄","花蓮","taiwan","domestic"]):
        r = MEAL_RULES["domestic"][rank]
        return {"region": "國內", "rule": f"NT${r['daily']}/日（{r['detail']}）", "total": f"NT$ {r['daily']*days:,}", "note": "不需憑據"}
    tier = "overseas_tier1" if any(k in d for k in TIER1) else "overseas_other"
    rule = MEAL_RULES[tier][rank]
    return {"region": "國外", "rule": rule, "total": f"{rule.split('/')[0]} × {days} 天", "note": "長途飛行機上供餐需扣除"}


# ── 請求模型（只需 text 一個參數）────────────────────────────────
class ParseRequest(BaseModel):
    text: str   # 企業微信傳入的參數名稱


# ── 主端點 ─────────────────────────────────────────────────────────
@app.post("/parse")
async def parse_trip(req: ParseRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY 未設定")

    today = date.today().isoformat()

    system_prompt = f"""你是勝利體育（VICTOR）公司出差申請單解析助手。今天日期：{today}

從使用者描述中提取以下欄位，只回傳 JSON，不加任何說明或 markdown：
{{
  "applicant":   "申請人姓名（若有提及）",
  "rank":        "職別代碼：vp=協理以上 / mgr=經理 / other=其他（若未提及填other）",
  "title":       "差旅主旨",
  "report":      "出差報告書",
  "dest":        "出差地點（請標示：國內/大陸/國外+城市）",
  "depart":      "出發日期時間 YYYY-MM-DD HH:mm",
  "return_date": "回來日期時間 YYYY-MM-DD HH:mm",
  "days":        天數整數,
  "zone":        "費用區間（小於兩萬元(含) 或 兩萬元以上）",
  "twd":         新台幣全額數字（不含膳雜費）,
  "advance":     預支金額（未提及填0）,
  "payable":     實付金額,
  "expenses":    "費用明細條列（不含膳雜費）",
  "project":     "專案代碼或null",
  "currency":    "幣別或null",
  "rate":        匯率數字或null,
  "over_limit":  "超限說明或無"
}}
無法提取的填 null。"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": req.text}],
        )
        form = json.loads(msg.content[0].text.strip())
    except Exception as e:
        raise HTTPException(500, f"解析失敗：{e}")

    # 受款人 = 申請人
    form["payee"] = form.get("applicant", "（請填寫）")
    rank = form.get("rank", "other")
    form["rank_label"] = RANK_LABEL.get(rank, "其他人員")

    # 計算膳雜費
    days = int(form.get("days") or 0)
    meal = calc_meal(form.get("dest", ""), rank, days)

    # 給企業微信顯示的摘要文字
    summary_lines = [
        f"✅ 出差申請單解析完成",
        f"",
        f"👤 申請人：{form.get('applicant','（未提及）')}（{form['rank_label']}）",
        f"📌 主旨：{form.get('title','—')}",
        f"📍 地點：{form.get('dest','—')}",
        f"📅 {form.get('depart','—')} → {form.get('return_date','—')}（{days} 天）",
        f"💰 費用：NT$ {form.get('twd','?')}（不含膳雜費）",
    ]
    if meal:
        summary_lines.append(f"🍽  膳雜費：{meal.get('total','—')}（{meal.get('rule','—')}）")
        summary_lines.append(f"    {meal.get('note','')}")
    if form.get("project"):
        summary_lines.append(f"🗂  專案代碼：{form['project']}")
    summary_lines += ["", "⚠️ 請確認後手動填入企業微信審批表單，日期與下拉選單需人工選擇。"]

    return {
        "form":     form,
        "meal":     meal,
        "summary":  "\n".join(summary_lines),
    }


@app.get("/")
def health():
    return {"status": "ok", "service": "VICTOR 出差申請解析 API v2"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("wecom_api_server:app", host="0.0.0.0", port=8000, reload=True)
