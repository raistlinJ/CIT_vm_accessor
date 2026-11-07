(function(){
  const button=document.getElementById('relinquish-account');
  if(!button)return;
  let state=null,busy=false;
  fetch('/api/account-checkout/session',{credentials:'same-origin',cache:'no-store'}).then(async response=>{
    if(!response.ok)return;
    state=await response.json();button.hidden=!state.assigned;
  }).catch(()=>{});
  button.addEventListener('click',async()=>{
    if(busy||!state?.assigned)return;
    if(state.requires_signin){alert('Sign in again before relinquishing your account.');location.assign('/login');return;}
    if(!confirm('Relinquish this account? You will lose all your work and access to this account. Save any files you need before continuing. Your AccessForge access will be disabled until an administrator re-enables it. You will be signed out.'))return;
    busy=true;button.disabled=true;button.textContent='Relinquishing…';
    try{
      const response=await fetch('/api/account-checkout/relinquish',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Checkout-CSRF':state.csrf},body:JSON.stringify({confirmed:true})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not relinquish the account.');
      location.assign(data.redirect||'/login');
    }catch(error){alert(error.message||'Could not relinquish your account. Try again.');busy=false;button.disabled=false;button.textContent='Relinquish account';}
  });
})();
