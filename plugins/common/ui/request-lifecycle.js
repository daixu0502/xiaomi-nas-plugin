/* Scoped request/timer ownership; never persists a page heartbeat. */
(function (global) {
  'use strict';
  var client=global.XiaomiPluginClient, doc=client.document, send=client.request;
  var disposed=false, epoch=0, active=new Set(), timers=new Set(), waiting=new Set();
  var reads=new Set(('status list templates details history files search check_code browse torrent_browse torrent_preview torrent_nas_preview detail logs overview overview_details smart processes process_network health_details container_list container_stats container_logs container_inspect container_upgrade_status container_bulk_status image_list network_list volume_list system_summary system_disk update_task_status node_test_status node_tests proxies providers rule_providers config subscription network_access core_status geodata_status docker_proxy_status transfers settings_get seed_cache_list').split(' '));
  'network_access_status core_update_status manual_nodes subscription_status config_get node_test_list plugin_update_status credentials_get openlist_info bt_cache_list bt_cache_preview log resources schedule_preview cleanup_preview preview preview_relations preview_repair preview_undo'.split(' ').forEach(function(action){reads.add(action);});
  function cancelled(){var e=new Error('页面已关闭或读取已取消');e.name='AbortError';e.code='REQUEST_CANCELLED';return e;}
  client.setTimer=function(fn,delay){if(disposed)return 0;var id=global.setTimeout(function(){timers.delete(id);if(!disposed)fn();},delay);timers.add(id);return id;};
  client.clearTimer=function(id){timers.delete(id);global.clearTimeout(id);};
  client.request=function(settings){
    if(disposed)return Promise.reject(cancelled());
    var action=settings.action||'status', read=reads.has(action), start;
    var visible=read&&doc.hidden?new Promise(function(resolve,reject){start={resolve:resolve,reject:reject};waiting.add(start);}):Promise.resolve();
    return visible.then(function(){
      if(disposed)throw cancelled();
      var controller=new AbortController(), original=settings.options||{}, timer, external=original.signal;
      var entry={controller:controller,read:read}, finished=false, generation=epoch, timedOut=false;
      function abort(){controller.abort();}
      function clean(){if(finished)return;finished=true;global.clearTimeout(timer);active.delete(entry);if(external)external.removeEventListener('abort',abort);}
      if(external){if(external.aborted)abort();else external.addEventListener('abort',abort,{once:true});}
      active.add(entry);
      timer=global.setTimeout(function(){timedOut=true;abort();},settings.timeoutMs||(read?15000:120000));
      var request=Object.assign({},settings,{options:Object.assign({},original,{signal:controller.signal})});
      return send(request).then(function(response){
        if(read&&(disposed||generation!==epoch)){clean();throw cancelled();}
        ['text','json','blob','arrayBuffer'].forEach(function(method){
          if(!response[method])return;
          var consume=response[method].bind(response);
          response[method]=function(){return consume().then(function(value){if(read&&(disposed||generation!==epoch))throw cancelled();return value;}).finally(clean);};
        });
        return response;
      }).catch(function(error){clean();if(error.name==='AbortError'&&timedOut){error=new Error(read?'读取超时，请重试':'请求超时，请查询实际结果后再操作，勿重复提交');error.code='REQUEST_TIMEOUT';}throw error;});
    });
  };
  function visibility(){
    if(doc.hidden){epoch++;active.forEach(function(entry){if(entry.read)entry.controller.abort();});}
    else {waiting.forEach(function(entry){entry.resolve();});waiting.clear();}
  }
  function dispose(){
    disposed=true;timers.forEach(global.clearTimeout);timers.clear();
    waiting.forEach(function(entry){entry.reject(cancelled());});waiting.clear();
    active.forEach(function(entry){if(entry.read)entry.controller.abort();});
    doc.removeEventListener('visibilitychange',visibility);
    global.removeEventListener('unmount',dispose);global.removeEventListener('pagehide',dispose);
  }
  doc.addEventListener('visibilitychange',visibility);
  global.addEventListener('unmount',dispose);global.addEventListener('pagehide',dispose);
})(window);
