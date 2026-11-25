#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Configuration UI ONLY on loopback, reached via an authenticated SSH tunnel."""
import json
import os
import re
from urllib.parse import urlsplit
import core
from app import Handler, ThreadingHTTPServer

STATIC=os.path.join(os.path.dirname(os.path.abspath(__file__)),'admin-static')


class AdminHandler(Handler):
    def _allowed(self,write=False):
        host=(self.headers.get('Host') or '').lower()
        if self.client_address[0] not in ('127.0.0.1','::1') or not re.match(r'^(?:(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?|\[::1\](?::[0-9]{1,5})?)$',host):
            self._send(403,{'ok':False,'error':'仅限 SSH 隧道内的本机管理入口'});return False
        origin=self.headers.get('Origin')
        try:
            invalid_origin=bool(origin and (urlsplit(origin).scheme!='http' or urlsplit(origin).netloc.lower()!=host))
        except ValueError:
            invalid_origin=True
        if invalid_origin:
            self._send(403,{'ok':False,'error':'拒绝跨站管理请求'});return False
        if write and (self.headers.get('X-QA-Admin')!='1' or (self.headers.get('Content-Type') or '').split(';')[0]!='application/json'):
            self._send(403,{'ok':False,'error':'缺少管理请求保护头'});return False
        return True

    def do_GET(self):
        if not self._allowed():return
        path=urlsplit(self.path).path
        if path=='/api/tasks':self._send(200,{'ok':True,'data':core.tasks()});return
        if path=='/api/events':self._send(200,{'ok':True,'data':core.events()});return
        file={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/admin.css':'admin.css','/styles.css':None}.get(path,'')
        if file is None:
            target=os.path.join(os.path.dirname(STATIC),'static','styles.css')
        elif file:target=os.path.join(STATIC,file)
        else:self._send(404,{'ok':False,'error':'接口不存在'});return
        with open(target,'rb') as f:data=f.read()
        ctype='text/css; charset=utf-8' if path.endswith('.css') else 'application/javascript; charset=utf-8' if path.endswith('.js') else 'text/html; charset=utf-8'
        self._send(200,data,ctype)

    def do_POST(self):
        if not self._allowed(True):return
        path=urlsplit(self.path).path
        match=re.match(r'^/api/tasks(?:/(custom-[a-f0-9]{12}|home|resume|ops-hub|ai-test|games|qa-health|jenkins-login))?$',path)
        if not match:self._send(404,{'ok':False,'error':'接口不存在'});return
        try:
            length=int(self.headers.get('Content-Length') or 0)
            if not 0<length<=8192:raise core.ValidationError('请求体上限为 8 KiB')
            payload=json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(payload,dict):raise core.ValidationError('配置必须为 JSON 对象')
            task=core.save_task(payload,match.group(1))
            self._send(200,{'ok':True,'data':task})
        except (ValueError,UnicodeError,core.ValidationError) as e:
            self._send(400,{'ok':False,'error':str(e)[:300]})
        except Exception:
            self._send(500,{'ok':False,'error':'配置保存失败，请检查本机服务日志'})


if __name__=='__main__':
    core.init()
    print('Private admin on 127.0.0.1:8766; SSH tunnel only',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8766),AdminHandler).serve_forever()
