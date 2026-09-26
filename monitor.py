"""悠悠有品 求购厚度 / 大佬监控  —  每晚运行一次: python monitor.py"""
import json, time, random, sqlite3, datetime, html, sys, os
import requests

BASE = "https://pc-api.youpin898.com"
DIR = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.environ.get("YYYP_CONFIG", os.path.join(DIR, "config.json")), encoding="utf-8"))

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
    db = sqlite3.connect(os.path.join(DIR, "yyyp.db"))
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

def main():
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
    prev = db.execute("SELECT MAX(run) FROM snapshot WHERE run<?", (run,)).fetchone()[0]
    P = {r[0]: r[1:] for r in db.execute("SELECT tid,on_sale,depth FROM snapshot WHERE run=?", (prev,))}
    E = html.escape
    rows = ""
    for tid, name, minp, cnt, depth, q in db.execute(
            "SELECT tid,name,min_price,on_sale,depth,depth_qty FROM snapshot WHERE run=? ORDER BY depth DESC", (run,)):
        pc, pd = P.get(tid, (None, None))
        dc = f"{cnt - pc:+d}" if pc is not None else "-"
        dd = f"{depth - pd:+,.0f}" if pd is not None else "-"
        rows += f"<tr><td>{E(name)}</td><td>{minp}</td><td>{cnt}</td><td>{dc}</td><td>¥{depth:,.0f}</td><td>{q}</td><td>{dd}</td></tr>"
    users = ""
    for uid, uname, amt, nitem in db.execute("""SELECT user_id, MAX(user_name), SUM(price*qty), COUNT(DISTINCT tid)
            FROM orders WHERE run=? AND valid=1 GROUP BY user_id ORDER BY 3 DESC LIMIT 100""", (run,)):
        det = "<br>".join(f"{E(n)}：¥{p:g} × {q}" for n, p, q in db.execute("""
            SELECT s.name,o.price,o.qty FROM orders o JOIN snapshot s ON s.run=o.run AND s.tid=o.tid
            WHERE o.run=? AND o.user_id=? AND o.valid=1 ORDER BY o.price*o.qty DESC""", (run, uid)))
        link = f"https://hybrid.youpin898.com/index.html#/shop/{uid}"
        users += (f"<tr><td><a href='{link}' target=_blank>{E(uname)}</a><br><small>ID {uid}</small></td>"
                  f"<td>¥{amt:,.0f}</td><td>{nitem}</td><td><details><summary>展开</summary>{det}</details></td></tr>")
    css = ("body{font-family:sans-serif;margin:20px}table{border-collapse:collapse;margin-bottom:30px}"
           "td,th{border:1px solid #ccc;padding:4px 8px;font-size:14px}th{background:#f0f0f0}")
    out = (f"<meta charset=utf-8><style>{css}</style><h2>悠悠有品监控 {run}（对比 {prev or '无'}）</h2>"
           f"<h3>商品求购厚度（出价 ≥ 底价×{cfg.get('ratio',0.85)}）</h3><table><tr><th>商品</th><th>底价</th>"
           f"<th>在售</th><th>在售变化</th><th>求购厚度</th><th>有效件数</th><th>厚度变化</th></tr>{rows}</table>"
           f"<h3>大佬排行（有效求购总额 Top100）</h3><table><tr><th>用户</th><th>总额</th><th>商品数</th>"
           f"<th>明细</th></tr>{users}</table>")
    fn = os.path.join(DIR, "report.html")
    open(fn, "w", encoding="utf-8").write(out)
    print("报告已生成:", fn)

if __name__ == "__main__":
    main()
