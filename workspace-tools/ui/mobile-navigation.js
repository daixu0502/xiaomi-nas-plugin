/* Event-driven mobile navigation. No timers or network polling. */
(function (global) {
  'use strict';
  var client=global.XiaomiPluginClient;
  if (!client) return;
  var root=client.document.documentElement, nav=root.querySelector('.tabs');
  if (!nav || nav.dataset.mobileNavigation==='ready') return;
  nav.dataset.mobileNavigation='ready';
  var originalLabel=nav.getAttribute('aria-label') || '功能导航';
  var frame=0, reveal=false, smooth=false, activeBefore=null, disposed=false;
  function visibleButtons() {
    return Array.from(nav.querySelectorAll('.tab')).filter(function (button) { return !button.hidden && button.getClientRects().length && !button.disabled; });
  }
  function refresh() {
    frame=0;
    if (disposed || !nav.isConnected) return;
    if (root.classList.contains('desktop-client')) { reveal=false; smooth=false; return; }
    var buttons=visibleButtons(), active=buttons.find(function (button) { return button.classList.contains('active'); });
    buttons.forEach(function (button) {
      var wanted=button===active?'page':null;
      if (wanted && button.getAttribute('aria-current')!==wanted) button.setAttribute('aria-current',wanted);
      else if (!wanted && button.hasAttribute('aria-current')) button.removeAttribute('aria-current');
    });
    if (active!==activeBefore) reveal=true;
    activeBefore=active;
    if (reveal && active) {
      var boundary=nav.getBoundingClientRect(), item=active.getBoundingClientRect(), scale=boundary.width/(nav.offsetWidth || 1), next=nav.scrollLeft;
      var inset=6*scale;
      if (item.left<boundary.left+inset) next-=(boundary.left+inset-item.left)/scale;
      else if (item.right>boundary.right-inset) next+=(item.right-boundary.right+inset)/scale;
      next=Math.max(0,Math.min(next,nav.scrollWidth-nav.clientWidth));
      if (Math.abs(next-nav.scrollLeft)>1) nav.scrollTo({left:next,behavior:smooth&&!global.matchMedia('(prefers-reduced-motion: reduce)').matches?'smooth':'auto'});
    }
    reveal=false; smooth=false;
    var overflow=nav.scrollWidth>nav.clientWidth+1;
    nav.dataset.scrollLeft=String(overflow&&nav.scrollLeft>1);
    nav.dataset.scrollRight=String(overflow&&nav.scrollLeft<nav.scrollWidth-nav.clientWidth-1);
    var label=originalLabel+(overflow?'，左右滑动查看更多功能':'');
    if (nav.getAttribute('aria-label')!==label) nav.setAttribute('aria-label',label);
  }
  function schedule(shouldReveal,shouldSmooth) {
    if (disposed) return;
    reveal=reveal||!!shouldReveal; smooth=smooth||!!shouldSmooth;
    if (!frame) frame=global.requestAnimationFrame(refresh);
  }
  function click(event) { if (event.target.closest('.tab')) schedule(true,true); }
  function scroll() { schedule(false,false); }
  function resize() { schedule(true,false); }
  function keys(event) {
    if (root.classList.contains('desktop-client') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    var current=event.target.closest('.tab'), buttons=visibleButtons(), index=buttons.indexOf(current);
    if (index<0) return;
    event.preventDefault();
    var next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
    buttons[next].focus({preventScroll:true}); buttons[next].click();
  }
  // Content swipe gestures must not activate another page while a user pans the menu.
  function gesture(event) { event.stopPropagation(); }
  nav.addEventListener('click',click);
  nav.addEventListener('keydown',keys);
  nav.addEventListener('scroll',scroll,{passive:true});
  nav.addEventListener('touchstart',gesture,{passive:true});
  nav.addEventListener('touchend',gesture,{passive:true});
  global.addEventListener('resize',resize);
  var mutation=new MutationObserver(function () { schedule(true,false); });
  mutation.observe(nav,{subtree:true,childList:true,attributes:true,attributeFilter:['class','hidden']});
  var rootMutation=new MutationObserver(resize);rootMutation.observe(root,{attributes:true,attributeFilter:['class']});
  var observer=global.ResizeObserver?new global.ResizeObserver(resize):null;
  if (observer) observer.observe(nav);
  if (root.ownerDocument.fonts) root.ownerDocument.fonts.ready.then(resize);
  schedule(true,false);
  function dispose() {
    disposed=true;if(frame)global.cancelAnimationFrame(frame);mutation.disconnect();rootMutation.disconnect();if(observer)observer.disconnect();
    nav.removeEventListener('click',click);nav.removeEventListener('keydown',keys);nav.removeEventListener('scroll',scroll);
    nav.removeEventListener('touchstart',gesture);nav.removeEventListener('touchend',gesture);global.removeEventListener('resize',resize);
    global.removeEventListener('pagehide',dispose);global.removeEventListener('unmount',dispose);
  }
  global.addEventListener('pagehide',dispose);global.addEventListener('unmount',dispose);
})(window);
