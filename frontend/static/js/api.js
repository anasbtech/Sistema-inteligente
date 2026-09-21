/* ==========================================================
   VIGIA — Cliente da API

   Um único ponto de contato com o backend. Guarda os tokens,
   renova o access token quando expira e reemite a requisição,
   e traduz erro de HTTP em mensagem legível.
   ========================================================== */
'use strict';

const API = {
  base: '',
  access: null,
  refresh: null,
  usuario: null,
  _renovando: null,

  /* ---------- Sessão ---------- */
  carregarSessao(){
    try{
      const bruto = localStorage.getItem('vigia.sessao');
      if(!bruto) return false;
      const s = JSON.parse(bruto);
      this.access = s.access; this.refresh = s.refresh; this.usuario = s.usuario;
      return !!this.access;
    }catch(e){ return false; }
  },
  salvarSessao(lembrar){
    const s = {access:this.access, refresh:this.refresh, usuario:this.usuario};
    try{
      if(lembrar) localStorage.setItem('vigia.sessao', JSON.stringify(s));
      else sessionStorage.setItem('vigia.sessao', JSON.stringify(s));
    }catch(e){ /* navegação privada: a sessão vive só em memória */ }
  },
  limparSessao(){
    this.access = this.refresh = this.usuario = null;
    try{ localStorage.removeItem('vigia.sessao'); sessionStorage.removeItem('vigia.sessao'); }catch(e){}
  },
  restaurar(){
    for(const store of [sessionStorage, localStorage]){
      try{
        const bruto = store.getItem('vigia.sessao');
        if(bruto){
          const s = JSON.parse(bruto);
          this.access = s.access; this.refresh = s.refresh; this.usuario = s.usuario;
          return true;
        }
      }catch(e){}
    }
    return false;
  },
  pode(permissao){
    return !!(this.usuario && (this.usuario.permissoes||[]).includes(permissao));
  },

  /* ---------- Requisição ---------- */
  async _bruto(metodo, rota, {corpo, query, formData, texto} = {}){
    let url = this.base + rota;
    if(query){
      const qs = new URLSearchParams();
      Object.entries(query).forEach(([k,v]) => {
        if(v !== undefined && v !== null && v !== '') qs.append(k, v);
      });
      const s = qs.toString();
      if(s) url += '?' + s;
    }

    const opts = {method: metodo, headers: {}};
    if(this.access) opts.headers['Authorization'] = 'Bearer ' + this.access;
    if(formData){ opts.body = formData; }
    else if(corpo !== undefined){
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(corpo);
    }

    const r = await fetch(url, opts);

    if(r.status === 204) return null;
    if(texto) return r.ok ? await r.text() : Promise.reject(await this._erro(r));
    if(!r.ok) throw await this._erro(r);
    const tipo = r.headers.get('content-type') || '';
    return tipo.includes('application/json') ? await r.json() : await r.text();
  },

  async _erro(r){
    let detalhe = `Erro ${r.status}`;
    try{
      const j = await r.json();
      if(typeof j.detail === 'string') detalhe = j.detail;
      else if(Array.isArray(j.problemas) && j.problemas.length){
        detalhe = j.problemas.map(p => `${p.campo}: ${p.problema}`).join(' · ');
      }else if(j.detail){ detalhe = JSON.stringify(j.detail); }
    }catch(e){}
    const erro = new Error(detalhe);
    erro.status = r.status;
    return erro;
  },

  async req(metodo, rota, opts = {}){
    try{
      return await this._bruto(metodo, rota, opts);
    }catch(e){
      // 401 em rota autenticada: tenta renovar uma única vez.
      if(e.status === 401 && this.refresh && !rota.startsWith('/auth/')){
        const ok = await this._renovar();
        if(ok) return await this._bruto(metodo, rota, opts);
        this.limparSessao();
        document.dispatchEvent(new CustomEvent('vigia:sessao-expirada'));
      }
      throw e;
    }
  },

  async _renovar(){
    if(this._renovando) return this._renovando;      // evita corrida de renovações
    this._renovando = (async () => {
      try{
        const d = await this._bruto('POST', '/auth/refresh', {corpo:{refresh_token:this.refresh}});
        this.access = d.access_token; this.refresh = d.refresh_token; this.usuario = d.usuario;
        this.salvarSessao(!!localStorage.getItem('vigia.sessao'));
        return true;
      }catch(e){ return false; }
      finally{ this._renovando = null; }
    })();
    return this._renovando;
  },

  get(rota, query){ return this.req('GET', rota, {query}); },
  post(rota, corpo, query){ return this.req('POST', rota, {corpo, query}); },
  patch(rota, corpo){ return this.req('PATCH', rota, {corpo}); },
  del(rota, query){ return this.req('DELETE', rota, {query}); },

  /* ---------- Autenticação ---------- */
  async login(identificador, senha, lembrar){
    const d = await this._bruto('POST', '/auth/login', {corpo:{identificador, senha}});
    this.access = d.access_token; this.refresh = d.refresh_token; this.usuario = d.usuario;
    this.salvarSessao(lembrar);
    return d.usuario;
  },
  async logout(){
    try{ await this.post('/auth/logout'); }catch(e){}
    this.limparSessao();
  },
  eu(){ return this.get('/auth/eu'); },
  recuperar(email){ return this._bruto('POST', '/auth/recuperar', {corpo:{email}}); },
  verificarCodigo(email, codigo){ return this._bruto('POST', '/auth/verificar-codigo', {corpo:{email, codigo}}); },
  redefinirSenha(email, codigo, nova_senha){
    return this._bruto('POST', '/auth/redefinir-senha', {corpo:{email, codigo, nova_senha}});
  },

  /* ---------- Câmeras ---------- */
  cameras(q){ return this.get('/cameras', q); },
  camera(codigo){ return this.get('/cameras/' + codigo); },
  criarCamera(dados){ return this.post('/cameras', dados); },
  salvarCamera(codigo, dados){ return this.patch('/cameras/' + codigo, dados); },
  removerCamera(codigo, motivo){ return this.del('/cameras/' + codigo, {motivo}); },
  setores(){ return this.get('/cameras/setores'); },
  ptz(codigo, comando, velocidade){ return this.post(`/cameras/${codigo}/ptz`, {comando, velocidade:velocidade||0.5}); },
  saudeCamera(codigo){ return this.get(`/cameras/${codigo}/saude`); },
  streams(setor){ return this.get('/stream', {setor}); },

  /* ---------- Base biométrica ---------- */
  pessoas(q){ return this.get('/pessoas', q); },
  pessoa(id){ return this.get('/pessoas/' + id); },
  criarPessoa(dados){ return this.post('/pessoas', dados); },
  salvarPessoa(id, dados){ return this.patch('/pessoas/' + id, dados); },
  desativarPessoa(id, motivo){ return this.del('/pessoas/' + id, {motivo}); },
  esquemaIds(){ return this.get('/pessoas/esquema-ids'); },
  proximoId(sexo){ return this.get('/pessoas/proximo-id', {sexo}); },
  capturasDaPessoa(id, limite){ return this.get(`/pessoas/${id}/capturas`, {limite:limite||30}); },
  async enviarAmostra(id, arquivo){
    const fd = new FormData();
    fd.append('imagem', arquivo);
    return this.req('POST', `/pessoas/${id}/amostras`, {formData: fd});
  },
  async buscarPorImagem(arquivo, limiar){
    const fd = new FormData();
    fd.append('imagem', arquivo);
    return this.req('POST', '/pessoas/buscar', {formData: fd, query:{limiar}});
  },

  /* ---------- Detecções ---------- */
  deteccoes(q){ return this.get('/deteccoes', q); },
  deteccao(codigo){ return this.get('/deteccoes/' + codigo); },
  resumoDeteccoes(horas){ return this.get('/deteccoes/resumo', {horas:horas||24}); },
  revisar(codigo, veredito, observacao){
    return this.post(`/deteccoes/${codigo}/revisao`, {veredito, observacao:observacao||''});
  },
  expurgar(){ return this.post('/deteccoes/expurgo'); },

  /* ---------- Alertas ---------- */
  alertas(q){ return this.get('/alertas', q); },
  alerta(codigo){ return this.get('/alertas/' + codigo); },
  resumoAlertas(){ return this.get('/alertas/resumo'); },
  assumirAlerta(codigo){ return this.post(`/alertas/${codigo}/assumir`); },
  encerrarAlerta(codigo, resolucao){ return this.post(`/alertas/${codigo}/encerrar`, {resolucao}); },

  /* ---------- Usuários ---------- */
  usuarios(q){ return this.get('/usuarios', {q}); },
  perfis(){ return this.get('/usuarios/perfis'); },
  criarUsuario(dados){ return this.post('/usuarios', dados); },
  salvarUsuario(id, dados){ return this.patch('/usuarios/' + id, dados); },
  forcarTrocaSenha(id){ return this.post(`/usuarios/${id}/forcar-troca-senha`); },
  desativarUsuario(id, motivo){ return this.del('/usuarios/' + id, {motivo}); },

  /* ---------- Auditoria ---------- */
  auditoria(q){ return this.get('/auditoria', q); },
  acoesAuditoria(){ return this.get('/auditoria/acoes'); },
  verificarCadeia(){ return this.get('/auditoria/verificar'); },
  urlExportarAuditoria(finalidade){
    return `${this.base}/auditoria/exportar?finalidade=${encodeURIComponent(finalidade)}`;
  },

  /* ---------- Sistema ---------- */
  saude(){ return this.get('/saude'); },
  info(){ return this.get('/api/info'); }
};


/* ==========================================================
   WebSocket de eventos ao vivo
   ========================================================== */
const EventosAoVivo = {
  ws: null,
  ouvintes: [],
  tentativas: 0,
  pingTimer: null,
  fechandoDeProposito: false,

  conectar(){
    if(!API.access) return;
    this.fechandoDeProposito = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/eventos?token=${encodeURIComponent(API.access)}`;

    try{ this.ws = new WebSocket(url); }
    catch(e){ return this._reagendar(); }

    this.ws.onopen = () => {
      this.tentativas = 0;
      this._emitir({tipo:'conexao', dados:{estado:'conectado'}});
      clearInterval(this.pingTimer);
      this.pingTimer = setInterval(() => {
        if(this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send('ping');
      }, 25000);
    };
    this.ws.onmessage = (ev) => {
      if(ev.data === 'pong') return;
      try{ this._emitir(JSON.parse(ev.data)); }catch(e){}
    };
    this.ws.onclose = () => {
      clearInterval(this.pingTimer);
      this._emitir({tipo:'conexao', dados:{estado:'desconectado'}});
      if(!this.fechandoDeProposito) this._reagendar();
    };
    this.ws.onerror = () => { try{ this.ws.close(); }catch(e){} };
  },

  _reagendar(){
    // Espera crescente até 30s — reconectar em rajada não ajuda ninguém.
    this.tentativas++;
    const espera = Math.min(1000 * Math.pow(1.6, this.tentativas), 30000);
    setTimeout(() => { if(API.access) this.conectar(); }, espera);
  },

  desconectar(){
    this.fechandoDeProposito = true;
    clearInterval(this.pingTimer);
    if(this.ws){ try{ this.ws.close(); }catch(e){} this.ws = null; }
  },

  ao(fn){ this.ouvintes.push(fn); return () => {
    const i = this.ouvintes.indexOf(fn);
    if(i >= 0) this.ouvintes.splice(i, 1);
  }; },

  _emitir(msg){ this.ouvintes.forEach(fn => { try{ fn(msg); }catch(e){ console.error(e); } }); }
};
