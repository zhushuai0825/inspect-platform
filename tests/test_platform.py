import copy
import glob
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,HTTPServer
from unittest import mock
from urllib.request import Request,urlopen
from urllib.error import HTTPError
SOURCE=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'))
sys.path.insert(0,os.path.join(SOURCE,'release') if os.path.isdir(os.path.join(SOURCE,'release')) else SOURCE)
import core,app,admin,worker

PUBLIC=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',80))]

def payload(**changes):
    d={'name':'临时测试','url':'https://example.com/health','method':'GET','expected_status':[200],'contains':'','timeout_sec':2,'enabled':False};d.update(changes);return d

def fields(t):return {k:t[k] for k in ('name','url','method','expected_status','contains','timeout_sec','enabled','revision')}

class Base(unittest.TestCase):
 def setUp(self):
    self.tmp=tempfile.TemporaryDirectory();self.old=(core.DATA,core.DB,core.REPORTS,core.SPOOL)
    core.DATA=self.tmp.name;core.DB=os.path.join(core.DATA,'platform.db');core.REPORTS=os.path.join(core.DATA,'reports');core.SPOOL=os.path.join(core.DATA,'spool');core.init()
 def tearDown(self):
    core.DATA,core.DB,core.REPORTS,core.SPOOL=self.old;self.tmp.cleanup()

class CoreTests(Base):
 def test_seed_persistent(self):
    core.init();self.assertEqual(len(core.tasks()),7);self.assertEqual(sum(t['enabled'] for t in core.tasks()),7)
 def test_create_edit_disable_revision(self):
    t=core.save_task(payload(),resolver=PUBLIC);self.assertFalse(t['builtin']);self.assertFalse(t['enabled']);self.assertEqual(t['revision'],1)
    p=fields(t);p['enabled']=True;p['method']='HEAD';u=core.save_task(p,t['id'],PUBLIC);self.assertEqual(u['revision'],2);self.assertTrue(u['enabled'])
    with self.assertRaises(core.ValidationError):core.save_task(p,t['id'],PUBLIC)
 def test_builtin_target_locked(self):
    t=core.tasks()[0];p=fields(t);p['url']='http://127.0.0.1/admin'
    with self.assertRaises(core.ValidationError):core.save_task(p,t['id'])
 def test_builtin_rules_editable(self):
    t=core.tasks()[0];p=fields(t);p['name']='主页检查';p['timeout_sec']=10;p['enabled']=False
    changed=core.save_task(p,t['id']);self.assertEqual(changed['name'],'主页检查');self.assertFalse(changed['enabled'])
 def test_untrusted_cannot_claim_builtin(self):
    with self.assertRaises(core.ValidationError):core.save_task(payload(builtin=True),resolver=PUBLIC)
 def test_private_ips(self):
    for ip in ['127.0.0.1','10.0.0.1','172.22.0.1','192.168.1.1','169.254.169.254','100.100.100.200','0.0.0.0','224.0.0.1','::1','fc00::1','::ffff:127.0.0.1','64:ff9b::7f00:1']:
      self.assertFalse(core.is_public(ip),ip)
 def test_reserved_ranges(self):
    for ip in ['192.0.2.1','198.51.100.1','203.0.113.1','198.18.0.1','2001:db8::1']:
      self.assertFalse(core.is_public(ip),ip)
 def test_public_ips(self):
    for ip in ['8.8.8.8','1.1.1.1','8.146.206.64','2606:4700:4700::1111']:self.assertTrue(core.is_public(ip),ip)
 def test_url_rejections(self):
    for url in ['file:///etc/passwd','gopher://example.com','http://user:pass@example.com/','http://example.com:8766/','http://example.com/?token=x','https://example.com/#x','http://127.0.0.1/','http://[::1]/','http://example.com\\@127.0.0.1','http://example.com/\nX:1']:
      with self.assertRaises(core.ValidationError,msg=url):core.normalize_url(url,resolver=PUBLIC)
 def test_decimal_and_alternate_loopback(self):
    private=lambda *args:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
    for host in ['2130706433','127.1','0x7f000001','0177.0.0.1']:
      with self.assertRaises(core.ValidationError):core.normalize_url('http://'+host+'/',resolver=private)
 def test_mixed_dns_rejected(self):
    mixed=lambda *args:PUBLIC()+[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',80))]
    with self.assertRaises(core.ValidationError):core.resolve_public('example.com',80,mixed)
 def test_dns_timeout_is_bounded(self):
    def slow(*args):time.sleep(4);return PUBLIC()
    start=time.monotonic()
    with self.assertRaises(core.ValidationError):core.resolve_public('example.com',80,slow)
    self.assertLess(time.monotonic()-start,3.5)
 def test_execution_revalidates_dns(self):
    t=core.save_task(payload(),resolver=PUBLIC)
    with mock.patch.object(core,'resolve_public',side_effect=core.ValidationError('私网已拒绝')):
      result=core.probe(t)
    self.assertFalse(result['ok']);self.assertEqual(result['status_code'],0)
 def test_invalid_fields(self):
    for change in [dict(method='POST'),dict(timeout_sec=0),dict(timeout_sec=16),dict(timeout_sec=True),dict(expected_status=[True]),dict(expected_status=[600]),dict(contains='x'*501),dict(enabled='true'),dict(method='HEAD',contains='x'),dict(name='')]:
      with self.assertRaises(core.ValidationError):core.save_task(payload(**change),resolver=PUBLIC)
 def test_count_limit(self):
    for i in range(23):core.save_task(payload(name='task'+str(i)),resolver=PUBLIC)
    with self.assertRaises(core.ValidationError):core.save_task(payload(),resolver=PUBLIC)
 def test_timeout_budget(self):
    core.save_task(payload(timeout_sec=15,enabled=True),resolver=PUBLIC)
    core.save_task(payload(timeout_sec=15,enabled=True),resolver=PUBLIC)
    with self.assertRaises(core.ValidationError):core.save_task(payload(timeout_sec=15,enabled=True),resolver=PUBLIC)
 def test_trigger_persistent_limit(self):
    self.assertEqual(core.reserve_trigger(),0);core.init();self.assertGreater(core.reserve_trigger(),0)
 def test_event_failure_recovery_dedup(self):
    def report(rid,ok):return{'id':rid,'finished_at':core.now(),'checks':[{'id':'home','name':'主页','ok':ok,'status_code':200 if ok else 503,'error':'' if ok else 'HTTP 503'}]}
    core.record_events(report('initial',True));self.assertEqual(len(core.events()),0)
    core.record_events(report('fail1',False));core.record_events(report('fail1',False));core.record_events(report('fail2',False));self.assertEqual(len(core.events()),1)
    core.record_events(report('recovered',True));core.record_events(report('passagain',True));self.assertEqual([e['kind'] for e in core.events()],['recovery','failure'])
 def test_snapshot_immutable(self):
    def fake(t):return{'id':t['id'],'name':t['name'],'ok':True,'status_code':200,'error':'','task_revision':t['revision']}
    with mock.patch.object(core,'probe',side_effect=fake):r=core.execute(run_id='snapshot-test')
    path=os.path.join(core.REPORTS,r['id']+'.json')
    with open(path,'rb') as f:old=f.read()
    t=core.tasks()[0];p=fields(t);p['name']='更新名称';core.save_task(p,t['id'])
    with open(path,'rb') as f:self.assertEqual(f.read(),old)
    self.assertNotEqual(r['config_snapshot'][0]['name'],core.tasks()[0]['name'])
 def test_all_disabled_is_skipped(self):
    with core.conn() as c:c.execute('UPDATE tasks SET enabled=0')
    r=core.execute(run_id='disabled');self.assertEqual(r['status'],'skipped');self.assertEqual(r['summary']['total'],0)
 def test_request_invalid_spool(self):
    p=os.path.join(core.SPOOL,'20260101-000000-b1.req')
    with open(p,'w') as f:json.dump({'build':'1','trigger':'shell; id'},f)
    with mock.patch.object(core,'execute') as run:worker.handle(p);run.assert_not_called()
    with open(p[:-4]+'.result') as f:self.assertIn('exit_code=2',f.read())
 def test_worker_retry_no_duplicate_report(self):
    with mock.patch.object(core,'probe',return_value={'id':'home','name':'主页','ok':True,'status_code':200,'error':''}):
      first=core.execute(run_id='stable',build='5');second=core.execute(run_id='stable',build='5')
    self.assertEqual(first['id'],second['id']);self.assertEqual(len(glob.glob(os.path.join(core.REPORTS,'stable.json'))),1)
 def test_socket_pin_uses_numeric_ip(self):
    fake=mock.Mock()
    with mock.patch('core.socket.socket',return_value=fake):
      c=core.PinnedConnection('example.com','8.8.8.8',80,2);c.connect();fake.connect.assert_called_once_with(('8.8.8.8',80));c.close()

class HTTPProbeTests(Base):
 def test_real_local_fixture_http_limits_redirect_and_timeout(self):
    hits=[]
    class H(BaseHTTPRequestHandler):
      def log_message(self,*args):pass
      def do_GET(self):
        hits.append(self.path)
        if self.path=='/redirect':self.send_response(302);self.send_header('Location','http://169.254.169.254/latest/meta-data/');self.end_headers();return
        if self.path=='/large':self.send_response(200);self.send_header('Content-Length','262145');self.end_headers();return
        if self.path=='/slow':
          self.send_response(200);self.end_headers()
          try:
            for i in range(20):self.wfile.write(b'x');self.wfile.flush();time.sleep(.2)
          except (BrokenPipeError,ConnectionResetError):pass
          return
        self.send_response(200);self.end_headers();self.wfile.write(b'healthy')
    server=app.ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
    original=core.PinnedConnection
    factory=lambda host,ip,port,timeout,secure:original(host,'127.0.0.1',server.server_port,timeout,False)
    task=core.save_task(payload(enabled=False,timeout_sec=1),resolver=PUBLIC)
    try:
      with mock.patch.object(core,'resolve_public',return_value=['8.8.8.8']),mock.patch.object(core,'PinnedConnection',side_effect=factory):
        task['url']='http://example.com/ok';task['contains']='healthy';self.assertTrue(core.probe(task)['ok'])
        task['contains']='';task['url']='http://example.com/redirect';r=core.probe(task);self.assertFalse(r['ok']);self.assertEqual(r['status_code'],302)
        task['url']='http://example.com/large';self.assertIn('256 KiB',core.probe(task)['error'])
        task['url']='http://example.com/slow';start=time.monotonic();self.assertFalse(core.probe(task)['ok']);self.assertLess(time.monotonic()-start,2)
      self.assertEqual(hits,['/ok','/redirect','/large','/slow'])
    finally:server.shutdown();server.server_close()

class APITests(Base):
 def test_public_private_separation_and_admin_save(self):
    old_reports=app.REPORT_DIR;app.REPORT_DIR=core.REPORTS
    pub=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);adm=app.ThreadingHTTPServer(('127.0.0.1',0),admin.AdminHandler)
    for s in (pub,adm):threading.Thread(target=s.serve_forever,daemon=True).start()
    def req(server,path,expected,body=None,headers=None):
      h=headers or {};data=None
      if body is not None:data=json.dumps(body).encode();h.setdefault('Content-Type','application/json')
      r=Request('http://127.0.0.1:'+str(server.server_port)+path,data=data,headers=h)
      try:response=urlopen(r,timeout=10);code=response.status;raw=response.read()
      except HTTPError as e:code=e.code;raw=e.read()
      self.assertEqual(code,expected,(path,raw[:100]));return json.loads(raw) if path.startswith('/api') or path.startswith('/qa/api') else raw
    try:
      req(pub,'/qa/api/tasks',200);req(pub,'/qa/api/events',200)
      old_password=os.environ.pop('RELEASE_PUBLISH_PASSWORD',None)
      try:
        for path in ['/qa/api/tasks','/qa/api/tasks/home']:
          denied=req(pub,path,403,payload(),{'X-QA-Admin':'1','Host':'localhost'})
          self.assertIn(denied['error'],('发布口令未配置，未执行操作','口令不正确'))
        self.assertEqual(len(req(pub,'/qa/api/tasks',200)['data']),7)
        for path in ['/qa/admin/api/tasks','/qa/api/internal/run']:
          req(pub,path,404,payload(),{'X-QA-Admin':'1','Host':'localhost'})
      finally:
        if old_password is not None:os.environ['RELEASE_PUBLISH_PASSWORD']=old_password
      for path in ['/qa/data/platform.db','/qa/data/secret.env','/qa/admin.py','/qa/admin-static/app.js']:req(pub,path,404)
      req(adm,'/api/tasks',403,headers={'Host':'attacker.example'})
      req(adm,'/api/tasks',403,payload(),{'X-QA-Admin':'1','Origin':'https://attacker.example'})
      req(adm,'/api/tasks',403,payload())
      with mock.patch.object(core,'resolve_public',return_value=['8.8.8.8']):
        result=req(adm,'/api/tasks',200,payload(),{'X-QA-Admin':'1'});task=result['data'];self.assertFalse(task['enabled'])
        body=fields(task);body['name']='编辑后的检查';body['enabled']=True;updated=req(adm,'/api/tasks/'+task['id'],200,body,{'X-QA-Admin':'1'})['data'];self.assertEqual(updated['revision'],2)
      self.assertEqual(len(req(pub,'/qa/api/tasks',200)['data']),8)
    finally:pub.shutdown();adm.shutdown();pub.server_close();adm.server_close();app.REPORT_DIR=old_reports

if __name__=='__main__':unittest.main(verbosity=2)
