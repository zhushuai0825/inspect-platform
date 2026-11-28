// TEMPLATE ONLY. Not installed on the live Jenkins controller.
// Copy into a trusted job later. Do not load this from an untrusted repository.
// The controller must not run the shell steps: the pipeline agent is none,
// and the only shell runs on the inspect-isolated agent label.
// Trigger shape: the loopback admin calls buildWithParameters with
// ACTION, COMMIT, VERSION, ARTIFACT, SERVICE, TICKET, ACTOR.
// It does not pass a repository URL. A trusted script on the agent, installed
// outside this repo, must resolve the artifact. This file does not deploy.

pipeline {
  agent none

  options {
    timestamps()
    disableConcurrentBuilds()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  parameters {
    string(name: 'ACTION', defaultValue: 'deploy', description: 'deploy or rollback')
    string(name: 'COMMIT', defaultValue: '', description: '7-40 lowercase hex, or empty')
    string(name: 'VERSION', defaultValue: '', description: 'Version token')
    string(name: 'ARTIFACT', defaultValue: '', description: 'Artifact id, not a path or URL')
    string(name: 'SERVICE', defaultValue: 'frontend', description: 'Must be frontend for this job')
    string(name: 'TICKET', defaultValue: '', description: 'Publish record id')
    string(name: 'ACTOR', defaultValue: '', description: 'Who requested the publish')
  }

  stages {
    stage('publish on isolated agent') {
      agent { label 'inspect-isolated' }
      steps {
        sh '''#!/bin/bash
set -euo pipefail
if [ "$SERVICE" != "frontend" ]; then echo "SERVICE must be frontend"; exit 2; fi
case "$ACTION" in deploy|rollback) ;; *) echo "invalid ACTION"; exit 2;; esac
if [ -n "$COMMIT" ]; then
  case "$COMMIT" in *[!0-9a-f]*) echo "invalid COMMIT"; exit 2;; esac
  n=${#COMMIT}
  if [ "$n" -lt 7 ] || [ "$n" -gt 40 ]; then echo "invalid COMMIT"; exit 2; fi
fi
if [ -n "$VERSION" ]; then
  case "$VERSION" in *[!A-Za-z0-9._-]*) echo "invalid VERSION"; exit 2;; esac
fi
if [ -n "$ARTIFACT" ]; then
  case "$ARTIFACT" in *[!A-Za-z0-9._+-]*) echo "invalid ARTIFACT"; exit 2;; esac
fi
case "$TICKET" in pub[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;; *) echo "invalid TICKET"; exit 2;; esac
case "$ACTOR" in *['$"`\\;|&<>(){}!#']*) echo "ACTOR contains unsupported characters"; exit 2;; esac
if [ -z "$COMMIT" ] && [ -z "$VERSION" ]; then echo "COMMIT or VERSION is required"; exit 2; fi
script=/opt/inspect-deploy/bin/publish-frontend.sh
if [ ! -x "$script" ]; then
  echo "UNFINISHED: trusted publish script is not installed on this agent ($script)."
  echo "This template does not deploy and does not clone a repository URL from the build."
  exit 2
fi
exec "$script"
'''
      }
    }
  }
}
