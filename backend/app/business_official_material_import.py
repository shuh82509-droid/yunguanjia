"""Private, reviewed official video day imports. No credentials, network, or source mixing."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

MODE = 'verified-official-qianchuan-video-financial-partitions'
PLATFORM = 'OFFICIAL_QIANCHUAN_VIDEO'
FIELDS = ('stat_cost_for_roi2', 'total_pay_order_gmv_for_roi2', 'total_pay_order_gmv_include_coupon_for_roi2')
EXPECTED = frozenset(('1758788274050062','1760222277505102','1761056209573901','1846827174923465',
 '1849835766114505','1850659758537995','1854813448093076','1854824483745801','1855705574634632',
 '1855705637693443','1864619805954196','1864699162217866','1869672250595328','1871831421669380',
 '1871833428793802','1875022942242346'))
EXCLUDED = '1871831398260748'
MONEY_NAMES = ('officialAccountTotalsAllMaterialTypes','coveredAccountTotalsAllMaterialTypes',
              'videoReportTotals','materialTotals','aggregateTotals','coveredAccountNonVideoOrUnallocatedRemainder')
TOPICS = {'OVERALL_ROI_PRODUCT_MATERIAL','SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO','OVERALL_ROI_LIVE_MATERIAL_VIDEO','SITE_PROMOTION_POST_DATA_VIDEO'}


class OfficialMaterialError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise OfficialMaterialError(message)


def currency(value):
    require(isinstance(value,str) and re.fullmatch(r'\d+\.\d{2}',value), '官方金额必须是精确两位小数且不能缺失')
    return Decimal(value)


def total(values):
    result={field:Decimal(0) for field in FIELDS}
    for value in values:
        require(type(value) is dict and set(value)==set(FIELDS),'官方消耗、实付、含券指标不完整')
        for field in FIELDS:
            result[field]+=currency(value[field])
    return {field:str(value.quantize(Decimal('.01'))) for field,value in result.items()}


def stamp(value):
    require(isinstance(value,str),'官方读取时间缺失')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    require(result.tzinfo is not None,'官方读取时间必须带时区')
    return result


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_regular(path, maximum):
    require(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=maximum,'官方导入文件缺失、过大或不是普通文件')
    raw=path.read_bytes()
    require(len(raw)<=maximum,'官方导入文件读取期间超出上限')
    return raw


def validate_manifest(value, now):
    require(type(value) is dict and value.get('schemaVersion')==1 and value.get('sourceMode')==MODE,'官方导入 manifest 格式错误')
    require(value.get('scope')=={'department':'品牌营销部','authorizedBy':'FD-026222'},'官方导入不属于已批准业务范围')
    accounts=value.get('accounts')
    require(type(accounts) is list and len(accounts)==len(EXPECTED) and {r.get('id') for r in accounts}==EXPECTED,'官方账户范围必须与已批准16账户一致')
    require(all(isinstance(r.get('name'),str) and r['name'].strip() and not r['name'].startswith('电商部达播') for r in accounts),'官方账户名称未核验或属于达播')
    exclusions=value.get('userRequestedExclusions')
    require(type(exclusions) is list and len(exclusions)==1 and exclusions[0].get('advertiserId')==EXCLUDED
            and exclusions[0].get('reason')=='user_requested_exclusion' and exclusions[0].get('treatAsZero') is False
            and exclusions[0].get('originalDataDeleted') is False,'缺少用户明确排除范围证据')
    require(stamp(value.get('publishedAt',value.get('preparedAt')))<=now+timedelta(minutes=5),'官方清单准备或发布时间在未来')
    snapshots=value.get('snapshots')
    require(type(snapshots) is dict and len(snapshots)<=366,'官方日清单缺失或过大')
    for day,item in snapshots.items():
        date.fromisoformat(day)
        require(type(item) is dict and re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256','')))
                and item.get('file')==f"{day}.{item['sha256']}.json",'官方日文件名或SHA不匹配')
        require(stamp(item.get('readAt'))<=now+timedelta(minutes=5),'官方日读取时间在未来')
    policy={'scope':value['scope'],'accounts':sorted(EXPECTED),'userRequestedExclusions':exclusions}
    return digest(json.dumps(policy,sort_keys=True,ensure_ascii=False).encode())


def validate_day(doc, day, manifest, observation, now):
    require(type(doc) is dict and doc.get('schemaVersion')==1 and doc.get('sourceMode')==MODE and doc.get('statDate')==str(day),'官方日数据来源或日期错误')
    require(doc.get('state')=='complete_for_declared_scope' and doc.get('expectedAccountIds')==sorted(EXPECTED)
            and doc.get('userRequestedExclusions')==manifest['userRequestedExclusions'],'官方日数据未闭合或账户策略漂移')
    require(doc.get('verifiedFinancialVideoAccountCount')==len(EXPECTED) and doc.get('verifiedAccountTotalCount')==len(EXPECTED)
            and doc.get('missingOrUnclosedAccounts')==[],'官方日完整性声明与账户数不符')
    external=stamp(observation['readAt'])
    require(external.astimezone(timezone(timedelta(hours=8))).date()>day and external<=now+timedelta(minutes=5),'官方数据并非日终后真实读取')
    evidence=doc.get('sourceInputSha256')
    require(type(evidence) is dict and len(evidence)>=len(EXPECTED)*4
            and all(re.fullmatch(r'[0-9a-f]{64}',str(value)) for value in evidence.values()),'缺少原始输入SHA证据')
    budgets=doc.get('coveredAccounts')
    require(type(budgets) is list and len(budgets)==len(EXPECTED) and {r.get('advertiserId') for r in budgets}==EXPECTED,'覆盖账户身份不符')
    for record in budgets:
        r=record.get('budgetReconciliation') or {}
        require(r.get('advertiserId')==record['advertiserId'] and r.get('statDate')==str(day)
                and r.get('observedTopicBudgetPartitionVerified') is True and r.get('providedGoalEvidenceConsistent') is True
                and r.get('legacyStandardZeroVerified') is True and r.get('accountTotals')==record['accountTotalsAllMaterialTypes'],'账户日官方预算没有闭合')
        checks=r.get('materialChecks')
        require(type(checks) is list and len(checks)==3 and {c.get('dataTopic') for c in checks}==TOPICS-{'SITE_PROMOTION_POST_DATA_VIDEO'}
                and all(c.get('containedInParent') is True for c in checks),'原视频主题金额未核对所属预算')
        refs=r.get('receiptReferences')
        require(type(refs) is list and {'all-promotion-account','legacy-standard-all-platforms','overall-product-plan','site-product-plan','overall-live-account'}<= {ref.get('kind') for ref in refs},'缺少独立账户或主题回执')
        for ref in refs:
            require(ref.get('officialRequestId') and re.fullmatch(r'[0-9a-f]{64}',str(ref.get('rawSha256','')))
                    and stamp(ref.get('readAt'))<=external,'账户原始回执或读取时间不一致')
    require(total(r['accountTotalsAllMaterialTypes'] for r in budgets)==doc.get('officialAccountTotalsAllMaterialTypes')==doc.get('coveredAccountTotalsAllMaterialTypes'),'账户总金额不守恒')
    for name in MONEY_NAMES:
        total([doc.get(name)])
    require(total([doc['materialTotals'],doc['aggregateTotals']])==doc['videoReportTotals'],'逐素材与聚合桶金额不守恒')
    require(total([doc['videoReportTotals'],doc['coveredAccountNonVideoOrUnallocatedRemainder']])==doc['coveredAccountTotalsAllMaterialTypes'],'视频与账户差额不守恒')
    records=doc.get('records');buckets=doc.get('aggregateBuckets')
    require(type(records) is list and len(records)<=50000 and type(buckets) is list and len(buckets)<=5000,'官方素材或聚合桶列表异常')
    identities=set();origins=set()
    def verify_origin(account,origin,material_id):
        require(origin.get('dataTopic') in TOPICS and str(origin.get('reportedDimensionId'))==material_id
                and re.fullmatch(r'[0-9a-f]{64}',str(origin.get('rawPageSha256',''))),'官方素材来源主题或原页身份不符')
        partition=()
        if origin['dataTopic']=='SITE_PROMOTION_POST_DATA_VIDEO':
            require(isinstance(origin.get('anchorId'),str) and origin['anchorId'].isdigit()
                    and origin.get('ecpAppId') in ('1','2') and origin.get('bidType') in ('0','7') and origin.get('videoType') is not None,'SITE直播来源分区不完整')
            partition=(origin['anchorId'],origin['ecpAppId'],origin['bidType'],str(origin['videoType']))
        key=(account,origin['dataTopic'],material_id,*partition)
        require(key not in origins,'同一财务来源重复计入');origins.add(key)
    for record in records:
        key=(record.get('advertiserId'),record.get('materialId'))
        require(key[0] in EXPECTED and isinstance(key[1],str) and re.fullmatch(r'[1-9]\d*',key[1]) and key not in identities and record.get('statDate')==str(day),'官方素材身份、日期缺失或重复')
        identities.add(key)
        require(record.get('person') is None,'官方素材不得凭名称直接归人')
        require(type(record.get('origins')) is list and record['origins'] and total(o['metrics'] for o in record['origins'])==record['metrics'],'素材来源金额未闭合')
        require(all(re.fullmatch(r'[0-9a-f]{64}',str(o.get('rawPageSha256',''))) for o in record['origins']),'素材原页SHA缺失')
        for origin in record['origins']:verify_origin(key[0],origin,key[1])
        require(type(record.get('names')) is list and all(isinstance(n,str) and len(n)<=4000 for n in record['names']),'官方素材名格式异常')
    for record in buckets:
        require(record.get('advertiserId') in EXPECTED and record.get('statDate')==str(day) and record.get('materialId') is None and record.get('person') is None,'官方聚合桶不能指认具体素材或人员')
        require(total([record['origin']['metrics']])==record['metrics'],'官方聚合桶来源金额不符')
        aggregate_id=str(record['origin'].get('reportedDimensionId'))
        require(re.fullmatch(r'-?\d+',aggregate_id) and int(aggregate_id)<=0,'官方聚合桶维度错误')
        verify_origin(record['advertiserId'],record['origin'],aggregate_id)
    require(total(r['metrics'] for r in records)==doc['materialTotals'] and total(r['metrics'] for r in buckets)==doc['aggregateTotals'],'官方记录合计与报表总额不符')
    rows=[]
    for record in records:
        rows.append({'report_date':str(day),'source_platform':PLATFORM,'advertiser_id':record['advertiserId'],
                     'material_id':record['materialId'],'material_name':max(record['names'],key=len,default=''),
                     'internal_author':None,'material_source':'千川官方自然日视频报表','order_count':None,
                     'gmv_yuan':float(currency(record['metrics'][FIELDS[2]])),
                     'actual_pay_gmv_yuan':float(currency(record['metrics'][FIELDS[1]])),
                     'cost_yuan':float(currency(record['metrics'][FIELDS[0]])),
                     'source_updated_at':observation['readAt'],'source_cutoff_at':str(day)+' 23:59:59+08:00'})
    spent=[r for r in rows if r['cost_yuan']>0]
    summary={'report_date':str(day),'source_platform':PLATFORM,'material_record_count':len(rows),
             'spent_material_count':len(spent),'effective_material_count':sum(r['gmv_yuan']>0 for r in spent),
             'named_spent_material_count':sum(bool(r['material_name']) for r in spent),
             'total_gmv_yuan':float(currency(doc['videoReportTotals'][FIELDS[2]])),
             'total_cost_yuan':float(currency(doc['videoReportTotals'][FIELDS[0]])),
             'daily_source_updated_at':observation['readAt']}
    return {'rows':rows,'summary':summary,'money':{name:doc[name] for name in MONEY_NAMES},
            'bucketCount':len(buckets),'readAt':observation['readAt'], 'businessDate':str(day)}


def read_official_range(directory, target, days, now):
    path=directory/'manifest.json'
    if not path.exists():
        return None
    coverage={'state':'partial','startDate':str(target-timedelta(days=days-1)),'endDate':str(target),
              'requestedDays':days,'observedDays':0,'missingDays':[],'sourceMode':MODE,
              'summaryScope':'用户确认的16账户官方全日视频，含官方聚合桶；排除电商部达播和用户指定的新户5',
              'candidateScope':'全部已核验正数ID视频，聚合桶不计为具体素材；账户素材日计数',
              'metric':'total_pay_order_gmv_include_coupon_for_roi2',
              'metricLabel':'千川含券视频归因成交（含平台补贴口径）',
              'actualPayMetric':'total_pay_order_gmv_for_roi2','orderCountAvailable':False}
    empty={'rows':[],'summaries':[],'freshness':{},'readAt':None,'externalReadAt':None,'sourceMode':MODE,
           'officialScopeEnforced':True,'importCoverage':coverage}
    try:
        manifest_raw=read_regular(path,200000);manifest=json.loads(manifest_raw)
        policy=validate_manifest(manifest,now);coverage['scopeFingerprint']=policy;coverage['manifestSha256']=digest(manifest_raw)
        coverage['userRequestedExclusions']=manifest['userRequestedExclusions'];coverage['accountCount']=len(EXPECTED)
    except (OSError,ValueError,TypeError,KeyError) as error:
        coverage['missingDays']=[{'reason':str(error)[:180]}]
        return empty
    daily=[]
    for offset in range(days):
        day=target-timedelta(days=days-offset-1)
        try:
            observation=manifest['snapshots'].get(str(day))
            require(observation is not None,'该自然日尚无已核验官方文件')
            raw=read_regular(directory/observation['file'],16_000_000)
            require(digest(raw)==observation['sha256'],'官方日文件SHA与发布清单不符')
            daily.append(validate_day(json.loads(raw),day,manifest,observation,now))
        except (OSError,ValueError,TypeError,KeyError) as error:
            coverage['missingDays'].append({'businessDate':str(day),'reason':str(error)[:180]})
    coverage['observedDays']=len(daily)
    coverage['observations']=[{'businessDate':d['businessDate'],'readAt':d['readAt']} for d in daily]
    if coverage['missingDays']:
        return empty
    coverage['state']='complete'
    read=max((d['readAt'] for d in daily),key=stamp)
    return {**empty,'rows':[row for d in daily for row in d['rows']], 'summaries':[d['summary'] for d in daily],
            'readAt':read,'externalReadAt':read,'freshness':{PLATFORM:str(target)},
            'officialTotals':{name:total(d['money'][name] for d in daily) for name in MONEY_NAMES},
            'officialAggregateBucketCount':sum(d['bucketCount'] for d in daily)}


def apply_official_metadata(snapshot, source):
    """Keep old API fields explicitly coupon-inclusive; expose actual pay separately."""
    if source.get('sourceMode')!=MODE:
        return
    coverage=snapshot['coverage']['material']
    coverage['source']='千川官方 · 完整自然日视频财务报表（16账户）'
    coverage['metricLabel']='含券视频归因成交'
    coverage['scopeFingerprint']=source['importCoverage'].get('scopeFingerprint')
    coverage['state']='available'
    coverage['officialTotals']=deepcopy(source['officialTotals'])
    coverage['aggregateBucketCount']=source['officialAggregateBucketCount']
    coverage['materialCountScope']='账户×素材×自然日；聚合桶不计具体素材'
    totals=source['officialTotals'];summary=snapshot['summary']
    summary['materialActualPayGmvYuan']=float(currency(totals['videoReportTotals'][FIELDS[1]]))
    summary['materialCouponInclusiveGmvYuan']=float(currency(totals['videoReportTotals'][FIELDS[2]]))
    actual={}
    for row in source['rows']:
        key=row['material_id'];actual[key]=actual.get(key,Decimal(0))+Decimal(str(row['actual_pay_gmv_yuan']))
    for values in (snapshot.get('topMaterials',[]),snapshot.get('_materialCandidates',[])):
        for record in values:
            record['orderCount']=None
            record['actualPayGmvYuan']=float(actual[record['materialId']])
            record['couponInclusiveGmvYuan']=record['gmvYuan']
            record['gmvMetric']='total_pay_order_gmv_include_coupon_for_roi2'
    snapshot['quality']['materialUnresolvedCandidateRowCount']=source['officialAggregateBucketCount']
    snapshot['quality']['materialUnresolvedCandidateGmvYuan']=float(currency(totals['aggregateTotals'][FIELDS[2]]))


def same_official_scope(coverage, source):
    return (coverage.get('sourceMode')==MODE and coverage.get('scopeFingerprint')
            and coverage.get('scopeFingerprint')==source.get('importCoverage',{}).get('scopeFingerprint'))
