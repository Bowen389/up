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
SCORE_AUDIT_URL='https://raw.githubusercontent.com/Bowen389/fable-ML/main/output/score_pool_status.json'
VERSION='independent-monitor-pool-v2'


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


def number(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):return None


def prepare_monitor_pool(ids,names,scores,audit):
    """One record per authoritative YY ID, regardless of research eligibility or failures."""
    score_lookup={};audit_lookup={};name_counts={}
    for tid in ids:name_counts[normal(names[tid])]=name_counts.get(normal(names[tid]),0)+1
    for x in scores:
        if x.get('cn'):score_lookup.setdefault(normal(x['cn']),[]).append(x)
    for x in audit.get('items',[]):
        if x.get('cn'):audit_lookup.setdefault(normal(x['cn']),[]).append(x)
    pool=[]
    for tid in ids:
        key=normal(names[tid]);matches=score_lookup.get(key,[]);details=audit_lookup.get(key,[])
        info=details[0] if len(details)==1 else {}
        matched=len(matches)==1 and name_counts[key]==1 and number(matches[0].get('多因子证据分')) is not None
        if matched:
            row=dict(matches[0]);row['_scoring_status']='可评分';row['_unscored_reason']=''
        else:
            reason='；'.join(info.get('reasons',[])) or '当前评分表无记录，具体研究资格待诊断'
            if name_counts[key]>1 or len(matches)>1:reason='名称匹配有歧义，保留监控但不附加评分'
            row={'name':info.get('name','yyyp:'+str(tid)),'cn':names[tid],'价格日期':info.get('last_bar_date'),
                '多因子证据分':None,'日K价':info.get('last_close'),'行动':'未评分/仅监控','未满足条件':reason,
                '_scoring_status':info.get('scoring_status','暂无评分/待诊断'),'_unscored_reason':reason}
        row['_yyyp_id']=tid;row['_score_matched']=matched;row['_audit_asof']=audit.get('asof')
        pool.append(row)
    if len(pool)!=len(set(ids)) or {r['_yyyp_id'] for r in pool}!=set(ids):raise AssertionError('Monitoring universe lost IDs')
    return pool


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
        tid=s.get('_yyyp_id',mapping.get(s['name']));p=prices.get(tid);q=demand.get(tid)
        reference=number(s.get('日K价'));score=number(s.get('多因子证据分'))
        gap=(p['price']/reference-1)*100 if p and reference is not None and reference>0 else None
        age=(now.date()-datetime.fromisoformat(q['batch'][:10]).date()).days if q else None
        score_age=(now.date()-datetime.fromisoformat(s['价格日期']).date()).days if s.get('价格日期') else None
        status='等待/研究观察' if score is not None else '仅监控/暂不评分';notes=[]
        if score is None:notes.append(s.get('_unscored_reason','暂无有效评分'))
        if score_age is not None and score_age>1:notes.append('日线评分已过期，不能沿用买入资格')
        if not p:notes.append('本轮报价缺失，不以旧价格冒充实时价')
        if age is None:notes.append('求购快照缺失')
        elif age>1:notes.append('求购快照超过上一日，待每日任务更新')
        if gap is not None and abs(gap)>10:notes.append('报价偏离日K超过10%，需复核形态')
        if score is not None and s.get('行动')=='允许前瞻模拟观察' and p and age is not None and age<=1 and score_age is not None and score_age<=1 and gap is not None and abs(gap)<=10:
            status='沿用日线前瞻模拟观察资格，非实际买单'
        notes.extend(x for x in [s.get('未满足条件',''),'求购只复用已有批次，本轮未请求求购列表'] if x)
        rows.append({'name':s['name'],'商品':s['cn'],'yyyp_id':tid,'日线数据日期':s['价格日期'],
            '日线多因子分':score,'评分状态':s.get('_scoring_status','可评分' if score is not None else '暂无评分'),
            '未评分原因':s.get('_unscored_reason',''),'评分诊断日期':s.get('_audit_asof'),'原始日线行动':s.get('行动'),'当前分类':status,
            '实时挂牌价':p['price'] if p else None,'价格采集时间':p['fetched_at'] if p else None,
            '报价来源':p['source'] if p else None,'价格状态':'本轮已刷新' if p else '缺失',
            '日K参考价':reference,'报价距日K百分比':gap,
            '当前在售数':p['on_sale'] if p else None,'求购批次':q['batch'] if q else None,
            '求购采集时间':q['collected_at'] if q else None,'求购时间精度':q['time_precision'] if q else None,
            '求购批次距今日天数':age,'求购厚度':q['depth'] if q else None,'求购件数':q['depth_qty'] if q else None,
            '求购参考价':q['reference_price'] if q else None,'厚度阈值比例':q['depth_ratio'] if q else None,
            '上个可比日期批次':q['previous_batch'] if q else None,'求购厚度变化百分比':q['depth_change_pct'] if q else None,
            '求购口径说明':q['depth_comparison_note'] if q else '缺失','主要日线理由':s.get('主要证据',''),
            '市场状态':s.get('市场状态'),'板块代理':s.get('板块代理'),'环境放行':s.get('环境放行'),
            '说明':'；'.join(notes)})
    return sorted(rows,key=lambda r:(r['价格状态']!='本轮已刷新',-(r['日线多因子分'] if r['日线多因子分'] is not None else -1),r['yyyp_id']))


def main():
    began=datetime.now(TZ);out=ROOT/'data';out.mkdir(exist_ok=True)
    ids=monitor.load_items()
    if not ids:raise RuntimeError('Authoritative items.txt monitoring list is empty')
    print('MONITOR_POOL',len(ids),'independent of scoring coverage',flush=True)
    monitor.check_token()
    def quote_post(path,body):return monitor.post(path,body,tries=2)
    prices,errors,pages=collect_prices(ids,monitor.ITEM_NAMES,post=quote_post)
    # A broken/empty score feed cannot stop price monitoring or remove an item.
    scores=[];audit={};payload=b'';score_error=None;audit_error=None
    try:
        with urllib.request.urlopen(SCORES_URL,timeout=60) as response:payload=response.read()
        scores=list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
        required={'name','cn','价格日期','日K价','多因子证据分'}
        if not scores or not required.issubset(scores[0]):raise ValueError('Score feed empty or schema changed')
    except Exception as exc:
        scores=[];score_error=type(exc).__name__+': '+str(exc)[:160]
    try:
        with urllib.request.urlopen(SCORE_AUDIT_URL,timeout=60) as response:audit=json.load(response)
        if not isinstance(audit.get('items'),list):raise ValueError('Eligibility diagnostics schema changed')
    except Exception as exc:
        audit={};audit_error=type(exc).__name__+': '+str(exc)[:160]
    pool=prepare_monitor_pool(ids,monitor.ITEM_NAMES,scores,audit)
    score_mapping,unmapped_scores=map_items(scores,monitor.ITEM_NAMES)
    dbpath=out/'yyyp.db';before=hashlib.sha256(dbpath.read_bytes()).hexdigest() if dbpath.exists() else None
    demand,demand_error=read_demand(dbpath,ids)
    after=hashlib.sha256(dbpath.read_bytes()).hexdigest() if dbpath.exists() else None
    if before!=after:raise RuntimeError('Read-only daily demand database changed unexpectedly')
    now=datetime.now(TZ);rows=combine(pool,{},prices,demand,now)
    if len(rows)!=len(ids) or {r['yyyp_id'] for r in rows}!=set(ids):raise AssertionError('Report must retain every monitoring ID')
    for row in rows:
        if row['实时挂牌价'] is None:
            reasons=[e['error'] for e in errors if e.get('tid')==row['yyyp_id']]
            kw=monitor.keyword_of(monitor.ITEM_NAMES[row['yyyp_id']])
            reasons.extend(e['error'] for e in errors if e.get('keyword')==kw)
            row['报价缺失原因']='；'.join(dict.fromkeys(reasons)) or '本轮未返回有效报价，可能无在售或请求不完整，原因未确认'
        else:row['报价缺失原因']=''
    coverage=len(prices)/len(ids);scored=sum(r['日线多因子分'] is not None for r in rows)
    counts={}
    for r in rows:counts[r['评分状态']]=counts.get(r['评分状态'],0)+1
    manifest={'version':VERSION,'run_id':os.environ.get('GITHUB_RUN_ID'),'started_at':began.isoformat(),'generated_at':now.isoformat(),
        'monitor_universe':'items.txt authoritative YY template IDs','monitor_items':len(ids),'report_rows':len(rows),
        'score_rows':len(scores),'scored_monitor_rows':scored,'unscored_monitor_rows':len(rows)-scored,'scoring_status_counts':counts,
        'scores_url':SCORES_URL,'scores_sha256':hashlib.sha256(payload).hexdigest() if payload else None,
        'score_dates':sorted({s['价格日期'] for s in scores if s.get('价格日期')}),'score_feed_error':score_error,
        'score_audit_url':SCORE_AUDIT_URL,'score_audit_asof':audit.get('asof'),'score_audit_error':audit_error,
        'exact_mapped':len(score_mapping),'unmapped_research_scores':unmapped_scores,
        'price_attempted':len(ids),'price_rows':len(prices),'price_missing_rows':len(ids)-len(prices),'price_coverage_monitor':coverage,
        'demand_rows':len(demand),'demand_batches':sorted({q['batch'] for q in demand.values()}),
        'purchase_list_requests_this_run':0,'price_pages':pages,'price_errors':errors,'demand_error':demand_error,
        'demand_database_unchanged':before==after,'demand_db_sha256':before,
        'status':'success' if coverage==1 and not errors and demand_error is None and not score_error and not audit_error else 'partial' if prices else 'failed',
        'notes':['监控池来自items.txt，报价尝试和报告行不受评分表、研究排除品类、价格门槛或历史长度影响',
            '无评分保留为空，不记0分；历史不足、低于研究价格门槛及其他不评分原因单列',
            '仅刷新挂牌报价与在售数，不代表已成交价格；接口未提供逐笔行情时间',
            '日线评分只作附加信息，评分源异常也不阻断全池报价；盘中不倒填日K、不重新训练',
            '求购沿用每日数据库，采集时间与报价时间分别展示；旧批次不补造秒级时间',
            '求购厚度不按本轮新价格重算；不自动下单，不因报价或旧求购提高未经验证的买入资格']}
    with (out/'intraday_scores_latest.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (out/'intraday_latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    fmt=lambda v:'—' if v is None else f'{v:,.2f}'
    md=['# 盘中交易参考：独立全池监控 / 每日评分附加','',f'生成时间：{now.isoformat(timespec="seconds")}',
        f'监控名单{len(ids)}件；本报告保留{len(rows)}件；本轮报价{len(prices)}件，缺报价{len(ids)-len(prices)}件。',
        f'已附加评分{scored}件，未评分{len(rows)-scored}件；评分日期：{", ".join(manifest["score_dates"]) or "缺失"}。',
        f'评分状态统计：{json.dumps(counts,ensure_ascii=False)}。',
        f'本轮求购列表请求：0；复用求购批次：{", ".join(manifest["demand_batches"]) or "缺失"}。','',
        f'完整{len(ids)}件名单以CSV为准；下表仅展示前25行，不代表监控池缩减。缺失分数和缺失报价不会删掉商品。','',
        '| 商品 | 日线分 | 评分状态 | 本轮挂牌价 | 求购批次 | 当前分类 |','|---|---:|---|---:|---|---|']
    for r in rows[:25]:md.append(f'| {r["商品"].replace("|","/")} | {fmt(r["日线多因子分"])} | {r["评分状态"]} | {fmt(r["实时挂牌价"])} | {r["求购批次"] or "缺失"} | {r["当前分类"]} |')
    md+=['','## 未评分商品示例（仍在监控）','','| 商品 | 评分状态 | 原因 | 本轮报价 |','|---|---|---|---:|']
    for r in [r for r in rows if r['日线多因子分'] is None][:15]:md.append(f'| {r["商品"].replace("|","/")} | {r["评分状态"]} | {r["未评分原因"]} | {fmt(r["实时挂牌价"])} |')
    md+=['','## 数据口径',*['- '+n for n in manifest['notes']],
        f'- 评分读取错误：{score_error or "无"}；资格诊断读取错误：{audit_error or "无"}；求购读取错误：{demand_error or "无"}。',
        '- 明细：data/intraday_scores_latest.csv；监控覆盖率与请求数：data/intraday_latest.json。']
    text='\n'.join(md);(out/'intraday_latest.md').write_text(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write(text)
    print('INTRADAY_COMPLETE',json.dumps({k:manifest[k] for k in ['status','monitor_items','report_rows','scored_monitor_rows','unscored_monitor_rows','price_rows','demand_rows','purchase_list_requests_this_run']},ensure_ascii=False),flush=True)
    if coverage<.8:raise RuntimeError('Quote coverage below 80%; all monitoring rows retained with missing status')

if __name__=='__main__':main()
