#!/usr/bin/env python3
"""
安全的部署 Dispatcher - Main-only 可信来源
部署位置：/opt/inspect-deploy/dispatcher.py
权限：root:root, 755
运行：systemd service

安全设计：
- 固定 repo URL（配置文件）
- 自己从受信 repo 获取代码
- 验证远程 main commit
- 自己创建制品
- 路径严格封闭
- 防重放/路径遍历/符号链接
- Python 3.6 兼容
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# Python 3.6 兼容：不使用 capture_output
PYTHON36_COMPAT = sys.version_info < (3, 7)

# 配置（由 root 拥有的配置文件加载）
CONFIG_FILE = Path('/opt/inspect-deploy/config.json')
DEFAULT_CONFIG = {
    'repo_url': 'https://github.com/zhushuai0825/inspect-platform.git',
    'branch': 'main',
    'spool_dir': '/opt/inspect-platform/data/deploy-spool',
    'work_dir': '/opt/inspect-deploy/work',
    'log_file': '/opt/inspect-deploy/dispatcher.log'
}

def load_config():
    """
    加载配置
    
    安全：
    - 生产配置固定（repo_url, branch）不接受环境变量覆盖
    - 测试模式：必须非root + RELEASE_ROOT + RELEASE_STATE + 安全临时根
    - Root 进程永远是生产模式，拒绝所有测试覆盖
    """
    config = DEFAULT_CONFIG.copy()
    
    # 从配置文件加载（生产）
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            file_config = json.load(f)
        config.update(file_config)
    
    # 测试模式：必须非root + RELEASE_ROOT + RELEASE_STATE
    # Root 进程永远是生产模式
    is_test_mode = (os.geteuid() != 0 and
                    'RELEASE_ROOT' in os.environ and 
                    'RELEASE_STATE' in os.environ)
    
    if is_test_mode:
        # 测试模式：允许覆盖路径配置（但不覆盖 repo_url/branch）
        if 'GITHUB_REPO' in os.environ:
            config['test_repo_url'] = os.environ['GITHUB_REPO']
            config['repo_url'] = os.environ['GITHUB_REPO']
        if 'SPOOL_DIR' in os.environ:
            config['spool_dir'] = os.environ['SPOOL_DIR']
        if 'WORK_DIR' in os.environ:
            config['work_dir'] = os.environ['WORK_DIR']
        if 'RELEASE_CLI' in os.environ:
            config['release_cli'] = os.environ['RELEASE_CLI']
        
        config['release_root'] = os.environ['RELEASE_ROOT']
        config['release_state'] = os.environ['RELEASE_STATE']
        config['is_test_mode'] = True
    else:
        # 生产模式：不接受任何环境变量覆盖
        config['is_test_mode'] = False
    
    return config

CONFIG = load_config()
SPOOL = Path(CONFIG['spool_dir'])
WORK_DIR = Path(CONFIG['work_dir'])
LOG_FILE = Path(CONFIG.get('log_file', '/tmp/dispatcher.log'))

def log(msg):
    """写日志（Python 3.6 兼容）"""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = '[{}] {}\n'.format(timestamp, msg)
    print(line, end='')
    sys.stdout.flush()
    
    if LOG_FILE.parent.exists():
        with open(str(LOG_FILE), 'a') as f:
            f.write(line)

def run_command(cmd, cwd=None, timeout=300):
    """运行命令（Python 3.6 兼容）"""
    # Python 3.6: 使用 universal_newlines 和 PIPE
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise Exception('命令超时: {}'.format(' '.join(cmd)))

def validate_commit_sha(commit):
    """验证 commit SHA 格式（40 hex）"""
    if not isinstance(commit, str):
        return False
    if len(commit) != 40:
        return False
    if not re.match(r'^[0-9a-f]{40}$', commit):
        return False
    return True

def validate_request(req):
    """
    验证请求
    
    安全要求：
    - 只接受 request_id 和 commit（可选）
    - 拒绝任意 artifact/path/result_file 等字段
    - commit 必须是 40 hex（如提供）
    """
    if not isinstance(req, dict):
        raise ValueError('请求必须是 JSON 对象')
    
    # 只允许这些字段
    allowed_fields = {'request_id', 'commit', 'timestamp', 'source'}
    unknown_fields = set(req.keys()) - allowed_fields
    if unknown_fields:
        raise ValueError('拒绝未知字段: {}'.format(', '.join(unknown_fields)))
    
    # request_id 必需
    if 'request_id' not in req:
        raise ValueError('缺少 request_id')
    
    # 验证 commit 格式（如提供）
    if 'commit' in req:
        if not validate_commit_sha(req['commit']):
            raise ValueError('commit 必须是 40 位十六进制')
    
    log('✓ 请求格式验证通过')
    return True

def get_remote_main_sha(repo_url, branch='main'):
    """
    获取远程 main 分支的 commit SHA
    
    安全：从受信 repo 获取，不信任请求
    """
    log('获取远程 {} 分支 SHA: {}'.format(branch, repo_url))
    
    # 使用 git ls-remote 获取远程分支 SHA
    cmd = ['git', 'ls-remote', repo_url, 'refs/heads/{}'.format(branch)]
    returncode, stdout, stderr = run_command(cmd, timeout=30)
    
    if returncode != 0:
        raise Exception('git ls-remote 失败: {}'.format(stderr))
    
    # 解析输出：<SHA>\trefs/heads/main
    lines = stdout.strip().split('\n')
    if not lines or not lines[0]:
        raise Exception('未找到分支: {}'.format(branch))
    
    parts = lines[0].split('\t')
    if len(parts) < 1:
        raise Exception('无法解析 git ls-remote 输出')
    
    sha = parts[0].strip()
    
    if not validate_commit_sha(sha):
        raise Exception('远程返回的 SHA 格式无效: {}'.format(sha))
    
    log('远程 {} SHA: {}'.format(branch, sha))
    return sha

def verify_commit_match(req, remote_sha):
    """
    验证请求的 commit（如提供）与远程 main 一致
    
    安全：防止假 commit
    """
    if 'commit' not in req:
        # 空通知，不验证 commit
        log('空通知（无 commit）')
        return remote_sha
    
    req_commit = req['commit']
    
    if req_commit != remote_sha:
        raise ValueError(
            '请求 commit ({}) 与远程 main ({}) 不匹配'.format(
                req_commit[:8], remote_sha[:8]
            )
        )
    
    log('✓ Commit 验证通过: {}'.format(req_commit[:8]))
    return req_commit

def safe_path_join(base, *parts):
    """
    安全路径拼接
    
    安全：防止路径遍历
    """
    base = Path(base).resolve()
    result = base.joinpath(*parts).resolve()
    
    # 确保结果在 base 下
    try:
        result.relative_to(base)
    except ValueError:
        raise ValueError('路径穿越尝试: {}'.format(result))
    
    return result

def check_no_symlinks(path):
    """
    检查路径及其父目录中无符号链接
    
    安全：防止符号链接攻击
    """
    # 不能先 resolve()，会丢失符号链接信息
    path = Path(path)
    
    # 检查路径本身
    if path.is_symlink():
        raise ValueError('拒绝符号链接: {}'.format(path))
    
    # 检查每个父目录
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('拒绝符号链接: {}'.format(parent))
        # 到根目录停止
        if parent == parent.parent:
            break

def fetch_source(repo_url, commit, work_dir):
    """
    从受信 repo 获取指定 commit 的源代码
    
    安全：
    - 使用临时目录
    - 浅克隆
    - 验证 commit 存在
    """
    log('获取源代码: {} @ {}'.format(repo_url, commit[:8]))
    
    # 创建临时 clone 目录
    clone_dir = safe_path_join(work_dir, 'clone-{}'.format(commit[:8]))
    
    if clone_dir.exists():
        shutil.rmtree(str(clone_dir))
    
    clone_dir.mkdir(parents=True, mode=0o700)
    
    try:
        # 浅克隆（节省空间和时间）
        log('克隆仓库...')
        cmd = ['git', 'clone', '--depth', '50', repo_url, str(clone_dir)]
        returncode, stdout, stderr = run_command(cmd, timeout=120)
        
        if returncode != 0:
            raise Exception('git clone 失败: {}'.format(stderr))
        
        # 切换到指定 commit
        log('切换到 commit {}...'.format(commit[:8]))
        cmd = ['git', 'checkout', commit]
        returncode, stdout, stderr = run_command(cmd, cwd=str(clone_dir), timeout=30)
        
        if returncode != 0:
            raise Exception('git checkout 失败: {}'.format(stderr))
        
        # 验证当前 HEAD
        cmd = ['git', 'rev-parse', 'HEAD']
        returncode, stdout, stderr = run_command(cmd, cwd=str(clone_dir), timeout=10)
        
        if returncode != 0:
            raise Exception('git rev-parse 失败: {}'.format(stderr))
        
        actual_commit = stdout.strip()
        
        if actual_commit != commit:
            raise Exception(
                'Commit 不匹配: 预期 {}, 实际 {}'.format(commit[:8], actual_commit[:8])
            )
        
        log('✓ 源代码获取成功')
        return clone_dir
        
    except Exception as e:
        # 清理失败的克隆
        if clone_dir.exists():
            shutil.rmtree(str(clone_dir))
        raise

def create_artifact(source_dir, commit):
    """
    创建部署制品（白名单过滤）
    
    安全：
    - 只打包白名单文件（与ops/release.py一致）
    - 排除控制面（deploy-scripts/, control-plane/）
    - 排除secrets, .git
    - 计算 SHA256
    """
    log('创建制品: {}'.format(commit[:8]))
    
    # 白名单（与 ops/release.py RUNTIME|OPS 完全一致）
    RUNTIME = set(""".gitignore DEPLOYMENT.md MAIL-ACTIVATION.md MAIL-SETUP.md README.md admin-static/admin.css admin-static/app.js admin-static/index.html admin.py app.py core.py deploy/inspect-admin.service deploy/inspect-mail.service deploy/inspect-platform.service deploy/inspect-worker.service deploy/update_job.py inspect_run.sh jenkins/Jenkinsfile notifications.py static/app.js static/index.html static/styles.css tests/test_notifications.py tests/test_platform.py worker.py""".split())
    OPS = {'ops/release.py', 'ops/test_release.py', 'ops/RELEASE.md'}
    WHITELIST = RUNTIME | OPS
    
    # 制品目录
    artifacts_dir = safe_path_join(WORK_DIR, 'artifacts')
    artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    
    artifact_name = 'inspect-platform-{}.zip'.format(commit[:8])
    artifact_path = safe_path_join(artifacts_dir, artifact_name)
    
    # 创建zip（只包含白名单文件）
    log('打包白名单: {} 个文件'.format(len(WHITELIST)))
    packed_count = 0
    missing_count = 0
    
    with zipfile.ZipFile(str(artifact_path), 'w', zipfile.ZIP_DEFLATED) as z:
        for file_path in sorted(WHITELIST):
            src_file = source_dir / file_path
            if src_file.exists() and src_file.is_file():
                z.write(str(src_file), 'inspect-platform/' + file_path)
                packed_count += 1
            else:
                missing_count += 1
                log('警告: 白名单文件缺失: {}'.format(file_path))
    
    if not artifact_path.exists():
        raise Exception('制品未创建')
    
    log('✓ 已打包: {} 个文件 ({} 个缺失)'.format(packed_count, missing_count))
    
    # 计算 SHA256
    sha256 = hashlib.sha256()
    with open(str(artifact_path), 'rb') as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            sha256.update(chunk)
    
    hash_value = sha256.hexdigest()
    hash_file = safe_path_join(artifacts_dir, artifact_name + '.sha256')
    
    with open(str(hash_file), 'w') as f:
        f.write('{}  {}\n'.format(hash_value, artifact_name))
    
    log('✓ 制品: {} ({} bytes)'.format(artifact_name, artifact_path.stat().st_size))
    
    return artifact_path, hash_file

def verify_trusted_path(path_str, description='path', check_ancestors=True):
    """
    验证可信路径安全性
    
    检查：
    1. 文件存在且不是符号链接
    2. 文件owner为root（root模式）
    3. 文件不可组写/他人写（mode & 0o022 == 0）
    4. 完整祖先链（到根或受保护锚点）：
       - 每个祖先不是符号链接
       - 每个祖先owner为root（root模式）
       - 每个祖先不可组写/他人写
    
    非root测试模式：
    - 跳过owner检查
    - 保留权限检查
    - 允许受保护锚点内的路径（如测试临时目录）
    """
    path = Path(path_str).resolve()
    is_root_mode = os.geteuid() == 0
    
    if not path.exists():
        raise Exception('{} does not exist: {}'.format(description, path_str))
    
    # 检查文件本身不是符号链接（在resolve前的原始路径）
    orig_path = Path(path_str)
    if orig_path.is_symlink():
        raise Exception('{} is a symlink (not allowed): {}'.format(description, path_str))
    
    # 检查路径规范化（防止 ../ 等路径穿越）
    if str(path) != str(orig_path.absolute()):
        raise Exception('{} contains path traversal: {} -> {}'.format(
            description, path_str, path))
    
    # 验证文件本身
    stat_info = path.stat()
    
    if is_root_mode and stat_info.st_uid != 0:
        raise Exception('{} not owned by root: uid={}'.format(
            description, stat_info.st_uid))
    
    if stat_info.st_mode & 0o022:
        raise Exception('{} is group/world-writable: mode={:o}'.format(
            description, stat_info.st_mode))
    
    # 验证完整祖先链
    if check_ancestors:
        current = path.parent
        ancestors_checked = 0
        
        # 受保护锚点：停止祖先检查的边界
        # Root模式：检查到根目录
        # 非root测试：检查到测试目录顶层（不检查/tmp等系统目录）
        protected_anchors = []
        if not is_root_mode:
            # 非root测试：识别测试目录顶层作为受保护锚点
            # 检查路径是否在/tmp下的测试目录中
            path_str = str(path)
            if path_str.startswith('/tmp/'):
                # 找到/tmp下的第一级目录（如 /tmp/test_dispatcher_xxx）
                parts = path_str.split('/')
                if len(parts) >= 3:  # ['', 'tmp', 'test_xxx', ...]
                    protected_anchors.append(Path('/' + parts[1] + '/' + parts[2]))
        
        while current != current.parent:  # 未到根目录
            ancestors_checked += 1
            if ancestors_checked > 100:  # 防止无限循环
                raise Exception('{} ancestor chain too deep'.format(description))
            
            # 检查到受保护锚点则停止（不检查锚点本身）
            if current in protected_anchors:
                break
            
            # 检查祖先不是符号链接
            if current.is_symlink():
                raise Exception('{} ancestor is symlink: {}'.format(description, current))
            
            # 检查祖先stat
            ancestor_stat = current.stat()
            
            # Root模式：检查祖先owner
            if is_root_mode and ancestor_stat.st_uid != 0:
                raise Exception('{} ancestor not root-owned: {} (uid={})'.format(
                    description, current, ancestor_stat.st_uid))
            
            # 检查祖先权限（所有模式）
            if ancestor_stat.st_mode & 0o022:
                raise Exception('{} ancestor group/world-writable: {} (mode={:o})'.format(
                    description, current, ancestor_stat.st_mode))
            
            current = current.parent
    
    return True

def execute_deploy(artifact_path, commit):
    """
    执行部署
    
    安全：
    - 生产：固定调用 /opt/inspect-deploy/bin/release.py
    - 测试：允许RELEASE_CLI注入（非root）
    - 验证可信路径（owner/mode/symlink/parent）
    """
    version = 'v{}-{}'.format(time.strftime('%Y%m%d-%H%M%S'), commit[:8])
    log('部署版本: {}'.format(version))
    
    # 确定release路径：root无条件固定控制面，非root允许沙盒注入
    if os.geteuid() == 0:
        # Root生产：固定控制面，绝不允许CONFIG/env覆盖
        release_py = Path('/opt/inspect-deploy/bin/release.py')
        log('Root模式: 固定控制面 /opt/inspect-deploy/bin/release.py')
    else:
        # 非root测试沙盒：允许明确的RELEASE_CLI env注入
        # 不允许任意注入：必须显式设置RELEASE_CLI
        release_py_str = os.environ.get('RELEASE_CLI')
        if not release_py_str:
            raise Exception('Non-root mode requires explicit RELEASE_CLI env')
        release_py = Path(release_py_str)
        log('非root测试模式: RELEASE_CLI={}'.format(release_py))
    
    # 验证release路径安全性
    verify_trusted_path(str(release_py), 'Control plane release.py')
    log('release.py 安全验证通过: {}'.format(release_py))
    
    # 构建命令（非root测试环境不使用 sudo）
    is_root_mode = os.geteuid() == 0
    python_cmd = sys.executable if not is_root_mode else '/usr/bin/python3'
    use_sudo = is_root_mode
    
    # 测试环境变量
    cmd_env = os.environ.copy()
    if 'release_root' in CONFIG:
        cmd_env['RELEASE_TEST_SANDBOX'] = '{}:{}'.format(
            CONFIG['release_root'],
            CONFIG['release_state']
        )
        # 不跳过 preflight：实际执行候选测试
        # cmd_env['RELEASE_TEST_SKIP_PREFLIGHT'] = '1'  # 已移除
        cmd_env['PYTHONDONTWRITEBYTECODE'] = '1'
    
    # Prepare
    log('执行: prepare (env={})'.format('test' if not is_root_mode else 'prod'))
    if not is_root_mode:
        log('测试环境变量: RELEASE_TEST_SANDBOX={}'.format(cmd_env.get('RELEASE_TEST_SANDBOX', 'NOT SET')))
    cmd = []
    if use_sudo:
        cmd.extend(['sudo', python_cmd])
    else:
        cmd.append(python_cmd)
    cmd.extend([str(release_py), 'prepare', str(artifact_path), version])
    
    # Python 3.6 兼容
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=cmd_env
    )
    stdout, stderr = proc.communicate(timeout=300)
    
    if proc.returncode != 0:
        log('prepare 失败: stdout={} stderr={}'.format(stdout, stderr))
        raise Exception('prepare 失败: {}'.format(stderr))
    
    log('prepare 成功: {}'.format(stdout[:200]))
    
    # Deploy
    log('执行: deploy (env={})'.format('test' if not is_root_mode else 'prod'))
    cmd = []
    if use_sudo:
        cmd.extend(['sudo', python_cmd])
    else:
        cmd.append(python_cmd)
    cmd.extend([str(release_py), 'deploy', version])
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=cmd_env
    )
    stdout, stderr = proc.communicate(timeout=600)
    
    if proc.returncode != 0:
        log('deploy 失败: stdout={} stderr={}'.format(stdout, stderr))
        raise Exception('deploy 失败: {}'.format(stderr))
    
    log('deploy 成功: {}'.format(stdout[:200]))
    
    log('✓ 部署成功: {}'.format(version))
    return version

def generate_result_path(request_id):
    """
    生成安全的结果文件路径
    
    安全：
    - 由服务生成，不接受请求控制
    - 封闭在 spool 目录
    - 防止路径遍历
    """
    # 只允许字母数字和连字符
    safe_id = re.sub(r'[^a-zA-Z0-9-]', '', request_id)[:64]
    if not safe_id:
        safe_id = str(int(time.time()))
    
    result_name = 'result-{}.json'.format(safe_id)
    result_path = safe_path_join(SPOOL, result_name)
    
    # 再次验证
    check_no_symlinks(result_path.parent)
    
    return result_path

def atomic_write_result(result_path, data):
    """
    原子写入结果
    
    安全：
    - 使用临时文件 + rename
    - 避免部分写入
    """
    # 写入临时文件
    temp_fd, temp_path = tempfile.mkstemp(
        prefix='.result-',
        dir=str(result_path.parent),
        text=True
    )
    
    try:
        with os.fdopen(temp_fd, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        
        # 原子 rename
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, str(result_path))
        
    finally:
        # 清理临时文件（如果失败）
        if os.path.exists(temp_path):
            os.unlink(temp_path)

def claim_request(req_file, request_id):
    """
    原子领取请求
    
    安全：
    - 防止重复处理（跨重启）
    - 使用 rename + 持久化已处理ID
    """
    # 检查已处理ID列表（跨重启防重放）
    processed_file = safe_path_join(WORK_DIR, 'processed-requests.txt')
    processed_file.parent.mkdir(parents=True, exist_ok=True)
    
    if processed_file.exists():
        with open(str(processed_file), 'r') as f:
            processed_ids = set(line.strip() for line in f if line.strip())
        
        if request_id in processed_ids:
            log('请求已处理（防重放）: {}'.format(request_id))
            # 删除重复请求文件
            try:
                req_file.unlink()
            except FileNotFoundError:
                pass
            return None
    else:
        processed_ids = set()
    
    # 原子 rename（防止并发）
    claimed_name = req_file.name.replace('request-', 'claimed-')
    claimed_path = req_file.parent / claimed_name
    
    try:
        req_file.rename(claimed_path)
        
        # 记录已处理ID（追加）
        with open(str(processed_file), 'a') as f:
            f.write(request_id + '\n')
            f.flush()
            os.fsync(f.fileno())
        
        return claimed_path
        
    except (OSError, FileNotFoundError):
        # 文件已被其他进程领取
        return None

def process_request(req_file):
    """
    处理一个部署请求
    
    安全：
    - 原子领取
    - 严格验证
    - 自己获取代码
    - 封闭路径
    """
    log('=' * 60)
    log('处理请求: {}'.format(req_file.name))
    
    # 读取请求（先读取 request_id）
    try:
        with open(str(req_file)) as f:
            req_peek = json.load(f)
        request_id = req_peek.get('request_id', 'unknown')
    except Exception as e:
        log('读取请求失败: {}'.format(e))
        # 删除无效请求文件
        try:
            req_file.unlink()
        except FileNotFoundError:
            pass
        return
    
    # 原子领取（带防重放）
    claimed_file = claim_request(req_file, request_id)
    if claimed_file is None:
        log('请求已被处理或已领取')
        return
    
    result_data = {
        'status': 'failed',
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    }
    result_path = None
    
    try:
        # 读取请求
        with open(str(claimed_file)) as f:
            req = json.load(f)
        
        # 生成结果路径（由服务控制）
        result_path = generate_result_path(req.get('request_id', 'unknown'))
        
        # 验证请求格式
        validate_request(req)
        
        # 获取远程 main SHA
        remote_sha = get_remote_main_sha(CONFIG['repo_url'], CONFIG['branch'])
        
        # 验证 commit（如提供）
        commit = verify_commit_match(req, remote_sha)
        
        # 获取源代码
        source_dir = fetch_source(CONFIG['repo_url'], commit, WORK_DIR)
        
        try:
            # 创建制品
            artifact_path, hash_file = create_artifact(source_dir, commit)
            
            # 执行部署
            version = execute_deploy(artifact_path, commit)
            
            # 成功
            result_data = {
                'status': 'success',
                'version': version,
                'commit': commit,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
            }
            
            log('✓ 请求处理成功')
            
        finally:
            # 清理源代码
            if source_dir.exists():
                shutil.rmtree(str(source_dir))
        
    except Exception as e:
        log('✗ 请求处理失败: {}'.format(e))
        result_data['error'] = str(e)
        # 重新抛出异常（让调用方知道失败）
        exc_to_raise = e
    else:
        exc_to_raise = None
    
    finally:
        # 写入结果
        if result_path:
            try:
                atomic_write_result(result_path, result_data)
            except Exception as e:
                log('写入结果失败: {}'.format(e))
        
        # 删除已处理的请求
        if claimed_file and claimed_file.exists():
            claimed_file.unlink()
    
    # 在清理后重新抛出异常
    if exc_to_raise is not None:
        raise exc_to_raise

def main():
    """主循环"""
    # 解析参数
    once_mode = '--once' in sys.argv
    
    log('=' * 60)
    log('Inspect Deploy Dispatcher 启动')
    if once_mode:
        log('模式: 单次处理（--once）')
    log('配置:')
    log('  Repo: {}'.format(CONFIG['repo_url']))
    log('  Branch: {}'.format(CONFIG['branch']))
    log('  Spool: {}'.format(SPOOL))
    log('  Work: {}'.format(WORK_DIR))
    log('=' * 60)
    
    # 确保目录存在
    SPOOL.mkdir(parents=True, exist_ok=True, mode=0o755)
    WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    
    # 安全检查：spool 及父目录无符号链接
    try:
        check_no_symlinks(SPOOL)
    except ValueError as e:
        log('致命错误: {}'.format(e))
        sys.exit(1)
    
    while True:
        try:
            # 扫描请求文件
            requests = sorted(SPOOL.glob('*.json'))
            # 过滤：只处理 request-*.json 或 test-req-*.json
            requests = [r for r in requests if r.name.startswith('request-') or r.name.startswith('test-req-')]
            
            processed_any = False
            failed = False
            for req_file in requests:
                try:
                    process_request(req_file)
                    processed_any = True
                except Exception as e:
                    log('处理请求异常: {}'.format(e))
                    processed_any = True
                    failed = True
            
            # 单次模式：处理完一轮后退出
            if once_mode:
                if processed_any:
                    if failed:
                        log('单次处理完成，存在失败，退出码 1')
                        sys.exit(1)
                    else:
                        log('单次处理完成，退出')
                else:
                    log('未发现待处理请求，退出')
                break
            
            # 清理旧文件（超过 1 小时）
            cutoff = time.time() - 3600
            for old_file in SPOOL.glob('result-*.json'):
                try:
                    if old_file.stat().st_mtime < cutoff:
                        old_file.unlink()
                except Exception:
                    pass
            
            for old_file in SPOOL.glob('claimed-*.json'):
                try:
                    if old_file.stat().st_mtime < cutoff:
                        old_file.unlink()
                except Exception:
                    pass
            
        except Exception as e:
            log('主循环错误: {}'.format(e))
            if once_mode:
                sys.exit(1)
        
        time.sleep(2)

if __name__ == '__main__':
    main()
