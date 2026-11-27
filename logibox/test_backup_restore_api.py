"""Authenticated restore HTTP flow and exclusion of concurrent operations."""
import base64
import json
import unittest
from unittest.mock import patch
import backup_restore as br
import test_purchase_attachments as helper


class RestoreApiTests(unittest.TestCase):
    setUpClass=classmethod(helper.PurchaseApiTests.setUpClass.__func__)
    request=classmethod(helper.PurchaseApiTests.request.__func__)
    api=helper.PurchaseApiTests.api

    def test_authentication_required_and_confirmation_required(self):
        self.api('POST','/api/backup/inspect',{'data':''},expected=401,auth=False)
        self.api('POST','/api/backup/restore',{},expected=400)

    def test_live_preview_restore_relogin_and_replay_rejected(self):
        status,_,raw=self.request('GET','/api/backup/full')
        self.assertEqual(status,200)
        info=self.api('POST','/api/backup/inspect',{'data':base64.b64encode(raw).decode()})
        self.assertFalse(info['missing']);self.assertTrue(info['created_at'])
        result=self.api('POST','/api/backup/restore',{'token':info['token'],'confirmed':True})
        self.assertTrue(result['ok'])
        self.api('GET','/api/master',expected=401)
        status,headers,_=self.request('POST','/api/login',{'password':'test-password'})
        self.assertEqual(status,200);type(self).cookie=headers['Set-Cookie'].split(';')[0]
        self.api('POST','/api/backup/restore',{'token':info['token'],'confirmed':True},expected=400)
        self.api('GET','/api/master')

    def test_restore_blocks_concurrent_actions(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        import server
        status,_,raw=self.request('GET','/api/backup/full')
        info=self.api('POST','/api/backup/inspect',{'data':base64.b64encode(raw).decode()})
        entered=threading.Event();release=threading.Event()
        actual=br.restore
        def slow_restore(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise RuntimeError('test timeout')
            return actual(*args,**kwargs)
        with patch.object(br,'restore',side_effect=slow_restore),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.request,'POST','/api/backup/restore',{'token':info['token'],'confirmed':True})
            try:
                self.assertTrue(entered.wait(10))
                self.api('POST','/api/cases',{'name':'must not be created','planned_rate_pct':7},expected=409)
                self.api('POST','/api/backup/restore',{'confirmed':True,'token':info['token']},expected=409)
            finally:release.set()
            self.assertEqual(future.result()[0],200)
        status,headers,_=self.request('POST','/api/login',{'password':'test-password'})
        self.assertEqual(status,200);type(self).cookie=headers['Set-Cookie'].split(';')[0]


if __name__=='__main__':unittest.main()
