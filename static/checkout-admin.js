(function () {
  const root=document.getElementById('checkout-admin');
  const poolSelect=document.getElementById('admin-pool');
  const feedback=document.getElementById('admin-feedback');
  let pools=[],pool=null,busy=false;
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function api(path,method='GET',data) {
    const response=await fetch('/api/admin/checkouts'+path,{method,credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Checkout-CSRF':root.dataset.csrf},body:data===undefined?undefined:JSON.stringify(data)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Request failed.');
    return result;
  }
  function message(text,error=false){feedback.textContent=text;feedback.className=error?'error':'';}
  function rows(){
    const filter=document.getElementById('admin-filter').value.toLowerCase();
    const body=document.querySelector('#admin-accounts tbody');
    body.innerHTML=Object.entries(pool?.accounts||{}).filter(([name,a])=>(name+' '+(a.name||'')+' '+(a.email||'')).toLowerCase().includes(filter)).map(([name,a])=>`<tr><td style="padding:.6rem; border-top:1px solid var(--muted)"><input type="checkbox" data-account="${esc(name)}" aria-label="Select ${esc(name)}" style="width:auto; margin:0"></td><td style="padding:.6rem; border-top:1px solid var(--muted); overflow-wrap:anywhere">${esc(name)}${a.password_rotated?'<div class="muted" style="font-size:.7rem">Legacy password differs</div>':''}</td><td style="padding:.6rem; border-top:1px solid var(--muted)">${a.releasing?'Disable pending':a.login_disabled?'Disabled':a.email?'Checked out':a.eligible?'Available':'Permanent'}</td><td style="padding:.6rem; border-top:1px solid var(--muted); overflow-wrap:anywhere">${esc(a.name||'—')}</td><td style="padding:.6rem; border-top:1px solid var(--muted); overflow-wrap:anywhere">${esc(a.email||'—')}</td><td style="padding:.6rem; border-top:1px solid var(--muted)">${(a.checked_out_at||a.assigned_at)?esc(new Date((a.checked_out_at||a.assigned_at)*1000).toLocaleString()):'—'}</td><td style="padding:.6rem; border-top:1px solid var(--muted)">${esc(a.checkout_ip||'—')}</td><td style="padding:.6rem; border-top:1px solid var(--muted)">${a.relinquished_at?esc(new Date(a.relinquished_at*1000).toLocaleString()):'—'}</td></tr>`).join('');
    body.querySelectorAll('[data-account]').forEach(box=>box.addEventListener('change',updateSelection));
    document.getElementById('admin-select-all').checked=false;
    updateSelection();
  }
  function render(){
    pool=pools.find(p=>p.id===poolSelect.value)||null;
    document.getElementById('admin-empty').hidden=!!pools.length;
    document.getElementById('admin-pool-panel').hidden=!pool;
    updateSelection();
    if(!pool)return;
    document.getElementById('admin-enabled').checked=pool.enabled;
    document.getElementById('admin-verify').checked=pool.verify_email;
    document.getElementById('admin-secret-label').textContent=`Lab secret for ${pool.name}`;
    const secret=document.getElementById('admin-secret');secret.value='';secret.placeholder=pool.secret_set?'A secret is set for this project; enter a new one to replace it':'Set this project’s lab secret (8+ characters)';
    document.getElementById('admin-policy-note').textContent=!pool.verify_email?'Verification is off: anyone with the lab secret and an email can retrieve that email’s credentials.':pool.smtp_configured?'Students must verify their email before receiving credentials.':'Configure SMTP on this AccessForge server before verified checkout can be used.';
    rows();
  }
  async function refresh(){
    const current=poolSelect.value;
    const data=await api('/pools');pools=data.pools;
    poolSelect.replaceChildren(...pools.map(p=>{const option=document.createElement('option');option.value=p.id;option.textContent=p.name;return option;}));
    if(pools.some(p=>p.id===current))poolSelect.value=current;
    render();
  }
  async function run(fn){
    if(busy)return;busy=true;root.querySelectorAll('button,select').forEach(b=>b.disabled=true);
    try{await fn();}catch(error){message(error.message,true);}finally{busy=false;root.querySelectorAll('button,select').forEach(b=>b.disabled=false);updateSelection();}
  }
  poolSelect.addEventListener('change',()=>{render();message('');});
  document.getElementById('admin-filter').addEventListener('input',rows);
  document.getElementById('admin-refresh').addEventListener('click',()=>run(async()=>{await refresh();message('Status refreshed.');}));
  document.getElementById('admin-save').addEventListener('click',()=>run(async()=>{
    if(!pool)return;
    await api('/pools/'+encodeURIComponent(pool.id),'PATCH',{enabled:document.getElementById('admin-enabled').checked,verify_email:document.getElementById('admin-verify').checked,secret:document.getElementById('admin-secret').value});
    await refresh();message('Checkout settings saved.');
  }));
  function updateSelection(){
    const boxes=Array.from(root.querySelectorAll('[data-account]'));
    const count=boxes.filter(box=>box.checked).length;
    document.getElementById('admin-selection-count').textContent=`${count} selected`;
    const all=document.getElementById('admin-select-all');
    all.checked=!!boxes.length&&count===boxes.length;
    all.indeterminate=count>0&&count<boxes.length;
    root.querySelectorAll('[data-action-menu]').forEach(menu=>menu.disabled=busy||!pool||!count);
  }
  document.getElementById('admin-select-all').addEventListener('change',event=>{
    root.querySelectorAll('[data-account]').forEach(box=>box.checked=event.target.checked);updateSelection();
  });
  root.querySelectorAll('[data-action-menu]').forEach(menu=>menu.addEventListener('change',()=>{
    const action=menu.value;menu.value='';
    if(!action)return;
    return run(async()=>{
      if(!pool)return;
      const usernames=Array.from(root.querySelectorAll('[data-account]:checked'),box=>box.dataset.account);
      if(!usernames.length)throw new Error('Select at least one account.');
      let email;
      if(action==='assign'){if(usernames.length!==1)throw new Error('Select one account to assign.');email=prompt('Student email address:');if(!email)return;}
      if((action==='release'||action==='remove')&&!confirm(action==='release'?`Relinquish ${usernames.length} selected account(s)? Students will lose access and their Proxmox logins will be disabled until enabled again.`:`Remove ${usernames.length} unassigned account(s) from this checkout pool?`))return;
      const data=await api('/pools/'+encodeURIComponent(pool.id),'POST',{action,usernames,email});
      await refresh();const errors=(data.results||[]).filter(row=>!row.released);
      const success={enable:'Selected accounts marked On-Demand.',disable:'Selected accounts marked Permanent.',assign:'Account assigned.',release:'Selected accounts relinquished. Their logins remain disabled.',remove:'Selected accounts removed.'}[action]||'Accounts updated.';
      message(errors.length?errors.map(row=>`${row.username}: ${row.error}`).join(' '):success,!!errors.length);
    });
  }));
  run(refresh);
})();
