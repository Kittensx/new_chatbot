"""WEB-03: deny-by-default, host-owned, scoped web retrieval and citation broker.

The AI can suggest a URL, but only a direct authenticated GUI decision creates
a one-use permission. Fetched evidence is untrusted, bounded, and only
returned to the model upon an approved tool call; no automatic memory promotion. This module must not be copied into Pi's shell/tool environment.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
from html.parser import HTMLParser
import re
import time
import xml.etree.ElementTree as ET
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import ssl
import threading
from urllib.parse import urlsplit, urlunsplit, quote_plus

from .memory_store import _atomic_json

UTC = dt.timezone.utc
MAX_BYTES = 128 * 1024
MAX_ENTRIES = 250
MAX_GRANTS = 60
MAX_BATCH = 50
MAX_TEXT = 12000


def timestamp():
    return dt.datetime.now(UTC)


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value:
        raise ValueError('A public HTTP(S) URL is required (maximum 2048 characters).')
    if any(ord(c) < 33 or ord(c) == 127 or c == '\\' for c in value):
        raise ValueError('Whitespace, controls and backslashes are not valid in URLs.')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError('Only public HTTP(S) URLs without embedded credentials are allowed.')
    if parts.fragment:
        raise ValueError('Remove the URL fragment before requesting access.')
    try:
        host = parts.hostname.encode('idna').decode('ascii').lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid host or port.') from exc
    if not host or len(host) > 253 or host.endswith('.') or '..' in host or ':' in host:
        raise ValueError('Only public DNS hostnames are supported in WEB-01.')
    if '.' not in host or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') or
                               not all(c.isascii() and (c.isalnum() or c == '-') for c in label)
                               for label in host.split('.')):
        raise ValueError('A public DNS domain name is required.')
    try:
        ipaddress.ip_address(host)
        raise ValueError('Direct IP-address access is disabled.')
    except ValueError as exc:
        if str(exc) == 'Direct IP-address access is disabled.':
            raise
    standard = 443 if parts.scheme == 'https' else 80
    if port is not None and port != standard:
        raise ValueError('Only standard HTTP/HTTPS ports are available.')
    return urlunsplit((parts.scheme, host, parts.path or '/', parts.query, ''))


def resolve_public(host: str, port: int) -> str:
    """Resolve once, reject mixed private/public answers, then pin the socket IP."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('DNS returned no addresses.')
    candidates = []
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if not ip.is_global:
            raise ValueError('DNS destination includes a non-public IP address.')
        candidates.append(str(ip))
    return candidates[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, **kwargs):
        self._approved_ip = ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        raw = socket.create_connection((self._approved_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, **kwargs):
        self._approved_ip = ip
        super().__init__(host, port, **kwargs)

    def connect(self):
        self.sock = socket.create_connection((self._approved_ip, self.port), self.timeout, self.source_address)


class _ReadableHTML(HTMLParser):
    """Text-only extraction; never renders, executes, or imports page markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
        self.blocked = {'script','style','noscript','svg','iframe','template','form'}

    def handle_starttag(self, tag, attrs):
        if tag in self.blocked:
            self.hidden += 1
        elif not self.hidden and tag in {'p','div','article','section','h1','h2','h3','li','br','tr'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.blocked and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {'p','div','article','section','li','h1','h2','h3','tr'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text_from_html(source: str) -> str:
    parser = _ReadableHTML()
    parser.feed(source)
    parser.close()
    text = ''.join(parser.parts)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()[:MAX_TEXT]


def _read_public_response(url: str, allowed_mimes: set) -> tuple:
    """Bounded public GET with DNS/IP pinning; no redirects, cookies or proxies."""
    url = canonical_url(url)
    parts = urlsplit(url)
    port = 443 if parts.scheme == 'https' else 80
    ip = resolve_public(parts.hostname, port)
    cls = _PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP
    options = {'timeout': 8}
    if parts.scheme == 'https':
        options['context'] = ssl.create_default_context()
    connection = cls(parts.hostname, port, ip, **options)
    try:
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        connection.request('GET', path, headers={
            'Host': parts.hostname, 'Accept': 'text/html,text/plain,application/rss+xml,application/xml',
            'Accept-Encoding': 'identity', 'User-Agent': 'LocalAI-WebBroker/0.3 (user-approved)',
            'Connection': 'close',
        })
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValueError('Redirect blocked. Request the destination URL separately.')
        if response.status != 200:
            raise ValueError(f'Web endpoint returned HTTP {response.status}.')
        ctype = response.getheader('Content-Type', '')
        mime = ctype.lower().split(';', 1)[0].strip()
        if mime not in allowed_mimes:
            raise ValueError('Unsupported web content type: ' + mime[:50])
        if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
            raise ValueError('Compressed content is not permitted.')
        length = response.getheader('Content-Length')
        if length and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise ValueError('Web content exceeds byte limit.')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Web content exceeds byte limit.')
        charset = 'utf-8'
        for component in ctype.split(';')[1:]:
            if component.strip().lower().startswith('charset='):
                candidate = component.split('=', 1)[1].strip().strip('"').lower()
                if candidate in ('utf-8','iso-8859-1','windows-1252','us-ascii'):
                    charset = candidate
        return {'url': url, 'mime': mime, 'status': 200,
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}, raw.decode(charset, errors='replace')
    finally:
        connection.close()


def fetch_public_text(url: str) -> dict:
    """Extract plain text from one approved, public HTML/text URL."""
    info, content = _read_public_response(url, {'text/html','text/plain'})
    info['preview'] = plain_text_from_html(content) if info['mime'] == 'text/html' else content[:MAX_TEXT]
    return info


def search_public_web(query: str) -> dict:
    """Opt-in public search through fixed Bing RSS endpoint; no credentials.

    Search is best-effort: providers can block or change their anonymous feeds.
    The exact query is sent only once its search proposal is approved.
    """
    if not isinstance(query,str) or not 1 <= len(query.strip()) <= 300 or any(ord(c)<32 for c in query):
        raise ValueError('Search query must contain 1-300 printable characters.')
    search_url='https://www.bing.com/search?format=rss&q='+quote_plus(query.strip())
    info, content=_read_public_response(search_url, {'application/rss+xml','application/xml','text/xml','text/html'})
    # A provider CAPTCHA or HTML page is not a search result. Never scrape an
    # unexpected page for purported results or silently follow a login redirect.
    if info['mime']=='text/html':
        raise ValueError('Search provider did not return an RSS feed. Try again later or propose exact pages.')
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        raise ValueError('Search XML with declarations or entities is not permitted.')
    try:
        root=ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('Search provider returned an invalid RSS feed.') from exc
    if root.tag.lower()!='rss':
        raise ValueError('Search provider returned non-RSS XML.')
    hits=[]
    for node in root.findall('./channel/item'):
        if len(hits)>=8:break
        label=plain_text_from_html(node.findtext('title',''))[:170]
        summary=plain_text_from_html(node.findtext('description',''))[:380]
        target=(node.findtext('link','') or '').strip()
        try:
            target=canonical_url(target)
        except ValueError:
            continue
        if target.startswith('http://'):
            # Returned HTTP links are safe to propose, but never auto-followed.
            pass
        hits.append({'index':len(hits)+1,'title':label,'url':target,'snippet':summary})
    if not hits:
        raise ValueError('Search provider returned no usable public results.')
    return {**info, 'preview':'\n'.join(f"{h['index']}. {h['title']} - {h['url']}: {h['snippet']}" for h in hits)[:MAX_TEXT],
            'hits':hits,'provider':'Bing public RSS'}


SEARCH_PROVIDERS = {
    'bing_rss': 'Bing public RSS (general)',
    'google_news_rss': 'Google News RSS (news only)',
    'searxng_json': 'Custom public SearXNG (JSON API)',
}


def validated_provider(provider: str, endpoint: str | None = None) -> dict:
    if provider not in SEARCH_PROVIDERS:
        raise ValueError('Unsupported search provider.')
    if provider != 'searxng_json':
        if endpoint not in (None, ''):
            raise ValueError('Custom endpoint is only valid for SearXNG.')
        return {'provider': provider, 'endpoint': None}
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError('Enter a public HTTPS SearXNG search endpoint.')
    url = canonical_url(endpoint)
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.query or not parts.path.rstrip('/').endswith('/search'):
        raise ValueError('SearXNG endpoint must be a public HTTPS URL ending in /search, without query parameters.')
    return {'provider': provider, 'endpoint': url}


def search_with_provider(query: str, provider: str = 'bing_rss', endpoint: str | None = None) -> dict:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300 or any(ord(c) < 32 for c in query):
        raise ValueError('Search query must contain 1-300 printable characters.')
    setting = validated_provider(provider, endpoint)
    if provider == 'bing_rss':
        # Preserve one well-tested, fixed public provider path.
        return search_public_web(query)
    if provider == 'google_news_rss':
        url = 'https://news.google.com/rss/search?q=' + quote_plus(query.strip()) + '&hl=en-US&gl=US&ceid=US:en'
        provider_name = 'Google News RSS (news only)'
        info, content = _read_public_response(url, {'application/rss+xml','application/xml','text/xml'})
        if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
            raise ValueError('Unsafe search feed declarations.')
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError('Search provider returned an invalid RSS feed.') from exc
        if root.tag.lower() != 'rss':
            raise ValueError('Search provider returned non-RSS XML.')
        hits=[]
        for node in root.findall('./channel/item'):
            if len(hits) >= 8: break
            try: target = canonical_url((node.findtext('link','') or '').strip())
            except ValueError: continue
            hits.append({'index':len(hits)+1, 'title':plain_text_from_html(node.findtext('title',''))[:170],
                         'url':target, 'snippet':plain_text_from_html(node.findtext('description',''))[:380]})
    else:
        url = setting['endpoint'] + ('&' if '?' in setting['endpoint'] else '?') + 'format=json&q=' + quote_plus(query.strip())
        provider_name = 'Custom SearXNG JSON'
        info, content = _read_public_response(url, {'application/json'})
        try: payload = json.loads(content)
        except (ValueError, TypeError) as exc: raise ValueError('Search provider returned invalid JSON.') from exc
        if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
            raise ValueError('Search provider returned an unsupported JSON schema.')
        hits=[]
        for item in payload['results'][:40]:
            if len(hits)>=8:break
            if not isinstance(item,dict):continue
            try:target=canonical_url(item.get('url',''))
            except ValueError:continue
            hits.append({'index':len(hits)+1,'title':plain_text_from_html(str(item.get('title','')))[:170],
                         'url':target, 'snippet':plain_text_from_html(str(item.get('content','')))[:380]})
    if not hits:
        raise ValueError('Search provider returned no usable public results.')
    return {**info, 'preview':'\n'.join(f"{h['index']}. {h['title']} - {h['url']}: {h['snippet']}" for h in hits)[:MAX_TEXT],
            'hits':hits,'provider':provider_name}


class WebPermissionBroker:
    """Host-owned approvals: known-request batch != future-access grant.

    Persistent JSON is an audit/recovery record, NOT persistent authorization.
    Every approval/grant expires on process startup; the user must grant again.
    """
    def __init__(self, root: Path, *, audit=None, fetcher=None, searcher=None, isolation_check=None):
        self.folder = Path(root)
        self.file = self.folder / 'web_permissions.json'
        self.lock = threading.RLock()
        self.audit = audit
        self._fetcher = fetcher or fetch_public_text
        self._searcher = searcher
        self.provider_file = self.folder / 'search_provider.json'
        self.provider_settings = {'provider':'bing_rss', 'endpoint':None}
        if self.provider_file.is_file():
            raw_provider=json.loads(self.provider_file.read_text(encoding='utf-8'))
            self.provider_settings=validated_provider(raw_provider.get('provider'),raw_provider.get('endpoint'))
        self._slots = threading.BoundedSemaphore(2)
        self._isolation_check = isolation_check
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.file.is_file():
            raw = json.loads(self.file.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or raw.get('schema') != 'local-ai-web-permissions-1' or \
                    raw.get('mode') not in ('offline','web') or not isinstance(raw.get('requests'),list):
                raise ValueError('Unrecognized web permission store. Existing data unchanged.')
            self.data = raw
            # WEB-01 files contain only requests; add a separate grant collection.
            self.data.setdefault('grants', [])
            self._validate_loaded()
            for item in self.data['requests']:
                if item['status'] in ('approved','queued','fetching'):
                    item['status'] = 'expired'
                    item['result'] = None
            for grant in self.data['grants']:
                if grant['status'] in ('active','paused'):
                    grant['status'] = 'expired'
            self._save()
        else:
            self.data = {'schema':'local-ai-web-permissions-1', 'mode':'offline',
                         'requests':[], 'grants':[]}
            self._save()

    def _validate_loaded(self):
        valid_status={'pending','approved','queued','denied','fetching','fetched','error','expired'}
        if not isinstance(self.data['grants'],list):
            raise ValueError('Invalid web grant collection.')
        for item in self.data['requests']:
            if not isinstance(item,dict) or item.get('status') not in valid_status or \
                    not isinstance(item.get('id'),str) or not isinstance(item.get('conversation_id'),str):
                raise ValueError('Invalid web permission record; nothing changed.')
        for g in self.data['grants']:
            if not isinstance(g,dict) or g.get('status') not in ('active','paused','expired','revoked') or \
                    g.get('scope') not in ('conversation','domain','broad') or not isinstance(g.get('id'),str) or \
                    not isinstance(g.get('expires_at'),str) or type(g.get('quota')) is not int or \
                    type(g.get('used')) is not int or not 0 <= g['used'] <= g['quota'] <= 50 or \
                    not isinstance(g.get('kinds'),list) or not set(g['kinds']).issubset({'page','search'}):
                raise ValueError('Invalid web grant record; nothing changed.')

    def _save(self):
        _atomic_json(self.file,self.data)

    def _audit(self,event,status,conversation_id):
        if self.audit is not None:
            self.audit.record(event,status=status,conversation_id=conversation_id)

    @property
    def mode(self):
        return self.data['mode']

    def _expire_locked(self):
        now=timestamp()
        changed=False
        for g in self.data['grants']:
            if g['status'] in ('active','paused') and now >= dt.datetime.fromisoformat(g['expires_at']):
                g['status']='expired'; changed=True
        for r in self.data['requests']:
            if r['status']=='approved' and r.get('expires_at') and now >= dt.datetime.fromisoformat(r['expires_at']):
                r['status']='expired'; changed=True
        if changed: self._save()

    def set_mode(self,mode):
        if mode not in ('offline','web') or not isinstance(mode,str):
            raise ValueError('Mode must be offline or web.')
        with self.lock:
            if mode=='offline':
                for r in self.data['requests']:
                    if r['status'] in ('pending','approved','queued','fetching'):
                        r['status']='expired'; r['result']=None
                for g in self.data['grants']:
                    if g['status'] in ('active','paused'):g['status']='revoked'
            self.data['mode']=mode
            self._save()
            self._audit('web.mode','ok',None)

    def list_requests(self,*,conversation_id=None):
        with self.lock:
            self._expire_locked()
            rows=self.data['requests']
            if conversation_id is not None:
                rows=[r for r in rows if r['conversation_id']==conversation_id]
            return [dict(r) for r in rows[-100:]][::-1]

    def list_grants(self,*,conversation_id=None):
        with self.lock:
            self._expire_locked()
            rows=self.data['grants']
            if conversation_id is not None:
                rows=[g for g in rows if g['scope']=='broad' or g['conversation_id']==conversation_id]
            return [dict(g) for g in rows[-60:]][::-1]

    def _get(self,request_id,conversation_id):
        if not isinstance(request_id,str) or not isinstance(conversation_id,str):
            raise ValueError('Invalid web request identifier.')
        for row in self.data['requests']:
            if row['id']==request_id and row['conversation_id']==conversation_id:
                return row
        raise ValueError('Unknown or wrong-conversation web request.')

    def _grant_get(self,grant_id,conversation_id):
        if not isinstance(grant_id,str) or not isinstance(conversation_id,str):
            raise ValueError('Invalid web grant identifier.')
        for g in self.data['grants']:
            if g['id']==grant_id and (g['scope']=='broad' or g['conversation_id']==conversation_id):
                return g
        raise ValueError('Unknown or wrong-conversation web grant.')

    def _match_grant(self,request):
        self._expire_locked()
        matches=[]
        for g in self.data['grants']:
            if g['status']!='active' or g['used']>=g['quota'] or request['kind'] not in g['kinds']:
                continue
            if g['scope']!='broad' and g['conversation_id']!=request['conversation_id']:
                continue
            if g['scope']=='domain' and (request['kind']!='page' or urlsplit(request['url']).hostname!=g['domain']):
                continue
            matches.append(g)
        # Never spend a wider grant when an eligible narrower grant exists.
        rank={'domain':0,'conversation':1,'broad':2}
        return min(matches,key=lambda g:(rank[g['scope']],g['created_at'])) if matches else None

    def _authorize(self,row,grant=None):
        now=timestamp()
        row['status']='approved'
        row['approved_at']=now.isoformat(timespec='seconds')
        row['grant_id']=grant['id'] if grant else None
        expiry=now+dt.timedelta(minutes=10)
        if grant:
            grant['used']+=1
            expiry=min(expiry,dt.datetime.fromisoformat(grant['expires_at']))
        row['expires_at']=expiry.isoformat(timespec='seconds')

    def request(self,url,*,conversation_id,purpose='',job_id=None):
        return self._request('page',url,conversation_id=conversation_id,purpose=purpose,job_id=job_id)

    def request_search(self,query,*,conversation_id,purpose='',job_id=None):
        if not isinstance(query,str) or not 1 <= len(query.strip()) <= 300 or any(ord(c)<32 for c in query):
            raise ValueError('Search query must contain 1-300 printable characters.')
        return self._request('search',query.strip(),conversation_id=conversation_id,purpose=purpose,job_id=job_id)

    def _request(self,kind,target,*,conversation_id,purpose='',job_id=None):
        if kind=='page': target=canonical_url(target)
        if not isinstance(conversation_id,str) or not conversation_id or len(conversation_id)>120:
            raise ValueError('A conversation is required for web requests.')
        if not isinstance(purpose,str) or len(purpose)>300:
            raise ValueError('Purpose is limited to 300 characters.')
        if job_id is not None and (not isinstance(job_id,str) or not 1<=len(job_id)<=80):
            raise ValueError('Invalid idempotency key.')
        with self.lock:
            if self.mode!='web':
                self._audit('web.request','denied',conversation_id)
                raise PermissionError('Offline mode denies all web requests.')
            if job_id:
                matches=[r for r in self.data['requests'] if r.get('job_id')==job_id]
                if matches:
                    old=matches[0]
                    if old.get('kind','page')!=kind or (old.get('url') if kind=='page' else old.get('query'))!=target or old['conversation_id']!=conversation_id:
                        raise ValueError('Idempotency key cannot be reused for a different request.')
                    return dict(old)
            if len(self.data['requests'])>=MAX_ENTRIES:
                self.data['requests']=[r for r in self.data['requests'] if r['status']=='pending'][-100:] + \
                    [r for r in self.data['requests'] if r['status']!='pending'][-100:]
                if len(self.data['requests'])>=MAX_ENTRIES:
                    raise ValueError('Web approval inbox is full.')
            now=timestamp().isoformat(timespec='seconds')
            row={'id':secrets.token_hex(12),'job_id':job_id or secrets.token_hex(12),
                 'conversation_id':conversation_id,'kind':kind,
                 'url':target if kind=='page' else None,
                 'query':target if kind=='search' else None,
                 'purpose':purpose,'status':'pending','created_at':now,
                 'approved_at':None,'expires_at':None,'grant_id':None,'result':None}
            if kind=='search':
                row['search_provider']=dict(self.provider_settings)
            grant=self._match_grant(row)
            if grant:self._authorize(row,grant)
            self.data['requests'].append(row)
            self._save()
            self._audit('web.auto_approve' if grant else 'web.request','ok' if grant else 'pending',conversation_id)
            return dict(row)

    def decide(self,request_id,*,conversation_id,approve):
        rows=self.decide_batch([request_id],conversation_id=conversation_id,approve=approve)
        return rows[0]

    def decide_batch(self,request_ids,*,conversation_id,approve):
        if type(approve) is not bool:
            raise ValueError('Approval decision must be true or false.')
        if not isinstance(request_ids,list) or not 1<=len(request_ids)<=MAX_BATCH or \
                len(set(map(str,request_ids)))!=len(request_ids) or not all(isinstance(x,str) for x in request_ids):
            raise ValueError('Select 1-50 distinct web requests.')
        with self.lock:
            if self.mode!='web':raise PermissionError('Web Access is disabled.')
            rows=[self._get(ident,conversation_id) for ident in request_ids]
            if any(row['status']!='pending' for row in rows):
                raise PermissionError('Batch contains a non-pending request; no decisions were applied.')
            for row in rows:
                if approve:self._authorize(row)
                else:row['status']='denied'
            self._save()
            self._audit('web.approve' if approve else 'web.deny','ok',conversation_id) if len(rows)==1 else \
                self._audit('web.batch_approve' if approve else 'web.batch_deny','ok',conversation_id)
            return [dict(row) for row in rows]

    def create_grant(self,*,conversation_id,scope,kinds,minutes=30,quota=10,domain=None):
        if self.mode!='web':raise PermissionError('Web Access is disabled.')
        if not isinstance(conversation_id,str) or not conversation_id or len(conversation_id)>120:
            raise ValueError('Grant requires an originating conversation.')
        if scope not in ('conversation','domain','broad'):
            raise ValueError('Unknown web grant scope.')
        if not isinstance(kinds,list) or not kinds or len(set(kinds))!=len(kinds) or not set(kinds).issubset({'page','search'}):
            raise ValueError('Allowed operations must be page, search, or both.')
        if type(minutes) is not int or not 5<=minutes<=120 or type(quota) is not int or not 1<=quota<=50:
            raise ValueError('Grant limits: 5-120 minutes and 1-50 requests.')
        if scope=='domain':
            if not isinstance(domain,str) or not domain or len(domain)>253 or '/' in domain or ':' in domain or '@' in domain:
                raise ValueError('Enter one public domain hostname, without scheme or path.')
            normalized=canonical_url('https://'+domain+'/')
            domain=urlsplit(normalized).hostname
            if kinds!=['page']:
                raise ValueError('Domain-only grants authorize page requests, not searches.')
        else:
            if domain not in (None,''):
                raise ValueError('Domain is only valid with a domain-scoped grant.')
            domain=None
        with self.lock:
            self._expire_locked()
            if self.mode!='web':raise PermissionError('Web Access is disabled.')
            for g in self.data['grants']:
                if (g['status'] in ('active','paused') and g['used']<g['quota'] and
                        g['scope']==scope and g['conversation_id']==conversation_id and
                        g['domain']==domain and g['kinds']==kinds):
                    raise ValueError('An equivalent unused or partially used grant already exists; review or revoke it first.')
            if len(self.data['grants'])>=MAX_GRANTS:
                self.data['grants']=[g for g in self.data['grants'] if g['status'] in ('active','paused')][-40:] + \
                    [g for g in self.data['grants'] if g['status'] not in ('active','paused')][-10:]
                if len(self.data['grants'])>=MAX_GRANTS:raise ValueError('Web grant inbox is full.')
            now=timestamp()
            grant={'id':secrets.token_hex(12),'status':'active','scope':scope,
                   'conversation_id':conversation_id, 'domain':domain,'kinds':kinds,
                   'quota':quota,'used':0,'created_at':now.isoformat(timespec='seconds'),
                   'expires_at':(now+dt.timedelta(minutes=minutes)).isoformat(timespec='seconds')}
            self.data['grants'].append(grant)
            self._save()
            self._audit('web.grant','ok',conversation_id)
            return dict(grant)

    def grant_action(self,grant_id,*,conversation_id,action):
        if action not in ('pause','resume','revoke'):
            raise ValueError('Grant action must be pause, resume or revoke.')
        with self.lock:
            grant=self._grant_get(grant_id,conversation_id)
            self._expire_locked()
            if action=='pause' and grant['status']=='active':grant['status']='paused'
            elif action=='resume' and grant['status']=='paused' and self.mode=='web':grant['status']='active'
            elif action=='revoke' and grant['status'] in ('active','paused'):grant['status']='revoked'
            else:raise PermissionError('Grant cannot perform that transition.')
            if action in ('pause','revoke'):
                for row in self.data['requests']:
                    if row.get('grant_id')!=grant_id:continue
                    # Pause cancels authorization for in-flight requests. A future
                    # resume cannot revive the result of a fetch paused mid-run.
                    if row['status'] in ('queued','fetching') or (action=='revoke' and row['status']=='approved'):
                        row['status']='expired';row['result']=None
            self._save()
            self._audit('web.grant_'+action,'ok',conversation_id)
            return dict(grant)

    def search_configuration(self):
        with self.lock:
            return dict(self.provider_settings, available=SEARCH_PROVIDERS)

    def configure_search(self, provider, endpoint=None):
        # Host GUI action only. Existing approved requests retain their original provider.
        setting=validated_provider(provider,endpoint)
        with self.lock:
            self.provider_settings=setting
            _atomic_json(self.provider_file,setting)
            self._audit('web.search_provider','ok',None)
            return self.search_configuration()

    def reserve_fetch(self,request_id,*,conversation_id):
        with self.lock:
            self._expire_locked()
            row=self._get(request_id,conversation_id)
            if self.mode!='web' or row['status']!='approved':
                raise PermissionError('Web request is not approved for queued retrieval.')
            if timestamp()>=dt.datetime.fromisoformat(row['expires_at']):
                row['status']='expired';self._save()
                raise PermissionError('Web approval expired.')
            if row.get('grant_id'):
                grant=self._grant_get(row['grant_id'],conversation_id)
                if grant['status']!='active':raise PermissionError('Web grant is not active.')
            row['status']='queued'
            self._save()
            return dict(row)

    def cancel_fetch(self,request_id,*,conversation_id):
        with self.lock:
            row=self._get(request_id,conversation_id)
            if row['status'] not in ('queued','fetching'):
                raise PermissionError('Only queued or running retrievals can be cancelled.')
            row['status']='expired';row['result']=None
            self._save()
            self._audit('web.fetch_cancel','ok',conversation_id)
            return dict(row)

    def fetch(self,request_id,*,conversation_id,from_queue=False):
        with self.lock:
            self._expire_locked()
            row=self._get(request_id,conversation_id)
            if row.get('kind','page') not in ('page','search'):
                raise PermissionError('Unknown web operation.')
            if self.mode!='web' or row['status']!=('queued' if from_queue else 'approved'):
                raise PermissionError('URL has not been approved for this conversation.')
            if not row.get('expires_at') or timestamp()>=dt.datetime.fromisoformat(row['expires_at']):
                row['status']='expired';self._save();raise PermissionError('Web approval expired.')
            grant_id=row.get('grant_id')
            if grant_id:
                grant=self._grant_get(grant_id,conversation_id)
                if grant['status']!='active':
                    raise PermissionError('The issuing future-access grant is paused or revoked.')
            row['status']='fetching'
            self._save()
            target=row['url'] if row['kind']=='page' else row['query']
            kind=row['kind']
        try:
            started = time.monotonic()
            if not self._slots.acquire(blocking=False):
                raise RuntimeError('Two web requests are already in progress. Retry after they finish.')
            try:
                if self._isolation_check:self._isolation_check()
                result=self._fetcher(target) if kind=='page' else (self._searcher(target) if self._searcher is not None else
                        search_with_provider(target, **row.get('search_provider',{'provider':'bing_rss','endpoint':None})))
            finally:
                self._slots.release()
            if not isinstance(result,dict) or (kind=='page' and result.get('url')!=target):
                raise ValueError('Fetcher returned a mismatched source.')
            safe={key:result[key] for key in ('url','mime','status','sha256','bytes','preview')}
            if not isinstance(safe['preview'],str):raise ValueError('Invalid page text.')
            safe['preview']=safe['preview'][:MAX_TEXT]
            if kind=='search':
                if not isinstance(result.get('hits'),list):raise ValueError('Search results are missing.')
                safe['hits']=[{k:hit[k] for k in ('index','title','url','snippet')} for hit in result['hits'][:8]]
                for hit in safe['hits']:
                    hit['url']=canonical_url(hit['url'])
                    hit['title']=str(hit['title'])[:170]
                    hit['snippet']=str(hit['snippet'])[:380]
                safe['provider']=str(result.get('provider','Public search'))[:50]
            safe['source_id']='web:'+row['id']
            safe['retrieved_at_utc']=timestamp().isoformat(timespec='seconds')
            safe['untrusted']=True
            safe['cache_status']='first_fetch'
            safe['elapsed_ms']=round((time.monotonic()-started)*1000)
        except Exception as exc:
            with self.lock:
                if self.mode!='web' or row['status']!='fetching':
                    row['status']='expired';row['result']=None
                else:
                    row['status']='error';row['result']={'error':str(exc)[:200]}
                self._save()
            self._audit('web.fetch','error',conversation_id)
            return dict(row)
        with self.lock:
            current_grant=next((g for g in self.data['grants'] if g['id']==grant_id),None) if grant_id else None
            if self.mode!='web' or row['status']!='fetching' or (current_grant and
                    (current_grant['status']!='active' or timestamp()>=dt.datetime.fromisoformat(current_grant['expires_at']))):
                row['status']='expired';row['result']=None
                self._save();self._audit('web.fetch','denied',conversation_id)
                return dict(row)
            row['status']='fetched';row['result']=safe
            self._save();self._audit('web.fetch','ok',conversation_id)
            return dict(row)

    def read_result(self,request_id,*,conversation_id):
        """Read a completed result from the matching conversation, without egress."""
        with self.lock:
            if self.mode!='web':raise PermissionError('Offline mode denies web evidence retrieval.')
            row=self._get(request_id,conversation_id)
            if row['status']!='fetched' or not isinstance(row.get('result'),dict):
                raise PermissionError('Web evidence is unavailable until an approved retrieval finishes.')
            return {'request_id':row['id'],'kind':row.get('kind','page'),
                    'result':dict(row['result'], cache_status='saved_result')}

    def follow_result(self,search_request_id,index,*,conversation_id,purpose='Follow selected search result'):
        """Following a link only creates a new page proposal; it never fetches."""
        if type(index) is not int or not 1 <= index <= 8:
            raise ValueError('Select a search result index from 1 to 8.')
        with self.lock:
            if self.mode!='web':raise PermissionError('Offline mode denies link following.')
            parent=self._get(search_request_id,conversation_id)
            if parent.get('kind')!='search' or parent['status']!='fetched':
                raise PermissionError('Search evidence must be retrieved before following a result.')
            hits=parent.get('result',{}).get('hits',[])
            hit=next((h for h in hits if h['index']==index),None)
            if hit is None:raise ValueError('Unknown search result index.')
            return self.request(hit['url'],conversation_id=conversation_id,purpose=purpose[:300],
                                job_id='follow-'+search_request_id+'-'+str(index))

    def revoke(self,request_id,*,conversation_id):
        with self.lock:
            row=self._get(request_id,conversation_id)
            if row['status'] in ('pending','approved'):
                row['status']='expired';self._save()
                self._audit('web.revoke','ok',conversation_id)
            return dict(row)
