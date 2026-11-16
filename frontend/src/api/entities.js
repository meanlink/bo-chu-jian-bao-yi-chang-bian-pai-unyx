import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={
 list:()=>api('/reports'),
 get:id=>api('/reports/'+id),
 create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),
 reschedule:(id,data)=>api('/reports/'+id+'/reschedule',{method:'POST',body:JSON.stringify(data)}),
 toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),
 preview:data=>api('/reports/preview',{method:'POST',body:JSON.stringify(data)}),
 upcoming:id=>api('/reports/'+id+'/upcoming'),
 occurrences:id=>api('/reports/'+id+'/occurrences'),
 history:id=>api('/reports/'+id+'/history'),
 deliveries:()=>api('/deliveries')
};
