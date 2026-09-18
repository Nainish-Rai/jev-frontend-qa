// Loaded-page reading; adapted MIT-licensed components are credited in THIRD_PARTY_NOTICES.md.
((query, offset) => {
  const scope = 'Loaded rendered main-document text only; excludes editable values. No scrolling performed. Search misses do not prove absence.';
  const base = {url: location.href, title: document.title, query, offset, scope, links: []};
  if (!document.body) return {...base, text: '', next_offset: null, scan_limited: true};
  const visible = element => {
    if (!element || element.closest('script,style,noscript,template,input,textarea,select,[contenteditable],[hidden],[aria-hidden="true"],[inert]')) return false;
    for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      if (style.display === 'none' || ['hidden','collapse'].includes(style.visibility) || style.opacity === '0') return false;
      if (ancestor.tagName === 'DETAILS' && !ancestor.open && !ancestor.querySelector(':scope > summary')?.contains(element)) return false;
    }
    return element.getClientRects().length > 0;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const lines = [];
  let node, scanned = 0, chars = 0, scan_limited = false;
  while ((node = walker.nextNode())) {
    if (++scanned > 30000 || chars >= 200000) { scan_limited = true; break; }
    if (!visible(node.parentElement)) continue;
    const text = node.textContent.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const remaining = 200000 - chars;
    lines.push(text.slice(0, remaining)); chars += Math.min(text.length, remaining) + 1;
    if (text.length > remaining) scan_limited = true;
  }
  const links = [], seen = new Set();
  for (const anchor of document.querySelectorAll('a[href]')) {
    if (!visible(anchor)) continue;
    try {
      const url = new URL(anchor.href);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || seen.has(url.href)) continue;
      if (links.length >= 200) { scan_limited = true; break; }
      seen.add(url.href);
      links.push({url: url.href, label: anchor.innerText.replace(/\s+/g,' ').trim().slice(0,300)});
    } catch { /* Non-navigation URLs are not planner candidates. */ }
  }
  const text = lines.join('\n');
  let content, next_offset = null;
  if (query) {
    const lower = text.toLowerCase(), needle = query.toLowerCase(), excerpts = [];
    let cursor = offset, size = 0;
    while (cursor < text.length) {
      const index = lower.indexOf(needle, cursor);
      if (index < 0) break;
      const excerpt = text.slice(Math.max(0,index-200), Math.min(text.length,index+query.length+300));
      if (size + excerpt.length + 3 > 6000) { next_offset = index; break; }
      excerpts.push(excerpt); size += excerpt.length + 3;
      cursor = index + Math.max(1, needle.length);
    }
    content = excerpts.join('\n…\n');
  } else {
    content = text.slice(offset, offset + 6000);
    if (offset + 6000 < text.length) next_offset = offset + 6000;
  }
  return {...base, text: content, links, next_offset, scan_limited};
})(__JEV_READ_ARGUMENTS__)
