#!/usr/bin/env python3
"""員工績效紀錄與獎勵推播系統（含 Google 評論加分流程）"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime, date
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string
import requests

# === 設定 ===
BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "rewards.db")))
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:5001")

app = Flask(__name__)

# ─── 資料庫 ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee    TEXT    NOT NULL,
            achievement TEXT    NOT NULL,
            points      INTEGER NOT NULL DEFAULT 1,
            recorder    TEXT    DEFAULT '',
            created_at  TIMESTAMP DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer    TEXT    DEFAULT '匿名',
            review_text TEXT    NOT NULL,
            rating      INTEGER DEFAULT 5,
            points      INTEGER DEFAULT 3,
            token       TEXT    UNIQUE,
            status      TEXT    DEFAULT 'pending',
            created_at  TIMESTAMP DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()

def add_record(employee, achievement, points, recorder=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO records (employee, achievement, points, recorder) VALUES (?,?,?,?)",
        (employee, achievement, points, recorder)
    )
    conn.commit()
    conn.close()

def get_today_records():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT employee, achievement, points, recorder, created_at FROM records "
        "WHERE DATE(created_at) = DATE('now','localtime') ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows

def get_all_names():
    conn = sqlite3.connect(DB_PATH)
    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT employee FROM records ORDER BY employee"
    ).fetchall()]
    conn.close()
    return names

def get_monthly_leaderboard(year=None, month=None):
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT employee,
               SUM(points)  AS total,
               COUNT(*)     AS cnt
        FROM records
        WHERE strftime('%Y', created_at) = ?
          AND strftime('%m', created_at) = ?
        GROUP BY employee
        ORDER BY total DESC
        LIMIT 10
    """, (str(year), f"{month:02d}")).fetchall()
    conn.close()
    return rows

def get_all_monthly_totals():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT strftime('%Y-%m', created_at) AS ym,
               employee, SUM(points) AS total, COUNT(*) AS cnt
        FROM records
        GROUP BY ym, employee
        ORDER BY ym DESC, total DESC
    """).fetchall()
    conn.close()
    return rows

def add_pending_review(reviewer, review_text, rating, points):
    token = uuid.uuid4().hex
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO pending_reviews (reviewer, review_text, rating, points, token) VALUES (?,?,?,?,?)",
        (reviewer, review_text, rating, points, token)
    )
    review_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return review_id, token

def get_pending_review(rid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, reviewer, review_text, rating, points, token, status FROM pending_reviews WHERE id=?",
        (rid,)
    ).fetchone()
    conn.close()
    return row

def set_review_status(rid, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE pending_reviews SET status=? WHERE id=?", (status, rid))
    conn.commit()
    conn.close()

def rating_to_points(rating):
    if rating >= 5:
        return 3
    if rating >= 4:
        return 2
    return 1

# ─── LINE 推播 ─────────────────────────────────────────────────────────────

def push_line(text, target_id=None):
    to = target_id or LINE_USER_ID
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": to, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )
    return resp.status_code == 200, resp.text

def push_line_flex_review(review_id, reviewer, review_text, rating, points, token):
    approve_url = f"{SERVER_BASE_URL}/approve/{review_id}?token={token}"
    skip_url = f"{SERVER_BASE_URL}/skip/{review_id}?token={token}"
    stars = "⭐" * int(rating)
    preview = review_text[:80] + ("…" if len(review_text) > 80 else "")

    flex = {
        "type": "flex",
        "altText": f"🌟 新 Google 好評待審核｜{reviewer}（{rating}顆星）",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#1a0f05",
                "paddingAll": "16px",
                "contents": [
                    {"type": "text", "text": "🌟 新 Google 好評", "weight": "bold",
                     "size": "lg", "color": "#d4a843"},
                    {"type": "text", "text": stars, "size": "md", "margin": "xs"}
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0d0802",
                "paddingAll": "16px",
                "spacing": "md",
                "contents": [
                    {
                        "type": "box", "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "評論者", "size": "sm",
                             "color": "#a08060", "flex": 2},
                            {"type": "text", "text": reviewer, "size": "sm",
                             "color": "#faf2e0", "flex": 5, "weight": "bold"}
                        ]
                    },
                    {"type": "text", "text": preview, "size": "sm",
                     "color": "#faf2e0", "wrap": True},
                    {"type": "separator", "color": "#2a1f12"},
                    {
                        "type": "box", "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "建議加分", "size": "sm",
                             "color": "#a08060", "flex": 3},
                            {"type": "text", "text": f"+{points} 分", "size": "sm",
                             "color": "#d4a843", "flex": 4, "weight": "bold"}
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "backgroundColor": "#0d0802",
                "paddingAll": "12px",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "action": {"type": "uri", "label": "✅ 確認加分", "uri": approve_url},
                        "style": "primary", "color": "#d4a843", "height": "sm"
                    },
                    {
                        "type": "button",
                        "action": {"type": "uri", "label": "略過", "uri": skip_url},
                        "style": "secondary", "height": "sm"
                    }
                ]
            }
        }
    }

    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": LINE_USER_ID, "messages": [flex]},
        timeout=10,
    )
    return resp.status_code == 200, resp.text

def push_line_group_review(reviewer, review_text, rating, employee, points):
    if not LINE_GROUP_ID:
        return False, "GROUP_ID 未設定"
    lb = get_monthly_leaderboard()
    stars = "⭐" * int(rating)
    short_review = review_text[:80] + ("…" if len(review_text) > 80 else "")
    lines = [
        f"🌟 Google 好評加分！",
        f"",
        f"顧客 {reviewer} 留下了 {stars}：",
        f"「{short_review}」",
        f"",
        f"恭喜 {employee} 獲得 +{points} 分！🎉",
    ]
    if lb:
        now = datetime.now()
        lines += [f"", f"📊 {now.month}月積分排行 Top 3"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (emp, total, cnt) in enumerate(lb[:3]):
            lines.append(f"{medals[i]} {emp}：{total}分")
    return push_line("\n".join(lines), target_id=LINE_GROUP_ID)

def build_daily_msg(records, date_str):
    if not records:
        return None
    lines = [f"🌟 今日績優表揚  {date_str}\n"]
    for emp, ach, pts, rec, _ in records:
        lines.append(f"✨ {emp}（+{pts}分）")
        lines.append(f"   {ach}")
        if rec:
            lines.append(f"   — 記錄人：{rec}")
        lines.append("")
    lb = get_monthly_leaderboard()
    if lb:
        now = datetime.now()
        lines.append(f"📊 {now.month}月積分排行 Top 3")
        for i, (emp, total, cnt) in enumerate(lb[:3]):
            medal = ["🥇", "🥈", "🥉"][i]
            lines.append(f"{medal} {emp}：{total}分")
    return "\n".join(lines)

def build_monthly_msg(year, month):
    lb = get_monthly_leaderboard(year, month)
    if not lb:
        return f"📊 {year}年{month}月份尚無績效紀錄"
    lines = [f"🏆 {year}年{month}月 風雲榜 🏆\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (emp, total, cnt) in enumerate(lb):
        m = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{m} {emp}：{total}分（共{cnt}次表揚）")
    lines.append("\n感謝本月所有表現優秀的夥伴！✨")
    return "\n".join(lines)

# ─── HTML 模板 ─────────────────────────────────────────────────────────────

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>績效紀錄｜楊楊教練</title>
<style>
  :root {
    --bg: #090502;
    --card: #150f08;
    --border: #2a1f12;
    --text: #faf2e0;
    --sub: #a08060;
    --accent: #7db3d0;
    --gold: #d4a843;
    --green: #6bbf8e;
    --red: #e07878;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, 'Noto Sans TC', sans-serif; min-height: 100vh; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 20px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1.1rem; color: var(--gold); }
  header span { font-size: 1.4rem; }

  nav { display: flex; background: var(--card); border-bottom: 1px solid var(--border); }
  nav a { flex: 1; text-align: center; padding: 12px; text-decoration: none; color: var(--sub); font-size: .9rem; border-bottom: 2px solid transparent; transition: all .2s; }
  nav a.active, nav a:hover { color: var(--accent); border-color: var(--accent); }

  .page { display: none; padding: 20px; max-width: 540px; margin: 0 auto; }
  .page.active { display: block; }

  .form-group { margin-bottom: 18px; }
  label { display: block; margin-bottom: 6px; font-size: .85rem; color: var(--sub); letter-spacing: .05em; }
  input[type=text], textarea {
    width: 100%; background: var(--card); border: 1px solid var(--border);
    color: var(--text); border-radius: 10px; padding: 12px 14px;
    font-size: 1rem; font-family: inherit; outline: none; transition: border .2s;
  }
  input[type=text]:focus, textarea:focus { border-color: var(--accent); }
  textarea { resize: vertical; min-height: 80px; }

  .pts-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .pts-btn {
    flex: 1; min-width: 44px; height: 44px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--card);
    color: var(--sub); font-size: 1rem; cursor: pointer; transition: all .15s;
  }
  .pts-btn:hover { border-color: var(--accent); color: var(--accent); }
  .pts-btn.selected { background: var(--accent); border-color: var(--accent); color: var(--bg); font-weight: 700; }

  .submit-btn {
    width: 100%; padding: 15px; border-radius: 12px; border: none;
    background: var(--gold); color: var(--bg); font-size: 1.05rem;
    font-weight: 700; cursor: pointer; letter-spacing: .05em; transition: opacity .2s;
    margin-top: 6px;
  }
  .submit-btn:hover { opacity: .85; }

  .toast {
    padding: 14px 16px; border-radius: 10px; margin-bottom: 18px;
    font-size: .9rem; border: 1px solid;
  }
  .toast.success { background: #0e2217; border-color: var(--green); color: var(--green); }
  .toast.error   { background: #220e0e; border-color: var(--red);   color: var(--red);   }

  .section-title {
    font-size: .8rem; color: var(--sub); letter-spacing: .12em;
    text-transform: uppercase; margin-bottom: 12px; margin-top: 8px;
  }
  .record-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
  }
  .record-card .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .record-card .name { font-weight: 700; font-size: 1rem; }
  .record-card .pts { color: var(--gold); font-weight: 700; font-size: 1rem; }
  .record-card .ach { color: var(--sub); font-size: .9rem; line-height: 1.5; }
  .record-card .meta { font-size: .75rem; color: #4a3820; margin-top: 6px; }
  .record-card.google-badge { border-color: #3a8f6a; }
  .record-card .google-tag { font-size: .7rem; color: #6bbf8e; background: #0e2217; border-radius: 4px; padding: 2px 6px; margin-left: 6px; }

  .rank-item {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; margin-bottom: 8px;
    display: flex; align-items: center; gap: 14px;
  }
  .rank-item.top1 { border-color: #c8922a; background: #180f03; }
  .rank-item.top2 { border-color: #8a9da8; }
  .rank-item.top3 { border-color: #8a6a40; }
  .rank-medal { font-size: 1.5rem; width: 36px; text-align: center; flex-shrink: 0; }
  .rank-name { flex: 1; font-weight: 700; }
  .rank-stats { text-align: right; }
  .rank-pts { font-size: 1.2rem; font-weight: 700; color: var(--gold); }
  .rank-cnt { font-size: .75rem; color: var(--sub); margin-top: 2px; }

  .month-tab-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .month-tab {
    padding: 6px 14px; border-radius: 20px; border: 1px solid var(--border);
    background: var(--card); color: var(--sub); font-size: .85rem; cursor: pointer; text-decoration: none;
  }
  .month-tab.active { border-color: var(--accent); color: var(--accent); background: #0d1e29; }

  .empty { text-align: center; color: var(--sub); padding: 40px 0; font-size: .9rem; }
</style>
</head>
<body>

<header>
  <span>⭐</span>
  <h1>員工績效紀錄系統</h1>
</header>

<nav>
  <a href="/?tab=add" class="{{ 'active' if tab == 'add' else '' }}">＋ 新增紀錄</a>
  <a href="/?tab=today" class="{{ 'active' if tab == 'today' else '' }}">今日紀錄</a>
  <a href="/?tab=board" class="{{ 'active' if tab == 'board' else '' }}">🏆 風雲榜</a>
</nav>

<!-- ── 新增紀錄 ── -->
<div class="page {{ 'active' if tab == 'add' else '' }}">

  {% if success %}
  <div class="toast success">✅ 已記錄 {{ success_name }} 的表現！</div>
  {% endif %}
  {% if error %}
  <div class="toast error">⚠️ {{ error }}</div>
  {% endif %}

  <form method="POST" action="/submit">
    <div class="form-group">
      <label>員工姓名 *</label>
      <input type="text" name="employee" placeholder="輸入員工大名" required
             list="names-list" autocomplete="off">
      <datalist id="names-list">
        {% for n in all_names %}<option value="{{ n }}">{% endfor %}
      </datalist>
    </div>

    <div class="form-group">
      <label>事蹟描述 *</label>
      <textarea name="achievement" placeholder="簡單描述本次優秀表現…" required></textarea>
    </div>

    <div class="form-group">
      <label>加幾分（1–5）</label>
      <div class="pts-row" id="pts-row">
        {% for p in [1, 2, 3, 4, 5] %}
        <button type="button" class="pts-btn {{ 'selected' if p == 1 else '' }}"
                onclick="selectPts(this, {{ p }})">{{ p }}</button>
        {% endfor %}
      </div>
      <input type="hidden" name="points" id="pts-input" value="1">
    </div>

    <div class="form-group">
      <label>記錄人（選填）</label>
      <input type="text" name="recorder" placeholder="你的名字">
    </div>

    <button type="submit" class="submit-btn">登記送出 🎉</button>
  </form>
</div>

<!-- ── 今日紀錄 ── -->
<div class="page {{ 'active' if tab == 'today' else '' }}">
  <div class="section-title">今日 {{ today_date }} 共 {{ today_records|length }} 筆</div>
  {% if today_records %}
    {% for emp, ach, pts, rec, ts in today_records %}
    <div class="record-card {{ 'google-badge' if rec == 'Google 評論' else '' }}">
      <div class="header">
        <span class="name">
          {{ emp }}
          {% if rec == 'Google 評論' %}<span class="google-tag">Google ⭐</span>{% endif %}
        </span>
        <span class="pts">+{{ pts }}分</span>
      </div>
      <div class="ach">{{ ach }}</div>
      <div class="meta">
        {{ ts[11:16] if ts|length > 10 else ts }}
        {% if rec and rec != 'Google 評論' %} · 記錄：{{ rec }}{% endif %}
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">今天還沒有紀錄 👀<br>快去新增第一筆！</div>
  {% endif %}
</div>

<!-- ── 風雲榜 ── -->
<div class="page {{ 'active' if tab == 'board' else '' }}">
  <div class="month-tab-row">
    {% for ym in available_months %}
    <a href="/?tab=board&ym={{ ym }}" class="month-tab {{ 'active' if ym == sel_ym else '' }}">
      {{ ym }}
    </a>
    {% endfor %}
    {% if not available_months %}
    <span style="color:var(--sub);font-size:.9rem">尚無月份資料</span>
    {% endif %}
  </div>

  {% if leaderboard %}
  {% set medals = ['🥇','🥈','🥉'] %}
  {% for emp, total, cnt in leaderboard %}
  {% set rank_class = 'top1' if loop.index == 1 else ('top2' if loop.index == 2 else ('top3' if loop.index == 3 else '')) %}
  <div class="rank-item {{ rank_class }}">
    <div class="rank-medal">{{ medals[loop.index0] if loop.index <= 3 else loop.index }}</div>
    <div class="rank-name">{{ emp }}</div>
    <div class="rank-stats">
      <div class="rank-pts">{{ total }}分</div>
      <div class="rank-cnt">{{ cnt }}次表揚</div>
    </div>
  </div>
  {% endfor %}
  {% else %}
    <div class="empty">這個月還沒有紀錄</div>
  {% endif %}
</div>

<script>
function selectPts(btn, val) {
  document.querySelectorAll('.pts-btn').forEach(b => b.classList.remove('selected'));
  btn.classList.add('selected');
  document.getElementById('pts-input').value = val;
}
</script>
</body>
</html>
"""

APPROVE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google 評論加分審核</title>
<style>
  :root {
    --bg: #090502; --card: #150f08; --border: #2a1f12;
    --text: #faf2e0; --sub: #a08060; --accent: #7db3d0;
    --gold: #d4a843; --green: #6bbf8e; --red: #e07878;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system,'Noto Sans TC',sans-serif; min-height: 100vh; padding: 0 0 40px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 20px; }
  header h1 { font-size: 1.05rem; color: var(--gold); }

  .wrap { max-width: 480px; margin: 0 auto; padding: 24px 20px; }

  .review-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 18px; margin-bottom: 24px; }
  .review-card .stars { font-size: 1.2rem; margin-bottom: 10px; }
  .review-card .reviewer { font-size: .8rem; color: var(--sub); margin-bottom: 8px; }
  .review-card .review-text { font-size: .95rem; line-height: 1.7; color: var(--text); }

  .form-group { margin-bottom: 20px; }
  label { display: block; margin-bottom: 8px; font-size: .85rem; color: var(--sub); letter-spacing: .05em; }
  input[type=text] {
    width: 100%; background: var(--card); border: 1px solid var(--border);
    color: var(--text); border-radius: 10px; padding: 14px 16px;
    font-size: 1rem; font-family: inherit; outline: none; transition: border .2s;
  }
  input[type=text]:focus { border-color: var(--accent); }

  .pts-row { display: flex; gap: 8px; }
  .pts-btn {
    flex: 1; height: 52px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--card);
    color: var(--sub); font-size: 1.1rem; cursor: pointer; transition: all .15s;
  }
  .pts-btn.selected { background: var(--accent); border-color: var(--accent); color: var(--bg); font-weight: 700; }

  .submit-btn {
    width: 100%; padding: 18px; border-radius: 12px; border: none;
    background: var(--gold); color: var(--bg); font-size: 1.1rem;
    font-weight: 700; cursor: pointer; margin-top: 8px;
  }

  .done-box { text-align: center; padding: 60px 20px; }
  .done-box .icon { font-size: 3rem; margin-bottom: 16px; }
  .done-box h2 { color: var(--gold); margin-bottom: 10px; }
  .done-box p { color: var(--sub); font-size: .9rem; }
</style>
</head>
<body>

<header><h1>⭐ Google 評論加分審核</h1></header>
<div class="wrap">

{% if done %}
  <div class="done-box">
    {% if status == 'approved' %}
      <div class="icon">✅</div>
      <h2>加分完成！</h2>
      <p>{{ employee }} 已獲得 +{{ points }} 分<br>並已推播至群組</p>
    {% elif status == 'skipped' %}
      <div class="icon">⏭️</div>
      <h2>已略過此評論</h2>
      <p>未寫入績效紀錄</p>
    {% else %}
      <div class="icon">ℹ️</div>
      <h2>此評論已處理過</h2>
      <p>無需重複操作</p>
    {% endif %}
  </div>

{% else %}
  <div class="review-card">
    <div class="stars">{{ stars }}</div>
    <div class="reviewer">顧客：{{ reviewer }}</div>
    <div class="review-text">{{ review_text }}</div>
  </div>

  <form method="POST">
    <div class="form-group">
      <label>這筆好評要加給哪位員工？ *</label>
      <input type="text" name="employee" placeholder="輸入員工姓名" required
             list="names-list" autocomplete="off">
      <datalist id="names-list">
        {% for n in all_names %}<option value="{{ n }}">{% endfor %}
      </datalist>
    </div>

    <div class="form-group">
      <label>加幾分？（建議：{{ points }} 分）</label>
      <div class="pts-row">
        {% for p in [1, 2, 3, 4, 5] %}
        <button type="button" class="pts-btn {{ 'selected' if p == points else '' }}"
                onclick="selectPts(this, {{ p }})">{{ p }}</button>
        {% endfor %}
      </div>
      <input type="hidden" name="points" id="pts-input" value="{{ points }}">
    </div>

    <button type="submit" class="submit-btn">確認加分 🎉</button>
  </form>
{% endif %}

</div>
<script>
function selectPts(btn, val) {
  document.querySelectorAll('.pts-btn').forEach(b => b.classList.remove('selected'));
  btn.classList.add('selected');
  document.getElementById('pts-input').value = val;
}
</script>
</body>
</html>
"""

# ─── Web Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    tab = request.args.get("tab", "add")
    success = request.args.get("success")
    success_name = request.args.get("name", "")
    error = request.args.get("error")

    today_records = get_today_records()
    today_date = date.today().strftime("%Y/%m/%d")
    all_names = get_all_names()

    now = datetime.now()
    sel_ym = request.args.get("ym", now.strftime("%Y-%m"))
    year, month = int(sel_ym[:4]), int(sel_ym[5:])
    leaderboard = get_monthly_leaderboard(year, month)

    conn = sqlite3.connect(DB_PATH)
    available_months = [r[0] for r in conn.execute(
        "SELECT DISTINCT strftime('%Y-%m', created_at) AS ym FROM records ORDER BY ym DESC"
    ).fetchall()]
    conn.close()
    if sel_ym not in available_months and available_months:
        sel_ym = available_months[0]

    return render_template_string(
        TEMPLATE,
        tab=tab, success=success, success_name=success_name, error=error,
        today_records=today_records, today_date=today_date, all_names=all_names,
        leaderboard=leaderboard, available_months=available_months, sel_ym=sel_ym,
    )

@app.route("/submit", methods=["POST"])
def submit():
    employee = request.form.get("employee", "").strip()
    achievement = request.form.get("achievement", "").strip()
    recorder = request.form.get("recorder", "").strip()
    try:
        points = max(1, min(10, int(request.form.get("points", 1))))
    except ValueError:
        points = 1

    if not employee or not achievement:
        return redirect(url_for("index", tab="add", error="請填寫員工姓名與事蹟"))

    add_record(employee, achievement, points, recorder)
    return redirect(url_for("index", tab="add", success=1, name=employee))

# ─── Google 評論流程 ────────────────────────────────────────────────────────

@app.route("/review-incoming", methods=["POST"])
def review_incoming():
    """
    Make / Zapier 打過來的 webhook，JSON 格式：
    {
        "reviewer": "顧客姓名",     (選填，預設「匿名」)
        "review_text": "評論內容",  (必填)
        "rating": 5                 (選填，預設 5，整數 1–5)
    }
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    reviewer = str(data.get("reviewer", "匿名")).strip() or "匿名"
    review_text = str(data.get("review_text", "")).strip()
    try:
        rating = max(1, min(5, int(data.get("rating", 5))))
    except (ValueError, TypeError):
        rating = 5

    if not review_text:
        return {"ok": False, "error": "review_text 必填"}, 400

    points = rating_to_points(rating)
    review_id, token = add_pending_review(reviewer, review_text, rating, points)
    ok, resp = push_line_flex_review(review_id, reviewer, review_text, rating, points, token)

    return {"ok": ok, "review_id": review_id, "line_status": resp}

@app.route("/approve/<int:rid>", methods=["GET", "POST"])
def approve_review(rid):
    token = request.args.get("token", "")
    row = get_pending_review(rid)

    if not row:
        return "找不到此評論", 404

    r_id, reviewer, review_text, rating, points, r_token, status = row

    if token != r_token:
        return "連結無效或已過期", 403

    if status != "pending":
        return render_template_string(APPROVE_TEMPLATE, done=True, status=status,
                                      employee="", points=points)

    if request.method == "POST":
        employee = request.form.get("employee", "").strip()
        try:
            final_points = max(1, min(10, int(request.form.get("points", points))))
        except ValueError:
            final_points = points

        if not employee:
            return render_template_string(
                APPROVE_TEMPLATE, done=False, rid=rid, token=token,
                reviewer=reviewer, review_text=review_text, rating=rating,
                points=points, all_names=get_all_names(),
                stars="⭐" * rating, error="請填寫員工姓名"
            )

        stars_str = "⭐" * rating
        short = review_text[:50] + ("…" if len(review_text) > 50 else "")
        achievement = f"Google 好評（{stars_str}）：{short}"

        add_record(employee, achievement, final_points, "Google 評論")
        set_review_status(rid, "approved")
        push_line_group_review(reviewer, review_text, rating, employee, final_points)

        return render_template_string(APPROVE_TEMPLATE, done=True, status="approved",
                                      employee=employee, points=final_points)

    return render_template_string(
        APPROVE_TEMPLATE, done=False, rid=rid, token=token,
        reviewer=reviewer, review_text=review_text, rating=rating,
        points=points, all_names=get_all_names(), stars="⭐" * rating
    )

@app.route("/skip/<int:rid>")
def skip_review(rid):
    token = request.args.get("token", "")
    row = get_pending_review(rid)
    if not row:
        return "找不到此評論", 404
    if row[5] != token:
        return "連結無效", 403
    if row[6] == "pending":
        set_review_status(rid, "skipped")
    return render_template_string(APPROVE_TEMPLATE, done=True, status="skipped",
                                  employee="", points=0)

# ─── CLI ────────────────────────────────────────────────────────────────────

def cmd_send_daily():
    today = date.today().strftime("%Y/%m/%d")
    records = get_today_records()
    if not records:
        print(f"[{today}] 今日無紀錄，跳過")
        return
    msg = build_daily_msg(records, today)
    ok, resp = push_line(msg)
    if ok:
        print(f"[{today}] 推播成功！共 {len(records)} 筆")
    else:
        print(f"[{today}] 推播失敗：{resp}")

def cmd_send_monthly():
    now = datetime.now()
    if now.month == 1:
        year, month = now.year - 1, 12
    else:
        year, month = now.year, now.month - 1
    msg = build_monthly_msg(year, month)
    ok, resp = push_line(msg)
    if ok:
        print(f"[{year}/{month}] 月度風雲榜推播成功！")
    else:
        print(f"[{year}/{month}] 推播失敗：{resp}")

init_db()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "send-daily":
            cmd_send_daily()
        elif sys.argv[1] == "send-monthly":
            cmd_send_monthly()
        else:
            print("用法：python3 app.py [send-daily|send-monthly]")
    else:
        print("🚀  http://localhost:5001  開啟")
        app.run(host="0.0.0.0", port=5001, debug=False)
