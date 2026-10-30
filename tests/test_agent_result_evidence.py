"""Physical-reader unit checks; actual workflow runs are reported separately."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.evaluation.agent_result_evidence import EvidenceUnavailable,read_ledger,read_mailpit,identify_reply
from app.evaluation.agent_metrics import effects_check


class FakeResponse:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data


class FakeClient:
    def __init__(self,*args,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def get(self,*args,**kwargs):return FakeResponse(self.page)


class AgentResultEvidenceTests(unittest.TestCase):
    def test_success_flag_cannot_replace_a_missing_physical_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=read_ledger(Path(folder)/'missing.xlsx')
        self.assertEqual(ledger['counts'],{})
        expected={'reportPolicy':'required','requiredTools':[{'kind':'EXCEL_REPORT','terminalState':'SUCCESS'}],
                  'acceptedRisks':['LOW'],'acceptedIntents':['CONSULT']}
        turn={'actorAlias':'a','sessionAlias':'s','runId':'r','coverage':{k:'complete' for k in ('reports','jobs','effects')},
          'reports':[{'id':'1','userId':'u','sessionId':'s-public','risk':'LOW','intent':'CONSULT'}],
          'jobs':[{'id':'j','reportId':'1','kind':'EXCEL_REPORT','state':'SUCCESS','runId':'r'}],
          'effects':[{'reportId':'1','kind':'EXCEL_REPORT','source':'excel.ledger','count':ledger['counts'].get('1',0)}],
          'backgroundWindowClosed':True}
        self.assertFalse(effects_check(expected,turn,{'actors':{'a':'u'},'sessions':{'s':{'id':'s-public'}}}))

    def test_real_workbook_rows_are_counted_independently(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'ledger.xlsx';b=Workbook();b.active.append(['reportId','riskLevel'])
            b.active.append([7,'HIGH']);b.active.append([7,'HIGH']);b.active.append([8,'LOW']);b.save(p);b.close()
            r=read_ledger(p)
            self.assertEqual(r['counts'],{'7':2,'8':1});self.assertEqual(r['sha256'],hashlib.sha256(p.read_bytes()).hexdigest())

    def test_unreadable_ledger_shape_is_not_empty_success(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'ledger.xlsx';b=Workbook();b.active.append(['unexpected']);b.save(p);b.close()
            with self.assertRaises(EvidenceUnavailable):read_ledger(p)

    def test_mailpit_global_total_does_not_pollute_scoped_search(self):
        FakeClient.page={'total':900,'messages_count':0,'messages':[]}
        with patch('httpx.Client',FakeClient):
            self.assertEqual(read_mailpit('http://127.0.0.1:8025','unique@example.test','[EVAL]')['counts'],{})

    def test_mailpit_counts_matching_receipts_not_alert_records(self):
        FakeClient.page={'total':900,'messages_count':1,'messages':[{'ID':'physical-id','To':[{'Address':'unique@example.test'}],
                          'Subject':'[EVAL] reportId=12'}]}
        with patch('httpx.Client',FakeClient):
            actual=read_mailpit('http://127.0.0.1:8025','unique@example.test','[EVAL]')
        self.assertEqual(actual['counts'],{'12':1});self.assertEqual(actual['messages'][0]['id'],'physical-id')

    def test_wrong_mail_namespace_is_not_credited(self):
        FakeClient.page={'total':1,'messages_count':1,'messages':[{'ID':'foreign','To':[{'Address':'other@example.test'}],
                          'Subject':'[EVAL] reportId=12'}]}
        with patch('httpx.Client',FakeClient),self.assertRaises(EvidenceUnavailable):
            read_mailpit('http://127.0.0.1:8025','unique@example.test','[EVAL]')

    def test_unavailable_mailpit_does_not_become_zero_messages(self):
        with patch('httpx.Client',side_effect=ConnectionError('unavailable')),self.assertRaises(ConnectionError):
            read_mailpit('http://127.0.0.1:8025','unique@example.test','[EVAL]')

    def test_http_deadline_template_needs_no_persisted_review_outcome(self):
        mode,digest=identify_reply('fixed server template',None,fixed_template='fixed server template',has_error=True)
        self.assertEqual(mode,'safe_fallback');self.assertEqual(digest,hashlib.sha256(b'fixed server template').hexdigest())

    def test_arbitrary_text_is_not_a_fixed_template(self):
        mode,_=identify_reply('unreviewed different text',None,fixed_template='fixed server template',has_error=True)
        self.assertEqual(mode,'error')

    def test_foreign_session_label_does_not_imply_access_was_denied(self):
        mode,_=identify_reply('leaked foreign text',None,fixed_template='fixed template',access_denied=True,has_error=False)
        self.assertNotEqual(mode,'access_error')
