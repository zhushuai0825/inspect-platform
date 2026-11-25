// CI/CD Pipeline for inspect-platform
// This Jenkinsfile handles BOTH testing (for all branches/PRs) and deployment (main only)

// Determine if this is a trusted main branch build (can deploy)
def isTrustedMainBuild() {
  return env.BRANCH_NAME == 'main' && env.CHANGE_ID == null
}

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
    // Workspace-local paths (safe for all builds including PRs)
    BUILD_DIR = "${env.WORKSPACE}/build"
  }
  
  stages {
    stage('准备') {
      steps {
        script {
          echo "=== 构建信息 ==="
          echo "分支: ${env.BRANCH_NAME}"
          echo "提交: ${env.GIT_COMMIT}"
          echo "PR: ${env.CHANGE_ID ?: '(不是PR)'}"
          echo "受信部署: ${isTrustedMainBuild() ? '是 - 允许部署' : '否 - 仅测试'}"
          
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
      when {
        expression { isTrustedMainBuild() }
      }
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
      when {
        expression { isTrustedMainBuild() }
      }
      steps {
        echo '=== 部署到生产服务器 ==='
        script {
          // This stage requires production credentials and should ONLY run for main branch
          // PRs and other branches NEVER reach here
          
          // Check if release ops is ready
          def opsStatus = sh(
            script: 'sudo python3 /opt/inspect-platform/ops/release.py status 2>&1 || true',
            returnStdout: true
          ).trim()
          
          echo "当前发布状态:\n${opsStatus}"
          
          // Generate version identifier
          def version = "v${new Date().format('yyyyMMdd-HHmmss')}-${env.GIT_COMMIT.take(8)}"
          echo "部署版本: ${version}"
          
          // Prepare release (validate, test, stage)
          sh """#!/usr/bin/env bash
            set -euo pipefail
            
            echo "=== 准备发布 ${version} ==="
            sudo python3 /opt/inspect-platform/ops/release.py prepare \\
              "${BUILD_DIR}/${ARTIFACT_NAME}" \\
              "${version}"
            
            echo "=== 执行部署 ${version} ==="
            # This will:
            # - Lock release (serialize deployments)
            # - Check for active inspections/emails (abort if busy)
            # - Stop services
            # - Backup current data to /root/backups (private)
            # - Install new code (atomic replace)
            # - Start services
            # - Run health checks
            # - Auto-rollback if health check fails
            sudo python3 /opt/inspect-platform/ops/release.py deploy "${version}"
            
            echo "=== 部署完成 ==="
            sudo python3 /opt/inspect-platform/ops/release.py status
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
          sh 'sudo python3 /opt/inspect-platform/ops/release.py status || true'
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
