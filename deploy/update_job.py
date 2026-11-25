#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update only the existing site-http-smoke job, using server-side credentials; never prints them."""
import os
import sys
import xml.sax.saxutils
from urllib.request import Request,urlopen
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
app.boot_config()
script=open(os.path.join(app.BASE_DIR,'jenkins','Jenkinsfile')).read()
config='''<?xml version="1.0" encoding="UTF-8"?>
<flow-definition plugin="workflow-job"><description>可配置 HTTP 巡检，私有文件队列执行，报告及失败恢复事件在公开看板展示。</description><keepDependencies>false</keepDependencies><properties><org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/><hudson.model.ParametersDefinitionProperty><parameterDefinitions><hudson.model.StringParameterDefinition><name>TRIGGER</name><defaultValue>jenkins</defaultValue><trim>true</trim></hudson.model.StringParameterDefinition></parameterDefinitions></hudson.model.ParametersDefinitionProperty><org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty><triggers><hudson.triggers.TimerTrigger><spec>TZ=Asia/Shanghai
0 9 * * *</spec></hudson.triggers.TimerTrigger></triggers></org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty></properties><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps"><script>%s</script><sandbox>true</sandbox></definition><disabled>false</disabled></flow-definition>'''%xml.sax.saxutils.escape(script)
request=Request(app.ENV['JENKINS_URL'].rstrip('/')+'/job/site-http-smoke/config.xml',data=config.encode('utf-8'),method='POST',headers={'Authorization':app._basic_auth_header(app.ENV['JENKINS_USER'],app.ENV['JENKINS_API_TOKEN']),'Content-Type':'application/xml; charset=UTF-8'})
with urlopen(request,timeout=15) as response:print('JOB_UPDATED_HTTP',response.status)
