// CI/CD Pipeline for inspect-platform
// SECURITY: This pipeline runs in Docker container with production mounts.
// PRs can modify this file and execute arbitrary code.
// Therefore: ONLY main branch builds are allowed in production Jenkins.
// PRs MUST use separate isolated CI without production access.

pipeline {
  agent any
  
  options {
    timeout(time: 20, unit: 'MINUTES')
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '40', artifactNumToKeepStr: '10'))
  }
  
  environment {
    TZ = 'Asia/Shanghai'
    // Artifact filename based on commit
    ARTIFACT_NAME = "inspect-platform-${env.GIT_COMMIT?.take(8) ?: 'unknown'}.zip"
    // Workspace-local paths
    BUILD_DIR = "${env.WORKSPACE}/build"
  }
  
  stages {
    stage('安全检查') {
      steps {
        script {
          echo "=== 构建信息 ==="
          echo "分支: ${env.BRANCH_NAME}"
          echo "提交: ${env.GIT_COMMIT}"
          echo "PR: ${env.CHANGE_ID ?: '(不是PR)'}"
          
          // SECURITY: Reject ALL non-main builds in production Jenkins
          // This Jenkins runs in Docker with production mounts (/opt/inspect-platform)
          // PRs can modify Jenkinsfile and execute arbitrary code
          // PRs MUST NOT run here - use separate isolated CI instead
          if (env.BRANCH_NAME != 'main' || env.CHANGE_ID != null) {
            error """
╔════════════════════════════════════════════════════════════════╗
║  🚨 安全拒绝：非 main 分支不允许在生产 Jenkins 构建          ║
╚════════════════════════════════════════════════════════════════╝

原因：
1. 本 Jenkins 运行在 Docker 容器内，挂载生产目录 /opt/inspect-platform
2. PR 可修改 Jenkinsfile 执行任意代码，访问生产凭证
3. 容器内可能接触 data/secret.env、data/mail.env 等机密文件

当前构建：
  分支: ${env.BRANCH_NAME}
  PR: ${env.CHANGE_ID ?: '无'}
  
允许条件：
  仅 main 分支（非 PR）可在此 Jenkins 构建

PR 测试方案：
  请配置独立 CI 环境（无生产挂载、无生产凭证）
  或使用 GitHub Actions 进行 PR 测试
  详见 JENKINS-SETUP.md "PR 安全隔离方案" 章节
            """
          }
          
          echo "✓ 安全检查通过：main 分支构建"
        }
      }
    }
    
    stage('准备') {
      steps {
        script {
          // Clean build directory
          sh "rm -rf ${env.BUILD_DIR}"
          sh "mkdir -p ${env.BUILD_DIR}"
        }
      }
    }
    
    stage('语法检查') {
      steps {
        echo '=== Python 语法检查 ==='
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          python3 -m py_compile app.py core.py admin.py worker.py notifications.py
          python3 -m py_compile deploy/update_job.py ops/release.py ops/test_release.py
        '''
        
        echo '=== JavaScript 语法检查 ==='
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          node --check static/app.js
          node --check admin-static/app.js
        '''
        
        echo '=== Bash 语法检查 ==='
        sh 'bash -n inspect_run.sh'
      }
    }
    
    stage('自动化测试') {
      steps {
        echo '=== 运行单元测试与集成测试 ==='
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          
          # Run tests in temporary directory, no production credentials
          export PYTHONPATH="${WORKSPACE}"
          export PYTHONDONTWRITEBYTECODE=1
          export QA_DATA_DIR="$(mktemp -d)"
          
          trap "rm -rf \\"${QA_DATA_DIR}\\"" EXIT
          
          # Run all tests
          python3 -m unittest discover -s tests -p 'test_*.py' -v
        '''
      }
    }
    
    stage('打包制品') {
      steps {
        echo '=== 创建部署制品 ==='
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          
          cd "${WORKSPACE}"
          
          # Create artifact with inspect-platform/ prefix as expected by release.py
          TEMP_PKG="${BUILD_DIR}/package"
          mkdir -p "${TEMP_PKG}/inspect-platform"
          
          # Copy only runtime files (see ops/release.py RUNTIME whitelist)
          cp -t "${TEMP_PKG}/inspect-platform/" \
            .gitignore README.md DEPLOYMENT.md MAIL-SETUP.md \
            app.py core.py admin.py worker.py notifications.py inspect_run.sh
          
          # Optional: include MAIL-ACTIVATION.md if present
          [ -f MAIL-ACTIVATION.md ] && cp MAIL-ACTIVATION.md "${TEMP_PKG}/inspect-platform/"
          
          cp -r static admin-static tests deploy jenkins "${TEMP_PKG}/inspect-platform/"
          
          # Create ZIP
          cd "${TEMP_PKG}"
          zip -r "${BUILD_DIR}/${ARTIFACT_NAME}" inspect-platform/
          
          # Compute and save SHA256 for integrity verification
          cd "${BUILD_DIR}"
          sha256sum "${ARTIFACT_NAME}" > "${ARTIFACT_NAME}.sha256"
          
          echo "制品已创建: ${ARTIFACT_NAME}"
          cat "${ARTIFACT_NAME}.sha256"
        '''
        
        // Archive artifact for all builds (useful for reviewing PR changes)
        archiveArtifacts artifacts: "build/${env.ARTIFACT_NAME},build/${env.ARTIFACT_NAME}.sha256", 
                         fingerprint: true
      }
    }
    
    stage('制品验证') {
      steps {
        echo '=== 预部署制品校验 ==='
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          
          cd "${BUILD_DIR}"
          
          # Verify SHA256
          sha256sum -c "${ARTIFACT_NAME}.sha256"
          
          # Basic sanity check: artifact size
          SIZE=$(stat -c%s "${ARTIFACT_NAME}")
          if [ "$SIZE" -lt 10000 ] || [ "$SIZE" -gt 4194304 ]; then
            echo "错误：制品大小异常 ($SIZE bytes)"
            exit 1
          fi
          
          echo "制品校验通过"
        '''
      }
    }
    
    stage('部署到生产') {
      steps {
        echo '=== 部署到生产服务器 ==='
        script {
          // SECURITY CRITICAL:
          // Do NOT execute ops/release.py from repo (PR-modifiable)
          // Use FIXED host script instead: /opt/inspect-ops/deploy.sh
          // That script will:
          // 1. Verify source: main branch + commit signature
          // 2. Download artifact from Jenkins (this build only)
          // 3. Use FIXED /opt/inspect-ops/release.py (not from repo)
          // 4. Deploy with safety checks
          
          def version = "v${new Date().format('yyyyMMdd-HHmmss')}-${env.GIT_COMMIT.take(8)}"
          echo "部署版本: ${version}"
          
          // Check if host deployment script exists
          def deployScriptExists = sh(
            script: 'test -f /opt/inspect-ops/deploy-from-jenkins.sh && echo "yes" || echo "no"',
            returnStdout: true
          ).trim()
          
          if (deployScriptExists != 'yes') {
            error """
部署脚本未就绪：/opt/inspect-ops/deploy-from-jenkins.sh 不存在

这是安全要求：不能直接执行 repo 中的 ops/release.py（PR 可修改）

需要父代理在宿主机准备固定的部署脚本：
  /opt/inspect-ops/deploy-from-jenkins.sh

该脚本应：
  1. 验证来源（main 分支 + commit）
  2. 从 Jenkins artifacts 下载制品
  3. 使用固定的 /opt/inspect-ops/release.py 部署
  4. 不执行 repo 中的任何代码

详见 JENKINS-SETUP.md "宿主部署脚本" 章节
            """
          }
          
          // Call FIXED host script (not from repo)
          sh """#!/usr/bin/env bash
            set -euo pipefail
            
            # This script is on HOST, not in repo
            # Jenkins container must have access via mount or docker exec
            /opt/inspect-ops/deploy-from-jenkins.sh \\
              --version "${version}" \\
              --commit "${env.GIT_COMMIT}" \\
              --branch "${env.BRANCH_NAME}" \\
              --artifact "${env.WORKSPACE}/${BUILD_DIR}/${ARTIFACT_NAME}" \\
              --artifact-sha256 "${env.WORKSPACE}/${BUILD_DIR}/${ARTIFACT_NAME}.sha256"
          """
        }
      }
      post {
        success {
          echo '✓ 部署成功'
        }
        failure {
          echo '✗ 部署失败 - 应已自动回滚到前一版本'
          echo '详细日志: /opt/inspect-ops/state/audit.jsonl'
        }
      }
    }
  }
  
  post {
    always {
      echo "=== 构建完成 ==="
      echo "分支: ${env.BRANCH_NAME}"
      echo "提交: ${env.GIT_COMMIT}"
      echo "公开看板: http://8.146.206.64/qa/"
    }
    success {
      echo '✓ 流水线执行成功'
    }
    failure {
      echo '✗ 流水线执行失败'
    }
    cleanup {
      // Clean up workspace build artifacts (but keep archived artifacts)
      sh "rm -rf ${env.BUILD_DIR} || true"
    }
  }
}
