const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync('static/account-checkout.js','utf8');
function harness(){
  const fields=new Map(),loginFields=new Map(),calls=[];let focused;
  class Element{
    constructor(id=''){this.id=id;this.value='';this.hidden=false;this.type='password';this.textContent='';this.dataset={csrf:'csrf'};this.listeners={};}
    addEventListener(type,fn){this.listeners[type]=fn;}
    fire(type='click'){return this.listeners[type]?.({preventDefault(){},currentTarget:this,target:this});}
    querySelector(){return el('start-button');}
    querySelectorAll(){return [el('verify-button'),el('checkout-restart')];}
    setAttribute(name,value){this[name]=value;}
    focus(){focused=this.id;}
  }
  const el=id=>{if(!fields.has(id))fields.set(id,new Element(id));return fields.get(id);};
  el('checkout-verify').hidden=true;el('checkout-result').hidden=true;
  el('checkout-login').elements={namedItem:name=>{if(!loginFields.has(name))loginFields.set(name,new Element(name));return loginFields.get(name);}};
  const context={document:{getElementById:el,querySelectorAll:()=>[]},fetch:async(url,options)=>{
    const body=JSON.parse(options.body);calls.push({url,body});
    const data=body.secret==='verify-secret'?{status:'verification_required',challenge:'challenge-one'}:
      {status:'assigned',project:body.secret==='second-secret'?'Forensics':'Networking',name:body.name,email:body.email,
       username:body.secret==='second-secret'?'forensics01@pve':'network01@pve',password:'test-project-password',
       connection:{host:'pve.example',port:'8006',realm:'pve',verify_ssl:true}};
    return {ok:true,json:async()=>data};
  }};
  vm.runInNewContext(source,context);
  return {el,loginFields,calls,focused:()=>focused};
}

test('a student can request another project with the same identity while prior credential fields are cleared',async()=>{
  const h=harness();h.el('checkout-name').value='Ava Stone';h.el('checkout-email').value='ava@example.edu';h.el('checkout-secret').value='network-secret';
  await h.el('checkout-start').fire('submit');
  assert.equal(h.el('checkout-project').textContent,'Networking');assert.equal(h.el('checkout-username').value,'network01@pve');
  h.el('checkout-show').fire();assert.equal(h.el('checkout-password').type,'text');
  h.el('checkout-another-project').fire();
  assert.equal(h.el('checkout-start').hidden,false);assert.equal(h.el('checkout-result').hidden,true);
  assert.equal(h.el('checkout-name').value,'Ava Stone');assert.equal(h.el('checkout-email').value,'ava@example.edu');
  for(const id of ['checkout-secret','checkout-code','checkout-username','checkout-password'])assert.equal(h.el(id).value,'');
  for(const field of h.loginFields.values())assert.equal(field.value,'');
  assert.equal(h.el('checkout-password').type,'password');assert.equal(h.el('checkout-show')['aria-pressed'],'false');
  assert.equal(h.focused(),'checkout-secret');assert.match(h.el('checkout-feedback').textContent,/existing account stays reserved/);
  h.el('checkout-secret').value='second-secret';await h.el('checkout-start').fire('submit');
  assert.equal(h.el('checkout-project').textContent,'Forensics');assert.equal(h.el('checkout-username').value,'forensics01@pve');
  assert.equal(h.calls[0].body.email,h.calls[1].body.email);assert.equal(h.calls[1].body.secret,'second-secret');
  assert.ok(h.calls.every(call=>call.url.endsWith('/start')),'requesting another project must never relinquish the earlier account');
});

test('verification restart resets the challenge and secret without discarding the student identity',async()=>{
  const h=harness();h.el('checkout-name').value='Ava Stone';h.el('checkout-email').value='ava@example.edu';h.el('checkout-secret').value='verify-secret';
  await h.el('checkout-start').fire('submit');assert.equal(h.el('checkout-verify').hidden,false);
  h.el('checkout-code').value='12345678';h.el('checkout-restart').fire();
  assert.equal(h.el('checkout-verify').hidden,true);assert.equal(h.el('checkout-start').hidden,false);
  assert.equal(h.el('checkout-code').value,'');assert.equal(h.el('checkout-secret').value,'');assert.equal(h.el('checkout-name').value,'Ava Stone');
  assert.equal(h.el('checkout-email').value,'ava@example.edu');assert.equal(h.focused(),'checkout-email');
});
