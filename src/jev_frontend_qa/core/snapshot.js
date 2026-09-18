// snapshot.js — adapted from jev-ultrafast (MIT). Original copyright:
// Copyright (c) 2026 Browser Use. See THIRD_PARTY_NOTICES.md for the full notice.
// Accessibility adaptations are credited in THIRD_PARTY_NOTICES.md.
//
// Atomically read visible content and controls, preserving actual DOM node
// identity. The runner never lets the model generate selectors; node IDs are
// looked up only by code, immediately before input.

(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],[contenteditable="plaintext-only"],[contenteditable=""],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  const axByNode = new Map((cache.axRecords||[]).map(record => [cache.axNodes?.get(record.backend_node_id),record]).filter(([e])=>e?.isConnected));
  const contextFor = e => {
    const contexts=[];
    for(let p=e.parentElement;p&&contexts.length<4;p=p.parentElement) {
      if(!p.matches('form,dialog,[role="dialog"],[role="alertdialog"],fieldset,[role="region"],[role="group"]')) continue;
      const heading=p.querySelector('legend,h1,h2,h3,h4,h5,h6,[role="heading"]');
      contexts.unshift({role:p.getAttribute('role')||p.tagName.toLowerCase(),
        label:(p.getAttribute('aria-label')||heading?.textContent||'').trim().slice(0,160)});
    }
    return contexts;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly,
        e.validity?.valid,e.validationMessage,e.getAttribute('aria-invalid')]),
    document.title,document.body.innerText.slice(0,6000)];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-pressed'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),e.getAttribute('name'),e.validity?.valid??null,
      e.validationMessage??null,e.getAttribute('aria-invalid'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[], controls=[], links=[], outside={above:[],below:[]};
  const candidates=new Set([...document.querySelectorAll(selector),...axByNode.keys()]);
  for (const e of candidates) {
    if (!safe(e) || !visible(e) || e.getRootNode()!==document) continue;
    const ax=axByNode.get(e);
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=ax?.role||role(e);
    if (!rname || r.width<=0 || r.height<=0) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const offscreen=x<0||x>=innerWidth||y<0||y>=innerHeight;
    const hit=offscreen?null:document.elementFromPoint(x,y);
    const availability=e.matches(':disabled')||e.closest('[aria-disabled="true"]')||ax?.states.disabled===true?'disabled':
      offscreen?'offscreen':!hit||(hit!==e&&!e.contains(hit))?'occluded':'available';
    const base={node:identity(e),role:rname,label:ax?.label||name(e)||rname,
      context:ax?.context?.length?ax.context:contextFor(e),availability,
      fixture_key:e.getAttribute('name')||null,
      unsupported:e.tagName==='A' && !!e.target && e.target!=='_self',
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    base.control_label=base.label;
    base.focused=document.activeElement===e;
    if(ax) base.accessibility={backend_node_id:ax.backend_node_id,role:ax.role,name:ax.label};
    for (const key of ['checked','pressed','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.willValidate) base.validation={valid:e.validity.valid,
      message:e.validationMessage,value_missing:e.validity.valueMissing,
      too_long:e.validity.tooLong,pattern_mismatch:e.validity.patternMismatch};
    if (e.hasAttribute('aria-invalid')) base.aria_invalid=e.getAttribute('aria-invalid');
    const value='value' in e ? String(e.value) : e.isContentEditable ? e.innerText : '';
    if(['textbox','searchbox','spinbutton','combobox'].includes(rname)) base.value=value;
    const popupIds=(e.getAttribute('aria-controls')||e.getAttribute('aria-owns')||'').split(/\s+/).filter(Boolean);
    if(popupIds.length) base.option_labels=popupIds.flatMap(id=>[...(document.getElementById(id)?.querySelectorAll('[role="option"]')||[])]
      .filter(option=>visible(option)).map(option=>name(option)));
    const active=document.getElementById(e.getAttribute('aria-activedescendant')||'');
    if(active && popupIds.some(id=>document.getElementById(id)?.contains(active)) && visible(active)) base.active_option=name(active);
    controls.push({...base});
    if(e.tagName==='A') {
      try {
        const url=new URL(e.href);
        if(['http:','https:'].includes(url.protocol)&&!url.username&&!url.password)
          links.push({url:url.href,label:base.label});
      } catch {}
    }
    if(availability!=='available') {
      if(availability==='offscreen' && (y<0||y>=innerHeight)) outside[y<0?'above':'below'].push({node:base.node,label:base.label});
      continue;
    }
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
      if(editable || rname==='combobox') {
        for(const key of ['Enter','Escape',...(['combobox','searchbox'].includes(rname)?['ArrowDown','ArrowUp']:[])])
          actions.push({...base,kind:'key',key,label:base.label+' → '+key});
      }
    }
  }
  const scrollers=[...document.querySelectorAll('*')].filter(e=>visible(e)&&e.clientHeight>0&&
    e.scrollHeight>e.clientHeight+2&&['auto','scroll'].includes(getComputedStyle(e).overflowY));
  for(const e of scrollers) {
    const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);
    if(x<0||y<0||x>=innerWidth||y>=innerHeight||!hit||(hit!==e&&!e.contains(hit))) continue;
    const base={node:identity(e),role:'region',label:name(e).slice(0,120)||'Scrollable region',
      kind:'scroll_element',scroll_y:e.scrollTop,rect:{x:r.x,y:r.y,w:r.width,h:r.height},context:contextFor(e)};
    controls.push({...base,availability:'available'});
    const delta=Math.max(1,Math.floor(e.clientHeight*0.7));
    if(e.scrollTop+e.clientHeight<e.scrollHeight-2) actions.push({...base,delta,label:base.label+' → Scroll down'});
    if(e.scrollTop>0) actions.push({...base,delta:-delta,label:base.label+' → Scroll up'});
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  const clipped = (rect,parent) => {
    let top=Math.max(0,rect.top),bottom=Math.min(innerHeight,rect.bottom),
      left=Math.max(0,rect.left),right=Math.min(innerWidth,rect.right);
    for(let e=parent;e&&e!==document.body;e=e.parentElement) {
      const style=getComputedStyle(e),box=e.getBoundingClientRect();
      if(['auto','scroll','hidden','clip'].includes(style.overflowY)) {
        top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);
      }
      if(['auto','scroll','hidden','clip'].includes(style.overflowX)) {
        left=Math.max(left,box.left);right=Math.min(right,box.right);
      }
    }
    return bottom<=top||right<=left;
  };
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth && !clipped(r,parent)) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250)+
    Math.max(0,outside.above.length-250)+Math.max(0,outside.below.length-250);
  outside.above.splice(250); outside.below.splice(250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  const scrollDelta=Math.max(1,Math.floor(innerHeight*0.7));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:scrollDelta,reveals:outside.below});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-scrollDelta,reveals:outside.above});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,
    controls:controls.slice(0,250),links:links.slice(0,200),
    diagnostics:{omitted_controls:Math.max(0,controls.length-250),omitted_links:Math.max(0,links.length-200)},
    truncated_text:length>6000||!!node,
    unsupported:!!document.querySelector('iframe,object,embed'),
    metadata:{...document.documentElement.dataset}};
})();
