"""Isolated SMTP mocks only. No network mail delivery and no production configuration reads."""
import json
import os
import smtplib
import time
import unittest
from unittest import mock
from test_platform import Base
import core
import notifications as mail


def ready():
 return mail.Settings(True,'enabled','',{'MAIL_ENABLED':'true','MAIL_HOST':'smtp.example.invalid','MAIL_PORT':465,'MAIL_SECURITY':'ssl','MAIL_USERNAME':'smtp-user','MAIL_PASSWORD':'mock-only-secret','MAIL_FROM':'sender@example.invalid','MAIL_TO':'receiver@example.invalid'})

class MailTests(Base):
 def setUp(self):
  Base.setUp(self);self.n=0
 def event(self,ok=False):
  self.n+=1
  r={'id':'mail-fixture-'+str(self.n),'finished_at':core.now(),'checks':[{'id':'home','name':'主页','ok':ok,'status_code':200 if ok else 503,'error':'' if ok else '期望状态码 200，实际 503'}]}
  core.record_events(r);return r
 def rows(self):
  with core.conn() as c:return [dict(r) for r in c.execute('SELECT * FROM mail_outbox ORDER BY event_id').fetchall()]
 def write_config(self,overrides=None):
  cfg=ready().values.copy();cfg['MAIL_PORT']=str(cfg['MAIL_PORT']);cfg.update(overrides or {})
  p=os.path.join(core.DATA,'mail.env')
  with open(p,'w') as f:f.write('\n'.join(k+'='+str(v) for k,v in cfg.items())+'\n')
  os.chmod(p,0o600);return p
 def test_unconfigured_never_connects(self):
  self.event(False)
  with mock.patch('notifications.smtplib.SMTP_SSL') as smtp,mock.patch('notifications.smtplib.SMTP') as plain:
   self.assertFalse(mail.process_once());smtp.assert_not_called();plain.assert_not_called()
  self.assertEqual(self.rows()[0]['status'],'unconfigured');self.assertEqual(self.rows()[0]['attempts'],0)
 def test_owner_file_permissions_enforced(self):
  p=self.write_config();self.assertTrue(mail.load_settings().ready)
  os.chmod(p,0o644);self.assertEqual(mail.load_settings().code,'CONFIG_PERMISSIONS')
 def test_symlink_rejected(self):
  p=self.write_config();os.rename(p,p+'.target');os.symlink(p+'.target',p)
  self.assertEqual(mail.load_settings().code,'CONFIG_PERMISSIONS')
 def test_disabled_complete_config_does_not_connect(self):
  self.write_config({'MAIL_ENABLED':'false'});cfg=mail.load_settings();self.assertFalse(cfg.ready);self.assertEqual(cfg.state,'disabled')
  self.event()
  with mock.patch('notifications.smtp_send') as smtp:mail.process_once();smtp.assert_not_called()
 def test_missing_secret_not_ready(self):
  self.write_config({'MAIL_PASSWORD':''});self.assertFalse(mail.load_settings().ready)
 def test_single_recipient_no_injection(self):
  for value in ['first@example.invalid,second@example.invalid','receiver@example.invalid\nBcc:second@example.invalid','Name <receiver@example.invalid>']:
   self.write_config({'MAIL_TO':value});self.assertFalse(mail.load_settings().ready)
 def test_tls_only(self):
  self.write_config({'MAIL_SECURITY':'plain','MAIL_PORT':'25'});self.assertFalse(mail.load_settings().ready)
 def test_enable_skips_existing_events(self):
  self.event();self.assertEqual(self.rows()[0]['status'],'unconfigured')
  mail.sync(ready());self.assertEqual(self.rows()[0]['status'],'skipped');self.assertIsNone(mail.claim(ready()))
 def test_recovery_of_prebaseline_failure_not_sent(self):
  self.event();mail.sync(ready());self.event(True)
  self.assertEqual([r['status'] for r in self.rows()],['skipped','skipped']);self.assertIsNone(mail.claim(ready()))
 def test_failure_recovery_and_event_dedup(self):
  cfg=ready();mail.sync(cfg);r=self.event();core.record_events(r);self.assertEqual(len(self.rows()),1)
  self.event(False);self.assertEqual(len(self.rows()),1)
  sender=mock.Mock();self.assertTrue(mail.process_once(cfg,sender));self.assertEqual(self.rows()[0]['status'],'sent')
  self.event(True);self.assertTrue(mail.process_once(cfg,sender));rows=self.rows();self.assertEqual([r['status'] for r in rows],['sent','sent']);self.assertEqual(rows[1]['incident_id'],rows[0]['event_id']);self.assertEqual(sender.call_count,2)
  messages=[c[0][0] for c in sender.call_args_list];self.assertEqual(messages[1]['In-Reply-To'],messages[0]['Message-ID']);self.assertIn('北京时间',messages[1].get_content());self.assertIn('/qa/#/reports/',messages[0].get_content())
 def test_recovery_waits_for_failure(self):
  cfg=ready();mail.sync(cfg);self.event();self.event(True);stamp=time.time()
  first=mail.claim(cfg,stamp);self.assertEqual(first['kind'],'failure')
  self.assertIsNone(mail.claim(cfg,stamp));rows=self.rows();self.assertEqual(rows[1]['attempts'],0);self.assertEqual(rows[1]['last_error_code'],'WAIT_FAILURE')
 def test_transient_retry_backoff_and_limit(self):
  cfg=ready();mail.sync(cfg);self.event();stamp=time.time();sender=mock.Mock(side_effect=mail.SendFailure('NETWORK_ERROR',True))
  for attempt in range(1,6):
   self.assertTrue(mail.process_once(cfg,sender,stamp));row=self.rows()[0];self.assertEqual(row['attempts'],attempt)
   if attempt<5:self.assertEqual(row['status'],'pending');self.assertEqual(row['next_attempt'],stamp+mail.BACKOFF[attempt-1]);stamp=row['next_attempt']+.1
   else:self.assertEqual(row['status'],'failed');self.assertEqual(row['last_error_code'],'RETRY_EXHAUSTED')
  self.assertFalse(mail.process_once(cfg,sender,stamp+99999));self.assertEqual(sender.call_count,5)
 def test_queue_persists_and_message_id_stable(self):
  cfg=ready();mail.sync(cfg);self.event();stamp=time.time();seen=[]
  def failing(msg,cfg):seen.append(msg['Message-ID']);raise mail.SendFailure('NETWORK_ERROR',True)
  mail.process_once(cfg,failing,stamp);core.init();row=self.rows()[0];self.assertEqual(row['status'],'pending')
  mail.process_once(cfg,lambda msg,cfg:seen.append(msg['Message-ID']),row['next_attempt']+1);self.assertEqual(seen[0],seen[1]);self.assertEqual(self.rows()[0]['status'],'sent')
 def test_permanent_auth_failure_is_redacted(self):
  cfg=ready();mail.sync(cfg);self.event();client=mock.Mock();client.login.side_effect=smtplib.SMTPAuthenticationError(535,b'mock-only-secret PRIVATE SMTP AUTH')
  with mock.patch('notifications.smtplib.SMTP_SSL',return_value=client):mail.process_once(cfg)
  row=self.rows()[0];self.assertEqual(row['status'],'failed');self.assertEqual(row['attempts'],1);self.assertEqual(row['last_error_code'],'AUTH_FAILED')
  public=json.dumps(core.events(),ensure_ascii=False)+json.dumps(mail.summary());self.assertNotIn('mock-only-secret',public);self.assertNotIn('PRIVATE SMTP AUTH',public);self.assertNotIn('receiver@',public);self.assertNotIn('smtp.example.invalid',public)
 def test_expired_sending_not_blindly_resent(self):
  cfg=ready();mail.sync(cfg);self.event();stamp=time.time();mail.claim(cfg,stamp)
  self.assertIsNone(mail.claim(cfg,stamp+91));self.assertEqual(self.rows()[0]['last_error_code'],'DELIVERY_UNCERTAIN');self.assertIsNone(self.rows()[0]['next_attempt'])
 def test_expired_attempt_visible_when_service_disabled(self):
  cfg=ready();mail.sync(cfg);self.event();mail.claim(cfg,time.time())
  # No external SMTP call: simulate a worker interrupted while claiming a message.
  with core.conn() as c:c.execute("UPDATE mail_outbox SET lease_until=? WHERE status='sending'",(time.time()-1,))
  mail.sync(mail.Settings());self.assertEqual(self.rows()[0]['status'],'failed');self.assertEqual(self.rows()[0]['last_error_code'],'DELIVERY_UNCERTAIN')
 def test_disable_enable_rebaselines_pending(self):
  cfg=ready();mail.sync(cfg);self.event();mail.sync(mail.Settings());mail.sync(cfg)
  self.assertEqual(self.rows()[0]['status'],'skipped');self.assertFalse(mail.process_once(cfg,mock.Mock()))
 def test_smtp_ssl_calls_only_fixed_single_recipient(self):
  cfg=ready();mail.sync(cfg);self.event();client=mock.Mock();client.send_message.return_value={}
  with mock.patch('notifications.smtplib.SMTP_SSL',return_value=client) as smtp:
   self.assertTrue(mail.process_once(cfg));smtp.assert_called_once();self.assertEqual(smtp.call_args[1]['timeout'],15)
  kw=client.send_message.call_args[1];self.assertEqual(kw['to_addrs'],['receiver@example.invalid']);self.assertEqual(kw['from_addr'],'sender@example.invalid');self.assertEqual(self.rows()[0]['status'],'sent')
 def test_smtp_starttls_before_login(self):
  cfg=ready();cfg.values['MAIL_SECURITY']='starttls';cfg.values['MAIL_PORT']=587;mail.sync(cfg);self.event();client=mock.Mock();client.send_message.return_value={}
  with mock.patch('notifications.smtplib.SMTP',return_value=client):mail.process_once(cfg)
  names=[c[0] for c in client.method_calls];self.assertLess(names.index('starttls'),names.index('login'));self.assertEqual(self.rows()[0]['status'],'sent')
 def test_data_uncertainty_no_resend(self):
  cfg=ready();mail.sync(cfg);self.event();client=mock.Mock();client.send_message.side_effect=smtplib.SMTPServerDisconnected('mock-only-secret')
  with mock.patch('notifications.smtplib.SMTP_SSL',return_value=client):mail.process_once(cfg)
  row=self.rows()[0];self.assertEqual(row['status'],'failed');self.assertEqual(row['last_error_code'],'DELIVERY_UNCERTAIN')
 def test_temporary_smtp_data_error_retries(self):
  cfg=ready();mail.sync(cfg);self.event();client=mock.Mock();client.send_message.side_effect=smtplib.SMTPDataError(451,b'temporary private text')
  with mock.patch('notifications.smtplib.SMTP_SSL',return_value=client):mail.process_once(cfg)
  self.assertEqual(self.rows()[0]['status'],'pending');self.assertEqual(self.rows()[0]['last_error_code'],'SMTP_TEMPORARY')
 def test_quit_close_error_after_accept_not_retry(self):
  cfg=ready();mail.sync(cfg);self.event();client=mock.Mock();client.send_message.return_value={};client.close.side_effect=OSError('closed')
  with mock.patch('notifications.smtplib.SMTP_SSL',return_value=client):mail.process_once(cfg)
  self.assertEqual(self.rows()[0]['status'],'sent')
 def test_legacy_events_not_backfilled(self):
  with core.conn() as c:c.execute("INSERT INTO events(kind,task_id,task_name,run_id,occurred_at,status_code,message) VALUES('failure','home','旧事件','legacy',?,503,'旧失败')",(core.now(),))
  mail.sync(ready());self.assertEqual(len(self.rows()),0);event=core.events()[0];self.assertEqual(event['notification']['status'],'skipped')
 def test_recovery_after_failed_notice_explains_context(self):
  cfg=ready();mail.sync(cfg);self.event();mail.process_once(cfg,mock.Mock(side_effect=mail.SendFailure('AUTH_FAILED',False)));self.event(True)
  captured=[];mail.process_once(cfg,lambda msg,cfg:captured.append(msg));self.assertIn('此前失败通知未确认发送成功',captured[0].get_content())
 def test_message_has_no_response_or_config_secrets(self):
  cfg=ready();mail.sync(cfg);self.event();row=mail.claim(cfg);msg=mail.message(row,cfg);body=msg.get_content()
  self.assertNotIn('mock-only-secret',body);self.assertNotIn('MAIL_PASSWORD',body);self.assertIn('HTTP 状态码：503',body);self.assertIsNotNone(msg['Date']);self.assertIsNone(msg['Cc']);self.assertIsNone(msg['Bcc'])

if __name__=='__main__':unittest.main(verbosity=2)
