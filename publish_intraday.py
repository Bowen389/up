"""Publish three report files, rebasing the Git tree on the latest head on retry."""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parent

def main():
    paths=['data/intraday_latest.json','data/intraday_latest.md','data/intraday_scores_latest.csv']
    if not all((ROOT/p).exists() for p in paths):
        print('No complete current report files; nothing published');return
    manifest=json.loads((ROOT/paths[0]).read_text())
    if os.environ.get('GITHUB_RUN_ID') and str(manifest.get('run_id'))!=os.environ['GITHUB_RUN_ID']:
        print('Existing report belongs to an older run; refusing to republish as current');return
    repo=os.environ['GITHUB_REPOSITORY'];token=os.environ['GITHUB_TOKEN']
    def api(path,data=None,method=None):
        req=urllib.request.Request('https://api.github.com/repos/'+repo+path,
            data=None if data is None else json.dumps(data).encode(),method=method,
            headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=45) as r:return json.load(r)
    entries=[{'path':p,'mode':'100644','type':'blob','content':(ROOT/p).read_text(encoding='utf-8-sig')} for p in paths]
    for attempt in range(3):
        try:
            head=api('/git/ref/heads/main')['object']['sha'];base=api('/git/commits/'+head)['tree']['sha']
            tree=api('/git/trees',{'base_tree':base,'tree':entries})['sha']
            sha=api('/git/commits',{'message':'Intraday quote refresh '+manifest['generated_at']+' ('+manifest['status']+')','tree':tree,'parents':[head]})['sha']
            api('/git/refs/heads/main',{'sha':sha,'force':False},method='PATCH')
            print('Published quote report',sha,'purchase queries',manifest['purchase_list_requests_this_run']);return
        except (urllib.error.URLError,TimeoutError):
            if attempt==2:raise
            time.sleep(5)

if __name__=='__main__':main()
