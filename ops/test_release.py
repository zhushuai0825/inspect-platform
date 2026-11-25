import contextlib,http.server,importlib.util,json,os,socketserver,tempfile,threading,unittest,zipfile
from pathlib import Path
from urllib.request import urlopen
from . import release as r
class Server(socketserver.ThreadingMixIn,http.server.HTTPServer):daemon_threads=True
class LocalController:
    def __init__(self,root):
        self.root=root;self.calls=[];self.running=True;self.fail_stop=False
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body=(owner.root/'app.py').read_bytes()
                ok=owner.running and b'unhealthy' not in body
                self.send_response(200 if ok else 503);self.end_headers();self.wfile.write(b'ok' if ok else b'not-ready')
            def log_message(self,*args):pass
        self.server=Server(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever);self.thread.daemon=True;self.thread.start()
    def stop(self):
        self.calls.append('stop')
        if self.fail_stop:raise r.ReleaseError('BUSY_FIXTURE')
        self.running=False
    def start_front(self):self.calls.append('front');self.running=True
    def health(self):
        self.calls.append('health')
        with urlopen('http://127.0.0.1:%s/'%self.server.server_port,timeout=2) as x:
            if x.status!=200:raise r.ReleaseError('UNHEALTHY_FIXTURE')
    def start_background(self):self.calls.append('background')
    def final_health(self):self.calls.append('final');self.health()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inspect-release-test-');self.base=Path(self.tmp.name);self.root=self.base/'app';self.root.mkdir()
        for name in r.RUNTIME:
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(self.content(name,'old'))
        d=self.root/'data';d.mkdir();(d/'mail.env').write_text('PRIVATE_FIXTURE');(d/'mail.env').chmod(0o600);(d/'secret.env').write_text('PRIVATE_OTHER_FIXTURE');(d/'platform.db').write_bytes(b'DATABASE_FIXTURE')
        (d/'reports').mkdir();(d/'reports'/'keep.json').write_text('{"fixture":true}')
        self.controller=LocalController(self.root);self.m=r.Manager(self.root,self.base/'state',self.controller);self.m.baseline('baseline')
    def tearDown(self):self.controller.close();self.tmp.cleanup()
    def content(self,name,version):
        if name.startswith('tests/'):
            return "import os,unittest\nclass Fixture(unittest.TestCase):\n def test_isolated(self):self.assertIn('inspect-release-preflight-',os.environ['QA_DATA_DIR'])\n"
        return "VALUE=%r\n"%version if name.endswith('.py') else version+'\n'
    def archive(self,version='new',extra=None,bad_tests=False):
        p=self.base/(version+'.zip')
        with zipfile.ZipFile(str(p),'w') as z:
            for name in sorted(r.RUNTIME):
                data=self.content(name,version)
                if bad_tests and name=='tests/test_platform.py':data='raise RuntimeError("fixture preflight failure")\n'
                z.writestr('inspect-platform/'+name,data)
            if extra:
                for name,data in extra:z.writestr(name,data)
        return p
    def prepared(self,version='new',**kw):self.m.prepare(self.archive(version,**kw),version)
    def assert_data(self):
        self.assertEqual((self.root/'data/platform.db').read_bytes(),b'DATABASE_FIXTURE');self.assertEqual((self.root/'data/mail.env').read_text(),'PRIVATE_FIXTURE');self.assertEqual((self.root/'data/reports/keep.json').read_text(),'{"fixture":true}');self.assertEqual((self.root/'data/mail.env').stat().st_mode&0o777,0o600)
    def test_successful_versioned_publish_preserves_data(self):
        self.prepared();self.assertEqual(self.m.deploy('new')['health'],'passed');self.assertIn("'new'",(self.root/'app.py').read_text());self.assertEqual(self.m.status()['current']['version'],'new');self.assert_data()
    def test_unhealthy_candidate_auto_rolls_back_real_loopback_http(self):
        self.prepared('unhealthy')
        with self.assertRaisesRegex(r.ReleaseError,'DEPLOY_FAILED_ROLLED_BACK'):self.m.deploy('unhealthy')
        self.assertIn("'old'",(self.root/'app.py').read_text());self.controller.health();self.assertEqual(self.m.status()['transaction']['phase'],'rolled_back');self.assert_data()
    def test_preflight_failure_never_stops_services(self):
        with self.assertRaisesRegex(r.ReleaseError,'PREFLIGHT_TEST_FAILED'):self.m.prepare(self.archive(bad_tests=True),'bad')
        self.assertEqual(self.controller.calls,[]);self.assert_data()
    def test_checksum_tampering_rejected_before_stop(self):
        self.prepared();(self.m.path('new')/'source/app.py').write_text('VALUE=0')
        with self.assertRaisesRegex(r.ReleaseError,'CHECKSUM_MISMATCH'):self.m.deploy('new')
        self.assertEqual(self.controller.calls,[])
    def test_private_file_in_archive_rejected(self):
        with self.assertRaisesRegex(r.ReleaseError,'FORBIDDEN_ARCHIVE_PATH'):self.m.prepare(self.archive(extra=[('inspect-platform/data/mail.env','FIXTURE_ONLY')]),'bad')
    def test_parent_traversal_rejected(self):
        with self.assertRaisesRegex(r.ReleaseError,'FORBIDDEN_ARCHIVE_PATH'):self.m.prepare(self.archive(extra=[('inspect-platform/../escape','fixture')]),'bad')
    def test_zip_symlink_rejected(self):
        p=self.archive();i=zipfile.ZipInfo('inspect-platform/ops/release.py');i.external_attr=(0o120777<<16)
        with zipfile.ZipFile(str(p),'a') as z:z.writestr(i,'/etc/passwd')
        with self.assertRaisesRegex(r.ReleaseError,'FORBIDDEN_ARCHIVE_PATH'):self.m.prepare(p,'bad')
    def test_zip_duplicate_rejected(self):
        with self.assertRaisesRegex(r.ReleaseError,'FORBIDDEN_ARCHIVE_PATH'):self.m.prepare(self.archive(extra=[('inspect-platform/app.py','VALUE=0')]),'bad')
    def test_oversize_archive_rejected(self):
        with self.assertRaisesRegex(r.ReleaseError,'ARCHIVE_LIMIT'):self.m.prepare(self.archive(extra=[('inspect-platform/ops/RELEASE.md','x'*1048577)]),'bad')
    def test_serialization(self):
        with self.m.lock():
            with self.assertRaisesRegex(r.ReleaseError,'RELEASE_BUSY'):self.m.baseline('another')
    def test_crash_journal_recovery_restores_previous_code(self):
        self.m.snapshot('before-crash');(self.root/'app.py').write_text("VALUE='unhealthy'\n")
        r.save(self.m.state/'transaction.json',{'phase':'installed','previous':'before-crash','target':'interrupted'})
        self.m.recover();self.assertIn("'old'",(self.root/'app.py').read_text());self.assertEqual(self.m.status()['transaction']['phase'],'rolled_back');self.assert_data()
    def test_unfinished_transaction_blocks_new_release(self):
        self.prepared();r.save(self.m.state/'transaction.json',{'phase':'installed'})
        with self.assertRaisesRegex(r.ReleaseError,'RECOVERY_REQUIRED'):self.m.deploy('new')
    def test_invalid_version_and_immutable_version(self):
        with self.assertRaisesRegex(r.ReleaseError,'INVALID_VERSION'):self.m.path('../escape')
        self.prepared()
        with self.assertRaisesRegex(r.ReleaseError,'VERSION_EXISTS'):self.m.prepare(self.archive(),'new')
    def test_source_snapshot_never_contains_runtime_data(self):
        files=r.load(self.m.path('baseline')/'manifest.json')['files'];self.assertTrue(set(files).issubset(r.RUNTIME));self.assertNotIn('data/mail.env',files)
    def test_explicit_known_release_rollback_preserves_data(self):
        self.prepared();self.m.deploy('new');self.m.deploy('baseline');self.assertIn("'old'",(self.root/'app.py').read_text());self.assert_data()
    def test_failed_rollback_requires_explicit_manual_recovery(self):
        self.prepared('unhealthy')
        self.controller.health=lambda: (_ for _ in ()).throw(r.ReleaseError('FIXTURE_ALWAYS_UNAVAILABLE'))
        with self.assertRaisesRegex(r.ReleaseError,'ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED'):self.m.deploy('unhealthy')
        self.assertEqual(self.m.status()['transaction']['phase'],'rollback_failed');self.assertIn("'old'",(self.root/'app.py').read_text());self.assert_data()
    def test_busy_guard_aborts_without_replacing_files(self):
        self.prepared();self.controller.fail_stop=True
        with self.assertRaisesRegex(r.ReleaseError,'DEPLOY_FAILED_ROLLED_BACK'):self.m.deploy('new')
        self.assertIn("'old'",(self.root/'app.py').read_text());self.assertEqual(self.m.status()['transaction']['phase'],'rolled_back')
if __name__=='__main__':unittest.main(verbosity=2)
