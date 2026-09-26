"""把筛选名单 CSV 转成悠悠 templateId，生成 items.txt（只在名单变化时运行）
用法: python build_items.py 名单.csv
CSV 需包含「中文名」或「名称」列，例如: AK-47 | 皇后 (崭新出厂)"""
import csv, sys, time, random, re, os
from monitor import post, DIR

norm = lambda s: re.sub(r"\s", "", s).replace("CZ75自动型", "CZ75")
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig")))
col = "中文名" if "中文名" in rows[0] else "名称"
names = list(dict.fromkeys(r[col].strip() for r in rows if r[col].strip()))
want = {norm(n): n for n in names}
found = {}

def search(kw):
    page = 1
    while True:
        d = post("/api/homepage/pc/goods/market/querySaleTemplate",
                 {"gameId": "730", "listType": "10", "keyWords": kw, "pageIndex": page,
                  "pageSize": 200, "listSortType": 0, "sortType": 0})
        lst = d.get("Data") or []
        for x in lst:
            k = norm(x["commodityName"])
            if k in want: found[want[k]] = x["id"]
        if len(lst) < 200: return
        page += 1; time.sleep(random.uniform(3, 5))

def kw_of(n):
    k = re.sub(r"（.*?）", "", n.split("|")[0]).strip()
    return {"CZ75自动型": "CZ75"}.get(k, k)

kws = ["探员"] + sorted({kw_of(n) for n in names})
for i, kw in enumerate(kws, 1):
    if all(n in found for n in names): break
    if kw != "探员" and all(n in found for n in names if kw_of(n) == kw): continue
    search(kw); print(f"💓 [{i}/{len(kws)}] {kw}  已匹配 {len(found)}/{len(names)}", flush=True)
    time.sleep(random.uniform(3, 5))

miss = [n for n in names if n not in found]
for n in list(miss):   # 最后按皮肤名单独补搜
    part = n.split("|")[-1].split("(")[0].strip()
    if part: search(part); time.sleep(random.uniform(3, 5))
miss = [n for n in names if n not in found]

with open(os.path.join(DIR, "items.txt"), "w", encoding="utf-8") as f:
    for n in names:
        if n in found: f.write(f"{found[n]}  # {n}\n")
open(os.path.join(DIR, "items_missing.txt"), "w", encoding="utf-8").write("\n".join(miss))
print(f"完成：匹配 {len(names)-len(miss)}，未匹配 {len(miss)}（见 items_missing.txt）")
