"""Local, encrypted checkout pools. AccessForge never calls SCE-web."""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from contextlib import contextmanager
from email.message import EmailMessage
from functools import wraps
from urllib.parse import urlsplit

from flask import current_app, has_request_context, request
from werkzeug.security import check_password_hash, generate_password_hash



class CheckoutError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def email_address(value):
    value = str(value or '').strip().casefold()
    if len(value) > 254 or not re.fullmatch(r'[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+', value):
        raise CheckoutError('Enter a valid email address.')
    return value


def checkout_client_ip():
    """Use the peer address unless an explicitly trusted proxy overwrote X-Real-IP."""
    if not has_request_context():
        return None
    try:
        peer=ipaddress.ip_address(request.remote_addr or '')
        if isinstance(peer,ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer=peer.ipv4_mapped
    except ValueError:
        return None
    configured=current_app.config.get('CHECKOUT_TRUSTED_PROXIES',os.environ.get('CHECKOUT_TRUSTED_PROXIES','127.0.0.1/32,::1/128'))
    for entry in str(configured).split(','):
        try:
            if peer in ipaddress.ip_network(entry.strip()):
                try:
                    return str(ipaddress.ip_address(request.headers.get('X-Real-IP','')))
                except ValueError:
                    break
        except ValueError:
            continue
    return str(peer)


def smtp_ready():
    return bool(os.environ.get('CHECKOUT_SMTP_HOST') and os.environ.get('CHECKOUT_SMTP_FROM'))


def send_verification(email, code, project_name):
    if not smtp_ready():
        raise CheckoutError('Email verification is not configured. Contact your instructor.', 503)
    message = EmailMessage()
    message['From'] = os.environ['CHECKOUT_SMTP_FROM']
    message['To'] = email
    message['Subject'] = 'Your lab access verification code'
    message.set_content(f'Your verification code for {project_name} is {code}.\n\nIt expires in 10 minutes. If you did not request lab access, ignore this message.')
    use_ssl = os.environ.get('CHECKOUT_SMTP_SSL', '0') == '1'
    factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    try:
        with factory(os.environ['CHECKOUT_SMTP_HOST'], int(os.environ.get('CHECKOUT_SMTP_PORT', '465' if use_ssl else '587')), timeout=15) as mail:
            if not use_ssl:
                mail.starttls(context=ssl.create_default_context())
            if os.environ.get('CHECKOUT_SMTP_USER'):
                mail.login(os.environ['CHECKOUT_SMTP_USER'], os.environ.get('CHECKOUT_SMTP_PASSWORD', ''))
            mail.send_message(message)
    except Exception:
        raise CheckoutError('Could not send verification email. Please try again later.', 503) from None


from cryptography.fernet import Fernet
import base64
import json
import math


def cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(str(current_app.secret_key).encode()).digest()))


def encrypt_str(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt_str(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except Exception:
        raise CheckoutError('Cannot decrypt checkout credentials. Restore the AccessForge secret key.', 503) from None


def user_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}(?:@[A-Za-z0-9_.-]{1,32})?', value):
        raise CheckoutError('Account usernames must be valid Proxmox user IDs.')
    return value if '@' in value else value + '@pve'


class CheckoutStore:
    def __init__(self):
        directory = current_app.config.get('CHECKOUT_DATA_DIR') or os.environ.get('CHECKOUT_DATA_DIR') or os.path.join(current_app.root_path, 'data')
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            raise CheckoutError('Checkout storage is not writable. Fix CHECKOUT_DATA_DIR permissions for the AccessForge container user (UID 1000 in the supplied image).',503) from None
        self.path = os.path.join(directory, 'account_checkout.sqlite3')
        self.key = str(current_app.secret_key)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pools (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    verify_email INTEGER NOT NULL DEFAULT 1, secret_hash TEXT NOT NULL DEFAULT '', secret_lookup TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS accounts (
                    pool_id TEXT NOT NULL, username TEXT NOT NULL, password_enc TEXT NOT NULL,
                    source_hash TEXT NOT NULL, eligible INTEGER NOT NULL DEFAULT 0, email TEXT, assigned_at REAL,
                    pending_password TEXT, legacy_imported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(pool_id,username));
                CREATE UNIQUE INDEX IF NOT EXISTS assigned_email ON accounts(pool_id,email) WHERE email IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS published_account ON accounts(username);
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY, pool_id TEXT NOT NULL, email TEXT NOT NULL, code_hash TEXT NOT NULL,
                    expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, pool_id TEXT NOT NULL, username TEXT, email TEXT,
                    action TEXT NOT NULL, actor TEXT, timestamp REAL NOT NULL);
            ''')
            # executescript commits its surrounding transaction; serialize schema upgrades across workers.
            db.execute('BEGIN IMMEDIATE')
            if 'legacy_imported' not in {row['name'] for row in db.execute('PRAGMA table_info(accounts)')}:
                db.execute('ALTER TABLE accounts ADD COLUMN legacy_imported INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS lease_sessions (lease_id TEXT NOT NULL,owner TEXT PRIMARY KEY)')
            columns={row['name'] for row in db.execute('PRAGMA table_info(accounts)')}
            for name,kind in [('checked_out_at','REAL'),('checkout_ip','TEXT'),('relinquished_at','REAL'),('relinquished_ip','TEXT'),('lease_id','TEXT'),('login_disabled','INTEGER NOT NULL DEFAULT 0'),('release_pending','INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE accounts ADD COLUMN {name} {kind}')
            pool_columns={row['name'] for row in db.execute('PRAGMA table_info(pools)')}
            for name in ('manager_username','manager_password_enc'):
                if name not in pool_columns:
                    db.execute(f'ALTER TABLE pools ADD COLUMN {name} TEXT')
            db.execute('UPDATE accounts SET release_pending=1,pending_password=NULL WHERE pending_password IS NOT NULL')
            if 'ip_address' not in {row['name'] for row in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN ip_address TEXT')
            db.execute('UPDATE accounts SET checked_out_at=assigned_at WHERE checked_out_at IS NULL AND assigned_at IS NOT NULL')
            db.execute('UPDATE accounts SET lease_id=lower(hex(randomblob(24))) WHERE email IS NOT NULL AND lease_id IS NULL')

    @contextmanager
    def transaction(self):
        try:
            db = sqlite3.connect(self.path, timeout=35)
        except sqlite3.OperationalError:
            raise CheckoutError('Could not open checkout storage. Check CHECKOUT_DATA_DIR permissions and disk space.',503) from None
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            raise CheckoutError('This account is already published to a pool, or the email is already assigned. Refresh status and try again.', 409) from None
        except sqlite3.OperationalError:
            db.rollback()
            raise CheckoutError('Checkout storage is busy or unavailable. Please try again later.', 503) from None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def digest(self, value):
        return hmac.new(self.key.encode(), str(value).encode(), hashlib.sha256).hexdigest()

    def limit(self, key, maximum, window):
        now = time.time()
        with self.transaction() as db:
            db.execute('DELETE FROM rate_limits WHERE start<?', (now - 3600,))
            row = db.execute('SELECT * FROM rate_limits WHERE key=?', (key,)).fetchone()
            if row and row['start'] > now-window and row['count'] >= maximum:
                raise CheckoutError('Too many attempts. Please try again later.', 429)
            if not row or row['start'] <= now-window:
                db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)', (key, now))
            else:
                db.execute('UPDATE rate_limits SET count=count+1 WHERE key=?', (key,))

    def event(self, db, pool_id, username, email, action, actor):
        db.execute('INSERT INTO events(pool_id,username,email,action,actor,timestamp,ip_address) VALUES(?,?,?,?,?,?,?)',
                   (pool_id, username, email, action, actor, time.time(), checkout_client_ip()))

    def pool(self, db, pool_id):
        row = db.execute('SELECT * FROM pools WHERE id=?', (pool_id,)).fetchone()
        if not row:
            raise CheckoutError('Checkout pool not found.', 404)
        return row

    def summary(self, pool_id):
        with self.transaction() as db:
            pool = self.pool(db, pool_id)
            accounts = {r['username']: {'eligible': bool(r['eligible']), 'email': r['email'], 'assigned_at': r['assigned_at'],
                         'checked_out_at': r['checked_out_at'], 'checkout_ip': r['checkout_ip'],
                         'relinquished_at': r['relinquished_at'], 'relinquished_ip': r['relinquished_ip'],
                         'login_disabled': bool(r['login_disabled']), 'releasing': bool(r['release_pending']), 'password_rotated': r['source_hash'] != self.digest(decrypt_str(r['password_enc']))}
                        for r in db.execute('SELECT * FROM accounts WHERE pool_id=? ORDER BY username', (pool_id,))}
        return {'id': pool_id, 'name': pool['name'], 'enabled': bool(pool['enabled']), 'verify_email': bool(pool['verify_email']),
                'secret_set': bool(pool['secret_hash']), 'smtp_configured': smtp_ready(), 'accounts': accounts,
                'login_management_ready':bool(pool['manager_password_enc'] or os.environ.get('CHECKOUT_PROXMOX_API_TOKEN'))}

    def summaries(self):
        with self.transaction() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM pools ORDER BY name,id')]
        return [self.summary(pid) for pid in ids]

    def publish(self, pool_id, data, actor):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', pool_id):
            raise CheckoutError('Invalid pool ID.')
        name, entries = data.get('name'), data.get('accounts')
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise CheckoutError('Provide a pool name of at most 200 characters.')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2000:
            raise CheckoutError('Publish between 1 and 2000 selected accounts.')
        seen = set()
        with self.transaction() as db:
            db.execute('INSERT INTO pools(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (pool_id, name.strip()))
            for entry in entries:
                if not isinstance(entry, dict):
                    raise CheckoutError('Invalid account entry.')
                username = user_id(entry.get('username'))
                password = entry.get('password')
                if not isinstance(password, str) or not 8 <= len(password) <= 128 or username in seen:
                    raise CheckoutError('Each selected account needs a unique username and a valid password.')
                seen.add(username)
                existing = db.execute('SELECT * FROM accounts WHERE pool_id=? AND username=?', (pool_id, username)).fetchone()
                source_hash = self.digest(password)
                if existing and source_hash != existing['source_hash']:
                    if existing['email'] or existing['release_pending']:
                        raise CheckoutError(f'Release {username} in AccessForge before replacing its published credentials.', 409)
                    db.execute('UPDATE accounts SET password_enc=?,source_hash=? WHERE pool_id=? AND username=?', (encrypt_str(password), source_hash, pool_id, username))
                elif not existing:
                    db.execute('INSERT INTO accounts(pool_id,username,password_enc,source_hash) VALUES(?,?,?,?)', (pool_id, username, encrypt_str(password), source_hash))
                # Re-publication does not re-enable a disabled login or change its checkout eligibility.
                if entry.get('email'):
                    email = email_address(entry['email'])
                    row = db.execute('SELECT email,legacy_imported FROM accounts WHERE pool_id=? AND username=?', (pool_id, username)).fetchone()
                    if row['legacy_imported']:
                        continue
                    if row['email'] and row['email'] != email:
                        raise CheckoutError('Legacy assignment conflicts with an existing AccessForge assignment.', 409)
                    if not row['email']:
                        assigned = entry.get('assigned_at', time.time())
                        if not isinstance(assigned, (int,float)) or not math.isfinite(assigned):
                            assigned = time.time()
                        db.execute('UPDATE accounts SET email=?,assigned_at=?,checked_out_at=?,lease_id=? WHERE pool_id=? AND username=?', (email, assigned, assigned, secrets.token_urlsafe(24), pool_id, username))
                    pending = entry.get('pending_release_password')
                    if pending is not None and (not isinstance(pending,str) or not 8 <= len(pending) <= 128):
                        raise CheckoutError('Invalid pending release credential.')
                    db.execute('UPDATE accounts SET legacy_imported=1,release_pending=MAX(release_pending,?) WHERE pool_id=? AND username=?', (int(bool(pending)),pool_id,username))
            self.event(db, pool_id, None, None, 'published', actor)
        return self.summary(pool_id)

    def configure(self, pool_id, data, actor):
        with self.transaction() as db:
            pool = dict(self.pool(db, pool_id))
            for key in ('enabled', 'verify_email'):
                if key in data:
                    if type(data[key]) is not bool:
                        raise CheckoutError(f'{key} must be a boolean.')
                    pool[key] = int(data[key])
            if data.get('secret'):
                secret = data['secret']
                if not isinstance(secret, str) or not 8 <= len(secret) <= 256:
                    raise CheckoutError('Use a lab secret between 8 and 256 characters.')
                pool['secret_hash'] = generate_password_hash(secret, method='pbkdf2:sha256')
                pool['secret_lookup'] = self.digest('class:' + secret)
            if pool['enabled'] and not pool['secret_hash']:
                raise CheckoutError('Set a lab secret before enabling checkout.')
            other = db.execute('SELECT id FROM pools WHERE secret_lookup=? AND id<>?', (pool.get('secret_lookup'), pool_id)).fetchone()
            if other:
                raise CheckoutError('That lab secret is already used by another pool.', 409)
            db.execute('UPDATE pools SET enabled=?,verify_email=?,secret_hash=?,secret_lookup=? WHERE id=?', (pool['enabled'],pool['verify_email'],pool['secret_hash'],pool.get('secret_lookup'),pool_id))
            db.execute('DELETE FROM challenges WHERE pool_id=?', (pool_id,))
            self.event(db, pool_id, None, None, 'configured', actor)
        return self.summary(pool_id)

    def allocate(self, db, pool_id, email, actor='student', username=None):
        pool = self.pool(db, pool_id)
        row = db.execute('SELECT * FROM accounts WHERE pool_id=? AND email=?', (pool_id,email)).fetchone()
        if row and (row['release_pending'] or row['login_disabled'] or (username and row['username'] != username)):
            raise CheckoutError('This email already has another account or its release is pending.', 409)
        if not row:
            sql = '''SELECT a.* FROM accounts a WHERE pool_id=? AND email IS NULL AND release_pending=0 AND login_disabled=0
                     AND NOT EXISTS(SELECT 1 FROM accounts b WHERE b.username=a.username AND b.email IS NOT NULL)'''
            args = [pool_id]
            if username:
                sql += ' AND username=?'
                args.append(username)
            else:
                sql += ' AND eligible=1'
            row = db.execute(sql+' ORDER BY username LIMIT 1',args).fetchone()
            if not row:
                raise CheckoutError('No accounts are available. Contact your instructor.', 409)
            assigned=time.time()
            db.execute('UPDATE accounts SET email=?,assigned_at=?,checked_out_at=?,checkout_ip=?,lease_id=? WHERE pool_id=? AND username=?', (email,assigned,assigned,checkout_client_ip(),secrets.token_urlsafe(24),pool_id,row['username']))
            self.event(db,pool_id,row['username'],email,'assigned',actor)
        return {'status':'assigned','project':pool['name'],'email':email,'username':row['username'],'password':decrypt_str(row['password_enc']),
                'connection': {'host':current_app.config['CHECKOUT_PROXMOX_HOST'],'port':current_app.config['CHECKOUT_PROXMOX_PORT'],
                               'realm':row['username'].rsplit('@',1)[1],'verify_ssl':current_app.config['CHECKOUT_PROXMOX_VERIFY']}}

    def account_action(self, pool_id, data, actor):
        usernames = data.get('usernames')
        if not isinstance(usernames,list) or not usernames or len(usernames)>2000:
            raise CheckoutError('Select at least one account.')
        usernames = list(dict.fromkeys(user_id(u) for u in usernames))
        action = data.get('action')
        with self.transaction() as db:
            self.pool(db,pool_id)
            for username in usernames:
                if not db.execute('SELECT 1 FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone():
                    raise CheckoutError('Unknown selected account.')
            if action in ('enable','disable'):
                for username in usernames:
                    db.execute('UPDATE accounts SET eligible=? WHERE pool_id=? AND username=?',(int(action=='enable'),pool_id,username))
                    self.event(db,pool_id,username,None,action,actor)
            elif action=='assign' and len(usernames)==1:
                email=email_address(data.get('email'))
                row=db.execute('SELECT email FROM accounts WHERE pool_id=? AND username=?',(pool_id,usernames[0])).fetchone()
                if row['email'] and row['email']!=email:
                    raise CheckoutError('Release this account before reassigning it.',409)
                self.allocate(db,pool_id,email,actor,usernames[0])
            elif action=='remove':
                for username in usernames:
                    row=db.execute('SELECT email,release_pending,login_disabled FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone()
                    if row['email'] or row['release_pending']:
                        raise CheckoutError('Release assigned accounts before removing them.',409)
                    db.execute('DELETE FROM accounts WHERE pool_id=? AND username=?',(pool_id,username))
                    self.event(db,pool_id,username,None,'removed',actor)
            else:
                raise CheckoutError('Select one account for assignment, or choose a valid action.')
        return self.summary(pool_id)

    def assigned_account(self,username):
        username=user_id(username)
        with self.transaction() as db:
            row=db.execute('SELECT * FROM accounts WHERE username=? AND email IS NOT NULL',(username,)).fetchone()
            return dict(row) if row else None

    def note_session(self,lease_id,owner):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO lease_sessions VALUES(?,?)',(lease_id,owner))

    def revoke_lease(self,lease_id):
        if not lease_id:
            return
        with self.transaction() as db:
            owners=[row['owner'] for row in db.execute('SELECT owner FROM lease_sessions WHERE lease_id=?',(lease_id,))]
            db.execute('DELETE FROM lease_sessions WHERE lease_id=?',(lease_id,))
        revoke=current_app.extensions.get('checkout_revoke_sessions')
        if revoke:
            for owner in owners:
                try:
                    revoke(owner)
                except Exception:
                    current_app.logger.warning('Could not close a relinquished account console connection')

    def account(self,username):
        username=user_id(username)
        with self.transaction() as db:
            row=db.execute('SELECT * FROM accounts WHERE username=?',(username,)).fetchone()
            return dict(row) if row else None

    def save_manager(self,pool_id,username,password):
        if not isinstance(username,str) or not isinstance(password,str) or not password:
            raise CheckoutError('Provide an administrator password for login management.')
        with self.transaction() as db:
            self.pool(db,pool_id)
            if db.execute('SELECT 1 FROM accounts WHERE pool_id=? AND username=?',(pool_id,user_id(username))).fetchone():
                raise CheckoutError('The login-management administrator cannot be a student account in this pool.',409)
            db.execute('UPDATE pools SET manager_username=?,manager_password_enc=? WHERE id=?',(username,encrypt_str(password),pool_id))
            self.event(db,pool_id,None,None,'login_manager_saved',username)

    def manager(self,pool_id):
        with self.transaction() as db:
            pool=self.pool(db,pool_id)
            if not pool['manager_password_enc']:
                raise CheckoutError('Publish accounts from SCE-web again or save a login-management administrator in AccessForge.',409)
            return pool['manager_username'],decrypt_str(pool['manager_password_enc'])

    def prepare_accounts(self,pool_id,data,actor,apply):
        action=data.get('action')
        entries=data.get('accounts')
        if action not in ('reset_passwords','reenable_logins') or not isinstance(entries,list) or not 1<=len(entries)<=2000:
            raise CheckoutError('Select saved accounts for this operation.')
        prepared=[];seen=set()
        for entry in entries:
            if not isinstance(entry,dict):
                raise CheckoutError('Invalid account entry.')
            username=user_id(entry.get('username'));password=entry.get('password')
            if username in seen or not isinstance(password,str) or not 8<=len(password)<=64:
                raise CheckoutError('Use unique accounts with passwords between 8 and 64 characters.')
            seen.add(username);prepared.append((username,password))
        results=[]
        for username,password in prepared:
            try:
                with self.transaction() as db:
                    self.pool(db,pool_id)
                    row=db.execute('SELECT * FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone()
                    if not row:
                        raise CheckoutError('Publish this account before managing its login.',409)
                    if row['email'] or row['release_pending']:
                        raise CheckoutError('Release the assigned account before resetting or re-enabling it.',409)
                    if not row['login_disabled']:
                        raise CheckoutError('Disable this login before preparing it for checkout.',409)
                    if action=='reenable_logins' and not hmac.compare_digest(password.encode(),decrypt_str(row['password_enc']).encode()):
                        raise CheckoutError('Reset the saved password or publish the updated credentials before re-enabling.',409)
                    apply(username,password)
                    if action=='reset_passwords':
                        db.execute('UPDATE accounts SET password_enc=?,source_hash=? WHERE pool_id=? AND username=?',(encrypt_str(password),self.digest(password),pool_id,username))
                    else:
                        db.execute('UPDATE accounts SET login_disabled=0 WHERE pool_id=? AND username=?',(pool_id,username))
                    self.event(db,pool_id,username,None,'password_reset' if action=='reset_passwords' else 'login_enabled',actor)
                results.append({'username':username,'success':True})
            except Exception as exc:
                message=exc.message if isinstance(exc,CheckoutError) else 'Proxmox operation failed; the account remains unavailable. Check the administrator connection and retry.'
                results.append({'username':username,'success':False,'error':message})
        return results

    def disable_logins(self,pool_id,usernames,actor,disable):
        if not isinstance(usernames,list) or not 1<=len(usernames)<=2000:
            raise CheckoutError('Select at least one account.')
        results=[]
        for username in dict.fromkeys(user_id(value) for value in usernames):
            try:
                with self.transaction() as db:
                    self.pool(db,pool_id)
                    row=db.execute('SELECT * FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone()
                    if not row:
                        raise CheckoutError('Publish this account before disabling its login.',409)
                    if row['email'] or row['release_pending']:
                        raise CheckoutError('Release the assigned account in AccessForge first.',409)
                    disable(username)
                    db.execute('UPDATE accounts SET login_disabled=1 WHERE pool_id=? AND username=?',(pool_id,username))
                    self.event(db,pool_id,username,None,'login_disabled',actor)
                results.append({'username':username,'success':True})
            except Exception as exc:
                results.append({'username':username,'success':False,'error':exc.message if isinstance(exc,CheckoutError) else 'Could not disable the Proxmox login.'})
        return results

    def release(self,pool_id,username,actor,disable,*,self_service=False,expected_assignment=None,expected_lease=None):
        username=user_id(username)
        with self.transaction() as db:
            self.pool(db,pool_id)
            row=db.execute('SELECT * FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone()
            if not row or not row['email']:
                return {'username':username,'released':False,'error':'Account is not checked out.'}
            if expected_assignment is not None and (row['email'],row['assigned_at']) != expected_assignment:
                raise CheckoutError('This account assignment changed. Sign in again before relinquishing.',409)
            if expected_lease is not None and row['lease_id'] != expected_lease:
                raise CheckoutError('This account assignment changed. Sign in again before relinquishing.',409)
            db.execute('UPDATE accounts SET release_pending=1 WHERE pool_id=? AND username=?',(pool_id,username))
        try:
            with self.transaction() as db:
                latest=db.execute('SELECT * FROM accounts WHERE pool_id=? AND username=?',(pool_id,username)).fetchone()
                if not latest['release_pending'] or latest['lease_id']!=row['lease_id'] or latest['email']!=row['email']:
                    raise CheckoutError('Assignment changed; refresh status.',409)
                disable(username)
                relinquished=time.time()
                db.execute('UPDATE accounts SET email=NULL,assigned_at=NULL,release_pending=0,login_disabled=1,lease_id=NULL,relinquished_at=?,relinquished_ip=? WHERE pool_id=? AND username=?',(relinquished,checkout_client_ip(),pool_id,username))
                db.execute('DELETE FROM challenges WHERE pool_id=? AND email=?',(pool_id,row['email']))
                self.event(db,pool_id,username,row['email'],'relinquished' if self_service else 'released_login_disabled',actor)
            try:
                self.revoke_lease(row['lease_id'])
            except Exception:
                current_app.logger.warning('Could not close all connections for a released checkout lease')
            return {'username':username,'released':True}
        except Exception:
            return {'username':username,'released':False,'error':'Could not disable the login. The account remains reserved; check login-management credentials and retry release.'}
