"""Operator-owned, read-only daily material observations; no network or write API."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
from pathlib import Path


class MaterialImportError(ValueError):
    pass


def material_query_specs(target, summary_sql, candidate_sql):
    return {
        source.lower() + kind: {
            'sql': sql, 'sqlSha256': hashlib.sha256(sql.encode()).hexdigest(),
            'limit': 10 if kind == 'Summary' else 300,
            'sourcePlatform': source, 'businessDate': target.isoformat(),
            'resultScope': 'full_qualifying_snapshot_summary' if kind == 'Summary' else 'daily_top_200_candidate',
        }
        for source in ('STANDARD', 'CHENGFANG')
        for kind, builder in (('Summary', summary_sql), ('Candidates', candidate_sql))
        for sql in [builder(target, source)]
    }


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def numeric(row, fields, integer_fields=()):
    for field in fields:
        raw = row.get(field)
        try:
            number = float(raw)
        except (ValueError, TypeError):
            number = float('nan')
        if isinstance(raw, bool) or not math.isfinite(number) or number < 0 or field in integer_fields and not number.is_integer():
            raise MaterialImportError(f'素材 {field} 不是可核验的非负数')


def validate_material_import(value, target, now, specs):
    if not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('sourceMode') != 'authorized-root-material-query-import':
        raise MaterialImportError('素材导入格式错误')
    if value.get('scope') != {'department': '品牌营销部', 'authorizedBy': 'FD-026222'} or value.get('realBusinessDate') != target.isoformat():
        raise MaterialImportError('素材导入授权范围或业务日期不匹配')
    read_at = timestamp(value.get('externalReadAt'))
    if not read_at or read_at > now + timedelta(minutes=5):
        raise MaterialImportError('素材外部读取时间不正确')
    queries = value.get('queries')
    if not isinstance(queries, dict) or set(queries) != set(specs):
        raise MaterialImportError('素材两平台汇总和候选必须分别提供')
    result = deepcopy(value)
    for key, query in result['queries'].items():
        spec = specs[key]
        if not isinstance(query, dict) or query.get('sqlSha256') != spec['sqlSha256'] or query.get('sql', spec['sql']) != spec['sql']:
            raise MaterialImportError('素材 SQL 校验值不匹配')
        rows = query.get('rows')
        if not isinstance(rows, list) or type(query.get('rowCount')) is not int or len(rows) != query['rowCount'] or query.get('truncated') is not False:
            raise MaterialImportError('素材查询传输被截断或行数不匹配')
        stamp = timestamp(query.get('readAt'))
        if not stamp or stamp > read_at or stamp.astimezone(now.tzinfo).date() <= target:
            raise MaterialImportError('素材查询必须在完整自然日后读取')
        summary = key.endswith('Summary')
        if summary and len(rows) != 1 or not summary and len(rows) > 200:
            raise MaterialImportError('素材汇总或每日候选界限不正确')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get('report_date') != target.isoformat() or row.get('source_platform') != spec['sourcePlatform']:
                raise MaterialImportError('素材行日期或平台不匹配')
            try:
                cutoff = datetime.fromisoformat(str(row.get('source_cutoff_at')).replace('Z', '+00:00'))
                cutoff = cutoff.astimezone(now.tzinfo).replace(tzinfo=None) if cutoff.tzinfo else cutoff
            except (ValueError, TypeError):
                raise MaterialImportError('素材源快照时间缺失或格式错误') from None
            if not datetime.combine(target, datetime.min.time()).replace(hour=23, minute=30) <= cutoff <= datetime.combine(target + timedelta(days=1), datetime.min.time()):
                raise MaterialImportError('素材源快照不属于该日最终采集窗口')
            if summary:
                counts = ['material_record_count', 'spent_material_count', 'effective_material_count', 'named_spent_material_count', 'total_order_count', 'snapshot_account_count']
                # SQL's aggregate over no active records yields NULL sums. A
                # positive final-snapshot account count proves the observed zero.
                if row.get('material_record_count') in (0, '0') and float(row.get('snapshot_account_count') or 0) > 0:
                    for field in counts + ['total_gmv_yuan', 'total_cost_yuan']:
                        if row.get(field) is None:
                            row[field] = 0
                numeric(row, counts + ['total_gmv_yuan', 'total_cost_yuan'], counts)
                if not row['snapshot_account_count'] or not row.get('source_cutoff_at'):
                    raise MaterialImportError('当天没有最终快照，不能将未采集素材记为零')
                if not 0 <= float(row['effective_material_count']) <= float(row['spent_material_count']) <= float(row['material_record_count']):
                    raise MaterialImportError('素材效率计数不守恒')
                if float(row['named_spent_material_count']) > float(row['spent_material_count']):
                    raise MaterialImportError('素材命名覆盖计数不守恒')
                if row['material_record_count'] and not row.get('daily_source_updated_at'):
                    raise MaterialImportError('素材汇总缺少源更新时间')
            else:
                identity = (str(row.get('advertiser_id') or ''), str(row.get('material_id') or ''))
                if not re.fullmatch(r'[0-9]+', identity[0]) or not re.fullmatch(r'-?[0-9]+', identity[1]) or identity in seen:
                    raise MaterialImportError('素材账户和素材编号缺失或重复')
                seen.add(identity)
                numeric(row, ['gmv_yuan', 'cost_yuan', 'order_count'], ['order_count'])
                if not row.get('source_updated_at') or not row.get('source_cutoff_at'):
                    raise MaterialImportError('素材候选缺少源更新时间')
        if not summary:
            total = result['queries'][key.replace('Candidates', 'Summary')]['rows'][0]
            # Independent sums establish coverage. Candidates never replace the
            # denominator, even when the SQL intentionally returns exactly 200.
            for field, full in [('gmv_yuan', 'total_gmv_yuan'), ('cost_yuan', 'total_cost_yuan')]:
                if sum(float(row[field]) for row in rows) > float(total.get(full) or 0) + .05:
                    raise MaterialImportError('素材候选金额超过对应完整汇总')
    return result


def read_material_range(directory: Path, target: date, days: int, now: datetime, specs_for_day):
    rows, summaries, observations, gaps = [], [], [], []
    for offset in range(days):
        day = target - timedelta(days=days - offset - 1)
        path = directory / f'{day.isoformat()}-material.json'
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 8_000_000:
                raise MaterialImportError('日文件缺失或不符合受限文件规则')
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw,dict) and raw.get('schemaVersion')==2:
                from .business_material_batches import validate_account_batches
                value=validate_account_batches(raw,day,now)
                rows.extend(value['rows']);summaries.extend(value['summaries']);observations.append(value['observation'])
                if not value['completeDayCoverage']:
                    gaps.append({'businessDate':str(day),'reason':'全日有记录的非达播账户缺少最终快照，不能冒充完整日',
                        'accountCoverage':value['observation']['accountCoverage']})
                continue
            if path.stat().st_size>2_000_000:
                raise MaterialImportError('普通日导入超过受限文件大小')
            value = validate_material_import(raw, day, now, specs_for_day(day))
            for key, query in value['queries'].items():
                (summaries if key.endswith('Summary') else rows).extend(query['rows'])
            observations.append({'businessDate': day.isoformat(), 'externalReadAt': value['externalReadAt'],
                'queries': {key: {field: query[field] for field in ['sqlSha256', 'readAt', 'rowCount', 'truncated']} for key, query in value['queries'].items()}})
        except (OSError, ValueError, TypeError) as error:
            gaps.append({'businessDate': day.isoformat(), 'reason': str(error)[:180]})
    coverage = {'state': 'complete' if not gaps else 'partial', 'startDate': (target - timedelta(days=days-1)).isoformat(),
        'endDate': target.isoformat(), 'requestedDays': days, 'observedDays': len(observations), 'missingDays': gaps,
        'summaryScope': '全部满足晚间最终快照条件的账户素材日记录汇总，永久排除电商部达播；上游应到账户全集完整性未单独验证',
        'candidateScope': '每日每平台前200条高成交候选；不代表全部素材或全部成片',
        'observations': observations}
    return {'rows': rows, 'summaries': summaries, 'importCoverage': coverage,
        'sourceMode': 'authorized-root-material-query-import',
        'externalReadAt': max((item['externalReadAt'] for item in observations), key=lambda item: timestamp(item), default=None),
        'readAt': max((q['readAt'] for item in observations for q in item['queries'].values()), key=lambda item: timestamp(item), default=None),
        'freshness': {source: target.isoformat() for source in ['QIANCHUAN_STANDARD', 'QIANCHUAN_CHENGFANG']} if not gaps else {}}
