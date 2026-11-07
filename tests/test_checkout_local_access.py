"""Checkout policy is enforced by AccessForge without a service token."""
from unittest.mock import patch

import pytest

import main
from checkout_store import CheckoutStore
from test_account_checkout import BASIC, POOL, checkout, publish, reply, setup_pool, start, student_login, verify


def renewed_student():
    return reply({'username':'student01@pve','ticket':'renewed','CSRFPreventionToken':'csrf'})


def test_self_service_lifecycle_needs_no_token_or_proxmox_user_writes(checkout,monkeypatch):
    monkeypatch.setenv('CHECKOUT_PROXMOX_API_TOKEN','obsolete-token-must-not-be-used')
    with patch('account_checkout.requests.put',side_effect=AssertionError('No Proxmox writes')):
        setup_pool(checkout,names=['student01'])
        ready=checkout.get(POOL,headers=BASIC).json
        assert ready['login_scope']=='accessforge' and ready['login_management_ready']
        account=ready['accounts']['student01@pve']
        assert account['awaiting_checkout'] and account['login_disabled']
        first=start(checkout)
        assert first.status_code==200
        assert start(checkout).json['password']==first.json['password']
        h=student_login(checkout)
        with patch('account_checkout.requests.post',return_value=renewed_student()):
            result=checkout.post('/api/account-checkout/relinquish',headers=h,json={'confirmed':True})
        assert result.status_code==200 and result.json['relinquished']
        state=checkout.get(POOL,headers=BASIC).json['accounts']['student01@pve']
        assert state['email'] is None and state['login_disabled']
        assert start(checkout,'two@example.edu').status_code==409
        enabled=checkout.post(POOL,headers=BASIC,json={'action':'enable_accounts','accounts':[{'username':'student01','password':'original-password1'}]})
        assert enabled.json['results'][0]['success']
        assert start(checkout,'two@example.edu').status_code==200


def test_verified_checkout_is_local_and_failed_code_does_not_assign(checkout):
    setup_pool(checkout,verify=True,names=['student01'])
    with patch('account_checkout.send_verification') as send,patch('account_checkout.requests.put',side_effect=AssertionError('No Proxmox writes')):
        request=start(checkout)
        challenge=request.json['challenge'];code=send.call_args.args[1]
        assert verify(checkout,challenge,'bad-code').status_code==403
        assert checkout.get(POOL,headers=BASIC).json['accounts']['student01@pve']['email'] is None
        assert verify(checkout,challenge,code).status_code==200


def test_refresh_and_marking_are_local_to_accessforge(checkout):
    setup_pool(checkout)
    with patch('account_checkout.requests.put',side_effect=AssertionError('No Proxmox writes')):
        for endpoint in (POOL,'/api/admin/checkouts/pools'):
            assert checkout.get(endpoint,headers=BASIC).status_code==200
        assert checkout.post(POOL,headers=BASIC,json={'action':'disable','usernames':['student01']}).status_code==200
        with main.app.app_context():
            row=CheckoutStore().account('student01@pve')
            assert not row['eligible'] and not row['login_disabled']
        assert checkout.post(POOL,headers=BASIC,json={'action':'enable','usernames':['student01']}).status_code==200
        assert checkout.post(POOL,headers=BASIC,json={'action':'remove','usernames':['student02']}).status_code==200
        assert checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student01'],'email':'manual@example.edu'}).status_code==200


def test_proxy_requires_session_and_matching_proxmox_identity(checkout):
    assert checkout.get('/api/internal/proxmox-access').status_code==403
    checkout.set_cookie('PVEAuthCookie','standalone-proxmox-ticket')
    assert checkout.get('/api/internal/proxmox-access').status_code==403
    setup_pool(checkout,names=['student01']);start(checkout);student_login(checkout)
    assert checkout.get('/api/internal/proxmox-access').status_code==204
    assert checkout.get('/api/internal/proxmox-access',headers={'Authorization':'PVEAPIToken=other-user!token=secret'}).status_code==403
    checkout.set_cookie('PVEAuthCookie','other-user-ticket')
    assert checkout.get('/api/internal/proxmox-access').status_code==403


def test_proxy_blocks_released_and_stale_leases_even_if_proxmox_ticket_still_works(checkout):
    setup_pool(checkout,names=['student01']);start(checkout);student_login(checkout)
    assert checkout.get('/api/internal/proxmox-access').status_code==204
    assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).json['results'][0]['released']
    assert checkout.get('/api/internal/proxmox-access').status_code==403
    assert checkout.post(POOL,headers=BASIC,json={'action':'enable_accounts','accounts':[{'username':'student01','password':'original-password1'}]}).json['results'][0]['success']
    assert start(main.app.test_client(),'new@example.edu').status_code==200
    assert checkout.get('/api/internal/proxmox-access').status_code==403
    student_login(checkout)
    assert checkout.get('/api/internal/proxmox-access').status_code==204


def test_proxy_preserves_ordinary_behavior_when_checkout_feature_is_off(checkout,monkeypatch):
    monkeypatch.setenv('ENABLE_ACCOUNT_CHECKOUT','false')
    assert checkout.get('/api/internal/proxmox-access').status_code==204


@pytest.mark.parametrize('assigned',[False,True])
def test_managed_account_cannot_bypass_gate_with_another_host_spelling(checkout,assigned):
    setup_pool(checkout,names=['student01'])
    if assigned:
        assert start(checkout).status_code==200
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'upstream-ticket','CSRFPreventionToken':'csrf'})):
        result=checkout.post('/login',data={'username':'student01','password':'original-password1','realm':'pve','host':'same-cluster-alias.example','port':'8006'})
    assert result.status_code==403
    with checkout.session_transaction() as session:
        assert not session.get('pve_ticket')


def test_raw_proxy_cannot_use_a_session_authenticated_to_a_different_cluster(checkout):
    publish(checkout)
    with patch.object(main,'proxmox_post',return_value=reply({'ticket':'foreign-ticket','CSRFPreventionToken':'csrf'})),patch.object(main,'proxmox_get',return_value=reply({'version':'test'})):
        result=checkout.post('/login',data={'username':'other-user','password':'test-password','realm':'pve','host':'other-cluster.example','port':'8006'})
    assert result.status_code==302
    assert checkout.get('/api/internal/proxmox-access').status_code==403


def test_raw_proxy_fails_closed_when_checkout_storage_is_unavailable(checkout):
    setup_pool(checkout);start(checkout);student_login(checkout)
    assert checkout.get('/api/internal/proxmox-access').status_code==204
    with patch('checkout_store.os.makedirs',side_effect=PermissionError('private-storage-path')):
        result=checkout.get('/api/internal/proxmox-access')
    assert result.status_code==403 and b'private-storage-path' not in result.data


def test_raw_proxy_rejects_ambiguous_proxmox_authentication_cookies(checkout):
    setup_pool(checkout);start(checkout);student_login(checkout)
    signed=checkout.get_cookie(main.app.config['SESSION_COOKIE_NAME']).value
    raw=main.app.test_client(use_cookies=False)
    cookies=f'{main.app.config["SESSION_COOKIE_NAME"]}={signed}; PVEAuthCookie=student-ticket'
    assert raw.get('/api/internal/proxmox-access',headers={'Cookie':cookies+'; PVEAuthCookie=other-user-ticket'}).status_code==403
    assert raw.get('/api/internal/proxmox-access',headers={'Cookie':cookies+'; PVEAuthCookie=student-ticket'}).status_code==204
