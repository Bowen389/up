"""Refresh YY platform list quotes three times/day; reuse read-only daily demand.
Never calls a purchase-list endpoint, retrains a model, or places an order.
"""
import csv
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import urllib.request
import monitor

ROOT=Path(__file__).resolve().parent
TZ=timezone(timedelta(hours=8))
SCORES_URL='https://raw.githubusercontent.com/Bowen389/fable-ML/main/output/daily_multifactor_scores.csv'
VERSION='intraday-price-daily-demand-v1'


def normal(name):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(name))).casefold()


def map_items(scores,names):
    lookup={}
    for tid,name in names.items():lookup.setdefault(normal(name),[]).append(tid)
    found={};missing=[]
    for row in scores:
        ids=lookup.get(normal(row['cn']),[])
        if len(ids)==1:found[row['name']]=ids[0]
        else:missing.append({'name':row['name'],'cn':row['cn'],'reason':'exact mapping absent or ambiguous'})
    return found,missing


def collect_prices(ids,names,post=None,pause=None):
    """Only bulk market listing quotes. No purchase-list operation, including fallbacks."""
    post=post or monitor.post;pause=pause or (lambda:time.sleep(3))
    wanted=set(ids);got={};errors=[];pages=0
    keywords=['探员']+sorted({monitor.keyword_of(names[t]) for t in wanted if names.get(t)})
    for kw in dict.fromkeys(keywords):
        if kw!='探员' and not any(monitor.keyword_of(names[t])==kw for t in wanted-set(got)):continue
        for page in range(1,101):
            try:
                d=post('/api/homepage/pc/goods/market/querySaleTemplate',
                    {'gameId':'730','listType':'10','keyWords':kw,'pageIndex':page,'pageSize':200,'listSortType':0,'sortType':0})
                code=d.get('Code',d.get('code'))
                if code!=0:raise RuntimeError('quote endpoint code '+str(code))
                batch=d.get('Data')
                if not isinstance(batch,list):raise ValueError('quote payload is not a list')
                observed_at=datetime.now(TZ).isoformat(timespec='seconds');pages+=1
                for x in batch:
                    tid=int(x.get('id',0))
                    if tid not in wanted:continue
                    # An exact ID must also retain the expected full wear/quality name.
                    if normal(x.get('commodityName',''))!=normal(names[tid]):
                        errors.append({'tid':tid,'error':'exact ID/name mismatch'});continue
                    try:price=float(x['price'])
                    except (KeyError,ValueError,TypeError):continue
                    if not math.isfinite(price) or price<=0:continue
                    count=x.get('onSaleCount')
                    got[tid]={'price':price,'on_sale':int(count) if count is not None else None,'fetched_at':observed_at,
                              'source':'YYYP querySaleTemplate listing quote','exchange_tick_time':None}
                pause()
                if len(batch)<200:break
            except Exception as exc:
                errors.append({'keyword':kw,'page':page,'error':type(exc).__name__+': '+str(exc)[:160]});break
        print('PRICE',kw,len(got),'/',len(wanted),flush=True)
        if len(got)==len(wanted):break
    return got,errors,pages


def read_demand(path,ids):
    if not path.exists():return {},'daily demand database absent'
    result={}
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        has_meta=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot_meta'").fetchone() is not None
        for tid in set(ids):
            snaps=db.execute('SELECT run,min_price,on_sale,depth,depth_qty FROM snapshot WHERE tid=? ORDER BY run DESC LIMIT 1',(tid,)).fetchone()
            if not snaps:continue
            run,minp,sale,depth,qty=snaps
            meta=db.execute('SELECT collected_at,depth_ratio FROM snapshot_meta WHERE run=? AND tid=?',(run,tid)).fetchone() if has_meta else None
            prev=db.execute('SELECT run,min_price,on_sale,depth,depth_qty FROM snapshot WHERE tid=? AND substr(run,1,10)<? ORDER BY run DESC LIMIT 1',(tid,run[:10])).fetchone()
            oldmeta=db.execute('SELECT collected_at,depth_ratio FROM snapshot_meta WHERE run=? AND tid=?',(prev[0],tid)).fetchone() if has_meta and prev else None
            comparable=bool(meta and oldmeta and meta[1] is not None and oldmeta[1] is not None and abs(meta[1]-oldmeta[1])<1e-9)
            result[tid]={'batch':run,'collected_at':meta[0] if meta else None,'time_precision':'second' if meta else ('minute' if len(run)>10 else 'date'),
                'reference_price':minp,'on_sale':sale,'depth':depth,'depth_qty':qty,'depth_ratio':meta[1] if meta else None,
                'previous_batch':prev[0] if prev else None,
                'depth_change_pct':(depth/prev[3]-1)*100 if comparable and prev[3] else None,
                'depth_comparison_note':'相同阈值比例；参考价变化仍会影响厚度，不等于净流入' if comparable else '旧批次阈值未记录或不可比，不计算厚度变化'}
    return result,None


def combine(scores,mapping,prices,demand,now):
    rows=[]
    for s in scores:
        tid=mapping.get(s['name']);p=prices.get(tid);q=demand.get(tid)
        gap=(p['price']/float(s['日K价'])-1)*100 if p and float(s['日K价'])>0 else None
        age=(now.date()-datetime.fromisoformat(q['batch'][:10]).date()).days if q else None
        score_age=(now.date()-datetime.fromisoformat(s['价格日期']).date()).days
        status='等待/研究观察';notes=[]
        if score_age>1:notes.append('日线评分已过期，不能沿用买入资格')
        if not p:notes.append('本轮报价缺失，不以旧价格冒充实时价')
        if age is None:notes.append('求购快照缺失')
        elif age>1:notes.append('求购快照超过上一日，待每日任务更新')
        if gap is not None and abs(gap)>10:notes.append('报价偏离日K超过10%，需复核形态')
        if s.get('行动')=='允许前瞻模拟观察' and p and age is not None and age<=1 and score_age<=1 and gap is not None and abs(gap)<=10:
            status='沿用日线前瞻模拟观察资格，非实际买单'
        notes.extend(x for x in [s.get('未满足条件',''),'求购只复用已有批次，本轮未请求求购列表'] if x)
        rows.append({'name':s['name'],'商品':s['cn'],'yyyp_id':tid,'日线数据日期':s['价格日期'],
            '日线多因子分':float(s['多因子证据分']),'原始日线行动':s.get('行动'),'当前分类':status,
            '实时挂牌价':p['price'] if p else None,'价格采集时间':p['fetched_at'] if p else None,
            '报价来源':p['source'] if p else None,'价格状态':'本轮已刷新' if p else '缺失',
            '日K参考价':float(s['日K价']),'报价距日K百分比':gap,
            '当前在售数':p['on_sale'] if p else None,'求购批次':q['batch'] if q else None,
            '求购采集时间':q['collected_at'] if q else None,'求购时间精度':q['time_precision'] if q else None,
            '求购批次距今日天数':age,'求购厚度':q['depth'] if q else None,'求购件数':q['depth_qty'] if q else None,
            '求购参考价':q['reference_price'] if q else None,'厚度阈值比例':q['depth_ratio'] if q else None,
            '上个可比日期批次':q['previous_batch'] if q else None,'求购厚度变化百分比':q['depth_change_pct'] if q else None,
            '求购口径说明':q['depth_comparison_note'] if q else '缺失','主要日线理由':s.get('主要证据',''),
            '市场状态':s.get('市场状态'),'板块代理':s.get('板块代理'),'环境放行':s.get('环境放行'),
            '说明':'；'.join(notes)})
    return sorted(rows,key=lambda r:(r['价格状态']!='本轮已刷新',-r['日线多因子分'],r['name']))


def main():
    began=datetime.now(TZ);out=ROOT/'data';out.mkdir(exist_ok=True)
    with urllib.request.urlopen(SCORES_URL,timeout=60) as response:payload=response.read()
    scores=list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
    if not scores:raise RuntimeError('Daily multifactor score table is empty; no quote refresh performed')
    monitor.load_items();mapping,missing=map_items(scores,monitor.ITEM_NAMES)
    if not mapping:raise RuntimeError('No exact name/quality mappings; refusing broad or fuzzy replacements')
    monitor.check_token()
    # Bound individual retry waits for this shallow task; no change to the full daily collector.
    def quote_post(path,body):return monitor.post(path,body,tries=2)
    prices,errors,pages=collect_prices(mapping.values(),monitor.ITEM_NAMES,post=quote_post)
    dbpath=out/'yyyp.db';before=hashlib.sha256(dbpath.read_bytes()).hexdigest() if dbpath.exists() else None
    demand,demand_error=read_demand(dbpath,mapping.values())
    after=hashlib.sha256(dbpath.read_bytes()).hexdigest() if dbpath.exists() else None
    if before!=after:raise RuntimeError('Read-only daily demand database changed unexpectedly')
    now=datetime.now(TZ);rows=combine(scores,mapping,prices,demand,now)
    coverage=len(prices)/len(mapping)
    manifest={'version':VERSION,'run_id':os.environ.get('GITHUB_RUN_ID'),'started_at':began.isoformat(),'generated_at':now.isoformat(),'scores_url':SCORES_URL,
        'scores_sha256':hashlib.sha256(payload).hexdigest(),'score_dates':sorted({s['价格日期'] for s in scores}),
        'score_rows':len(scores),'exact_mapped':len(mapping),'price_rows':len(prices),'price_coverage_mapped':coverage,
        'demand_rows':len(demand),'demand_batches':sorted({q['batch'] for q in demand.values()}),
        'purchase_list_requests_this_run':0,'price_pages':pages,'unmapped':missing,'price_errors':errors,
        'demand_error':demand_error,'demand_database_unchanged':before==after,'demand_db_sha256':before,
        'status':'success' if coverage==1 and not missing and not errors and demand_error is None else 'partial' if prices else 'failed',
        'notes':['仅刷新挂牌报价与在售数，不代表已成交价格；接口未提供逐笔行情时间',
            '多因子分来自完整日K，盘中报价不倒填日K、不重新训练或伪造盘中分数',
            '求购沿用每日数据库，采集时间与报价时间分别展示；历史批次没有秒级时间时不补造',
            '求购厚度不按本轮新价格重算，实时价格变化不能当成真实需求流入',
            '不自动下单；原版规则或多因子未通过资格时不会因本轮报价或求购而升级为买入']}
    with (out/'intraday_scores_latest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (out/'intraday_latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    fmt=lambda v:'缺失' if v is None else f'{v:,.2f}'
    md=['# 盘中交易参考：三次报价更新 / 每日一次求购','',f'生成时间：{now.isoformat(timespec="seconds")}',
        f'日线评分日期：{", ".join(manifest["score_dates"])}；商品{len(scores)}件，精确映射{len(mapping)}件，本轮报价{len(prices)}件。',
        f'本轮求购列表请求：0；复用求购批次：{", ".join(manifest["demand_batches"]) or "缺失"}。','',
        '日线分数保留原资格；挂牌报价仅用于更新参考和识别价格偏离，不代表成交。完整时间和缺失项见CSV。','',
        '| 商品 | 日线分 | 本轮挂牌价 | 报价距日K | 求购批次 | 求购厚度 | 当前分类 |','|---|---:|---:|---:|---|---:|---|']
    for r in rows[:25]:md.append(f'| {r["商品"].replace("|","/")} | {r["日线多因子分"]:.1f} | {fmt(r["实时挂牌价"])} | {fmt(r["报价距日K百分比"])}% | {r["求购批次"] or "缺失"} | {fmt(r["求购厚度"])} | {r["当前分类"]} |')
    md+=['','## 数据口径',*['- '+n for n in manifest['notes']],
        f'- 未映射{len(missing)}件；价格错误{len(errors)}项；求购读取错误：{demand_error or "无"}。',
        '- 明细：data/intraday_scores_latest.csv；状态和覆盖率：data/intraday_latest.json。']
    text='\n'.join(md);(out/'intraday_latest.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write(text)
    print('INTRADAY_COMPLETE',json.dumps({k:manifest[k] for k in ['status','score_rows','exact_mapped','price_rows','demand_rows','purchase_list_requests_this_run']},ensure_ascii=False),flush=True)
    if coverage<.8:raise RuntimeError('Quote coverage below 80%; partial report preserved, not a zero-signal success')

if __name__=='__main__':main()
