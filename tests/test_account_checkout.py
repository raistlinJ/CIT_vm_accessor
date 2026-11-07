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
    monkeypatch.delenv('CHECKOUT_PROXMOX_API_TOKEN',raising=False)
    monkeypatch.delenv('CHECKOUT_SMTP_HOST',raising=False)
    monkeypatch.delenv('CHECKOUT_SMTP_FROM',raising=False)
    monkeypatch.setitem(main.app.config,'CHECKOUT_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(account_checkout,'authenticate_admin',lambda:dict(ADMIN))
    def deny_proxmox_write(*args,**kwargs):
        raise AssertionError('Unexpected Proxmox write')
    monkeypatch.setattr(account_checkout.requests,'put',deny_proxmox_write)
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
    assert b'id="login-checkout-link"' not in checkout.get('/login').data


def test_student_checkout_needs_no_sce_or_proxmox_calls(checkout):
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


@pytest.mark.parametrize('phase',['ticket','permissions'])
@pytest.mark.parametrize('failure,expected',[
    (requests.exceptions.SSLError('private-password'),'Proxmox API\'s TLS certificate'),
    (requests.Timeout('private-password'),'timed out contacting'),
    (requests.ConnectionError('private-password'),'container DNS/network access'),
    (requests.RequestException('private-password'),'could not request'),
    (requests.exceptions.JSONDecodeError('private-password','private-document',0),'invalid Proxmox API response'),
    (ValueError('private-password'),'invalid Proxmox API response'),
])
def test_admin_backend_failures_identify_the_proxmox_connection_without_leaking_details(checkout,monkeypatch,caplog,phase,failure,expected):
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    monkeypatch.setitem(main.app.config,'CHECKOUT_PROXMOX_VERIFY',True)
    ticket=reply({'username':'root@pam','ticket':'ticket','CSRFPreventionToken':'csrf'})
    with patch('account_checkout.requests.post',side_effect=failure if phase=='ticket' else None,return_value=ticket),patch('account_checkout.requests.get',side_effect=failure if phase=='permissions' else None,return_value=reply({'/':{'Sys.Modify':1}})):
        result=checkout.get('/api/admin/checkouts/status',headers=BASIC)
    assert result.status_code==502 and expected in result.json['error']
    assert 'private-password' not in result.get_data(as_text=True) and 'private-password' not in caplog.text
    assert 'configured Proxmox' in caplog.text


def test_backend_tls_negotiation_failure_does_not_claim_certificate_verification_when_disabled(checkout,monkeypatch):
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    monkeypatch.setitem(main.app.config,'CHECKOUT_PROXMOX_VERIFY',False)
    with patch('account_checkout.requests.post',side_effect=requests.exceptions.SSLError('private-details')):
        result=checkout.get('/api/admin/checkouts/status',headers=BASIC)
    assert result.status_code==502 and 'could not establish TLS' in result.json['error']
    assert 'could not verify' not in result.json['error']


@pytest.mark.parametrize('phase',['ticket','permissions'])
def test_proxmox_server_errors_are_not_reported_as_bad_admin_credentials(checkout,monkeypatch,phase):
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    ticket=reply({'username':'root@pam','ticket':'ticket','CSRFPreventionToken':'csrf'})
    with patch('account_checkout.requests.post',return_value=reply(None,502) if phase=='ticket' else ticket),patch('account_checkout.requests.get',return_value=reply(None,503)):
        result=checkout.get('/api/admin/checkouts/status',headers=BASIC)
    assert result.status_code==502 and 'Proxmox API returned a server error' in result.json['error']


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


def test_release_disables_accessforge_without_changing_proxmox_or_password(checkout):
    setup_pool(checkout,names=['student01'])
    original=start(checkout).get_json()['password']
    with patch('account_checkout.requests.put') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.get_json()['results'][0]['released']
    disable.assert_not_called()
    reset.assert_not_called()
    assert publish(checkout).status_code==200
    assert start(checkout,'two@example.edu').status_code==409
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['login_disabled'] and not state['password_rotated']
    with main.app.app_context():
        from checkout_store import decrypt_str
        assert decrypt_str(CheckoutStore().account('student01')['password_enc'])==original


def test_failed_release_is_rolled_back_and_retry_does_not_rotate(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch.object(CheckoutStore,'event',side_effect=RuntimeError('private details')):
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert not result.get_json()['results'][0]['released']
    assert start(checkout).status_code==200 and start(checkout,'two@example.edu').status_code==409
    with patch('account_checkout.requests.put') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.get_json()['results'][0]['released']
    disable.assert_not_called()
    reset.assert_not_called()


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
    with patch('account_checkout.requests.put') as disable,patch('account_checkout.set_account_password') as reset:
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    disable.assert_not_called();reset.assert_not_called()
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
    with patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch('account_checkout.requests.put') as disable:
        response=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True,'username':'student02@pve','pool_id':'untrusted'},environ_overrides={'REMOTE_ADDR':'203.0.113.50'})
    assert response.status_code==200 and response.get_json()['relinquished']
    disable.assert_not_called()
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
    with patch('account_checkout.requests.put') as disable,patch('account_checkout.requests.post') as authenticate:
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':False}).status_code==400
        assert checkout.post('/api/account-checkout/relinquish',json={'confirmed':True}).status_code==403
        assert checkout.post('/api/account-checkout/relinquish',headers={**h,'Origin':'https://attacker.example'},json={'confirmed':True}).status_code==403
        disable.assert_not_called();authenticate.assert_not_called()
    with patch('account_checkout.requests.post',return_value=reply({},401)),patch('account_checkout.requests.put') as disable:
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==401
        disable.assert_not_called()
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['email']=='one@example.edu'


def test_failed_relinquish_does_not_log_out_or_mark_completion(checkout):
    setup_pool(checkout);start(checkout);h=student_login(checkout)
    with patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch.object(CheckoutStore,'event',side_effect=RuntimeError('private failure')):
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==503
    with checkout.session_transaction() as session:
        assert session['pve_ticket']=='student-ticket'
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['email']=='one@example.edu' and state['relinquished_at'] is None and not state['releasing']


def test_released_lease_invalidates_other_browser_and_console_sessions(checkout,monkeypatch):
    from spice_bridge import session_owner
    setup_pool(checkout);start(checkout);h=student_login(checkout,ticket='student-ticket-a')
    other=main.app.test_client();student_login(other,ticket='student-ticket-b')
    cookie=other.get_cookie(main.app.config['SESSION_COOKIE_NAME']).value
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie)==session_owner('student-ticket-b')
    revoked=[];monkeypatch.setitem(main.app.extensions,'checkout_revoke_sessions',revoked.append)
    with patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed-ticket','CSRFPreventionToken':'renewed-csrf'})),patch('account_checkout.requests.put'):
        assert checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True}).status_code==200
    assert session_owner('student-ticket-a') in revoked and session_owner('student-ticket-b') in revoked
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie) is None
    with patch.object(main,'proxmox_get') as proxmox:
        assert other.get('/api/vms').status_code==401
        proxmox.assert_not_called()


def test_old_login_cannot_relinquish_reassigned_account(checkout):
    setup_pool(checkout,names=['student01']);start(checkout);h=student_login(checkout)
    with patch('account_checkout.requests.put'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    with patch('account_checkout.requests.put'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]}).get_json()['results'][0]['success']
    anonymous=main.app.test_client();assert start(anonymous,'new@example.edu').status_code==200
    with patch('account_checkout.requests.post') as authenticate:
        result=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True})
        assert result.status_code==409;authenticate.assert_not_called()
    assert checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']['email']=='new@example.edu'


def test_new_checkout_retains_previous_relinquishment_and_full_history(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.requests.put'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']},environ_overrides={'REMOTE_ADDR':'203.0.113.30'}).get_json()['results'][0]['released']
    old=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    with patch('account_checkout.requests.put'):
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
    with patch('account_checkout.requests.put'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    assert start(checkout,'new@example.edu').status_code==409
    assert checkout.post(POOL,headers=BASIC,json={'action':'enable','usernames':['student01']}).status_code==200
    assert start(checkout,'new@example.edu').status_code==409
    with patch('account_checkout.requests.put') as enable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
    assert result.get_json()['results'][0]['success']
    enable.assert_not_called()
    reset.assert_not_called()
    assert start(checkout,'new@example.edu').get_json()['password']=='original-password1'


def test_explicit_password_reset_keeps_login_disabled_and_reenable_uses_new_credentials(checkout):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.requests.put'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    entries=[{'username':'student01','password':'new-sce-password'}]
    with patch('account_checkout.requests.put') as disable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'reset_passwords','accounts':entries})
    assert result.get_json()['results'][0]['success']
    disable.assert_not_called()
    assert reset.call_args.args[1:]==('student01@pve','new-sce-password')
    state=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert state['login_disabled'] and not state['password_rotated']
    assert start(checkout,'new@example.edu').status_code==409
    with patch('account_checkout.requests.put') as enable:
        stale=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
        assert not stale.get_json()['results'][0]['success'];enable.assert_not_called()
        response=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':entries})
    assert response.get_json()['results'][0]['success']
    assert start(checkout,'new@example.edu').get_json()['password']=='new-sce-password'


def test_reset_and_reenable_reject_assigned_accounts(checkout):
    setup_pool(checkout);start(checkout)
    for action in ('reset_passwords','reenable_logins','enable_accounts'):
        with patch('account_checkout.requests.put') as change,patch('account_checkout.set_account_password') as reset:
            result=checkout.post(POOL,headers=BASIC,json={'action':action,'accounts':[{'username':'student01','password':'new-password'}]})
        assert not result.get_json()['results'][0]['success'];change.assert_not_called();reset.assert_not_called()


def test_publishing_reports_student_token_readiness_without_storing_an_administrator_password(checkout):
    assert publish(checkout).status_code==200
    status=checkout.get(POOL,headers=BASIC)
    assert status.json['login_management_ready'] is True
    assert 'admin-password' not in status.get_data(as_text=True)
    assert 'test-management-token' not in status.get_data(as_text=True)
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute('SELECT manager_password_enc FROM pools WHERE id=?',('lab',)).fetchone()[0] is None


def test_upstream_successful_login_cannot_bypass_local_disabled_state(checkout):
    setup_pool(checkout);start(checkout)
    with patch('account_checkout.requests.put'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'stale-upstream-ticket','CSRFPreventionToken':'csrf'})):
        result=checkout.post('/login',data={'username':'student01','password':'original-password1','realm':'pve','host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert result.status_code==403 and b'account is disabled' in result.data


@pytest.mark.parametrize('action', ['reset_passwords', 'reenable_logins'])
def test_failed_preparation_keeps_login_disabled_and_password_unchanged(checkout,action):
    setup_pool(checkout,names=['student01']);start(checkout)
    with patch('account_checkout.requests.put'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    password='new-saved-password' if action=='reset_passwords' else 'original-password1'
    target='account_checkout.set_account_password' if action=='reset_passwords' else 'checkout_store.CheckoutStore.event'
    with patch('account_checkout.requests.put'),patch(target,side_effect=RuntimeError('private upstream details')):
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
    with patch('account_checkout.requests.put'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).get_json()['results'][0]['released']
    account=checkout.get(POOL,headers=BASIC).get_json()['accounts']['student01@pve']
    assert account['name'] is None and account['email'] is None
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute("SELECT name FROM events WHERE action='released_accessforge_disabled'").fetchone()['name']=='First Student'
    with patch('account_checkout.requests.put'):
        checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
    assert start(checkout,'new@example.edu',name='Next Student').get_json()['name']=='Next Student'


@pytest.mark.parametrize('username,permissions,visible',[
    ('instructor',{'/':{'Sys.Modify':1}},True),
    ('instructor',{'/':{'Sys.Modify':0}},True),
    ('student01',{'/':{'VM.Console':1}},False),
    ('sudo-user',{'/':{'Sys.Audit':1}},False),
])
def test_management_link_is_available_immediately_after_verified_admin_login(checkout,username,permissions,visible):
    with checkout.session_transaction() as session:
        session['checkout_admin']=True  # A new login must clear an old administrator marker.
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'login-ticket','CSRFPreventionToken':'login-csrf'})),patch.object(main,'proxmox_get',return_value=reply({'version':'test'})),patch('account_checkout.requests.get',return_value=reply(permissions)) as get:
        response=checkout.post('/login',data={'username':username,'password':'test-password','realm':'pam',
                                             'host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert response.status_code==302
    get.assert_called_once()
    assert get.call_args.kwargs['params']=={'path':'/'}
    assert get.call_args.kwargs['cookies']=={'PVEAuthCookie':'login-ticket'}
    assert get.call_args.args[0]==account_checkout.backend_url()+'/access/permissions'
    with checkout.session_transaction() as session:
        assert bool(session.get('checkout_admin')) is visible
    page=checkout.get('/checkout')
    assert (b'<a href="/admin/checkouts">Checkouts</a>' in page.data) is visible
    assert (b'<a href="/">Connections</a>' in page.data) is visible
    assert b'<a href="/checkout">Get access</a>' not in page.data
    if visible:
        assert page.data.index(b'>Connections</a>') < page.data.index(b'>Checkouts</a>')
    assert b'Manage account checkouts' not in page.data


@pytest.mark.parametrize('other_cluster,enabled',[(True,True),(False,False)])
def test_management_navigation_never_checks_another_cluster_or_a_disabled_checkout_service(checkout,monkeypatch,other_cluster,enabled):
    monkeypatch.setenv('ENABLE_ACCOUNT_CHECKOUT','true' if enabled else 'false')
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'login-ticket','CSRFPreventionToken':'login-csrf'})),patch.object(main,'proxmox_get',return_value=reply({'version':'test'})),patch('account_checkout.requests.get') as get:
        response=checkout.post('/login',data={'username':'instructor','password':'test-password','realm':'pam',
                                             'host':'other-cluster.example' if other_cluster else main.app.config['CHECKOUT_PROXMOX_HOST'],
                                             'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert response.status_code==302
    get.assert_not_called()
    with checkout.session_transaction() as session:
        assert not session.get('checkout_admin')


def test_management_permission_lookup_failure_hides_link_without_blocking_login(checkout):
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'login-ticket','CSRFPreventionToken':'login-csrf'})),patch.object(main,'proxmox_get',return_value=reply({'version':'test'})),patch('account_checkout.requests.get',side_effect=requests.Timeout):
        response=checkout.post('/login',data={'username':'instructor','password':'test-password','realm':'pam',
                                             'host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert response.status_code==302
    with checkout.session_transaction() as session:
        assert session.get('pve_ticket')=='login-ticket' and not session.get('checkout_admin')


def test_home_backfills_admin_navigation_for_existing_sessions_and_clears_revoked_roles(checkout):
    with checkout.session_transaction() as session:
        session.update(pve_user='instructor@pam',pve_ticket='ticket',pve_csrf='csrf',
                       pve_host=main.app.config['CHECKOUT_PROXMOX_HOST'],pve_port=str(main.app.config['CHECKOUT_PROXMOX_PORT']))
    for permissions,visible in [({'/':{'Sys.Modify':1}},True),({'/':{'VM.Console':1}},False)]:
        with patch.object(main,'proxmox_get',side_effect=[reply([]),reply(permissions)]):
            response=checkout.get('/')
        assert response.status_code==200
        assert (b'<a href="/admin/checkouts">Checkouts</a>' in response.data) is visible
        assert (b'<a href="/">Connections</a>' in response.data) is visible


def setup_second_project(client,verify_email=False):
    endpoint='/api/admin/checkouts/pools/second-project'
    assert client.put(endpoint,headers=BASIC,json={'name':'Digital Forensics','accounts':[
        {'username':'forensics01','password':'forensics-password1'}]}).status_code==200
    assert client.patch(endpoint,headers=BASIC,json={'secret':'forensics-secret','enabled':True,'verify_email':verify_email}).status_code==200
    assert client.post(endpoint,headers=BASIC,json={'action':'enable','usernames':['forensics01']}).status_code==200
    return endpoint


def test_same_email_can_hold_and_recover_one_account_in_each_project(checkout):
    setup_pool(checkout,names=['student01']);second=setup_second_project(checkout)
    first_result=start(checkout,'same@example.edu',secret='class-secret').get_json()
    second_result=start(checkout,'same@example.edu',secret='forensics-secret').get_json()
    assert first_result['username']=='student01@pve' and first_result['project']=='Networking'
    assert second_result['username']=='forensics01@pve' and second_result['project']=='Digital Forensics'
    assert start(checkout,'same@example.edu',secret='class-secret').get_json()['username']==first_result['username']
    assert start(checkout,'same@example.edu',secret='forensics-secret').get_json()['username']==second_result['username']
    assert start(checkout,'different@example.edu',secret='class-secret').status_code==409
    assert start(checkout,'different@example.edu',secret='forensics-secret').status_code==409
    assert checkout.get(POOL,headers=BASIC).json['accounts']['student01@pve']['email']=='same@example.edu'
    assert checkout.get(second,headers=BASIC).json['accounts']['forensics01@pve']['email']=='same@example.edu'


def test_email_verification_is_independent_for_the_same_email_in_multiple_projects(checkout):
    setup_pool(checkout,verify=True,names=['student01']);second=setup_second_project(checkout,verify_email=True)
    with patch('account_checkout.send_verification') as send,patch('account_checkout.secrets.randbelow',side_effect=[123,456]):
        first=start(checkout,'same@example.edu',secret='class-secret').json
        second_request=start(checkout,'same@example.edu',secret='forensics-secret').json
    assert first['status']==second_request['status']=='verification_required'
    assert send.call_args_list[0].args[2]=='Networking' and send.call_args_list[1].args[2]=='Digital Forensics'
    assert verify(checkout,first['challenge'],'00000456').status_code==403
    assert verify(checkout,first['challenge'],'00000123').json['username']=='student01@pve'
    assert verify(checkout,second_request['challenge'],'00000456').json['username']=='forensics01@pve'
    assert checkout.get(second,headers=BASIC).json['accounts']['forensics01@pve']['email']=='same@example.edu'


def test_rotating_one_projects_secret_does_not_invalidate_another_projects_verification(checkout):
    setup_pool(checkout,verify=True,names=['student01']);setup_second_project(checkout,verify_email=True)
    with patch('account_checkout.send_verification') as send:
        first=start(checkout,'same@example.edu',secret='class-secret').json;first_code=send.call_args.args[1]
        second=start(checkout,'same@example.edu',secret='forensics-secret').json;second_code=send.call_args.args[1]
    assert checkout.patch(POOL,headers=BASIC,json={'secret':'new-networking-secret'}).status_code==200
    assert verify(checkout,first['challenge'],first_code).status_code==403
    assert verify(checkout,second['challenge'],second_code).json['username']=='forensics01@pve'


def test_relinquishing_one_project_leaves_the_same_emails_other_project_account_reserved(checkout):
    setup_pool(checkout,names=['student01']);second=setup_second_project(checkout)
    start(checkout,'same@example.edu',secret='class-secret')
    assigned=start(checkout,'same@example.edu',secret='forensics-secret').json
    with patch('account_checkout.requests.put') as disable:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.json['results'][0]['released'] is True
    disable.assert_not_called()
    state=checkout.get(second,headers=BASIC).json['accounts']['forensics01@pve']
    assert state['email']=='same@example.edu' and state['login_disabled'] is False
    assert start(checkout,'same@example.edu',secret='forensics-secret').json['password']==assigned['password']


def test_projects_require_distinct_lab_secrets_to_route_student_checkout(checkout):
    setup_pool(checkout);second=setup_second_project(checkout)
    result=checkout.patch(second,headers=BASIC,json={'secret':'class-secret'})
    assert result.status_code==409 and 'another project' in result.json['error']
    assert start(checkout,secret='forensics-secret').json['project']=='Digital Forensics'


@pytest.mark.parametrize('use_basic',[False,True])
@pytest.mark.parametrize('has_token',[False,True])
def test_admin_relinquish_authenticates_without_proxmox_writes_for_browser_and_sce(checkout,monkeypatch,use_basic,has_token):
    setup_pool(checkout);start(checkout)
    if not has_token:
        monkeypatch.delenv('CHECKOUT_PROXMOX_API_TOKEN',raising=False)
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    if use_basic:
        request_headers=BASIC
    else:
        with checkout.session_transaction() as session:
            session.update(pve_user='root@pam',pve_ticket='admin-ticket',pve_csrf='admin-csrf',checkout_admin_csrf='action-csrf')
        request_headers={'Origin':'http://localhost','X-Checkout-CSRF':'action-csrf'}
    with patch('account_checkout.requests.put',return_value=reply(None)) as put,patch('account_checkout.requests.post',return_value=reply({'username':'root@pam','ticket':'admin-ticket','CSRFPreventionToken':'admin-csrf'})) as post,patch('account_checkout.requests.get',return_value=reply({'/':{'Sys.Modify':1}})):
        result=checkout.post(POOL,headers=request_headers,json={'action':'release','usernames':['student01']})
    assert result.status_code==200 and result.json['results'][0]['released']
    put.assert_not_called()
    assert post.call_count==int(use_basic)
    if use_basic:
        assert post.call_args.kwargs['data']=={'username':'root@pam','password':'admin-password'}
    assert 'test-management-token' not in result.get_data(as_text=True)


def test_admin_without_api_token_uses_current_session_and_ignores_legacy_password(checkout,monkeypatch):
    setup_pool(checkout);start(checkout)
    with main.app.app_context():
        CheckoutStore().save_manager('lab','root@pam','old-manager-password')
    monkeypatch.delenv('CHECKOUT_PROXMOX_API_TOKEN',raising=False)
    with patch('account_checkout.requests.put',return_value=reply(None)) as put,patch('account_checkout.requests.post') as post:
        result=checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert result.status_code==200 and result.json['results'][0]['released']
    put.assert_not_called()
    post.assert_not_called()
    summary=checkout.get(POOL,headers=BASIC).json
    assert summary['login_management_ready'] is True and summary['login_scope']=='accessforge'
    assert summary['accounts']['student01@pve']['email'] is None
    assert summary['accounts']['student01@pve']['releasing'] is False
    assert 'old-manager-password' not in result.get_data(as_text=True)


@pytest.mark.parametrize('expired',[False,True])
def test_admin_authentication_failure_cannot_use_service_token_to_relinquish(checkout,monkeypatch,expired):
    setup_pool(checkout);start(checkout)
    with checkout.session_transaction() as session:
        session.update(pve_user='root@pam',pve_ticket='admin-ticket',pve_csrf='admin-csrf',checkout_admin_csrf='action-csrf')
    monkeypatch.setattr(account_checkout,'authenticate_admin',REAL_ADMIN_AUTH)
    with patch('account_checkout.requests.get',return_value=reply({'/':{'VM.Console':1}},401 if expired else 200)),patch('account_checkout.requests.put') as put:
        result=checkout.post(POOL,headers={'Origin':'http://localhost','X-Checkout-CSRF':'action-csrf'},json={'action':'release','usernames':['student01']})
    assert result.status_code==(401 if expired else 403)
    put.assert_not_called()
    with main.app.app_context():
        account=CheckoutStore().account('student01@pve')
    assert account['email']=='one@example.edu' and not account['release_pending']


@pytest.mark.parametrize('has_token',[False,True])
def test_accessforge_enable_disable_does_not_change_proxmox_or_passwords(checkout,monkeypatch,has_token):
    setup_pool(checkout)
    if not has_token:
        monkeypatch.delenv('CHECKOUT_PROXMOX_API_TOKEN',raising=False)
    with patch('account_checkout.requests.put',return_value=reply(None)) as put,patch('account_checkout.set_account_password') as reset:
        disabled=checkout.post(POOL,headers=BASIC,json={'action':'disable_logins','usernames':['student01']})
        assert disabled.json['results'][0]['success']
        enabled=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
        assert enabled.json['results'][0]['success']
    put.assert_not_called()
    assert all(call.kwargs['headers']=={'CSRFPreventionToken':'admin-csrf'} for call in put.call_args_list)
    assert all(call.kwargs['cookies']=={'PVEAuthCookie':'admin-ticket'} for call in put.call_args_list)
    reset.assert_not_called()


def test_legacy_password_storage_endpoint_no_longer_saves_credentials(checkout):
    setup_pool(checkout)
    result=checkout.post(POOL,headers=BASIC,json={'action':'save_login_manager','password':'do-not-store'})
    assert result.status_code==400 and 'no management API token is required' in result.json['error']
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            assert db.execute('SELECT manager_password_enc FROM pools WHERE id=?',('lab',)).fetchone()[0] is None


def test_student_without_a_management_token_can_relinquish(checkout,monkeypatch):
    setup_pool(checkout);start(checkout);h=student_login(checkout)
    monkeypatch.delenv('CHECKOUT_PROXMOX_API_TOKEN',raising=False)
    with patch('account_checkout.requests.post',return_value=reply({'username':'student01@pve','ticket':'renewed','CSRFPreventionToken':'csrf'})),patch('account_checkout.requests.put') as put:
        result=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True})
    assert result.status_code==200 and result.json['relinquished']
    assert 'CHECKOUT_PROXMOX_API_TOKEN' not in result.get_data(as_text=True)
    put.assert_not_called()
    assert checkout.get(POOL,headers=BASIC).json['accounts']['student01@pve']['email'] is None


@pytest.mark.parametrize('initially_disabled',[False,True])
@pytest.mark.parametrize('enable_checkout',[False,True])
@pytest.mark.parametrize('action',['reenable_logins','enable_accounts'])
def test_enable_is_idempotent_and_enables_checkout_only_when_requested(checkout,initially_disabled,enable_checkout,action):
    assert publish(checkout).status_code==200
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            db.execute('UPDATE accounts SET login_disabled=? WHERE username=?',(int(initially_disabled),'student01@pve'))
    with patch('account_checkout.requests.put') as enable,patch('account_checkout.set_account_password') as reset:
        for _ in range(2):
            result=checkout.post(POOL,headers=BASIC,json={'action':action,'accounts':[{'username':'student01','password':'original-password1','enable_checkout':enable_checkout}]})
            assert result.status_code==200 and result.json['results'][0]['success']
            state=result.json['checkout']['accounts']['student01@pve']
            assert state['login_disabled']==enable_checkout and state['awaiting_checkout']==enable_checkout and state['eligible']==enable_checkout
        enable.assert_not_called()
        reset.assert_not_called()


def test_account_setup_capability_and_default_setup_enable_a_new_active_account(checkout):
    assert checkout.get('/api/admin/checkouts/status',headers=BASIC).json['capabilities']['account_setup'] is True
    publish(checkout)
    with patch('account_checkout.requests.put') as enable:
        result=checkout.post(POOL,headers=BASIC,json={'action':'enable_accounts','accounts':[{'username':'student01','password':'original-password1'}]})
    assert result.status_code==200 and result.json['results'][0]['success']
    state=result.json['checkout']['accounts']['student01@pve']
    assert state['eligible'] and state['login_disabled'] and state['awaiting_checkout']
    enable.assert_not_called()


@pytest.mark.parametrize('initially_disabled',[False,True])
def test_failed_login_enable_does_not_enable_checkout_or_change_login_state(checkout,initially_disabled):
    publish(checkout)
    with main.app.app_context():
        with CheckoutStore().transaction() as db:
            db.execute('UPDATE accounts SET login_disabled=? WHERE username=?',(int(initially_disabled),'student01@pve'))
    with patch.object(CheckoutStore,'event',side_effect=RuntimeError('private-failure')):
        result=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1','enable_checkout':True}]})
    assert result.status_code==200 and not result.json['results'][0]['success']
    state=result.json['checkout']['accounts']['student01@pve']
    assert not state['eligible'] and state['login_disabled']==initially_disabled
    assert 'private-failure' not in result.get_data(as_text=True)


def test_password_reset_still_requires_disabling_and_invalid_checkout_flag_is_rejected(checkout):
    publish(checkout)
    with patch('account_checkout.requests.put') as enable,patch('account_checkout.set_account_password') as reset:
        result=checkout.post(POOL,headers=BASIC,json={'action':'reset_passwords','accounts':[{'username':'student01','password':'updated-password'}]})
        assert result.status_code==200 and not result.json['results'][0]['success']
        assert 'Disable this login' in result.json['results'][0]['error']
        for flag in ('true',1,None):
            result=checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1','enable_checkout':flag}]})
            assert result.status_code==400
        enable.assert_not_called();reset.assert_not_called()


def test_unassigned_on_demand_account_blocks_login_and_old_unbound_console_session(checkout):
    assert publish(checkout).status_code==200
    student_login(checkout)
    cookie=checkout.get_cookie(main.app.config['SESSION_COOKIE_NAME']).value
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie)
    checkout.post(POOL,headers=BASIC,json={'action':'enable','usernames':['student01']})
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie) is None
    with patch.object(main,'proxmox_get') as get:
        assert checkout.get('/api/vms').status_code==401
        get.assert_not_called()
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'ticket','CSRFPreventionToken':'csrf'})):
        result=checkout.post('/login',data={'username':'student01','password':'original-password1','realm':'pve',
            'host':main.app.config['CHECKOUT_PROXMOX_HOST'],'port':str(main.app.config['CHECKOUT_PROXMOX_PORT'])})
    assert result.status_code==403 and b'awaiting checkout' in result.data


def test_old_unassigned_session_does_not_become_valid_when_someone_checks_out(checkout):
    publish(checkout);student_login(checkout)
    cookie=checkout.get_cookie(main.app.config['SESSION_COOKIE_NAME']).value
    checkout.patch(POOL,headers=BASIC,json={'secret':'class-secret','enabled':True,'verify_email':False})
    checkout.post(POOL,headers=BASIC,json={'action':'enable','usernames':['student01']})
    other=main.app.test_client()
    assert start(other).status_code==200
    assert main.spice_cookie_owner(main.app.config['SESSION_COOKIE_NAME']+'='+cookie) is None
    assert checkout.get('/api/vms').status_code==401
    student_login(checkout)  # A fresh login binds the newly assigned lease.
