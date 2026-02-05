(function(){
  const bulkForm = document.getElementById('bulkForm');
  const refreshBtn = document.getElementById('refreshBtn');
  const refreshMeta = document.getElementById('refreshMeta');
  const dockBody = document.getElementById('dockBody');
  const dock = document.getElementById('activityDock');
  const dockToggle = document.getElementById('dockToggle');
  const dockClear = document.getElementById('dockClear');
  const progressOverlay = document.getElementById('progressOverlay');
  const progressMessage = document.getElementById('progressMessage');
  const appConfig = document.getElementById('appConfig');
  const apiVmsUrl = (appConfig && appConfig.dataset && appConfig.dataset.apiVms) ? appConfig.dataset.apiVms : '/api/vms';
  const sessionResetUrl = (appConfig && appConfig.dataset && appConfig.dataset.sessionReset) ? appConfig.dataset.sessionReset : '/session-reset?reason=invalid';
  const jobsStatusUrl = (appConfig && appConfig.dataset && appConfig.dataset.jobsStatus) ? appConfig.dataset.jobsStatus : '/api/jobs';
  // Fixed-height dock (no resize)
  const btnStart = document.getElementById('btnStart');
  const btnPoweroff = document.getElementById('btnPoweroff');
  const btnRestore = document.getElementById('btnRestore');
  const hiddenAction = document.getElementById('hiddenBulkAction');
  const hiddenSnapshot = document.getElementById('hiddenSnapshot');
  const LOG_KEY = 'activityLogLines';
  function ts(){ return new Date().toISOString(); }
  function addLog(msg,type){
    if(!dockBody) return;
    const div=document.createElement('div');
    div.className='log-line'+(type?(' '+type):'');
    div.textContent='['+ts()+'] '+msg;
    dockBody.appendChild(div);
    dockBody.scrollTop = dockBody.scrollHeight;
    try { if(typeof console!== 'undefined' && console.debug){ console.debug('[dock]', div.textContent); } } catch(e){}
    try {
      const existing = JSON.parse(sessionStorage.getItem(LOG_KEY)||'[]');
      existing.push(div.textContent);
      if(existing.length>500) existing.splice(0, existing.length-500); // cap
      sessionStorage.setItem(LOG_KEY, JSON.stringify(existing));
    } catch(e){}
  }
  // Restore previous log entries
  try {
    const prev = JSON.parse(sessionStorage.getItem(LOG_KEY)||'[]');
    prev.forEach(line=>{ const div=document.createElement('div'); div.className='log-line'; div.textContent=line; dockBody.appendChild(div); });
    if(prev.length) dockBody.scrollTop = dockBody.scrollHeight;
  } catch(e){}
  // (Replaced by scroll-preserving toggle later after padding helper is defined)
  // Original simple toggle removed to prevent scroll jump.
  dockClear && dockClear.addEventListener('click',()=>{ if(dockBody){ dockBody.innerHTML=''; sessionStorage.removeItem(LOG_KEY); addLog('Activity log cleared','info'); }});
  // Update enabled/disabled state for central bulk buttons
  function updateBulkButtons(){
    const any = !!document.querySelector('.vm-item input[type=checkbox]:checked');
    if(btnStart) btnStart.disabled = !any;
    if(btnPoweroff) btnPoweroff.disabled = !any;
    if(btnRestore){
      btnRestore.disabled = !any;
    }
  }
  const vmCheckboxes = document.querySelectorAll('.vm-item input[type=checkbox]');
  vmCheckboxes.forEach(cb=>{ cb.addEventListener('change', updateBulkButtons); });
  updateBulkButtons();
  // Select / Deselect all controls
  const selectAllBtn = document.getElementById('selectAllBtn');
  const deselectAllBtn = document.getElementById('deselectAllBtn');
  selectAllBtn && selectAllBtn.addEventListener('click', ()=>{ vmCheckboxes.forEach(cb=>cb.checked=true); updateBulkButtons(); addLog('All VMs selected','info'); });
  deselectAllBtn && deselectAllBtn.addEventListener('click', ()=>{ vmCheckboxes.forEach(cb=>cb.checked=false); updateBulkButtons(); addLog('All VMs deselected','info'); });
  function setBusy(flag, label){ const btns=document.querySelectorAll('button'); btns.forEach(b=>{ if(flag){ if(!b.dataset.originalText){ b.dataset.originalText=b.textContent; } b.disabled=true; if(label) b.textContent=label; } else { b.disabled=false; if(b.dataset.originalText){ b.textContent=b.dataset.originalText; delete b.dataset.originalText; } } }); }
  function showProgress(msg){
    if(progressOverlay){
      if(progressMessage){ progressMessage.textContent = msg || 'Please wait.'; }
      progressOverlay.classList.add('visible');
      progressOverlay.setAttribute('aria-hidden','false');
    }
  }
  function hideProgress(){
    if(progressOverlay){
      progressOverlay.classList.remove('visible');
      progressOverlay.setAttribute('aria-hidden','true');
    }
  }
  function waitWithCountdown(seconds){
    const total = Math.max(0, parseInt(seconds, 10) || 0);
    if(!total) return Promise.resolve();
    return new Promise((resolve)=>{
      let remaining = total;
      if(progressMessage){
        progressMessage.textContent = 'Updating status, please wait... ('+remaining+'s)';
      }
      const timer = setInterval(()=>{
        remaining -= 1;
        if(progressMessage){
          progressMessage.textContent = 'Updating status, please wait... ('+Math.max(0, remaining)+'s)';
        }
        if(remaining <= 0){
          clearInterval(timer);
          resolve();
        }
      }, 1000);
    });
  }
  async function fetchJobsStatus(){
    const r = await fetch(jobsStatusUrl,{headers:{'Accept':'application/json'}});
    if(r.status === 401){
      let redirectTarget = sessionResetUrl;
      try {
        const data = await r.json();
        if(data && data.redirect){ redirectTarget = data.redirect; }
      } catch(ignore){}
      addLog('Session expired while waiting for jobs; redirecting','warn');
      setTimeout(()=>{ window.location.href = redirectTarget; }, 250);
      return null;
    }
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }
  function waitForJobsToComplete(){
    if(!jobsStatusUrl) return Promise.resolve(null);
    let attempts = 0;
    return new Promise((resolve)=>{
      const poll = async ()=>{
        attempts++;
        try {
          const data = await fetchJobsStatus();
          if(!data){ resolve(null); return; }
          const total = data.total || 0;
          const done = data.done || 0;
          const failed = data.failed || 0;
          if(total === 0){ resolve(data); return; }
          if(progressMessage){
            progressMessage.textContent = 'Jobs complete: '+done+' / '+total+(failed?(' (failed '+failed+')'):'');
          }
          if(done >= total){
            addLog('All jobs completed: '+done+' total, '+failed+' failed','info');
            resolve(data);
            return;
          }
        } catch(e){
          addLog('Job status check failed: '+e.message,'error');
        }
        const delay = Math.min(2000 + attempts*200, 6000);
        setTimeout(poll, delay);
      };
      setTimeout(poll, 250);
    });
  }
  if(bulkForm){ bulkForm.addEventListener('submit', function(ev){
    const selected=[...document.querySelectorAll('.vm-item input[type=checkbox]:checked')].map(cb=>cb.value);
    // Determine action from submitter OR hidden action field (for floating panel submission)
    const hiddenActionInput = bulkForm.querySelector('input[name=action]');
    const actionBtn = (ev && ev.submitter && ev.submitter.value) || (document.activeElement && document.activeElement.value) || (hiddenActionInput && hiddenActionInput.value) || '(unknown)';
    if(!selected.length){ ev.preventDefault(); addLog('No VMs selected; action aborted','warn'); return; }
    // Confirmation dialog before submitting
    const previewList = selected.slice(0,15).map(v=>v.split('|')[2]).join(', ')+(selected.length>15?' ...':'');
    // Use an escaped \n for readability in the confirm dialog
    const confirmMsg = (actionBtn === 'restore-all')
      ? 'RESTORE WARNING: This will revert selected VMs to a snapshot and all current data will be removed.\nProceed with RESTORE on '+selected.length+' VM(s)?\nVMIDs: '+previewList
      : 'Proceed with '+actionBtn.toUpperCase()+' on '+selected.length+' VM(s)?\nVMIDs: '+previewList; 
    if(!window.confirm(confirmMsg)){
      ev.preventDefault();
      addLog('Bulk '+actionBtn+' canceled by user','warn');
      // clear hidden action so future attempts can set it again
      if(hiddenActionInput) hiddenActionInput.value='';
      hideProgress();
      return;
    }
    addLog('DEBUG bulk submit (pre) action_btn='+actionBtn+' total_selected='+selected.length+' values=['+selected.join(',')+'] formAction='+bulkForm.getAttribute('action'),'info');
    showProgress('Submitting '+actionBtn+' for '+selected.length+' VM(s)...');
    // Disable buttons but keep their labels unchanged
    setTimeout(()=>{ setBusy(true); addLog('Bulk action submitted (deferred disable)','info'); }, 25);
  }); }
  async function doRefresh(skipJobWait){
    if(!refreshBtn) return;
    setBusy(true);
    showProgress('Checking for active jobs...');
    try {
      if(!skipJobWait){
        const jobSnap = await fetchJobsStatus();
        if(jobSnap && jobSnap.total && jobSnap.done < jobSnap.total){
          showProgress('Waiting for jobs to complete before refresh...');
          await waitForJobsToComplete();
        }
      }
      await waitWithCountdown(10);
      showProgress('Refreshing VM status...');
      const r = await fetch(apiVmsUrl,{headers:{'Accept':'application/json'}});
      if(r.status === 401){
        let redirectTarget = sessionResetUrl;
        try {
          const data = await r.json();
          if(data && data.redirect){ redirectTarget = data.redirect; }
        } catch(ignore){}
        addLog('Session expired; redirecting to sign-in','warn');
        if(refreshMeta){ refreshMeta.textContent='Session expired; redirecting...'; }
        setTimeout(()=>{ window.location.href = redirectTarget; }, 250);
        return;
      }
      if(!r.ok) throw new Error('HTTP '+r.status);
      const data = await r.json();
      let updated=0;
      (data.vms||[]).forEach(vm=>{
        const id='vm-status-'+vm.node+'-'+vm.vmid;
        const el=document.getElementById(id);
        if(el){
          const old=el.textContent;
          if(old!==vm.status){
            el.textContent=vm.status;
            el.className='vm-status '+vm.status+' changed';
            setTimeout(()=>{ el.classList.remove('changed'); },1200);
          }
          updated++;
        }
      });
      if(refreshMeta){
        const stamp = (new Date()).toLocaleTimeString();
        const label = updated === 1 ? 'status' : 'statuses';
        refreshMeta.textContent='Last refresh: '+stamp+' - '+updated+' '+label+' updated';
      }
      addLog('Refresh completed ('+updated+' statuses)','info');
    } catch(e){
      addLog('Refresh failed: '+e.message,'error');
      if(refreshMeta){ refreshMeta.textContent='Last refresh failed'; }
    } finally {
      setBusy(false);
      hideProgress();
    }
  }
  if(refreshBtn){ refreshBtn.addEventListener('click', doRefresh); }
  const params = new URLSearchParams(window.location.search);
  const lastAction = {
    action: params.get('bulk'),
    done: params.get('done'),
    failed: params.get('failed'),
    skipped: params.get('skipped')
  };
  if(lastAction && lastAction.action){ addLog('Bulk '+lastAction.action+' summary: '+(lastAction.done||0)+' ok, '+(lastAction.failed||0)+' failed'+(lastAction.skipped?(', '+lastAction.skipped+' skipped'):'') , (parseInt(lastAction.failed||0)>0)?'warn':'success'); }
  const failListRaw = params.get('fail_list');
  const successListRaw = params.get('success_list');
  const skipListRaw = params.get('skip_list');
  if(successListRaw){ successListRaw.split(';').forEach(s=>{ if(s.trim()) addLog('OK '+s.trim(),'success'); }); }
  if(skipListRaw){ skipListRaw.split(';').forEach(s=>{ if(s.trim()) addLog('SKIP '+s.trim(),'info'); }); }
  if(failListRaw){ failListRaw.split(';').forEach(f=>{ if(f.trim()) addLog('FAIL '+f.trim(),'error'); }); }
  // Auto-refresh disabled per user request.
  if(params.get('jobs') === '1'){
    showProgress('Waiting for jobs to complete...');
    setBusy(true);
    waitForJobsToComplete()
      .then(()=>doRefresh(true))
      .finally(()=>{ setBusy(false); hideProgress(); });
  }

  // Intercept VM card link clicks to open popup window instead of a new tab
  const vmLinks = document.querySelectorAll('.vm-list .vm-item a');
  vmLinks.forEach(a=>{
    a.addEventListener('click', function(ev){
      // Only intercept simple left click (no modifiers). Otherwise let browser handle (incl. Ctrl/Cmd+click new tab).
      if(ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      const url = this.href;
      const vmid = this.getAttribute('data-vmid') || 'vm';
      const features = 'width=1100,height=760,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes';
      let win = null;
      try { win = window.open(url, 'vm_console_'+vmid, features); } catch(e) { /* ignore */ }
      if(!win){
        // Try a plain new tab
        try { win = window.open(url, '_blank'); } catch(e) { /* ignore */ }
      }
      if(!win){
        addLog('Popup blocked; falling back to same-tab navigation','warn');
        window.location.href = url;
        return;
      }
      try { win.focus(); } catch(e){}
      addLog('Opened console '+(win===window?'(same tab) ':'')+'for VM '+vmid,'info');
    });
  });

  // Reworked dock: inline frame with resize + collapse
  if(dock){
    const resizeHandle = dock.querySelector('.dock-resize-handle');
    let isResizing=false; let startY=0; let startH=0;
    function applyHeight(h){
      const min=34; const max = Math.min(window.innerHeight*0.75, 600);
      h=Math.max(min, Math.min(max, h));
      dock.style.height = h+"px";
    }
    if(resizeHandle){
      resizeHandle.addEventListener('mousedown', (e)=>{
        if(dock.classList.contains('collapsed')) return;
        isResizing=true; startY=e.clientY; startH=dock.getBoundingClientRect().height; dock.classList.add('resizing');
        e.preventDefault();
      });
      window.addEventListener('mousemove', (e)=>{ if(!isResizing) return; const delta = startY - e.clientY; applyHeight(startH + delta); });
      window.addEventListener('mouseup', ()=>{ if(isResizing){ isResizing=false; dock.classList.remove('resizing'); }});
    }
    if(dockToggle){
      if(!dock.classList.contains('collapsed')){
        dockToggle.textContent = 'v';
        dockToggle.setAttribute('aria-expanded', 'true');
        if(!dock.style.height || parseInt(dock.style.height,10) < 120){
          applyHeight(Math.round(window.innerHeight * 0.28));
        }
      }
      dockToggle.addEventListener('click', ()=>{
        const collapsed = dock.classList.toggle('collapsed');
        dockToggle.textContent = collapsed ? '^' : 'v';
        dockToggle.setAttribute('aria-expanded', String(!collapsed));
        if(!collapsed){
          // Expand to previous or default height
          if(!dock.style.height || parseInt(dock.style.height,10) < 120){
            applyHeight(Math.round(window.innerHeight * 0.28));
          }
          if(dockBody){ dockBody.scrollTop = dockBody.scrollHeight; }
        }
      });
    }
  }
  // Bulk action triggers (outside dock conditional so they work even if dock hidden)
  updateBulkButtons();
  function triggerAction(action){
    if(!bulkForm) return;
    if(hiddenSnapshot) hiddenSnapshot.value = '';
    hiddenAction.value = action;
    showProgress('Submitting '+action+' request...');
    let canceled = false;
    const preValue = hiddenAction.value;
    const evt = new Event('submit', {cancelable:true});
    if(!bulkForm.dispatchEvent(evt)) canceled = true; // if any listener called preventDefault via legacy path
    // If listener prevented default, canceled stays true (bulkForm listener uses preventDefault on cancel)
    if(hiddenAction.value != preValue) canceled = true; // listener cleared hidden action when canceled
    if(!canceled){
      try { bulkForm.submit(); } catch(e){ addLog('Submit error: '+e.message,'error'); }
    } else {
      hideProgress();
    }
  }
  btnStart && btnStart.addEventListener('click', ()=>triggerAction('start'));
  btnPoweroff && btnPoweroff.addEventListener('click', ()=>triggerAction('poweroff'));
  btnRestore && btnRestore.addEventListener('click', ()=>triggerAction('restore-all'));
})();
