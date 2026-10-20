"""WEB-03X: nonblocking, bounded GUI retrieval queue (not a new permission path).

Only the broker can fetch. A job reserves an *already approved* request
before enqueue, preventing another tool/UI path from consuming the approval.
Cancellation invalidates broker authorization immediately; an in-flight socket
may finish its bounded timeout, but its result is discarded by the broker.
"""
from __future__ import annotations

from collections import deque
import secrets
import threading
import time


class WebRetrievalQueue:
    MAX_PENDING = 24

    def __init__(self, broker):
        self.broker = broker
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.jobs = {}
        self.pending = deque()
        self.worker = threading.Thread(target=self._work, name='localai-web-retrieval', daemon=True)
        self.worker.start()

    def submit(self, request_id, *, conversation_id):
        with self.condition:
            if len(self.pending) >= self.MAX_PENDING:
                raise ValueError('Web retrieval queue is full. Wait or cancel a queued request.')
            # reservation and queue insertion are serialized with cancellation
            record = self.broker.reserve_fetch(request_id, conversation_id=conversation_id)
            jid = secrets.token_hex(10)
            job = {'id': jid, 'request_id': request_id, 'conversation_id': conversation_id,
                   'kind':record['kind'], 'status':'queued', 'stage':'Waiting for retrieval worker',
                   'created_at':time.time(), 'started_at':None, 'elapsed_seconds':0.0,
                   'error':None}
            self.jobs[jid]=job
            self.pending.append(jid)
            self.condition.notify()
            return self._public(job)

    @staticmethod
    def _public(job):
        data={k:v for k,v in job.items() if k!='created_at' and k!='started_at'}
        if job['started_at'] is not None and job['status']=='running':
            data['elapsed_seconds']=round(time.monotonic()-job['started_at'],1)
        return data

    def list_jobs(self, *, conversation_id):
        with self.lock:
            return [self._public(j) for j in reversed(list(self.jobs.values()))
                    if j['conversation_id']==conversation_id][-50:]

    def cancel(self, job_id, *, conversation_id):
        with self.condition:
            job=self.jobs.get(job_id)
            if not job or job['conversation_id']!=conversation_id:
                raise ValueError('Unknown or wrong-conversation retrieval job.')
            if job['status'] not in ('queued','running'):
                raise PermissionError('Only queued or running jobs can be cancelled.')
            # Invalidate broker approval before marking the UI job cancelled.
            self.broker.cancel_fetch(job['request_id'],conversation_id=conversation_id)
            job['status']='cancelled';job['stage']='Cancelled; any pending response is discarded'
            if job_id in self.pending:self.pending.remove(job_id)
            self.condition.notify_all()
            return self._public(job)

    def cancel_all(self):
        """Used when switching Offline; invalidate all jobs before mode change."""
        with self.condition:
            for job in self.jobs.values():
                if job['status'] not in ('queued','running'):
                    continue
                try:
                    self.broker.cancel_fetch(job['request_id'],conversation_id=job['conversation_id'])
                except PermissionError:
                    pass
                job['status']='cancelled'
                job['stage']='Cancelled by Offline profile'
            self.pending.clear()
            self.condition.notify_all()

    def _work(self):
        while True:
            with self.condition:
                while not self.pending:self.condition.wait()
                jid=self.pending.popleft()
                job=self.jobs[jid]
                if job['status']!='queued':continue
                job['status']='running';job['stage']='Connecting to approved source'
                job['started_at']=time.monotonic()
                rid=job['request_id'];cid=job['conversation_id']
            try:
                result=self.broker.fetch(rid,conversation_id=cid,from_queue=True)
                status=result['status']
                error=(result.get('result') or {}).get('error') if status=='error' else None
            except Exception as exc:
                status='error';error=str(exc)[:200]
            with self.condition:
                job=self.jobs[jid]
                if job['status']=='cancelled':continue
                job['elapsed_seconds']=round(time.monotonic()-job['started_at'],1)
                job['status']='completed' if status=='fetched' else 'cancelled' if status=='expired' else 'error'
                job['stage']='Completed' if status=='fetched' else 'Not retrieved'
                job['error']=error
