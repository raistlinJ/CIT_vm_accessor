import base64
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import requests

import main
import account_checkout
from checkout_store import CheckoutStore

REAL_ADMIN_AUTH=account_checkout.authenticate_admin
ADMIN={'username':'root@pam','ticket':'admin-ticket','csrf':'admin-csrf','password':'admin-password'}
BASIC={'Authorization':'Basic '+base64.b64encode(b'root@pam:admin-password').decode()}
POOL='/api/admin/checkouts/pools/lab'


@pytest.fixture
def checkout(tmp_path,monkeypatch):
    monkeypatch.setenv('ENABLE_ACCOUNT_CHECKOUT','true')
    monkeypatch.delenv('CHECKOUT_SMTP_HOST',raising=False)
    monkeypatch.delenv('CHECKOUT_SMTP_FROM',raising=False)
    monkeypatch.setitem(main.app.config,'CHECKOUT_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(account_checkout,'authenticate_admin',lambda:dict(ADMIN))
    with main.app.test_client() as client:
        yield client


def publish(client,accounts=None,pool=POOL):
    return client.put(pool,headers=BASIC,json={'name':'Networking','accounts':accounts or [
        {'username':'student01','password':'original-password1'},
        {'username':'student02','password':'original-password2'}]})


def setup_pool(client,verify=False,names=None):
    assert publish(client).status_code==200
    assert client.patch(POOL,headers=BASIC,json={'secret':'class-secret','enabled':True,'verify_email':verify}).status_code==200
    assert client.post(POOL,headers=BASIC,json={'action':'enable','usernames':names or ['student01','student02']}).status_code==200


def headers(client):
    assert client.get('/checkout').status_code==200
    with client.session_transaction() as session:
        token=session['checkout_csrf']
    return {'Origin':'http://localhost','X-Checkout-CSRF':token}


def start(client,email='one@example.edu',secret='class-secret',name='Test Student'):
    return client.post('/api/account-checkout/start',headers=headers(client),json={'name':name,'email':email,'secret':secret})


def verify(client,challenge,code):
    return client.post('/api/account-checkout/verify',headers=headers(client),json={'challenge':challenge,'code':code})


def reply(data,status=200):
    import json
    result=requests.Response();result.status_code=status;result._content=json.dumps({'data':data}).encode()
    return result


def test_disabled_feature_hides_links_and_admin_api(checkout,monkeypatch):
    monkeypatch.setenv('ENABLE_ACCOUNT_CHECKOUT','false')
    assert checkout.get('/checkout').status_code==503
    assert checkout.get('/api/admin/checkouts/status',headers=BASIC).status_code==404
    assert b'Get lab access</a>' not in checkout.get('/login').data


def test_student_checkout_works_locally_without_sce_or_proxmox(checkout):
    setup_pool(checkout,names=['student02'])
    with patch('account_checkout.requests.post',side_effect=AssertionError('No upstream calls')),patch('account_checkout.requests.get',side_effect=AssertionError('No upstream calls')):
        result=start(checkout,'One@Example.EDU')
        assert result.status_code==200
        assert result.get_json()['username']=='student02@pve'
        assert result.get_json()['email']=='one@example.edu'
        assert start(checkout).get_json()['password']==result.get_json()['password']
        assert start(checkout,'two@example.edu').status_code==409
    assert result.headers['Cache-Control']=='no-store'
    with checkout.session_transaction() as session:
        assert 'original-password' not in str(dict(session)) and 'class-secret' not in str(dict(session))


def test_atomic_last_account_allocation(checkout):
    setup_pool(checkout,names=['student01'])
    def claim(i):
        with main.app.test_client() as client:
            return start(client,f'student{i}@example.edu').status_code
    with ThreadPoolExecutor(max_workers=6) as executor:
        codes=list(executor.map(claim,range(6)))
    assert codes.count(200)==1 and codes.count(409)==5


def test_admin_configuration_and_assignments_are_not_public(checkout,monkeypatch):
    setup_pool(checkout)
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    assert checkout.get('/api/admin/checkouts/pools').status_code==401
    assert checkout.put(POOL,json={}).status_code==403
    assert checkout.get('/admin/checkouts').status_code==302
    with patch('account_checkout.requests.post',return_value=reply({'ticket':'ticket','CSRFPreventionToken':'csrf','username':'student@pve'})),patch('account_checkout.requests.get',return_value=reply({'/':{'VM.Console':1}})):
        assert checkout.get('/api/admin/checkouts/pools',headers=BASIC).status_code==403


def test_admin_is_checked_on_configured_cluster_and_permissions_presence(checkout,monkeypatch):
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    with patch('account_checkout.requests.post',return_value=reply({'ticket':'ticket','CSRFPreventionToken':'csrf','username':'root@pam'})) as post,patch('account_checkout.requests.get',return_value=reply({'/':{'Sys.Modify':0}})) as get:
        result=checkout.get('/api/admin/checkouts/status',headers=BASIC)
    assert result.status_code==200
    assert post.call_args.args[0].endswith('/api2/json/access/ticket')
    assert get.call_args.kwargs['params']=={'path':'/'}
    assert result.get_json()['administrator']=='root@pam'
    assert 'password' not in result.get_data(as_text=True)


@pytest.mark.parametrize('entered,expected',[
    ('jcacosta','jcacosta@pam'),(' jcacosta ','jcacosta@pam'),
    ('jcacosta@pve','jcacosta@pve'),('jcacosta@ldap','jcacosta@ldap'),
])
def test_admin_bare_usernames_default_to_pam_independently_of_student_realm(checkout,monkeypatch,entered,expected):
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    monkeypatch.setitem(main.app.config,'CHECKOUT_PROXMOX_REALM','pve')
    auth={'Authorization':'Basic '+base64.b64encode((entered+':admin-password').encode()).decode()}
    with patch('account_checkout.requests.post',return_value=reply({'ticket':'ticket','CSRFPreventionToken':'csrf','username':expected})) as post,patch('account_checkout.requests.get',return_value=reply({'/':{'Sys.Modify':1}})):
        response=checkout.get('/api/admin/checkouts/status',headers=auth)
    assert response.status_code==200
    assert post.call_args.kwargs['data']['username']==expected
    assert response.get_json()['administrator']==expected


def test_cookie_administration_requires_csrf_and_origin(checkout):
    setup_pool(checkout)
    with checkout.session_transaction() as session:
        session.update(pve_ticket='ticket',pve_user='root@pam',pve_csrf='csrf')
    page=checkout.get('/admin/checkouts')
    assert page.status_code==200
    with checkout.session_transaction() as session:
        token=session['checkout_admin_csrf']
    assert checkout.patch(POOL,json={'enabled':False}).status_code==403
    assert checkout.patch(POOL,json={'enabled':False},headers={'Origin':'https://attacker.example','X-Checkout-CSRF':token}).status_code==403
    assert checkout.patch(POOL,json={'enabled':False},headers={'Origin':'http://localhost','X-Checkout-CSRF':token}).status_code==200


def test_student_csrf_and_size_validation(checkout):
    setup_pool(checkout)
    assert checkout.post('/api/account-checkout/start',json={}).status_code==403
    h=headers(checkout)
    assert checkout.post('/api/account-checkout/start',json={},headers={**h,'Origin':'https://attacker.example'}).status_code==403
    assert checkout.post('/api/account-checkout/start',data='x'*9000,content_type='application/json',headers=h).status_code==413


def test_verification_gates_allocation_and_is_single_use(checkout):
    setup_pool(checkout,verify=True)
    with patch('account_checkout.send_verification') as send:
        response=start(checkout)
    assert 'password' not in response.get_json()
    challenge=response.get_json()['challenge'];code=send.call_args.args[1]
    assert len(code)==8
    assert all(not a['email'] for a in checkout.get(POOL,headers=BASIC).get_json()['accounts'].values())
    assert verify(checkout,challenge,'wrong').status_code==403
    assert verify(checkout,challenge,code).get_json()['username']=='student01@pve'
    assert verify(checkout,challenge,code).status_code==403


def test_verification_expiry_retry_limit_and_resend_limit(checkout):
    setup_pool(checkout,verify=True)
    with patch('account_checkout.send_verification') as send:
        challenge=start(checkout).get_json()['challenge'];code=send.call_args.args[1]
        assert start(checkout).status_code==429
    for _ in range(6):
        assert verify(checkout,challenge,'wrong').status_code==403
    assert verify(checkout,challenge,code).status_code==403
    with patch('account_checkout.send_verification') as send:
        challenge=start(checkout,'two@example.edu').get_json()['challenge'];code=send.call_args.args[1]
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            db.execute('UPDATE challenges SET expires=0 WHERE id=?',(challenge,))
    assert verify(checkout,challenge,code).status_code==403


def test_missing_smtp_fails_closed(checkout):
    setup_pool(checkout,verify=True)
    assert start(checkout).status_code==503
    assert all(not a['email'] for a in checkout.get(POOL,headers=BASIC).get_json()['accounts'].values())


def test_settings_changes_invalidate_pending_verification(checkout):
    setup_pool(checkout,verify=True)
    with patch('account_checkout.send_verification') as send:
        challenge=start(checkout).get_json()['challenge'];code=send.call_args.args[1]
    assert checkout.patch(POOL,headers=BASIC,json={'secret':'different-secret'}).status_code==200
    assert verify(checkout,challenge,code).status_code==403
    assert start(checkout).status_code==403
    checkout.patch(POOL,headers=BASIC,json={'enabled':False})
    assert start(checkout,secret='different-secret').status_code==403


def test_account_selection_and_manual_assignment_do_not_overwrite(checkout):
    setup_pool(checkout)
    assert checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student02'],'email':'one@example.edu'}).status_code==200
    assert checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student02'],'email':'two@example.edu'}).status_code==409
    assert checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student01'],'email':'one@example.edu'}).status_code==409
    assert start(checkout).get_json()['username']=='student02@pve'


def test_release_disables_login_without_changing_password_and_republish_keeps_disabled(checkout):
    setup_pool(checkout,names=['student01'])
    original=start(checkout).get_json()['password']
    with patch('account_checkout.set_login_enabled') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.get_json()['results'][0]['released']
    assert disable.call_args.args[1:] == ('student01@pve',False)
    reset.assert_not_called()
    assert publish(checkout).status_code==200
    assert start(checkout,'two@example.edu').status_code==409
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['login_disabled'] and not state['password_rotated']
    with main.app.app_context():
        from checkout_store import decrypt_str
        assert decrypt_str(CheckoutStore().account('student01')['password_enc'])==original


def test_failed_disable_is_held_and_retry_does_not_rotate(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.set_login_enabled',side_effect=RuntimeError('private details')):
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert not result.get_json()['results'][0]['released']
    assert start(checkout).status_code==409 and start(checkout,'two@example.edu').status_code==409
    with patch('account_checkout.set_login_enabled') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.get_json()['results'][0]['released']
    assert disable.call_args.args[1:]==('student01@pve',False)
    reset.assert_not_called()


def test_enable_disable_uses_user_configuration_without_password(checkout):
    with main.app.test_request_context():
        with patch('account_checkout.requests.put',return_value=reply(None)) as put:
            account_checkout.set_login_enabled(ADMIN,'student01@pve',False)
    assert put.call_args.args[0].endswith('/api2/json/access/users/student01%40pve')
    assert put.call_args.kwargs['data']=={'enable':0}


def test_published_passwords_encrypted_and_status_never_returns_them(checkout):
    setup_pool(checkout)
    response=checkout.get(POOL,headers=BASIC)
    assert 'original-password' not in response.get_data(as_text=True) and 'class-secret' not in response.get_data(as_text=True)
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert 'original-password' not in db.execute('SELECT password_enc FROM accounts LIMIT 1').fetchone()[0]


def test_conflicting_publication_is_atomic_and_does_not_change_policy(checkout):
    setup_pool(checkout);start(checkout)
    result=publish(checkout,[{'username':'student01','password':'replacement-password'}])
    assert result.status_code==409
    assert start(checkout).get_json()['password']=='original-password1'
    assert publish(checkout,pool='/api/admin/checkouts/pools/other').status_code==409
    assert checkout.get('/api/admin/checkouts/pools/other',headers=BASIC).status_code==404
    assert checkout.get(POOL,headers=BASIC).get_json()['enabled']


def test_legacy_assignments_preserved_on_initial_publication(checkout):
    result=publish(checkout,[{'username':'student01','password':'original-password1','email':'legacy@example.edu','assigned_at':12345}])
    assert result.status_code==200
    state=result.get_json()['accounts']['student01@pve']
    assert state['email']=='legacy@example.edu' and state['assigned_at']==12345
    assert not result.get_json()['enabled'] and not state['eligible']


def test_assigned_accounts_cannot_be_removed(checkout):
    setup_pool(checkout);start(checkout)
    assert checkout.post(POOL,headers=BASIC,json={'action':'remove','usernames':['student01']}).status_code==409
    assert checkout.post(POOL,headers=BASIC,json={'action':'remove','usernames':['student02']}).status_code==200


def test_legacy_republication_does_not_reclaim_disabled_accounts(checkout):
    entry={'username':'student01','password':'original-password1','email':'legacy@example.edu','assigned_at':12345,'pending_release_password':'pending-password1'}
    assert publish(checkout,[entry]).status_code==200
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['releasing']
    with patch('account_checkout.set_login_enabled') as disable,patch('account_checkout.set_account_password') as reset:
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    assert disable.call_args.args[1:]==('student01@pve',False);reset.assert_not_called()
    assert publish(checkout,[entry]).status_code==200
    account=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert account['email'] is None and not account['releasing'] and account['login_disabled']


def student_login(client,username='student01',ticket='student-ticket'):
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':ticket,'CSRFPreventionToken':'student-csrf'})),patch.object(main,'proxmox_get',return_value=reply({'version':'test'})):
        result=client.post('/login',data={'username':username,'password':'original-password1','realm':'pve',
                                        'host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert result.status_code==302
    status=client.get('/api/account-checkout/session')
    assert status.status_code==200
    return {'Origin':'http://localhost','X-Checkout-CSRF':status.get_json().get('csrf','')}


def test_checkout_ip_and_time_only_change_on_new_allocation(checkout):
    setup_pool(checkout)
    response=checkout.post('/api/account-checkout/start',headers=headers(checkout),json={'name':'Test Student','email':'one@example.edu','secret':'class-secret'},environ_overrides={'REMOTE_ADDR':'203.0.113.10'})
    assert response.status_code==200
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['checkout_ip']=='203.0.113.10' and state['checked_out_at']==state['assigned_at']
    recovered=checkout.post('/api/account-checkout/start',headers=headers(checkout),json={'name':'Test Student','email':'one@example.edu','secret':'class-secret'},environ_overrides={'REMOTE_ADDR':'203.0.113.20'})
    assert recovered.status_code==200
    current=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert current['checkout_ip']==state['checkout_ip'] and current['checked_out_at']==state['checked_out_at']


def test_relinquish_releases_only_signed_in_account_preserves_audit_and_signs_out(checkout):
    setup_pool(checkout);start(checkout)
    h=student_login(checkout)
    before=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    with patch('account_checkout.authenticate_manager_password',return_value=ADMIN),patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch('account_checkout.set_login_enabled') as disable:
        response=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True,'username':'student02@pve','pool_id':'untrusted'},environ_overrides={'REMOTE_ADDR':'203.0.113.50'})
    assert response.status_code==200 and response.get_json()['relinquished']
    assert disable.call_args.args==(ADMIN,'student01@pve',False)
    with checkout.session_transaction() as session:
        assert not session.get('pve_ticket')
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['email'] is None and state['assigned_at'] is None
    assert state['checked_out_at']==before['checked_out_at'] and state['checkout_ip']==before['checkout_ip']
    assert state['relinquished_ip']=='203.0.113.50' and state['relinquished_at']>=before['checked_out_at']
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student02@pve']['email'] is None
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            event=db.execute("SELECT * FROM events WHERE action='relinquished'").fetchone()
            assert event['email']=='one@example.edu' and event['ip_address']=='203.0.113.50'
            assert event['actor']=='student01@pve'
    assert any('PVEAuthCookie=' in cookie and 'Max-Age=0' in cookie for cookie in response.headers.getlist('Set-Cookie'))


def test_relinquish_requires_confirmation_csrf_and_live_assignment(checkout):
    setup_pool(checkout);start(checkout)
    h=student_login(checkout)
    with patch('account_checkout.set_login_enabled') as disable,patch('account_checkout.requests.post') as authenticate:
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':False}).status_code==400
        assert checkout.post('/api/account-checkout/relinquish',json={'confirmed':True}).status_code==403
        assert checkout.post('/api/account-checkout/relinquish',headers={**h,'Origin':'https://attacker.example'},json={'confirmed':True}).status_code==403
        disable.assert_not_called();authenticate.assert_not_called()
    with patch('account_checkout.requests.post',return_value=reply({},401)),patch('account_checkout.set_login_enabled') as disable:
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==401
        disable.assert_not_called()
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['email']=='one@example.edu'


def test_failed_relinquish_does_not_log_out_or_mark_completion(checkout):
    setup_pool(checkout);start(checkout);h=student_login(checkout)
    with patch('account_checkout.authenticate_manager_password',return_value=ADMIN),patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch('account_checkout.set_login_enabled',side_effect=RuntimeError('private failure')):
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==503
    with checkout.session_transaction() as session:
        assert session['pve_ticket']=='student-ticket'
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['email']=='one@example.edu' and state['relinquished_at'] is None and state['releasing']


def test_released_lease_invalidates_other_browser_and_console_sessions(checkout,monkeypatch):
    from spice_bridge import session_owner
    setup_pool(checkout);start(checkout);h=student_login(checkout,ticket='student-ticket-a')
    other=main.app.test_client();student_login(other,ticket='student-ticket-b')
    cookie=other.get_cookie(main.app.config['SESSION_COOKIE_NAME']).value
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie)==session_owner('student-ticket-b')
    revoked=[];monkeypatch.setitem(main.app.extensions,'checkout_revoke_sessions',revoked.append)
    with patch('account_checkout.authenticate_manager_password',return_value=ADMIN),patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch('account_checkout.set_login_enabled'):
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==200
    assert session_owner('student-ticket-a') in revoked and session_owner('student-ticket-b') in revoked
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie) is None
    with patch.object(main,'proxmox_get') as proxmox:
        assert other.get('/api/vms').status_code==401
        proxmox.assert_not_called()


def test_old_login_cannot_relinquish_reassigned_account(checkout):
    setup_pool(checkout,names=['student01']);start(checkout);h=student_login(checkout)
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]}).get_json()['results'][0]['success']
    anonymous=main.app.test_client();assert start(anonymous,'new@example.edu').status_code==200
    with patch('account_checkout.requests.post') as authenticate:
        result=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True})
        assert result.status_code==409;authenticate.assert_not_called()
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['email']=='new@example.edu'


def test_new_checkout_retains_previous_relinquishment_and_full_history(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']},environ_overrides={'REMOTE_ADDR':'203.0.113.30'}).get_json()['results'][0]['released']
    old=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]}).get_json()['results'][0]['success']
    assert start(checkout,'new@example.edu').status_code==200
    current=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert current['relinquished_at']==old['relinquished_at'] and current['relinquished_ip']=='203.0.113.30'
    assert current['checked_out_at']>=old['relinquished_at']
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute("SELECT count(*) FROM events WHERE action='assigned'").fetchone()[0]==2


def test_client_ip_ignores_untrusted_and_malformed_proxy_headers(checkout,monkeypatch):
    from checkout_store import checkout_client_ip
    monkeypatch.setitem(main.app.config,'CHECKOUT_TRUSTED_PROXIES','127.0.0.1/32')
    with main.app.test_request_context(environ_overrides={'REMOTE_ADDR':'203.0.113.10'},headers={'X-Real-IP':'198.51.100.99','X-Forwarded-For':'198.51.100.99'}):
        assert checkout_client_ip()=='203.0.113.10'
    with main.app.test_request_context(environ_overrides={'REMOTE_ADDR':'127.0.0.1'},headers={'X-Real-IP':'2001:db8::123','X-Forwarded-For':'198.51.100.99'}):
        assert checkout_client_ip()=='2001:db8::123'
    with main.app.test_request_context(environ_overrides={'REMOTE_ADDR':'127.0.0.1'},headers={'X-Real-IP':'198.51.100.1, 203.0.113.1'}):
        assert checkout_client_ip()=='127.0.0.1'


def test_disabled_account_stays_unavailable_until_explicit_reenable(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    assert start(checkout,'new@example.edu').status_code==409
    assert checkout.post(POOL,headers=BASIC,json={'action':'enable','usernames':['student01']}).status_code==200
    assert start(checkout,'new@example.edu').status_code==409
    with patch('account_checkout.set_login_enabled') as enable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
    assert result.get_json()['results'][0]['success']
    assert enable.call_args.args[1:]==('student01@pve',True)
    reset.assert_not_called()
    assert start(checkout,'new@example.edu').get_json()['password']=='original-password1'


def test_explicit_password_reset_keeps_login_disabled_and_reenable_uses_new_credentials(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.set_login_enabled'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    entries=[{'username':'student01','password':'new-sce-password'}]
    with patch('account_checkout.set_login_enabled') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'reset_passwords','accounts':entries})
    assert result.get_json()['results'][0]['success']
    assert disable.call_args.args[1:]==('student01@pve',False)
    assert reset.call_args.args[1:]==('student01@pve','new-sce-password')
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['login_disabled'] and not state['password_rotated']
    assert start(checkout,'new@example.edu').status_code==409
    with patch('account_checkout.set_login_enabled') as enable:
        stale=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
        assert not stale.get_json()['results'][0]['success'];enable.assert_not_called()
        response=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':entries})
    assert response.get_json()['results'][0]['success']
    assert start(checkout,'new@example.edu').get_json()['password']=='new-sce-password'


def test_reset_and_reenable_reject_assigned_accounts(checkout):
    setup_pool(checkout);start(checkout)
    for action in ('reset_passwords','reenable_logins'):
        with patch('account_checkout.set_login_enabled') as change,patch('account_checkout.set_account_password') as reset:
            result=checkout.post(POOL,headers=BASIC,json={'action':action,'accounts':[{'username':'student01','password':'new-password'}]})
        assert not result.get_json()['results'][0]['success'];change.assert_not_called();reset.assert_not_called()


def test_login_management_password_is_encrypted_and_never_in_status(checkout):
    assert publish(checkout).status_code==200
    status=checkout.get(POOL,headers=BASIC)
    assert status.get_json()['login_management_ready']
    assert 'admin-password' not in status.get_data(as_text=True)
    with main.app.app_context():
        store=CheckoutStore()
        assert store.manager('lab')==('root@pam','admin-password')
        with store.transaction() as db:
            assert db.execute('SELECT manager_password_enc FROM pools WHERE id=?',('lab',)).fetchone()[0]!='admin-password'
    with main.app.test_request_context():
        with patch('account_checkout.authenticate_manager_password',return_value=ADMIN) as authenticate,patch('account_checkout.set_login_enabled') as disable:
            account_checkout.set_login_enabled(account_checkout.login_management_principal('lab'),'student01@pve',False)
        authenticate.assert_called_once_with('root@pam','admin-password')
        assert disable.call_args.args[1:]==('student01@pve',False)


def test_scoped_management_token_does_not_store_publishing_admin_password(checkout,monkeypatch):
    monkeypatch.setenv('CHECKOUT_PROXMOX_API_TOKEN','service@pve!disable=secret')
    assert publish(checkout).status_code==200
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute('SELECT manager_password_enc FROM pools WHERE id=?',('lab',)).fetchone()[0] is None
    with main.app.test_request_context():
        with patch('account_checkout.requests.put',return_value=reply(None)) as put:
            account_checkout.set_login_enabled(account_checkout.login_management_principal('lab'),'student01@pve',False)
    assert put.call_args.kwargs['headers']=={'Authorization':'PVEAPIToken=service@pve!disable=secret'}
    assert put.call_args.kwargs['data']=={'enable':0}


def test_upstream_successful_login_cannot_bypass_local_disabled_state(checkout):
    setup_pool(checkout);start(checkout)
    with patch('account_checkout.set_login_enabled'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'stale-upstream-ticket','CSRFPreventionToken':'csrf'})):
        result=checkout.post('/login',data={'username':'student01','password':'original-password1','realm':'pve','host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert result.status_code==403 and b'account is disabled' in result.data


@pytest.mark.parametrize('action', ['reset_passwords', 'reenable_logins'])
def test_failed_preparation_keeps_login_disabled_and_password_unchanged(checkout,action):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.set_login_enabled'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    password='new-saved-password' if action=='reset_passwords' else 'original-password1'
    target='account_checkout.set_account_password' if action=='reset_passwords' else 'account_checkout.set_login_enabled'
    with patch('account_checkout.set_login_enabled'),patch(target,side_effect=RuntimeError('private upstream details')):
        result=checkout.post(POOL,headers=BASIC,json={'action':action,'accounts':[{'username':'student01','password':password}]})
    assert not result.get_json()['results'][0]['success']
    assert 'private upstream' not in result.get_data(as_text=True)
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['login_disabled']
    with main.app.app_context():
        from checkout_store import decrypt_str
        assert decrypt_str(CheckoutStore().account('student01@pve')['password_enc'])=='original-password1'
    assert start(checkout,'new@example.edu').status_code==409


def test_storage_permission_error_reports_service_unavailable_in_admin_api(checkout):
    with patch('checkout_store.os.makedirs',side_effect=PermissionError('private path')):
        response=checkout.get(POOL,headers=BASIC)
    assert response.status_code==503
    assert 'CHECKOUT_DATA_DIR permissions' in response.get_json()['error']
    assert 'private path' not in response.get_data(as_text=True)


def test_storage_permission_error_is_not_reported_as_invalid_login_or_disabled_account(checkout):
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'ticket','CSRFPreventionToken':'csrf'})),patch('checkout_store.os.makedirs',side_effect=PermissionError('private path')):
        response=checkout.post('/login',data={'username':'root','password':'private-password','realm':'pam',
                                             'host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert response.status_code==503
    assert b'Checkout storage is not writable' in response.data
    assert b'This account is disabled' not in response.data and b'private-password' not in response.data
    with checkout.session_transaction() as session:
        assert not session.get('pve_ticket')


def test_database_open_failure_is_reported_as_storage_error(checkout):
    import sqlite3
    with patch('checkout_store.sqlite3.connect',side_effect=sqlite3.OperationalError('private details')):
        response=checkout.get(POOL,headers=BASIC)
    assert response.status_code==503
    assert 'CHECKOUT_DATA_DIR permissions' in response.get_json()['error']
    assert 'private details' not in response.get_data(as_text=True)


def test_checkout_saves_normalized_name_and_recovery_keeps_original_identity(checkout):
    setup_pool(checkout)
    response=start(checkout,name='  Zoë   García  ')
    assert response.status_code==200 and response.get_json()['name']=='Zoë García'
    assert start(checkout,name='Changed Name').get_json()['name']=='Zoë García'
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['name']=='Zoë García'
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute("SELECT name FROM events WHERE action='assigned'").fetchone()['name']=='Zoë García'
    with checkout.session_transaction() as session:
        assert 'Zoë' not in str(dict(session))


@pytest.mark.parametrize('name',[None,'','   ',123,'x'*121,'Name\nInjected'])
def test_checkout_requires_a_bounded_name_before_assignment(checkout,name):
    setup_pool(checkout)
    assert start(checkout,name=name).status_code==400
    assert not any(account['email'] for account in checkout.get(POOL,headers=BASIC).get_json()['accounts'].values())


def test_email_verification_uses_the_name_from_the_original_challenge(checkout):
    setup_pool(checkout,verify=True)
    with patch('account_checkout.send_verification') as send:
        response=start(checkout,name='Jordan Lee')
    challenge=response.get_json()['challenge'];code=send.call_args.args[1]
    result=checkout.post('/api/account-checkout/verify',headers=headers(checkout),json={'challenge':challenge,'code':code,'name':'Untrusted replacement'})
    assert result.status_code==200 and result.get_json()['name']=='Jordan Lee'
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['name']=='Jordan Lee'


def test_legacy_assignment_can_gain_a_name_without_reallocating(checkout):
    setup_pool(checkout)
    checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student01'],'email':'legacy@example.edu'})
    before=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert before['name'] is None
    result=start(checkout,'legacy@example.edu',name='Legacy Student')
    after=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert result.get_json()['name']=='Legacy Student'
    assert after['assigned_at']==before['assigned_at'] and after['checked_out_at']==before['checked_out_at']


def test_release_clears_current_name_and_keeps_it_in_the_audit_history(checkout):
    setup_pool(checkout);start(checkout,name='First Student')
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    account=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert account['name'] is None and account['email'] is None
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute("SELECT name FROM events WHERE action='released_login_disabled'").fetchone()['name']=='First Student'
    with patch('account_checkout.set_login_enabled'):
        checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
    assert start(checkout,'new@example.edu',name='Next Student').get_json()['name']=='Next Student'
