/* ==========================================================
   SEGURANÇA — Alertas, usuários, auditoria e configurações
   ========================================================== */
'use strict';

/* ==========================================================
   ALERTAS
   ========================================================== */
async function renderAlertas(alvo){
  const resumo = await API.resumoAlertas();
  const f = FILTRO.alertas, hoje = diaISO(new Date());

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Central de alertas</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="segm" id="alSeg">
        <button data-s="" class="${!f.status?'on':''}">Todos</button>
        <button data-s="ABERTO" class="${f.status==='ABERTO'?'on':''}">Abertos (${resumo.abertos||0})</button>
        <button data-s="EM_ANDAMENTO" class="${f.status==='EM_ANDAMENTO'?'on':''}">Em curso (${resumo.em_andamento||0})</button>
        <button data-s="RESOLVIDO" class="${f.status==='RESOLVIDO'?'on':''}">Resolvidos (${resumo.resolvidos||0})</button>
      </div>
      <select class="inp" id="alSev"><option value="">Qualquer severidade</option>
        ${[4,3,2].map(s => `<option value="${s}" ${f.severidade==String(s)?'selected':''}>${SEVERIDADE[s]}</option>`).join('')}</select>
      <div class="periodo">
        <label for="alDe">Período</label>
        <input class="inp" id="alDe" type="date" value="${esc(f.de)}" max="${esc(f.ate || hoje)}">
        <span class="sep">até</span>
        <input class="inp" id="alAte" type="date" value="${esc(f.ate)}" min="${esc(f.de)}" max="${hoje}">
        <button class="btn btn-ghost btn-sm" id="alLimpar" ${f.de||f.ate?'':'hidden'}>Limpar</button>
      </div>
    </div></div>
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="kpi k-red"><div class="kpi-top"><span class="kpi-lbl">Abertos</span><span class="kpi-ico">${ico('bell',15)}</span></div><div class="kpi-val">${resumo.abertos||0}</div><div class="kpi-delta dn">aguardando triagem</div></div>
    <div class="kpi k-amber"><div class="kpi-top"><span class="kpi-lbl">Em andamento</span><span class="kpi-ico">${ico('clock',15)}</span></div><div class="kpi-val">${resumo.em_andamento||0}</div><div class="kpi-delta nt">com responsável atribuído</div></div>
    <div class="kpi k-green"><div class="kpi-top"><span class="kpi-lbl">Resolvidos</span><span class="kpi-ico">${ico('check',15)}</span></div><div class="kpi-val">${resumo.resolvidos||0}</div><div class="kpi-delta up">com desfecho registrado</div></div>
    <div class="kpi"><div class="kpi-top"><span class="kpi-lbl">Tempo médio de resposta</span><span class="kpi-ico">${ico('gauge',15)}</span></div>
      <div class="kpi-val" style="font-size:22px">${resumo.tempo_medio_resposta_s ? duracao(resumo.tempo_medio_resposta_s) : '—'}</div>
      <div class="kpi-delta nt">${resumo.criticos_pendentes||0} crítico(s) pendente(s)</div></div>
  </div>
  <div class="panel"><div class="panel-bd tight"><div id="alCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#alCorpo');
    corpo.innerHTML = carregando();
    try{
      const r = await API.alertas({
        pagina: PAG.alertas, por_pagina: POR_PAGINA,
        status: FILTRO.alertas.status || undefined,
        severidade: FILTRO.alertas.severidade || undefined,
        desde: limiteDia(FILTRO.alertas.de),
        ate: limiteDia(FILTRO.alertas.ate, true)
      });
      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr><th style="width:8px"></th><th>Alerta</th><th>Tipo</th><th>Câmera</th>
          <th>Data / hora</th><th>Status</th><th style="width:110px"></th></tr></thead>
        <tbody>${(r.itens||[]).map(a => {
          const st = STATUS_ALERTA[a.status] || {};
          return `<tr class="clickable" data-a="${esc(a.codigo)}">
            <td style="padding:0"><div class="sev sev-${a.severidade}" style="height:100%;min-height:44px;width:3px"></div></td>
            <td><b>${esc(a.titulo)}</b><br><span class="td-ts">${esc((a.detalhe||'').slice(0,72))}${(a.detalhe||'').length>72?'…':''}</span></td>
            <td><span class="chip ${a.tipo==='FACIAL'?'c-blue':a.tipo==='OBJETO'?'c-green':'c-violet'}">${esc(a.tipo)}</span></td>
            <td><b>${esc(a.camera?a.camera.codigo:'')}</b><br><span class="td-ts">${esc((a.camera?a.camera.nome:'').slice(0,24))}</span></td>
            <td class="td-ts">${fmtDT(a.disparado_em)}<br><span style="color:var(--txt-4)">${relTime(a.disparado_em)}</span></td>
            <td><span class="chip ${st.cls}">${esc(st.lbl)}</span></td>
            <td style="text-align:right">${a.status!=='RESOLVIDO' && API.pode('edit')
              ? `<button class="btn btn-soft btn-sm" data-assume="${esc(a.codigo)}">Assumir</button>` : ''}</td>
          </tr>`;
        }).join('') || `<tr><td colspan="7">${vazio('check','Nenhum alerta neste filtro.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'alertas')}`;

      $$('tr[data-a]', corpo).forEach(t => t.onclick = () => abrirAlerta(t.dataset.a));
      $$('[data-assume]', corpo).forEach(b => b.onclick = async e => {
        e.stopPropagation();
        try{
          await API.assumirAlerta(b.dataset.assume);
          toast('Alerta assumido', `${b.dataset.assume} atribuído a ${API.usuario.nome}.`, 'ok');
          irPara('alerts');
        }catch(err){ erroToast(err); }
      });
      ligarPager(corpo, 'alertas', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  const aplicar = () => { PAG.alertas = 1; FILTRO.alertas = {...FILTRO.alertas, de: $('#alDe').value, ate: $('#alAte').value}; $('#alDe').max = FILTRO.alertas.ate || hoje; $('#alAte').min = FILTRO.alertas.de; $('#alLimpar').hidden = !(FILTRO.alertas.de || FILTRO.alertas.ate); carregar(); };
  $$('#alSeg button').forEach(b => b.onclick = () => {
    FILTRO.alertas.status = b.dataset.s; PAG.alertas = 1; irPara('alerts');
  });
  $('#alSev').onchange = e => { FILTRO.alertas.severidade = e.target.value; PAG.alertas = 1; carregar(); };
  ['alDe','alAte'].forEach(id => $('#'+id).onchange = aplicar);
  $('#alLimpar').onclick = () => { $('#alDe').value = ''; $('#alAte').value = ''; aplicar(); };
  await carregar();
}

async function abrirAlerta(codigo){
  const box = abrirModal(`<div class="modal-bd">${carregando()}</div>`, true);
  let a;
  try{ a = await API.alerta(codigo); }
  catch(e){ box.innerHTML = `<div class="modal-bd">${erroBloco(e)}</div>`; return; }

  const st = STATUS_ALERTA[a.status] || {};
  const p = a.pessoa;
  const podeFechar = a.status !== 'RESOLVIDO' && API.pode('edit');

  box.className = 'modal wide';
  box.innerHTML = `
    <div class="modal-hd">
      <span class="sev sev-${a.severidade}" style="height:26px;width:3px"></span>
      <h3>${esc(a.titulo)}</h3><span class="chip ${st.cls}">${esc(st.lbl)}</span>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <p style="margin:0 0 15px;color:var(--txt-2);font-size:13.5px;line-height:1.55">${esc(a.detalhe)}</p>
      ${p ? `<div style="display:flex;gap:12px;align-items:center;padding:11px;border:1px solid var(--line);border-radius:var(--r-m);background:var(--ink-800);margin-bottom:15px">
        <canvas class="bioface" data-face="${esc(p.id_biometrico)}" data-sexo="${esc(p.sexo)}" width="48" height="48"></canvas>
        <div style="flex:1;min-width:0"><b style="display:block">${esc(p.nome)}</b>
        <span class="mono" style="font-size:12px;color:var(--acc-2)">${esc(p.id_biometrico)}</span></div>
        <span class="chip ${(STATUS_PESSOA[p.status]||{}).cls}">${esc((STATUS_PESSOA[p.status]||{}).lbl)}</span>
      </div>` : ''}
      <dl class="kv">
        <dt>Identificador</dt><dd class="mono">${esc(a.codigo)}</dd>
        <dt>Câmera</dt><dd>${esc(a.camera?a.camera.nome:'')} <span class="mono" style="color:var(--txt-3)">(${esc(a.camera?a.camera.codigo:'')})</span></dd>
        <dt>Setor</dt><dd>${esc(a.camera?a.camera.setor:'—')}</dd>
        <dt>Severidade</dt><dd><span class="chip ${CLS_SEV[a.severidade]}">${esc(SEVERIDADE[a.severidade])}</span></dd>
        <dt>Disparado em</dt><dd class="mono">${fmtDT(a.disparado_em)}</dd>
        ${a.fechado_em ? `<dt>Encerrado em</dt><dd class="mono">${fmtDT(a.fechado_em)}</dd>
        <dt>Desfecho</dt><dd>${esc(a.resolucao)}</dd>` : ''}
      </dl>
      ${podeFechar ? `<div class="field" style="margin-top:15px">
        <label for="alRes">Registro do desfecho</label>
        <textarea class="inp" id="alRes" placeholder="Descreva a providência adotada — despacho de guarnição, confirmação de falso positivo, encaminhamento…"></textarea>
        <span class="hint">Obrigatório: um alerta sem registro do que foi feito não serve nem à operação nem à prestação de contas.</span>
      </div>` : ''}
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      ${a.camera ? `<button class="btn btn-soft" id="alCam">${ico('live',14)} Ver câmera</button>` : ''}
      ${podeFechar ? `<button class="btn btn-primary" id="alClose">${ico('check',14)} Encerrar alerta</button>` : ''}
    </div>`;

  $$('[data-close]', box).forEach(b => b.onclick = fecharModal);
  pintarFaces(box);
  const bc = $('#alCam'); if(bc) bc.onclick = () => { fecharModal(); abrirCamera(a.camera.codigo); };

  if(podeFechar) $('#alClose').onclick = async () => {
    const txt = $('#alRes').value.trim();
    if(txt.length < 8){ toast('Desfecho obrigatório', 'Descreva a providência adotada antes de encerrar.', 'err'); return; }
    try{
      await API.encerrarAlerta(a.codigo, txt);
      fecharModal();
      toast('Alerta encerrado', `${a.codigo} — desfecho registrado na trilha de auditoria.`, 'ok');
      irPara('alerts');
    }catch(e){ erroToast(e); }
  };
}

/* ==========================================================
   USUÁRIOS
   ========================================================== */
const PERMS_COLUNAS = ['live','cams','facial','biometrics','alerts','users','audit','settings','export','edit'];
const PERMS_ROTULO = {live:'Ao vivo', cams:'Câmeras', facial:'Registros', biometrics:'Base bio.',
  alerts:'Alertas', users:'Usuários', audit:'Auditoria', settings:'Config.', export:'Exportar', edit:'Editar'};

async function renderUsuarios(alvo){
  const [perfis, usuarios] = await Promise.all([API.perfis(), API.usuarios()]);

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Usuários e perfis de acesso</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="usQ" placeholder="Nome, matrícula, e-mail…"></div>
      <button class="btn btn-primary btn-sm" id="usNovo">${ico('plus',14)} Novo usuário</button>
    </div></div>
  <div class="panel" style="margin-bottom:14px">
    <div class="panel-hd"><h3>Matriz de permissões</h3><span class="sub">o que cada perfil pode fazer</span></div>
    <div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Perfil</th>${PERMS_COLUNAS.map(p => `<th style="text-align:center">${PERMS_ROTULO[p]}</th>`).join('')}</tr></thead>
      <tbody>${(perfis.perfis||[]).map(pf => `<tr>
        <td><b>${esc(rotuloPerfil(pf.codigo))}</b><br><span class="td-id">${esc(pf.codigo)}</span></td>
        ${PERMS_COLUNAS.map(p => `<td style="text-align:center">${pf.permissoes.includes(p)
          ? `<span style="color:var(--green)">${ico('check',14)}</span>`
          : '<span style="color:var(--txt-4)">—</span>'}</td>`).join('')}
      </tr>`).join('')}</tbody></table></div></div></div>
  <div class="panel"><div class="panel-bd tight"><div class="tbl-wrap"><table class="tbl" id="usTbl">
    <thead><tr><th>Usuário</th><th>Matrícula</th><th>Perfil</th><th>Unidade</th><th>MFA</th>
      <th>Último acesso</th><th>Status</th><th style="width:80px"></th></tr></thead>
    <tbody></tbody></table></div></div></div>`;

  const pintar = lista => {
    $('#usTbl tbody').innerHTML = lista.map(u => `
      <tr class="clickable" data-u="${esc(u.id)}">
        <td><div class="face-cell"><span class="avatar" style="width:30px;height:30px;font-size:11px">${esc(iniciais(u.nome))}</span>
          <div class="face-meta"><b>${esc(u.nome)}</b><span style="color:var(--txt-3);font-family:var(--f-mono)">${esc(u.email)}</span></div></div></td>
        <td class="mono">${esc(u.matricula)}</td>
        <td><span class="chip ${u.perfil==='ADMIN'?'c-red':u.perfil==='SUPER'?'c-amber':u.perfil==='AUDITOR'?'c-violet':'c-blue'}">${esc(rotuloPerfil(u.perfil))}</span></td>
        <td>${esc(u.unidade||'—')}</td>
        <td>${u.mfa_ativo ? '<span class="chip c-green">Ativo</span>' : '<span class="chip c-amber">Inativo</span>'}</td>
        <td class="td-ts">${u.ultimo_acesso ? fmtDT(u.ultimo_acesso) + '<br><span style="color:var(--txt-4)">' + relTime(u.ultimo_acesso) + '</span>' : '—'}</td>
        <td>${u.ativo ? '<span class="chip c-green">Ativo</span>' : '<span class="chip c-gray">Desativado</span>'}</td>
        <td style="text-align:right"><button class="btn btn-ghost btn-sm btn-icon" data-eu="${esc(u.id)}">${ico('edit',14)}</button></td>
      </tr>`).join('') || `<tr><td colspan="8">${vazio('users','Nenhum usuário encontrado.')}</td></tr>`;
    $$('#usTbl [data-eu]').forEach(b => b.onclick = e => { e.stopPropagation(); editarUsuario(b.dataset.eu, usuarios, perfis); });
    $$('#usTbl tr[data-u]').forEach(t => t.onclick = () => editarUsuario(t.dataset.u, usuarios, perfis));
  };

  $('#usQ').oninput = debounce(() => {
    const q = $('#usQ').value.toLowerCase().trim();
    pintar(usuarios.filter(u => !q ||
      [u.nome, u.email, u.matricula, u.unidade].join(' ').toLowerCase().includes(q)));
  }, 200);
  $('#usNovo').onclick = () => editarUsuario(null, usuarios, perfis);
  pintar(usuarios);
}

function editarUsuario(id, usuarios, perfis){
  const novo = !id;
  const u = novo ? null : usuarios.find(x => x.id === id);
  const listaPerfis = (perfis.perfis || []).map(p => p.codigo);

  abrirModal(`
    <div class="modal-hd"><h3>${novo ? 'Novo usuário' : 'Editar usuário'}</h3>
      <span class="sp" style="flex:1"></span><button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div class="form-grid">
        <div class="field full"><label for="uNome">Nome e posto/graduação</label>
          <input class="inp" id="uNome" value="${u?esc(u.nome):''}" placeholder="Ex.: Sgt. Priscila Tavares"></div>
        <div class="field"><label for="uMat">Matrícula</label>
          <input class="inp mono" id="uMat" value="${u?esc(u.matricula):''}" placeholder="PA-000000"></div>
        <div class="field"><label for="uMail">E-mail funcional</label>
          <input class="inp mono" id="uMail" value="${u?esc(u.email):''}" placeholder="nome@pm.gov.br"></div>
        <div class="field"><label for="uPerfil">Perfil de acesso</label>
          <select class="inp" id="uPerfil">${listaPerfis.map(p =>
            `<option value="${p}" ${u&&u.perfil===p?'selected':''}>${esc(rotuloPerfil(p))}</option>`).join('')}</select></div>
        <div class="field"><label for="uUni">Unidade</label>
          <select class="inp" id="uUni">${['Comando Geral','COPOM','Inteligência','1º BPM','2º BPM','3º BPM','Corregedoria']
            .map(x => `<option ${u&&u.unidade===x?'selected':''}>${x}</option>`).join('')}</select></div>
      </div>
      <div class="eyebrow" style="margin:17px 0 4px">Permissões efetivas</div>
      <div id="permBox" style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px"></div>
      <div class="sw-row" data-usw="mfa_ativo"><div class="bd"><b>Autenticação em dois fatores</b>
        <span>Recomendada para todos os perfis com permissão de edição</span></div>
        <span class="sw ${(!u||u.mfa_ativo)?'on':''}"></span></div>
      <div class="sw-row" data-usw="ativo"><div class="bd"><b>Conta ativa</b>
        <span>Contas desativadas não autenticam nem mantêm sessão</span></div>
        <span class="sw ${(!u||u.ativo)?'on':''}"></span></div>
      ${novo ? `<div class="note" style="margin-top:14px">${ico('key',15)}<div>Uma senha provisória será gerada e exibida uma única vez. O usuário deverá redefini-la no primeiro acesso.</div></div>` : ''}
    </div>
    <div class="modal-ft">
      ${!novo ? `<button class="btn btn-soft" id="uReset">${ico('key',14)} Forçar troca de senha</button>` : ''}
      <span style="flex:1"></span>
      <button class="btn btn-ghost" data-close>Cancelar</button>
      <button class="btn btn-primary" id="uSave">${ico('check',14)} ${novo ? 'Criar usuário' : 'Salvar'}</button>
    </div>`);

  const flags = {mfa_ativo: !u || u.mfa_ativo, ativo: !u || u.ativo};
  $$('[data-usw]').forEach(r => r.onclick = () => {
    const k = r.dataset.usw; flags[k] = !flags[k];
    r.querySelector('.sw').classList.toggle('on', flags[k]);
  });
  const pintarPerms = () => {
    const pf = (perfis.perfis || []).find(p => p.codigo === $('#uPerfil').value);
    $('#permBox').innerHTML = (pf ? pf.permissoes : []).map(p => `<span class="chip c-blue">${esc(p)}</span>`).join('');
  };
  $('#uPerfil').onchange = pintarPerms;
  pintarPerms();

  const br = $('#uReset');
  if(br) br.onclick = async () => {
    try{
      const r = await API.forcarTrocaSenha(u.id);
      fecharModal();
      toast('Troca de senha exigida', r.mensagem, 'warn', 5000);
    }catch(e){ erroToast(e); }
  };

  $('#uSave').onclick = async () => {
    const dados = {
      nome: $('#uNome').value.trim(),
      email: $('#uMail').value.trim().toLowerCase(),
      matricula: $('#uMat').value.trim(),
      perfil: $('#uPerfil').value,
      unidade: $('#uUni').value,
      ...flags
    };
    if(dados.nome.length < 4){ toast('Nome inválido', 'Informe nome e posto/graduação.', 'err'); return; }
    if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(dados.email)){
      toast('E-mail inválido', 'Use o e-mail funcional institucional.', 'err'); return;
    }
    const btn = $('#uSave'); btn.disabled = true;
    try{
      if(novo){
        const r = await API.criarUsuario(dados);
        fecharModal();
        if(r.senha_provisoria){
          abrirModal(`
            <div class="modal-hd"><h3>Usuário criado</h3><span class="sp" style="flex:1"></span>
              <button class="x" data-close>${ico('x',17)}</button></div>
            <div class="modal-bd">
              <p style="margin:0 0 14px;color:var(--txt-2)">Entregue esta senha provisória a <b>${esc(dados.nome)}</b> por canal seguro. Ela não será exibida de novo.</p>
              <div class="demo-box"><div class="eyebrow">Senha provisória</div>
                <div class="mono" style="font-size:19px;color:var(--acc-2);padding:8px 0;user-select:all">${esc(r.senha_provisoria)}</div></div>
              <div class="note" style="margin-top:14px">${ico('info',15)}<div>A troca é obrigatória no primeiro acesso.</div></div>
            </div>
            <div class="modal-ft"><button class="btn btn-primary" data-close>Entendi</button></div>`);
          $$('[data-close]').forEach(b => b.onclick = () => { fecharModal(); irPara('users'); });
        }else{
          toast('Usuário criado', dados.nome, 'ok');
          irPara('users');
        }
      }else{
        await API.salvarUsuario(u.id, dados);
        fecharModal();
        toast('Usuário atualizado', `${dados.nome} · ${rotuloPerfil(dados.perfil)}`, 'ok');
        irPara('users');
      }
    }catch(e){ erroToast(e); }
    finally{ btn.disabled = false; }
  };
}

/* ==========================================================
   AUDITORIA
   ========================================================== */
async function renderAuditoria(alvo){
  const acoes = await API.acoesAuditoria();

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Trilha de auditoria</h2><span class="sp"></span>
    <div class="toolbar">
      <div class="search">${ico('search',14)}<input class="inp" id="auQ" value="${esc(FILTRO.audit.q)}" placeholder="Usuário, ação, alvo, IP…"></div>
      <select class="inp" id="auAcao"><option value="">Todas as ações</option>
        ${acoes.map(a => `<option value="${esc(a.acao)}" ${FILTRO.audit.acao===a.acao?'selected':''}>${esc(a.acao)} (${a.total})</option>`).join('')}</select>
      <button class="btn btn-ghost btn-sm" id="auVerificar">${ico('shield',14)} Verificar cadeia</button>
      ${API.pode('export') ? `<button class="btn btn-ghost btn-sm" id="auExp">${ico('dl',14)} Exportar</button>` : ''}
    </div></div>
  <div id="auVerif"></div>
  <div class="note" style="margin-bottom:14px">${ico('lock',15)}<div>Registros encadeados por hash e somente-inserção — uma trigger no banco recusa <span class="mono">UPDATE</span> e <span class="mono">DELETE</span>. Consulta de biométricos, exportação ou mudança de limiar fica vinculada à matrícula do operador.</div></div>
  <div class="panel"><div class="panel-bd tight"><div id="auCorpo">${carregando()}</div></div></div>`;

  const carregar = async () => {
    const corpo = $('#auCorpo');
    FILTRO.audit = {q: $('#auQ').value.trim(), acao: $('#auAcao').value};
    try{
      const r = await API.auditoria({
        pagina: PAG.audit, por_pagina: 30,
        q: FILTRO.audit.q || undefined, acao: FILTRO.audit.acao || undefined
      });
      const itens = r.itens || [];

      const iconoPorAcao = (acao) => {
        if(acao.includes('LOGIN')) return 'key';
        if(acao.includes('LOGOUT')) return 'logout';
        if(acao.includes('DELETE')) return 'trash';
        if(acao.includes('UPDATE')) return 'edit';
        if(acao.includes('EXPORT')) return 'dl';
        if(acao.includes('CADEIA')) return 'shield';
        return 'log';
      };

      corpo.innerHTML = `<div class="tbl-wrap"><table class="tbl">
        <thead><tr>
          <th style="width:8px"></th>
          <th>Data / hora</th>
          <th>Ação</th>
          <th>Descrição</th>
          <th>Usuário</th>
          <th>Alvo</th>
          <th style="width:80px">IP</th></tr></thead>
        <tbody>${itens.map(a => `<tr class="audit-row">
          <td style="padding:0"><div style="height:100%;min-height:52px;width:3px;background:${
            a.severidade === '4' ? 'var(--red)' :
            a.severidade === '3' ? 'var(--amber)' :
            a.severidade === '2' ? 'var(--green)' : 'var(--acc)'
          }"></div></td>
          <td class="mono" style="font-size:12px;white-space:nowrap"><b>${fmtDT(a.ocorrido_em).split(' ')[0]}</b><br><span style="color:var(--txt-4)">${fmtDT(a.ocorrido_em).split(' ')[1]}</span></td>
          <td><div style="display:flex;align-items:center;gap:7px"><span style="width:20px;text-align:center">${ico(iconoPorAcao(a.acao),14)}</span><b style="white-space:nowrap">${esc(a.acao)}</b></div></td>
          <td style="max-width:280px"><span style="color:var(--txt-2);font-size:13px">${esc(a.descricao || '—')}</span></td>
          <td><b style="display:block">${esc(a.usuario_nome||'—')}</b><span class="mono" style="font-size:11px;color:var(--txt-3)">${esc((a.usuario_email||'').split('@')[0]||'')}</span></td>
          <td class="mono" style="font-size:12px;color:var(--txt-3);max-width:120px;overflow:hidden;text-overflow:ellipsis" title="${esc(a.alvo||'')}">${esc(a.alvo||'—')}</td>
          <td class="mono" style="font-size:11px;color:var(--txt-4)">${esc(a.ip||'—')}</td>
        </tr>`).join('') || `<tr><td colspan="7" style="text-align:center;padding:24px">${vazio('log','Nenhum registro.')}</td></tr>`}
        </tbody></table></div>
        ${pagerHTML(r.total, r.pagina, r.por_pagina, 'audit')}`;
      ligarPager(corpo, 'audit', carregar);
    }catch(e){ corpo.innerHTML = erroBloco(e); }
  };

  $('#auQ').oninput = debounce(() => { PAG.audit = 1; carregar(); }, 350);
  $('#auAcao').onchange = () => { PAG.audit = 1; carregar(); };

  $('#auVerificar').onclick = async () => {
    const box = $('#auVerif');
    box.innerHTML = `<div class="alert alert-info" style="margin-bottom:13px">${ico('info',15)}<div>Recalculando a cadeia…</div></div>`;
    try{
      const v = await API.verificarCadeia();
      box.innerHTML = `<div class="alert alert-${v.integra ? 'ok' : 'err'}" style="margin-bottom:13px">
        ${ico(v.integra ? 'check' : 'alert', 15)}<div><b>${v.integra ? 'Cadeia íntegra' : 'Cadeia comprometida'}</b><br>${esc(v.mensagem)}</div></div>`;
    }catch(e){
      box.innerHTML = `<div class="alert alert-err" style="margin-bottom:13px">${ico('alert',15)}<div>${esc(e.message)}</div></div>`;
    }
  };

  const be = $('#auExp');
  if(be) be.onclick = async () => {
    const fin = await confirmar('Exportar trilha',
      'A exportação em si será registrada na trilha, com a justificativa informada.',
      {rotulo:'Exportar', exigeTexto:'Finalidade da exportação'});
    if(!fin) return;
    window.open(API.urlExportarAuditoria(fin), '_blank');
    toast('Exportação solicitada', 'O download começa em instantes.', 'ok');
  };

  await carregar();
}

/* ==========================================================
   CONFIGURAÇÕES
   ========================================================== */
async function renderConfiguracoes(alvo){
  const [info, saude] = await Promise.all([API.info(), API.saude()]);
  const c = info.config || {};

  alvo.innerHTML = `
  <div class="sec-hd"><h2>Configurações do sistema</h2></div>

  <div class="split" style="align-items:flex-start;margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Motores de análise</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Reconhecimento facial</dt><dd>${info.modelos && info.modelos.face ? '<span class="chip c-green">InsightFace carregado</span>' : '<span class="chip c-amber">Modelo não instalado</span>'}</dd>
          <dt>Detecção de objetos</dt><dd>${info.modelos && info.modelos.yolo ? '<span class="chip c-green">YOLO carregado</span>' : '<span class="chip c-amber">Modelo não instalado</span>'}</dd>
          <dt>Limiar global facial</dt><dd class="mono">${Math.round((c.limiar_facial||0)*100)}%</dd>
          <dt>Limiar global objeto</dt><dd class="mono">${Math.round((c.limiar_objeto||0)*100)}%</dd>
          <dt>Dimensão do vetor</dt><dd class="mono">${c.embedding_dim || 512}-D</dd>
          <dt>Dispositivo</dt><dd class="mono">${esc(c.device||'cpu')}</dd>
        </dl>
        ${!(info.modelos && info.modelos.face) ? `<div class="note" style="margin-top:14px">${ico('info',15)}<div>Sem os modelos instalados, a busca por imagem fica indisponível — o restante do sistema opera normalmente com o simulador. Para habilitar:<br><span class="mono" style="color:var(--acc-2)">pip install insightface onnxruntime ultralytics</span></div></div>` : ''}
        <div class="note" style="margin-top:12px">${ico('alert',15)}<div>O limiar é ajustado <b>por câmera</b>, na tela de configuração de cada uma. Limiares baixos aumentam falsos positivos — e, em uso policial, o custo de um falso positivo recai sobre uma pessoa inocente.</div></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Privacidade e retenção</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Retenção de não identificadas</dt><dd>${c.retencao_dias || 90} dias</dd>
          <dt>Mascaramento de PII</dt><dd>${esc((c.mascarar_para||[]).join(', ') || 'nenhum perfil')}</dd>
          <dt>Marca d'água</dt><dd>${c.watermark ? '<span class="chip c-green">Ativa</span>' : '<span class="chip c-gray">Inativa</span>'}</dd>
          <dt>Expiração de senha</dt><dd>${c.senha_expira_dias || 90} dias</dd>
          <dt>Tentativas de login</dt><dd>${c.max_tentativas || 5} antes do bloqueio</dd>
          <dt>Sessão</dt><dd>${c.sessao_minutos || 60} minutos</dd>
        </dl>
        ${API.pode('settings') ? `<button class="btn btn-soft btn-sm" id="cfgExpurgo" style="margin-top:14px">${ico('trash',13)} Expurgar capturas vencidas</button>` : ''}
        <div class="note" style="margin-top:14px">${ico('shield',15)}<div>Reconhecimento facial em segurança pública exige <b>base legal específica</b>, avaliação de impacto e supervisão. Este ambiente é uma demonstração técnica — em produção, a implantação depende das normas aplicáveis na sua jurisdição e de decisão da autoridade competente.</div></div>
      </div>
    </div>
  </div>

  <div class="split" style="align-items:flex-start;margin-bottom:14px">
    <div class="panel">
      <div class="panel-hd"><h3>Infraestrutura</h3>
        <span class="sp"></span><span class="chip ${saude.saudavel?'c-green':'c-red'}">${saude.saudavel?'Saudável':'Degradado'}</span></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Versão</dt><dd class="mono">SEGURANÇA ${esc(info.versao||'')}</dd>
          <dt>Modo</dt><dd class="mono">${esc(info.modo||'')}</dd>
          <dt>Banco</dt><dd class="mono">${esc(saude.componentes.banco)}</dd>
          <dt>Barramento</dt><dd class="mono">${esc(saude.componentes.eventos||'—')}</dd>
          <dt>WebSockets ativos</dt><dd class="mono">${saude.websockets_ativos}</dd>
          <dt>Simulador</dt><dd>${info.simulador ? `<span class="chip c-blue">Ativo · ${fmtNum(info.simulador_gerados||0)} eventos</span>` : '<span class="chip c-gray">Desligado</span>'}</dd>
          <dt>Busca vetorial</dt><dd class="mono">${esc(info.busca_vetorial||'—')}</dd>
        </dl>
      </div>
    </div>
    <div class="panel">
      <div class="panel-hd"><h3>Estatísticas</h3></div>
      <div class="panel-bd">
        <dl class="kv">
          <dt>Câmeras</dt><dd class="mono">${fmtNum((info.estatisticas||{}).cameras||0)}</dd>
          <dt>Registros biométricos</dt><dd class="mono">${fmtNum((info.estatisticas||{}).pessoas||0)}</dd>
          <dt>Detecções</dt><dd class="mono">${fmtNum((info.estatisticas||{}).deteccoes||0)}</dd>
          <dt>Alertas</dt><dd class="mono">${fmtNum((info.estatisticas||{}).alertas||0)}</dd>
          <dt>Linhas de auditoria</dt><dd class="mono">${fmtNum((info.estatisticas||{}).auditoria||0)}</dd>
          <dt>Usuários</dt><dd class="mono">${fmtNum((info.estatisticas||{}).usuarios||0)}</dd>
        </dl>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-hd"><h3>Como ligar às câmeras reais</h3><span class="sub">o que muda ao sair da demonstração</span></div>
    <div class="panel-bd">
      <p class="hint" style="max-width:76ch;margin:0 0 13px">A imagem que você vê no mosaico é gerada no navegador. Para operar com equipamentos reais, cada bloco troca de origem sem alteração na interface:</p>
      <div class="grid cols-3">
        ${[
          ['Vídeo ao vivo','As câmeras publicam RTSP; um gateway (MediaMTX) converte para WebRTC. Cada tile troca o &lt;canvas&gt; por um &lt;video&gt; apontado a <span class="mono">GET /stream/{codigo}</span>.','live'],
          ['Reconhecimento facial','RetinaFace para detectar e ArcFace para o vetor 512-D. Instale <span class="mono">insightface</span> e a busca por imagem passa a funcionar.','face'],
          ['Objetos e ações','YOLO v8 para objetos e análise de trajetórias para comportamento, com ByteTrack mantendo a identidade entre quadros.','box'],
          ['Banco','Troque <span class="mono">DATABASE_URL</span> para PostgreSQL e a busca passa a usar índice HNSW do pgvector em vez de força bruta.','db'],
          ['Barramento','Defina <span class="mono">REDIS_URL</span> e vários workers e instâncias da API passam a compartilhar o mesmo fluxo de eventos.','cog'],
          ['Worker','<span class="mono">python -m worker.run --todas</span> lê o RTSP, roda os modelos e envia para <span class="mono">POST /deteccoes/ingest</span> — a mesma rota do simulador.','gauge']
        ].map(([t,d,i]) => `<div class="id-card" style="min-width:0">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><span style="color:var(--acc)">${ico(i,15)}</span><b style="font-size:13px">${t}</b></div>
          <p class="hint" style="margin:0">${d}</p></div>`).join('')}
      </div>
    </div>
  </div>`;

  const be = $('#cfgExpurgo');
  if(be) be.onclick = async () => {
    const ok = await confirmar('Expurgar capturas vencidas',
      'Remove capturas faciais <b>sem correspondência</b> cujo prazo de retenção venceu. Capturas vinculadas a alerta ou já revisadas são preservadas.',
      {rotulo:'Expurgar', perigo:true});
    if(!ok) return;
    try{
      const r = await API.expurgar();
      toast('Expurgo concluído', `${r.removidos} registro(s) removido(s) — prazo de ${r.prazo_dias} dias.`, 'ok');
    }catch(e){ erroToast(e); }
  };
}
