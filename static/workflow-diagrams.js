(() => {
  const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
  const text=key=>window.skeinI18n.t(key);
  const validate=tasks=>{
    if(!Array.isArray(tasks)||!tasks.length)throw Error(text("graphNeedsTasks"));
    const byKey=new Map(tasks.map(task=>[task.key,task]));
    if(byKey.size!==tasks.length||tasks.some(task=>!task.key||!Array.isArray(task.dependencies)||task.dependencies.some(key=>!byKey.has(key))))throw Error(text("graphInvalidDependencies"));
    const visiting=new Set(),levels=new Map();
    const depth=key=>{if(levels.has(key))return levels.get(key);if(visiting.has(key))throw Error(text("graphCycleDetected"));visiting.add(key);const task=byKey.get(key),value=task.dependencies.length?1+Math.max(...task.dependencies.map(depth)):0;visiting.delete(key);levels.set(key,value);return value};
    tasks.forEach(task=>depth(task.key));return {byKey,levels};
  };
  const actionLabel=task=>`${task.action_type||"llm"} · ${task.output_format||"markdown"}`;
  const activitySvg=tasks=>{
    const {levels}=validate(tasks),groups=new Map();tasks.forEach(task=>{const level=levels.get(task.key);if(!groups.has(level))groups.set(level,[]);groups.get(level).push(task)});
    const rows=[...groups.keys()].length,width=Math.max(820,Math.max(...[...groups.values()].map(group=>group.length))*280),height=130+rows*180,positions=new Map();
    [...groups].forEach(([level,group])=>group.forEach((task,index)=>positions.set(task.key,{x:70+index*(width-140)/Math.max(1,group.length),y:100+level*180,task})));
    const start=`<ellipse cx="${width/2}" cy="32" rx="44" ry="20"/><text x="${width/2}" y="37" text-anchor="middle">${esc(text("diagramStart"))}</text>`;
    const starters=tasks.filter(task=>!task.dependencies.length).map(task=>{const to=positions.get(task.key);return `<path d="M ${width/2} 52 C ${width/2} 72, ${to.x} 72, ${to.x} ${to.y-34}"/>`}).join("");
    const edges=tasks.flatMap(task=>task.dependencies.map(dependency=>{const from=positions.get(dependency),to=positions.get(task.key);return `<path d="M ${from.x} ${from.y+34} C ${from.x} ${from.y+90}, ${to.x} ${to.y-90}, ${to.x} ${to.y-34}"/>`})).join("");
    const nodes=[...positions.values()].map(({x,y,task})=>{const condition=task.action_config?.condition,shape=condition?`<polygon points="${x},${y-42} ${x+112},${y} ${x},${y+42} ${x-112},${y}"/>`:`<rect x="${x-112}" y="${y-36}" width="224" height="72" rx="16"/>`;return `<g class="activity-node ${condition?'decision':''}">${shape}<text x="${x}" y="${y-10}" text-anchor="middle" class="node-title">${esc(String(task.title||task.key).slice(0,34))}</text><text x="${x}" y="${y+11}" text-anchor="middle">${esc(actionLabel(task))}</text>${condition?`<text x="${x+118}" y="${y-7}" class="branch-label">${esc(text("diagramTrue"))}</text><text x="${x+118}" y="${y+17}" class="branch-label">${esc(text("diagramFalse"))}</text>`:""}</g>`}).join("");
    const terminal=tasks.find(task=>!tasks.some(other=>other.dependencies.includes(task.key))),endY=height-35,endFrom=positions.get(terminal.key);
    const end=`<path d="M ${endFrom.x} ${endFrom.y+36} C ${endFrom.x} ${endY-45}, ${width/2} ${endY-45}, ${width/2} ${endY-22}"/><ellipse cx="${width/2}" cy="${endY}" rx="44" ry="20"/><ellipse cx="${width/2}" cy="${endY}" rx="38" ry="15"/><text x="${width/2}" y="${endY+5}" text-anchor="middle">${esc(text("diagramEnd"))}</text>`;
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(text("algorithmDiagram"))}"><defs><marker id="activity-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z"/></marker></defs><g class="activity-edges">${starters}${edges}</g><g class="activity-nodes">${start}${nodes}${end}</g></svg>`;
  };
  const participantFor=task=>task.action_type==="command"||task.action_type==="script"?"Runtime":task.role==="integrator"||["architect","analyst","reviewer","security-reviewer","researcher","workflow-reporter"].includes(task.role)?"Reasoner":"Worker";
  const sequenceSvg=tasks=>{
    const {levels}=validate(tasks),participants=["User","Orchestrator","Reasoner","Worker","Runtime"],labels={User:text("sequenceUser"),Orchestrator:text("sequenceOrchestrator"),Reasoner:"Reasoner",Worker:"Worker",Runtime:text("sequenceRuntime")};
    const width=1000,margin=90,gap=(width-margin*2)/(participants.length-1),headerY=45,rowHeight=92,height=125+tasks.length*rowHeight,positions=Object.fromEntries(participants.map((name,index)=>[name,margin+index*gap]));
    const heads=participants.map(name=>`<g><rect x="${positions[name]-78}" y="18" width="156" height="46" rx="8"/><text x="${positions[name]}" y="46" text-anchor="middle">${esc(labels[name])}</text><path class="lifeline" d="M ${positions[name]} 64 L ${positions[name]} ${height-30}"/></g>`).join("");
    let lastDepth=-1;const messages=tasks.map((task,index)=>{const y=105+index*rowHeight,target=participantFor(task),condition=task.action_config?.condition,depth=levels.get(task.key),parallel=depth===lastDepth;lastDepth=depth;const prefix=parallel?`<text x="18" y="${y-18}" class="fragment">par</text>`:"";const conditionText=condition?`<rect class="fragment-box" x="12" y="${y-35}" width="976" height="76" rx="5"/><text x="18" y="${y-18}" class="fragment">opt ${esc(condition.output_schema||text("condition"))}</text>`:"";return `${prefix}${conditionText}<path class="message" d="M ${positions.Orchestrator} ${y} L ${positions[target]-9} ${y}"/><text x="${(positions.Orchestrator+positions[target])/2}" y="${y-8}" text-anchor="middle">${esc(task.title)}</text><text x="${(positions.Orchestrator+positions[target])/2}" y="${y+17}" text-anchor="middle" class="message-meta">${esc(actionLabel(task))}</text><path class="return" d="M ${positions[target]} ${y+32} L ${positions.Orchestrator+9} ${y+32}"/>`}).join("");
    const begin=`<path class="message" d="M ${positions.User} 82 L ${positions.Orchestrator-9} 82"/><text x="${(positions.User+positions.Orchestrator)/2}" y="75" text-anchor="middle">${esc(text("userRequest"))}</text>`,finishY=height-44,finish=`<path class="return" d="M ${positions.Orchestrator} ${finishY} L ${positions.User+9} ${finishY}"/><text x="${(positions.User+positions.Orchestrator)/2}" y="${finishY-7}" text-anchor="middle">${esc(text("finalDeliverable"))}</text>`;
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(text("sequenceDiagram"))}"><defs><marker id="sequence-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z"/></marker></defs>${heads}${begin}${messages}${finish}</svg>`;
  };
  const enableViewport=(panel,svg)=>{
    const canvas=panel.querySelector(".diagram-canvas"),viewport=panel.querySelector(".diagram-viewport");let scale=1,x=0,y=0,drag=null;
    const apply=()=>canvas.style.transform=`translate(${x}px,${y}px) scale(${scale})`,zoom=delta=>{scale=Math.max(.35,Math.min(3,scale+delta));apply()};
    panel.querySelector('[data-zoom="in"]').onclick=()=>zoom(.2);panel.querySelector('[data-zoom="out"]').onclick=()=>zoom(-.2);panel.querySelector('[data-zoom="reset"]').onclick=()=>{scale=1;x=0;y=0;apply()};
    panel.querySelector('[data-zoom="fullscreen"]').onclick=()=>targetFullscreen(panel.closest(".workflow-graph"));
    viewport.onwheel=event=>{event.preventDefault();zoom(event.deltaY<0?.12:-.12)};
    viewport.onpointerdown=event=>{drag={x:event.clientX-x,y:event.clientY-y};viewport.setPointerCapture(event.pointerId);viewport.classList.add("dragging")};
    viewport.onpointermove=event=>{if(!drag)return;x=event.clientX-drag.x;y=event.clientY-drag.y;apply()};
    viewport.onpointerup=event=>{drag=null;viewport.releasePointerCapture(event.pointerId);viewport.classList.remove("dragging")};
  };
  const targetFullscreen=async target=>{
    if(target.classList.contains("diagram-fullscreen")){target.classList.remove("diagram-fullscreen");return}
    if(document.fullscreenElement){await document.exitFullscreen();return}
    try{await target.requestFullscreen()}catch(error){target.classList.add("diagram-fullscreen")}
  };
  const panelMarkup=(kind,svg)=>`<div class="workflow-diagram ${kind==="algorithm"?"active":""}" data-diagram-panel="${kind}"><div class="diagram-toolbar"><button type="button" data-zoom="out" title="${esc(text("zoomOut"))}">−</button><button type="button" data-zoom="in" title="${esc(text("zoomIn"))}">+</button><button type="button" data-zoom="reset" title="${esc(text("resetView"))}">1:1</button><button type="button" data-zoom="fullscreen" title="${esc(text("fullscreen"))}">⛶</button></div><div class="diagram-viewport"><div class="diagram-canvas">${svg}</div></div></div>`;
  const render=(target,tasks)=>{try{
    validate(tasks);target.classList.remove("graph-error");
    target.innerHTML=`<div class="diagram-tabs" role="tablist"><button type="button" class="active" data-diagram="algorithm">${esc(text("algorithmDiagram"))}</button><button type="button" data-diagram="sequence">${esc(text("sequenceDiagram"))}</button></div>${panelMarkup("algorithm",activitySvg(tasks))}${panelMarkup("sequence",sequenceSvg(tasks))}`;
    target.querySelectorAll("[data-diagram]").forEach(button=>button.onclick=()=>{target.querySelectorAll("[data-diagram]").forEach(item=>item.classList.toggle("active",item===button));target.querySelectorAll("[data-diagram-panel]").forEach(panel=>panel.classList.toggle("active",panel.dataset.diagramPanel===button.dataset.diagram))});
    target.querySelectorAll("[data-diagram-panel]").forEach(panel=>enableViewport(panel));
  }catch(error){target.textContent=error.message;target.classList.add("graph-error")}};
  window.skeinWorkflowDiagrams={render,activitySvg,sequenceSvg};
})();
