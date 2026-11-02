"""Local credential, app-server, and desktop IPC adapters. Never log raw responses."""
import base64
import hashlib
import json
import os
import pathlib
import queue
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import fcntl
import sqlite3
import unicodedata
import datetime
import math


HOME = pathlib.Path.home() / '.codex'
_cleanup_pending = []
_cleanup_lock = threading.Lock()


def cleanup_query_temp(temp, delays=(0, .05, .15, .3)):
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            temp.cleanup()
            return True
        except OSError:
            pass
    return False


def retry_query_cleanup():
    with _cleanup_lock:
        pending = _cleanup_pending[:3]
        del _cleanup_pending[:3]
    for temp in pending:
        if not cleanup_query_temp(temp, (0,)):
            with _cleanup_lock:
                _cleanup_pending.append(temp)


def cleanup_pending_count():
    with _cleanup_lock:
        return len(_cleanup_pending)


def identity_claims(auth):
    """Local display metadata only; never use unverified claims as authorization."""
    part = auth['tokens']['id_token'].split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


def account_profile(auth):
    try:
        claims = identity_claims(auth)
    except (KeyError, ValueError, IndexError, TypeError):
        return {}
    if not isinstance(claims, dict):
        return {}
    def clean(value):
        if not isinstance(value, str):
            return ''
        return ' '.join(''.join(c for c in value if not unicodedata.category(c).startswith('C')).split())[:160]
    profile = claims.get('https://api.openai.com/profile') or {}
    if not isinstance(profile, dict):
        profile = {}
    name = clean(claims.get('name')) or clean(profile.get('name')) or clean(claims.get('nickname'))
    email = clean(claims.get('email')) or clean(profile.get('email'))
    return dict(alias=name or email or '未命名账号', email=email)


def credential_revision(auth):
    """Compare local credential freshness without refreshing or disclosing tokens."""
    revisions = []
    refresh = auth.get('last_refresh')
    if isinstance(refresh, str):
        try:
            value = datetime.datetime.fromisoformat(refresh.replace('Z', '+00:00'))
            if value.tzinfo:
                revisions.append(value.timestamp())
        except ValueError:
            pass
    try:
        part = auth['tokens']['access_token'].split('.')[1]
        issued = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('iat')
        if isinstance(issued, (int, float)) and not isinstance(issued, bool) and math.isfinite(issued):
            revisions.append(issued)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        pass
    return max(revisions) if revisions else None


def identity(auth):
    tokens = auth.get('tokens') or {}
    if not tokens.get('access_token') or not tokens.get('account_id'):
        raise ValueError('请选择 ChatGPT 登录生成的 auth.json')
    try:
        claims = identity_claims(auth)
        subject = claims['sub']
    except (KeyError, ValueError, IndexError):
        raise ValueError('登录文件缺少有效账号身份')
    return hashlib.sha256((subject + ':' + tokens['account_id']).encode()).hexdigest()[:24]


def read_auth(path=HOME / 'auth.json'):
    raw = pathlib.Path(path).read_bytes()
    auth = json.loads(raw)
    return auth, identity(auth), hashlib.sha256(raw).hexdigest()


class CredentialStoreError(RuntimeError):
    pass


class MissingCredentials(CredentialStoreError):
    pass


class Vault:
    def __init__(self, executable):
        self.executable = str(pathlib.Path(executable).with_name('CodexQuotaKeychain'))
        self.blocked_until = 0
        self.needs_attention = False

    def clear_backoff(self):
        self.blocked_until = 0

    def run(self, operation, key, data=None):
        if time.monotonic() < self.blocked_until:
            raise CredentialStoreError('钥匙串暂不可用；解锁后点击刷新，后台不会弹出密码窗口')
        try:
            p = subprocess.run([self.executable, operation, key], input=data,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        except subprocess.TimeoutExpired:
            self.needs_attention = True
            self.blocked_until = time.monotonic()+30
            raise CredentialStoreError('钥匙串响应超时，已暂停重试；稍后点击刷新') from None
        except OSError:
            self.needs_attention = True
            raise CredentialStoreError('凭据助手无法启动，请重新安装小组件') from None
        if p.returncode == 10:
            raise MissingCredentials('尚未载入此账号的登录资料，请选择已有登录文件')
        if p.returncode == 11:
            self.needs_attention = True
            self.blocked_until = time.monotonic()+30
            raise CredentialStoreError('钥匙串暂不可用；解锁后点击刷新，后台不会弹出密码窗口')
        if p.returncode:
            self.needs_attention = True
            self.blocked_until = time.monotonic()+30
            raise CredentialStoreError('钥匙串操作未完成，已暂停重试；请检查钥匙串状态')
        self.needs_attention = False
        return p.stdout

    def put(self, key, auth):
        self.run('--vault-put', key, json.dumps(auth).encode())

    def get(self, key):
        try:
            auth = json.loads(self.run('--vault-get', key))
        except (ValueError, TypeError):
            raise CredentialStoreError('钥匙串中的登录资料格式无效，请重新载入账号') from None
        if identity(auth) != key:
            raise RuntimeError('钥匙串账号身份不匹配')
        return auth


def find_codex():
    candidates = [os.environ.get('CODEX_QUOTA_CLI'),
        '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
        '/Applications/Codex.app/Contents/Resources/codex', shutil.which('codex')]
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError('找不到 Codex CLI，请先安装或打开 ChatGPT 桌面端')


class RPC:
    def __init__(self, auth):
        retry_query_cleanup()
        self.closed = False
        self.temp = tempfile.TemporaryDirectory(prefix='codex-quota-')
        os.chmod(self.temp.name, 0o700)
        env = os.environ.copy()
        for name in list(env):
            if name.startswith(('OPENAI_', 'CODEX_')):
                env.pop(name)
        env.update(CODEX_HOME=self.temp.name, RUST_LOG='off')
        self.p = subprocess.Popen([find_codex(), 'app-server', '--listen', 'stdio://',
            '-c', 'cli_auth_credentials_store="ephemeral"', '-c', 'analytics.enabled=false'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=self.temp.name)
        self.messages = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'codex_quota_widget', 'title': 'QuotaHandoff', 'version': '1.10.1'},
                                     'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            t = auth['tokens']
            self.call('account/login/start', {'type': 'chatgptAuthTokens', 'accessToken': t['access_token'],
                                            'chatgptAccountId': t['account_id']})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.p.stdout:
                self.messages.put(json.loads(line))
        except Exception:
            pass
        finally:
            self.messages.put(None)

    def send(self, value):
        self.p.stdin.write(json.dumps(value).encode() + b'\n')
        self.p.stdin.flush()

    def call(self, method, params=None, timeout=25):
        self.counter += 1
        rid = self.counter
        self.send({'id': rid, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            try:
                m = self.messages.get(timeout=max(0.01, deadline-time.monotonic()))
            except queue.Empty:
                raise RuntimeError('额度服务响应超时')
            if m is None:
                raise RuntimeError('额度查询进程已退出')
            if m.get('method') == 'account/chatgptAuthTokens/refresh' and 'id' in m:
                self.send({'id': m['id'], 'error': {'code': -32000, 'message': 'Reauthentication required'}})
                raise RuntimeError('账号登录已过期，请手动重新登录后刷新')
            if m.get('id') == rid and 'method' not in m:
                if 'error' in m:
                    # Do not expose server diagnostics that could echo sensitive input.
                    raise RuntimeError('账号查询被拒绝，请检查网络或重新登录')
                return m.get('result', {})

    def close(self):
        if self.closed:
            return
        if self.p.poll() is None:
            self.p.stdin.close()
            try:
                self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.p.terminate()
                try:
                    self.p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.p.kill()
                    self.p.wait()
        self.p.stdout.close()
        if not self.closed:
            self.closed = True
            if not cleanup_query_temp(self.temp):
                # Cleanup must not erase a successful quota read or redemption acknowledgement.
                # Keep the private directory object alive and retry only our own failed directories.
                with _cleanup_lock:
                    _cleanup_pending.append(self.temp)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def usage(auth):
    with RPC(auth) as rpc:
        result = rpc.call('account/rateLimits/read')
    if result.get('accountId') and result['accountId'] != auth['tokens']['account_id']:
        raise RuntimeError('额度响应的账号不匹配')
    return result


def latest_desktop_turn(state):
    history = state.get('turnHistory')
    if history is None:
        return (state.get('turns') or [{}])[-1]
    if history.get('kind') != 'canonical':
        return {}  # Unknown history formats must not fall back to possibly stale legacy data.
    canonical = history.get('history') or {}
    tails = [island for island in canonical.get('islands', [])
             if (island.get('newerBoundary') or {}).get('status') == 'exhausted']
    if len(tails) != 1 or not tails[0].get('entries'):
        return {}
    key = tails[0]['entries'][-1].get('value')
    return canonical.get('entitiesByKey', {}).get(key) or {}


class Desktop:
    """Read-only snapshots plus narrowly scoped continuation through existing owner."""
    def __init__(self):
        path = HOME / 'ipc/ipc.sock'
        if path.stat().st_uid != os.getuid():
            raise RuntimeError('桌面连接所属用户不匹配')
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(2)
        self.s.connect(str(path))
        self.followed = set()
        self.states = {}
        self.owners = {}
        self.unresolved = 0
        self.issues = []
        self.metadata = {}
        self.call('initialize', {'clientType': 'codex-quota-widget'}, 0)

    def send(self, m):
        b = json.dumps(m).encode()
        self.s.sendall(struct.pack('<I', len(b)) + b)

    def take(self, n):
        b = b''
        while len(b) < n:
            part = self.s.recv(n-len(b))
            if not part:
                raise RuntimeError('桌面连接已关闭')
            b += part
        return b

    def receive(self):
        n = struct.unpack('<I', self.take(4))[0]
        if not 0 < n < 32_000_000:
            raise RuntimeError('桌面协议帧无效')
        m = json.loads(self.take(n))
        if m.get('type') == 'client-discovery-request':
            self.send({'type': 'client-discovery-response', 'requestId': m.get('requestId'), 'response': {'canHandle': False}})
        if m.get('method') == 'thread-stream-state-changed':
            if m.get('version') != 11:
                raise RuntimeError('桌面协议版本变化，需要更新小组件')
            p = m['params']
            tid = p['conversationId']
            if m.get('sourceClientId') == self.owners.get(tid):
                ch = p['change']
                if ch['type'] == 'snapshot':
                    st = ch['conversationState']
                    rt = st.get('threadRuntimeStatus') or {}
                    last = latest_desktop_turn(st)
                    self.states[tid] = dict(id=tid, owner=self.owners[tid], revision=ch['revision'],
                        title=st.get('title') or st.get('fallbackSearchTitle') or '未命名任务',
                        status=rt.get('type'), waiting=bool(set(rt.get('activeFlags') or []) & {'waitingOnApproval', 'waitingOnUserInput'}),
                        child=bool(st.get('parentThreadId') or st.get('sideConversation')),
                        goal=(st.get('threadGoal') or {}).get('status'), turn=last.get('turnId'),
                        turnStatus=last.get('status'), message=st.get('latestTurnStartMessageId'))
                elif ch['type'] == 'patches':
                    # A fresh snapshot avoids incorrectly interpreting a changed patch schema.
                    self.states.pop(tid, None)
                    self.follow(tid, False)
                    self.follow(tid, True)
        return m

    def call(self, method, params, version, owner=None, timeout=5):
        rid = str(uuid.uuid4())
        m = dict(type='request', requestId=rid, method=method, params=params, version=version, timeoutMs=max(500, int((timeout-1)*1000)))
        if owner:
            m['targetClientId'] = owner
        self.send(m)
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                msg = self.receive()
            except socket.timeout:
                continue
            if msg.get('type') == 'response' and msg.get('requestId') == rid:
                return msg
        raise RuntimeError('桌面请求超时')

    def follow(self, tid, value):
        self.send(dict(type='broadcast', method='thread-stream-following-changed', version=1,
                       params=dict(hostId='local', conversationId=tid, following=value)))
        if value:
            self.followed.add(tid)
        else:
            self.followed.discard(tid)

    def snapshot(self, strict=True):
        # Internal approval-review agents hold writer locks but have no desktop tab owner.
        # Their lifecycle belongs to the parent turn; never treat them as independent chats.
        sources = {}
        dbpath = HOME / 'state_5.sqlite'
        if dbpath.exists():
            try:
                db = sqlite3.connect('file:' + str(dbpath) + '?mode=ro', uri=True, timeout=1)
                for lock in (HOME / 'thread-writer-locks').glob('*.lock'):
                    row = db.execute('SELECT source,title FROM threads WHERE id=?', (lock.stem,)).fetchone()
                    if row:
                        sources[lock.stem] = row[0]
                        self.metadata[lock.stem] = str(row[1] or '未命名聊天')[:160]
                db.close()
            except sqlite3.Error:
                pass  # Owner discovery remains the fallback; unknowns block mutations.
        for p in (HOME / 'thread-writer-locks').glob('*.lock'):
            try:
                uuid.UUID(p.stem)
            except ValueError:
                continue
            try:
                source = json.loads(sources.get(p.stem, 'null'))
                if isinstance(source, dict) and source.get('subagent', {}).get('other') == 'guardian':
                    continue
            except (ValueError, AttributeError):
                pass
            try:
                with p.open() as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                        continue  # Persisted lock file with no live writer.
                    except BlockingIOError:
                        pass
                result = self.call('thread-owner-discovery', {'hostId': 'local', 'conversationId': p.stem}, 1, timeout=4)
            except FileNotFoundError:
                continue
            except RuntimeError:
                self.add_issue(p.stem, 'owner_timeout', '桌面端未及时返回所属聊天')
                continue
            if result.get('resultType') == 'success' and result.get('handledByClientId'):
                self.owners[p.stem] = result['handledByClientId']
                self.follow(p.stem, True)
            else:
                self.add_issue(p.stem, 'owner_missing', '未找到管理此聊天的桌面窗口')
        deadline = time.monotonic()+5
        while time.monotonic() < deadline and set(self.owners) - set(self.states):
            try:
                self.receive()
            except socket.timeout:
                continue
        for tid in set(self.owners) - set(self.states):
            self.add_issue(tid, 'snapshot_missing', '已找到聊天，但尚未收到运行状态')
        for x in self.states.values():
            if not x['child'] and x['status'] == 'active' and not x.get('turn'):
                self.metadata[x['id']] = x['title']
                self.add_issue(x['id'], 'turn_missing', '运行中的回合尚未确认')
        if strict and self.unresolved:
            raise RuntimeError('部分桌面任务状态尚未确认')
        return [x for x in self.states.values() if not x['child']]

    def add_issue(self, tid, code, reason):
        self.issues.append(dict(id=tid, title=self.metadata.get(tid, '未识别的后台记录'),
                                code=code, reason=reason, metadataFound=tid in self.metadata))
        self.unresolved = len(self.issues)

    def interrupt_chat(self, saved, live):
        if live.get('turn') != saved.get('turn') or live.get('status') != 'active' or live.get('waiting'):
            return False
        params = dict(conversationId=saved['id'], mode='system', expectedTurnId=saved['turn'])
        reply = self.call('thread-follower-interrupt-turn', params, 4, live['owner'], timeout=25)
        result = reply.get('result') or {}
        if reply.get('resultType') != 'success' or reply.get('handledByClientId') != live['owner'] or result.get('ok') is not True:
            raise RuntimeError('任务暂停请求尚未确认，未退出桌面端')
        interrupted = result.get('interruptedTurnId')
        if interrupted is None:
            return False  # Already stopped or a newer turn; never interrupt that newer turn.
        if interrupted != saved['turn']:
            raise RuntimeError('暂停返回的回合不匹配，未继续操作')
        return True

    def continue_chat(self, saved, live):
        p = {'conversationId': saved['id'], 'turnStart': {'request': {'threadId': saved['id'],
             'input': [{'type': 'text', 'text': '继续刚才因额度调度中断的任务。先核对已完成的操作，避免重复执行。', 'text_elements': []}],
             'clientUserMessageId': saved['message']}, 'context': {'inheritThreadSettings': True}}}
        r = self.call('thread-follower-start-turn', p, 2, live['owner'], timeout=25)
        if r.get('resultType') != 'success' or r.get('handledByClientId') != live['owner']:
            raise RuntimeError('续跑未确认，保留记录，不重复发送')
        turn = ((r.get('result') or {}).get('result') or {}).get('turn') or {}
        if not turn.get('id'):
            raise RuntimeError('续跑回合未确认，保留记录，不重复发送')

    def stop_chat_for_manual_switch(self, live):
        # Explicit manual switch stops the chat, including an active continuous goal.
        # Omitting expectedTurnId makes the desktop pause the goal before interrupting.
        reply = self.call('thread-follower-interrupt-turn',
                          dict(conversationId=live['id'], mode='system'), 4, live['owner'], timeout=25)
        result = reply.get('result') or {}
        if (reply.get('resultType') != 'success' or reply.get('handledByClientId') != live['owner']
                or result.get('ok') is not True or result.get('goalPauseError')):
            raise RuntimeError('聊天停止尚未确认，请在桌面端确认退出')

    def close(self):
        for tid in list(self.followed):
            try:
                self.follow(tid, False)
            except OSError:
                break
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def desktop_running():
    p = subprocess.run(['/usr/bin/pgrep', '-x', 'ChatGPT'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def close_desktop():
    # Normal quit only; no process killing or database edits.
    helper = os.environ.get('CODEX_QUOTA_HELPER')
    if not helper:
        raise RuntimeError('缺少本机应用退出组件')
    p = subprocess.run([helper, '--quit-desktop'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
    if p.returncode:
        raise RuntimeError('桌面端拒绝退出，已保留登录状态')
    deadline = time.monotonic()+30
    while desktop_running() and time.monotonic() < deadline:
        time.sleep(.25)
    if desktop_running():
        raise RuntimeError('桌面端尚未退出，取消切号')


def open_desktop(tid=None):
    args = ['/usr/bin/open', '-a', '/Applications/ChatGPT.app']
    if tid:
        args.append('codex://threads/' + str(uuid.UUID(tid)))
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def replace_auth(auth, expected_key, expected_hash):
    if desktop_running():
        raise RuntimeError('桌面端已被手动打开，取消自动切号')
    _, key, digest = read_auth()
    if key != expected_key or digest != expected_hash:
        raise RuntimeError('检测到用户修改登录文件，取消自动切号')
    fd, name = tempfile.mkstemp(prefix='.quota-auth-', dir=str(HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(auth, f)
            f.flush()
            os.fsync(f.fileno())
        # Recheck immediately before atomic replacement.
        if read_auth()[2] != expected_hash or desktop_running():
            raise RuntimeError('登录状态发生变化，取消自动切号')
        os.replace(name, HOME / 'auth.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)
