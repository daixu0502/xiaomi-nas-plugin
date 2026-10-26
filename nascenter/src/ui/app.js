(function(){'use strict';
var document = window.XiaomiPluginClient.document;var previousNetwork=null,overviewBusy=false,toastTimer=null,confirmCallback=null,lastFocus=null,detailFocus=null,currentPage='overview',detailsBusy=false,lastDetails=0,slowFilesystems=[];function id(v){return document.getElementById(v)}function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}function request(action,body){return window.XiaomiPluginClient.request({plugin:'nascenter',cgi:'nascenter.cgi',action:action,options:{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}}).then(function(r){return r.json().catch(function(){throw new Error('设备返回了无法解析的数据')})}).then(function(d){if(!d.ok)throw new Error(d.error||'操作失败');return d})}function toast(m,e){var n=id('toast');n.textContent=m;n.className='toast show'+(e?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(function(){n.className='toast'},2800)}function bytes(v){var n=Number(v||0),u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return(i?n.toFixed(1):Math.round(n))+' '+u[i]}function speed(v){return bytes(v)+'/s'}function uptime(v){var s=Math.floor(v||0),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'天 ':'')+h+'小时 '+m+'分'}function shown(v,fallback){return v===null||v===undefined||v===''?(fallback||'—'):String(v)}function objectText(v){if(v===null||v===undefined)return '—';if(typeof v!=='object')return String(v);return shown(v.string||v.name||v.value||v.passed)}
// Match smartctl names rather than IDs: SATA SSD vendors reuse ATA IDs.
var smartAttributeNames={
 rawreaderrorrate:'读取错误率',readerrorrate:'读取错误率',throughputperformance:'吞吐性能',
 spinuptime:'起转用时',startstopcount:'启停次数',reallocatedsectorct:'重新分配扇区数',
 reallocatedsectorcount:'重新分配扇区数',reallocatedsectorscount:'重新分配扇区数',
 readchannelmargin:'读取通道余量',seekerrorrate:'寻道错误率',seektimeperformance:'寻道时间性能',
 poweronhours:'通电时间（小时）',poweronhoursandmsec:'通电时间（小时及毫秒）',
 spinretrycount:'起转重试次数',calibrationretrycount:'校准重试次数',powercyclecount:'通电次数',
 readsoftenderrorrate:'软读取错误率',endtoenderror:'端到端错误',endtoenderrorcount:'端到端错误数',
 reporteduncorrect:'已报告不可校正错误',reporteduncorrectable:'已报告不可校正错误',
 reporteduncorrectableerrors:'已报告不可校正错误',commandtimeout:'命令超时',
 highflywrites:'磁头非正常高度写入',airflowtemperaturecel:'气流温度',airflowtemperaturecelsius:'气流温度',
 gsenseerrorrate:'冲击感应错误率',poweroffretractcount:'断电磁头回收次数',
 loadcyclecount:'磁头加载/卸载循环次数',loadunloadcyclecount:'磁头加载/卸载循环次数',
 temperaturecelsius:'温度',temperatureinternal:'内部温度',temperaturecase:'外壳温度',
 drivetemperature:'硬盘温度',temperature:'温度',hardwareeccrecovered:'硬件 ECC 校正计数',
 reallocatedeventcount:'重新分配事件数',currentpendingsector:'待处理扇区数',
 currentpendingsectorcount:'待处理扇区数',offlineuncorrectable:'离线不可校正扇区数',
 offlineuncorrectablecount:'离线不可校正扇区数',udmacrcerrorcount:'UltraDMA CRC 错误计数',
 ultradmacrcerrorcount:'UltraDMA CRC 错误计数',multizoneerrorrate:'多区域写入错误率',
 writeerrorrate:'写入错误率',softreaderrorrate:'软读取错误率',softreaderrorcount:'软读取错误数',
 dataaddressmarkerrors:'数据地址标记错误',runoutcancel:'偏移抵消',shockcount:'冲击次数',
 spinehighcurrent:'起转电流过高',spinhighcurrent:'起转电流过高',spinbuzz:'起转重试',
 offlineseekperformnce:'离线寻道性能',vibrationduringwrite:'写入时振动',shockduringwrite:'写入时冲击',
 diskshift:'盘片偏移',loadedhours:'磁头加载时间',loadretrycount:'磁头加载重试次数',
 loadfriction:'磁头加载摩擦',loadincycle:'磁头加载循环次数',loadintime:'磁头加载时间',
 torqueamplificationcount:'扭矩放大次数',poweroffretractcycle:'断电磁头回收次数',
 gmrheadamplitude:'磁头振幅',headflyinghours:'磁头飞行时间',
 transfererrorrate:'传输错误率',totallbaswritten:'主机累计写入（LBA）',totallbasread:'主机累计读取（LBA）',
 totallbaswrittenexpanded:'扩展累计写入（LBA）',totallbasreadexpanded:'扩展累计读取（LBA）',
 readerrorretryrate:'读取重试错误率',freefallsensor:'跌落感应次数',
 // Common SSD names from the smartmontools drive database. Units stay vendor-defined.
 retiredblockcount:'已退役块数',reallocatedblockcount:'重新分配块数',badblockcount:'坏块数',
 initialbadblockcount:'初始坏块数',laterbadblockcount:'后期新增坏块数',factorybadblockct:'出厂坏块数',
 grownfailingblockct:'新增失效块数',runtimebadblock:'运行时坏块数',runtimeinvalidblkcnt:'运行时无效块数',
 wearlevelingcount:'磨损均衡计数',wearlevellingcount:'磨损均衡计数',
 averagepecount:'平均编程/擦除次数',averageerasecount:'平均擦除次数',avgerasecount:'平均擦除次数',
 totalerasecount:'累计擦除次数',erasecount:'擦除次数',maxerasecount:'最大擦除次数',
 minerasecount:'最小擦除次数',maxmineccount:'最大/最小擦除次数',
 programfailcount:'编程失败次数',programfailcnttotal:'编程失败总次数',programfailcounttotal:'编程失败总次数',
 programfailcountchip:'芯片编程失败次数',erasefailcount:'擦除失败次数',
 erasefailcounttotal:'擦除失败总次数',erasefailcnttotal:'擦除失败总次数',erasefailcountchip:'芯片擦除失败次数',
 usedrsvdblkcntchip:'芯片已用保留块数',usedrsvdblkcnttot:'已用保留块总数',
 unusedrsvdblkcnttot:'未用保留块总数',availablereservdspace:'可用保留空间',
 availablereservedspace:'可用保留空间',reservedblockcount:'保留块数',spareblockcount:'备用块数',
 unexpectedpowerlosscount:'意外断电次数',unexpectpowerlossct:'意外断电次数',
 unsafeshutdowncount:'非正常关机次数',unsafeshutdowns:'非正常关机次数',
 percratedlifeused:'额定寿命已用百分比',percentlifetimeused:'已用寿命百分比',percentageused:'已用寿命百分比',
 percentlifetimeremain:'剩余寿命百分比',remaininglifetimeperc:'剩余寿命百分比',
 ssdlifeleft:'SSD 剩余寿命',ssdlifeleftperc:'SSD 剩余寿命百分比',
 mediawearoutindicator:'介质磨损指标',wearlevelingct:'磨损均衡计数',
 sataifacedownshift:'SATA 接口降速次数',satadownshiftcount:'SATA 接口降速次数',
 sataphyerrorcount:'SATA 物理层错误计数',satacrcerrorcount:'SATA CRC 错误计数',
 non4kalignedaccess:'非 4K 对齐访问次数',non4kalignedaccesses:'非 4K 对齐访问次数',
 uncorrectableerrorcnt:'不可校正错误数',uncorrectableerrorcount:'不可校正错误数',
 uncorrectableecccount:'不可校正 ECC 错误数',uncorrectablesofterrrate:'不可校正软错误率',
 hostwrites:'主机累计写入',hostreads:'主机累计读取',hostwrites32mib:'主机累计写入（32 MiB）',
 hostreads32mib:'主机累计读取（32 MiB）',hostwritess32mib:'主机累计写入（32 MiB）',
 hostwritesgib:'主机累计写入（GiB）',hostreadsgib:'主机累计读取（GiB）',
 lifetimewritesgib:'生命周期写入（GiB）',lifetimereadsgib:'生命周期读取（GiB）',
 nandwrites1gib:'NAND 累计写入（GiB）',nandwrites1gb:'NAND 累计写入（GB）',
 nandwrites32mib:'NAND 累计写入（32 MiB）',nandwrites:'NAND 累计写入',
 totalnandwritten:'NAND 累计写入',totalnandwrites:'NAND 累计写入',flashwritesgib:'闪存累计写入（GiB）',
 totalwritesgib:'累计写入（GiB）',totalreadsgib:'累计读取（GiB）',
 thermalthrottlestatus:'温度限速状态',thermalthrottle:'温度限速',
 thermalthrottlecount:'温度限速次数',thermalthrottleminutes:'温度限速时间（分钟）',
 capacitorlife:'电容寿命',plpfailure:'断电保护失败次数',sataerrorcount:'SATA 错误计数',
 writeamplification:'写入放大',backgroundprogramfailcnt:'后台编程失败次数',
 endtoenderrordetection:'端到端错误检测计数',recoveryattempts:'恢复尝试次数'
};
function smartNameKey(name){return String(name||'').toLowerCase().replace(/[^a-z0-9]/g,'')}
function smartAttributeName(row){var name=shown(row.name,'');return /[\u3400-\u9fff]/.test(name)?name:(smartAttributeNames[smartNameKey(name)]||'厂商自定义属性')}
// Decimal long division preserves legacy cached counters without requiring BigInt.
function smartHex(value,width){
 if(value===null||value===undefined||value===''||typeof value==='boolean')return '—';
 if(typeof value==='number'&&(!Number.isSafeInteger(value)||value<0))return '—';
 var text=String(value).trim(),hex='';
 if(/^0x[0-9a-f]+$/i.test(text))hex=text.slice(2).replace(/^0+(?=.)/,'').toUpperCase();
 else if(/^\d+$/.test(text)){
  text=text.replace(/^0+(?=.)/,'');
  do{var next='',carry=0;for(var i=0;i<text.length;i++){var digit=carry*10+Number(text[i]),part=Math.floor(digit/16);carry=digit%16;if(next||part)next+=part}hex='0123456789ABCDEF'[carry]+hex;text=next}while(text);
 }else return '—';
 while(hex.length<width)hex='0'+hex;return hex;
}
function smartRawHex(row){if(typeof row.rawHex==='string'&&/^[0-9a-f]+$/i.test(row.rawHex))return smartHex('0x'+row.rawHex,12);return smartHex(row.rawValue!=null?row.rawValue:row.raw,12)}
var nvmeNames={
 nsid:'命名空间 ID',critical_warning:'关键警告',temperature:'温度',available_spare:'可用备用空间',
 available_spare_threshold:'备用空间阈值',percentage_used:'已用寿命',data_units_read:'累计读取数据单元',
 data_units_written:'累计写入数据单元',host_reads:'主机读取命令数',host_writes:'主机写入命令数',
 controller_busy_time:'控制器忙碌时间',power_cycles:'通电次数',power_on_hours:'通电时间',
 unsafe_shutdowns:'非正常关机次数',media_errors:'介质与数据完整性错误数',num_err_log_entries:'错误日志条目数',
 warning_temp_time:'超过警告温度的时间',critical_comp_time:'超过临界温度的时间',temperature_sensors:'温度传感器',
 thermal_mgmt_temperature_1_transition_count:'温度管理一级切换次数',thermal_mgmt_temperature_2_transition_count:'温度管理二级切换次数',
 thermal_mgmt_temperature_1_total_time:'温度管理一级累计时间',thermal_mgmt_temperature_2_total_time:'温度管理二级累计时间'
};
function nvmeHealthPairs(health){return Object.keys(health).filter(function(key){return !/_(?:s|le)$/.test(key)||!Object.prototype.hasOwnProperty.call(health,key.replace(/_(?:s|le)$/,''))}).map(function(key){
 var value=Object.prototype.hasOwnProperty.call(health,key+'_s')?health[key+'_s']:health[key],text=Array.isArray(value)?value.map(function(v){return shown(v)+'°C'}).join(' / '):objectText(value);
 if(['available_spare','available_spare_threshold','percentage_used'].includes(key))text=shown(value)+'%';
 else if(key==='temperature')text=shown(value)+'°C';
 else if(['controller_busy_time','warning_temp_time','critical_comp_time'].includes(key))text=shown(value)+' 分钟';
 else if(key==='power_on_hours')text=shown(value)+' 小时';
 else if(/^thermal_mgmt_temperature_[12]_total_time$/.test(key))text=shown(value)+' 秒';
 else if(key==='critical_warning')text=smartHex(value,2)==='—'?shown(value):'0x'+smartHex(value,2);
 else if(key==='nsid'&&value===-1)text='全部命名空间';
 else if(/^data_units_(read|written)$/.test(key))text=shown(value)+'（每单元 512,000 B）';
 return [nvmeNames[key]||'厂商扩展数据（'+key+'）',text];
})}
function diskIsNvme(disk){return /nvme/i.test(disk.protocol||disk.path||'')||Object.keys(disk.nvmeHealth||{}).length>0}
function nvmeMetric(disk,key,unit){var value=(disk.nvmeHealth||{})[key];return value==null?'—':shown(value)+(unit||'')}
function diskRotation(disk){return disk.rotation>0?disk.rotation+' RPM':disk.isSSD||disk.rotation===0||diskIsNvme(disk)?'固态硬盘（SSD）':'未报告'}
function renderSmartAttributes(body,disk){
 var rows=disk.attributes||[];if(!rows.length&&diskIsNvme(disk))return;
 var attrs=addDetailSection(body,'完整 SMART 属性');
 if(!rows.length){attrs.appendChild(el('div','empty-detail','该硬盘没有返回 ATA SMART 属性'));return}
 var wrap=el('div','smart-table-wrap'),table=el('table','smart-table'),thead=el('thead'),headrow=el('tr');
 ['ID（十六进制）','属性名称','当前值','最差值','临界值','原始值（十六进制）','状态'].forEach(function(label){headrow.appendChild(el('th','',label))});
 thead.appendChild(headrow);table.appendChild(thead);var tbody=el('tbody');
 rows.forEach(function(row){
  var tr=el('tr'),failed=!!row.whenFailed;
  [smartHex(row.id,2),smartAttributeName(row),shown(row.value),shown(row.worst),shown(row.threshold),smartRawHex(row),failed?(row.whenFailed==='now'?'当前失败':row.whenFailed==='past'?'曾经失败':'曾失败 '+row.whenFailed):'正常'].forEach(function(value,index){
   var cell=el('td',index===0?'smart-id':index===5?'smart-raw':index===6&&failed?'failed':'',value);
   if(index===1)cell.title=shown(row.name);
   if(index===5)cell.title='硬盘解码值：'+shown(row.raw)+(value==='—'?'；重新检测后可获取十六进制原始值':'');
   tr.appendChild(cell);
  });tbody.appendChild(tr);
 });
 table.appendChild(tbody);wrap.appendChild(table);attrs.appendChild(wrap);
}
function openConfirm(title,message,callback){lastFocus=document.activeElement;confirmCallback=callback;id('confirmTitle').textContent=title;id('confirmMessage').textContent=message;id('backdrop').hidden=false;id('confirmDialog').hidden=false;document.body.classList.add('modal-open')}function closeConfirm(){id('backdrop').hidden=true;id('confirmDialog').hidden=true;document.body.classList.remove('modal-open');var target=lastFocus;lastFocus=null;if(target&&document.documentElement.contains(target)){var x=scrollX,y=scrollY;try{target.focus({preventScroll:true})}catch(e){target.focus()}if(scrollX!==x||scrollY!==y)scrollTo(x,y)}}
function renderOverview(data){id('cpuUsage').textContent=data.cpu.usage.toFixed(1)+'%';id('loadAverage').textContent='负载 '+data.cpu.load.map(function(v){return v.toFixed(2)}).join(' / ');id('memoryUsage').textContent=data.memory.percent.toFixed(1)+'%';id('memoryDetail').textContent=bytes(data.memory.used)+' / '+bytes(data.memory.total);id('cpuTemp').textContent=data.cpu.temperature==null?'不可用':data.cpu.temperature.toFixed(1)+'°C';id('uptime').textContent='运行 '+uptime(data.uptime);id('networkName').textContent=data.network.name;id('networkErrors').textContent='错误 '+data.network.errors+' · 丢包 '+data.network.drops;id('rxTotal').textContent='累计 '+bytes(data.network.rx);id('txTotal').textContent='累计 '+bytes(data.network.tx);if(previousNetwork&&previousNetwork.name===data.network.name){var seconds=Math.max(.1,data.network.timestamp-previousNetwork.timestamp);id('rxSpeed').textContent=speed(Math.max(0,data.network.rx-previousNetwork.rx)/seconds);id('txSpeed').textContent=speed(Math.max(0,data.network.tx-previousNetwork.tx)/seconds)}else{id('rxSpeed').textContent='计算中';id('txSpeed').textContent='计算中'}previousNetwork=data.network;if(data.filesystems){slowFilesystems=data.filesystems;renderFilesystems(data.filesystems)}if(data.services)renderServices(data.services);id('cacheInfo').textContent='当前页缓存约 '+bytes(data.cache.pageCache)+'，释放后系统会按需重新加载。';if(data.cache.logSize!=null)id('logInfo').textContent='系统日志当前约 '+bytes(data.cache.logSize)+'；保留最近 7 天并限制到约 32 MB。';var warning=data.cpu.usage>=85||data.cpu.temperature>=75||data.memory.percent>=90||(data.filesystems||slowFilesystems).some(function(f){return f.mount!=='/firmware'&&(f.spaceWarning||f.percent>=90)});id('healthBadge').className='status-pill '+(warning?'warning':'healthy');id('healthBadge').innerHTML='<i></i>'+(warning?'需要关注':'运行中');id('heroTitle').textContent=warning?'发现高负载项目':'设备运行正常';id('heroSub').textContent='CPU '+data.cpu.usage.toFixed(1)+'% · 内存 '+data.memory.percent.toFixed(1)+'% · '+(data.cpu.temperature==null?'温度不可用':data.cpu.temperature.toFixed(1)+'°C');if(data.pluginVersion)id('version').textContent='插件版本 '+data.pluginVersion+' · 公共权限组件 '+(data.helperVersion||'不可用')}
function renderFilesystems(items){var box=id('filesystems');box.textContent='';box.className='stack';items.forEach(function(item){var row=el('div','storage-row'),head=el('div','row-head'),directory=item.kind==='directory',physical=item.kind==='filesystem';head.appendChild(el('strong','',item.label+' · '+item.mount));head.appendChild(el('span','',item.percent==null?'目录占用统计中…':(directory?'占所在文件系统 ':'实际使用 ')+item.percent+'%'));row.appendChild(head);var detail=directory?'目录实际占用 '+(item.used==null?'统计中':bytes(item.used))+' · '+(item.sharedWith?'共享剩余空间 ':'所在文件系统剩余 ')+bytes(item.available):'已用 '+bytes(item.used)+' / 总容量 '+bytes(item.total)+' · '+(physical?'实际剩余 ':'可用 ')+bytes(item.available);row.appendChild(el('p','space-detail',detail));if(item.percent!=null){var bar=el('div','progress'),fill=el('i',item.spaceWarning||item.percent>=95?'danger':item.percent>=85?'warn':'');fill.style.width=Math.max(0,Math.min(100,item.percent))+'%';bar.appendChild(fill);row.appendChild(bar)}if(physical||directory){var note='普通用户可用 '+bytes(item.ordinaryAvailable)+' · 受限/保留空闲 '+bytes(item.restrictedFree)+'（不能保证普通用户可写）';if(directory){note+=(item.sharedWith?'；与 '+item.sharedWith+' 共用空间，不是独立分区':'；目录没有独立容量配额')+'；目录占用约每 60 秒更新';if(item.scannedAt)note+='，采样于 '+new Date(item.scannedAt*1000).toLocaleTimeString();if(item.scanError)note+='；'+item.scanError}else if(item.mount==='/data'){note+='；已用包含 Docker 数据及文件系统开销'}row.appendChild(el('p','space-note',note))}box.appendChild(row)})}
function renderServices(items){var box=id('services');box.textContent='';box.className='stack';items.forEach(function(item){var row=el('div','service-row'),name=el('div','service-name');name.appendChild(el('i','dot '+(item.state==='active'?'active':'')));var copy=el('div');copy.appendChild(el('strong','',item.name));copy.appendChild(el('p','',({active:'运行中',activating:'启动中',deactivating:'停止中',failed:'异常退出',degraded:'核心未就绪',missing:'未安装'})[item.state]||'已停止'));if(item.cores)copy.appendChild(el('p','', 'aria2 '+(item.cores.aria2?'已监听':'未监听')+' · qB '+(item.cores.qbittorrent?'已监听':'未监听')));name.appendChild(copy);row.appendChild(name);var button=el('button','',item.state==='active'?'重启':'尝试启动');button.disabled=['missing','activating','deactivating'].includes(item.state);if(item.state==='missing'){button.textContent='未安装';button.title='服务未安装，无法启动'}else if(button.disabled){button.textContent=item.state==='activating'?'启动中':'停止中'}button.addEventListener('click',function(){restartService(item,button)});row.appendChild(button);box.appendChild(row)})}
function loadOverviewDetails(force){if(detailsBusy||(!force&&Date.now()-lastDetails<30000))return;detailsBusy=true;request('overview_details',{force:force}).then(function(data){if(disposed)return;slowFilesystems=data.filesystems||[];renderFilesystems(slowFilesystems);renderServices(data.services||[]);id('logInfo').textContent='系统日志当前约 '+bytes(data.cache.logSize)+'；保留最近 7 天并限制到约 32 MB。';lastDetails=Date.now();id('detailSample').textContent='存储与服务采样：'+new Date(data.sampledAt*1000).toLocaleTimeString()}).catch(function(error){id('detailSample').textContent='存储与服务读取失败：'+error.message}).finally(function(){detailsBusy=false})}
function loadOverview(show){if(overviewBusy||!id('processDetail').hidden)return Promise.resolve();overviewBusy=true;loadOverviewDetails(show===true);return request('overview',{fast:true}).then(function(data){renderOverview(data);if(show)toast('状态已刷新')}).catch(function(error){toast(error.message,true);id('healthBadge').className='status-pill failed';id('healthBadge').innerHTML='<i></i>连接失败'}).finally(function(){overviewBusy=false})}
function loadSmart(show){var box=id('diskList');box.className='stack loading';box.textContent='正在读取 SMART，休眠中的硬盘将跳过检测…';return request('smart').then(function(data){renderDisks(data.disks||[]);if(show)toast('SMART 检测完成')}).catch(function(error){box.textContent=error.message;toast(error.message,true)})}
function renderDisks(disks){var box=id('diskList');box.textContent='';box.className='stack';if(!disks.length){box.className='stack loading';box.textContent='没有检测到支持 SMART 的物理硬盘';return}var allGood=disks.every(function(d){return d.status==='healthy'});id('diskSummary').textContent=disks.length+' 块物理硬盘 · '+(allGood?'全部健康':disks.some(function(d){return d.status==='standby'})?'包含休眠硬盘':disks.some(function(d){return d.status==='unavailable'})?'部分硬盘状态不可用':'存在需要关注的项目');disks.forEach(function(d){var card=el('article','disk-card'),head=el('div','disk-head'),copy=el('div');copy.appendChild(el('h3','',d.model));copy.appendChild(el('p','',d.path+' · '+bytes(d.size)+' · '+(d.serial||'无序列号')));head.appendChild(copy);head.appendChild(el('span','badge '+(d.status==='healthy'?'':'warning'),d.status==='healthy'?'SMART 正常':d.status==='failed'?'SMART 失败':d.status==='standby'?'休眠中':d.status==='unavailable'?'状态不可用':'需要关注'));card.appendChild(head);if(d.status==='standby'||d.status==='unavailable'){card.appendChild(el('p','',d.status==='standby'?(d.checkedAt?'硬盘休眠中，以下为上次检测数据：'+new Date(d.checkedAt*1000).toLocaleString():'硬盘休眠中，已跳过检测'):'SMART 暂不可用，未获得有效健康数据'));if(!d.checkedAt){box.appendChild(card);return}}var grid=el('div','smart-grid');[['温度',d.temperature==null?'—':d.temperature+'°C'],['通电时间',d.hours==null?'—':d.hours+' 小时'],['转速 / 介质类型',diskRotation(d)],diskIsNvme(d)?['已用寿命',nvmeMetric(d,'percentage_used','%')]:['重新分配',shown(d.reallocated)],diskIsNvme(d)?['可用备用空间',nvmeMetric(d,'available_spare','%')]:['待定扇区',shown(d.pending)],diskIsNvme(d)?['介质错误',shown(d.mediaErrors)]:['不可校正',shown(Number(d.offlineUncorrectable||0)+Number(d.reportedUncorrectable||0)+Number(d.mediaErrors||0))]].forEach(function(pair){var item=el('div','smart-item');item.appendChild(el('span','',pair[0]));item.appendChild(el('strong','',pair[1]));grid.appendChild(item)});card.appendChild(grid);var button=el('button','disk-detail-button','查看详细 SMART');button.type='button';button.addEventListener('click',function(){openDiskDetail(d)});card.appendChild(button);box.appendChild(card)})}
function addDetailGrid(parent,pairs){var grid=el('div','detail-grid');pairs.forEach(function(pair){var item=el('div','detail-item');item.appendChild(el('span','',pair[0]));item.appendChild(el('strong','',shown(pair[1])));grid.appendChild(item)});parent.appendChild(grid)}
function addDetailSection(body,title){var section=el('section','detail-section');section.appendChild(el('h3','',title));body.appendChild(section);return section}
function openDiskDetail(disk){detailFocus=document.activeElement;id('detailTitle').textContent=disk.model||disk.path||'硬盘';var body=id('detailBody');body.textContent='';var basic=addDetailSection(body,'设备信息');addDetailGrid(basic,[['设备路径',disk.path],['容量',bytes(disk.size)],['序列号',disk.serial||'无序列号'],['固件版本',disk.firmware],['外形规格',disk.formFactor],['接口版本',diskIsNvme(disk)?'NVMe':disk.sataVersion],['当前接口速率',disk.interfaceSpeed],['转速 / 介质类型',diskRotation(disk)],['逻辑扇区',disk.logicalBlockSize?disk.logicalBlockSize+' B':'—'],['物理扇区',disk.physicalBlockSize?disk.physicalBlockSize+' B':'—']]);var health=addDetailSection(body,'健康概览');addDetailGrid(health,[['SMART 总体状态',disk.status==='healthy'?'正常':disk.status==='failed'?'失败':disk.status==='standby'?'休眠中（本次已跳过）':disk.status==='unavailable'?'暂不可用':'需要关注'],['SMART 支持',disk.smartAvailable?'支持':'不可用'],['SMART 功能',disk.smartEnabled?'已启用':'未启用'],['温度',disk.temperature==null?'—':disk.temperature+'°C'],['通电时间',disk.hours==null?'—':disk.hours+' 小时'],['通电次数',disk.powerCycles],['重新分配扇区',disk.reallocated],['待定扇区',disk.pending],['离线不可校正',disk.offlineUncorrectable],['已报告不可校正',disk.reportedUncorrectable],['NVMe 媒体错误',disk.mediaErrors],['SMART 错误日志',disk.errorLogCount+' 条']]);renderSmartAttributes(body,disk);var tests=addDetailSection(body,'错误与自检记录');addDetailGrid(tests,[['SMART 错误日志',disk.errorLogCount+' 条'],['自检记录',disk.selfTestCount+' 条']]);var history=disk.selfTests||[];if(!history.length){tests.appendChild(el('div','empty-detail','暂无自检记录'));}else{var testWrap=el('div','smart-table-wrap'),testTable=el('table','smart-table'),testHead=el('thead'),testHeadRow=el('tr');['编号','类型','结果','通电小时','首个错误 LBA'].forEach(function(label){testHeadRow.appendChild(el('th','',label))});testHead.appendChild(testHeadRow);testTable.appendChild(testHead);var testBody=el('tbody');history.forEach(function(test){var tr=el('tr');[shown(test.number),objectText(test.type),objectText(test.status),shown(test.lifetimeHours),shown(test.lba)].forEach(function(value){tr.appendChild(el('td','',value))});testBody.appendChild(tr)});testTable.appendChild(testBody);testWrap.appendChild(testTable);tests.appendChild(testWrap)}var nvme=disk.nvmeHealth||{},keys=Object.keys(nvme);if(keys.length){var nvmeSection=addDetailSection(body,'NVMe 健康数据');addDetailGrid(nvmeSection,nvmeHealthPairs(nvme))}id('detailBackdrop').hidden=false;id('diskDetail').hidden=false;document.body.classList.add('modal-open');body.scrollTop=0;setTimeout(function(){id('closeDetail').focus()},0)}
function closeDiskDetail(){id('detailBackdrop').hidden=true;id('diskDetail').hidden=true;document.body.classList.remove('modal-open');var target=detailFocus;detailFocus=null;if(target&&document.documentElement.contains(target)){var x=scrollX,y=scrollY;try{target.focus({preventScroll:true})}catch(e){target.focus()}if(scrollX!==x||scrollY!==y)scrollTo(x,y)}}
function restartService(item,button){openConfirm('重启 '+item.name,'服务会短暂中断。正在使用该服务的任务可能需要重新连接，是否继续？',function(){button.disabled=true;request('service_restart',{service:item.id}).then(function(){toast(item.name+' 已重启');lastDetails=0;return loadOverview(false)}).catch(function(error){toast(error.message,true)}).finally(function(){button.disabled=false})})}
function activate(page){currentPage=page;document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.dataset.page===page)});document.querySelectorAll('.page').forEach(function(p){p.classList.toggle('active',p.id==='page-'+page)});if(page==='disks')loadSmart(false);else loadOverview(false)}

// Keep service buttons and storage cards stable when their data has not changed.
function onlyWhenChanged(render){var previous='';return function(items){var signature=JSON.stringify(items);if(signature===previous)return;previous=signature;render(items)}}
renderFilesystems=onlyWhenChanged(renderFilesystems);
renderServices=onlyWhenChanged(renderServices);
var overviewTimer=null,disposed=false;
function scheduleOverview(){clearTimeout(overviewTimer);if(disposed||document.hidden)return;overviewTimer=setTimeout(function(){if(disposed)return;var work=currentPage==='overview'?loadOverview(false):Promise.resolve();work.finally(scheduleOverview)},3000)}
function onVisibility(){previousNetwork=null;if(!document.hidden){if(currentPage==='overview')loadOverview(false);scheduleOverview()}else clearTimeout(overviewTimer)}
function disposeOverview(){disposed=true;clearTimeout(overviewTimer);clearTimeout(toastTimer);document.removeEventListener('visibilitychange',onVisibility)}
document.addEventListener('visibilitychange',onVisibility);
window.addEventListener('unmount',disposeOverview);
window.addEventListener('pagehide',disposeOverview);
document.querySelectorAll('.tab').forEach(function(t){t.addEventListener('click',function(){activate(t.dataset.page)})});id('refresh').addEventListener('click',function(){if(currentPage==='disks')loadSmart(true);else loadOverview(true)});id('refreshSmart').addEventListener('click',function(){loadSmart(true)});id('dropCaches').addEventListener('click',function(){var b=this;openConfirm('释放内存缓存','这不会删除文件。短时间内磁盘读取可能增加，是否继续？',function(){b.disabled=true;request('drop_caches').then(function(d){toast('已释放约 '+bytes(d.released));lastDetails=0;return loadOverview(false)}).catch(function(e){toast(e.message,true)}).finally(function(){b.disabled=false})})});id('cleanLogs').addEventListener('click',function(){var b=this;openConfirm('整理系统日志','将删除 7 天前的系统日志，并把日志总量控制在约 32 MB。不会删除用户文件，是否继续？',function(){b.disabled=true;request('clean_logs').then(function(d){toast('已释放约 '+bytes(d.released));lastDetails=0;return loadOverview(false)}).catch(function(e){toast(e.message,true)}).finally(function(){b.disabled=false})})});id('cancelConfirm').addEventListener('click',function(){confirmCallback=null;closeConfirm()});id('backdrop').addEventListener('click',function(){confirmCallback=null;closeConfirm()});id('acceptConfirm').addEventListener('click',function(){var cb=confirmCallback;confirmCallback=null;closeConfirm();if(cb)cb()});id('closeDetail').addEventListener('click',closeDiskDetail);id('detailBackdrop').addEventListener('click',closeDiskDetail);document.addEventListener('keydown',function(e){if(e.key!=='Escape')return;if(!id('confirmDialog').hidden){confirmCallback=null;closeConfirm()}else if(!id('diskDetail').hidden)closeDiskDetail()});var swipe=null;document.querySelector('.shell').addEventListener('touchstart',function(e){if(e.touches.length!==1||e.target.closest('button,a')||!id('confirmDialog').hidden||!id('diskDetail').hidden){swipe=null;return}var t=e.touches[0];if(t.clientX<24||t.clientX>innerWidth-24){swipe=null;return}swipe={x:t.clientX,y:t.clientY}},{passive:true});document.querySelector('.shell').addEventListener('touchend',function(e){if(!swipe||!e.changedTouches.length)return;var t=e.changedTouches[0],dx=t.clientX-swipe.x,dy=t.clientY-swipe.y;swipe=null;if(Math.abs(dx)<55||Math.abs(dx)<Math.abs(dy)*1.35)return;var pages=['overview','disks','tools'],i=pages.indexOf(currentPage),n=dx<0?i+1:i-1;if(n>=0&&n<pages.length)activate(pages[n])},{passive:true});loadOverview(false).finally(scheduleOverview)})();
