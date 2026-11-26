#!/usr/bin/env python3
"""Private, root-operated release CLI. Python 3.6+, no HTTP or Git interface."""
import ast,contextlib,datetime,fcntl,hashlib,json,os,re,shutil,sqlite3,subprocess,sys,tempfile,time,uuid,zipfile
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
from urllib.error import HTTPError
RUNTIME=set(""".gitignore DEPLOYMENT.md MAIL-ACTIVATION.md MAIL-SETUP.md README.md admin-static/admin.css admin-static/app.js admin-static/index.html admin.py app.py core.py deploy/inspect-admin.service deploy/inspect-mail.service deploy/inspect-platform.service deploy/inspect-worker.service deploy/update_job.py inspect_run.sh jenkins/Jenkinsfile notifications.py static/app.js static/index.html static/styles.css tests/test_notifications.py tests/test_platform.py worker.py""".split())
OPS={'ops/release.py','ops/test_release.py','ops/RELEASE.md'}
REQUIRED=RUNTIME-{'MAIL-ACTIVATION.md'}
UNITS=['inspect-platform','inspect-admin','inspect-worker','inspect-mail']
def now():return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')
class ReleaseError(Exception):pass
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.release-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.chmod(name,0o600);os.replace(name,str(path))
        d=os.open(str(path.parent),os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if os.path.exists(name):os.unlink(name)
def save(path,obj):atomic(path,(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode())
def load(path,default=None):return json.loads(path.read_text()) if path.exists() else default
class Manager:
    def __init__(self,root,state,controller):
        self.root=Path(root);self.state=Path(state);self.controller=controller
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        (self.state/'releases').mkdir(mode=0o700,exist_ok=True)
    @contextlib.contextmanager
    def lock(self):
        with (self.state/'release.lock').open('a') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ReleaseError('RELEASE_BUSY')
            yield
    def path(self,v):
        if not isinstance(v,str) or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$',v):raise ReleaseError('INVALID_VERSION')
        return self.state/'releases'/v
    def receipt(self,event,**fields):
        r=dict(time=now(),event=event,**fields)
        with (self.state/'audit.jsonl').open('a') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
    def status(self):
        return {'current':load(self.state/'current.json'), 'transaction':load(self.state/'transaction.json'), 'releases':sorted(p.name for p in (self.state/'releases').iterdir() if p.is_dir() and not p.name.startswith('.'))}
    def snapshot(self,v):
        dest=self.path(v)
        if dest.exists():raise ReleaseError('VERSION_EXISTS')
        stage=Path(tempfile.mkdtemp(prefix='.snapshot-',dir=str(self.state/'releases')))
        try:
            src=stage/'source';src.mkdir()
            for name in sorted(RUNTIME):
                p=self.root/name
                if p.exists():
                    if p.is_symlink() or not p.is_file():raise ReleaseError('UNSAFE_SOURCE')
                    target=src/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(str(p),str(target))
            self.manifest(stage,v);os.replace(str(stage),str(dest))
        finally:
            if stage.exists():shutil.rmtree(str(stage))
        return v
    def manifest(self,dest,v):
        files={str(p.relative_to(dest/'source')):digest(p) for p in (dest/'source').rglob('*') if p.is_file()}
        if not REQUIRED.issubset(set(files)):raise ReleaseError('MISSING_REQUIRED_SOURCE')
        save(dest/'manifest.json',{'version':v,'created_at':now(),'files':files})
    def verify(self,v):
        dest=self.path(v);m=load(dest/'manifest.json')
        if not m or m['version']!=v or not REQUIRED.issubset(set(m['files'])):raise ReleaseError('INVALID_MANIFEST')
        if not set(m['files']).issubset(RUNTIME|OPS):raise ReleaseError('FORBIDDEN_MANIFEST_PATH')
        actual={str(p.relative_to(dest/'source')) for p in (dest/'source').rglob('*') if p.is_file()}
        if actual!=set(m['files']):raise ReleaseError('UNTRACKED_RELEASE_FILE')
        for name,h in m['files'].items():
            p=dest/'source'/name
            if p.is_symlink() or not p.is_file() or digest(p)!=h:raise ReleaseError('CHECKSUM_MISMATCH')
        return dest/'source'
    def preflight(self,src):
        """
        Preflight候选测试（隔离runner）
        
        生产安全：
        - 调用固定可信隔离runner（不直接执行候选代码）
        - Runner路径：/opt/inspect-deploy/bin/run-candidate-tests
        - 验证runner存在、owner=root、mode不可组写/他人写
        - Fail-closed：runner不存在/不可信/失败均阻止部署
        - 禁止：root/当前UID直接执行候选unittest
        - 禁止：通过env/候选覆盖runner路径
        
        测试模式（RELEASE_TEST_RUNNER_PATH）：
        - 仅限非root环境
        - 仅用于测试依赖注入
        - 不影响生产root逻辑
        """
        # 语法检查（快速失败）
        for name in sorted(RUNTIME):
            if name.endswith('.py') and (src/name).exists():
                ast.parse((src/name).read_text(),filename=name)
        
        # 确定runner路径
        # 生产：固定路径，不可覆盖
        # 测试：仅非root可用env override
        if os.geteuid() == 0:
            # Root生产模式：固定runner路径，拒绝env覆盖
            runner_path = Path('/opt/inspect-deploy/bin/run-candidate-tests')
        else:
            # 非root测试模式：允许依赖注入
            runner_path = Path(os.environ.get(
                'RELEASE_TEST_RUNNER_PATH',
                '/opt/inspect-deploy/bin/run-candidate-tests'
            ))
        
        # 验证runner存在
        if not runner_path.exists():
            raise ReleaseError('PREFLIGHT_RUNNER_MISSING')
        
        # 验证runner安全性：文件及完整祖先链
        # Root模式：owner=root，检查到根
        # 非root测试：跳过owner检查，检查到受保护锚点
        # 所有模式：拒绝符号链接，拒绝组写/他人写
        try:
            runner_path_resolved = runner_path.resolve()
            is_root_mode = os.geteuid() == 0
            
            # 检查文件不是符号链接
            if runner_path.is_symlink():
                raise ReleaseError('PREFLIGHT_RUNNER_IS_SYMLINK')
            
            # 检查路径规范化
            if str(runner_path_resolved) != str(runner_path.absolute()):
                raise ReleaseError('PREFLIGHT_RUNNER_PATH_TRAVERSAL')
            
            # 验证文件本身
            stat_info = runner_path.stat()
            
            if is_root_mode and stat_info.st_uid != 0:
                raise ReleaseError('PREFLIGHT_RUNNER_NOT_ROOT_OWNED')
            
            if stat_info.st_mode & 0o022:
                raise ReleaseError('PREFLIGHT_RUNNER_WORLD_WRITABLE')
            
            # 验证完整祖先链
            current = runner_path.parent
            ancestors_checked = 0
            
            # 受保护锚点（非root测试）
            protected_anchors = []
            if not is_root_mode:
                runner_str = str(runner_path)
                if runner_str.startswith('/tmp/'):
                    parts = runner_str.split('/')
                    if len(parts) >= 3:
                        protected_anchors.append(Path('/' + parts[1] + '/' + parts[2]))
            
            while current != current.parent:
                ancestors_checked += 1
                if ancestors_checked > 100:
                    raise ReleaseError('PREFLIGHT_RUNNER_ANCESTOR_TOO_DEEP')
                
                # 受保护锚点：停止检查
                if current in protected_anchors:
                    break
                
                # 检查祖先不是符号链接
                if current.is_symlink():
                    raise ReleaseError('PREFLIGHT_RUNNER_ANCESTOR_SYMLINK')
                
                ancestor_stat = current.stat()
                
                if is_root_mode and ancestor_stat.st_uid != 0:
                    raise ReleaseError('PREFLIGHT_RUNNER_ANCESTOR_NOT_ROOT_OWNED')
                
                if ancestor_stat.st_mode & 0o022:
                    raise ReleaseError('PREFLIGHT_RUNNER_ANCESTOR_WRITABLE')
                
                current = current.parent
                
        except OSError as e:
            raise ReleaseError('PREFLIGHT_RUNNER_STAT_FAILED')
        
        # 调用隔离runner
        # 传递候选源码目录
        # 不直接执行候选代码
        try:
            proc = subprocess.run(
                [str(runner_path), str(src)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=180  # 3分钟超时
            )
        except subprocess.TimeoutExpired:
            raise ReleaseError('PREFLIGHT_RUNNER_TIMEOUT')
        except OSError as e:
            raise ReleaseError('PREFLIGHT_RUNNER_EXEC_FAILED')
        
        # Fail-closed: runner返回非0阻止部署
        if proc.returncode != 0:
            # 不持久化候选输出（可能包含敏感数据）
            raise ReleaseError('PREFLIGHT_RUNNER_FAILED')
    def prepare(self,archive,v):
        with self.lock():
            dest=self.path(v)
            if dest.exists():raise ReleaseError('VERSION_EXISTS')
            stage=Path(tempfile.mkdtemp(prefix='.prepare-',dir=str(self.state/'releases')))
            try:
                src=stage/'source';src.mkdir();seen=set();total=0
                with zipfile.ZipFile(str(archive)) as z:
                    for item in z.infolist():
                        name=item.filename
                        if item.is_dir():continue
                        if not name.startswith('inspect-platform/'):raise ReleaseError('BAD_ARCHIVE_PREFIX')
                        name=name[len('inspect-platform/'):]
                        if name not in RUNTIME|OPS or name in seen or ((item.external_attr>>16)&0o170000)==0o120000:raise ReleaseError('FORBIDDEN_ARCHIVE_PATH')
                        seen.add(name);total+=item.file_size
                        if len(seen)>64 or total>4194304 or item.file_size>1048576:raise ReleaseError('ARCHIVE_LIMIT')
                        target=src/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(item))
                # 候选树保持 umask 0700。交给 runner 复制到受控暂存后再给 nobody 读。
                # 不在发布状态目录上 chmod 755：那既写不了临时 data，也会把状态树暴露给其他用户。
                self.manifest(stage,v);self.preflight(src);os.replace(str(stage),str(dest))
                self.receipt('prepared',version=v)
            finally:
                if stage.exists():shutil.rmtree(str(stage))
        return {'prepared':v,'preflight':'passed'}
    def baseline(self,v):
        with self.lock():
            if (self.state/'current.json').exists():raise ReleaseError('BASELINE_ALREADY_REGISTERED')
            self.snapshot(v);save(self.state/'current.json',{'version':v,'at':now()});self.receipt('baseline',version=v)
        return {'baseline':v,'services_restarted':False}
    def secret_fingerprints(self):
        return {name:digest(self.root/'data'/name) for name in ['mail.env','secret.env'] if (self.root/'data'/name).exists()}
    def install(self,v):
        src=self.verify(v)
        for name in sorted(RUNTIME):
            p=src/name;target=self.root/name
            for parent in [target]+list(target.parents):
                if parent==self.root.parent:break
                if parent.is_symlink():raise ReleaseError('UNSAFE_DESTINATION')
            if p.exists():atomic(target,p.read_bytes());os.chmod(str(target),0o755 if name=='inspect_run.sh' else 0o644)
            elif target.exists():target.unlink()
    def restore(self,previous):
        self.controller.stop();self.install(previous);self.controller.start_front();self.controller.health();self.controller.start_background();self.controller.final_health()
        save(self.state/'current.json',{'version':previous,'at':now()})
    def deploy(self,v):
        with self.lock():
            tx=load(self.state/'transaction.json')
            if tx and tx['phase'] not in ('committed','rolled_back'):raise ReleaseError('RECOVERY_REQUIRED')
            src=self.verify(v);self.preflight(src)
            current=load(self.state/'current.json')
            if not current:raise ReleaseError('BASELINE_REQUIRED')
            previous='before-'+datetime.datetime.now().strftime('%Y%m%d%H%M%S')+'-'+uuid.uuid4().hex[:6]
            self.snapshot(previous);before=self.secret_fingerprints()
            tx={'target':v,'previous':previous,'previous_version':current['version'],'phase':'prepared','at':now()}
            save(self.state/'transaction.json',tx)
            try:
                self.controller.stop();tx['phase']='stopped';save(self.state/'transaction.json',tx)
                if hasattr(self.controller,'backup_runtime'):
                    tx['runtime_backup']=self.controller.backup_runtime();save(self.state/'transaction.json',tx)
                self.install(v);tx['phase']='installed';save(self.state/'transaction.json',tx)
                self.controller.start_front();self.controller.health()
                if before!=self.secret_fingerprints():raise ReleaseError('PRIVATE_CONFIG_CHANGED')
                self.controller.start_background();self.controller.final_health()
                save(self.state/'current.json',{'version':v,'at':now()});tx['phase']='committed';save(self.state/'transaction.json',tx)
                self.receipt('committed',version=v,private_config_unchanged=True)
                return {'deployed':v,'health':'passed','private_config_unchanged':True}
            except Exception:
                try:
                    if tx['phase']=='prepared':
                        self.controller.start_front();self.controller.health();self.controller.start_background();self.controller.final_health()
                    else:self.restore(previous)
                    if before!=self.secret_fingerprints():raise ReleaseError('PRIVATE_CONFIG_CHANGED')
                    tx['phase']='rolled_back';save(self.state/'transaction.json',tx);self.receipt('auto_rollback',target=v,restored=previous)
                except Exception:
                    tx['phase']='rollback_failed';save(self.state/'transaction.json',tx);self.receipt('rollback_failed',target=v)
                    raise ReleaseError('ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED')
                raise ReleaseError('DEPLOY_FAILED_ROLLED_BACK')
    def recover(self):
        with self.lock():
            tx=load(self.state/'transaction.json')
            if not tx or tx['phase'] in ('committed','rolled_back'):raise ReleaseError('NO_RECOVERY_NEEDED')
            self.verify(tx['previous']);self.restore(tx['previous']);tx['phase']='rolled_back';save(self.state/'transaction.json',tx)
            self.receipt('crash_recovery',restored=tx['previous']);return {'recovered':tx['previous']}
class Systemd:
    def __init__(self,root):self.root=Path(root);self.runlock=None
    def systemctl(self,*args):subprocess.check_call(['systemctl']+list(args),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=40)
    def stop(self):
        # Never interrupt a running suite or an SMTP transmission. Refuse busy maintenance.
        if self.runlock is None:
            self.runlock=(self.root/'data'/'runner.lock').open('a')
            try:fcntl.flock(self.runlock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:self.runlock.close();self.runlock=None;raise ReleaseError('INSPECTION_BUSY')
        self.systemctl('stop','inspect-worker')
        with sqlite3.connect(str(self.root/'data'/'platform.db'),timeout=5) as c:
            if c.execute("SELECT COUNT(*) FROM mail_outbox WHERE status IN ('pending','sending')").fetchone()[0]:raise ReleaseError('MAIL_QUEUE_BUSY')
        self.systemctl('stop','inspect-mail','inspect-admin','inspect-platform')
    def backup_runtime(self):
        # Private rollback aid, NEVER a source artifact and NEVER restored automatically.
        # 备份留在 dispatcher 可写集合内的 root-only 目录，不放进应用树，也不放 /root（ProtectHome 不可写）。
        state_root=Path('/var/lib/inspect-deploy')
        if not state_root.exists():
            state_root.mkdir(mode=0o750)
            os.chmod(str(state_root),0o750)
        backup_root=state_root/'backups'
        backup_root.mkdir(mode=0o700)
        os.chmod(str(backup_root),0o700)
        dest=backup_root/('inspect-release-data-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        dest.mkdir(mode=0o700)
        subprocess.check_call(['tar','-czf',str(dest/'data.tar.gz'),'-C',str(self.root),'data'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
        os.chmod(str(dest/'data.tar.gz'),0o600);return str(dest)
    def start_front(self):self.systemctl('start','inspect-platform','inspect-admin')
    def get(self,path,status=200,contains=None):
        req=Request('http://127.0.0.1'+path,headers={'Host':'8.146.206.64'})
        try:
            with build_opener(ProxyHandler({})).open(req,timeout=3) as r:code=r.status;body=r.read(1048576)
        except HTTPError as e:code=e.code;body=e.read(1048576)
        if code!=status or (contains is not None and contains not in body):raise ReleaseError('HEALTH_CHECK_FAILED')
        return body
    def health(self):
        for attempt in range(20):
            try:
                self.get('/qa/api/health',contains=b'ok');self.get('/qa/');self.get('/');self.get('/zhushuai/')
                self.get('/jenkins/login',contains=b'Jenkins');self.get('/jenkins/',403)
                data=json.loads(self.get('/qa/api/tasks'));assert data.get('ok',True) is not False
                return
            except Exception:
                if attempt==19:raise ReleaseError('HEALTH_CHECK_FAILED')
                time.sleep(0.5)
    def start_background(self):
        # Release the runner guard before workers are started.
        if self.runlock is not None:self.runlock.close();self.runlock=None
        self.systemctl('start','inspect-worker','inspect-mail')
    def final_health(self):self.systemctl('is-active','--quiet',*UNITS)
def main():
    # Guard：root 生产模式拒绝所有测试 overrides
    if os.geteuid() == 0:
        test_overrides = [k for k in os.environ if k.startswith('RELEASE_TEST_')]
        if test_overrides:
            raise ReleaseError('ROOT_PRODUCTION_REJECT_TEST_OVERRIDES')
    
    # 沙盒模式：RELEASE_TEST_SANDBOX=root:state[:controller_class]
    # 例如：RELEASE_TEST_SANDBOX=/tmp/root:/tmp/state:LocalController
    sandbox_env = os.environ.get('RELEASE_TEST_SANDBOX')
    
    if sandbox_env:
        # 沙盒模式：非 root，临时目录，Mock 控制器
        parts = sandbox_env.split(':')
        if len(parts) < 2:
            raise ReleaseError('RELEASE_TEST_SANDBOX format: root:state[:controller_class]')
        
        root = Path(parts[0])
        state = Path(parts[1])
        
        # Guard：拒绝生产路径（包括 .., symlink）
        prod_paths = ['/opt/inspect-platform', '/opt/inspect-ops']
        
        # 解析路径以处理 .. 和符号链接
        try:
            root_resolved = root.resolve()
            state_resolved = state.resolve()
        except Exception:
            # 如果解析失败（路径不存在等），使用原始路径
            root_resolved = root.absolute()
            state_resolved = state.absolute()
        
        # 检查解析后的路径
        for prod_path in prod_paths:
            prod_path_obj = Path(prod_path)
            # 检查是否是生产路径或其子目录
            if (str(root_resolved).startswith(str(prod_path)) or
                str(state_resolved).startswith(str(prod_path)) or
                root_resolved == prod_path_obj or
                state_resolved == prod_path_obj or
                prod_path_obj in root_resolved.parents or
                prod_path_obj in state_resolved.parents):
                raise ReleaseError('SANDBOX_PRODUCTION_PATH_REJECTED')
        
        # 控制器类（默认 LocalController）
        controller_class_name = parts[2] if len(parts) > 2 else 'LocalController'
        
        # 动态导入控制器
        if controller_class_name == 'LocalController':
            # 本地回环 HTTP 控制器（不调用 systemctl，使用真实 HTTP）
            class LocalController:
                def __init__(self, root):
                    self.root = Path(root)
                    self.runlock = None
                    self.calls = []
                    self.http_port = None
                    self.http_server = None
                    self.http_thread = None
                
                def systemctl(self, *args):
                    self.calls.append(('systemctl',) + args)
                
                def stop(self):
                    self.calls.append(('stop',))
                    if self.runlock is None:
                        self.runlock = (self.root/'data'/'runner.lock').open('a') if (self.root/'data').exists() else None
                        if self.runlock:
                            try:
                                fcntl.flock(self.runlock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError:
                                self.runlock.close();self.runlock=None
                                raise ReleaseError('INSPECTION_BUSY')
                    # 停止旧版本 HTTP 服务（如存在）
                    self._stop_http()
                
                def backup_runtime(self):
                    self.calls.append(('backup_runtime',))
                    return str(self.root / 'mock-backup')
                
                def start_front(self):
                    self.calls.append(('start_front',))
                    # 启动新版本 HTTP 服务
                    self._start_http()
                
                def _start_http(self):
                    """启动真实 loopback HTTP 服务"""
                    import http.server
                    import socketserver
                    import threading
                    
                    # 检查是否应该失败（通过标记文件在 root/.test_health_fail_VERSION）
                    # 读取当前 app.py 判断是否应该失败
                    # 简化：通过读取 app.py 内容判断
                    should_fail = False
                    app_py = self.root / 'app.py'
                    if app_py.exists():
                        content = app_py.read_text()
                        should_fail = 'unhealthy' in content
                    
                    class HealthHandler(http.server.BaseHTTPRequestHandler):
                        def do_GET(handler_self):
                            if should_fail:
                                handler_self.send_response(503)
                                handler_self.end_headers()
                                handler_self.wfile.write(b'unhealthy')
                            else:
                                handler_self.send_response(200)
                                handler_self.end_headers()
                                handler_self.wfile.write(b'ok')
                        def log_message(handler_self, *args):
                            pass
                    
                    self.http_server = socketserver.TCPServer(('127.0.0.1', 0), HealthHandler)
                    self.http_port = self.http_server.server_address[1]
                    
                    # 记录端口
                    port_file = self.root / '.test_http_port'
                    port_file.write_text(str(self.http_port))
                    
                    # 启动服务线程
                    self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
                    self.http_thread.start()
                
                def _stop_http(self):
                    """停止 HTTP 服务"""
                    if self.http_server:
                        self.http_server.shutdown()
                        self.http_server.server_close()
                        self.http_server = None
                        self.http_thread = None
                    
                    port_file = self.root / '.test_http_port'
                    if port_file.exists():
                        port_file.unlink()
                    self.http_port = None
                
                def get(self, path, status=200, contains=None):
                    self.calls.append(('get', path, status, contains))
                    # 真实 HTTP 请求到 loopback
                    if self.http_port is None:
                        # 读取端口文件
                        port_file = self.root / '.test_http_port'
                        if not port_file.exists():
                            raise ReleaseError('HEALTH_CHECK_FAILED')
                        self.http_port = int(port_file.read_text().strip())
                    
                    # 发送真实 HTTP 请求
                    import urllib.request
                    try:
                        url = 'http://127.0.0.1:{}{}'.format(self.http_port, path)
                        resp = urllib.request.urlopen(url, timeout=2)
                        body = resp.read()
                        if resp.status != status:
                            raise ReleaseError('HEALTH_CHECK_FAILED')
                        if contains and contains not in body:
                            raise ReleaseError('HEALTH_CHECK_FAILED')
                        return body
                    except Exception:
                        raise ReleaseError('HEALTH_CHECK_FAILED')
                
                def health(self):
                    self.calls.append(('health',))
                    # 真实健康检查：尝试 HTTP 请求
                    try:
                        self.get('/qa/api/health', contains=b'ok')
                        self.get('/qa/')
                        self.get('/')
                    except Exception:
                        raise ReleaseError('HEALTH_CHECK_FAILED')
                
                def start_background(self):
                    self.calls.append(('start_background',))
                    if self.runlock is not None:
                        self.runlock.close();self.runlock=None
                
                def final_health(self):
                    self.calls.append(('final_health',))
                    # 真实健康检查
                    try:
                        self.get('/qa/api/health', contains=b'ok')
                    except Exception:
                        raise ReleaseError('HEALTH_CHECK_FAILED')
            
            controller = LocalController(root)
        else:
            raise ReleaseError('UNKNOWN_CONTROLLER_CLASS')
        
        os.umask(0o077)
        m = Manager(root, state, controller)
    else:
        # 生产模式：root only，固定路径，真实 Systemd
        if os.geteuid()!=0:
            raise ReleaseError('ROOT_SSH_ONLY')
        os.umask(0o077)
        root=Path('/opt/inspect-platform')
        m=Manager(root,Path('/opt/inspect-ops/state'),Systemd(root))
    
    a=sys.argv[1:]
    if a==['status']:result=m.status()
    elif len(a)==2 and a[0]=='baseline':result=m.baseline(a[1])
    elif len(a)==3 and a[0]=='prepare':result=m.prepare(a[1],a[2])
    elif len(a)==2 and a[0] in ('deploy','rollback'):result=m.deploy(a[1])
    elif a==['recover']:result=m.recover()
    else:raise ReleaseError('USAGE: status | baseline VERSION | prepare ZIP VERSION | deploy VERSION | rollback VERSION | recover')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except ReleaseError as e:print(json.dumps({'ok':False,'code':str(e)}));sys.exit(1)
    except Exception:print(json.dumps({'ok':False,'code':'RELEASE_ERROR_REDACTED'}));sys.exit(1)
