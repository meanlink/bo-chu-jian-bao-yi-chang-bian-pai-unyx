/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean,schedule_type:'interval'|'calendar',interval_seconds:number|null,timezone:string|null,weekdays:number[]|null,local_time:string|null,schedule_revision:number,rules:string}} ReportSchedule */
/** @typedef {{id:number,report_id:number,scheduled_for:number,timezone:string,local_time:string,offset_text:string,status:'pending'|'claimed'|'archived'|'missed'|'superseded',schedule_revision:number,delivery_id:number|null,note:string}} ReportOccurrence */
export {};
