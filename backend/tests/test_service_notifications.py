import os
os.environ['DATABASE_URL']='sqlite:///:memory:'
import unittest
from sqlalchemy import select
from app.database import Base, engine, SessionLocal
from app import main
from app.models import Asset, ReviewWorkflowConfig, ReviewRoleAssignment, UserNotification

class ServiceNotificationTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        main.ensure_asset_schema()
        self.user={'number':'FD-TEST','realName':'测试审核人','groupName':'品牌营销部','status':'normal'}

    def test_review_submission_next_reviewer_and_rejection_are_durable_and_deduplicated(self):
        with SessionLocal() as db:
            cfg=db.get(ReviewWorkflowConfig,1)
            if not cfg:
                cfg=ReviewWorkflowConfig(id=1);db.add(cfg)
            cfg.enabled=True;cfg.naming_enabled=False;cfg.ai_redline_enabled=False
            for role in ['team_lead','supervisor']:
                db.add(ReviewRoleAssignment(role_code=role,identifier='test-'+role,user_number='FD-TEST',user_name='测试审核人',center='',active=True))
            asset=Asset(object_key='local/test.mp4',filename='本地测试.mp4',media_type='video',category='黑晶面膜')
            db.add(asset);db.commit()
            sub=main.review_asset_submit(asset.id,main.AssetReviewSubmit(note='本地通知测试'),db,self.user)
            rows=list(db.scalars(select(UserNotification)))
            self.assertEqual(len(rows),1,'submitter and reviewer same person receives one combined notice')
            self.assertEqual(rows[0].recipient_number,'FD-TEST')
            self.assertIn('组长',rows[0].message)
            main.review_asset_submit(asset.id,main.AssetReviewSubmit(note='重复提交'),db,self.user)
            self.assertEqual(len(list(db.scalars(select(UserNotification)))),1)
            main.review_approve(sub['id'],main.AssetReviewAct(role_code='team_lead',quality_scores={k:15 for k in main.REVIEW_QUALITY_KEYS}),db,self.user)
            rows=list(db.scalars(select(UserNotification).order_by(UserNotification.id)))
            self.assertEqual(len(rows),2)
            self.assertIn('主管',rows[-1].message)
            main.review_reject(sub['id'],main.AssetReviewAct(role_code='supervisor',note='测试驳回意见'),db,self.user)
            rows=list(db.scalars(select(UserNotification).order_by(UserNotification.id)))
            self.assertEqual(len(rows),3)
            self.assertIn('测试驳回意见',rows[-1].message)
            self.assertTrue(all(row.external_status=='pending' for row in rows))

if __name__=='__main__':unittest.main()
