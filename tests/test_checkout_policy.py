from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import main
from checkout_store import CheckoutStore
from test_account_checkout import BASIC, POOL, checkout, headers, publish, setup_pool, start


def policy(client, **settings):
    result=client.patch(POOL,headers=BASIC,json={'policy':settings})
    assert result.status_code==200, result.json
    return result.json['policy']


def claim(client, email, ip='192.0.2.10', **extra):
    return client.post('/api/account-checkout/start',headers=headers(client),json={
        'name':'Test Student','email':email,'secret':'class-secret',
    },environ_overrides={'REMOTE_ADDR':ip},**extra)


def test_policy_defaults_normalization_partial_updates_and_republication(checkout):
    setup_pool(checkout)
    defaults=checkout.get(POOL,headers=BASIC).json['policy']
    assert defaults==dict(allowed_email_patterns=[],allowed_subnets=[],subnet_checkout_limit=0,
                         ipv4_prefix=24,ipv6_prefix=64,global_delay_seconds=0,subnet_delay_seconds=0)
    configured=policy(checkout,allowed_email_patterns=[' *@EXAMPLE.EDU ','*@example.edu'],
                      allowed_subnets=['192.0.2.10/24','2001:db8::1/48'],subnet_checkout_limit=2)
    assert configured['allowed_email_patterns']==['*@example.edu']
    assert configured['allowed_subnets']==['192.0.2.0/24','2001:db8::/48']
    assert policy(checkout,global_delay_seconds=10)['subnet_checkout_limit']==2
    assert publish(checkout).status_code==200
    with main.app.app_context():
        assert CheckoutStore().summary('lab')['policy']['global_delay_seconds']==10
    assert checkout.get(POOL,headers=BASIC).json['policy']['allowed_email_patterns']==['*@example.edu']


@pytest.mark.parametrize('settings',[
    None,[],{'unknown':1},{'subnet_checkout_limit':True},{'subnet_checkout_limit':-1},
    {'subnet_checkout_limit':2001},{'ipv4_prefix':33},{'ipv6_prefix':129},{'ipv4_prefix':'24'},
    {'global_delay_seconds':1.5},{'global_delay_seconds':604801},{'subnet_delay_seconds':-1},
    {'allowed_email_patterns':'*@example.edu'},{'allowed_email_patterns':['no-at-sign']},
    {'allowed_email_patterns':['*@']},{'allowed_email_patterns':['*@exam ple.edu']},
    {'allowed_email_patterns':['[abc]@example.edu']},{'allowed_email_patterns':[None]},
    {'allowed_email_patterns':['*@example.edu']*51},{'allowed_subnets':['192.0.2.0/99']},
    {'allowed_subnets':['not-a-network']},{'allowed_subnets':None},
])
def test_invalid_policy_does_not_change_existing_settings(checkout,settings):
    setup_pool(checkout)
    before=policy(checkout,subnet_checkout_limit=2)
    result=checkout.patch(POOL,headers=BASIC,json={'enabled':False,'policy':settings})
    assert result.status_code==400
    current=checkout.get(POOL,headers=BASIC).json
    assert current['enabled'] and current['policy']==before


def test_email_patterns_match_entire_casefolded_address_and_support_multiple_formats(checkout):
    setup_pool(checkout)
    policy(checkout,allowed_email_patterns=['student?@example.edu','*@utep.edu'])
    assert claim(checkout,'student12@example.edu').status_code==403
    assert claim(checkout,'student1@example.edu.invalid').status_code==403
    assert claim(checkout,'STUDENT1@EXAMPLE.EDU').status_code==200
    assert claim(checkout,'other@utep.edu').status_code==200


@pytest.mark.parametrize('ip,allowed',[
    ('192.0.2.25',True),('192.0.3.25',False),('2001:db8:1::25',True),
    ('2001:db8:2::25',False),('::ffff:192.0.2.25',True),
])
def test_allowed_networks_support_ipv4_ipv6_and_mapped_addresses(checkout,ip,allowed):
    setup_pool(checkout)
    policy(checkout,allowed_subnets=['192.0.2.0/24','2001:db8:1::/48'])
    assert claim(checkout,'one@example.edu',ip).status_code==(200 if allowed else 403)


def test_untrusted_forwarded_ip_cannot_bypass_allowed_network(checkout):
    setup_pool(checkout);policy(checkout,allowed_subnets=['192.0.2.0/24'])
    result=checkout.post(
        '/api/account-checkout/start',headers={**headers(checkout),'X-Real-IP':'192.0.2.10','X-Forwarded-For':'192.0.2.10'},
        json={'name':'Test Student','email':'one@example.edu','secret':'class-secret'},environ_overrides={'REMOTE_ADDR':'198.51.100.10'})
    assert result.status_code==403
    result=checkout.post('/api/account-checkout/start',headers={**headers(checkout),'X-Real-IP':'192.0.2.10'},
                         json={'name':'Test Student','email':'one@example.edu','secret':'class-secret'},environ_overrides={'REMOTE_ADDR':'127.0.0.1'})
    assert result.status_code==200


@pytest.mark.parametrize('key,value',[
    ('allowed_email_patterns',['*@utep.edu']),('allowed_subnets',['198.51.100.0/24']),
])
def test_disallowed_requests_do_not_send_verification_email(checkout,key,value):
    setup_pool(checkout,verify=True);policy(checkout,**{key:value})
    with patch('account_checkout.send_verification') as send:
        assert claim(checkout,'one@example.edu').status_code==403
    send.assert_not_called()


def test_subnet_quota_counts_active_assignments_and_recovers_without_extra_slot(checkout):
    setup_pool(checkout);policy(checkout,subnet_checkout_limit=1,ipv4_prefix=24)
    first=claim(checkout,'one@example.edu','192.0.2.1')
    assert first.status_code==200
    assert claim(checkout,'one@example.edu','192.0.2.2').json['username']==first.json['username']
    assert claim(checkout,'two@example.edu','192.0.2.200').status_code==429
    assert claim(checkout,'two@example.edu','192.0.3.1').status_code==200
    with patch('account_checkout.set_login_enabled'):
        assert checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']}).json['results'][0]['released']
        checkout.post(POOL,headers=BASIC,json={'action':'reenable_logins','accounts':[{'username':'student01','password':'original-password1'}]})
    assert claim(checkout,'three@example.edu','192.0.2.30').status_code==200


def test_ipv6_quota_respects_configured_prefix(checkout):
    setup_pool(checkout);policy(checkout,subnet_checkout_limit=1,ipv6_prefix=64)
    assert claim(checkout,'one@example.edu','2001:db8:1:1::1').status_code==200
    assert claim(checkout,'two@example.edu','2001:db8:1:1::abcd').status_code==429
    assert claim(checkout,'two@example.edu','2001:db8:1:2::1').status_code==200


@pytest.mark.parametrize('settings,second_ip,blocked',[
    ({'global_delay_seconds':30},'192.0.3.10',True),
    ({'subnet_delay_seconds':30},'192.0.2.99',True),
    ({'subnet_delay_seconds':30},'192.0.3.10',False),
    ({'subnet_delay_seconds':30,'ipv4_prefix':16},'192.0.3.10',True),
])
def test_cooldowns_only_throttle_new_allocations_and_expire(checkout,settings,second_ip,blocked):
    setup_pool(checkout);policy(checkout,**settings)
    with patch('checkout_store.time.time',return_value=1000):
        assert claim(checkout,'one@example.edu').status_code==200
        assert claim(checkout,'one@example.edu').status_code==200
        result=claim(checkout,'two@example.edu',second_ip)
        assert result.status_code==(429 if blocked else 200)
        if blocked:
            assert '30 seconds' in result.json['error']
    with patch('checkout_store.time.time',return_value=1030):
        assert claim(checkout,'two@example.edu',second_ip).status_code==200


def test_cooldown_survives_relinquishment_and_is_project_scoped(checkout):
    from test_account_checkout import setup_second_project
    setup_pool(checkout);setup_second_project(checkout)
    policy(checkout,subnet_delay_seconds=60)
    assert claim(checkout,'one@example.edu').status_code==200
    with patch('account_checkout.set_login_enabled'):
        checkout.post(POOL,headers=BASIC,json={'action':'release','usernames':['student01']})
    assert claim(checkout,'two@example.edu').status_code==429
    assert start(checkout,'two@example.edu',secret='forensics-secret').status_code==200


@pytest.mark.parametrize('settings',[
    {'subnet_checkout_limit':1},{'global_delay_seconds':30},{'subnet_delay_seconds':30},
])
def test_concurrent_students_cannot_exceed_limits(checkout,settings):
    setup_pool(checkout);policy(checkout,**settings)
    def request_checkout(index):
        with main.app.test_client() as client:
            return claim(client,f'student{index}@example.edu',f'192.0.2.{index+1}').status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(request_checkout,range(2)))
    assert sorted(results)==[200,429]


def test_email_verification_rechecks_capacity_and_current_network(checkout):
    setup_pool(checkout,verify=True);policy(checkout,subnet_checkout_limit=1,allowed_subnets=['192.0.2.0/24'])
    with patch('account_checkout.send_verification') as send:
        first=claim(checkout,'one@example.edu').json['challenge'];first_code=send.call_args.args[1]
        second=claim(checkout,'two@example.edu').json['challenge'];second_code=send.call_args.args[1]
    h=headers(checkout)
    assert checkout.post('/api/account-checkout/verify',headers=h,json={'challenge':first,'code':first_code},environ_overrides={'REMOTE_ADDR':'198.51.100.1'}).status_code==403
    assert checkout.post('/api/account-checkout/verify',headers=h,json={'challenge':first,'code':first_code},environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).status_code==200
    assert checkout.post('/api/account-checkout/verify',headers=h,json={'challenge':second,'code':second_code},environ_overrides={'REMOTE_ADDR':'192.0.2.2'}).status_code==429


def test_recovery_still_requires_allowed_email_and_network_and_admin_can_assign(checkout):
    setup_pool(checkout)
    assert claim(checkout,'one@example.edu').status_code==200
    policy(checkout,allowed_email_patterns=['*@utep.edu'],allowed_subnets=['198.51.100.0/24'],global_delay_seconds=30)
    assert claim(checkout,'one@example.edu').status_code==403
    result=checkout.post(POOL,headers=BASIC,json={'action':'assign','usernames':['student02'],'email':'manual@example.edu'})
    assert result.status_code==200
    policy(checkout,allowed_email_patterns=[])
    assert claim(checkout,'one@example.edu').status_code==403
    assert claim(checkout,'one@example.edu','198.51.100.1').status_code==200
