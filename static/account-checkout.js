(function () {
  const start = document.getElementById('checkout-start');
  const verify = document.getElementById('checkout-verify');
  const result = document.getElementById('checkout-result');
  const feedback = document.getElementById('checkout-feedback');
  let challenge = '', busy = false;
  async function request(action, body) {
    if (busy) return;
    busy = true;
    start.querySelector('button').disabled = true;
    verify.querySelectorAll('button').forEach(button => { button.disabled = true; });
    feedback.textContent = 'Please wait…';
    feedback.className = '';
    try {
      const response = await fetch(`/api/account-checkout/${action}`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-Checkout-CSRF': start.dataset.csrf},
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Checkout failed. Please try again.');
      feedback.textContent = '';
      if (data.status === 'verification_required') {
        challenge = data.challenge;
        start.hidden = true; verify.hidden = false;
        document.getElementById('checkout-secret').value = '';
        document.getElementById('checkout-code').focus();
      } else if (data.status === 'assigned') {
        start.hidden = true; verify.hidden = true; result.hidden = false;
        document.getElementById('checkout-project').textContent = data.project;
        document.getElementById('checkout-assignment').textContent = `Assigned to ${data.name?data.name+' ('+data.email+')':data.email}. Return with the same email and this project’s secret to recover this account.`;
        document.getElementById('checkout-username').value = data.username;
        document.getElementById('checkout-password').value = data.password;
        const login = document.getElementById('checkout-login');
        const fields = {username:data.username, password:data.password, ...data.connection, verify_ssl:data.connection.verify_ssl ? '1' : ''};
        for (const [name, value] of Object.entries(fields)) {
          if (login.elements.namedItem(name)) login.elements.namedItem(name).value = value;
        }
        document.getElementById('checkout-secret').value = '';
        document.getElementById('checkout-code').value = '';
        document.getElementById('checkout-username').focus();
      }
    } catch (error) {
      feedback.textContent = error.message || 'Could not reach checkout. Please try again.';
      feedback.className = 'error';
    } finally {
      busy = false;
      start.querySelector('button').disabled = false;
      verify.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  start.addEventListener('submit', event => {
    event.preventDefault();
    return request('start', {name: document.getElementById('checkout-name').value.trim(), email: document.getElementById('checkout-email').value.trim(), secret: document.getElementById('checkout-secret').value});
  });
  verify.addEventListener('submit', event => {
    event.preventDefault();
    return request('verify', {challenge, code: document.getElementById('checkout-code').value.trim()});
  });
  function resetCheckout(anotherProject=false){
    challenge='';verify.hidden=true;result.hidden=true;start.hidden=false;
    for(const id of ['checkout-code','checkout-secret','checkout-username','checkout-password'])document.getElementById(id).value='';
    document.getElementById('checkout-password').type='password';
    const show=document.getElementById('checkout-show');show.textContent='Show password';show.setAttribute('aria-pressed','false');
    document.getElementById('checkout-project').textContent='';document.getElementById('checkout-assignment').textContent='';
    const login=document.getElementById('checkout-login');
    for(const name of ['username','password','host','port','realm','verify_ssl']){const field=login.elements.namedItem(name);if(field)field.value='';}
    feedback.className='';feedback.textContent=anotherProject?'Your existing account stays reserved. Enter another project’s secret to get its account.':'';
    document.getElementById(anotherProject?'checkout-secret':'checkout-email').focus();
  }
  document.getElementById('checkout-restart').addEventListener('click',()=>resetCheckout());
  document.getElementById('checkout-another-project').addEventListener('click',()=>resetCheckout(true));
  document.getElementById('checkout-show').addEventListener('click', event => {
    const password = document.getElementById('checkout-password');
    const visible = password.type === 'password';
    password.type = visible ? 'text' : 'password';
    event.currentTarget.textContent = visible ? 'Hide password' : 'Show password';
    event.currentTarget.setAttribute('aria-pressed', String(visible));
  });
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    const input = document.getElementById(button.dataset.copy);
    try { await navigator.clipboard.writeText(input.value); feedback.textContent = 'Copied.'; }
    catch { input.focus(); input.select(); feedback.textContent = 'Select the field and copy it manually.'; }
  }));
})();
