#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private SMTP adapter and durable outbox. No connection until explicitly configured/enabled."""
import datetime
import email.message
import email.utils
import fcntl
import hashlib
import json
import os
import re
import smtplib
import socket
import ssl
import stat
import time
import uuid
from urllib.parse import quote
import core

MAX_ATTEMPTS=5
BACKOFF=(60,300,900,3600)
LEASE_SECONDS=90
LABELS={'unconfigured':'未配置','disabled':'未启用','pending':'待发','sending':'待发','sent':'已发送','failed':'失败','skipped':'未发送（基线）'}
ERRORS={
 'NOT_CONFIGURED':'发信服务未配置','DISABLED':'邮件发送未启用','CONFIG_PERMISSIONS':'配置文件权限不安全',
 'CONFIG_INVALID':'发信配置无效','BASELINE':'启用前事件不补发','BASELINE_NO_FAILURE':'关联故障发生于启用基线之前',
 'AUTH_FAILED':'发信认证失败，需检查服务器配置','TLS_FAILED':'TLS 连接校验失败','TLS_REQUIRED':'服务器不支持要求的 TLS',
 'ADDRESS_REJECTED':'发件人或收件人被服务器拒绝','DATA_REJECTED':'邮件内容被服务器拒绝',
 'SMTP_REJECTED':'发信服务器拒绝请求','SMTP_TEMPORARY':'发信服务暂时不可用','NETWORK_ERROR':'邮件连接失败，等待重试',
 'DELIVERY_UNCERTAIN':'投递结果不确定，已停止自动重试，需人工核验',
 'RETRY_EXHAUSTED':'已达到最大重试次数','WAIT_FAILURE':'等待关联失败通知完成',
}


def init_schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS mail_outbox(
      event_id INTEGER PRIMARY KEY,kind TEXT NOT NULL,task_id TEXT NOT NULL,task_name TEXT NOT NULL,
      run_id TEXT NOT NULL,occurred_at TEXT NOT NULL,status_code INTEGER NOT NULL,
      incident_id INTEGER,incident_time TEXT,incident_run TEXT,eligible INTEGER NOT NULL,
      status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL,
      last_error_code TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      sent_at TEXT,message_id TEXT,lease_until REAL);
    CREATE TABLE IF NOT EXISTS mail_incidents(task_id TEXT PRIMARY KEY,failure_event_id INTEGER NOT NULL,
      occurred_at TEXT NOT NULL,run_id TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS mail_outbox_due ON mail_outbox(status,next_attempt);
    ''')
    for k,v in [('mail_ready','0'),('mail_state','unconfigured'),('mail_reason','NOT_CONFIGURED'),('mail_floor','0'),('mail_instance',uuid.uuid4().hex),('mail_last_check','0')]:
        c.execute('INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)',(k,v))


def meta(c,key,default=''):
    r=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    return r['value'] if r else default


def setmeta(c,key,value):
    c.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',(key,str(value)))


def enqueue(c,event_id,kind,check,report):
    """Called in the SAME transaction as core event creation. No SMTP or secret reads here."""
    incident=c.execute('SELECT * FROM mail_incidents WHERE task_id=?',(check['id'],)).fetchone()
    if kind=='failure':
        incident_id=event_id;incident_time=report['finished_at'];incident_run=report['id']
        c.execute('INSERT OR REPLACE INTO mail_incidents VALUES(?,?,?,?)',(check['id'],incident_id,incident_time,incident_run))
    elif incident:
        incident_id=incident['failure_event_id'];incident_time=incident['occurred_at'];incident_run=incident['run_id']
    else:
        old=c.execute("SELECT id,occurred_at,run_id FROM events WHERE task_id=? AND kind='failure' AND id<? ORDER BY id DESC LIMIT 1",(check['id'],event_id)).fetchone()
        incident_id=old['id'] if old else None;incident_time=old['occurred_at'] if old else None;incident_run=old['run_id'] if old else None
    ready=meta(c,'mail_ready')=='1';floor=int(meta(c,'mail_floor','0'))
    eligible=ready and (kind=='failure' or (incident_id is not None and incident_id>floor))
    status='pending' if eligible else ('skipped' if ready else 'unconfigured')
    reason='' if eligible else ('BASELINE_NO_FAILURE' if ready else 'NOT_CONFIGURED')
    c.execute('''INSERT OR IGNORE INTO mail_outbox
      (event_id,kind,task_id,task_name,run_id,occurred_at,status_code,incident_id,incident_time,incident_run,eligible,status,attempts,next_attempt,last_error_code,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)''',
      (event_id,kind,check['id'],check['name'][:80],report['id'],report['finished_at'],check['status_code'],
       incident_id,incident_time,incident_run,int(eligible),status,time.time() if eligible else None,reason,core.now(),core.now()))


class Settings:
    def __init__(self,ready=False,state='unconfigured',code='NOT_CONFIGURED',values=None):
        self.ready=ready;self.state=state;self.code=code;self.values=values or {}


def valid_address(value):
    return isinstance(value,str) and bool(re.match(r'^[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$',value)) and len(value)<=254 and '\r' not in value and '\n' not in value


def load_settings():
    """Read only this dedicated, owner-only file; never expose its values in the API/logs."""
    path=os.path.join(core.DATA,'mail.env')
    if not os.path.isfile(path):return Settings()
    try:
        info=os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid():
            return Settings(state='unconfigured',code='CONFIG_PERMISSIONS')
        if info.st_size>16384:return Settings(code='CONFIG_INVALID')
        values={}
        allowed={'MAIL_ENABLED','MAIL_HOST','MAIL_PORT','MAIL_SECURITY','MAIL_USERNAME','MAIL_PASSWORD','MAIL_FROM','MAIL_TO'}
        with open(path,encoding='utf-8') as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith('#'):continue
                if '=' not in line:return Settings(code='CONFIG_INVALID')
                k,v=line.split('=',1)
                if k not in allowed or k in values:return Settings(code='CONFIG_INVALID')
                v=v.strip()
                if len(v)>=2 and v[0]==v[-1] and v[0] in ("'",'"'):v=v[1:-1]
                values[k]=v
        if values.get('MAIL_ENABLED','false').lower() not in ('true','false'):return Settings(code='CONFIG_INVALID')
        complete=all(values.get(k) for k in ('MAIL_HOST','MAIL_USERNAME','MAIL_PASSWORD','MAIL_FROM','MAIL_TO'))
        if not complete:return Settings(values=values)
        if not valid_address(values['MAIL_FROM']) or not valid_address(values['MAIL_TO']):return Settings(code='CONFIG_INVALID')
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$',values['MAIL_HOST']):return Settings(code='CONFIG_INVALID')
        mode=values.get('MAIL_SECURITY','ssl');port=int(values.get('MAIL_PORT','465'))
        if (mode,port) not in (('ssl',465),('starttls',587)):return Settings(code='CONFIG_INVALID')
        if any('\r' in values[k] or '\n' in values[k] for k in ('MAIL_USERNAME','MAIL_PASSWORD')):return Settings(code='CONFIG_INVALID')
        values['MAIL_PORT']=port;values['MAIL_SECURITY']=mode
        if values.get('MAIL_ENABLED','false').lower()!='true':return Settings(state='disabled',code='DISABLED',values=values)
        return Settings(True,'enabled','',values)
    except Exception:return Settings(code='CONFIG_INVALID')


def sync(settings):
    """Every enable/re-enable creates a new baseline. Historical events are NEVER backfilled."""
    with core.conn() as c:
        c.execute('BEGIN IMMEDIATE')
        old_ready=meta(c,'mail_ready')=='1'
        c.execute("UPDATE mail_outbox SET status='failed',last_error_code='DELIVERY_UNCERTAIN',next_attempt=NULL,lease_until=NULL,updated_at=? WHERE status='sending' AND lease_until<?",(core.now(),time.time()))
        if settings.ready and not old_ready:
            floor=c.execute('SELECT COALESCE(MAX(id),0) AS n FROM events').fetchone()['n']
            setmeta(c,'mail_floor',floor);setmeta(c,'mail_baseline_at',core.now())
            c.execute("UPDATE mail_outbox SET status='skipped',eligible=0,last_error_code='BASELINE',next_attempt=NULL,lease_until=NULL,updated_at=? WHERE status IN ('pending','unconfigured','disabled','skipped')",(core.now(),))
        elif not settings.ready and old_ready:
            c.execute("UPDATE mail_outbox SET status='unconfigured',last_error_code='NOT_CONFIGURED',next_attempt=NULL,updated_at=? WHERE status='pending'",(core.now(),))
        for key,value in [('mail_ready','1' if settings.ready else '0'),('mail_state',settings.state),('mail_reason',settings.code)]:
            if meta(c,key)!=value:setmeta(c,key,value)
        if time.time()-float(meta(c,'mail_last_check','0'))>30:setmeta(c,'mail_last_check',time.time())


def iso_epoch(value):
    return datetime.datetime.fromtimestamp(float(value),datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds') if value else None


def public_row(row):
    if row is None:return None
    return {'status':row['status'],'label':LABELS.get(row['status'],'未配置'),'attempts':row['attempts'],
      'max_attempts':MAX_ATTEMPTS,'next_attempt_at':iso_epoch(row['next_attempt']), 'sent_at':row['sent_at'],
      'error_code':row['last_error_code'],'reason':ERRORS.get(row['last_error_code'],''),'failure_event_id':row['incident_id']}


def summary():
    with core.conn() as c:
        state=meta(c,'mail_state','unconfigured');reason=meta(c,'mail_reason','NOT_CONFIGURED')
        counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) AS n FROM mail_outbox GROUP BY status')}
        ready=meta(c,'mail_ready')=='1';baseline=meta(c,'mail_baseline_at') or None
        checked=meta(c,'mail_last_check','0')
    return {'state':state,'label':'已配置' if ready else LABELS.get(state,'未配置'), 'enabled':ready,
      'reason':ERRORS.get(reason,'配置完整，等待新的失败或恢复事件；尚未发送验证信' if ready else '发信服务未配置'),
      'counts':counts,'baseline_at':baseline,'last_checked_at':iso_epoch(checked),
      'smtp_accepted_count':counts.get('sent',0),'delivery_note':'已发送表示 SMTP 已接收，不代表收件箱已送达',
      'policy':'每次启用时以已有事件为基线，不补发历史；仅发送启用后新故障及其恢复'}


def attach(events):
    with core.conn() as c:
        for event in events:
            row=c.execute('SELECT * FROM mail_outbox WHERE event_id=?',(event['id'],)).fetchone()
            if row:event['notification']=public_row(row)
            else:
                ready=meta(c,'mail_ready')=='1'
                event['notification']={'status':'skipped' if ready else 'unconfigured','label':'未发送（基线）' if ready else '未配置','attempts':0,'max_attempts':MAX_ATTEMPTS,'next_attempt_at':None,'sent_at':None,'error_code':'BASELINE' if ready else 'NOT_CONFIGURED','reason':ERRORS['BASELINE' if ready else 'NOT_CONFIGURED'],'failure_event_id':None}
    return events


def claim(settings,clock=None):
    stamp=time.time() if clock is None else clock
    if not settings.ready:return None
    with core.conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if meta(c,'mail_ready')!='1':return None
        # An interrupted SMTP DATA transaction can have been accepted remotely: do not blindly resend it.
        c.execute("UPDATE mail_outbox SET status='failed',last_error_code='DELIVERY_UNCERTAIN',next_attempt=NULL,lease_until=NULL,updated_at=? WHERE status='sending' AND lease_until<?",(core.now(),stamp))
        rows=c.execute("SELECT * FROM mail_outbox WHERE status='pending' AND next_attempt<=? AND attempts<? ORDER BY event_id LIMIT 20",(stamp,MAX_ATTEMPTS)).fetchall()
        for row in rows:
            if row['kind']=='recovery':
                failure=c.execute('SELECT status,eligible FROM mail_outbox WHERE event_id=?',(row['incident_id'],)).fetchone()
                if not failure or not failure['eligible']:
                    c.execute("UPDATE mail_outbox SET status='skipped',last_error_code='BASELINE_NO_FAILURE',next_attempt=NULL WHERE event_id=?",(row['event_id'],));continue
                if failure['status'] in ('pending','sending'):
                    c.execute("UPDATE mail_outbox SET last_error_code='WAIT_FAILURE',next_attempt=? WHERE event_id=?",(stamp+30,row['event_id']));continue
            domain=settings.values['MAIL_FROM'].rsplit('@',1)[1]
            message_id=row['message_id'] or '<qa.%s.%s@%s>'%(meta(c,'mail_instance'),row['event_id'],domain)
            c.execute("UPDATE mail_outbox SET status='sending',attempts=attempts+1,lease_until=?,message_id=?,updated_at=? WHERE event_id=?",(stamp+LEASE_SECONDS,message_id,core.now(),row['event_id']))
            return dict(c.execute('SELECT * FROM mail_outbox WHERE event_id=?',(row['event_id'],)).fetchone())
    return None


def beijing(value):
    return str(value or '').replace('T',' ').replace('+08:00','')+'（北京时间）'


def message(row,settings):
    v=settings.values;event='失败' if row['kind']=='failure' else '恢复'
    name=re.sub(r'[\r\n\x00-\x1f]',' ',row['task_name'])[:80]
    msg=email.message.EmailMessage()
    msg['From']=v['MAIL_FROM'];msg['To']=v['MAIL_TO'];msg['Subject']='[巡检%s] %s'%(event,name)
    msg['Message-ID']=row['message_id'];msg['Auto-Submitted']='auto-generated'
    msg['Date']=email.utils.format_datetime(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))))
    incident=row['incident_id']
    failure=None
    if row['kind']=='recovery' and incident:
        with core.conn() as c:failure=c.execute('SELECT message_id,status FROM mail_outbox WHERE event_id=?',(incident,)).fetchone()
        if failure and failure['message_id']:
            msg['In-Reply-To']=failure['message_id'];msg['References']=failure['message_id']
    lines=['巡检%s通知'%event,'','检查项：'+name,'事件编号：'+str(row['event_id']),'发生时间：'+beijing(row['occurred_at']),
      'HTTP 状态码：'+str(row['status_code']),'报告：'+core.PUBLIC_ORIGIN+'/qa/#/reports/'+quote(row['run_id'],safe='')]
    if incident:
        lines+=['关联故障事件：'+str(incident),'故障开始：'+beijing(row['incident_time'])]
        if row['kind']=='recovery' and row['incident_run']:
            lines+=['原失败报告：'+core.PUBLIC_ORIGIN+'/qa/#/reports/'+quote(row['incident_run'],safe='')]
    if row['kind']=='recovery' and failure and failure['status']!='sent':
        lines+=['此前失败通知未确认发送成功；本邮件说明检查当前已恢复。']
    lines+=['','失败详情请在报告中查看。本邮件不包含响应正文、认证信息或服务器秘密。','此邮件由巡检平台按失败/恢复状态变化生成；连续相同状态不会重复通知。']
    msg.set_content('\n'.join(lines))
    return msg


class SendFailure(Exception):
    def __init__(self,code,retry):self.code=code;self.retry=retry


def smtp_send(msg,settings):
    v=settings.values;client=None;in_data=False
    try:
        context=ssl.create_default_context()
        if v['MAIL_SECURITY']=='ssl':client=smtplib.SMTP_SSL(v['MAIL_HOST'],v['MAIL_PORT'],timeout=15,context=context)
        else:
            client=smtplib.SMTP(v['MAIL_HOST'],v['MAIL_PORT'],timeout=15)
            client.ehlo();client.starttls(context=context);client.ehlo()
        client.login(v['MAIL_USERNAME'],v['MAIL_PASSWORD'])
        in_data=True
        rejected=client.send_message(msg,from_addr=v['MAIL_FROM'],to_addrs=[v['MAIL_TO']])
        if rejected:raise SendFailure('ADDRESS_REJECTED',False)
    except SendFailure:raise
    except smtplib.SMTPAuthenticationError:raise SendFailure('AUTH_FAILED',False)
    except smtplib.SMTPNotSupportedError:raise SendFailure('TLS_REQUIRED',False)
    except smtplib.SMTPSenderRefused as e:raise SendFailure('SMTP_TEMPORARY' if 400<=e.smtp_code<500 else 'ADDRESS_REJECTED',400<=e.smtp_code<500)
    except smtplib.SMTPRecipientsRefused as e:
        temporary=bool(e.recipients) and all(400<=item[0]<500 for item in e.recipients.values())
        raise SendFailure('SMTP_TEMPORARY' if temporary else 'ADDRESS_REJECTED',temporary)
    except smtplib.SMTPDataError as e:raise SendFailure('SMTP_TEMPORARY' if 400<=e.smtp_code<500 else 'DATA_REJECTED',400<=e.smtp_code<500)
    except ssl.SSLError:raise SendFailure('TLS_FAILED',False)
    except (smtplib.SMTPServerDisconnected,socket.timeout,OSError):raise SendFailure('DELIVERY_UNCERTAIN' if in_data else 'NETWORK_ERROR',not in_data)
    except smtplib.SMTPResponseException as e:raise SendFailure('SMTP_TEMPORARY' if 400<=e.smtp_code<500 else 'SMTP_REJECTED',400<=e.smtp_code<500)
    except Exception:raise SendFailure('CONFIG_INVALID',False)
    finally:
        if client:
            # A QUIT failure AFTER successful DATA must not turn accepted mail into a retry.
            try:client.close()
            except Exception:pass


def process_once(settings=None,sender=None,clock=None):
    settings=settings or load_settings();sync(settings)
    if not settings.ready:return False
    row=claim(settings,clock)
    if not row:return False
    stamp=time.time() if clock is None else clock
    try:
        (sender or smtp_send)(message(row,settings),settings)
        with core.conn() as c:
            c.execute("UPDATE mail_outbox SET status='sent',sent_at=?,updated_at=?,next_attempt=NULL,last_error_code='',lease_until=NULL WHERE event_id=? AND status='sending'",(core.now(),core.now(),row['event_id']))
    except SendFailure as exc:
        retry=exc.retry and row['attempts']<MAX_ATTEMPTS
        code=exc.code if not exc.retry or retry else 'RETRY_EXHAUSTED'
        with core.conn() as c:
            c.execute("UPDATE mail_outbox SET status=?,last_error_code=?,next_attempt=?,lease_until=NULL,updated_at=? WHERE event_id=? AND status='sending'",('pending' if retry else 'failed',code,stamp+BACKOFF[row['attempts']-1] if retry else None,core.now(),row['event_id']))
    except Exception:
        with core.conn() as c:c.execute("UPDATE mail_outbox SET status='failed',last_error_code='CONFIG_INVALID',next_attempt=NULL,lease_until=NULL,updated_at=? WHERE event_id=?",(core.now(),row['event_id']))
    return True


def main():
    core.init()
    with open(os.path.join(core.DATA,'mail-worker.lock'),'a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        print('Mail outbox worker running; disabled without private SMTP configuration',flush=True)
        while True:
            try:process_once()
            except Exception:print('MAIL_WORKER_ERROR (details redacted)',flush=True)
            time.sleep(2)


if __name__=='__main__':main()
