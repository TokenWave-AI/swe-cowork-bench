#!/usr/bin/env python3
"""Stage task images from the shared pull-through cache ahead of dispatch, and
evict images of finished tasks when disk is low. Never touches images of
pending/running tasks. Images keep their harbor.tokenwave.us digest reference,
so the runtime's ensure_image() finds them present and skips its own pull."""
import glob,json,os,shutil,subprocess,time
ROWMAP=json.load(open('/usr/local/etc/rowmap.json'))
LOOKAHEAD=int(os.environ.get('PREFETCH_LOOKAHEAD','30'))
LOW,HIGH=14,20
LOG='/var/log/prefetch.log'
TERMINAL_DONE={'finished','agent_error','cancelled'}
def log(msg):
    with open(LOG,'a') as f: f.write(time.strftime('%FT%TZ ',time.gmtime())+msg+'\n')
def sh(*a,timeout=None):
    return subprocess.run(a,capture_output=True,text=True,timeout=timeout)
def present(ref): return sh('docker','image','inspect',ref).returncode==0
def free_gib(): return shutil.disk_usage('/').free/2**30
def root():
    r=sorted(glob.glob('/data2/cowork/*/'))
    return r[0].rstrip('/') if r else None
def phases(R):
    out={}
    for f in glob.glob(R+'/jobs/*-cowork/STATUS.json'):
        try: out[int(os.path.basename(os.path.dirname(f))[:3])]=json.load(open(f)).get('phase')
        except Exception: pass
    return out
def fetch(row):
    info=ROWMAP[str(row)]
    for ref in info['images']:
        if present(ref): continue
        cref=ref.replace('harbor.tokenwave.us',info['cache'])
        t=time.time()
        for i in range(2):
            try: r=sh('docker','pull','-q',cref,timeout=600)
            except subprocess.TimeoutExpired: r=None
            if r is not None and r.returncode==0: break
            log(f'cache miss/slow {row} {cref.split("/")[-1][:32]}; will retry later')
            time.sleep(5)
        else:
            continue
        r=sh('docker','pull','-q',ref,timeout=1800)   # manifest/config only; layers already local
        sh('docker','rmi',cref)
        log(f'staged {row} {ref.split("/")[-1][:40]} {round(time.time()-t)}s ok={r.returncode==0}')
def evict(R,ph):
    if free_gib()>=LOW: return
    used=set(sh('docker','ps','-a','--format','{{.Image}}').stdout.split())
    keep=set()
    for row,info in ROWMAP.items():
        if ph.get(int(row)) not in TERMINAL_DONE: keep.update(info['images'])
    for row,info in ROWMAP.items():
        if ph.get(int(row)) in TERMINAL_DONE:
            for ref in info['images']:
                if free_gib()>=HIGH: return
                if ref in keep or ref in used or not present(ref): continue
                if sh('docker','rmi',ref).returncode==0: log(f'evicted {row} {ref.split("/")[-1][:40]} free={free_gib():.1f}G')
    sh('docker','image','prune','-f')
while True:
    R=root()
    if R:
        ph=phases(R)
        evict(R,ph)
        # upcoming: rows not yet started, plus env-failed rows (they will be retried)
        cfg=json.load(open(R+'/CONFIG.json')); rows=sorted(t['row'] for t in cfg['tasks'])
        # rows the harness is preparing right now first (it is pulling them from harbor), then the rest in order
        want=[r for r in rows if ph.get(r)=='preparing']+[r for r in rows if ph.get(r) in (None,'environment_error','recovery_error','grading_error')]
        staged=0
        for r in want:
            if staged>=LOOKAHEAD or free_gib()<LOW: break
            if all(present(x) for x in ROWMAP[str(r)]['images']): staged+=1; continue
            fetch(r); staged+=1
    time.sleep(30)
