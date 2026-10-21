import hashlib
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from intraday_guide import TZ,collect_prices,combine,map_items,read_demand

class Intraday(unittest.TestCase):
    def score(self,**kw):
        return {'name':'exact FN','cn':'法玛斯 | 测试 (崭新出厂)','价格日期':'2026-10-20','多因子证据分':'85','日K价':'100',
            '行动':'等待/研究观察','未满足条件':'验证未通过',**kw}

    def test_exact_wear_mapping_and_ambiguity(self):
        r=self.score();m,miss=map_items([r],{1:'法玛斯 | 测试（崭新出厂）',2:'法玛斯 | 测试 (略有磨损)'})
        self.assertEqual(m,{r['name']:1});self.assertFalse(miss)
        self.assertEqual(map_items([r],{1:r['cn'],2:r['cn']})[0],{})

    def test_quote_only_transport_and_missing_sale_count(self):
        calls=[]
        def post(path,body):
            calls.append(path);self.assertNotIn('purchase',path.lower())
            return {'Code':0,'Data':[{'id':1,'commodityName':self.score()['cn'],'price':'101.2'}]}
        p,errors,pages=collect_prices([1],{1:self.score()['cn']},post=post,pause=lambda:None)
        self.assertEqual(p[1]['price'],101.2);self.assertIsNone(p[1]['on_sale'])
        self.assertFalse(errors);self.assertEqual(pages,1);self.assertEqual(len(calls),1)

    def test_api_failure_is_missing_not_zero_price(self):
        p,errors,_=collect_prices([1],{1:self.score()['cn']},post=lambda *a:{'Code':84104},pause=lambda:None)
        self.assertEqual(p,{});self.assertTrue(errors)

    def test_daily_snapshot_reused_read_only_and_previous_day_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'yyyp.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE snapshot(run TEXT,tid INT,min_price REAL,on_sale INT,depth REAL,depth_qty INT)')
                db.execute('CREATE TABLE snapshot_meta(run TEXT,tid INT,collected_at TEXT,depth_ratio REAL)')
                db.executemany('INSERT INTO snapshot VALUES(?,?,?,?,?,?)',[
                    ('2026-10-20',1,100,300,2000,20),('2026-10-21',1,101,290,2200,22),
                    ('2026-10-21 10:00',1,102,280,2400,24)])
                for run in ['2026-10-20','2026-10-21','2026-10-21 10:00']:
                    db.execute('INSERT INTO snapshot_meta VALUES(?,?,?,?)',(run,1,run[:10]+'T10:00:00+08:00',.85))
            before=hashlib.sha256(path.read_bytes()).hexdigest();d,error=read_demand(path,[1])
            self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest());self.assertIsNone(error)
            self.assertEqual(d[1]['batch'],'2026-10-21 10:00');self.assertEqual(d[1]['previous_batch'],'2026-10-20')
            self.assertAlmostEqual(d[1]['depth_change_pct'],20.)

    def test_legacy_day_precision_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'yyyp.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE snapshot(run TEXT,tid INT,min_price REAL,on_sale INT,depth REAL,depth_qty INT)')
                db.execute("INSERT INTO snapshot VALUES('2026-10-20',1,100,300,2000,20)")
            d,_=read_demand(path,[1]);self.assertIsNone(d[1]['collected_at']);self.assertEqual(d[1]['time_precision'],'date')
            self.assertIsNone(d[1]['depth_ratio']);self.assertIsNone(d[1]['depth_change_pct'])

    def test_fresh_quote_cannot_upgrade_unvalidated_score(self):
        s=self.score();now=datetime(2026,10,21,15,tzinfo=TZ)
        q={1:{'price':105,'on_sale':280,'fetched_at':now.isoformat(),'source':'fixture'}}
        r=combine([s],{s['name']:1},q,{},now)[0]
        self.assertEqual(r['日线多因子分'],85);self.assertEqual(r['当前分类'],'等待/研究观察')
        self.assertEqual(r['报价距日K百分比'],5.000000000000004)

    def test_missing_quote_not_replaced_by_daily_price(self):
        s=self.score();r=combine([s],{s['name']:1},{},{},datetime(2026,10,21,15,tzinfo=TZ))[0]
        self.assertIsNone(r['实时挂牌价']);self.assertEqual(r['价格状态'],'缺失')

    def test_stale_score_cannot_reuse_simulation_permission(self):
        s=self.score(**{'行动':'允许前瞻模拟观察','价格日期':'2026-10-18'});now=datetime(2026,10,21,15,tzinfo=TZ)
        p={1:{'price':101,'on_sale':280,'fetched_at':now.isoformat(),'source':'fixture'}}
        d={1:{'batch':'2026-10-21','collected_at':None,'time_precision':'date','reference_price':100,'on_sale':300,
            'depth':2000,'depth_qty':20,'depth_ratio':None,'previous_batch':None,'depth_change_pct':None,'depth_comparison_note':'legacy'}}
        r=combine([s],{s['name']:1},p,d,now)[0]
        self.assertEqual(r['当前分类'],'等待/研究观察');self.assertIn('日线评分已过期',r['说明'])



class IndependentPool(unittest.TestCase):
    def base_score(self,**kw):
        return {'name':'AK-47 | Test (Factory New)','cn':'AK-47 | 测试 (崭新出厂)','价格日期':'2026-10-20',
                '多因子证据分':'70','日K价':'100','行动':'等待/研究观察',**kw}

    def test_unscored_low_price_and_short_history_stay(self):
        from intraday_guide import prepare_monitor_pool
        names={1:self.base_score()['cn'],2:'法玛斯 | 新品 (崭新出厂)',3:'新星 | 测试 (崭新出厂)'}
        audit={'asof':'2026-10-20','items':[{'name':'new FN','cn':names[2],'scoring_status':'历史不足','reasons':['不足300条有效历史日K']},
            {'name':'Nova | X','cn':names[3],'scoring_status':'研究品类排除','reasons':['研究排除但仍保留监控']}]}
        pool=prepare_monitor_pool([1,2,3],names,[self.base_score()],audit)
        rows=combine(pool,{}, {2:{'price':10,'on_sale':8,'fetched_at':'fixture','source':'fixture'}},{},datetime(2026,10,21,tzinfo=TZ))
        self.assertEqual({r['yyyp_id'] for r in rows},{1,2,3})
        young=next(r for r in rows if r['yyyp_id']==2)
        self.assertIsNone(young['日线多因子分']);self.assertEqual(young['实时挂牌价'],10)
        self.assertEqual(young['评分状态'],'历史不足');self.assertEqual(young['当前分类'],'仅监控/暂不评分')
        self.assertTrue(all(r['价格状态']=='缺失' for r in rows if r['yyyp_id'] in (1,3)))

    def test_empty_scores_and_diagnostics_cannot_reduce_pool(self):
        from intraday_guide import prepare_monitor_pool
        names={i:f'item {i}' for i in range(1,816)}
        pool=prepare_monitor_pool(list(names),names,[],{})
        rows=combine(pool,{},{},{},datetime(2026,10,21,tzinfo=TZ))
        self.assertEqual(len(rows),815);self.assertEqual({r['yyyp_id'] for r in rows},set(names))
        self.assertTrue(all(r['日线多因子分'] is None for r in rows))
        self.assertTrue(all(r['实时挂牌价'] is None for r in rows))

    def test_research_only_item_cannot_expand_or_replace_monitor_pool(self):
        from intraday_guide import prepare_monitor_pool
        score=self.base_score();pool=prepare_monitor_pool([11],{11:'other item'},[score],{})
        self.assertEqual([r['_yyyp_id'] for r in pool],[11]);self.assertIsNone(pool[0]['多因子证据分'])

    def test_duplicate_names_do_not_merge_authoritative_ids(self):
        from intraday_guide import prepare_monitor_pool
        s=self.base_score();pool=prepare_monitor_pool([1,2],{1:s['cn'],2:s['cn']},[s],{})
        rows=combine(pool,{},{},{},datetime(2026,10,21,tzinfo=TZ))
        self.assertEqual(len(rows),2);self.assertEqual({r['yyyp_id'] for r in rows},{1,2})
        self.assertTrue(all(r['日线多因子分'] is None for r in rows))


    def test_main_still_quotes_all_815_when_score_sources_fail(self):
        from unittest.mock import patch
        import contextlib,csv,io,json,os
        import intraday_guide as app
        names={i:f'item {i}' for i in range(1,816)};ids=list(names)
        prices={i:{'price':10.,'on_sale':1,'fetched_at':'fixture','source':'fixture'} for i in ids}
        def collect(target,names_arg,**kw):
            self.assertEqual(set(target),set(ids));return prices,[],1
        with tempfile.TemporaryDirectory() as td,patch.object(app,'ROOT',Path(td)),patch.dict(app.monitor.ITEM_NAMES,names,clear=True),patch.object(app.monitor,'load_items',return_value=ids),patch.object(app.monitor,'check_token'),patch.object(app,'collect_prices',side_effect=collect),patch.object(app,'read_demand',return_value=({},None)),patch.object(app.urllib.request,'urlopen',side_effect=OSError('score source unavailable')),patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':''}),contextlib.redirect_stdout(io.StringIO()):
            app.main()
            meta=json.loads((Path(td)/'data/intraday_latest.json').read_text())
            with (Path(td)/'data/intraday_scores_latest.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertEqual(meta['monitor_items'],815);self.assertEqual(meta['price_rows'],815)
            self.assertEqual(meta['report_rows'],815);self.assertEqual(meta['scored_monitor_rows'],0)
            self.assertEqual(len(rows),815);self.assertEqual({int(r['yyyp_id']) for r in rows},set(ids))
            self.assertTrue(all(r['日线多因子分']=='' for r in rows));self.assertEqual(meta['purchase_list_requests_this_run'],0)

if __name__=='__main__':unittest.main()

