#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private file-spool bridge from Jenkins; no HTTP execution endpoint."""
import glob
import json
import os
import re
import time
import core

PATTERN=re.compile(r'^(\d{8}-\d{6}-b([1-9]\d{0,9}))\.(req|work)$')


def handle(path):
    name=os.path.basename(path);match=PATTERN.match(name)
    if not match or os.path.islink(path): return
    run_id,build=match.group(1),match.group(2)
    result=os.path.join(core.SPOOL,run_id+'.result')
    if path.endswith('.req'):
        work=path[:-4]+'.work'
        try:os.replace(path,work)
        except FileNotFoundError:return
        path=work
    try:
        if os.path.getsize(path)>512:raise ValueError('queue request too large')
        with open(path) as f:data=json.load(f)
        if not isinstance(data,dict) or set(data)!={'build','trigger'} or str(data['build'])!=build or data['trigger'] not in ('manual','schedule','jenkins'):
            raise ValueError('invalid queue request')
        report=core.execute(run_id=run_id,build=build,trigger=data['trigger'])
        code=1 if report['status']=='failed' else 0
        message='INSPECT_STATUS=%s passed=%s failed=%s total=%s'%(report['status'],report['summary']['passed'],report['summary']['failed'],report['summary']['total'])
    except Exception as exc:
        code=2;message='INSPECT_WORKER_ERROR='+type(exc).__name__
    tmp=result+'.tmp'
    with open(tmp,'w') as f:f.write('exit_code=%s\nreport_id=%s\n%s\n'%(code,run_id,message))
    os.chmod(tmp,0o600);os.replace(tmp,result)
    os.unlink(path)
    print(message,run_id,flush=True)


def main():
    core.init()
    print('inspect worker started: private spool, no network listener',flush=True)
    while True:
        paths=sorted(glob.glob(os.path.join(core.SPOOL,'*.work'))+glob.glob(os.path.join(core.SPOOL,'*.req')))
        for path in paths[:3]:handle(path)
        time.sleep(0.5)


if __name__=='__main__':main()
