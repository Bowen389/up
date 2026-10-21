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

if __name__=='__main__':unittest.main()
