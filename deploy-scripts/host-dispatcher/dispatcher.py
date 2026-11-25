#!/usr/bin/env python3
"""
宿主部署 Dispatcher
部署位置：/opt/inspect-deploy/dispatcher.py
权限：root:root, 755
运行：systemd service

功能：
- 监听 /opt/inspect-platform/data/deploy-spool/
- 验证部署请求（仅 main 分支）
- 验证制品完整性
- 执行部署（调用固定 release.py）
- 写入结果
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SPOOL = Path('/opt/inspect-platform/data/deploy-spool')
LOG_FILE = Path('/opt/inspect-deploy/dispatcher.log')

def log(msg):
    """写日志"""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {msg}\n"
    print(line, end='')
    sys.stdout.flush()
    
    if LOG_FILE.parent.exists():
        with open(LOG_FILE, 'a') as f:
            f.write(line)

def validate_request(req):
    """验证请求"""
    # 分支验证
    allowed_branches = ('main', 'refs/heads/main', 'origin/main')
    if req.get('branch') not in allowed_branches:
        raise ValueError(f"拒绝非 main 分支: {req.get('branch')}")
    
    # 制品存在性
    artifact = Path(req.get('artifact', ''))
    if not artifact.exists():
        raise FileNotFoundError(f"制品不存在: {artifact}")
    
    artifact_sha256 = Path(req.get('artifact_sha256', ''))
    if not artifact_sha256.exists():
        raise FileNotFoundError(f"哈希文件不存在: {artifact_sha256}")
    
    # 验证 SHA256
    log(f"验证制品完整性: {artifact.name}")
    result = subprocess.run(
        ['sha256sum', '-c', artifact_sha256.name],
        cwd=artifact.parent,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise ValueError(f"制品完整性校验失败: {result.stderr}")
    
    log("✓ 请求验证通过")
    return artifact

def execute_deploy(artifact, commit):
    """执行部署"""
    version = f"v{time.strftime('%Y%m%d-%H%M%S')}-{commit[:8]}"
    log(f"部署版本: {version}")
    
    # Prepare
    log("执行: prepare")
    subprocess.run(
        ['sudo', '/usr/bin/python3', '/opt/inspect-platform/ops/release.py', 
         'prepare', str(artifact), version],
        check=True,
        capture_output=True
    )
    
    # Deploy
    log("执行: deploy")
    subprocess.run(
        ['sudo', '/usr/bin/python3', '/opt/inspect-platform/ops/release.py', 
         'deploy', version],
        check=True,
        capture_output=True
    )
    
    log(f"✓ 部署成功: {version}")
    return version

def process_request(req_file):
    """处理一个部署请求"""
    log(f"处理请求: {req_file.name}")
    
    try:
        with open(req_file) as f:
            req = json.load(f)
        
        # 验证
        artifact = validate_request(req)
        
        # 执行部署
        version = execute_deploy(artifact, req['commit'])
        
        # 写入成功结果
        result_file = Path(req.get('result_file', 
                                   str(req_file).replace('request-', 'result-')))
        with open(result_file, 'w') as f:
            json.dump({
                'status': 'success',
                'version': version,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
            }, f, indent=2)
        
        # 删除请求文件
        req_file.unlink()
        log(f"✓ 请求处理完成")
        
    except Exception as e:
        log(f"✗ 请求处理失败: {e}")
        
        # 写入失败结果
        try:
            with open(req_file) as f:
                req = json.load(f)
            
            result_file = Path(req.get('result_file', 
                                       str(req_file).replace('request-', 'result-')))
            with open(result_file, 'w') as f:
                json.dump({
                    'status': 'failed',
                    'error': str(e),
                    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
                }, f, indent=2)
        except Exception:
            pass
        
        # 删除请求文件
        req_file.unlink()

def main():
    """主循环"""
    log("=" * 60)
    log("Inspect Deploy Dispatcher 启动")
    log(f"监听目录: {SPOOL}")
    log("=" * 60)
    
    # 确保 spool 目录存在
    SPOOL.mkdir(parents=True, exist_ok=True, mode=0o755)
    
    while True:
        try:
            # 扫描请求文件
            requests = sorted(SPOOL.glob('request-*.json'))
            
            for req_file in requests:
                process_request(req_file)
            
            # 清理旧结果文件（超过 1 小时）
            cutoff = time.time() - 3600
            for result_file in SPOOL.glob('result-*.json'):
                if result_file.stat().st_mtime < cutoff:
                    result_file.unlink()
            
        except Exception as e:
            log(f"主循环错误: {e}")
        
        time.sleep(2)

if __name__ == '__main__':
    main()
