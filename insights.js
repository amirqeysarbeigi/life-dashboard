/* Pure statistical helpers. No network, storage, or DOM access. */
(function(root) {
  'use strict';
  const DAY = 86400000;
  const mean = a => a.length ? a.reduce((s,v) => s+v,0)/a.length : null;
  const date = t => new Date(t).toISOString().slice(0,10);
  function ranks(a) {
    const sorted=a.map((v,i)=>({v,i})).sort((a,b)=>a.v-b.v), out=[];
    for(let i=0;i<sorted.length;) { let j=i+1; while(j<sorted.length && sorted[j].v===sorted[i].v) j++;
      for(let k=i;k<j;k++) out[sorted[k].i]=(i+j-1)/2+1; i=j; }
    return out;
  }
  function correlation(x,y) {
    if(x.length<3 || x.length!==y.length) return null;
    const mx=mean(x),my=mean(y); let xx=0,yy=0,xy=0;
    x.forEach((v,i)=>{const a=v-mx,b=y[i]-my;xx+=a*a;yy+=b*b;xy+=a*b;});
    return xx<1e-10 || yy<1e-10 ? null : Math.max(-1,Math.min(1,xy/Math.sqrt(xx*yy)));
  }
  const spearman=(x,y)=>correlation(ranks(x),ranks(y));
  function residuals(values,dates) {
    const r=ranks(values),groups=Array.from({length:7},()=>[]);
    dates.forEach((d,i)=>groups[new Date(d+'T00:00:00Z').getUTCDay()].push(r[i]));
    const means=groups.map(mean);
    return r.map((v,i)=>v-means[new Date(dates[i]+'T00:00:00Z').getUTCDay()]);
  }
  function adjusted(x,y,dates,yDates) { return correlation(residuals(x,dates),residuals(y,yDates)); }
  function aligned(a,b,days,end,lag=0) {
    const firstA=Object.keys(a.history).sort()[0], firstB=Object.keys(b.history).sort()[0];
    if(!firstA || !firstB) return [];
    const endT=Date.parse(end+'T00:00:00Z');
    const start=Math.max(endT-days*DAY,Date.parse(firstA+'T00:00:00Z'),Date.parse(firstB+'T00:00:00Z')-lag*DAY);
    const rows=[];
    for(let t=start;t+lag*DAY<endT;t+=DAY) {
      const d=date(t),next=date(t+lag*DAY),x=a.history[d]??0,y=b.history[next]??0;
      if(Number.isFinite(x)&&Number.isFinite(y)&&x>=0&&y>=0) rows.push({date:d,yDate:next,x,y});
    }
    return rows;
  }
  function pair(a,b,days,end,lag) {
    const rows=aligned(a,b,days,end,lag),x=rows.map(r=>r.x),y=rows.map(r=>r.y),dates=rows.map(r=>r.date),yDates=rows.map(r=>r.yDate);
    const n=x.length,activeX=x.filter(v=>v>0).length,activeY=y.filter(v=>v>0).length;
    const raw=spearman(x,y),r=adjusted(x,y,dates,yDates),mid=Math.floor(n/2);
    const first=adjusted(x.slice(0,mid),y.slice(0,mid),dates.slice(0,mid),yDates.slice(0,mid));
    const second=adjusted(x.slice(mid),y.slice(mid),dates.slice(mid),yDates.slice(mid));
    let reason=n<28?'Fewer than 28 paired days':Math.min(activeX,activeY)<7?'Fewer than 7 active days in one area':r===null?'No variation after weekday adjustment':null;
    let p=null;
    if(!reason) {
      const rx=residuals(x,dates),ry=residuals(y,yDates); let extreme=1;
      // Circular shifts preserve each series' internal order; an approximate time-series null.
      for(let shift=1;shift<n;shift++) {
        const shifted=ry.map((_,i)=>ry[(i+shift)%n]);
        if(Math.abs(correlation(rx,shifted)??0)>=Math.abs(r)-1e-12) extreme++;
      }
      p=extreme/n;
    }
    const sorted=[...x].sort((a,b)=>a-b),median=sorted.length?(sorted[Math.floor((sorted.length-1)/2)]+sorted[Math.floor(sorted.length/2)])/2:0;
    const high=y.filter((_,i)=>x[i]>median),low=y.filter((_,i)=>x[i]<=median);
    const stable=first!==null && second!==null && Math.sign(first)===Math.sign(second) && Math.abs(first)>=0.15 && Math.abs(second)>=0.15;
    return {a:a.id,b:b.id,lag,n,activeX,activeY,raw,r,first,second,stable,p,q:null,reason,rows,
      highMean:mean(high),lowMean:mean(low),highN:high.length,lowN:low.length,median,
      delta:high.length&&low.length?mean(high)-mean(low):null};
  }
  function rolling(rows,window=28,step=7) {
    const out=[];
    for(let end=window;end<=rows.length;end+=step) {
      const slice=rows.slice(end-window,end),x=slice.map(r=>r.x),y=slice.map(r=>r.y);
      const enough=x.filter(v=>v>0).length>=7&&y.filter(v=>v>0).length>=7;
      out.push({date:slice.at(-1).yDate,r:enough?adjusted(x,y,slice.map(r=>r.date),slice.map(r=>r.yDate)):null});
    }
    return out;
  }
  function correct(results) {
    // Benjamini–Yekutieli accounts for arbitrary dependence between comparisons.
    const tested=results.filter(r=>r.p!==null).sort((a,b)=>a.p-b.p),m=tested.length;
    const harmonic=Array.from({length:m},(_,i)=>1/(i+1)).reduce((a,b)=>a+b,0);
    let prev=1;
    for(let i=m-1;i>=0;i--) {prev=Math.min(prev,tested[i].p*m*harmonic/(i+1));tested[i].q=prev;}
    return results;
  }
  function analyze(domains,days,end) {
    const results=[];
    for(let i=0;i<domains.length;i++) for(let j=i+1;j<domains.length;j++) {
      results.push(pair(domains[i],domains[j],days,end,0));
      results.push(pair(domains[i],domains[j],days,end,1));
      results.push(pair(domains[j],domains[i],days,end,1));
    }
    correct(results);
    results.forEach(r=>{r.screened=!r.reason&&r.q<=0.1&&Math.abs(r.r)>=0.3&&r.stable;});
    return {results,days,end,domains};
  }
  const api={mean,ranks,correlation,spearman,residuals,aligned,pair,rolling,correct,analyze};
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  else root.RelationshipStats=api;
})(typeof globalThis!=='undefined'?globalThis:this);
