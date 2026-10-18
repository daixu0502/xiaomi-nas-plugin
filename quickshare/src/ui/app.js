(function(){
'use strict';
var document = window.XiaomiPluginClient.document;
var state={kind:'download',path:'',expiry:86400,browsePath:'',shares:[],running:null,serviceBusy:false};
var toastTimer=null,modalType='',confirmCallback=null,lastFocus=null;
var extendingShare=null,extendBusy=false;
function id(name){return document.getElementById(name)}
function el(tag,className,text){var node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node}
function toast(message,isError){var node=id('toast');node.textContent=message;node.className='toast show'+(isError?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(function(){node.className='toast'},2800)}
function request(action,data){return window.XiaomiPluginClient.request({plugin:'quickshare',cgi:'quickshare.cgi',action:action,options:{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})}}).then(function(response){return response.json().catch(function(){throw new Error('设备返回了无法解析的数据')})}).then(function(result){if(!result.ok)throw new Error(result.error||'操作失败');return result})}
function restoreFocus(){if(!lastFocus||!document.documentElement.contains(lastFocus))return;var x=window.pageXOffset,y=window.pageYOffset;try{lastFocus.focus({preventScroll:true})}catch(error){lastFocus.focus()}if(window.pageXOffset!==x||window.pageYOffset!==y)window.scrollTo(x,y)}
function openModal(type){lastFocus=document.activeElement;modalType=type;id('modalBackdrop').hidden=false;id(type).hidden=false;document.body.classList.add('modal-open')}
function closeModal(){if(!modalType||(extendBusy&&modalType==='extendModal'))return;id(modalType).hidden=true;id('modalBackdrop').hidden=true;document.body.classList.remove('modal-open');modalType='';restoreFocus()}
function confirmAction(title,message,callback){id('confirmTitle').textContent=title;id('confirmMessage').textContent=message;confirmCallback=callback;openModal('confirmModal')}
function formatSize(bytes){var size=Number(bytes||0),units=['B','KB','MB','GB','TB'],i=0;while(size>=1024&&i<units.length-1){size/=1024;i++}return(i?size.toFixed(1):Math.round(size))+' '+units[i]}
function formatTime(value){if(!value)return'—';var date=new Date(Number(value)*1000);return date.toLocaleString()}
function statusText(value){return{active:'有效',expired:'已过期',exhausted:'次数用完',revoked:'已失效'}[value]||value}
function activatePage(page){document.querySelectorAll('.tab').forEach(function(tab){tab.classList.toggle('active',tab.dataset.page===page)});document.querySelectorAll('.page').forEach(function(node){node.classList.toggle('active',node.id==='page-'+page)});if(page==='records')loadShares();if(page==='settings')loadSettings()}

function updateServiceControls(){id('startService').disabled=state.serviceBusy||state.running!==false;id('stopService').disabled=state.serviceBusy||state.running!==true}
function loadStatus(showToast){return request('status').then(function(data){state.running=Boolean(data.running);id('serviceBadge').className='status-pill '+(data.running?'online':'offline');id('serviceBadge').innerHTML='<i></i>'+(data.running?'服务运行中':'服务已停止');id('endpoint').textContent=data.baseUrl;id('version').textContent='插件版本 '+data.pluginVersion;id('recordSummary').textContent=data.activeShares+' 个有效 · '+data.totalShares+' 条记录';updateServiceControls();if(showToast)toast('状态已刷新')}).catch(function(error){state.running=null;updateServiceControls();id('serviceBadge').className='status-pill offline';id('serviceBadge').innerHTML='<i></i>连接失败';toast(error.message,true)})}
function runServiceAction(action,button,message){if(state.serviceBusy)return;state.serviceBusy=true;updateServiceControls();button.setAttribute('aria-busy','true');request(action).then(function(){toast(message)}).catch(function(error){toast(error.message,true)}).then(function(){return loadStatus(false)}).finally(function(){state.serviceBusy=false;button.removeAttribute('aria-busy');updateServiceControls()})}
function loadSettings(){return request('settings_get').then(function(data){id('externalBase').value=data.externalBase||'';id('lanAddress').textContent='局域网地址：'+data.lanUrl}).catch(function(error){toast(error.message,true)})}
function targetTitle(){return{download:'选择文件',folder:'选择分享文件夹',upload:'选择上传文件夹'}[state.kind]}
function updateKind(kind){state.kind=kind;state.path='';document.querySelectorAll('#kindSwitch button').forEach(function(button){button.classList.toggle('active',button.dataset.kind===kind)});id('targetTitle').textContent=targetTitle();id('targetHint').textContent={download:'只会开放选中的单个文件。',folder:'整个文件夹以 ZIP 下载，保留子目录；不含隐藏文件和符号链接。包含下载时的内容，请勿放入不希望公开的文件。',upload:'朋友上传的文件会保存到所选文件夹。'}[kind];id('selectedPath').textContent=kind==='download'?'尚未选择文件':'尚未选择文件夹';id('selectedPath').className='selected-path empty';id('usesLabel').textContent=kind==='upload'?'最多上传文件数':'最多下载次数'}

function openBrowser(){state.browsePath='';id('browserTitle').textContent=targetTitle();id('chooseFolder').hidden=true;openModal('browserModal');browse('')}
function parentPath(path){var parts=String(path||'').split('/').filter(Boolean);parts.pop();return parts.join('/')}
var browseEpoch=0,browserEntries=[];
function browse(path,offset){offset=offset||0;var epoch=++browseEpoch;if(!offset){browserEntries=[];id('chooseFolder').hidden=true;id('browserList').className='file-list loading';id('browserList').textContent='正在读取…'}request('browse',{path:path,offset:offset,limit:200,dirsOnly:state.kind!=='download'}).then(function(data){if(epoch!==browseEpoch)return;state.browsePath=data.path||'';id('browserPath').textContent=state.browsePath||'根目录';id('browserBack').disabled=!state.browsePath;id('chooseFolder').hidden=state.kind==='download'||!state.browsePath;browserEntries=browserEntries.concat(data.entries||[]);renderBrowser(browserEntries);if(data.hasMore){var more=el('button','secondary-button','加载更多');more.type='button';more.onclick=function(){more.disabled=true;browse(state.browsePath,data.nextOffset)};id('browserList').appendChild(more)}}).catch(function(error){if(epoch!==browseEpoch)return;if(!offset)id('browserList').textContent=error.message;else{var more=id('browserList').lastElementChild;if(more)more.disabled=false}toast(error.message,true)})}
function renderBrowser(entries){var list=id('browserList');list.textContent='';list.className='file-list';var visible=entries.filter(function(item){return item.type==='dir'||state.kind==='download'});if(!visible.length){list.className='file-list empty-state';list.textContent=state.kind==='download'?'此目录没有可分享的文件':'此目录没有子文件夹';return}visible.forEach(function(item){var button=el('button','file-row');button.type='button';button.appendChild(el('span','file-icon',item.type==='dir'?'▰':'▤'));var copy=el('span','file-copy');copy.appendChild(el('strong','',item.name));copy.appendChild(el('span','',item.type==='dir'?'文件夹':formatSize(item.size)));button.appendChild(copy);button.addEventListener('click',function(){if(item.type==='dir')browse(item.path);else selectPath(item.path)});list.appendChild(button)})}
function selectPath(path){state.path=path;id('selectedPath').textContent=path;id('selectedPath').className='selected-path';closeModal()}

function showShareLink(value,title){id('createdLink').value=value;id('linkTitle').textContent=title||'链接已生成';id('copyHelp').hidden=true;openModal('linkModal')}
function selectLink(field){try{field.focus({preventScroll:true});field.select();field.setSelectionRange(0,field.value.length);return field.selectionStart===0&&field.selectionEnd===field.value.length}catch(error){return false}}
function legacyCopy(value){
  // Use the DOM node's real document: Electron's micro-app may proxy globals.
  var field=id('createdLink'),owner=field.ownerDocument,view=owner.defaultView,previous=owner.activeElement;
  var temporary=id('linkModal').hidden||field.value!==value,area=temporary?owner.createElement('textarea'):field;
  try{
    if(view)try{view.focus()}catch(error){}
    if(temporary){area.value=value;area.readOnly=true;area.tabIndex=-1;area.style.cssText='position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none';document.body.appendChild(area)}
    return selectLink(area)&&owner.execCommand('copy')===true;
  }catch(error){return false}
  finally{if(temporary){area.remove();if(previous&&previous.isConnected)try{previous.focus({preventScroll:true})}catch(error){}}}
}
function copyText(value){
  // Desktop first tries synchronous copy while the click gesture is active.
  if(document.documentElement.classList.contains('desktop-client')&&legacyCopy(value))return Promise.resolve();
  var owner=id('createdLink').ownerDocument,view=owner.defaultView||window;
  try{
    if(view.isSecureContext&&view.navigator.clipboard&&typeof view.navigator.clipboard.writeText==='function'){
      return Promise.resolve(view.navigator.clipboard.writeText(value)).catch(function(){if(!legacyCopy(value))throw new Error('manual-copy-required')});
    }
  }catch(error){}
  return legacyCopy(value)?Promise.resolve():Promise.reject(new Error('manual-copy-required'));
}
function manualCopy(value){
  clearTimeout(toastTimer);id('toast').className='toast';id('toast').textContent='';
  if(modalType!=='linkModal')showShareLink(value,'复制分享链接');
  var selected=selectLink(id('createdLink'));
  id('copyHelp').textContent=selected?'自动复制未成功，链接已选中。电脑按 Ctrl+C（Mac 按 ⌘C），手机可长按复制。':'自动复制未成功。请选中上方链接，电脑按 Ctrl+C（Mac 按 ⌘C），手机可长按复制。';
  id('copyHelp').hidden=false;
}
function createShare(){var password=id('password').value,maxUses=Number(id('maxUses').value);if(!state.path){toast('请先'+targetTitle(),true);return}if(password&&password.length<4){toast('密码至少需要 4 位',true);return}if(!Number.isInteger(maxUses)||maxUses<0||maxUses>1000){toast('次数必须是 0–1000 的整数',true);return}var button=id('createButton');button.disabled=true;request('create',{kind:state.kind,path:state.path,password:password,expiresIn:state.expiry,maxUses:maxUses}).then(function(data){id('password').value='';showShareLink(data.share.url);loadStatus(false);loadShares()}).catch(function(error){toast(error.message,true)}).finally(function(){button.disabled=false})}

function loadShares(){var list=id('shareList');list.className='stack loading';list.textContent='正在读取分享记录…';return request('list').then(function(data){state.shares=data.shares||[];renderShares()}).catch(function(error){list.textContent=error.message;toast(error.message,true)})}
function extendSeconds(){var selected=document.querySelector('#extendForm input[name="extendSeconds"]:checked');return selected?Number(selected.value):86400}
function updateExtendPreview(){if(!extendingShare)return;id('extendOld').textContent=formatTime(extendingShare.expires_at);id('extendNew').textContent=formatTime(Math.max(Number(extendingShare.expires_at),Math.floor(Date.now()/1000))+extendSeconds())}
function openExtend(share){
  if(extendBusy)return;extendingShare=share;id('extendName').textContent=share.name;
  id('extendForm').reset();id('extendError').hidden=true;updateExtendPreview();openModal('extendModal');
}
function setExtendBusy(on){extendBusy=on;id('extendSave').disabled=on;id('extendCancel').disabled=on;id('extendSave').setAttribute('aria-busy',String(on));document.querySelectorAll('#extendForm input').forEach(function(input){input.disabled=on})}
id('extendForm').addEventListener('change',function(){id('extendError').hidden=true;updateExtendPreview()});
id('extendCancel').addEventListener('click',closeModal);
id('extendForm').addEventListener('submit',function(event){
  event.preventDefault();if(extendBusy||!extendingShare)return;
  var share=extendingShare;setExtendBusy(true);id('extendError').hidden=true;
  request('extend',{id:share.id,seconds:extendSeconds(),expectedExpiresAt:share.expires_at}).then(function(data){
    setExtendBusy(false);closeModal();extendingShare=null;toast('已延期至 '+formatTime(data.share.expires_at));loadShares();loadStatus(false);
  }).catch(function(error){
    var box=id('extendError');box.textContent=error.message;box.hidden=false;box.scrollIntoView({block:'nearest'});try{box.focus({preventScroll:true})}catch(e){box.focus()}
  }).finally(function(){setExtendBusy(false)});
});
function renderShares(){
  var list=id('shareList');
  list.textContent='';list.className='stack';
  id('clearShares').hidden=!state.shares.length;
  if(!state.shares.length){list.className='stack empty-state';list.textContent='还没有分享记录。';return}
  state.shares.forEach(function(share){
    var card=el('article','share-card');
    var head=el('div','share-head'),title=el('div','share-title');
    title.appendChild(el('h3','',share.name));title.appendChild(el('p','',share.path));
    head.appendChild(title);
    head.appendChild(el('span','badge'+(share.status==='active'?'':' inactive'),statusText(share.status)));
    card.appendChild(head);
    var meta=el('div','share-meta');
    meta.appendChild(el('span','chip',{download:'文件下载',folder:'文件夹 · ZIP',upload:'上传入口'}[share.kind]||'分享'));
    meta.appendChild(el('span','chip',share.password_protected?'密码保护':'无密码'));
    meta.appendChild(el('span','chip','已使用 '+share.uses+(share.max_uses?'/'+share.max_uses:'/不限')));
    meta.appendChild(el('span','chip','到期 '+formatTime(share.expires_at)));
    if(share.last_used_at)meta.appendChild(el('span','chip','最近 '+formatTime(share.last_used_at)));
    card.appendChild(meta);
    var actions=el('div','share-actions');
    var copy=el('button','','复制链接');copy.type='button';
    copy.addEventListener('click',function(){copyText(share.url).then(function(){toast('链接已复制')}).catch(function(){manualCopy(share.url)})});
    actions.appendChild(copy);
    var open=el('a','','打开');open.href=share.url;open.target='_blank';open.rel='noreferrer';actions.appendChild(open);
    if(share.status==='active'||share.status==='expired'){
      var extend=el('button','','延期');extend.type='button';
      extend.disabled=Boolean(share.max_uses&&share.uses>=share.max_uses);
      if(extend.disabled)extend.title='次数已用完，延期不会恢复次数';
      extend.addEventListener('click',function(){openExtend(share)});actions.appendChild(extend);
    }
    if(share.status==='active'){
      var revoke=el('button','revoke quickshare-danger-action','立即失效');revoke.type='button';
      revoke.addEventListener('click',function(){confirmAction('使分享失效','失效后原链接将立即无法访问，是否继续？',function(){request('revoke',{id:share.id}).then(function(){toast('分享已失效');loadShares();loadStatus(false)}).catch(function(error){toast(error.message,true)})})});
      actions.appendChild(revoke);
    }
    var remove=el('button','revoke quickshare-danger-action','删除记录');remove.type='button';
    remove.addEventListener('click',function(){
      confirmAction('删除分享记录',share.status==='active'?'删除后该分享链接将立即失效，记录无法恢复；原文件不会删除。是否继续？':'记录删除后无法恢复；原文件不会删除。是否继续？',function(){
        request('delete',{id:share.id}).then(function(){toast('记录已删除');loadShares();loadStatus(false)}).catch(function(error){toast(error.message,true)});
      });
    });
    actions.appendChild(remove);
    card.appendChild(actions);list.appendChild(card);
  });
}

document.querySelectorAll('.tab').forEach(function(tab){tab.addEventListener('click',function(){activatePage(tab.dataset.page)})});
document.querySelectorAll('#kindSwitch button').forEach(function(button){button.addEventListener('click',function(){updateKind(button.dataset.kind)})});
document.querySelectorAll('#expiryChoices button').forEach(function(button){button.addEventListener('click',function(){state.expiry=Number(button.dataset.value);document.querySelectorAll('#expiryChoices button').forEach(function(item){item.classList.toggle('active',item===button)})})});
id('refresh').addEventListener('click',function(){loadStatus(true);loadShares()});id('browseButton').addEventListener('click',openBrowser);id('browserBack').addEventListener('click',function(){browse(parentPath(state.browsePath))});id('chooseFolder').addEventListener('click',function(){selectPath(state.browsePath)});id('closeBrowser').addEventListener('click',closeModal);id('createButton').addEventListener('click',createShare);id('closeLink').addEventListener('click',closeModal);id('copyCreatedLink').addEventListener('click',function(){copyText(id('createdLink').value).then(function(){toast('链接已复制');closeModal()}).catch(function(){manualCopy(id('createdLink').value)})});
id('clearShares').addEventListener('click',function(){
  confirmAction('清空分享记录','将删除全部分享记录，所有仍有效的链接会立即失效。此操作无法恢复，但不会删除原文件或已上传的文件。是否继续？',function(){
    request('clear').then(function(data){toast('已清空 '+data.deleted+' 条记录');loadShares();loadStatus(false)}).catch(function(error){toast(error.message,true)});
  });
});
id('modalBackdrop').addEventListener('click',function(){if(modalType==='confirmModal'){confirmCallback=null}closeModal()});id('confirmCancel').addEventListener('click',function(){confirmCallback=null;closeModal()});id('confirmAccept').addEventListener('click',function(){var callback=confirmCallback;confirmCallback=null;closeModal();if(callback)callback()});
id('saveSettings').addEventListener('click',function(){var value=id('externalBase').value.trim();if(value&&!/^https?:\/\//i.test(value)){toast('请输入完整的 HTTP 或 HTTPS 地址',true);return}var button=this;button.disabled=true;request('settings_save',{externalBase:value}).then(function(data){toast('公网分享地址已保存');id('endpoint').textContent=data.baseUrl;loadShares()}).catch(function(error){toast(error.message,true)}).finally(function(){button.disabled=false})});
id('startService').addEventListener('click',function(){runServiceAction('server_start',this,'分享服务已启动')});id('stopService').addEventListener('click',function(){var button=this;confirmAction('停止分享服务','停止后所有临时链接都会暂时无法访问，但分享记录不会删除。',function(){runServiceAction('server_stop',button,'分享服务已停止')})});
document.addEventListener('keydown',function(event){if(event.key==='Escape'&&modalType){if(modalType==='confirmModal')confirmCallback=null;closeModal()}});
var swipe=null;document.querySelector('.shell').addEventListener('touchstart',function(event){if(event.touches.length!==1||event.target.closest('button,input,textarea,a')||modalType){swipe=null;return}var touch=event.touches[0];if(touch.clientX<24||touch.clientX>window.innerWidth-24){swipe=null;return}swipe={x:touch.clientX,y:touch.clientY}},{passive:true});document.querySelector('.shell').addEventListener('touchend',function(event){if(!swipe||!event.changedTouches.length)return;var touch=event.changedTouches[0],dx=touch.clientX-swipe.x,dy=touch.clientY-swipe.y;swipe=null;if(Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;var pages=['create','records','settings'],active=document.querySelector('.tab.active'),index=pages.indexOf(active?active.dataset.page:'create'),next=dx<0?index+1:index-1;if(next>=0&&next<pages.length)activatePage(pages[next])},{passive:true});
updateServiceControls();loadStatus(false);loadShares();loadSettings();
})();
