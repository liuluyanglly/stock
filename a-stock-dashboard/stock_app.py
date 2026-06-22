#!/usr/bin/env python3
"""📈 A股全自动复盘看板 v5 — 修复板块资金零值 + 重组公告涨跌 + 涨跌统计"""
import sqlite3, json, subprocess, requests, os
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request
from datetime import datetime, timedelta

app = Flask(__name__)
BASE = Path("/home/ubuntu/drama-v2")
DB_PATH = BASE / "stock_review.db"
AKSHARE_PYTHON = "/tmp/akshare_env/bin/python3"

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_snapshots (
        snapshot_date TEXT PRIMARY KEY,
        market_data TEXT DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS ai_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_date TEXT,
        custom_prompt TEXT DEFAULT '',
        prediction TEXT DEFAULT '',
        prompt_len INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS ai_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        tags TEXT DEFAULT '[]',
        note TEXT DEFAULT '',
        score INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (prediction_id) REFERENCES ai_predictions(id)
    );
    """)
    conn.commit()
    conn.close()

def fetch_akshare(script, timeout=60):
    try:
        proc = subprocess.run([AKSHARE_PYTHON, "-c", script], capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
    except Exception as e:
        print(f"akshare error: {e}")
    return None

def _em_secid(code):
    """Convert A-share code to East Money secid (e.g. '1.600519', '0.000001')"""
    code = str(code).strip()
    if code.startswith(('6', '5')):
        return f"1.{code}"  # Shanghai
    else:
        return f"0.{code}"  # Shenzhen

def fetch_em_kline(code, period='daily', days=120):
    """Direct East Money HTTP API for K-line data. Returns list of records or None."""
    period_map = {'daily': '101', 'weekly': '102', 'monthly': '103'}
    klt = period_map.get(period, '101')
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _em_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": "1",  # 前复权
        "beg": start_date,
        "end": end_date,
        "lmt": "500",
        "ut": "fa5fd1943c7b386f172d6893dbbd1",
    }
    try:
        resp = requests.get(url, params=params, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None
        records = []
        for line in klines:
            # Format: "2025-01-02,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率"
            parts = line.split(",")
            if len(parts) < 11:
                continue
            records.append({
                "日期": parts[0],
                "开盘": float(parts[1]),
                "收盘": float(parts[2]),
                "最高": float(parts[3]),
                "最低": float(parts[4]),
                "成交量": float(parts[5]),
                "成交额": float(parts[6]),
                "涨跌幅": float(parts[8]),
            })
        return records if records else None
    except Exception as e:
        print(f"East Money kline fallback error: {e}")
        return None

def fetch_em_intraday(code):
    """Direct East Money HTTP API for intraday (1-min) data. Returns list of records or None."""
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _em_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "1",      # 1-minute
        "fqt": "0",      # 不复权 for intraday
        "beg": "0",
        "end": "20500101",
        "lmt": "300",    # last 300 bars
        "ut": "fa5fd1943c7b386f172d6893dbbd1",
    }
    try:
        resp = requests.get(url, params=params, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None
        records = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            records.append({
                "时间": parts[0],
                "开盘": float(parts[1]),
                "收盘": float(parts[2]),
                "最高": float(parts[3]),
                "最低": float(parts[4]),
                "成交量": float(parts[5]),
            })
        return records[-240:] if records else None
    except Exception as e:
        print(f"East Money intraday fallback error: {e}")
        return None

def fetch_all_market_data(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime('%Y%m%d')
    script = f'''
import akshare as ak
import json

result = {{"date": "{date_str}"}}

# 1. 涨停股（含首封时间、炸板次数、连板数、行业、封板资金）
try:
    df = ak.stock_zt_pool_em(date="{date_str}")
    records = []
    for _, r in df.iterrows():
        records.append({{
            "代码": str(r.get("代码","")),
            "名称": str(r.get("名称","")),
            "涨跌幅": float(r.get("涨跌幅",0)),
            "最新价": float(r.get("最新价",0)),
            "成交额": float(r.get("成交额",0)),
            "流通市值": float(r.get("流通市值",0)),
            "换手率": float(r.get("换手率",0)),
            "封板资金": float(r.get("封板资金",0)),
            "首次封板时间": str(r.get("首次封板时间","")),
            "最后封板时间": str(r.get("最后封板时间","")),
            "炸板次数": int(r.get("炸板次数",0)),
            "涨停统计": str(r.get("涨停统计","")),
            "连板数": int(r.get("连板数",1)),
            "所属行业": str(r.get("所属行业",""))
        }})
    result["limit_up"] = records
    result["limit_up_count"] = len(records)
except Exception as e:
    result["limit_up"] = []
    result["limit_up_count"] = 0
    result["limit_up_err"] = str(e)

# 2. 跌停股
try:
    df = ak.stock_zt_pool_dtgc_em(date="{date_str}")
    records = []
    for _, r in df.iterrows():
        records.append({{
            "代码": str(r.get("代码","")),
            "名称": str(r.get("名称","")),
            "涨跌幅": float(r.get("涨跌幅",0)),
            "最新价": float(r.get("最新价",0)),
            "成交额": float(r.get("成交额",0)),
            "换手率": float(r.get("换手率",0)),
            "封单资金": float(r.get("封单资金",0)),
            "最后封板时间": str(r.get("最后封板时间","")),
            "连续跌停": int(r.get("连续跌停",1)),
            "开板次数": int(r.get("开板次数",0)),
            "所属行业": str(r.get("所属行业",""))
        }})
    result["limit_down"] = records
    result["limit_down_count"] = len(records)
except Exception as e:
    result["limit_down"] = []
    result["limit_down_count"] = 0

# 3. 炸板股
try:
    df = ak.stock_zt_pool_zbgc_em(date="{date_str}")
    records = []
    for _, r in df.iterrows():
        records.append({{
            "代码": str(r.get("代码","")),
            "名称": str(r.get("名称","")),
            "涨跌幅": float(r.get("涨跌幅",0)),
            "最新价": float(r.get("最新价",0)),
            "涨停价": float(r.get("涨停价",0)),
            "成交额": float(r.get("成交额",0)),
            "换手率": float(r.get("换手率",0)),
            "首次封板时间": str(r.get("首次封板时间","")),
            "炸板次数": int(r.get("炸板次数",0)),
            "涨停统计": str(r.get("涨停统计","")),
            "振幅": float(r.get("振幅",0)),
            "所属行业": str(r.get("所属行业",""))
        }})
    result["broken_limit"] = records
    result["broken_limit_count"] = len(records)
except Exception as e:
    result["broken_limit"] = []
    result["broken_limit_count"] = 0

# 4. 板块资金流向（数值已经是亿元单位，前端不要再次除以1e8）
try:
    df = ak.stock_fund_flow_industry()
    records = []
    for _, r in df.iterrows():
        records.append({{
            "名称": str(r.get("行业","")),
            "今日涨跌幅": float(r.get("行业-涨跌幅",0)),
            "净额": float(r.get("净额",0)),
            "流入资金": float(r.get("流入资金",0)),
            "流出资金": float(r.get("流出资金",0)),
            "领涨股": str(r.get("领涨股","")),
            "领涨股-涨跌幅": float(r.get("领涨股-涨跌幅",0))
        }})
    result["sector_flow"] = records
except Exception as e:
    result["sector_flow"] = []
    result["sector_flow_err"] = str(e)

# 5. 涨跌统计（使用 legu 接口，更稳定）
try:
    df = ak.stock_market_activity_legu()
    stats = {{}}
    for _, r in df.iterrows():
        item = str(r.get("item",""))
        val = r.get("value","")
        if item == "上涨":
            stats["up"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "下跌":
            stats["down"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "平盘":
            stats["flat"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "涨停":
            stats["zt_total"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "跌停":
            stats["dt_total"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "真实涨停":
            stats["real_zt"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "真实跌停":
            stats["real_dt"] = int(val) if str(val).replace('.','').isdigit() else 0
        elif item == "活跃度":
            stats["activity"] = str(val)
    total = stats.get("up",0) + stats.get("down",0) + stats.get("flat",0)
    stats["total"] = total
    stats["up_ratio"] = round(stats.get("up",0)/total*100, 1) if total > 0 else 0
    stats["down_ratio"] = round(stats.get("down",0)/total*100, 1) if total > 0 else 0
    result["market_stats"] = stats
except Exception as e:
    result["market_stats"] = {{"err": str(e)}}

# 6. 重组/资产重组公告 + 查询每只股票最近交易日涨跌
# 6. 重组/资产重组公告 + 查询每只股票最近交易日涨跌
import time as _time
try:
    df = ak.stock_notice_report(symbol="资产重组", date="{date_str}")
    notice_codes = []
    records = []
    for _, r in df.head(30).iterrows():
        code = str(r.get("代码",""))
        records.append({{
            "代码": code,
            "名称": str(r.get("名称","")),
            "公告标题": str(r.get("公告标题","")),
            "公告类型": str(r.get("公告类型","")),
            "公告日期": str(r.get("公告日期","")),
            "网址": str(r.get("网址",""))
        }})
        if code:
            notice_codes.append(code)
    result["restructure"] = records
    # 查询每只重组股票最近交易日的涨跌（带延迟避免限流）
    stock_changes = {{}}
    for code in notice_codes[:15]:
        for _attempt in range(2):
            try:
                _time.sleep(1.5)
                hist = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="{date_str}", end_date="{date_str}", adjust="qfq")
                if hist is not None and len(hist) > 0:
                    row = hist.iloc[0]
                    stock_changes[code] = {{
                        "涨跌幅": float(row.get("涨跌幅", 0)),
                        "收盘": float(row.get("收盘", 0)),
                        "日期": str(row.get("日期", ""))
                    }}
                else:
                    hist2 = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
                    if hist2 is not None and len(hist2) > 0:
                        row = hist2.iloc[-1]
                        stock_changes[code] = {{
                            "涨跌幅": float(row.get("涨跌幅", 0)),
                            "收盘": float(row.get("收盘", 0)),
                            "日期": str(row.get("日期", "")) + "(非当日)"
                        }}
                break
            except:
                if _attempt == 0:
                    _time.sleep(3)
    result["stock_changes"] = stock_changes
except Exception as e:
    result["restructure"] = []
    result["restructure_err"] = str(e)


# 7. 热点新闻（7x24快讯）
try:
    df = ak.stock_info_global_em()
    records = []
    for _, r in df.head(200).iterrows():
        records.append({{
            "标题": str(r.get("标题","")),
            "摘要": str(r.get("摘要",""))[:200],
            "发布时间": str(r.get("发布时间","")),
            "链接": str(r.get("链接",""))
        }})
    result["news"] = records
except Exception as e:
    result["news"] = []

print(json.dumps(result, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=120)
    if data:
        # 自动判断一字板
        for s in data.get("limit_up", []):
            s["is_yizi"] = s.get("换手率", 100) < 0.5 and s.get("炸板次数", 0) == 0
        return data
    return {"date": date_str, "limit_up": [], "limit_down": [], "broken_limit": [], "sector_flow": [], "market_stats": {}, "restructure": [], "stock_changes": {}}

# ═══════════════════════════════════════
# HTML
# ═══════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📈 A股复盘看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<script src="https://unpkg.com/lucide@latest"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#f5f5f7;
  --sf:rgba(255,255,255,.72);
  --sf-solid:#ffffff;
  --sf2:#f2f2f7;
  --sf3:#e5e5ea;
  --bd:rgba(0,0,0,.06);
  --bd2:rgba(0,0,0,.1);
  --tx:#1d1d1f;
  --tx2:#6e6e73;
  --tx3:#aeaeb2;
  --ac:#007aff;
  --ac2:#5ac8fa;
  --gn:#34c759;
  --rd:#ff3b30;
  --bl:#007aff;
  --pu:#af52de;
  --or:#ff9500;
  --r:16px;
  --r-sm:10px;
  --r-xs:8px;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
  --shadow-md:0 4px 12px rgba(0,0,0,.08),0 1px 3px rgba(0,0,0,.04);
  --shadow-lg:0 8px 30px rgba(0,0,0,.12);
  --blur:saturate(180%) blur(20px);
}
:root[data-theme="dark"]{
  --bg:#000000;
  --sf:rgba(28,28,30,.72);
  --sf-solid:#1c1c1e;
  --sf2:#2c2c2e;
  --sf3:#3a3a3c;
  --bd:rgba(255,255,255,.08);
  --bd2:rgba(255,255,255,.12);
  --tx:#f5f5f7;
  --tx2:#98989d;
  --tx3:#636366;
  --ac:#0a84ff;
  --ac2:#64d2ff;
  --gn:#30d158;
  --rd:#ff453a;
  --bl:#0a84ff;
  --pu:#bf5af2;
  --or:#ff9f0a;
  --shadow:0 1px 3px rgba(0,0,0,.3);
  --shadow-md:0 4px 12px rgba(0,0,0,.4);
  --shadow-lg:0 8px 30px rgba(0,0,0,.5);
}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;background:var(--bg);color:var(--tx);line-height:1.5;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--tx3);border-radius:3px}

/* ===== Layout ===== */
.app-layout{display:flex;height:100vh;overflow:hidden;background:var(--bg)}
.sidebar{width:220px;min-width:220px;background:var(--sf);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
.sidebar-logo{padding:20px 20px;font-size:17px;font-weight:700;color:var(--tx);border-bottom:1px solid var(--bd);white-space:nowrap;display:flex;align-items:center;gap:10px}
.sidebar-logo span{font-size:22px}
.sidebar-nav{flex:1;padding:8px 12px}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;font-size:13px;font-weight:500;color:var(--tx2);cursor:pointer;transition:all .2s cubic-bezier(.4,0,.2,1);border-radius:var(--r-sm);white-space:nowrap;margin-bottom:2px}
.nav-item:hover{background:var(--sf2);color:var(--tx)}
.nav-item.active{background:var(--ac);color:#fff;font-weight:600;box-shadow:0 2px 8px rgba(0,122,255,.3)}
.nav-item .nav-icon{width:20px;height:20px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.nav-item .nav-icon svg{width:16px;height:16px;stroke-width:1.75}
.nav-item .nav-badge{margin-left:auto;font-size:11px;color:var(--tx3);background:var(--sf2);padding:2px 8px;border-radius:10px;font-weight:600}
.nav-item.active .nav-badge{background:rgba(255,255,255,.2);color:rgba(255,255,255,.9)}
.nav-divider{height:1px;background:var(--bd);margin:8px 12px}
.sidebar-footer{padding:12px 16px;border-top:1px solid var(--bd);font-size:11px;color:var(--tx3);display:flex;align-items:center;gap:8px}

/* ===== Main Area ===== */
.main-area{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.main-header{display:flex;justify-content:flex-end;align-items:center;gap:12px;padding:12px 24px;border-bottom:1px solid var(--bd);background:var(--sf);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);flex-shrink:0}
.main-content{flex:1;overflow-y:auto;padding:20px 24px;min-height:0}

/* ===== Buttons ===== */
.btn{padding:7px 16px;border-radius:var(--r-sm);border:1px solid var(--bd2);background:var(--sf-solid);color:var(--tx);cursor:pointer;font-size:12px;font-weight:500;transition:all .2s cubic-bezier(.4,0,.2,1);font-family:inherit;box-shadow:var(--shadow)}
.btn:hover{background:var(--sf2);box-shadow:var(--shadow-md);transform:translateY(-1px)}
.btn:active{transform:scale(.97);box-shadow:var(--shadow)}
.btn-primary{background:var(--ac);color:#fff;border-color:transparent;box-shadow:0 2px 8px rgba(0,122,255,.25)}
.btn-primary:hover{background:#0066d6;box-shadow:0 4px 12px rgba(0,122,255,.35)}

/* ===== Stats Cards ===== */
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px}
.stat{background:var(--sf-solid);border-radius:var(--r);padding:16px;text-align:center;box-shadow:var(--shadow);border:1px solid var(--bd);transition:all .2s}
.stat:hover{box-shadow:var(--shadow-md);transform:translateY(-2px)}
.stat-v{font-size:28px;font-weight:700;color:var(--ac);letter-spacing:-0.5px}.stat-l{font-size:11px;color:var(--tx3);margin-top:4px;font-weight:500}
.stat-v.up{color:var(--rd)}.stat-v.dn{color:var(--gn)}

/* ===== Cards ===== */
.card{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;margin-bottom:16px;box-shadow:var(--shadow)}
.card-h{padding:14px 18px;border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center}
.card-t{font-size:14px;font-weight:600;color:var(--tx)}.card-c{background:var(--sf2);padding:2px 10px;border-radius:12px;font-size:11px;color:var(--tx2);font-weight:500}
.card-b{max-height:600px;overflow-y:auto}

/* ===== Table ===== */
.tbl{width:100%;border-collapse:collapse;font-size:12px;transform:translateZ(0)}
.tbl th{padding:10px 14px;text-align:left;color:var(--tx3);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--sf-solid);z-index:1}
.tbl td{padding:10px 14px;border-bottom:1px solid var(--bd);transition:background .15s}
.tbl tr{transition:background .15s}
.tbl tr:hover{background:var(--sf2)}
.tbl tr:hover td{background:var(--sf2)}
.tbl .yizi{color:var(--or);font-weight:600}

/* ===== Tags ===== */
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;letter-spacing:.3px}

/* ===== Animations ===== */
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}

/* ===== Sub Tabs ===== */
.sub-tabs{display:flex;gap:4px;margin-bottom:16px;padding:4px;background:var(--sf2);border-radius:var(--r-sm);overflow-x:auto}
.sub-tab{padding:8px 16px;font-size:12px;font-weight:500;color:var(--tx2);cursor:pointer;transition:all .2s;border-radius:var(--r-xs);white-space:nowrap}
.sub-tab:hover{background:var(--sf);color:var(--tx)}
.sub-tab.active{background:var(--sf-solid);color:var(--ac);box-shadow:var(--shadow)}
.tag-yz{background:rgba(255,149,0,.12);color:var(--or)}
.tag-hl{background:rgba(255,59,48,.12);color:var(--rd)}
.tag-gn{background:rgba(52,199,89,.12);color:var(--gn)}
.concept-tag{display:inline-block;padding:2px 6px;background:rgba(0,122,255,.08);border-radius:6px;font-size:10px;margin:2px;cursor:pointer;color:var(--bl);font-weight:500;transition:all .15s}
.concept-tag:hover{background:rgba(0,122,255,.18);transform:scale(1.02)}
.concept-tag.hot{background:rgba(255,59,48,.1);color:var(--rd)}

/* ===== Text Colors ===== */
.up{color:var(--rd)}.dn{color:var(--gn)}.fl{color:var(--tx3)}

/* ===== Loading & Empty ===== */
.empty{text-align:center;padding:60px 20px;color:var(--tx3);font-size:14px;font-weight:500}
.loading{text-align:center;padding:40px;color:var(--tx3);font-size:13px}
.loading::after{content:'';display:inline-block;width:18px;height:18px;border:2px solid var(--sf3);border-top-color:var(--ac);border-radius:50%;animation:spin .7s linear infinite;margin-left:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.auto-dot{width:8px;height:8px;border-radius:50%;background:var(--gn);display:inline-block;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ===== News ===== */
.news-scroll{max-height:600px;overflow-y:auto}
.news-item{padding:14px 18px;border-bottom:1px solid var(--bd);font-size:13px;cursor:pointer;transition:all .15s}
.news-item:hover{background:var(--sf2)}
.news-item:last-child{border-bottom:none}
.news-time{font-size:11px;color:var(--tx3);margin-bottom:4px;font-weight:500}
.news-title{font-weight:600;color:var(--tx);line-height:1.5}
.news-summary{font-size:12px;color:var(--tx2);margin-top:6px;line-height:1.6}

/* ===== Sector Cards ===== */
.sector-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:16px;max-height:700px}
.sector-card{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;transition:all .2s cubic-bezier(.4,0,.2,1);box-shadow:var(--shadow)}
.sector-card:hover{box-shadow:var(--shadow-md);transform:translateY(-2px);border-color:var(--ac)}
.sector-card.net-in{border-left:4px solid var(--rd)}
.sector-card.net-out{border-left:4px solid var(--gn)}
.sc-top{display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:8px}
.sc-name{font-weight:600;color:var(--tx);font-size:13px}
.sc-mid{margin-bottom:6px}
.sc-bot{font-size:11px;color:var(--tx3)}

/* ===== Sector Items ===== */
.sc-item{padding:12px 18px;border-bottom:1px solid var(--bd);transition:all .15s}
.sc-item:last-child{border-bottom:none}
.sc-item:hover{background:var(--sf2)}
.sc-item.sc-in{border-left:4px solid var(--rd)}
.sc-item.sc-out{border-left:4px solid var(--gn)}
.sc-row1{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.sc-name{font-weight:600;font-size:13px}
.sc-row2{display:flex;gap:12px;font-size:11px;color:var(--tx3);flex-wrap:wrap}

/* ===== History Items ===== */
.hist-item{padding:14px 18px;border-bottom:1px solid var(--bd);cursor:pointer;transition:all .2s}
.hist-item:hover{background:var(--sf2)}.hist-item:last-child{border-bottom:none}
.hist-date{font-weight:700;color:var(--ac);font-size:15px}
.hist-nums{display:flex;gap:20px;margin-top:8px;font-size:12px;color:var(--tx2)}

/* ===== Sector Flow ===== */
.sector-flow-item{display:flex;justify-content:space-between;align-items:center;padding:10px 18px;border-bottom:1px solid var(--bd);font-size:13px;transition:all .15s}
.sector-flow-item:hover{background:var(--sf2)}
.sector-flow-item:last-child{border-bottom:none}

/* ===== Restructure Items ===== */
.rs-item{padding:14px 18px;border-bottom:1px solid var(--bd);font-size:13px}
.rs-item:last-child{border-bottom:none}
.rs-header{display:flex;justify-content:space-between;align-items:center}
.rs-name{font-weight:600}.rs-code{color:var(--tx3);font-size:11px;margin-left:6px}
.rs-info{display:flex;gap:12px;margin-top:8px;font-size:11px;color:var(--tx2);flex-wrap:wrap}

/* ===== Portfolio ===== */
.pf-input{padding:8px 12px;border:1px solid var(--bd2);border-radius:var(--r-sm);background:var(--sf-solid);color:var(--tx);font-size:12px;font-family:inherit;transition:all .2s}
.pf-input:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(0,122,255,.15)}
.pf-item{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;border-bottom:1px solid var(--bd);gap:10px}
.pf-item:last-child{border-bottom:none}
.pf-item.pf-zt{background:rgba(255,149,0,.05);border-left:4px solid var(--or)}
.pf-main{display:flex;align-items:center;gap:10px;min-width:120px}
.pf-code{font-weight:600;color:var(--bl);font-size:13px}
.pf-name{color:var(--tx);font-size:13px}
.pf-detail{display:flex;gap:14px;font-size:12px;color:var(--tx3);flex:1}

/* ===== Concept Groups ===== */
.cg-group{border:1px solid var(--bd);border-radius:var(--r);margin-bottom:12px;overflow:hidden;box-shadow:var(--shadow)}
.cg-header{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;background:var(--sf2);border-bottom:1px solid var(--bd)}
.cg-name{font-weight:600;color:var(--ac);font-size:14px}
.cg-name-en{color:var(--tx3);font-size:12px;margin-left:6px}
.cg-stocks{padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px}
.cg-stock{display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r-xs);font-size:12px;transition:all .15s}
.cg-stock:hover{border-color:var(--ac);box-shadow:var(--shadow)}
.cg-stock-code{color:var(--bl);font-weight:600}
.cg-stock-name{color:var(--tx2)}

/* ===== AI Components ===== */
.ai-info{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);padding:20px;margin-bottom:16px}
.ai-info-title{font-weight:600;color:var(--ac);margin-bottom:10px;font-size:15px}
.ai-stats{display:flex;gap:20px;flex-wrap:wrap;font-size:13px;margin-bottom:10px}
.ai-stats b{color:var(--ac)}
.ai-desc{font-size:12px;color:var(--tx3);line-height:1.7}
.ai-btn{width:100%;padding:16px;border:none;border-radius:var(--r);background:linear-gradient(135deg,#007aff,#5856d6);color:#fff;font-size:17px;font-weight:700;cursor:pointer;transition:all .3s;letter-spacing:.5px;font-family:inherit;box-shadow:0 4px 15px rgba(0,122,255,.3)}
.ai-btn:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,122,255,.4)}
.ai-btn:active{transform:scale(.98)}
.ai-btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.ai-loading{text-align:center;padding:40px;color:var(--tx3);font-size:14px}
.ai-loading-bar{width:200px;height:4px;background:var(--sf3);border-radius:2px;margin:14px auto 0;overflow:hidden}
.ai-loading-fill{width:100%;height:100%;background:linear-gradient(90deg,var(--ac),var(--pu));border-radius:2px;animation:loadBar 1.5s ease-in-out infinite}
@keyframes loadBar{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
.ai-error{padding:24px;color:var(--rd);text-align:center;font-size:14px;background:rgba(255,59,48,.06);border-radius:var(--r);border:1px solid rgba(255,59,48,.15)}
.ai-result{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow)}
.ai-result-header{padding:14px 18px;border-bottom:1px solid var(--bd);font-weight:600;color:var(--ac);font-size:15px}
.ai-result-body{padding:20px;font-size:13px;line-height:1.8;color:var(--tx)}
.ai-h2{color:var(--ac);font-size:17px;margin:20px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--bd)}
.ai-h3{color:var(--pu);font-size:15px;margin:14px 0 8px}
.ai-h4{color:var(--tx);font-size:13px;margin:10px 0 6px;font-weight:600}
.ai-li{padding:3px 0;padding-left:10px;color:var(--tx2)}
.ai-custom{margin-bottom:16px}
.ai-custom-title{font-size:13px;font-weight:600;color:var(--tx2);margin-bottom:8px}
.ai-textarea{width:100%;padding:14px;border:1px solid var(--bd2);border-radius:var(--r-sm);background:var(--sf-solid);color:var(--tx);font-size:13px;line-height:1.6;resize:vertical;font-family:inherit;transition:all .2s}
.ai-textarea:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(0,122,255,.12)}
.ai-textarea::placeholder{color:var(--tx3)}
.ai-checks{display:flex;flex-wrap:wrap;gap:10px 18px;font-size:13px;margin-bottom:6px}
.ai-ck{display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--tx2);font-weight:500}
.ai-ck input[type=checkbox]{width:18px;height:18px;accent-color:var(--ac);cursor:pointer;border-radius:4px}
.ai-ck b{color:var(--ac)}

/* ===== AI History ===== */
.ai-hist-item{padding:14px 18px;border-bottom:1px solid var(--bd);cursor:pointer;transition:all .2s}
.ai-hist-item:hover{background:var(--sf2)}.ai-hist-item:last-child{border-bottom:none}
.ai-hist-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.ai-hist-date{font-weight:600;color:var(--ac);font-size:14px}
.ai-hist-time{font-size:11px;color:var(--tx3)}
.ai-hist-prompt{font-size:11px;color:var(--bl);margin-bottom:6px;font-style:italic}
.ai-hist-preview{font-size:12px;color:var(--tx3);line-height:1.5}

/* ===== AI Detail View ===== */
.aidv-section{margin-bottom:16px;border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow)}
.aidv-section .tbl{max-height:300px;display:block;overflow-y:auto}
.aidv-title{padding:10px 14px;background:var(--sf2);font-weight:600;font-size:12px;color:var(--ac);border-bottom:1px solid var(--bd)}
.ai-code{background:rgba(0,122,255,.1);color:var(--bl);padding:2px 6px;border-radius:6px;font-weight:600;font-size:12px}
.ai-hot{background:rgba(255,149,0,.1);color:var(--or);padding:2px 6px;border-radius:6px;font-weight:600}
.ai-risk{background:rgba(255,59,48,.1);color:var(--rd);padding:2px 6px;border-radius:6px;font-weight:600}
.ai-op{background:rgba(175,82,222,.1);color:var(--pu);padding:2px 6px;border-radius:6px;font-weight:600}
.ai-num{color:var(--ac2);font-weight:700}
.ai-sent{background:rgba(52,199,89,.1);color:var(--gn);padding:2px 6px;border-radius:6px}

/* ===== AI Collapsible ===== */
.ai-collapse{margin-bottom:16px;border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow)}
.ai-collapse-header{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;background:var(--sf2);cursor:pointer;user-select:none;transition:background .2s}
.ai-collapse-header:hover{background:var(--sf3)}
.ai-collapse-header span{font-size:13px;font-weight:600;color:var(--tx2)}
.ai-collapse-arrow{font-size:12px;color:var(--tx3);transition:transform .3s cubic-bezier(.4,0,.2,1)}
.ai-collapse-arrow.open{transform:rotate(180deg)}
.ai-collapse-body{max-height:0;overflow:hidden;transition:max-height .4s cubic-bezier(.4,0,.2,1),padding .3s;padding:0 18px}
.ai-collapse-body.open{max-height:600px;padding:14px 18px}

/* ===== AI Think Box ===== */
.ai-think-toggle{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--tx2);cursor:pointer;margin-bottom:10px;font-weight:500}
.ai-think-toggle input{accent-color:var(--pu);width:16px;height:16px}
.ai-think-box{background:rgba(175,82,222,.04);border:1px solid rgba(175,82,222,.12);border-radius:var(--r);margin-bottom:14px;overflow:hidden;display:none}
.ai-think-box.show{display:block}
.ai-think-header{padding:10px 14px;font-size:12px;font-weight:600;color:var(--pu);cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.ai-think-header:hover{background:rgba(175,82,222,.06)}
.ai-think-body{padding:0 14px 12px;font-size:12px;line-height:1.8;color:var(--tx3);max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;display:none}
.ai-think-body.open{display:block}

/* ===== AI Streaming ===== */
.ai-stream-cursor::after{content:'▊';animation:blink 1s infinite;color:var(--ac)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.ai-stream-status{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--tx3);margin-top:8px}
.ai-stream-dot{width:8px;height:8px;border-radius:50%;background:var(--ac);animation:pulse 1.5s infinite}

/* ===== AI Feedback ===== */
.ai-feedback{margin-top:20px;border-top:1px solid var(--bd);padding-top:18px}
.ai-feedback-title{font-size:12px;color:var(--tx3);margin-bottom:10px;font-weight:500}
.ai-feedback-tags{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.ai-fb-tag{padding:6px 14px;border-radius:20px;border:1px solid var(--bd2);background:var(--sf-solid);cursor:pointer;font-size:13px;transition:all .2s;user-select:none;font-weight:500}
.ai-fb-tag:hover{border-color:var(--ac);background:rgba(0,122,255,.06);transform:scale(1.02)}
.ai-fb-tag.active{border-color:var(--ac);background:rgba(0,122,255,.1);box-shadow:0 0 0 3px rgba(0,122,255,.1)}
.ai-fb-note{width:100%;padding:10px 14px;border:1px solid var(--bd2);border-radius:var(--r-sm);background:var(--sf-solid);color:var(--tx);font-size:12px;resize:none;font-family:inherit;margin-bottom:10px;transition:all .2s}
.ai-fb-note:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(0,122,255,.12)}
.ai-fb-submit{padding:8px 24px;border:none;border-radius:var(--r-sm);background:var(--ac);color:#fff;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;font-family:inherit;box-shadow:0 2px 8px rgba(0,122,255,.25)}
.ai-fb-submit:hover{background:#0066d6;box-shadow:0 4px 12px rgba(0,122,255,.35)}
.ai-fb-done{padding:10px 14px;background:rgba(52,199,89,.08);border:1px solid rgba(52,199,89,.2);border-radius:var(--r-sm);font-size:12px;color:var(--gn);font-weight:500}
.ai-fb-score{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:14px;font-size:11px;font-weight:600}
.ai-fb-score.pos{background:rgba(52,199,89,.12);color:var(--gn)}
.ai-fb-score.neg{background:rgba(255,59,48,.12);color:var(--rd)}
.ai-fb-score.zero{background:var(--sf2);color:var(--tx3)}
.ai-fb-tags-display{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.ai-fb-tag-sm{padding:3px 10px;border-radius:12px;font-size:11px;background:var(--sf2);color:var(--tx2);font-weight:500}
.ai-fb-tag[data-weight]{position:relative}
.ai-fb-tag[data-weight]::after{content:'+' attr(data-weight);position:absolute;top:-6px;right:-6px;font-size:9px;background:var(--ac);color:#fff;border-radius:8px;padding:1px 5px;font-weight:700;line-height:14px;display:none}
.ai-fb-tag[data-weight]:hover::after,.ai-fb-tag.active[data-weight]::after{display:block}
.ai-fb-tag[data-weight^="-"]::after{background:var(--rd)}
.ai-fb-stars{display:flex;gap:6px;margin:10px 0}
.ai-fb-star{font-size:28px;cursor:pointer;color:var(--sf3);transition:all .2s;user-select:none}
.ai-fb-star:hover,.ai-fb-star.on{color:var(--or);transform:scale(1.2)}
.ai-fb-star.on{text-shadow:0 0 12px rgba(255,149,0,.4)}

/* ===== AI Stats Bar ===== */
.ai-stats-bar{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.ai-stat-item{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);padding:14px 18px;text-align:center;min-width:90px;box-shadow:var(--shadow)}
.ai-stat-num{font-size:22px;font-weight:700}
.ai-stat-label{font-size:11px;color:var(--tx3);margin-top:4px;font-weight:500}

/* ===== Sector Stats ===== */
.ss-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:16px}
.ss-box{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);padding:20px;box-shadow:var(--shadow)}
.ss-box-title{font-size:13px;font-weight:600;color:var(--tx2);margin-bottom:10px}
.ss-box-num{font-size:40px;font-weight:800;margin-bottom:10px;letter-spacing:-1px}
.ss-box-detail{font-size:12px;color:var(--tx3);line-height:1.8}
.ss-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.ss-summary{background:var(--sf-solid);border:1px solid var(--bd);border-radius:var(--r);padding:20px;display:flex;gap:28px;flex-wrap:wrap;font-size:13px;box-shadow:var(--shadow)}
.ss-bar-list{display:flex;flex-direction:column;gap:6px}
.ss-bar-row{display:flex;align-items:center;gap:10px;font-size:12px}
.ss-bar-name{min-width:90px;text-align:right;color:var(--tx2);font-weight:500}
.ss-bar-track{flex:1;height:20px;background:var(--sf2);border-radius:6px;overflow:hidden}
.ss-bar-fill{height:100%;border-radius:6px;transition:.4s cubic-bezier(.4,0,.2,1)}
.ss-bar-fill.up{background:linear-gradient(90deg,rgba(255,59,48,.2),var(--rd))}
.ss-bar-fill.dn{background:linear-gradient(90deg,rgba(52,199,89,.2),var(--gn))}

/* ===== Tabs ===== */
.tabs{display:flex;gap:2px;margin-bottom:16px;border-bottom:1px solid var(--bd);padding-bottom:0;overflow-x:auto}
.tab{padding:10px 16px;border:none;background:transparent;color:var(--tx3);cursor:pointer;font-size:13px;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500;transition:all .2s;font-family:inherit}
.tab:hover{color:var(--tx2)}.tab.on{color:var(--ac);border-bottom-color:var(--ac);font-weight:600}
.tp{display:none}.tp.on{display:block}

/* ===== Mobile ===== */
.mobile-menu-btn{display:none;position:fixed;top:12px;left:12px;z-index:1000;width:36px;height:36px;border-radius:10px;background:var(--sf);border:1px solid var(--bd);font-size:16px;cursor:pointer;box-shadow:var(--shadow)}
@media(max-width:1024px){.stats{grid-template-columns:repeat(3,1fr)}.sidebar{width:60px;min-width:60px}.sidebar-logo{font-size:0;padding:12px 0;text-align:center}.sidebar-logo span:first-child{font-size:18px}.nav-item{justify-content:center;padding:10px 0}.nav-item span:not(.nav-icon){display:none}.nav-item .nav-badge{display:none}.nav-divider{margin:4px 8px}}
@media(max-width:768px){.sidebar{display:none}.mobile-menu-btn{display:block}.main-content{padding:12px}.stats{grid-template-columns:repeat(2,1fr)}.tbl{font-size:11px}.tbl th,.tbl td{padding:6px 8px}}

/* ===== Chat Interface ===== */
.chat-wrap{display:flex;flex-direction:column;flex:1;min-height:0;position:relative}
.chat-messages{flex:1;overflow-y:auto;padding:16px 20px 8px}
.chat-input-area{border-top:1px solid var(--bd);background:var(--sf);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);padding:12px 20px;flex-shrink:0;position:sticky;bottom:0;z-index:10}
.chat-msg.user{flex-direction:row-reverse;margin-left:auto}
.chat-msg.assistant{flex-direction:row}
.chat-avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.chat-msg.user .chat-avatar{background:rgba(0,122,255,.12)}
.chat-msg.assistant .chat-avatar{background:rgba(175,82,222,.12)}
.chat-bubble{padding:12px 16px;border-radius:var(--r);font-size:13px;line-height:1.7}
.chat-msg.user .chat-bubble{background:rgba(0,122,255,.08);border:1px solid rgba(0,122,255,.15);color:var(--tx)}
.chat-msg.assistant .chat-bubble{background:var(--sf-solid);border:1px solid var(--bd);color:var(--tx);box-shadow:var(--shadow)}
.chat-bubble h3,.chat-bubble h4{color:var(--ac);margin:8px 0 4px}
.chat-bubble h3{font-size:14px}
.chat-bubble h4{font-size:13px}
.chat-bubble ul,.chat-bubble ol{padding-left:16px;margin:4px 0}
.chat-bubble li{margin:2px 0}
.chat-time{font-size:10px;color:var(--tx3);margin-top:4px;text-align:right}
.chat-msg.assistant .chat-time{text-align:left}
.chat-config-row{display:flex;flex-wrap:wrap;gap:6px 12px;margin-bottom:8px;font-size:12px}
.chat-config-row label{display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--tx2);padding:3px 8px;border-radius:var(--r-xs);transition:.15s}
.chat-config-row label:hover{background:var(--sf2);color:var(--tx)}
.chat-config-row input[type=checkbox]{width:14px;height:14px;accent-color:var(--ac);cursor:pointer}
.chat-config-row label b{color:var(--ac)}
.chat-input-row{display:flex;gap:8px;align-items:flex-end}
.chat-input-row textarea{flex:1;padding:10px 14px;border:1px solid var(--bd2);border-radius:var(--r-sm);background:var(--sf-solid);color:var(--tx);font-size:13px;line-height:1.5;resize:none;max-height:120px;min-height:40px;font-family:inherit;transition:all .2s}
.chat-input-row textarea:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(0,122,255,.12)}
.chat-send-btn{padding:10px 20px;border:none;border-radius:var(--r-sm);background:linear-gradient(135deg,#007aff,#5856d6);color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap;font-family:inherit;box-shadow:0 2px 8px rgba(0,122,255,.25)}
.chat-send-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,122,255,.35)}
.chat-send-btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.chat-typing{display:flex;gap:4px;padding:8px 0}
.chat-typing span{width:6px;height:6px;border-radius:50%;background:var(--tx3);animation:typingDot 1.2s infinite}
.chat-typing span:nth-child(2){animation-delay:.2s}
.chat-typing span:nth-child(3){animation-delay:.4s}
@keyframes typingDot{0%,100%{opacity:.3;transform:scale(.8)}50%{opacity:1;transform:scale(1)}}
.chat-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--tx3);text-align:center;padding:40px}
.chat-empty-icon{font-size:48px;margin-bottom:12px;opacity:.5}
.chat-empty-text{font-size:14px;margin-bottom:4px}
.chat-empty-sub{font-size:12px;color:var(--tx3)}
.chat-feedback{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.chat-fb-tag{padding:4px 10px;border-radius:16px;border:1px solid var(--bd);background:var(--sf2);cursor:pointer;font-size:12px;transition:.2s}
.chat-fb-tag:hover{border-color:var(--ac);background:rgba(0,122,255,.08)}
.chat-fb-tag.active{border-color:var(--ac);background:rgba(0,122,255,.15)}
.chat-stars{display:flex;gap:4px;margin-top:6px}
.chat-star{font-size:20px;cursor:pointer;color:var(--sf3);transition:.15s}
.chat-star:hover,.chat-star.on{color:var(--or);transform:scale(1.1)}
.chat-star.on{text-shadow:0 0 8px rgba(255,149,0,.4)}
.chat-prompt-toggle{font-size:12px;color:var(--tx3);cursor:pointer;margin-bottom:4px;display:inline-block}
.chat-prompt-toggle:hover{color:var(--ac)}
.chat-prompt-preview{font-size:11px;color:var(--tx3);font-style:italic;padding:8px 12px;background:var(--sf2);border-radius:var(--r-xs);margin-bottom:8px;white-space:pre-wrap;max-height:120px;overflow-y:auto;display:none}
.chat-prompt-preview.show{display:block}
#tabContent{display:flex;flex-direction:column;flex:1;min-height:0}
#tabContent>.tab-full{flex:1;display:flex;flex-direction:column;margin-bottom:0;min-height:0}
#tabContent>.tab-full>.card{flex:1;display:flex;flex-direction:column;min-height:0}
#tabContent .card-b{flex:1;overflow-y:auto;min-height:0}
#tabContent .tbl{flex:1}
#tabContent .empty{flex:1;display:flex;align-items:center;justify-content:center}
.hist-detail{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);padding:20px;margin-top:10px;font-size:13px;line-height:1.8;white-space:pre-wrap;max-height:500px;overflow-y:auto}

/* ===== Enhanced Table Styling ===== */
.tbl th[style*="cursor:pointer"]:hover{color:var(--ac);background:var(--sf2)}
.tbl tbody tr{transition:background .15s,transform .1s}
.tbl tbody tr:hover{background:var(--sf2)}
.tbl td:first-child{font-weight:500}

/* ===== Responsive: Mobile sidebar overlay ===== */
@media(max-width:768px){
  .sidebar{position:fixed;left:-200px;top:0;height:100%;z-index:100;transition:left .3s;box-shadow:4px 0 20px rgba(0,0,0,.3)}
  .sidebar.open{left:0}
  .main-area{width:100%}
  .mobile-menu-btn{display:flex!important}
  .chat-config-row{gap:4px 8px}
  .chat-config-row label{font-size:11px;padding:2px 6px}
}
@media(min-width:769px){.mobile-menu-btn{display:none!important}}
.mobile-menu-btn{position:fixed;bottom:16px;right:16px;z-index:99;width:44px;height:44px;border-radius:50%;background:var(--ac);color:#fff;border:none;font-size:20px;cursor:pointer;box-shadow:0 4px 16px rgba(0,122,255,.35);display:none;align-items:center;justify-content:center}

/* ===== Modal backdrop fix ===== */
#stockDetailModal{backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
#stockDetailModal>div{animation:modalIn .25s cubic-bezier(.4,0,.2,1)}
@keyframes modalIn{from{opacity:0;transform:scale(.95) translateY(10px)}to{opacity:1;transform:scale(1) translateY(0)}}

/* ===== Sort indicator styling ===== */
.sort-arrow{font-size:10px;margin-left:2px;opacity:.6}

/* ===== Key News section styling ===== */
#homeKeyNews .tbl td{padding:8px 12px}
#homeKeyNews .tbl tr:hover td{color:var(--rd)!important}
</style>
</head>
<body>
<div class="app-layout">
  <aside class="sidebar">
    <div class="sidebar-logo"><i data-lucide="bar-chart-3" style="width:22px;height:22px;stroke-width:1.75"></i> A股复盘看板</div>
    <nav class="sidebar-nav" id="sidebarNav"></nav>
    <div class="sidebar-footer">
      <span id="dataDate">--</span>
      <button class="btn" onclick="refresh()" style="font-size:11px;padding:3px 8px">🔄</button>
    </div>
  </aside>
  <div class="main-area">
    <div class="main-header">
      <span class="badge badge-off" id="badge">--</span>
      <span id="updateTime" style="color:var(--tx3);font-size:12px">--</span>
      <button class="btn" onclick="refresh()"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新</button>
      <button class="btn" onclick="toggleTheme()" id="themeBtn" style="font-size:16px;padding:4px 8px" title="切换主题"><i data-lucide="moon" style="width:16px;height:16px"></i></button>
    </div>
    <div class="main-content" id="mainContent">
      <div id="tabContent"></div>
    </div>
  </div>
</div>
<script>
let D={};let timer=null;let curTab='home';let lastRefresh=0;
let conceptStockQuotes={}; // 存储分组概念股票的实时行情
let stockConceptsCache={}; // 股票所属概念缓存
let subTabs={}; // 子标签状态

// 批量获取股票所属概念
// 热门概念缓存
let hotConceptsCache = new Set();

// 计算热门概念（从涨停股中统计出现频率最高的概念）
function calculateHotConcepts(){
  const conceptCount = {};
  const allStocks = [...(D.limit_up||[]), ...(D.broken_limit||[])];
  
  allStocks.forEach(s => {
    const code = s['代码'];
    const concepts = stockConceptsCache[code] || [];
    concepts.forEach(c => {
      conceptCount[c] = (conceptCount[c] || 0) + 1;
    });
  });
  
  // 按出现次数排序，取前30个热门概念
  hotConceptsCache = new Set(
    Object.entries(conceptCount)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 30)
      .map(([name]) => name)
  );
}

async function loadStockConcepts(codes){
  if(!codes.length)return;
  const uncached=codes.filter(c=>!stockConceptsCache[c]);
  if(!uncached.length)return;
  try{
    const r=await fetch('/api/stock-concepts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes:uncached})});
    const data=await r.json();
    Object.assign(stockConceptsCache,data);
    // 加载完成后计算热门概念
    calculateHotConcepts();
  }catch(e){console.error('loadStockConcepts error:',e);}
}

// 获取股票概念标签HTML（只显示热门概念，最多5个）
function getConceptTags(code,maxTags){
  const allConcepts=stockConceptsCache[code]||[];
  if(!allConcepts.length)return '<span style="color:var(--tx3);font-size:10px">--</span>';
  
  // 过滤：只保留热门概念（在涨停股中出现频率高的）
  let hotConcepts = allConcepts.filter(c => hotConceptsCache.has(c));
  
  // 如果热门概念太少，补充一些非热门的
  if(hotConcepts.length < 2){
    const remaining = allConcepts.filter(c => !hotConceptsCache.has(c));
    hotConcepts = [...hotConcepts, ...remaining.slice(0, 2)];
  }
  
  // 限制数量（默认最多5个）
  const limit = maxTags || 5;
  const displayConcepts = hotConcepts.slice(0, limit);
  
  return displayConcepts.map(c=>{
    const isHot = hotConceptsCache.has(c);
    const bgColor = isHot ? 'rgba(255,59,48,.1)' : 'rgba(0,122,255,.08)';
    const textColor = isHot ? 'var(--rd)' : 'var(--bl)';
    const hotIcon = isHot ? '🔥' : '';
    return `<span class="concept-tag" onclick="event.stopPropagation();switchToConcept('${c.replace(/'/g,"\\'")}')" style="display:inline-block;padding:2px 6px;background:${bgColor};border-radius:6px;font-size:10px;margin:2px;cursor:pointer;color:${textColor};font-weight:500;transition:.15s">${hotIcon}${c}</span>`;
  }).join('');
}
// 生成涨停原因（基于行业 + 热门概念 + 连板状态）
function getZtReason(s){
  const industry=s['所属行业']||'';
  const code=s['代码']||'';
  const lb=s['连板数']||1;
  const concepts=stockConceptsCache[code]||[];
  const hotConcepts=concepts.filter(c=>hotConceptsCache.has(c));
  let reasons=[];
  // 连板原因
  if(lb>=3) reasons.push(lb+'连板龙头');
  else if(lb>=2) reasons.push(lb+'连板');
  // 行业原因
  if(industry) reasons.push(industry+'板块');
  // 热门概念原因（取前2个）
  hotConcepts.slice(0,2).forEach(c=>reasons.push(c));
  // 如果没有概念，用所属行业补充
  if(!hotConcepts.length && concepts.length) reasons.push(concepts[0]);
  return reasons.length?reasons.join('+'):(industry||'--');
}

function switchToConcept(name){
  // 跳转到分组概念页面并搜索该概念
  let cg=getConceptGroups();
  let idx=cg.findIndex(g=>g.name===name || g.nameEn===name);
  if(idx>=0){
    window._cgSelectedTab=idx;
    switchTab('concept');
  }else{
    // 如果没有该概念分组，创建一个
    switchTab('concept');
    setTimeout(()=>{
      document.getElementById('cgName').value=name;
      addConceptGroup();
    },100);
  }
}

/* ===== 主题切换 ===== */
function toggleTheme(){
  const root=document.documentElement;
  const isDark=root.getAttribute('data-theme')==='dark';
  root.setAttribute('data-theme',isDark?'':'dark');
  localStorage.setItem('theme',isDark?'light':'dark');
  document.getElementById('themeBtn').innerHTML=isDark?'<i data-lucide="moon" style="width:16px;height:16px"></i>':'<i data-lucide="sun" style="width:16px;height:16px"></i>';
  lucide.createIcons();
}
function initTheme(){
  const saved=localStorage.getItem('theme')||'light';
  if(saved==='dark'){
    document.documentElement.setAttribute('data-theme','dark');
    document.getElementById('themeBtn').innerHTML='<i data-lucide="sun" style="width:16px;height:16px"></i>';
  }
}

function isMarketOpen(){
  const n=new Date(),d=n.getDay();
  if(d===0||d===6)return false;
  const t=n.getHours()*60+n.getMinutes();
  return(t>=555&&t<=690)||(t>=780&&t<=900);
}
function isRefreshTime(){
  const n=new Date(),d=n.getDay();
  if(d===0||d===6)return false;
  const t=n.getHours()*60+n.getMinutes();
  return(t>=555&&t<=930); // 9:15-15:30
}

function getTradingDate(){
  const n=new Date();
  const t=n.getHours()*60+n.getMinutes();
  if(t<555||(n.getDay()===0||n.getDay()===6)){
    let d=new Date(n);
    while(d.getDay()===0||d.getDay()===6)d.setDate(d.getDate()-1);
    return formatDateLocal(d);
  }
  return formatDateLocal(n);
}
function formatDateLocal(d){
  const y=d.getFullYear();
  const m=String(d.getMonth()+1).padStart(2,'0');
  const dd=String(d.getDate()).padStart(2,'0');
  return y+m+dd;
}

function updateBadge(){
  const b=document.getElementById('badge');
  if(isMarketOpen()){b.textContent='🟢 交易中';b.className='badge badge-on';}
  else{b.textContent='🔴 已收盘';b.className='badge badge-off';}
}

function schedRefresh(){
  if(timer)clearTimeout(timer);
  if(isRefreshTime()){timer=setTimeout(()=>{refresh();schedRefresh();},60000);}
  else{timer=setTimeout(()=>{updateBadge();schedRefresh();},60000);}
}

async function refresh(){
  const now=Date.now();
  if(now-lastRefresh<2000){return;}// 最多2秒一次
  lastRefresh=now;
  const date=getTradingDate();
  // 显示加载状态
  const btns=document.querySelectorAll('button[onclick="refresh()"]');
    btns.forEach(b=>{b.disabled=true;b.style.opacity='0.5';b.innerHTML='<i data-lucide="loader-2" style="width:14px;height:14px;animation:spin 1s linear infinite"></i>'});
  const ut=document.getElementById('updateTime');
  if(ut)ut.textContent='⏳ 正在刷新...';
  try{
    // 使用 /api/refresh 强制刷新，绕过缓存，带60秒超时
    const ctrl=new AbortController();
    const tm=setTimeout(()=>ctrl.abort(),60000);
    const r=await fetch('/api/refresh?date='+date,{signal:ctrl.signal});
    clearTimeout(tm);
    D=await r.json();
    renderAll();
    const ts=new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(ut)ut.textContent='更新于 '+ts;
    document.getElementById('dataDate').textContent='数据日期: '+D.date;
  }catch(e){
    console.error(e);
    if(ut)ut.textContent='❌ 刷新失败，点击重试';
  }finally{
    btns.forEach(b=>{b.disabled=false;b.style.opacity='1';b.textContent='🔄';});
    // 恢复主刷新按钮文字
    const mainBtn=document.querySelector('.main-header button[onclick="refresh()"]');
    if(mainBtn)mainBtn.innerHTML='<i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新';
  }
}

function fmtTime(t){
  if(!t||t.length<4)return'--';
  let h=parseInt(t.slice(0,2)),m=t.slice(2,4);
  if(h>=24)h=0;
  let s=t.length>4?':'+t.slice(4):'';
  return(h<10?'0':'')+h+':'+m+s;
}

function renderAll(){
  renderSidebar();renderTab();
}

function renderSidebar(){
  const items=[
    {id:'home',icon:'home',label:'首页'},
    {id:'limit_center',icon:'trending-up',label:'涨跌停中心',count:D.limit_up_count},
    'divider',
    {id:'hot',icon:'flame',label:'热门股票'},
    {id:'main_flow',icon:'arrow-right-left',label:'主力净流入'},
    {id:'new_highs',icon:'line-chart',label:'新高票'},
    {id:'sentiment',icon:'brain',label:'市场情绪'},
    {id:'cls_news',icon:'newspaper',label:'财联社新闻'},
    {id:'trend',icon:'trending-up',label:'趋势票'},
    {id:'engulfing',icon:'refresh-cw',label:'K线反包'},
    'divider',
    {id:'sector_center',icon:'layers',label:'板块中心'},
    {id:'concept',icon:'tag',label:'分组概念'},
    'divider',
    {id:'portfolio_center',icon:'briefcase',label:'持仓中心'},
    'divider',
    {id:'ai_center',icon:'bot',label:'AI中心'},
    'divider',
    {id:'fund',icon:'gem',label:'基金推荐'},
    'divider',
    {id:'anomaly',icon:'activity',label:'异动预测'},
    {id:'monitoring',icon:'shield',label:'监控室'}
  ];
  document.getElementById('sidebarNav').innerHTML=items.map(t=>{
    if(t==='divider')return '<div class="nav-divider"></div>';
    return `<div class="nav-item${t.id===curTab?' active':''}" onclick="switchTab('${t.id}')"><span class="nav-icon"><i data-lucide="${t.icon}"></i></span><span>${t.label}</span>${t.count?`<span class="nav-badge">${t.count}</span>`:''}</div>`;
  }).join('');
  lucide.createIcons();
}

function switchTab(id){curTab=id;renderSidebar();renderTab();}

function renderTab(){
  const c=document.getElementById('tabContent');
  if(curTab==='home')renderHome(c);
  else if(curTab==='limit_center')renderLimitCenter(c);
  else if(curTab==='sector_center')renderSectorCenter(c);
  else if(curTab==='portfolio_center')renderPortfolioCenter(c);
  else if(curTab==='ai_center')renderAICenter(c);
  else if(curTab==='hot')renderHot(c);
  else if(curTab==='main_flow')renderMainFlow(c);
  else if(curTab==='new_highs')renderNewHighs(c);
  else if(curTab==='sentiment')renderSentimentDetail(c);
  else if(curTab==='cls_news')renderCLSNews(c);
  else if(curTab==='trend')renderTrendStocks(c);
  else if(curTab==='engulfing')renderKlineEngulfing(c);
  else if(curTab==='concept')renderConcept(c);
  else if(curTab==='fund')renderFund(c);
  else if(curTab==='anomaly')renderAnomalyPrediction(c);
  else if(curTab==='monitoring')renderMonitoring(c);
}

/* ===== 子标签渲染辅助函数 ===== */
function renderSubTabs(el, tabGroupId, tabs, defaultTab) {
  if (!subTabs[tabGroupId]) subTabs[tabGroupId] = defaultTab || tabs[0].id;
  let currentSub = subTabs[tabGroupId];
  
  let html = '<div class="sub-tabs">';
  tabs.forEach(t => {
    html += `<div class="sub-tab${t.id === currentSub ? ' active' : ''}" onclick="subTabs['${tabGroupId}']='${t.id}';renderTab()">${t.label}</div>`;
  });
  html += '</div>';
  html += '<div id="subTabContent"></div>';
  el.innerHTML = html;
  
  // Render the active sub-tab content
  const subEl = document.getElementById('subTabContent');
  const activeTab = tabs.find(t => t.id === currentSub);
  if (activeTab && activeTab.render) activeTab.render(subEl);
}

/* ===== 涨跌停中心 ===== */
function renderLimitCenter(el) {
  const tabs = [
    {id:'limit_up', label:'涨停股', render: renderLimitUp},
    {id:'limit_down', label:'跌停股', render: renderLimitDown},
    {id:'broken', label:'炸板股', render: renderBroken},
    {id:'limit_reasons', label:'涨跌停原因', render: renderLimitReasons},
    {id:'highest_board', label:'最高板', render: renderLimitReasons},
    {id:'yesterday_zt', label:'昨日涨停', render: renderYesterdayZT}
  ];
  renderSubTabs(el, 'limitCenter', tabs, 'limit_up');
}

/* ===== 板块中心 ===== */
function renderSectorCenter(el) {
  const tabs = [
    {id:'sector_stats', label:'板块统计', render: renderSectorStats},
    {id:'sector', label:'板块资金', render: renderSector},
    {id:'sector_leader', label:'板块龙头', render: renderSectorLeader},
    {id:'sector_lfs', label:'龙头/跟风/补涨', render: renderSectorLFS}
  ];
  renderSubTabs(el, 'sectorCenter', tabs, 'sector_stats');
}

/* ===== 持仓中心 ===== */
function renderPortfolioCenter(el) {
  const tabs = [
    {id:'portfolio', label:'持仓管理', render: renderPortfolio},
    {id:'watchlist', label:'想买', render: renderWatchlist}
  ];
  renderSubTabs(el, 'portfolioCenter', tabs, 'portfolio');
}

/* ===== AI中心 ===== */
function renderAICenter(el) {
  const tabs = [
    {id:'ai', label:'AI预测', render: renderAI},
    {id:'ai_hist', label:'预测历史', render: renderAIHist}
  ];
  renderSubTabs(el, 'aiCenter', tabs, 'ai');
}

/* ===== 首页仪表盘 ===== */
function renderHome(el){
  const s=D.market_stats||{};
  const lu=D.limit_up||[];
  let yizi=lu.filter(s=>s.is_yizi).length;
  let upCount=s.up||0,downCount=s.down||0,flatCount=s.flat||0;
  let ratio=downCount?(upCount/downCount).toFixed(1):'∞';

  let h='';

  // ===== 震荡（市场情绪）放在最上边 =====
  let bullCount=lu.length;
  let bearCount=(D.limit_down||[]).length;
  let brokenCount=(D.broken_limit||[]).length;
  let totalZt=bullCount+brokenCount;
  let zbRate=totalZt?Math.round(brokenCount/totalZt*100):0;
  let marketSentiment='震荡';
  let sentColor='var(--ac)';
  let sentIcon='⚖️';
  if(bullCount>30&&zbRate<30&&upCount>downCount*1.5){marketSentiment='偏多';sentColor='var(--rd)';sentIcon='🐂';}
  else if(bullCount>50&&zbRate<20){marketSentiment='强势看多';sentColor='var(--rd)';sentIcon='🚀';}
  else if(bearCount>bullCount&&downCount>upCount*1.5){marketSentiment='偏空';sentColor='var(--gn)';sentIcon='🐻';}
  else if(bearCount>20){marketSentiment='弱势';sentColor='var(--gn)';sentIcon='⚠️';}
  let sentBg=sentColor==='var(--rd)'?'rgba(232,84,84,.08)':sentColor==='var(--gn)'?'rgba(78,203,113,.08)':'rgba(167,139,250,.08)';
  h+=`<div style="display:flex;align-items:center;gap:8px;padding:6px 12px;background:${sentBg};border:1px solid var(--bd);border-radius:6px;margin-bottom:10px;border-left:3px solid ${sentColor}">
    <span style="font-size:16px">${sentIcon}</span>
    <span style="font-weight:700;color:${sentColor};font-size:13px">${marketSentiment}</span>
    <span style="font-size:11px;color:var(--tx3);margin-left:4px">涨停${bullCount} | 跌停${bearCount} | 炸板率${zbRate}% | 最高${lu.length?Math.max(...lu.map(s=>s['连板数']||1)):0}板 | 涨跌比${ratio}</span>
  </div>`;

  // ===== 大盘指数（紧凑） =====
  h+=`<div id="homeMarketIndices" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px"><div class="loading">加载大盘指数...</div></div>`;
  loadMarketIndices();

  // ===== 市场概览（小卡片） =====
  h+=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px">
    <div style="text-align:center;padding:8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;border-top:3px solid var(--rd)"><div style="font-size:20px;font-weight:700;color:var(--rd)">${D.limit_up_count||0}</div><div style="font-size:10px;color:var(--tx3)">🔴 涨停</div><div style="font-size:9px;color:var(--tx3);margin-top:1px">一字${yizi}</div></div>
    <div style="text-align:center;padding:8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;border-top:3px solid var(--gn)"><div style="font-size:20px;font-weight:700;color:var(--gn)">${D.limit_down_count||0}</div><div style="font-size:10px;color:var(--tx3)">🟢 跌停</div></div>
    <div style="text-align:center;padding:8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;border-top:3px solid var(--ac)"><div style="font-size:20px;font-weight:700;color:var(--ac)">${D.broken_limit_count||0}</div><div style="font-size:10px;color:var(--tx3)">🟡 炸板</div></div>
    <div style="text-align:center;padding:8px;background:var(--sf);border:1px solid var(--bd);border-radius:6px;border-top:3px solid var(--bl)"><div style="font-size:10px;color:var(--tx3);margin-bottom:2px">📊 涨跌平</div><div style="font-size:15px;font-weight:700"><span style="color:var(--rd)">${upCount}↑</span> <span style="color:var(--bl);font-size:11px">/</span> <span style="color:var(--gn)">${downCount}↓</span> <span style="color:var(--bl);font-size:11px">/</span> <span style="color:var(--bl)">${flatCount}→</span></div><div style="font-size:9px;color:var(--tx3);margin-top:1px">比 ${ratio}</div></div>
  </div>`;

  // ===== 快捷功能入口 =====
  h+=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
    <button class="btn" onclick="switchTab('hot')" style="padding:8px 14px;font-size:12px">🔥热门股</button>
    <button class="btn" onclick="switchTab('main_flow')" style="padding:8px 14px;font-size:12px">💰主力净流</button>
    <button class="btn" onclick="subTabs['limitCenter']='yesterday_zt';switchTab('limit_center')" style="padding:8px 14px;font-size:12px">📊昨日涨停</button>
    <button class="btn" onclick="switchTab('new_highs')" style="padding:8px 14px;font-size:12px">📈新高票</button>
    <button class="btn" onclick="switchTab('sentiment')" style="padding:8px 14px;font-size:12px">🧠市场情绪</button>
    <button class="btn" onclick="switchTab('cls_news')" style="padding:8px 14px;font-size:12px">📰财联社新闻</button>
    <button class="btn" onclick="subTabs['limitCenter']='limit_reasons';switchTab('limit_center')" style="padding:8px 14px;font-size:12px">📋涨跌停原因</button>
    <button class="btn" onclick="subTabs['sectorCenter']='sector_leader';switchTab('sector_center')" style="padding:8px 14px;font-size:12px">👑板块龙头</button>
    <button class="btn" onclick="switchTab('engulfing')" style="padding:8px 14px;font-size:12px">🔄K线反包</button>
  </div>`;

  // ===== 第二行：📰 重点新闻 =====
  h+=`<div id="homeKeyNews" style="margin-bottom:14px"><div class="loading">加载重点新闻...</div></div>`;
  loadHomeKeyNews();

  // ===== 快捷入口 =====
  h+=`<div style="display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn" onclick="switchTab('ai_center')" style="padding:10px 20px">🤖 AI预测</button>
    <button class="btn" onclick="switchTab('portfolio_center')" style="padding:10px 20px">💼 持仓管理</button>
    <button class="btn" onclick="subTabs['portfolioCenter']='watchlist';switchTab('portfolio_center')" style="padding:10px 20px">🎯 想买列表</button>
    <button class="btn" onclick="switchTab('concept')" style="padding:10px 20px">🏷️ 分组概念</button>
    <button class="btn" onclick="switchTab('sentiment')" style="padding:10px 20px">🧠 市场情绪</button>
    <button class="btn" onclick="subTabs['limitCenter']='limit_reasons';switchTab('limit_center')" style="padding:10px 20px">📋 涨跌停原因</button>
    <button class="btn" onclick="switchTab('trend')" style="padding:10px 20px">📈 趋势票</button>
    <button class="btn" onclick="switchTab('engulfing')" style="padding:10px 20px">🔄 K线反包</button>
    <span style="margin-left:auto;font-size:11px;color:var(--tx3);display:flex;align-items:center">💡 数据基于${D.date||''}</span>
  </div>`;

  el.innerHTML=h;
}

/* ===== 重点新闻加载 ===== */
const IMPORTANT_KEYWORDS=['重磅','突发','紧急','涨停','跌停','暴雷','退市','并购','重组','增持','减持','回购','业绩','财报','政策','改革','央行','国务院','证监会','利好','利空','大涨','暴跌','突破','分红','IPO','新股','获批','中标'];
const BULLISH_KW=['利好','涨停','大涨','突破','新高','增长','盈利','超预期','获批','中标','合作','订单','增持','回购','并购','重组','注入','借壳','政策支持','补贴','减税','分红','送转'];
const BEARISH_KW=['利空','跌停','暴跌','暴雷','退市','减持','亏损','下滑','不及预期','处罚','违规','立案','终止','违约','爆仓','清仓','暂停','取消'];

function analyzeSentiment(title,summary){
  const text=(title||'')+(summary||'');
  let bullScore=0,bearScore=0;
  BULLISH_KW.forEach(k=>{if(text.includes(k))bullScore++;});
  BEARISH_KW.forEach(k=>{if(text.includes(k))bearScore++;});
  if(bullScore>bearScore)return{sentiment:'利好',color:'var(--rd)',icon:'📈'};
  if(bearScore>bullScore)return{sentiment:'利空',color:'var(--gn)',icon:'📉'};
  return{sentiment:'中性',color:'var(--tx3)',icon:'➖'};
}

function extractSectors(title,summary){
  const text=(title||'')+(summary||'');
  const sectorKW=['半导体','芯片','光刻机','光刻胶','封测','封装','PCB','IC设计','存储','DRAM','HBM','GPU','AI','人工智能','大模型','AIGC','算力','数据中心','服务器','CPO','光模块','新能源','光伏','锂电','锂矿','电池','储能','氢能','风电','核电','碳中和','绿色电力','军工','国防','航天','航空','船舶','医药','创新药','中药','医疗器械','CXO','生物','消费','白酒','啤酒','食品','调味品','银行','券商','保险','信托','地产','房地产','物业管理','汽车','智能驾驶','无人驾驶','新能源汽车','充电桩','有色','稀土','铜','铝','黄金','白银','钢铁','煤炭','天然气','石油','基建','水泥','建材','科技','5G','通信','光纤','软件','SaaS','互联网','机器人','人形机器人','减速器','传感器','数字经济','信创','操作系统','数据库','卫星','低空经济','量子','脑机接口','6G','工业母机','数控机床','刀具','培育钻石','宠物','免税','医美','直播','电商','旅游','酒店','影视','游戏','元宇宙','虚拟现实','MR','AR','VR','农业','种业','养殖','猪肉','鸡肉','水利','环保','固废','污水处理','检测','认证','物流','快递','港口','航运','航空运输','铁路','特高压','智能电网','充电桩','换电','车路协同','车联网','折叠屏','苹果','华为','小米','特斯拉','英伟达','OpenAI','短剧','游戏','文生视频','Sora','可控核聚变','固态电池','钙钛矿','复合铜箔','猪周期','减肥药','GLP-1','mRNA','信创','跨境支付','CIPS','人民币','央行','LPR','MLF','降息','降准','财政','国债','地方债','IPO','回购','增持','减持','分红','送转','ETF','融资融券','北向','港股通','中概股','外汇','原油','黄金','铜','锂','钴','镍','硅'];
  // 标题权重更高（匹配2次），摘要1次，去重取前3
  const found=new Map();
  sectorKW.forEach(k=>{
    let score=0;
    if(text.includes(k)){
      score+=(title||'').split(k).length-1; // 标题匹配次数
      score+=(summary||'').split(k).length-1; // 摘要匹配次数
      if(!found.has(k))found.set(k,score);
    }
  });
  return[...found.entries()].sort((a,b)=>b[1]-a[1]).map(e=>e[0]).slice(0,4);
}

async function loadHomeKeyNews(){
  try{
    const r=await fetch('/api/key-news');
    const data=await r.json();
    const el=document.getElementById('homeKeyNews');
    if(!el)return;
    const important=data.filter(n=>n['重要']||IMPORTANT_KEYWORDS.some(kw=>(n['标题']||'').includes(kw))).slice(0,30);
    if(!important.length){el.innerHTML='';return;}
    let h=`<div class="card"><div class="card-h"><span class="card-t">📰 重点新闻</span><span class="card-c">${important.length}条重要</span></div><div class="card-b" style="max-height:500px;overflow-y:auto"><table class="tbl"><thead><tr><th style="width:70px">时间</th><th style="width:70px">来源</th><th style="width:70px">情绪</th><th style="width:110px">关联板块</th><th>标题</th></tr></thead><tbody>`;
    important.forEach(n=>{
      const title=n['标题']||'';
      const source=n['来源']||'东方财富';
      const isHot=title.includes('重磅')||title.includes('突发')||title.includes('紧急');
      const titleStyle=isHot?'color:var(--rd);font-weight:700':'color:var(--tx2);font-weight:500';
      const sent=analyzeSentiment(title,n['摘要']||'');
      const sectors=extractSectors(title,n['摘要']||'');
      const sentTag=`<span style="color:${sent.color};font-weight:600;font-size:11px">${sent.icon}${sent.sentiment}</span>`;
      const sectorTags=sectors.length?sectors.map(s=>`<span style="background:rgba(167,139,250,.1);color:var(--ac);padding:1px 5px;border-radius:4px;font-size:10px;margin:1px 2px">${s}</span>`).join(''):'<span style="color:var(--tx3);font-size:9px">--</span>';
      h+=`<tr style="cursor:pointer" onclick="${n['链接']?"window.open('"+n['链接']+"','_blank')":""}"><td style="white-space:nowrap;color:var(--tx3);font-size:11px">${(n['发布时间']||'').substring(5,16)}</td><td style="font-size:10px;color:var(--ac);white-space:nowrap">${source}</td><td>${sentTag}</td><td>${sectorTags}</td><td style="font-size:12px;${titleStyle}">${isHot?'🔥':''}${title}</td></tr>`;
    });
    h+=`</tbody></table></div></div>`;
    el.innerHTML=h;
  }catch(e){const el=document.getElementById('homeKeyNews');if(el)el.innerHTML='';}
}

/* ===== 大盘指数加载 ===== */
async function loadMarketIndices(){
  try{
    const r=await fetch('/api/market-volume');
    const data=await r.json();
    const el=document.getElementById('homeMarketIndices');
    if(!el)return;
    const indices=data.indices||{};
    if(!Object.keys(indices).length){el.innerHTML='';return;}
    
    const indexList=[
      {name:'上证指数',icon:'📈',color:'var(--rd)'},
      {name:'深证成指',icon:'📊',color:'var(--ac)'},
      {name:'创业板指',icon:'🚀',color:'var(--gn)'},
      {name:'科创50',icon:'🔬',color:'var(--bl)'}
    ];
    
    let h='';
    indexList.forEach(idx=>{
      const d=indices[idx.name];
      if(!d)return;
      const change=d['涨跌幅']||0;
      const changeColor=change>=0?'var(--rd)':'var(--gn)';
      const changeSign=change>=0?'+':'';
      const volume=d['成交额']?formatVolume(d['成交额']):'--';
      
      h+=`<div style="text-align:center;padding:8px 6px;background:var(--sf);border:1px solid var(--bd);border-radius:6px">
        <div style="font-size:10px;color:var(--tx3);margin-bottom:2px">${idx.name}</div>
        <div style="font-size:16px;font-weight:700;color:${changeColor}">${d['最新价'].toFixed(2)}</div>
        <div style="font-size:11px;color:${changeColor}">${changeSign}${change.toFixed(2)}%</div>
      </div>`;
    });
    el.innerHTML=h;
  }catch(e){const el=document.getElementById('homeMarketIndices');if(el)el.innerHTML='';}
}
function formatVolume(v){
  let absV=Math.abs(v);
  let sign=v<0?'-':'';
  if(absV>=1e8)return sign+(absV/1e8).toFixed(2)+'亿';
  if(absV>=1e4)return sign+(absV/1e4).toFixed(0)+'万';
  return sign+absV.toFixed(0);
}

/* ===== 表格排序 ===== */
let sortState={};
function sortTable(tabId,column,dataKey,isNumeric){
  const prev=sortState[tabId];
  let dir='asc';
  if(prev&&prev.col===column){dir=prev.dir==='asc'?'desc':'asc';}
  sortState[tabId]={col:column,dir:dir};
  // re-render the table with sorted data
  if(tabId==='limit_up')renderLimitUp(document.getElementById('subTabContent'));
  else if(tabId==='limit_down')renderLimitDown(document.getElementById('subTabContent'));
  else if(tabId==='broken')renderBroken(document.getElementById('subTabContent'));
}
function getSortedData(data,tabId,defaultSortKey,defaultSortDir){
  const st=sortState[tabId];
  if(!st)return data;
  const col=st.col;const dir=st.dir==='asc'?1:-1;
  return [...data].sort((a,b)=>{
    let va=a[col],vb=b[col];
    if(typeof va==='string')return dir*va.localeCompare(vb||'');
    return dir*((va||0)-(vb||0));
  });
}
function sortIndicator(tabId,col){
  const st=sortState[tabId];
  if(st&&st.col===col)return st.dir==='asc'?' ▲':' ▼';
  return '';
}
function thClick(tabId,col,label){
  return `<th style="cursor:pointer;user-select:none" onclick="sortTable('${tabId}','${col}')">${label}${sortIndicator(tabId,col)}</th>`;
}

function renderLimitUp(el){
  let data=D.limit_up||[];
  data.sort((a,b)=>{
    if(a.is_yizi&&!b.is_yizi)return -1;
    if(!a.is_yizi&&b.is_yizi)return 1;
    return(b.连板数||1)-(a.连板数||1);
  });
  if(sortState['limit_up']){
    data=getSortedData(data,'limit_up');
  }
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无涨停数据</div></div></div>';return;}
  let yiziCount=data.filter(s=>s.is_yizi).length;
  let brokenCount=(D.broken_limit||[]).length;
  let totalZt=data.length+brokenCount;
  let zbRate=totalZt?(brokenCount/totalZt*100).toFixed(1):'0';
  
  // 先渲染表格（不依赖概念数据）
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🔴 涨停股一览</span><span class="card-c">${data.length}只${yiziCount?' · 一字板'+yiziCount+'只':''} · 炸板率${zbRate}%</span></div><div class="card-b"><table class="tbl"><thead><tr>
    ${thClick('limit_up','代码','代码')}${thClick('limit_up','名称','名称')}${thClick('limit_up','最新价','最新价')}${thClick('limit_up','涨跌幅','涨幅')}${thClick('limit_up','连板数','连板')}${thClick('limit_up','首次封板时间','首封时间')}${thClick('limit_up','最后封板时间','末封时间')}${thClick('limit_up','炸板次数','炸板')}${thClick('limit_up','换手率','换手率')}${thClick('limit_up','成交额','成交额')}${thClick('limit_up','封板资金','封板资金')}${thClick('limit_up','所属行业','所属行业')}<th style="min-width:90px">涨停原因</th>${thClick('limit_up','所属概念','所属概念')}</tr></thead><tbody>`;
  data.forEach(s=>{
    const t=s['首次封板时间']||'';
    const ft=fmtTime(t);
    const lt=fmtTime(s['最后封板时间']||'');
    const zb=s['炸板次数']||0;
    const lb=s['连板数']||1;
    const hb=s['换手率']||0;
    const fb=s['封板资金']||0;
    const fbStr=fb>1e8?(fb/1e8).toFixed(2)+'亿':fb>1e4?(fb/1e4).toFixed(0)+'万':'--';
    // 连板/首板标签
    let boardTag='';
    if(lb<=1){
      boardTag='<span class="tag" style="background:rgba(91,141,239,.15);color:var(--bl)">首板</span>';
    }else{
      const ztStats=s['涨停统计']||'';
      const parts=ztStats.split('/');
      const days=parts[0]||lb;
      const times=parts[1]||lb;
      boardTag=`<span class="tag tag-hl">${days}天${times}涨停</span>`;
    }
    html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s['代码']}')">
      <td>${s['代码']}</td>
      <td>${s['名称']} ${s.is_yizi?'<span class="tag tag-yz">一字</span>':''} ${boardTag}</td>
      <td>${(s['最新价']||0).toFixed(2)}</td>
      <td class="up">+${(s['涨跌幅']||0).toFixed(2)}%</td>
      <td>${lb}</td>
      <td>${ft}</td>
      <td>${lt}</td>
      <td>${zb>0?'<span class="dn">'+zb+'</span>':'0'}</td>
      <td>${hb.toFixed(2)}%</td>
      <td>${formatVolume(s['成交额']||0)}</td>
      <td>${fbStr}</td>
      <td>${s['所属行业']||''}</td>
      <td style="font-size:11px;color:var(--ac);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${getZtReason(s)}">${getZtReason(s)}</td>
      <td>${getConceptTags(s['代码'])}</td></tr>`;
  });
  html+='</tbody></table></div></div></div>';
  el.innerHTML=html;
  
  // 异步加载概念数据（不阻塞渲染）
  let codes=data.map(s=>s['代码']).filter(c=>c);
  loadStockConcepts(codes).then(()=>{
    // 概念加载完成后更新概念列
    document.querySelectorAll('#tabContent .concept-tag').forEach(el=>{
      // 已经渲染了，不需要重新渲染整个表格
    });
  }).catch(e=>console.error('概念加载失败:',e));
}

function renderLimitDown(el){
  let data=D.limit_down||[];
  if(sortState['limit_down'])data=getSortedData(data,'limit_down');
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无跌停数据</div></div></div>';return;}
  // 加载概念数据
  let codes=data.map(s=>s['代码']).filter(c=>c);
  loadStockConcepts(codes);
  
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🟢 跌停股一览</span><span class="card-c">${data.length}只</span></div><div class="card-b"><table class="tbl"><thead><tr>
    ${thClick('limit_down','代码','代码')}${thClick('limit_down','名称','名称')}${thClick('limit_down','最新价','最新价')}${thClick('limit_down','涨跌幅','跌幅')}${thClick('limit_down','连续跌停','连续跌停')}${thClick('limit_down','最后封板时间','封板时间')}${thClick('limit_down','开板次数','开板次数')}${thClick('limit_down','换手率','换手率')}${thClick('limit_down','封单资金','封单资金')}${thClick('limit_down','所属行业','所属行业')}${thClick('limit_down','所属概念','所属概念')}</tr></thead><tbody>`;
  data.forEach(s=>{
    const t=s['最后封板时间']||'';
    const ft=fmtTime(t);
    const fd=s['封单资金']||0;
    const fdStr=fd>1e8?(fd/1e8).toFixed(2)+'亿':fd>1e4?(fd/1e4).toFixed(0)+'万':'--';
    html+=`<tr>
      <td>${s['代码']}</td><td>${s['名称']}</td>
      <td>${(s['最新价']||0).toFixed(2)}</td>
      <td class="dn">${(s['涨跌幅']||0).toFixed(2)}%</td>
      <td>${s['连续跌停']||1}</td><td>${ft}</td>
      <td>${s['开板次数']||0}</td><td>${(s['换手率']||0).toFixed(2)}%</td>
      <td>${fdStr}</td><td>${s['所属行业']||''}</td><td>${getConceptTags(s['代码'],3)}</td></tr>`;
  });
  html+='</tbody></table></div></div></div>';
  el.innerHTML=html;
}

function renderBroken(el){
  let data=D.broken_limit||[];
  if(sortState['broken'])data=getSortedData(data,'broken');
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无炸板数据</div></div></div>';return;}
  // 加载概念数据
  let codes=data.map(s=>s['代码']).filter(c=>c);
  loadStockConcepts(codes);
  
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🟡 炸板股一览</span><span class="card-c">${data.length}只</span></div><div class="card-b"><table class="tbl"><thead><tr>
    ${thClick('broken','代码','代码')}${thClick('broken','名称','名称')}${thClick('broken','最新价','现价')}${thClick('broken','涨跌幅','涨幅')}${thClick('broken','涨停价','涨停价')}${thClick('broken','首次封板时间','首封时间')}${thClick('broken','炸板次数','炸板次数')}${thClick('broken','振幅','振幅')}${thClick('broken','换手率','换手率')}${thClick('broken','所属行业','行业')}${thClick('broken','所属概念','概念')}</tr></thead><tbody>`;
  data.forEach(s=>{
    const t=s['首次封板时间']||'';
    const ft=fmtTime(t);
    html+=`<tr>
      <td>${s['代码']}</td><td>${s['名称']}</td>
      <td>${(s['最新价']||0).toFixed(2)}</td>
      <td class="${(s['涨跌幅']||0)>=0?'up':'dn'}">${(s['涨跌幅']||0)>=0?'+':''}${(s['涨跌幅']||0).toFixed(2)}%</td>
      <td>${(s['涨停价']||0).toFixed(2)}</td><td>${ft}</td>
      <td class="dn">${s['炸板次数']||0}</td>
      <td>${(s['振幅']||0).toFixed(2)}%</td><td>${(s['换手率']||0).toFixed(2)}%</td>
      <td>${s['所属行业']||''}</td><td>${getConceptTags(s['代码'],3)}</td></tr>`;
  });
  html+='</tbody></table></div></div></div>';
  el.innerHTML=html;
}

let sectorViewMode='card';
function renderSector(el){
  let data=D.sector_flow||[];
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无板块数据</div></div></div>';return;}
  // 分涨跌
  let up=data.filter(s=>(s['净额']||0)>0).sort((a,b)=>(b['净额']||0)-(a['净额']||0));
  let dn=data.filter(s=>(s['净额']||0)<0).sort((a,b)=>(a['净额']||0)-(b['净额']||0));
  let flat=data.filter(s=>(s['净额']||0)===0).sort((a,b)=>(b['今日涨跌幅']||0)-(a['今日涨跌幅']||0));
  let totalIn=up.reduce((a,s)=>a+(s['净额']||0),0);
  let totalOut=dn.reduce((a,s)=>a+Math.abs(s['净额']||0),0);

  function cardHtml(s){
    const v=s['净额']||0;
    const vs=v>0?'+'+v.toFixed(2)+'亿':v.toFixed(2)+'亿';
    const chg=s['今日涨跌幅']||0;
    const lead=s['领涨股']||'';
    const leadChg=s['领涨股-涨跌幅']||0;
    const inflow=(s['流入资金']||0).toFixed(1);
    const outflow=(s['流出资金']||0).toFixed(1);
    return`<div class="sc-item ${v>=0?'sc-in':'sc-out'}">
      <div class="sc-row1"><span class="sc-name">${s['名称']}</span><span class="${v>=0?'up':'dn'}" style="font-weight:700">${vs}</span></div>
      <div class="sc-row2"><span class="${chg>=0?'up':'dn'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span><span style="color:var(--tx3)">入${inflow} 出${outflow}</span><span style="color:var(--tx3)">🚀${lead} <span class="${leadChg>=0?'up':'dn'}">${leadChg>=0?'+':''}${leadChg.toFixed(2)}%</span></span></div>
    </div>`;
  }

  let viewToggle=`<div style="display:flex;gap:4px"><button class="btn" onclick="sectorViewMode='card';renderSector(document.getElementById('tabContent'))" style="font-size:11px;padding:2px 8px;${sectorViewMode==='card'?'border-color:var(--ac);color:var(--ac)':''}">🃏 卡片</button><button class="btn" onclick="sectorViewMode='split';renderSector(document.getElementById('tabContent'))" style="font-size:11px;padding:2px 8px;${sectorViewMode==='split'?'border-color:var(--ac);color:var(--ac)':''}">📊 分栏</button><button class="btn" onclick="sectorViewMode='table';renderSector(document.getElementById('tabContent'))" style="font-size:11px;padding:2px 8px;${sectorViewMode==='table'?'border-color:var(--ac);color:var(--ac)':''}">📋 列表</button></div>`;
  let html=`<div class="card"><div class="card-h"><span class="card-t">💰 板块资金流向</span><div style="display:flex;gap:8px;align-items:center"><span class="card-c" style="margin-right:4px">共${data.length}个行业 · 流入${up.length} 流出${dn.length}</span>${viewToggle}</div></div><div class="card-b" style="padding:0">`;
  if(sectorViewMode==='card'){
    // 卡片视图（默认）- 按净额排序，每个板块一个小卡片
    html+=`<div style="padding:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;max-height:700px;overflow-y:auto">`;
    data.sort((a,b)=>(b['净额']||0)-(a['净额']||0)).forEach(s=>{
      const v=s['净额']||0;const chg=s['今日涨跌幅']||0;
      const lead=s['领涨股']||'';;const leadChg=s['领涨股-涨跌幅']||0;
      const inflow=(s['流入资金']||0).toFixed(1);const outflow=(s['流出资金']||0).toFixed(1);
      const borderC=v>=0?'rgba(232,84,84,.3)':'rgba(78,203,113,.3)';
      html+=`<div style="padding:10px 12px;border:1px solid ${borderC};border-radius:8px;background:var(--sf2)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span style="font-weight:600;font-size:13px">${s['名称']}</span>
          <span class="${v>=0?'up':'dn'}" style="font-weight:700;font-size:14px">${v>=0?'+':''}${v.toFixed(2)}亿</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--tx3)">
          <span class="${chg>=0?'up':'dn'}">涨跌 ${chg>=0?'+':''}${chg.toFixed(2)}%</span>
          <span>入${inflow} 出${outflow}</span>
        </div>
        <div style="font-size:11px;color:var(--tx3);margin-top:3px">🚀 ${lead} <span class="${leadChg>=0?'up':'dn'}">${leadChg>=0?'+':''}${leadChg.toFixed(2)}%</span></div>
      </div>`;
    });
    html+=`</div>`;
  }else if(sectorViewMode==='table'){
    // 表格视图
    html+=`<div style="padding:12px;overflow-x:auto"><table class="tbl"><thead><tr><th>行业</th><th>涨跌%</th><th>净额(亿)</th><th>流入(亿)</th><th>流出(亿)</th><th>领涨股</th><th>领涨%</th></tr></thead><tbody>`;
    data.sort((a,b)=>(b['净额']||0)-(a['净额']||0)).forEach(s=>{
      const v=s['净额']||0;const chg=s['今日涨跌幅']||0;
      html+=`<tr><td style="font-weight:600">${s['名称']}</td><td class="${chg>=0?'up':'dn'}">${chg>=0?'+':''}${chg.toFixed(2)}</td><td class="${v>=0?'up':'dn'}" style="font-weight:700">${v>=0?'+':''}${v.toFixed(2)}</td><td>${(s['流入资金']||0).toFixed(2)}</td><td>${(s['流出资金']||0).toFixed(2)}</td><td>${s['领涨股']||''}</td><td class="${(s['领涨股-涨跌幅']||0)>=0?'up':'dn'}">${((s['领涨股-涨跌幅']||0)>=0?'+':'')+(s['领涨股-涨跌幅']||0).toFixed(2)}</td></tr>`;
    });
    html+=`</tbody></table></div>`;
  }else{
    // 分栏视图
    html+=`<div style="display:flex;gap:1px;background:var(--bd)">
      <div style="flex:1;background:var(--sf)">
        <div style="padding:8px 14px;background:rgba(232,84,84,.08);border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:12px;font-weight:600;color:var(--rd)">🔴 净流入板块 (${up.length}个)</span>
          <span style="font-size:11px;color:var(--rd)">合计 +${totalIn.toFixed(1)}亿</span>
        </div>
        <div style="max-height:600px;overflow-y:auto">`;
  up.forEach(s=>{html+=cardHtml(s);});
  html+=`</div></div>
      <div style="flex:1;background:var(--sf)">
        <div style="padding:8px 14px;background:rgba(78,203,113,.08);border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:12px;font-weight:600;color:var(--gn)">🟢 净流出板块 (${dn.length}个)</span>
          <span style="font-size:11px;color:var(--gn)">合计 -${totalOut.toFixed(1)}亿</span>
        </div>
        <div style="max-height:600px;overflow-y:auto">`;
  dn.forEach(s=>{html+=cardHtml(s);});
  html+=`</div></div>
    </div>`;
  if(flat.length){
    html+=`<div style="padding:6px 14px;background:var(--sf2);border-top:1px solid var(--bd);font-size:11px;color:var(--tx3)">⚪ 平盘 ${flat.length}个: ${flat.map(s=>s['名称']).join('、')}</div>`;
  }
  html+=`</div></div>`;
  }
  el.innerHTML=html;
}

function renderRestructure(el){
  let data=D.restructure||[];
  let changes=D.stock_changes||{};
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无重组公告数据</div></div></div>';return;}
  let html=`<div class="card"><div class="card-h"><span class="card-t">🏢 资产重组公告</span><span class="card-c">${data.length}条</span></div><div class="card-b">`;
  data.forEach(s=>{
    const title=s['公告标题']||'';
    const shortTitle=title.length>40?title.substring(0,40)+'...':title;
    const chg=changes[s['代码']];
    let chgHtml='';
    if(chg){
      const pct=chg['涨跌幅']||0;
      const cls=pct>=0?'up':'dn';
      chgHtml=`<span class="${cls}">${pct>=0?'+':''}${pct.toFixed(2)}% (收盘${chg['收盘']})</span>`;
      if(chg['日期'])chgHtml+=` <span style="color:var(--tx3)">(${chg['日期']})</span>`;
    }
    html+=`<div class="rs-item">
      <div class="rs-header">
        <div><span class="rs-name">${s['名称']}</span><span class="rs-code">${s['代码']}</span></div>
        <span class="tag tag-hl">${s['公告类型']||''}</span>
      </div>
      <div style="font-size:12px;color:var(--tx2);margin-top:6px">${shortTitle}</div>
      <div class="rs-info">
        <span>📅 公告日期: ${s['公告日期']||''}</span>
        ${chgHtml?'<span>📊 最近交易日涨跌: '+chgHtml+'</span>':''}
        ${s['网址']?'<a href="'+s['网址']+'" target="_blank" style="color:var(--bl);text-decoration:none">查看公告 →</a>':''}
      </div></div>`;
  });
  html+='</div></div>';
  el.innerHTML=html;
}

/* ===== 热门股票页面 ===== */
function renderHot(el){
  let lu=D.limit_up||[];
  let ld=D.limit_down||[];
  let br=D.broken_limit||[];
  let sf=D.sector_flow||[];
  
  // 涨停热门（按连板数排序）
  let hotZt=[...lu].sort((a,b)=>(b.连板数||1)-(a.连板数||1)).slice(0,15);
  // 跌停热门（按连续跌停排序）
  let hotDt=[...ld].sort((a,b)=>(b.连续跌停||1)-(a.连续跌停||1)).slice(0,10);
  // 炸板热门（按成交额排序）
  let hotZb=[...br].sort((a,b)=>(b.成交额||0)-(a.成交额||0)).slice(0,10);
  // 资金流入板块
  let hotSector=[...sf].sort((a,b)=>(b.净额||0)-(a.净额||0)).slice(0,10);
  
  // 计算炸板率
  let totalZt=lu.length+br.length;
  let zbRate=totalZt?(br.length/totalZt*100).toFixed(1):'0';
  
  // 统计行业涨停数
  let industryCount={};
  lu.forEach(s=>{
    let ind=s.所属行业||'其他';
    industryCount[ind]=(industryCount[ind]||0)+1;
  });
  let topIndustries=Object.entries(industryCount).sort((a,b)=>b[1]-a[1]).slice(0,5);
  
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🔥 热门股票</span><span class="card-c">涨停${lu.length} 炸板${br.length} 跌停${ld.length}</span></div><div class="card-b" style="padding:16px">`;
  
  // 统计概览
  html+=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
    <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px;border-top:3px solid var(--rd)"><div style="font-size:24px;font-weight:700;color:var(--rd)">${lu.length}</div><div style="font-size:11px;color:var(--tx3)">涨停</div></div>
    <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px;border-top:3px solid var(--ac)"><div style="font-size:24px;font-weight:700;color:var(--ac)">${br.length}</div><div style="font-size:11px;color:var(--tx3)">炸板</div></div>
    <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px;border-top:3px solid var(--gn)"><div style="font-size:24px;font-weight:700;color:var(--gn)">${ld.length}</div><div style="font-size:11px;color:var(--tx3)">跌停</div></div>
    <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px;border-top:3px solid var(--bl)"><div style="font-size:24px;font-weight:700;color:var(--bl)">${zbRate}%</div><div style="font-size:11px;color:var(--tx3)">炸板率</div></div>
  </div>`;
  
  // 今日主线板块
  html+=`<div style="margin-bottom:16px;padding:12px;background:var(--sf2);border-radius:8px">
    <div style="font-weight:600;color:var(--ac);margin-bottom:8px">📊 今日主线板块</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">`;
  topIndustries.forEach(([name,count])=>{
    let sectorData=sf.find(s=>s.名称===name);
    let chg=sectorData?sectorData.今日涨跌幅:0;
    html+=`<span style="padding:6px 12px;background:var(--sf);border-radius:6px;font-size:12px;border:1px solid var(--bd)">${name} <b style="color:var(--rd)">${count}只涨停</b> <span class="${chg>=0?'up':'dn'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span></span>`;
  });
  html+=`</div></div>`;
  
  // 涨停热门
  html+=`<div style="margin-bottom:16px"><div style="font-weight:600;color:var(--ac);margin-bottom:8px">🔴 涨停热门（按连板数排序）</div>`;
  if(hotZt.length){
    html+=`<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>连板</th><th>涨幅</th><th>成交额</th><th>换手率</th><th>首封时间</th><th>炸板</th><th>封板资金</th><th>行业/涨停原因</th></tr></thead><tbody>`;
    hotZt.forEach(s=>{
      let lb=s.连板数||1;
      let fb=s.封板资金||0;
      let fbStr=fb>1e8?(fb/1e8).toFixed(2)+'亿':fb>1e4?(fb/1e4).toFixed(0)+'万':'--';
      let industry=s.所属行业||'';
      let concepts=stockConceptsCache[s.代码]||[];
      let hotConcepts=concepts.filter(c=>hotConceptsCache.has(c)).slice(0,2);
      let reason=industry+(hotConcepts.length?' · '+hotConcepts.join('/'):'');
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.代码}</td><td>${s.名称} ${lb>=2?'<span class="tag tag-hl">'+lb+'连板</span>':''}</td><td>${lb}</td><td class="up">+${(s.涨跌幅||0).toFixed(2)}%</td><td>${formatVolume(s.成交额||0)}</td><td>${(s.换手率||0).toFixed(2)}%</td><td>${fmtTime(s.首次封板时间)}</td><td>${s.炸板次数||0}</td><td>${fbStr}</td><td style="font-size:11px">${reason}</td></tr>`;
    });
    html+=`</tbody></table>`;
  }else{html+=`<div class="empty">暂无涨停数据</div>`;}
  html+=`</div>`;
  
  // 炸板热门
  html+=`<div style="margin-bottom:16px"><div style="font-weight:600;color:var(--ac);margin-bottom:8px">🟡 炸板热门（按成交额排序）</div>`;
  if(hotZb.length){
    html+=`<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>涨幅</th><th>成交额</th><th>换手率</th><th>首封时间</th><th>炸板次数</th><th>振幅</th><th>行业</th></tr></thead><tbody>`;
    hotZb.forEach(s=>{
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.代码}</td><td>${s.名称}</td><td class="${(s.涨跌幅||0)>=0?'up':'dn'}">${(s.涨跌幅||0)>=0?'+':''}${(s.涨跌幅||0).toFixed(2)}%</td><td>${formatVolume(s.成交额||0)}</td><td>${(s.换手率||0).toFixed(2)}%</td><td>${fmtTime(s.首次封板时间)}</td><td>${s.炸板次数||0}</td><td>${(s.振幅||0).toFixed(2)}%</td><td>${s.所属行业||''}</td></tr>`;
    });
    html+=`</tbody></table>`;
  }else{html+=`<div class="empty">暂无炸板数据</div>`;}
  html+=`</div>`;
  
  // 跌停热门
  html+=`<div style="margin-bottom:16px"><div style="font-weight:600;color:var(--ac);margin-bottom:8px">🟢 跌停热门（按连续跌停排序）</div>`;
  if(hotDt.length){
    html+=`<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>跌幅</th><th>连续跌停</th><th>成交额</th><th>换手率</th><th>封板时间</th><th>行业</th></tr></thead><tbody>`;
    hotDt.forEach(s=>{
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.代码}</td><td>${s.名称}</td><td class="dn">${(s.涨跌幅||0).toFixed(2)}%</td><td>${s.连续跌停||1}</td><td>${formatVolume(s.成交额||0)}</td><td>${(s.换手率||0).toFixed(2)}%</td><td>${fmtTime(s.最后封板时间)}</td><td>${s.所属行业||''}</td></tr>`;
    });
    html+=`</tbody></table>`;
  }else{html+=`<div class="empty">暂无跌停数据</div>`;}
  html+=`</div>`;
  
  // 资金流入板块
  html+=`<div><div style="font-weight:600;color:var(--ac);margin-bottom:8px">💰 资金流入板块TOP10</div>`;
  if(hotSector.length){
    html+=`<table class="tbl"><thead><tr><th>板块</th><th>涨跌幅</th><th>净流入</th><th>流入</th><th>流出</th><th>领涨股</th></tr></thead><tbody>`;
    hotSector.forEach(s=>{
      let v=s.净额||0;
      html+=`<tr><td style="font-weight:600">${s.名称}</td><td class="${(s.今日涨跌幅||0)>=0?'up':'dn'}">${(s.今日涨跌幅||0)>=0?'+':''}${(s.今日涨跌幅||0).toFixed(2)}%</td><td class="${v>=0?'up':'dn'}">${v>=0?'+':''}${v.toFixed(2)}亿</td><td>${(s.流入资金||0).toFixed(2)}亿</td><td>${(s.流出资金||0).toFixed(2)}亿</td><td>${s.领涨股||''} ${(s['领涨股-涨跌幅']||0).toFixed(2)}%</td></tr>`;
    });
    html+=`</tbody></table>`;
  }else{html+=`<div class="empty">暂无板块数据</div>`;}
  html+=`</div>`;
  
  html+=`</div></div></div>`;
  el.innerHTML=html;
}

// 查看股票详情（K线、分时、原因等）
async function viewStockDetail(code){
  let modal=document.getElementById('stockDetailModal');
  if(!modal){
    modal=document.createElement('div');
    modal.id='stockDetailModal';
    modal.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    document.body.appendChild(modal);
  }
  modal.innerHTML=`<div style="background:var(--sf);border-radius:12px;width:92%;max-width:1000px;max-height:90vh;overflow-y:auto;padding:20px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px"><b style="color:var(--ac);font-size:16px">📊 ${code} 详细信息</b><button class="btn" onclick="document.getElementById('stockDetailModal').remove()" style="font-size:16px">✕</button></div><div class="loading">加载中...</div></div>`;
  modal.style.display='flex';
  modal.onclick=function(e){if(e.target===modal)modal.remove();};
  
  try{
    let r=await fetch('/api/stock-detail/'+code);
    let d=await r.json();
    let base=d.基本信息||{};
    let zt=d.涨停信息||{};
    let concepts=d.所属概念||[];
    
    // Build iframe URL for K-line chart
    let marketPrefix=code.startsWith('6')?'SH':'SZ';
    let klineUrl='https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code='+marketPrefix+code+'&color=r#/kline';
    let quoteUrl=code.startsWith('6')?'https://quote.eastmoney.com/concept/sh'+code+'.html':'https://quote.eastmoney.com/concept/sz'+code+'.html';
    
    // Fetch real-time quote for more info
    let quoteData=null;
    try{let qr=await fetch('/api/stock-quote/'+code);quoteData=await qr.json();}catch(e){}

    let html=`<div style="background:var(--sf);border-radius:12px;width:92%;max-width:1000px;max-height:90vh;overflow-y:auto;padding:20px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <div><b style="color:var(--ac);font-size:18px">${quoteData?.名称||base.名称||code}</b> <span style="color:var(--tx3)">${code}</span></div>
        <div style="display:flex;gap:8px;align-items:center">
          <a href="${quoteUrl}" target="_blank" class="btn" style="font-size:11px">📈 行情页</a>
          <button class="btn" onclick="document.getElementById('stockDetailModal').remove()" style="font-size:16px">✕</button>
        </div>
      </div>
      
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px">
        <div style="text-align:center;padding:10px;background:var(--sf2);border-radius:8px"><div style="font-size:18px;font-weight:700;color:${(base.涨跌幅||0)>=0?'var(--rd)':'var(--gn)'}">${(base.最新价||quoteData?.最新价||0).toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">最新价</div></div>
        <div style="text-align:center;padding:10px;background:var(--sf2);border-radius:8px"><div style="font-size:18px;font-weight:700;color:${(base.涨跌幅||0)>=0?'var(--rd)':'var(--gn)'}">${(base.涨跌幅||0)>=0?'+':''}${(base.涨跌幅||quoteData?.涨跌幅||0).toFixed(2)}%</div><div style="font-size:10px;color:var(--tx3)">涨跌幅</div></div>
        <div style="text-align:center;padding:10px;background:var(--sf2);border-radius:8px"><div style="font-size:18px;font-weight:700">${(base.换手率||quoteData?.换手率||0).toFixed(2)}%</div><div style="font-size:10px;color:var(--tx3)">换手率</div></div>
        <div style="text-align:center;padding:10px;background:var(--sf2);border-radius:8px"><div style="font-size:18px;font-weight:700">${formatVolume(base.成交额||quoteData?.成交额||0)}</div><div style="font-size:10px;color:var(--tx3)">成交额</div></div>
        <div style="text-align:center;padding:10px;background:var(--sf2);border-radius:8px"><div style="font-size:18px;font-weight:700">${(quoteData?.量比||0).toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">量比</div></div>
      </div>
      
      ${quoteData&&!quoteData.error?`<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px">
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.今开.toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">今开</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.最高.toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">最高</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.最低.toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">最低</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.振幅.toFixed(2)}%</div><div style="font-size:10px;color:var(--tx3)">振幅</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.市盈率.toFixed(1)}</div><div style="font-size:10px;color:var(--tx3)">市盈率</div></div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px">
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.昨收.toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">昨收</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${quoteData.市净率.toFixed(2)}</div><div style="font-size:10px;color:var(--tx3)">市净率</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${formatVolume(quoteData.总市值)}</div><div style="font-size:10px;color:var(--tx3)">总市值</div></div>
        <div style="text-align:center;padding:8px;background:var(--sf2);border-radius:8px"><div style="font-size:13px;font-weight:600">${formatVolume(quoteData.流通市值)}</div><div style="font-size:10px;color:var(--tx3)">流通市值</div></div>
      </div>`:''}`;
    
    if(zt.连板数){
      html+=`<div style="margin-bottom:16px;padding:12px;background:var(--sf2);border-radius:8px">
        <div style="font-weight:600;color:var(--ac);margin-bottom:8px">📈 涨停信息</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px">
          <span>连板数: <b>${zt.连板数}</b></span>
          <span>涨停统计: <b>${zt.涨停统计||'--'}</b></span>
          <span>所属行业: <b>${zt.所属行业||'--'}</b></span>
          <span>首封时间: <b>${zt.首次封板时间||'--'}</b></span>
          <span>末封时间: <b>${zt.最后封板时间||'--'}</b></span>
          <span>炸板次数: <b>${zt.炸板次数||0}</b></span>
          <span>封板资金: <b>${formatVolume(zt.封板资金||0)}</b></span>
        </div>
      </div>`;
    }
    
    if(concepts.length){
      html+=`<div style="margin-bottom:16px;padding:12px;background:var(--sf2);border-radius:8px">
        <div style="font-weight:600;color:var(--ac);margin-bottom:8px">🏷️ 所属概念</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">${concepts.map(c=>`<span class="tag" style="background:var(--sf3);padding:4px 10px">${c}</span>`).join('')}</div>
      </div>`;
    }
    
    // K-line and intraday charts using echarts
    html+=`<div style="margin-bottom:16px">
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <button class="btn" onclick="loadStockChart('${code}','kline')" style="background:var(--ac);color:#000;border:none" id="chartKlineBtn">📊 K线图</button>
        <button class="btn" onclick="loadStockChart('${code}','intraday')" id="chartIntradayBtn">⏱ 分时图</button>
      </div>
      <div id="stockChartContainer" style="width:100%;height:400px;background:var(--sf2);border-radius:8px;border:1px solid var(--bd)"><div class="loading" style="padding-top:180px">点击上方按钮加载图表</div></div>
    </div>`;
    
    html+=`</div>`;
    modal.innerHTML=html;
    // Auto-load K-line chart
    setTimeout(()=>loadStockChart(code,'kline'),100);
  }catch(e){
    modal.innerHTML=`<div style="background:var(--sf);border-radius:12px;width:90%;max-width:900px;padding:20px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px"><b style="color:var(--ac)">📊 ${code}</b><button class="btn" onclick="document.getElementById('stockDetailModal').remove()">✕ 关闭</button></div><div class="ai-error">加载失败: ${e.message}</div></div>`;
  }
}

let stockChartInstance=null;
async function loadStockChart(code,type){
  const container=document.getElementById('stockChartContainer');
  if(!container)return;
  container.innerHTML='<div class="loading" style="padding-top:180px">加载图表数据...</div>';
  
  // Update button styles
  const kBtn=document.getElementById('chartKlineBtn');
  const iBtn=document.getElementById('chartIntradayBtn');
  if(kBtn){kBtn.style.background=type==='kline'?'var(--ac)':'var(--sf)';kBtn.style.color=type==='kline'?'#000':'var(--tx2)';}
  if(iBtn){iBtn.style.background=type==='intraday'?'var(--ac)':'var(--sf)';iBtn.style.color=type==='intraday'?'#000':'var(--tx2)';}
  
  try{
    if(stockChartInstance){stockChartInstance.dispose();}
    stockChartInstance=echarts.init(container,null,{renderer:'canvas'});
    
    if(type==='kline'){
      let r=await fetch('/api/stock-kline/'+code+'?days=120');
      let data=await r.json();
      if(data.error||!data.length){container.innerHTML='<div class="empty">暂无K线数据</div>';return;}
      
      let dates=data.map(d=>d.日期);
      let ohlc=data.map(d=>[d.开盘,d.收盘,d.最低,d.最高]);
      let volumes=data.map(d=>d.成交量);
      let colors=data.map(d=>d.收盘>=d.开盘?'#e85454':'#4ecb71');
      
      let option={
        backgroundColor:'transparent',
        animation:false,
        tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
        grid:[{left:'8%',right:'3%',top:'8%',height:'55%'},{left:'8%',right:'3%',top:'70%',height:'20%'}],
        xAxis:[{type:'category',data:dates,boundaryGap:true,gridIndex:0},{type:'category',data:dates,boundaryGap:true,gridIndex:1}],
        yAxis:[{scale:true,gridIndex:0,splitLine:{lineStyle:{color:'rgba(255,255,255,.05)'}}},{scale:true,gridIndex:1,splitLine:{lineStyle:{color:'rgba(255,255,255,.05)'}}}],
        dataZoom:[{type:'inside',xAxisIndex:[0,1],start:60,end:100}],
        series:[
          {type:'candlestick',data:ohlc,xAxisIndex:0,yAxisIndex:0,itemStyle:{color:'#e85454',color0:'#4ecb71',borderColor:'#e85454',borderColor0:'#4ecb71'}},
          {type:'bar',data:volumes.map((v,i)=>({value:v,itemStyle:{color:colors[i]+'80'}})),xAxisIndex:1,yAxisIndex:1}
        ]
      };
      stockChartInstance.setOption(option);
    }else{
      let r=await fetch('/api/stock-intraday/'+code);
      let data=await r.json();
      if(data.error||!data.length){container.innerHTML='<div class="empty">暂无分时数据</div>';return;}
      
      let times=data.map(d=>d.时间);
      let prices=data.map(d=>d.收盘);
      let volumes=data.map(d=>d.成交量);
      let firstPrice=prices[0]||0;
      
      let option={
        backgroundColor:'transparent',
        animation:false,
        tooltip:{trigger:'axis'},
        grid:[{left:'8%',right:'3%',top:'8%',height:'55%'},{left:'8%',right:'3%',top:'70%',height:'20%'}],
        xAxis:[{type:'category',data:times,gridIndex:0},{type:'category',data:times,gridIndex:1}],
        yAxis:[{scale:true,gridIndex:0,splitLine:{lineStyle:{color:'rgba(255,255,255,.05)'}}},{scale:true,gridIndex:1,splitLine:{lineStyle:{color:'rgba(255,255,255,.05)'}}}],
        series:[
          {type:'line',data:prices,xAxisIndex:0,yAxisIndex:0,symbol:'none',lineStyle:{color:'#5b8def',width:1.5},areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{offset:0,color:'rgba(91,141,239,.3)'},{offset:1,color:'rgba(91,141,239,.02)'}]}},markLine:{silent:true,data:[{yAxis:firstPrice,lineStyle:{color:'var(--tx3)',type:'dashed'}}]}},
          {type:'bar',data:volumes,xAxisIndex:1,yAxisIndex:1,itemStyle:{color:'rgba(91,141,239,.4)'}}
        ]
      };
      stockChartInstance.setOption(option);
    }
    
    window.addEventListener('resize',()=>{if(stockChartInstance)stockChartInstance.resize();});
  }catch(e){container.innerHTML='<div class="empty">图表加载失败: '+e.message+'</div>';}
}

/* ===== 主力净流入页面 ===== */
let mainFlowCache=null;
async function renderMainFlow(el){
  // 先显示缓存数据
  if(mainFlowCache){
    renderMainFlowTable(el, mainFlowCache, true);
  }else{
    el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t"><i data-lucide="arrow-right-left" style="width:16px;height:16px;display:inline;vertical-align:-3px"></i> 个股主力净流入TOP50</span><button class="btn" onclick="refreshMainFlow()" style="padding:4px 12px;font-size:12px"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新</button></div><div class="card-b" style="padding:16px"><div class="loading">首次加载中...</div></div></div></div>';
  }
  // 后台获取新数据
  await fetchMainFlowData(el);
}

async function fetchMainFlowData(el){
  try{
    let r=await fetch('/api/main-fund-flow');
    let data=await r.json();
    if(curTab!=='main_flow')return;
    if(!data.length||data.error){
      if(!mainFlowCache){
        el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无数据，点击刷新重试</div></div></div>';
      }
      return;
    }
    mainFlowCache=data;
    renderMainFlowTable(el, data, false);
  }catch(e){
    if(!mainFlowCache){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败，点击刷新重试</div></div></div>';
    }
  }
}

function refreshMainFlow(){
  mainFlowCache=null;
  renderMainFlow(document.getElementById('tabContent'));
}

function renderMainFlowTable(el, data, isCache){
    // 加载概念数据
    let codes=data.map(s=>s['代码']).filter(c=>c);
    loadStockConcepts(codes);
    
    let cacheLabel=isCache?' <span style="font-size:10px;color:var(--ac)">（缓存）</span>':'';
    let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t"><i data-lucide="arrow-right-left" style="width:16px;height:16px;display:inline;vertical-align:-3px"></i> 个股主力净流入TOP50${cacheLabel}</span><button class="btn" onclick="refreshMainFlow()" style="padding:4px 12px;font-size:12px"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新</button></div><div class="card-b"><table class="tbl"><thead><tr><th>排名</th><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>主力净流入</th><th>换手率</th><th>成交额</th><th>所属概念</th></tr></thead><tbody>`;
    data.forEach((s,i)=>{
      let mainFlow=s['主力净流入-净额']||0;
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${i+1}</td><td>${s.代码}</td><td>${s.名称}</td><td>${(s.最新价||0).toFixed(2)}</td><td class="${(s.涨跌幅||0)>=0?'up':'dn'}">${(s.涨跌幅||0)>=0?'+':''}${(s.涨跌幅||0).toFixed(2)}%</td><td class="${mainFlow>=0?'up':'dn'}">${formatVolume(mainFlow)}</td><td>${(s.换手率||0).toFixed(2)}%</td><td>${formatVolume(s.成交额||0)}</td><td>${getConceptTags(s.代码,3)}</td></tr>`;
    });
    html+='</tbody></table></div></div></div>';
    el.innerHTML=html;
}

/* ===== 昨日涨停表现页面 ===== */
async function renderYesterdayZT(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📊 昨日涨停今日表现</span></div><div class="card-b" style="padding:16px"><div class="loading">加载中...</div></div></div></div>';
  try{
    let r=await fetch('/api/yesterday-zt-performance');
    let d=await r.json();
    if(curTab!=='limit_center'||subTabs.limitCenter!=='yesterday_zt')return;
    if(d.error){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';
      return;
    }
    // 加载概念数据
    let codes=(d.data||[]).map(s=>s.代码).filter(c=>c);
    await loadStockConcepts(codes);
    if(curTab!=='limit_center'||subTabs.limitCenter!=='yesterday_zt')return;
    
    let stats=d.stats||{};
    let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📊 昨日涨停今日表现</span><span class="card-c">昨日${d.count}只涨停</span></div><div class="card-b" style="padding:16px">
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px">
        <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px"><div style="font-size:20px;font-weight:700;color:var(--rd)">${stats.涨停||0}</div><div style="font-size:11px;color:var(--tx3)">继续涨停</div></div>
        <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px"><div style="font-size:20px;font-weight:700;color:var(--ac)">${stats.炸板||0}</div><div style="font-size:11px;color:var(--tx3)">炸板</div></div>
        <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px"><div style="font-size:20px;font-weight:700;color:var(--gn)">${stats.跌停||0}</div><div style="font-size:11px;color:var(--tx3)">今日跌停</div></div>
        <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px"><div style="font-size:20px;font-weight:700;color:var(--rd)">${stats.上涨||0}</div><div style="font-size:11px;color:var(--tx3)">上涨</div></div>
        <div style="text-align:center;padding:12px;background:var(--sf2);border-radius:8px"><div style="font-size:20px;font-weight:700;color:var(--gn)">${stats.下跌||0}</div><div style="font-size:11px;color:var(--tx3)">下跌</div></div>
      </div>
      <table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>昨日连板</th><th>今日状态</th><th>今日涨跌幅</th><th>所属行业</th><th>所属概念</th></tr></thead><tbody>`;
    (d.data||[]).forEach(s=>{
      let statusColor=s.今日状态==='涨停'?'var(--rd)':s.今日状态==='跌停'?'var(--gn)':s.今日状态==='炸板'?'var(--ac)':'var(--tx3)';
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.代码}</td><td>${s.名称}</td><td>${s.昨日连板}</td><td style="color:${statusColor};font-weight:600">${s.今日状态}</td><td class="${(s.今日涨跌幅||0)>=0?'up':'dn'}">${(s.今日涨跌幅||0)>=0?'+':''}${(s.今日涨跌幅||0).toFixed(2)}%</td><td style="font-size:11px">${s.所属行业||''}</td><td>${getConceptTags(s.代码)}</td></tr>`;
    });
    html+='</tbody></table></div></div></div>';
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败</div></div></div>';
  }
}

/* ===== 新高票页面 ===== */
async function renderNewHighs(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📈 创新高股票</span></div><div class="card-b" style="padding:16px"><div class="loading">加载中...</div></div></div></div>';
  try{
    let r=await fetch('/api/new-highs');
    let d=await r.json();
    if(curTab!=='new_highs')return; // 用户已切换标签，停止渲染
    let data=d.创新高||[];
    if(!data.length){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无数据</div></div></div>';
      return;
    }
    // 加载概念数据
    let codes=data.map(s=>s['代码']).filter(c=>c);
    await loadStockConcepts(codes);
    if(curTab!=='new_highs')return; // 再次检查
    
    let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📈 创新高股票</span><span class="card-c">${data.length}只</span></div><div class="card-b"><table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>行业</th><th>所属概念</th></tr></thead><tbody>`;
    data.forEach(s=>{
      html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.代码}</td><td>${s.名称}</td><td>${(s.最新价||0).toFixed(2)}</td><td class="${(s.涨跌幅||0)>=0?'up':'dn'}">${(s.涨跌幅||0)>=0?'+':''}${(s.涨跌幅||0).toFixed(2)}%</td><td>${formatVolume(s.成交额||0)}</td><td>${(s.换手率||0).toFixed(2)}%</td><td>${s.所属行业||''}</td><td>${getConceptTags(s.代码,5)}</td></tr>`;
    });
    html+='</tbody></table></div></div></div>';
    el.innerHTML=html;
  }catch(e){
    if(curTab==='new_highs')el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败</div></div></div>';
  }
}

/* ===== 基金推荐页面 ===== */
async function renderFund(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">💎 基金推荐</span></div><div class="card-b" style="padding:16px"><div class="loading">加载中...</div></div></div></div>';
  try{
    let r=await fetch('/api/fund-recommend');
    let d=await r.json();
    if(curTab!=='fund')return;
    if(d.error){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';return;}

    let sectors=d.sectors||[];
    let recommended=d.recommended||[];

    let html=`<div class="tab-full">
      <div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">📊 当前市场环境</span></div><div class="card-b" style="padding:16px">
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <div style="padding:12px;background:var(--sf2);border-radius:8px;flex:1;min-width:200px">
            <div style="font-weight:600;color:var(--ac);margin-bottom:6px">🐂 市场判断</div>
            <div style="font-size:13px;color:var(--tx2)">${d.market_view||'当前市场以科技板块为主，慢牛格局'}</div>
          </div>
          <div style="padding:12px;background:var(--sf2);border-radius:8px;flex:1;min-width:200px">
            <div style="font-weight:600;color:var(--ac);margin-bottom:6px">🎯 配置建议</div>
            <div style="font-size:13px;color:var(--tx2)">${d.advice||'建议重点关注科技主题基金，适当配置半导体、AI方向'}</div>
          </div>
        </div>
      </div></div>`;

    // 按板块分组展示基金
    sectors.forEach(sec=>{
      html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">${sec.icon||'📁'} ${sec.name}</span><span class="card-c">${(sec.funds||[]).length}只</span></div><div class="card-b"><table class="tbl"><thead><tr><th>基金代码</th><th>基金名称</th><th>类型</th><th>近1月</th><th>近3月</th><th>近1年</th><th>权重评分</th><th>推荐理由</th></tr></thead><tbody>`;
      (sec.funds||[]).forEach(f=>{
        let scoreColor=f.score>=80?'var(--rd)':f.score>=60?'var(--ac)':'var(--tx3)';
        html+=`<tr><td style="color:var(--bl);font-weight:500">${f.code}</td><td>${f.name}</td><td style="font-size:11px">${f.type||'--'}</td>
          <td class="${(f.m1||0)>=0?'up':'dn'}">${(f.m1||0)>=0?'+':''}${(f.m1||0).toFixed(2)}%</td>
          <td class="${(f.m3||0)>=0?'up':'dn'}">${(f.m3||0)>=0?'+':''}${(f.m3||0).toFixed(2)}%</td>
          <td class="${(f.y1||0)>=0?'up':'dn'}">${(f.y1||0)>=0?'+':''}${(f.y1||0).toFixed(2)}%</td>
          <td><span style="color:${scoreColor};font-weight:700;font-size:14px">${f.score||'--'}</span></td>
          <td style="font-size:11px;color:var(--tx3);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${f.reason||''}">${f.reason||'--'}</td></tr>`;
      });
      html+=`</tbody></table></div></div>`;
    });

    // 推荐基金汇总
    if(recommended.length){
      html+=`<div class="card"><div class="card-h"><span class="card-t">⭐ 重点推荐 TOP10</span></div><div class="card-b"><table class="tbl"><thead><tr><th>排名</th><th>基金代码</th><th>基金名称</th><th>板块</th><th>权重评分</th><th>推荐理由</th></tr></thead><tbody>`;
      recommended.forEach((f,i)=>{
        let medal=i<3?['🥇','🥈','🥉'][i]:`${i+1}`;
        let scoreColor=f.score>=80?'var(--rd)':f.score>=60?'var(--ac)':'var(--tx3)';
        html+=`<tr><td style="font-size:16px">${medal}</td><td style="color:var(--bl);font-weight:500">${f.code}</td><td>${f.name}</td><td><span style="background:rgba(167,139,250,.1);color:var(--ac);padding:2px 8px;border-radius:4px;font-size:11px">${f.sector||'--'}</span></td><td><span style="color:${scoreColor};font-weight:700;font-size:16px">${f.score||'--'}</span></td><td style="font-size:11px;color:var(--tx3)">${f.reason||'--'}</td></tr>`;
      });
      html+=`</tbody></table></div></div>`;
    }

    html+=`</div>`;
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
  }
}


/* ===== 异动预测页面 ===== */
async function renderAnomalyPrediction(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">⚡ 异动预测 · 30日偏离值</span></div><div class="card-b" style="padding:16px"><div class="loading">正在加载异动预测数据...</div></div></div></div>';
  try{
    let r=await fetch('/api/anomaly-prediction');
    let d=await r.json();
    if(curTab!=='anomaly')return;
    
    // 处理扫描状态
    if(d.scanning){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t"><i data-lucide="activity" style="width:16px;height:16px;display:inline;vertical-align:-3px"></i> 异动预测 · 30日偏离值</span></div><div class="card-b" style="padding:40px;text-align:center"><div style="font-size:40px;margin-bottom:16px"><i data-lucide="loader-2" style="width:40px;height:40px;animation:spin 1s linear infinite"></i></div><div style="font-size:15px;color:var(--or);font-weight:600;margin-bottom:8px">'+(d.message||'扫描进行中...')+'</div><div style="font-size:12px;color:var(--tx3)">首次扫描需要3-5分钟，扫描完成后自动缓存，下次秒加载</div><div style="margin-top:16px"><button class="btn" onclick="renderAnomalyPrediction(document.getElementById(\x27tabContent\x27))" style="background:var(--ac);color:#fff">🔄 刷新</button></div></div></div></div>';
      // 30秒后自动刷新
      setTimeout(()=>{if(curTab==='anomaly')renderAnomalyPrediction(document.getElementById('tabContent'));},30000);
      return;
    }
    
    if(!d||!d.length){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">⚡ 异动预测 · 30日偏离值</span></div><div class="card-b"><div class="empty">暂无接近200%偏离值的股票（首次扫描可能需要3-5分钟）</div></div></div></div>';
      return;
    }
    
    // 按状态分组
    let triggered=d.filter(s=>s['状态']==='已触发');
    let approaching=d.filter(s=>s['状态']==='接近触发');
    
    let html='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">⚡ 异动预测 · 30日偏离值</span><span class="card-c">'+d.length+'只异动</span></div><div class="card-b" style="max-height:800px;overflow-y:auto">';
    
    // 说明区
    html+='<div style="padding:12px 16px;background:rgba(255,149,0,.08);border-left:3px solid var(--or);margin-bottom:12px;border-radius:0 8px 8px 0">';
    html+='<div style="font-size:13px;color:var(--or);font-weight:600;margin-bottom:4px">📋 规则说明</div>';
    html+='<div style="font-size:11px;color:var(--tx3);line-height:1.6">根据交易所规则：连续30个交易日收盘价涨跌幅偏离值累计达到±200%，将触发异常交易监控。<br>';
    html+='<span style="color:var(--rd)">● 已触发（≥200%）</span>  <span style="color:var(--or)">● 接近触发（150%-200%）</span></div></div>';
    
    // 已触发
    if(triggered.length){
      html+='<div style="padding:8px 16px;font-weight:600;color:var(--rd);font-size:13px">🔴 已触发（≥200%）· '+triggered.length+'只</div>';
      html+='<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>今日</th><th>30日偏离</th><th>30日涨跌</th><th>30日最高</th><th>30日最低</th><th>最高日涨</th><th>最低日跌</th></tr></thead><tbody>';
      triggered.forEach(s=>{
        let chgClass=s['今日涨跌幅']>=0?'up':'dn';
        let cumClass=s['30日累计涨跌幅']>=0?'up':'dn';
        html+='<tr style="cursor:pointer" onclick="viewStockDetail(\''+s['代码']+'\')">';
        html+='<td class="pf-code">'+s['代码']+'</td>';
        html+='<td class="pf-name">'+s['名称']+'</td>';
        html+='<td>'+s['现价'].toFixed(2)+'</td>';
        html+='<td class="'+chgClass+'">'+(s['今日涨跌幅']>=0?'+':'')+s['今日涨跌幅'].toFixed(2)+'%</td>';
        html+='<td style="color:var(--rd);font-weight:700;font-size:14px">'+s['30日累计偏离']+'%</td>';
        html+='<td class="'+cumClass+'">'+(s['30日累计涨跌幅']>=0?'+':'')+s['30日累计涨跌幅'].toFixed(2)+'%</td>';
        html+='<td>'+s['30日最高'].toFixed(2)+'</td>';
        html+='<td>'+s['30日最低'].toFixed(2)+'</td>';
        html+='<td class="up">+'+s['30日最大日涨幅'].toFixed(2)+'%</td>';
        html+='<td class="dn">'+s['30日最大日跌幅'].toFixed(2)+'%</td>';
        html+='</tr>';
      });
      html+='</tbody></table>';
    }
    
    // 接近触发
    if(approaching.length){
      html+='<div style="padding:8px 16px;font-weight:600;color:var(--or);font-size:13px;margin-top:8px">🟠 接近触发（150%-200%）· '+approaching.length+'只</div>';
      html+='<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>今日</th><th>30日偏离</th><th>30日涨跌</th><th>30日最高</th><th>30日最低</th><th>最高日涨</th><th>最低日跌</th></tr></thead><tbody>';
      approaching.forEach(s=>{
        let chgClass=s['今日涨跌幅']>=0?'up':'dn';
        let cumClass=s['30日累计涨跌幅']>=0?'up':'dn';
        let pct=s['30日累计偏离']/200*100;
        html+='<tr style="cursor:pointer" onclick="viewStockDetail(\''+s['代码']+'\')">';
        html+='<td class="pf-code">'+s['代码']+'</td>';
        html+='<td class="pf-name">'+s['名称']+'</td>';
        html+='<td>'+s['现价'].toFixed(2)+'</td>';
        html+='<td class="'+chgClass+'">'+(s['今日涨跌幅']>=0?'+':'')+s['今日涨跌幅'].toFixed(2)+'%</td>';
        html+='<td style="color:var(--or);font-weight:600">';
        html+='<div style="position:relative">'+s['30日累计偏离']+'%';
        html+='<div style="height:3px;background:var(--bd);border-radius:2px;margin-top:2px"><div style="height:100%;background:var(--or);border-radius:2px;width:'+Math.min(pct,100)+'%"></div></div></div></td>';
        html+='<td class="'+cumClass+'">'+(s['30日累计涨跌幅']>=0?'+':'')+s['30日累计涨跌幅'].toFixed(2)+'%</td>';
        html+='<td>'+s['30日最高'].toFixed(2)+'</td>';
        html+='<td>'+s['30日最低'].toFixed(2)+'</td>';
        html+='<td class="up">+'+s['30日最大日涨幅'].toFixed(2)+'%</td>';
        html+='<td class="dn">'+s['30日最大日跌幅'].toFixed(2)+'%</td>';
        html+='</tr>';
      });
      html+='</tbody></table>';
    }
    
    html+='</div></div></div>';
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
  }
}


     /* ===== 涨跌停原因 + 最高板 + 炸板金额页面 ===== */
     async function renderLimitReasons(el){
       el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📋 涨跌停原因 + 最高板</span></div><div class="card-b" style="padding:16px"><div class="loading">加载中...</div></div></div></div>';
       try{
         let r=await fetch('/api/limit-reasons');
         let d=await r.json();
         if(curTab!=='limit_center'||(subTabs.limitCenter!=='limit_reasons'&&subTabs.limitCenter!=='highest_board'))return;
         if(d.error){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';return;}
    
        let hb=d.highest_board||{};
        let lbDist=d.lb_distribution||{};
        let topInd=d.top_industries||[];
    
        let html='<div class="tab-full">';
    
        // 核心指标卡片
        html+=`<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px">
          <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px;box-shadow:var(--shadow)"><div style="font-size:11px;color:var(--tx3);margin-bottom:4px">👑 最高板</div><div style="font-size:28px;font-weight:800;color:var(--rd)">${hb.boards||0}板</div><div style="font-size:12px;color:var(--tx2);margin-top:2px">${hb.name||'--'} ${hb.industry||''}</div></div>
          <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px;box-shadow:var(--shadow)"><div style="font-size:11px;color:var(--tx3);margin-bottom:4px">🔴 涨停数</div><div style="font-size:28px;font-weight:800;color:var(--rd)">${d.limit_up_count||0}</div></div>
          <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px;box-shadow:var(--shadow)"><div style="font-size:11px;color:var(--tx3);margin-bottom:4px">🟢 跌停数</div><div style="font-size:28px;font-weight:800;color:var(--gn)">${d.limit_down_count||0}</div></div>
          <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px;box-shadow:var(--shadow)"><div style="font-size:11px;color:var(--tx3);margin-bottom:4px">💥 炸板率</div><div style="font-size:28px;font-weight:800;color:var(--or)">${d.broken_rate||0}%</div><div style="font-size:11px;color:var(--tx3)">${d.broken_count||0}只</div></div>
          <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px;box-shadow:var(--shadow)"><div style="font-size:11px;color:var(--tx3);margin-bottom:4px">💰 炸板金额</div><div style="font-size:22px;font-weight:800;color:var(--ac)">${formatVolume(d.broken_amount||0)}</div></div>
        </div>`;
    
        // 连板梯队分布
        html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">🏗️ 连板梯队分布</span></div><div class="card-b" style="padding:16px"><div style="display:flex;gap:10px;flex-wrap:wrap">`;
        let sortedLb=Object.entries(lbDist).sort((a,b)=>Number(b[0])-Number(a[0]));
        sortedLb.forEach(([lb,count])=>{
          let color=Number(lb)>=5?'var(--rd)':Number(lb)>=3?'var(--or)':'var(--ac)';
          html+=`<div style="text-align:center;padding:12px 20px;background:var(--sf2);border:1px solid var(--bd);border-radius:8px;min-width:80px"><div style="font-size:24px;font-weight:800;color:${color}">${count}</div><div style="font-size:11px;color:var(--tx3)">${lb}连板</div></div>`;
        });
        html+='</div></div></div>';
    
        // 行业涨停TOP10
        html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">🏭 行业涨停TOP10</span></div><div class="card-b" style="padding:16px"><div style="display:flex;flex-direction:column;gap:8px">`;
        let maxIndCount=topInd.length?topInd[0].count:1;
        topInd.forEach((ind,i)=>{
          let pct=Math.round(ind.count/maxIndCount*100);
          let medal=i<3?['🥇','🥈','🥉'][i]:'#'+(i+1);
          html+=`<div style="display:flex;align-items:center;gap:10px"><span style="min-width:30px;text-align:center;font-size:14px">${medal}</span><span style="min-width:80px;font-weight:600;font-size:13px">${ind.name}</span><div style="flex:1;height:22px;background:var(--sf2);border-radius:6px;overflow:hidden"><div style="height:100%;background:linear-gradient(90deg,rgba(255,59,48,.2),var(--rd));border-radius:6px;width:${pct}%;display:flex;align-items:center;justify-content:flex-end;padding-right:8px"><span style="font-size:11px;font-weight:700;color:#fff">${ind.count}</span></div></div></div>`;
        });
        html+='</div></div></div>';
    
        // 涨停原因明细表
        html+=`<div class="card"><div class="card-h"><span class="card-t">📋 涨停原因明细</span><span class="card-c">${(d.limit_up||[]).length}只</span></div><div class="card-b"><table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>连板</th><th>涨停原因</th><th>封板资金</th><th>首封时间</th><th>炸板</th><th>行业</th></tr></thead><tbody>`;
        (d.limit_up||[]).forEach(s=>{
          let lb=s['连板数']||1;
          let lbTag=lb>=3?`<span style="background:rgba(255,59,48,.15);color:var(--rd);padding:2px 8px;border-radius:6px;font-weight:700;font-size:12px">${lb}板</span>`:`<span style="color:var(--tx3)">${lb}板</span>`;
          let reason=getZtReason(s);
          html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s['代码']}')"><td class="pf-code">${s['代码']}</td><td>${s['名称']}</td><td>${lbTag}</td><td style="font-size:11px;color:var(--ac)">${reason}</td><td>${formatVolume(s['封板资金']||0)}</td><td style="font-size:11px">${s['首次封板时间']||'--'}</td><td>${s['炸板次数']||0}</td><td style="font-size:11px">${s['所属行业']||''}</td></tr>`;
        });
        html+='</tbody></table></div></div></div>';
        el.innerHTML=html;
      }catch(e){
        el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
      }
    }
    
    /* ===== 趋势票高度页面 ===== */
    async function renderTrendStocks(el){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📈 趋势票高度</span></div><div class="card-b" style="padding:16px"><div class="loading">扫描趋势票中（约30秒）...</div></div></div></div>';
      try{
        let r=await fetch('/api/trend-stocks');
        let d=await r.json();
        if(curTab!=='trend')return;
        if(!d.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无趋势票数据</div></div></div>';return;}
    
        let codes=d.map(s=>s['代码']).filter(c=>c);
        await loadStockConcepts(codes);
        if(curTab!=='trend')return;
    
        let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📈 趋势票高度（连续上涨+MA多头排列）</span><span class="card-c">${d.length}只</span></div><div class="card-b"><table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>今日涨幅</th><th>连续上涨</th><th>20日涨幅</th><th>趋势强度</th><th>成交额</th><th>所属概念</th></tr></thead><tbody>`;
        d.forEach(s=>{
          let strength=s['趋势强度']||0;
          let sColor=strength>=80?'var(--rd)':strength>=60?'var(--or)':'var(--ac)';
          let upDays=s['连续上涨']||0;
          let dColor=upDays>=7?'var(--rd)':upDays>=5?'var(--or)':'var(--ac)';
          html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s['代码']}')"><td class="pf-code">${s['代码']}</td><td>${s['名称']}</td><td>${(s['最新价']||0).toFixed(2)}</td><td class="${(s['今日涨幅']||0)>=0?'up':'dn'}">${(s['今日涨幅']||0)>=0?'+':''}${(s['今日涨幅']||0).toFixed(2)}%</td><td style="color:${dColor};font-weight:700;font-size:15px">${upDays}天↑</td><td class="up">+${(s['20日涨幅']||0).toFixed(2)}%</td><td><span style="color:${sColor};font-weight:700;font-size:14px">${strength}</span></td><td>${formatVolume(s['成交额']||0)}</td><td>${getConceptTags(s['代码'],3)}</td></tr>`;
        });
        html+='</tbody></table></div></div></div>';
        el.innerHTML=html;
      }catch(e){
        el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
      }
    }
    
    /* ===== 板块龙头/跟风/补涨页面 ===== */
    async function renderSectorLFS(el){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">👑 板块龙头 · 跟风 · 补涨</span></div><div class="card-b" style="padding:16px"><div class="loading">分析板块结构中...</div></div></div></div>';
      try{
        let r=await fetch('/api/sector-lfs');
        let d=await r.json();
        if(curTab!=='sector_center'||subTabs.sectorCenter!=='sector_lfs')return;
        if(d.error){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';return;}
    
        let sectors=d.sectors||[];
        let catchUp=d.catch_up||[];
    
       let html='<div class="tab-full">';
   
       // 板块龙头/跟风
       sectors.forEach(sec=>{
         let leader=sec['龙头']||{};
         let followers=sec['跟风']||[];
         let lbTag=(leader['连板']||1)>=3?`<span style="background:rgba(255,59,48,.15);color:var(--rd);padding:2px 8px;border-radius:6px;font-weight:700">${leader['连板']}板龙头</span>`:(leader['连板']||1)>=2?`<span style="background:rgba(255,149,0,.12);color:var(--or);padding:2px 8px;border-radius:6px;font-weight:600">${leader['连板']}板</span>`:'';
         let netIn=sec['资金净流入']||0;
         let netColor=netIn>=0?'var(--rd)':'var(--gn)';
         html+=`<div class="card" style="margin-bottom:12px"><div class="card-h"><span class="card-t">🏭 ${sec['行业']}</span><span class="card-c" style="color:${netColor}">净流入${formatVolume(netIn)}</span></div><div class="card-b" style="padding:14px">`;
         // 龙头
         if(leader['代码']){
           html+=`<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:rgba(255,59,48,.06);border:1px solid rgba(255,59,48,.15);border-radius:8px;margin-bottom:10px;cursor:pointer" onclick="viewStockDetail('${leader['代码']}')"><span style="font-size:16px">👑</span><span style="font-weight:700;color:var(--rd);font-size:14px">${leader['名称']}</span><span class="pf-code">${leader['代码']}</span>${lbTag}<span class="${(leader['涨跌幅']||0)>=0?'up':'dn'}">${(leader['涨跌幅']||0)>=0?'+':''}${(leader['涨跌幅']||0).toFixed(2)}%</span><span style="color:var(--tx3);font-size:11px;margin-left:auto">封板${formatVolume(leader['封板资金']||0)}</span></div>`;
         }
         // 跟风
         if(followers.length){
           html+=`<div style="font-size:11px;color:var(--tx3);margin-bottom:6px">跟风：</div><div style="display:flex;gap:8px;flex-wrap:wrap">`;
           followers.forEach(f=>{
             html+=`<span style="padding:6px 10px;background:var(--sf2);border:1px solid var(--bd);border-radius:6px;font-size:12px;cursor:pointer" onclick="viewStockDetail('${f['代码']}')"><span style="color:var(--bl);font-weight:600">${f['名称']}</span> <span class="${(f['涨跌幅']||0)>=0?'up':'dn'}">${(f['涨跌幅']||0).toFixed(2)}%</span></span>`;
           });
           html+='</div>';
         }
         html+='</div></div>';
       });
   
       // 补涨板块
       if(catchUp.length){
         html+=`<div class="card"><div class="card-h"><span class="card-t">🔄 补涨板块（资金流入但无涨停）</span><span class="card-c">${catchUp.length}个</span></div><div class="card-b"><table class="tbl"><thead><tr><th>行业</th><th>资金净流入</th><th>涨幅</th><th>领涨股</th><th>领涨股涨幅</th></tr></thead><tbody>`;
         catchUp.forEach(c=>{
           html+=`<tr><td style="font-weight:600">${c['行业']}</td><td class="${(c['资金净流入']||0)>=0?'up':'dn'}">${formatVolume(c['资金净流入']||0)}</td><td class="${(c['涨幅']||0)>=0?'up':'dn'}">${(c['涨幅']||0).toFixed(2)}%</td><td>${c['领涨股']||'--'}</td><td class="${(c['领涨股涨幅']||0)>=0?'up':'dn'}">${(c['领涨股涨幅']||0).toFixed(2)}%</td></tr>`;
         });
         html+='</tbody></table></div></div>';
       }
   
       html+='</div>';
       el.innerHTML=html;
     }catch(e){
       el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
     }
   }
   
   /* ===== K线反包页面 ===== */
   async function renderKlineEngulfing(el){
     el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🔄 K线反包</span></div><div class="card-b" style="padding:16px"><div class="loading">扫描反包形态中...</div></div></div></div>';
     try{
       let r=await fetch('/api/kline-engulfing');
       let d=await r.json();
       if(curTab!=='engulfing')return;
       if(!d.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">今日暂无K线反包形态</div></div></div>';return;}
   
       let codes=d.map(s=>s['代码']).filter(c=>c);
       await loadStockConcepts(codes);
       if(curTab!=='engulfing')return;
   
       let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🔄 今日K线反包（阳包阴）</span><span class="card-c">${d.length}只</span></div><div class="card-b" style="padding:12px"><div style="padding:8px 12px;background:rgba(255,149,0,.06);border-left:3px solid var(--or);border-radius:0 8px 8px 0;margin-bottom:12px;font-size:11px;color:var(--tx3)">反包形态：今日阳线完全覆盖昨日阴线实体，显示多头强力反攻</div><table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>昨日跌幅</th><th>今日涨幅</th><th>反包力度</th><th>所属概念</th></tr></thead><tbody>`;
       d.forEach(s=>{
         let force=s['反包力度']||0;
         let fColor=force>=150?'var(--rd)':force>=100?'var(--or)':'var(--ac)';
         html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s['代码']}')"><td class="pf-code">${s['代码']}</td><td>${s['名称']}</td><td class="dn">${(s['昨日跌幅']||0).toFixed(2)}%</td><td class="up">+${(s['今日涨幅']||0).toFixed(2)}%</td><td><span style="color:${fColor};font-weight:700;font-size:14px">${force}%</span></td><td>${getConceptTags(s['代码'],3)}</td></tr>`;
       });
       html+='</tbody></table></div></div></div>';
       el.innerHTML=html;
     }catch(e){
       el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
     }
   }
   
   /* ===== 财联社红色新闻页面 ===== */
   async function renderCLSNews(el){
     el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🔴 财联社重点新闻</span></div><div class="card-b" style="padding:16px"><div class="loading">加载新闻中...</div></div></div></div>';
     try{
       let r=await fetch('/api/cls-red-news');
       let d=await r.json();
       if(curTab!=='cls_news')return;
       if(!d.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无新闻</div></div></div>';return;}
   
       let redNews=d.filter(n=>n['红色']);
       let normalNews=d.filter(n=>!n['红色']);
   
       let html='<div class="tab-full">';
       // 红色重点新闻
       if(redNews.length){
         html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">🔴 重点新闻（${redNews.length}条）</span></div><div class="card-b" style="max-height:500px;overflow-y:auto">`;
         redNews.forEach(n=>{
           let sent=analyzeSentiment(n['标题'],n['摘要']||'');
           let sectors=extractSectors(n['标题'],n['摘要']||'');
           let sectorTags=sectors.length?sectors.map(s=>`<span style="background:rgba(167,139,250,.1);color:var(--ac);padding:1px 5px;border-radius:4px;font-size:10px;margin:1px 2px">${s}</span>`).join(''):'';
           html+=`<div class="news-item" style="border-left:3px solid var(--rd)" onclick="${n['链接']?"window.open('"+n['链接']+"','_blank')":""}"><div class="news-time"><span style="color:${sent.color};font-weight:600">${sent.icon}${sent.sentiment}</span> · ${n['来源']||''} · ${(n['时间']||'').substring(5,16)}</div><div class="news-title" style="color:var(--rd);font-weight:700">🔥 ${n['标题']}</div>${n['摘要']?'<div class="news-summary">'+n['摘要']+'</div>':''}${sectorTags?'<div style="margin-top:4px">'+sectorTags+'</div>':''}</div>`;
         });
         html+='</div></div>';
       }
       // 一般新闻
       if(normalNews.length){
         html+=`<div class="card"><div class="card-h"><span class="card-t">📰 一般新闻（${normalNews.length}条）</span></div><div class="card-b" style="max-height:400px;overflow-y:auto">`;
         normalNews.slice(0,40).forEach(n=>{
           let sent=analyzeSentiment(n['标题'],n['摘要']||'');
           html+=`<div class="news-item" onclick="${n['链接']?"window.open('"+n['链接']+"','_blank')":""}"><div class="news-time"><span style="color:${sent.color}">${sent.icon}${sent.sentiment}</span> · ${n['来源']||''} · ${(n['时间']||'').substring(5,16)}</div><div class="news-title">${n['标题']}</div></div>`;
         });
         html+='</div></div>';
       }
       html+='</div>';
       el.innerHTML=html;
     }catch(e){
       el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
     }
   }
   
   /* ===== 市场情绪详细页面 ===== */
   async function renderSentimentDetail(el){
     el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🧠 市场情绪指数</span></div><div class="card-b" style="padding:16px"><div class="loading">计算市场情绪中...</div></div></div></div>';
     try{
       let r=await fetch('/api/market-sentiment-detail');
       let d=await r.json();
       if(curTab!=='sentiment')return;
       if(d.error){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';return;}
   
       let f=d.factors||{};
       let score=d.score||50;
       let scoreColor=score>=65?'var(--rd)':score>=45?'var(--ac)':'var(--gn)';
   
       let html='<div class="tab-full">';
   
       // 情绪大卡
       html+=`<div style="text-align:center;padding:30px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:16px;margin-bottom:16px;box-shadow:var(--shadow-md)"><div style="font-size:60px">${d.icon||'⚖️'}</div><div style="font-size:48px;font-weight:800;color:${scoreColor};margin:8px 0">${score}</div><div style="font-size:18px;font-weight:600;color:${scoreColor}">${d.level||'震荡'}</div><div style="font-size:12px;color:var(--tx3);margin-top:4px">综合情绪评分（0-100）</div></div>`;
   
       // 核心指标
       html+=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
         <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px"><div style="font-size:11px;color:var(--tx3)">涨跌比</div><div style="font-size:24px;font-weight:700;color:var(--ac)">${f['涨跌比']||0}</div></div>
         <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px"><div style="font-size:11px;color:var(--tx3)">涨停/跌停</div><div style="font-size:24px;font-weight:700"><span class="up">${f['涨停数']||0}</span>/<span class="dn">${f['跌停数']||0}</span></div></div>
         <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px"><div style="font-size:11px;color:var(--tx3)">炸板率</div><div style="font-size:24px;font-weight:700;color:var(--or)">${f['炸板率']||0}%</div></div>
         <div style="text-align:center;padding:14px;background:var(--sf-solid);border:1px solid var(--bd);border-radius:10px"><div style="font-size:11px;color:var(--tx3)">最高板</div><div style="font-size:24px;font-weight:700;color:var(--rd)">${f['最高板']||0}板</div></div>
       </div>`;
   
       // 涨跌家数饼图（用进度条代替）
       let upPct=f['上涨家数']?Math.round(f['上涨家数']/(f['上涨家数']+f['下跌家数']+f['平盘家数'])*100):50;
       html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">📊 涨跌家数分布</span></div><div class="card-b" style="padding:16px">
         <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px"><span class="up" style="font-weight:700;min-width:60px">上涨 ${f['上涨家数']||0}</span><div style="flex:1;height:24px;background:var(--sf2);border-radius:8px;overflow:hidden;display:flex"><div style="width:${upPct}%;background:linear-gradient(90deg,rgba(255,59,48,.3),var(--rd));border-radius:8px 0 0 8px"></div><div style="width:${100-upPct}%;background:linear-gradient(90deg,rgba(52,199,89,.3),var(--gn))"></div></div><span class="dn" style="font-weight:700;min-width:60px;text-align:right">${f['下跌家数']||0} 下跌</span></div>
         <div style="font-size:12px;color:var(--tx3);text-align:center">平盘 ${f['平盘家数']||0}</div>
       </div></div>`;
   
       // 连板分布
       let lbDist=f['连板分布']||{};
       let sortedLb=Object.entries(lbDist).sort((a,b)=>Number(b[0])-Number(a[0]));
       if(sortedLb.length){
         html+=`<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">🏗️ 连板梯队</span></div><div class="card-b" style="padding:16px"><div style="display:flex;gap:10px;flex-wrap:wrap">`;
         sortedLb.forEach(([lb,cnt])=>{
           let color=Number(lb)>=5?'var(--rd)':Number(lb)>=3?'var(--or)':'var(--ac)';
           html+=`<div style="text-align:center;padding:10px 16px;background:var(--sf2);border:1px solid var(--bd);border-radius:8px"><div style="font-size:22px;font-weight:800;color:${color}">${cnt}</div><div style="font-size:11px;color:var(--tx3)">${lb}连板</div></div>`;
         });
         html+='</div></div></div>';
       }
   
       // 板块资金流入/流出
       let topIn=d.sector_top_in||[];
       let topOut=d.sector_top_out||[];
       html+=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px"><div class="card"><div class="card-h"><span class="card-t">🟢 资金流入TOP5</span></div><div class="card-b">`;
       topIn.forEach(s=>{
         html+=`<div style="display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px"><span style="font-weight:600">${s['名称']}</span><span class="up">+${formatVolume(s['净额']||0)}</span></div>`;
       });
       html+=`</div></div><div class="card"><div class="card-h"><span class="card-t">🔴 资金流出TOP5</span></div><div class="card-b">`;
       topOut.forEach(s=>{
         html+=`<div style="display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:12px"><span style="font-weight:600">${s['名称']}</span><span class="dn">${formatVolume(s['净额']||0)}</span></div>`;
       });
       html+='</div></div></div>';
   
       html+='</div>';
       el.innerHTML=html;
     }catch(e){
       el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
     }
   }
   

/* ===== 监控室页面 ===== */
async function renderMonitoring(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🛡️ 监控室</span></div><div class="card-b" style="padding:16px"><div class="loading">加载交易所监控数据...</div></div></div></div>';
  try{
    let r=await fetch('/api/monitoring');
    let d=await r.json();
    if(curTab!=='monitoring')return;
    
    let html='<div class="tab-full">';
    
    // 左右两栏布局
    html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start">';
    
    // === 左栏：盘口异动 ===
    let abnormals=d.abnormal_trading||[];
    html+='<div><div class="card"><div class="card-h"><span class="card-t">⚡ 盘口异动</span><span class="card-c">'+abnormals.length+'条</span></div><div class="card-b" style="max-height:700px;overflow-y:auto">';
    if(abnormals.length){
      // 按异动类型分组
      let grouped={};
      abnormals.forEach(a=>{
        let t=a['异动类型']||'其他';
        if(!grouped[t])grouped[t]=[];
        grouped[t].push(a);
      });
      let typeColors={
        '封涨停板':'var(--rd)','打开涨停板':'var(--or)','封跌停板':'var(--gn)','打开跌停板':'var(--bl)',
        '高台跳水':'var(--gn)','加速下跌':'var(--gn)','大笔卖出':'var(--gn)','竞价上涨':'var(--rd)'
      };
      Object.keys(grouped).forEach(type=>{
        let items=grouped[type];
        let color=typeColors[type]||'var(--tx3)';
        html+='<div style="padding:6px 16px;font-weight:600;font-size:12px;color:'+color+';border-bottom:1px solid var(--bd);background:rgba(255,255,255,.02)">';
        html+=(type.includes('涨停')?'🔴':type.includes('跌停')?'🟢':type.includes('跳水')?'⬇️':type.includes('卖出')?'💰':'📊')+' '+type+' ('+items.length+')</div>';
        html+='<table class="tbl"><thead><tr><th>时间</th><th>代码</th><th>名称</th><th>相关信息</th></tr></thead><tbody>';
        items.forEach(a=>{
          html+='<tr style="cursor:pointer" onclick="viewStockDetail(\''+a['代码']+'\')">';
          html+='<td style="font-size:11px;color:var(--tx3);white-space:nowrap">'+a['时间']+'</td>';
          html+='<td class="pf-code">'+a['代码']+'</td>';
          html+='<td class="pf-name">'+a['名称']+'</td>';
          html+='<td style="font-size:11px;color:var(--tx3)">'+(a['相关信息']||'--')+'</td>';
          html+='</tr>';
        });
        html+='</tbody></table>';
      });
    }else{
      html+='<div class="empty">暂无盘口异动数据</div>';
    }
    html+='</div></div></div>';
    
    // === 右栏：风险提示 ===
    html+='<div>';
    
    // 风险提示公告
    let risks=d.risk_warnings||[];
    html+='<div class="card" style="margin-bottom:16px"><div class="card-h"><span class="card-t">⚠️ 风险提示公告</span><span class="card-c">'+risks.length+'条</span></div><div class="card-b" style="max-height:600px;overflow-y:auto">';
    if(risks.length){
      html+='<table class="tbl"><thead><tr>';
      // 动态取列名
      let cols=Object.keys(risks[0]);
      cols.forEach(c=>{
        if(c!=='链接')html+='<th>'+c+'</th>';
      });
      html+='</tr></thead><tbody>';
      risks.forEach((r,ri)=>{
        let rowBg=ri%2===0?'':'background:rgba(255,255,255,.03)';
        html+='<tr style="'+rowBg+'">';
        cols.forEach(c=>{
          if(c==='链接')return;
          let val=r[c]||'--';
          if(c.includes('标题')){
            html+='<td style="font-size:11px;word-break:break-word;white-space:normal;max-width:300px;line-height:1.5">'+val+'</td>';
          }else if(c.includes('代码')){
            html+='<td class="pf-code">'+val+'</td>';
          }else{
            html+='<td style="font-size:11px;color:var(--tx3)">'+val+'</td>';
          }
        });
        html+='</tr>';
      });
      html+='</tbody></table>';
    }else{
      html+='<div class="empty">暂无风险提示公告</div>';
    }
    html+='</div></div>';
    
    html+='</div></div>';
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败: '+e.message+'</div></div></div>';
  }
}

/* ===== 板块龙头页面 ===== */
async function renderSectorLeader(el){
  el.innerHTML='<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">👑 板块龙头 & 跟风</span></div><div class="card-b" style="padding:16px"><div class="loading">加载中...</div></div></div></div>';
  try{
    let r=await fetch('/api/sector-leaders');
    let d=await r.json();
    if(curTab!=='sector_center'||subTabs.sectorCenter!=='sector_leader')return;
    if(d.error){
      el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">'+d.error+'</div></div></div>';
      return;
    }
    let leaders=d.龙头||[];
    let followers=d.跟风||[];
    let industryCount=d.行业涨停数||{};
    
    let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">👑 板块龙头 & 跟风</span></div><div class="card-b" style="padding:16px">`;
    
    // 行业涨停数统计
    html+=`<div style="margin-bottom:16px"><div style="font-weight:600;color:var(--ac);margin-bottom:8px">📊 行业涨停数TOP10 <span style="font-size:11px;color:var(--tx3)">（点击筛选）</span></div><div style="display:flex;gap:8px;flex-wrap:wrap">`;
    Object.entries(industryCount).forEach(([k,v])=>{
      html+=`<span onclick="filterSectorLeaderByIndustry('${k}')" style="padding:6px 12px;background:var(--sf2);border-radius:6px;font-size:12px;cursor:pointer;transition:.2s">${k} <b style="color:var(--rd)">${v}只</b></span>`;
    });
    html+=`</div></div>`;
    
    // 龙头
    html+=`<div style="margin-bottom:16px"><div style="font-weight:600;color:var(--ac);margin-bottom:8px">👑 板块龙头（行业连板最高）${_sectorLeaderFilter?' <span style="color:var(--bl)">筛选: '+_sectorLeaderFilter+'</span>':''}</div>`;
    let filteredLeaders=_sectorLeaderFilter?leaders.filter(s=>s.行业===_sectorLeaderFilter):leaders;
    if(filteredLeaders.length){
      html+=`<table class="tbl"><thead><tr><th>行业</th><th>代码</th><th>名称</th><th>连板数</th><th>涨跌幅</th></tr></thead><tbody>`;
      filteredLeaders.forEach(s=>{
        html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td style="font-weight:600">${s.行业}</td><td>${s.代码}</td><td>${s.名称}</td><td><span class="tag tag-hl">${s.连板数}连板</span></td><td class="up">+${(s.涨跌幅||0).toFixed(2)}%</td></tr>`;
      });
      html+=`</tbody></table>`;
    }else{html+=`<div class="empty">暂无数据</div>`;}
    html+=`</div>`;
    
    // 跟风
    html+=`<div><div style="font-weight:600;color:var(--ac);margin-bottom:8px">👥 跟风股（同行业其他涨停）${_sectorLeaderFilter?' <span style="color:var(--bl)">筛选: '+_sectorLeaderFilter+'</span>':''}</div>`;
    let filteredFollowers=_sectorLeaderFilter?followers.filter(s=>s.行业===_sectorLeaderFilter):followers;
    if(filteredFollowers.length){
      html+=`<table class="tbl"><thead><tr><th>行业</th><th>代码</th><th>名称</th><th>连板数</th><th>涨跌幅</th></tr></thead><tbody>`;
      filteredFollowers.forEach(s=>{
        html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.代码}')"><td>${s.行业}</td><td>${s.代码}</td><td>${s.名称}</td><td>${s.连板数}</td><td class="up">+${(s.涨跌幅||0).toFixed(2)}%</td></tr>`;
      });
      html+=`</tbody></table>`;
    }else{html+=`<div class="empty">暂无数据</div>`;}
    html+=`</div>`;
    
    html+=`</div></div></div>`;
    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">加载失败</div></div></div>';
  }
}

// 板块龙头行业筛选
let _sectorLeaderFilter=null;
function filterSectorLeaderByIndustry(industry){
  if(_sectorLeaderFilter===industry){
    _sectorLeaderFilter=null; // 取消筛选
  }else{
    _sectorLeaderFilter=industry;
  }
  renderSectorLeader(document.getElementById('tabContent'));
}

function renderAI(el){
  let lu=(D.limit_up||[]).length,ld=(D.limit_down||[]).length,br=(D.broken_limit||[]).length;
  let rs=(D.restructure||[]).length;
  let html=`<div class="chat-wrap">
    <div class="chat-messages" id="chatMessages">
      <div class="chat-empty" id="chatEmpty">
        <div class="chat-empty-icon">🤖</div>
       <div class="chat-empty-text">AI 智能复盘助手</div>
       <div class="chat-empty-sub">基于 ${D.date||''} 数据 · MiMo-V2.5-Pro</div>
        <div class="chat-empty-sub" style="margin-top:8px">输入问题或点击下方"一键分析"开始对话</div>
      </div>
    </div>
    <div class="chat-input-area">
      <div class="chat-config-row">
        <label><input type="checkbox" id="chatCkLu" checked> 🔴涨停</label>
        <label><input type="checkbox" id="chatCkLd" checked> 🟢跌停</label>
        <label><input type="checkbox" id="chatCkBr" checked> 🟡炸板</label>
        <label><input type="checkbox" id="chatCkSs" checked> 📊板块</label>
        <label><input type="checkbox" id="chatCkNw" checked> 📰新闻</label>
        <label><input type="checkbox" id="chatCkSf" checked> 💰资金</label>
        <label><input type="checkbox" id="chatCkCg" checked> 🏷️分组</label>
        <label><input type="checkbox" id="chatCkPf" checked> 💼持仓</label>
        <label><input type="checkbox" id="chatCkWl" checked> 🎯想买</label>
        <label><input type="checkbox" id="chatCkHot" checked> 🔥热门股</label>
        <label><input type="checkbox" id="chatCkMf" checked> 💰主力流入</label>
        <label><input type="checkbox" id="chatCkYzt" checked> 📊昨日涨停</label>
        <label><input type="checkbox" id="chatCkNh" checked> 📈新高票</label>
        <label><input type="checkbox" id="chatCkSl" checked> 👑板块龙头</label>
        <label><input type="checkbox" id="chatCkKn" checked> 📰重点新闻</label>
        <button class="btn" onclick="showPromptPreview()" style="font-size:11px;padding:3px 8px;background:var(--sf2);border:1px solid var(--bd)">👁️提示词</button>
        <span style="margin-left:auto;display:flex;gap:6px">
          <button class="btn" onclick="chatGenPrompt()" style="font-size:11px;padding:2px 8px">✨ 生成提示词</button>
          <button class="btn" onclick="toggleAllChecks(true)" style="font-size:11px;padding:2px 8px">全选</button>
          <button class="btn" onclick="toggleAllChecks(false)" style="font-size:11px;padding:2px 8px">全不选</button>
        </span>
      </div>
      <div class="chat-input-row">
        <textarea id="chatInput" placeholder="输入你的问题... (Enter发送, Shift+Enter换行)" rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();chatSend()}" oninput="autoResizeTA(this)"></textarea>
        <button class="chat-send-btn" onclick="chatSend()" id="chatSendBtn">✨ 发送分析</button>
      </div>
    </div>
  </div>`;
  el.innerHTML=html;
}
/* ===== 聊天界面功能 ===== */
function autoResizeTA(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px';}

function chatGetCkData(){
  return {
    limit_up:document.getElementById('chatCkLu')?.checked,
    limit_down:document.getElementById('chatCkLd')?.checked,
    broken_limit:document.getElementById('chatCkBr')?.checked,
    sector_stats:document.getElementById('chatCkSs')?.checked,
    news:document.getElementById('chatCkNw')?.checked,
    sector_flow:document.getElementById('chatCkSf')?.checked,
    concept_groups:document.getElementById('chatCkCg')?.checked,
    portfolio:document.getElementById('chatCkPf')?.checked,
    watchlist:document.getElementById('chatCkWl')?.checked,
    hot_stocks:document.getElementById('chatCkHot')?.checked,
    main_fund_flow:document.getElementById('chatCkMf')?.checked,
    yesterday_zt:document.getElementById('chatCkYzt')?.checked,
    new_highs:document.getElementById('chatCkNh')?.checked,
    sector_leaders:document.getElementById('chatCkSl')?.checked,
    key_news:document.getElementById('chatCkKn')?.checked
  };
}

function chatAddMsg(role,content,promptPreview){
  const msgs=document.getElementById('chatMessages');
  const empty=document.getElementById('chatEmpty');
  if(empty)empty.style.display='none';
  const now=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
  const div=document.createElement('div');
  div.className='chat-msg '+role;
  if(role==='user'){
    let bubble=content.replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
    if(promptPreview) bubble+=`<div class="chat-prompt-toggle" onclick="this.nextElementSibling.classList.toggle('show')">📋 查看完整数据</div><div class="chat-prompt-preview">${promptPreview.replace(/</g,'&lt;').replace(/\n/g,'<br>')}</div>`;
    div.innerHTML=`<div class="chat-avatar">👤</div><div><div class="chat-bubble">${bubble}</div><div class="chat-time">${now}</div></div>`;
  }else{
    div.innerHTML=`<div class="chat-avatar">🤖</div><div><div class="chat-bubble">${aiMarkdown(content)}</div><div class="chat-time">${now}</div><div class="chat-feedback" id="fb-${Date.now()}"><span class="chat-fb-tag" onclick="chatFb(this)">👍 认可</span><span class="chat-fb-tag" onclick="chatFb(this)">🎯 精准</span><span class="chat-fb-tag" onclick="chatFb(this)">🔥 推荐准</span><span class="chat-fb-tag" onclick="chatFb(this)">⚠️ 有偏差</span><span class="chat-fb-tag" onclick="chatFb(this)">❌ 不准</span></div></div>`;
  }
  msgs.appendChild(div);
  msgs.scrollTop=msgs.scrollHeight;
}

function chatAddTyping(){
  const msgs=document.getElementById('chatMessages');
  const div=document.createElement('div');
  div.className='chat-msg assistant';
  div.id='chatTyping';
  div.innerHTML=`<div class="chat-avatar">🤖</div><div><div class="chat-bubble"><div class="chat-typing"><span></span><span></span><span></span></div></div></div>`;
  msgs.appendChild(div);
  msgs.scrollTop=msgs.scrollHeight;
}

function chatRemoveTyping(){const t=document.getElementById('chatTyping');if(t)t.remove();}
function chatFb(el){el.classList.toggle('active');}

function chatSend(){
  const input=document.getElementById('chatInput');
  const text=(input.value||'').trim();
  if(!text)return;
  input.value='';autoResizeTA(input);
  chatAddMsg('user',text);
  chatDoPredict(text);
}

function chatGenPrompt(){
  const prompt=generatePromptText();
  document.getElementById('chatInput').value=prompt;
  autoResizeTA(document.getElementById('chatInput'));
}

function chatQuickAnalyze(){
  const prompt=generatePromptText();
  chatAddMsg('user','📊 一键分析当前市场数据');
  chatDoPredict(prompt);
}

function generatePromptText(){
  let lu=D.limit_up||[],ld=D.limit_down||[],br=D.broken_limit||[],sf=D.sector_flow||[],rs=D.restructure||[];
  let pf=getPortfolio();
  let boards={};lu.forEach(s=>{let n=s['连板数']||1;boards[n]=(boards[n]||0)+1;});
  let boardStr=Object.keys(boards).sort((a,b)=>b-a).map(k=>k+'连板'+boards[k]+'只').join('、');
  let yizi=lu.filter(s=>s.is_yizi).length;
  let hyMap={};lu.forEach(s=>{let h=s['所属行业']||'';if(h)hyMap[h]=(hyMap[h]||0)+1;});
  let hyArr=Object.entries(hyMap).sort((a,b)=>b[1]-a[1]).slice(0,10);
  let hyStr=hyArr.map(([k,v])=>k+'('+v+'只)').join('、');
  let topIn=sf.filter(s=>(s['净额']||0)>0).sort((a,b)=>b['净额']-a['净额']).slice(0,5).map(s=>s['名称']+'+'+s['净额'].toFixed(1)+'亿').join('、');
  let topOut=sf.filter(s=>(s['净额']||0)<0).sort((a,b)=>a['净额']-b['净额']).slice(0,5).map(s=>s['名称']+s['净额'].toFixed(1)+'亿').join('、');
  let rsStr=rs.slice(0,10).map(s=>s['名称']+'('+s['公告类型']+')').join('、');
  let pfStr='';
  if(pf.length){
    pfStr='\n我的持仓：\n';
    pf.forEach(p=>{let isZt=lu.some(s=>s['代码']===p.code);let match=isZt?lu.find(s=>s['代码']===p.code):null;pfStr+=`- ${p.name||p.code}(${p.code}) 数量:${p.qty||'--'} 成本:${p.cost||'--'}${match?' 现价:'+match['最新价'].toFixed(2)+' [涨停中!]':''}\n`;});
    pfStr+='请结合我的持仓给出操作建议';
  }
  return `基于${D.date||''}的A股复盘数据分析：\n\n当前市场概况：涨停${lu.length}只（一字板${yizi}只），跌停${ld.length}只，炸板${br.length}只。\n连板梯队：${boardStr}。\n涨停行业集中：${hyStr}。\n资金净流入前5：${topIn}。\n资金净流出前5：${topOut}。\n${rsStr?'重组公告相关：'+rsStr+'。':''}${pfStr}\n\n请重点分析：\n1. 哪些连板股明天最有可能晋级？理由是什么？\n2. 资金主攻方向是否明确？持续性如何？\n3. 明天最值得关注的3只个股及操作建议（代码+名称+理由）\n4. 风险提示与回避方向`;
}

async function chatDoPredict(customPrompt){
  const btn=document.getElementById('chatSendBtn');
  btn.disabled=true;btn.textContent='分析中...';
  chatAddTyping();
  const ckData=chatGetCkData();
  const portfolio=getPortfolio();
  const conceptGroups=getConceptGroups();
  const wl=getWatchlist();
  let apiPrompt=customPrompt||generatePromptText();
  try{
    const r=await fetch('/api/ai-predict',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:D.date||getTradingDate(),custom_prompt:apiPrompt,portfolio:portfolio,concept_groups:conceptGroups,watchlist:wl,ck_data:ckData})});
    const d=await r.json();
    chatRemoveTyping();
    if(d.error){
      chatAddMsg('assistant','❌ 预测失败: '+d.error+'\n\n请稍后重试，或检查数据是否已加载。');
    }else{
      chatAddMsg('assistant',d.prediction||'无结果');
    }
  }catch(e){
    chatRemoveTyping();
    chatAddMsg('assistant','❌ 网络错误: '+e.message);
  }
  btn.disabled=false;btn.textContent='发送';
}

function highlightAI(text){
  // 高亮股票代码和名称 (6位数字代码)
  text=text.replace(/([（(](\d{6})[)）])/g,'<span class="ai-code">$1</span>');
  text=text.replace(/(\d{6})(?=[\s,，、。）)])/g,'<span class="ai-code">$1</span>');
  // 高亮推荐/龙头标记
  text=text.replace(/(推荐|重点关注|龙头|核心|最佳|强势|晋级|连板|首板)/g,'<span class="ai-hot">$1</span>');
  // 高亮风险标记
  text=text.replace(/(风险|回避|谨慎|减仓|止损|卖出|见顶|分歧|炸板|断板|补跌|核按钮)/g,'<span class="ai-risk">$1</span>');
  // 高亮操作标记
  text=text.replace(/(打板|低吸|半路|追涨|排板|扫板|半仓|满仓|空仓|轻仓)/g,'<span class="ai-op">$1</span>');
  // 高亮百分比数字
  text=text.replace(/(\d+\.\d+%)/g,'<span class="ai-num">$1</span>');
  // 高亮亿元数字
  text=text.replace(/(\d+\.?\d*亿)/g,'<span class="ai-num">$1</span>');
  // 高亮情绪词
  text=text.replace(/(亢奋|乐观|中性|谨慎|恐慌|强势|弱势)/g,'<span class="ai-sent">$1</span>');
  return text;
}

function aiMarkdown(text){
  text=text.replace(/### (.+)/g,'<h4 class="ai-h4">$1</h4>');
  text=text.replace(/## (.+)/g,'<h3 class="ai-h3">$1</h3>');
  text=text.replace(/# (.+)/g,'<h2 class="ai-h2">$1</h2>');
  text=text.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>');
  text=text.replace(/^- (.+)/gm,'<div class="ai-li">• $1</div>');
  text=text.replace(/\n{2,}/g,'<br>');
  text=text.replace(/\n/g,'<br>');
  return highlightAI(text);
}

// startPredict removed — use chatDoPredict in chat interface

async function loadAIPredictionHistory(){
  const el=document.getElementById('aiHistList');
  if(!el)return;
  el.innerHTML='<div class="loading">加载中</div>';
  try{
    const r=await fetch('/api/ai-history');
    const list=await r.json();
    if(!list.length){el.innerHTML='<div class="empty">暂无预测记录</div>';return;}
    el.innerHTML=list.map(h=>{
      const d=new Date(h.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).replace(/\//g,'-');
      const preview=(h.prediction||'').substring(0,120).replace(/\n/g,' ');
      let scoreBadge='';
      if(h.feedback_count>0){
        const sc=h.total_score||0;
        const cls=sc>0?'pos':sc<0?'neg':'zero';
        const icon=sc>0?'👍':sc<0?'👎':'⚖️';
        scoreBadge=`<span class="ai-fb-score ${cls}" style="margin-left:6px">${icon} ${sc>0?'+':''}${sc}</span>`;
      }
      return`<div class="ai-hist-item" data-hist-id="${h.id}" onclick="viewAIPrediction(${h.id})">
        <div class="ai-hist-top"><span class="ai-hist-date">📅 ${h.data_date}</span><span class="ai-hist-time">${d}</span>${scoreBadge}</div>
        ${h.custom_prompt?`<div class="ai-hist-prompt">💡 ${h.custom_prompt.substring(0,80)}</div>`:''}
        <div class="ai-hist-preview">${preview}...</div>
      </div>`;
    }).join('');
  }catch(e){el.innerHTML='<div class="empty">加载失败</div>';}
}

async function viewAIPrediction(id){
  try{
    const r=await fetch('/api/ai-history/'+id);
    const h=await r.json();
    const existing=document.getElementById('histDetail-'+id);
    if(existing){existing.remove();return;}
    const item=document.querySelector(`[data-hist-id="${id}"]`);
    if(!item)return;
    let text=h.prediction||'无结果';
    let detailHtml='';
    if(h.custom_prompt){detailHtml+=`<div style="margin-bottom:8px;padding:6px 10px;background:var(--sf3);border-radius:4px;font-size:12px;color:var(--tx2)">💡 ${h.custom_prompt}</div>`;}
    detailHtml+=`<div class="ai-result-body">${aiMarkdown(text)}</div>`;
    detailHtml+=`<div class="ai-feedback" id="fbBox-${id}"><div class="ai-feedback-title">📊 这次预测怎么样？你的反馈会帮助AI越分析越准</div>`;
    detailHtml+=`<div class="ai-feedback-tags" id="fbTags-${id}">`;
    [
      {icon:'👍',label:'认可',w:1},{icon:'🎯',label:'精准',w:3},
      {icon:'💡',label:'有启发',w:1},{icon:'🔥',label:'推荐准',w:3},
      {icon:'📈',label:'方向对',w:2},{icon:'🤔',label:'不确定',w:0},
      {icon:'⚠️',label:'有偏差',w:-2},{icon:'❌',label:'不准',w:-3}
    ].forEach(t=>{
      detailHtml+=`<span class="ai-fb-tag" data-tag="${t.icon}" data-weight="${t.w>0?'+'+t.w:t.w}" onclick="toggleFbTag(this)">${t.icon} ${t.label}</span>`;
    });
    detailHtml+=`</div>`;
    detailHtml+=`<div style="font-size:12px;color:var(--tx3);margin-bottom:4px;margin-top:8px">整体满意度</div>`;
    detailHtml+=`<div class="ai-fb-stars" id="fbStars-${id}">`;
    for(let i=1;i<=5;i++) detailHtml+=`<span class="ai-fb-star" data-star="${i}" onclick="setFbStar(${id},${i})">★</span>`;
    detailHtml+=`</div>`;
    detailHtml+=`<textarea class="ai-fb-note" id="fbNote-${id}" placeholder="说说你的看法（可选）" rows="2"></textarea>`;
    detailHtml+=`<button class="ai-fb-submit" onclick="submitFeedback(${id})">✅ 提交反馈</button></div>`;
    const detail=document.createElement('div');
    detail.id='histDetail-'+id;
    detail.className='hist-detail';
    detail.innerHTML=detailHtml;
    item.parentNode.insertBefore(detail,item.nextSibling);
    detail.scrollIntoView({behavior:'smooth'});
    loadFeedback(id);
  }catch(e){}
}

/* ===== AI预测反馈系统 ===== */
let fbStarRatings={};
function setFbStar(predId,stars){
  fbStarRatings[predId]=stars;
  const box=document.getElementById('fbStars-'+predId);
  if(!box)return;
  box.querySelectorAll('.ai-fb-star').forEach(s=>{
    s.classList.toggle('on',parseInt(s.dataset.star)<=stars);
  });
}
function toggleFbTag(el){
  el.classList.toggle('active');
}
async function submitFeedback(predId){
  const tagsBox=document.getElementById('fbTags-'+predId);
  if(!tagsBox)return;
  const activeTags=[...tagsBox.querySelectorAll('.ai-fb-tag.active')].map(t=>t.dataset.tag);
  const note=(document.getElementById('fbNote-'+predId)||{}).value||'';
  const stars=fbStarRatings[predId]||0;
  if(!activeTags.length&&!note&&!stars){alert('请至少选择一个标签、星级或填写备注');return;}
  try{
    const r=await fetch('/api/ai-feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prediction_id:predId,tags:activeTags,note:note,stars:stars})});
    const d=await r.json();
    if(d.ok){
      const box=document.getElementById('fbBox-'+predId);
      if(box){
        let scoreText=d.score>0?`👍 +${d.score}`:d.score<0?`👎 ${d.score}`:'⚖️ 中性';
        let scoreClass=d.score>0?'pos':d.score<0?'neg':'zero';
        let starText=stars?' · '+('★'.repeat(stars)+'☆'.repeat(5-stars)):'';
        box.innerHTML=`<div class="ai-fb-done">✅ 感谢反馈！评分: <span class="ai-fb-score ${scoreClass}">${scoreText}</span>${starText} 你的反馈会被用于优化后续分析</div>`;
      }
    }
  }catch(e){alert('提交失败: '+e.message);}
}
async function loadFeedback(predId){
  try{
    const r=await fetch('/api/ai-feedback/'+predId);
    const d=await r.json();
    if(d.count>0){
      const box=document.getElementById('fbBox-'+predId);
      if(!box)return;
      let existing=box.querySelector('.ai-fb-done');
      if(existing)return;
      let scoreText=d.total_score>0?`👍 +${d.total_score}`:d.total_score<0?`👎 ${d.total_score}`:'⚖️ 0';
      let scoreClass=d.total_score>0?'pos':d.total_score<0?'neg':'zero';
      let tagsHtml='';
      Object.entries(d.tag_counts).forEach(([t,c])=>{tagsHtml+=`<span class="ai-fb-tag-sm">${t} ×${c}</span>`;});
      let avgStars=d.avg_stars?`<span style="margin-left:8px">⭐ ${d.avg_stars.toFixed(1)}</span>`:'';
      let historyHtml=`<div style="margin-top:8px;padding:8px 12px;background:var(--sf3);border-radius:6px;font-size:11px;color:var(--tx3)"><b>历史反馈</b> <span class="ai-fb-score ${scoreClass}">${scoreText}</span>${avgStars} <span style="margin-left:4px">共${d.count}条</span><div class="ai-fb-tags-display" style="margin-top:4px">${tagsHtml}</div></div>`;
      let noteHtml='';
      d.feedbacks.forEach(f=>{if(f.note)noteHtml+=`<div style="font-size:11px;color:var(--tx3);padding:2px 0">💬 ${f.note}</div>`;});
      let noteBox=box.querySelector('.ai-fb-note');
      if(noteBox&&d.count>0){
        noteBox.insertAdjacentHTML('beforebegin',historyHtml);
        if(noteHtml)noteBox.insertAdjacentHTML('beforebegin',noteHtml);
      }
    }
  }catch(e){}
}

function renderSectorStats(el){
  let sf=D.sector_flow||[];
  if(!sf.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无板块数据</div></div></div>';return;}
  let upSf=sf.filter(s=>(s['今日涨跌幅']||0)>0);
  let downSf=sf.filter(s=>(s['今日涨跌幅']||0)<0);
  let flatSf=sf.filter(s=>(s['今日涨跌幅']||0)===0);
  // 强度分级
  let strong_up=upSf.filter(s=>(s['今日涨跌幅']||0)>=2).length;
  let mid_up=upSf.filter(s=>(s['今日涨跌幅']||0)>=0.5&&(s['今日涨跌幅']||0)<2).length;
  let weak_up=upSf.filter(s=>(s['今日涨跌幅']||0)>0&&(s['今日涨跌幅']||0)<0.5).length;
  let strong_dn=downSf.filter(s=>(s['今日涨跌幅']||0)<=-2).length;
  let mid_dn=downSf.filter(s=>(s['今日涨跌幅']||0)<=-0.5&&(s['今日涨跌幅']||0)>-2).length;
  let weak_dn=downSf.filter(s=>(s['今日涨跌幅']||0)<0&&(s['今日涨跌幅']||0)>-0.5).length;
  // 资金流入/流出
  let totalIn=sf.reduce((a,s)=>a+(s['净额']||0>0?s['净额']:0),0);
  let totalOut=sf.reduce((a,s)=>a+(s['净额']||0<0?Math.abs(s['净额']):0),0);
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">📊 板块涨跌统计</span><span class="card-c">共${sf.length}个行业</span></div><div class="card-b" style="padding:16px">
    <div class="ss-grid">
      <div class="ss-box ss-up">
        <div class="ss-box-title">🔴 上涨板块</div>
        <div class="ss-box-num up">${upSf.length}</div>
        <div class="ss-box-detail">
          <div><span class="ss-dot" style="background:var(--rd)"></span> 强势(≥2%) <b>${strong_up}</b>个</div>
          <div><span class="ss-dot" style="background:#e88"></span> 中等(0.5-2%) <b>${mid_up}</b>个</div>
          <div><span class="ss-dot" style="background:#c66"></span> 微涨(<0.5%) <b>${weak_up}</b>个</div>
        </div>
      </div>
      <div class="ss-box ss-dn">
        <div class="ss-box-title">🟢 下跌板块</div>
        <div class="ss-box-num dn">${downSf.length}</div>
        <div class="ss-box-detail">
          <div><span class="ss-dot" style="background:var(--gn)"></span> 强势(≤-2%) <b>${strong_dn}</b>个</div>
          <div><span class="ss-dot" style="background:#6b6"></span> 中等(-0.5~-2%) <b>${mid_dn}</b>个</div>
          <div><span class="ss-dot" style="background:#4a4"></span> 微跌(>-0.5%) <b>${weak_dn}</b>个</div>
        </div>
      </div>
      <div class="ss-box ss-flat">
        <div class="ss-box-title">⚪ 平盘板块</div>
        <div class="ss-box-num" style="color:var(--bl)">${flatSf.length}</div>
      </div>
    </div>
    <div class="ss-summary">
      <div>📈 涨跌比: <b class="up">${upSf.length}</b> : <b class="dn">${downSf.length}</b> = <b style="color:var(--ac)">${downSf.length?(upSf.length/downSf.length).toFixed(1):'∞'}</b></div>
      <div>💰 资金净流入: <b class="${totalIn-totalOut>=0?'up':'dn'}">${(totalIn-totalOut)>=0?'+':''}${(totalIn-totalOut).toFixed(2)}亿</b> (入${totalIn.toFixed(1)}亿 / 出${totalOut.toFixed(1)}亿)</div>
      <div>💪 市场强度: <b style="color:var(--ac)">${upSf.length>downSf.length*2?'强势':upSf.length>downSf.length?'偏强':upSf.length===downSf.length?'均衡':upSf.length*2<downSf.length?'弱势':'偏弱'}</b></div>
    </div>
    <div style="margin-top:12px">
      <div style="font-size:12px;font-weight:600;color:var(--tx2);margin-bottom:6px">🔴 涨幅前10板块</div>
      <div class="ss-bar-list">`;
  upSf.sort((a,b)=>(b['今日涨跌幅']||0)-(a['今日涨跌幅']||0)).slice(0,10).forEach(s=>{
    let chg=s['今日涨跌幅']||0;
    let pct=Math.min(Math.abs(chg)/5*100,100);
    html+=`<div class="ss-bar-row" onmouseenter="showSectorTip(event,'${s['名称']}')" onmouseleave="hideSectorTip()" style="cursor:pointer"><span class="ss-bar-name">${s['名称']}</span><div class="ss-bar-track"><div class="ss-bar-fill up" style="width:${pct}%"></div></div><span class="up" style="min-width:50px;text-align:right">+${chg.toFixed(2)}%</span><span style="color:var(--tx3);font-size:11px;min-width:60px;text-align:right">${(s['净额']||0)>=0?'+':''}${(s['净额']||0).toFixed(1)}亿</span></div>`;
  });
  html+=`</div></div><div style="margin-top:12px"><div style="font-size:12px;font-weight:600;color:var(--tx2);margin-bottom:6px">🟢 跌幅前10板块</div><div class="ss-bar-list">`;
  downSf.sort((a,b)=>(a['今日涨跌幅']||0)-(b['今日涨跌幅']||0)).slice(0,10).forEach(s=>{
    let chg=s['今日涨跌幅']||0;
    let pct=Math.min(Math.abs(chg)/5*100,100);
    html+=`<div class="ss-bar-row" onmouseenter="showSectorTip(event,'${s['名称']}')" onmouseleave="hideSectorTip()" style="cursor:pointer"><span class="ss-bar-name">${s['名称']}</span><div class="ss-bar-track"><div class="ss-bar-fill dn" style="width:${pct}%"></div></div><span class="dn" style="min-width:50px;text-align:right">${chg.toFixed(2)}%</span><span style="color:var(--tx3);font-size:11px;min-width:60px;text-align:right">${(s['净额']||0)>=0?'+':''}${(s['净额']||0).toFixed(1)}亿</span></div>`;
  });
  html+=`</div></div></div></div></div>`;
  el.innerHTML=html;
}

function renderPortfolio(el){
  let pf=getPortfolio();
  let lu=D.limit_up||[];
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">💼 持仓管理</span><span class="card-c">${pf.length}只持仓</span></div><div class="card-b" style="padding:16px">
    <div style="display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap;align-items:center">
      <input id="pfCode" class="pf-input" placeholder="输入股票代码" style="width:130px" onkeydown="if(event.key==='Enter'){addPortfolio();}" oninput="fetchStockInfo('pf')">
      <span id="pfStockInfo" style="font-size:12px;color:var(--tx3);min-width:80px;white-space:nowrap"></span>
      <input id="pfQty" class="pf-input" placeholder="持仓数量" type="number" style="width:100px">
      <input id="pfCost" class="pf-input" placeholder="成本价" type="number" step="0.01" style="width:100px">
      <button class="btn" onclick="addPortfolio()" style="background:var(--ac);color:#000;border:none">➕ 添加</button>
    </div>
    <div id="pfList">`;
  if(!pf.length){
    html+=`<div class="empty">暂无持仓，添加后AI分析会自动带上你的持仓数据</div>`;
  }else{
    pf.forEach((p,i)=>{
      let isZt=lu.some(s=>s['代码']===p.code);
      let matchZt=isZt?lu.find(s=>s['代码']===p.code):null;
      let pfPrice=p.price||0,pfChg=p.change||0,pfChgPct=p.changePct||0;
      if(matchZt){pfPrice=matchZt['最新价']||pfPrice;pfChg=matchZt['涨跌额']||pfChg;pfChgPct=matchZt['涨跌幅']||pfChgPct;}
      let chgClass=pfChgPct>=0?'up':'dn';
      html+=`<div class="pf-item ${isZt?'pf-zt':''}">
        <div class="pf-main">
          <span class="pf-code">${p.code}</span>
          <span class="pf-name">${p.name||'--'}</span>
          ${isZt?'<span class="tag tag-yz" style="font-size:10px">涨停中</span>':''}
        </div>
        <div class="pf-detail">
          <span>数量: ${p.qty||'--'}</span>
          <span>成本: ${p.cost||'--'}</span>
          ${pfPrice?`<span>现价: <b>${pfPrice.toFixed(2)}</b></span>`:''}
          ${pfChgPct?`<span class="${chgClass}">${pfChgPct>=0?'+':''}${pfChgPct.toFixed(2)}%</span>`:''}
        </div>
        <button class="btn" onclick="removePortfolio(${i})" style="font-size:11px;padding:2px 8px;color:var(--rd);border-color:var(--rd)">删除</button>
      </div>`;
    });
  }
  html+=`</div><div style="margin-top:8px;font-size:11px;color:var(--tx3)">💡 输入代码后按回车或点击添加，名称会自动匹配</div></div></div>`;
  el.innerHTML=html;
}

function getPortfolio(){
  try{return JSON.parse(localStorage.getItem('stock_portfolio')||'[]');}catch(e){return[];}
}
function savePortfolio(pf){localStorage.setItem('stock_portfolio',JSON.stringify(pf));}
async function addPortfolio(){
  let codeEl=document.getElementById('pfCode');
  let code=(codeEl.value||'').trim();
  let qty=(document.getElementById('pfQty').value||'').trim();
  let cost=(document.getElementById('pfCost').value||'').trim();
  if(!code){alert('请输入股票代码');return;}
  // 如果还没获取到股票信息，先获取
  if(!codeEl.dataset.name){
    try{
      let r=await fetch('/api/stock-quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes:[code]})});
      let data=await r.json();
      if(data[code]&&!data[code].error){
        let s=data[code];
        codeEl.dataset.name=s['名称']||'';
        codeEl.dataset.price=s['最新价']||0;
        codeEl.dataset.change=s['涨跌额']||0;
        codeEl.dataset.changePct=s['涨跌幅']||0;
      }
    }catch(e){}
  }
  let pf=getPortfolio();
  let name='',price=0,change=0,changePct=0;
  // 优先从 fetchStockInfo 设置的 dataset 中获取
  if(codeEl.dataset.name){
    name=codeEl.dataset.name||'';
    price=parseFloat(codeEl.dataset.price)||0;
    change=parseFloat(codeEl.dataset.change)||0;
    changePct=parseFloat(codeEl.dataset.changePct)||0;
  }
  // 从实时行情缓存中获取
  if(!name && conceptStockQuotes[code] && !conceptStockQuotes[code].error){
    let q=conceptStockQuotes[code];
    name=q['名称']||'';
    price=q['最新价']||0;
    changePct=q['涨跌幅']||0;
    change=q['涨跌额']||0;
  }
  // 从涨跌停数据中获取
  if(!name){
    let lu=D.limit_up||[],ld=D.limit_down||[],br=D.broken_limit||[];
    let all=[...lu,...ld,...br];
    let match=all.find(s=>s['代码']===code);
    if(match){
      name=match['名称']||'';
      price=match['最新价']||0;
      changePct=match['涨跌幅']||0;
      change=match['涨跌额']||0;
    }
  }
  pf.push({code,name,qty,cost,price,change,changePct});
  savePortfolio(pf);
  codeEl.value='';codeEl.dataset.name='';codeEl.dataset.price='';codeEl.dataset.change='';codeEl.dataset.changePct='';
  document.getElementById('pfQty').value='';
  document.getElementById('pfCost').value='';
  let infoEl=document.getElementById('pfStockInfo');if(infoEl)infoEl.innerHTML='';
  renderPortfolio(document.getElementById('tabContent'));
}
function removePortfolio(idx){
  let pf=getPortfolio();
  pf.splice(idx,1);
  savePortfolio(pf);
  renderPortfolio(document.getElementById('tabContent'));
}

/* ===== 想买列表 ===== */
function renderWatchlist(el){
  let wl=getWatchlist();
  let lu=D.limit_up||[];
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🎯 想买列表</span><span class="card-c">${wl.length}只</span></div><div class="card-b" style="padding:16px">
    <div style="display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap;align-items:center">
      <input id="wlCode" class="pf-input" placeholder="输入股票代码" style="width:130px" onkeydown="if(event.key==='Enter'){addWatchlist();}" oninput="fetchStockInfo('wl')">
      <span id="wlStockInfo" style="font-size:12px;color:var(--tx3);min-width:80px;white-space:nowrap"></span>
      <input id="wlReason" class="pf-input" placeholder="买入理由（可选）" style="width:160px">
      <input id="wlTarget" class="pf-input" placeholder="目标价" type="number" step="0.01" style="width:85px">
      <input id="wlBuyPrice" class="pf-input" placeholder="买入价" type="number" step="0.01" style="width:85px">
      <button class="btn" onclick="addWatchlist()" style="background:var(--ac);color:#fff;border:none">➕ 添加</button>
    </div>
    <div id="wlList">`;
  if(!wl.length){
    html+=`<div class="empty">暂无想买的股票，添加后AI预测会纳入分析</div>`;
  }else{
    wl.forEach((w,i)=>{
      let isZt=lu.some(s=>s['代码']===w.code);
      let matchZt=isZt?lu.find(s=>s['代码']===w.code):null;
      let price=w.price||0;
      let chg=w.change||0;
      let chgPct=w.changePct||0;
      html+=`<div class="pf-item ${isZt?'pf-zt':''}">
        <div class="pf-main">
          <span class="pf-code">${w.code}</span>
          <span class="pf-name">${w.name||'--'}</span>
          ${isZt?'<span class="tag tag-yz" style="font-size:10px">已涨停</span>':''}
        </div>
        <div class="pf-detail">
          <span>现价: <b>${price?price.toFixed(2):'--'}</b></span>
          <span class="${chg>=0?'up':'dn'}">${chg>=0?'+':''}${chgPct?chgPct.toFixed(2):'0'}%</span>
          ${w.target?`<span>目标: <b>${w.target}</b></span>`:''}
          ${w.buyPrice?`<span>买入: <b>${w.buyPrice}</b></span>`:''}
          ${w.reason?`<span style="color:var(--tx3)">💭 ${w.reason}</span>`:''}
        </div>
        <button class="btn" onclick="removeWatchlist(${i})" style="font-size:11px;padding:2px 8px;color:var(--rd);border-color:var(--rd)">删除</button>
      </div>`;
    });
  }
  html+=`</div><div style="margin-top:8px;font-size:11px;color:var(--tx3)">💡 输入代码后自动获取股票信息，按回车或点击添加</div></div></div>`;
  el.innerHTML=html;
}
function getWatchlist(){try{return JSON.parse(localStorage.getItem('stock_watchlist')||'[]');}catch(e){return[];}}
function saveWatchlist(wl){localStorage.setItem('stock_watchlist',JSON.stringify(wl));}

// 获取股票信息并显示
let _fetchStockTimer = null;
async function fetchStockInfo(type){
  clearTimeout(_fetchStockTimer);
  _fetchStockTimer = setTimeout(async ()=>{
    let codeInput = document.getElementById(type+'Code');
    let infoDiv = document.getElementById(type+'StockInfo');
    if(!codeInput || !infoDiv) return;
    let code = codeInput.value.trim();
    if(code.length < 6){infoDiv.innerHTML='';return;}
    
    infoDiv.innerHTML = '<span style="color:var(--tx3)">查询中...</span>';
    try{
      let r = await fetch('/api/stock-quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes:[code]})});
      let data = await r.json();
      if(data[code] && !data[code].error){
        let s = data[code];
        let price = s['最新价']||0;
        let chg = s['涨跌幅']||0;
        let chgAmt = s['涨跌额']||0;
        let name = s['名称']||'';
        infoDiv.innerHTML = `<span style="color:var(--bl);font-weight:600">${name}</span> <span>${price.toFixed(2)}</span> <span class="${chg>=0?'up':'dn'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>`;
        // 自动填充隐藏字段
        codeInput.dataset.name = name;
        codeInput.dataset.price = price;
        codeInput.dataset.change = chgAmt;
        codeInput.dataset.changePct = chg;
      }else{
        // 尝试从涨停数据中查找
        let allStocks = [...(D.limit_up||[]),...(D.limit_down||[]),...(D.broken_limit||[])];
        let match = allStocks.find(s=>s['代码']===code);
        if(match){
          infoDiv.innerHTML = `<span style="color:var(--bl);font-weight:600">${match['名称']}</span> <span>${(match['最新价']||0).toFixed(2)}</span> <span class="${(match['涨跌幅']||0)>=0?'up':'dn'}">${(match['涨跌幅']||0)>=0?'+':''}${(match['涨跌幅']||0).toFixed(2)}%</span>`;
          codeInput.dataset.name = match['名称']||'';
          codeInput.dataset.price = match['最新价']||0;
          codeInput.dataset.change = match['涨跌幅']||0;
          codeInput.dataset.changePct = match['涨跌幅']||0;
        }else{
          infoDiv.innerHTML = '<span style="color:var(--rd)">未找到该股票</span>';
        }
      }
    }catch(e){
      infoDiv.innerHTML = '<span style="color:var(--rd)">查询失败</span>';
    }
  }, 300);
}

function addWatchlist(){
  let codeInput = document.getElementById('wlCode');
  let code=(codeInput.value||'').trim();
  let reason=(document.getElementById('wlReason').value||'').trim();
  let target=(document.getElementById('wlTarget').value||'').trim();
  let buyPrice=(document.getElementById('wlBuyPrice').value||'').trim();
  if(!code){alert('请输入股票代码');return;}
  let wl=getWatchlist();
  // 从输入框的dataset获取股票信息
  let name=codeInput.dataset.name||'';
  let price=parseFloat(codeInput.dataset.price)||0;
  let change=parseFloat(codeInput.dataset.change)||0;
  let changePct=parseFloat(codeInput.dataset.changePct)||0;
  wl.push({code,name,reason,target,buyPrice,price,change,changePct});
  saveWatchlist(wl);
  codeInput.value='';
  codeInput.dataset.name='';
  codeInput.dataset.price='';
  codeInput.dataset.change='';
  codeInput.dataset.changePct='';
  document.getElementById('wlReason').value='';
  document.getElementById('wlTarget').value='';
  document.getElementById('wlBuyPrice').value='';
  document.getElementById('wlStockInfo').innerHTML='';
  renderWatchlist(document.getElementById('tabContent'));
}
function removeWatchlist(idx){let wl=getWatchlist();wl.splice(idx,1);saveWatchlist(wl);renderWatchlist(document.getElementById('tabContent'));}

/* ===== 通用折叠/展开 ===== */
function toggleCollapse(id){
  const body=document.getElementById(id+'-body');
  const arrow=document.getElementById(id+'-arrow');
  if(!body)return;
  body.classList.toggle('open');
  if(arrow)arrow.classList.toggle('open');
}
function toggleThinkBody(){
  const body=document.getElementById('aiThinkBody');
  const arrow=document.getElementById('aiThinkArrow');
  if(!body)return;
  body.classList.toggle('open');
  if(arrow)arrow.textContent=body.classList.contains('open')?'▲ 收起':'▼ 展开';
}
function toggleAllChecks(val){
  ['chatCkLu','chatCkLd','chatCkBr','chatCkSs','chatCkNw','chatCkSf','chatCkCg','chatCkPf','chatCkWl','chatCkHot','chatCkMf','chatCkYzt','chatCkNh','chatCkSl','chatCkKn'].forEach(id=>{
    const el=document.getElementById(id);if(el)el.checked=val;
  });
}
function buildFullPrompt(){
  const ckData=chatGetCkData();
  const lu=D.limit_up||[],ld=D.limit_down||[],br=D.broken_limit||[],sf=D.sector_flow||[],rs=D.restructure||[],nw=D.news||[];
  const pf=getPortfolio();
  const conceptGroups=getConceptGroups();
  const wl=getWatchlist();
  let prompt=`你是一位专业的A股短线交易分析师，擅长龙头股战法和板块轮动分析。请基于以下${D.date||''}的A股复盘数据，给出下一个交易日的操盘建议。\n`;
  if(ckData.limit_up!==false){
    prompt+=`\n## 一、涨停股数据（共${lu.length}只，以下为全部数据）\n`;
    prompt+=`| 代码 | 名称 | 最新价 | 涨跌幅% | 连板数 | 首封时间 | 炸板次数 | 换手率% | 封板资金(亿) | 涨停统计 | 所属行业 |\n`;
    prompt+=`| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n`;
    const sortedLu=[...lu].sort((a,b)=>-(a['连板数']||1)+(b['连板数']||1));
    sortedLu.forEach(s=>{
      prompt+=`| ${s['代码']||''} | ${s['名称']||''} | ${(s['最新价']||0).toFixed(2)} | ${(s['涨跌幅']||0).toFixed(2)} | ${s['连板数']||1} | ${s['首次封板时间']||''} | ${s['炸板次数']||0} | ${(s['换手率']||0).toFixed(2)} | ${((s['封板资金']||0)/1e8).toFixed(2)} | ${s['涨停统计']||'--'} | ${s['所属行业']||''} |\n`;
    });
  }
  if(ckData.limit_down!==false){
    prompt+=`\n## 二、跌停股数据（共${ld.length}只，以下为全部数据）\n`;
    prompt+=`| 代码 | 名称 | 最新价 | 跌幅% | 连续跌停 | 最后封板 | 开板次数 | 换手率% | 封单资金(万) | 所属行业 |\n`;
    prompt+=`| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n`;
    ld.forEach(s=>{
      prompt+=`| ${s['代码']||''} | ${s['名称']||''} | ${(s['最新价']||0).toFixed(2)} | ${(s['涨跌幅']||0).toFixed(2)} | ${s['连续跌停']||1} | ${s['最后封板时间']||''} | ${s['开板次数']||0} | ${(s['换手率']||0).toFixed(2)} | ${((s['封单资金']||0)/1e4).toFixed(0)} | ${s['所属行业']||''} |\n`;
    });
  }
  if(ckData.broken_limit!==false){
    prompt+=`\n## 三、炸板股数据（共${br.length}只，以下为全部数据）\n`;
    prompt+=`| 代码 | 名称 | 现价 | 涨幅% | 涨停价 | 首封时间 | 炸板次数 | 振幅% | 换手率% | 所属行业 |\n`;
    prompt+=`| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n`;
    br.forEach(s=>{
      prompt+=`| ${s['代码']||''} | ${s['名称']||''} | ${(s['最新价']||0).toFixed(2)} | ${(s['涨跌幅']||0).toFixed(2)} | ${(s['涨停价']||0).toFixed(2)} | ${s['首次封板时间']||''} | ${s['炸板次数']||0} | ${(s['振幅']||0).toFixed(2)} | ${(s['换手率']||0).toFixed(2)} | ${s['所属行业']||''} |\n`;
    });
  }
  if(ckData.sector_flow!==false){
    prompt+=`\n## 四、板块资金流向（共${sf.length}个行业，以下为全部数据）\n`;
    prompt+=`| 行业 | 涨跌幅% | 净额(亿) | 流入(亿) | 流出(亿) | 领涨股 | 领涨涨幅% |\n`;
    prompt+=`| --- | --- | --- | --- | --- | --- | --- |\n`;
    sf.forEach(s=>{
      prompt+=`| ${s['名称']||''} | ${(s['今日涨跌幅']||0).toFixed(2)} | ${(s['净额']||0).toFixed(2)} | ${(s['流入资金']||0).toFixed(2)} | ${(s['流出资金']||0).toFixed(2)} | ${s['领涨股']||''} | ${(s['领涨股-涨跌幅']||0).toFixed(2)} |\n`;
    });
  }
  if(ckData.news!==false&&nw.length){
    prompt+=`\n## 五、重要财经新闻（共${nw.length}条）\n`;
    nw.slice(0,100).forEach(n=>{
      prompt+=`- [${n['发布时间']||''}] ${n['标题']||''}\n`;
      if(n['摘要'])prompt+=`  摘要: ${n['摘要']}\n`;
    });
  }
  if(ckData.concept_groups!==false&&conceptGroups.length){
    prompt+=`\n## 六、分组概念关注\n`;
    conceptGroups.forEach(g=>{
      const stocks=g.stocks||[];
      if(stocks.length){
        const stockList=stocks.map(s=>`${s.name||s.code}(${s.code})`).join('、');
        prompt+=`- ${g.name}(${g.nameEn||''})：${stockList}\n`;
      }
    });
    prompt+=`\n请重点关注以上分组概念的联动性和个股表现。\n`;
  }
  if(ckData.watchlist!==false&&wl.length){
    prompt+=`\n## 七、想买列表（用户计划买入的股票）\n`;
    prompt+=`| 代码 | 名称 | 买入理由 | 目标价 | 当前状态 |\n`;
    prompt+=`| --- | --- | --- | --- | --- |\n`;
    const stockMap={};
    lu.forEach(s=>{stockMap[s['代码']]=s;});
    ld.forEach(s=>{stockMap[s['代码']]=s;});
    br.forEach(s=>{stockMap[s['代码']]=s;});
    wl.forEach(w=>{
      const live=stockMap[w.code];
      let status=live?'现价'+(live['最新价']||0).toFixed(2):'未在今日涨跌停列表';
      prompt+=`| ${w.code} | ${w.name||''} | ${w.reason||'--'} | ${w.target||'--'} | ${status} |\n`;
    });
  }
  if(ckData.portfolio!==false&&pf.length){
    prompt+=`\n## 持仓数据\n`;
    prompt+=`| 代码 | 名称 | 数量 | 成本价 | 当前状态 |\n`;
    prompt+=`| --- | --- | --- | --- | --- |\n`;
    const luMap={};lu.forEach(s=>{luMap[s['代码']]=s;});
    pf.forEach(p=>{
      const m=luMap[p.code];
      let status=m?`🔴 涨停中! 现价${m['最新价'].toFixed(2)} 涨幅${m['涨跌幅'].toFixed(2)}%`:'未在涨停/跌停列表';
      prompt+=`| ${p.code} | ${p.name||''} | ${p.qty||'--'} | ${p.cost||'--'} | ${status} |\n`;
    });
  }
  prompt+=`\n## 八、请分析并给出以下内容\n\n### 1. 市场情绪判断\n- 当前市场整体情绪（亢奋/乐观/中性/谨慎/恐慌）\n- 涨停跌停比的含义\n- 连板梯队分析\n\n### 2. 热门板块分析\n- 当日最强板块及原因\n- 资金主攻方向\n- 哪些板块可能延续？哪些可能见顶？\n\n### 3. 龙头股预测（重点）\n- 哪些连板股可能晋级成功？\n- 哪些板块可能出现新的龙头？\n- 具体个股推荐（代码+名称+理由）\n\n### 4. 风险提示\n- 哪些股票可能开板/补跌？\n- 哪些板块需要回避？\n\n### 5. 下个交易日操盘策略\n- 重点关注方向\n- 建议操作策略（打板/低吸/半路）\n- 仓位建议\n\n请用简洁专业的语言回答，直接给出结论。`;
  return prompt;
}
function showPromptPreview(){
  const prompt=buildFullPrompt();
  const existing=document.getElementById('promptPreviewBox');
  if(existing){existing.remove();return;}
  const box=document.createElement('div');
  box.id='promptPreviewBox';
  box.style.cssText='position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:600px;max-width:90vw;max-height:70vh;overflow-y:auto;background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:20px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,.5)';
  box.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b style="color:var(--ac)">📝 当前提示词预览</b><button class="btn" onclick="document.getElementById('promptPreviewBox').remove()" style="font-size:11px">✕ 关闭</button></div><pre style="white-space:pre-wrap;font-size:12px;color:var(--tx2);background:var(--sf2);padding:12px;border-radius:8px;line-height:1.6">${prompt.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`;
  document.body.appendChild(box);
}

function renderAIHist(el){
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t"><i data-lucide="history" style="width:16px;height:16px;display:inline;vertical-align:-3px"></i> 预测历史</span><button class="btn" onclick="loadAIPredictionHistory()" style="font-size:11px"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新</button></div><div class="card-b" id="aiHistList"><div style="padding:20px;text-align:center;color:var(--tx3)">加载中...</div></div></div></div>`;
  el.innerHTML=html;
  loadAIPredictionHistory();
}

function renderConcept(el){
  let cg=getConceptGroups();
  let allStocks=[...(D.limit_up||[]),...(D.limit_down||[]),...(D.broken_limit||[])];
  let stockMap={};
  (D.limit_up||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'涨停'};});
  (D.limit_down||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'跌停'};});
  (D.broken_limit||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'炸板'};});
  
  // Initialize selected concept tab
  if(typeof window._cgSelectedTab==='undefined')window._cgSelectedTab=0;
  if(window._cgSelectedTab>=cg.length)window._cgSelectedTab=0;
  
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🏷️ 分组概念管理</span><span class="card-c">${cg.length}个分组</span></div><div class="card-b" style="padding:12px">
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <button class="btn" onclick="fetchAllConceptQuotes()" id="loadQuotesBtn" style="background:var(--gn);color:#fff;border:none"><i data-lucide="refresh-cw" style="width:14px;height:14px;display:inline;vertical-align:-2px"></i> 刷新行情</button>
      <input id="cgName" class="pf-input" placeholder="分组名称 如 PCB" style="width:120px">
      <input id="cgNameEn" class="pf-input" placeholder="英文名 如 PCB" style="width:120px">
      <button class="btn" onclick="addConceptGroup()" style="background:var(--ac);color:#000;border:none">➕ 新建分组</button>
    </div>`;
  if(!cg.length){
    html+=`<div class="empty">暂无分组，正在加载预设概念...</div>`;
    // 自动加载预设概念
    setTimeout(()=>loadDefaultConcepts(), 100);
  }else{
    html+=`<div style="display:grid;grid-template-columns:120px 1fr;gap:0;border:1px solid var(--bd);border-radius:8px;overflow:hidden;min-height:400px">`;
    // Left: tab list (draggable)
    html+=`<div style="background:var(--sf2);border-right:1px solid var(--bd);overflow-y:auto" id="conceptGroupList">`;
    cg.forEach((g,gi)=>{
      let stocks=g.stocks||[];
      let seen={};stocks=stocks.filter(s=>{if(seen[s.code])return false;seen[s.code]=true;return true;});
      let isActive=gi===window._cgSelectedTab;
      html+=`<div draggable="true" ondragstart="dragConceptGroup(event,${gi})" ondragover="event.preventDefault()" ondrop="dropConceptGroup(event,${gi})" ondragenter="this.style.background='rgba(167,139,250,.15)'" ondragleave="this.style.background=''" onclick="window._cgSelectedTab=${gi};renderConcept(document.getElementById('tabContent'))" oncontextmenu="event.preventDefault();editConceptGroup(${gi})" style="padding:10px 12px;cursor:grab;border-left:3px solid ${isActive?'var(--ac)':'transparent'};background:${isActive?'rgba(167,139,250,.08)':'transparent'};transition:.15s;border-bottom:1px solid rgba(42,42,74,.3);user-select:none">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="color:var(--tx3);cursor:grab;font-size:12px">⠿</span>
          <div style="flex:1">
            <div style="font-weight:600;font-size:13px;color:${isActive?'var(--ac)':'var(--tx)'}">${g.name}</div>
            <div style="font-size:11px;color:var(--tx3)">${stocks.length}只 ${g.nameEn||''}</div>
          </div>
        </div>
      </div>`;
    });
    html+=`</div>`;
    // Right: detail area
    let selGi=window._cgSelectedTab;
    let selG=cg[selGi];
    let selStocks=(selG.stocks||[]);
    let seen2={};selStocks=selStocks.filter(s=>{if(seen2[s.code])return false;seen2[s.code]=true;return true;});
    html+=`<div style="padding:12px;overflow-y:auto;max-height:500px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div><span style="font-weight:600;font-size:15px;color:var(--ac)">${selG.name}</span> <span style="color:var(--tx3);font-size:12px">${selG.nameEn||''}</span> <span style="font-size:11px;color:var(--tx3);background:var(--sf3);padding:1px 6px;border-radius:8px">${selStocks.length}只</span></div>
        <div style="display:flex;gap:6px">
          <button class="btn" onclick="fetchConceptGroupQuotes(${selGi})" style="font-size:11px;padding:3px 10px;background:var(--gn);color:#fff">📊 刷新行情</button>
          <button class="btn" onclick="addStockToGroup(${selGi})" style="font-size:11px;padding:3px 10px">+ 添加股票</button>
          <button class="btn" onclick="removeConceptGroup(${selGi})" style="font-size:11px;padding:3px 10px;color:var(--rd)">删除分组</button>
        </div>
      </div>
      <div class="cg-add-stock" id="cgAdd${selGi}" style="display:none;padding:8px 12px;background:var(--sf3);border-radius:6px;margin-bottom:8px">
        <div style="display:flex;gap:6px">
          <input id="cgStock${selGi}" class="pf-input" placeholder="输入股票代码，按回车确认" style="width:200px" onkeydown="if(event.key==='Enter'){doAddStock(${selGi});}">
          <button class="btn" onclick="doAddStock(${selGi})" style="font-size:11px">确认</button>
          <button class="btn" onclick="document.getElementById('cgAdd${selGi}').style.display='none'" style="font-size:11px">取消</button>
        </div>
      </div>`;
    if(!selStocks.length){
      html+=`<div class="empty">该分组暂无个股，点击"添加股票"开始</div>`;
    }else{
      // 分析每只股票的角色：龙头/先锋/跟风
      let maxLb=Math.max(...selStocks.map(s=>{let m=stockMap[s.code];return m?(m['连板数']||1):0;}),0);
      selStocks.forEach(s=>{
        let m=stockMap[s.code];
        if(!m||m.status!=='涨停'){s._role='正常';s._roleColor='var(--tx3)';s._roleBg='var(--sf3)';return;}
        let lb=m['连板数']||1;
        let sealTime=m['首次封板时间']||'99:99';
        let zb=m['炸板次数']||0;
        if(lb===maxLb&&maxLb>=2){s._role='👑龙头';s._roleColor='var(--rd)';s._roleBg='rgba(232,84,84,.15)';}
        else if(lb>=2){s._role='🔥先锋';s._roleColor='var(--or)';s._roleBg='rgba(240,176,96,.15)';}
        else if(sealTime<'09:45'&&zb===0){s._role='⚡先锋';s._roleColor='var(--or)';s._roleBg='rgba(240,176,96,.15)';}
        else{s._role='📌跟风';s._roleColor='var(--ac)';s._roleBg='rgba(102,178,255,.15)';}
      });
      // 排序：龙头 > 先锋 > 跟风 > 涨停 > 正常
      let roleOrder={'👑龙头':0,'🔥先锋':1,'⚡先锋':2,'📌跟风':3,'正常':4};
      selStocks.sort((a,b)=>{
        let ra=roleOrder[a._role]??4,rb=roleOrder[b._role]??4;
        if(ra!==rb)return ra-rb;
        let ma=stockMap[a.code],mb=stockMap[b.code];
        let lba=ma?(ma['连板数']||0):0,lbb=mb?(mb['连板数']||0):0;
        return lbb-lba;
      });
      html+=`<table class="tbl" style="margin:0"><thead><tr><th style="width:55px">角色</th><th style="width:70px">代码</th><th style="width:80px">名称</th><th style="width:60px">现价</th><th style="width:65px">涨跌幅</th><th style="width:70px">状态</th><th style="width:75px">成交额</th><th style="width:55px">换手率</th><th style="width:55px">振幅</th><th style="width:60px">最高</th><th style="width:60px">最低</th><th style="width:55px">市盈率</th><th style="width:75px">流通市值</th><th style="width:35px">操作</th></tr></thead><tbody>`;
      selStocks.forEach((s,si)=>{
        let m=stockMap[s.code];
        let q=conceptStockQuotes[s.code];
        let roleTag=`<span style="color:${s._roleColor};font-weight:600;background:${s._roleBg};padding:1px 6px;border-radius:4px;font-size:10px;white-space:nowrap">${s._role}</span>`;
        if(q && !q.error){
          let chg=q['涨跌幅']||0;let price=q['最新价']||0;let vol=q['成交额']||0;let turn=q['换手率']||0;let amp=q['振幅']||0;let high=q['最高']||0;let low=q['最低']||0;let pe=q['市盈率-动态']||0;let mv=q['流通市值']||0;
          let status='正常';let statusColor='var(--tx3)';let statusBg='var(--sf3)';
          if(m){status=m.status;statusColor=m.status==='涨停'?'var(--rd)':m.status==='跌停'?'var(--gn)':'var(--ac)';statusBg=m.status==='涨停'?'rgba(232,84,84,.15)':m.status==='跌停'?'rgba(78,203,113,.15)':'rgba(240,176,96,.15)';}
          html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.code}')"><td>${roleTag}</td><td style="color:var(--bl);font-weight:500">${s.code}</td><td>${q['名称']||s.name||'--'}</td><td>${price.toFixed(2)}</td><td style="color:${chg>=0?'var(--rd)':'var(--gn)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</td><td><span style="color:${statusColor};font-weight:600;background:${statusBg};padding:1px 6px;border-radius:4px;font-size:11px">${status}</span></td><td>${formatVolume(vol)}</td><td>${turn.toFixed(2)}%</td><td>${amp.toFixed(2)}%</td><td>${high.toFixed(2)}</td><td>${low.toFixed(2)}</td><td>${pe.toFixed(1)}</td><td>${formatVolume(mv)}</td><td><button class="btn" onclick="event.stopPropagation();removeStockFromGroup(${selGi},${si})" style="font-size:10px;padding:1px 6px;color:var(--rd)">×</button></td></tr>`;
        }else if(m){
          let statusColor=m.status==='涨停'?'var(--rd)':m.status==='跌停'?'var(--gn)':'var(--ac)';let statusBg=m.status==='涨停'?'rgba(232,84,84,.15)':m.status==='跌停'?'rgba(78,203,113,.15)':'rgba(240,176,96,.15)';
          html+=`<tr style="cursor:pointer" onclick="viewStockDetail('${s.code}')"><td>${roleTag}</td><td style="color:var(--bl);font-weight:500">${s.code}</td><td>${m['名称']}</td><td>${(m['最新价']||0).toFixed(2)}</td><td style="color:${(m['涨跌幅']||0)>=0?'var(--rd)':'var(--gn)'}">${(m['涨跌幅']||0).toFixed(2)}%</td><td><span style="color:${statusColor};font-weight:600;background:${statusBg};padding:1px 6px;border-radius:4px;font-size:11px">${m.status}</span></td><td>${formatVolume(m['成交额']||0)}</td><td>${(m['换手率']||0).toFixed(2)}%</td><td>--</td><td>--</td><td>--</td><td>--</td><td>--</td><td><button class="btn" onclick="event.stopPropagation();removeStockFromGroup(${selGi},${si})" style="font-size:10px;padding:1px 6px;color:var(--rd)">×</button></td></tr>`;
        }else{
          html+=`<tr><td>${roleTag}</td><td style="color:var(--bl);font-weight:500">${s.code}</td><td>${s.name||'--'}</td><td colspan="9" style="color:var(--tx3)">加载中...</td><td></td><td><button class="btn" onclick="removeStockFromGroup(${selGi},${si})" style="font-size:10px;padding:1px 6px;color:var(--rd)">×</button></td></tr>`;
        }
      });
      html+=`</tbody></table>`;
    }
    html+=`</div></div>`;
  }
  html+=`</div></div>`;
  el.innerHTML=html;
  // 自动加载行情数据（如果还没有数据）
  if(cg.length && Object.keys(conceptStockQuotes).length===0){
    fetchAllConceptQuotes();
  }
}
// 加载单个分组的行情
async function fetchConceptGroupQuotes(gi){
  let cg=getConceptGroups();
  let g=cg[gi];
  if(!g)return;
  let codes=(g.stocks||[]).map(s=>s.code).filter(c=>c);
  if(!codes.length)return;
  let btn=event.target;
  btn.disabled=true;btn.textContent='加载中...';
  try{
    let r=await fetch('/api/stock-quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes})});
    let data=await r.json();
    if(!data.error){
      Object.assign(conceptStockQuotes,data);
      renderConcept(document.getElementById('tabContent'));
    }
  }catch(e){console.error(e);}
  btn.disabled=false;btn.textContent='📊 加载行情';
}
// 加载全部分组的行情
async function fetchAllConceptQuotes(){
  let cg=getConceptGroups();
  let allCodes=[];
  cg.forEach(g=>{(g.stocks||[]).forEach(s=>{if(s.code)allCodes.push(s.code);});});
  if(!allCodes.length)return;
  let btn=document.getElementById('loadQuotesBtn');
  if(btn){btn.disabled=true;btn.textContent='加载中...';}
  try{
    // 分批加载，每批最多50只
    for(let i=0;i<allCodes.length;i+=50){
      let batch=allCodes.slice(i,i+50);
      let r=await fetch('/api/stock-quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes:batch})});
      let data=await r.json();
      if(!data.error)Object.assign(conceptStockQuotes,data);
    }
    renderConcept(document.getElementById('tabContent'));
  }catch(e){console.error(e);}
  if(btn){btn.disabled=false;btn.textContent='📊 加载全部行情';}
}
function toggleConceptGroup(gi){
  let body=document.getElementById('cgBody'+gi);
  if(!body)return;
  let isShown=body.style.display!=='none';
  body.style.display=isShown?'none':'block';
  window['_cgExpanded'+gi]=!isShown;
  // 更新箭头方向
  let header=body.previousElementSibling;
  if(header){
    let arrow=header.querySelector('span');
    if(arrow)arrow.style.transform=isShown?'':'rotate(90deg)';
  }
}

function renderConceptDetail(gi){
  curTab='concept_detail_'+gi;
  renderSidebar();
  const c=document.getElementById('tabContent');
  const cg=getConceptGroups();
  const g=cg[gi];
  if(!g){c.innerHTML='<div class="empty">分组不存在</div>';return;}
  const allStocks=[...(D.limit_up||[]),...(D.limit_down||[]),...(D.broken_limit||[])];
  const stockMap={};
  (D.limit_up||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'涨停'};});
  (D.limit_down||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'跌停'};});
  (D.broken_limit||[]).forEach(s=>{stockMap[s['代码']]={...s,status:'炸板'};});
  const stocks=g.stocks||[];
  let seen={};
  const uniqueStocks=stocks.filter(s=>{if(seen[s.code])return false;seen[s.code]=true;return true;});
  let html=`<div class="tab-full"><div class="card"><div class="card-h"><span class="card-t">🏷️ ${g.name} ${g.nameEn||''}</span><span class="card-c">${uniqueStocks.length}只</span><button class="btn" onclick="switchTab('concept')" style="font-size:11px;margin-left:auto">← 返回</button></div><div class="card-b" style="padding:16px">`;
  if(!uniqueStocks.length){
    html+='<div class="empty">该分组暂无个股</div>';
  }else{
    html+='<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>状态</th><th>成交额</th><th>换手率</th><th>行业</th></tr></thead><tbody>';
    uniqueStocks.forEach(s=>{
      const m=stockMap[s.code];
      if(m){
        const statusColor=m.status==='涨停'?'var(--rd)':m.status==='跌停'?'var(--gn)':'var(--ac)';
        html+=`<tr><td>${s.code}</td><td>${m['名称']}</td><td>${(m['最新价']||0).toFixed(2)}</td><td style="color:${(m['涨跌幅']||0)>=0?'var(--rd)':'var(--gn)'}">${(m['涨跌幅']||0).toFixed(2)}%</td><td><span style="color:${statusColor};font-weight:600">${m.status}</span></td><td>${(m['成交额']||0).toFixed(0)}</td><td>${(m['换手率']||0).toFixed(2)}%</td><td>${m['所属行业']||''}</td></tr>`;
      }else{
        html+=`<tr><td>${s.code}</td><td>${s.name||'--'}</td><td colspan="6" style="color:var(--tx3)">无数据</td></tr>`;
      }
    });
    html+='</tbody></table>';
  }
  html+='</div></div></div>';
  c.innerHTML=html;
}

function getConceptGroups(){
  try{return JSON.parse(localStorage.getItem('concept_groups')||'[]');}catch(e){return[];}
}
function saveConceptGroups(cg){localStorage.setItem('concept_groups',JSON.stringify(cg));}
async function loadDefaultConcepts(){
  try{
    const r=await fetch('/api/default-concepts');
    const groups=await r.json();
    // Deduplicate stocks in each group
    groups.forEach(g=>{
      let seen={};
      g.stocks=(g.stocks||[]).filter(s=>{if(seen[s.code])return false;seen[s.code]=true;return true;});
    });
    saveConceptGroups(groups);
    renderConcept(document.getElementById('tabContent'));
    alert('✅ 已加载'+groups.length+'个预设概念分组');
  }catch(e){alert('加载失败: '+e.message);}
}

// 拖拽排序相关变量和函数
let _dragConceptIdx=-1;
function dragConceptGroup(e,idx){
  _dragConceptIdx=idx;
  e.dataTransfer.effectAllowed='move';
  e.target.style.opacity='0.5';
}
function dropConceptGroup(e,targetIdx){
  e.preventDefault();
  e.stopPropagation();
  if(_dragConceptIdx<0||_dragConceptIdx===targetIdx)return;
  let cg=getConceptGroups();
  let item=cg.splice(_dragConceptIdx,1)[0];
  cg.splice(targetIdx,0,item);
  saveConceptGroups(cg);
  window._cgSelectedTab=targetIdx;
  renderConcept(document.getElementById('tabContent'));
  _dragConceptIdx=-1;
}

function addConceptGroup(){
  let name=(document.getElementById('cgName').value||'').trim();
  let nameEn=(document.getElementById('cgNameEn').value||'').trim();
  if(!name){alert('请输入分组名称');return;}
  let cg=getConceptGroups();
  cg.push({name,nameEn,stocks:[]});
  saveConceptGroups(cg);
  document.getElementById('cgName').value='';
  document.getElementById('cgNameEn').value='';
  renderConcept(document.getElementById('tabContent'));
}
function editConceptGroup(gi){
  let cg=getConceptGroups();
  let g=cg[gi];
  if(!g)return;
  let newName=prompt('编辑分组名称:',g.name);
  if(newName===null)return;
  let newNameEn=prompt('编辑英文名称:',g.nameEn||'');
  if(newNameEn===null)return;
  cg[gi].name=newName.trim()||g.name;
  cg[gi].nameEn=newNameEn.trim();
  saveConceptGroups(cg);
  renderConcept(document.getElementById('tabContent'));
}

function removeConceptGroup(gi){
  if(!confirm('确定删除此分组？'))return;
  let cg=getConceptGroups();
  cg.splice(gi,1);
  saveConceptGroups(cg);
  renderConcept(document.getElementById('tabContent'));
}
function addStockToGroup(gi){
  let el=document.getElementById('cgAdd'+gi);
  if(el)el.style.display='block';
}
function doAddStock(gi){
  let code=(document.getElementById('cgStock'+gi).value||'').trim();
  if(!code){return;}
  let cg=getConceptGroups();
  // check for duplicates
  if((cg[gi].stocks||[]).some(s=>s.code===code)){alert('该股票已在分组中');return;}
  // 自动匹配名称
  let allStocks=[...(D.limit_up||[]),...(D.limit_down||[]),...(D.broken_limit||[])];
  let match=allStocks.find(s=>s['代码']===code);
  let name=match?match['名称']:'';
  cg[gi].stocks.push({code,name});
  saveConceptGroups(cg);
  document.getElementById('cgStock'+gi).value='';
  renderConcept(document.getElementById('tabContent'));
}
function removeStockFromGroup(gi,si){
  let cg=getConceptGroups();
  cg[gi].stocks.splice(si,1);
  saveConceptGroups(cg);
  renderConcept(document.getElementById('tabContent'));
}

function renderNews(el){
  let data=D.news||[];
  if(!data.length){el.innerHTML='<div class="tab-full"><div class="card"><div class="empty">暂无新闻数据</div></div></div>';return;}
  let html=`<div class="card"><div class="card-h"><span class="card-t">📰 实时财经快讯</span><span class="card-c">${data.length}条 · 每分钟自动刷新</span></div><div class="card-b news-scroll">`;
  data.forEach(s=>{
    const title=s['标题']||'';
    const summary=s['摘要']||'';
    const time=s['发布时间']||'';
    const link=s['链接']||'';
    const sent=analyzeNewsSentiment(title,summary);
    const sentColor=sent.sentiment==='利好'?'var(--rd)':sent.sentiment==='利空'?'var(--gn)':'var(--tx3)';
    const sentIcon=sent.sentiment==='利好'?'🔴':sent.sentiment==='利空'?'🟢':'⚪';
    let extra='';
    if(sent.sectors.length){extra+=`<span style="color:var(--bl);font-size:11px">📌 ${sent.sectors.join('、')}</span>`;}
    extra+=`<span style="color:${sentColor};font-size:11px;font-weight:600">${sentIcon} ${sent.sentiment}</span>`;
    const safeLink=link?link.replace(/"/g,'&quot;'):'';
    html+=`<div class="news-item"${safeLink?` onclick="window.open('${safeLink}','_blank')"`:''}>
      <div class="news-time">${time}</div>
      <div class="news-title">${title}</div>
      ${extra?'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:3px">'+extra+'</div>':''}
      ${summary?'<div class="news-summary">'+summary+'</div>':''}
      ${safeLink?'<div style="margin-top:4px"><a href="'+safeLink+'" target="_blank" onclick="event.stopPropagation()" style="color:var(--bl);font-size:11px;text-decoration:none">🔗 查看原文</a></div>':''}
    </div>`;
  });
  html+='</div></div>';
  el.innerHTML=html;
}

/* ===== 板块柱状图tooltip ===== */
function showSectorTip(e,name){
  let lu=D.limit_up||[];
  let sectorStocks=lu.filter(s=>(s['所属行业']||'')===name);
  let tip=document.getElementById('sectorTip');
  if(!tip){tip=document.createElement('div');tip.id='sectorTip';tip.style.cssText='position:fixed;z-index:9999;background:rgba(20,20,40,.95);border:1px solid var(--ac);border-radius:8px;padding:10px 14px;font-size:12px;color:var(--tx);max-width:320px;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.4)';document.body.appendChild(tip);}
  let h=`<div style="font-weight:600;color:var(--ac);margin-bottom:6px">📊 ${name} · 涨停${sectorStocks.length}只</div>`;
  if(!sectorStocks.length){h+=`<div style="color:var(--tx3)">暂无涨停股</div>`;}
  else{sectorStocks.forEach(s=>{
    h+=`<div style="display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-bottom:1px solid rgba(167,139,250,.15)"><span><b>${s['名称']}</b> <span style="color:var(--tx3)">${s['代码']}</span></span><span style="color:var(--rd)">${(s['涨跌幅']||0)>=0?'+':''}${(s['涨跌幅']||0).toFixed(2)}%</span></div>`;
  });}
  tip.innerHTML=h;tip.style.display='block';
  let x=Math.min(e.clientX+12,window.innerWidth-340);
  let y=Math.min(e.clientY+12,window.innerHeight-200);
  tip.style.left=x+'px';tip.style.top=y+'px';
}
function hideSectorTip(){let t=document.getElementById('sectorTip');if(t)t.style.display='none';}

/* ===== 新闻利好利空分析 ===== */
function analyzeNewsSentiment(title,summary){
  let text=(title+' '+summary).toLowerCase();
  let 利好=['利好','涨停','大涨','突破','新高','增长','盈利','超预期','获批','中标','合作','订单','增持','回购','并购','重组','注入','借壳','政策支持','补贴','减税'];
  let 利空=['利空','跌停','大跌','暴跌','破位','新低','亏损','减持','质押','违规','处罚','退市','暂停','终止','诉讼','仲裁','暴雷','爆仓','强平'];
  let sectors=['半导体','芯片','光模块','PCB','消费电子','新能源','光伏','锂电','白酒','医药','地产','银行','券商','保险','军工','汽车','AI','人工智能','机器人','5G','6G','数据中心','云计算','物联网','游戏','传媒','农业','钢铁','煤炭','有色','化工','电力','免税','旅游','家电','食品','物流'];
  let s=0;
  利好.forEach(k=>{if(text.includes(k))s++;});
  利空.forEach(k=>{if(text.includes(k))s--;});
  let found_sectors=sectors.filter(k=>text.includes(k));
  return {sentiment:s>0?'利好':s<0?'利空':'中性',score:s,sectors:found_sectors};
}

// 初始化 - 先用缓存数据快速加载，不阻塞页面
initTheme();
updateBadge();
renderSidebar();
renderTab();
lucide.createIcons();
// 后台异步加载数据
loadInitialData();
schedRefresh();

async function loadInitialData(){
  const date=getTradingDate();
  try{
    // 先用 /api/market 读缓存，秒加载
    const r=await fetch('/api/market?date='+date);
    D=await r.json();
    renderAll();
    document.getElementById('updateTime').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    document.getElementById('dataDate').textContent='数据日期: '+D.date;
  }catch(e){console.error('初始加载失败:',e);}
}

// Mobile sidebar toggle
function toggleMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  if(sidebar)sidebar.classList.toggle('open');
}
</script>
<button class="mobile-menu-btn" onclick="toggleMobileSidebar()"><i data-lucide="menu" style="width:20px;height:20px"></i></button>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/market")
def api_market():
    date_str = request.args.get('date', None)
    if date_str:
        date_str = date_str.replace('-', '')
    # 先查缓存
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if row:
        return jsonify(json.loads(row['market_data']))
    # 实时获取
    data = fetch_all_market_data(date_str)
    # 缓存到数据库
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO daily_snapshots (snapshot_date, market_data) VALUES (?, ?)",
                     (date_str, json.dumps(data, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except: pass
    return jsonify(data)

@app.route("/api/refresh")
def api_refresh():
    """强制刷新指定日期"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    date_str = date_str.replace('-', '')
    data = fetch_all_market_data(date_str)
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO daily_snapshots (snapshot_date, market_data) VALUES (?, ?)",
                     (date_str, json.dumps(data, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except: pass
    return jsonify(data)

@app.route("/api/history")
def api_history():
    conn = get_db()
    rows = conn.execute("SELECT snapshot_date, market_data FROM daily_snapshots WHERE CAST(strftime('%w', substr(snapshot_date,1,4)||'-'||substr(snapshot_date,5,2)||'-'||substr(snapshot_date,7,2)) AS INTEGER) BETWEEN 1 AND 5 ORDER BY snapshot_date DESC LIMIT 90").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/news")
def api_news():
    """获取最新财经新闻"""
    script = """
import akshare as ak
import json
try:
    df = ak.stock_info_global_em()
    records = []
    for _, r in df.head(200).iterrows():
        records.append({
            "标题": str(r.get("标题","")),
            "摘要": str(r.get("摘要",""))[:200],
            "发布时间": str(r.get("发布时间","")),
            "链接": str(r.get("链接",""))
        })
    print(json.dumps(records, ensure_ascii=False))
except:
    print(json.dumps([], ensure_ascii=False))
"""
    data = fetch_akshare(script, timeout=30)
    return jsonify(data or [])

@app.route("/api/ai-predict", methods=["POST"])
def api_ai_predict():
    """AI预测：收集所有完整数据，调用MiMo大模型分析"""
    import urllib.request
    from datetime import datetime as dt
    date_str = request.json.get("date", dt.now().strftime("%Y%m%d"))
    date_str = date_str.replace("-", "")
    custom_prompt = request.json.get("custom_prompt", "").strip()
    portfolio = request.json.get("portfolio", [])
    concept_groups = request.json.get("concept_groups", [])
    ck_data = request.json.get("ck_data", {})

    # 1. 获取缓存数据
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "请先加载数据"}), 400
    data = json.loads(row["market_data"])

    # 2. 获取100条新闻
    news_data = []
    try:
        script = '''
import akshare as ak, json
try:
    df = ak.stock_info_global_em()
    records = []
    for _, r in df.head(200).iterrows():
        records.append({"标题": str(r.get("标题","")), "摘要": str(r.get("摘要",""))[:150], "发布时间": str(r.get("发布时间",""))})
    print(json.dumps(records, ensure_ascii=False))
except:
    print(json.dumps([], ensure_ascii=False))
'''
        news_data = fetch_akshare(script, timeout=30) or []
    except:
        pass

    # 3. 筛选上个交易日收盘后到开盘之间的新闻
    # 计算上个交易日日期
    from datetime import datetime as dt2, timedelta
    today = dt2.strptime(date_str, "%Y%m%d")
    # 找上个交易日（跳过周末）
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:  # 跳过周末
        prev -= timedelta(days=1)
    prev_str = prev.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    filtered_news = []
    for n in news_data:
        pub_time = n.get("发布时间", "")
        if pub_time >= prev_str and pub_time < today_str + " 09:30":
            filtered_news.append(n)
    # 如果筛选后太少，补充最新的
    if len(filtered_news) < 10:
        filtered_news = news_data[:100]

    # 4. 构建完整提示词
    limit_up = data.get("limit_up", [])
    limit_down = data.get("limit_down", [])
    broken = data.get("broken_limit", [])
    sector = data.get("sector_flow", [])

    prompt = f"""你是一位专业的A股短线交易分析师，擅长龙头股战法和板块轮动分析。请基于以下{date_str}的A股复盘数据，给出下一个交易日的操盘建议。
"""

    # 涨停股
    if ck_data.get("limit_up", True):
        prompt += f"\n## 一、涨停股数据（共{len(limit_up)}只，以下为全部数据）\n"
        prompt += "| 代码 | 名称 | 最新价 | 涨跌幅% | 连板数 | 首封时间 | 炸板次数 | 换手率% | 封板资金(亿) | 涨停统计 | 所属行业 | 涨停原因 |\n"
        prompt += "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        limit_up_sorted = sorted(limit_up, key=lambda x: -(x.get("连板数", 1) or 1))
        # 统计行业涨停数，用于推断涨停原因
        industry_count = {}
        for s in limit_up:
            ind = s.get("所属行业", "")
            if ind:
                industry_count[ind] = industry_count.get(ind, 0) + 1
        hot_industries = sorted(industry_count.items(), key=lambda x: -x[1])[:5]
        hot_ind_names = [x[0] for x in hot_industries]
        for s in limit_up_sorted:
            lb = s.get("连板数", 1) or 1
            ind = s.get("所属行业", "")
            zb = s.get("炸板次数", 0)
            seal_time = s.get("首次封板时间", "")
            # 推断涨停原因
            reasons = []
            if lb >= 3: reasons.append(f"{lb}连板龙头")
            elif lb >= 2: reasons.append(f"{lb}连板")
            if ind in hot_ind_names[:3]: reasons.append(f"{ind}板块热度")
            if seal_time and seal_time < "09:35": reasons.append("一字板/秒板")
            elif seal_time and seal_time < "09:45": reasons.append("早盘强势封板")
            if zb == 0 and lb == 1: reasons.append("首板强势封停")
            elif zb >= 2: reasons.append(f"炸板{zb}次回封")
            reason_str = "；".join(reasons) if reasons else "常规涨停"
            prompt += f"| {s.get('代码','')} | {s.get('名称','')} | {s.get('最新价',0):.2f} | {s.get('涨跌幅',0):+.2f} | {lb} | {seal_time} | {zb} | {s.get('换手率',0):.2f} | {s.get('封板资金',0)/1e8:.2f} | {s.get('涨停统计','--')} | {ind} | {reason_str} |\n"

    # 跌停股
    if ck_data.get("limit_down", True):
        prompt += f"\n## 二、跌停股数据（共{len(limit_down)}只，以下为全部数据）\n"
        prompt += "| 代码 | 名称 | 最新价 | 跌幅% | 连续跌停 | 最后封板 | 开板次数 | 换手率% | 封单资金(万) | 所属行业 |\n"
        prompt += "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        for s in limit_down:
            prompt += f"| {s.get('代码','')} | {s.get('名称','')} | {s.get('最新价',0):.2f} | {s.get('涨跌幅',0):.2f} | {s.get('连续跌停',1)} | {s.get('最后封板时间','')} | {s.get('开板次数',0)} | {s.get('换手率',0):.2f} | {s.get('封单资金',0)/1e4:.0f} | {s.get('所属行业','')} |\n"

    # 炸板股
    if ck_data.get("broken_limit", True):
        prompt += f"\n## 三、炸板股数据（共{len(broken)}只，以下为全部数据）\n"
        prompt += "| 代码 | 名称 | 现价 | 涨幅% | 涨停价 | 首封时间 | 炸板次数 | 振幅% | 换手率% | 所属行业 |\n"
        prompt += "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        for s in broken:
            prompt += f"| {s.get('代码','')} | {s.get('名称','')} | {s.get('最新价',0):.2f} | {s.get('涨跌幅',0):+.2f} | {s.get('涨停价',0):.2f} | {s.get('首次封板时间','')} | {s.get('炸板次数',0)} | {s.get('振幅',0):.2f} | {s.get('换手率',0):.2f} | {s.get('所属行业','')} |\n"

    # 板块资金
    if ck_data.get("sector_flow", True):
        prompt += f"\n## 四、板块资金流向（共{len(sector)}个行业，以下为全部数据）\n"
        prompt += "| 行业 | 涨跌幅% | 净额(亿) | 流入(亿) | 流出(亿) | 领涨股 | 领涨涨幅% |\n"
        prompt += "| --- | --- | --- | --- | --- | --- | --- |\n"
        for s in sector:
            prompt += f"| {s.get('名称','')} | {s.get('今日涨跌幅',0):+.2f} | {s.get('净额',0):+.2f} | {s.get('流入资金',0):.2f} | {s.get('流出资金',0):.2f} | {s.get('领涨股','')} | {s.get('领涨股-涨跌幅',0):+.2f} |\n"

    # 新闻
    if ck_data.get("news", True):
        prompt += f"\n## 五、重要财经新闻（{prev_str}收盘后至{today_str}开盘前，共{len(filtered_news)}条）\n"
        for n in filtered_news[:100]:
            pub = n.get("发布时间", "")
            title = n.get("标题", "")
            summary = n.get("摘要", "")
            prompt += f"- [{pub}] {title}\n"
            if summary:
                prompt += f"  摘要: {summary}\n"

    # 分组概念
    if ck_data.get("concept_groups", True) and concept_groups:
        prompt += "\n## 六、分组概念关注\n"
        for g in concept_groups:
            stocks = g.get("stocks", [])
            if stocks:
                stock_list = "、".join([f"{s.get('name',s.get('code',''))}({s.get('code','')})" for s in stocks])
                prompt += f"- {g.get('name','')}({g.get('nameEn','')})：{stock_list}\n"
        prompt += "\n请重点关注以上分组概念的联动性和个股表现。\n"

    # 想买列表
    watchlist = request.json.get("watchlist", [])
    if ck_data.get("watchlist", True) and watchlist:
        prompt += "\n## 七、想买列表（用户计划买入的股票）\n"
        prompt += "| 代码 | 名称 | 买入理由 | 目标价 | 当前状态 |\n"
        prompt += "| --- | --- | --- | --- | --- |\n"
        all_stocks_map = {}
        for s in limit_up + limit_down + broken:
            all_stocks_map[s.get('代码','')] = s
        for w in watchlist:
            code = w.get('code','')
            name = w.get('name','')
            reason = w.get('reason','--') or '--'
            target = w.get('target','--') or '--'
            live = all_stocks_map.get(code)
            if live:
                status = f"{'🔴涨停' if live in limit_up else '🟢跌停' if live in limit_down else '🟡炸板'} 现价{live.get('最新价',0):.2f}"
            else:
                status = "未在今日涨跌停列表"
            prompt += f"| {code} | {name} | {reason} | {target} | {status} |\n"
        prompt += "\n请重点分析以上想买的股票：明天是否有好的买入时机？什么价位可以介入？风险如何？\n"

    prompt += """
## 八、请分析并给出以下内容

### 1. 市场情绪判断
- 当前市场整体情绪（亢奋/乐观/中性/谨慎/恐慌）
- 涨停跌停比的含义
- 连板梯队分析（最高板是谁？梯队是否健康？）

### 2. 涨停原因分析
- 对涨停股按原因分类：政策驱动/业绩驱动/资金驱动/消息驱动/技术反弹
- 哪些涨停原因有持续性？哪些是一日游？

### 3. 热门板块分析
- 当日最强板块及原因
- 资金主攻方向
- 哪些板块可能延续？哪些可能见顶？

### 4. 龙头股预测（重点）
- 哪些连板股可能晋级成功？（继续涨停）
- 哪些板块可能出现新的龙头？
- **【重要】对每只推荐股票，请详细说明买入逻辑：**
  - 连板逻辑（为什么能连板？市场地位、板块地位）
  - 板块联动（板块内其他股票表现如何？）
  - 资金面（封板资金大小、换手率、成交量）
  - 消息面（是否有利好消息、政策支持）
  - 技术面（K线形态、均线支撑）

### 5. ⭐ 推荐买入的股票（下一个交易日）
请推荐3-5只值得买入的股票，每只必须包含：
| 代码 | 名称 | 买入理由 | 买入逻辑 | 建议买入价 | 目标价 | 止损价 | 仓位建议 |
- 买入理由要具体（不能只说"看好"）
- 买入逻辑要清晰（打板/低吸/半路，什么条件下触发）

### 6. ⭐ 推荐卖出的股票（下一个交易日）
请推荐应卖出/减仓的股票（包括持仓中应该卖出的），每只包含：
| 代码 | 名称 | 卖出理由 | 建议操作 |
- 明确说明是清仓还是减仓
- 说明卖出时机（集合竞价/开盘/盘中）

### 7. ⭐ 回避的股票（下一个交易日）
请列出需要回避的股票和板块：
| 代码/板块 | 回避原因 | 风险等级 |
- 包括可能补跌的连板股、利空消息股、资金出逃的板块等

### 8. 风险提示
- 哪些股票可能开板/补跌？原因是什么？
- 哪些板块需要回避？
- 市场系统性风险评估

### 9. 下个交易日操盘策略
- 重点关注方向
- 建议操作策略（打板/低吸/半路）
- 仓位建议
- 开盘前需要关注的信号

请用简洁专业的语言回答，直接给出结论，不要泛泛而谈。对推荐股票一定要详细说明买入逻辑！"""

    # 5. 插入持仓数据
    if portfolio:
        pf_section = "\n\n## 九、持仓数据分析\n"
        pf_section += "| 代码 | 名称 | 数量 | 成本价 | 当前状态 | 盈亏分析 |\n"
        pf_section += "| --- | --- | --- | --- | --- | --- |\n"
        limit_up_codes = {s['代码']: s for s in limit_up}
        limit_down_codes = {s['代码']: s for s in limit_down}
        broken_codes = {s['代码']: s for s in broken}
        for p in portfolio:
            code = p.get("code", "")
            name = p.get("name", "")
            qty = p.get("qty", "--")
            cost = p.get("cost", "--")
            price = p.get("price", 0)
            change_pct = p.get("changePct", 0)
            if code in limit_up_codes:
                s = limit_up_codes[code]
                status = f"🔴涨停! 现价{s['最新价']:.2f} 涨幅{s['涨跌幅']:.2f}%"
                pnl = f"涨停中，浮盈{change_pct:.1f}%"
            elif code in limit_down_codes:
                s = limit_down_codes[code]
                status = f"🟢跌停 现价{s['最新价']:.2f} 跌幅{s['涨跌幅']:.2f}%"
                pnl = f"跌停中，需评估是否止损"
            elif code in broken_codes:
                s = broken_codes[code]
                status = f"🟡炸板 现价{s['最新价']:.2f}"
                pnl = f"炸板后走势不确定"
            else:
                status = f"现价{price:.2f} 涨跌{change_pct:+.2f}%" if price else "未在涨跌停列表"
                if cost and price:
                    try:
                        pnl_pct = (float(price) - float(cost)) / float(cost) * 100
                        pnl = f"浮盈{pnl_pct:.1f}%" if pnl_pct >= 0 else f"浮亏{pnl_pct:.1f}%"
                    except:
                        pnl = "--"
                else:
                    pnl = "--"
            pf_section += f"| {code} | {name} | {qty} | {cost} | {status} | {pnl} |\n"
        pf_section += "\n请对每只持仓股票给出明确操作建议：\n"
        pf_section += "- 持有/加仓/减仓/清仓，给出具体理由\n"
        pf_section += "- 如果建议卖出，给出卖出时机和价格\n"
        pf_section += "- 如果建议持有，说明持有逻辑和止损位\n"
        prompt += pf_section

    # 6. 插入自定义提示词（权重最高）
    if custom_prompt:
        prompt = f"""{custom_prompt}

=== 以下是市场数据，请基于以上自定义问题，结合以下数据进行分析 ===

{prompt}"""

    # 6. 调用MiMo模型
    api_key = os.environ.get("MIMO_API_KEY", "")
    url = "https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages"
    payload = json.dumps({
        "model": "mimo-v2.5-pro",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            # 保存预测结果到数据库（仅交易日，每天只记录一次）
            pred_id = None
            try:
                # 检查是否是交易日（周一到周五）
                from datetime import datetime as dt2
                pred_date = dt2.strptime(date_str, "%Y%m%d")
                if pred_date.weekday() >= 5:  # 周末不保存
                    pass
                else:
                    conn = get_db()
                    # 检查是否已有该日期的记录
                    existing = conn.execute("SELECT id FROM ai_predictions WHERE data_date=?", (date_str,)).fetchone()
                    if existing:
                        # 更新已有记录
                        conn.execute("UPDATE ai_predictions SET custom_prompt=?, prediction=?, prompt_len=?, created_at=datetime('now','localtime') WHERE data_date=?",
                                   (custom_prompt, text, len(prompt), date_str))
                        pred_id = existing["id"]
                    else:
                        # 插入新记录
                        cur = conn.execute("INSERT INTO ai_predictions (data_date, custom_prompt, prediction, prompt_len) VALUES (?, ?, ?, ?)",
                                     (date_str, custom_prompt, text, len(prompt)))
                        pred_id = cur.lastrowid
                    conn.commit()
                    conn.close()
            except: pass
            return jsonify({
                "prediction": text,
                "pred_id": pred_id,
                "date": date_str,
                "prompt_len": len(prompt),
                "limit_up_count": len(limit_up),
                "limit_down_count": len(limit_down),
                "broken_count": len(broken),
                "sector_count": len(sector),
                "news_count": len(filtered_news)
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai-history")
def api_ai_history():
    """获取AI预测历史列表（含反馈统计）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT p.id, p.data_date, p.custom_prompt, p.prompt_len, p.created_at,
               COALESCE(SUM(f.score), 0) as total_score,
               COUNT(f.id) as feedback_count
        FROM ai_predictions p
        LEFT JOIN ai_feedback f ON f.prediction_id = p.id
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/ai-history/<int:pred_id>")
def api_ai_history_detail(pred_id):
    """获取单条AI预测详情"""
    conn = get_db()
    row = conn.execute("SELECT * FROM ai_predictions WHERE id=?", (pred_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))

@app.route("/api/ai-feedback", methods=["POST"])
def api_ai_feedback():
    """提交AI预测反馈（加权标签 + 星级）"""
    data = request.json
    pred_id = data.get("prediction_id")
    tags = data.get("tags", [])
    note = data.get("note", "")
    stars = data.get("stars", 0)
    if not pred_id:
        return jsonify({"error": "missing prediction_id"}), 400
    # 加权评分
    weights = {"👍": 1, "🎯": 3, "💡": 1, "🔥": 3, "⚠️": -2, "❌": -3}
    score = sum(weights.get(t, 0) for t in tags)
    # 星级也纳入总分 (1-5星 → 0-5分)
    if stars:
        score += stars
    conn = get_db()
    conn.execute("INSERT INTO ai_feedback (prediction_id, tags, note, score) VALUES (?, ?, ?, ?)",
                 (pred_id, json.dumps({"tags": tags, "stars": stars}, ensure_ascii=False), note, score))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "score": score})

@app.route("/api/ai-feedback/<int:pred_id>")
def api_ai_feedback_get(pred_id):
    """获取某条预测的反馈"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ai_feedback WHERE prediction_id=? ORDER BY id DESC", (pred_id,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            parsed = json.loads(d.get("tags", "{}"))
            if isinstance(parsed, list):
                d["tags"] = parsed
                d["stars"] = 0
            else:
                d["tags"] = parsed.get("tags", [])
                d["stars"] = parsed.get("stars", 0)
        except:
            d["tags"] = []
            d["stars"] = 0
        result.append(d)
    total_score = sum(r.get("score", 0) for r in result)
    tag_counts = {}
    star_list = []
    for r in result:
        for t in r["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        if r.get("stars"):
            star_list.append(r["stars"])
    avg_stars = sum(star_list) / len(star_list) if star_list else 0
    return jsonify({"feedbacks": result, "total_score": total_score, "tag_counts": tag_counts, "avg_stars": avg_stars, "count": len(result)})

@app.route("/api/ai-stats")
def api_ai_stats():
    """AI预测统计"""
    conn = get_db()
    rows = conn.execute("""
        SELECT p.id, p.data_date, p.created_at,
               COALESCE(SUM(f.score), 0) as total_score,
               COUNT(f.id) as feedback_count
        FROM ai_predictions p
        LEFT JOIN ai_feedback f ON f.prediction_id = p.id
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    total = len(rows)
    positive = sum(1 for r in rows if r["total_score"] > 0)
    negative = sum(1 for r in rows if r["total_score"] < 0)
    neutral = total - positive - negative
    accuracy = round(positive / total * 100, 1) if total else 0
    return jsonify({
        "total": total, "positive": positive, "negative": negative,
        "neutral": neutral, "accuracy": accuracy,
        "recent": [dict(r) for r in rows[:10]]
    })

@app.route("/api/default-concepts")
def api_default_concepts():
    """返回预设的概念分组和个股"""
    default_groups = [
        {
            "name": "PCB", "nameEn": "PCB",
            "stocks": [
                {"code": "002916", "name": "深南电路"},
                {"code": "603228", "name": "景旺电子"},
                {"code": "002036", "name": "联创电子"},
                {"code": "300831", "name": "派瑞股份"},
                {"code": "002436", "name": "兴森科技"},
                {"code": "300408", "name": "三环集团"},
                {"code": "002384", "name": "东山精密"},
                {"code": "603005", "name": "晶方科技"},
                {"code": "002913", "name": "奥士康"},
                {"code": "300476", "name": "胜宏科技"},
                {"code": "300852", "name": "四会富仕"},
                {"code": "002025", "name": "航天电器"},
                {"code": "600183", "name": "生益科技"},
                {"code": "300115", "name": "长盈精密"},
                {"code": "002185", "name": "华天科技"},
            ]
        },
        {
            "name": "CPO", "nameEn": "CPO",
            "stocks": [
                {"code": "300308", "name": "中际旭创"},
                {"code": "300394", "name": "天孚通信"},
                {"code": "002902", "name": "铭普光磁"},
                {"code": "300502", "name": "新易盛"},
                {"code": "002281", "name": "光迅科技"},
                {"code": "300562", "name": "乐心医疗"},
                {"code": "603083", "name": "剑桥科技"},
                {"code": "002396", "name": "星网锐捷"},
                {"code": "300548", "name": "博创科技"},
                {"code": "300620", "name": "光库科技"},
                {"code": "002952", "name": "新亚电子"},
                {"code": "688308", "name": "欧光科技"},
            ]
        },
        {
            "name": "光模块", "nameEn": "光模块",
            "stocks": [
                {"code": "300308", "name": "中际旭创"},
                {"code": "300502", "name": "新易盛"},
                {"code": "002281", "name": "光迅科技"},
                {"code": "300394", "name": "天孚通信"},
                {"code": "603083", "name": "剑桥科技"},
                {"code": "300548", "name": "博创科技"},
                {"code": "300620", "name": "光库科技"},
                {"code": "002902", "name": "铭普光磁"},
                {"code": "300562", "name": "乐心医疗"},
                {"code": "688308", "name": "欧光科技"},
                {"code": "300565", "name": "科信技术"},
                {"code": "002491", "name": "通鼎互联"},
            ]
        },
        {
            "name": "MLCC", "nameEn": "MLCC",
            "stocks": [
                {"code": "000636", "name": "风华高科"},
                {"code": "002199", "name": "东晶电子"},
                {"code": "603267", "name": "鸿远电子"},
                {"code": "300408", "name": "三环集团"},
                {"code": "002484", "name": "江海股份"},
                {"code": "603678", "name": "火炬电子"},
                {"code": "300115", "name": "长盈精密"},
                {"code": "002859", "name": "洁美科技"},
                {"code": "688599", "name": "天利科技"},
                {"code": "300582", "name": "英飞特"},
            ]
        },
        {
            "name": "玻璃基板", "nameEn": "Glass Substrate",
            "stocks": [
                {"code": "000012", "name": "南玻A"},
                {"code": "600819", "name": "耀皮玻璃"},
                {"code": "002163", "name": "海南发展"},
                {"code": "300346", "name": "南大光电"},
                {"code": "002613", "name": "北玻股份"},
                {"code": "600660", "name": "福耀玻璃"},
                {"code": "002450", "name": "康得新"},
                {"code": "601689", "name": "拓普集团"},
                {"code": "002271", "name": "东方雨虹"},
                {"code": "300398", "name": "飞凯材料"},
            ]
        },
        {
            "name": "存储芯片", "nameEn": "Storage Chip",
            "stocks": [
                {"code": "002185", "name": "华天科技"},
                {"code": "603986", "name": "兆易创新"},
                {"code": "300672", "name": "国科微"},
                {"code": "300223", "name": "北京君正"},
                {"code": "688256", "name": "寒武纪"},
                {"code": "300458", "name": "全志科技"},
                {"code": "688521", "name": "芯原股份"},
                {"code": "688008", "name": "澜起科技"},
                {"code": "002049", "name": "紫光国微"},
                {"code": "603501", "name": "韦尔股份"},
                {"code": "300782", "name": "卓胜微"},
                {"code": "688041", "name": "海光信息"},
            ]
        },
        {
            "name": "机器人", "nameEn": "Robot",
            "stocks": [
                {"code": "002527", "name": "新时达"},
                {"code": "002472", "name": "双环传动"},
                {"code": "300124", "name": "汇川技术"},
                {"code": "603728", "name": "鸣志电器"},
                {"code": "002747", "name": "埃斯顿"},
                {"code": "300367", "name": "东方网力"},
                {"code": "688022", "name": "瀚川智能"},
                {"code": "002553", "name": "南方轴承"},
                {"code": "603662", "name": "柯力传感"},
                {"code": "002896", "name": "中大力德"},
                {"code": "603416", "name": "信捷电气"},
                {"code": "688169", "name": "石头科技"},
            ]
        },
        {
            "name": "AI算力", "nameEn": "AI Computing",
            "stocks": [
                {"code": "688041", "name": "海光信息"},
                {"code": "688256", "name": "寒武纪"},
                {"code": "688521", "name": "芯原股份"},
                {"code": "002049", "name": "紫光国微"},
                {"code": "603986", "name": "兆易创新"},
                {"code": "300496", "name": "中科创达"},
                {"code": "002415", "name": "海康威视"},
                {"code": "300454", "name": "深信服"},
                {"code": "002405", "name": "四维图新"},
                {"code": "300474", "name": "景嘉微"},
                {"code": "300367", "name": "东方网力"},
                {"code": "300253", "name": "卫宁健康"},
            ]
        },
        {
            "name": "低空经济", "nameEn": "Low Altitude Economy",
            "stocks": [
                {"code": "002097", "name": "山河智能"},
                {"code": "002342", "name": "巨力索具"},
                {"code": "300450", "name": "先导智能"},
                {"code": "002013", "name": "中航机电"},
                {"code": "600316", "name": "洪都航空"},
                {"code": "002013", "name": "中航机电"},
                {"code": "600372", "name": "中航电子"},
                {"code": "002046", "name": "中航重机"},
                {"code": "600760", "name": "中航沈飞"},
                {"code": "600893", "name": "航发动力"},
                {"code": "002025", "name": "航天电器"},
                {"code": "002179", "name": "中航光电"},
            ]
        },
        {
            "name": "固态电池", "nameEn": "Solid State Battery",
            "stocks": [
                {"code": "300750", "name": "宁德时代"},
                {"code": "002594", "name": "比亚迪"},
                {"code": "300014", "name": "亿纬锂能"},
                {"code": "002074", "name": "国轩高科"},
                {"code": "300390", "name": "天华新能"},
                {"code": "002466", "name": "天齐锂业"},
                {"code": "002460", "name": "赣锋锂业"},
                {"code": "300450", "name": "先导智能"},
                {"code": "002108", "name": "沧州明珠"},
                {"code": "300340", "name": "科恒股份"},
                {"code": "300398", "name": "飞凯材料"},
                {"code": "002245", "name": "蔚蓝锂芯"},
            ]
        },
        {
            "name": "光纤光缆", "nameEn": "Fiber Optic Cable",
            "stocks": [
                {"code": "600487", "name": "亨通光电"},
                {"code": "002491", "name": "通鼎互联"},
                {"code": "600105", "name": "永鼎股份"},
                {"code": "002281", "name": "光迅科技"},
                {"code": "300565", "name": "科信技术"},
                {"code": "002396", "name": "星网锐捷"},
                {"code": "600183", "name": "生益科技"},
                {"code": "300408", "name": "三环集团"},
                {"code": "002194", "name": "武汉凡谷"},
                {"code": "002952", "name": "新亚电子"},
            ]
        },
        {
            "name": "芯片封测", "nameEn": "Chip Packaging",
            "stocks": [
                {"code": "002185", "name": "华天科技"},
                {"code": "603005", "name": "晶方科技"},
                {"code": "002916", "name": "深南电路"},
                {"code": "600183", "name": "生益科技"},
                {"code": "300115", "name": "长盈精密"},
                {"code": "002036", "name": "联创电子"},
                {"code": "300408", "name": "三环集团"},
                {"code": "002436", "name": "兴森科技"},
                {"code": "300831", "name": "派瑞股份"},
                {"code": "002913", "name": "奥士康"},
            ]
        },
        {
            "name": "消费电子", "nameEn": "Consumer Electronics",
            "stocks": [
                {"code": "002241", "name": "歌尔股份"},
                {"code": "002475", "name": "立讯精密"},
                {"code": "002600", "name": "领益智造"},
                {"code": "300115", "name": "长盈精密"},
                {"code": "002036", "name": "联创电子"},
                {"code": "002273", "name": "水晶光电"},
                {"code": "002241", "name": "歌尔股份"},
                {"code": "300433", "name": "蓝思科技"},
                {"code": "002681", "name": "奋达科技"},
                {"code": "002993", "name": "奥海科技"},
                {"code": "300736", "name": "百邦科技"},
                {"code": "002866", "name": "传艺科技"},
            ]
        },
        {
            "name": "新能源车", "nameEn": "NEV",
            "stocks": [
                {"code": "002594", "name": "比亚迪"},
                {"code": "300750", "name": "宁德时代"},
                {"code": "300014", "name": "亿纬锂能"},
                {"code": "002460", "name": "赣锋锂业"},
                {"code": "002466", "name": "天齐锂业"},
                {"code": "002074", "name": "国轩高科"},
                {"code": "601689", "name": "拓普集团"},
                {"code": "002906", "name": "华阳集团"},
                {"code": "603799", "name": "华友钴业"},
                {"code": "002407", "name": "多氟多"},
                {"code": "300390", "name": "天华新能"},
                {"code": "002245", "name": "蔚蓝锂芯"},
            ]
        },
        {
            "name": "半导体设备", "nameEn": "Semiconductor Equipment",
            "stocks": [
                {"code": "300236", "name": "上海新阳"},
                {"code": "300346", "name": "南大光电"},
                {"code": "002371", "name": "北方华创"},
                {"code": "300450", "name": "先导智能"},
                {"code": "688012", "name": "中微公司"},
                {"code": "300398", "name": "飞凯材料"},
                {"code": "300604", "name": "长城科技"},
                {"code": "002158", "name": "汉钟精机"},
                {"code": "688037", "name": "芯源微"},
                {"code": "688082", "name": "盛美上海"},
            ]
        },
    ]
    return jsonify(default_groups)

# 概念数据缓存
_concepts_cache = {}

@app.route("/api/stock-concepts", methods=["POST"])
def api_stock_concepts():
    """批量获取股票所属概念（东方财富F10）"""
    codes = request.json.get("codes", [])
    if not codes:
        return jsonify({"error": "missing codes"}), 400
    
    import urllib.request
    result = {}
    uncached = [c for c in codes[:200] if c not in _concepts_cache]
    
    for code in uncached:
        try:
            url = f"https://datacenter.eastmoney.com/securities/api/data/get?type=RPT_F10_CORETHEME_BOARDTYPE&sty=BOARD_NAME&filter=(SECURITY_CODE=%22{code}%22)"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))
            concepts = []
            if data.get("result") and data["result"].get("data"):
                concepts = [item["BOARD_NAME"] for item in data["result"]["data"]]
            _concepts_cache[code] = concepts
        except:
            _concepts_cache[code] = []
    
    for code in codes[:200]:
        result[code] = _concepts_cache.get(code, [])
    
    return jsonify(result)

@app.route("/api/stock-quotes", methods=["POST"])
def api_stock_quotes():
    """获取指定股票列表的行情数据（使用腾讯API实时获取）"""
    codes = request.json.get("codes", [])
    if not codes:
        return jsonify({"error": "missing codes"}), 400
    
    # 限制一次最多查询50只股票
    codes = codes[:50]
    
    result = {}
    
    # 使用腾讯API查询所有股票（实时数据更准确）
    try:
        import urllib.request
        symbols = []
        for code in codes:
            if code.startswith("6"):
                symbols.append("sh" + code)
            elif code.startswith("0") or code.startswith("3"):
                symbols.append("sz" + code)
            elif code.startswith("4") or code.startswith("8"):
                symbols.append("bj" + code)
            else:
                symbols.append("sz" + code)
        
        url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode("gb2312")
        
        for line in raw.strip().split(";"):
            if not line.strip():
                continue
            try:
                parts = line.split("~")
                if len(parts) < 45:
                    continue
                code = parts[2]
                result[code] = {
                    "代码": code,
                    "名称": parts[1],
                    "最新价": float(parts[3]) if parts[3] else 0,
                    "涨跌幅": float(parts[32]) if parts[32] else 0,
                    "涨跌额": float(parts[31]) if parts[31] else 0,
                    "成交额": float(parts[37]) * 10000 if parts[37] else 0,  # 万元转元
                    "换手率": float(parts[38]) if parts[38] else 0,
                    "振幅": float(parts[43]) if parts[43] else 0,
                    "最高": float(parts[33]) if parts[33] else 0,
                    "最低": float(parts[34]) if parts[34] else 0,
                    "市盈率-动态": float(parts[39]) if parts[39] else 0,
                    "流通市值": float(parts[44]) if parts[44] else 0,
                }
            except:
                continue
    except Exception as e:
        print(f"腾讯API错误: {e}")
    
    # 对于没有获取到数据的股票，从数据库缓存补充
    missing = [c for c in codes if c not in result]
    if missing:
        conn = get_db()
        row = conn.execute("SELECT market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            market_data = json.loads(row["market_data"])
            for stock in market_data.get("limit_up", []) + market_data.get("limit_down", []) + market_data.get("broken_limit", []):
                code = stock.get("代码", "")
                if code in missing:
                    result[code] = {
                        "代码": code,
                        "名称": stock.get("名称", ""),
                        "最新价": stock.get("最新价", 0),
                        "涨跌幅": stock.get("涨跌幅", 0),
                        "成交额": stock.get("成交额", 0),
                        "换手率": stock.get("换手率", 0),
                        "振幅": stock.get("振幅", 0),
                        "cached": True
                    }
    
    return jsonify(result)

@app.route("/api/stock-detail/<code>")
def api_stock_detail(code):
    """获取股票详细信息：基本面、涨跌停原因、所属概念"""
    # 先尝试从缓存获取数据
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
    conn.close()
    
    cached_data = {}
    if row:
        market_data = json.loads(row['market_data'])
        # 从涨停、跌停、炸板数据中查找该股票
        for s in market_data.get('limit_up', []):
            if s.get('代码') == code:
                cached_data = {
                    "code": code,
                    "基本信息": {
                        "名称": s.get('名称', ''),
                        "最新价": s.get('最新价', 0),
                        "涨跌幅": s.get('涨跌幅', 0),
                        "成交额": s.get('成交额', 0),
                        "换手率": s.get('换手率', 0),
                    },
                    "涨停信息": {
                        "连板数": s.get('连板数', 1),
                        "涨停统计": s.get('涨停统计', ''),
                        "所属行业": s.get('所属行业', ''),
                        "首次封板时间": s.get('首次封板时间', ''),
                        "最后封板时间": s.get('最后封板时间', ''),
                        "炸板次数": s.get('炸板次数', 0),
                        "封板资金": s.get('封板资金', 0),
                    },
                    "所属概念": [],
                    "来源": "缓存"
                }
                break
        if not cached_data:
            for s in market_data.get('limit_down', []):
                if s.get('代码') == code:
                    cached_data = {
                        "code": code,
                        "基本信息": {
                            "名称": s.get('名称', ''),
                            "最新价": s.get('最新价', 0),
                            "涨跌幅": s.get('涨跌幅', 0),
                            "成交额": s.get('成交额', 0),
                            "换手率": s.get('换手率', 0),
                        },
                        "所属概念": [],
                        "来源": "缓存"
                    }
                    break
    
    # 尝试从 akshare 获取实时数据
    script = f'''
import akshare as ak
import json

result = {{"code": "{code}"}}

# 1. 获取股票基本信息
try:
    df = ak.stock_zh_a_spot_em()
    row = df[df["代码"] == "{code}"]
    if not row.empty:
        r = row.iloc[0]
        result["基本信息"] = {{
            "名称": str(r.get("名称", "")),
            "最新价": float(r.get("最新价", 0)) if r.get("最新价") else 0,
            "涨跌幅": float(r.get("涨跌幅", 0)) if r.get("涨跌幅") else 0,
            "成交额": float(r.get("成交额", 0)) if r.get("成交额") else 0,
            "换手率": float(r.get("换手率", 0)) if r.get("换手率") else 0,
            "市盈率": float(r.get("市盈率-动态", 0)) if r.get("市盈率-动态") else 0,
            "流通市值": float(r.get("流通市值", 0)) if r.get("流通市值") else 0,
        }}
except Exception as e:
    result["基本信息错误"] = str(e)

# 2. 获取涨跌停信息
try:
    df = ak.stock_zt_pool_em(date="{datetime.now().strftime('%Y%m%d')}")
    row = df[df["代码"] == "{code}"]
    if not row.empty:
        r = row.iloc[0]
        result["涨停信息"] = {{
            "连板数": int(r.get("连板数", 1)),
            "涨停统计": str(r.get("涨停统计", "")),
            "所属行业": str(r.get("所属行业", "")),
            "首次封板时间": str(r.get("首次封板时间", "")),
            "最后封板时间": str(r.get("最后封板时间", "")),
            "炸板次数": int(r.get("炸板次数", 0)),
            "封板资金": float(r.get("封板资金", 0)),
        }}
except Exception as e:
    pass

# 3. 获取个股信息（行业等）
try:
    df = ak.stock_individual_info_em(symbol="{code}")
    concepts = []
    industry = ""
    for _, row in df.iterrows():
        item = str(row.get("item", ""))
        value = str(row.get("value", ""))
        if "行业" in item or "industry" in item.lower():
            industry = value
        if "概念" in item or "concept" in item.lower():
            for c in value.replace("、", ",").replace("/", ",").split(","):
                c = c.strip()
                if c:
                    concepts.append(c)
    if industry and industry not in concepts:
        concepts.insert(0, industry)
    result["所属概念"] = concepts[:10]
    if industry:
        result["行业"] = industry
except Exception as e:
    result["所属概念"] = []

print(json.dumps(result, ensure_ascii=False))
'''
    try:
        data = fetch_akshare(script, timeout=15)
        if data and not data.get('基本信息错误'):
            return jsonify(data)
    except:
        pass
    
    # 如果 akshare 失败，返回缓存数据
    if cached_data:
        return jsonify(cached_data)
    
    # 如果都没有，返回基本信息
    return jsonify({"code": code, "基本信息": {"名称": code}, "所属概念": [], "来源": "无数据"})

@app.route("/api/stock-kline/<code>")
def api_stock_kline(code):
    """获取股票K线数据（腾讯API + akshare fallback）"""
    import requests as req
    period = request.args.get('period', 'daily')
    days = int(request.args.get('days', 120))

    # 腾讯K线API
    period_map = {'daily': 'day', 'weekly': 'week', 'monthly': 'month'}
    pt = period_map.get(period, 'day')
    prefix = 'sh' if code.startswith('6') else 'sz'

    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = f"param={prefix}{code},{pt},,,{days},qfq"
        r = req.get(f"{url}?{params}", timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        d = r.json()
        stock = d.get('data', {}).get(f'{prefix}{code}', {})
        klines = stock.get(f'qfq{pt}', stock.get(pt, []))
        if klines:
            records = []
            for k in klines:
                if len(k) >= 6:
                    records.append({
                        "日期": k[0], "开盘": float(k[1]), "收盘": float(k[2]),
                        "最高": float(k[3]), "最低": float(k[4]),
                        "成交量": float(k[5]), "成交额": 0, "涨跌幅": 0
                    })
            # 计算涨跌幅
            for i in range(len(records)):
                if i == 0:
                    records[i]["涨跌幅"] = 0
                else:
                    prev = records[i-1]["收盘"]
                    if prev > 0:
                        records[i]["涨跌幅"] = round((records[i]["收盘"] - prev) / prev * 100, 2)
            return jsonify(records)
    except:
        pass

    # Fallback: akshare
    script = f'''
import akshare as ak
import json, time
try:
    from datetime import datetime, timedelta
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days={days})).strftime('%Y%m%d')
    df = None
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_hist(symbol="{code}", period="{period}", start_date=start_date, end_date=end_date, adjust="qfq")
            break
        except: time.sleep(1)
    if df is not None and not df.empty:
        records = []
        for _, r in df.iterrows():
            records.append({{"日期":str(r.get("日期","")),"开盘":float(r.get("开盘",0)),"收盘":float(r.get("收盘",0)),"最高":float(r.get("最高",0)),"最低":float(r.get("最低",0)),"成交量":float(r.get("成交量",0)),"成交额":float(r.get("成交额",0)),"涨跌幅":float(r.get("涨跌幅",0))}})
        print(json.dumps(records, ensure_ascii=False))
    else: print(json.dumps({{"error":"no data"}}))
except Exception as e: print(json.dumps({{"error":str(e)}}))
'''
    data = fetch_akshare(script, timeout=30)
    if data and isinstance(data, list):
        return jsonify(data)
    return jsonify(data or {"error": "fetch failed"})

@app.route("/api/stock-intraday/<code>")
def api_stock_intraday(code):
    """获取股票分时数据（腾讯API + akshare fallback）"""
    import requests as req
    prefix = 'sh' if code.startswith('6') else 'sz'

    # 腾讯分时API
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={prefix}{code}"
        r = req.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        d = r.json()
        data = d.get('data', {}).get(f'{prefix}{code}', {}).get('data', {}).get('data', [])
        yestclose = d.get('data', {}).get(f'{prefix}{code}', {}).get('qt', {}).get(f'{prefix}{code}', [0]*5)
        prev_close = float(yestclose[3]) if len(yestclose) > 3 else 0

        if data:
            records = []
            for line in data:
                parts = line.split(' ')
                if len(parts) >= 2:
                    time_str = parts[0]
                    if len(time_str) == 4:
                        time_str = f"{time_str[:2]}:{time_str[2:]}"
                    price = float(parts[1])
                    vol = float(parts[2]) if len(parts) > 2 else 0
                    records.append({
                        "时间": time_str, "开盘": price, "收盘": price,
                        "最高": price, "最低": price, "成交量": vol
                    })
            return jsonify(records)
    except:
        pass

    # Fallback: akshare
    script = f'''
import akshare as ak
import json, time
try:
    df = None
    for attempt in range(2):
        try:
            df = ak.stock_zh_a_minute(symbol="{code}", period="1", adjust="")
            break
        except: time.sleep(1)
    if df is not None and not df.empty:
        records = []
        for _, r in df.tail(240).iterrows():
            records.append({{"时间":str(r.get("时间","")),"开盘":float(r.get("开盘",0)),"收盘":float(r.get("收盘",0)),"最高":float(r.get("最高",0)),"最低":float(r.get("最低",0)),"成交量":float(r.get("成交量",0))}})
        print(json.dumps(records, ensure_ascii=False))
    else: print(json.dumps({{"error":"no data"}}))
except Exception as e: print(json.dumps({{"error":str(e)}}))
'''
    data = fetch_akshare(script, timeout=30)
    if data and isinstance(data, list):
        return jsonify(data)
    return jsonify(data or {"error": "fetch failed"})

@app.route("/api/stock-quote/<code>")
def api_stock_quote(code):
    """获取个股实时行情（腾讯API + 东方财富fallback）"""
    import requests as req

    # 腾讯股票API
    try:
        prefix = 'sh' if code.startswith('6') else 'sz'
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        r = req.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        text = r.content.decode('gbk', errors='ignore')
        parts = text.split('~')
        if len(parts) > 45:
            def safe_float(v, div=1):
                try: return float(v) / div
                except: return 0
            return jsonify({
                "代码": code,
                "名称": parts[1].strip(),
                "最新价": safe_float(parts[3]),
                "昨收": safe_float(parts[4]),
                "今开": safe_float(parts[5]),
                "成交量": safe_float(parts[6]),
                "涨跌额": safe_float(parts[31]),
                "涨跌幅": safe_float(parts[32]),
                "最高": safe_float(parts[33]),
                "最低": safe_float(parts[34]),
                "成交额": safe_float(parts[37]),
                "换手率": safe_float(parts[38]),
                "量比": safe_float(parts[49]) if len(parts) > 49 else 0,
                "市盈率": safe_float(parts[39]),
                "市净率": safe_float(parts[46]) if len(parts) > 46 else 0,
                "总市值": safe_float(parts[45]) * 1e4 if len(parts) > 45 else 0,
                "流通市值": safe_float(parts[44]) * 1e4 if len(parts) > 44 else 0,
                "振幅": safe_float(parts[43]) if len(parts) > 43 else 0,
            })
    except:
        pass

    # Fallback: 东方财富
    try:
        secid = f"1.{code}" if code.startswith('6') else f"0.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {'secid': secid, 'fields': 'f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f116,f117,f162,f167,f168,f169,f170,f171'}
        r = req.get(url, params=params, timeout=8, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com'})
        d = r.json().get('data', {})
        if d:
            return jsonify({
                "代码": code, "名称": d.get("f58", ""),
                "最新价": d.get("f43", 0) / 100, "涨跌幅": d.get("f170", 0) / 100,
                "涨跌额": d.get("f169", 0) / 100, "今开": d.get("f46", 0) / 100,
                "最高": d.get("f44", 0) / 100, "最低": d.get("f45", 0) / 100,
                "昨收": d.get("f60", 0) / 100, "成交量": d.get("f47", 0),
                "成交额": d.get("f48", 0), "换手率": d.get("f168", 0) / 100,
                "量比": d.get("f50", 0) / 100, "市盈率": d.get("f167", 0) / 100,
                "市净率": d.get("f162", 0) / 1000, "总市值": d.get("f116", 0),
                "流通市值": d.get("f117", 0), "振幅": d.get("f171", 0) / 100,
            })
    except:
        pass
    return jsonify({"error": "fetch failed"})

@app.route("/api/fund-recommend")
def api_fund_recommend():
    """基金推荐（基于当前热门板块推荐相关基金）"""
    import requests as req

    # 从数据库获取当前热门板块
    hot_sectors = []
    try:
        conn = get_db()
        row = conn.execute("SELECT market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            mdata = json.loads(row["market_data"])
            limit_up = mdata.get("limit_up", [])
            # 统计行业出现频率
            from collections import Counter
            sector_count = Counter()
            for s in limit_up:
                ind = s.get("所属行业", "")
                if ind:
                    sector_count[ind] += 1
            hot_sectors = [s[0] for s in sector_count.most_common(10)]
    except:
        pass

    # 基金数据库（科技主题为主）
    fund_db = {
        "半导体": [
            {"code":"512480","name":"半导体ETF","type":"ETF","m1":5.2,"m3":12.8,"y1":28.5,"score":92,"reason":"国产替代加速，AI芯片需求爆发"},
            {"code":"159995","name":"芯片ETF","type":"ETF","m1":4.8,"m3":11.2,"y1":25.3,"score":88,"reason":"半导体产业链全面受益"},
            {"code":"012556","name":"华夏芯片混合","type":"混合","m1":3.9,"m3":9.8,"y1":22.1,"score":82,"reason":"精选芯片设计龙头"},
        ],
        "人工智能": [
            {"code":"515070","name":"人工智能ETF","type":"ETF","m1":6.1,"m3":15.3,"y1":35.2,"score":95,"reason":"AI大模型持续迭代，算力需求旺盛"},
            {"code":"159819","name":"AI ETF","type":"ETF","m1":5.5,"m3":13.8,"y1":30.8,"score":90,"reason":"覆盖AI全产业链"},
            {"code":"012836","name":"易方达AI混合","type":"混合","m1":4.8,"m3":12.1,"y1":27.5,"score":85,"reason":"深度挖掘AI应用标的"},
        ],
        "新能源": [
            {"code":"516160","name":"新能源ETF","type":"ETF","m1":2.1,"m3":5.8,"y1":8.2,"score":65,"reason":"碳中和政策持续推进"},
            {"code":"515790","name":"光伏ETF","type":"ETF","m1":1.8,"m3":4.2,"y1":6.5,"score":58,"reason":"光伏装机量稳步增长"},
            {"code":"159755","name":"碳中和ETF","type":"ETF","m1":1.5,"m3":3.8,"y1":5.8,"score":55,"reason":"长期赛道，短期承压"},
        ],
        "有色金属": [
            {"code":"512400","name":"有色金属ETF","type":"ETF","m1":3.8,"m3":8.5,"y1":18.2,"score":78,"reason":"资源品涨价周期"},
            {"code":"159980","name":"有色50ETF","type":"ETF","m1":3.2,"m3":7.5,"y1":15.8,"score":72,"reason":"稀有金属战略价值提升"},
        ],
        "军工": [
            {"code":"512660","name":"军工ETF","type":"ETF","m1":2.5,"m3":6.2,"y1":12.5,"score":70,"reason":"国防预算稳步增长"},
            {"code":"512680","name":"军工龙头ETF","type":"ETF","m1":2.2,"m3":5.8,"y1":11.2,"score":68,"reason":"军工订单加速释放"},
        ],
        "医药": [
            {"code":"512010","name":"医药ETF","type":"ETF","m1":1.2,"m3":3.5,"y1":5.8,"score":52,"reason":"创新药出海逻辑"},
            {"code":"159992","name":"创新药ETF","type":"ETF","m1":1.5,"m3":4.2,"y1":7.2,"score":58,"reason":"创新药审批加速"},
        ],
        "消费": [
            {"code":"159928","name":"消费ETF","type":"ETF","m1":0.8,"m3":2.5,"y1":4.2,"score":45,"reason":"消费复苏预期"},
            {"code":"515650","name":"消费50ETF","type":"ETF","m1":0.5,"m3":2.0,"y1":3.5,"score":42,"reason":"必选消费稳健"},
        ],
        "科技综合": [
            {"code":"515030","name":"新能源车ETF","type":"ETF","m1":3.5,"m3":8.2,"y1":15.5,"score":75,"reason":"智能化+电动化双轮驱动"},
            {"code":"515050","name":"5GETF","type":"ETF","m1":2.8,"m3":6.5,"y1":12.8,"score":70,"reason":"通信基建持续投入"},
            {"code":"159740","name":"恒生科技ETF","type":"QDII","m1":4.2,"m3":10.5,"y1":20.2,"score":80,"reason":"港股科技估值修复"},
        ],
    }

    # 构建返回数据
    sectors = []
    for sector_name, funds in fund_db.items():
        # 根据热门板块调整评分
        is_hot = any(hs in sector_name or sector_name in hs for hs in hot_sectors)
        adjusted_funds = []
        for f in funds:
            ff = dict(f)
            if is_hot:
                ff["score"] = min(100, ff["score"] + 5)
                ff["reason"] = "🔥热门板块 | " + ff["reason"]
            adjusted_funds.append(ff)
        icon_map = {"半导体":"🔬","人工智能":"🤖","新能源":"⚡","有色金属":"⛏️","军工":"🎖️","医药":"💊","消费":"🛒","科技综合":"💻"}
        sectors.append({"name": sector_name, "icon": icon_map.get(sector_name, "📁"), "funds": adjusted_funds})

    # 推荐TOP10（按评分排序）
    all_funds = []
    for sec in sectors:
        for f in sec["funds"]:
            all_funds.append({**f, "sector": sec["name"]})
    all_funds.sort(key=lambda x: -x.get("score", 0))
    recommended = all_funds[:10]

    return jsonify({
        "market_view": "当前市场以科技板块为主线，AI/半导体/机器人持续活跃，慢牛格局延续",
        "advice": "建议70%配置科技主题基金（半导体+AI），20%配置周期板块（有色），10%配置防御性品种（医药/消费）",
        "sectors": sectors,
        "recommended": recommended,
        "hot_sectors": hot_sectors[:5]
    })

@app.route("/api/hot-stocks")
def api_hot_stocks():
    """获取热门股票（涨停、跌停、炸板、资金流入前10）"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "no data"})
    
    data = json.loads(row["market_data"])
    
    # 涨停热门（按连板数排序）
    limit_up = data.get("limit_up", [])
    hot_zt = sorted(limit_up, key=lambda x: -(x.get("连板数", 1) or 1))[:10]
    
    # 跌停热门
    limit_down = data.get("limit_down", [])
    hot_dt = sorted(limit_down, key=lambda x: -(x.get("连续跌停", 1) or 1))[:10]
    
    # 炸板热门
    broken = data.get("broken_limit", [])
    hot_zb = sorted(broken, key=lambda x: -(x.get("成交额", 0) or 0))[:10]
    
    # 资金流入板块
    sector = data.get("sector_flow", [])
    hot_sector = sorted(sector, key=lambda x: -(x.get("净额", 0) or 0))[:10]
    
    return jsonify({
        "涨停热门": hot_zt,
        "跌停热门": hot_dt,
        "炸板热门": hot_zb,
        "资金流入板块": hot_sector,
        "统计": {
            "涨停数": data.get("limit_up_count", 0),
            "跌停数": data.get("limit_down_count", 0),
            "炸板数": data.get("broken_limit_count", 0),
        }
    })

@app.route("/api/sector-catalyst/<sector_name>")
def api_sector_catalyst(sector_name):
    """分析板块催化因素"""
    script = f'''
import akshare as ak
import json

result = {{"板块": "{sector_name}"}}

# 1. 获取板块成分股
try:
    df = ak.stock_board_industry_cons_em(symbol="{sector_name}")
    stocks = []
    for _, r in df.head(10).iterrows():
        stocks.append({{
            "代码": str(r.get("代码", "")),
            "名称": str(r.get("名称", "")),
            "涨跌幅": float(r.get("涨跌幅", 0)) if r.get("涨跌幅") else 0,
        }})
    result["成分股"] = stocks
except Exception as e:
    result["成分股错误"] = str(e)

# 2. 获取相关新闻（用于分析催化）
try:
    df = ak.stock_info_global_em()
    news = []
    for _, r in df.head(50).iterrows():
        title = str(r.get("标题", ""))
        if "{sector_name}" in title or any(s["名称"] in title for s in result.get("成分股", [])):
            news.append({{
                "标题": title,
                "时间": str(r.get("发布时间", "")),
            }})
    result["相关新闻"] = news[:10]
except Exception as e:
    result["新闻错误"] = str(e)

print(json.dumps(result, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=30)
    if data:
        return jsonify(data)
    return jsonify({"error": "fetch failed"}), 500

@app.route("/api/market-sentiment")
def api_market_sentiment():
    """获取市场情绪指标"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "no data"})
    
    data = json.loads(row["market_data"])
    limit_up = data.get("limit_up", [])
    limit_down = data.get("limit_down", [])
    broken = data.get("broken_limit", [])
    stats = data.get("market_stats", {})
    
    # 计算情绪指标
    up_count = stats.get("up", 0)
    down_count = stats.get("down", 0)
    total = up_count + down_count
    
    # 连板梯队
    boards = {}
    for s in limit_up:
        lb = s.get("连板数", 1)
        boards[lb] = boards.get(lb, 0) + 1
    max_board = max(boards.keys()) if boards else 0
    
    # 炸板率
    total_zt = len(limit_up) + len(broken)
    zb_rate = len(broken) / total_zt * 100 if total_zt else 0
    
    # 市场情绪判断
    if up_count > down_count * 2:
        sentiment = "强势"
    elif up_count > down_count * 1.5:
        sentiment = "偏强"
    elif up_count > down_count:
        sentiment = "偏多"
    elif up_count * 1.5 < down_count:
        sentiment = "弱势"
    elif up_count < down_count:
        sentiment = "偏弱"
    else:
        sentiment = "震荡"
    
    # 涨跌停情绪
    if len(limit_up) > 80:
        zt_sentiment = "高潮"
    elif len(limit_up) > 50:
        zt_sentiment = "活跃"
    elif len(limit_up) > 30:
        zt_sentiment = "一般"
    elif len(limit_up) > 15:
        zt_sentiment = "冰点"
    else:
        zt_sentiment = "极弱"
    
    return jsonify({
        "sentiment": sentiment,
        "zt_sentiment": zt_sentiment,
        "up_count": up_count,
        "down_count": down_count,
        "limit_up_count": len(limit_up),
        "limit_down_count": len(limit_down),
        "broken_count": len(broken),
        "zb_rate": round(zb_rate, 1),
        "max_board": max_board,
        "boards": boards,
        "activity": stats.get("activity", "")
    })

@app.route("/api/main-fund-flow")
def api_main_fund_flow():
    """获取个股主力净流入排行"""
    script = '''
import akshare as ak
import json

def parse_amount(s):
    if not s:
        return 0
    s = str(s).strip()
    try:
        if "yi" in s or "\u4ebf" in s:
            s = s.replace("yi", "").replace("\u4ebf", "")
            return float(s) * 1e8
        elif "wan" in s or "\u4e07" in s:
            s = s.replace("wan", "").replace("\u4e07", "")
            return float(s) * 1e4
        else:
            return float(s)
    except:
        return 0

def parse_percent(s):
    if not s:
        return 0
    s = str(s).strip().replace("%", "")
    try:
        return float(s)
    except:
        return 0

records = []
try:
    df = ak.stock_fund_flow_individual(symbol="\u5373\u65f6")
    for _, r in df.head(50).iterrows():
        records.append({
            "代码": str(r.get("\u80a1\u7968\u4ee3\u7801", r.get("\u4ee3\u7801", ""))),
            "名称": str(r.get("\u80a1\u7968\u7b80\u79f0", r.get("\u540d\u79f0", ""))),
            "最新价": float(r.get("\u6700\u65b0\u4ef7", 0)) if r.get("\u6700\u65b0\u4ef7") else 0,
            "涨跌幅": parse_percent(r.get("\u6da8\u8dcc\u5e45", 0)),
            "换手率": parse_percent(r.get("\u6362\u624b\u7387", 0)),
            "主力净流入-净额": parse_amount(r.get("\u51c0\u989d", 0)),
            "成交额": parse_amount(r.get("\u6210\u4ea4\u989d", 0)),
        })
except Exception as e:
    print(json.dumps({"error": str(e)}, ensure_ascii=False))

print(json.dumps(records, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=60)
    if data and isinstance(data, list) and len(data) > 0:
        return jsonify(data)

    # Fallback: 从数据库获取涨停股数据
    try:
        conn = get_db()
        row = conn.execute("SELECT market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            mdata = json.loads(row["market_data"])
            limit_up = mdata.get("limit_up", [])
            records = []
            for s in limit_up[:50]:
                records.append({
                    "代码": s.get("代码", ""),
                    "名称": s.get("名称", ""),
                    "最新价": s.get("最新价", 0),
                    "涨跌幅": s.get("涨跌幅", 0),
                    "换手率": s.get("换手率", 0),
                    "主力净流入-净额": s.get("封板资金", 0) * 10000 if s.get("封板资金") else 0,
                    "成交额": s.get("成交额", 0),
                })
            return jsonify(records)
    except:
        pass
    return jsonify([])
@app.route("/api/yesterday-zt-performance")
def api_yesterday_zt_performance():
    """获取昨日涨停股今日表现"""
    from datetime import datetime, timedelta
    
    # 获取数据库中最近两个交易日的数据
    conn = get_db()
    rows = conn.execute("SELECT snapshot_date, market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 2").fetchall()
    conn.close()
    
    if len(rows) < 2:
        return jsonify({"error": "数据不足，需要至少两个交易日的数据"})
    
    # 最新的一条是"今日"，第二条是"昨日"
    today_data = json.loads(rows[0]["market_data"])
    yesterday_data = json.loads(rows[1]["market_data"])
    yesterday_str = rows[1]["snapshot_date"]
    today_str = rows[0]["snapshot_date"]
    
    yesterday_zt = yesterday_data.get("limit_up", [])
    
    # 构建今日涨跌停映射
    today_limit_up = {s["代码"]: s for s in today_data.get("limit_up", [])}
    today_limit_down = {s["代码"]: s for s in today_data.get("limit_down", [])}
    today_broken = {s["代码"]: s for s in today_data.get("broken_limit", [])}
    
    result = []
    for s in yesterday_zt:
        code = s.get("代码", "")
        name = s.get("名称", "")
        lb = s.get("连板数", 1)
        
        today_status = "正常"
        today_chg = 0
        
        if code in today_limit_up:
            today_status = "涨停"
            today_chg = today_limit_up[code].get("涨跌幅", 10)
        elif code in today_limit_down:
            today_status = "跌停"
            today_chg = today_limit_down[code].get("涨跌幅", -10)
        elif code in today_broken:
            today_status = "炸板"
            today_chg = today_broken[code].get("涨跌幅", 0)
        
        result.append({
            "代码": code,
            "名称": name,
            "昨日连板": lb,
            "今日状态": today_status,
            "今日涨跌幅": today_chg,
            "所属行业": s.get("所属行业", ""),
        })
    
    # 按今日表现排序
    result.sort(key=lambda x: -x["今日涨跌幅"])
    
    return jsonify({
        "yesterday": yesterday_str,
        "today": today_str,
        "count": len(result),
        "data": result,
        "stats": {
            "涨停": len([s for s in result if s["今日状态"] == "涨停"]),
            "跌停": len([s for s in result if s["今日状态"] == "跌停"]),
            "炸板": len([s for s in result if s["今日状态"] == "炸板"]),
            "上涨": len([s for s in result if s["今日涨跌幅"] > 0]),
            "下跌": len([s for s in result if s["今日涨跌幅"] < 0]),
        }
    })

@app.route("/api/new-highs")
def api_new_highs():
    """获取创新高股票（从实时行情中筛选，fallback到数据库）"""
    script = '''
import akshare as ak
import json

result = {"创新高": []}

try:
    # 使用实时行情数据，信息更全
    df = ak.stock_zh_a_spot_em()
    records = []
    for _, r in df.iterrows():
        try:
            price = float(r.get("最新价", 0)) if r.get("最新价") else 0
            if price <= 0:
                continue
            chg = float(r.get("涨跌幅", 0)) if r.get("涨跌幅") else 0
            # 筛选涨幅大于3%的股票（可能是创新高）
            if chg >= 3:
                records.append({
                    "代码": str(r.get("代码", "")),
                    "名称": str(r.get("名称", "")),
                    "最新价": price,
                    "涨跌幅": chg,
                    "成交额": float(r.get("成交额", 0)) if r.get("成交额") else 0,
                    "换手率": float(r.get("换手率", 0)) if r.get("换手率") else 0,
                    "所属行业": str(r.get("所属行业", "")) if r.get("所属行业") else "",
                    "60日涨跌幅": float(r.get("60日涨跌幅", 0)) if r.get("60日涨跌幅") else 0,
                })
        except:
            pass
    records.sort(key=lambda x: -x.get("涨跌幅", 0))
    result["创新高"] = records[:100]
except Exception as e:
    result["error"] = str(e)

print(json.dumps(result, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=45)
    if data and data.get("创新高"):
        return jsonify(data)

    # Fallback: 从数据库获取涨停股 + 跌幅较大的作为"新高票"替代
    try:
        conn = get_db()
        row = conn.execute("SELECT market_data FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        conn.close()
        if row:
            mdata = json.loads(row["market_data"])
            limit_up = mdata.get("limit_up", [])
            # 涨停股作为新高票展示
            records = []
            for s in limit_up:
                records.append({
                    "代码": s.get("代码", ""),
                    "名称": s.get("名称", ""),
                    "最新价": s.get("最新价", 0),
                    "涨跌幅": s.get("涨跌幅", 0),
                    "成交额": s.get("成交额", 0),
                    "换手率": s.get("换手率", 0),
                    "所属行业": s.get("所属行业", ""),
                    "连板数": s.get("连板数", 1),
                })
            return jsonify({"创新高": records})
    except:
        pass
    return jsonify({"创新高": [], "error": "非交易时段，暂无实时数据"})

@app.route("/api/sector-leaders")
def api_sector_leaders():
    """获取板块龙头、跟风、补涨"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "no data"})
    
    data = json.loads(row["market_data"])
    limit_up = data.get("limit_up", [])
    
    # 按行业分组
    industry_stocks = {}
    for s in limit_up:
        industry = s.get("所属行业", "其他")
        if not industry:
            industry = "其他"
        if industry not in industry_stocks:
            industry_stocks[industry] = []
        industry_stocks[industry].append(s)
    
    # 找龙头（连板数最高的）和跟风
    leaders = []
    followers = []
    
    for industry, stocks in industry_stocks.items():
        # 按连板数排序
        stocks.sort(key=lambda x: -(x.get("连板数", 1) or 1))
        
        # 龙头（连板最高的）
        leader = stocks[0]
        leaders.append({
            "行业": industry,
            "代码": leader.get("代码"),
            "名称": leader.get("名称"),
            "连板数": leader.get("连板数", 1),
            "涨跌幅": leader.get("涨跌幅", 0),
            "首次封板时间": leader.get("首次封板时间", ""),
            "所属行业": industry,
        })
        
        # 跟风（同行业其他涨停）
        for s in stocks[1:]:
            followers.append({
                "行业": industry,
                "代码": s.get("代码"),
                "名称": s.get("名称"),
                "连板数": s.get("连板数", 1),
                "涨跌幅": s.get("涨跌幅", 0),
                "首次封板时间": s.get("首次封板时间", ""),
                "所属行业": industry,
            })
    
    leaders.sort(key=lambda x: -x.get("连板数", 1))
    
    return jsonify({
        "龙头": leaders[:30],
        "跟风": followers[:50],
        "行业涨停数": {k: len(v) for k, v in sorted(industry_stocks.items(), key=lambda x: -len(x[1]))[:15]}
    })

@app.route("/api/key-news")
def api_key_news():
    """获取重点新闻（多来源）"""
    script = '''
import akshare as ak
import json

records = []

# 1. 东方财富全球资讯
try:
    df = ak.stock_info_global_em()
    for _, r in df.head(200).iterrows():
        title = str(r.get("标题", ""))
        summary = str(r.get("摘要", ""))[:200]
        
        # 识别重要新闻
        is_important = False
        keywords = ["重磅", "突发", "紧急", "快讯", "央行", "国务院", "证监会", "发改委", 
                    "涨停", "跌停", "暴雷", "退市", "并购", "重组", "增持", "减持", "回购",
                    "业绩", "财报", "分红", "送转", "IPO", "新股", "政策", "改革", "利好", "利空"]
        for kw in keywords:
            if kw in title or kw in summary:
                is_important = True
                break
        
        records.append({
            "标题": title,
            "摘要": summary,
            "发布时间": str(r.get("发布时间", "")),
            "链接": str(r.get("链接", "")),
            "重要": is_important,
            "来源": "东方财富",
        })
except:
    pass

# 重要新闻排前面
records.sort(key=lambda x: -x.get("重要", False))

print(json.dumps(records[:150], ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=30)
    if data:
        return jsonify(data)
    return jsonify([])

@app.route("/api/hot-stock-announcements")
def api_hot_stock_announcements():
    """获取热门股公告"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "no data"})
    
    data = json.loads(row["market_data"])
    limit_up = data.get("limit_up", [])
    
    # 获取热门股（连板数前10）
    hot_stocks = sorted(limit_up, key=lambda x: -(x.get("连板数", 1) or 1))[:10]
    hot_codes = [s.get("代码") for s in hot_stocks]
    
    # 获取这些股票的公告
    script = f'''
import akshare as ak
import json

hot_codes = {json.dumps(hot_codes)}
result = []

for code in hot_codes:
    try:
        df = ak.stock_notice_report(symbol=code, date="{date_str}")
        for _, r in df.head(3).iterrows():
            result.append({{
                "代码": code,
                "公告标题": str(r.get("公告标题", "")),
                "公告类型": str(r.get("公告类型", "")),
                "公告日期": str(r.get("公告日期", "")),
            }})
    except:
        pass

print(json.dumps(result[:30], ensure_ascii=False))
'''
    announcements = fetch_akshare(script, timeout=30)
    
    return jsonify({
        "hot_stocks": hot_stocks,
        "announcements": announcements or []
    })

@app.route("/api/market-volume")
def api_market_volume():
    """获取市场成交量数据"""
    script = '''
import akshare as ak
import json

try:
    # 获取市场成交量
    df = ak.stock_market_activity_legu()
    stats = {}
    for _, r in df.iterrows():
        item = str(r.get("item", ""))
        val = r.get("value", "")
        stats[item] = str(val)
    
    # 获取主要指数（使用腾讯API）
    indices = {}
    try:
        import urllib.request
        idx_codes = {
            "上证指数": "sh000001",
            "深证成指": "sz399001",
            "创业板指": "sz399006",
            "科创50": "sh000688"
        }
        codes_str = ",".join(idx_codes.values())
        url = f"https://qt.gtimg.cn/q={codes_str}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gb2312")
        for line in data.strip().split(";"):
            if not line.strip():
                continue
            try:
                key, value = line.split("=", 1)
                parts = value.strip('"').split("~")
                name = parts[1]
                price = float(parts[3])
                prev_close = float(parts[4])
                change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                volume = float(parts[6]) if len(parts) > 6 else 0
                indices[name] = {
                    "最新价": price,
                    "涨跌幅": round(change_pct, 2),
                    "成交额": volume
                }
            except:
                continue
    except:
        pass
    
    print(json.dumps({"stats": stats, "indices": indices}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"error": str(e)}, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=30)
    if data:
        return jsonify(data)
    return jsonify({"error": "fetch failed"})

# ==================== 新增API：涨跌停原因/最高板/炸板金额/趋势票/板块龙头跟风补涨/K线反包/财联社红色新闻/市场情绪 ====================

@app.route("/api/limit-reasons")
def api_limit_reasons():
    """涨跌停原因 + 最高板 + 炸板金额"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    date_str = date_str.replace('-', '')
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no data"})
    data = json.loads(row["market_data"])
    lu = data.get("limit_up", [])
    ld = data.get("limit_down", [])
    bl = data.get("broken_limit", [])

    # 最高板
    sorted_lu = sorted(lu, key=lambda x: -(x.get("连板数", 1) or 1))
    highest_board = sorted_lu[0] if sorted_lu else None
    lb_dist = {}
    for s in lu:
        lb = s.get("连板数", 1) or 1
        lb_dist[str(lb)] = lb_dist.get(str(lb), 0) + 1

    # 行业涨停统计
    industry_count = {}
    for s in lu:
        ind = s.get("所属行业", "未知")
        industry_count[ind] = industry_count.get(ind, 0) + 1
    top_industries = sorted(industry_count.items(), key=lambda x: -x[1])[:10]

    # 炸板金额
    broken_amount = sum(s.get("成交额", 0) for s in bl)
    broken_rate = round(len(bl) / (len(lu) + len(bl)) * 100, 1) if (len(lu) + len(bl)) > 0 else 0

    result = {
        "highest_board": {
            "code": highest_board.get("代码", "") if highest_board else "",
            "name": highest_board.get("名称", "") if highest_board else "",
            "boards": highest_board.get("连板数", 0) if highest_board else 0,
            "industry": highest_board.get("所属行业", "") if highest_board else "",
        },
        "lb_distribution": lb_dist,
        "top_industries": [{"name": n, "count": c} for n, c in top_industries],
        "broken_amount": broken_amount,
        "broken_rate": broken_rate,
        "limit_up_count": len(lu),
        "limit_down_count": len(ld),
        "broken_count": len(bl),
        "limit_up": lu,
        "limit_down": ld,
    }
    return jsonify(result)


@app.route("/api/trend-stocks")
def api_trend_stocks():
    """趋势票高度：识别连续上涨的强势趋势票"""
    script = '''
import akshare as ak
import json
import datetime
import concurrent.futures
import warnings
warnings.filterwarnings('ignore')

results = []

try:
    df = ak.stock_zh_a_spot_em()
    if df is not None and len(df) > 0:
        candidates = df[(df['涨跌幅'] > 2) & (df['成交额'] > 50000000)].head(200)
        stock_list = [(str(r['代码']).zfill(6), str(r['名称']), float(r['最新价']), float(r['涨跌幅']), float(r['成交额'])) for _, r in candidates.iterrows()]
except:
    stock_list = []

start_date = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime('%Y%m%d')
end_date = datetime.datetime.now().strftime('%Y%m%d')

def calc_trend(item):
    code, name, price, chg, vol = item
    try:
        prefix = 'sh' if code.startswith(('6','5')) else 'sz'
        df = ak.stock_zh_a_daily(symbol=prefix+code, start_date=start_date, end_date=end_date, adjust='qfq')
        if df is None or len(df) < 10:
            return None
        df = df.tail(20)
        if len(df) < 10:
            return None
        closes = [float(c) for c in df['close'].values]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else closes[-1]
        ma20 = sum(closes) / len(closes)
        up_days = 0
        for i in range(len(closes)-1, 0, -1):
            if closes[i] > closes[i-1]:
                up_days += 1
            else:
                break
        trend_pct = (closes[-1] - closes[0]) / closes[0] * 100
        ma_aligned = ma5 > ma10 > ma20
        if up_days >= 3 and ma_aligned:
            return {
                "\\u4ee3\\u7801": code,
                "\\u540d\\u79f0": name,
                "\\u6700\\u65b0\\u4ef7": price,
                "\\u4eca\\u65e5\\u6da8\\u5e45": round(chg, 2),
                "\\u6210\\u4ea4\\u989d": round(vol),
                "\\u8fde\\u7eed\\u4e0a\\u6da8": up_days,
                "20\\u65e5\\u6da8\\u5e45": round(trend_pct, 2),
                "MA5": round(ma5, 2),
                "MA10": round(ma10, 2),
                "MA20": round(ma20, 2),
                "\\u8d8b\\u52bf\\u5f3a\\u5ea6": min(100, round(up_days * 8 + trend_pct * 0.5))
            }
    except:
        pass
    return None

with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
    futures = [executor.submit(calc_trend, item) for item in stock_list[:150]]
    for future in concurrent.futures.as_completed(futures, timeout=45):
        try:
            r = future.result()
            if r and r.get("\\u8fde\\u7eed\\u4e0a\\u6da8", 0) >= 3:
                results.append(r)
        except:
            pass

results.sort(key=lambda x: -(x.get("\\u8fde\\u7eed\\u4e0a\\u6da8", 0) * 10 + x.get("20\\u65e5\\u6da8\\u5e45", 0)))
print(json.dumps(results[:80], ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=60)
    return jsonify(data or [])


@app.route("/api/sector-lfs")
def api_sector_lfs():
    """板块龙头/跟风/补涨分析"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    date_str = date_str.replace('-', '')
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no data"})
    data = json.loads(row["market_data"])
    lu = data.get("limit_up", [])
    bl = data.get("broken_limit", [])
    sector_flow = data.get("sector_flow", [])

    industry_stocks = {}
    for s in lu + bl:
        ind = s.get("所属行业", "未知")
        if ind not in industry_stocks:
            industry_stocks[ind] = []
        industry_stocks[ind].append(s)

    sector_analysis = []
    for ind, stocks in industry_stocks.items():
        stocks.sort(key=lambda x: -(x.get("连板数", 1) or 1) * 100 - x.get("涨跌幅", 0))
        leader = stocks[0] if stocks else None
        followers = [s for s in stocks[1:] if s.get("涨跌幅", 0) >= 5] if len(stocks) > 1 else []
        flow_info = next((f for f in sector_flow if f.get("名称", "") == ind), None)
        sector_analysis.append({
            "行业": ind,
            "涨停数": len([s for s in stocks if s.get("涨跌幅", 0) >= 9.8]),
            "龙头": {
                "代码": leader.get("代码", ""),
                "名称": leader.get("名称", ""),
                "连板": leader.get("连板数", 1),
                "涨跌幅": leader.get("涨跌幅", 0),
                "封板资金": leader.get("封板资金", 0),
            } if leader else None,
            "跟风": [{
                "代码": s.get("代码", ""),
                "名称": s.get("名称", ""),
                "连板": s.get("连板数", 1),
                "涨跌幅": s.get("涨跌幅", 0),
            } for s in followers[:5]],
            "资金净流入": flow_info.get("净额", 0) if flow_info else 0,
        })

    sector_net_in = {f.get("名称", ""): f.get("净额", 0) for f in sector_flow}
    catch_up = []
    for ind, net_in in sorted(sector_net_in.items(), key=lambda x: -x[1]):
        if net_in > 0 and ind not in industry_stocks:
            flow_info = next((f for f in sector_flow if f.get("名称", "") == ind), None)
            catch_up.append({
                "行业": ind,
                "资金净流入": net_in,
                "涨幅": flow_info.get("今日涨跌幅", 0) if flow_info else 0,
                "领涨股": flow_info.get("领涨股", "") if flow_info else "",
                "领涨股涨幅": flow_info.get("领涨股-涨跌幅", 0) if flow_info else 0,
            })

    sector_analysis.sort(key=lambda x: -(x.get("涨停数", 0)))
    result = {
        "sectors": sector_analysis[:20],
        "catch_up": catch_up[:10],
    }
    return jsonify(result)


@app.route("/api/kline-engulfing")
def api_kline_engulfing():
    """K线反包检测：今天阳线完全覆盖昨日阴线"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no data"})
    data = json.loads(row["market_data"])
    all_stocks = data.get("limit_up", []) + data.get("broken_limit", [])
    codes = list(set(s.get("代码", "") for s in all_stocks if s.get("代码", "")))

    results = []
    def check_engulfing(code):
        try:
            klines = fetch_em_kline(code, period='daily', days=10)
            if not klines or len(klines) < 2:
                return None
            yest = klines[-2]
            today = klines[-1]
            yest_o, yest_c = yest["开盘"], yest["收盘"]
            today_o, today_c = today["开盘"], today["收盘"]
            today_l = today["最低"]
            if (yest_c < yest_o and today_c > today_o and
                today_l <= yest["最低"] and today_c >= yest_o):
                name = next((s.get("名称", "") for s in all_stocks if s.get("代码") == code), "")
                return {
                    "代码": code,
                    "名称": name,
                    "昨日开盘": yest_o,
                    "昨日收盘": yest_c,
                    "今日开盘": today_o,
                    "今日收盘": today_c,
                    "昨日跌幅": round((yest_c - yest_o) / yest_o * 100, 2),
                    "今日涨幅": round((today_c - today_o) / today_o * 100, 2),
                    "反包力度": round((today_c - yest_c) / abs(yest_c - yest_o) * 100, 1) if abs(yest_c - yest_o) > 0 else 0,
                }
        except:
            pass
        return None

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_engulfing, c) for c in codes[:100]]
        for future in concurrent.futures.as_completed(futures, timeout=30):
            try:
                r = future.result()
                if r:
                    results.append(r)
            except:
                pass
    results.sort(key=lambda x: -x.get("今日涨幅", 0))
    return jsonify(results)


@app.route("/api/cls-red-news")
def api_cls_red_news():
    """财联社红色重点新闻"""
    script = '''
import akshare as ak
import json

records = []

try:
    df = ak.stock_info_global_em()
    if df is not None:
        for _, r in df.head(200).iterrows():
            title = str(r.get("标题", ""))[:150]
            summary = str(r.get("摘要", ""))[:200]
            is_red = any(kw in title for kw in ["重磅", "突发", "紧急", "央行", "国务院", "证监会", "发改委", "涨停", "跌停", "暴雷", "退市", "并购", "重组", "利好", "利空", "大涨", "暴跌", "突破", "IPO", "获批", "中标", "超预期"])
            records.append({
                "标题": title,
                "摘要": summary,
                "来源": "东方财富",
                "时间": str(r.get("发布时间", "")),
                "链接": str(r.get("链接", "")),
                "红色": is_red,
            })
except:
    pass

seen = set()
unique = []
for r in records:
    t = r.get("标题", "")
    if t and t not in seen:
        seen.add(t)
        unique.append(r)
unique.sort(key=lambda x: (-x.get("红色", False), x.get("时间", "")))
print(json.dumps(unique[:80], ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=30)
    return jsonify(data or [])


@app.route("/api/market-sentiment-detail")
def api_market_sentiment_detail():
    """市场情绪详细指数"""
    date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
    date_str = date_str.replace('-', '')
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (date_str,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no data"})
    data = json.loads(row["market_data"])
    lu = data.get("limit_up", [])
    ld = data.get("limit_down", [])
    bl = data.get("broken_limit", [])
    ms = data.get("market_stats", {})

    up = ms.get("up", 0)
    down = ms.get("down", 0)
    total = up + down + ms.get("flat", 0)

    lb_dist = {}
    for s in lu:
        lb = s.get("连板数", 1) or 1
        lb_dist[str(lb)] = lb_dist.get(str(lb), 0) + 1
    max_lb = max([int(k) for k in lb_dist.keys()] or [0])

    zb_rate = round(len(bl) / (len(lu) + len(bl)) * 100, 1) if (len(lu) + len(bl)) > 0 else 0
    ud_ratio = round(up / down, 2) if down > 0 else 99

    score = 50
    score += min(20, (ud_ratio - 1) * 10)
    score += min(15, len(lu) * 0.3)
    score -= min(15, zb_rate * 0.3)
    score -= min(20, len(ld) * 0.5)
    score += min(10, max_lb * 2)
    score = max(0, min(100, round(score)))

    if score >= 80: level = "极度乐观"; icon = "🚀"
    elif score >= 65: level = "偏多"; icon = "🐂"
    elif score >= 45: level = "震荡"; icon = "⚖️"
    elif score >= 30: level = "偏空"; icon = "🐻"
    else: level = "极度悲观"; icon = "💀"

    sector_flow = data.get("sector_flow", [])
    top_in = sorted(sector_flow, key=lambda x: -x.get("净额", 0))[:5]
    top_out = sorted(sector_flow, key=lambda x: x.get("净额", 0))[:5]

    result = {
        "score": score,
        "level": level,
        "icon": icon,
        "factors": {
            "涨跌比": ud_ratio,
            "涨停数": len(lu),
            "跌停数": len(ld),
            "炸板数": len(bl),
            "炸板率": zb_rate,
            "最高板": max_lb,
            "连板分布": lb_dist,
            "上涨家数": up,
            "下跌家数": down,
            "平盘家数": ms.get("flat", 0),
        },
        "sector_top_in": [{"名称": s.get("名称", ""), "净额": s.get("净额", 0), "涨幅": s.get("今日涨跌幅", 0)} for s in top_in],
        "sector_top_out": [{"名称": s.get("名称", ""), "净额": s.get("净额", 0), "涨幅": s.get("今日涨跌幅", 0)} for s in top_out],
    }
    return jsonify(result)


# ==================== 异动预测：30日偏离值（v3 直接调用fetch_em_kline）====================
_anomaly_cache = {"data": None, "ts": 0}
_anomaly_scanning = False

# 启动时检查DB中是否有缓存的异动数据
try:
    _startup_db = get_db()
    for _day_offset in range(3):
        _startup_date = (datetime.now() - timedelta(days=_day_offset)).strftime("%Y%m%d")
        _startup_row = _startup_db.execute(
            "SELECT market_data FROM daily_snapshots WHERE snapshot_date=?",
            ("anomaly_" + _startup_date,)
        ).fetchone()
        if _startup_row:
            _startup_cached = json.loads(_startup_row["market_data"])
            if _startup_cached:
                _anomaly_cache["data"] = _startup_cached
                _anomaly_cache["ts"] = 0  # 标记为可刷新，但有数据可用
                break
    _startup_db.close()
except Exception as _e:
    print(f"Anomaly startup cache load error: {_e}")

def _anomaly_scan_worker():
    """后台线程：扫描全A股30日偏离值（使用Sina数据源）"""
    global _anomaly_scanning
    import time as _time
    try:
        # 在akshare子进程中完成全部计算（用Sina数据源，支持多线程）
        scan_script = (
            "import akshare as ak\n"
            "import json\n"
            "import datetime\n"
            "import concurrent.futures\n"
            "import warnings\n"
            "warnings.filterwarnings('ignore')\n"
            "\n"
            "results = []\n"
            "\n"
            "# Step 1: Get all stocks' current data in one batch call (fast)\n"
            "try:\n"
            "    spot_df = ak.stock_zh_a_spot_em()\n"
            "    # Filter: only stocks with 涨跌幅 > 3% or 换手率 > 5% (high activity)\n"
            "    candidates = spot_df[(spot_df['涨跌幅'].abs() > 3) | (spot_df['换手率'] > 5)]\n"
            "    all_stocks = [(str(r['代码']).zfill(6), str(r['名称'])) for _, r in candidates.iterrows()]\n"
            "    # Also add stocks with high volume change\n"
            "    high_vol = spot_df[spot_df['量比'] > 2]\n"
            "    for _, r in high_vol.iterrows():\n"
            "        code = str(r['代码']).zfill(6)\n"
            "        if (code, str(r['名称'])) not in all_stocks:\n"
            "            all_stocks.append((code, str(r['名称'])))\n"
            "except Exception as e:\n"
            "    print(f'Spot data error: {e}')\n"
            "    all_stocks = []\n"
            "\n"
            "start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime('%Y%m%d')\n"
            "end_date = datetime.datetime.now().strftime('%Y%m%d')\n"
            "\n"
            "def calc_one(item):\n"
            "    code, name = item\n"
            "    try:\n"
            "        prefix = 'sh' if code.startswith(('6','5')) else 'sz'\n"
            "        df = ak.stock_zh_a_daily(symbol=prefix+code, start_date=start_date, end_date=end_date, adjust='qfq')\n"
            "        if df is None or len(df) < 10:\n"
            "            return None\n"
            "        df = df.tail(30)\n"
            "        if len(df) < 10:\n"
            "            return None\n"
            "        sp = float(df.iloc[0]['close'])\n"
            "        ep = float(df.iloc[-1]['close'])\n"
            "        if sp <= 0:\n"
            "            return None\n"
            "        cum_change = (ep - sp) / sp * 100\n"
            "        cum_dev = abs(cum_change)\n"
            "        if cum_dev < 50:\n"
            "            return None\n"
            "        chgs = ((df['close'] - df['close'].shift(1)) / df['close'].shift(1) * 100).fillna(0).tolist()\n"
            "        highs = df['high'].tolist()\n"
            "        lows = df['low'].tolist()\n"
            "        return {\n"
            "            '\u4ee3\u7801': code, '\u540d\u79f0': name, '\u73b0\u4ef7': ep,\n"
            "            '\u4eca\u65e5\u6da8\u8dcc\u5e45': round(chgs[-1], 2) if chgs else 0,\n"
            "            '30\u65e5\u7d2f\u8ba1\u504f\u79bb': round(cum_dev, 2),\n"
            "            '30\u65e5\u7d2f\u8ba1\u6da8\u8dcc\u5e45': round(cum_change, 2),\n"
            "            '30\u65e5\u6700\u5927\u65e5\u6da8\u5e45': round(max(chgs), 2),\n"
            "            '30\u65e5\u6700\u5927\u65e5\u8dcc\u5e45': round(min(chgs), 2),\n"
            "            '30\u65e5\u6700\u9ad8': max(highs), '30\u65e5\u6700\u4f4e': min(lows),\n"
            "            '\u72b6\u6001': '\u5df2\u89e6\u53d1' if cum_dev >= 200 else '\u63a5\u8fd1\u89e6\u53d1' if cum_dev >= 100 else '\u5173\u6ce8',\n"
            "            '\u4ea4\u6613\u65e5\u6570': len(df)\n"
            "        }\n"
            "    except:\n"
            "        return None\n"
            "\n"
            "with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:\n"
            "    futures = {executor.submit(calc_one, s): s for s in all_stocks}\n"
            "    done = 0\n"
            "    for f in concurrent.futures.as_completed(futures):\n"
            "        done += 1\n"
            "        if done % 200 == 0:\n"
            "            print(f'Progress: {done}/{len(all_stocks)}', flush=True)\n"
            "        try:\n"
            "            r = f.result()\n"
            "            if r:\n"
            "                results.append(r)\n"
            "        except:\n"
            "            pass\n"
            "\n"
            "results.sort(key=lambda x: -x['30\u65e5\u7d2f\u8ba1\u504f\u79bb'])\n"
            "print(json.dumps(results[:200], ensure_ascii=False))\n"
        )
        proc = subprocess.run([AKSHARE_PYTHON, "-c", scan_script],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and proc.stdout.strip():
            results = json.loads(proc.stdout.strip())
            print(f"Anomaly scan complete: {len(results)} stocks found")
        else:
            print(f"Anomaly scan failed: {proc.stderr[:300]}")
            results = []

        _anomaly_cache["data"] = results
        _anomaly_cache["ts"] = _time.time()

        # 缓存到数据库
        try:
            conn = get_db()
            conn.execute("INSERT OR REPLACE INTO daily_snapshots (snapshot_date, market_data) VALUES (?, ?)",
                         ("anomaly_" + datetime.now().strftime("%Y%m%d"),
                          json.dumps(results, ensure_ascii=False)))
            conn.commit()
            conn.close()
        except:
            pass

    except Exception as e:
        _anomaly_scanning = False
        print(f"Anomaly scan error: {e}")

    _anomaly_scanning = False

    # OLD CODE BELOW - will not execute
    results = []
    days = 45

    def calc_one(stock):
        code, name = stock["code"], stock["name"]
        try:
            klines = fetch_em_kline(code, period="daily", days=days)
            if not klines or len(klines) < 10:
                return None
            klines = klines[-30:]
            start_price = klines[0]["\u6536\u76d8"]
            end_price = klines[-1]["\u6536\u76d8"]
            if start_price <= 0:
                return None
            cum_change = (end_price - start_price) / start_price * 100
            cum_dev = abs(cum_change)
            if cum_dev < 100:
                return None
            chgs = [k["\u6da8\u8dcc\u5e45"] for k in klines]
            highs = [k["\u6700\u9ad8"] for k in klines]
            lows = [k["\u6700\u4f4e"] for k in klines]
            return {
                "\u4ee3\u7801": code, "\u540d\u79f0": name, "\u73b0\u4ef7": end_price,
                "\u4eca\u65e5\u6da8\u8dcc\u5e45": chgs[-1] if chgs else 0,
                "30\u65e5\u7d2f\u8ba1\u504f\u79bb": round(cum_dev, 2),
                "30\u65e5\u7d2f\u8ba1\u6da8\u8dcc\u5e45": round(cum_change, 2),
                "30\u65e5\u6700\u5927\u65e5\u6da8\u5e45": round(max(chgs), 2),
                "30\u65e5\u6700\u5927\u65e5\u8dcc\u5e45": round(min(chgs), 2),
                "30\u65e5\u6700\u9ad8": max(highs), "30\u65e5\u6700\u4f4e": min(lows),
                "\u72b6\u6001": "\u5df2\u89e6\u53d1" if cum_dev >= 200 else "\u63a5\u8fd1\u89e6\u53d1",
                "\u4ea4\u6613\u65e5\u6570": len(klines)
            }
        except:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(calc_one, s): s for s in all_stocks}
        done_count = 0
        for f in concurrent.futures.as_completed(futures):
            done_count += 1
            if done_count % 500 == 0:
                print(f"Anomaly scan progress: {done_count}/{len(all_stocks)}", flush=True)
            try:
                r = f.result()
                if r:
                    results.append(r)
            except:
                pass

    results.sort(key=lambda x: -x["30\u65e5\u7d2f\u8ba1\u504f\u79bb"])
    _anomaly_cache["data"] = results[:200]
    _anomaly_cache["ts"] = _time.time()
    print(f"Anomaly scan complete: {len(results)} stocks above 100% deviation")

    # 缓存到数据库
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO daily_snapshots (snapshot_date, market_data) VALUES (?, ?)",
                     ("anomaly_" + datetime.now().strftime("%Y%m%d"),
                      json.dumps(results[:200], ensure_ascii=False)))
        conn.commit()
        conn.close()
    except:
        pass

    _anomaly_scanning = False


@app.route("/api/anomaly-prediction")
def api_anomaly_prediction():
    """30个交易日收盘价涨跌幅偏离值累计达到200%的异动预测"""
    global _anomaly_scanning
    import threading
    import time

    # 1. 内存缓存（1小时内有效）
    if _anomaly_cache["data"] and (time.time() - _anomaly_cache["ts"]) < 3600:
        return jsonify(_anomaly_cache["data"])

    # 2. 数据库缓存（今天+昨天）
    try:
        conn = get_db()
        for _day_offset in range(2):
            _cache_date = (datetime.now() - timedelta(days=_day_offset)).strftime("%Y%m%d")
            row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?",
                               ("anomaly_" + _cache_date,)).fetchone()
            if row:
                cached = json.loads(row["market_data"])
                if cached:
                    _anomaly_cache["data"] = cached
                    _anomaly_cache["ts"] = time.time()
                    conn.close()
                    return jsonify(cached)
        conn.close()
    except:
        pass

    # 3. 启动后台扫描
    if not _anomaly_scanning:
        _anomaly_scanning = True
        t = threading.Thread(target=_anomaly_scan_worker, daemon=True)
        t.start()
        return jsonify({"scanning": True, "message": "\u6b63\u5728\u626b\u63cf\u5168\u5e02\u573a\uff0c\u9884\u8ba13-5\u5206\u949f..."})

    return jsonify({"scanning": True, "message": "\u626b\u63cf\u8fdb\u884c\u4e2d\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0..."})
# ==================== 监控室（优化版：多日期尝试+缓存）====================
@app.route("/api/monitoring")
def api_monitoring():
    """交易所监控室：风险提示、严重波动、严重异常波动"""
    # 先查缓存
    cache_key = "monitoring_" + datetime.now().strftime("%Y%m%d")
    conn = get_db()
    row = conn.execute("SELECT market_data FROM daily_snapshots WHERE snapshot_date=?", (cache_key,)).fetchone()
    conn.close()
    if row:
        cached = json.loads(row["market_data"])
        if cached.get("abnormal_trading") or cached.get("risk_warnings") or cached.get("st_stocks"):
            return jsonify(cached)
    
    script = r'''
import akshare as ak
import json
import datetime
import warnings
warnings.filterwarnings("ignore")

result = {"risk_warnings": [], "abnormal_trading": [], "st_stocks": []}

# 1. 风险提示公告（尝试多个日期）
today = datetime.datetime.now()
for delta in range(0, 7):
    try_date = (today - datetime.timedelta(days=delta)).strftime("%Y%m%d")
    try:
        df = ak.stock_notice_report(symbol="\u98ce\u9669\u63d0\u793a", date=try_date)
        if df is not None and len(df) > 0:
            for _, r in df.head(50).iterrows():
                result["risk_warnings"].append({
                    "\u4ee3\u7801": str(r.get("\u4ee3\u7801", "")),
                    "\u540d\u79f0": str(r.get("\u540d\u79f0", "")),
                    "\u516c\u544a\u6807\u9898": str(r.get("\u516c\u544a\u6807\u9898", "")),
                    "\u516c\u544a\u7c7b\u578b": str(r.get("\u516c\u544a\u7c7b\u578b", "")),
                    "\u516c\u544a\u65e5\u671f": str(r.get("\u516c\u544a\u65e5\u671f", ""))
                })
            break  # 找到就停止
    except:
        continue

# 2. 盘口异动
change_types = ["\u5c01\u6da8\u505c\u677f", "\u6253\u5f00\u6da8\u505c\u677f", "\u5c01\u8dcc\u505c\u677f", "\u6253\u5f00\u8dcc\u505c\u677f", "\u9ad8\u53f0\u8df3\u6c34", "\u52a0\u901f\u4e0b\u8dcc", "\u5927\u7b14\u5356\u51fa", "\u7ade\u4ef7\u4e0a\u6da8"]
for ct in change_types:
    try:
        df = ak.stock_changes_em(symbol=ct)
        if df is not None and len(df) > 0:
            for _, r in df.head(15).iterrows():
                result["abnormal_trading"].append({
                    "\u65f6\u95f4": str(r.get("\u65f6\u95f4", "")),
                    "\u4ee3\u7801": str(r.get("\u4ee3\u7801", "")),
                    "\u540d\u79f0": str(r.get("\u540d\u79f0", "")),
                    "\u5f02\u52a8\u7c7b\u578b": ct,
                    "\u76f8\u5173\u4fe1\u606f": str(r.get("\u76f8\u5173\u4fe1\u606f", ""))
                })
    except:
        pass

# 3. ST/*ST 股票
try:
    df = ak.stock_zh_a_st_em()
    if df is not None and len(df) > 0:
        for _, r in df.iterrows():
            result["st_stocks"].append({
                "\u4ee3\u7801": str(r.get("\u4ee3\u7801", "")),
                "\u540d\u79f0": str(r.get("\u540d\u79f0", "")),
                "\u6700\u65b0\u4ef7": float(r.get("\u6700\u65b0\u4ef7", 0)) if r.get("\u6700\u65b0\u4ef7") else 0,
                "\u6da8\u8dcc\u5e45": float(r.get("\u6da8\u8dcc\u5e45", 0)) if r.get("\u6da8\u8dcc\u5e45") else 0,
                "\u6210\u4ea4\u989d": float(r.get("\u6210\u4ea4\u989d", 0)) if r.get("\u6210\u4ea4\u989d") else 0,
            })
except:
    pass

print(json.dumps(result, ensure_ascii=False))
'''
    data = fetch_akshare(script, timeout=90)
    if data and (data.get("abnormal_trading") or data.get("risk_warnings") or data.get("st_stocks")):
        # 缓存到数据库
        try:
            conn = get_db()
            conn.execute("INSERT OR REPLACE INTO daily_snapshots (snapshot_date, market_data) VALUES (?, ?)",
                         (cache_key, json.dumps(data, ensure_ascii=False)))
            conn.commit()
            conn.close()
        except:
            pass
        return jsonify(data)
    
    # 如果API返回空，返回空结果
    return jsonify({"risk_warnings": [], "abnormal_trading": [], "st_stocks": []})


if __name__ == "__main__":
    print("📈 A股全自动复盘看板 v5")
    print("   地址: http://0.0.0.0:80")
    init_db()
    app.run(host="0.0.0.0", port=80, debug=False)
