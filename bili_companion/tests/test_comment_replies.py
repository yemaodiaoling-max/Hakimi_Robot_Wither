import tempfile
import unittest
from pathlib import Path
from bili_companion.runtime.storage import Database
from bili_companion.policy.comment_replies import CommentReplies
from bili_companion.platform.bilibili import SendResult
from bili_companion.platform.bilibili import PlatformError
import urllib.error


def row(rpid,uid,text,root=0,parent=0):
    return dict(rpid=rpid,oid=2,root=root,parent=parent,member=dict(mid=uid,uname='person'),
        content=dict(message=text),ctime=1000)


class RepliesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'test.db');self.now=1100
        self.thread=dict(root=row(10,'1','own piano'),replies=[row(11,'2','piano?',10,10)])
        self.sent=[];self.calls=0;self.uid='1';self.fail=False
        self.read_calls=[];self.anonymous_error=None;self.account_error=None;self.account_thread=None
        test=self
        class Platform:
            def authenticated_uid(self):return test.uid
            def comment_thread(self,*args,authenticated=False):
                test.read_calls.append(authenticated)
                if test.fail:raise RuntimeError()
                error=test.account_error if authenticated else test.anonymous_error
                if error:raise error
                return test.account_thread if authenticated and test.account_thread is not None else test.thread
            def send_comment_reply(self,*args,before_post):
                if not before_post():return SendResult('rejected','cancelled_before_post')
                test.sent.append(args);return SendResult('sent','accepted',12,2)
        class Engine:
            def reply_to_comment(self,*args):test.calls+=1;return 'yes piano','model-1'
        self.service=CommentReplies(self.db,Platform(),Engine(),'logical',clock=lambda:self.now,enabled=True)

    def receipt(self,rpid=10):
        with self.db.connect(write=True) as db:db.execute('INSERT INTO comment_receipts VALUES (?,?,?,?,?,?,?,?)',
            ('logical','BV1xx411c7mD',2,rpid,'own piano',1000,'accepted',None))

    def test_no_manual_adoption(self):
        self.assertEqual(self.service.poll(),'no_tracked_root');self.assertFalse(self.sent)

    def test_send_once_and_public_verify(self):
        self.receipt();self.assertEqual(self.service.poll(),'sent');self.assertEqual(len(self.sent),1)
        self.thread['replies'].append(row(12,'1','yes piano',10,11));self.now+=601
        self.service.poll()
        with self.db.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM comment_reply_attempts').fetchone()[0],'public_confirmed')
            self.assertEqual(db.execute('SELECT visibility FROM comment_receipts').fetchone()[0],'public_confirmed')
        self.assertEqual(self.calls,1)

    def test_wrong_author_not_adopted(self):
        self.receipt();self.uid='9';self.assertEqual(self.service.poll(),'not_confirmed');self.assertFalse(self.sent)

    def test_deleted_root_never_adopted(self):
        self.receipt();self.thread['root']['state']=-1
        self.assertEqual(self.service.poll(),'not_confirmed');self.assertFalse(self.sent)

    def test_retracted_parent_fails_preflight(self):
        self.receipt();original=self.service.engine.reply_to_comment
        def generate(*args):
            result=original(*args);self.thread['replies']=[];return result
        self.service.engine.reply_to_comment=generate
        self.assertEqual(self.service.poll(),'rejected');self.assertFalse(self.sent)

    def test_self_sibling_bot_and_old_ignored(self):
        self.receipt()
        self.thread['replies']=[row(11,'1','self',10,10),row(12,'2','sibling',10,99),row(13,'3','auto',10,10)]
        self.thread['replies'][2]['member']['uname']='helpful Bot'
        self.assertEqual(self.service.poll(),'no_eligible_reply');self.assertEqual(self.calls,0)

    def test_interruption_after_generation_blocks_post(self):
        self.receipt();checks=iter([True,False]);self.service.poll(lambda:next(checks));self.assertFalse(self.sent)

    def test_read_failure_preserves_visibility_and_advances(self):
        self.receipt();self.receipt(20);self.fail=True;self.assertEqual(self.service.poll(),'read_unavailable')
        with self.db.connect() as db:
            rows=db.execute('SELECT * FROM comment_receipts ORDER BY rpid').fetchall()
            self.assertEqual(rows[0]['visibility'],'accepted');self.assertEqual(rows[0]['checked_at'],1100)
            self.assertIsNone(rows[1]['checked_at'])

    def check(self):
        with self.db.connect() as db:return dict(db.execute('SELECT * FROM comment_read_checks').fetchone())

    def test_explicit_missing_then_owner_readable(self):
        self.receipt();self.anonymous_error=PlatformError('comment_read_unavailable');self.anonymous_error.business_code=12006
        self.assertEqual(self.service.poll(),'not_confirmed')
        self.assertEqual(self.read_calls,[False,True]);self.assertFalse(self.sent)
        check=self.check();self.assertEqual((check['anonymous_state'],check['anonymous_code'],check['account_state']),('missing','12006','matched'))
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT visibility FROM comment_receipts').fetchone()[0],'account_only_readable')

    def test_both_missing_is_observation_not_moderation_claim(self):
        self.receipt();error=PlatformError('comment_read_unavailable');error.business_code=12006
        self.anonymous_error=self.account_error=error;self.service.poll()
        self.assertEqual(self.read_calls,[False,True])
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT visibility FROM comment_receipts').fetchone()[0],'both_not_read')

    def test_owner_reads_exact_original_in_state_17_without_authorizing_reply(self):
        self.receipt()
        self.anonymous_error=PlatformError('comment_read_unavailable');self.anonymous_error.business_code=12006
        self.account_thread=dict(root=dict(row(10,'1','own piano'),state=17,invisible=False),
            replies=[row(11,'2','piano?',10,10)])
        self.assertEqual(self.service.poll(),'not_confirmed')
        check=self.check()
        self.assertEqual((check['anonymous_state'],check['account_state'],check['account_code'],
            check['account_review_state']),('missing','matched','0',17))
        self.assertEqual(self.calls,0);self.assertFalse(self.sent)
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT visibility FROM comment_receipts').fetchone()[0],'account_only_readable')

    def test_anonymous_nonzero_state_never_authorizes_reply(self):
        self.receipt();self.thread['root'].update(state=17,invisible=False)
        self.assertEqual(self.service.poll(),'not_confirmed')
        self.assertEqual((self.check()['anonymous_state'],self.check()['anonymous_review_state']),('mismatch',17))
        self.assertEqual(self.calls,0);self.assertFalse(self.sent)

    def test_owner_read_still_requires_all_identity_and_text_fields(self):
        receipt=dict(oid=2,rpid=10,text='own piano')
        for key,value in [('rpid',99),('oid',99),('root',99),('parent',99),
                ('member',dict(mid='2')),('content',dict(message='other'))]:
            with self.subTest(key=key):
                self.account_thread=dict(root=dict(row(10,'1','own piano'),state=17,**{key:value}),replies=[])
                status,_,_=self.service.read_side(receipt,'1',authenticated=True)
                self.assertEqual(status,'mismatch')

    def test_read_state_columns_migrate_existing_check_table(self):
        with self.db.connect(write=True) as db:
            db.execute('DROP TABLE comment_read_checks')
            db.execute('''CREATE TABLE comment_read_checks(account TEXT,rpid INTEGER,
                anonymous_state TEXT,anonymous_code TEXT,account_state TEXT,account_code TEXT,
                checked_at REAL,PRIMARY KEY(account,rpid))''')
            db.execute("INSERT INTO comment_read_checks VALUES ('logical',10,'missing','12006','matched','0',1000)")
        CommentReplies.initialize(self.db)
        check=self.check()
        self.assertEqual(check['account_state'],'matched')
        self.assertIsNone(check['account_review_state'])

    def test_http_block_does_not_trigger_account_read_or_erase_previous(self):
        self.receipt()
        with self.db.connect(write=True) as db:db.execute("UPDATE comment_receipts SET visibility='public_confirmed'")
        self.anonymous_error=urllib.error.HTTPError('https://private.invalid',412,'blocked',None,None)
        self.addCleanup(self.anonymous_error.close)
        self.assertEqual(self.service.poll(),'read_unavailable');self.assertEqual(self.read_calls,[False])
        self.assertEqual(self.check()['anonymous_code'],'http_412')
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT visibility FROM comment_receipts').fetchone()[0],'public_confirmed')

    def test_business_risk_control_and_timeout_never_mean_missing(self):
        self.receipt();error=PlatformError('comment_read_unavailable');error.business_code=-412
        for failure,code in [(error,'-412'),(TimeoutError(),'timeout')]:
            self.anonymous_error=failure;self.service.poll();self.now+=601
            self.assertEqual(self.check()['anonymous_code'],code);self.assertEqual(self.check()['anonymous_state'],'blocked')
        self.assertEqual(self.read_calls,[False,False]);self.assertFalse(self.sent)

    def test_mismatch_then_account_timeout_is_blocked(self):
        self.receipt();self.thread['root']['content']['message']='placeholder'
        self.account_error=TimeoutError();self.assertEqual(self.service.poll(),'read_unavailable')
        self.assertEqual(self.check()['anonymous_state'],'mismatch');self.assertEqual(self.check()['account_code'],'timeout')

    def test_restart_ambiguous_intent_not_retried(self):
        self.receipt()
        with self.db.connect(write=True) as db:db.execute('INSERT INTO comment_reply_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('logical','BV1xx411c7mD',10,11,'2','piano?','',1000,'day','generating',None,''))
        self.assertEqual(self.service.poll(),'no_eligible_reply');self.assertEqual(self.calls,0)

if __name__=='__main__':unittest.main()
