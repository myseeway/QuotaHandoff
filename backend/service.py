#!/usr/bin/env python3
"""Resident scheduler. JSON lines on stdio carry sanitized UI state only."""
import concurrent.futures
import json
import os
import pathlib
import queue
import sqlite3
import sys
import threading
import time
import uuid
import fcntl

from adapters import HOME, Vault, CredentialStoreError, MissingCredentials, Desktop, RPC, cleanup_pending_count, read_auth, usage, identity, account_profile, credential_revision, desktop_running, close_desktop, open_desktop, replace_auth
from policy import normalize, decide, resumable, usable, known
import polling


class UserControlChanged(RuntimeError):
    pass


class Store:
    def __init__(self, path):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        self.db = sqlite3.connect(str(path/'state.sqlite'))
        os.chmod(path/'state.sqlite', 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (at REAL, message TEXT)')
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))
        self.db.commit()

    def event(self, message):
        self.db.execute('INSERT INTO events VALUES (?,?)', (time.time(), message))
        self.db.execute('DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 100)')
        self.db.commit()

    def events(self):
        return [dict(at=r[0], message=r[1]) for r in self.db.execute('SELECT at,message FROM events ORDER BY rowid DESC LIMIT 8')]


class Engine:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.profiles = store.get('profiles', [])
        self.imported = store.get('imported', {})
        self.backup_paths = store.get('backup_paths', list(self.imported))
        store.put('backup_paths', self.backup_paths)
        if store.get('vault_generation') != 2:
            self.imported = {}  # Reimport only explicitly enrolled paths into the stable helper.
        self.accounts, self.tasks = [], []
        self.current, self.auth_hash = store.get('last_identity'), None
        self.identity_verified, self.identity_error = False, None
        self.backup_error = None
        self.publish = lambda: None
        self.manual_request = None
        self.mode = store.get('desired_mode', 'observe')
        self.auto_reset = store.get('auto_reset', False)
        self.pending = store.get('pending')
        if self.current is None and self.pending:
            self.current = self.pending.get('account')
        if self.mode not in ('observe', 'paused', 'auto') or (self.pending and (self.pending.get('phase') not in ('paused', 'redeeming') or self.pending.get('origin') == 'manual')):
            self.mode = 'observe'
        # Retire the installation-specific date override; retain it as migration history.
        if store.get('priority_reset'):
            store.put('legacy_priority_reset', store.get('priority_reset'))
            store.put('priority_reset', None)
        self.live_redemption = store.get('live_redemption')
        self.last_live_attempt = 0
        self.problem, self.desktop_error = None, None
        if self.pending and (self.pending.get('phase') not in ('paused', 'redeeming') or self.pending.get('origin') == 'manual'):
            self.problem = '上次调度在中途停止，已保留恢复记录；核对后再继续'
        self.plan = dict(action='unknown', reason='正在读取账号')
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.futures = {}
        self.desktop_future = None
        self.poll_records = store.get('poll_records', {})
        self.refresh_requested = set()
        self.last_desktop = 0
        self.last_identity = 0
        self.cancel = threading.Event()
        self.shutdown_requested = threading.Event()
        self.reset_disabled = threading.Event()
        if not self.auto_reset:
            self.reset_disabled.set()
        self.control_lock = threading.Lock()
        self.excluded_ids = set()
        self.desktop_checked_at = 0
        self.desktop_issues = []

    def signal(self, message):
        """Accept user cancellation even while the controller waits for an IPC reply."""
        name = message.get('command')
        if name == 'quit':
            self.shutdown_requested.set()
        if name in ('quit', 'clear_pending', 'import', 'switch_account', 'cancel_switch', 'cycle_mode') or (name == 'mode' and message.get('value') != 'auto'):
            self.cancel.set()
        if name == 'rotation' and message.get('value') is not True:
            self.cancel.set()
        if name == 'auto_reset' and message.get('value') is not True:
            self.reset_disabled.set()
        if name == 'exclude' and isinstance(message.get('id'), str):
            with self.control_lock:
                self.excluded_ids.add(message['id'])

    def excluded(self, task):
        with self.control_lock:
            return task['id'] in self.excluded_ids or task.get('phase') == 'excluded'

    def check_control(self):
        if self.cancel.is_set():
            raise UserControlChanged('用户已改变操作选择，保留当前恢复记录')

    def register(self, auth, key):
        if not any(p['key'] == key for p in self.profiles):
            enabled = len(self.profiles) < 2
            self.vault.put(key, auth)
            self.profiles.append(dict(key=key, rotationEnabled=enabled, **account_profile(auth)))
            if not enabled:
                self.store.event('已发现新账号并开始监控；开启该账号的自动轮换后参与调度')
        else:
            self.vault.put(key, auth)
            next(p for p in self.profiles if p['key'] == key).update(account_profile(auth))
        self.store.put('profiles', self.profiles)

    def migrate_profiles(self):
        # Existing installs also gain names, without importing stale backup credentials.
        for profile in self.profiles:
            profile.update(account_profile(self.vault.get(profile['key'])))
            profile.setdefault('rotationEnabled', True)
        self.store.put('profiles', self.profiles)
        self.store.put('vault_generation', 2)

    def import_file(self, path):
        path = pathlib.Path(path).expanduser().resolve()
        auth, key, digest = read_auth(path)
        self.register(auth, key)
        if path != (HOME/'auth.json').resolve() and str(path) not in self.backup_paths:
            self.backup_paths.append(str(path))
            self.store.put('backup_paths', self.backup_paths)
        self.imported[str(path)] = digest
        self.store.put('imported', self.imported)
        self.store.event('已载入账号登录文件')

    def sync_backups(self):
        """Watch only explicitly imported backup paths; never discover arbitrary files."""
        self.backup_error = None
        for name in self.backup_paths:
            path = pathlib.Path(name)
            if path.resolve() == (HOME/'auth.json').resolve():
                continue
            try:
                auth, key, digest = read_auth(path)
            except FileNotFoundError:
                continue  # A rename removes one backup by design.
            except (OSError, ValueError):
                self.backup_error = '账号备份暂时不可读，等待文件写入完成'
                continue
            if self.imported.get(str(path)) == digest:
                continue
            if key == self.current and not self.identity_verified:
                continue  # Revisit after the two-step rename completes.
            known_profile = any(p['key'] == key for p in self.profiles)
            if not known_profile:
                self.register(auth, key)
                self.store.event('已识别导入路径中的账号备份')
            elif key != self.current:
                try:
                    saved = self.vault.get(key)
                except MissingCredentials:
                    self.register(auth, key)
                    self.store.event('已从指定登录文件载入固定凭据助手')
                else:
                    incoming, existing = credential_revision(auth), credential_revision(saved)
                    if incoming is not None and existing is not None and incoming > existing:
                        self.register(auth, key)
                        self.store.event('已同步更新后的账号备份')
                # Old or unorderable backups never overwrite a later confirmed login.
            self.imported[str(path)] = digest
            self.store.put('imported', self.imported)

    def sync_identity(self):
        auth, key, digest = read_auth()
        if digest == self.auth_hash:
            self.identity_verified, self.identity_error = True, None
            return
        self.register(auth, key)
        if self.current and key != self.current:
            self.store.event('检测到手动切号，已跟随新账号')
            if self.pending or self.live_redemption or self.manual_request:
                self.store.put('abandoned', self.pending)
                self.store.put('abandoned_live_redemption', self.live_redemption)
                self.pending = None
                self.store.put('pending', None)
                self.live_redemption = None
                self.finish_manual('cancelled', '检测到其他登录操作，已取消原切换请求')
                self.store.put('live_redemption', None)
                self.mode = 'observe'
                self.store.put('desired_mode', self.mode)
                self.problem = '手动切号优先：旧恢复计划已取消'
            self.refresh()
        self.current, self.auth_hash = key, digest
        self.store.put('last_identity', key)
        self.identity_verified, self.identity_error = True, None

    def poll_identity(self):
        try:
            self.sync_identity()
            return True
        except Exception as error:
            # Replacing a login file can briefly expose no file or incomplete JSON.
            # Retain the last confirmed identity so a later change still cancels recovery.
            message = str(error) if isinstance(error, RuntimeError) else '登录文件暂时不可读，等待登录信息写入完成'
            if self.identity_verified:
                self.store.event('登录文件暂不可读，自动动作等待身份重新确认')
            self.identity_verified, self.identity_error = False, message
            return False

    def refresh(self):
        self.refresh_requested.update(p['key'] for p in self.profiles if p['key'] not in self.futures)

    def poll_context(self, now):
        current = next((a for a in self.accounts if a['key'] == self.current), {})
        available = any(a.get('rotationEnabled', True) and usable(a, now) for a in self.accounts)
        waiting = bool(self.pending and self.pending.get('phase') == 'paused'
                       and not self.pending.get('redemption') and not available
                       and self.plan['action'] in ('wait', 'hold', 'unknown'))
        review = (self.mode == 'auto' and not waiting and
                  ((current.get('remaining') is not None and current['remaining'] <= 5) or (self.pending and available)))
        active = any(t.get('status') == 'active' for t in self.tasks)
        return waiting, review, active

    def next_query_at(self, account, now):
        waiting, review, active = self.poll_context(now)
        key = account['key']
        return polling.due_at(self.poll_records.get(key, {}), account, now,
            polling.interval(self.mode, account, self.current, active, waiting),
            requested=key in self.refresh_requested,
            review=review and account.get('rotationEnabled', True) and not known(account, now), waiting=waiting)

    def quota_failed(self, key, error, now):
        message = str(error) if isinstance(error, RuntimeError) else '额度暂时读取失败，将延长间隔后重试'
        previous = next((a for a in self.accounts if a['key'] == key), {})
        data = {k: previous[k] for k in ('checkedAt', 'windows', 'remaining', 'denied', 'cards', 'cardCount', 'cardsComplete') if k in previous}
        data.update(error=message, credentialError=isinstance(error, CredentialStoreError) and not isinstance(error, MissingCredentials))
        self.update_account(key, data)  # Retain the last observation, explicitly marked unusable.
        self.poll_records[key] = polling.failed(self.poll_records.get(key, {}), now)
        self.store.put('poll_records', self.poll_records)

    def poll_quotas(self, now):
        for key, future in list(self.futures.items()):
            if not future.done():
                continue
            try:
                self.update_account(key, future.result())
                self.poll_records[key] = dict(self.poll_records.get(key, {}), failures=0, retryAt=0, completedAt=now)
                self.store.put('poll_records', self.poll_records)
            except Exception as error:
                self.quota_failed(key, error, now)
            del self.futures[key]
        if self.manual_request:
            return  # A manual login action never waits for quota reads.
        for profile in self.profiles:
            key = profile['key']
            if key in self.futures or (self.mode == 'paused' and key not in self.refresh_requested):
                continue
            account = next((a for a in self.accounts if a['key'] == key), profile)
            if now < self.next_query_at(account, now):
                continue
            self.refresh_requested.discard(key)
            self.poll_records[key] = dict(self.poll_records.get(key, {}), attemptedAt=now)
            self.store.put('poll_records', self.poll_records)
            try:
                self.futures[key] = self.pool.submit(self.query, self.vault.get(key))
            except Exception as error:
                self.quota_failed(key, error, now)

    @staticmethod
    def query(auth):
        return normalize(usage(auth), time.time())

    @staticmethod
    def query_desktop():
        with Desktop() as desktop:
            tasks = desktop.snapshot(strict=False)
            return tasks, desktop.issues

    def record_desktop_issues(self, issues):
        previous = self.store.get('desktop_issues', [])
        if {(x['id'], x['code']) for x in previous} != {(x['id'], x['code']) for x in issues}:
            if issues:
                self.store.put('last_desktop_issue_report', dict(at=time.time(), issues=issues))
                for issue in issues:
                    self.store.event('任务待确认：' + issue['title'] + ' [' + issue['id'] + '] · ' + issue['reason'])
            elif previous:
                self.store.event('此前未确认的任务记录已恢复，桌面检查通过')
            self.store.put('desktop_issues', issues)
        self.desktop_issues = issues

    def cycle(self):
        now = time.time()
        if now-self.last_identity >= 2:
            self.last_identity = now
            self.poll_identity()
            try:
                self.sync_backups()
            except Exception:
                self.backup_error = '账号备份同步未完成，保留已确认的登录信息'
        self.poll_quotas(now)
        if self.mode == 'paused' and not self.manual_request:
            return
        if now-self.last_desktop >= 8 and self.desktop_future is None:
            self.last_desktop = now
            self.desktop_future = self.pool.submit(self.query_desktop)
        if self.desktop_future and self.desktop_future.done():
            try:
                self.tasks, issues = self.desktop_future.result()
                self.record_desktop_issues(issues)
                self.desktop_checked_at = time.time()
                self.desktop_error = f'{len(issues)} 项任务状态待确认，自动调度暂缓' if issues else None
            except Exception:
                self.desktop_checked_at = 0
                self.tasks = []
                self.desktop_error = '桌面端未连接或状态未确认'
            self.desktop_future = None
        self.plan = self.rotation_plan(now)

    def rotation_plan(self, now):
        if not self.identity_verified:
            return dict(action='unknown', reason='等待当前登录身份重新确认')
        current = next((p for p in self.profiles if p['key'] == self.current), {})
        if current.get('rotationEnabled', True) is False:
            return dict(action='watch', reason='当前账号仅监控；开启此账号的自动轮换后参与调度')
        eligible = [a for a in self.accounts if a.get('rotationEnabled', True)]
        return decide(eligible, self.current, now)

    def update_account(self, key, data):
        profile = next(p for p in self.profiles if p['key'] == key)
        self.accounts = [a for a in self.accounts if a['key'] != key] + [dict(profile, **data)]
        order = {p['key']: i for i, p in enumerate(self.profiles)}
        self.accounts.sort(key=lambda a: order[a['key']])

    def preflight(self):
        now = time.time()
        eligible = [a for a in self.accounts if a.get('rotationEnabled', True)]
        current_enabled = any(p['key'] == self.current and p.get('rotationEnabled', True) for p in self.profiles)
        accounts_ready = len(eligible) >= 2 and all(known(a, now) for a in eligible)
        desktop_ready = not self.desktop_error and 0 < self.desktop_checked_at and now-self.desktop_checked_at <= 30
        checks = [
            dict(label='当前登录身份已确认', ready=self.identity_verified),
            dict(label='轮换账号额度已确认', ready=accounts_ready),
            dict(label='当前账号已加入自动轮换', ready=current_enabled),
            dict(label='桌面任务状态已确认', ready=desktop_ready),
            dict(label='没有等待审批或输入的任务', ready=desktop_ready and not any(t.get('waiting') for t in self.tasks)),
            dict(label='没有待检查的恢复记录', ready=not self.pending),
        ]
        return dict(ready=all(c['ready'] for c in checks), checks=checks)

    def checkpoint(self, phase):
        self.pending['phase'] = phase
        self.store.put('pending', self.pending)
        self.publish()

    def pause_tasks(self):
        """Interrupt exact captured turns before requesting normal app termination."""
        self.checkpoint('interrupting')
        for saved in self.pending['tasks']:
            self.check_control()
            if self.excluded(saved):
                saved.update(phase='excluded', outcome='user_excluded')
                continue
            with Desktop() as desktop:
                live = next((t for t in desktop.snapshot() if t['id'] == saved['id']), None)
                if not live:
                    raise RuntimeError('暂停前无法确认原聊天，未退出桌面端')
                if live.get('turn') != saved['turn']:
                    raise RuntimeError('暂停前出现新回合，已让出控制，未退出桌面端')
                if live.get('waiting') or live.get('goal') in ('paused', 'blocked', 'complete', 'budgetLimited'):
                    raise RuntimeError('任务已暂停或等待用户操作，未退出桌面端')
                if live['status'] != 'active':
                    saved.update(phase='excluded', outcome='already_stopped')
                    continue
                if live.get('goal') == 'active':
                    raise RuntimeError('该聊天有持续运行目标，需要先暂停目标；未退出桌面端')
                saved['pauseRequestedAt'] = time.time()
                self.checkpoint('interrupting')
                self.check_control()
                if self.excluded(saved):
                    saved.update(phase='excluded', outcome='user_excluded')
                    continue
                if read_auth()[1] != self.current:
                    raise RuntimeError('暂停前账号已改变，停止原计划')
                acknowledged = desktop.interrupt_chat(saved, live)
                if not acknowledged:
                    raise RuntimeError('原回合已变化，暂停结果未确认；未退出桌面端')
                saved['pauseAcknowledged'] = True
                self.checkpoint('interrupting')
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                self.check_control()
                with Desktop() as desktop:
                    live = next((t for t in desktop.snapshot() if t['id'] == saved['id']), None)
                if not live or live.get('turn') != saved['turn']:
                    raise RuntimeError('等待暂停时聊天状态改变，未退出桌面端')
                if live['status'] == 'idle':
                    if live.get('turnStatus') == 'interrupted':
                        saved['pauseConfirmed'] = True
                    elif live.get('turnStatus') == 'completed':
                        saved.update(phase='excluded', outcome='completed_before_pause')
                    else:
                        raise RuntimeError('回合结束原因尚未确认，未退出桌面端')
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('任务尚未确认停止，未退出桌面端')
            self.checkpoint('interrupting')
        with Desktop() as desktop:
            latest = desktop.snapshot()
        if any(t['status'] == 'active' or t.get('waiting') for t in latest):
            raise RuntimeError('仍有运行任务或用户待办，未退出桌面端')
        self.checkpoint('stopped')

    def operation(self):
        if self.live_redemption:
            return dict(phase='reset_current', title='正在核验当前账号的重置卡兑换' if self.mode == 'auto' else '重置卡兑换记录待核对', completed=0, total=0, skipped=0)
        if not self.pending:
            return None
        phase = self.pending.get('phase')
        if self.pending.get('origin') == 'manual':
            return dict(phase=phase, title=(self.manual_request or {}).get('message', '手动切换已停止，操作记录已保留'),
                        total=0, completed=0, skipped=0)
        title = {'prepared': '准备暂停任务', 'interrupting': '正在逐项暂停运行回合',
                 'stopped': '已确认任务停止，准备退出', 'closing': '任务已停止，正在退出桌面端',
                 'paused': '任务已暂停，等待可用额度', 'switching': '正在切换账号',
                 'redeeming': '正在核验重置卡兑换', 'reopening': '正在重新打开桌面端',
                 'resuming': '正在核验并恢复原任务'}.get(phase, '恢复记录需要检查')
        if self.mode != 'auto' and not self.manual_request:
            title = '自动动作已停止，恢复记录已保留'
        tasks = self.pending.get('tasks', [])
        return dict(phase=phase, title=title, total=len(tasks),
                    completed=sum(t.get('phase') == 'done' for t in tasks),
                    skipped=sum(t.get('phase') == 'excluded' for t in tasks))

    def reset_current(self):
        """Reset an active account without closing chats; replay only one durable key."""
        if not self.auto_reset or self.reset_disabled.is_set() or time.time()-self.last_live_attempt < 60:
            return
        self.check_control()
        if read_auth()[1] != self.current:
            self.sync_identity()
            return
        entry = self.live_redemption
        if not entry:
            entry = dict(account=self.current, credit=self.plan['credit'], key=str(uuid.uuid4()))
            self.live_redemption = entry
            self.store.put('live_redemption', entry)
            self.publish()
        if entry['account'] != self.current:
            raise RuntimeError('重置卡兑换账号已改变，保留记录待检查')
        self.last_live_attempt = time.time()
        try:
            if not entry.get('confirmed'):
                with RPC(self.vault.get(self.current)) as rpc:
                    self.check_control()
                    if self.reset_disabled.is_set():
                        return
                    if read_auth()[1] != self.current:
                        raise RuntimeError('账号已改变')
                    result = rpc.call('account/rateLimitResetCredit/consume', dict(creditId=entry['credit'], idempotencyKey=entry['key']))
                if result.get('outcome') not in ('reset', 'alreadyRedeemed'):
                    raise ValueError('reset not confirmed')
                entry['confirmed'] = True
                self.store.put('live_redemption', entry)
            fresh = self.query(self.vault.get(self.current))
            self.update_account(self.current, fresh)
            if not usable(fresh, time.time()):
                self.problem = '用卡结果已确认，等待该账号额度恢复；不会改用另一张卡'
                return
        except RuntimeError:
            self.check_control()
            self.problem = '重置卡兑换暂未确认，将用同一请求复查，避免重复用卡'
            return
        except ValueError:
            raise RuntimeError('指定重置卡未恢复额度，已保留兑换记录')
        self.store.put('last_live_redemption', entry)
        self.live_redemption = None
        self.store.put('live_redemption', None)
        self.store.event('已使用当前账号重置卡并确认额度恢复，无需切号')
        self.problem = None
        self.accounts = [a for a in self.accounts if a['key'] != self.current]
        self.refresh()
        self.publish()

    def finish_manual(self, status, message, request=None):
        request = request or self.manual_request
        if not request:
            return
        alias = next((p['alias'] for p in self.profiles if p['key'] == request['target']), '目标账号')
        self.store.put('last_manual_switch', dict(id=request.get('id'), target=request['target'], alias=alias,
                                                status=status, message=message, at=time.time()))
        self.store.event('手动切换至 ' + alias + '：' + message)
        self.manual_request = None
        if status == 'failed':
            self.problem = message
        self.publish()

    def manual_switch_view(self):
        if self.manual_request:
            return dict(id=self.manual_request.get('id'), target=self.manual_request['target'], status='pending',
                        message=self.manual_request.get('message', '正在准备手动切换'))
        result = self.store.get('last_manual_switch')
        if self.pending and self.pending.get('origin') == 'manual' and not result:
            return dict(id=self.pending.get('requestId'), target=self.pending.get('target'), status='failed',
                        message='手动切换中途停止，操作记录已保留；不会自动续跑任务，可重新选择账号')
        return result

    def manual_progress(self, message, phase=None):
        self.manual_request['message'] = message
        if phase:
            self.checkpoint(phase)
        else:
            self.publish()
        self.check_control()

    def manual_switch(self):
        """User-selected login only: no quota gate, redemption, or task continuation."""
        target = self.manual_request['target']
        auth, source, digest = read_auth()
        if source != self.current:
            self.sync_identity()
            return
        if source == target:
            self.finish_manual('complete', '已经是当前账号，已转为仅观察')
            return
        # Resolve credentials before interrupting anything. Quota queries are irrelevant.
        self.manual_progress('正在读取所选账号的登录信息')
        target_auth = self.vault.get(target)
        if identity(target_auth) != target:
            raise RuntimeError('所选账号登录信息不匹配，未切换')
        self.vault.put(source, auth)
        self.pending = dict(origin='manual', source=source, account=source, target=target,
                            requestId=self.manual_request['id'], tasks=[], stoppedTasks=[], phase='prepared')
        self.manual_progress('正在停止当前聊天；切换后不会自动恢复', 'interrupting')
        if desktop_running():
            try:
                with Desktop() as desktop:
                    for live in desktop.snapshot(strict=False):
                        self.check_control()
                        if live['status'] == 'active' or live.get('goal') == 'active':
                            self.pending['stoppedTasks'].append(dict(id=live['id'], title=live['title']))
                            self.checkpoint('interrupting')
                            self.check_control()
                            desktop.stop_chat_for_manual_switch(live)
                    deadline = time.monotonic()+15
                    while time.monotonic() < deadline:
                        self.check_control()
                        latest = desktop.snapshot(strict=False)
                        if not any(t['status'] == 'active' or t.get('goal') == 'active' for t in latest):
                            break
                        time.sleep(.2)
            except UserControlChanged:
                raise
            except (RuntimeError, OSError):
                # Manual switching is allowed even when task discovery is incomplete.
                # The desktop's normal quit confirmation remains authoritative.
                self.store.event('手动切号：部分聊天停止未确认，将请求桌面端正常退出')
            self.manual_progress('正在等待 ChatGPT 退出；若出现退出提示，请确认', 'closing')
            close_desktop()
        self.check_control()
        auth, key, digest = read_auth()
        if key != source:
            raise RuntimeError('退出过程中登录账号已改变，已停止原切换请求')
        self.vault.put(source, auth)
        self.manual_progress('正在切换到所选账号', 'switching')
        replace_auth(target_auth, source, digest)
        self.current = target
        self.auth_hash = read_auth()[2]
        self.store.put('last_identity', target)
        self.pending['account'] = target
        self.manual_progress('正在打开 ChatGPT；任务保持停止', 'reopening')
        open_desktop()
        self.pending.update(phase='complete', completedAt=time.time())
        self.store.put('last_manual_journal', self.pending)
        self.pending = None
        self.store.put('pending', None)
        self.problem = None
        self.refresh()
        self.finish_manual('complete', '切换完成，已转为仅观察；未使用重置卡，也未自动恢复任务')

    def automate(self):
        if self.manual_request:
            try:
                self.manual_switch()
            except UserControlChanged:
                raise
            except Exception as error:
                message = str(error) if isinstance(error, RuntimeError) else '手动切换未完成，请检查登录和桌面状态'
                self.finish_manual('failed', message)
                raise
            return
        self.automate_quota()

    def automate_quota(self):
        """Called only on the controller thread, with fresh quota observations."""
        if self.mode != 'auto' or self.futures or not self.identity_verified:
            return
        if self.pending and self.pending.get('origin') == 'manual':
            return  # A restart cannot silently turn a one-shot request into automation.
        if self.pending and time.time() < self.pending.get('nextQuotaCheckAt', 0):
            return
        if self.pending and self.pending.get('redemption'):
            redemption = self.pending['redemption']
            self.plan = dict(action='redeem', target=redemption['account'], credit=redemption['credit'],
                             reason='继续核验已选定的重置卡，不切换到其他低额度账号')
        action = self.plan['action']
        self.check_control()
        if action == 'reset_current' and not self.auto_reset and not self.live_redemption:
            self.plan = self.rotation_plan(time.time())
            action = self.plan['action']
            if action == 'reset_current':
                self.plan = dict(action='hold', reason='自动用卡已关闭，先暂停任务并等待额度')
                action = 'hold'
        if self.live_redemption or action == 'reset_current':
            self.reset_current()
            return
        if not self.pending:
            if action not in ('switch', 'redeem', 'wait', 'hold'):
                return
            if self.desktop_error or not desktop_running():
                return
            # Do not close a newly selected account based on the old account's quota.
            if read_auth()[1] != self.current:
                self.sync_identity()
                return
            # Take a new snapshot immediately before normal shutdown. Never capture idle/manual stops.
            with Desktop() as d:
                tasks = d.snapshot()
            if any(t['status'] == 'active' and not t.get('turn') for t in tasks):
                raise RuntimeError('运行任务的回合尚未确认，未关闭桌面端')
            if any(t['status'] == 'active' and t.get('goal') == 'active' for t in tasks):
                raise RuntimeError('有聊天启用了持续运行目标，需要先暂停目标；未中断其他任务')
            active = [dict(t, phase='saved', message=str(uuid.uuid4())) for t in tasks
                      if t['status'] == 'active' and not t['waiting'] and t.get('turn')
                      and t.get('goal') not in ('paused', 'blocked', 'complete', 'budgetLimited')]
            if any(t['waiting'] for t in tasks):
                raise RuntimeError('有任务等待用户操作，保持观察，避免关闭审批或输入窗口')
            with self.control_lock:
                self.excluded_ids.clear()
            self.pending = dict(source=self.current, account=self.current, tasks=active, phase='prepared',
                                origin='auto', target=self.plan.get('target'))
            if action == 'redeem':
                self.pending['redemption'] = dict(account=self.plan['target'], credit=self.plan['credit'], key=str(uuid.uuid4()))
            self.checkpoint('prepared')
            self.pause_tasks()
            self.checkpoint('closing')
            self.check_control()
            close_desktop()
            self.check_control()
            # Closing can rotate credentials. Save the final token state before replacing it.
            auth, key, digest = read_auth()
            if key != self.current:
                raise RuntimeError('退出过程中账号变化，已停止自动操作')
            self.vault.put(key, auth)
            self.auth_hash = digest
            self.checkpoint('paused')
            self.store.event(f'已记录 {len(active)} 项任务，逐项核验停止并正常关闭桌面端')
        elif self.pending['phase'] not in ('paused', 'redeeming'):
            raise RuntimeError('上次操作未完整结束，请检查恢复记录后手动处理')
        if desktop_running():
            raise RuntimeError('检测到用户重新打开桌面端，自动恢复已让出控制')
        if read_auth()[1] != self.current:
            raise RuntimeError('检测到用户切号，取消旧恢复计划')
        if action in ('wait', 'hold', 'unknown'):
            return
        if action == 'redeem' and (not self.auto_reset or self.reset_disabled.is_set()):
            self.problem = '任务已暂停；自动使用重置卡尚未启用'
            return
        target = self.plan.get('target', self.current)
        self.check_control()
        locked_redemption = self.pending.get('redemption')
        if locked_redemption and locked_redemption.get('attempted') and self.current != locked_redemption['account']:
            raise RuntimeError('原重置卡兑换结果仍待确认，不切换账号')
        if target != self.current:
            auth = self.vault.get(target)
            _, key, digest = read_auth()
            self.pending['target'] = target
            self.checkpoint('switching')
            self.check_control()
            replace_auth(auth, self.current, digest)
            self.current = target
            self.auth_hash = read_auth()[2]
            self.store.put('last_identity', target)
            self.pending['account'] = target
            self.checkpoint('paused')
            self.store.event('已切换到备用账号')
        if action == 'redeem':
            self.check_control()
            redemption = self.pending.get('redemption')
            if redemption and (redemption['account'] != target or redemption['credit'] != self.plan['credit']):
                raise RuntimeError('上次重置卡兑换结果待确认，暂停新的兑换')
            if not redemption:
                redemption = dict(account=target, credit=self.plan['credit'], key=str(uuid.uuid4()))
                self.pending['redemption'] = redemption
            self.checkpoint('redeeming')
            if not redemption.get('confirmed'):
                with RPC(self.vault.get(target)) as rpc:
                    self.check_control()
                    if self.reset_disabled.is_set():
                        self.checkpoint('paused')
                        self.problem = '自动使用重置卡已关闭，未发起兑换'
                        return
                    redemption['attempted'] = True
                    self.checkpoint('redeeming')
                    result = rpc.call('account/rateLimitResetCredit/consume',
                                      dict(creditId=redemption['credit'], idempotencyKey=redemption['key']))
                if result.get('outcome') not in ('reset', 'alreadyRedeemed'):
                    self.checkpoint('paused')
                    raise RuntimeError('重置卡未恢复额度，请刷新并检查账号')
                redemption['confirmed'] = True
                self.checkpoint('redeeming')
                self.store.event('已确认重置卡兑换结果，正在重新核验额度')
        fresh = self.query(self.vault.get(target))
        self.update_account(target, fresh)
        if not usable(fresh, time.time()):
            self.pending['nextQuotaCheckAt'] = time.time()+60
            self.checkpoint('paused')
            return
        self.checkpoint('reopening')
        self.check_control()
        open_desktop()
        for saved in self.pending['tasks']:
            self.check_control()
            if self.excluded(saved):
                saved['phase'] = 'excluded'
                self.checkpoint('resuming')
                continue
            if saved['phase'] in ('done', 'excluded', 'sending'):
                continue
            if read_auth()[1] != target:
                raise RuntimeError('恢复前账号已改变，停止续跑')
            open_desktop(saved['id'])
            deadline = time.monotonic()+25
            live = None
            while time.monotonic() < deadline:
                self.check_control()
                try:
                    with Desktop() as d:
                        tasks = d.snapshot()
                    live = next((t for t in tasks if t['id'] == saved['id']), None)
                    if live:
                        break
                except (RuntimeError, OSError):
                    pass
                time.sleep(.5)
            if live is None:
                raise RuntimeError('无法确认原聊天已恢复，保留恢复记录')
            if live['status'] == 'active' or live.get('turn') != saved['turn']:
                saved.update(phase='excluded', outcome='already_running_or_new_turn')
            elif resumable(saved, live):
                with Desktop() as d:
                    latest = next((t for t in d.snapshot() if t['id'] == saved['id']), None)
                    if latest and resumable(saved, latest):
                        self.check_control()
                        if self.excluded(saved):
                            saved['phase'] = 'excluded'
                            self.checkpoint('resuming')
                            continue
                        if read_auth()[1] != target:
                            raise RuntimeError('用户已切换账号，停止续跑')
                        saved['phase'] = 'sending'
                        self.checkpoint('resuming')  # Persist BEFORE sending. Unknown delivery is never retried.
                        self.check_control()
                        d.continue_chat(saved, latest)
                        saved.update(phase='done', outcome='continuation_confirmed')
                    else:
                        saved['phase'] = 'excluded'
            else:
                saved['phase'] = 'excluded'
            self.checkpoint('resuming')
        completed = sum(t.get('outcome') == 'continuation_confirmed' for t in self.pending['tasks'])
        skipped = sum(t.get('phase') == 'excluded' for t in self.pending['tasks'])
        self.pending.update(completedAt=time.time(), continuedCount=completed, skippedCount=skipped, phase='complete')
        self.store.event(f'额度可用，已确认续跑 {completed} 项，跳过 {skipped} 项' if self.pending['tasks'] else '账号调度完成，桌面端已打开；本次没有待恢复任务')
        self.store.put('last_completed', self.pending)
        self.pending = None
        self.store.put('pending', None)
        self.problem = None
        self.refresh()
        self.manual_request = None
        self.publish()

    def command(self, msg):
        try:
            self._command(msg)
        except Exception as error:
            if msg.get('command') == 'switch_account':
                message = str(error) if isinstance(error, RuntimeError) else '切换请求未执行，请重试'
                self.finish_manual('failed', message, dict(id=msg.get('requestId'), target=msg.get('key')))
            raise

    def _command(self, msg):
        self.signal(msg)
        name = msg.get('command')
        if name == 'refresh':
            self.vault.clear_backoff()
            self.refresh()
        elif name == 'cycle_mode':
            self.command(dict(command='mode', value={'observe': 'auto', 'auto': 'paused', 'paused': 'observe'}[self.mode]))
        elif name == 'retry_failed_quit':
            if not self.can_retry_failed_quit() or read_auth()[1] != self.current or not desktop_running():
                raise RuntimeError('原操作状态已变化，请核对恢复记录')
            with Desktop() as desktop:
                tasks = desktop.snapshot()
            for saved in self.pending['tasks']:
                live = next((t for t in tasks if t['id'] == saved['id']), None)
                if not live or live.get('turnStatus') == 'interrupted':
                    raise RuntimeError('原任务可能已中断，保留恢复记录，未重新调度')
            self.store.put('abandoned', self.pending)
            self.pending = None
            self.store.put('pending', None)
            self.manual_request = None
            self.command(dict(command='mode', value='auto'))
            self.store.event('已核对上次退出未完成，重新按最新任务和额度调度')
        elif name == 'rotation':
            profile = next((p for p in self.profiles if p['key'] == msg.get('key')), None)
            if not profile:
                self.cancel.clear()
                raise RuntimeError('找不到该账号，请刷新后重试')
            enabled = msg.get('value') is True
            profile['rotationEnabled'] = enabled
            self.store.put('profiles', self.profiles)
            for account in self.accounts:
                if account['key'] == profile['key']:
                    account['rotationEnabled'] = enabled
            if self.pending or self.live_redemption:
                if not enabled:
                    self.mode = 'observe'
                    self.manual_request = None
                    self.store.put('desired_mode', self.mode)
                    self.problem = '轮换账号选择已改变，原操作记录已保留'
            elif self.mode == 'auto' or self.manual_request:
                self.cancel.clear()
            self.plan = self.rotation_plan(time.time())
            self.store.event('账号已加入自动轮换' if enabled else '账号已改为仅监控')
            self.refresh()
        elif name == 'cancel_switch':
            self.finish_manual('cancelled', '已取消切换；已停止的任务不会自动恢复')
            if self.pending and self.pending.get('origin') == 'manual':
                self.mode = 'observe'
                self.store.put('desired_mode', self.mode)
                self.problem = '手动切号已停止，操作记录已保留；任务不会自动恢复'
            else:
                self.cancel.clear()
                self.problem = None
            self.store.event('用户取消了手动切号')
        elif name == 'switch_account':
            target = msg.get('key')
            if not any(p['key'] == target for p in self.profiles):
                raise RuntimeError('找不到目标账号，请刷新账号信息')
            self.mode = 'observe'
            self.store.put('desired_mode', self.mode)
            # User intent replaces automation. Keep its journal, never resume or redeem it.
            if self.pending:
                self.store.put('abandoned', self.pending)
                self.pending = None
                self.store.put('pending', None)
            if self.live_redemption:
                self.store.put('abandoned_live_redemption', self.live_redemption)
                self.live_redemption = None
                self.store.put('live_redemption', None)
            self.problem = None
            if target == self.current and self.identity_verified:
                self.cancel.clear()
                self.finish_manual('complete', '已经是当前账号，已转为仅观察', dict(id=msg.get('requestId'), target=target))
                return
            self.manual_request = dict(target=target, id=msg.get('requestId') or str(uuid.uuid4()))
            self.store.put('last_manual_switch', None)
            self.cancel.clear()
            self.problem = None
            self.store.event('已接收手动切号请求；不检查额度、不用卡、不恢复任务')
        elif name == 'mode':
            value = msg.get('value')
            if value not in ('observe', 'auto', 'paused'):
                return
            self.mode, self.problem = value, None
            self.finish_manual('cancelled', '监控模式已改变，已停止手动切换')
            self.store.put('desired_mode', value)
            if value == 'auto':
                self.cancel.clear()
            self.store.event({'observe': '已切换为仅观察', 'auto': '用户启用了自动调度', 'paused': '用户暂停了监控和调度'}[value])
            if value == 'paused':
                self.refresh_requested.clear()
            else:
                self.refresh()
        elif name == 'auto_reset':
            self.auto_reset = msg.get('value') is True
            if self.auto_reset:
                self.reset_disabled.clear()
            self.store.put('auto_reset', self.auto_reset)
        elif name == 'import':
            self.import_file(msg['path'])
            self.refresh()
        elif name == 'exclude':
            if self.pending:
                for t in self.pending['tasks']:
                    if t['id'] == msg.get('id') and t.get('phase') == 'saved':
                        t['phase'] = 'excluded'
                self.store.put('pending', self.pending)
        elif name == 'clear_pending':
            self.store.put('abandoned', self.pending)
            self.pending = None
            self.manual_request = None
            self.store.put('pending', None)
            self.mode = 'observe'
            self.store.put('desired_mode', self.mode)
            self.problem = None

    def view(self):
        pending = self.pending or {}
        # Card IDs and authentication material never cross into the UI or diagnostics.
        accounts = []
        records = list(self.accounts)
        for profile in self.profiles:
            if not any(a['key'] == profile['key'] for a in records):
                records.append(dict(profile, remaining=None, windows=[], cards=[], cardCount=None, cardsComplete=False))
        for a in records:
            data = {k: v for k, v in a.items() if k != 'cards'}
            data['current'] = self.identity_verified and a['key'] == self.current
            data['nextCheckAt'] = (self.next_query_at(a, time.time()) if self.mode != 'paused' or a['key'] in self.refresh_requested else None)
            cards = sorted(a.get('cards', []), key=lambda c: (c.get('expiresAt') is None, c.get('expiresAt') or 0))
            data['cards'] = [dict(expiresAt=c.get('expiresAt'), status=c.get('status'), title=c.get('title')) for c in cards]
            accounts.append(data)
        plan = {k: v for k, v in self.plan.items() if k != 'credit'}
        return dict(version='1.10.0', mode=self.mode, autoReset=self.auto_reset, accounts=accounts, tasks=self.tasks,
                    desktopIssues=self.desktop_issues, cleanupPendingCount=cleanup_pending_count(),
                    lastCompleted=self.completed_view(),
                    canRetryFailedQuit=self.can_retry_failed_quit(),
                    credentialNeedsAttention=getattr(self.vault, 'needs_attention', False) is True or any(a.get('credentialError') for a in self.accounts),
                    manualSwitchPending=bool(self.manual_request),
                    manualSwitchResult=self.manual_switch_view(),
                    identityVerified=self.identity_verified, operation=self.operation(),
                    liveRedemptionPending=bool(self.live_redemption), refreshQueued=bool(self.refresh_requested),
                    preflight=self.preflight(),
                    desktopError=self.desktop_error, problem=self.identity_error or self.problem or self.backup_error, plan=plan,
                    pending=[dict(id=t['id'], title=t['title'], phase=t['phase']) for t in pending.get('tasks', [])],
                    pendingPhase=pending.get('phase'), events=self.store.events(), refreshing=bool(self.futures), at=time.time())

    def completed_view(self):
        completed = self.store.get('last_completed')
        if not completed:
            return None
        return dict(at=completed.get('completedAt'), captured=len(completed.get('tasks', [])),
                    continued=completed.get('continuedCount'), skipped=completed.get('skippedCount'))

    def can_retry_failed_quit(self):
        return bool(self.pending and self.mode == 'observe' and self.pending.get('phase') == 'closing'
                    and self.pending.get('origin') != 'manual'
                    and not any(t.get('pauseRequestedAt') for t in self.pending.get('tasks', []))
                    and not self.pending.get('redemption') and not self.live_redemption)


def main():
    os.umask(0o077)
    data = pathlib.Path(os.environ.get('CODEX_QUOTA_DATA_DIR', str(pathlib.Path.home()/'Library/Application Support/CodexQuota')))
    store = Store(data)
    os.environ['CODEX_QUOTA_HELPER'] = sys.argv[1]
    lease = (data/'controller.lock').open('a')
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({'mode': 'observe', 'problem': '已有小组件正在监控，第二个实例不会接管'}), flush=True)
        return
    engine = Engine(store, Vault(sys.argv[1]))
    def publish():
        snapshot = engine.view()
        status_path = data/'status.tmp'
        status_path.write_text(json.dumps(snapshot, ensure_ascii=False))
        os.replace(status_path, data/'status.json')
        try:
            print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        except BrokenPipeError:
            engine.signal({'command': 'quit'})
    engine.publish = publish
    # Existing user-chosen backup only; never scan unrelated credential stores.
    try:
        engine.poll_identity()
        engine.sync_backups()
        engine.migrate_profiles()
        if engine.mode != 'paused':
            engine.refresh()
    except CredentialStoreError:
        pass  # Polling exposes the account-specific error and clears it after recovery.
    except Exception as e:
        engine.problem = str(e) if isinstance(e, RuntimeError) else '账号导入失败，请选择登录文件'
    commands = queue.Queue()
    def reader():
        for line in sys.stdin:
            try:
                message = json.loads(line)
                engine.signal(message)
                commands.put(message)
            except ValueError:
                pass
        engine.signal({'command': 'quit'})
        commands.put({'command': 'quit'})
    threading.Thread(target=reader, daemon=True).start()
    while True:
        try:
            msg = commands.get(timeout=1)
            if msg.get('command') == 'quit':
                break
            engine.command(msg)
        except queue.Empty:
            pass
        except Exception as e:
            engine.problem = str(e) if isinstance(e, RuntimeError) else '操作未完成，请重试'
            if engine.cancel.is_set():
                engine.manual_request = None
                engine.mode = 'observe'
                store.put('desired_mode', engine.mode)
        try:
            engine.cycle()
            # Publish before any longer action so the UI never appears frozen.
            engine.publish()
            engine.automate()
        except Exception as e:
            if engine.shutdown_requested.is_set():
                break
            if isinstance(e, UserControlChanged):
                continue  # Let the queued user command choose the mode; do not overwrite it.
            engine.manual_request = None
            engine.mode = 'observe'
            store.put('desired_mode', engine.mode)
            engine.problem = str(e) if isinstance(e, RuntimeError) else '自动调度未完成，已停止后续操作'
            store.event(engine.problem)
    engine.pool.shutdown(wait=False)


if __name__ == '__main__':
    main()
