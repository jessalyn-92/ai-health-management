import os
import json
import base64
import re
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "health_system_secret_key_2024")
app.config["UPLOAD_FOLDER"] = "uploads"

# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect("health.db", timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gender TEXT, age INTEGER, height REAL, weight REAL, bmi REAL,
            goal_weight REAL, activity_level TEXT, tdee REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS health_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            daily_calories REAL,
            protein_g REAL, fat_g REAL, carbs_g REAL,
            diet_advice TEXT, exercise_strategy TEXT,
            weekly_plan TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS meal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, food_name TEXT,
            calories REAL, protein_g REAL, fat_g REAL, carbs_g REAL,
            source TEXT, log_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    db.commit()
    db.close()

init_db()

# ── OpenAI ───────────────────────────────────────────────────────────────────

def get_client():
    key = os.environ.get("OPENAI_API_KEY", "")
    return OpenAI(api_key=key) if key else None

# ── Helpers ──────────────────────────────────────────────────────────────────

def calculate_bmi(weight, height_cm):
    h = height_cm / 100
    return round(weight / (h * h), 1)

def bmi_category(bmi):
    if bmi < 18.5: return "過輕", "#3b82f6"
    elif bmi < 24: return "正常", "#22c55e"
    elif bmi < 27: return "過重", "#f97316"
    else: return "肥胖", "#ef4444"

def calculate_bmr(gender, age, weight, height_cm):
    if gender == "male":
        return 10 * weight + 6.25 * height_cm - 5 * age + 5
    return 10 * weight + 6.25 * height_cm - 5 * age - 161

ACTIVITY_FACTORS = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "high": 1.725}

def calculate_tdee(bmr, activity):
    return round(bmr * ACTIVITY_FACTORS.get(activity, 1.2))

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("step1"))

@app.route("/step1", methods=["GET", "POST"])
def step1():
    if request.method == "POST":
        gender = request.form.get("gender", "male")
        age    = int(request.form.get("age", 25))
        height = float(request.form.get("height", 170))
        weight = float(request.form.get("weight", 65))
        bmi    = calculate_bmi(weight, height)
        session.update(gender=gender, age=age, height=height, weight=weight, bmi=bmi)
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO users (gender,age,height,weight,bmi) VALUES (?,?,?,?,?)",
                (gender, age, height, weight, bmi)
            )
            session["user_id"] = cur.lastrowid
            db.commit()
        finally:
            db.close()
        return redirect(url_for("step2"))
    return render_template("step1.html")

@app.route("/step2", methods=["GET", "POST"])
def step2():
    if request.method == "POST":
        goal_weight = float(request.form.get("goal_weight", session.get("weight", 65)))
        activity    = request.form.get("activity_level", "moderate")
        gender  = session.get("gender", "male")
        age     = session.get("age", 25)
        weight  = session.get("weight", 65)
        height  = session.get("height", 170)
        bmi     = session.get("bmi", 22)
        user_id = session.get("user_id", 1)

        bmr  = calculate_bmr(gender, age, weight, height)
        tdee = calculate_tdee(bmr, activity)
        session.update(tdee=tdee, goal_weight=goal_weight, activity_level=activity)

        sport_hint = "低衝擊運動（游泳、快走、橢圓機）" if bmi > 27 else "中高強度運動（HIIT、慢跑）"

        client = get_client()
        weekly_plan = []
        protein_g = fat_g = carbs_g = 0
        diet_advice = exercise_strategy = daily_calories = ""

        if client:
            prompt = f"""你是一位專業健身教練與營養師。請根據以下用戶數據生成 JSON 格式建議：

【用戶數據】
性別: {"男" if gender=="male" else "女"}, 年齡: {age}歲, 身高: {height}cm
目前體重: {weight}kg (BMI: {bmi}), 目標體重: {goal_weight}kg
活動量: {activity}, TDEE: {tdee}kcal, 推薦運動: {sport_hint}

【回傳格式】嚴格遵守 JSON，欄位如下：
{{
  "daily_calories": 每日建議熱量(數字),
  "macros": {{"protein_g": 蛋白質克數, "fat_g": 脂肪克數, "carbs_g": 碳水克數}},
  "diet_advice": "簡短飲食建議（繁體中文，2-3句）",
  "exercise_strategy": "針對此BMI的運動策略說明（繁體中文，2-3句）",
  "weekly_schedule": [
    {{"day":"週一","type":"有氧","item":"快走","sets":"無","duration":"30分","distance":"3km","intensity":"低"}},
    ...共7天
  ]
}}"""
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "你是輸出 JSON 的後端 API，請用繁體中文。"},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=2048, temperature=0.3,
                    response_format={"type": "json_object"},
                )
                data = json.loads(resp.choices[0].message.content)
                daily_calories    = data.get("daily_calories", tdee)
                macros            = data.get("macros", {})
                protein_g         = macros.get("protein_g", round(weight * 1.6))
                fat_g             = macros.get("fat_g", round(tdee * 0.25 / 9))
                carbs_g           = macros.get("carbs_g", round((tdee - protein_g*4 - fat_g*9) / 4))
                diet_advice       = data.get("diet_advice", "")
                exercise_strategy = data.get("exercise_strategy", "")
                weekly_plan       = data.get("weekly_schedule", [])
            except Exception as e:
                print(f"OpenAI error: {e}")

        # Fallback
        if not weekly_plan:
            protein_g = round(weight * 1.6)
            fat_g     = round(tdee * 0.25 / 9)
            carbs_g   = round((tdee - protein_g*4 - fat_g*9) / 4)
            daily_calories = tdee
            days = ["週一","週二","週三","週四","週五","週六","週日"]
            ex   = [("有氧","慢跑","無","30分","3km","中"),
                    ("有氧","游泳","無","40分","—","中"),
                    ("肌力","HIIT","4組","20分","—","高"),
                    ("有氧","快走","無","45分","4km","低"),
                    ("肌力","重訓","3組×12下","50分","—","高"),
                    ("有氧","瑜珈","無","60分","—","低"),
                    ("休息","主動恢復","—","—","—","低")]
            weekly_plan = [{"day":d,"type":e[0],"item":e[1],"sets":e[2],"duration":e[3],"distance":e[4],"intensity":e[5]} for d,e in zip(days,ex)]

        session.update(
            protein_g=protein_g, fat_g=fat_g, carbs_g=carbs_g,
            daily_calories=daily_calories,
            diet_advice=diet_advice, exercise_strategy=exercise_strategy,
            weekly_plan=weekly_plan
        )

        db = get_db()
        try:
            db.execute("UPDATE users SET goal_weight=?,activity_level=?,tdee=? WHERE id=?",
                       (goal_weight, activity, tdee, user_id))
            db.execute(
                "INSERT INTO health_plans (user_id,daily_calories,protein_g,fat_g,carbs_g,diet_advice,exercise_strategy,weekly_plan) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, daily_calories, protein_g, fat_g, carbs_g,
                 diet_advice, exercise_strategy, json.dumps(weekly_plan, ensure_ascii=False))
            )
            db.commit()
        finally:
            db.close()
        return redirect(url_for("step3"))

    bmi = session.get("bmi", 0)
    cat, color = bmi_category(bmi)
    return render_template("step2.html", bmi=bmi, bmi_category=cat, bmi_color=color)

@app.route("/step3")
def step3():
    return render_template("step3.html")

# ── OCR：改用 GPT-4o Vision 直接辨識營養標示 ─────────────────────────────────
@app.route("/api/ocr", methods=["POST"])
def api_ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image"}), 400
    file      = request.files["image"]
    img_bytes = file.read()
    b64       = base64.b64encode(img_bytes).decode()
    ext       = (file.filename.rsplit(".", 1)[-1].lower()) if "." in file.filename else "jpeg"
    media_type = f"image/{ext if ext in ['png','gif','webp'] else 'jpeg'}"

    client = get_client()
    if not client:
        return jsonify({"error": "OpenAI API not configured"}), 503

    prompt = """這是一張食品營養成分標示的圖片。
請仔細辨識並以 JSON 格式回傳以下欄位（只回傳 JSON，不要其他文字）：
{
  "food_name": "依包裝辨識的食品名稱（若無則填'掃描食品'）",
  "calories": 每份熱量數字(kcal，純數字),
  "protein": 蛋白質克數(純數字),
  "fat": 脂肪克數(純數字),
  "carbs": 碳水化合物克數(純數字),
  "serving_size": "每份份量說明（如有）"
}
若某欄位看不清楚，填 0。"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}", "detail": "high"}},
                {"type": "text", "text": prompt}
            ]}],
            max_tokens=512, temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── AI Vision（食物拍照估算）────────────────────────────────────────────────
@app.route("/api/vision", methods=["POST"])
def api_vision():
    if "image" not in request.files:
        return jsonify({"error": "No image"}), 400
    file      = request.files["image"]
    img_bytes = file.read()
    b64       = base64.b64encode(img_bytes).decode()
    ext       = (file.filename.rsplit(".", 1)[-1].lower()) if "." in file.filename else "jpeg"
    media_type = f"image/{ext if ext in ['png','gif','webp'] else 'jpeg'}"

    client = get_client()
    if not client:
        return jsonify({"error": "OpenAI API not configured"}), 503

    prompt = """請分析這張食物照片，以 JSON 格式回傳（只回傳 JSON）：
{"food_name":"食物名稱","calories":熱量數字,"protein":蛋白質克數,"fat":脂肪克數,"carbs":碳水化合物克數,"confidence":0到1的信心值}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                {"type": "text", "text": prompt}
            ]}],
            max_tokens=512, temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Save meal ────────────────────────────────────────────────────────────────
@app.route("/api/meal", methods=["POST"])
def api_save_meal():
    data    = request.json
    user_id = session.get("user_id", 1)
    from datetime import date
    db = get_db()
    try:
        db.execute(
            "INSERT INTO meal_logs (user_id,food_name,calories,protein_g,fat_g,carbs_g,source,log_date) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, data.get("food_name",""), data.get("calories",0),
             data.get("protein",0), data.get("fat",0), data.get("carbs",0),
             data.get("source","手動輸入"), str(date.today()))
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok"})

# ── Dashboard ────────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/dashboard/summary")
def api_dashboard_summary():
    user_id = session.get("user_id", 1)
    from datetime import date
    today = str(date.today())
    db = get_db()
    try:
        meals = db.execute(
            "SELECT * FROM meal_logs WHERE user_id=? AND log_date=?", (user_id, today)
        ).fetchall()
        total_cal     = sum(m["calories"]  for m in meals)
        total_protein = sum(m["protein_g"] for m in meals)
        total_fat     = sum(m["fat_g"]     for m in meals)
        total_carbs   = sum(m["carbs_g"]   for m in meals)
    finally:
        db.close()
    return jsonify({
        "gender": session.get("gender",""),
        "age":    session.get("age", 0),
        "current_weight": session.get("weight", 0),
        "goal_weight":    session.get("goal_weight", 0),
        "bmi":            session.get("bmi", 0),
        "tdee":           session.get("tdee", 2000),
        "daily_calories": session.get("daily_calories", 0),
        "diet_advice":       session.get("diet_advice", ""),
        "exercise_strategy": session.get("exercise_strategy", ""),
        "total_calories": total_cal,
        "total_protein":  total_protein,
        "total_fat":      total_fat,
        "total_carbs":    total_carbs,
        "recommended_protein": session.get("protein_g", 0),
        "recommended_fat":     session.get("fat_g", 0),
        "recommended_carbs":   session.get("carbs_g", 0),
        "weekly_plan": session.get("weekly_plan", []),
    })

@app.route("/api/dashboard/meal-logs")
def api_meal_logs():
    user_id = session.get("user_id", 1)
    from datetime import date
    today = str(date.today())
    db = get_db()
    try:
        meals = db.execute(
            "SELECT * FROM meal_logs WHERE user_id=? AND log_date=? ORDER BY created_at DESC",
            (user_id, today)
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(m) for m in meals])


# ── Regenerate weekly plan ────────────────────────────────────────────────────
@app.route("/api/regenerate-plan", methods=["POST"])
def api_regenerate_plan():
    """重新生成運動菜單，支援肌群偏好"""
    data        = request.json or {}
    muscle_prefs = data.get("muscles", [])   # e.g. ["胸", "背", "腿"]
    intensity_pref = data.get("intensity", "均衡")  # 輕鬆 / 均衡 / 強烈

    gender  = session.get("gender", "male")
    age     = session.get("age", 25)
    weight  = session.get("weight", 65)
    height  = session.get("height", 170)
    bmi     = session.get("bmi", 22)
    tdee    = session.get("tdee", 2000)
    goal_weight = session.get("goal_weight", 60)

    muscle_str = "、".join(muscle_prefs) if muscle_prefs else "全身均衡"
    sport_hint = "低衝擊運動（游泳、快走）" if bmi > 27 else "中高強度運動（HIIT、重訓）"

    client = get_client()
    if not client:
        return jsonify({"error": "OpenAI API not configured"}), 503

    prompt = f"""你是一位專業健身教練。請根據使用者需求生成一份個人化一週運動菜單。

【使用者資料】
性別: {"男" if gender=="male" else "女"}, 年齡: {age}歲, BMI: {bmi}, TDEE: {tdee}kcal
目標體重: {goal_weight}kg, 推薦運動類型: {sport_hint}

【本次偏好】
想加強的肌群: {muscle_str}
訓練強度偏好: {intensity_pref}

【回傳格式】嚴格遵守 JSON，只回傳 JSON：
{{
  "weekly_schedule": [
    {{
      "day": "週一",
      "type": "有氧/肌力/HIIT/休息",
      "item": "運動項目名稱",
      "sets": "組數（無則填無）",
      "duration": "時間（如30分）",
      "distance": "距離（無則填無）",
      "intensity": "低/中/高",
      "muscle_focus": "主要訓練肌群",
      "equipment": ["器材1", "器材2"]
    }},
    ... 共7天（包含至少1天休息）
  ],
  "tips": "針對此肌群組合的訓練小提示（繁體中文，2句）"
}}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你是輸出 JSON 的後端 API，用繁體中文。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=2048, temperature=0.5,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)
        new_plan = result.get("weekly_schedule", [])
        tips     = result.get("tips", "")
        session["weekly_plan"] = new_plan

        # Update DB
        user_id = session.get("user_id", 1)
        db = get_db()
        try:
            db.execute(
                "UPDATE health_plans SET weekly_plan=? WHERE user_id=?",
                (json.dumps(new_plan, ensure_ascii=False), user_id)
            )
            db.commit()
        finally:
            db.close()

        return jsonify({"status": "ok", "weekly_plan": new_plan, "tips": tips})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Workout check-in ─────────────────────────────────────────────────────────
@app.route("/api/checkin", methods=["POST"])
def api_checkin():
    """記錄當天運動打卡"""
    data    = request.json or {}
    day     = data.get("day", "")
    done    = data.get("done", True)
    user_id = session.get("user_id", 1)
    from datetime import date
    today   = str(date.today())

    db = get_db()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS workout_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                checkin_date TEXT,
                day_label TEXT,
                done INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Upsert
        existing = db.execute(
            "SELECT id FROM workout_checkins WHERE user_id=? AND checkin_date=? AND day_label=?",
            (user_id, today, day)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE workout_checkins SET done=? WHERE id=?",
                (1 if done else 0, existing["id"])
            )
        else:
            db.execute(
                "INSERT INTO workout_checkins (user_id,checkin_date,day_label,done) VALUES (?,?,?,?)",
                (user_id, today, day, 1 if done else 0)
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok"})


# ── Get this week checkins ────────────────────────────────────────────────────
@app.route("/api/checkins")
def api_get_checkins():
    user_id = session.get("user_id", 1)
    db = get_db()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS workout_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                checkin_date TEXT,
                day_label TEXT,
                done INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        rows = db.execute(
            "SELECT day_label, done, checkin_date FROM workout_checkins WHERE user_id=? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
    finally:
        db.close()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    app.run(debug=True, port=5000)
