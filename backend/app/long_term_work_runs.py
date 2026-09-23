"""Private, bounded journals for the existing long-term-work refresh lane.

One application writer; no new service, worker, public raw-evidence endpoint or
credentials on argv. A resumed round reuses immutable confirmed responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlencode
from uuid import uuid4

RUN_ID = re.compile(r"^[a-f0-9]{32}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")

def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()

def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def sync_directory(path: Path) -> None:
    if os.name == 'nt': return  # POSIX directory durability is tested on Linux.
    fd=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def private_directory(path: Path) -> None:
    if path.is_symlink():raise ValueError('Private directory cannot be a symlink')
    if path.exists():
        if not path.is_dir():raise ValueError('Expected private directory')
        return
    private_directory(path.parent)
    path.mkdir(mode=0o700)
    sync_directory(path.parent)

def private_write(path: Path, body: bytes) -> None:
    private_directory(path.parent)
    if path.is_symlink():raise ValueError('Private file cannot be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(body); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

@dataclass(frozen=True)
class Limits:
    total_seconds: float = 300
    source_seconds: float = 60
    source_calls: int = 20
    model_calls: int = 4
    model_seconds: float = 60
    response_bytes: int = 4 * 1024 * 1024

class RunStopped(RuntimeError):
    def __init__(self, message: str, state: str):
        super().__init__(message); self.state = state

@dataclass
class RawResponse:
    status_code: int
    body: bytes
    headers: bytes
    returncode: int = 0
    elapsed: float = 0
    def json(self) -> Any:
        return json.loads(self.body)

def _curl_quote(value: str) -> str:
    return '"' + value.replace('\\','\\\\').replace('"','\\"').replace('\r','\\r').replace('\n','\\n').replace('\t','\\t') + '"'

def bounded_transfer(*, url: str, headers: dict, payload: dict | None, seconds: float,
                     directory: Path, max_bytes: int, memory_only: bool = False) -> RawResponse:
    """curl's total-transfer timer also covers DNS and trickling response bodies.

The existing image has curl 8.5/AsynchDNS. No --retry, redirects, shell, verbose
headers, request credentials on argv, or subprocess timeout that kills a worker.
"""
    if seconds <= 0: raise RunStopped('本轮读取时间预算已用完', 'budget_exhausted')
    executable = '/usr/bin/curl' if os.name != 'nt' else str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/curl.exe')
    def execute(body_path: Path | None, headers_path: Path | None) -> tuple[subprocess.CompletedProcess,float]:
        lines = ['silent', 'show-error', 'retry = 0', 'max-redirs = 0', 'proto = "=http,https"',
            'max-time = ' + str(round(seconds, 3)), 'connect-timeout = ' + str(round(min(5,seconds),3)),
            'max-filesize = ' + str(max_bytes), 'url = ' + _curl_quote(url),
            'output = ' + _curl_quote(str(body_path) if body_path else '-'),
            'write-out = ' + _curl_quote('%{http_code}' if body_path else '%{stderr}\nWIS_HTTP:%{http_code}\n')]
        if headers_path:lines.append('dump-header = ' + _curl_quote(str(headers_path)))
        for key, value in headers.items():
            if any(c in str(key) + str(value) for c in '\r\n'):
                raise ValueError('Header contains invalid newline')
            lines.append('header = ' + _curl_quote(str(key) + ': ' + str(value)))
        if payload is not None:
            lines.extend(['request = "POST"', 'header = "Content-Type: application/json"',
                          'data-binary = ' + _curl_quote(canonical(payload).decode('utf-8'))])
        started = time.monotonic()
        completed = subprocess.run([executable, '-q', '--config', '-'],
            input=('\n'.join(lines)+'\n').encode('utf-8'), capture_output=True)
        return completed,time.monotonic()-started
    if memory_only:
        # Authentication never creates transfer files, not even temporary ones.
        # Only bounded stdout holds its response; no response headers are saved.
        completed,elapsed=execute(None,None)
        matches=re.findall(rb'WIS_HTTP:(\d{3})',completed.stderr)
        status=int(matches[-1]) if matches else 0
        return RawResponse(status,completed.stdout[:max_bytes],b'',completed.returncode,elapsed)
    with tempfile.TemporaryDirectory(prefix='.transfer-', dir=directory) as tmp:
        target = Path(tmp); os.chmod(target, 0o700)
        body_path = target/'body'; headers_path = target/'headers'
        for item in (body_path, headers_path):
            item.touch(mode=0o600); os.chmod(item, 0o600)
        completed,elapsed=execute(body_path,headers_path)
        # stderr is deliberately not persisted: it may contain a configured URL.
        raw_body = body_path.read_bytes()
        if len(raw_body) > max_bytes: raw_body = raw_body[:max_bytes]
        status = int(completed.stdout) if completed.stdout.strip().isdigit() else 0
        return RawResponse(status, raw_body, headers_path.read_bytes(), completed.returncode, elapsed)

class RunJournal:
    def __init__(self, root: Path, run_id: str, *, limits: Limits | None = None,
                 clock: Callable[[],float] = time.monotonic, transfer: Callable = bounded_transfer):
        if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run id')
        self.path = root/run_id; self.file = self.path/'journal.json'
        if root.is_symlink() or self.path.is_symlink() or self.file.is_symlink():raise ValueError('Journal must use real private paths')
        self.limits = limits or Limits(); self.clock = clock; self.transfer = transfer
        self.state = json.loads(self.file.read_text(encoding='utf-8'))
        if self.state.get('runId') != run_id or self.state.get('schemaVersion') != 1:
            raise ValueError('Run journal cannot be verified')
        self.started = None; self.source_started = None
        self.source_calls = 0; self.model_calls = 0

    @classmethod
    def create(cls, root: Path, *, window_start: str, window_end: str, **kwargs) -> 'RunJournal':
        if root.is_symlink():raise ValueError('Journal root cannot be a symlink')
        private_directory(root); os.chmod(root,0o700)
        run_id = uuid4().hex
        # Publish the directory only after its initial journal is durable.
        # Concurrent readonly lookup never sees a half-created visible run.
        path = root/('.initial-'+run_id); private_directory(path)
        value = {'schemaVersion':1, 'runId':run_id, 'createdAt':timestamp(), 'status':'pending',
            'windowStart':window_start, 'windowEnd':window_end, 'rounds':[], 'requestIds':[],
            'sourceState':'pending', 'analysisState':'pending', 'sources':{}, 'models':{}, 'plan':[],
            'messagesRead':0, 'documentsRead':0, 'sourceReadAt':None, 'lastError':None}
        private_write(path/'journal.json',canonical(value))
        os.rename(path,root/run_id); sync_directory(root)
        return cls(root,run_id,**kwargs)

    @property
    def run_id(self) -> str: return self.state['runId']
    def save(self) -> None: private_write(self.file, canonical(self.state))
    def has_request(self, request_id: str) -> bool: return request_id in self.state['requestIds']

    def begin_round(self, request_id: str, *, now: str) -> bool:
        if not REQUEST_ID.fullmatch(request_id): raise ValueError('Invalid refresh request id')
        if self.has_request(request_id): return False
        self.started = self.clock(); self.source_started = self.started
        self.source_calls = self.model_calls = 0
        self.state['requestIds'].append(request_id)
        self.state['rounds'].append({'requestId':request_id,'startedAt':now,'status':'running',
            'sourceCalls':0,'modelCalls':0, 'limits':self.limits.__dict__})
        self.state.update(status='running',lastError=None)
        self.save()
        index_file=self.path.parent/'request-index.json'
        if index_file.is_symlink():raise ValueError('Request index cannot be a symlink')
        index=json.loads(index_file.read_text()) if index_file.exists() else {}
        if request_id in index and index[request_id]!=self.run_id:raise ValueError('Request id already belongs to a different run')
        index[request_id]=self.run_id; private_write(index_file,canonical(index))
        return True

    @classmethod
    def for_request(cls, root: Path, request_id: str) -> 'RunJournal | None':
        if not REQUEST_ID.fullmatch(request_id):raise ValueError('Invalid refresh request id')
        path=root/'request-index.json'
        if root.is_symlink() or path.is_symlink():raise ValueError('Request index cannot be a symlink')
        run_id=json.loads(path.read_text()).get(request_id) if path.exists() else None
        if run_id:return cls(root,run_id)
        # A journal commit can succeed while the following index commit fails.
        # A read-only fallback finds that exact intent rather than accepting the
        # same request as new after a lost/uncertain index write.
        found=[]
        for candidate in root.iterdir() if root.exists() else []:
            if RUN_ID.fullmatch(candidate.name):
                if not candidate.is_symlink() and candidate.is_dir() and not (candidate/'journal.json').exists() and not any(candidate.iterdir()):
                    # An empty legacy creation has no intent or returned data.
                    # Missing journals with ANY remaining file still fail closed.
                    continue
                journal=cls(root,candidate.name)
                if journal.has_request(request_id):found.append(journal)
        if len(found)>1:raise ValueError('Request id has conflicting journals')
        return found[0] if found else None

    def remaining(self, stage: str) -> float:
        if self.started is None: raise RuntimeError('Round not started')
        total = self.limits.total_seconds-(self.clock()-self.started)
        if stage == 'source': return min(total,self.limits.source_seconds-(self.clock()-self.source_started))
        return min(total,self.limits.model_seconds)

    def _reserve(self, stage: str) -> float:
        remaining = self.remaining(stage)
        exhausted = self.source_calls >= self.limits.source_calls if stage == 'source' else self.model_calls >= self.limits.model_calls
        if remaining <= .01 or exhausted:
            raise RunStopped('本轮读取预算已到上限；已保存结果，可继续同一任务', 'budget_exhausted')
        if stage == 'source': self.source_calls += 1
        else: self.model_calls += 1
        row=self.state['rounds'][-1]; row.update(sourceCalls=self.source_calls,modelCalls=self.model_calls)
        self.save()
        remaining = self.remaining(stage)
        if remaining <= .01:raise RunStopped('本轮读取时间预算已用完，未新增外部调用','budget_exhausted')
        return min(remaining,20 if stage=='source' else self.limits.model_seconds)

    def _response_from(self, entry: dict) -> RawResponse:
        paths=[self.path/entry[k] for k in ('bodyFile','headersFile')]
        if any(p.is_symlink() or not p.resolve().is_relative_to(self.path.resolve()) for p in paths):
            raise RunStopped('读取回执路径无法校验','blocked_state')
        body=paths[0].read_bytes(); headers=paths[1].read_bytes()
        if digest(body)!=entry['bodySha256'] or digest(headers)!=entry['headersSha256']:
            raise RunStopped('已保存的读取回执校验失败，未重新发送请求','blocked_state')
        return RawResponse(entry['httpStatus'],body,headers,entry['returncode'],entry.get('elapsedSeconds',0))

    def source(self, url: str, params: dict | None, headers: dict, *, auth_payload: dict | None = None) -> RawResponse:
        if self.remaining('source')<=.01:raise RunStopped('本轮来源读取时间预算已用完，可继续同一任务','budget_exhausted')
        if auth_payload is not None:
            seconds=self._reserve('source')
            # Authentication body/response contains credentials and is memory-only.
            response=self.transfer(url=url,headers=headers,payload=auth_payload,seconds=seconds,
                directory=self.path,max_bytes=min(65536,self.limits.response_bytes),memory_only=True)
            self.state['rounds'][-1]['lastAuth']={'at':timestamp(),'httpStatus':response.status_code,'returncode':response.returncode}
            self.save()
            if response.returncode: raise RunStopped('飞书授权读取未完成，已保留旧快照','error')
            return response
        key=digest(canonical({'url':url,'params':params or {}})); entries=self.state['sources']
        prior=entries.get(key)
        if prior and prior.get('state')=='complete': return self._response_from(prior)
        seconds=self._reserve('source'); self.state['sourceState']='running'
        entry={'state':'intent','url':url,'params':params or {},'intentAt':timestamp()}; entries[key]=entry; self.save()
        target=url+('?' + urlencode(params) if params else '')
        seconds=min(seconds,self.remaining('source'))
        if seconds <= .01:raise RunStopped('本轮来源时间预算已用完，未新增调用','budget_exhausted')
        response=self.transfer(url=target,headers=headers,payload=None,seconds=seconds,
            directory=self.path,max_bytes=self.limits.response_bytes)
        self._record_response('source',key,entry,response)
        try:
            payload=response.json(); success=response.status_code==200 and isinstance(payload,dict) and payload.get('code',0)==0
        except (ValueError,UnicodeError): success=False
        entry['state']='complete' if success and response.returncode==0 else 'failed'; self.save()
        if response.returncode: raise RunStopped('飞书来源读取未完成；已保存本轮进度','error')
        return response

    def _record_response(self, kind: str, key: str, entry: dict, response: RawResponse) -> None:
        folder=self.path/kind; private_directory(folder)
        # Unique receipts retain previous failed readbacks instead of replacing them.
        stem=key+'-'+uuid4().hex
        body_file=kind+'/'+stem+'.body'; headers_file=kind+'/'+stem+'.headers'
        private_write(self.path/body_file,response.body); private_write(self.path/headers_file,response.headers)
        entry.update(bodyFile=body_file,headersFile=headers_file,bodySha256=digest(response.body),
            headersSha256=digest(response.headers),httpStatus=response.status_code,returncode=response.returncode,
            elapsedSeconds=response.elapsed,responseReceivedAt=timestamp())
        self.save()

    def sources_complete(self, *, messages: int, documents: int, errors: list) -> None:
        times=[e.get('responseReceivedAt') for e in self.state['sources'].values() if e.get('state')=='complete']
        self.state.update(sourceState='complete' if not errors else 'partial',messagesRead=messages,
            documentsRead=documents,sourceReadAt=max(filter(None,times),default=None),sourceErrorCount=len(errors))
        self.save()
        if errors: raise RunStopped('部分关联文档尚未读取成功；本轮来源已保存，旧事项保持不变','partial')

    def set_plan(self, payloads: list[dict]) -> None:
        plan=[digest(canonical(p)) for p in payloads]
        if self.state['plan'] and self.state['plan']!=plan:
            raise RunStopped('本次固定来源的提炼输入发生变化，未重复发送模型请求','blocked_state')
        self.state['plan']=plan; self.state['analysisState']='running'
        private_directory(self.path/'inputs')
        for payload,key in zip(payloads,plan):
            destination=self.path/'inputs'/(key+'.json')
            if destination.is_symlink():raise RunStopped('提炼输入路径无法校验','blocked_state')
            if destination.exists():
                if digest(destination.read_bytes())!=key:raise RunStopped('提炼输入校验失败','blocked_state')
            else:private_write(destination,canonical(payload))
        self.save()

    def model(self, url: str, headers: dict, payload: dict) -> RawResponse:
        if self.remaining('analysis')<=.01:raise RunStopped('本轮提炼时间预算已用完，可继续同一任务','budget_exhausted')
        key=digest(canonical(payload)); entries=self.state['models']; prior=entries.get(key)
        if prior:
            if prior.get('state') in {'intent','uncertain'}:
                raise RunStopped('模型请求结果尚不确定；保留本次编号，不自动再次发送','uncertain')
            return self._response_from(prior)
        seconds=self._reserve('analysis')
        entry={'state':'intent','inputSha256':key,'intentAt':timestamp()}; entries[key]=entry; self.save()
        seconds=min(seconds,self.remaining('analysis'))
        if seconds <= .01:
            # No call has started; persist that fact before allowing a future round.
            del entries[key]; self.save()
            raise RunStopped('本轮提炼时间预算已用完，未新增模型调用','budget_exhausted')
        response=self.transfer(url=url,headers=headers,payload=payload,seconds=seconds,
            directory=self.path,max_bytes=self.limits.response_bytes)
        self._record_response('model',key,entry,response)
        entry['state']='received' if response.returncode==0 else 'uncertain'
        if response.returncode==0:
            try:
                value=response.json()
                if isinstance(value,dict):
                    entry['requestId']=value.get('request_id') or value.get('requestId') or value.get('id')
                    usage=value.get('usage') or (value.get('data') or {}).get('usage')
                    if isinstance(usage,dict):entry['usage']=usage
            except (ValueError,UnicodeError,AttributeError,TypeError):pass
        self.save()
        if response.returncode:raise RunStopped('模型网络等待结束，结果尚不确定；未重复发送','uncertain')
        return response

    def model_validated(self, payload: dict) -> None:
        key=digest(canonical(payload)); self.state['models'][key]['validated']=True; self.save()

    def finish(self, status: str, message: str | None = None) -> None:
        self.state.update(status=status,lastError=message)
        if status=='ready':self.state['analysisState']='complete'
        elif status in {'uncertain','budget_exhausted','blocked_state'}:self.state['analysisState']=status if self.state['plan'] else 'pending'
        elif self.state['plan']:self.state['analysisState']='failed'
        self.state['rounds'][-1].update(status=status,finishedAt=timestamp(),sourceCalls=self.source_calls,
            modelCalls=self.model_calls,elapsedSeconds=self.clock()-self.started)
        self.save()

    def public(self) -> dict:
        state=self.state; planned=state['plan']; models=state['models']; last=state['rounds'][-1] if state['rounds'] else {}
        complete=sum(bool(models.get(k,{}).get('validated')) for k in planned)
        uncertain=sum(e.get('state') in {'intent','uncertain'} for e in models.values())
        return {'runId':self.run_id,'status':state['status'],'windowStart':state['windowStart'],'windowEnd':state['windowEnd'],
            'sourceState':state['sourceState'],'analysisState':state['analysisState'],'messagesRead':state['messagesRead'],
            'documentsRead':state['documentsRead'],'sourceReadAt':state['sourceReadAt'],'sourceErrorCount':state.get('sourceErrorCount',0),
            'completedBatches':complete,'totalBatches':len(planned),'uncertainBatches':uncertain,'roundCount':len(state['rounds']),
            'sourceCallsThisRound':last.get('sourceCalls',0),'modelCallsThisRound':last.get('modelCalls',0),
            'lastRoundAt':last.get('startedAt'),'lastFinishedAt':last.get('finishedAt'),'lastError':state['lastError'],
            'lastRequestId':state['requestIds'][-1] if state['requestIds'] else None,
            'canContinue':state['status'] in {'budget_exhausted','partial','interrupted','error'} and not uncertain,
            'limits':(last.get('limits') or self.limits.__dict__)}
