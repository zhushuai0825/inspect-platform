#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent HTTP checks, bounded SSRF-safe runner and local event outbox (Python 3.6+)."""
import contextlib
import datetime
import fcntl
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('QA_DATA_DIR', os.path.join(ROOT, 'data'))
DB = os.path.join(DATA, 'platform.db')
REPORTS = os.path.join(DATA, 'reports')
SPOOL = os.path.join(DATA, 'spool')
MAX_TASKS, MAX_ENABLED, MAX_RESPONSE = 30, 15, 262144
PUBLIC_ORIGIN = 'http://8.146.206.64'
BUILTINS = {
 'home': ('个人主页', '/', '朱帅 — 个人主页'),
 'resume': ('简历页', '/zhushuai/', '朱帅'),
 'ops-hub': ('运维与 CI', '/apps/ops-hub.html', '运维与 CI'),
 'ai-test': ('AI 测试导览', '/apps/ai-test/', ''),
 'games': ('复古游戏架', '/apps/games/', ''),
 'qa-health': ('巡检平台健康检查', '/qa/api/health', 'ok'),
 'jenkins-login': ('Jenkins 登录页', '/jenkins/login', 'Jenkins'),
}
BUILTIN_ORDER = ['home','resume','ops-hub','ai-test','games','qa-health','jenkins-login']
BLOCKED = [ipaddress.ip_network(n) for n in [
 '0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16',
 '172.16.0.0/12','192.0.0.0/24','192.0.2.0/24','192.168.0.0/16',
 '198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/4','240.0.0.0/4',
 '::/128','::1/128','fc00::/7','fe80::/10','ff00::/8','2001:db8::/32','64:ff9b::/96','64:ff9b:1::/48'
]]

class ValidationError(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')


@contextlib.contextmanager
def conn():
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    for path in (DATA, REPORTS, SPOOL):
        os.makedirs(path, mode=0o700, exist_ok=True)
    with conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
          method TEXT NOT NULL, expected_status TEXT NOT NULL, contains_text TEXT NOT NULL,
          timeout_sec INTEGER NOT NULL, enabled INTEGER NOT NULL, builtin INTEGER NOT NULL,
          revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_state(task_id TEXT PRIMARY KEY, failed INTEGER NOT NULL,
          run_id TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed_runs(run_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          task_id TEXT NOT NULL, task_name TEXT NOT NULL, run_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
          status_code INTEGER NOT NULL, message TEXT NOT NULL, UNIQUE(kind,task_id,run_id));
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
          action TEXT, revision INTEGER, created_at TEXT);
        ''')
        import notifications
        notifications.init_schema(c)
        # Initialization marker avoids re-adding builtins after future operator maintenance.
        if not c.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
            for task_id in BUILTIN_ORDER:
                name, path, contains = BUILTINS[task_id]
                c.execute('INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task_id,name,PUBLIC_ORIGIN+path,'GET','[200]',contains,12,1,1,1,now(),now()))
            c.execute("INSERT INTO metadata VALUES('initialized','1')")
    os.chmod(DB, 0o600)


def as_task(row):
    return {'id':row['id'],'name':row['name'],'url':row['url'],'method':row['method'],
      'expected_status':json.loads(row['expected_status']),'contains':row['contains_text'],
      'timeout_sec':row['timeout_sec'],'enabled':bool(row['enabled']),'builtin':bool(row['builtin']),
      'revision':row['revision'],'created_at':row['created_at'],'updated_at':row['updated_at']}


def tasks():
    with conn() as c:
        rows = c.execute('SELECT * FROM tasks ORDER BY builtin DESC, created_at, id').fetchall()
    result = [as_task(r) for r in rows]
    order = {v:i for i,v in enumerate(BUILTIN_ORDER)}
    result.sort(key=lambda t:(0,order[t['id']]) if t['id'] in order else (1,t['created_at'],t['id']))
    return result


def is_public(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
        return False
    return not any(ip.version == net.version and ip in net for net in BLOCKED)


def resolve_public(host, port, resolver=None):
    if host.lower().rstrip('.') in ('localhost','metadata','metadata.google.internal') or host.lower().rstrip('.').endswith(('.local','.internal','.localhost')):
        raise ValidationError('禁止本机、内网与云元数据地址')
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        fn = resolver or socket.getaddrinfo
        q = queue.Queue(maxsize=1)
        def resolve():
            try: q.put((True, fn(host, port, 0, socket.SOCK_STREAM)))
            except Exception: q.put((False, None))
        thread = threading.Thread(target=resolve); thread.daemon=True;thread.start()
        try: success, records = q.get(timeout=3)
        except queue.Empty: raise ValidationError('DNS 解析超时')
        if not success: raise ValidationError('域名解析失败')
        addresses = list(dict.fromkeys(item[4][0] for item in records))
    if not addresses or any(not is_public(ip) for ip in addresses):
        raise ValidationError('目标 DNS 含内网、回环、保留或元数据地址，已拒绝')
    return addresses


def normalize_url(value, check_dns=True, resolver=None):
    if not isinstance(value,str) or not (1 <= len(value) <= 2048) or re.search(r'[\x00-\x20\x7f\\]',value):
        raise ValidationError('地址长度或字符无效')
    try:
        u = urlsplit(value)
        if u.scheme not in ('http','https') or not u.hostname or u.username is not None or u.password is not None:
            raise ValueError()
        host = u.hostname.encode('idna').decode('ascii').lower()
        if '%' in host or u.query or u.fragment: raise ValueError()
        port = u.port or (443 if u.scheme=='https' else 80)
        if port != (443 if u.scheme=='https' else 80): raise ValueError()
        authority = '['+host+']' if ':' in host else host
        path = u.path or '/'
        path.encode('ascii')  # Non-ASCII URL paths must use percent encoding.
        normalized = urlunsplit((u.scheme,authority,path,'',''))
    except (ValueError, UnicodeError):
        raise ValidationError('仅支持 HTTP:80 / HTTPS:443，无用户名、查询参数或片段；中文路径请编码')
    if check_dns: resolve_public(host,port,resolver)
    return normalized


def validate_task(payload, previous=None, resolver=None):
    allowed = {'name','url','method','expected_status','contains','timeout_sec','enabled','revision'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise ValidationError('存在不支持的字段')
    required = allowed - {'revision'}
    if not required.issubset(payload): raise ValidationError('配置字段不完整')
    name = payload['name']
    if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or re.search(r'[\x00-\x1f\x7f]',name):
        raise ValidationError('名称需为 1–80 个可显示字符')
    method = payload['method']
    if method not in ('GET','HEAD'): raise ValidationError('首版仅支持 GET 和 HEAD')
    codes = payload['expected_status']
    if not isinstance(codes,list) or not 1 <= len(codes) <= 8 or any(type(n) is not int or not 200 <= n <= 599 for n in codes):
        raise ValidationError('预期状态码需为 200–599 的整数，最多 8 个')
    contains = payload['contains']
    if not isinstance(contains,str) or len(contains)>500 or '\x00' in contains:
        raise ValidationError('正文断言最多 500 字符')
    if method=='HEAD' and contains: raise ValidationError('HEAD 不返回正文，请清空内容断言')
    timeout=payload['timeout_sec']
    if type(timeout) is not int or not 1 <= timeout <= 15: raise ValidationError('超时需为 1–15 秒整数')
    if type(payload['enabled']) is not bool: raise ValidationError('启用状态必须为布尔值')
    if previous and previous.get('builtin'):
        expected_url=PUBLIC_ORIGIN+BUILTINS[previous['id']][1]
        if payload['url'] != expected_url: raise ValidationError('系统内置地址固定；如需其他地址请新建任务')
        url=expected_url
    else:
        url=normalize_url(payload['url'],True,resolver)
    return dict(name=name.strip(),url=url,method=method,expected_status=sorted(set(codes)),contains=contains,timeout_sec=timeout,enabled=payload['enabled'])


def save_task(payload, task_id=None, resolver=None):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone() if task_id else None
        if task_id and not row: raise ValidationError('任务不存在')
        previous=as_task(row) if row else None
        if previous and (type(payload.get('revision')) is not int or payload['revision']!=previous['revision']):
            raise ValidationError('配置已被更新，请刷新后重试')
        valid=validate_task(payload,previous,resolver)
        rows=[as_task(r) for r in c.execute('SELECT * FROM tasks').fetchall()]
        if not previous and len(rows)>=MAX_TASKS: raise ValidationError('最多 30 个任务')
        remaining=[r for r in rows if r['id']!=task_id and r['enabled']]
        if valid['enabled']: remaining.append(valid)
        if len(remaining)>MAX_ENABLED or sum(t['timeout_sec'] for t in remaining)>120:
            raise ValidationError('最多启用 15 项，启用项的超时总预算不超过 120 秒')
        task_id=task_id or 'custom-'+uuid.uuid4().hex[:12]
        revision=(previous['revision']+1) if previous else 1
        values=(valid['name'],valid['url'],valid['method'],json.dumps(valid['expected_status']),valid['contains'],valid['timeout_sec'],int(valid['enabled']))
        if previous:
            c.execute('UPDATE tasks SET name=?,url=?,method=?,expected_status=?,contains_text=?,timeout_sec=?,enabled=?,revision=?,updated_at=? WHERE id=?',values+(revision,now(),task_id))
        else:
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(task_id,)+values+(0,revision,now(),now()))
        c.execute('INSERT INTO audit(task_id,action,revision,created_at) VALUES(?,?,?,?)',(task_id,'update' if previous else 'create',revision,now()))
        row=c.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    return as_task(row)


def reserve_trigger(cooldown=60):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT value FROM metadata WHERE key='last_trigger'").fetchone()
        wait=cooldown-(time.time()-float(row['value'])) if row else 0
        if wait>0: return int(wait)+1
        c.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('last_trigger',?)",(str(time.time()),))
    return 0


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self,host,ip,port,timeout,secure=False):
        http.client.HTTPConnection.__init__(self,host,port,timeout=timeout)
        self.ip=ip;self.secure=secure;self.raw_socket=None
    def connect(self):
        family=socket.AF_INET6 if ':' in self.ip else socket.AF_INET
        sock=socket.socket(family,socket.SOCK_STREAM);self.raw_socket=sock;sock.settimeout(self.timeout)
        try:
            sock.connect((self.ip,self.port))
            if self.secure:
                sock=ssl.create_default_context().wrap_socket(sock,server_hostname=self.host,do_handshake_on_connect=False)
                self.raw_socket=sock
                sock.do_handshake()
            self.sock=sock
        except Exception:
            sock.close();raise
    def abort(self):
        sock=self.raw_socket
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass


def probe(task):
    start=time.monotonic();code=0;error='';request=None;timer=None;expired=threading.Event()
    try:
        trusted=task.get('builtin') and task.get('id') in BUILTINS and task.get('url')==PUBLIC_ORIGIN+BUILTINS[task['id']][1]
        if trusted:
            host,ip,port,secure,path='8.146.206.64','127.0.0.1',80,False,BUILTINS[task['id']][1]
        else:
            normalized=normalize_url(task['url'],False)
            u=urlsplit(normalized);host=u.hostname;port=443 if u.scheme=='https' else 80;secure=u.scheme=='https';path=u.path
            # Validate EVERY resolved address on EVERY execution and connect to a pinned numeric IP.
            ip=resolve_public(host,port)[0]
        request=PinnedConnection(host,ip,port,task['timeout_sec'],secure)
        def abort(): expired.set();request.abort()
        timer=threading.Timer(task['timeout_sec'],abort);timer.daemon=True;timer.start()
        request.request(task['method'],path,headers={'Host':('['+host+']' if ':' in host else host),'User-Agent':'qa-inspector/3.0','Accept-Encoding':'identity','Connection':'close'})
        response=request.getresponse();code=response.status
        # Never follow redirects: Location is neither resolved nor requested.
        if response.getheader('Content-Encoding','identity').lower() not in ('','identity'):
            raise ValidationError('不接受压缩响应，请使用返回原始正文的健康检查地址')
        length=response.getheader('Content-Length')
        if task['method']!='HEAD' and length and int(length)>MAX_RESPONSE: raise ValidationError('响应超过 256 KiB 上限')
        raw=response.read(MAX_RESPONSE+1) if task['method']!='HEAD' else b''
        if len(raw)>MAX_RESPONSE: raise ValidationError('响应超过 256 KiB 上限')
        if expired.is_set(): raise ValidationError('请求超过配置的总超时')
        if code not in task['expected_status']: error='期望状态码 %s，实际 %s'%(task['expected_status'],code)
        elif task['contains'] and task['contains'] not in raw.decode('utf-8','replace'): error='响应正文未匹配配置的内容断言'
    except ValidationError as e: error=str(e)
    except Exception:
        error='请求超时' if expired.is_set() else '连接、TLS、DNS 或 HTTP 响应异常'
    finally:
        if timer: timer.cancel()
        if request: request.close()
    elapsed=int((time.monotonic()-start)*1000);ok=not error
    expected='HTTP '+', '.join(str(n) for n in task['expected_status'])
    if task['contains']: expected+='，正文包含「'+task['contains']+'」'
    return {'id':task['id'],'name':task['name'],'public_url':task['url'],'url':task['url'],'method':task['method'],
      'ok':ok,'status_code':code,'expected':expected,'error':error,'duration_ms':elapsed,
      'task_revision':task['revision'],'log':'HTTP %s，耗时 %sms。%s'%(code,elapsed,error or '断言通过；未跟随重定向。')}


def record_events(report):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM processed_runs WHERE run_id=?',(report['id'],)).fetchone(): return
        for check in report['checks']:
            row=c.execute('SELECT failed FROM task_state WHERE task_id=?',(check['id'],)).fetchone()
            failed=not check['ok'];kind=None
            if failed and (row is None or not row['failed']):kind='failure'
            elif not failed and row is not None and row['failed']:kind='recovery'
            if kind:
                cursor=c.execute('INSERT OR IGNORE INTO events(kind,task_id,task_name,run_id,occurred_at,status_code,message) VALUES(?,?,?,?,?,?,?)',
                  (kind,check['id'],check['name'],report['id'],report['finished_at'],check['status_code'],check.get('error') or '检查已恢复'))
                if cursor.rowcount:
                    import notifications
                    notifications.enqueue(c,cursor.lastrowid,kind,check,report)
            c.execute('INSERT OR REPLACE INTO task_state VALUES(?,?,?,?)',(check['id'],int(failed),report['id'],report['finished_at']))
        c.execute('INSERT INTO processed_runs VALUES(?)',(report['id'],))
        c.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')


def events(limit=100):
    with conn() as c:
        rows=c.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?',(min(200,max(1,limit)),)).fetchall()
    import notifications
    return notifications.attach([dict(r) for r in rows])


def execute(run_id=None,build='',trigger='manual'):
    run_id=run_id or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    if not re.match(r'^[a-zA-Z0-9_-]{1,80}$',run_id):raise ValidationError('无效运行编号')
    if build and not re.match(r'^\d{1,10}$',str(build)):raise ValidationError('无效构建编号')
    if trigger not in ('manual','schedule','jenkins'):raise ValidationError('无效触发来源')
    path=os.path.join(REPORTS,run_id+'.json')
    with open(os.path.join(DATA,'runner.lock'),'a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        if os.path.isfile(path):
            with open(path) as f:report=json.load(f)
            record_events(report);return report
        snapshot=[t for t in tasks() if t['enabled']]
        start=time.monotonic();started_at=now()
        checks=[probe(t) for t in snapshot]
        passed=sum(1 for c in checks if c['ok']);total=len(checks);failed=total-passed
        report={'id':run_id,'suite':'site-http-smoke','suite_name':'站点 HTTP 冒烟巡检',
          'started_at':started_at,'finished_at':now(),'duration_ms':int((time.monotonic()-start)*1000),
          'trigger':trigger,'jenkins_job':'site-http-smoke','jenkins_build':str(build),
          'status':('failed' if failed else 'passed') if total else 'skipped','summary':{'total':total,'passed':passed,'failed':failed,'pass_rate':round(100.0*passed/total,1) if total else 0.0},
          'config_snapshot':snapshot,'checks':checks}
        tmp=path+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
        os.chmod(tmp,0o600);os.replace(tmp,path)
        # Latest symlink update is atomic. Public access is only through the API allowlist.
        link=os.path.join(REPORTS,'latest.json');newlink=link+'.new'
        try:os.unlink(newlink)
        except FileNotFoundError:pass
        os.symlink(run_id+'.json',newlink);os.replace(newlink,link)
        record_events(report)
        return report
