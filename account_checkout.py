"""AccessForge-owned student checkout and authenticated administration."""
import functools
import hmac
import os
import secrets
import time

import requests
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from checkout_store import CheckoutError, CheckoutStore, checkout_client_ip, decrypt_str, email_address, person_name, send_verification
from spice_bridge import session_owner


def checkout_enabled():
    return os.environ.get('ENABLE_ACCOUNT_CHECKOUT', 'false').strip().lower() in ('1','true','yes','on')


def configured_checkout_cluster(data):
    expected_host=str(current_app.config['CHECKOUT_PROXMOX_HOST']).strip('[]').lower().rstrip('.')
    actual_host=str(data.get('pve_host') or '').strip('[]').lower().rstrip('.')
    return actual_host==expected_host and str(data.get('pve_port') or '')==str(current_app.config['CHECKOUT_PROXMOX_PORT'])


def bind_checkout_session():
    session.pop('checkout_lease',None)
    if not checkout_enabled() or not session.get('pve_ticket') or not configured_checkout_cluster(session):
        return
    try:
        store=CheckoutStore()
        row=store.assigned_account(session.get('pve_user'))
        if row:
            session['checkout_lease']=row['lease_id']
            store.note_session(row['lease_id'],session_owner(session['pve_ticket']))
    except CheckoutError:
        current_app.logger.warning('Checkout assignment unavailable during sign-in')


def bind_checkout_admin(permissions=None):
    """Show management navigation only for a verified admin on the fixed cluster."""
    session.pop('checkout_admin',None)
    if not checkout_enabled() or not session.get('pve_ticket') or not session.get('pve_user') or not configured_checkout_cluster(session):
        return
    try:
        if permissions is None:
            response=requests.get(backend_url()+'/access/permissions',params={'path':'/'},
                                  cookies={'PVEAuthCookie':session['pve_ticket']},timeout=(5,15),
                                  verify=current_app.config['CHECKOUT_PROXMOX_VERIFY'],allow_redirects=False)
            if response.status_code!=200:
                return
            permissions=response.json().get('data',{})
        root=permissions.get('/',{}) if isinstance(permissions,dict) else {}
        if isinstance(root,dict) and 'Sys.Modify' in root:
            session['checkout_admin']=True
    except (requests.RequestException,ValueError,KeyError,TypeError,AttributeError):
        current_app.logger.warning('Could not check checkout management navigation permissions')


def checkout_session_current(data,*,strict=False,authenticating=False):
    if not checkout_enabled():
        return True
    try:
        row=CheckoutStore().account(data.get('pve_user')) if data.get('pve_user') else None
        if not configured_checkout_cluster(data):
            # A different host spelling must not bypass a managed account's gate.
            managed=row and (row['eligible'] or row['email'] or row['login_disabled'])
            return not data.get('checkout_lease') and not managed
        if row and (row['login_disabled'] or row['release_pending'] or (row['eligible'] and not row['email'])):
            return False
        if not data.get('checkout_lease'):
            return authenticating or not (row and row['email'])
        return bool(row and row['email'] and row['lease_id']==data['checkout_lease'])
    except CheckoutError as exc:
        if strict and exc.status!=400:
            raise
        return exc.status==400 and not data.get('checkout_lease')


def backend_url():
    host=current_app.config['CHECKOUT_PROXMOX_HOST'].strip('[]')
    host=f'[{host}]' if ':' in host else host
    return f'https://{host}:{current_app.config["CHECKOUT_PROXMOX_PORT"]}/api2/json'


def authenticate_admin():
    """Validate against this server's configured cluster, never a caller-supplied host."""
    authorization=request.authorization
    principal={'ticket':session.get('pve_ticket'),'csrf':session.get('pve_csrf'),'username':session.get('pve_user')}
    verify=current_app.config['CHECKOUT_PROXMOX_VERIFY']
    admin_limit=None
    phase='administrator authentication'
    def unavailable(message,exc):
        current_app.logger.warning('Checkout %s failed on configured Proxmox %s:%s (%s)',phase,
                                   current_app.config['CHECKOUT_PROXMOX_HOST'],current_app.config['CHECKOUT_PROXMOX_PORT'],type(exc).__name__)
        raise CheckoutError(message,502) from None
    try:
        if authorization and authorization.type.lower()=='basic':
            username,password=(authorization.username or '').strip(),authorization.password
            if not username or not password or len(username)>128 or len(password)>1024:
                raise CheckoutError('An AccessForge administrator account is required.',401)
            if '@' not in username:
                username+='@pam'
            admin_limit='admin:'+str(request.remote_addr)+':'+username
            CheckoutStore().limit(admin_limit,20,300)
            response=requests.post(backend_url()+'/access/ticket',data={'username':username,'password':password},timeout=(5,15),verify=verify,allow_redirects=False)
            if response.status_code>=500:
                current_app.logger.warning('Checkout administrator authentication upstream failure (HTTP %s)',response.status_code)
                raise CheckoutError('The configured Proxmox API returned a server error during administrator authentication.',502)
            if response.status_code != 200:
                current_app.logger.warning('Checkout administrator login rejected for %s on %s (HTTP %s)',username,current_app.config['CHECKOUT_PROXMOX_HOST'],response.status_code)
                raise CheckoutError('Administrator authentication failed.',401)
            data=response.json()['data']
            if data.get('NeedTFA') or str(data.get('ticket','')).startswith('PVE:!tfa!'):
                current_app.logger.warning('Checkout administrator login requires interactive MFA for %s',username)
                raise CheckoutError('Use an administrator account supported by the password login integration.',401)
            principal={'username':data.get('username') or username,'ticket':data['ticket'],'csrf':data['CSRFPreventionToken'],'password':password}
        if not principal['ticket'] or not principal['username']:
            raise CheckoutError('Sign in with an administrator account.',401)
        phase='administrator permission lookup'
        response=requests.get(backend_url()+'/access/permissions',params={'path':'/'},cookies={'PVEAuthCookie':principal['ticket']},timeout=(5,15),verify=verify,allow_redirects=False)
        if response.status_code>=500:
            current_app.logger.warning('Checkout administrator permissions upstream failure (HTTP %s)',response.status_code)
            raise CheckoutError('The configured Proxmox API returned a server error while checking administrator permissions.',502)
        if response.status_code!=200:
            current_app.logger.warning('Checkout administrator permission lookup failed for %s (HTTP %s)',principal['username'],response.status_code)
            raise CheckoutError('Administrator session expired. Sign in again.',401)
        permissions=response.json().get('data',{}).get('/',{})
        if not isinstance(permissions,dict) or 'Sys.Modify' not in permissions:
            current_app.logger.warning('Checkout administrator %s lacks Sys.Modify on / in %s',principal['username'],current_app.config['CHECKOUT_PROXMOX_HOST'])
            raise CheckoutError('Checkout administration requires Sys.Modify on / in the configured Proxmox cluster.',403)
        if admin_limit:
            with CheckoutStore().transaction() as db:
                db.execute('DELETE FROM rate_limits WHERE key=?',(admin_limit,))
        return principal
    except requests.exceptions.SSLError as exc:
        unavailable("AccessForge could not verify the configured Proxmox API's TLS certificate. Check VERIFY_SSL and the Proxmox certificate trust on AccessForge." if verify else
                    'AccessForge could not establish TLS with the configured Proxmox API. Check PROXMOX_HOST, PROXMOX_PORT, and its TLS configuration.',exc)
    except requests.Timeout as exc:
        unavailable('AccessForge timed out contacting its configured Proxmox API. Check PROXMOX_HOST, PROXMOX_PORT, and network access from the AccessForge container.',exc)
    except requests.ConnectionError as exc:
        unavailable('AccessForge could not connect to its configured Proxmox API. Check PROXMOX_HOST, PROXMOX_PORT, and container DNS/network access.',exc)
    except requests.exceptions.JSONDecodeError as exc:
        unavailable('AccessForge received an invalid Proxmox API response while validating the administrator. Check PROXMOX_HOST, PROXMOX_PORT, and reverse-proxy routing.',exc)
    except requests.RequestException as exc:
        unavailable('AccessForge could not request its configured Proxmox API. Check PROXMOX_HOST and PROXMOX_PORT.',exc)
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        unavailable('AccessForge received an invalid Proxmox API response while validating the administrator. Check PROXMOX_HOST, PROXMOX_PORT, and reverse-proxy routing.',exc)


def set_account_password(principal,username,password,confirmation=''):
    # Proxmox's dedicated password endpoint; existing API tokens cannot change user passwords.
    payload={'userid':username,'password':password}
    confirmation=principal.get('password') or confirmation
    if confirmation:
        payload['confirmation-password']=confirmation
    response=requests.put(backend_url()+'/access/password',data=payload,cookies={'PVEAuthCookie':principal['ticket']},
                          headers={'CSRFPreventionToken':principal['csrf']},timeout=(5,25),
                          verify=current_app.config['CHECKOUT_PROXMOX_VERIFY'],allow_redirects=False)
    if response.status_code!=200:
        raise CheckoutError('Proxmox password reset failed.',502)


def register_account_checkout(app, *, host=None, port=None, realm=None, verify=None, revoke_sessions=None):
    app.config.setdefault('CHECKOUT_PROXMOX_HOST',host or os.environ.get('PROXMOX_HOST','127.0.0.1'))
    app.config.setdefault('CHECKOUT_PROXMOX_PORT',int(port or os.environ.get('PROXMOX_PORT','8006')))
    app.config.setdefault('CHECKOUT_PROXMOX_REALM',realm or os.environ.get('PROXMOX_REALM','pam'))
    app.config.setdefault('CHECKOUT_PROXMOX_VERIFY',verify if verify is not None else os.environ.get('VERIFY_SSL','false').lower()=='true')
    if revoke_sessions is not None:
        app.extensions['checkout_revoke_sessions']=revoke_sessions
    bp=Blueprint('checkout',__name__)

    @app.context_processor
    def checkout_context():
        return {'checkout_enabled':checkout_enabled()}

    @app.get('/api/internal/proxmox-access')
    def proxmox_proxy_access():
        """Nginx auth_request gate for its raw Proxmox UI/API locations."""
        if not checkout_enabled():
            return '',204
        ticket=session.get('pve_ticket')
        supplied=request.cookies.getlist('PVEAuthCookie')
        # Bind the upstream identity to the signed AccessForge session. Neither a
        # different PVE cookie nor an Authorization token may bypass its lease.
        if not ticket or not supplied or not configured_checkout_cluster(session) or request.headers.get('Authorization'):
            return '',403
        if any(not hmac.compare_digest(ticket.encode(),value.encode()) for value in supplied):
            return '',403
        if not checkout_session_current(session):
            return '',403
        return '',204

    @bp.errorhandler(CheckoutError)
    def error(exc):
        return jsonify(error=exc.message),exc.status

    @bp.after_request
    def private(response):
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @bp.before_request
    def enabled():
        if not checkout_enabled():
            raise CheckoutError('Account checkout is disabled on this AccessForge server.',404)

    def body(maximum=1024*1024):
        if request.content_length is not None and request.content_length>maximum:
            raise CheckoutError('Request is too large.',413)
        data=request.get_json(silent=True)
        if not isinstance(data,dict):
            raise CheckoutError('A JSON object is required.')
        return data

    def csrf(kind):
        expected=session.get(kind)
        supplied=request.headers.get('X-Checkout-CSRF','')
        if not expected or not hmac.compare_digest(expected.encode(),supplied.encode()) or request.headers.get('Origin')!=f'{request.scheme}://{request.host}':
            raise CheckoutError('Reload this page and try again.',403)

    def admin(fn):
        @functools.wraps(fn)
        def wrapped(*args,**kwargs):
            if request.method not in ('GET','HEAD'):
                if request.authorization and request.authorization.type.lower()=='basic':
                    if request.headers.get('Origin') and request.headers['Origin']!=f'{request.scheme}://{request.host}':
                        raise CheckoutError('Invalid request origin.',403)
                    body()
                else:
                    csrf('checkout_admin_csrf')
            return fn(authenticate_admin(),*args,**kwargs)
        return wrapped

    @app.get('/checkout')
    def account_checkout():
        enabled=checkout_enabled()
        token=session.setdefault('checkout_csrf',secrets.token_urlsafe(32)) if enabled else ''
        session.setdefault('checkout_client',secrets.token_urlsafe(32))
        response=app.make_response(render_template('account_checkout.html',enabled=enabled,csrf=token))
        response.headers['Cache-Control']='no-store'
        return response if enabled else (response,503)

    @bp.get('/admin/checkouts')
    def checkout_admin_page():
        if not session.get('pve_ticket'):
            return redirect(url_for('login',next='/admin/checkouts'))
        try:
            authenticate_admin()
        except CheckoutError as exc:
            session.pop('checkout_admin',None)
            return render_template('checkout_admin.html',error=exc.message),exc.status
        session['checkout_admin']=True
        token=session.setdefault('checkout_admin_csrf',secrets.token_urlsafe(32))
        return render_template('checkout_admin.html',csrf=token)

    @bp.get('/api/admin/checkouts/status')
    @admin
    def admin_status(principal):
        return jsonify(service='AccessForge',checkout_api=1,administrator=principal['username'],
                       capabilities={'account_setup':True,'assignment_login_control':True,'checkout_login_scope':'accessforge'},
                       cluster={'host':app.config['CHECKOUT_PROXMOX_HOST'],'port':app.config['CHECKOUT_PROXMOX_PORT']},
                       smtp_configured=bool(os.environ.get('CHECKOUT_SMTP_HOST') and os.environ.get('CHECKOUT_SMTP_FROM')))

    @bp.get('/api/admin/checkouts/pools')
    @admin
    def pools(principal):
        return jsonify(pools=CheckoutStore().summaries())

    @bp.route('/api/admin/checkouts/pools/<pool_id>',methods=['GET','PUT','PATCH','POST'])
    @admin
    def pool_action(principal,pool_id):
        store=CheckoutStore()
        if request.method=='GET':
            return jsonify(store.summary(pool_id))
        data=body()
        if request.method=='PUT':
            from checkout_store import user_id
            entries=data.get('accounts')
            if isinstance(entries,list) and any(isinstance(entry,dict) and user_id(entry.get('username'))==principal['username'] for entry in entries):
                raise CheckoutError('Do not publish the administrator account used for login management.',409)
            store.publish(pool_id,data,principal['username'])
            return jsonify(store.summary(pool_id))
        if request.method=='PATCH':
            return jsonify(store.configure(pool_id,data,principal['username']))
        action=data.get('action')
        if action=='save_login_manager':
            raise CheckoutError('Administrator passwords are no longer saved for checkout login management. Checkout access is controlled within AccessForge; no management API token is required.')
        if action=='release':
            usernames=data.get('usernames')
            if not isinstance(usernames,list) or not 1<=len(usernames)<=2000:
                raise CheckoutError('Select at least one account.')
            results=[store.release(pool_id,username,principal['username']) for username in dict.fromkeys(usernames)]
            return jsonify(checkout=store.summary(pool_id),results=results)
        if action=='disable_logins':
            results=store.disable_logins(pool_id,data.get('usernames'),principal['username'])
            return jsonify(checkout=store.summary(pool_id),results=results)
        if action in ('reset_passwords','reenable_logins','enable_accounts'):
            def apply(user,password):
                set_account_password(principal,user,password)
            results=store.prepare_accounts(pool_id,data,principal['username'],apply)
            return jsonify(checkout=store.summary(pool_id),results=results)
        return jsonify(checkout=store.account_action(pool_id,data,principal['username']))

    @bp.get('/api/account-checkout/session')
    def current_checkout():
        if not session.get('pve_ticket') or not session.get('pve_user'):
            raise CheckoutError('Sign in to manage your checked-out account.',401)
        if not configured_checkout_cluster(session):
            return jsonify(assigned=False)
        try:
            row=CheckoutStore().assigned_account(session['pve_user'])
        except CheckoutError as exc:
            if exc.status==400:
                return jsonify(assigned=False)
            raise
        if not row:
            return jsonify(assigned=False)
        token=session.setdefault('checkout_relinquish_csrf',secrets.token_urlsafe(32))
        return jsonify(assigned=True,csrf=token,requires_signin=session.get('checkout_lease')!=row['lease_id'])

    @bp.post('/api/account-checkout/relinquish')
    def relinquish():
        csrf('checkout_relinquish_csrf')
        data=body(8192)
        if data.get('confirmed') is not True:
            raise CheckoutError('Confirm that you will lose all your work and access before relinquishing this account.')
        if not session.get('pve_ticket') or not session.get('pve_user') or not configured_checkout_cluster(session):
            raise CheckoutError('Sign in to your assigned account before relinquishing it.',401)
        store=CheckoutStore()
        row=store.assigned_account(session['pve_user'])
        lease=session.get('checkout_lease')
        if not row or not lease or row['lease_id']!=lease:
            raise CheckoutError('This account assignment changed. Sign in again before relinquishing.',409)
        # Authenticate the signed-in user against this server's fixed cluster before any mutation.
        try:
            response=requests.post(backend_url()+'/access/ticket',data={'username':session['pve_user'],'password':session['pve_ticket']},
                                   timeout=(5,15),verify=app.config['CHECKOUT_PROXMOX_VERIFY'],allow_redirects=False)
            if response.status_code!=200:
                raise CheckoutError('Your session expired. Sign in again before relinquishing.',401)
            verified=response.json()['data']
            if verified.get('NeedTFA') or verified.get('username')!=session['pve_user'] or not verified.get('ticket') or not verified.get('CSRFPreventionToken'):
                raise CheckoutError('Could not validate the account session. Sign in again.',401)
        except (requests.RequestException,ValueError,KeyError,TypeError):
            raise CheckoutError('Could not validate your session. Try again later.',502) from None
        result=store.release(row['pool_id'],row['username'],row['username'],self_service=True,expected_lease=lease)
        if not result['released']:
            raise CheckoutError('Could not relinquish your account. It remains reserved; try again or contact your instructor.',503)
        host=session.get('pve_host')
        ticket=session['pve_ticket']
        revoke=app.extensions.get('checkout_revoke_sessions')
        if revoke:
            try:
                revoke(session_owner(ticket))
            except Exception:
                app.logger.warning('Could not close the relinquished account console connection')
        session.clear()
        response=jsonify(relinquished=True,redirect=url_for('login'))
        for cookie in ('PVEAuthCookie','CSRFPreventionToken'):
            response.delete_cookie(cookie,path='/')
            if host:
                response.delete_cookie(cookie,path='/',domain=host)
        return response

    @bp.post('/api/account-checkout/<action>')
    def student_request(action):
        csrf('checkout_csrf')
        data=body(8192)
        store=CheckoutStore()
        identity=store.digest(str(checkout_client_ip())+':'+str(session.get('checkout_client','')))
        store.limit('student:'+identity,30,300)
        if action=='start':
            email=email_address(data.get('email'))
            name=person_name(data.get('name'))
            secret=data.get('secret')
            if not isinstance(secret,str) or not 8<=len(secret)<=256:
                raise CheckoutError('Invalid lab secret.',403)
            with store.transaction() as db:
                pool=db.execute('SELECT * FROM pools WHERE enabled=1 AND secret_lookup=?',(store.digest('class:'+secret),)).fetchone()
                from werkzeug.security import check_password_hash
                if not pool or not check_password_hash(pool['secret_hash'],secret):
                    raise CheckoutError('Invalid lab secret or checkout is closed.',403)
                existing=db.execute('SELECT 1 FROM accounts WHERE pool_id=? AND email=?',(pool['id'],email)).fetchone()
                store.check_policy(db,pool,email,new_checkout=not existing)
                if not pool['verify_email']:
                    return jsonify(store.allocate(db,pool['id'],email,name=name))
                pool_id,version=pool['id'],pool['secret_hash']
            store.limit('email:'+store.digest(pool_id+':'+email),1,60)
            challenge=secrets.token_urlsafe(32)
            code=f'{secrets.randbelow(100000000):08d}'
            with store.transaction() as db:
                latest=store.pool(db,pool_id)
                if not latest['enabled'] or latest['secret_hash']!=version:
                    raise CheckoutError('Checkout settings changed. Start again.',409)
                existing=db.execute('SELECT 1 FROM accounts WHERE pool_id=? AND email=?',(pool_id,email)).fetchone()
                store.check_policy(db,latest,email,new_checkout=not existing)
                db.execute('DELETE FROM challenges WHERE expires<? OR (pool_id=? AND email=?)',(time.time(),pool_id,email))
                db.execute('INSERT INTO challenges(id,pool_id,email,name,code_hash,expires) VALUES(?,?,?,?,?,?)',(challenge,pool_id,email,name,store.digest(challenge+':'+code),time.time()+600))
            try:
                send_verification(email,code,pool['name'])
            except CheckoutError:
                with store.transaction() as db:
                    db.execute('DELETE FROM challenges WHERE id=?',(challenge,))
                raise
            return jsonify(status='verification_required',challenge=challenge)
        if action=='verify':
            challenge,code=data.get('challenge'),data.get('code')
            if not isinstance(challenge,str) or len(challenge)>128 or not isinstance(code,str) or len(code)>16:
                raise CheckoutError('Invalid verification request.')
            failure=None
            with store.transaction() as db:
                row=db.execute('SELECT * FROM challenges WHERE id=?',(challenge,)).fetchone()
                if not row or row['expires']<time.time() or row['attempts']>=6:
                    failure=CheckoutError('Verification expired. Start again.',403)
                elif not hmac.compare_digest(row['code_hash'],store.digest(challenge+':'+code)):
                    db.execute('UPDATE challenges SET attempts=attempts+1 WHERE id=?',(challenge,))
                    failure=CheckoutError('Incorrect verification code.',403)
                elif not store.pool(db,row['pool_id'])['enabled']:
                    failure=CheckoutError('Checkout is closed.',403)
                else:
                    result=store.allocate(db,row['pool_id'],row['email'],name=row['name'])
                    db.execute('DELETE FROM challenges WHERE id=?',(challenge,))
            if failure:
                raise failure
            return jsonify(result)
        raise CheckoutError('Not found.',404)

    app.register_blueprint(bp)
