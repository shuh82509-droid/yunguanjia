"""Indexed, account-scoped material read contracts. No network or write capability."""
import hashlib
import re
from datetime import datetime, timedelta

from .business_material_import import MaterialImportError, numeric, timestamp

TABLES = {'STANDARD':'d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report',
          'CHENGFANG':'d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report'}


def spec(sql, limit):
    return {'sql':sql,'sqlSha256':hashlib.sha256(sql.encode()).hexdigest(),'limit':limit}


def account_id(value):
    value=str(value)
    if not re.fullmatch(r'[0-9]{1,30}',value):
        raise MaterialImportError('账户编号不正确')
    return value


def cutoff_time(day,value,final=True):
    try:
        parsed=datetime.fromisoformat(str(value))
    except (ValueError,TypeError):
        raise MaterialImportError('账户快照时间不正确') from None
    start=datetime.combine(day,datetime.min.time()).replace(hour=23,minute=30) if final else datetime.combine(day,datetime.min.time())
    end=datetime.combine(day+timedelta(days=1),datetime.min.time())
    if parsed.tzinfo or not start<=parsed<=end:
        raise MaterialImportError('账户快照不属于当日最终窗口')
    return parsed.isoformat(sep=' ')


def discovery_spec(day,source):
    table=TABLES[source]
    return spec(f"SELECT advertiser_id, MAX(stat_time) AS source_cutoff_at FROM {table} FORCE INDEX (uk_qc_material_hourly) WHERE stat_time BETWEEN TIMESTAMP('{day} 23:30:00') AND TIMESTAMP('{day+timedelta(days=1)} 00:00:00') GROUP BY advertiser_id ORDER BY advertiser_id",300)


def day_discovery_spec(day,source):
    table=TABLES[source]
    return spec(f"SELECT advertiser_id, MAX(stat_time) AS source_cutoff_at FROM {table} FORCE INDEX (uk_qc_material_hourly) WHERE stat_time > TIMESTAMP('{day} 00:00:00') AND stat_time <= TIMESTAMP('{day+timedelta(days=1)} 00:00:00') GROUP BY advertiser_id ORDER BY advertiser_id",300)


def exclusion_spec(advertiser):
    advertiser=account_id(advertiser)
    return spec(f"SELECT '{advertiser}' AS advertiser_id, EXISTS(SELECT 1 FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger FORCE INDEX (uk_qc_material_asset) WHERE advertiser_id = '{advertiser}' AND advertiser_name LIKE '电商部达播%' LIMIT 1) AS ledger_excluded, EXISTS(SELECT 1 FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost FORCE INDEX (idx_qc_account_daily_advertiser_date) WHERE advertiser_id = '{advertiser}' AND advertiser_name LIKE '电商部达播%' LIMIT 1) AS account_excluded",10)


def account_specs(day,source,advertiser,cutoff):
    table=TABLES[source];advertiser=account_id(advertiser);cutoff=cutoff_time(day,cutoff)
    name="NULLIF(m.content_description, '')" if source=='STANDARD' else "COALESCE(NULLIF(m.material_name, ''), NULLIF(m.content_description, ''))"
    where=f"m.stat_time = TIMESTAMP('{cutoff}') AND m.advertiser_id = '{advertiser}' AND m.report_date = DATE('{day}')"
    video="COALESCE(NULLIF(m.platform_material_type, ''), 'VIDEO') = 'VIDEO'"
    gmv='COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0)'
    cost='COALESCE(m.stat_cost_for_roi2, 0)'
    summary=f"""SELECT '{day}' AS report_date, '{source}' AS source_platform, '{advertiser}' AS advertiser_id,
  (SELECT COUNT(*) FROM {table} m FORCE INDEX (uk_qc_material_hourly) WHERE {where}) AS snapshot_record_count,
  (SELECT COUNT(DISTINCT m.advertiser_id) FROM {table} m FORCE INDEX (uk_qc_material_hourly) WHERE {where}) AS snapshot_account_count,
  TIMESTAMP('{cutoff}') AS source_cutoff_at,
  COUNT(*) AS material_record_count,
  COALESCE(SUM(CASE WHEN {cost} > 0 THEN 1 ELSE 0 END),0) AS spent_material_count,
  COALESCE(SUM(CASE WHEN {cost} > 0 AND {gmv} > 0 THEN 1 ELSE 0 END),0) AS effective_material_count,
  COALESCE(SUM(CASE WHEN {cost} > 0 AND {name} IS NOT NULL THEN 1 ELSE 0 END),0) AS named_spent_material_count,
  ROUND(COALESCE(SUM({gmv}),0),2) AS total_gmv_yuan,
  ROUND(COALESCE(SUM({cost}),0),2) AS total_cost_yuan,
  COALESCE(SUM(COALESCE(m.total_pay_order_count_for_roi2,0)),0) AS total_order_count,
  MAX(m.updated_at) AS daily_source_updated_at
FROM {table} m FORCE INDEX (uk_qc_material_hourly)
WHERE {where} AND {video} AND ({cost} > 0 OR {gmv} > 0)"""
    candidates=f"""SELECT '{day}' AS report_date, '{source}' AS source_platform,
  m.advertiser_id, m.material_id, MAX({name}) AS material_name,
  NULL AS internal_author, NULL AS material_source,
  ROUND(SUM({gmv}),2) AS gmv_yuan, ROUND(SUM({cost}),2) AS cost_yuan,
  SUM(COALESCE(m.total_pay_order_count_for_roi2,0)) AS order_count,
  MAX(m.stat_time) AS source_cutoff_at, MAX(m.updated_at) AS source_updated_at
FROM {table} m FORCE INDEX (uk_qc_material_hourly)
WHERE {where} AND {video}
GROUP BY m.advertiser_id, m.material_id
HAVING gmv_yuan > 0 OR cost_yuan > 0
ORDER BY gmv_yuan DESC, cost_yuan DESC, m.material_id
LIMIT 200"""
    return {'summary':spec(summary,10),'candidates':spec(candidates,300)}


def validate_account_batches(value,day,now):
    if not isinstance(value,dict) or value.get('schemaVersion')!=2 or value.get('sourceMode')!='authorized-root-material-query-import':
        raise MaterialImportError('账户分批导入格式错误')
    if value.get('scope')!={'department':'品牌营销部','authorizedBy':'FD-026222'} or value.get('realBusinessDate')!=str(day):
        raise MaterialImportError('账户分批导入日期或授权范围不正确')
    external=timestamp(value.get('externalReadAt'))
    if not external or external>now+timedelta(minutes=5):
        raise MaterialImportError('账户分批导入读取时间错误')
    receipts={}
    def read_query(label,query,expected,exact_count=None):
        if not isinstance(query,dict) or query.get('sqlSha256')!=expected['sqlSha256'] or query.get('sql',expected['sql'])!=expected['sql']:
            raise MaterialImportError('账户分批查询 SQL 不匹配')
        rows=query.get('rows');stamp=timestamp(query.get('readAt'))
        if not isinstance(rows,list) or type(query.get('rowCount')) is not int or query['rowCount']!=len(rows) or query.get('truncated') is not False or len(rows)>=expected['limit']:
            raise MaterialImportError('账户分批结果不完整或被截断')
        if exact_count is not None and len(rows)!=exact_count:
            raise MaterialImportError('账户分批查询行数不正确')
        if not stamp or stamp>external or stamp.astimezone(now.tzinfo).date()<=day:
            raise MaterialImportError('账户分批查询读取时间不正确')
        receipts[label]={key:query[key] for key in ['sqlSha256','readAt','rowCount','truncated']}
        return rows
    for field in ['discovery','discoveryAfter','dayDiscovery','accounts']:
        if not isinstance(value.get(field),dict) or set(value[field])!=set(TABLES):
            raise MaterialImportError('账户分批缺少两个平台')
    discovered={};day_discovered={};all_ids=set()
    for source in TABLES:
        expected=discovery_spec(day,source)
        before=read_query('discovery:'+source,value['discovery'][source],expected)
        after=read_query('discoveryAfter:'+source,value['discoveryAfter'][source],expected)
        full_day=read_query('dayDiscovery:'+source,value['dayDiscovery'][source],day_discovery_spec(day,source))
        def indexed(rows,final=True):
            result={}
            for row in rows:
                advertiser=account_id(row.get('advertiser_id'))
                if advertiser in result:
                    raise MaterialImportError('账户发现结果重复')
                result[advertiser]=cutoff_time(day,row.get('source_cutoff_at'),final)
            return result
        first,second=indexed(before),indexed(after)
        if not first or len(first)>64:
            raise MaterialImportError('当天最终窗口没有账户快照，不能记为完整零值')
        if first!=second:
            raise MaterialImportError('采集前后账户范围或最终快照已变化，需补齐后复核')
        whole=indexed(full_day,False)
        if not set(first).issubset(whole) or len(whole)>64:
            raise MaterialImportError('晚窗账户必须属于全日已观测账户范围')
        if timestamp(value['dayDiscovery'][source]['readAt'])<timestamp(value['discoveryAfter'][source]['readAt']):
            raise MaterialImportError('全日账户覆盖复核必须晚于采集结束')
        discovered[source]=first;day_discovered[source]=whole;all_ids.update(whole)
    exclusions=value.get('exclusions')
    if not isinstance(exclusions,dict) or set(exclusions)!=all_ids:
        raise MaterialImportError('每个发现账户都必须有独立达播排除回执')
    excluded=set()
    for advertiser,query in exclusions.items():
        row=read_query('exclude:'+advertiser,query,exclusion_spec(advertiser),1)[0]
        if str(row.get('advertiser_id'))!=advertiser or row.get('ledger_excluded') not in (0,1) or row.get('account_excluded') not in (0,1):
            raise MaterialImportError('达播排除结果不正确')
        if row['ledger_excluded'] or row['account_excluded']:
            excluded.add(advertiser)
    all_rows=[];summaries=[];scope=[]
    counts=['snapshot_record_count','snapshot_account_count','material_record_count','spent_material_count','effective_material_count','named_spent_material_count','total_order_count']
    money=['total_gmv_yuan','total_cost_yuan']
    for source,accounts in discovered.items():
        eligible=set(accounts)-excluded
        if set(value['accounts'][source])!=eligible:
            raise MaterialImportError('日账户批次未闭合，不能漏账户或包含达播账户')
        daily_rows=[];daily_summaries=[]
        for advertiser in sorted(eligible):
            queries=value['accounts'][source][advertiser]
            if not isinstance(queries,dict) or set(queries)!={'summary','candidates'}:
                raise MaterialImportError('每个账户必须有完整汇总和有界候选')
            expected=account_specs(day,source,advertiser,accounts[advertiser])
            summary=read_query('summary:'+source+':'+advertiser,queries['summary'],expected['summary'],1)[0]
            candidates=read_query('candidates:'+source+':'+advertiser,queries['candidates'],expected['candidates'])
            if len(candidates)>200:
                raise MaterialImportError('账户候选超过固定前200条上限')
            for row in [summary]+candidates:
                if row.get('report_date')!=str(day) or row.get('source_platform')!=source or str(row.get('advertiser_id'))!=advertiser or cutoff_time(day,row.get('source_cutoff_at'))!=accounts[advertiser]:
                    raise MaterialImportError('账户批次日期、账户、平台或快照错配')
            numeric(summary,counts+money,counts)
            if summary['snapshot_account_count']!=1 or not summary['snapshot_record_count'] or float(summary['snapshot_record_count'])<float(summary['material_record_count']):
                raise MaterialImportError('最终快照与业务日期无对应记录')
            if not 0<=float(summary['effective_material_count'])<=float(summary['spent_material_count'])<=float(summary['material_record_count']) or float(summary['named_spent_material_count'])>float(summary['spent_material_count']):
                raise MaterialImportError('账户素材效率计数不守恒')
            if summary['material_record_count'] and not summary.get('daily_source_updated_at'):
                raise MaterialImportError('账户汇总缺少源时间')
            seen=set()
            for row in candidates:
                material=str(row.get('material_id'))
                if not re.fullmatch(r'-?[0-9]+',material) or material in seen or not row.get('source_updated_at'):
                    raise MaterialImportError('账户候选编号重复、缺失或缺少源时间')
                seen.add(material);numeric(row,['gmv_yuan','cost_yuan','order_count'],['order_count'])
            for field,full in [('gmv_yuan','total_gmv_yuan'),('cost_yuan','total_cost_yuan')]:
                if sum(float(row[field]) for row in candidates)>float(summary[full])+.05:
                    raise MaterialImportError('账户候选金额超过完整汇总')
            end=timestamp(value['discoveryAfter'][source]['readAt'])
            if any(timestamp(queries[key]['readAt'])>end for key in queries):
                raise MaterialImportError('账户范围复核必须晚于该账户查询')
            daily_summaries.append(summary);daily_rows.extend(candidates)
        combined={'report_date':str(day),'source_platform':source,'source_cutoff_at':max(accounts.values()),
            'daily_source_updated_at':max((str(row.get('daily_source_updated_at') or '') for row in daily_summaries),default=None)}
        for field in counts+money:
            combined[field]=round(sum(float(row[field]) for row in daily_summaries),2)
        selected=sorted(daily_rows,key=lambda row:(-float(row['gmv_yuan']),-float(row['cost_yuan']),str(row['advertiser_id']),str(row['material_id'])))[:200]
        all_rows.extend(selected);summaries.append(combined)
        scope.append({'sourcePlatform':source,'discoveredAccountCount':len(accounts),'excludedAccountCount':len(set(accounts)&excluded),'verifiedAccountCount':len(eligible),
            'dayObservedAccountCount':len(day_discovered[source]),'dayExcludedAccountCount':len(set(day_discovered[source])&excluded),
            'unclosedAccountIds':sorted(set(day_discovered[source])-set(accounts)-excluded),
            'accountCandidateRowCount':len(daily_rows),'selectedPlatformCandidateRowCount':len(selected)})
    complete=all(not item['unclosedAccountIds'] for item in scope)
    return {'rows':all_rows,'summaries':summaries,'completeDayCoverage':complete,'observation':{'businessDate':str(day),'externalReadAt':value['externalReadAt'],
        'collectionMode':'indexed-account-batches','accountCoverage':scope,'queries':receipts}}
