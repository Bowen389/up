"""悠悠有品 求购厚度 / 大佬监控  —  每晚运行一次: python monitor.py"""
import json, time, random, sqlite3, datetime, html, sys, os
import requests

BASE = "https://pc-api.youpin898.com"
DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("YYYP_DATA", os.path.join(DIR, "data"))
os.makedirs(DATA, exist_ok=True)
def _load_cfg():
    c = {"ratio": 0.85, "delay_min": 1.5, "delay_max": 3.0, "headers": {}}
    p = os.environ.get("YYYP_CONFIG", os.path.join(DIR, "config.json"))
    if os.path.exists(p): c.update(json.load(open(p, encoding="utf-8")))
    for k, env in [("authorization", "YP_AUTHORIZATION"), ("deviceId", "YP_DEVICE_ID"),
                   ("deviceUk", "YP_DEVICE_UK"), ("uk", "YP_UK")]:
        if os.environ.get(env): c["headers"][k] = os.environ[env].strip()
    return c
cfg = _load_cfg()

S = requests.Session()
S.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.youpin898.com",
    "Referer": "https://www.youpin898.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "App-Version": "5.26.0", "AppVersion": "5.26.0",
    "appType": "1", "platform": "pc", "secret-v": "h5_v1",
    **cfg["headers"],
})

def sleep():
    time.sleep(random.uniform(cfg.get("delay_min", 1.5), cfg.get("delay_max", 3)))

def post(path, body, tries=6):
    """自动处理网络错误和限流(84104)：逐步加长等待"""
    for i in range(tries):
        try:
            d = S.post(BASE + path, json=body, timeout=20).json()
        except Exception as e:
            print("  请求失败重试:", e); time.sleep(5 * (i + 1)); continue
        code = d.get("code", d.get("Code"))
        if code == 84104:
            w = 60 * (i + 1); print(f"  平台限流，等待 {w}s ..."); time.sleep(w); continue
        return d
    raise RuntimeError("请求多次失败: " + path)

def get_on_sale(tid):
    """返回 (最低在售价, 在售总数, 商品名)"""
    d = post("/api/homepage/pc/goods/market/queryOnSaleCommodityList",
             {"gameId": "730", "listType": "10", "templateId": str(tid),
              "listSortType": 1, "sortType": 0, "pageIndex": 1, "pageSize": 10})
    if d.get("Code") != 0:
        raise RuntimeError(f"在售接口异常: {d.get('Code')} {d.get('Msg')}")
    lst = d.get("Data") or []
    prices = [float(x["price"]) for x in lst if x.get("price")]
    name = lst[0]["commodityName"] if lst else ""
    return (min(prices) if prices else None), d.get("TotalCount", 0), name

import re
def keyword_of(name):
    k = re.sub(r"（.*?）", "", name.split("|")[0]).strip()
    return {"CZ75自动型": "CZ75"}.get(k, k)

GROUP_KEYWORDS = ["探员"]   # 一次搜出整类；结果按 templateId 精确匹配，不会混淆

def _search_all(kw, want, got):
    page = 1
    while True:
        d = post("/api/homepage/pc/goods/market/querySaleTemplate",
                 {"gameId": "730", "listType": "10", "keyWords": kw, "pageIndex": page,
                  "pageSize": 200, "listSortType": 0, "sortType": 0})
        lst = d.get("Data") or []
        for x in lst:
            if x["id"] in want and x["id"] not in got:
                got[x["id"]] = (float(x["price"]) if x.get("price") else None,
                                x.get("onSaleCount") or 0, x["commodityName"])
        if len(lst) < 200: return
        page += 1; sleep()

def batch_prices(items):
    """批量拿 底价/在售数：先用分类大词，未覆盖的再按武器类型搜"""
    want = set(items); got = {}
    for kw in GROUP_KEYWORDS:
        _search_all(kw, want, got); sleep()
        print(f"  💓 价格批量 [{kw}] 已覆盖 {len(got)}/{len(want)}", flush=True)
    kws = sorted({keyword_of(ITEM_NAMES.get(t, "")) for t in want - set(got) if ITEM_NAMES.get(t)})
    for n, kw in enumerate(kws, 1):
        _search_all(kw, want, got)
        print(f"  💓 价格批量 {n}/{len(kws)} {kw}  已覆盖 {len(got)}/{len(want)}", flush=True)
        sleep()
    return got

def get_purchases(tid, stop_below):
    """求购列表按价格从高到低；低于阈值就停止翻页"""
    out, page = [], 1
    while True:
        d = post("/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderListPC",
                 {"templateId": str(tid), "pageIndex": page, "pageSize": 50})
        if d.get("code") != 0:
            raise RuntimeError(f"求购接口异常: {d.get('code')} {d.get('msg')}")
        data = d.get("data") or {}
        lst = data.get("purchaseOrderResponseList") or []
        out += lst
        total = data.get("total") or 0
        if not lst or page * 50 >= total or float(lst[-1]["purchasePrice"]) < stop_below:
            return out
        page += 1; sleep()

def init_db():
    db = sqlite3.connect(os.path.join(DATA, "yyyp.db"))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS snapshot(run TEXT, tid INT, name TEXT, min_price REAL,
        on_sale INT, depth REAL, depth_qty INT, PRIMARY KEY(run,tid));
    CREATE TABLE IF NOT EXISTS orders(run TEXT, tid INT, purchase_no TEXT, user_id INT,
        user_name TEXT, price REAL, qty INT, abrade TEXT, valid INT);
    CREATE INDEX IF NOT EXISTS i1 ON orders(run,user_id);
    """)
    return db

ITEM_NAMES = {}
def load_items():
    ids = []
    for line in open(os.path.join(DIR, "items.txt"), encoding="utf-8"):
        a, _, b = line.partition("#")
        if a.strip():
            ids.append(int(a)); ITEM_NAMES[int(a)] = b.strip()
    return list(dict.fromkeys(ids))

def check_token():
    import base64
    try:
        p = cfg["headers"]["authorization"].split(".")[1]; p += "=" * (-len(p) % 4)
        exp = datetime.datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(p))["exp"])
    except Exception:
        sys.exit("❌ authorization 缺失或格式错误，请检查配置")
    left = exp - datetime.datetime.now()
    if left.total_seconds() <= 0: sys.exit(f"❌ token 已于 {exp} 过期，请更新")
    print(f"token 有效期至 {exp}（剩余 {left.days} 天）", flush=True)

def main():
    check_token()
    db = init_db(); ratio = cfg.get("ratio", 0.85)
    run = datetime.datetime.now().strftime("%Y-%m-%d")  # 同一天重复运行=断点续跑
    done = {r[0] for r in db.execute("SELECT tid FROM snapshot WHERE run=?", (run,))}
    items = load_items()
    todo = [t for t in items if t not in done]
    print(f"本次待扫 {len(todo)} 个（已完成 {len(done)}）", flush=True)
    prices = batch_prices(todo) if todo else {}
    for n, tid in enumerate(items, 1):
        if tid in done: continue
        try:
            if tid in prices: minp, cnt, name = prices[tid]
            else:
                print(f"💓 [{n}/{len(items)}] {tid} 批量未覆盖(可能无人在售)，跳过"); continue
            th = (minp or 0) * ratio
            orders = get_purchases(tid, th); sleep()
        except Exception as e:
            print(f"[{n}/{len(items)}] {tid} 失败: {e}")
            if "401" in str(e) or "登录" in str(e): sys.exit("token 失效，请更新 config.json")
            continue
        db.execute("DELETE FROM orders WHERE run=? AND tid=?", (run, tid))
        depth = qty = 0
        for o in orders:
            p, q = float(o["purchasePrice"]), int(o["surplusQuantity"] or 0)
            valid = 1 if minp and p >= th else 0
            if valid: depth += p * q; qty += q
            db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                       (run, tid, o["purchaseNo"], o["userId"], o["userName"], p, q,
                        o.get("abradeText"), valid))
            name = name or o.get("commodityName", "")
        db.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?)",
                   (run, tid, name, minp, cnt, depth, qty))
        db.commit()
        print(f"💓 [{n}/{len(items)}] {name} 底价{minp} 在售{cnt} 厚度¥{depth:,.0f}({qty}件)")
    report(db, run)

def report(db, run):
    """生成报告：① 异动榜（在售减少 + 求购增多）② 商品表 ③ 大佬排行"""
    E = html.escape
    A = cfg.get("alert", {})
    SALE_DROP = A.get("sale_drop_pct", 3)      # 在售至少减少 %
    DEPTH_UP = A.get("depth_up_pct", 5)        # 厚度至少增加 %
    MIN_ADD = A.get("min_depth_add", 5000)     # 厚度至少增加金额（过滤便宜货噪音）
    runs = [r[0] for r in db.execute("SELECT DISTINCT run FROM snapshot WHERE run<=? ORDER BY run DESC", (run,))]
    prev = runs[1] if len(runs) > 1 else None
    wk = next((r for r in runs[1:] if r <= (datetime.datetime.strptime(run[:10], "%Y-%m-%d")
                                           - datetime.timedelta(days=6)).strftime("%Y-%m-%d")), None)
    snap = lambda r: {x[0]: x[1:] for x in db.execute(
        "SELECT tid,name,min_price,on_sale,depth,depth_qty FROM snapshot WHERE run=?", (r,))} if r else {}
    T, P, W = snap(run), snap(prev), snap(wk)

    def pct(a, b): return (a - b) / b * 100 if b else (100.0 if a > 0 else 0.0)
    def fmt_pct(v, good_up=True):
        if v is None: return "-"
        c = "#c00" if (v > 0) == good_up and abs(v) >= 0.5 else ("#080" if abs(v) >= 0.5 else "#888")
        return f"<span style='color:{c}'>{v:+.1f}%</span>"

    # 谁在加仓：该商品上 用户有效求购额 的变化
    def movers(tid):
        q = "SELECT user_id,MAX(user_name),SUM(price*qty) FROM orders WHERE run=? AND tid=? AND valid=1 GROUP BY user_id"
        now = {u: (n, a) for u, n, a in db.execute(q, (run, tid))}
        old = {u: a for u, n, a in db.execute(q, (prev, tid))} if prev else {}
        d = sorted(((now[u][1] - old.get(u, 0), now[u][0], u) for u in now), reverse=True)
        return [f"<a href='https://hybrid.youpin898.com/index.html#/shop/{u}' target=_blank>{E(n)}</a> +¥{x:,.0f}"
                + (" <b style='color:#c00'>新</b>" if u not in old else "") for x, n, u in d[:3] if x > 0]

    rows_alert, rows_all = [], []
    for tid, (name, minp, cnt, depth, q) in T.items():
        p = P.get(tid); w = W.get(tid)
        ds = pct(cnt, p[2]) if p else None
        dd = pct(depth, p[3]) if p else None
        add = depth - p[3] if p else 0
        dp = pct(minp or 0, p[1] or 0) if p and p[1] else None
        ws = pct(cnt, w[2]) if w else None
        wd = pct(depth, w[3]) if w else None
        # 评分：供给收缩 + 需求扩张 同时发生；按金额加权避免便宜货刷屏
        score = 0
        if p and ds <= -SALE_DROP and dd >= DEPTH_UP and add >= MIN_ADD:
            score = (-ds) + dd + min(add / 10000, 50)
            rows_alert.append((score, tid, name, minp, dp, cnt, ds, depth, dd, add, ws, wd))
        rows_all.append((depth, name, minp, dp, cnt, ds, depth, q, dd, ws, wd, score))
    rows_alert.sort(reverse=True)

    alert_html = ""
    for i, (sc, tid, name, minp, dp, cnt, ds, depth, dd, add, ws, wd) in enumerate(rows_alert[:50], 1):
        mv = "<br>".join(movers(tid)) or "-"
        link = f"https://www.youpin898.com/market/goods-list?listType=10&templateId={tid}&gameId=730"
        alert_html += (f"<tr><td>{i}</td><td><a href='{link}' target=_blank>{E(name)}</a></td><td>{minp:g} {fmt_pct(dp)}</td>"
                       f"<td>{cnt} {fmt_pct(ds, False)}</td><td>¥{depth:,.0f} {fmt_pct(dd)}</td><td>+¥{add:,.0f}</td>"
                       f"<td>{fmt_pct(ws, False)} / {fmt_pct(wd)}</td><td><b>{sc:.0f}</b></td><td><small>{mv}</small></td></tr>")
    if not alert_html:
        alert_html = ("<tr><td colspan=9>" + ("今天没有同时满足条件的商品" if prev else
                      "这是第一次运行，没有对比数据；下次运行后才会出现异动") + "</td></tr>")

    rows = ""
    for _, name, minp, dp, cnt, ds, depth, q, dd, ws, wd, sc in sorted(rows_all, reverse=True):
        bg = " style='background:#fff3f3'" if sc else ""
        rows += (f"<tr{bg}><td>{E(name)}</td><td>{minp}</td><td>{fmt_pct(dp)}</td><td>{cnt}</td><td>{fmt_pct(ds, False)}</td>"
                 f"<td data-v='{depth}'>¥{depth:,.0f}</td><td>{q}</td><td>{fmt_pct(dd)}</td>"
                 f"<td>{fmt_pct(ws, False)}</td><td>{fmt_pct(wd)}</td></tr>")

    users = ""
    for uid, uname, amt, nitem in db.execute("""SELECT user_id, MAX(user_name), SUM(price*qty), COUNT(DISTINCT tid)
            FROM orders WHERE run=? AND valid=1 GROUP BY user_id ORDER BY 3 DESC LIMIT 100""", (run,)):
        old = db.execute("SELECT SUM(price*qty) FROM orders WHERE run=? AND user_id=? AND valid=1", (prev, uid)).fetchone()[0] if prev else None
        chg = "-" if not prev else ("<b style='color:#c00'>新上榜</b>" if not old else fmt_pct(pct(amt, old)))
        det = "<br>".join(f"{E(n)}：¥{p:g} × {q}" for n, p, q in db.execute("""
            SELECT s.name,o.price,o.qty FROM orders o JOIN snapshot s ON s.run=o.run AND s.tid=o.tid
            WHERE o.run=? AND o.user_id=? AND o.valid=1 ORDER BY o.price*o.qty DESC""", (run, uid)))
        link = f"https://hybrid.youpin898.com/index.html#/shop/{uid}"
        users += (f"<tr><td><a href='{link}' target=_blank>{E(uname)}</a><br><small>ID {uid}</small></td>"
                  f"<td data-v='{amt}'>¥{amt:,.0f}</td><td>{chg}</td><td>{nitem}</td><td><details><summary>展开</summary>{det}</details></td></tr>")

    css = ("body{font-family:sans-serif;margin:20px}table{border-collapse:collapse;margin-bottom:30px}"
           "td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px}th{background:#f0f0f0;cursor:pointer}"
           ".hint{color:#666;font-size:13px}")
    js = """<script>document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const t=th.closest('table'),i=[...th.parentNode.children].indexOf(th),
    rs=[...t.querySelectorAll('tr')].slice(1),v=r=>{const c=r.children[i];if(!c)return 0;const d=c.dataset.v??c.innerText.replace(/[¥,%+]/g,'');const n=parseFloat(d);return isNaN(n)?d:n};
    const a=th.dataset.a=th.dataset.a==='1'?'0':'1';rs.sort((x,y)=>{const p=v(x),q=v(y);return (p>q?1:p<q?-1:0)*(a==='1'?-1:1)});rs.forEach(r=>t.appendChild(r))})</script>"""
    out = (f"<meta charset=utf-8><style>{css}</style><h2>悠悠有品监控 {run}</h2>"
           f"<p class=hint>对比：上次 {prev or '无'}；7日前 {wk or '无（数据不足7天）'}。颜色：<span style='color:#c00'>红=利好方向</span>"
           f"（在售减少/求购增加/价格上涨），<span style='color:#080'>绿=反向</span>。点击表头可排序。</p>"
           f"<h3>🔥 异动榜：在售减少 + 求购增多</h3><p class=hint>条件：在售 ≤ -{SALE_DROP}%，厚度 ≥ +{DEPTH_UP}%，"
           f"且厚度增加 ≥ ¥{MIN_ADD:,}。评分 = 在售降幅 + 厚度涨幅 + 新增金额(万)。</p>"
           f"<table><tr><th>#</th><th>商品</th><th>底价</th><th>在售</th><th>求购厚度</th><th>新增厚度</th>"
           f"<th>7日 在售/厚度</th><th>评分</th><th>谁在加仓(Top3)</th></tr>{alert_html}</table>"
           f"<h3>全部商品</h3><table><tr><th>商品</th><th>底价</th><th>价格变化</th><th>在售</th><th>在售变化</th>"
           f"<th>求购厚度</th><th>有效件数</th><th>厚度变化</th><th>7日在售</th><th>7日厚度</th></tr>{rows}</table>"
           f"<h3>大佬排行（有效求购总额 Top100）</h3><table><tr><th>用户</th><th>总额</th><th>较上次</th><th>商品数</th>"
           f"<th>明细</th></tr>{users}</table>{js}")
    fn = os.path.join(DATA, "report.html")
    open(fn, "w", encoding="utf-8").write(out)
    print("报告已生成:", fn)


if __name__ == "__main__":
    main()
