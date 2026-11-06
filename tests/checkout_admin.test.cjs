const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync('static/checkout-admin.js','utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function harness(){
  const elements=new Map(),calls=[];let confirmation=true,email='student@example.edu',override;
  class Element{
    constructor(id=''){this.id=id;this.value='';this.textContent='';this.dataset={};this.checked=false;this.disabled=false;this.listeners={};this.boxes=[];}
    addEventListener(type,fn){this.listeners[type]=fn;}
    async fire(type='click'){return this.listeners[type]?.({target:this});}
    set innerHTML(html){this.html=html;this.boxes=[...html.matchAll(/data-account="([^"]+)"/g)].map(match=>{const box=new Element();box.dataset.account=match[1];return box;});}
    get innerHTML(){return this.html||'';}
    replaceChildren(...children){if(!children.some(child=>child.value===this.value))this.value=children[0]?.value||'';}
    querySelectorAll(selector){
      if(selector==='button,select')return [...elements.values()].filter(el=>el.id!=='tbody');
      if(selector==='[data-action-menu]')return [el('admin-credential-type'),el('admin-account-actions')];
      return el('tbody').boxes.filter(box=>!selector.includes(':checked')||box.checked);
    }
  }
  const el=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  el('checkout-admin').dataset.csrf='test-csrf';
  const pool={id:'lab',name:'Networking',enabled:true,verify_email:false,secret_set:true,login_management_ready:true,
    accounts:{'student01@pve':{eligible:true},'student02@pve':{eligible:true}}};
  const context={document:{getElementById:el,querySelector:()=>el('tbody'),createElement:()=>new Element()},
    prompt:()=>email,confirm:()=>confirmation,fetch:async(url,options={})=>{
      calls.push({url,options,body:options.body?JSON.parse(options.body):undefined});
      if(override){const result=override(url,options);if(result)return result;}
      return {ok:true,json:async()=>url.endsWith('/pools')?{pools:[pool]}:{checkout:pool,results:[]}};
    }};
  vm.runInNewContext(source,context);await settle();
  return {el,calls,pool,confirm:value=>confirmation=value,email:value=>email=value,override:fn=>override=fn,
    select:async index=>{const box=el('tbody').boxes[index];box.checked=true;await box.fire('change');},
    action:async(menu,action)=>{el(menu).value=action;await el(menu).fire('change');}};
}

test('account menus require a selection and use SCE-web credential-type names',async()=>{
  const h=await harness();assert.equal(h.el('admin-credential-type').disabled,true);assert.equal(h.el('admin-account-actions').disabled,true);
  await h.select(0);assert.equal(h.el('admin-selection-count').textContent,'1 selected');assert.equal(h.el('admin-credential-type').disabled,false);
  await h.action('admin-credential-type','disable');
  assert.deepEqual(h.calls.find(call=>call.body?.action==='disable').body,{action:'disable',usernames:['student01@pve']});
  assert.equal(h.el('admin-credential-type').value,'');assert.equal(h.el('admin-credential-type').disabled,true);
  assert.equal(h.el('admin-feedback').textContent,'Selected accounts marked Permanent.');
  await h.select(1);await h.action('admin-credential-type','enable');
  assert.deepEqual(h.calls.find(call=>call.body?.action==='enable').body,{action:'enable',usernames:['student02@pve']});
  assert.equal(h.el('admin-feedback').textContent,'Selected accounts marked On-Demand.');
  const page=fs.readFileSync('templates/checkout_admin.html','utf8');
  for(const label of ['Mark Permanent','Mark On-Demand','Assign','Relinquish','Remove'])assert.ok(page.includes(`>${label}</option>`));
  assert.doesNotMatch(page,/Allow selected|Stop selected checkouts|Remove selected|Relinquish selected accounts<\/button>/);
});

test('Relinquish and Remove retain confirmation and reset their combobox after Cancel',async()=>{
  const h=await harness();await h.select(0);h.confirm(false);
  await h.action('admin-account-actions','release');assert.equal(h.calls.filter(call=>call.options.method==='POST').length,0);
  assert.equal(h.el('admin-account-actions').value,'');assert.equal(h.el('admin-account-actions').disabled,false);
  h.confirm(true);await h.action('admin-account-actions','remove');
  assert.deepEqual(h.calls.find(call=>call.body?.action==='remove').body,{action:'remove',usernames:['student01@pve']});
});

test('per-account relinquishment failures stay visible',async()=>{
  const h=await harness();await h.select(0);
  h.override(url=>!url.endsWith('/pools')?{ok:true,json:async()=>({results:[{username:'student01@pve',released:false,error:'Could not disable the login.'}]})}:null);
  await h.action('admin-account-actions','release');
  assert.match(h.el('admin-feedback').textContent,/student01@pve: Could not disable/);assert.equal(h.el('admin-feedback').className,'error');
});

test('Assign requires exactly one selected account',async()=>{
  const h=await harness();await h.select(0);await h.select(1);await h.action('admin-account-actions','assign');
  assert.equal(h.el('admin-feedback').textContent,'Select one account to assign.');assert.equal(h.calls.filter(call=>call.body).length,0);
  h.el('tbody').boxes[1].checked=false;await h.action('admin-account-actions','assign');
  assert.deepEqual(h.calls.find(call=>call.body?.action==='assign').body,{action:'assign',usernames:['student01@pve'],email:'student@example.edu'});
});

test('administrator relinquishment reuses the session without a password field, modal or credential payload',async()=>{
  const h=await harness();await h.select(0);await h.action('admin-account-actions','release');
  const request=h.calls.find(call=>call.body?.action==='release');
  assert.deepEqual(request.body,{action:'release',usernames:['student01@pve']});
  const page=fs.readFileSync('templates/checkout_admin.html','utf8');
  assert.doesNotMatch(page,/admin-manager-password|credentials-dialog|Save credentials|Save administrator password/);
  h.el('admin-secret').value='updated-lab-secret';await h.el('admin-save').fire();
  const settings=h.calls.find(call=>call.options.method==='PATCH');assert.equal(settings.body.secret,'updated-lab-secret');assert.ok(!('password' in settings.body));
});

test('a missing student token does not block administrator relinquishment',async()=>{
  const h=await harness();h.pool.login_management_ready=false;await h.el('admin-refresh').fire();await h.select(0);
  await h.action('admin-account-actions','release');
  assert.match(h.el('admin-feedback').textContent,/Selected accounts relinquished/);
});

test('expired administrator session errors stay inline without asking for a password',async()=>{
  const h=await harness();await h.select(0);
  h.override((url,options)=>options.method==='POST'?{ok:false,json:async()=>({error:'Administrator session expired. Sign in again.'})}:null);
  await h.action('admin-account-actions','release');
  assert.match(h.el('admin-feedback').textContent,/Administrator session expired/);
  assert.doesNotMatch(source,/askCredentials|showModal|save_credentials/);
});

test('checkout restrictions load, save all policy controls, and reset for a project with defaults',async()=>{
  const h=await harness();
  const policy={allowed_email_patterns:['*@utep.edu','student?@example.org'],allowed_subnets:['192.0.2.0/24'],
    subnet_checkout_limit:3,ipv4_prefix:28,ipv6_prefix:56,global_delay_seconds:10,subnet_delay_seconds:30};
  h.pool.policy=policy;await h.el('admin-refresh').fire();
  assert.equal(h.el('admin-policy-allowed_email_patterns').value,'*@utep.edu\nstudent?@example.org');
  assert.equal(h.el('admin-policy-ipv4_prefix').value,28);
  h.el('admin-policy-allowed_subnets').value='192.0.2.0/24, 2001:db8::/48';
  h.el('admin-policy-subnet_checkout_limit').value='5';await h.el('admin-save-advanced').fire();
  const sent=h.calls.find(call=>call.options.method==='PATCH').body.policy;
  assert.deepEqual(sent,{...policy,allowed_subnets:['192.0.2.0/24','2001:db8::/48'],subnet_checkout_limit:5});
  delete h.pool.policy;await h.el('admin-refresh').fire();
  assert.equal(h.el('admin-policy-allowed_email_patterns').value,'');
  assert.equal(h.el('admin-policy-subnet_checkout_limit').value,0);
  assert.equal(h.el('admin-policy-ipv4_prefix').value,24);
});

test('available accounts awaiting checkout show that their login remains disabled',async()=>{
  const h=await harness();
  h.pool.accounts['student01@pve']={eligible:true,login_disabled:true,awaiting_checkout:true};
  h.pool.accounts['student02@pve']={eligible:true,login_disabled:true,awaiting_checkout:false};
  await h.el('admin-refresh').fire();
  assert.match(h.el('tbody').innerHTML,/Available \(login disabled until assigned\)/);
  assert.match(h.el('tbody').innerHTML,/>Disabled<\/td>/);
});
