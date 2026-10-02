/* ==========================================================
   SEGURANÇA — Shell da aplicação
   Autenticação, recuperação de senha, navegação e barra de status.
   ========================================================== */
'use strict';

const APP = {
  view: 'dash',
  cfg: {},          // preenchido por /api/info
  mosaico: 9,
  paginaMosaico: 0,
  camSelecionada: null,
  pausado: false,
  cameras: [],      // catálogo carregado uma vez por sessão
  relogio: null,
  periodoDesde: null,
  periodoAte: null
};

/* ==========================================================
   AUTENTICAÇÃO
   ========================================================== */
let REC = {email:'', expira:0, timer:null};

function authAlert(alvo, tipo, msg){
  const el = $(alvo);
  if(!msg){ el.innerHTML = ''; return; }
  const icone = tipo === 'ok' ? 'i-check' : tipo === 'err' ? 'i-alert' : 'i-info';
  el.innerHTML = `<div class="alert alert-${tipo}">${ico(icone,15)}<div>${msg}</div></div>`;
}

async function fazerLogin(){
  const u = $('#loginUser').value.trim();
  const p = $('#loginPass').value;
  if(!u || !p){ authAlert('#loginAlert', 'err', 'Informe e-mail/matrícula e senha.'); return; }

  const btn = $('#btnLogin');
  btn.disabled = true;
  btn.innerHTML = ico('lock',16) + ' Verificando…';
  authAlert('#loginAlert', '', '');

  try{
    // Sem "manter conectado": a sessão vive só na aba (sessionStorage).
    const usuario = await API.login(u, p, false);
    await entrarNoSistema(usuario);
  }catch(e){
    if(e.status === 428){
      authAlert('#loginAlert', 'info', 'Sua senha precisa ser redefinida. Redirecionando…');
      setTimeout(() => { $('#recEmail').value = u.includes('@') ? u : ''; mostrarRecover(); }, 900);
    }else{
      authAlert('#loginAlert', 'err', esc(e.message));
    }
  }finally{
    btn.disabled = false;
    btn.innerHTML = ico('lock',16) + ' Entrar no sistema';
  }
}

async function entrarNoSistema(usuario){
  $('#authScreen').hidden = true;
  $('#app').hidden = false;
  AUTHFX.parar();
  await montarApp(usuario);
  toast('Sessão iniciada', `${usuario.nome} · ${rotuloPerfil(usuario.perfil)}`, 'ok');
}

async function sair(){
  await API.logout();
  EventosAoVivo.desconectar();
  FEED.stop(); FEED.clear();
  clearInterval(APP.relogio);
  $('#app').hidden = true;
  $('#authScreen').hidden = false;
  $('#loginPass').value = '';
  mostrarLogin();
  AUTHFX.iniciar();
  toast('Sessão encerrada', 'Suas credenciais foram liberadas deste terminal.', 'info');
}

const rotuloPerfil = p => ({
  ADMIN:'Administrador', SUPER:'Supervisor', OPERADOR:'Operador',
  ANALISTA:'Analista', AUDITOR:'Auditor'
}[p] || p);

function mostrarLogin(){
  $('#viewLogin').hidden = false;
  $('#viewRecover').hidden = true;
  authAlert('#loginAlert', '', '');
}
function mostrarRecover(){
  $('#viewLogin').hidden = true;
  $('#viewRecover').hidden = false;
  recStep(1);
  authAlert('#recAlert', '', '');
  if(!$('#recEmail').value && $('#loginUser').value.includes('@')){
    $('#recEmail').value = $('#loginUser').value;
  }
}
function recStep(n){
  [1,2,3].forEach(i => $('#recStep' + i).hidden = i !== n);
  $$('#recStepper .step').forEach(s => {
    const i = +s.dataset.step;
    s.classList.toggle('active', i === n);
    s.classList.toggle('done', i < n);
    s.querySelector('.step-dot').innerHTML = i < n ? ico('check',12) : i;
  });
  $$('#recStepper .step-bar').forEach((b, i) => b.classList.toggle('done', i < n - 1));
}

/* ---------- Etapa 1 ---------- */
async function enviarCodigo(){
  const e = $('#recEmail').value.trim().toLowerCase();
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e)){
    authAlert('#recAlert', 'err', 'Informe um e-mail funcional válido.'); return;
  }
  const btn = $('#btnRecSend');
  btn.disabled = true;
  try{
    const r = await API.recuperar(e);
    REC.email = e;
    REC.expira = Date.now() + (r.expira_em_segundos || 600) * 1000;
    $('#recMailEcho').textContent = e;
    $$('.otp').forEach(o => { o.value = ''; o.classList.remove('filled'); });
    recStep(2);
    iniciarTimerOTP();
    // A resposta é a mesma exista ou não a conta — do contrário o endpoint
    // viraria um oráculo de e-mails válidos.
    let msg = esc(r.mensagem);
    if(r.codigo_debug){
      msg += `<br><span class="mono" style="font-size:11px;color:var(--txt-3)">Ambiente de desenvolvimento — código gerado: <b style="color:var(--acc-2)">${esc(r.codigo_debug)}</b></span>`;
    }
    authAlert('#recAlert', 'info', msg);
    setTimeout(() => $('#otp0').focus(), 120);
  }catch(err){
    authAlert('#recAlert', 'err', esc(err.message));
  }finally{ btn.disabled = false; }
}

function iniciarTimerOTP(){
  clearInterval(REC.timer);
  REC.timer = setInterval(() => {
    const s = Math.max(0, Math.floor((REC.expira - Date.now()) / 1000));
    const el = $('#otpTimer');
    if(el) el.textContent = `${pad(Math.floor(s/60))}:${pad(s%60)}`;
    if(s <= 0){ clearInterval(REC.timer); if(el){ el.textContent = '00:00'; el.style.color = 'var(--red)'; } }
  }, 1000);
}

/* ---------- Etapa 2 ---------- */
async function verificarCodigo(){
  const cod = $$('.otp').map(o => o.value).join('');
  if(cod.length < 6){ authAlert('#recAlert', 'err', 'Digite os 6 dígitos do código.'); return; }
  const btn = $('#btnRecVerify');
  btn.disabled = true;
  try{
    await API.verificarCodigo(REC.email, cod);
    clearInterval(REC.timer);
    authAlert('#recAlert', 'ok', 'Código validado. Defina sua nova senha.');
    recStep(3);
    setTimeout(() => $('#recPass').focus(), 120);
  }catch(e){
    authAlert('#recAlert', 'err', esc(e.message));
    if(/novo código|expirad/i.test(e.message)) recStep(1);
  }finally{ btn.disabled = false; }
}

/* ---------- Etapa 3 ---------- */
function forcaSenha(p){
  const r = {
    len: p.length >= 10, upper: /[A-Z]/.test(p), lower: /[a-z]/.test(p),
    num: /[0-9]/.test(p), sym: /[^A-Za-z0-9]/.test(p)
  };
  let s = Object.values(r).filter(Boolean).length;
  if(p.length >= 14) s = Math.min(5, s + 1);
  return {score: Math.max(0, s - 1), regras: r};
}
function atualizarMedidor(){
  const p = $('#recPass').value;
  const {score, regras} = forcaSenha(p);
  const cores = ['var(--red)','var(--amber)','var(--amber)','var(--green)'];
  $$('#pwMeter i').forEach((b, i) =>
    b.style.background = (p && i < score) ? cores[Math.min(score-1, 3)] : 'var(--ink-700)');
  $$('#pwRules .pw-rule').forEach(r => r.classList.toggle('ok', !!regras[r.dataset.rule]));
}
async function salvarSenha(){
  const p = $('#recPass').value, p2 = $('#recPass2').value;
  const {score, regras} = forcaSenha(p);
  if(!Object.values(regras).every(Boolean)){
    authAlert('#recAlert', 'err', 'A senha não atende a todos os requisitos de complexidade.'); return;
  }
  if(p !== p2){ authAlert('#recAlert', 'err', 'As senhas não conferem.'); return; }
  if(score < 3){ authAlert('#recAlert', 'err', 'Escolha uma senha mais forte.'); return; }

  const cod = $$('.otp').map(o => o.value).join('');
  const btn = $('#btnRecSave');
  btn.disabled = true;
  try{
    await API.redefinirSenha(REC.email, cod, p);
    authAlert('#recAlert', 'ok', 'Senha redefinida. Entrando no sistema…');
    const usuario = await API.login(REC.email, p, false);
    setTimeout(async () => { mostrarLogin(); await entrarNoSistema(usuario); }, 700);
  }catch(e){
    authAlert('#recAlert', 'err', esc(e.message));
  }finally{ btn.disabled = false; }
}

/* ---------- Campo OTP ---------- */
$$('.otp').forEach((o, i, arr) => {
  o.addEventListener('input', () => {
    o.value = o.value.replace(/\D/g, '').slice(0, 1);
    o.classList.toggle('filled', !!o.value);
    if(o.value && i < arr.length - 1) arr[i+1].focus();
    if(arr.every(x => x.value)) setTimeout(verificarCodigo, 150);
  });
  o.addEventListener('keydown', e => {
    if(e.key === 'Backspace' && !o.value && i > 0){
      arr[i-1].focus(); arr[i-1].value = ''; arr[i-1].classList.remove('filled');
    }
    if(e.key === 'ArrowLeft'  && i > 0) arr[i-1].focus();
    if(e.key === 'ArrowRight' && i < arr.length - 1) arr[i+1].focus();
  });
  o.addEventListener('paste', e => {
    e.preventDefault();
    const d = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6);
    d.split('').forEach((c, k) => { if(arr[k]){ arr[k].value = c; arr[k].classList.add('filled'); } });
    if(d.length === 6) setTimeout(verificarCodigo, 150);
  });
});

$('#btnLogin').onclick = fazerLogin;
$('#loginPass').addEventListener('keydown', e => { if(e.key === 'Enter') fazerLogin(); });
$('#loginUser').addEventListener('keydown', e => { if(e.key === 'Enter') $('#loginPass').focus(); });
$('#goRecover').onclick = mostrarRecover;
$('#btnRecBack1').onclick = mostrarLogin;
$('#btnRecBack2').onclick = () => { clearInterval(REC.timer); recStep(1); };
$('#btnRecSend').onclick = enviarCodigo;
$('#recEmail').addEventListener('keydown', e => { if(e.key === 'Enter') enviarCodigo(); });
$('#btnResend').onclick = enviarCodigo;
$('#btnRecVerify').onclick = verificarCodigo;
$('#btnRecSave').onclick = salvarSenha;
$('#recPass').addEventListener('input', atualizarMedidor);
$('#recPass2').addEventListener('keydown', e => { if(e.key === 'Enter') salvarSenha(); });
/* ---------- Fundo animado do login ----------
   Um <canvas> aceita um único tipo de contexto: com o 2D tomado aqui, o
   WebGL do Three.js voltaria null. O desenho fica em three-auth.js. */
const AUTHFX = {
  iniciar(){ window.AuthFX3D && window.AuthFX3D.iniciar(); indicadoresDaMarca(); },
  parar(){ window.AuthFX3D && window.AuthFX3D.parar(); }
};

/* Os três números da coluna da marca.
   São indicadores do serviço — disponibilidade, componentes de pé e prazo de
   retenção. Aqui ficava a contagem de registros biométricos, que dizia a quem
   ainda não autenticou quantas pessoas a base monitora; a capacidade instalada
   do sistema é o que cabe nesta tela, não o volume de gente dentro dele. */
async function indicadoresDaMarca(){
  const alvo = $('#statUptime');
  if(!alvo) return;
  const pôr = (id, v) => { const el = $('#' + id); if(el) el.textContent = v; };
  try{
    const [info, saude] = await Promise.all([
      fetch('/api/info', {cache:'no-store'}).then(r => r.json()),
      fetch('/saude',    {cache:'no-store'}).then(r => r.json())
    ]);
    const comps = Object.values(saude.componentes || {});
    const oks = comps.filter(v => /^(ok|mem)/i.test(String(v))).length;
    pôr('statUptime', (info.estatisticas || {}).disponibilidade || '—');
    pôr('statComp', comps.length ? `${oks}/${comps.length}` : '—');
    pôr('statRet', (info.config || {}).retencao_dias ?? '—');
  }catch(e){
    ['statUptime','statComp','statRet'].forEach(id => pôr(id, '—'));
  }
}

/* ==========================================================
   SHELL
   ========================================================== */
const VIEWS = {
  dash:       {t:'Dashboard operacional', s:'Visão geral em tempo real',              perm:'live'},
  live:       {t:'Ao vivo', s:'Mosaico de câmeras e eventos',           perm:'live'},
  cams:       {t:'Câmeras',               s:'Cadastro, status e parâmetros de análise',perm:'live'},
  facial:     {t:'Reconhecimento facial', s:'Registros de identificação biométrica',  perm:'facial'},
  objects:    {t:'Detecção de objetos',   s:'Classificação de objetos por câmera',    perm:'facial'},
  actions:    {t:'Análise de ações',      s:'Eventos comportamentais detectados',     perm:'facial'},
  biometrics: {t:'Base biométrica',       s:'Registros com identificador permanente', perm:'biometrics'},
  alerts:     {t:'Alertas',               s:'Ocorrências que exigem tratamento',      perm:'alerts'},
  users:      {t:'Usuários e perfis',     s:'Controle de acesso e permissões',        perm:'users'},
  audit:      {t:'Trilha de auditoria',   s:'Registro imutável de operações',         perm:'audit'},
  settings:   {t:'Configurações',         s:'Parâmetros do sistema e da análise',     perm:'settings'}
};

async function montarApp(usuario){
  $('#usrName').textContent = usuario.nome;
  $('#usrRole').textContent = rotuloPerfil(usuario.perfil);
  $('#usrAvatar').textContent = iniciais(usuario.nome);

  $$('.nav-item').forEach(b => {
    const v = b.dataset.view;
    const perm = VIEWS[v] ? VIEWS[v].perm : null;
    b.style.display = (perm && !API.pode(perm)) ? 'none' : '';
    b.onclick = () => { irPara(v); document.body.classList.remove('nav-open'); };
  });

  try{ APP.cfg = await API.info(); }catch(e){ APP.cfg = {}; }

  FEED.watermark = false;
  FEED.aoMedirFps = fps => { const e = $('#mFps'); if(e) e.textContent = fps; };
  // O token vai na URL porque <img> não envia cabeçalho. Se a câmera não
  // responder, o onerror do próprio <img> devolve o tile à cena sintética.
  FEED.fonteAoVivo = (cam, opts) => cam.status === 'ONLINE'
    ? `${API.base}/stream/${encodeURIComponent(cam.codigo)}/mjpeg?token=${encodeURIComponent(API.access)}`
      + ((opts && opts.hd) ? '&hd=1' : '')
    : null;
  FEED.fonteSnapshot = cam => cam.status === 'ONLINE'
    ? `${API.base}/stream/${encodeURIComponent(cam.codigo)}/snapshot?token=${encodeURIComponent(API.access)}`
    : null;

  await carregarCatalogoCameras();
  iniciarRelogio();
  EventosAoVivo.conectar();

  const inicial = API.pode('live') ? 'dash' : (API.pode('audit') ? 'audit' : 'alerts');
  await irPara(inicial);
  atualizarContadores();
}

const iniciais = nome => String(nome || '').split(' ')
  .filter(w => w.length > 2).slice(-2).map(w => w[0]).join('').toUpperCase().slice(0, 2) || '–';

async function carregarCatalogoCameras(){
  try{ APP.cameras = await API.cameras(); }
  catch(e){ APP.cameras = []; }
}
const camPorCodigo = c => APP.cameras.find(x => x.codigo === c);

async function irPara(v){
  if(!VIEWS[v]) v = 'dash';
  if(VIEWS[v].perm && !API.pode(VIEWS[v].perm)){
    toast('Acesso negado',
      `Seu perfil (${rotuloPerfil(API.usuario.perfil)}) não possui esta permissão.`, 'err');
    return;
  }
  APP.view = v;
  FEED.clear();
  $$('.nav-item').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('on', s.id === 'v-' + v));
  $('#tbTitle').textContent = VIEWS[v].t;
  $('#tbSub').textContent = VIEWS[v].s;
  $('#main').scrollTop = 0;
  location.hash = v;

  const render = {
    dash: renderPainel, live: renderAoVivo, cams: renderCameras,
    facial: renderFacial, objects: renderObjetos, actions: renderAcoes,
    biometrics: renderBiometria, alerts: renderAlertas, users: renderUsuarios,
    audit: renderAuditoria, settings: renderConfiguracoes
  }[v];

  const alvo = $('#v-' + v);
  alvo.innerHTML = carregando();
  try{ await render(alvo); }
  catch(e){
    alvo.innerHTML = erroBloco(e, 'btnRetry');
    const r = $('#btnRetry'); if(r) r.onclick = () => irPara(v);
  }
}

/* ---------- Barra superior ---------- */
function iniciarRelogio(){
  const tick = () => {
    const d = new Date();
    $('#tbTime').textContent = fmtTime(d);
    $('#tbDate').textContent = d.toLocaleDateString('pt-BR',
      {weekday:'short', day:'2-digit', month:'short'}).replace('.', '');
  };
  tick();
  clearInterval(APP.relogio);
  APP.relogio = setInterval(tick, 1000);
}

async function atualizarContadores(){
  try{
    const [resumoA, resumoD, bio] = await Promise.all([
      API.pode('alerts') ? API.resumoAlertas() : Promise.resolve(null),
      API.pode('facial') ? API.resumoDeteccoes(24) : Promise.resolve(null),
      API.pode('biometrics') ? API.pessoas({por_pagina: 1}) : Promise.resolve(null)
    ]);
    const online = APP.cameras.filter(c => c.status === 'ONLINE').length;
    $('#mCams').textContent = online;
    $('#mCamsTot').textContent = APP.cameras.length;
    $('#navLive').textContent = online;
    $('#navCams').textContent = APP.cameras.length;

    if(resumoA){
      const abertos = (resumoA.abertos || 0) + (resumoA.em_andamento || 0);
      $('#mAlerts').textContent = abertos;
      $('#navAlerts').textContent = abertos;
      const badge = $('#bellBadge');
      badge.textContent = abertos;
      badge.style.display = abertos ? 'grid' : 'none';
    }
    if(bio) $('#navBio').textContent = fmtNum(bio.total);
    if(resumoD){
      const t = resumoD.por_tipo || {};
      $('#navFacial').textContent = fmtNum(t.FACIAL || 0);
      $('#navObj').textContent = fmtNum(t.OBJETO || 0);
      $('#navAct').textContent = fmtNum(t.ACAO || 0);
    }
  }catch(e){ /* contadores são informativos: falha não interrompe a operação */ }
}

/* ---------- Eventos ao vivo ---------- */
EventosAoVivo.ao(msg => {
  if(msg.tipo === 'conexao'){
    const d = $('#wsDot');
    if(d) d.className = 'dot ' + (msg.dados.estado === 'conectado' ? 'g pulse' : 'r');
    return;
  }
  if(msg.tipo === 'deteccao'){
    const d = msg.dados;
    if(d.alerta && d.severidade >= 4){
      toast('⚠ Alerta crítico', `${d.alerta.titulo} · ${d.camera_nome}`, 'err', 7000);
    }
    if(APP.view === 'live')  aoVivoNovoEvento(d);
    if(APP.view === 'dash')  painelNovoEvento(d);
    atualizarContadoresDebounced();
  }
  if(msg.tipo === 'quadro_ao_vivo'){
    // Sobreposição do mosaico: a cena atual, que não vira registro.
    FEED.definirCaixas(msg.dados.camera_codigo, msg.dados.caixas, msg.dados.validade_s);
  }
  if(msg.tipo === 'identificacao'){
    // Pessoa identificada: renderizar o nome sobre a câmera
    FEED.adicionarIdentificacao(msg.dados.camera_codigo, msg.dados);
  }
  if(msg.tipo === 'alerta_atualizado'){
    atualizarContadoresDebounced();
    if(APP.view === 'alerts') irPara('alerts');
  }
});
const atualizarContadoresDebounced = debounce(atualizarContadores, 2500);

document.addEventListener('seguranca:sessao-expirada', () => {
  toast('Sessão expirada', 'Autentique-se novamente para continuar.', 'warn', 6000);
  EventosAoVivo.desconectar();
  FEED.stop();
  $('#app').hidden = true;
  $('#authScreen').hidden = false;
  mostrarLogin();
  AUTHFX.iniciar();
});

/* ---------- Controles do shell ---------- */
$('#navToggle').onclick = () => document.body.classList.toggle('nav-open');
$('#navScrim').onclick  = () => document.body.classList.remove('nav-open');
$('#btnLogout').onclick = sair;
$('#bellBtn').onclick   = () => irPara('alerts');
$('#usrBtn').onclick    = abrirPerfil;

async function abrirPerfil(){
  const u = API.usuario;
  abrirModal(`
    <div class="modal-hd"><h3>Sessão ativa</h3><span class="sp" style="flex:1"></span>
      <button class="x" data-close>${ico('x',17)}</button></div>
    <div class="modal-bd">
      <div style="display:flex;gap:14px;align-items:center;margin-bottom:16px">
        <span class="avatar" style="width:52px;height:52px;font-size:18px">${esc(iniciais(u.nome))}</span>
        <div><b style="font-size:16px;display:block">${esc(u.nome)}</b>
        <span class="chip c-blue" style="margin-top:4px">${esc(rotuloPerfil(u.perfil))}</span></div>
      </div>
      <dl class="kv">
        <dt>Matrícula</dt><dd class="mono">${esc(u.matricula)}</dd>
        <dt>E-mail</dt><dd class="mono">${esc(u.email)}</dd>
        <dt>Unidade</dt><dd>${esc(u.unidade || '—')}</dd>
        <dt>MFA</dt><dd>${u.mfa_ativo ? '<span class="chip c-green">Ativo</span>' : '<span class="chip c-amber">Inativo</span>'}</dd>
        <dt>Último acesso</dt><dd class="mono">${u.ultimo_acesso ? fmtDT(u.ultimo_acesso) : '—'}</dd>
        <dt>Permissões</dt><dd style="display:flex;gap:4px;flex-wrap:wrap">${(u.permissoes||[]).map(p => `<span class="chip c-gray">${esc(p)}</span>`).join('')}</dd>
      </dl>
      <div class="note" style="margin-top:16px">${ico('info',15)}<div>Toda consulta a registro biométrico feita nesta sessão fica vinculada à sua matrícula na trilha de auditoria.</div></div>
    </div>
    <div class="modal-ft">
      <button class="btn btn-ghost" data-close>Fechar</button>
      <button class="btn btn-danger" id="mLogout">${ico('logout',15)} Encerrar sessão</button>
    </div>`);
  $('#mLogout').onclick = () => { fecharModal(); sair(); };
}

/* ---------- Atalhos ---------- */
document.addEventListener('keydown', e => {
  if(!API.usuario || $('#app').hidden) return;
  if(e.target.matches('input, textarea, select')) return;
  const mapa = {'1':'dash','2':'live','3':'cams','4':'facial','5':'objects',
                '6':'actions','7':'biometrics','8':'alerts'};
  if(mapa[e.key]) irPara(mapa[e.key]);
  if(e.key === '/'){
    e.preventDefault();
    const s = document.querySelector('.view.on .search .inp');
    if(s) s.focus();
  }
});

document.addEventListener('visibilitychange', () => {
  if(!document.hidden && API.usuario && !APP.pausado && APP.view === 'live') FEED.start();
});

/* ==========================================================
   BOOT
   ========================================================== */
(async function iniciar(){
  AUTHFX.iniciar();

  // Sessão salva? Valida contra o servidor antes de confiar nela.
  if(API.restaurar()){
    try{
      const u = await API.eu();
      API.usuario = u;
      await entrarNoSistema(u);
      const h = location.hash.replace('#','');
      if(h && VIEWS[h]) await irPara(h);
      return;
    }catch(e){ API.limparSessao(); }
  }

  mostrarLogin();
  setTimeout(() => $('#loginUser').focus(), 300);
})();

